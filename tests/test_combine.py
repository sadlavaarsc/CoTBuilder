"""combine 工具测试（纯离线，tmp_path 文件断言，无需 mock server）。

验收口径（多路径哑合并）：
- 目录输入读 success+failed、文件输入直读；按 status 路由输出
- sample_id 去重后赢（后面的路径覆盖）；无 id 记录全部保留
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
        """同一 sample_id 多次出现：内容以最后输入为准，顺序按首次出现。"""
        combined, counts = combine_records([
            [rec("a", marker="old"), rec("b")],
            [rec("a", marker="new"), rec("c")],
        ])
        assert [r["sample_id"] for r in combined] == ["a", "b", "c"]
        assert combined[0]["marker"] == "new"
        assert counts["total_in"] == 4 and counts["deduped"] == 1

    def test_no_sample_id_kept(self):
        """无 sample_id 记录无法去重，全部保留。"""
        combined, counts = combine_records([
            [{"status": "success"}, rec("a"), {"status": "failed"}]])
        assert len(combined) == 3 and counts["no_sample_id"] == 2


class TestRunCombine:
    def test_dirs_and_files_routed_and_reconciled(self, tmp_path):
        """目录（success+failed 都读）+ 文件混合输入；按 status 路由、
        summary 与文件对账。"""
        d1 = write_dir(tmp_path / "run1",
                       [rec("s1"), rec("s2")], [rec("f1", "failed")])
        d2 = write_dir(tmp_path / "judge",
                       [rec("f1", "success", marker="judged")],  # 覆盖 d1 的 f1
                       [rec("f2", "failed")])
        f3 = tmp_path / "extra.json"
        f3.write_text(json.dumps([rec("s3")], ensure_ascii=False))
        out = str(tmp_path / "combined")

        summary = run_combine([d1, d2, str(f3)], out)

        success = read_json(tmp_path / "combined" / "success_samples.json")
        failed = read_json(tmp_path / "combined" / "failed_samples.json")
        assert summary["final_success"] == len(success) == 4  # s1 s2 f1 s3
        assert summary["final_failed"] == len(failed) == 1    # f2
        assert summary["deduped"] == 1
        judged = next(r for r in success if r["sample_id"] == "f1")
        assert judged["marker"] == "judged"   # 后路径覆盖前路径

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
