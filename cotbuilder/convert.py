"""ShareGPT 数据集转换工具（纯离线只读，不进主流程、不触网络）。

把 run / merged / judge 输出目录中的记录转换为下游训练框架（LLaMA-
Factory 等）可直接读取的 ShareGPT 格式：

    {"conversations": [{"from": "human", "value": "<image>\n<prompt>"},
                       {"from": "gpt",   "value": "<thinking>推理链</thinking>\n{答案JSON}"}],
     "images": ["<原始图片路径>"]}

gpt 轮三种形态（--gpt-mode，默认 thinking，2026-08-03 用户拍板）：
- thinking：<thinking>推理链</thinking> + 纯 JSON 答案。推理链回收
  优先级（design.md §6e 防误解）：① full_api_response 的
  choices[0].message.reasoning_content（服务端若返回思考通道）；
  ② cot_response 剥离 JSON span 后的文本（extractor.find_json_span，
  连同代码围栏 strip）；仍为空则**不加包裹**（不要拼接 predicted_json
  凑标签）；
- raw：cot_response 原文不动（思考与 JSON 混在一起，最忠实）；
- json：纯 predicted_json 序列化答案（无推理）。

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
  自动剥 <thinking> 段 + 加 no-think 标志。

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
_THINK_BLOCK_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL)

# thinking 软开关标志（2026-08-03 用户拍板：Qwen3 风格 /think /no_think）。
# 加到 human 轮末尾——训练后模型按 prompt 中的标志决定是否输出推理段；
# 标志按样本 gpt 轮**实际是否含推理内容**逐样本选择（§5.21 同一原则：
# 标志必须如实反映内容，不为凑格式给无推理样本挂 /think）。
DEFAULT_THINK_FLAG = "/think"
DEFAULT_NO_THINK_FLAG = "/no_think"


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
                no_think_flag: str = DEFAULT_NO_THINK_FLAG
                ) -> Optional[Dict[str, Any]]:
    """把一条 run 输出记录转成 ShareGPT 样本；必需素材缺失返回 None。

    thinking/json 模式必需 predicted_json；raw 模式必需 cot_response。
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
        answer_json = json.dumps(predicted, ensure_ascii=False, indent=2)
        if mode == "json":
            answer = answer_json
        else:
            cot = _reasoning_text(record)
            answer = (f"<thinking>{cot}</thinking>\n{answer_json}"
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
                    think_flag: str = DEFAULT_THINK_FLAG) -> Dict[str, Any]:
    """把一条外部无 CoT ShareGPT 样本调整为混合口径（就地修改并返回）。

    - gpt 轮剥掉任何 <thinking>...</thinking> 段（无 CoT 数据不应含推理段）；
    - human 轮末尾加 no_think_flag（已有的任何 think 标志行先剥掉）。
    """
    for turn in sample.get("conversations", []):
        value = turn.get("value")
        if not isinstance(value, str):
            continue
        if turn.get("from") == "gpt":
            turn["value"] = _THINK_BLOCK_RE.sub("", value)
        elif turn.get("from") == "human":
            turn["value"] = _apply_think_flag(
                value, no_think_flag,
                all_flags=(think_flag, no_think_flag,
                           DEFAULT_THINK_FLAG, DEFAULT_NO_THINK_FLAG))
    return sample


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
                no_think_flag: str = DEFAULT_NO_THINK_FLAG) -> Dict[str, Any]:
    """执行转换并落盘，返回 convert_summary 字典。

    mix_path 给定时，从该文件按比例混入无 CoT ShareGPT 样本：混入条数 =
    int(mix_ratio × CoT 转换条数)（超出混入文件体量则全取并 warning），
    每条经 sanitize_no_cot 调整（去 thinking 段 + no_think 标志），
    追加在 CoT 样本之后（下游 shuffle 交给训练框架）。
    """
    records = _load_records(input_path)

    samples: List[Dict[str, Any]] = []
    skipped = 0
    for rec in records:
        sample = to_sharegpt(rec, mode, strip_fences=strip_fences,
                             think_flag=think_flag, no_think_flag=no_think_flag)
        if sample is None:
            logger.warning("记录 %s 缺必需素材，跳过",
                           rec.get("sample_id", "?"))
            skipped += 1
            continue
        samples.append(sample)

    mixed_in = 0
    if mix_path:
        pool = _load_sharegpt(mix_path)
        take = int(mix_ratio * len(samples))
        if take > len(pool):
            logger.warning("混入需求量 %d 超出混入文件体量 %d，全部取",
                           take, len(pool))
            take = len(pool)
        rng = random.Random(42)   # 固定种子：同输入可复现（对齐 cli.load_samples）
        chosen = rng.sample(pool, take) if take < len(pool) else list(pool)
        for sample in chosen:
            samples.append(sanitize_no_cot(sample, no_think_flag, think_flag))
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

    summary: Dict[str, Any] = {
        "input": os.path.abspath(input_path),
        "output": os.path.abspath(output_path),
        "gpt_mode": mode,
        "format": fmt,
        "strip_fences": strip_fences,
        "think_flag": think_flag,
        "no_think_flag": no_think_flag,
        "total_records": len(records),
        "converted": len(samples) - mixed_in,
        "skipped": skipped,
        "mixed_in": mixed_in,
        "mix_ratio": mix_ratio if mix_path else None,
        "mix_source": os.path.abspath(mix_path) if mix_path else None,
        "total_samples": len(samples),
    }
    summary_path = os.path.join(
        os.path.dirname(os.path.abspath(output_path)), "convert_summary.json")
    with open(summary_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(summary_path + ".tmp", summary_path)

    logger.info("Converted %d/%d records + %d mixed (%s mode, %s) -> %s",
                len(samples) - mixed_in, len(records), mixed_in,
                mode, fmt, output_path)
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
                        help="混入条数 = ratio × CoT 转换条数（默认 1.0；"
                             "超出混入文件体量则全取）")
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
                          no_think_flag=args.no_think_flag)

    print("\n" + "=" * 50)
    print("CoT Convert Summary")
    print("=" * 50)
    print(f"Total records: {summary['total_records']}")
    print(f"Converted (CoT): {summary['converted']}")
    print(f"Mixed in (无 CoT): {summary['mixed_in']}")
    print(f"Skipped (缺素材): {summary['skipped']}")
    print(f"Total samples: {summary['total_samples']}")
    print(f"Mode / format: {summary['gpt_mode']} / {summary['format']}")
    print(f"Output: {summary['output']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
