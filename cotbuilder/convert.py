"""ShareGPT 数据集转换工具（纯离线只读，不进主流程、不触网络）。

把 run / merged / judge 输出目录中的记录转换为下游训练框架（LLaMA-
Factory 等）可直接读取的 ShareGPT 格式：

    {"conversations": [{"from": "human", "value": "<image>\n<prompt>\n/think"},
                       {"from": "gpt",   "value": "<think>推理链</think>\n<answer>{答案JSON}</answer>"}],
     "images": ["<原始图片路径>"]}

gpt 轮形态（Qwen3 式双标签，2026-08-03 用户拍板）：答案统一
`<answer>{JSON}</answer>` 包裹，thinking 模式有推理链时前置
`<think>推理链</think>`——vLLM 等下游一条规则即可切出 thinking 段 /
answer 段；标签名 --think-tag/--answer-tag 可覆盖。

gpt 轮三种 --gpt-mode（默认 thinking）：
- thinking：`<think>推理链</think>\n<answer>{JSON}</answer>`；推理链
  为空则仅 `<answer>`。推理链回收优先级（design.md §5.21）：
  ① full_api_response 的 choices[0].message.reasoning_content；
  ② cot_response 剥离 JSON span 后的文本（extractor.find_json_span，
  连同代码围栏 strip）；仍为空则**不加 <think> 段**（不拼假推理链）；
- raw：cot_response 原文不动（忠实模式，**不参与 think/answer 提取
  契约**；--strip-fences 可删 ```json 围栏）；
- json：纯 `<answer>{JSON}</answer>`（无推理）。

容器格式（--format，默认 json）：json = JSON 数组单文件（对齐 13 万条
老数据的微调输入）；jsonl = 每行一个样本（大文件流式友好）。

附加能力（2026-08-03 用户追加）：
- **thinking 软开关标志**：human 轮末尾按 gpt 轮实际是否含推理自动加
  /think（含推理：thinking 模式有推理链、raw 模式）或 /no_think
  （json 模式、thinking 模式但推理链为空）；--think-flag/--no-think-flag
  可自定义文案，空串禁用；
- **--strip-fences**：删掉 raw 模式 cot_response 中的 ```json 围栏标记
  （teacher 模型爱自带围栏），保留 JSON 内容；
- **--mix 无 CoT 混入**：--mix <shareGPT文件> --mix-ratio R，从外部
  ShareGPT 数据混入 int(R × CoT 条数) 条（固定种子抽样可复现），每条
  自动剥 <thinking> 段 + 加 no-think 标志；
- **--include-failed failed 派生入训**（2026-08-03 用户拍板）：把输入
  目录 failed_samples.json 中符合条件的记录转为 /no_think + **GT 答案**
  的硬样本（hard example mining）。档位：upheld（judge 维持原判，
  规则+模型双重确认，默认）/ mismatch（有规则 diff 证据）/ all（任何
  带 GT dict）。**无 CoT 预算统一控制**：总条数 = mix_ratio × CoT 条数，
  **failed 派生优先填充、填不满由 --mix 外部数据补齐**。
  防误解（design.md §5.21 同原则）：failed 记录的 cot_response /
  predicted_json 是与 GT 不一致的错误产物，**永不作训练目标**——
  拼「错误推理 + 正确答案」是负样本。

默认只读输入目录的 success_samples.json（训练数据语义；judge 改判成功
的记录经 merge 后就在 success 里，天然包含）。调试需求用 --input 直接
指定文件。

入口：python -m cotbuilder.convert --input <目录|文件> --output <路径>
"""

import argparse
import json
import logging
import os
import random
import re
import sys
from typing import Any, Dict, List, Optional

from .extractor import find_json_span

logger = logging.getLogger(__name__)

GPT_MODES = ("thinking", "raw", "json")
CONTAINER_FORMATS = ("json", "jsonl")

_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*")

# mix sanitize 剥除的旧版推理段（<think> 与历史格式的 <thinking> 都剥）
_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>\s*", re.DOTALL)

# thinking 软开关标志（2026-08-03 用户拍板：Qwen3 风格 /think /no_think）。
# 加到 human 轮末尾——训练后模型按 prompt 中的标志决定是否输出推理段；
# 标志按样本 gpt 轮**实际是否含推理内容**逐样本选择（§5.21 同一原则：
# 标志必须如实反映内容，不为凑格式给无推理样本挂 /think）。
DEFAULT_THINK_FLAG = "/think"
DEFAULT_NO_THINK_FLAG = "/no_think"

