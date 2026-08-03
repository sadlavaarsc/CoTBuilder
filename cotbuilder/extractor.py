"""从模型响应文本中提取 JSON 对象。

模型响应与 Ground Truth 文本的解析共用本模块，保证「什么算合法 JSON」
只有一份实现（老代码中 GT 解析在仓库外黑盒模块里，见审计报告 02 §3）。

提取策略（按优先级）：
1. 整体直接 json.loads；
2. ast.literal_eval 兜底（单引号 Python 风格字典，吸收自原版
   RobustJSONComparator._parse_json）；
3. ```json 代码块中的第一个平衡 JSON 对象；
4. 文本中第一个平衡花括号对象。

注意：老代码最后一步用非贪婪正则 ``\\{.*?\\}``，遇到嵌套 JSON（单据明细行）
必然截断，这里改为平衡括号扫描——这是有依据的行为修正，见 doc/design.md。
"""

import ast
import json
import re
from typing import Any, Optional, Tuple

_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*", re.IGNORECASE)


def _scan_balanced(text: str, start: int) -> Optional[str]:
    """从 text[start]（必须是 '{'）起扫描到与其平衡的 '}'，返回子串。

    正确处理字符串字面量内的括号与转义字符；找不到平衡右括号返回 None。
    """
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _loads_obj(candidate: str) -> Optional[dict]:
    """尝试把 candidate 解析为 JSON 对象（dict），失败或非对象返回 None。"""
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _ast_obj(candidate: str) -> Optional[dict]:
    """ast.literal_eval 兜底（单引号 Python 风格字典），失败返回 None。"""
    try:
        value = ast.literal_eval(candidate)
    except (ValueError, SyntaxError, MemoryError):
        return None
    return value if isinstance(value, dict) else None


def find_json_span(text: Optional[str]) -> Optional[Tuple[int, int]]:
    """定位文本中第一个可解析 JSON 对象的 (start, end) 偏移（end 不含）。

    与 extract_json 完全同一套提取优先级（整体 → 代码块 → 全文平衡
    扫描），只是返回**位置**而不是解析结果；text[start:end] 保证可被
    json 或 ast 解析为 dict。用途：convert.py 把 cot_response 拆成
    「推理链文本 + JSON 答案」两段（剥离 JSON span 即得推理链）。
    """
    if not text:
        return None

    # 1. 整体（strip 后 json 或 ast 任一可解为 dict）
    begin = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    stripped = text[begin:end]
    if _loads_obj(stripped) is not None or _ast_obj(stripped) is not None:
        return (begin, end)

    # 2. 代码块：取 ``` 标记之后的第一个平衡对象
    for m in _CODE_BLOCK_RE.finditer(text):
        brace = text.find("{", m.end())
        if brace == -1:
            continue
        candidate = _scan_balanced(text, brace)
        if candidate and _loads_obj(candidate) is not None:
            return (brace, brace + len(candidate))

    # 3. 全文第一个平衡对象（json 或 ast 任一可解）
    brace = text.find("{")
    while brace != -1:
        candidate = _scan_balanced(text, brace)
        if candidate:
            if (_loads_obj(candidate) is not None
                    or _ast_obj(candidate) is not None):
                return (brace, brace + len(candidate))
            # 该对象解析失败，跳到其之后继续找
            brace = text.find("{", brace + len(candidate))
        else:
            brace = text.find("{", brace + 1)

    return None


def extract_json(text: Optional[str]) -> Optional[dict]:
    """从文本中提取第一个可解析的 JSON 对象，失败返回 None。

    Args:
        text: 模型响应文本或 GT 文本，可能为 None 或空串。

    Returns:
        提取出的 dict；无法提取时返回 None。
    """
    span = find_json_span(text)
    if span is None:
        return None
    candidate = text[span[0]:span[1]]
    result = _loads_obj(candidate)
    if result is not None:
        return result
    return _ast_obj(candidate)
