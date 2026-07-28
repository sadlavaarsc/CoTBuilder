"""降级场景测试：网络问题/杂散故障导致有效 QPM 略低于标称值时的表现。

背景（用户要求）：真实环境中单纯请求失败、服务端偶发变慢等因素会让
有效吞吐略低于 QPM 上限。当前版本**不实现**针对这类现象的补偿机制，
但必须验证系统在此类场景下不失态：
- QPM 上限与并发上限全程不破（不因重试而突发补发）；
- 样本内串行不变量成立；
- 不振荡、不死锁，最终完成；
- 失败样本正确分类落盘。

断言刻意使用上界/不变量而非精确计数：网络错误由 seeded rng 按到达
顺序分配，并发交错下重试次数允许 ± 波动。
"""

import math

import pytest

from cotbuilder.config import Config
from mock.mock_server import MockExpertServer, MockScenario, fixture_sample
from tests.test_batch import run_batch, bucket_violations

FAST = {"backoff_base": 0.02, "backoff_cap": 0.1}


def assert_invariants(srv, runner, qpm, max_concurrent):
    """所有降级场景统一断言的不变量。"""
    arrivals = sorted(r.arrival_monotonic for r in srv.stats.records)
    assert bucket_violations(arrivals, qpm) == [], "存在 1s 桶突发"
    for t in arrivals:  # 任意 60s 滑窗 ≤ qpm（+1 浮点边界容差）
        assert sum(1 for a in arrivals if t <= a < t + 60.0) <= qpm + 1
    assert srv.stats.max_in_flight <= max_concurrent
    assert runner._client.stats.max_per_sample_in_flight <= 1
    # 请求数守恒：到达数 == quota 三桶之和
    stats = runner._client.stats
    assert len(arrivals) == sum(stats.quota.values())


class TestNetworkFlakiness:
    async def test_10pct_network_error(self, tmp_path):
        """10% 请求直接断连：重试补上后全部成功，无突发补发。"""
        qpm, conc = 600, 10
        samples = [fixture_sample(f"d1-{i}") for i in range(20)]
        srv, runner, result, wall = await run_batch(
            MockScenario(latency=(0.05, 0.15), seed=11,
                         match_probability=1.0, network_error_rate=0.1),
            samples, tmp_path, qpm_limit=qpm, max_concurrent=conc,
            network_max_attempts=10)

        assert result["success_count"] == len(samples)
        assert_invariants(srv, runner, qpm, conc)
        # 有效吞吐 ≤ 标称上限（允许略小，这正是本场景的意义）
        effective_qpm = len(srv.stats.records) / wall * 60
        assert effective_qpm <= qpm * 1.05
        # 包络：含重试的请求数 × Δ + 延迟 + 退避余量
        assert wall <= len(srv.stats.records) * 60 / qpm + 0.15 + 2.0

    async def test_5pct_probabilistic_403(self, tmp_path):
        """5% 概率性 403：只烧网络账，恢复后全部成功。"""
        qpm, conc = 600, 10
        samples = [fixture_sample(f"d2-{i}") for i in range(20)]
        srv, runner, result, wall = await run_batch(
            MockScenario(latency=(0.05, 0.15), seed=12,
                         match_probability=1.0, rate_limit_rate=0.05),
            samples, tmp_path, qpm_limit=qpm, max_concurrent=conc,
            network_max_attempts=10)

        assert result["success_count"] == len(samples)
        assert_invariants(srv, runner, qpm, conc)
        stats = runner._client.stats
        assert stats.quota["retry_network"] >= 0  # 403 只进网络账
        assert stats.quota["retry_quality"] == 0

    async def test_slow_server_jitter(self, tmp_path):
        """服务端延迟抖动大（0.05–0.8s）：有效吞吐由并发与延迟决定
        （低于 QPM 上限），系统不补发、不突发，并发被打满。

        qpm 取 60000 使限流近似不介入——此时若并发打不满上限，
        说明限流/并发仍有耦合（R4 推论：并发应是瓶颈）。
        """
        qpm, conc = 60000, 10
        samples = [fixture_sample(f"d3-{i}") for i in range(20)]
        srv, runner, result, wall = await run_batch(
            MockScenario(latency=(0.05, 0.8), seed=13, match_probability=1.0),
            samples, tmp_path, qpm_limit=qpm, max_concurrent=conc)

        assert result["success_count"] == len(samples)
        assert_invariants(srv, runner, qpm, conc)
        # 大延迟 + 限流不介入 → 并发必须打满（并发是瓶颈的直接证据）
        assert srv.stats.max_in_flight >= conc - 1


class TestMixedDegradation:
    async def test_mixed_small_failures(self, tmp_path):
        """混合小故障（断连 5% + 空响应 3% + 非法 JSON 3%）：
        能完成的完成，不能完成的正确分类落盘，全程不失态。"""
        qpm, conc = 600, 10
        samples = [fixture_sample(f"d4-{i}") for i in range(30)]
        srv, runner, result, wall = await run_batch(
            MockScenario(latency=(0.05, 0.15), seed=14,
                         match_probability=1.0,
                         network_error_rate=0.05,
                         empty_response_rate=0.03,
                         invalid_json_rate=0.03),
            samples, tmp_path, qpm_limit=qpm, max_concurrent=conc,
            network_max_attempts=10)

        r = result
        assert r["success_count"] + r["failed_count"] == len(samples)
        # 失败样本的错误类型必须是已知类别（空响应/JSON 解析/网络耗尽）
        known = {"EMPTY_RESPONSE", "JSON_PARSE_ERROR", "NETWORK_ERROR",
                 "RATE_LIMITED"}
        for rec in r["results"]:
            if rec["status"] == "failed":
                assert rec["error_type"] in known
        assert_invariants(srv, runner, qpm, conc)

    async def test_all_network_down_fails_cleanly(self, tmp_path):
        """极端情况：100% 断连。样本以 NETWORK_ERROR 失败、真实 attempts、
        无无界重试、退避期间不占并发槽。"""
        qpm, conc = 600, 10
        samples = [fixture_sample(f"d5-{i}") for i in range(5)]
        srv, runner, result, wall = await run_batch(
            MockScenario(latency=(0.0, 0.0), seed=15, network_error_rate=1.0),
            samples, tmp_path, qpm_limit=qpm, max_concurrent=conc,
            network_max_attempts=3)

        assert result["success_count"] == 0
        assert result["failed_count"] == 5
        for rec in result["results"]:
            assert rec["error_type"] == "NETWORK_ERROR"
            assert rec["attempts"] == 3   # 真实次数，不再硬编码 1
        stats = runner._client.stats
        # 请求数守恒：5 样本 × 3 次 = 15，全部记在 initial + retry_network
        assert stats.total_requests == 15
        assert stats.quota["initial"] == 5
        assert stats.quota["retry_network"] == 10
        assert stats.max_per_sample_in_flight <= 1
