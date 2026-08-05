"""CoT 润色/修复工具（可选后处理，不进正常工作流）。

用途：成功样本的 CoT 原文带有 thinking 模型的典型毛病（反复纠结、
自我推翻、语气词冗余），直接进训练数据会带坏学生的推理风格。本工具
把「原始问题 + CoT 文本」发给同一模型（纯文本、不看图、不给 GT），
按**规则化 prompt**（非笼统「润色」）收敛推理链，双模式：

- **polish 模式**（读 success_samples.json）：模型输出修正 CoT +
  答案，用 Matcher（STRICT 口径）比对 polished answer 与**同一次调用
  的原 predicted_json**（不是 GT——比的是「有没有改原意」）；
  对得上 → applied=true；对不上 → applied=false 保留原文（**一次定音
  不做样本内重试**，polished 文本仍存进结果块供抽查）；
- **repair 模式**（读 failed_samples.json）：连问题 + 原 CoT + GT
  丢给模型修 CoT，**不做任何一致性验证**（可靠性本就无法保证，
  2026-08-05 用户拍板），applied 即采用。

边界（与 judge.py 同一套原则，design.md §5.20/§6c）：
- 不改主流程任何模块；不改原记录任何字段——`polish_result` 块即标签，
  原 cot_response / full_api_response 原样保留可溯源；
- 重试仅限网络类错误（NETWORK_ERROR/RATE_LIMITED/GATEWAY_ERROR），
  复用 BackoffPolicy 与 network_max_attempts 寿命；answer_changed /
  parse_failed / 终态错误不是网络问题，不重试；
- 输出独立目录（ResultWriter 复用，自带 checkpoint 可断点续跑），
  源文件不动；applied → success_samples.json，其余 → failed_samples.json；
- **失败重试路径**：输出目录的 failed_samples.json 可直接作 --input
  再跑一轮（写新 --output 避开 checkpoint），再用 combine 并回——
  与反复 judge 循环同一模式。

convert 衔接：convert.reasoning_text / to_sharegpt 自动优先
polish_result（applied）的 polished_cot / polished_answer——
run → polish → convert 直接串，无需 merge。

入口：python -m cotbuilder.polish --mode polish|repair
--input <目录|文件> --output <polish目录> --api-key <key>
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
from .convert import gt_from_original_sample, human_text, reasoning_text
from .extractor import extract_json
from .matcher import Matcher
from .ratelimit import BackoffPolicy, PacedRateLimiter
from .writer import ResultWriter

logger = logging.getLogger(__name__)

MODES = ("polish", "repair")

# polish 系统提示词（2026-08-05：用户调研结论——不写笼统「润色」，
# 写规则化条目 + few-shot 例如）。输出契约 {"cot", "answer"}。
POLISH_SYSTEM_PROMPT = (
    "任务：下面是一段文档信息提取的推理过程和最终答案。请按规则修正"
    "推理过程，并原样给出最终答案。\n"
    "\n"
    "修正规则（必须严格遵守）：\n"
    "1. 反复纠结、自我推翻的内容，只保留第一次提出和最终结论。"
    "   例如原文「总价是 5.83……不对，再核对一下……应该是 5.83……"
    "   等等，再确认一遍……嗯，5.83」，修正为「总价为 5.83」；\n"
    "2. 删除无信息量的语气词和重复确认（如「好的」「让我再看看」"
    "   「没错」「确实如此」）；\n"
    "3. 推理步骤、关键事实和最终答案都不得改动：不得新增、删除或修改"
    "   任何与字段值相关的事实，不得改变答案中任何字段的值；\n"
    "4. 输出语言与原文保持一致。\n"
    "\n"
    "输出 JSON（不要输出其他内容）：\n"
    "{\"cot\": \"修正后的推理过程\", \"answer\": {最终答案 JSON}}\n"
    "answer 必须与输入的最终答案完全一致。"
)

# repair 系统提示词（参考答案修推理；不做验证，可靠性下游抽查兜底）。
REPAIR_SYSTEM_PROMPT = (
    "任务：下面是一段文档信息提取的推理过程，其最终答案是错误的。"
    "请参考标准答案，重新写出正确的推理过程和最终答案。\n"
    "\n"
    "修正规则（必须严格遵守）：\n"
    "1. 最终答案必须与标准答案完全一致（字段与值都一致）；\n"
    "2. 推理过程必须能合理地推出该答案：先得出各个字段的值，再汇总"
    "   为答案；不得跳过推理直接照抄标准答案；\n"
    "3. 推理风格简洁自然，语言与原文保持一致。\n"
    "\n"
    "输出 JSON（不要输出其他内容）：\n"
    "{\"cot\": \"修正后的推理过程\", \"answer\": {最终答案 JSON}}"
)


class PolishRunner:
    """CoT 润色/修复批处理器：读记录 → 取素材 → 模型修正 → 校验 → 落盘。

    Args:
        config: 运行配置（限流/并发/超时/采样参数与主流程同一套；
            network_max_attempts 是本工具唯一的寿命账）。
        mode: "polish"（答案一致性校验）或 "repair"（GT 修复不验证）。
    """

    def __init__(self, config: Config, mode: str = "polish"):
        if mode not in MODES:
            raise ValueError(f"未知 mode: {mode}（可选 {MODES}）")
        self._config = config
        self._mode = mode
        self._matcher = Matcher()   # 默认 STRICT 口径（非 legacy）
        self._limiter = PacedRateLimiter(config.qpm_limit)
        self._client = ExpertModelClient(config, self._limiter)
        self._backoff = BackoffPolicy(
            base=config.backoff_base, cap=config.backoff_cap,
            jitter=config.backoff_jitter)

    # ------------------------------------------------------------------
    # 纯函数（可脱离 mock 单测）

    @staticmethod
    def polish_material(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """提取 polish 三件套：question + cot + answer（原 predicted_json）。

        缺任一 → None（skipped_no_material）。**忽略已有 polish_result
        块**——重跑时从原字段取材（reasoning_text 的 polish 优先级在
        这里故意绕开，用原 reasoning_content / cot_response）。
        """
        original = record.get("original_sample") or {}
        question = human_text(original)
        # 绕开 polish_result 优先级：复制 record 去掉标签块再回收 CoT
        bare = {k: v for k, v in record.items() if k != "polish_result"}
        cot = reasoning_text(bare)
        answer = record.get("predicted_json")
        if not question or not cot or not isinstance(answer, dict):
            return None
        return {"question": question, "cot": cot, "answer": answer}

    @staticmethod
    def repair_material(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """提取 repair 素材：question + cot（可空）+ gt（必填）。

        GT 优先 record["ground_truth"]，缺失回退 original_sample 提取
        （救 2026-08-04 修复前的旧记录，与 convert 同一回退路径）。
        """
        original = record.get("original_sample") or {}
        question = human_text(original)
        bare = {k: v for k, v in record.items() if k != "polish_result"}
        cot = reasoning_text(bare)
        gt = record.get("ground_truth")
        if not isinstance(gt, dict):
            gt = gt_from_original_sample(original)
        if not question or not isinstance(gt, dict):
            return None
        return {"question": question, "cot": cot, "gt": gt}

    @staticmethod
    def apply_polish(record: Dict[str, Any], content: Optional[str],
                     matcher: Matcher) -> Optional[Dict[str, Any]]:
        """解析 polish 输出并校验答案一致性（比对原 predicted_json，非 GT）。

        返回 {"applied": bool, "polished_cot", "polished_answer",
        "match_level"}；content 解析不出或缺 cot/answer 键返回 None
        （parse_failed 路径）。applied ⟺ verdict.is_accepted（STRICT +
        NORMALIZED_MATCH，与主流程验收同口径）。
        """
        obj = extract_json(content)
        if not isinstance(obj, dict):
            return None
        cot, answer = obj.get("cot"), obj.get("answer")
        if not isinstance(cot, str) or not isinstance(answer, dict):
            return None
        verdict = matcher.compare(answer, record.get("predicted_json"))
        return {
            "applied": verdict.is_accepted,
            "polished_cot": cot.strip(),
            "polished_answer": answer,
            "match_level": verdict.level,
        }

    @staticmethod
    def apply_repair(content: Optional[str]) -> Optional[Dict[str, Any]]:
        """解析 repair 输出——不验证（用户拍板），结构合法即 applied。"""
        obj = extract_json(content)
        if not isinstance(obj, dict):
            return None
        cot, answer = obj.get("cot"), obj.get("answer")
        if not isinstance(cot, str) or not isinstance(answer, dict):
            return None
        return {"applied": True, "polished_cot": cot.strip(),
                "polished_answer": answer}

    @staticmethod
    def _build_messages(material: Dict[str, Any], mode: str
                        ) -> List[Dict[str, Any]]:
        """构造纯文本消息（不看图；结构仿 judge._build_messages）。"""
        if mode == "polish":
            user_text = (
                f"【原始问题】\n{material['question']}\n\n"
                f"【推理过程】\n{material['cot']}\n\n"
                f"【最终答案】\n{json.dumps(material['answer'], ensure_ascii=False)}")
            system = POLISH_SYSTEM_PROMPT
        else:
            cot_block = (f"【原推理过程】\n{material['cot']}\n\n"
                         if material["cot"] else "")
            user_text = (
                f"【原始问题】\n{material['question']}\n\n"
                f"{cot_block}"
                f"【标准答案】\n{json.dumps(material['gt'], ensure_ascii=False)}")
            system = REPAIR_SYSTEM_PROMPT
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
        ]

    # ------------------------------------------------------------------
    # 单记录润色/修复（寿命循环：仅网络类错误退避重试）

    async def _polish_one(self, record: Dict[str, Any],
                          material: Dict[str, Any]) -> Dict[str, Any]:
        sample_id = record["sample_id"]
        messages = self._build_messages(material, self._mode)
        network_life = self._config.network_max_attempts
        attempts = 0
        retry_idx = 0
        last_error: Optional[str] = None
        last_content: Optional[str] = None

        while network_life > 0:
            outcome = await self._client.call(
                messages, sample_id=sample_id, kind=self._mode)
            attempts += 1
            if outcome.ok:
                last_content = outcome.content
                applied = (self.apply_polish(record, outcome.content,
                                             self._matcher)
                           if self._mode == "polish"
                           else self.apply_repair(outcome.content))
                if applied is None:
                    logger.error("Sample %s: %s output unparseable",
                                 sample_id, self._mode)
                    return self._result(
                        record, attempts, applied=False,
                        content=last_content, error="output_unparseable",
                        failure="parse_failed")
                logger.info("Sample %s: %s %s (attempt %d)", sample_id,
                            self._mode,
                            "applied" if applied["applied"] else "ANSWER_CHANGED",
                            attempts)
                return self._result(
                    record, attempts, content=last_content, **applied)

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
                        "Sample %s: %s %s, retrying in %.1fs "
                        "(network life %d/%d)", sample_id, self._mode,
                        outcome.error.value, delay, network_life,
                        self._config.network_max_attempts)
                    await asyncio.sleep(delay)
                continue
            # API_ERROR / EMPTY_RESPONSE / LENGTH_TRUNCATED：终态不重试
            logger.error("Sample %s: %s terminal %s", sample_id,
                         self._mode, outcome.error.value)
            return self._result(record, attempts, applied=False,
                                error=last_error, failure="terminal_error")

        logger.error("Sample %s: %s network life exhausted (%s)",
                     sample_id, self._mode, last_error)
        return self._result(record, attempts, applied=False,
                            error=last_error, failure="network_exhausted")

    def _result(self, record: Dict[str, Any], attempts: int,
                applied: bool, polished_cot: Optional[str] = None,
                polished_answer: Optional[Dict[str, Any]] = None,
                match_level: Optional[str] = None,
                content: Optional[str] = None,
                error: Optional[str] = None,
                failure: Optional[str] = None) -> Dict[str, Any]:
        """结果记录 = 原记录浅拷贝 + polish_result 块；applied 翻 status。

        applied=false 时 polished_cot / polished_answer 也留存（抽查用）。
        """
        result = dict(record)
        result["status"] = "success" if applied else "failed"
        polish_result: Dict[str, Any] = {
            "mode": self._mode,
            "applied": applied,
            "attempts": attempts,
        }
        if polished_cot is not None:
            polish_result["polished_cot"] = polished_cot
        if polished_answer is not None:
            polish_result["polished_answer"] = polished_answer
        if match_level is not None:
            polish_result["match_level"] = match_level
        if content is not None:
            polish_result["content"] = content
        if error is not None:
            polish_result["error"] = error
        if failure is not None:
            polish_result["failure"] = failure
        result["polish_result"] = polish_result
        return result

    # ------------------------------------------------------------------
    # 批处理

    async def run(self, records: List[Dict[str, Any]], output_dir: str,
                  progress_callback: Optional[Callable[[int, int], None]] = None
                  ) -> Dict[str, Any]:
        """润色/修复一批记录，返回汇总字典（同时写 polish_summary.json）。

        writer.save 全部发生在 as_completed 主循环（事件循环天然串行，
        与 batch/judge 同一约束，writer 无需锁）。
        """
        writer = ResultWriter(output_dir, self._config.flush_every)

        material_fn = (self.polish_material if self._mode == "polish"
                       else self.repair_material)
        workable, skipped_no_material = [], 0
        for r in records:
            material = material_fn(r)
            if material is None or not r.get("sample_id"):
                skipped_no_material += 1
                continue
            workable.append((r, material))

        counts = {"applied": 0, "answer_changed": 0, "parse_failed": 0,
                  "network_exhausted": 0, "terminal_error": 0}
        skipped_resume = 0
        futures = {}
        async with self._client:
            for record, material in workable:
                if writer.is_processed(record["sample_id"]):
                    skipped_resume += 1
                    continue
                fut = asyncio.ensure_future(self._polish_one(record, material))
                futures[fut] = record["sample_id"]

            total = len(futures)
            completed = 0
            for fut in asyncio.as_completed(futures):
                result = await fut
                writer.save(result)
                completed += 1
                pr = result["polish_result"]
                if pr["applied"]:
                    counts["applied"] += 1
                elif "failure" in pr:
                    counts[pr["failure"]] += 1
                else:
                    counts["answer_changed"] += 1
                if progress_callback:
                    progress_callback(completed, total)
        writer.close()

        summary = self._write_summary(
            output_dir, records, counts, skipped_no_material, skipped_resume)
        return summary

    def _write_summary(self, output_dir: str, records: List[Dict[str, Any]],
                       counts: Dict[str, int], skipped_no_material: int,
                       skipped_resume: int) -> Dict[str, Any]:
        """polish_summary.json：计数 + config + client 指标（可追溯）。"""
        stats = self._client.stats
        processed = sum(counts.values())
        summary = {
            "timestamp": time.time(),
            "mode": self._mode,
            "total_records": len(records),
            "processed": processed,
            "skipped_no_material": skipped_no_material,
            "skipped_resume": skipped_resume,
            **counts,
            "applied_rate": (counts["applied"] / processed)
            if processed else 0.0,
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
        path = os.path.join(output_dir, "polish_summary.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        logger.info("Saved polish summary to %s", path)
        return summary


# ----------------------------------------------------------------------
# CLI（镜像 judge.py：只负责参数解析、日志配置与 asyncio.run）

def _load_records(input_path: str, mode: str) -> List[Dict[str, Any]]:
    """--input 目录时按 mode 选文件（polish 读 success、repair 读
    failed_samples.json）；文件直读。"""
    if os.path.isdir(input_path):
        filename = ("success_samples.json" if mode == "polish"
                    else "failed_samples.json")
        path = os.path.join(input_path, filename)
    else:
        path = input_path
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CoT Polish — 思维链润色/GT 修复（独立后处理工具）")
    parser.add_argument("--mode", choices=MODES, default="polish",
                        help="polish=润色+答案一致性校验（读 success）；"
                             "repair=按 GT 修 CoT 不验证（读 failed）")
    parser.add_argument("--input", required=True,
                        help="run/merged 输出目录（按 mode 选 success/failed "
                             "文件）或记录 JSON 文件")
    parser.add_argument("--output", required=True, help="polish 输出目录")
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
            logging.FileHandler("cot_polish.log", encoding="utf-8"),
        ],
    )

    records = _load_records(args.input, args.mode)
    print(f"Loaded {len(records)} records from {args.input} "
          f"(mode={args.mode})")
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

    runner = PolishRunner(config, mode=args.mode)
    summary = asyncio.run(
        runner.run(records, args.output, progress_callback=progress))

    print("\n" + "=" * 50)
    print(f"CoT Polish Summary (mode={summary['mode']})")
    print("=" * 50)
    print(f"Total records: {summary['total_records']}")
    print(f"Processed: {summary['processed']}")
    print(f"Applied (采用): {summary['applied']}")
    print(f"Answer changed (改原意弃用): {summary['answer_changed']}")
    print(f"Parse failed: {summary['parse_failed']}")
    print(f"Network exhausted: {summary['network_exhausted']}")
    print(f"Terminal error: {summary['terminal_error']}")
    print(f"Skipped (缺素材): {summary['skipped_no_material']}")
    print(f"Skipped (断点续跑): {summary['skipped_resume']}")
    print(f"Applied rate: {summary['applied_rate']:.2%}")
    print("=" * 50)


if __name__ == "__main__":
    main()
