"""鲁棒匹配器：验收判定与诊断分析共用的唯一组件。

实现审计报告 02 §4–§6 的需求规格，合并老代码中 RobustMatcher（分析）与
RobustJSONComparator（验收，代码缺失的黑盒）两份实现。

归一化规格边界（超出即 bug，见 R-N3）：
- 只做：全角→半角、标点相邻空白删除、首尾 strip；
- 不做：大小写转换、删除货币符号、删除非标点相邻的内部空格、数字格式等价；
- 禁用 unicodedata.NFKC（会把 ™→TM、①→1，把符号错误洗成一致）。
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# R-N1 全角 → 半角受控映射
# ---------------------------------------------------------------------------

def _build_fullwidth_map() -> dict:
    """构造全角→半角映射表（显式受控，不用 NFKC）。

    覆盖：全角 ASCII 区（U+FF01–FF5E 减 0xFEE0）、全角空格、货币宽度变体、
    常见中文标点→英文标点对照。
    """
    table = {chr(0xFF01 + i): chr(0x21 + i) for i in range(0x5E)}  # ！-~
    table["　"] = " "      # U+3000 全角空格
    table["￥"] = "¥"      # U+FFE5 → U+00A5
    table["￡"] = "£"      # U+FFE1 → U+00A3
    # 中文标点 → 英文标点（全角 ASCII 区不含的 CJK 标点）
    table.update({
        "，": ",", "：": ":", "；": ";", "？": "?", "！": "!",
        "（": "(", "）": ")", "【": "[", "】": "]",
        "“": '"', "”": '"', "‘": "'", "’": "'",
        "、": ",", "。": ".", "…": "...", "—": "-", "·": ".",
    })
    return table


_FULLWIDTH_MAP = _build_fullwidth_map()

# 标点相邻空白删除（R-N2）作用的标点集合：映射后全部为半角标点。
# 注意 ¥ 等货币符号不是标点，不参与此规则。
_PUNCT_RE = re.compile(r"[\s]*([,.:;?!\(\)\[\]\"'/\-])[\s]*")


def normalize(text: str) -> str:
    """归一化函数 N(text)：两个值 N(a) == N(b) 时判为归一化一致。

    规则（R-N1 + R-N2，仅此而已）：
    1. 全角字符→半角（受控映射表，含中文标点对照）；
    2. 删除紧邻标点前后的空白字符；
    3. 首尾 strip；其余位置的空格一律保留（英文姓名/地址内部空格承载语义）。

    明确不做（R-N3）：大小写转换、删除货币符号、删除内部空格、数字格式等价。

    Args:
        text: 原始字段值。

    Returns:
        归一化后的字符串。
    """
    text = "".join(_FULLWIDTH_MAP.get(ch, ch) for ch in text)
    text = _PUNCT_RE.sub(r"\1", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 逐字段三级判定（R-C1）与样本聚合（R-C2）
# ---------------------------------------------------------------------------

class MatchLevel(str, Enum):
    """字段级判定级别。"""

    STRICT = "STRICT"                          # 原始值完全相等
    NORMALIZED = "NORMALIZED"                  # 归一化后相等（格式噪声）
    MISMATCH = "MISMATCH"                      # 归一化后仍不等（真实差异）
    KEY_MISSING_IN_PRED = "KEY_MISSING_IN_PRED"  # 模型漏字段
    KEY_MISSING_IN_GT = "KEY_MISSING_IN_GT"      # GT 缺字段 / 模型多输出


# 样本级判定（R-C2）
SAMPLE_STRICT = "STRICT"
SAMPLE_NORMALIZED_MATCH = "NORMALIZED_MATCH"
SAMPLE_MISMATCH = "MISMATCH"

# 样本级排序权重：抽卡/多尝试选优时级别优先
_LEVEL_RANK = {SAMPLE_STRICT: 2, SAMPLE_NORMALIZED_MATCH: 1, SAMPLE_MISMATCH: 0}


@dataclass
class FieldVerdict:
    """单个字段（叶子值）的判定明细。"""

    field: str            # 字段路径，嵌套用 a.b[0].c 表示
    level: MatchLevel
    pred_value: Any = None
    gt_value: Any = None


@dataclass
class SampleVerdict:
    """样本级判定结果：全字段明细 + 聚合级别。

    is_accepted 为验收口径（R-C3）：STRICT 与 NORMALIZED_MATCH 均算通过，
    格式噪声不应否决推理正确的样本。
    """

    level: str
    fields: list = field(default_factory=list)  # List[FieldVerdict]

    @property
    def matched_field_count(self) -> int:
        """STRICT + NORMALIZED 字段数（同级选优依据）。"""
        return sum(
            1 for f in self.fields
            if f.level in (MatchLevel.STRICT, MatchLevel.NORMALIZED)
        )

    @property
    def is_accepted(self) -> bool:
        return self.level in (SAMPLE_STRICT, SAMPLE_NORMALIZED_MATCH)

    def to_dict(self) -> dict:
        """序列化为结果字典中的 comparison_result / robust_match 字段。"""
        return {
            "match_level": self.level,
            "is_match": self.is_accepted,
            "matched_field_count": self.matched_field_count,
            "total_field_count": len(self.fields),
            "fields": [
                {
                    "field": f.field,
                    "level": f.level.value,
                    "pred_value": f.pred_value,
                    "gt_value": f.gt_value,
                }
                for f in self.fields
            ],
        }


def _flatten(obj: Any, prefix: str = "") -> dict:
    """把嵌套 dict/list 展平为 {路径: 叶子值}。

    数组按位置对齐（首版不支持乱序对齐，留作扩展）。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, path))
        return out
    if isinstance(obj, list):
        out = {}
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
        return out
    return {prefix: obj}


