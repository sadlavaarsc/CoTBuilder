"""单样本生成管线：寿命（life）模型的串行尝试循环。

并发模型（用户确认，替代老代码的 MISMATCH 3 并发抽卡）：
- **样本内串行、跨样本并发**：每个样本协程一次只发一个请求，
  并发度靠同时处理多个样本实现；同一样本的在途请求恒 ≤ 1；
- **两本寿命账**（审计报告 01 附录 B.3.4 预算隔离）：
  - sample_life（默认 3）：只被 MISMATCH 消耗，耗尽即终止；
  - network_life（默认 5）：只被网络错误 / 403 限流消耗；
  两本账互不挤占——403 风暴烧不掉样本质量重试的额度，反之亦然；
- **排期**：MISMATCH 后立即重排（循环即重排，节奏交给限流器）；
  网络错误必须先退避（指数 + jitter）到点才能重排——「排期要求更高」；
- **收尾**：全部尝试不通过时按 matcher.rank_key 选历史最优作为失败结果。

「桶」的语义：限流器 + 并发槽的排队就是桶——每次重排都重新走
client.call → limiter.acquire → semaphore，与其他样本自然轮转，
无需自建调度器。
"""

import asyncio
import base64
import logging
from typing import Any, Dict, List, Optional

from .client import ErrorType, ExpertModelClient
from .config import Config
from .extractor import extract_json
from .matcher import Matcher, SampleVerdict
from .ratelimit import BackoffPolicy

logger = logging.getLogger(__name__)

# CoT v2.3 系统提示词（与老代码一致，prompt 内容不在本次重构范围内）
SYSTEM_PROMPT_V2_3 = (
    "你是一个专业的文档信息提取专家。请仔细分析图片中的文档，提取指定的关键信息。\n"
    "\n"
    "请按照以下步骤进行推理：\n"
    "1. 首先观察图片的整体布局和文档类型\n"
    "2. 识别文档中的关键字段位置\n"
    "3. 逐个提取每个字段的值\n"
    "4. 验证提取结果的准确性\n"
    "5. 输出完整的 JSON 格式结果\n"
    "\n"
    "提取规则（必须严格遵守）：\n"
    "1. 照抄原文：图中已有字段值必须与图中文字完全一致，不得改写、不得补全、不得省略。\n"
    "   图中有的符号（如 ¥、®、™）保留，图中没有的不得添加；\n"
    "2. 标点风格：中文字段使用中文标点（全角），英文字段使用英文标点，\n"
    "   英文逗号后保留一个空格（如 \"LAU, LAI LI\"）；\n"
    "3. 金额字段：保留图中所示的货币符号与数字格式（如图中为\"¥5.83\"，输出\"¥5.83\"）；\n"
    "4. 字段在图中不存在时，结合已有信息进行推理，得到实际的结果；\n"
    "5. 纯数字长串（发票号码、校验码等）逐位核对后再输出。\n"
    "\n"
    "请使用中文进行推理思考，最终输出 JSON 格式的结果。"
)


