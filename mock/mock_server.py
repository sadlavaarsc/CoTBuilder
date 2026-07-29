"""Mock 专家模型 API（R4）：指标测试的唯一基准，开发与测试不触真实服务。

建模约定（CLAUDE.md R4 + 审计报告 01 附录 A.5.3 / B.3）：
- 单次请求延迟可配，默认 (30, 90)s 贴近真实多模态推理；测试整体缩小
  时间常数（如 (0.05, 0.2)s 延迟 + qpm 同步放大），用真实 event loop
  几秒跑完场景，而不是注入虚拟时钟——这样测的是真实 asyncio 行为；
- 返回内容符合 API 格式即可（choices[0].message.content 含可解析 JSON），
  具体取值由场景决定；
- 场景概率全部走 seeded random.Random：同 seed 全管线确定性可复现，
  「并发 vs 串行成功率等价」可精确断言，无需统计检验；
- 观测端点 /_stats 暴露每请求到达时间戳与 max_in_flight，是 QPM 上限
  与并发上限断言的数据源。
"""

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

from aiohttp import web

# 规范夹具文档：mock 对「应匹配」请求返回此 JSON 的字符串形式；
# 测试构造样本时 GT 用同一夹具（fixture_gt()）。
CANONICAL_DOC = {
    "发票号码": "12345678",
    "购买方名称": "LAU, LAI LI",
    "总价": "¥5.83",
    "明细": [{"品名": "办公用品", "单价": "¥5.83", "数量": 1}],
}

# 「应归一化一致」变体：全角 + 标点空格噪声（走 NORMALIZED 路径）
NORMALIZED_NOISE_DOC = {
    "发票号码": "１２３４５６７８",
    "购买方名称": "ＬＡＵ， ＬＡＩ ＬＩ",
    "总价": "总价 ：￥5.83",
    "明细": [{"品名": "办公用品", "单价": "￥５.８３", "数量": 1}],
}

# 「应不匹配」扰动：改一个字段的真实值（走 MISMATCH 路径）
MISMATCH_DOC = {
    "发票号码": "87654321",
    "购买方名称": "LAU, LAI LI",
    "总价": "¥5.83",
    "明细": [{"品名": "办公用品", "单价": "¥5.83", "数量": 1}],
}


def fixture_gt() -> dict:
    """测试样本的 Ground Truth：与 CANONICAL_DOC 同构。"""
    return json.loads(json.dumps(CANONICAL_DOC, ensure_ascii=False))


def fixture_sample(sample_id: str, fmt: str = "messages") -> dict:
    """构造一个输入样本，兼容 messages / conversations 两种格式（R3）。

    prompt 中嵌入 sample_id：确定性模式下 mock 按请求内容哈希分配命运，
    各样本需要不同的内容。
    """
    gt_text = json.dumps(fixture_gt(), ensure_ascii=False)
    prompt = f"<image>\n请提取图片中的关键信息（样本 {sample_id}）"
    if fmt == "messages":
        return {
            "id": sample_id,
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": gt_text},
            ],
            "images": [],   # 测试无真实图片；_build_messages 容忍空 images
        }
    return {
        "id": sample_id,
        "conversations": [
            {"from": "human", "value": prompt},
            {"from": "gpt", "value": gt_text},
        ],
        "images": [],
    }