def _compare_leaf(pred: Any, gt: Any) -> MatchLevel:
    """叶子值三级判定。

    类型规则：两端均为 str 才走归一化比较；其余类型只做严格相等，
    避免 str(None) == "None" 这类假阳性（审计报告 02 §2.6）。
    """
    if isinstance(pred, str) and isinstance(gt, str):
        if pred == gt:
            return MatchLevel.STRICT
        if normalize(pred) == normalize(gt):
            return MatchLevel.NORMALIZED
        return MatchLevel.MISMATCH
    # JSON 数值：int/float 跨类型但值相等视为 STRICT（bool 除外，True != 1）
    numeric = (int, float)
    if (isinstance(pred, numeric) and isinstance(gt, numeric)
            and not isinstance(pred, bool) and not isinstance(gt, bool)):
        return MatchLevel.STRICT if pred == gt else MatchLevel.MISMATCH
    if type(pred) is type(gt) and pred == gt:
        return MatchLevel.STRICT
    return MatchLevel.MISMATCH


class Matcher:
    """验收判定 + 诊断分析的单一组件。

    无状态，可全局共享一个实例。主流程验收（is_accepted）与结果中的
    诊断明细（to_dict）由同一次 compare 产出，保证判定一致性（§7.3）。
    """

    def compare(self, pred: dict, gt: dict) -> SampleVerdict:
        """逐字段比较预测与 GT，返回样本级判定。

        Args:
            pred: 模型提取的 JSON 对象。
            gt: Ground Truth JSON 对象。

        Returns:
            SampleVerdict，含全部字段明细（一个字段都不吞）。
        """
        pred_flat = _flatten(pred)
        gt_flat = _flatten(gt)

        fields = []
        for path in sorted(set(pred_flat) | set(gt_flat)):
            in_pred = path in pred_flat
            in_gt = path in gt_flat
            if in_pred and in_gt:
                level = _compare_leaf(pred_flat[path], gt_flat[path])
            elif in_pred:
                level = MatchLevel.KEY_MISSING_IN_GT
            else:
                level = MatchLevel.KEY_MISSING_IN_PRED
            fields.append(FieldVerdict(
                field=path,
                level=level,
                pred_value=pred_flat.get(path),
                gt_value=gt_flat.get(path),
            ))

        # R-C2 聚合：全 STRICT → STRICT；无 MISMATCH 但有 NORMALIZED →
        # NORMALIZED_MATCH；任一 MISMATCH / KEY_MISSING → MISMATCH
        bad = {MatchLevel.MISMATCH, MatchLevel.KEY_MISSING_IN_PRED,
               MatchLevel.KEY_MISSING_IN_GT}
        if all(f.level == MatchLevel.STRICT for f in fields):
            level = SAMPLE_STRICT
        elif not any(f.level in bad for f in fields):
            level = SAMPLE_NORMALIZED_MATCH
        else:
            level = SAMPLE_MISMATCH
        return SampleVerdict(level=level, fields=fields)

    def rank_key(self, verdict: SampleVerdict) -> tuple:
        """多尝试选优排序键：级别优先，同级按匹配字段数。越大越优。"""
        return (_LEVEL_RANK[verdict.level], verdict.matched_field_count)

    def aggregate(self, verdicts: list) -> dict:
        """GT 交叉验证离线分析（§6），batch 结束时调用，不进主流程。

        产出：字段级 MISMATCH 分布、NORMALIZED 占比（格式噪声率）、
        KEY_MISSING 方向统计、样本级判定计数。

        Args:
            verdicts: 全样本的 SampleVerdict 列表。

        Returns:
            可 JSON 序列化的分析字典。
        """
        field_stats = {}   # path -> {"mismatch": n, "total": n}
        normalized_count = 0
        total_fields = 0
        key_missing = {MatchLevel.KEY_MISSING_IN_PRED.value: 0,
                       MatchLevel.KEY_MISSING_IN_GT.value: 0}
        level_counts = {SAMPLE_STRICT: 0, SAMPLE_NORMALIZED_MATCH: 0,
                        SAMPLE_MISMATCH: 0}

        for v in verdicts:
            level_counts[v.level] += 1
            for f in v.fields:
                total_fields += 1
                stat = field_stats.setdefault(f.field, {"mismatch": 0, "total": 0})
                stat["total"] += 1
                if f.level == MatchLevel.MISMATCH:
                    stat["mismatch"] += 1
                elif f.level == MatchLevel.NORMALIZED:
                    normalized_count += 1
                elif f.level in (MatchLevel.KEY_MISSING_IN_PRED,
                                 MatchLevel.KEY_MISSING_IN_GT):
                    key_missing[f.level.value] += 1

        return {
            "sample_level_counts": level_counts,
            "normalized_field_ratio": (
                normalized_count / total_fields if total_fields else 0.0
            ),
            "field_mismatch_distribution": {
                path: {**s, "rate": s["mismatch"] / s["total"]}
                for path, s in sorted(
                    field_stats.items(),
                    key=lambda kv: kv[1]["mismatch"] / kv[1]["total"],
                    reverse=True,
                )
                if s["mismatch"] > 0
            },
            "key_missing_counts": key_missing,
        }
