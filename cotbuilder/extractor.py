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
from typing import Any, Optional

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


def extract_json(text: Optional[str]) -> Optional[dict]:
    """从文本中提取第一个可解析的 JSON 对象，失败返回 None。

    Args:
        text: 模型响应文本或 GT 文本，可能为 None 或空串。

    Returns:
        提取出的 dict；无法提取时返回 None。
    """
    if not text:
        return None

    # 1. 整体直解
    stripped = text.strip()
    result = _loads_obj(stripped)
    if result is not None:
        return result

    # 2. 单引号 Python 风格字典兜底（ast.literal_eval 安全解析）
    try:
        value = ast.literal_eval(stripped)
        if isinstance(value, dict):
            return value
    except (ValueError, SyntaxError, MemoryError):
        pass

    # 3. 代码块：取 ``` 标记之后的第一个平衡对象
    for m in _CODE_BLOCK_RE.finditer(text):
        brace = text.find("{", m.end())
        if brace == -1:
            continue
        candidate = _scan_balanced(text, brace)
        if candidate:
            result = _loads_obj(candidate)
            if result is not None:
                return result

    # 4. 全文第一个平衡对象（含单引号变体）
    brace = text.find("{")
    while brace != -1:
        candidate = _scan_balanced(text, brace)
        if candidate:
            result = _loads_obj(candidate)
            if result is None:
                try:
                    value = ast.literal_eval(candidate)
                    if isinstance(value, dict):
                        result = value
                except (ValueError, SyntaxError, MemoryError):
                    pass
            if result is not None:
                return result
            # 该对象解析失败，跳到其之后继续找
            brace = text.find("{", brace + len(candidate))
        else:
            brace = text.find("{", brace + 1)

    return None
