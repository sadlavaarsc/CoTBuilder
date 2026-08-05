"""convert 工具测试（纯离线，tmp_path 文件断言，无需 mock server）。

验收口径（plan：ShareGPT 转换）：
- thinking 模式：reasoning_content 优先 → cot_response 剥离 JSON 回退 →
  空则不加包裹；raw 原文不动；json 纯答案
- human 轮兼容 messages / conversations 两格式；<image> 前置规则；images 透传
- 缺 predicted_json 跳过；jsonl 容器逐行可解析
"""

import json

import pytest

from cotbuilder.convert import failed_to_sharegpt, run_convert, to_sharegpt

GT = {"发票号码": "J123", "总价": "¥5.83"}
GT_JSON = json.dumps(GT, ensure_ascii=False, indent=2)
GT_ANSWER = f"<answer>{GT_JSON}</answer>"
GT_INLINE = json.dumps(GT, ensure_ascii=False)
COT_TEXT = "先看发票号码，再看总价，逐项核对。"
COT_WITH_JSON = f"{COT_TEXT}\n```json\n{GT_INLINE}\n```"


def make_record(sid="s1", fmt="messages", cot=COT_WITH_JSON, reasoning=None,
                predicted=GT, images=("/tmp/a.jpg",), with_image_tag=True):
    prompt = "请提取图片中的关键信息"
    if with_image_tag:
        prompt = "<image>\n" + prompt
    if fmt == "messages":
        original = {"id": sid,
                    "messages": [{"role": "user", "content": prompt},
                                 {"role": "assistant", "content": GT_INLINE}],
                    "images": list(images)}
    else:
        original = {"id": sid,
                    "conversations": [{"from": "human", "value": prompt},
                                      {"from": "gpt", "value": GT_INLINE}],
                    "images": list(images)}
    record = {"sample_id": sid, "status": "success", "attempts": 1,
              "original_sample": original,
              "cot_response": cot,
              "predicted_json": predicted}
    if reasoning is not None:
        record["full_api_response"] = {"choices": [{"message": {
            "content": cot, "reasoning_content": reasoning}}]}
    return record


class TestToShareGPT:
    def test_thinking_prefers_reasoning_content(self):
        """思考通道存在时优先 reasoning_content，不取 cot_response 文本。"""
        rec = make_record(reasoning="  模型的内部思考链。  ")
        gpt = to_sharegpt(rec)["conversations"][1]["value"]
        assert gpt == f"<think>模型的内部思考链。</think>\n{GT_ANSWER}"

    def test_thinking_falls_back_to_cot_strip(self):
        """无 reasoning_content：cot_response 剥离 JSON span 与围栏作推理链。"""
        gpt = to_sharegpt(make_record())["conversations"][1]["value"]
        assert gpt == f"<think>{COT_TEXT}</think>\n{GT_ANSWER}"

    def test_empty_cot_no_think_block(self):
        """剥离后推理链为空（cot_response 只有 JSON）：仅 <answer>，无 <think> 段。"""
        rec = make_record(cot=GT_INLINE)
        gpt = to_sharegpt(rec)["conversations"][1]["value"]
        assert gpt == GT_ANSWER
        assert "<think>" not in gpt

    def test_raw_mode_verbatim(self):
        """raw 模式：gpt 轮 = cot_response 原文不动（不参与双标签契约）。"""
        gpt = to_sharegpt(make_record(), mode="raw")["conversations"][1]["value"]
        assert gpt == COT_WITH_JSON

    def test_json_mode_pure_answer(self):
        """json 模式：gpt 轮 = <answer> 包裹的纯 predicted_json。"""
        gpt = to_sharegpt(make_record(), mode="json")["conversations"][1]["value"]
        assert gpt == GT_ANSWER

    def test_custom_tags(self):
        """--think-tag/--answer-tag 自定义标签名。"""
        gpt = to_sharegpt(make_record(), think_tag="reasoning",
                          answer_tag="result")["conversations"][1]["value"]
        assert gpt == (f"<reasoning>{COT_TEXT}</reasoning>\n"
                       f"<result>{GT_JSON}</result>")

    @pytest.mark.parametrize("fmt", ["messages", "conversations"])
    def test_human_both_formats_and_images(self, fmt):
        """human 轮两种输入格式取值一致；<image> 已存在不重复；images 透传。"""
        sample = to_sharegpt(make_record(fmt=fmt))
        human, gpt = sample["conversations"]
        assert human["from"] == "human"
        assert human["value"].count("<image>") == 1
        assert "请提取图片中的关键信息" in human["value"]
        assert gpt["from"] == "gpt"
        assert sample["images"] == ["/tmp/a.jpg"]

    def test_image_tag_prepended_when_missing(self):
        """human 文本无 <image> 且 images 非空：前置占位符。"""
        sample = to_sharegpt(make_record(with_image_tag=False))
        assert sample["conversations"][0]["value"].startswith("<image>\n")

    def test_missing_material_skipped(self):
        """thinking/json 模式缺 predicted_json、raw 模式缺 cot_response → None。"""
        assert to_sharegpt(make_record(predicted=None)) is None
        assert to_sharegpt(make_record(cot=None), mode="raw") is None


