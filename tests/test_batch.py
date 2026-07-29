"""batch 端到端指标测试（mock server，缩小时间尺度）。

验收指标（audit-01 附录 A.5 / B.3、R3、老代码问题清单）：
- 请求数守恒：arrivals == Σattempts == quota 三桶之和；initial == 样本数
- 时间包络：墙钟 ≤ 1.5× 理论值
- 并发不劣化：确定性 mock 下 max_concurrent=1 vs 10 结果精确相等
- 403 风暴恢复：无同步突发、恢复稳态、最终全部完成
- 断点恢复：cancel 后重跑跳过已处理、无重复
- success_rate 分母不含 skipped
- 样本内串行：max_per_sample_in_flight ≤ 1（每个 e2e 测试统一断言）
"""

import asyncio
import json
import math
import time

import pytest

from cotbuilder.batch import BatchRunner
from cotbuilder.config import Config
from mock.mock_server import (
    CANONICAL_DOC, MISMATCH_DOC, MockExpertServer, MockScenario,
    fixture_sample,
)

FAST_BACKOFF = {"backoff_base": 0.02, "backoff_cap": 0.1}


async def run_batch(scenario: MockScenario, samples, out_dir,
                    **cfg_overrides):
    """起 mock + 跑一批，返回 (server, runner, result, wall_seconds)。"""
    srv = MockExpertServer(scenario)
    base = await srv.start()
    cfg = Config(api_key="k", api_endpoint=base, **FAST_BACKOFF,
                 **cfg_overrides)
    runner = BatchRunner(cfg)
    t0 = time.monotonic()
    result = await runner.run(samples, str(out_dir))
    wall = time.monotonic() - t0
    await srv.close()
    return srv, runner, result, wall


def bucket_violations(arrivals, qpm):
    """任意 1s 桶内到达数超过 ⌈qpm/60⌉+1 的时刻列表。"""
    bound = math.ceil(qpm / 60) + 1
    bad = []
    for t in arrivals:
        if sum(1 for a in arrivals if t <= a < t + 1.0) > bound:
            bad.append(t)
    return bad


class TestHappyPath:
    async def test_envelope_and_conservation(self, tmp_path):
        """30 样本全匹配：请求数守恒 + 时间包络 + 并发/串行不变量。"""
        n, qpm, conc = 30, 600, 10     # qpm 600 → Δ=0.1s
        samples = [fixture_sample(f"s{i}") for i in range(n)]
        srv, runner, result, wall = await run_batch(
            MockScenario(latency=(0.05, 0.15), seed=1, match_probability=1.0),
            samples, tmp_path,
            qpm_limit=qpm, max_concurrent=conc)

        assert result["success_count"] == n
        assert result["success_rate"] == 1.0

        # 请求数守恒：零错误场景每样本恰好 1 次
        arrivals = [r.arrival_monotonic for r in srv.stats.records]
        assert len(arrivals) == n
        stats = runner._client.stats
        assert stats.quota == {"initial": n, "retry_quality": 0,
                               "retry_network": 0}
        assert stats.total_requests == n

        # 时间包络：(n-1)×Δ + 延迟填充，1.5× 上界
        theoretical = (n - 1) * 60 / qpm + 0.15
        assert wall <= 1.5 * theoretical, f"{wall:.2f}s vs {theoretical:.2f}s"

        # 并发上限 + 样本内串行 + paced
        assert srv.stats.max_in_flight <= conc
        assert stats.max_per_sample_in_flight <= 1
        assert bucket_violations(sorted(arrivals), qpm) == []


class TestRequestConservation:
    async def test_mismatch_retries_accounted(self, tmp_path):
        """确定性 mock：initial == 样本数，retry_quality == 质量重试数，
        arrivals == Σattempts（附录 B.3.1 请求数守恒）。"""
        n = 20
        samples = [fixture_sample(f"s{i}") for i in range(n)]
        srv, runner, result, wall = await run_batch(
            MockScenario(latency=(0.01, 0.03), seed=2,
                         match_probability=0.6,
                         normalized_noise_probability=0.2,
                         deterministic_by_content=True),
            samples, tmp_path,
            qpm_limit=600, max_concurrent=10, max_sample_attempts=3)

        stats = runner._client.stats
        arrivals = len(srv.stats.records)
        assert stats.quota["initial"] == n
        assert stats.quota["retry_network"] == 0
        assert arrivals == sum(r["attempts"] for r in result["results"])
        assert arrivals == sum(stats.quota.values())
        # 每样本请求上界：1 + (sample_life - 1)
        assert all(r["attempts"] <= 3 for r in result["results"])
        assert stats.max_per_sample_in_flight <= 1


