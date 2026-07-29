"""与原版 RobustJSONComparator 的兼容测试（oldCode/ 只读 import，不修改）。

三层断言：
1. schema 完备：comparison_result 为原版返回结构的超集；
2. legacy 对齐：Matcher.legacy() 与原版在顶层字段语料上判定一致
   （missing_fields 原版多算一倍，按 ÷2 对齐——已修正并文档化）；
3. 已知差异固化：默认模式（audit-02 规格）与原版判定相反的案例，
   逐项断言方向，作为「活的差异文档」（doc/comparator-compat.md 同源）。
"""

import importlib.util
import json
from pathlib import Path

import pytest

from cotbuilder.extractor import extract_json
from cotbuilder.matcher import Matcher

_ORIGINAL_PATH = (Path(__file__).parent.parent
                  / "oldCode" / "RobustJSONComparator.py")
_spec = importlib.util.spec_from_file_location(
    "robust_json_comparator", _ORIGINAL_PATH)
_original_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_original_mod)

ORIGINAL = _original_mod.RobustJSONComparator()
ORIGINAL_KEYS = set(ORIGINAL.compare({"a": "1"}, {"a": "1"}).keys())


def legacy_verdict(pred, gt):
    return Matcher.legacy().compare(pred, gt)


def default_verdict(pred, gt):
    return Matcher().compare(pred, gt)


# ---------------------------------------------------------------------------
# 1. schema 完备
# ---------------------------------------------------------------------------

class TestSchemaCompat:
    @pytest.mark.parametrize("matcher_factory", [Matcher, Matcher.legacy])
    def test_result_is_superset_of_original(self, matcher_factory):
        ours = matcher_factory().compare(
            {"a": "1", "b": "x"}, {"a": "1", "b": "y"}).to_dict()
        assert ORIGINAL_KEYS <= set(ours), (
            f"缺失原版键: {ORIGINAL_KEYS - set(ours)}")

    def test_key_types_match_original(self):
        original = ORIGINAL.compare(
            {"a": "1", "b": "x"}, {"a": "1", "b": "y"})
        ours = default_verdict({"a": "1", "b": "x"},
                               {"a": "1", "b": "y"}).to_dict()
        for key in ORIGINAL_KEYS:
            assert type(ours[key]) is type(original[key]), (
                f"键 {key} 类型不一致: {type(ours[key])} vs "
                f"{type(original[key])}")


# ---------------------------------------------------------------------------
# 2. legacy 对齐（顶层字段语料）
# ---------------------------------------------------------------------------

# (描述, pred, gt) —— 全部取自原版自测用例与其默认规则覆盖的场景
LEGACY_CASES = [
    ("大小写差异", {"name": "HIKVISION"}, {"name": "hikvision"}),
    ("首尾空格", {"name": "  hello  "}, {"name": "hello"}),
    ("内部空格折叠", {"name": "hello   world"}, {"name": "hello world"}),
    ("全角字母", {"name": "ＨＩＫ"}, {"name": "HIK"}),
    ("全角数字", {"price": "１００"}, {"price": "100"}),
    ("货币宽度变体", {"price": "￥100"}, {"price": "¥100"}),
    ("扩展货币 €", {"price": "€100"}, {"price": "EUR100"}),
    ("数字分组逗号", {"price": "1,234"}, {"price": "1234"}),
    ("标点统一", {"desc": "你好，世界。"}, {"desc": "你好,世界."}),
    ("类型不敏感", {"price": "100"}, {"price": 100}),
    ("字段缺失", {"name": "test"}, {"name": "test", "age": "20"}),
    ("值不匹配", {"name": "test1"}, {"name": "test2"}),
    ("多余字段", {"name": "test", "extra": "x"}, {"name": "test"}),
    ("全角发票号", {"发票号码": "３０６１３９４４"}, {"发票号码": "30613944"}),
    ("空字段丢弃", {"name": "test", "note": ""}, {"name": "test"}),
]


class TestLegacyAlignment:
    @pytest.mark.parametrize("desc,pred,gt", LEGACY_CASES,
                             ids=[c[0] for c in LEGACY_CASES])
    def test_is_match_aligned(self, desc, pred, gt):
        original = ORIGINAL.compare(json.dumps(pred, ensure_ascii=False),
                                    json.dumps(gt, ensure_ascii=False))
        ours = legacy_verdict(pred, gt)
        assert ours.is_accepted == original["is_match"], (
            f"{desc}: 原版 {original['is_match']} vs legacy {ours.is_accepted}")

    @pytest.mark.parametrize("desc,pred,gt", LEGACY_CASES,
                             ids=[c[0] for c in LEGACY_CASES])
    def test_score_and_counts_aligned(self, desc, pred, gt):
        original = ORIGINAL.compare(json.dumps(pred, ensure_ascii=False),
                                    json.dumps(gt, ensure_ascii=False))
        ours = legacy_verdict(pred, gt).to_dict()
        assert ours["match_score"] == pytest.approx(
            original["match_score"]), desc
        assert ours["matched_fields"] == original["matched_fields"], desc
        # 原版 missing_fields 多算一倍（:358-359 连加两次），按 ÷2 对齐
        assert ours["missing_fields"] == original["missing_fields"] // 2, desc
        assert ours["extra_fields"] == original["extra_fields"], desc


