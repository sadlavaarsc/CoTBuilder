"""generator 寿命循环测试（FakeClient 脚本化结果，不依赖网络）。

验收指标：
- 寿命语义：纯 MISMATCH 时请求数 == max_sample_attempts；纯网络错误时
  == network_max_attempts；混合时 ≤ 两者之和；两本账互不挤占；
- 一遍过即成功，不再尝试；
- 寿命耗尽按 rank_key 选历史最优收尾；
- 配额分账桶正确；attempts 为真实请求数。
"""

import json

import pytest

from cotbuilder.client import CallOutcome, ErrorType
from cotbuilder.config import Config
from cotbuilder.generator import SampleProcessor
from cotbuilder.matcher import Matcher


class FakeClient:
    """按脚本返回 CallOutcome 的假 client，记录每次调用的 kind。"""

    def __init__(self, script):
        self._script = list(script)
        self.kinds = []

    async def call(self, messages, sample_id=None, kind="initial"):
        self.kinds.append(kind)
        return self._script.pop(0)


def ok(content: dict) -> CallOutcome:
    return CallOutcome(ok=True, response={"choices": []},
                       content=json.dumps(content, ensure_ascii=False))


def err(error: ErrorType, retry_after=None) -> CallOutcome:
    return CallOutcome(ok=False, error=error, retry_after=retry_after)


GT = {"a": "1", "b": "x", "c": "y"}


def make_processor(script, **cfg_overrides) -> SampleProcessor:
    cfg = Config(api_key="k", backoff_base=0.001, backoff_cap=0.002,
                 **cfg_overrides)
    return SampleProcessor(FakeClient(script), Matcher(), cfg)


def make_sample(fmt="messages") -> dict:
    gt_text = json.dumps(GT)
    if fmt == "messages":
        return {"id": "s1", "messages": [
            {"role": "user", "content": "<image>\n提取"},
            {"role": "assistant", "content": gt_text}]}
    return {"id": "s1", "conversations": [
        {"from": "human", "value": "<image>\n提取"},
        {"from": "gpt", "value": gt_text}]}


class TestLifeCycleSemantics:
    async def test_first_try_pass_no_more_requests(self):
        """一遍过：1 次请求即成功，不再尝试。"""
        p = make_processor([ok(GT)])
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "success"
        assert r["attempts"] == 1
        assert p._client.kinds == ["initial"]

    async def test_pure_mismatch_uses_exactly_sample_life(self):
        """纯 MISMATCH：请求数 == max_sample_attempts，失败 MISMATCH。"""
        bad = {"a": "9", "b": "9", "c": "9"}
        p = make_processor([ok(bad)] * 5, max_sample_attempts=3)
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "failed"
        assert r["error_type"] == "MISMATCH"
        assert r["attempts"] == 3
        assert p._client.kinds == ["initial", "retry_quality", "retry_quality"]

    async def test_pure_network_uses_exactly_network_life(self):
        """纯网络错误：请求数 == network_max_attempts，两本账互不影响。"""
        p = make_processor([err(ErrorType.NETWORK_ERROR)] * 10,
                           network_max_attempts=5)
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "failed"
        assert r["error_type"] == "NETWORK_ERROR"
        assert r["attempts"] == 5
        assert p._client.kinds == ["initial"] + ["retry_network"] * 4

    async def test_mixed_bounded_by_sum(self):
        """混合错误：请求数 ≤ sample_life + network_life，互记账。"""
        bad = {"a": "9"}
        script = [
            ok(bad),                          # mismatch（sample 3→2）
            err(ErrorType.NETWORK_ERROR),     # network（network 5→4）
            ok(bad),                          # mismatch（sample 2→1）
            err(ErrorType.RATE_LIMITED),      # network（network 4→3）
            ok(bad),                          # mismatch（sample 1→0）→ 终止
            ok(GT),                           # 不应被消费
        ]
        p = make_processor(script, max_sample_attempts=3,
                           network_max_attempts=5)
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "failed"
        assert r["attempts"] == 5
        assert p._client.kinds == [
            "initial", "retry_quality", "retry_network",
            "retry_quality", "retry_network",
        ]

    async def test_network_retry_then_pass(self):
        """网络重试后成功：attempts 含重试，retry_after 被尊重。"""
        p = make_processor([
            err(ErrorType.RATE_LIMITED, retry_after=0.001),
            err(ErrorType.NETWORK_ERROR),
            ok(GT),
        ])
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "success"
        assert r["attempts"] == 3

    async def test_api_error_fails_immediately(self):
        """API_ERROR 不重试不耗寿命（显式决策）。"""
        p = make_processor([err(ErrorType.API_ERROR), ok(GT)])
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "failed"
        assert r["error_type"] == "API_ERROR"
        assert r["attempts"] == 1

    async def test_empty_response_fails_immediately(self):
        p = make_processor([err(ErrorType.EMPTY_RESPONSE), ok(GT)])
        r = await p.process(make_sample(), "s1")
        assert r["error_type"] == "EMPTY_RESPONSE"
        assert r["attempts"] == 1

    async def test_length_truncated_fails_immediately(self):
        """LENGTH_TRUNCATED（thinking 耗尽，确定性失败）同样不重试。"""
        p = make_processor([err(ErrorType.LENGTH_TRUNCATED), ok(GT)])
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "failed"
        assert r["error_type"] == "LENGTH_TRUNCATED"
        assert r["attempts"] == 1