class TestConcurrencyNotWorse:
    async def test_serial_vs_concurrent_exact_equal(self, tmp_path):
        """附录 B.3.3：确定性 mock 下串行与并发结果精确相等（负优化不得复现）。"""
        samples = [fixture_sample(f"s{i}") for i in range(20)]
        scenario = lambda: MockScenario(
            latency=(0.02, 0.08), seed=3, match_probability=0.7,
            normalized_noise_probability=0.2,
            deterministic_by_content=True)

        _, _, r_serial, _ = await run_batch(
            scenario(), samples, tmp_path / "serial",
            qpm_limit=600, max_concurrent=1)
        _, _, r_conc, _ = await run_batch(
            scenario(), samples, tmp_path / "concurrent",
            qpm_limit=600, max_concurrent=10)

        assert r_serial["success_count"] == r_conc["success_count"]
        by_id = lambda rs: {r["sample_id"]: r["status"] for r in rs}
        assert by_id(r_serial["results"]) == by_id(r_conc["results"])


class TestStormRecovery:
    async def test_403_storm_recovers_without_burst(self, tmp_path):
        """附录 A.5.3：403 风暴期间无同步突发，结束后恢复稳态并全部完成。"""
        qpm = 600
        samples = [fixture_sample(f"s{i}") for i in range(10)]
        srv, runner, result, wall = await run_batch(
            MockScenario(latency=(0.01, 0.03), seed=4, match_probability=1.0,
                         storm_duration=1.0),
            samples, tmp_path,
            qpm_limit=qpm, max_concurrent=10, network_max_attempts=50)

        # 最终全部完成（恢复能力）
        assert result["success_count"] == len(samples)

        # 全程无同步突发（ paced + jitter 破坏舰队同步）
        arrivals = sorted(r.arrival_monotonic for r in srv.stats.records)
        assert bucket_violations(arrivals, qpm) == []

        # 风暴的 403 只烧网络账，样本质量账不受影响
        stats = runner._client.stats
        assert stats.quota["retry_network"] >= 1
        assert stats.quota["retry_quality"] == 0
        assert stats.max_per_sample_in_flight <= 1

        # 恢复时效：墙钟 ≈ 风暴时长 + 稳态包络，不给死等
        assert wall <= 1.0 + (len(samples) * 60 / qpm) + 3.0


class TestResume:
    async def test_cancel_and_resume(self, tmp_path):
        """跑 k 个 cancel → 重跑：skip == k、无重复 id、最终完整。"""
        samples = [fixture_sample(f"s{i}") for i in range(6)]

        async def start_run():
            srv = MockExpertServer(MockScenario(latency=(0.05, 0.1), seed=5))
            base = await srv.start()
            cfg = Config(api_key="k", api_endpoint=base, qpm_limit=120,
                         max_concurrent=2, **FAST_BACKOFF)
            runner = BatchRunner(cfg)
            task = asyncio.ensure_future(
                runner.run(samples, str(tmp_path)))
            return srv, task

        # 第一轮：等到 3 个落盘后 cancel（模拟崩溃）
        srv1, task1 = await start_run()
        checkpoint = tmp_path / "checkpoint.json"
        for _ in range(200):
            await asyncio.sleep(0.05)
            if checkpoint.exists():
                with open(checkpoint) as f:
                    if len(json.load(f)["processed_ids"]) >= 3:
                        break
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1
        await srv1.close()

        # 崩溃瞬间结果文件仍是合法 JSON（就地追加的崩溃安全性）
        with open(tmp_path / "success_samples.json") as f:
            partial = json.load(f)
        assert len(partial) >= 3

        # 第二轮：断点恢复
        srv2 = MockExpertServer(MockScenario(latency=(0.01, 0.03), seed=5))
        base2 = await srv2.start()
        cfg2 = Config(api_key="k", api_endpoint=base2, qpm_limit=600,
                      max_concurrent=10, **FAST_BACKOFF)
        result = await BatchRunner(cfg2).run(samples, str(tmp_path))
        await srv2.close()

        assert result["skipped_count"] == len(partial)
        with open(tmp_path / "success_samples.json") as f:
            final = json.load(f)
        ids = [r["sample_id"] for r in final]
        assert len(ids) == len(set(ids)) == 6

    async def test_success_rate_excludes_skipped(self, tmp_path):
        """success_rate 分母为实际处理数（修老代码口径错误）。"""
        # 第一轮：4 个样本全部失败（mock 恒定返回 MISMATCH_DOC，
        # 样本 GT 为 CANONICAL → 永远不匹配）
        first = [fixture_sample(f"s{i}") for i in range(4)]
        await run_batch(
            MockScenario(latency=(0.0, 0.0), seed=6, match_probability=0.0),
            first, tmp_path, qpm_limit=600, max_sample_attempts=2)

        # 第二轮：8 个样本（前 4 个已处理被跳过）；新 4 个中 2 个的 GT
        # 就是 MISMATCH_DOC → 成功，另 2 个 → 失败
        def sample_with_gt(sample_id, gt_doc):
            s = fixture_sample(sample_id)
            s["messages"][1]["content"] = json.dumps(gt_doc,
                                                     ensure_ascii=False)
            return s

        second = first + [
            sample_with_gt("s4", MISMATCH_DOC),
            sample_with_gt("s5", MISMATCH_DOC),
            sample_with_gt("s6", CANONICAL_DOC),
            sample_with_gt("s7", CANONICAL_DOC),
        ]
        _, _, result, _ = await run_batch(
            MockScenario(latency=(0.0, 0.0), seed=6, match_probability=0.0),
            second, tmp_path, qpm_limit=600, max_sample_attempts=2)

        assert result["skipped_count"] == 4
        assert result["success_count"] == 2
        # 2/(8-4)=0.5；若分母含 skipped 则为 2/8=0.25
        assert result["success_rate"] == 0.5


