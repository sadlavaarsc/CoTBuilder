"""matcher 单元测试：audit-02 §7 验收指标。

- §7.1 归一化规格正反例（R-N1/R-N2 必须判一致，R-N3 必须判 MISMATCH）
- §7.2 逐字段明细完整性
- §7.3 判定一致性（验收与诊断同源）
- §7.4 三个永久回归用例
"""

import pytest

from cotbuilder.matcher import (
    Matcher,
    MatchLevel,
    SAMPLE_MISMATCH,
    SAMPLE_NORMALIZED_MATCH,
    SAMPLE_STRICT,
    normalize,
)


@pytest.fixture
def m():
    return Matcher()


# ---------------------------------------------------------------------------
# R-N1 全角 → 半角（规格内，必须判一致）
# ---------------------------------------------------------------------------

class TestFullwidthNormalization:
    @pytest.mark.parametrize("fullwidth,halfwidth", [
        ("Ａ", "A"), ("Ｚ", "Z"), ("ａ", "a"), ("ｚ", "z"),   # 全角字母
        ("０", "0"), ("９", "9"),                              # 全角数字
        ("＄", "$"), ("＃", "#"), ("％", "%"),                 # 全角符号
        ("￥", "¥"), ("￡", "£"),                              # 货币宽度变体
        ("，", ","), ("：", ":"), ("；", ";"), ("？", "?"), ("！", "!"),
        ("（", "("), ("）", ")"), ("【", "["), ("】", "]"),
        ("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"),
        ("、", ","), ("。", "."),
    ])
    def test_char_pairs(self, fullwidth, halfwidth):
        assert normalize(fullwidth) == halfwidth

    def test_fullwidth_space(self):
        assert normalize("LAU　LAI") == "LAU LAI"   # U+3000 → 半角空格

    def test_fullwidth_ticket_number(self):
        """中文单据 OCR 高频场景：全角票号（audit-02 §2.1）。"""
        assert normalize("１２３４５") == "12345"

    def test_fullwidth_mixed_field(self):
        assert normalize("总价：￥５.８３") == "总价:¥5.83"


# ---------------------------------------------------------------------------
# R-N2 标点相邻空格（规格内，必须判一致）
# ---------------------------------------------------------------------------

class TestPunctuationAdjacentSpace:
    def test_space_after_comma(self):
        assert normalize("LAU, LAI LI") == "LAU,LAI LI"

    def test_space_before_colon(self):
        assert normalize("总价 :100") == "总价:100"

    def test_strip_ends(self):
        assert normalize("  LAU LAI  ") == "LAU LAI"

    def test_internal_spaces_preserved(self):
        """英文姓名/地址内部空格承载语义，不得删除（audit-02 §2.2）。"""
        assert normalize("LAI LI WEN TAO") == "LAI LI WEN TAO"


# ---------------------------------------------------------------------------
# R-N3 规格外差异（必须判 MISMATCH）—— §7.4 永久回归用例
# ---------------------------------------------------------------------------

class TestOutOfSpecMismatch:
    def test_case_difference(self, m):
        """回归用例 1：大小写错误不得归一化。"""
        v = m.compare({"姓名": "LAU"}, {"姓名": "Lau"})
        assert v.level == SAMPLE_MISMATCH
        assert v.fields[0].level == MatchLevel.MISMATCH

    def test_missing_currency_symbol(self, m):
        """回归用例 2：货币符号缺失是真实提取错误（prompt 规则 3 要求保留）。"""
        v = m.compare({"总价": "¥5.83"}, {"总价": "5.83"})
        assert v.level == SAMPLE_MISMATCH

    def test_destroyed_internal_spacing(self, m):
        """回归用例 3：删除内部空格破坏姓名拼写，不得洗成一致。"""
        v = m.compare({"姓名": "LAU, LAI LI"}, {"姓名": "LAU,LAIL I"})
        assert v.level == SAMPLE_MISMATCH

    def test_numeric_format_not_equivalent(self, m):
        """5.83 vs 5.830 / 1,000 vs 1000 默认 MISMATCH（R-N3 第 4 条）。"""
        assert m.compare({"金额": "5.83"}, {"金额": "5.830"}).level == SAMPLE_MISMATCH
        assert m.compare({"金额": "1,000"}, {"金额": "1000"}).level == SAMPLE_MISMATCH

    def test_no_nfkc_trademark_folding(self):
        """禁 NFKC：™ 不得折叠为 TM（提取规则要求符号照抄）。"""
        assert normalize("A™") != normalize("ATM")
        assert normalize("①") == "①"   # ① 不在受控映射表，保持原样


# ---------------------------------------------------------------------------
# 字段级三级判定与样本聚合（R-C1 / R-C2）
# ---------------------------------------------------------------------------