class TestRunConvertFiles:
    def _write_input(self, tmp_path, records):
        d = tmp_path / "merged"
        d.mkdir()
        (d / "success_samples.json").write_text(
            json.dumps(records, ensure_ascii=False))
        return str(d)

    def test_json_container_default(self, tmp_path):
        """默认 JSON 数组容器：json.load 可读、条数与 skip 对账。"""
        records = [make_record("s1"), make_record("s2", reasoning="思考"),
                   make_record("s3", predicted=None)]
        out = str(tmp_path / "train.json")
        summary = run_convert(self._write_input(tmp_path, records), out)

        data = json.loads((tmp_path / "train.json").read_text())
        assert len(data) == 2
        assert summary["converted"] == 2 and summary["skipped"] == 1
        assert summary["gpt_mode"] == "thinking" and summary["format"] == "json"
        convert_summary = json.loads(
            (tmp_path / "convert_summary.json").read_text())
        assert convert_summary["converted"] == 2

    def test_jsonl_container(self, tmp_path):
        """JSONL 容器：逐行可解析、条数一致、每条字段齐全。"""
        records = [make_record(f"s{i}") for i in range(3)]
        out = str(tmp_path / "train.jsonl")
        summary = run_convert(self._write_input(tmp_path, records), out,
                              fmt="jsonl")

        lines = (tmp_path / "train.jsonl").read_text().splitlines()
        assert len(lines) == 3 == summary["converted"]
        for line in lines:
            sample = json.loads(line)
            assert [t["from"] for t in sample["conversations"]] == ["human", "gpt"]
            assert sample["images"]


class TestStripFences:
    def test_raw_mode_strip_fences(self):
        """--strip-fences：raw 模式删 ```json 围栏标记、保留 JSON 内容。"""
        rec = make_record()
        fenced = to_sharegpt(rec, mode="raw")["conversations"][1]["value"]
        assert "```" in fenced                      # 默认保留原文围栏
        stripped = to_sharegpt(rec, mode="raw",
                               strip_fences=True)["conversations"][1]["value"]
        assert "```" not in stripped
        assert GT_INLINE in stripped and COT_TEXT in stripped


