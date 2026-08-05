"""polish 工具测试（纯函数 + FakeClient 脚本化，不依赖 mock server）。

验收口径（2026-08-05 plan）：
- material 三件套提取：两输入格式、reasoning_content 优先 / cot 剥离、
  缺素材 → None、已有 polish_result 被忽略（重跑从原字段取材）
- apply_polish：STRICT/NORMALIZED 一致 → applied；不一致 → applied=false
  + polished 文本留存 + 不重试；parse 失败 → parse_failed
- repair：不验证，结构合法即 applied
- runner：网络类退避重试烧 network_life、终态不重试、status 路由两文件、
  checkpoint 断点续跑、summary 计数对账
"""

import json

from cotbuilder.client import CallOutcome, ErrorType
from cotbuilder.config import Config
from cotbuilder.matcher import Matcher
from cotbuilder.polish import PolishRunner

GT = {"发票号码": "J123", "总价": "¥5.83"}
COT = "先看发票号码，再看总价，逐项核对。"


class FakeClient:
    """按脚本返回 CallOutcome 的假 client，记录每次调用的 kind。"""

    def __init__(self, script):
        self._script = list(script)
        self.kinds = []

    async def call(self, messages, sample_id=None, kind="initial"):
        self.kinds.append(kind)
        return self._script.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def stats(self):
        class S:
            total_requests = 0
            quota: dict = {}
            outcomes: dict = {}
            peak_in_flight = 0
            tokens: dict = {}
        return S()


def polished_content(cot=COT, answer=None) -> str:
    return json.dumps({"cot": cot, "answer": answer or GT},
                      ensure_ascii=False)


def ok(content: str) -> CallOutcome:
    return CallOutcome(ok=True, response={"choices": []}, content=content)


def err(error: ErrorType) -> CallOutcome:
    return CallOutcome(ok=False, error=error)


def polish_record(sid, fmt="messages", reasoning=None, cot=COT,
                  answer=GT, with_polish_result=False):
    """success 记录：cot_response = CoT 文本 + JSON；可选 reasoning_content。"""
    gt_text = json.dumps(answer, ensure_ascii=False)
    if fmt == "messages":
        original = {"id": sid, "messages": [
            {"role": "user", "content": "<image>\n请提取图片中的关键信息"},
            {"role": "assistant", "content": gt_text}],
            "images": ["/tmp/x.jpg"]}
    else:
        original = {"id": sid, "conversations": [
            {"from": "human", "value": "<image>\n请提取图片中的关键信息"},
            {"from": "gpt", "value": gt_text}],
            "images": ["/tmp/x.jpg"]}
    full = {"choices": [{"message": {}}]}
    if reasoning is not None:
        full["choices"][0]["message"]["reasoning_content"] = reasoning
    rec = {"sample_id": sid, "status": "success", "attempts": 1,
           "original_sample": original,
           "cot_response": f"{cot}\n```json\n{gt_text}\n```",
           "full_api_response": full,
           "predicted_json": answer}
    if with_polish_result:
        rec["polish_result"] = {"mode": "polish", "applied": True,
                                "polished_cot": "已润色的旧推理",
                                "polished_answer": answer, "attempts": 1}
    return rec


def make_runner(script, mode="polish", tmp_path=None, **cfg):
    runner = PolishRunner(Config(api_key="k", backoff_base=0.001,
                                 backoff_cap=0.002, **cfg), mode=mode)
    runner._client = FakeClient(script)
    return runner