# gpt 轮双标签（2026-08-03 用户拍板：Qwen3 式 <think>/<answer>）。
# 全来源答案统一 <answer> 包裹——vLLM 等下游一条规则即可切出
# thinking 段 / answer 段；raw 模式例外（忠实原文，不参与提取契约）。
DEFAULT_THINK_TAG = "think"
DEFAULT_ANSWER_TAG = "answer"


def _wrap_tag(tag: str, content: str) -> str:
    return f"<{tag}>{content}</{tag}>"


def _apply_think_flag(text: str, flag: str,
                      all_flags=(DEFAULT_THINK_FLAG, DEFAULT_NO_THINK_FLAG)
                      ) -> str:
    """把 thinking 标志加到 human 文本末尾（独占一行）。

    先剥掉文本中已有的任何标志行（防重复/防矛盾——混入的无 CoT 数据
    可能自带旧标志），flag 为空串时不加。
    """
    lines = [ln for ln in text.splitlines()
             if ln.strip() not in all_flags or not flag]
    if flag:
        lines.append(flag)
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    """删掉 ```json / ``` 围栏标记本身（保留其中的 JSON 内容）。"""
    return _FENCE_RE.sub("", text)


# ----------------------------------------------------------------------
# 纯函数（可脱离文件单测）

def _human_text(original_sample: Dict[str, Any]) -> str:
    """从 original_sample 取 human 轮文本（兼容 messages / conversations）。

    与 generator._build_messages 同一取值来源（generator.py:263-271）；
    content 为多模态 parts 列表时拼接其中 text 段。
    """
    content: Any = ""
    messages = original_sample.get("messages")
    if messages:
        content = messages[0].get("content", "")
    else:
        convs = original_sample.get("conversations") or []
        content = convs[0].get("value", "") if convs else ""
    if isinstance(content, list):
        content = "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""


def _reasoning_text(record: Dict[str, Any]) -> str:
    """回收推理链文本：reasoning_content 优先，回退 cot_response 剥离 JSON。

    两条路径都可能为空（服务端不回 reasoning_content 且 cot_response
    只有 JSON）——返回空串，调用方不加 <thinking> 包裹。
    """
    full = record.get("full_api_response") or {}
    try:
        reasoning = full["choices"][0]["message"]["reasoning_content"]
    except (KeyError, IndexError, TypeError):
        reasoning = None
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip()

    cot = record.get("cot_response")
    if not cot:
        return ""
    span = find_json_span(cot)
    if span is None:
        # 整段都不是 JSON → 全文视为推理文本
        return cot.strip()
    text = cot[:span[0]] + cot[span[1]:]
    return _FENCE_RE.sub("", text).strip()


def to_sharegpt(record: Dict[str, Any], mode: str = "thinking",
                strip_fences: bool = False,
                think_flag: str = DEFAULT_THINK_FLAG,
                no_think_flag: str = DEFAULT_NO_THINK_FLAG,
                think_tag: str = DEFAULT_THINK_TAG,
                answer_tag: str = DEFAULT_ANSWER_TAG
                ) -> Optional[Dict[str, Any]]:
    """把一条 run 输出记录转成 ShareGPT 样本；必需素材缺失返回 None。

    thinking/json 模式必需 predicted_json；raw 模式必需 cot_response。
    答案统一 <answer> 包裹；thinking 模式有推理链时前置 <think> 段。
    images 非空且 human 文本不含 <image> 占位符时前置（ShareGPT 约定）。
    strip_fences=True 时删掉 raw 模式 cot_response 中的 ```json 围栏标记
    （teacher 模型爱自带围栏，design.md §6e）。human 轮末尾按 gpt 轮
    实际是否含推理加 think_flag / no_think_flag（空串禁用）。
    """
    has_thinking = False
    if mode == "raw":
        answer = record.get("cot_response")
        if not answer:
            return None
        if strip_fences:
            answer = _strip_fences(answer)
        has_thinking = True   # raw 保留推理原文 → 语义上含推理
    else:
        predicted = record.get("predicted_json")
        if predicted is None:
            return None
        answer_json = _wrap_tag(
            answer_tag, json.dumps(predicted, ensure_ascii=False, indent=2))
        if mode == "json":
            answer = answer_json
        else:
            cot = _reasoning_text(record)
            answer = (f"{_wrap_tag(think_tag, cot)}\n{answer_json}"
                      if cot else answer_json)
            has_thinking = bool(cot)

    original = record.get("original_sample") or {}
    human = _human_text(original)
    images = original.get("images") or []
    if images and "<image>" not in human:
        human = "<image>\n" + human
    human = _apply_think_flag(
        human, think_flag if has_thinking else no_think_flag,
        all_flags=(think_flag, no_think_flag,
                   DEFAULT_THINK_FLAG, DEFAULT_NO_THINK_FLAG))

    return {
        "conversations": [
            {"from": "human", "value": human},
            {"from": "gpt", "value": answer},
        ],
        "images": images,
    }


