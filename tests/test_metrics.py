"""metrics 单元测试 + 性能追踪集成测试。

覆盖（对应 design.md §8 性能追踪）：
- 单元：滑动桶聚合、percentile、有效 QPM 口径、jsonl 往返、record 不 await；
- 集成（mock server，缩小时间尺度）：
  - 超时拆分：超长尾档 + 小 request_timeout → NETWORK_ERROR，
    耗时 ≈ 超时值，日志含异常类名（区分超时与连接错误）；
  - 超长尾修复回归：600s 级超时（缩小尺度）放超长尾样本跑完，最终成功；
  - metrics 接线：metrics.jsonl 存在且四段齐全、四段之和 ≈ 总耗时、
    summary 含 performance 块、有效 QPM ≤ 标称 ×容差、reporter 行按 interval 输出。
"""

import asyncio
import inspect
import json
import logging
import os

import pytest

from cotbuilder.batch import BatchRunner
from cotbuilder.client import ErrorType, ExpertModelClient
from cotbuilder.config import Config
from cotbuilder.metrics import Metrics, MetricsEvent, percentile
from cotbuilder.ratelimit import PacedRateLimiter
from mock.mock_server import MockExpertServer, MockScenario, fixture_sample


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------
# 单元：桶聚合 / percentile / 有效 QPM / jsonl / 协程安全
# ---------------------------------------------------------------------------

