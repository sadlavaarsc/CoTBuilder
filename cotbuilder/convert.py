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

默认只读输入目录的 success_samples.json（训练数据语义；judge 改判成功
的记录经 merge 后就在 success 里，天然包含）。调试需求用 --input 直接
指定文件。

入口：python -m cotbuilder.convert --input <目录|文件> --output <路径>
"""

import argparse
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional

from .extractor import find_json_span

logger = logging.getLogger(__name__)

GPT_MODES = ("thinking", "raw", "json")
CONTAINER_FORMATS = ("json", "jsonl")

_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*")


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


def to_sharegpt(record: Dict[str, Any],
                mode: str = "thinking") -> Optional[Dict[str, Any]]:
    """把一条 run 输出记录转成 ShareGPT 样本；必需素材缺失返回 None。

    thinking/json 模式必需 predicted_json；raw 模式必需 cot_response。
    images 非空且 human 文本不含 <image> 占位符时前置（ShareGPT 约定）。
    """
    if mode == "raw":
        answer = record.get("cot_response")
        if not answer:
            return None
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

    original = record.get("original_sample") or {}
    human = _human_text(original)
    images = original.get("images") or []
    if images and "<image>" not in human:
        human = "<image>\n" + human

    return {
        "conversations": [
            {"from": "human", "value": human},
            {"from": "gpt", "value": answer},
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


def run_convert(input_path: str, output_path: str,
                mode: str = "thinking", fmt: str = "json") -> Dict[str, Any]:
    """执行转换并落盘，返回 convert_summary 字典。"""
    records = _load_records(input_path)

    samples: List[Dict[str, Any]] = []
    skipped = 0
    for rec in records:
        sample = to_sharegpt(rec, mode)
        if sample is None:
            logger.warning("记录 %s 缺必需素材，跳过",
                           rec.get("sample_id", "?"))
            skipped += 1
            continue
        samples.append(sample)

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
        "total_records": len(records),
        "converted": len(samples),
        "skipped": skipped,
    }
    summary_path = os.path.join(
        os.path.dirname(os.path.abspath(output_path)), "convert_summary.json")
    with open(summary_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(summary_path + ".tmp", summary_path)

    logger.info("Converted %d/%d records (%s mode, %s) -> %s",
                len(samples), len(records), mode, fmt, output_path)
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    summary = run_convert(args.input, args.output,
                          mode=args.gpt_mode, fmt=args.format)

    print("\n" + "=" * 50)
    print("CoT Convert Summary")
    print("=" * 50)
    print(f"Total records: {summary['total_records']}")
    print(f"Converted: {summary['converted']}")
    print(f"Skipped (缺素材): {summary['skipped']}")
    print(f"Mode / format: {summary['gpt_mode']} / {summary['format']}")
    print(f"Output: {summary['output']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