class TestFieldVerdicts:
    def test_strict_sample(self, m):
        v = m.compare({"a": "1", "b": "x"}, {"a": "1", "b": "x"})
        assert v.level == SAMPLE_STRICT
        assert v.is_accepted
        assert v.matched_field_count == 2

    def test_normalized_sample(self, m):
        """格式噪声（全角逗号 + 标点后空格）→ NORMALIZED_MATCH，验收通过。"""
        v = m.compare({"姓名": "ＬＡＵ， ＬＡＩ"}, {"姓名": "LAU,LAI"})
        assert v.level == SAMPLE_NORMALIZED_MATCH
        assert v.is_accepted
        assert v.fields[0].level == MatchLevel.NORMALIZED

    def test_one_bad_field_fails_sample(self, m):
        v = m.compare({"a": "1", "b": "wrong"}, {"a": "1", "b": "right"})
        assert v.level == SAMPLE_MISMATCH
        assert not v.is_accepted
        assert v.matched_field_count == 1

    def test_key_missing_in_pred(self, m):
        v = m.compare({"a": "1"}, {"a": "1", "b": "2"})
        assert v.level == SAMPLE_MISMATCH
        levels = {f.field: f.level for f in v.fields}
        assert levels["b"] == MatchLevel.KEY_MISSING_IN_PRED

    def test_key_missing_in_gt(self, m):
        v = m.compare({"a": "1", "extra": "x"}, {"a": "1"})
        levels = {f.field: f.level for f in v.fields}
        assert levels["extra"] == MatchLevel.KEY_MISSING_IN_GT

    def test_full_detail_no_swallowed_fields(self, m):
        """§7.2：MISMATCH 样本输出含全部字段（含 STRICT 字段）的级别与原始值。"""
        v = m.compare(
            {"a": "1", "b": "x", "c": "bad"},
            {"a": "1", "b": "x", "c": "good"},
        )
        assert len(v.fields) == 3
        d = v.to_dict()
        by_field = {f["field"]: f for f in d["fields"]}
        assert by_field["a"]["level"] == "STRICT"
        assert by_field["a"]["pred_value"] == "1"
        assert by_field["c"]["gt_value"] == "good"


class TestNestedAndTypes:
    def test_nested_list_position_aligned(self, m):
        """明细行数组按位置对齐比较（R-C3）。"""
        pred = {"明细": [{"单价": "¥5.83"}, {"单价": "¥1.00"}]}
        gt = {"明细": [{"单价": "¥5.83"}, {"单价": "¥2.00"}]}
        v = m.compare(pred, gt)
        assert v.level == SAMPLE_MISMATCH
        levels = {f.field: f.level for f in v.fields}
        assert levels["明细[0].单价"] == MatchLevel.STRICT
        assert levels["明细[1].单价"] == MatchLevel.MISMATCH

    def test_list_length_mismatch(self, m):
        v = m.compare({"明细": [{"a": 1}, {"a": 2}]}, {"明细": [{"a": 1}]})
        levels = {f.field: f.level for f in v.fields}
        assert levels["明细[1].a"] == MatchLevel.KEY_MISSING_IN_GT

    def test_none_vs_string_none(self, m):
        """str(None) == "None" 假阳性回归（audit-02 §2.6）。"""
        v = m.compare({"a": None}, {"a": "None"})
        assert v.fields[0].level == MatchLevel.MISMATCH

    def test_number_vs_string(self, m):
        v = m.compare({"a": 5.83}, {"a": "5.83"})
        assert v.fields[0].level == MatchLevel.MISMATCH

    def test_numeric_strict_equality(self, m):
        v = m.compare({"a": 5}, {"a": 5.0})
        assert v.fields[0].level == MatchLevel.STRICT


# ---------------------------------------------------------------------------
# rank_key 选优与 aggregate 离线分析
# ---------------------------------------------------------------------------

class TestRankAndAggregate:
    def test_rank_prefers_level_then_field_count(self, m):
        strict = m.compare({"a": "1"}, {"a": "1"})
        norm = m.compare({"a": "１"}, {"a": "1"})
        mismatch = m.compare({"a": "2"}, {"a": "1"})
        assert m.rank_key(strict) > m.rank_key(norm) > m.rank_key(mismatch)

    def test_rank_same_level_by_field_count(self, m):
        one = m.compare({"a": "1", "b": "x"}, {"a": "1", "b": "y"})
        two = m.compare({"a": "2", "b": "x"}, {"a": "1", "b": "y"})
        assert m.rank_key(one) > m.rank_key(two)  # 同级 MISMATCH，匹配字段多者优

    def test_aggregate(self, m):
        verdicts = [
            m.compare({"a": "1", "b": "x"}, {"a": "1", "b": "x"}),          # STRICT
            m.compare({"a": "１", "b": "y"}, {"a": "1", "b": "y"}),          # NORMALIZED
            m.compare({"a": "2", "c": "z"}, {"a": "1", "b": "y"}),          # MISMATCH
        ]
        report = m.aggregate(verdicts)
        assert report["sample_level_counts"] == {
            "STRICT": 1, "NORMALIZED_MATCH": 1, "MISMATCH": 1,
        }
        assert report["field_mismatch_distribution"]["a"]["mismatch"] == 1
        assert report["normalized_field_ratio"] == pytest.approx(1 / 7)
        assert report["key_missing_counts"]["KEY_MISSING_IN_PRED"] == 1
        assert report["key_missing_counts"]["KEY_MISSING_IN_GT"] == 1

    def test_verdict_to_dict_compat_fields(self, m):
        """comparison_result 序列化字段齐全（兼容老结果结构：is_match 等）。"""
        d = m.compare({"a": "1"}, {"a": "1"}).to_dict()
        assert d["is_match"] is True
        assert d["match_level"] == "STRICT"
        assert "fields" in d and "matched_field_count" in d