class TestSummaryObservability:
    async def test_token_usage_and_full_config_in_summary(self, tmp_path):
        """summary 含 token_usage 与完整生成参数（实验可追溯）。"""
        samples = [fixture_sample(f"t{i}") for i in range(3)]
        srv, runner, result, _ = await run_batch(
            MockScenario(latency=(0.0, 0.0), seed=9, match_probability=1.0),
            samples, tmp_path, qpm_limit=6000)

        summary = json.loads((tmp_path / "summary.json").read_text())
        # token_usage：3 个 OK 响应 × mock usage（512/128）
        assert summary["metrics"]["token_usage"] == {
            "prompt_tokens": 512 * 3,
            "completion_tokens": 128 * 3,
            "responses_with_usage": 3,
        }
        # config 块含全部生成参数与超时（32768 事件的教训）
        cfg = summary["config"]
        for key in ("max_tokens", "temperature", "top_p", "top_k",
                    "presence_penalty", "enable_thinking",
                    "request_timeout", "connect_timeout"):
            assert key in cfg, key
        assert cfg["max_tokens"] == 32768
        assert cfg["temperature"] == 0.6

    async def test_length_truncated_counted_in_outcomes(self, tmp_path):
        """LENGTH_TRUNCATED 在 outcomes 独立成桶且不重试（请求数守恒）。"""
        samples = [fixture_sample(f"lt{i}") for i in range(4)]
        srv, runner, result, _ = await run_batch(
            MockScenario(latency=(0.0, 0.0), slow_latency=(0.0, 0.0),
                         seed=10, length_truncated_rate=1.0),
            samples, tmp_path, qpm_limit=6000)

        stats = runner._client.stats
        assert stats.outcomes.get("LENGTH_TRUNCATED") == 4
        # 不重试：恰好 4 次请求，全部 initial
        assert stats.total_requests == 4
        assert stats.quota["retry_quality"] == 0
        assert stats.quota["retry_network"] == 0
        assert result["failed_count"] == 4
        for rec in result["results"]:
            assert rec["error_type"] == "LENGTH_TRUNCATED"
            assert rec["attempts"] == 1


class TestCompat:
    async def test_output_files_and_fields(self, tmp_path):
        """R3：两种输入格式、输出三文件、结果字段与老代码兼容。"""
        samples = [fixture_sample("m1", "messages"),
                   fixture_sample("c1", "conversations")]
        srv, runner, result, _ = await run_batch(
            MockScenario(latency=(0.0, 0.0), seed=7, match_probability=1.0),
            samples, tmp_path, qpm_limit=600)

        for name in ("success_samples.json", "checkpoint.json",
                     "summary.json"):
            assert (tmp_path / name).exists()

        with open(tmp_path / "success_samples.json") as f:
            records = json.load(f)
        assert len(records) == 2
        old_keys = {"sample_id", "status", "attempts", "original_sample",
                    "cot_response", "full_api_response", "predicted_json",
                    "ground_truth", "comparison_result", "robust_match"}
        for r in records:
            assert old_keys <= set(r), f"缺失字段: {old_keys - set(r)}"

        with open(tmp_path / "summary.json") as f:
            summary = json.load(f)
        assert summary["metrics"]["total_http_requests"] == 2
        assert "quota" in summary["metrics"]
        assert "gt_analysis" in summary
