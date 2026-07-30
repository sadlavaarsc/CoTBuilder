"""Model judge 后处理工具（可选，不进正常工作流）。

用途：规则匹配器（STRICT 口径）判为失败的样本中，混有一批「语义一致但
字面有差异」的误判——典型来源是 GT 标注质量（空格/连字符/字段内顺序，
如 "J-123" vs "J123"）。本工具用同一个模型、纯文本（不看图）对失败样本
做改判：**只判规则判出的失败 KV pair**（comparison_result.differences），
输入极小、省时间省 token。

判定语义（用户定调「定义什么是错」）：明显多出/缺失影响实义的内容、或
字符识别不一致 → 不一致；无实义差异（空白、顺序、无义符号）→ 忽略。
**改判是保守的**：样本被改判 ⟺ 每个失败 pair 都有对应 verdict 且全部
match=true；任一 pair 缺 verdict 或判 false → 维持失败（防模型漏判
误判成功）。

边界（design.md §7）：
- 不改主流程任何模块；规则口径保持 STRICT，主流程 summary 的
  gt_analysis/quality 仍以规则判定为准，改判率单独在 judge_summary.json
  观测；
- 只改判状态，不改 predicted_json 内容（修内容是「按 GT 修」另一条线）；
- 重试仅限网络类错误（NETWORK_ERROR/RATE_LIMITED/GATEWAY_ERROR），
  复用 BackoffPolicy 与 network_max_attempts 寿命；judge 判 false 不是
  错误、不重试；
- 输出独立目录（ResultWriter 复用，自带 checkpoint 可断点续判），
  原 run 文件不动。

入口：python -m cotbuilder.judge --input <run输出目录|failed_samples.json>
--output <judge目录> --api-key <key>
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from .client import ErrorType, ExpertModelClient
from .config import Config
from .extractor import extract_json
from .ratelimit import BackoffPolicy, PacedRateLimiter
from .writer import ResultWriter

logger = logging.getLogger(__name__)

# judge 系统提示词（2026-07-30，用户定调「定义什么是错」的保守改判规则）。
# 逐字段判定、逐字段独立——样本级改判规则（全部 match=true 才改判）在
# JudgeRunner.apply_verdicts，不在 prompt 里。
JUDGE_SYSTEM_PROMPT = (
    "任务：以下字段的「提取值」与「标准答案」存在字面差异。\n"
    "请逐字段判断两者在语义上是否一致。\n"
    "\n"
    "判定规则（必须严格遵守）：\n"
    "1. 忽略无实义差异：空白字符（空格、换行、全角空格）、字符/词语顺序差异、\n"
    "   不影响实际含义的符号差异（如 \"J-123\" 与 \"J123\" 中的连字符）；\n"
    "2. 以下任一情况判定该字段不一致（match=false）：\n"
    "   a. 提取值比标准答案多出影响实际意义的内容；\n"
    "   b. 提取值缺失标准答案中存在的内容（提取值为 null 而标准答案有值）；\n"
    "   c. 提取值与标准答案存在影响实际意义的字符差异（识别错误）；\n"
    "3. null、空字符串、\"无\"、\"N/A\" 视为等价（无内容），不据此判不一致；\n"
    "4. 逐字段独立判定，只比较字段值内容本身。\n"
    "\n"
    "输出 JSON（不要输出其他内容）：\n"
    "{\"verdicts\": [{\"field\": \"字段名\", \"match\": true或false, "
    "\"reason\": \"一句话理由\"}, ...]}\n"
    "必须为输入的每个字段都给出判定。"
)


class JudgeRunner:
    """失败样本改判批处理器：读记录 → 判失败 pair → 改判 → 落盘。

    Args:
        config: 运行配置（限流/并发/超时/采样参数与主流程同一套；
            network_max_attempts 是本工具唯一的寿命账）。
    """

    def __init__(self, config: Config):
        self._config = config
        self._limiter = PacedRateLimiter(config.qpm_limit)
        self._client = ExpertModelClient(config, self._limiter)
        self._backoff = BackoffPolicy(
            base=config.backoff_base, cap=config.backoff_cap,
            jitter=config.backoff_jitter)

    # ------------------------------------------------------------------
    # 纯函数（可脱离 mock 单测）

    @staticmethod
    def judge_pairs(record: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """从记录的 comparison_result.differences 提取待判字段对。

        返回 [{"field", "predicted", "ground_truth"}, ...]；无 diff
        （纯网络失败/无 predicted_json/无 comparison_result）返回 None
        ——该记录不可判，跳过。
        """
        comparison = record.get("comparison_result")
        if not isinstance(comparison, dict):
            return None
        differences = comparison.get("differences")
        if not differences:
            return None
        return [
            {
                "field": d.get("field"),
                "predicted": d.get("predicted"),
                "ground_truth": d.get("ground_truth"),
            }
            for d in differences
        ]

    @staticmethod
    def apply_verdicts(pairs: List[Dict[str, Any]],
                       content: Optional[str]) -> Optional[Dict[str, Any]]:
        """解析模型 verdict 并按保守规则给出样本级改判结论。

        返回 {"overturned": bool, "verdicts": [...]}；content 解析不出
        或缺 verdicts 键返回 None（judge_parse_failed 路径）。
        改判 ⟺ 每个输入 pair 都有对应字段的 verdict 且 match 全为 true
        ——模型漏判的字段按未改判处理（保守默认，防漏判误判成功）。
        """
        obj = extract_json(content)
        if not isinstance(obj, dict) or not isinstance(obj.get("verdicts"), list):
            return None
        by_field: Dict[str, Dict[str, Any]] = {}
        for v in obj["verdicts"]:
            if (isinstance(v, dict) and "field" in v
                    and v["field"] not in by_field):
                by_field[v["field"]] = v
        overturned = all(
            (v := by_field.get(p["field"])) is not None
            and v.get("match") is True
            for p in pairs
        )
        return {"overturned": overturned, "verdicts": obj["verdicts"]}

    @staticmethod
    def _build_messages(pairs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """构造纯文本 judge 消息（结构仿 generator._build_messages）。"""
        lines = []
        for i, p in enumerate(pairs, 1):
            pred = json.dumps(p["predicted"], ensure_ascii=False)
            gt = json.dumps(p["ground_truth"], ensure_ascii=False)
            lines.append(
                f"{i}. 字段：{p['field']}\n   提取值：{pred}\n   标准答案：{gt}")
        user_text = "\n".join(lines)
        return [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
        ]

    # ------------------------------------------------------------------
    # 单记录改判（寿命循环：仅网络类错误退避重试）

    async def _judge_one(self, record: Dict[str, Any],
                         pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
        sample_id = record["sample_id"]
        messages = self._build_messages(pairs)
        network_life = self._config.network_max_attempts
        attempts = 0
        retry_idx = 0
        last_error: Optional[str] = None
        last_content: Optional[str] = None

        while network_life > 0:
            outcome = await self._client.call(
                messages, sample_id=sample_id, kind="judge")
            attempts += 1
            if outcome.ok:
                last_content = outcome.content
                applied = self.apply_verdicts(pairs, outcome.content)
                if applied is None:
                    logger.error("Sample %s: judge verdict unparseable",
                                 sample_id)
                    return self._result(
                        record, pairs, attempts, overturned=False,
                        content=last_content, error="verdict_unparseable",
                        failure="judge_parse_failed")
                logger.info("Sample %s: judge %s (attempt %d)", sample_id,
                            "OVERTURNED" if applied["overturned"] else "upheld",
                            attempts)
                return self._result(
                    record, pairs, attempts,
                    overturned=applied["overturned"],
                    verdicts=applied["verdicts"], content=last_content)

            last_error = outcome.error.value
            if outcome.error in (ErrorType.NETWORK_ERROR,
                                 ErrorType.RATE_LIMITED,
                                 ErrorType.GATEWAY_ERROR):
                network_life -= 1
                if network_life > 0:
                    delay = self._backoff.delay(retry_idx)
                    if outcome.retry_after:
                        delay = max(delay, outcome.retry_after)
                    retry_idx += 1
                    logger.warning(
                        "Sample %s: judge %s, retrying in %.1fs "
                        "(network life %d/%d)", sample_id, outcome.error.value,
                        delay, network_life,
                        self._config.network_max_attempts)
                    await asyncio.sleep(delay)
                continue
            # API_ERROR / EMPTY_RESPONSE / LENGTH_TRUNCATED：不重试终态，
            # 维持原判（与主流程 §5.4 同一决策：确定性失败重试=纯浪费）
            logger.error("Sample %s: judge terminal %s", sample_id,
                         outcome.error.value)
            return self._result(record, pairs, attempts, overturned=False,
                                error=last_error, failure="terminal_error")

        logger.error("Sample %s: judge network life exhausted (%s)",
                     sample_id, last_error)
        return self._result(record, pairs, attempts, overturned=False,
                            error=last_error, failure="network_exhausted")

    @staticmethod
    def _result(record: Dict[str, Any], pairs: List[Dict[str, Any]],
                attempts: int, overturned: bool,
                verdicts: Optional[list] = None,
                content: Optional[str] = None,
                error: Optional[str] = None,
                failure: Optional[str] = None) -> Dict[str, Any]:
        """结果记录 = 原记录浅拷贝 + judge_result 块；改判成功翻 status。"""
        result = dict(record)
        result["status"] = "success" if overturned else "failed"
        judge_result: Dict[str, Any] = {
            "overturned": overturned,
            "pairs": pairs,
            "attempts": attempts,
        }
        if verdicts is not None:
            judge_result["verdicts"] = verdicts
        if content is not None:
            judge_result["content"] = content
        if error is not None:
            judge_result["error"] = error
        if failure is not None:
            judge_result["failure"] = failure
        result["judge_result"] = judge_result
        return result

    # ------------------------------------------------------------------
    # 批处理

    async def run(self, records: List[Dict[str, Any]], output_dir: str,
                  progress_callback: Optional[Callable[[int, int], None]] = None
                  ) -> Dict[str, Any]:
        """改判一批失败记录，返回汇总字典（同时写 judge_summary.json）。

        writer.save 全部发生在 as_completed 主循环（事件循环天然串行，
        与 batch 同一约束，writer 无需锁）。
        """
        writer = ResultWriter(output_dir, self._config.flush_every)

        # 过滤：只判有 differences 的记录；无 diff 的跳过（无可判内容）
        judgable, skipped_no_diff = [], 0
        for r in records:
            pairs = self.judge_pairs(r)
            if pairs is None or not r.get("sample_id"):
                skipped_no_diff += 1
                continue
            judgable.append((r, pairs))

        counts = {"overturned": 0, "upheld": 0, "judge_parse_failed": 0,
                  "network_exhausted": 0, "terminal_error": 0}
        skipped_resume = 0
        futures = {}
        async with self._client:
            for record, pairs in judgable:
                if writer.is_processed(record["sample_id"]):
                    skipped_resume += 1
                    continue
                fut = asyncio.ensure_future(self._judge_one(record, pairs))
                futures[fut] = record["sample_id"]

            total = len(futures)
            completed = 0
            for fut in asyncio.as_completed(futures):
                result = await fut
                writer.save(result)
                completed += 1
                jr = result["judge_result"]
                if jr["overturned"]:
                    counts["overturned"] += 1
                elif "failure" in jr:
                    counts[jr["failure"]] += 1
                else:
                    counts["upheld"] += 1
                if progress_callback:
                    progress_callback(completed, total)
        writer.close()

        summary = self._write_summary(
            output_dir, records, counts, skipped_no_diff, skipped_resume)
        return summary

    def _write_summary(self, output_dir: str, records: List[Dict[str, Any]],
                       counts: Dict[str, int], skipped_no_diff: int,
                       skipped_resume: int) -> Dict[str, Any]:
        """judge_summary.json：计数 + config + client 指标（可追溯）。"""
        stats = self._client.stats
        judged = (counts["overturned"] + counts["upheld"]
                  + counts["judge_parse_failed"] + counts["network_exhausted"]
                  + counts["terminal_error"])
        summary = {
            "timestamp": time.time(),
            "total_records": len(records),
            "judged": judged,
            "skipped_no_differences": skipped_no_diff,
            "skipped_resume": skipped_resume,
            **counts,
            "overturn_rate": (counts["overturned"] / judged) if judged else 0.0,
            "config": {
                "model": self._config.model,
                "qpm_limit": self._config.qpm_limit,
                "max_concurrent": self._config.max_concurrent,
                "network_max_attempts": self._config.network_max_attempts,
                "request_timeout": self._config.request_timeout,
                "connect_timeout": self._config.connect_timeout,
                "max_tokens": self._config.max_tokens,
                "temperature": self._config.temperature,
                "top_p": self._config.top_p,
                "top_k": self._config.top_k,
                "presence_penalty": self._config.presence_penalty,
                "enable_thinking": self._config.enable_thinking,
            },
            "metrics": {
                "total_http_requests": stats.total_requests,
                "quota": stats.quota,
                "outcomes": stats.outcomes,
                "peak_in_flight": stats.peak_in_flight,
                "token_usage": stats.tokens,
            },
        }
        path = os.path.join(output_dir, "judge_summary.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        logger.info("Saved judge summary to %s", path)
        return summary


# ----------------------------------------------------------------------
# CLI（镜像 cli.py：只负责参数解析、日志配置与 asyncio.run）

def _load_failed_records(input_path: str) -> List[Dict[str, Any]]:
    """--input 可以是 run 输出目录（读其 failed_samples.json）或 JSON 文件。"""
    path = (os.path.join(input_path, "failed_samples.json")
            if os.path.isdir(input_path) else input_path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CoT Judge — 规则失败样本的 LLM 改判（独立后处理工具）")
    parser.add_argument("--input", required=True,
                        help="run 输出目录（读 failed_samples.json）或记录 JSON 文件")
    parser.add_argument("--output", required=True, help="judge 输出目录")
    parser.add_argument("--limit", type=int, default=None,
                        help="只处理前 N 条记录（试跑用，默认全部）")
    parser.add_argument("--api-key", required=True, help="专家模型 API 密钥")
    parser.add_argument("--api-endpoint",
                        default="https://maasrd.hikvision.com.cn/v1",
                        help="API 基础地址")
    parser.add_argument("--model", default="Qwen3.6-35B-A3B-FP8",
                        help="专家模型名称")
    parser.add_argument("--qpm-limit", type=int, default=50,
                        help="每分钟请求发起上限（含全部重试，匀速放行）")
    parser.add_argument("--max-concurrent", type=int, default=10,
                        help="在途 HTTP 请求硬上限")
    parser.add_argument("--network-max-attempts", type=int, default=5,
                        help="网络寿命：网络/限流/网关错误最大重试次数")
    parser.add_argument("--request-timeout", type=float, default=120.0,
                        help="单次请求总超时（秒）")
    parser.add_argument("--connect-timeout", type=float, default=30.0,
                        help="建立连接超时（秒）")
    parser.add_argument("--max-tokens", type=int, default=32768,
                        help="输出 token 上限（32768 为服务端硬上限）")
    parser.add_argument("--temperature", type=float, default=0.6,
                        help="采样温度，默认官方思考·精确档 0.6")
    parser.add_argument("--top-p", type=float, default=0.95,
                        help="nucleus sampling，默认官方档 0.95")
    parser.add_argument("--top-k", type=int, default=20,
                        help="top-k 采样，默认官方档 20")
    parser.add_argument("--presence-penalty", type=float, default=0.0,
                        help="存在惩罚，默认官方精确档 0")
    parser.add_argument("--no-thinking", action="store_false",
                        dest="enable_thinking",
                        help="关闭思考模式（enable_thinking=false）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("cot_judge.log", encoding="utf-8"),
        ],
    )

    records = _load_failed_records(args.input)
    print(f"Loaded {len(records)} failed records from {args.input}")
    if args.limit:
        records = records[:args.limit]
        print(f"Limited to first {len(records)} records")

    config = Config(
        api_key=args.api_key,
        api_endpoint=args.api_endpoint,
        model=args.model,
        qpm_limit=args.qpm_limit,
        max_concurrent=args.max_concurrent,
        network_max_attempts=args.network_max_attempts,
        request_timeout=args.request_timeout,
        connect_timeout=args.connect_timeout,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        presence_penalty=args.presence_penalty,
        enable_thinking=args.enable_thinking,
    )

    def progress(completed: int, total: int) -> None:
        print(f"Progress: {completed}/{total} ({completed / total * 100:.1f}%)")

    runner = JudgeRunner(config)
    summary = asyncio.run(
        runner.run(records, args.output, progress_callback=progress))

    print("\n" + "=" * 50)
    print("CoT Judge Summary")
    print("=" * 50)
    print(f"Total records: {summary['total_records']}")
    print(f"Judged: {summary['judged']}")
    print(f"Overturned (改判成功): {summary['overturned']}")
    print(f"Upheld (维持原判): {summary['upheld']}")
    print(f"Judge parse failed: {summary['judge_parse_failed']}")
    print(f"Network exhausted: {summary['network_exhausted']}")
    print(f"Terminal error: {summary['terminal_error']}")
    print(f"Skipped (无可判 diff): {summary['skipped_no_differences']}")
    print(f"Skipped (断点续判): {summary['skipped_resume']}")
    print(f"Overturn rate: {summary['overturn_rate']:.2%}")
    print("=" * 50)


if __name__ == "__main__":
    main()
