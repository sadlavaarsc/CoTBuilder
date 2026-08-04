"""combine 工具测试（纯离线，tmp_path 文件断言，无需 mock server）。

验收口径（多路径哑合并，2026-08-04 起按 (路径, sample_id) 分键去重）：
- 目录输入读 success+failed、文件输入直读；按 status 路由输出
- 路径内同 id 后赢；跨路径同 id 全部保留（sample_id 仅单 run 内唯一）
- 缺文件按空处理；计数与文件对账；输出可直接喂 convert
"""

import json

from cotbuilder.combine import combine_records, run_combine
from cotbuilder.convert import run_convert


def rec(sid, status="success", marker=None):
    r = {"sample_id": sid, "status": status, "attempts": 1,
         "original_sample": {"id": sid, "messages": [
             {"role": "user", "content": "<image>\n提取"},
             {"role": "assistant", "content": "{\"a\": 1}"}],
             "images": ["/tmp/x.jpg"]},
         "predicted_json": {"a": 1}}
    if marker:
        r["marker"] = marker
    return r


def write_dir(path, success, failed):
    path.mkdir(parents=True, exist_ok=True)
    (path / "success_samples.json").write_text(
        json.dumps(success, ensure_ascii=False))
    (path / "failed_samples.json").write_text(
        json.dumps(failed, ensure_ascii=False))
    return str(path)


def read_json(path):
    return json.loads(path.read_text())


class TestCombineRecords:
    def test_dedup_last_wins(self):
        """同一路径内同一 sample_id：内容以最后出现为准，顺序按首次出现。"""
        combined, counts = combine_records([
            ("p1", [rec("a", marker="old"), rec("b"), rec("a", marker="new")]),
        ])
        assert [r["sample_id"] for r in combined] == ["a", "b"]
        assert combined[0]["marker"] == "new"
        assert counts["total_in"] == 3 and counts["deduped"] == 1
        assert counts["cross_path_id_collisions"] == 0

    def test_cross_path_same_id_kept(self):
        """跨路径同 id（2026-08-04 修复）：一律视为不同样本全部保留，
        计 cross_path_id_collisions——sample_id 只保证单 run 内唯一。"""
        combined, counts = combine_records([
            ("part1", [rec("a", marker="p1"), rec("b")]),
            ("part2", [rec("a", marker="p2"), rec("c")]),
        ])
        assert len(combined) == 4          # 修复前会静默吞成 3 条
        a_records = [r for r in combined if r["sample_id"] == "a"]
        assert {r["marker"] for r in a_records} == {"p1", "p2"}
        assert counts["deduped"] == 0
        assert counts["cross_path_id_collisions"] == 1

    def test_no_sample_id_kept(self):
        """无 sample_id 记录无法去重，全部保留。"""
        combined, counts = combine_records([
            ("p1", [{"status": "success"}, rec("a"), {"status": "failed"}])])
        assert len(combined) == 3 and counts["no_sample_id"] == 2


class TestRunCombine:
    def test_dirs_and_files_routed_and_reconciled(self, tmp_path):
        """目录（success+failed 都读）+ 文件混合输入；按 status 路由、
        summary 与文件对账；路径内重复 id 后赢。"""
        d1 = write_dir(tmp_path / "run1",
                       [rec("s1", marker="old"), rec("s2"),
                        rec("s1", marker="new")],   # 路径内重复 → 后赢
                       [rec("f1", "failed")])
        d2 = write_dir(tmp_path / "judge",
                       [rec("s3")],
                       [rec("f2", "failed")])
        f3 = tmp_path / "extra.json"
        f3.write_text(json.dumps([rec("s4")], ensure_ascii=False))
        out = str(tmp_path / "combined")

        summary = run_combine([d1, d2, str(f3)], out)

        success = read_json(tmp_path / "combined" / "success_samples.json")
        failed = read_json(tmp_path / "combined" / "failed_samples.json")
        assert summary["final_success"] == len(success) == 4  # s1 s2 s3 s4
        assert summary["final_failed"] == len(failed) == 2    # f1 f2
        assert summary["deduped"] == 1
        assert summary["cross_path_id_collisions"] == 0
        s1 = next(r for r in success if r["sample_id"] == "s1")
        assert s1["marker"] == "new"        # 路径内后出现覆盖

    def test_three_parts_colliding_ids_all_kept(self, tmp_path):
        """下游汇报案例复现（2026-08-04）：三个独立切分的 part 都按位置
        补 sample_{i} 编号，id 整批撞车——修复前 3000 条被吞成 1000，
        修复后全部保留。"""
        dirs = []
        for part in range(3):
            dirs.append(write_dir(
                tmp_path / f"part{part}",
                [rec(f"sample_{i}", marker=f"p{part}") for i in range(5)],
                []))
        summary = run_combine(dirs, str(tmp_path / "combined"))
        success = read_json(tmp_path / "combined" / "success_samples.json")
        assert summary["final_success"] == len(success) == 15
        assert summary["cross_path_id_collisions"] == 5
        markers = {r["marker"] for r in success}
        assert markers == {"p0", "p1", "p2"}   # 三个 part 的记录都在

    def test_missing_file_tolerated(self, tmp_path):
        """目录缺 failed_samples.json：按空处理不报错。"""
        d = tmp_path / "partial"
        d.mkdir()
        (d / "success_samples.json").write_text(json.dumps([rec("s1")]))
        summary = run_combine([str(d)], str(tmp_path / "out"))
        assert summary["final_success"] == 1

    def test_output_feeds_convert(self, tmp_path):
        """合并输出目录可直接作 convert --input（格式契约成立）。"""
        d = write_dir(tmp_path / "run",
                      [rec("s1"), rec("s2")], [rec("f1", "failed")])
        combined = str(tmp_path / "combined")
        run_combine([d], combined)
        summary = run_convert(combined, str(tmp_path / "train.json"))
        assert summary["converted"] == 2
        data = read_json(tmp_path / "train.json")
        assert all("conversations" in s for s in data)