@dataclass
class MockScenario:
    """mock 行为配置。所有概率为每请求独立判定（seeded）。

    Attributes:
        latency: 单次请求延迟区间（秒），均匀随机。默认 (4,30) 为典型档——
            实测真实 API 的 OK 请求分布 4–31s、p50≈10s（2026-07-29 e2e）。
        slow_response_rate: 其他 outcome 附加超长尾延迟的概率（默认 0）。
            独立旋钮，用于测试超时拆分等需要「慢但正常返回」的场景。
        slow_latency: 超长尾档延迟区间（秒），默认 (230,320)——实测
            thinking 耗尽响应 230–316s。empty / length_truncated outcome
            **确定性**使用此档（实测慢=token 耗尽，二者强绑定）。
        seed: 随机种子，None 则不设种子。
        match_probability: 返回「与 GT 一致」响应的概率；
            其余按 normalized_noise_probability 分流到归一化噪声/不匹配。
        normalized_noise_probability: 不匹配分流中返回归一化噪声的比例。
        network_error_rate: 直接断开连接（模拟网络错误）的概率。
        server_error_rate: 返回 500 的概率（API_ERROR，不重试）。
        rate_limit_rate: 概率性 403（含 qpm 文案）的概率。
        empty_response_rate: 200 但 content 为空的概率（finish_reason=stop，
            真空响应）。
        length_truncated_rate: 200 但 content=null 且 finish_reason=length
            的概率（thinking 耗尽输出预算，实测 43% 失败的主根因）；
            该 outcome 使用 slow_latency 档并带 completion_tokens=32768。
        invalid_json_rate: 200 但 content 非 JSON 的概率。
        fixed_window_qpm: 非 None 时启用服务端固定自然分钟窗口限流
            （复现审计附录 A.2：窗口内到达数 > 此值 → 403）。
        storm_duration: 服务启动后前 N 秒全部 403（风暴模式，附录 A.5.3）。
        deterministic_by_content: True 时内容类场景（match/normalized/
            mismatch）按请求 body 哈希确定而非掷硬币——同一样本在任何
            并发交错下命运一致，「并发 vs 串行成功率精确相等」可断言。
            基础设施类场景（网络错误/500/403/空响应）仍走 seeded rng。
    """

    latency: Tuple[float, float] = (4.0, 30.0)
    slow_response_rate: float = 0.0
    slow_latency: Tuple[float, float] = (230.0, 320.0)
    seed: Optional[int] = 42
    match_probability: float = 1.0
    normalized_noise_probability: float = 0.0
    network_error_rate: float = 0.0
    server_error_rate: float = 0.0
    rate_limit_rate: float = 0.0
    empty_response_rate: float = 0.0
    length_truncated_rate: float = 0.0
    invalid_json_rate: float = 0.0
    fixed_window_qpm: Optional[int] = None
    storm_duration: float = 0.0
    deterministic_by_content: bool = False


@dataclass
class RequestRecord:
    seq: int
    arrival_monotonic: float
    arrival_wall: float
    outcome: str          # ok_match / ok_normalized / ok_mismatch / network_cut /
                          # server_500 / rate_limited / empty / invalid_json
    status: int
    done_monotonic: float = 0.0   # 响应完成时刻（服务端侧延迟可算）


@dataclass
class MockStats:
    records: list = field(default_factory=list)  # List[RequestRecord]
    in_flight: int = 0
    max_in_flight: int = 0
    connection_count: int = 0
    payloads: list = field(default_factory=list)  # 每请求 body JSON


