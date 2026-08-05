# CoTBuilder 闭环流程说明

> 创建于 2026-08-03 ｜ 用途：从切好的样本到训练数据的最短闭环，含推荐参数与验收检查点。
> **上游数据切分不在本项目范围**——本文假设输入已是切好的样本 JSON（格式见 formats.md §1）。
> 更新 2026-08-05：补「三分钟版」最短路径、概念速览、golden answer 获取、常见状况（面向操作者）。

## 三分钟版（最短路径）

只有两步是必做的，其余都是可选增强：

```bash
cd /Users/liwentao/Documents/开发/CoTBuilder     # 所有命令都在项目根目录跑

# 必做①：生成（专家模型提取 + 自动比对筛选，中断直接重跑同命令即续跑）
python -m cotbuilder.cli --input samples.json --output run1/ \
    --api-key <key> --qpm-limit 40 --max-concurrent 25

# 必做②：转成训练格式（交付物 train.json）
python -m cotbuilder.convert --input run1/ --output train.json
```

可选增强（按需要做，都在 ① 之后、② 之前）：**judge**（模型改判捞回规则
冤案，多救 10–30% 样本）→ **merge**（改判结果并回）；**polish**（润色
推理链，去掉反复纠结）。下面分节详述。

## 概念速览（每个文件是什么）

| 文件/目录 | 是什么 |
|---|---|
| `samples.json` | 输入：图片路径 + 提问 + 标准答案（ground truth）的 JSON 数组 |
| `run1/success_samples.json` | **通过验收的样本 = golden answer（模型答案与标准答案一致）+ 推理链**，这是 CoT 生产的核心产出 |
| `run1/failed_samples.json` | 没通过验收的样本（含比对明细，可供 judge 改判/repair 修复） |
| `run1/summary.json` | 本次运行的统计（成功率、限流情况、token 用量） |
| `train.json` | **交付物**：ShareGPT 格式训练数据，训练框架直接读 |

判定逻辑：① 中每条样本自动与标准答案做规则比对，一致才进 success——
所以**生产 CoT 的过程本身就顺带筛出了 golden answer**，无需额外步骤。

## 流程总览

```
samples.json ─► ① cli 生成 ─► run/ ─► ② judge 改判 ─► judge/ ─► ③ merge ─► merged/
                                                                              │
多批次：各批 merged/ ─► combine ─► combined/ ◄────────────────────────────────┤
                                              │                               │
                                              ▼                               │
                                     ④ convert ─► train.json（训练框架）      │
                                              ▲                               │
                                     ⑤ polish/repair（可选，convert 前） ◄────┘
```

① ② ⑤ 触网（专家模型 API）；③ ④ 与 combine 纯离线，随便重跑。

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

**只要答案不要推理链（golden answer 提取）**：success 样本的答案本身就是
筛过的 golden answer，加 `--gpt-mode json` 即得纯 `<answer>` 版训练数据
（无 `<think>` 块，human 自动挂 `/no_think`）：

```bash
python -m cotbuilder.convert --input run1/ --output train_answer_only.json \
    --gpt-mode json
```

## ⑤ polish / repair 润色修复（可选，convert 前）

```bash
# polish：润色成功样本 CoT（试跑 20 条先抽查 polished_cot）
python -m cotbuilder.polish --mode polish --input merged1/ \
    --output polished1/ --api-key <key> --limit 20 --qpm-limit 40

# 失败/弃用样本重试循环（answer_changed 一次定音，重试走新 output）
python -m cotbuilder.polish --input polished1/failed_samples.json \
    --output polished1_retry/ --api-key <key> --qpm-limit 40
python -m cotbuilder.combine --inputs polished1/ polished1_retry/ \
    --output polished1_final/

# repair：按 GT 修 failed 样本 CoT（不做验证，产物靠抽查兜底）
python -m cotbuilder.polish --mode repair --input merged1/ \
    --output repaired1/ --api-key <key> --qpm-limit 40
```

convert 自动衔接：polish 输出目录直接作 convert `--input`，CoT 与答案
自动优先 polished 版本（`polish_result.applied` 才生效），无需 merge。

**验收检查点**（polish_summary.json）：`applied_rate` 留档（过低 →
prompt 或模型问题，抽查 polished_cot）；`answer_changed` 占比过高 →
模型润色时改动事实，考虑调低 temperature；抽查若干 applied 样本的
polished_cot 对比原 cot_response（纠结收敛、事实不变）。

## 多批次：combine 汇总（可选）

```bash
python -m cotbuilder.combine --inputs merged1/ merged2/ merged3/ \
    --output combined/
python -m cotbuilder.convert --input combined/ --output train.json \
    --include-failed upheld --mix-ratio 1.0 --mix <老数据.json>
```

combine 按 **(输入路径, sample_id)** 分键去重（2026-08-04 起）——各批
sample_id 撞车不再丢数据（跨路径同 id 全部保留，撞车数见
combine_summary 的 `cross_path_id_collisions`）；仍建议上游切分时让
id 全局唯一（如加批次前缀），下游 judge 按裸 id 判重，重复 id 会被跳过。

## 常见状况

| 状况 | 怎么办 |
|---|---|
| 跑到一半中断（Ctrl-C / 断网 / 关电脑） | **直接重跑同一条命令**——checkpoint 断点续跑，已完成的样本自动跳过 |
| 日志里 403 / RATE_LIMITED 很多 | 偶发正常（自动退避重试）；summary.json 里占比 >15% 就把 `--qpm-limit` 降到 35 重跑 |
| 大概要跑多久 | qpm=40 ≈ 每小时最多 2400 次请求；含重试放大后，8000 样本约 4–6 小时 |
| 想先小规模试试 | ① 加 `--num-samples 30`；② ⑤ 加 `--limit 20` |
| 不知道结果好不好 | 看 run 目录 summary.json 的 `success_rate`（通过率）与 `quality.match_score_mean`（平均答案质量），再抽查几条 success_samples.json 的记录 |

## 已知缺口（手动处理）

- **infra 失败样本重跑**：`skipped_no_differences` 对应的样本（网络/限流/网关耗尽）
  是最值得重跑的一批（没上过考场而非考不过），但目前没有 requeue 工具——
  手工从这些记录的 sample_id 筛出原 input 子集另存文件再跑 ① 即可；
- **数据切分**：上游职责，本项目输入一律视为已切好。

*关联文档：[formats.md](formats.md)（各阶段 JSON 格式）、[design.md](design.md)（设计决策与防误解清单 §5）、[investigation-01](investigation-01-e2e-diagnosis.md)（实测诊断与参数由来）*
