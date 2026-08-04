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

去重语义（2026-08-04 修订，按输入路径分键）：去重键 = **(输入路径,
sample_id)**——**同一路径内**同一 sample_id 多次出现时内容以最后
出现为准（更新语义：后出现的记录视为同一样本的更新版）；**跨路径**
同 id 一律视为不同样本、全部保留（下游汇报案例：三个独立切分的
part 都按位置补 sample_{i} 编号，id 整批撞车，按裸 sample_id 去重
会把 3000 条静默吞成 1000 条——sample_id 只保证单 run 内唯一，
跨路径不具区分度，design.md §5）。无 sample_id 的记录无法去重，
全部保留并计数。

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


def combine_records(tagged_inputs: List[Tuple[str, List[Dict[str, Any]]]]
                    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """拼接多路记录并按 (路径, sample_id) 分键去重，返回 (记录列表, counts)。

    tagged_inputs = [(tag, records), ...]，tag 为输入路径（abspath）。
    路径内同 id 后赢；跨路径同 id 全保留并计 cross_path_id_collisions
    （出现在 >1 个路径的 id 数）。顺序按首次出现位置。
    """
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    no_id: List[Dict[str, Any]] = []
    id_paths: Dict[str, set] = {}
    total_in = deduped = 0
    for tag, records in tagged_inputs:
        for rec in records:
            total_in += 1
            sid = rec.get("sample_id")
            if sid:
                id_paths.setdefault(sid, set()).add(tag)
                key = (tag, sid)
                if key in by_key:
                    deduped += 1
                by_key[key] = rec     # 路径内后出现的记录覆盖（视为更新）
            else:
                no_id.append(rec)
    cross_path = sum(1 for paths in id_paths.values() if len(paths) > 1)
    if cross_path:
        logger.warning(
            "%d 个 sample_id 出现在多个输入路径（跨路径撞 id，已按路径分键"
            "全部保留）；注意输出中存在重复 sample_id，下游 judge 的 "
            "checkpoint 按 sample_id 判重会跳过后者", cross_path)
    return list(by_key.values()) + no_id, {
        "total_in": total_in,
        "deduped": deduped,
        "no_sample_id": len(no_id),
        "cross_path_id_collisions": cross_path,
    }


def _write_json_atomic(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def run_combine(paths: List[str], output_dir: str) -> Dict[str, Any]:
    """执行合并并落盘，返回 combine_summary 字典。"""
    records, counts = combine_records(
        [(os.path.abspath(p), load_path(p)) for p in paths])

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
    print(f"Deduped (路径内重复 sample_id): {summary['deduped']}")
    print(f"Cross-path id collisions (跨路径撞 id，全保留): "
          f"{summary['cross_path_id_collisions']}")
    print(f"No sample_id (全部保留): {summary['no_sample_id']}")
    print(f"Final success / failed: {summary['final_success']} / "
          f"{summary['final_failed']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