class MockExpertServer:
    """aiohttp mock API。用法：

        server = MockExpertServer(MockScenario(...))
        base_url = await server.start()          # 如 http://127.0.0.1:51234
        ... 跑管线 ...
        stats = server.stats                     # 断言数据源
        await server.close()
    """

    def __init__(self, scenario: MockScenario):
        self.scenario = scenario
        self.rng = random.Random(scenario.seed)
        self.stats = MockStats()
        self._start_monotonic: Optional[float] = None
        self._window_minute: Optional[int] = None
        self._window_count = 0
        self._runner: Optional[web.AppRunner] = None
        self._seq = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._handle_chat)
        app.router.add_get("/_stats", self._handle_stats)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        actual_port = site._server.sockets[0].getsockname()[1]
        self._start_monotonic = time.monotonic()
        return f"http://{host}:{actual_port}/v1"

    async def close(self):
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------

    def _decide_outcome(self, body_key: Optional[str] = None) -> str:
        """按场景概率判定本次请求的命运（优先判定失败类场景）。"""
        r = self.rng.random
        s = self.scenario
        if r() < s.network_error_rate:
            return "network_cut"
        if r() < s.server_error_rate:
            return "server_500"
        if r() < s.rate_limit_rate:
            return "rate_limited"
        if r() < s.empty_response_rate:
            return "empty"
        if r() < s.length_truncated_rate:
            return "length_truncated"
        if r() < s.invalid_json_rate:
            return "invalid_json"
        if s.deterministic_by_content and body_key is not None:
            # 内容哈希决定：同一样本在任意并发交错下结果一致
            digest = hashlib.md5(body_key.encode("utf-8")).hexdigest()
            roll = (int(digest, 16) % 1000) / 1000.0
        else:
            roll = r()
        if roll < s.match_probability:
            return "ok_match"
        if roll < s.match_probability + s.normalized_noise_probability:
            return "ok_normalized"
        return "ok_mismatch"

    async def _handle_chat(self, request: web.Request) -> web.StreamResponse:
        s = self.scenario
        self._seq += 1
        seq = self._seq
        arrival_mono = time.monotonic()
        self.stats.in_flight += 1
        self.stats.max_in_flight = max(self.stats.max_in_flight,
                                       self.stats.in_flight)
        try:
            # 请求体始终读取并记录（测试据此断言生成参数到达服务端）
            body_key = None
            try:
                payload = await request.json()
                self.stats.payloads.append(payload)
                if s.deterministic_by_content:
                    body_key = json.dumps(payload.get("messages", []),
                                          sort_keys=True, ensure_ascii=False)
            except Exception:
                payload = None
                body_key = None
            outcome = self._decide_outcome(body_key)

            # 风暴模式：启动后前 N 秒无条件 403（优先级最高）
            if (s.storm_duration > 0
                    and arrival_mono - self._start_monotonic < s.storm_duration):
                outcome = "rate_limited"

            # 服务端固定自然分钟窗口限流（复现附录 A.2 口径错配）
            if (outcome not in ("network_cut", "server_500")
                    and s.fixed_window_qpm is not None):
                minute = int(time.time() // 60)
                if minute != self._window_minute:
                    self._window_minute, self._window_count = minute, 0
                self._window_count += 1
                if self._window_count > s.fixed_window_qpm:
                    outcome = "rate_limited"

            # 网络错误：延迟前直接断开（对端看到连接异常）
            if outcome == "network_cut":
                request.transport.close()
                self._record(seq, arrival_mono, outcome, 0)
                return web.Response(status=599)  # 不可达，transport 已断

            # 模拟推理延迟：empty / length_truncated 确定性走超长尾档
            # （实测慢=token 耗尽，二者强绑定）；其他 outcome 按
            # slow_response_rate 概率附加（测试超时拆分的独立旋钮）
            latency_range = s.latency
            if outcome in ("empty", "length_truncated"):
                latency_range = s.slow_latency
            elif (s.slow_response_rate > 0
                    and self.rng.random() < s.slow_response_rate):
                latency_range = s.slow_latency
            await asyncio.sleep(self.rng.uniform(*latency_range))

            status, payload = self._render(outcome)
            self._record(seq, arrival_mono, outcome, status)
            if payload is None:
                return web.Response(status=status,
                                    text="qpm rate limit exceeded, 请求频率超限")
            return web.Response(
                status=status,
                content_type="application/json",
                text=json.dumps(payload, ensure_ascii=False),
            )
        finally:
            self.stats.in_flight -= 1

    def _render(self, outcome: str):
        """生成 (status, payload)。payload=None 表示纯文本错误响应。"""
        if outcome == "rate_limited":
            return 403, None
        if outcome == "server_500":
            return 500, None
        # thinking 耗尽输出预算：content=null + finish_reason=length +
        # completion_tokens 顶满 32768（实测完全一致的模式）
        if outcome == "length_truncated":
            return 200, {
                "choices": [{
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": "length",
                }],
                "model": "mock-expert",
                "usage": {"prompt_tokens": 512, "completion_tokens": 32768},
            }
        content = {
            "ok_match": json.dumps(CANONICAL_DOC, ensure_ascii=False),
            "ok_normalized": json.dumps(NORMALIZED_NOISE_DOC, ensure_ascii=False),
            "ok_mismatch": json.dumps(MISMATCH_DOC, ensure_ascii=False),
            "empty": "",
            "invalid_json": "这不是 JSON，模型输出了解析不了的内容",
        }[outcome]
        return 200, {
            "choices": [{
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "model": "mock-expert",
            "usage": {"prompt_tokens": 512, "completion_tokens": 128},
        }

    def _record(self, seq, arrival_mono, outcome, status):
        self.stats.records.append(RequestRecord(
            seq=seq,
            arrival_monotonic=arrival_mono,
            arrival_wall=time.time(),
            outcome=outcome,
            status=status,
            done_monotonic=time.monotonic(),
        ))

    async def _handle_stats(self, request: web.Request) -> web.Response:
        return web.json_response({
            "max_in_flight": self.stats.max_in_flight,
            "total_requests": len(self.stats.records),
            "records": [
                {
                    "seq": r.seq,
                    "arrival_monotonic": r.arrival_monotonic,
                    "arrival_wall": r.arrival_wall,
                    "done_monotonic": r.done_monotonic,
                    "outcome": r.outcome,
                    "status": r.status,
                }
                for r in self.stats.records
            ],
        })