class TestPolishMaterial:
    def test_messages_and_conversations(self):
        for fmt in ("messages", "conversations"):
            m = PolishRunner.polish_material(polish_record("s1", fmt=fmt))
            assert m is not None
            assert "提取" in m["question"]
            assert m["cot"] == COT
            assert m["answer"] == GT

    def test_reasoning_content_priority(self):
        """reasoning_content 优先于 cot_response 剥离。"""
        m = PolishRunner.polish_material(
            polish_record("s1", reasoning="服务端推理链"))
        assert m["cot"] == "服务端推理链"

    def test_missing_material_returns_none(self):
        """无 CoT / 无 predicted_json / 无问题 → None。"""
        rec = polish_record("s1")
        rec["cot_response"] = ""          # 无 CoT
        rec["full_api_response"] = {"choices": [{"message": {}}]}
        assert PolishRunner.polish_material(rec) is None
        rec2 = polish_record("s1")
        del rec2["predicted_json"]
        assert PolishRunner.polish_material(rec2) is None

    def test_existing_polish_result_ignored(self):
        """重跑时从原字段取材，不用旧 polish_result 的 polished_cot。"""
        m = PolishRunner.polish_material(
            polish_record("s1", with_polish_result=True))
        assert m["cot"] == COT            # 原 CoT，而非「已润色的旧推理」


class TestRepairMaterial:
    def test_gt_from_ground_truth_field(self):
        rec = polish_record("s1")
        rec["ground_truth"] = GT
        m = PolishRunner.repair_material(rec)
        assert m["gt"] == GT and m["cot"] == COT

    def test_gt_fallback_from_original_sample(self):
        """无 ground_truth 字段（旧记录）→ original_sample 回退。"""
        m = PolishRunner.repair_material(polish_record("s1"))
        assert m["gt"] == GT

    def test_no_gt_anywhere_returns_none(self):
        rec = polish_record("s1")
        rec["original_sample"]["messages"][1]["content"] = "not json"
        assert PolishRunner.repair_material(rec) is None


class TestApplyPolish:
    def test_strict_identical_applied(self):
        rec = polish_record("s1")
        r = PolishRunner.apply_polish(rec, polished_content(), Matcher())
        assert r["applied"] is True and r["match_level"] == "STRICT"
        assert r["polished_cot"] == COT and r["polished_answer"] == GT

    def test_normalized_match_applied(self):
        """全角数字等格式噪声 → NORMALIZED_MATCH 也算没改原意。"""
        normalized = {"发票号码": "J123", "总价": "¥５.８３"}
        rec = polish_record("s1")
        r = PolishRunner.apply_polish(
            rec, polished_content(answer=normalized), Matcher())
        assert r["applied"] is True
        assert r["match_level"] == "NORMALIZED_MATCH"

    def test_answer_changed_not_applied(self):
        changed = {"发票号码": "J999", "总价": "¥5.83"}
        rec = polish_record("s1")
        r = PolishRunner.apply_polish(
            rec, polished_content(answer=changed), Matcher())
        assert r["applied"] is False and r["match_level"] == "MISMATCH"
        assert r["polished_answer"] == changed   # 留存供抽查

    def test_unparseable_returns_none(self):
        rec = polish_record("s1")
        assert PolishRunner.apply_polish(rec, "模型没输出 JSON", Matcher()) is None
        assert PolishRunner.apply_polish(
            rec, json.dumps({"cot": "只有cot"}), Matcher()) is None

    def test_repair_no_validation(self):
        """repair：答案与 GT 不同也 applied（不验证，用户拍板）。"""
        r = PolishRunner.apply_repair(
            polished_content(answer={"发票号码": "X", "总价": "Y"}))
        assert r["applied"] is True
        assert PolishRunner.apply_repair("垃圾文本") is None


