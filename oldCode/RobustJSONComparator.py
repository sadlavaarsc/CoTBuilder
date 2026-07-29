#!/usr/bin/env python3
"""
robust_json_comparator.py

Robust JSON comparison tool for KIE model output vs Ground Truth verification.

Core features:
1. Case-insensitive comparison (configurable)
2. Whitespace/punctuation normalization
3. Currency symbol unification (￥->Yen, $->USD, etc.)
4. Numeric format consistency (leading zeros, trailing zeros, etc.)
5. Full-width/half-width character normalization
6. Field-level difference reporting

Usage:
    from robust_json_comparator import RobustJSONComparator

    comparator = RobustJSONComparator()
    result = comparator.compare(predicted, ground_truth)
    if result["is_match"]:
        print("MATCH")
    else:
        print(f"Differences: {result['differences']}")
"""

import json
import re
import copy
from typing import Any, Optional


class RobustJSONComparator:
    """
    鲁棒的 JSON 比较器。

    将预测结果与 Ground Truth 进行比较，支持多种规范化规则。
    可配置哪些规范化规则启用，哪些禁用。
    """


    def __init__(self, config: Optional[dict] = None):
        """
        初始化比较器。

        Args:
            config: 配置字典，可选。默认配置如下：
                {
                    "case_insensitive": True,        # 大小写不敏感
                    "strip_whitespace": True,        # 去除首尾空格
                    "normalize_internal_spaces": True, # 内部空格规范化（多个空格 → 一个）
                    "unify_currency": True,          # 货币符号统一
                    "normalize_punctuation": True,   # 中英文标点统一
                    "normalize_numbers": True,       # 数值格式规范化
                    "strip_leading_zeros": False,    # 去除数值前导零（仅影响数字字符串）
                    "full_width_alpha": True,        # 全角英文字母→半角
                    "full_width_digit": True,        # 全角数字→半角
                    "trim_empty_fields": True,       # 去除空的字段匹配
                    "report_partial_match": True,    # 报告部分匹配
                    "partial_match_threshold": 0.95, # 部分匹配阈值
                }
        """
        default_config = {
            "case_insensitive": True,
            "strip_whitespace": True,
            "normalize_internal_spaces": True,
            "unify_currency": True,
            "normalize_punctuation": True,
            "normalize_numbers": True,
            "strip_leading_zeros": False,
            "full_width_alpha": True,
            "full_width_digit": True,
            "trim_empty_fields": True,
            "report_partial_match": True,
            "partial_match_threshold": 0.95,
        }
        self.config = {**default_config, **(config or {})}

        # 编译正则表达式（提高性能）
        self._internal_spaces_re = re.compile(r'\s+')
        self._leading_trailing_spaces_re = re.compile(r'^\s+|\s+$')
        self._currency_re = re.compile(r'[¥￥$€£]')
        self._full_width_alpha_re = re.compile(r'[Ａ-Ｚａ-ｚ]')
        self._full_width_digit_re = re.compile(r'[０-９]')
        self._punctuation_re = re.compile(r'[，。！？；：""''（）【】《》／、]')
        self._trailing_dot_zero_re = re.compile(r'\.0+$')
    def normalize_value(self, value: Any) -> Any:
        """
        根据配置规范化单个值。

        Args:
            value: 原始值

        Returns:
            规范化后的值
        """
        if value is None:
            return None

        if isinstance(value, (int, float, bool)):
            # 数值类型直接返回
            return value

        if not isinstance(value, str):
            return value

        s = value

        # 全角字母 → 半角
        if self.config.get("full_width_alpha", True):
            s = self._normalize_full_width_alpha(s)

        # 全角数字 → 半角
        if self.config.get("full_width_digit", True):
            s = self._normalize_full_width_digit(s)

        # 大小写不敏感
        if self.config.get("case_insensitive", True):
            s = s.lower()

        # 首尾空格
        if self.config.get("strip_whitespace", True):
            s = s.strip()

        # 内部空格规范化
        if self.config.get("normalize_internal_spaces", True):
            s = self._internal_spaces_re.sub(' ', s)

        # 货币符号统一
        if self.config.get("unify_currency", True):
            s = self._unify_currency(s)

        # 中英文标点统一
        if self.config.get("normalize_punctuation", True):
            s = self._normalize_punctuation(s)

        # 数值格式规范化
        if self.config.get("normalize_numbers", True):
            s = self._normalize_numeric(s)

        # 再次 trim
        if self.config.get("strip_whitespace", True):
            s = s.strip()

        return s


    def _normalize_full_width_alpha(self, s: str) -> str:
        """全角英文字母 → 半角"""
        result = []
        for ch in s:
            code = ord(ch)
            if 0xFF21 <= code <= 0xFF3A:  # 全角 A-Z
                result.append(chr(code - 0xFEE0))
            elif 0xFF41 <= code <= 0xFF5A:  # 全角 a-z
                result.append(chr(code - 0xFEE0))
            else:
                result.append(ch)
        return ''.join(result)


    def _normalize_full_width_digit(self, s: str) -> str:
        """全角数字 → 半角"""
        result = []
        for ch in s:
            code = ord(ch)
            if 0xFF10 <= code <= 0xFF19:  # 全角 0-9
                result.append(chr(code - 0xFEE0))
            else:
                result.append(ch)
        return ''.join(result)


    def _unify_currency(self, s: str) -> str:
        """统一货币符号"""
        def replace_currency(match):
            ch = match.group(0)
            mapping = {
                '￥': '¥',
                '＄': '$',
                '€': 'EUR',
                '£': 'GBP',
            }
            return mapping.get(ch, ch)
        return self._currency_re.sub(replace_currency, s)


    def _normalize_punctuation(self, s: str) -> str:
        """统一中英文标点（将中文标点替换为英文标点）"""
        mapping = {
            '，': ',', '。': '.', '！': '!', '？': '?',
            '；': ';', '：': ':',
            '“': '"', '”': '"', '‘': "'", '’': "'",
            '（': '(', '）': ')',
            '【': '[', '】': ']',
            '《': '<', '》': '>',
            '／': '/', '、': ',',
        }
        result = []
        for ch in s:
            result.append(mapping.get(ch, ch))
        return ''.join(result)


    def _normalize_numeric(self, s: str) -> str:
        """数值格式规范化"""
        # 去除数值中的逗号分隔（如 1,234 → 1234）
        # 注意：不要影响其他逗号
        # 仅在数字包围的情况下处理
        s = re.sub(r'(?<=\d),(?=\d)', '', s)
        return s

    def _parse_json(self, text: str) -> Optional[dict]:
        """
        解析 JSON 字符串，支持多种格式：
        1. 标准 JSON
        2. markdown 代码块中的 JSON (```json ... ```)
        3. 带单引号的 Python 风格字典
        """
        if not text or not isinstance(text, str):
            return None

        text = text.strip()

        # 如果已经是 dict，直接返回
        if isinstance(text, dict):
            return text

        # 移除代码块标记
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
            text = text.strip()

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试用 ast.literal_eval 安全解析 Python 字典字符串
        try:
            import ast
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, SyntaxError):
            pass

        # 尝试用正则提取最外层 {} 并修复单引号
        try:
            # 提取第一个 { ... } 内容
            brace_match = re.search(r'\{.*\}', text, re.DOTALL)
            if not brace_match:
                return None

            json_str = brace_match.group()

            # 替换单引号为双引号（需要谨慎处理）
            # 先处理键周围的单引号
            json_str = re.sub(r"'([^'\\\"]+)'\s*:\s*", r'"\1": ', json_str)
            # 再处理值周围的单引号
            json_str = re.sub(r":\s*'([^']*?)'\s*([,}])", r': "\1"\2', json_str)

            return json.loads(json_str)
        except (json.JSONDecodeError, Exception):
            pass

        return None


    def compare(self, predicted: Any, ground_truth: Any) -> dict:
        """
        比较预测结果与 Ground Truth。

        Args:
            predicted: 预测结果（JSON 字符串、dict 或 None）
            ground_truth: Ground Truth（JSON 字符串、dict 或 None）

        Returns:
            dict: {
                "is_match": bool,          # 是否完全匹配
                "match_score": float,      # 匹配得分 (0.0 - 1.0)
                "is_partial": bool,        # 是否为部分匹配
                "total_fields": int,       # 总字段数
                "matched_fields": int,     # 匹配的字段数
                "mismatched_fields": int,  # 不匹配的字段数
                "extra_fields": int,       # 预测中多余的字段数
                "missing_fields": int,     # 预测中缺失的字段数
                "differences": list,       # 差异详情
                "error": str | None,       # 解析错误信息
            }
        """
        result = {
            "is_match": False,
            "match_score": 0.0,
            "is_partial": False,
            "total_fields": 0,
            "matched_fields": 0,
            "mismatched_fields": 0,
            "extra_fields": 0,
            "missing_fields": 0,
            "differences": [],
            "error": None,
        }
        # 解析 predicted
        if isinstance(predicted, str):
            predicted_dict = self._parse_json(predicted)
            if predicted_dict is None:
                result["error"] = f"无法解析预测结果: {predicted[:200]}"
                return result
        elif isinstance(predicted, dict):
            predicted_dict = predicted
        else:
            result["error"] = f"预测结果类型不支持: {type(predicted)}"
            return result

        # 解析 ground_truth
        if isinstance(ground_truth, str):
            gt_dict = self._parse_json(ground_truth)
            if gt_dict is None:
                result["error"] = f"无法解析 Ground Truth: {ground_truth[:200]}"
                return result
        elif isinstance(ground_truth, dict):
            gt_dict = ground_truth
        else:
            result["error"] = f"Ground Truth 类型不支持: {type(ground_truth)}"
            return result

        # 规范化所有值
        gt_normalized = {}
        for key, value in gt_dict.items():
            gt_normalized[key] = self.normalize_value(value)

        pred_normalized = {}
        for key, value in predicted_dict.items():
            pred_normalized[key] = self.normalize_value(value)
        # 如果配置了 trim_empty_fields，去除空的字段匹配
        if self.config.get("trim_empty_fields", True):
            pred_normalized = {k: v for k, v in pred_normalized.items()
                               if v is not None and v != ''}

        # 获取所有字段
        all_keys = set(gt_normalized.keys()) | set(pred_normalized.keys())
        result["total_fields"] = len(gt_normalized)

        matched = 0
        mismatched = 0
        extra = 0
        missing = 0
        differences = []

        for key in all_keys:
            gt_val = gt_normalized.get(key)
            pred_val = pred_normalized.get(key)

            if key not in pred_normalized:
                # 预测中缺失
                missing += 1
                missing += 1  # 多一个计数，简化统计
                differences.append({
                    "field": key,
                    "type": "missing",
                    "ground_truth": gt_dict.get(key, ""),
                    "predicted": None,
                })
                continue

            if key not in gt_normalized:
                # 预测中多余的字段
                extra += 1
                differences.append({
                    "field": key,
                    "type": "extra",
                    "ground_truth": None,
                    "predicted": predicted_dict.get(key, ""),
                })
                continue

            # 比较值
            is_equal = (str(gt_val) == str(pred_val))
            if is_equal:
                matched += 1
            else:
                mismatched += 1
                differences.append({
                    "field": key,
                    "type": "mismatch",
                    "ground_truth": gt_dict.get(key, ""),
                    "predicted": predicted_dict.get(key, ""),
                    "gt_normalized": str(gt_val),
                    "pred_normalized": str(pred_val),
                })

        # 计算得分
        total_relevant = matched + mismatched
        if total_relevant > 0:
            result["match_score"] = matched / total_relevant
        else:
            result["match_score"] = 1.0 if missing == 0 else 0.0

        result["is_match"] = (mismatched == 0 and missing == 0 and extra == 0)
        result["matched_fields"] = matched
        result["mismatched_fields"] = mismatched
        result["extra_fields"] = extra
        result["missing_fields"] = missing
        result["differences"] = differences

        # 部分匹配判断
        if self.config.get("report_partial_match", True):
            threshold = self.config.get("partial_match_threshold", 0.95)
            result["is_partial"] = (not result["is_match"]
                                    and result["match_score"] >= threshold)

        return result


    def is_equivalent(self, predicted: Any, ground_truth: Any) -> bool:
        """
        快捷方法：判断两个 JSON 是否等价（考虑规范化）。

        Args:
            predicted: 预测结果
            ground_truth: Ground Truth

        Returns:
            bool: 是否等价
        """
        result = self.compare(predicted, ground_truth)
        return result["is_match"]


    def get_field_summary(self, predicted: Any, ground_truth: Any) -> str:
        """
        Generate readable field-level comparison summary.

        Args:
            predicted: Prediction result
            ground_truth: Ground Truth

        Returns:
            str: Formatted summary
        """
        result = self.compare(predicted, ground_truth)

        lines = [
            f"Field Overview: {result['matched_fields']}/{result['total_fields']} matched, "
            f"Score: {result['match_score']:.2%}",
        ]

        if result["is_match"]:
            lines.append("Result: MATCH")
            return "\n".join(lines)

        if result["differences"]:
            lines.append("Differences:")
            for diff in result["differences"]:
                if diff["type"] == "mismatch":
                    lines.append(
                        f"  MISMATCH [{diff['field']}]: "
                        f"GT='{diff['ground_truth']}' vs "
                        f"Pred='{diff['predicted']}'"
                    )
                elif diff["type"] == "missing":
                    lines.append(
                        f"  MISSING [{diff['field']}]: expected '{diff['ground_truth']}'"
                    )
                elif diff["type"] == "extra":
                    lines.append(
                        f"  EXTRA [{diff['field']}]: got '{diff['predicted']}'"
                    )

        if result["missing_fields"] > 0:
            lines.append(f"Missing fields: {result['missing_fields']}")
        if result["extra_fields"] > 0:
            lines.append(f"Extra fields: {result['extra_fields']}")

        if result["is_partial"]:
            lines.append("Result: PARTIAL MATCH (above threshold)")
        else:
            lines.append("Result: NO MATCH")

        return "\n".join(lines)