class TestThinkFlags:
    def test_flag_follows_actual_thinking_content(self):
        """标志按 gpt 轮实际是否含推理逐样本选择，加在 human 末尾。"""
        cot_human = to_sharegpt(make_record())["conversations"][0]["value"]
        assert cot_human.endswith("\n/think")       # thinking 模式有推理链
        raw_human = to_sharegpt(make_record(), mode="raw"
                                )["conversations"][0]["value"]
        assert raw_human.endswith("\n/think")       # raw 保留推理原文
        json_human = to_sharegpt(make_record(), mode="json"
                                 )["conversations"][0]["value"]
        assert json_human.endswith("\n/no_think")   # json 纯答案无推理
        empty_cot = make_record(cot=GT_INLINE)      # 推理链为空 → 无包裹
        empty_human = to_sharegpt(empty_cot)["conversations"][0]["value"]
        assert empty_human.endswith("\n/no_think")  # 无推理内容不挂 /think

    def test_custom_flags_and_disable(self):
        """自定义文案；空串禁用标志。"""
        sample = to_sharegpt(make_record(), think_flag="<THINK>",
                             no_think_flag="<NO_THINK>")
        assert sample["conversations"][0]["value"].endswith("\n<THINK>")
        disabled = to_sharegpt(make_record(), think_flag="",
                               no_think_flag="")
        assert "/think" not in disabled["conversations"][0]["value"]

    def test_existing_flag_lines_replaced(self):
        """human 已有旧标志行：剥掉后加正确标志，不重复不矛盾。"""
        rec = make_record()
        rec["original_sample"]["messages"][0]["content"] += "\n/no_think"
        human = to_sharegpt(rec)["conversations"][0]["value"]
        assert human.count("/think") == 1 and "/no_think" not in human


def no_cot_sample(sid, with_thinking_block=False, qwen3_format=False):
    gpt = "答案是 42"
    if with_thinking_block:
        gpt = "<thinking>残留推理</thinking>\n答案是 42"
    elif qwen3_format:
        gpt = "<think>残留推理</think>\n<answer>答案是 42</answer>"
    return {"conversations": [{"from": "human", "value": f"问题 {sid}"},
                              {"from": "gpt", "value": gpt}],
            "images": []}


class TestMix:
    def _setup(self, tmp_path, n_cot=3, n_mix=5):
        d = tmp_path / "merged"
        d.mkdir()
        (d / "success_samples.json").write_text(json.dumps(
            [make_record(f"c{i}") for i in range(n_cot)], ensure_ascii=False))
        mix = tmp_path / "nocot.json"
        mix.write_text(json.dumps(
            [no_cot_sample(f"m{i}", with_thinking_block=(i == 0),
                           qwen3_format=(i == 1))
             for i in range(n_mix)], ensure_ascii=False))
        return str(d), str(mix)

    def test_mix_ratio_and_sanitize(self, tmp_path):
        """ratio=1.0 → 混入 CoT 条数等量；混入样本剥推理段、统一 <answer> 重包。"""
        input_dir, mix_path = self._setup(tmp_path)
        out = str(tmp_path / "train.json")
        summary = run_convert(input_dir, out, mix_path=mix_path, mix_ratio=1.0)

        assert summary["converted"] == 3 and summary["mixed_in"] == 3
        assert summary["total_samples"] == 6
        data = json.loads((tmp_path / "train.json").read_text())
        for s in data[3:]:
            human, gpt = s["conversations"]
            assert human["value"].endswith("\n/no_think")
            assert "<think" not in gpt["value"]         # 推理段已剥（两种旧标签）
            assert gpt["value"] == "<answer>答案是 42</answer>"  # 统一重包
            assert gpt["value"].count("<answer>") == 1  # 已有 <answer> 不双包

    def test_mix_ratio_partial_and_overflow(self, tmp_path):
        """ratio=0.5 → int(0.5×3)=1 条；需求超体量 → 全取并计数如实。"""
        input_dir, mix_path = self._setup(tmp_path, n_cot=3, n_mix=2)
        out = str(tmp_path / "t1.json")
        summary = run_convert(input_dir, out, mix_path=mix_path, mix_ratio=0.5)
        assert summary["mixed_in"] == 1

        out2 = str(tmp_path / "t2.json")
        summary2 = run_convert(input_dir, out2, mix_path=mix_path,
                               mix_ratio=10.0)   # 需 30 条，只有 2 条
        assert summary2["mixed_in"] == 2

    def test_mix_jsonl_source_and_deterministic(self, tmp_path):
        """混入源支持 .jsonl；固定种子 → 两次运行选中的样本一致。"""
        input_dir, _ = self._setup(tmp_path)
        mixl = tmp_path / "nocot.jsonl"
        mixl.write_text("".join(
            json.dumps(no_cot_sample(f"m{i}"), ensure_ascii=False) + "\n"
            for i in range(5)))
        out1, out2 = str(tmp_path / "a.json"), str(tmp_path / "b.json")
        run_convert(input_dir, out1, mix_path=str(mixl), mix_ratio=0.67)
        run_convert(input_dir, out2, mix_path=str(mixl), mix_ratio=0.67)
        assert json.loads((tmp_path / "a.json").read_text()) == \
               json.loads((tmp_path / "b.json").read_text())