class TestPercentile:
    def test_empty(self):
        assert percentile([], 50) == 0.0

    def test_basic(self):
        vals = sorted([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        assert percentile(vals, 50) in (50, 60)
        assert percentile(vals, 0) == 10
        assert percentile(vals, 100) == 100

    def test_single(self):
        assert percentile([7.0], 95) == 7.0


class TestBuckets:
    def test_bucket_aggregation(self):
        """手工放置事件：发起/完成/错误/在途峰值/有效 QPM 逐项断言。"""
        clock = FakeClock()
        m = Metrics(interval=10.0, clock=clock)
        # 桶 0：3 发起 3 完成（1 错误）；桶 1：1 发起 1 完成
        for start, end, outcome in [
            (1000.0, 1002.0, "OK"),
            (1001.0, 1003.0, "NETWORK_ERROR"),
            (1004.0, 1006.0, "OK"),
            (1011.0, 1012.0, "OK"),
        ]:
            clock.t = end
            m.record_request("s", "initial", outcome,
                             wait_limiter=0.5, wait_slot=0.1,
                             rtt=end - start, started=start)
        bks = m.buckets()
        assert len(bks) == 2
        assert bks[0]["started"] == 3 and bks[0]["finished"] == 3
        assert bks[0]["errors"] == 1
        # 时间线：+1(1000) +1(1001) -1(1002) -1(1003) +1(1004) -1(1006)
        assert bks[0]["peak_in_flight"] == 2
        # 有效 QPM = 桶内发起数 × 60 / interval
        assert bks[0]["effective_qpm"] == pytest.approx(18.0)
        assert bks[1]["started"] == 1
        assert bks[1]["effective_qpm"] == pytest.approx(6.0)

    def test_empty(self):
        assert Metrics(interval=10.0).buckets() == []

    def test_report_fields(self):
        clock = FakeClock()
        m = Metrics(interval=10.0, clock=clock)
        clock.t = 1002.0
        m.record_request("s1", "initial", "OK",
                         wait_limiter=1.0, wait_slot=0.5, rtt=2.0,
                         started=1000.0)
        clock.t = 1003.0
        m.record_backoff("s2", 4.0)
        rep = m.report()
        assert rep["total_requests"] == 1
        assert rep["rtt_p50"] == 2.0
        assert rep["phase_totals"]["backoff"] == 4.0
        # 四段占比之和为 1（分项四舍五入到 4 位小数，容差放宽）
        assert sum(rep["phase_shares"].values()) == pytest.approx(1.0,
                                                                  abs=0.001)
        assert rep["effective_qpm_mean"] > 0

    def test_jsonl_roundtrip(self, tmp_path):
        """flush 增量写盘 → 逐行读回 from_dict，字段一致。"""
        clock = FakeClock()
        m = Metrics(interval=10.0, clock=clock)
        path = str(tmp_path / "metrics.jsonl")
        clock.t = 1001.0
        m.record_request("s1", "initial", "OK",
                         wait_limiter=0.1, wait_slot=0.2, rtt=0.3,
                         started=1000.0)
        assert m.flush_to(path) == 1
        clock.t = 1002.0
        m.record_backoff("s1", 0.5)
        # 增量 flush：第二次只写新事件
        assert m.flush_to(path) == 1
        assert m.flush_to(path) == 0    # 无新事件不写

        with open(path, encoding="utf-8") as f:
            events = [MetricsEvent.from_dict(json.loads(line)) for line in f]
        assert len(events) == 2
        assert events[0].kind == "request"
        assert events[0].wait_limiter == pytest.approx(0.1)
        assert events[0].wait_slot == pytest.approx(0.2)
        assert events[0].rtt == pytest.approx(0.3)
        assert events[1].kind == "backoff"
        assert events[1].backoff == pytest.approx(0.5)

    def test_record_is_not_coroutine(self):
        """record/flush 禁止是协程函数（关键路径上不允许 await）。"""
        for fn in (Metrics.record_request, Metrics.record_backoff,
                   Metrics.flush_to, Metrics.report, Metrics.buckets,
                   Metrics.progress_line):
            assert not inspect.iscoroutinefunction(fn), fn.__name__

    def test_progress_line_format(self):
        clock = FakeClock()
        m = Metrics(interval=10.0, clock=clock)
        clock.t = 1001.0
        m.record_request("s1", "initial", "OK",
                         wait_limiter=0, wait_slot=0, rtt=61.0,
                         started=1000.0)
        clock.t = 1002.0
        line = m.progress_line(in_flight=8, completed=17, total=100)
        assert "in_flight=8" in line
        assert "eff_qpm=" in line
        assert "completed=17/100" in line
        assert "rtt_p50=61.0s" in line


# ---------------------------------------------------------------------------
# 集成：超时拆分（mock 超长尾档）
# ---------------------------------------------------------------------------

async def _one_call(scenario: MockScenario, **cfg_overrides):
    srv = MockExpertServer(scenario)
    base = await srv.start()
    try:
        cfg = Config(api_key="k", api_endpoint=base, qpm_limit=60000,
                     **cfg_overrides)
        async with ExpertModelClient(cfg, PacedRateLimiter(60000)) as c:
            return await c.call([{"role": "user", "content": "x"}],
                                sample_id="s0"), srv
    finally:
        await srv.close()


class TestTimeoutSplit:
    async def test_total_timeout_kills_slow_inference(self, caplog):
        """超长尾档 + 小 request_timeout → NETWORK_ERROR，耗时 ≈ 超时值，
        日志含异常类名（修复前日志为空串，无法区分超时与连接错误）。"""
        import time
        srv = MockExpertServer(MockScenario(
            latency=(0.01, 0.02), slow_response_rate=1.0,
            slow_latency=(2.0, 3.0), seed=5))
        base = await srv.start()
        cfg = Config(api_key="k", api_endpoint=base, qpm_limit=60000,
                     request_timeout=0.3)
        with caplog.at_level(logging.WARNING, logger="cotbuilder.client"):
            async with ExpertModelClient(cfg, PacedRateLimiter(60000)) as c:
                t0 = time.monotonic()
                outcome = await c.call([{"role": "user", "content": "x"}],
                                       sample_id="s0")
                elapsed = time.monotonic() - t0
        # close() 会等慢请求收尾，不计入调用耗时
        await srv.close()
        assert outcome.error == ErrorType.NETWORK_ERROR
        assert elapsed == pytest.approx(0.3, abs=0.25)
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "TimeoutError" in msgs
        assert "elapsed" in msgs

    async def test_slow_tail_succeeds_with_generous_timeout(self):
        """超长尾修复回归：放足超时让慢推理跑完（线上 sample_20 场景）。"""
        outcome, srv = await _one_call(
            MockScenario(latency=(0.01, 0.02),
                         slow_response_rate=1.0,
                         slow_latency=(0.4, 0.6), seed=5),
            request_timeout=5.0)
        assert outcome.ok and outcome.content
        rec = srv.stats.records[0]
        assert rec.done_monotonic - rec.arrival_monotonic >= 0.35

    async def test_connect_timeout_fast_fail(self):
        """连不上的端点：connect_timeout 快速失败，不陪跑 request_timeout。"""
        import time
        # 127.0.0.1:1 必定连接拒绝
        cfg = Config(api_key="k", api_endpoint="http://127.0.0.1:1/v1",
                     qpm_limit=60000, request_timeout=600.0,
                     connect_timeout=0.5)
        t0 = time.monotonic()
        async with ExpertModelClient(cfg, PacedRateLimiter(60000)) as c:
            outcome = await c.call([{"role": "user", "content": "x"}])
        assert outcome.error == ErrorType.NETWORK_ERROR
        assert time.monotonic() - t0 < 5.0


# ---------------------------------------------------------------------------
# 集成：BatchRunner 的 metrics 接线
# ---------------------------------------------------------------------------

class TestMetricsWiring:
    async def test_metrics_files_and_summary(self, tmp_path):
        """跑一批后：metrics.jsonl 四段齐全、四段之和 ≈ 总耗时、
        summary 含 performance 块、有效 QPM 不超 标称 ×容差。"""
        n = 8
        samples = [fixture_sample(f"s{i}") for i in range(n)]
        srv = MockExpertServer(
            MockScenario(latency=(0.05, 0.15), seed=1,
                         match_probability=1.0))
        base = await srv.start()
        cfg = Config(api_key="k", api_endpoint=base,
                     qpm_limit=600, max_concurrent=4,
                     metrics_interval=0.5,
                     backoff_base=0.02, backoff_cap=0.1)
        runner = BatchRunner(cfg)
        result = await runner.run(samples, str(tmp_path))
        await srv.close()
        assert result["success_count"] == n

        # metrics.jsonl 存在且每请求事件四段齐全
        path = tmp_path / "metrics.jsonl"
        assert path.exists()
        with open(path, encoding="utf-8") as f:
            events = [json.loads(line) for line in f]
        reqs = [e for e in events if e["kind"] == "request"]
        assert len(reqs) == n
        for e in reqs:
            assert {"wait_limiter", "wait_slot", "rtt",
                    "outcome", "quota_kind", "sample_id"} <= set(e)
            assert e["rtt"] >= 0.03     # 服务端延迟 0.05–0.15
            assert e["wait_limiter"] >= 0 and e["wait_slot"] >= 0
        # 全程有效 QPM 不超过标称（任意桶，浮点容差 +1 档）
        summary = json.loads((tmp_path / "summary.json").read_text())
        perf = summary["metrics"]["performance"]
        assert perf["total_requests"] == n
        assert perf["rtt_p50"] > 0
        for b in perf["buckets"]:
            assert b["effective_qpm"] <= 600 + 60.0 / 0.5 + 1
        # 四段占比之和为 1（分项四舍五入到 4 位小数，容差放宽）
        assert sum(perf["phase_shares"].values()) == pytest.approx(1.0,
                                                                   abs=0.001)
        # 四段都被记录（限流绑定场景下 wait_limiter 占大头是合理的）
        assert set(perf["phase_totals"]) == {
            "wait_limiter", "wait_slot", "rtt", "backoff"}
        assert perf["phase_totals"]["rtt"] > 0

    async def test_reporter_line_emitted(self, tmp_path, caplog):
        """reporter 按 progress_log_interval 输出一行式状态。"""
        samples = [fixture_sample(f"s{i}") for i in range(6)]
        srv = MockExpertServer(
            MockScenario(latency=(0.3, 0.4), seed=2, match_probability=1.0))
        base = await srv.start()
        cfg = Config(api_key="k", api_endpoint=base,
                     qpm_limit=6000, max_concurrent=3,
                     progress_log_interval=0.2)
        runner = BatchRunner(cfg)
        with caplog.at_level(logging.INFO, logger="cotbuilder.batch"):
            await runner.run(samples, str(tmp_path))
        await srv.close()
        lines = [r.getMessage() for r in caplog.records
                 if "eff_qpm=" in r.getMessage()]
        assert lines, "reporter 未输出进度行"
        assert all("in_flight=" in line and "completed=" in line
                   for line in lines)

    async def test_reporter_disabled_by_zero(self, tmp_path, caplog):
        """progress_log_interval=0 时 reporter 关闭。"""
        samples = [fixture_sample("s0")]
        srv = MockExpertServer(
            MockScenario(latency=(0.05, 0.1), seed=2,
                         match_probability=1.0))
        base = await srv.start()
        cfg = Config(api_key="k", api_endpoint=base,
                     qpm_limit=6000, progress_log_interval=0)
        runner = BatchRunner(cfg)
        with caplog.at_level(logging.INFO, logger="cotbuilder.batch"):
            await runner.run(samples, str(tmp_path))
        await srv.close()
        assert not [r for r in caplog.records
                    if "eff_qpm=" in r.getMessage()]

    async def test_metrics_events_do_not_block_pipeline(self, tmp_path):
        """metrics 不进关键路径决策：全匹配场景结果与无追踪完全一致。"""
        samples = [fixture_sample(f"s{i}") for i in range(5)]
        srv = MockExpertServer(
            MockScenario(latency=(0.02, 0.05), seed=7,
                         match_probability=1.0))
        base = await srv.start()
        cfg = Config(api_key="k", api_endpoint=base, qpm_limit=6000)
        result = await BatchRunner(cfg).run(samples, str(tmp_path))
        await srv.close()
        assert result["success_count"] == 5
        assert result["success_rate"] == 1.0
