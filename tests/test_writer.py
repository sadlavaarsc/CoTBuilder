"""writer 单元测试：实时写入、断点恢复、自愈重建。"""

import json
import os

import pytest

from cotbuilder.writer import ResultWriter


def _result(sample_id: str, status: str = "success") -> dict:
    return {"sample_id": sample_id, "status": status, "attempts": 1}


class TestIncrementalAppend:
    def test_file_valid_json_after_every_save(self, tmp_path):
        """任意时刻 json.load 可读（就地追加保持文件恒为合法数组）。"""
        w = ResultWriter(str(tmp_path), flush_every=100)
        for i in range(7):
            w.save(_result(f"s{i}"))
            with open(w.success_file, encoding="utf-8") as f:
                records = json.load(f)
            assert len(records) == i + 1
            assert records[-1]["sample_id"] == f"s{i}"
        w.close()

    def test_success_failed_split(self, tmp_path):
        w = ResultWriter(str(tmp_path))
        w.save(_result("ok1", "success"))
        w.save(_result("bad1", "failed"))
        w.close()
        with open(w.success_file, encoding="utf-8") as f:
            assert [r["sample_id"] for r in json.load(f)] == ["ok1"]
        with open(w.failed_file, encoding="utf-8") as f:
            assert [r["sample_id"] for r in json.load(f)] == ["bad1"]

    def test_periodic_full_rewrite_dedupes(self, tmp_path):
        """flush_every 触发全量重写：按 sample_id 去重、文件仍合法。"""
        w = ResultWriter(str(tmp_path), flush_every=5)
        for i in range(12):
            w.save(_result(f"s{i}"))
        w.close()
        with open(w.success_file, encoding="utf-8") as f:
            records = json.load(f)
        assert len(records) == 12

    def test_resume_appends_to_old_format_file(self, tmp_path):
        """老代码/全量重写产出的 indent=2 文件可以继续就地追加。"""
        path = tmp_path / "success_samples.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump([_result("old")], f, ensure_ascii=False, indent=2)
        w = ResultWriter(str(tmp_path))
        w.save(_result("new"))
        w.close()
        with open(path, encoding="utf-8") as f:
            assert [r["sample_id"] for r in json.load(f)] == ["old", "new"]


class TestCheckpoint:
    def test_checkpoint_written_after_result(self, tmp_path):
        w = ResultWriter(str(tmp_path))
        w.save(_result("s1"))
        with open(w.checkpoint_file, encoding="utf-8") as f:
            ckpt = json.load(f)
        assert ckpt["processed_ids"] == ["s1"]
        assert "timestamp" in ckpt
        w.close()

    def test_is_processed_across_instances(self, tmp_path):
        """断点恢复：新实例从 checkpoint 识别已处理样本。"""
        w1 = ResultWriter(str(tmp_path))
        w1.save(_result("s1"))
        w1.save(_result("s2", "failed"))
        w1.close()
        w2 = ResultWriter(str(tmp_path))
        assert w2.is_processed("s1") and w2.is_processed("s2")
        assert not w2.is_processed("s3")

    def test_rebuild_from_files_when_checkpoint_lost(self, tmp_path):
        """自愈：checkpoint 删除后从结果文件重建 processed_ids。"""
        w1 = ResultWriter(str(tmp_path))
        w1.save(_result("s1"))
        w1.save(_result("s2", "failed"))
        w1.close()
        os.remove(w1.checkpoint_file)
        w2 = ResultWriter(str(tmp_path))
        assert w2.is_processed("s1") and w2.is_processed("s2")

    def test_rebuild_from_files_when_checkpoint_corrupt(self, tmp_path):
        w1 = ResultWriter(str(tmp_path))
        w1.save(_result("s1"))
        w1.close()
        with open(w1.checkpoint_file, "w") as f:
            f.write("{corrupt json")
        w2 = ResultWriter(str(tmp_path))
        assert w2.is_processed("s1")
