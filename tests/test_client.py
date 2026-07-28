"""client 集成测试（mock server，缩小时间尺度）。

验收指标（audit-01 §5 / 附录 A.5）：
- 并发上限真实守住：max_in_flight ≤ max_concurrent，限流饱和时仍成立；
- paced 匀速放行：到达时间戳间隔与 1s 桶上界；
- 错误分类按状态码驱动；
- 共享 session 连接复用。
"""

import asyncio
import math

import pytest

from cotbuilder.client import ErrorType, ExpertModelClient
from cotbuilder.config import Config
from cotbuilder.ratelimit import PacedRateLimiter
from mock.mock_server import MockExpertServer, MockScenario


def make_config(base_url: str, **overrides) -> Config:
    return Config(api_key="test-key", api_endpoint=base_url, **overrides)


@pytest.fixture
async def mock():
    srv = MockExpertServer(MockScenario(latency=(0.05, 0.15), seed=3))
    base = await srv.start()
    yield srv, base
    await srv.close()


class TestConcurrencyCap:
    async def test_max_in_flight_never_exceeds_cap(self, mock):
        """40 个协程同时发起，服务端观测在途峰值 ≤ max_concurrent。"""
        srv, base = mock
        cfg = make_config(base, max_concurrent=5, qpm_limit=6000)
        limiter = PacedRateLimiter(qpm=cfg.qpm_limit)
        async with ExpertModelClient(cfg, limiter) as client:
            outcomes = await asyncio.gather(*[
                client.call([{"role": "user", "content": "x"}],
                            sample_id=f"s{i}")
                for i in range(40)
            ])
        assert all(o.ok for o in outcomes)
        assert srv.stats.max_in_flight <= cfg.max_concurrent
        assert client.stats.peak_in_flight <= cfg.max_concurrent
        # 并发度打满（审计 R4：并发长期低于上限也是耦合问题的信号）
        assert srv.stats.max_in_flight == cfg.max_concurrent

    async def test_cap_holds_when_limiter_saturated(self, mock):
        """限流远慢于并发时（等待限流的协程不得占槽位）。

        qpm=60 → 每秒放行 1 个；max_concurrent=3。若限流等待占槽，
        在途会贴着 3 不动甚至饿死；正确实现下在途 ≈ 放行速率 × 延迟。
        """
        srv, base = mock
        cfg = make_config(base, max_concurrent=3, qpm_limit=60)
        limiter = PacedRateLimiter(qpm=cfg.qpm_limit)
        async with ExpertModelClient(cfg, limiter) as client:
            await asyncio.gather(*[
                client.call([{"role": "user", "content": "x"}],
                            sample_id=f"s{i}")
                for i in range(8)
            ])
        assert srv.stats.max_in_flight <= cfg.max_concurrent
        # 8 个请求按 1/s 放行，墙钟应 ≈ 7s+（证明限流真的在限速，
        # 且等待者没有绕过或占住槽位）
        arrivals = [r.arrival_monotonic for r in srv.stats.records]
        assert max(arrivals) - min(arrivals) >= 6.5


class TestPacing:
    async def test_arrival_buckets_bounded(self, mock):
        """到达时间戳任意 1s 桶 ≤ ⌈qpm/60⌉+1（附录 A.5.1 的匀速断言）。"""
        srv, base = mock
        qpm = 600  # Δ=0.1s
        cfg = make_config(base, max_concurrent=50, qpm_limit=qpm)
        limiter = PacedRateLimiter(qpm=qpm)
        async with ExpertModelClient(cfg, limiter) as client:
            await asyncio.gather(*[
                client.call([{"role": "user", "content": "x"}],
                            sample_id=f"s{i}")
                for i in range(30)
            ])
        arrivals = sorted(r.arrival_monotonic for r in srv.stats.records)
        bound = math.ceil(qpm / 60) + 1
        for t in arrivals:
            n = sum(1 for a in arrivals if t <= a < t + 1.0)
            assert n <= bound


class TestErrorClassification:
    async def _one(self, outcome_srv_scenario: MockScenario):
        srv = MockExpertServer(outcome_srv_scenario)
        base = await srv.start()
        try:
            cfg = make_config(base, qpm_limit=6000)
            async with ExpertModelClient(cfg, PacedRateLimiter(6000)) as c:
                return await c.call([{"role": "user", "content": "x"}])
        finally:
            await srv.close()

    async def test_rate_limited(self):
        o = await self._one(MockScenario(latency=(0, 0), rate_limit_rate=1.0))
        assert o.error == ErrorType.RATE_LIMITED

    async def test_api_error_not_retryable(self):
        o = await self._one(MockScenario(latency=(0, 0), server_error_rate=1.0))
        assert o.error == ErrorType.API_ERROR

    async def test_network_error(self):
        o = await self._one(MockScenario(latency=(0, 0), network_error_rate=1.0))
        assert o.error == ErrorType.NETWORK_ERROR

    async def test_empty_response(self):
        o = await self._one(MockScenario(latency=(0, 0), empty_response_rate=1.0))
        assert o.error == ErrorType.EMPTY_RESPONSE

    async def test_ok(self):
        o = await self._one(MockScenario(latency=(0, 0)))
        assert o.ok and o.content


class TestSharedSession:
    async def test_connection_reuse(self, mock):
        """30 个请求复用少量 TCP 连接（老代码每请求新建 session）。"""
        srv, base = mock
        cfg = make_config(base, max_concurrent=5, qpm_limit=6000)
        async with ExpertModelClient(cfg, PacedRateLimiter(6000)) as client:
            await asyncio.gather(*[
                client.call([{"role": "user", "content": "x"}])
                for _ in range(30)
            ])
            conns = sum(len(v) for v in client._session.connector._conns.values())
        assert 0 < conns <= cfg.max_concurrent
