"""merge 工具测试（纯离线，tmp_path 文件断言，无需 mock server）。

验收口径（plan：judge 结果合并）：
- 翻转+搬移+标签：judge 改判成功 → merged success（原字段不动 + judge_result 块）
- 维持原判/判失败 → merged failed（带标签）；未覆盖 run failed 原样无标签
- orphaned / collision 计数；二次 merge 幂等可反复；计数与文件对账
"""

import json

from cotbuilder.merge import merge_records, run_merge

PAIR = [("发票号码", "J-123", "J123")]


def run_success_rec(sid):
    return {"sample_id": sid, "status": "success", "attempts": 1,
            "original_sample": {"id": sid},
            "predicted_json": {"发票号码": "J123"},
            "comparison_result": {"is_match": True, "differences": []}}


def run_failed_rec(sid):
    return {"sample_id": sid, "status": "failed", "attempts": 3,
            "original_sample": {"id": sid},
            "predicted_json": {"发票号码": "J-123"},
            "comparison_result": {
                "is_match": False,
                "differences": [{"field": f, "type": "mismatch",
                                 "predicted": p, "ground_truth": g}
                                for f, p, g in PAIR]},
            "error_type": "MISMATCH"}


def judged_rec(sid, overturned, failure=None):
    """模拟 judge 输出记录：原 failed 记录 + judge_result 块 + status 翻转。"""
    rec = run_failed_rec(sid)
    rec["status"] = "success" if overturned else "failed"
    jr = {"overturned": overturned, "pairs": [
        {"field": f, "predicted": p, "ground_truth": g} for f, p, g in PAIR],
        "attempts": 1}
    if failure:
        jr["failure"] = failure
    rec["judge_result"] = jr
    return rec


def write_run(tmp_path, success, failed):
    d = tmp_path / "run"
    d.mkdir()
    (d / "success_samples.json").write_text(
        json.dumps(success, ensure_ascii=False))
    (d / "failed_samples.json").write_text(
        json.dumps(failed, ensure_ascii=False))
    return str(d)


def write_judge(tmp_path, success, failed, name="judge"):
    d = tmp_path / name
    d.mkdir()
    (d / "success_samples.json").write_text(
        json.dumps(success, ensure_ascii=False))
    (d / "failed_samples.json").write_text(
        json.dumps(failed, ensure_ascii=False))
    return str(d)


def read_json(path):
    return json.loads(path.read_text())