def sanitize_no_cot(sample: Dict[str, Any],
                    no_think_flag: str = DEFAULT_NO_THINK_FLAG,
                    think_flag: str = DEFAULT_THINK_FLAG,
                    answer_tag: str = DEFAULT_ANSWER_TAG) -> Dict[str, Any]:
    """把一条外部无 CoT ShareGPT 样本调整为混合口径（就地修改并返回）。

    - gpt 轮剥掉任何 <think>/<thinking> 推理段与已有 <answer> 包装，
      统一重包 <answer>（无 CoT 数据不应含推理段；答案包裹与管线
      派生样本同口径，下游提取规则统一）；
    - human 轮末尾加 no_think_flag（已有的任何 think 标志行先剥掉）。
    """
    prefix, suffix = f"<{answer_tag}>", f"</{answer_tag}>"
    for turn in sample.get("conversations", []):
        value = turn.get("value")
        if not isinstance(value, str):
            continue
        if turn.get("from") == "gpt":
            value = _THINK_BLOCK_RE.sub("", value).strip()
            if value.startswith(prefix) and value.endswith(suffix):
                value = value[len(prefix):-len(suffix)].strip()
            if value:
                turn["value"] = _wrap_tag(answer_tag, value)
        elif turn.get("from") == "human":
            turn["value"] = _apply_think_flag(
                value, no_think_flag,
                all_flags=(think_flag, no_think_flag,
                           DEFAULT_THINK_FLAG, DEFAULT_NO_THINK_FLAG))
    return sample


# failed 派生档位（--include-failed）：按 GT 可信度分层。
# upheld = judge 维持原判（规则+模型双重确认 GT 对、模型错，最干净）；
# mismatch = 有规则 diff 证据（含 upheld + 未判 MISMATCH，GT 未经复核）；
# all = 任何记录（转换时再按有无 GT dict 过滤，含 infra 失败的普通样本）。
FAILED_SOURCES = ("upheld", "mismatch", "all")


def _failed_eligible(record: Dict[str, Any], source: str) -> bool:
    if source == "upheld":
        jr = record.get("judge_result")
        return (isinstance(jr, dict) and jr.get("overturned") is False
                and "failure" not in jr)
    if source == "mismatch":
        comparison = record.get("comparison_result")
        return (isinstance(comparison, dict)
                and bool(comparison.get("differences")))
    return True   # all


def failed_to_sharegpt(record: Dict[str, Any],
                       think_flag: str = DEFAULT_THINK_FLAG,
                       no_think_flag: str = DEFAULT_NO_THINK_FLAG,
                       answer_tag: str = DEFAULT_ANSWER_TAG
                       ) -> Optional[Dict[str, Any]]:
    """把一条 failed 记录转成 /no_think + GT 答案的硬样本；无 GT dict 跳过。

    gpt 轮 = <answer>ground_truth 序列化</answer>（与成功样本的答案格式
    一致）；**绝不使用**该记录的 cot_response / predicted_json（错误产物，
    见模块 docstring）。
    """
    gt = record.get("ground_truth")
    if not isinstance(gt, dict):
        return None
    original = record.get("original_sample") or {}
    human = _human_text(original)
    images = original.get("images") or []
    if images and "<image>" not in human:
        human = "<image>\n" + human
    human = _apply_think_flag(
        human, no_think_flag,
        all_flags=(think_flag, no_think_flag,
                   DEFAULT_THINK_FLAG, DEFAULT_NO_THINK_FLAG))
    return {
        "conversations": [
            {"from": "human", "value": human},
            {"from": "gpt", "value": _wrap_tag(
                answer_tag, json.dumps(gt, ensure_ascii=False, indent=2))},
        ],
        "images": images,
    }