class SampleProcessor:
    """单样本管线：编码 → 构 messages → 寿命循环（请求/提取/比对）→ 结果。

    Args:
        client: 专家模型客户端（单发 + 错误分类，不知道重试的存在）。
        matcher: 匹配器（验收判定与诊断共用）。
        config: 运行配置（寿命参数在此读取）。
    """

    def __init__(self, client: ExpertModelClient, matcher: Matcher,
                 config: Config):
        self._client = client
        self._matcher = matcher
        self._config = config
        self._backoff = BackoffPolicy(
            base=config.backoff_base, cap=config.backoff_cap,
            jitter=config.backoff_jitter)
        # 最终判定过的 verdict（§6 GT 交叉验证分析的数据源，batch 结束时聚合）
        self.verdicts: List[SampleVerdict] = []

    async def process(self, sample: Dict[str, Any], sample_id: str,
                      cot_prompt: Optional[str] = None) -> Dict[str, Any]:
        """处理一个样本，返回结果字典（字段结构与老代码兼容）。"""
        logger.info("Processing sample %s", sample_id)

        ground_truth = extract_json(self._gt_text(sample))
        if not ground_truth:
            logger.error("Failed to parse ground truth for sample %s", sample_id)
            return self._build_result(
                sample_id, sample, status="failed",
                error="Invalid ground truth format",
                error_type="JSON_PARSE_ERROR")

        messages = self._build_messages(sample, cot_prompt)

        sample_life = self._config.max_sample_attempts
        network_life = self._config.network_max_attempts
        network_retries = 0        # 退避索引（0 起）
        http_count = 0
        kind = "initial"           # 配额分账桶：首次 initial，之后按上次原因
        best = None                # 历史最优 (rank_key, content, response, pred, verdict)
        last_error: Optional[str] = None

        while sample_life > 0 and network_life > 0:
            outcome = await self._client.call(
                messages, sample_id=sample_id, kind=kind)
            http_count += 1

            if outcome.ok:
                predicted = extract_json(outcome.content)
                if not predicted:
                    return self._build_result(
                        sample_id, sample, status="failed", attempts=http_count,
                        error="Failed to extract JSON from response",
                        error_type="JSON_PARSE_ERROR",
                        cot_response=outcome.content,
                        full_api_response=outcome.response,
                        ground_truth=ground_truth)
                verdict = self._matcher.compare(predicted, ground_truth)
                best = self._keep_better(
                    best, verdict, outcome.content, outcome.response, predicted)
                if verdict.is_accepted:
                    logger.info("Sample %s: %s (attempt %d)",
                                sample_id, verdict.level, http_count)
                    return self._success_result(
                        sample_id, sample, http_count, best, ground_truth)
                # MISMATCH：消耗样本寿命，立即重排
                sample_life -= 1
                kind = "retry_quality"
                last_error = "MISMATCH"
                continue

            if outcome.error in (ErrorType.NETWORK_ERROR, ErrorType.RATE_LIMITED):
                # 网络/限流：消耗网络寿命，退避到点才重排（排期要求更高）
                network_life -= 1
                kind = "retry_network"
                last_error = outcome.error.value
                if network_life > 0:
                    delay = self._backoff.delay(network_retries)
                    if outcome.retry_after:
                        delay = max(delay, outcome.retry_after)
                    network_retries += 1
                    logger.warning(
                        "Sample %s: %s, retrying in %.1fs "
                        "(network life %d/%d)",
                        sample_id, outcome.error.value, delay,
                        network_life, self._config.network_max_attempts)
                    await asyncio.sleep(delay)
                continue

            # API_ERROR / EMPTY_RESPONSE：不重试，直接失败（显式决策，
            # 见 design.md——空响应重试会引入无界放大）
            logger.error("Sample %s: %s", sample_id, outcome.error.value)
            return self._build_result(
                sample_id, sample, status="failed", attempts=http_count,
                error=f"API call failed: {outcome.error.value}",
                error_type=outcome.error.value,
                full_api_response=outcome.response)

        # 寿命耗尽：历史最优收尾
        return self._exhausted_result(
            sample_id, sample, http_count, best, last_error, ground_truth)

    # ------------------------------------------------------------------
    # 寿命循环辅助

    def _keep_better(self, best, verdict: SampleVerdict, content,
                     response, predicted):
        """按 rank_key 维护历史最优尝试。"""
        candidate = (self._matcher.rank_key(verdict),
                     content, response, predicted, verdict)
        if best is None or candidate[0] > best[0]:
            return candidate
        return best

    # ------------------------------------------------------------------
    # 结果构造（唯一入口，消灭老代码 4 次重复构造）

    def _success_result(self, sample_id, sample, attempts, best,
                        ground_truth):
        _, content, response, predicted, verdict = best
        return self._build_result(
            sample_id, sample, status="success", attempts=attempts,
            cot_response=content, full_api_response=response,
            predicted_json=predicted, verdict=verdict,
            ground_truth=ground_truth)

    def _exhausted_result(self, sample_id, sample, attempts, best,
                          last_error, ground_truth):
        """寿命耗尽收尾：有最优尝试则按 MISMATCH 失败返回，否则纯网络失败。"""
        if best is not None:
            _, content, response, predicted, verdict = best
            logger.info("Sample %s: exhausted, best level %s",
                        sample_id, verdict.level)
            return self._build_result(
                sample_id, sample, status="failed", attempts=attempts,
                cot_response=content, full_api_response=response,
                predicted_json=predicted, verdict=verdict,
                error=(f"Failed to match within "
                       f"{self._config.max_sample_attempts} attempts, "
                       f"best level: {verdict.level}"),
                error_type="MISMATCH")
        return self._build_result(
            sample_id, sample, status="failed", attempts=attempts,
            error=f"All attempts failed, last error: {last_error}",
            error_type=last_error or "NETWORK_ERROR",
            ground_truth=ground_truth)

    def _build_result(self, sample_id, sample, status, attempts=1,
                      verdict=None, ground_truth=None, **optional):
        """结果字典唯一构造点。

        字段与老代码一致（R3）：sample_id / status / attempts /
        original_sample / cot_response / full_api_response /
        predicted_json / ground_truth / comparison_result / robust_match /
        error / error_type。仅新增 match_level 与逐字段明细（在
        comparison_result 内）。
        """
        result = {
            "sample_id": sample_id,
            "status": status,
            "attempts": attempts,
            "original_sample": sample,
        }
        if verdict is not None:
            comparison = verdict.to_dict()
            result["comparison_result"] = comparison
            # robust_match 与验收判定同源（audit-02 §7.3 判定一致性）
            result["robust_match"] = comparison
            result["match_level"] = verdict.level
            self.verdicts.append(verdict)
        for key in ("cot_response", "full_api_response", "predicted_json",
                    "ground_truth", "error", "error_type"):
            if key in optional and optional[key] is not None:
                result[key] = optional[key]
        if ground_truth is not None:
            result["ground_truth"] = ground_truth
        return result

    # ------------------------------------------------------------------
    # 输入处理（移植老代码语义，保持兼容）

    @staticmethod
    def _gt_text(sample: Dict[str, Any]) -> str:
        """取 Ground Truth 文本（兼容 messages / conversations 两种格式）。"""
        if "messages" in sample and len(sample.get("messages", [])) > 1:
            return sample["messages"][1].get("content", "")
        convs = sample.get("conversations", [{}, {}])
        return convs[1].get("value", "") if len(convs) > 1 else ""

    def _build_messages(self, sample: Dict[str, Any],
                        cot_prompt: Optional[str]) -> List[Dict[str, Any]]:
        """构造多模态消息（与老代码语义一致）。"""
        system_message = cot_prompt or SYSTEM_PROMPT_V2_3

        if "messages" in sample and sample["messages"]:
            user_message = sample["messages"][0].get("content", "")
        else:
            convs = sample.get("conversations", [{}])
            user_message = convs[0].get("value", "") if convs else ""

        prompt_text = user_message.replace("<image>", "").strip() \
                      or user_message.strip()

        user_content: List[Dict[str, Any]] = []
        images = sample.get("images") or []
        if images:
            image_path = images[0]
            if image_path and isinstance(image_path, str) and image_path.strip():
                image_url = self._encode_image_to_base64(image_path)
                if image_url:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    })
        user_content.append({"type": "text", "text": prompt_text})

        return [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_content},
        ]

    @staticmethod
    def _encode_image_to_base64(image_path: str) -> Optional[str]:
        """图片文件 → base64 data URL，失败返回 None。"""
        try:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            return f"data:image/jpeg;base64,{b64}"
        except OSError as e:
            logger.error("Failed to encode image %s: %s", image_path, e)
            return None