class TestPolishRunner:
    async def test_one_shot_applied_routes_success(self, tmp_path):
        runner = make_runner([ok(polished_content())])
        summary = await runner.run([polish_record("s1")], str(tmp_path))
        assert summary["applied"] == 1
        out = json.loads((tmp_path / "success_samples.json").read_text())
        pr = out[0]["polish_result"]
        assert pr["applied"] is True and pr["mode"] == "polish"
        assert out[0]["cot_response"].startswith(COT)   # 原字段不动

    async def test_answer_changed_no_retry_routes_failed(self, tmp_path):
        changed = {"发票号码": "J999", "总价": "¥5.83"}
        runner = make_runner([ok(polished_content(answer=changed)),
                              ok(polished_content())])   # 第二个不应被消费
        summary = await runner.run([polish_record("s1")], str(tmp_path))
        assert summary["answer_changed"] == 1
        assert runner._client.kinds == ["polish"]        # 不重试
        out = json.loads((tmp_path / "failed_samples.json").read_text())
        pr = out[0]["polish_result"]
        assert pr["applied"] is False
        assert pr["polished_answer"] == changed          # 留存供抽查

    async def test_network_retry_then_applied(self, tmp_path):
        runner = make_runner([err(ErrorType.RATE_LIMITED),
                              ok(polished_content())])
        summary = await runner.run([polish_record("s1")], str(tmp_path))
        assert summary["applied"] == 1
        assert runner._client.kinds == ["polish", "polish"]

    async def test_terminal_error_no_retry(self, tmp_path):
        runner = make_runner([err(ErrorType.EMPTY_RESPONSE),
                              ok(polished_content())])
        summary = await runner.run([polish_record("s1")], str(tmp_path))
        assert summary["terminal_error"] == 1
        assert runner._client.kinds == ["polish"]

    async def test_network_exhausted(self, tmp_path):
        runner = make_runner([err(ErrorType.NETWORK_ERROR)] * 5,
                             network_max_attempts=5)
        summary = await runner.run([polish_record("s1")], str(tmp_path))
        assert summary["network_exhausted"] == 1
        assert len(runner._client.kinds) == 5

    async def test_repair_mode_applied_without_validation(self, tmp_path):
        """repair：答案与 GT 不同也 applied，status 翻 success。"""
        runner = make_runner(
            [ok(polished_content(answer={"发票号码": "X", "总价": "Y"}))],
            mode="repair")
        rec = polish_record("s1")
        rec["status"] = "failed"
        rec["ground_truth"] = GT
        summary = await runner.run([rec], str(tmp_path))
        assert summary["applied"] == 1
        out = json.loads((tmp_path / "success_samples.json").read_text())
        assert out[0]["polish_result"]["mode"] == "repair"

    async def test_checkpoint_resume(self, tmp_path):
        """断点续跑：先跑 1 条，重跑 2 条，skipped_resume=1、id 无重复。"""
        runner = make_runner([ok(polished_content())])
        await runner.run([polish_record("s1")], str(tmp_path))
        runner2 = make_runner([ok(polished_content())])
        summary = await runner2.run(
            [polish_record("s1"), polish_record("s2")], str(tmp_path))
        assert summary["skipped_resume"] == 1
        assert summary["applied"] == 1
        out = json.loads((tmp_path / "success_samples.json").read_text())
        assert sorted(r["sample_id"] for r in out) == ["s1", "s2"]

    async def test_summary_reconciles_with_files(self, tmp_path):
        changed = {"发票号码": "J999", "总价": "¥5.83"}
        runner = make_runner([
            ok(polished_content()),                       # s1 applied
            ok(polished_content(answer=changed)),         # s2 answer_changed
            ok("垃圾文本"),                                # s3 parse_failed
        ])
        records = [polish_record("s1"), polish_record("s2"),
                   polish_record("s3")]
        summary = await runner.run(records, str(tmp_path))
        success = json.loads((tmp_path / "success_samples.json").read_text())
        failed = json.loads((tmp_path / "failed_samples.json").read_text())
        assert len(success) == 1 and len(failed) == 2
        assert summary["processed"] == 3 == len(success) + len(failed)
        assert summary["applied_rate"] == 1 / 3
        summary_file = json.loads((tmp_path / "polish_summary.json").read_text())
        assert summary_file["applied"] == summary["applied"]

    async def test_skipped_no_material(self, tmp_path):
        rec = polish_record("s1")
        rec["cot_response"] = ""
        rec["full_api_response"] = {"choices": [{"message": {}}]}
        runner = make_runner([])
        summary = await runner.run([rec], str(tmp_path))
        assert summary["skipped_no_material"] == 1
        assert summary["processed"] == 0
