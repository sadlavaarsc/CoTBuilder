"""多路径结果合并工具（纯离线只读，不进主流程、不触网络）。

把分散在多个路径的私有格式结果（run 输出目录、judge 输出目录、
单文件 JSON 数组）拼接去重成一个标准数据集目录：

    combined/
      success_samples.json   # 所有输入中 status=success 的记录
      failed_samples.json    # 其余记录
      combine_summary.json   # 计数对账

与 merge.py 的区别：merge 是「judge 改判结果并回原 run」的语义合并
（翻转+搬移+标签，两目录 join）；combine 是**任意多路径的哑合并**——
只拼接、按 sample_id 去重、按 status 路由，不解读记录内容。输出目录
可直接作为 convert --input、judge --input 或下一轮 combine 的输入。

去重语义：同一 sample_id 多次出现时**内容以最后出现的输入为准**
（后面的路径视为更新），顺序按首次出现位置。无 sample_id 的记录
无法去重，全部保留并计数。

入口：python -m cotbuilder.combine --inputs <路径1> [路径2 ...]
--output <合并目录>
"""

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def load_path(path: str) -> List[Dict[str, Any]]:
    """读一个输入路径：目录 → 其 success+failed_samples.json（缺文件按空
    并 warning）；文件 → 直接按 JSON 数组读。"""
    records: List[Dict[str, Any]] = []
    if os.path.isdir(path):
        files = [os.path.join(path, "success_samples.json"),
                 os.path.join(path, "failed_samples.json")]
    else:
        files = [path]
    for f in files:
        if not os.path.exists(f):
            logger.warning("文件不存在，按空处理: %s", f)
            continue
        with open(f, "r", encoding="utf-8") as fh:
            records.extend(json.load(fh))
    return records


def combine_records(inputs: List[List[Dict[str, Any]]]
                    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """拼接多路记录并按 sample_id 去重（后赢），返回 (记录列表, counts)。"""
    by_id: Dict[str, Dict[str, Any]] = {}
    no_id: List[Dict[str, Any]] = []
    total_in = deduped = 0
    for records in inputs:
        for rec in records:
            total_in += 1
            sid = rec.get("sample_id")
            if sid:
                if sid in by_id:
                    deduped += 1
                by_id[sid] = rec      # 后出现的输入覆盖（视为更新）
            else:
                no_id.append(rec)
    return list(by_id.values()) + no_id, {
        "total_in": total_in,
        "deduped": deduped,
        "no_sample_id": len(no_id),
    }


def _write_json_atomic(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def run_combine(paths: List[str], output_dir: str) -> Dict[str, Any]:
    """执行合并并落盘，返回 combine_summary 字典。"""
    records, counts = combine_records([load_path(p) for p in paths])

    success = [r for r in records if r.get("status") == "success"]
    failed = [r for r in records if r.get("status") != "success"]

    os.makedirs(output_dir, exist_ok=True)
    _write_json_atomic(
        os.path.join(output_dir, "success_samples.json"), success)
    _write_json_atomic(
        os.path.join(output_dir, "failed_samples.json"), failed)

    summary: Dict[str, Any] = {
        "inputs": [os.path.abspath(p) for p in paths],
        **counts,
        "final_records": len(records),
        "final_success": len(success),
        "final_failed": len(failed),
    }
    _write_json_atomic(
        os.path.join(output_dir, "combine_summary.json"), summary)
    logger.info("Combined %d records from %d paths -> %s "
                "(%d success / %d failed)", len(records), len(paths),
                output_dir, len(success), len(failed))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CoT Combine — 多路径私有格式结果拼接去重（纯离线）")
    parser.add_argument("--inputs", required=True, nargs="+",
                        help="输入路径列表：目录（读其 success+failed_samples.json）"
                             "或记录 JSON 文件；后者优先级高于前者（去重后赢）")
    parser.add_argument("--output", required=True, dest="output_dir",
                        help="合并输出目录（不得与任一输入目录相同）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    out_abs = os.path.abspath(args.output_dir)
    for p in args.inputs:
        if os.path.isdir(p) and os.path.abspath(p) == out_abs:
            parser.error("--output 不得与任一输入目录相同（本工具只读源目录）")

    summary = run_combine(args.inputs, args.output_dir)

    print("\n" + "=" * 50)
    print("CoT Combine Summary")
    print("=" * 50)
    print(f"Inputs: {len(summary['inputs'])} paths")
    print(f"Total in: {summary['total_in']}")
    print(f"Deduped (重复 sample_id): {summary['deduped']}")
    print(f"No sample_id (全部保留): {summary['no_sample_id']}")
    print(f"Final success / failed: {summary['final_success']} / "
          f"{summary['final_failed']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
