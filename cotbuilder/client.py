"""专家模型 HTTP 客户端：全系统唯一发请求的地方。

职责边界（审计报告 01 §5，三职责分离）：
- 限流（PacedRateLimiter）只管放行时刻，本模块在 acquire 等待期间
  **不持有任何资源**；
- 并发信号量包裹**完整 HTTP 生命周期**（获取 → 请求 → 读完 body → 释放），
  这是与老代码的本质区别——老代码的信号量只守住了限流检查这一微秒级
  内存操作，对真正的在途请求毫无约束；
- 本模块**不知道「重试」的存在**：单发请求 + 错误分类返回，
  重试决策全部在 generator 的寿命循环里。

错误分类按 HTTP 状态码驱动（修老代码靠错误文案搜 "qpm" 子串的脆弱实现）：
- 403 / 429 → RATE_LIMITED（可重试，尊重 Retry-After 头）
- 其他 4xx/5xx → API_ERROR（不重试）
- 连接错误 / 超时 → NETWORK_ERROR（可重试）
- 200 但 content 为空 → EMPTY_RESPONSE
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

import aiohttp

from .config import Config
from .metrics import Metrics
from .ratelimit import PacedRateLimiter

logger = logging.getLogger(__name__)


class ErrorType(str, Enum):
    NETWORK_ERROR = "NETWORK_ERROR"      # 连接/超时，可重试
    RATE_LIMITED = "RATE_LIMITED"        # 403/429，可重试
    API_ERROR = "API_ERROR"              # 其他 4xx/5xx，不重试
    EMPTY_RESPONSE = "EMPTY_RESPONSE"    # 200 但 content 为空（真空响应）
    LENGTH_TRUNCATED = "LENGTH_TRUNCATED"  # 200 但 content 为空且
    # finish_reason=length——thinking 耗尽输出预算（确定性失败，不重试；
    # 与真空响应拆开是因为它是实测 43% 失败的主根因，需要独立观测）


@dataclass
class CallOutcome:
    """单发请求的结果。"""

    ok: bool
    response: Optional[Dict[str, Any]] = None   # 完整 API 响应（200 时）
    content: Optional[str] = None               # choices[0].message.content
    error: Optional[ErrorType] = None
    retry_after: Optional[float] = None         # RATE_LIMITED 时服务端要求
    usage: Optional[Dict[str, Any]] = None      # 200 时透传 body["usage"]


@dataclass
class ClientStats:
    """内建指标（审计报告 01 §2.6：老代码完全无观测）。"""

    in_flight: int = 0
    peak_in_flight: int = 0
    total_requests: int = 0
    per_sample_in_flight: Dict[str, int] = field(default_factory=dict)
    max_per_sample_in_flight: int = 0
    quota: Dict[str, int] = field(default_factory=lambda: {
        "initial": 0, "retry_quality": 0, "retry_network": 0,
    })
    outcomes: Dict[str, int] = field(default_factory=dict)
    # token 消耗累计（usage 缺失的响应不计入分母之外，单独记数）
    tokens: Dict[str, int] = field(default_factory=lambda: {
        "prompt_tokens": 0, "completion_tokens": 0,
        "responses_with_usage": 0,
    })


class ExpertModelClient:
    """共享 session 的单发客户端。

    用法::

        async with ExpertModelClient(config, limiter) as client:
            outcome = await client.call(messages, sample_id="s1")

    Args:
        config: 运行配置。
        limiter: 匀速限流器（全实例共享一个，保证全局 QPM）。
        metrics: 性能追踪（可选）。None 时零开销、零行为变化；
            传入后 call() 内打四段 monotonic 点并记事件（metrics.py
            的 record 不 await，不引入关键路径耦合）。
    """

    def __init__(self, config: Config, limiter: PacedRateLimiter,
                 metrics: Optional[Metrics] = None):
        self._config = config
        self._limiter = limiter
        self._metrics = metrics
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._session: Optional[aiohttp.ClientSession] = None
        self.stats = ClientStats()

    async def __aenter__(self):
        # 超时拆分（2026-07-29 实测修复）：total 管慢推理（思考型模型可
        # 超过 2 分钟，120s 会把正常推理掐成 NETWORK_ERROR）；connect /
        # sock_connect 管连接建立，真网络故障几秒内快速失败，不陪跑 total。
        timeout = aiohttp.ClientTimeout(
            total=self._config.request_timeout,
            connect=self._config.connect_timeout,
            sock_connect=self._config.connect_timeout,
        )
        connector = aiohttp.TCPConnector(limit=self._config.max_concurrent)
        self._session = aiohttp.ClientSession(
            timeout=timeout, connector=connector)
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()
            self._session = None

    async def call(
        self,
        messages: list,
        sample_id: Optional[str] = None,
        kind: str = "initial",
    ) -> CallOutcome:
        """单发一次 chat/completions 请求。

        顺序不可交换：先等限流放行（不持任何资源），再获取并发槽位。
        反过来就是老代码「占着槽位睡觉」的队头阻塞。

        Args:
            messages: 多模态消息列表。
            sample_id: 样本 ID，用于 per-sample 在途指标（断言样本内串行）。
            kind: 配额分账桶：initial / retry_quality / retry_network。

        Returns:
            CallOutcome；ok=True 时 response 与 content 非空。
        """
        # 四段耗时的 t0（限流排队起点）；各段打点见下方注释
        t0 = time.monotonic()

        # ① 限流放行：等待期间只挂起本协程，不占槽位
        await self._limiter.acquire()
        t1 = time.monotonic()

        # ② 并发槽包裹完整 HTTP 生命周期
        t2 = t1
        async with self._semaphore:
            t2 = time.monotonic()
            self._track_start(sample_id, kind)
            try:
                outcome = await self._do_request(messages)
            finally:
                self._track_end(sample_id)

        if self._metrics is not None:
            self._metrics.record_request(
                sample_id, kind,
                outcome.error.value if outcome.error else "OK",
                wait_limiter=t1 - t0, wait_slot=t2 - t1,
                rtt=time.monotonic() - t2, started=t2)
        return outcome

    # ------------------------------------------------------------------

    def _track_start(self, sample_id, kind):
        self.stats.in_flight += 1
        self.stats.peak_in_flight = max(self.stats.peak_in_flight,
                                        self.stats.in_flight)
        self.stats.total_requests += 1
        self.stats.quota[kind] = self.stats.quota.get(kind, 0) + 1
        if sample_id is not None:
            n = self.stats.per_sample_in_flight.get(sample_id, 0) + 1
            self.stats.per_sample_in_flight[sample_id] = n
            self.stats.max_per_sample_in_flight = max(
                self.stats.max_per_sample_in_flight, n)

    def _track_end(self, sample_id):
        self.stats.in_flight -= 1
        if sample_id is not None:
            self.stats.per_sample_in_flight[sample_id] -= 1

    def _record_outcome(self, name: str):
        self.stats.outcomes[name] = self.stats.outcomes.get(name, 0) + 1

    def _record_usage(self, body: Dict[str, Any]):
        """累计 usage token 消耗（200 响应）；缺失 usage 时只计响应数不变。"""
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return
        t = self.stats.tokens
        t["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        t["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        t["responses_with_usage"] += 1

    async def _do_request(self, messages: list) -> CallOutcome:
        cfg = self._config
        assert self._session is not None, "client must be used as async context manager"
        req_start = time.monotonic()
        try:
            async with self._session.post(
                f"{cfg.api_endpoint}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.model,
                    "messages": messages,
                    # 生成参数全部来自 Config（默认 = 官方思考·精确档，
                    # 见 config.py docstring）；max_tokens=32768 是服务端
                    # 输出硬上限，调大被静默钳制
                    "max_tokens": cfg.max_tokens,
                    "temperature": cfg.temperature,
                    "top_p": cfg.top_p,
                    "top_k": cfg.top_k,
                    "presence_penalty": cfg.presence_penalty,
                    "chat_template_kwargs": {
                        "enable_thinking": cfg.enable_thinking,
                    },
                },
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    choice = body.get("choices", [{}])[0]
                    content = choice.get("message", {}).get("content", "")
                    self._record_usage(body)
                    if not content:
                        # token 耗尽（thinking 吃完预算）与真空响应分开：
                        # 前者是实测 43% 失败的主根因，确定性、不重试
                        if choice.get("finish_reason") == "length":
                            self._record_outcome("LENGTH_TRUNCATED")
                            return CallOutcome(
                                ok=False, response=body,
                                error=ErrorType.LENGTH_TRUNCATED,
                                usage=body.get("usage"))
                        self._record_outcome("EMPTY_RESPONSE")
                        return CallOutcome(ok=False, response=body,
                                           error=ErrorType.EMPTY_RESPONSE,
                                           usage=body.get("usage"))
                    self._record_outcome("OK")
                    return CallOutcome(ok=True, response=body,
                                       content=content,
                                       usage=body.get("usage"))

                if resp.status in (403, 429):
                    retry_after = resp.headers.get("Retry-After")
                    logger.warning("rate limited (%s)", resp.status)
                    self._record_outcome("RATE_LIMITED")
                    return CallOutcome(
                        ok=False,
                        error=ErrorType.RATE_LIMITED,
                        retry_after=float(retry_after) if retry_after else None,
                    )

                text = await resp.text()
                logger.error("API error %s: %s", resp.status, text[:200])
                self._record_outcome("API_ERROR")
                return CallOutcome(ok=False, error=ErrorType.API_ERROR)

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # 带异常类名 + 已耗时长：TimeoutError 的 str() 是空串，没有类名
            # 根本无法区分「超时被掐」与「连接被拒」（2026-07-29 实测教训）
            elapsed = time.monotonic() - req_start
            logger.warning("network error (%s, elapsed %.1fs): %s",
                           type(e).__name__, elapsed, e)
            self._record_outcome("NETWORK_ERROR")
            return CallOutcome(ok=False, error=ErrorType.NETWORK_ERROR)
