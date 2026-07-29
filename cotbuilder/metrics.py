"""性能追踪：四段耗时分解 + 有效 QPM 曲线 + 终态报告。

为什么需要它（真实线上排查的教训）：
现有 ClientStats 只有终态计数器，无法回答「时间花在限流排队 / 槽位排队 /
HTTP 飞行 / 退避哪一段」「有效 QPM 随时间的曲线」——「疑似单线程」这类
疑问只能靠时间戳算术人肉还原。本模块把每次请求拆成四段 monotonic 打点：

```
t0 → limiter.acquire() → t1 → semaphore 获取 → t2 → HTTP 完成 → t3
     [wait_limiter]           [wait_slot]          [rtt]
```

generator 的退避 sleep 另记 backoff 事件。四段之和 ≈ 单请求总耗时，
有效 QPM = 滑动桶发起数 × 60 / interval。

防误解（修改前必读）：
- record() 内**禁止 await**：事件记录发生在 client 的并发关键路径上
  （信号量持有期间），任何挂起都可能引入新的耦合；事件先入内存 buffer，
  由 batch 主循环顺带 flush 落盘。
- 本模块**不进关键路径决策**：只观测，不影响限流/重试/落盘任何行为。
  metrics=None（默认）时各模块零开销、零行为变化，保持可独立测试。
- 时钟统一 time.monotonic（与限流器同源，见 design.md §5.8）。
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 事件类型
KIND_REQUEST = "request"   # 一次完整请求（含四段耗时）
KIND_BACKOFF = "backoff"   # generator 的一次退避 sleep


@dataclass
class MetricsEvent:
    """单条追踪事件（内存 buffer 与 metrics.jsonl 的共同载体）。

    Attributes:
        ts: 事件时间戳（monotonic，请求事件取 t3 完成时刻）。
        sample_id: 样本 ID（可为 None）。
        kind: request / backoff。
        outcome: 请求结果（OK / NETWORK_ERROR / ...），backoff 事件为 None。
        wait_limiter: 限流排队耗时（秒）。
        wait_slot: 并发槽位排队耗时（秒）。
        rtt: HTTP 飞行耗时（秒，含服务端推理）。
        backoff: 退避时长（秒），仅 backoff 事件。
        quota_kind: 配额分账桶（initial / retry_quality / retry_network）。
    """

    ts: float
    sample_id: Optional[str]
    kind: str
    outcome: Optional[str] = None
    wait_limiter: float = 0.0
    wait_slot: float = 0.0
    rtt: float = 0.0
    backoff: float = 0.0
    quota_kind: str = "initial"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "ts": round(self.ts, 4),
            "sample_id": self.sample_id,
            "kind": self.kind,
        }
        if self.kind == KIND_REQUEST:
            d.update({
                "outcome": self.outcome,
                "quota_kind": self.quota_kind,
                "wait_limiter": round(self.wait_limiter, 4),
                "wait_slot": round(self.wait_slot, 4),
                "rtt": round(self.rtt, 4),
            })
        else:
            d["backoff"] = round(self.backoff, 4)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MetricsEvent":
        return cls(
            ts=d["ts"], sample_id=d.get("sample_id"), kind=d["kind"],
            outcome=d.get("outcome"),
            wait_limiter=d.get("wait_limiter", 0.0),
            wait_slot=d.get("wait_slot", 0.0),
            rtt=d.get("rtt", 0.0),
            backoff=d.get("backoff", 0.0),
            quota_kind=d.get("quota_kind", "initial"),
        )


def percentile(sorted_values: List[float], q: float) -> float:
    """最近邻百分位。sorted_values 必须已排序；空列表返回 0.0。"""
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1,
              max(0, int(round(q / 100.0 * (len(sorted_values) - 1)))))
    return sorted_values[idx]


class Metrics:
    """事件流 + 滑动桶聚合 + 终态报告。

    用法::

        metrics = Metrics(interval=10.0)
        # client.call 内：
        metrics.record_request(sample_id, quota_kind, outcome,
                               wait_limiter, wait_slot, rtt)
        # generator 退避时：
        metrics.record_backoff(sample_id, delay)
        # batch 主循环：
        metrics.flush_to(path)          # 增量追加 metrics.jsonl
        report = metrics.report()       # 终态，进 summary.json

    协程安全：record_* 不 await（防误解见模块 docstring）；全部方法只在
    事件循环单线程内被调用，无需锁。
    """

    def __init__(self, interval: float = 10.0,
                 clock=time.monotonic):
        self._interval = interval
        self._clock = clock
        self._t0: Optional[float] = None       # 首事件时刻（桶对齐基准）
        self.events: List[MetricsEvent] = []
        self._flushed = 0                      # 已落盘事件数（增量 flush）
        # 在途追踪（request 事件记完成时刻，需要另记发起以算在途）
        self._started: List[float] = []        # 各请求发起时刻（t0 段起点）
        self._finished: List[float] = []       # 各请求完成时刻

    # ------------------------------------------------------------------
    # 记录（禁止 await）

    def record_request(self, sample_id: Optional[str], quota_kind: str,
                       outcome: str, wait_limiter: float, wait_slot: float,
                       rtt: float, started: Optional[float] = None) -> None:
        """记录一次完成请求。

        Args:
            started: HTTP 段起点（拿到并发槽、开始发请求的时刻 t2）——
                有效 QPM 与在途数都按 HTTP 飞行口径统计；缺省取当前时刻。
        """
        now = self._clock()
        if self._t0 is None:
            self._t0 = now
        self.events.append(MetricsEvent(
            ts=now, sample_id=sample_id, kind=KIND_REQUEST,
            outcome=outcome, wait_limiter=wait_limiter,
            wait_slot=wait_slot, rtt=rtt, quota_kind=quota_kind))
        self._started.append(started if started is not None else now)
        self._finished.append(now)

    def record_backoff(self, sample_id: Optional[str], delay: float) -> None:
        now = self._clock()
        if self._t0 is None:
            self._t0 = now
        self.events.append(MetricsEvent(
            ts=now, sample_id=sample_id, kind=KIND_BACKOFF, backoff=delay))

    # ------------------------------------------------------------------
    # 滑动桶聚合

    def buckets(self) -> List[Dict[str, Any]]:
        """按 interval 分桶：发起数 / 完成数 / 在途峰值 / 错误数 / 有效 QPM。

        有效 QPM 口径：桶内发起数 × 60 / interval（发起 = 进入 HTTP 段，
        即限流放行之后——这是「实际打到服务端的速率」）。
        桶对齐基准为最早请求发起时刻（不是首事件落盘时刻——一个请求可能
        飞很久才完成，发起时刻才是时间轴的起点）。
        """
        if self._t0 is None:
            return []
        base = min(self._started) if self._started else self._t0
        span = max(self._finished + [self._t0]) - base
        n = int(span // self._interval) + 1
        out = []
        for i in range(n):
            lo = base + i * self._interval
            hi = lo + self._interval
            starts = [t for t in self._started if lo <= t < hi]
            finishes = [t for t in self._finished if lo <= t < hi]
            errors = sum(
                1 for e in self.events
                if e.kind == KIND_REQUEST and lo <= e.ts < hi
                and e.outcome != "OK")
            # 在途峰值：桶内逐事件扫描（起 +1，止 -1）
            points = sorted(
                [(t, 1) for t in starts] + [(t, -1) for t in finishes])
            cur = peak = 0
            for _, delta in points:
                cur += delta
                peak = max(peak, cur)
            out.append({
                "bucket_start": round(lo - base, 2),
                "started": len(starts),
                "finished": len(finishes),
                "errors": errors,
                "peak_in_flight": peak,
                "effective_qpm": round(len(starts) * 60.0 / self._interval, 2),
            })
        return out

    # ------------------------------------------------------------------
    # 终态报告（进 summary.json 的 metrics 块）

    def report(self) -> Dict[str, Any]:
        reqs = [e for e in self.events if e.kind == KIND_REQUEST]
        rtts = sorted(e.rtt for e in reqs)
        bks = self.buckets()
        phases = {
            "wait_limiter": sum(e.wait_limiter for e in reqs),
            "wait_slot": sum(e.wait_slot for e in reqs),
            "rtt": sum(e.rtt for e in reqs),
            "backoff": sum(e.backoff for e in self.events
                           if e.kind == KIND_BACKOFF),
        }
        total = sum(phases.values())
        eff_qpms = [b["effective_qpm"] for b in bks if b["started"] > 0]
        return {
            "total_requests": len(reqs),
            "rtt_p50": round(percentile(rtts, 50), 2),
            "rtt_p95": round(percentile(rtts, 95), 2),
            "rtt_p99": round(percentile(rtts, 99), 2),
            "phase_totals": {k: round(v, 2) for k, v in phases.items()},
            "phase_shares": {
                k: round(v / total, 4) if total else 0.0
                for k, v in phases.items()
            },
            "effective_qpm_mean": (
                round(sum(eff_qpms) / len(eff_qpms), 2) if eff_qpms else 0.0),
            "effective_qpm_min": round(min(eff_qpms), 2) if eff_qpms else 0.0,
            "buckets": bks,
        }

    # ------------------------------------------------------------------
    # 落盘（metrics.jsonl，增量追加；JSONL 无就地数组问题）

    def flush_to(self, path: str) -> int:
        """把未落盘的事件追加写入 path，返回本次写入条数。"""
        pending = self.events[self._flushed:]
        if not pending:
            return 0
        with open(path, "a", encoding="utf-8") as f:
            for e in pending:
                f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
        self._flushed = len(self.events)
        return len(pending)

    # ------------------------------------------------------------------
    # 控制台进度行

    def progress_line(self, in_flight: int, completed: int, total: int) -> str:
        """一行式实时状态，供 batch 的 reporter 协程周期性输出。

        Args:
            in_flight: 当前在途请求数——实时值只能从 client.stats 读
                （本模块只在请求完成时记事件，无法实时计算），由调用方传入。
        """
        now = self._clock()
        reqs = [e for e in self.events if e.kind == KIND_REQUEST]
        rtts = sorted(e.rtt for e in reqs)
        # 近 interval 窗口的有效 QPM（比全程均值更灵敏）
        lo = now - self._interval
        recent = sum(1 for t in self._started if lo <= t <= now)
        eff_qpm = recent * 60.0 / self._interval
        return (f"in_flight={in_flight} eff_qpm={eff_qpm:.1f} "
                f"completed={completed}/{total} "
                f"rtt_p50={percentile(rtts, 50):.1f}s")
