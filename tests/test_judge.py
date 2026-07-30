"""judge 后处理工具测试（mock server，缩小时间尺度）。

验收口径（plan：可选 model judge 改判工具）：
- 只判 comparison_result.differences 中的失败 KV pair（无 diff 跳过）
- 保守改判：全部 pair 判 match=true 才改判；缺 verdict / 判 false 维持失败
- 仅网络类错误退避重试（network_max_attempts 寿命），终态错误不重试
- 独立输出目录 + checkpoint 断点续判；请求数守恒；quota 入 judge 桶
"""

import json

import pytest

from cotbuilder.config import Config
from cotbuilder.judge import JudgeRunner
from mock.mock_server import MockExpertServer, MockScenario

FAST_BACKOFF = {"backoff_base": 0.02, "backoff_cap": 0.1}


def failed_record(sid, pairs):
    """构造一条规则判失败的记录。pairs: [(field, predicted, ground_truth)]。"""
    return {
        "sample_id": sid,
        "status": "failed",
        "attempts": 3,
        "original_sample": {"id": sid, "messages": [], "images": []},
        "cot_response": f"推理链（{sid}）",
        "predicted_json": {f: p for f, p, _ in pairs},
        "ground_truth": {f: g for f, _, g in pairs},
        "comparison_result": {
            "is_match": False,
            "differences": [
                {"field": f, "type": "mismatch",
                 "ground_truth": g, "predicted": p}
                for f, p, g in pairs
            ],
        },
    }


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return []


async def run_judge(scenario, records, out_dir, **cfg_overrides):
    srv = MockExpertServer(scenario)
    base = await srv.start()
    cfg = Config(api_key="k", api_endpoint=base, **FAST_BACKOFF,
                 **cfg_overrides)
    runner = JudgeRunner(cfg)
    summary = await runner.run(records, str(out_dir))
    await srv.close()
    return srv, runner, summary


PAIR_WS = [("发票号码", "J-123", "J123")]                    # 纯符号差异
PAIR_TWO = [("发票号码", "J-123", "J123"),
            ("购买方名称", "LAU,LAI LI", "LAU, LAI LI")]   # 两字段失败


