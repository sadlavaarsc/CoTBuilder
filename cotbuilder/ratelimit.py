"""限流与退避：两个互不知晓对方存在的独立组件。

- PacedRateLimiter：只管「何时可发起」——匀速放行闸门，不持有任何资源；
- BackoffPolicy：只管「多久后再试」——纯函数，不睡眠、不感知限流。

设计动机（审计报告 01）：
- 老代码用一个信号量同时承担并发上限与 QPM 限流，结果并发维度完全失守、
  QPM 维度靠副作用碰巧守住；线上出现 ~100QPM 尖峰后同步退避的极限环振荡
  （附录 A）。本模块是重构的 P0 地基（R1），后续所有开发不得再把两条
  控制流缠回一起。

防误解（design.md 同款警告）：
- acquire() 等待期间**不持有任何槽位/锁**，只挂起调用方协程自身；
- 匀速（paced）而非仅封顶（capped）：相邻放行间隔 ≥ 60/qpm，
  数学上蕴含任意 60s 滑窗内放行数 ≤ qpm，且杜绝冷启动突发与
  固定/滑动窗口口径错配造成的 2× 峰值。
"""

import asyncio
import random
import time
from collections import deque
from typing import Awaitable, Callable


class PacedRateLimiter:
    """匀速 QPM 限流器（预约式时间戳）。

    算法：acquire 时在锁内 O(1) 预约一个放行时刻
    ``t = max(now, last_grant + interval)``，随后在**锁外**睡到 t。
    N 个协程同时 acquire 会拿到等差放行时刻 t0, t0+Δ, t0+2Δ…，
    各自睡到自己的时刻——不存在队头阻塞（对比老代码持信号量睡窗口滑出）。

    不变量：
    - 任意两次放行时刻差 ≥ interval = 60/qpm（预约即保证，与等待者数量无关）；
    - 任意 60s 滑窗内放行数 ≤ qpm（由上一性质蕴含）；
    - 空闲后第一个 acquire 立即放行（最大「突发」为 1 个请求）。
    """

    def __init__(
        self,
        qpm: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable] = asyncio.sleep,
    ):
        """
        Args:
            qpm: 每分钟放行上限，必须为正整数。
            clock: 单调时钟（测试可注入 fake clock）。禁止 wall clock——
                NTP 跳变会破坏放行间隔不变量。
            sleep: 异步睡眠函数（测试可注入以虚拟化时间）。
        """
        if qpm <= 0:
            raise ValueError("qpm must be positive")
        self._interval = 60.0 / qpm
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_grant = float("-inf")
        self._grants: deque = deque()  # 最近 60s 的放行时刻，指标用

    @property
    def interval(self) -> float:
        """相邻放行的最小间隔（秒）。"""
        return self._interval

    async def acquire(self) -> None:
        """预约一个放行时刻并睡到该时刻。返回后即可发起一次请求。"""
        async with self._lock:
            now = self._clock()
            t = max(now, self._last_grant + self._interval)
            self._last_grant = t
            self._grants.append(t)
            while self._grants and self._grants[0] <= now - 60.0:
                self._grants.popleft()
        wait = t - now
        if wait > 0:
            await self._sleep(wait)

    def window_count(self) -> int:
        """过去 60s 内已放行数（指标观测用）。"""
        now = self._clock()
        while self._grants and self._grants[0] <= now - 60.0:
            self._grants.popleft()
        return len(self._grants)


class BackoffPolicy:
    """指数退避 + 抖动的纯函数策略（审计报告 01 附录 A.5.2）。

    ``delay(n) = min(base * 2^n, cap) * U(1-jitter, 1+jitter)``。
    抖动破坏「同时 403 → 同时入睡 → 同时醒来 → 再突发」的舰队同步结构。
    只负责算时间，不睡眠、不感知限流与并发。
    """

    def __init__(
        self,
        base: float = 5.0,
        cap: float = 60.0,
        jitter: float = 0.5,
        rng: random.Random = None,
    ):
        """
        Args:
            base: 退避基数（秒）。
            cap: 退避上限（秒）。
            jitter: 抖动幅度，0.5 表示 ±50%。
            rng: 随机源，测试可注入 seeded Random 保证可复现。
        """
        self._base = base
        self._cap = cap
        self._jitter = jitter
        self._rng = rng or random.Random()

    def delay(self, attempt: int) -> float:
        """第 attempt 次重试（0 起）的退避秒数。"""
        nominal = min(self._base * (2 ** attempt), self._cap)
        factor = self._rng.uniform(1 - self._jitter, 1 + self._jitter)
        return nominal * factor
