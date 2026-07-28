"""extractor 单元测试。"""

from cotbuilder.extractor import extract_json


class TestDirectParse:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_whitespace_padded(self):
        assert extract_json('  \n {"a": 1} \n') == {"a": 1}

    def test_non_object_json_returns_none(self):
        # 顶层数组/标量不是提取目标
        assert extract_json('[1, 2]') is None
        assert extract_json('"just a string"') is None


class TestCodeBlock:
    def test_json_code_block(self):
        text = '推理过程……\n```json\n{"发票号码": "12345"}\n```\n完'
        assert extract_json(text) == {"发票号码": "12345"}

    def test_plain_code_block(self):
        text = '```\n{"a": [1, 2]}\n```'
        assert extract_json(text) == {"a": [1, 2]}


class TestBalancedScan:
    def test_nested_object_in_prose(self):
        """老代码非贪婪正则 \\{.*?\\} 在此必然截断的回归用例（明细行嵌套）。"""
        text = '结果如下：{"明细": [{"单价": "¥5.83", "数量": 2}], "总价": "¥11.66"} 以上'
        assert extract_json(text) == {
            "明细": [{"单价": "¥5.83", "数量": 2}],
            "总价": "¥11.66",
        }

    def test_braces_inside_strings(self):
        text = 'prefix {"note": "括号 { 在字符串里 }"} suffix'
        assert extract_json(text) == {"note": "括号 { 在字符串里 }"}

    def test_escaped_quotes_inside_strings(self):
        text = '{"a": "he said \\"{\\" ok"}'
        assert extract_json(text) == {"a": 'he said "{" ok'}

    def test_first_invalid_then_valid(self):
        text = '{invalid json} 然后是 {"a": 1}'
        assert extract_json(text) == {"a": 1}


class TestGarbage:
    def test_empty_and_none(self):
        assert extract_json("") is None
        assert extract_json(None) is None

    def test_no_json(self):
        assert extract_json("模型没有输出任何 JSON") is None

    def test_unbalanced(self):
        assert extract_json('{"a": 1') is None