class TestJudgeE2E:
    async def test_overturn_moves_to_success(self, tmp_path):
        """改判成功：记录进 success_samples.json，judge_result 完整、
        original_sample/cot_response 保留（可直接作训练数据）。"""
        records = [failed_record(f"j{i}", PAIR_WS) for i in range(3)]
        srv, runner, summary = await run_judge(
            MockScenario(latency=(0.0, 0.0), seed=1, judge_mode=True),
            records, tmp_path, qpm_limit=6000)

        assert summary["overturned"] == 3
        assert summary["judged"] == 3
        assert summary["overturn_rate"] == 1.0

        success = read_json(tmp_path / "success_samples.json")
        assert len(success) == 3
        for rec in success:
            assert rec["status"] == "success"
            jr = rec["judge_result"]
            assert jr["overturned"] is True
            assert jr["attempts"] == 1
            assert [v["match"] for v in jr["verdicts"]] == [True]
            assert jr["pairs"] == [
                {"field": "发票号码", "predicted": "J-123",
                 "ground_truth": "J123"}]
            # 训练数据所需字段原样保留
            assert rec["original_sample"]["id"]
            assert rec["cot_response"]
            assert rec["predicted_json"]
        assert read_json(tmp_path / "failed_samples.json") == []
        # quota 全部入 judge 桶（与主流程 initial/retry_* 分账不混）
        assert runner._client.stats.quota == {
            "initial": 0, "retry_quality": 0, "retry_network": 0, "judge": 3}

    async def test_upheld_stays_failed(self, tmp_path):
        """维持原判：任一 verdict 判 false → 记录进 failed_samples.json。"""
        records = [failed_record("u1", PAIR_TWO)]
        srv, runner, summary = await run_judge(
            MockScenario(latency=(0.0, 0.0), seed=1, match_probability=0.0,
                         judge_mode=True),
            records, tmp_path, qpm_limit=6000)

        assert summary["upheld"] == 1
        failed = read_json(tmp_path / "failed_samples.json")
        assert len(failed) == 1
        assert failed[0]["status"] == "failed"
        assert failed[0]["judge_result"]["overturned"] is False

    async def test_skip_records_without_differences(self, tmp_path):
        """无 differences 的记录（纯网络失败等）跳过，不发请求。"""
        no_diff = {"sample_id": "n1", "status": "failed",
                   "error_type": "NETWORK_ERROR"}
        empty_diff = failed_record("n2", PAIR_WS)
        empty_diff["comparison_result"]["differences"] = []
        srv, runner, summary = await run_judge(
            MockScenario(latency=(0.0, 0.0), seed=1, judge_mode=True),
            [no_diff, empty_diff, failed_record("ok1", PAIR_WS)],
            tmp_path, qpm_limit=6000)

        assert summary["skipped_no_differences"] == 2
        assert summary["judged"] == 1
        assert len(srv.stats.records) == 1  # 只发出 1 次请求

    async def test_storm_retry_then_success(self, tmp_path):
        """403 风暴：退避重试后成功，attempts 如实，quota 计入 judge 桶。"""
        records = [failed_record("s1", PAIR_WS)]
        srv, runner, summary = await run_judge(
            MockScenario(latency=(0.0, 0.0), seed=1, judge_mode=True,
                         storm_duration=0.05),
            records, tmp_path, qpm_limit=600)   # Δ=0.1s，重试落在风暴后

        assert summary["overturned"] == 1
        stats = runner._client.stats
        assert stats.outcomes.get("RATE_LIMITED", 0) >= 1
        rec = read_json(tmp_path / "success_samples.json")[0]
        assert rec["judge_result"]["attempts"] >= 2
        # 请求数守恒：服务端到达数 == 客户端请求数 == quota judge 桶
        assert len(srv.stats.records) == stats.total_requests
        assert stats.quota["judge"] == stats.total_requests

    async def test_network_exhausted_keeps_failed(self, tmp_path):
        """网络寿命耗尽：维持失败不丢数据，failure=network_exhausted。"""
        records = [failed_record("e1", PAIR_WS)]
        srv, runner, summary = await run_judge(
            MockScenario(latency=(0.0, 0.0), seed=1, judge_mode=True,
                         network_error_rate=1.0),
            records, tmp_path, qpm_limit=6000, network_max_attempts=2)

        assert summary["network_exhausted"] == 1
        assert len(srv.stats.records) == 2  # 恰好烧完 2 次寿命
        rec = read_json(tmp_path / "failed_samples.json")[0]
        assert rec["status"] == "failed"
        assert rec["judge_result"]["failure"] == "network_exhausted"
        assert rec["judge_result"]["attempts"] == 2

    async def test_terminal_error_no_retry(self, tmp_path):
        """终态错误（500 API_ERROR）不重试，attempts=1。"""
        records = [failed_record("t1", PAIR_WS)]
        srv, runner, summary = await run_judge(
            MockScenario(latency=(0.0, 0.0), seed=1, judge_mode=True,
                         server_error_rate=1.0),
            records, tmp_path, qpm_limit=6000)

        assert summary["terminal_error"] == 1
        assert len(srv.stats.records) == 1
        rec = read_json(tmp_path / "failed_samples.json")[0]
        assert rec["judge_result"]["failure"] == "terminal_error"

    async def test_unparseable_verdict_keeps_failed(self, tmp_path):
        """verdict 解析失败：维持失败，failure=judge_parse_failed，不重试。"""
        records = [failed_record("p1", PAIR_WS)]
        srv, runner, summary = await run_judge(
            MockScenario(latency=(0.0, 0.0), seed=1, judge_mode=True,
                         invalid_json_rate=1.0),
            records, tmp_path, qpm_limit=6000)

        assert summary["judge_parse_failed"] == 1
        assert len(srv.stats.records) == 1
        rec = read_json(tmp_path / "failed_samples.json")[0]
        assert rec["judge_result"]["failure"] == "judge_parse_failed"

    async def test_resume_skips_judged(self, tmp_path):
        """断点续判：第二轮跳过已判记录，文件无重复 id。"""
        records = [failed_record(f"r{i}", PAIR_WS) for i in range(4)]
        scenario = lambda: MockScenario(latency=(0.0, 0.0), seed=1,
                                        judge_mode=True)

        _, _, s1 = await run_judge(scenario(), records[:2], tmp_path,
                                   qpm_limit=6000)
        assert s1["overturned"] == 2

        _, _, s2 = await run_judge(scenario(), records, tmp_path,
                                   qpm_limit=6000)
        assert s2["skipped_resume"] == 2
        assert s2["judged"] == 2

        final = read_json(tmp_path / "success_samples.json")
        ids = [r["sample_id"] for r in final]
        assert len(ids) == len(set(ids)) == 4

        # summary 计数与文件记录数对账一致
        summary_file = json.loads(
            (tmp_path / "judge_summary.json").read_text())
        assert summary_file["overturned"] + summary_file["skipped_resume"] == 4


