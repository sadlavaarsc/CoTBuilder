"""convert 工具测试（纯离线，tmp_path 文件断言，无需 mock server）。

验收口径（plan：ShareGPT 转换）：
- thinking 模式：reasoning_content 优先 → cot_response 剥离 JSON 回退 →
  空则不加包裹；raw 原文不动；json 纯答案
- human 轮兼容 messages / conversations 两格式；<image> 前置规则；images 透传
- 缺 predicted_json 跳过；jsonl 容器逐行可解析
"""

import json

import pytest

from cotbuilder.convert import run_convert, to_sharegpt

GT = {"发票号码": "J123", "总价": "¥5.83"}
GT_JSON = json.dumps(GT, ensure_ascii=False, indent=2)
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
        assert gpt == f"<thinking>模型的内部思考链。</thinking>\n{GT_JSON}"

    def test_thinking_falls_back_to_cot_strip(self):
        """无 reasoning_content：cot_response 剥离 JSON span 与围栏作推理链。"""
        gpt = to_sharegpt(make_record())["conversations"][1]["value"]
        assert gpt == f"<thinking>{COT_TEXT}</thinking>\n{GT_JSON}"

    def test_empty_cot_no_wrapper(self):
        """剥离后推理链为空（cot_response 只有 JSON）：不加 <thinking> 包裹。"""
        rec = make_record(cot=GT_INLINE)
        gpt = to_sharegpt(rec)["conversations"][1]["value"]
        assert gpt == GT_JSON
        assert "<thinking>" not in gpt

    def test_raw_mode_verbatim(self):
        """raw 模式：gpt 轮 = cot_response 原文不动。"""
        gpt = to_sharegpt(make_record(), mode="raw")["conversations"][1]["value"]
        assert gpt == COT_WITH_JSON

    def test_json_mode_pure_answer(self):
        """json 模式：gpt 轮 = 纯 predicted_json 序列化。"""
        gpt = to_sharegpt(make_record(), mode="json")["conversations"][1]["value"]
        assert gpt == GT_JSON

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


def no_cot_sample(sid, with_thinking_block=False):
    gpt = "答案是 42"
    if with_thinking_block:
        gpt = "<thinking>残留推理</thinking>\n答案是 42"
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
            [no_cot_sample(f"m{i}", with_thinking_block=(i == 0))
             for i in range(n_mix)], ensure_ascii=False))
        return str(d), str(mix)

    def test_mix_ratio_and_sanitize(self, tmp_path):
        """ratio=1.0 → 混入 CoT 条数等量；混入样本去 thinking 段 + /no_think。"""
        input_dir, mix_path = self._setup(tmp_path)
        out = str(tmp_path / "train.json")
        summary = run_convert(input_dir, out, mix_path=mix_path, mix_ratio=1.0)

        assert summary["converted"] == 3 and summary["mixed_in"] == 3
        assert summary["total_samples"] == 6
        data = json.loads((tmp_path / "train.json").read_text())
        mixed = data[3:]
        for s in mixed:
            human, gpt = s["conversations"]
            assert human["value"].endswith("\n/no_think")
            assert "<thinking>" not in gpt["value"]  # 残留 thinking 段已剥

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
