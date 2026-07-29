"""mock server 自检：场景与观测端点可用性。"""

import aiohttp
import pytest

from mock.mock_server import MockExpertServer, MockScenario, fixture_sample


@pytest.fixture
async def server():
    srv = MockExpertServer(MockScenario(latency=(0.01, 0.02), seed=1))
    base = await srv.start()
    yield srv, base
    await srv.close()


async def _post(base, session):
    async with session.post(
        f"{base}/chat/completions",
        json={"model": "m", "messages": []},
    ) as resp:
        return resp.status, await resp.json() if resp.status == 200 else None


class TestMockServer:
    async def test_match_response(self, server):
        srv, base = server
        async with aiohttp.ClientSession() as session:
            status, payload = await _post(base, session)
        assert status == 200
        content = payload["choices"][0]["message"]["content"]
        assert '"发票号码": "12345678"' in content

    async def test_stats_endpoint(self, server):
        srv, base = server
        async with aiohttp.ClientSession() as session:
            await _post(base, session)
            await _post(base, session)
            async with session.get(f"{base.rsplit('/v1', 1)[0]}/_stats") as resp:
                stats = await resp.json()
        assert stats["total_requests"] == 2
        assert stats["max_in_flight"] >= 1

    async def test_fixed_window_rate_limit(self):
        """服务端固定窗口限流：窗口内到达数超上限 → 403。"""
        srv = MockExpertServer(MockScenario(
            latency=(0.0, 0.0), seed=1, fixed_window_qpm=2))
        base = await srv.start()
        try:
            async with aiohttp.ClientSession() as session:
                statuses = [(await _post(base, session))[0] for _ in range(4)]
        finally:
            await srv.close()
        assert statuses[:2] == [200, 200]
        assert statuses[2:] == [403, 403]

    async def test_gateway_504(self):
        """gateway_error_rate → 504（GATEWAY_ERROR 分类的测试基准）。"""
        srv = MockExpertServer(MockScenario(
            latency=(0.0, 0.0), seed=1, gateway_error_rate=1.0))
        base = await srv.start()
        try:
            async with aiohttp.ClientSession() as session:
                status, _ = await _post(base, session)
        finally:
            await srv.close()
        assert status == 504
        assert srv.stats.records[0].outcome == "gateway_504"

    async def test_deterministic_with_seed(self):
        """同 seed 两次运行，outcome 序列完全一致（并发等价性测试的基础）。"""
        async def run_once():
            srv = MockExpertServer(MockScenario(
                latency=(0.0, 0.0), seed=7,
                match_probability=0.5, normalized_noise_probability=0.5))
            base = await srv.start()
            try:
                async with aiohttp.ClientSession() as session:
                    for _ in range(10):
                        await _post(base, session)
                return [r.outcome for r in srv.stats.records]
            finally:
                await srv.close()

        assert await run_once() == await run_once()

    def test_fixture_sample_both_formats(self):
        s1 = fixture_sample("s1", "messages")
        s2 = fixture_sample("s2", "conversations")
        assert s1["messages"][1]["content"]
        assert s2["conversations"][1]["value"]

    async def test_length_truncated_response_shape(self):
        """length_truncated outcome：content=null + finish_reason=length +
        completion_tokens 顶满 32768（实测模式复刻）。"""
        srv = MockExpertServer(MockScenario(
            latency=(0.0, 0.0), slow_latency=(0.0, 0.0),
            seed=1, length_truncated_rate=1.0))
        base = await srv.start()
        try:
            async with aiohttp.ClientSession() as session:
                status, payload = await _post(base, session)
        finally:
            await srv.close()
        assert status == 200
        choice = payload["choices"][0]
        assert choice["message"]["content"] is None
        assert choice["finish_reason"] == "length"
        assert payload["usage"]["completion_tokens"] == 32768

    async def test_empty_outcome_uses_slow_latency(self):
        """慢=EMPTY 确定性绑定：empty outcome 走 slow_latency 档
        （实测 thinking 耗尽响应 230–316s，典型档 4–31s）。"""
        srv = MockExpertServer(MockScenario(
            latency=(0.0, 0.0), slow_latency=(0.3, 0.4),
            seed=1, empty_response_rate=1.0))
        base = await srv.start()
        try:
            async with aiohttp.ClientSession() as session:
                await _post(base, session)
        finally:
            await srv.close()
        rec = srv.stats.records[0]
        assert rec.outcome == "empty"
        assert rec.done_monotonic - rec.arrival_monotonic >= 0.28

    async def test_normal_outcome_has_finish_reason_stop(self, server):
        srv, base = server
        async with aiohttp.ClientSession() as session:
            status, payload = await _post(base, session)
        assert payload["choices"][0]["finish_reason"] == "stop"
        assert "usage" in payload