class TestApplyVerdicts:
    """保守改判规则的纯函数测试（不经 mock）。"""

    pairs = [
        {"field": "发票号码", "predicted": "J-123", "ground_truth": "J123"},
        {"field": "购买方名称", "predicted": "LAU,LAI LI",
         "ground_truth": "LAU, LAI LI"},
    ]

    def test_all_true_overturns(self):
        content = json.dumps({"verdicts": [
            {"field": "发票号码", "match": True, "reason": "连字符无实义"},
            {"field": "购买方名称", "match": True, "reason": "空格无实义"},
        ]}, ensure_ascii=False)
        applied = JudgeRunner.apply_verdicts(self.pairs, content)
        assert applied["overturned"] is True
        assert len(applied["verdicts"]) == 2

    def test_any_false_upholds(self):
        content = json.dumps({"verdicts": [
            {"field": "发票号码", "match": True, "reason": "ok"},
            {"field": "购买方名称", "match": False, "reason": "少了字"},
        ]}, ensure_ascii=False)
        assert JudgeRunner.apply_verdicts(self.pairs, content)["overturned"] is False

    def test_missing_verdict_upholds(self):
        """模型漏判一个字段 → 保守维持失败（防漏判误判成功）。"""
        content = json.dumps({"verdicts": [
            {"field": "发票号码", "match": True, "reason": "ok"},
        ]}, ensure_ascii=False)
        assert JudgeRunner.apply_verdicts(self.pairs, content)["overturned"] is False

    def test_unparseable_returns_none(self):
        assert JudgeRunner.apply_verdicts(self.pairs, "这不是 JSON") is None
        assert JudgeRunner.apply_verdicts(self.pairs, None) is None
        # 缺 verdicts 键
        assert JudgeRunner.apply_verdicts(self.pairs, '{"match": true}') is None


class TestJudgePairs:
    def test_extract_from_differences(self):
        rec = failed_record("x", PAIR_TWO)
        pairs = JudgeRunner.judge_pairs(rec)
        assert pairs == [
            {"field": "发票号码", "predicted": "J-123",
             "ground_truth": "J123"},
            {"field": "购买方名称", "predicted": "LAU,LAI LI",
             "ground_truth": "LAU, LAI LI"},
        ]

    def test_no_differences_returns_none(self):
        assert JudgeRunner.judge_pairs({"sample_id": "x"}) is None
        assert JudgeRunner.judge_pairs(
            {"sample_id": "x", "comparison_result": {}}) is None
        assert JudgeRunner.judge_pairs(
            {"sample_id": "x",
             "comparison_result": {"differences": []}}) is None
