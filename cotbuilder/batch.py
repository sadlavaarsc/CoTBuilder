"""批量编排：跳过已处理 → 样本协程并发 → 逐样本落盘 → 汇总指标。

职责边界：本模块只做「哪些样本要跑、结果往哪写、指标怎么汇总」，
并发/限流/重试的正确性全部由 client / ratelimit / generator 保证，
这里没有任何并发控制逻辑（这是有意为之——审计报告 01 的教训就是
控制流缠在一起导致任何指标都无法独立验证）。
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from .client import ExpertModelClient
from .config import Config
from .generator import SampleProcessor
from .matcher import Matcher
from .ratelimit import PacedRateLimiter
from .writer import ResultWriter

logger = logging.getLogger(__name__)


class BatchRunner:
    """批量生成入口。

    用法::

        runner = BatchRunner(config)
        result = await runner.run(samples, output_dir)

    线程/协程模型：所有样本协程由同一个事件循环驱动；writer.save 只在
    as_completed 主循环中调用，天然串行，无需锁。
    """

    def __init__(self, config: Config):
        self._config = config
        self._matcher = Matcher.legacy() if config.matcher_legacy else Matcher()
        self._limiter = PacedRateLimiter(config.qpm_limit)
        self._client = ExpertModelClient(config, self._limiter)

    async def run(
        self,
        samples: List[Dict[str, Any]],
        output_dir: str,
        cot_prompt: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, Any]:
        """跑一批样本，返回批量结果（字段与老代码兼容）。

        Returns:
            含 total_samples / success_count / failed_count /
            skipped_count / success_rate / results。success_rate 分母
            为实际处理数（不含 skipped，修老代码口径错误）。
        """
        logger.info("Starting batch generation for %d samples", len(samples))
        writer = ResultWriter(output_dir, self._config.flush_every)
        processor = SampleProcessor(self._client, self._matcher, self._config)

        skipped_count = 0
        futures = []
        async with self._client:
            for i, sample in enumerate(samples):
                sample_id = sample.get("id", f"sample_{i}")
                if writer.is_processed(sample_id):
                    logger.info("Skipping already processed sample: %s",
                                sample_id)
                    skipped_count += 1
                    continue
                futures.append(asyncio.ensure_future(
                    processor.process(sample, sample_id, cot_prompt)))

            logger.info("Starting generation for %d samples (skipped %d)",
                        len(futures), skipped_count)

            results = []
            for future in asyncio.as_completed(futures):
                result = await future
                results.append(result)
                writer.save(result)
                done = len(results) + skipped_count
                if progress_callback:
                    progress_callback(done, len(samples))
                logger.info("Progress: %d/%d (skipped: %d)",
                            done, len(samples), skipped_count)
            writer.close()

        success_count = sum(1 for r in results if r["status"] == "success")
        processed = len(results)
        batch_result = {
            "total_samples": len(samples),
            "success_count": success_count,
            "failed_count": processed - success_count,
            "skipped_count": skipped_count,
            "success_rate": (success_count / processed) if processed else 0.0,
            "results": results,
        }
        self._write_summary(output_dir, batch_result, processor)
        return batch_result

    # ------------------------------------------------------------------

    def _write_summary(self, output_dir: str, batch_result: Dict[str, Any],
                       processor: SampleProcessor) -> None:
        """汇总报告：老 summary 字段 + 内建指标 + §6 GT 交叉验证分析。"""
        stats = self._client.stats
        processed = batch_result["success_count"] + batch_result["failed_count"]
        summary = {
            "timestamp": time.time(),
            "total_samples": batch_result["total_samples"],
            "success_count": batch_result["success_count"],
            "failed_count": batch_result["failed_count"],
            "skipped_count": batch_result["skipped_count"],
            "success_rate": batch_result["success_rate"],
            "config": {
                "model": self._config.model,
                "qpm_limit": self._config.qpm_limit,
                "max_concurrent": self._config.max_concurrent,
                "max_sample_attempts": self._config.max_sample_attempts,
                "network_max_attempts": self._config.network_max_attempts,
            },
            # 内建指标（audit-01 §5.5）：配额分账 / 在途峰值 / 放大倍数
            "metrics": {
                "total_http_requests": stats.total_requests,
                "quota": stats.quota,
                "outcomes": stats.outcomes,
                "peak_in_flight": stats.peak_in_flight,
                "max_per_sample_in_flight": stats.max_per_sample_in_flight,
                "amplification": (
                    stats.total_requests / processed if processed else 0.0),
            },
            # §6 GT 交叉验证离线分析（不影响主流程）
            "gt_analysis": self._matcher.aggregate(processor.verdicts),
        }
        path = os.path.join(output_dir, "summary.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        logger.info("Saved summary to %s", path)
