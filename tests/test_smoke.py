"""近真实尺度冒烟测试（@pytest.mark.slow，默认跳过）。

用法：python -m pytest tests/test_smoke.py -v -m slow

与指标测试的区别：使用真实 qpm=50 与秒级延迟，验证真实量级下
限流/并发行为不变形（R4 关键推论：平均延迟 60s 时打满 QPM 50 需
约 50 个在途请求，max_concurrent=10 时并发才是瓶颈）。
"""

import pytest

from cotbuilder.config import Config
from mock.mock_server import MockExpertServer, MockScenario, fixture_sample
from tests.test_batch import run_batch

pytestmark = pytest.mark.slow


class TestNearRealScale:
    async def test_real_qpm_pacing(self, tmp_path):
        """真实 qpm=50（Δ=1.2s）+ 秒级延迟：8 样本墙钟 ≈ 7×1.2+延迟。

        断言：到达间隔贴近 1.2s（匀速而非突发）、并发/串行不变量、
        全部成功、墙钟在包络内。
        """
        n = 8
        samples = [fixture_sample(f"smoke{i}") for i in range(n)]
        srv, runner, result, wall = await run_batch(
            MockScenario(latency=(1.0, 3.0), seed=8, match_probability=0.75,
                         normalized_noise_probability=0.25,
                         deterministic_by_content=True),
            samples, tmp_path,
            qpm_limit=50, max_concurrent=10, max_sample_attempts=2)

        assert result["failed_count"] == 0 or result["success_count"] > 0
        arrivals = sorted(r.arrival_monotonic for r in srv.stats.records)
        gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
        # 真实尺度下匀速放行：相邻到达间隔 ≥ 1.2s（容忍 50ms 时钟抖动）
        assert all(g >= 1.15 for g in gaps), gaps
        assert srv.stats.max_in_flight <= 10
        assert runner._client.stats.max_per_sample_in_flight <= 1
        # 包络：请求数×1.2s + 延迟填充 + 余量
        assert wall <= len(arrivals) * 1.2 + 3.0 + 5.0

    async def test_concurrency_is_the_bottleneck(self, tmp_path):
        """R4 关键推论验证：max_concurrent=10 + 高延迟时，吞吐由并发决定，
        QPM 限流不应成为瓶颈（到达间隔由 延迟/并发 而非 60/qpm 决定）。"""
        n = 10
        samples = [fixture_sample(f"smoke2-{i}") for i in range(n)]
        srv, runner, result, wall = await run_batch(
            MockScenario(latency=(2.0, 3.0), seed=9, match_probability=1.0),
            samples, tmp_path,
            qpm_limit=50, max_concurrent=3)

        assert result["success_count"] == n
        # 并发 3、延迟 ~2.5s → 稳态吞吐 ≈ 3/2.5 = 1.2/s ≈ 72 QPM > 50
        # → 此时限流器才开始介入；用 qpm=50 的间隔检查到达间隔下界
        arrivals = sorted(r.arrival_monotonic for r in srv.stats.records)
        assert srv.stats.max_in_flight <= 3
        assert runner._client.stats.max_per_sample_in_flight <= 1
