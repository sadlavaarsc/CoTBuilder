# CoTBuilder 闭环流程说明

> 创建于 2026-08-03 ｜ 用途：从切好的样本到训练数据的最短闭环，含推荐参数与验收检查点。
> **上游数据切分不在本项目范围**——本文假设输入已是切好的样本 JSON（格式见 formats.md §1）。

## 流程总览

```
samples.json ─► ① cli 生成 ─► run/ ─► ② judge 改判 ─► judge/ ─► ③ merge ─► merged/
                                                                              │
多批次：各批 merged/ ─► combine ─► combined/ ◄────────────────────────────────┘
                                              │
                                              ▼
                                     ④ convert ─► train.json（训练框架）
```

① ② 触网（专家模型 API）；③ ④ 与 combine 纯离线，随便重跑。

## ① 生成（python -m cotbuilder.cli）

```bash
# 先试跑 30 条验证连通与参数（断点恢复，重跑自动续）
python -m cotbuilder.cli --input samples.json --output run_trial/ \
    --api-key <key> --num-samples 30 --qpm-limit 40 --max-concurrent 25

# 全量
python -m cotbuilder.cli --input samples.json --output run1/ \
    --api-key <key> --qpm-limit 40 --max-concurrent 25
```

推荐参数（默认值即生产口径，只需显式改这两个）：

| 参数 | 推荐值 | 理由（详见） |
|---|---|---|
| `--qpm-limit` | **40**（标称 50 的 80%） | 骑线 50 会周期性触发 403 自我维持振荡（design.md §5.18） |
| `--max-concurrent` | **25** | rtt 2–70s 分布下撑满 QPM 的在途需求（Little's Law，实测 in_flight 峰值 26） |
| 采样参数 | 默认（0.6/0.95/top_k=20/pp=0，thinking 开） | 官方思考·精确档，修复死循环的关键（investigation-01） |
| `--request-timeout` / `--connect-timeout` | 默认 120 / 30 | 合法响应 4–31s，>120s 几乎必死，早掐释放并发槽（§5.17） |

**验收检查点**（run 目录 summary.json）：
- `metrics.outcomes.RATE_LIMITED / total_http_requests` < 10% → 正常；持续 >15% → qpm 降到 35；
- `metrics.amplification` ≈ 1 + 重试率（无异常放大）；`success_rate` 与 `quality.match_score_mean` 记录留档。

## ② judge 改判（python -m cotbuilder.judge）

```bash
# 先试判 20 条看改判质量（抽查 judge_result.verdicts 的理由）
python -m cotbuilder.judge --input run1/ --output judge_trial/ \
    --api-key <key> --limit 20 --qpm-limit 40

# 全量（checkpoint 断点续判，中断重跑即可）
python -m cotbuilder.judge --input run1/ --output judge1/ \
    --api-key <key> --qpm-limit 40
```

**验收检查点**（judge_summary.json）：`overturn_rate` 留档（GT 标注质量指标，30 样本实测量级 10–30%）；`judge_parse_failed` 高 → 检查模型输出格式；`skipped_no_differences` 为纯 infra 失败样本数（这些只能靠重跑主流程捞，judge 原则性救不了）。

## ③ merge 并回（python -m cotbuilder.merge）

```bash
python -m cotbuilder.merge --run run1/ --judge judge1/ --output merged1/
```

**验收检查点**（merge_summary.json）：`final_success = run_success + judged_overturned` 恒成立；`orphaned` / `collision` 应为 0。

反复 judge：merged1/failed_samples.json 可直接再喂 ②，再 merge 叠加。

## ④ convert 出训练集（python -m cotbuilder.convert）

```bash
python -m cotbuilder.convert --input merged1/ --output train.json \
    --include-failed upheld --mix-ratio 1.0 \
    --mix <老数据shareGPT.json>   # upheld 不足预算时自动补齐，可省
```

推荐口径（讨论结论，design.md §6e）：
- **无 CoT 预算 1:1**（ratio=1.0，条数 1:1 时 CoT token 实际占 70–90%，信号足够强）；
- **failed 派生（upheld 硬样本）优先填充**，外部老数据只补缺口；
- gpt 轮 `<think>/<answer>` 双标签 + human 末尾 `/think` `/no_think` 软开关均为默认，不用管；
- 容器默认 JSON 数组（对齐 13 万老数据微调输入）。

**验收检查点**（convert_summary.json）：`converted + failed_used + mixed_in = total_samples`；`failed_used=0` 且跑过 judge → 检查 merged 目录是否真有 upheld 记录。抽查 train.json：`<think>` 覆盖率（= 服务端 reasoning_content 返回率的代理指标）、`/think` 与 `/no_think` 条数比例 ≈ 1:1。

## 多批次：combine 汇总（可选）

```bash
python -m cotbuilder.combine --inputs merged1/ merged2/ merged3/ \
    --output combined/
python -m cotbuilder.convert --input combined/ --output train.json \
    --include-failed upheld --mix-ratio 1.0 --mix <老数据.json>
```

## 已知缺口（手动处理）

- **infra 失败样本重跑**：`skipped_no_differences` 对应的样本（网络/限流/网关耗尽）
  是最值得重跑的一批（没上过考场而非考不过），但目前没有 requeue 工具——
  手工从这些记录的 sample_id 筛出原 input 子集另存文件再跑 ① 即可；
- **数据切分**：上游职责，本项目输入一律视为已切好。

*关联文档：[formats.md](formats.md)（各阶段 JSON 格式）、[design.md](design.md)（设计决策与防误解清单 §5）、[investigation-01](investigation-01-e2e-diagnosis.md)（实测诊断与参数由来）*