class TestGatewayError:
    async def test_gateway_error_retried_then_success(self):
        """GATEWAY_ERROR 走网络账退避重试（retry_network 桶）。"""
        p = make_processor([err(ErrorType.GATEWAY_ERROR), ok(GT)])
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "success"
        assert r["attempts"] == 2
        assert p._client.kinds == ["initial", "retry_network"]

    async def test_gateway_retry_capped(self):
        """504 重试受 gateway_max_attempts（默认 2）封顶：
        1 次 initial + 2 次重试后放弃，不烧满 network_life。"""
        p = make_processor([err(ErrorType.GATEWAY_ERROR) for _ in range(5)])
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "failed"
        assert r["error_type"] == "GATEWAY_ERROR"
        assert r["attempts"] == 3          # 1 + 2 封顶，而非 network 寿命 5
        assert p._client.kinds == ["initial"] + ["retry_network"] * 2

    async def test_gateway_error_still_consumes_network_life(self):
        """两个约束同时生效：network_life 先耗尽时按其收尾。"""
        p = make_processor([err(ErrorType.GATEWAY_ERROR) for _ in range(3)],
                           network_max_attempts=2, gateway_max_attempts=10)
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "failed"
        assert r["error_type"] == "GATEWAY_ERROR"
        assert r["attempts"] == 2          # network_life=2 先耗尽


class TestBestEffortFinalization:
    async def test_best_attempt_selected_on_exhaustion(self):
        """寿命耗尽：predicted_json == 历史最优（匹配字段数最多者）。"""
        worst = {"a": "9", "b": "9", "c": "9"}      # 0 字段匹配
        best_bad = {"a": "1", "b": "x", "c": "9"}   # 2 字段匹配
        mid = {"a": "1", "b": "9", "c": "9"}        # 1 字段匹配
        p = make_processor([ok(mid), ok(best_bad), ok(worst)],
                           max_sample_attempts=3)
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "failed"
        assert r["predicted_json"] == best_bad
        assert r["match_level"] == "MISMATCH"

    async def test_normalized_match_is_accepted(self):
        """NORMALIZED_MATCH 验收通过（格式噪声不否决推理正确的样本）。"""
        normalized = {"a": "１", "b": "x", "c": "y"}   # 全角 1 → 归一化一致
        p = make_processor([ok(normalized)])
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "success"
        assert r["match_level"] == "NORMALIZED_MATCH"


class TestResultCompat:
    async def test_success_result_fields(self):
        p = make_processor([ok(GT)])
        r = await p.process(make_sample(), "s1")
        for key in ("sample_id", "status", "attempts", "original_sample",
                    "cot_response", "full_api_response", "predicted_json",
                    "ground_truth", "comparison_result", "robust_match"):
            assert key in r, f"missing field: {key}"
        assert r["comparison_result"]["is_match"] is True
        # §7.3：验收判定与诊断分析同源
        assert r["robust_match"] is r["comparison_result"]

    async def test_failed_result_fields(self):
        p = make_processor([err(ErrorType.API_ERROR)])
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "failed"
        assert "error" in r and "error_type" in r

    async def test_mismatch_exhausted_result_has_ground_truth(self):
        """MISMATCH 耗尽（best 分支收尾）记录必须带 ground_truth——
        2026-08-04 修复前 best 分支漏传，卡住 convert 硬样本流程。"""
        bad = {"a": "9", "b": "9", "c": "9"}
        p = make_processor([ok(bad)] * 3, max_sample_attempts=3)
        r = await p.process(make_sample(), "s1")
        assert r["status"] == "failed"
        assert r["error_type"] == "MISMATCH"
        assert r["ground_truth"] == GT

    async def test_invalid_gt(self):
        p = make_processor([])
        r = await p.process({"id": "s1", "messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "not json at all"}]}, "s1")
        assert r["error_type"] == "JSON_PARSE_ERROR"

    async def test_conversations_format(self):
        p = make_processor([ok(GT)])
        r = await p.process(make_sample("conversations"), "s1")
        assert r["status"] == "success"

    async def test_invalid_json_response(self):
        p = make_processor([CallOutcome(
            ok=True, response={}, content="模型没输出 JSON")])
        r = await p.process(make_sample(), "s1")
        assert r["error_type"] == "JSON_PARSE_ERROR"
        assert r["cot_response"] == "模型没输出 JSON"
