"""实时结果写入器：每样本完成即落盘 + checkpoint 断点恢复。

输出文件（格式与老代码完全一致，R3 数据兼容约束）：
- success_samples.json / failed_samples.json：JSON 数组，记录结构不变；
- checkpoint.json：{"timestamp": ..., "processed_ids": [...]}。

写入策略（修老代码每样本全量重写的 O(n²) IO）：
- 平时**就地追加**：文件恒为合法 JSON 数组，追加是把末尾 "]" 替换为
  ",<新记录>]"，单次 IO = O(记录大小)，任意时刻 json.load 可读；
- 每 flush_every 条或 close() 时**全量原子重写**（tmp + os.replace）：
  按 sample_id 去重（保留最后一条）、规整缩进，兼作自愈；
- checkpoint 在结果落盘成功之后才更新（tmp + os.replace 原子写），
  保证「文件里有的 id 才会被标记已处理」；
- checkpoint 丢失/损坏时从 success+failed 文件重建 processed_ids（自愈）。

调用约束：所有 save() 只发生在 batch 的 as_completed 主循环里
（事件循环天然串行），本模块不加锁。
"""

import json
import logging
import os
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)


class ResultWriter:
    """实时写入器。

    Args:
        output_dir: 输出目录，不存在则创建。
        flush_every: 每追加多少条记录做一次全量原子重写。
    """

    def __init__(self, output_dir: str, flush_every: int = 10):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.success_file = os.path.join(output_dir, "success_samples.json")
        self.failed_file = os.path.join(output_dir, "failed_samples.json")
        self.checkpoint_file = os.path.join(output_dir, "checkpoint.json")
        self._flush_every = flush_every
        self._since_flush = 0
        # 每个目标文件的记录数（启动时从磁盘恢复，追加时递增）
        self._counts = {
            self.success_file: self._file_count(self.success_file),
            self.failed_file: self._file_count(self.failed_file),
        }
        self.processed_ids = self._load_processed_ids()

    # ------------------------------------------------------------------
    # 断点恢复

    def is_processed(self, sample_id: str) -> bool:
        """样本是否已处理（断点恢复时跳过）。"""
        return sample_id in self.processed_ids

    def _load_processed_ids(self) -> set:
        """优先读 checkpoint；丢失/损坏时从结果文件自愈重建。"""
        try:
            with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("processed_ids", []))
        except (OSError, json.JSONDecodeError):
            pass
        rebuilt = set()
        for path in (self.success_file, self.failed_file):
            for record in self._read_array(path):
                if isinstance(record, dict) and "sample_id" in record:
                    rebuilt.add(record["sample_id"])
        if rebuilt:
            logger.warning(
                "checkpoint 缺失/损坏，已从结果文件重建 %d 个 processed_ids",
                len(rebuilt))
        return rebuilt

    # ------------------------------------------------------------------
    # 写入

    def save(self, result: Dict[str, Any]) -> None:
        """保存单样本结果（就地追加 + fsync），随后原子更新 checkpoint。"""
        target = (self.success_file if result["status"] == "success"
                  else self.failed_file)
        self._append_record(target, result)
        self._counts[target] += 1
        self.processed_ids.add(result["sample_id"])
        self._save_checkpoint()

        self._since_flush += 1
        if self._since_flush >= self._flush_every:
            self._full_rewrite()

    def close(self) -> None:
        """收尾：全量原子重写（去重 + 规整缩进）。"""
        self._full_rewrite()

    # ------------------------------------------------------------------

    def _append_record(self, path: str, record: Dict[str, Any]) -> None:
        """把 record 追加进 JSON 数组文件（文件保持时刻合法）。"""
        body = json.dumps(record, ensure_ascii=False, indent=2)
        body = body.replace("\n", "\n  ")
        count = self._counts[path]
        size = (os.path.getsize(path) if os.path.exists(path) else 0)
        if count == 0 and size == 0:
            with open(path, "w", encoding="utf-8") as f:
                f.write("[\n  " + body + "\n]")
                f.flush()
                os.fsync(f.fileno())
            return
        # 截掉末尾 "\n]"（count==0 时文件为 "[]"，截掉 "]"）
        with open(path, "r+b") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            tail = 2 if count > 0 else 1
            f.truncate(size - tail)
            f.seek(size - tail)
            chunk = (",\n  " if count > 0 else "\n  ") + body + "\n]"
            f.write(chunk.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())

    def _full_rewrite(self) -> None:
        """全量原子重写两个结果文件：去重（保留最后一条）+ 规整缩进。"""
        for path in (self.success_file, self.failed_file):
            records = self._read_array(path)
            if not records and not os.path.exists(path):
                continue
            deduped = {}
            for r in records:
                if isinstance(r, dict) and "sample_id" in r:
                    deduped[r["sample_id"]] = r
                else:
                    deduped[id(r)] = r
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(list(deduped.values()), f,
                          ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            self._counts[path] = len(deduped)
        self._since_flush = 0

    def _save_checkpoint(self) -> None:
        checkpoint = {
            "timestamp": time.time(),
            "processed_ids": sorted(self.processed_ids),
        }
        tmp = self.checkpoint_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(checkpoint, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.checkpoint_file)

    # ------------------------------------------------------------------

    @staticmethod
    def _read_array(path: str) -> list:
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            logger.warning("结果文件损坏，按空处理：%s", path)
            return []

    @classmethod
    def _file_count(cls, path: str) -> int:
        return len(cls._read_array(path))