# ---------------------------------------------------------------------------
# 3. 已知差异固化（默认模式 vs 原版，方向即文档）
# ---------------------------------------------------------------------------

class TestDocumentedDivergences:
    """默认模式（audit-02 规格）与原版的判定差异——每例断言两个方向。

    修改这些断言前必读 doc/comparator-compat.md：它们就是差异文档本身。
    """

    def test_case_difference(self):
        """原版大小写不敏感判一致；audit R-N3 要求判 MISMATCH。"""
        assert ORIGINAL.compare('{"n": "LAU"}', '{"n": "Lau"}')["is_match"] is True
        assert default_verdict({"n": "LAU"}, {"n": "Lau"}).is_accepted is False

    def test_numeric_comma(self):
        """原版去数字分组逗号判一致；audit R-N3 要求判 MISMATCH。"""
        assert ORIGINAL.compare('{"n": "1,000"}', '{"n": "1000"}')["is_match"] is True
        assert default_verdict({"n": "1,000"}, {"n": "1000"}).is_accepted is False

    def test_extended_currency(self):
        """原版 €→EUR 映射在默认配置下不自洽（lower 先执行，"€5"→"EUR5"
        而 "EUR5"→"eur5"），判不一致；只有 case_insensitive=False 时才一致。
        规格不做扩展货币统一，默认同样判不一致（但理由正当：€ 与 EUR 本就不同）。"""
        assert ORIGINAL.compare('{"n": "€5"}', '{"n": "EUR5"}')["is_match"] is False
        case_sensitive = _original_mod.RobustJSONComparator(
            {"case_insensitive": False})
        assert case_sensitive.compare('{"n": "€5"}',
                                      '{"n": "EUR5"}')["is_match"] is True
        assert default_verdict({"n": "€5"}, {"n": "EUR5"}).is_accepted is False

    def test_punct_adjacent_space(self):
        """原版无标点空格归一化判不一致；audit R-N2 要求判一致。"""
        assert ORIGINAL.compare('{"n": "LAU, LAI"}',
                                '{"n": "LAU,LAI"}')["is_match"] is False
        assert default_verdict({"n": "LAU, LAI"},
                               {"n": "LAU,LAI"}).is_accepted is True

    def test_type_insensitive_numbers(self):
        """原版 str() 比较：100 == "100" 判一致；规格类型敏感判 MISMATCH。"""
        assert ORIGINAL.compare('{"n": 100}', '{"n": "100"}')["is_match"] is True
        assert default_verdict({"n": 100}, {"n": "100"}).is_accepted is False

    def test_none_handling(self):
        """None 字段：原版两边都判不一致，但机制都很曲折
        （pred None 被 trim_empty_fields 丢弃 → missing；gt None 经 str()
        变 "None" 后又被 lower 错杀）。规格：类型敏感，同样判不一致。"""
        assert ORIGINAL.compare({"n": None}, {"n": "None"})["is_match"] is False
        assert ORIGINAL.compare('{"n": "None"}', '{"n": null}')["is_match"] is False
        assert default_verdict({"n": None}, {"n": "None"}).is_accepted is False

    def test_nested_fullwidth(self):
        """原版嵌套值不归一化（str(dict) 比较）判不一致；规格递归逐叶子。"""
        pred, gt = {"d": {"a": "１"}}, {"d": {"a": "1"}}
        assert ORIGINAL.compare(pred, gt)["is_match"] is False
        assert default_verdict(pred, gt).is_accepted is True

    def test_internal_spaces_kept(self):
        """原版折叠内部空格判一致；规格保留（姓名空格承载语义）。"""
        assert ORIGINAL.compare('{"n": "LAI  LI"}',
                                '{"n": "LAI LI"}')["is_match"] is True
        assert default_verdict({"n": "LAI  LI"},
                               {"n": "LAI LI"}).is_accepted is False


# ---------------------------------------------------------------------------
# extractor：吸收原版的 ast.literal_eval 单引号兜底
# ---------------------------------------------------------------------------

class TestSingleQuoteParsing:
    def test_plain_single_quotes(self):
        assert extract_json("{'发票号码': '12345'}") == {"发票号码": "12345"}

    def test_nested_single_quotes(self):
        text = "{'明细': [{'单价': '¥5.83'}]}"
        assert extract_json(text) == {"明细": [{"单价": "¥5.83"}]}

    def test_mixed_quotes(self):
        assert extract_json('''{"a": 1, 'b': 2}''') == {"a": 1, "b": 2}

    def test_single_quote_in_prose(self):
        text = "提取结果：{'a': 'x'} 完毕"
        assert extract_json(text) == {"a": "x"}

    def test_python_literals(self):
        """True/None 等 Python 字面量也可解析（ast 语义的附带能力）。"""
        assert extract_json("{'ok': True, 'v': None}") == {"ok": True, "v": None}