# ----------------------------------------------------------------------
# 文件读写与批处理

def _load_records(input_path: str) -> List[Dict[str, Any]]:
    """--input 为目录时读其 success_samples.json，否则直接读该文件。"""
    path = (os.path.join(input_path, "success_samples.json")
            if os.path.isdir(input_path) else input_path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_sharegpt(path: str) -> List[Dict[str, Any]]:
    """读外部 ShareGPT 数据（.jsonl 逐行解析，否则按 JSON 数组）。"""
    with open(path, "r", encoding="utf-8") as f:
        if path.endswith(".jsonl"):
            return [json.loads(line) for line in f if line.strip()]
        return json.load(f)


def run_convert(input_path: str, output_path: str,
                mode: str = "thinking", fmt: str = "json",
                strip_fences: bool = False,
                mix_path: Optional[str] = None, mix_ratio: float = 1.0,
                think_flag: str = DEFAULT_THINK_FLAG,
                no_think_flag: str = DEFAULT_NO_THINK_FLAG,
                include_failed: Optional[str] = None,
                think_tag: str = DEFAULT_THINK_TAG,
                answer_tag: str = DEFAULT_ANSWER_TAG) -> Dict[str, Any]:
    """执行转换并落盘，返回 convert_summary 字典。

    无 CoT 预算（include_failed 或 mix_path 给定时启用）：总条数 =
    int(mix_ratio × CoT 转换条数)，**failed 派生优先填充、填不满由
    mix_path 外部数据补齐**（均未给则预算为 0）。抽样固定种子 42 可复现，
    排布顺序：CoT 样本 → failed 派生 → mix 混入（shuffle 交给训练框架）。
    """
    records = _load_records(input_path)

    samples: List[Dict[str, Any]] = []
    skipped = 0
    for rec in records:
        sample = to_sharegpt(rec, mode, strip_fences=strip_fences,
                             think_flag=think_flag, no_think_flag=no_think_flag,
                             think_tag=think_tag, answer_tag=answer_tag)
        if sample is None:
            logger.warning("记录 %s 缺必需素材，跳过",
                           rec.get("sample_id", "?"))
            skipped += 1
            continue
        samples.append(sample)

    rng = random.Random(42)   # 固定种子：同输入可复现（对齐 cli.load_samples）
    budget = (int(mix_ratio * len(samples))
              if (include_failed or mix_path) else 0)

    # 优先：failed 派生硬样本（/no_think + GT 答案）
    failed_used = 0
    if include_failed and budget > 0:
        failed_path = (os.path.join(input_path, "failed_samples.json")
                       if os.path.isdir(input_path)
                       else os.path.join(os.path.dirname(
                           os.path.abspath(input_path)), "failed_samples.json"))
        failed_records = _load_sharegpt(failed_path) \
            if os.path.exists(failed_path) else []
        if not failed_records:
            logger.warning("failed 记录为空或文件不存在（%s），预算全部留给 mix",
                           failed_path)
        eligible = [s for r in failed_records
                    if _failed_eligible(r, include_failed)
                    for s in [failed_to_sharegpt(r, think_flag, no_think_flag,
                                                 answer_tag)]
                    if s is not None]
        take = min(budget, len(eligible))
        chosen = rng.sample(eligible, take) if take < len(eligible) \
            else eligible
        samples.extend(chosen)
        failed_used = len(chosen)

    # 补齐：外部无 CoT 数据
    mixed_in = 0
    remaining = budget - failed_used
    if mix_path and remaining > 0:
        pool = _load_sharegpt(mix_path)
        take = min(remaining, len(pool))
        if remaining > len(pool):
            logger.warning("混入需求量 %d 超出混入文件体量 %d，全部取",
                           remaining, len(pool))
        chosen = rng.sample(pool, take) if take < len(pool) else list(pool)
        for sample in chosen:
            samples.append(sanitize_no_cot(sample, no_think_flag, think_flag,
                                           answer_tag))
            mixed_in += 1

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if fmt == "jsonl":
            for sample in samples:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
        else:
            json.dump(samples, f, ensure_ascii=False, indent=2)
            f.write("\n")
    os.replace(tmp, output_path)

    cot_count = len(samples) - failed_used - mixed_in
    summary: Dict[str, Any] = {
        "input": os.path.abspath(input_path),
        "output": os.path.abspath(output_path),
        "gpt_mode": mode,
        "format": fmt,
        "strip_fences": strip_fences,
        "think_flag": think_flag,
        "no_think_flag": no_think_flag,
        "think_tag": think_tag,
        "answer_tag": answer_tag,
        "total_records": len(records),
        "converted": cot_count,
        "skipped": skipped,
        "no_cot_budget": budget,
        "include_failed": include_failed,
        "failed_used": failed_used,
        "mixed_in": mixed_in,
        "mix_source": os.path.abspath(mix_path) if mix_path else None,
        "total_samples": len(samples),
    }
    summary_path = os.path.join(
        os.path.dirname(os.path.abspath(output_path)), "convert_summary.json")
    with open(summary_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(summary_path + ".tmp", summary_path)

    logger.info("Converted %d/%d records + %d failed-derived + %d mixed "
                "(%s mode, %s) -> %s", cot_count, len(records), failed_used,
                mixed_in, mode, fmt, output_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CoT Convert — run/merged 输出转 ShareGPT 训练数据（纯离线）")
    parser.add_argument("--input", required=True,
                        help="run/merged/judge 输出目录（读 success_samples.json）"
                             "或记录 JSON 文件")
    parser.add_argument("--output", required=True,
                        help="输出文件路径（建议 .json / .jsonl 扩展名）")
    parser.add_argument("--gpt-mode", choices=GPT_MODES, default="thinking",
                        help="gpt 轮形态：thinking=<thinking>包裹+JSON（默认）"
                             " / raw=cot_response 原文 / json=纯答案")
    parser.add_argument("--format", choices=CONTAINER_FORMATS, default="json",
                        help="容器格式：json=JSON 数组（默认，对齐老数据微调输入）"
                             " / jsonl=每行一个样本")
    parser.add_argument("--strip-fences", action="store_true",
                        help="删掉 raw 模式输出中的 ```json 围栏标记（保留内容）")
    parser.add_argument("--think-flag", default=DEFAULT_THINK_FLAG,
                        help="含推理样本的 human 末尾标志（默认 /think，空串禁用）")
    parser.add_argument("--no-think-flag", default=DEFAULT_NO_THINK_FLAG,
                        help="无推理样本的 human 末尾标志（默认 /no_think，空串禁用）")
    parser.add_argument("--mix", default=None, metavar="SHAREGPT_FILE",
                        help="混入外部无 CoT ShareGPT 数据（.json/.jsonl），"
                             "自动去 thinking 段 + 加 no-think 标志")
    parser.add_argument("--mix-ratio", type=float, default=1.0,
                        help="无 CoT 总预算 = ratio × CoT 转换条数（默认 1.0）；"
                             "failed 派生优先填充，不足由 --mix 补齐")
    parser.add_argument("--include-failed", nargs="?", const="upheld",
                        choices=FAILED_SOURCES, default=None,
                        metavar="SOURCE",
                        help="failed 样本派生入训（/no_think + GT 答案硬样本）："
                             "upheld（默认，judge 维持原判）/ mismatch"
                             "（有规则 diff 证据）/ all（任何带 GT 的记录）")
    parser.add_argument("--think-tag", default=DEFAULT_THINK_TAG,
                        help="gpt 轮推理段标签名（默认 think → <think>）")
    parser.add_argument("--answer-tag", default=DEFAULT_ANSWER_TAG,
                        help="gpt 轮答案段标签名（默认 answer → <answer>）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    summary = run_convert(args.input, args.output,
                          mode=args.gpt_mode, fmt=args.format,
                          strip_fences=args.strip_fences,
                          mix_path=args.mix, mix_ratio=args.mix_ratio,
                          think_flag=args.think_flag,
                          no_think_flag=args.no_think_flag,
                          include_failed=args.include_failed,
                          think_tag=args.think_tag,
                          answer_tag=args.answer_tag)

    print("\n" + "=" * 50)
    print("CoT Convert Summary")
    print("=" * 50)
    print(f"Total records: {summary['total_records']}")
    print(f"Converted (CoT): {summary['converted']}")
    print(f"Failed-derived (无CoT硬样本): {summary['failed_used']}")
    print(f"Mixed in (外部无CoT): {summary['mixed_in']}")
    print(f"Skipped (缺素材): {summary['skipped']}")
    print(f"Total samples: {summary['total_samples']}")
    print(f"Mode / format: {summary['gpt_mode']} / {summary['format']}")
    print(f"Output: {summary['output']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