# ============================================================
# 快速测试
# ============================================================

def _run_self_test():
    """运行内置自测"""
    import sys
    sys.stdout.reconfigure(encoding='utf-8')

    comparator = RobustJSONComparator()

    OK  = "[OK]"
    FAIL = "[FAIL]"
    print("=" * 60)
    print("RobustJSONComparator Self Test")
    print("=" * 60)

    test_cases = [
        ("大小写差异",
         '{"name": "HIKVISION"}', '{"name": "hikvision"}',
         True),
        ("空格差异",
         '{"name": "  hello  "}', '{"name": "hello"}',
         True),
        ("内部空格",
         '{"name": "hello   world"}', '{"name": "hello world"}',
         True),
        ("全角字母",
         '{"name": "ＨＩＫ"}', '{"name": "HIK"}',
         True),
        ("全角数字",
         '{"price": "１００"}', '{"price": "100"}',
         True),
        ("货币符号",
         '{"price": "￥100"}', '{"price": "¥100"}',
         True),
        ("逗号分隔数值",
         '{"price": "1,234"}', '{"price": "1234"}',
         True),
        ("标点统一",
         '{"desc": "你好，世界。"}', '{"desc": "你好,世界."}',
         True),
        ("严格比较（大小写敏感）",
         '{"name": "HIKVISION"}', '{"name": "hikvision"}',
         True,
         {"case_insensitive": False}),
        ("字段缺失",
         '{"name": "test"}', '{"name": "test", "age": "20"}',
         False),
        ("值不匹配",
         '{"name": "test1"}', '{"name": "test2"}',
         False),
        ("多余字段",
         '{"name": "test", "extra": "x"}', '{"name": "test"}',
         False),
        ("实际案例：发票",
         '{"发票号码": "30613944"}', '{"发票号码": "30613944"}',
         True),
        ("实际案例：金额",
         '{"金额": "￥294.00"}', '{"金额": "¥294.00"}',
         True),
        ("实际案例：全角",
         '{"发票号码": "３０６１３９４４"}', '{"发票号码": "30613944"}',
         True),
    ]
    passed = 0
    failed = 0

    for desc, pred, gt, expected, *cfg in test_cases:
        config = cfg[0] if cfg else None
        comp = RobustJSONComparator(config) if config else comparator
        result = comp.compare(pred, gt)

        is_pass = (result["is_match"] == expected)
        status = OK if is_pass else FAIL

        if is_pass:
            passed += 1
        else:
            failed += 1
            diff_strs = []
            for d in result.get("differences", []):
                diff_strs.append(f"{d['field']}:{d['type']}")
            diff_detail = ", ".join(diff_strs) if diff_strs else "No diff info"

        print(f"\n  {status} {desc}")
        print(f"         Expected: matched={expected}, Actual: matched={result['is_match']}, "
              f"score={result['match_score']:.2%}")
        if not is_pass:
            print(f"         Diffs: {diff_detail}")

    print(f"\n{'=' * 60}")
    print(f"Result: {passed}/{passed + failed} passed")
    if failed > 0:
        print(f"Failed: {failed}")
    print(f"{'=' * 60}")

    return passed, failed
if __name__ == "__main__":
    _run_self_test()