WRONG_GT = {"发票号码": "WRONG", "总价": "¥5.83"}


def failed_rec(sid, kind):
    """构造 failed 记录。kind: upheld / mismatch / judge_error / infra / no_gt。"""
    rec = {"sample_id": sid, "status": "failed", "attempts": 3,
           "original_sample": {"id": sid,
                               "messages": [{"role": "user", "content": "<image>\n提取"},
                                            {"role": "assistant", "content": GT_INLINE}],
                               "images": ["/tmp/f.jpg"]},
           "cot_response": COT_WITH_JSON,
           "predicted_json": WRONG_GT,
           "ground_truth": GT,
           "comparison_result": {"is_match": False, "differences": [
               {"field": "发票号码", "type": "mismatch",
                "predicted": "WRONG", "ground_truth": "J123"}]},
           "error_type": "MISMATCH"}
    if kind == "upheld":
        rec["judge_result"] = {"overturned": False, "pairs": [], "attempts": 1}
    elif kind == "judge_error":
        rec["judge_result"] = {"overturned": False, "pairs": [], "attempts": 5,
                               "failure": "network_exhausted"}
    elif kind == "infra":
        for k in ("cot_response", "predicted_json", "comparison_result"):
            del rec[k]
        rec["error_type"] = "NETWORK_ERROR"
    elif kind == "no_gt":
        for k in ("cot_response", "predicted_json",
                  "ground_truth", "comparison_result"):
            del rec[k]
        # original_sample 的 GT 文本也抹掉——无 GT 可供回退提取
        rec["original_sample"]["messages"][1]["content"] = "not json"
        rec["error_type"] = "LENGTH_TRUNCATED"
    elif kind == "no_gt_field":
        # 2026-08-04 修复前的受损记录：无 ground_truth 字段，
        # 但 original_sample 里 GT 文本完整（convert 回退可救）
        del rec["ground_truth"]
    return rec


class TestFailedGtFallback:
    """2026-08-04 Bug① 修复：ground_truth 字段缺失时从 original_sample
    回退提取 GT（救修复前产出的旧记录；两处都无 GT 才跳过）。"""

    def test_fallback_from_original_sample_messages(self):
        """无 ground_truth 字段、original_sample 为 messages 格式 → 回退救回。"""
        rec = failed_rec("x1", "no_gt_field")
        sample = failed_to_sharegpt(rec)
        assert sample is not None
        assert sample["conversations"][1]["value"] == GT_ANSWER
        assert sample["conversations"][0]["value"].endswith("\n/no_think")

    def test_fallback_from_original_sample_conversations(self):
        """conversations 格式 original_sample 同样可回退。"""
        rec = failed_rec("x2", "no_gt_field")
        rec["original_sample"] = {
            "id": "x2",
            "conversations": [{"from": "human", "value": "<image>\n提取"},
                              {"from": "gpt", "value": GT_INLINE}],
            "images": ["/tmp/f.jpg"]}
        sample = failed_to_sharegpt(rec)
        assert sample is not None
        assert sample["conversations"][1]["value"] == GT_ANSWER

    def test_no_gt_anywhere_still_skipped(self):
        """两处都无 GT（original_sample GT 文本也不可解析）→ None 不变。"""
        assert failed_to_sharegpt(failed_rec("x3", "no_gt")) is None

    def test_recovered_counted_in_summary(self, tmp_path):
        """回退救回的条数计入 convert_summary.failed_gt_recovered。"""
        input_dir, _ = TestIncludeFailed()._setup(tmp_path, [
            failed_rec("u1", "no_gt_field"), failed_rec("u2", "upheld")])
        summary = run_convert(input_dir, str(tmp_path / "t.json"),
                              include_failed="all", mix_ratio=10.0)
        assert summary["failed_used"] == 2
        assert summary["failed_gt_recovered"] == 1   # u1 回退救回，u2 自带 GT


