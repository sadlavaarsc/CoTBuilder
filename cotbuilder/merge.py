"""judge 结果合并工具（纯离线只读，不进主流程、不触网络）。

把 judge 后处理（cotbuilder/judge.py）的改判结果并回原 run 数据，产出
「规则判定 + judge 改判」最终口径的数据集目录：

    merged/
      success_samples.json   # run success 原样 + judge 改判成功（带标签）
      failed_samples.json    # judge 维持原判/判失败（带标签）+ 未覆盖 run failed 原样
      merge_summary.json     # 计数对账

合并语义（2026-08-03 用户拍板：翻转 + 搬移 + 标签）：
- judge 输出记录 = 原 run 记录浅拷贝 + judge_result 块 + status 翻转
  （judge.py），因此合并**只按 sample_id join、按 status 路由，不修改
  任何已有字段**——judge_result 块的存在本身即「该记录被 judge 判过」
  的标签，下游可视化/过滤直接看这个键；
- 未被 judge 覆盖的 run 记录原样保留，不加任何字段。

反复 judge 循环（design.md §6d）：合并目录的 failed_samples.json 可
直接作为 judge 的 --input 再判一轮（记录带 comparison_result.differences，
judge 正常受理，新 judge_result 覆盖旧块），再回来 merge 即可叠加。

入口：python -m cotbuilder.merge --run <run目录> --judge <judge目录>
--output <合并目录>
"""

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def merge_records(
    run_success: List[Dict[str, Any]],
    run_failed: List[Dict[str, Any]],
    judge_success: List[Dict[str, Any]],
    judge_failed: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    """合并 run 与 judge 记录，返回 (merged_success, merged_failed, counts)。

    纯函数：不读写文件、不改传入记录。路由规则：
    - run success 原样进 merged success（与 judge 输出撞 id 时以 run
      success 为准并计 collision——理论上不会发生）；
    - run failed 被 judge 覆盖：judge 侧 status=success → merged success
      （overturned）；status=failed → merged failed（按 judge_result
      有无 failure 细分 upheld / judge_error）；
    - run failed 未被覆盖 → 原样留 merged failed（untouched_failed）；
    - judge 记录 id 在 run 两侧都不存在 → 跳过（orphaned）。
    """
    judged: Dict[str, Dict[str, Any]] = {}
    for rec in judge_success + judge_failed:
        sid = rec.get("sample_id")
        if sid:
            judged[sid] = rec

    counts = {
        "run_success": len(run_success),
        "run_failed": len(run_failed),
        "judged_overturned": 0,
        "judged_upheld": 0,
        "judged_error": 0,
        "untouched_failed": 0,
        "orphaned": 0,
        "collision": 0,
    }
    merged_success = list(run_success)
    merged_failed: List[Dict[str, Any]] = []
    run_success_ids = {r.get("sample_id") for r in run_success}
    seen_judged: set = set()

    for sid in sorted(judged):
        if sid in run_success_ids:
            logger.warning("judge 记录 %s 与 run success 撞 id，以 run 为准", sid)
            counts["collision"] += 1
            seen_judged.add(sid)

    for rec in run_failed:
        sid = rec.get("sample_id")
        jrec = judged.get(sid)
        if jrec is None:
            merged_failed.append(rec)
            counts["untouched_failed"] += 1
            continue
        seen_judged.add(sid)
        if jrec.get("status") == "success":
            merged_success.append(jrec)
            counts["judged_overturned"] += 1
        else:
            merged_failed.append(jrec)
            failure = (jrec.get("judge_result") or {}).get("failure")
            counts["judged_error" if failure else "judged_upheld"] += 1

    counts["orphaned"] = len(judged) - len(seen_judged)
    for sid in sorted(judged):
        if sid not in seen_judged:
            logger.warning("judge 记录 %s 在 run 中不存在，跳过（orphaned）", sid)

    counts["final_success"] = len(merged_success)
    counts["final_failed"] = len(merged_failed)
    return merged_success, merged_failed, counts


# ----------------------------------------------------------------------
# 文件读写（原子写，仿 writer._full_rewrite 的 tmp + os.replace）

def _load_records(path: str, required: bool) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        if required:
            raise FileNotFoundError(f"缺少必需文件: {path}")
        logger.warning("文件不存在，按空处理: %s", path)
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def run_merge(run_dir: str, judge_dir: str, output_dir: str) -> Dict[str, Any]:
    """执行合并并落盘，返回 merge_summary 字典。"""
    run_success = _load_records(
        os.path.join(run_dir, "success_samples.json"), required=True)
    run_failed = _load_records(
        os.path.join(run_dir, "failed_samples.json"), required=True)
    judge_success = _load_records(
        os.path.join(judge_dir, "success_samples.json"), required=False)
    judge_failed = _load_records(
        os.path.join(judge_dir, "failed_samples.json"), required=False)

    merged_success, merged_failed, counts = merge_records(
        run_success, run_failed, judge_success, judge_failed)

    os.makedirs(output_dir, exist_ok=True)
    _write_json_atomic(
        os.path.join(output_dir, "success_samples.json"), merged_success)
    _write_json_atomic(
        os.path.join(output_dir, "failed_samples.json"), merged_failed)

    summary: Dict[str, Any] = {
        "run_dir": os.path.abspath(run_dir),
        "judge_dir": os.path.abspath(judge_dir),
        **counts,
    }
    _write_json_atomic(os.path.join(output_dir, "merge_summary.json"), summary)
    logger.info("Merged %d success / %d failed into %s",
                counts["final_success"], counts["final_failed"], output_dir)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CoT Merge — judge 改判结果并回原 run 数据（纯离线）")
    parser.add_argument("--run", required=True, dest="run_dir",
                        help="原 run 输出目录（含 success/failed_samples.json）")
    parser.add_argument("--judge", required=True, dest="judge_dir",
                        help="judge 输出目录（python -m cotbuilder.judge 的产物）")
    parser.add_argument("--output", required=True, dest="output_dir",
                        help="合并输出目录（不得与 --run 相同）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if os.path.abspath(args.run_dir) == os.path.abspath(args.output_dir):
        parser.error("--output 不得与 --run 相同（本工具只读源目录，不原地覆盖）")

    summary = run_merge(args.run_dir, args.judge_dir, args.output_dir)

    print("\n" + "=" * 50)
    print("CoT Merge Summary")
    print("=" * 50)
    print(f"Run success / failed: {summary['run_success']} / "
          f"{summary['run_failed']}")
    print(f"Judge overturned (搬入 success): {summary['judged_overturned']}")
    print(f"Judge upheld / error (留 failed): {summary['judged_upheld']} / "
          f"{summary['judged_error']}")
    print(f"Untouched failed (未判原样): {summary['untouched_failed']}")
    print(f"Orphaned / collision: {summary['orphaned']} / "
          f"{summary['collision']}")
    print(f"Final success / failed: {summary['final_success']} / "
          f"{summary['final_failed']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