class TestMergeRecords:
    def test_duplicate_ids_counted_behavior_unchanged(self):
        """重复 id 防御性检测（2026-08-04）：run_failed / judge 输出带
        重复 sample_id 时计数暴露，join 行为不变（judge 侧后赢）。"""
        s, f, counts = merge_records(
            [], [run_failed_rec("d1"), run_failed_rec("d1"),
                 run_failed_rec("ok1")],
            [judged_rec("d1", overturned=False),
             judged_rec("d1", overturned=True)],   # 后者赢
            [])

        assert counts["duplicate_run_failed_ids"] == 1
        assert counts["duplicate_judged_ids"] == 1
        # join 行为不变：judge 后赢（overturned），两条 d1 都被它覆盖
        assert counts["judged_overturned"] == 2
        assert counts["untouched_failed"] == 1     # ok1 未被覆盖

    def test_overturn_moves_to_success_with_tag(self):
        """改判成功：进 merged success，judge_result 标签在、原字段不动。"""
        s, f, counts = merge_records(
            [run_success_rec("ok1")], [run_failed_rec("bad1")],
            [judged_rec("bad1", overturned=True)], [])

        assert len(s) == 2 and len(f) == 0
        untagged = next(r for r in s if r["sample_id"] == "ok1")
        assert "judge_result" not in untagged  # 未被 judge 的记录不加任何字段
        flipped = next(r for r in s if r["sample_id"] == "bad1")
        assert flipped["status"] == "success"
        assert flipped["judge_result"]["overturned"] is True
        # 原字段不动：规则判定的 differences 与 predicted_json 原样保留
        assert flipped["comparison_result"]["is_match"] is False
        assert flipped["predicted_json"] == {"发票号码": "J-123"}
        assert counts["judged_overturned"] == 1

    def test_upheld_and_error_stay_failed_with_tag(self):
        """维持原判 / judge 判失败：留 merged failed，带标签，按 failure 细分。"""
        s, f, counts = merge_records(
            [], [run_failed_rec("u1"), run_failed_rec("e1")],
            [], [judged_rec("u1", overturned=False),
                 judged_rec("e1", overturned=False, failure="network_exhausted")])

        assert len(s) == 0 and len(f) == 2
        assert all(r["status"] == "failed" and "judge_result" in r for r in f)
        assert counts["judged_upheld"] == 1
        assert counts["judged_error"] == 1

    def test_untouched_failed_preserved_without_tag(self):
        """未被 judge 覆盖的 run failed：原样保留，无标签。"""
        s, f, counts = merge_records(
            [], [run_failed_rec("x1"), run_failed_rec("x2")],
            [judged_rec("x1", overturned=True)], [])

        assert [r["sample_id"] for r in f] == ["x2"]
        assert "judge_result" not in f[0]
        assert counts["untouched_failed"] == 1

    def test_orphaned_judge_record_skipped(self):
        """judge 记录 id 在 run 中不存在：跳过并计 orphaned。"""
        s, f, counts = merge_records(
            [], [run_failed_rec("a")], [judged_rec("ghost", overturned=True)], [])

        assert len(s) == 0 and len(f) == 1
        assert counts["orphaned"] == 1

    def test_collision_run_success_wins(self):
        """judge 记录与 run success 撞 id：以 run success 为准。"""
        s, f, counts = merge_records(
            [run_success_rec("dup")], [],
            [judged_rec("dup", overturned=True)], [])

        assert len(s) == 1 and "judge_result" not in s[0]
        assert counts["collision"] == 1


class TestRunMergeFiles:
    def test_counts_reconcile_with_files(self, tmp_path):
        """落盘文件记录数与 merge_summary 计数严格对账。"""
        run_dir = write_run(tmp_path,
                            [run_success_rec("ok1"), run_success_rec("ok2")],
                            [run_failed_rec(f"bad{i}") for i in range(4)])
        judge_dir = write_judge(tmp_path,
                                [judged_rec("bad0", overturned=True),
                                 judged_rec("bad1", overturned=True)],
                                [judged_rec("bad2", overturned=False)])
        out_dir = str(tmp_path / "merged")

        summary = run_merge(run_dir, judge_dir, out_dir)

        success = read_json(tmp_path / "merged" / "success_samples.json")
        failed = read_json(tmp_path / "merged" / "failed_samples.json")
        assert summary["final_success"] == len(success) == 4  # 2 + 2 翻转
        assert summary["final_failed"] == len(failed) == 2    # 1 upheld + 1 未覆盖
        assert (summary["judged_overturned"] == 2
                and summary["judged_upheld"] == 1
                and summary["untouched_failed"] == 1)
        ids = [r["sample_id"] for r in success + failed]
        assert len(ids) == len(set(ids))  # 无重复

    def test_remerge_cycle(self, tmp_path):
        """反复 judge 循环：merged failed 再判一轮 → 二次 merge 叠加翻转。"""
        run_dir = write_run(tmp_path, [run_success_rec("ok1")],
                            [run_failed_rec("bad1"), run_failed_rec("bad2")])
        judge1 = write_judge(tmp_path, [judged_rec("bad1", overturned=True)],
                             [judged_rec("bad2", overturned=False)],
                             name="judge1")
        merged1 = str(tmp_path / "merged1")
        run_merge(run_dir, judge1, merged1)

        # 第二轮：merged1 当 run，judge2 改判 bad2
        judge2 = write_judge(tmp_path, [judged_rec("bad2", overturned=True)],
                             [], name="judge2")
        merged2 = str(tmp_path / "merged2")
        summary = run_merge(merged1, judge2, merged2)

        success = read_json(tmp_path / "merged2" / "success_samples.json")
        assert summary["final_success"] == len(success) == 3
        assert read_json(tmp_path / "merged2" / "failed_samples.json") == []
        assert summary["run_success"] == 2  # merged1 的 success 含首轮翻转