class TestPolishIntegration:
    """polish 衔接（2026-08-05）：applied 的 polish_result 优先于原回收
    路径（CoT 与答案都是）；applied=false 回落原路径。"""

    @staticmethod
    def _tag(rec, applied, cot="润色后的推理", answer=None):
        rec["polish_result"] = {"mode": "polish", "applied": applied,
                                "polished_cot": cot, "attempts": 1}
        if answer is not None:
            rec["polish_result"]["polished_answer"] = answer
        return rec

    def test_applied_polish_preferred(self):
        """applied：gpt = <think>polished_cot</think> + polished 路径 CoT。"""
        rec = self._tag(make_record(), True)
        gpt = to_sharegpt(rec)["conversations"][1]["value"]
        assert gpt.startswith("<think>润色后的推理</think>")
        assert COT_TEXT not in gpt                 # 原 CoT 不再使用

    def test_repair_answer_preferred_over_predicted(self):
        """repair 记录：polished_answer ≠ predicted_json 时用 polished。"""
        repaired = {"发票号码": "J123", "总价": "¥9.99"}
        rec = self._tag(make_record(predicted={"发票号码": "J123",
                                               "总价": "WRONG"}),
                        True, answer=repaired)
        rec["polish_result"]["mode"] = "repair"
        gpt = to_sharegpt(rec)["conversations"][1]["value"]
        assert "¥9.99" in gpt and "WRONG" not in gpt

    def test_unapplied_polish_ignored(self):
        """applied=false（answer_changed）：回落原 CoT / predicted_json。"""
        rec = self._tag(make_record(), False,
                        answer={"发票号码": "J999", "总价": "¥5.83"})
        gpt = to_sharegpt(rec)["conversations"][1]["value"]
        assert COT_TEXT in gpt                     # 原 CoT
        assert "J999" not in gpt                   # polished_answer 不用

    def test_json_mode_also_prefers_polished_answer(self):
        repaired = {"发票号码": "J123", "总价": "¥9.99"}
        rec = self._tag(make_record(), True, answer=repaired)
        gpt = to_sharegpt(rec, mode="json")["conversations"][1]["value"]
        assert "¥9.99" in gpt


class TestIncludeFailed:
    def _setup(self, tmp_path, failed_records, n_cot=3, with_mix=5):
        d = tmp_path / "merged"
        d.mkdir()
        (d / "success_samples.json").write_text(json.dumps(
            [make_record(f"c{i}") for i in range(n_cot)], ensure_ascii=False))
        (d / "failed_samples.json").write_text(
            json.dumps(failed_records, ensure_ascii=False))
        mix_path = None
        if with_mix:
            mix = tmp_path / "nocot.json"
            mix.write_text(json.dumps(
                [no_cot_sample(f"m{i}") for i in range(with_mix)],
                ensure_ascii=False))
            mix_path = str(mix)
        return str(d), mix_path

    def test_upheld_filter_and_gt_answer(self, tmp_path):
        """upheld 档：只选 judge 维持原判；gpt = GT 答案（绝不用 predicted）。"""
        input_dir, _ = self._setup(tmp_path, [
            failed_rec("u1", "upheld"), failed_rec("m1", "mismatch"),
            failed_rec("e1", "judge_error")])
        out = str(tmp_path / "train.json")
        summary = run_convert(input_dir, out, include_failed="upheld",
                              mix_ratio=10.0)

        assert summary["failed_used"] == 1  # 只有 u1（m1 未判、e1 有 failure）
        derived = json.loads((tmp_path / "train.json").read_text())[3]
        human, gpt = derived["conversations"]
        assert gpt["value"] == GT_ANSWER            # <answer> 包裹的 GT 答案
        assert "WRONG" not in gpt["value"]          # predicted_json 永不入训
        assert "<think" not in gpt["value"]         # 错误推理链永不入训
        assert human["value"].endswith("\n/no_think")
        assert derived["images"] == ["/tmp/f.jpg"]

    def test_mismatch_and_all_filters(self, tmp_path):
        """mismatch 档选有 diff 证据的（含未判 MISMATCH）；all 档含 infra、
        无 GT 的终态失败跳过。"""
        input_dir, _ = self._setup(tmp_path, [
            failed_rec("u1", "upheld"), failed_rec("m1", "mismatch"),
            failed_rec("i1", "infra"), failed_rec("t1", "no_gt")])
        out = str(tmp_path / "t.json")
        s1 = run_convert(input_dir, out, include_failed="mismatch",
                         mix_ratio=10.0)
        assert s1["failed_used"] == 2   # u1 + m1；i1 无 diff 证据

        s2 = run_convert(input_dir, out, include_failed="all",
                         mix_ratio=10.0)
        assert s2["failed_used"] == 3   # u1 + m1 + i1（有 GT）；t1 无 GT 跳过

    def test_priority_fill_failed_first_mix_complements(self, tmp_path):
        """预算 = ratio × CoT；failed 优先填充，缺口由 mix 补齐；排布有序。"""
        input_dir, mix_path = self._setup(tmp_path, [
            failed_rec("u1", "upheld"), failed_rec("u2", "upheld")],
            n_cot=3, with_mix=5)
        out = str(tmp_path / "train.json")
        summary = run_convert(input_dir, out, include_failed="upheld",
                              mix_path=mix_path, mix_ratio=1.0)

        assert summary["no_cot_budget"] == 3
        assert summary["failed_used"] == 2      # 预算 3，failed 只有 2
        assert summary["mixed_in"] == 1         # 缺口 1 由 mix 补齐
        data = json.loads((tmp_path / "train.json").read_text())
        assert len(data) == 6
        assert data[3]["images"] == ["/tmp/f.jpg"]   # failed 派生在前
        assert data[5]["images"] == []               # mix 混入在后

    def test_failed_over_budget_sampled_deterministic(self, tmp_path):
        """eligible 超预算：按预算抽样，固定种子两次一致。"""
        input_dir, _ = self._setup(tmp_path, [
            failed_rec(f"u{i}", "upheld") for i in range(5)], n_cot=2)
        out1, out2 = str(tmp_path / "a.json"), str(tmp_path / "b.json")
        s1 = run_convert(input_dir, out1, include_failed="upheld",
                         mix_ratio=1.0)   # 预算 2 < eligible 5
        s2 = run_convert(input_dir, out2, include_failed="upheld",
                         mix_ratio=1.0)
        assert s1["failed_used"] == 2
        assert json.loads((tmp_path / "a.json").read_text()) == \
               json.loads((tmp_path / "b.json").read_text())

    def test_missing_failed_file_budget_goes_to_mix(self, tmp_path):
        """无 failed_samples.json：预算全部留给 mix，不报错。"""
        d = tmp_path / "merged"
        d.mkdir()
        (d / "success_samples.json").write_text(json.dumps(
            [make_record(f"c{i}") for i in range(2)], ensure_ascii=False))
        mix = tmp_path / "nocot.json"
        mix.write_text(json.dumps([no_cot_sample("m0")], ensure_ascii=False))
        out = str(tmp_path / "train.json")
        summary = run_convert(str(d), out, include_failed="upheld",
                              mix_path=str(mix), mix_ratio=1.0)
        assert summary["failed_used"] == 0 and summary["mixed_in"] == 1
