# CoTBuilder — CLAUDE.md

> 创建于 2026-07-28 ｜ 用途：CoT 数据生产管线（用于模型后训练）
> 更新于 2026-07-29（二）｜ 30 样本 e2e 诊断：官方采样档默认 + LENGTH_TRUNCATED 分类 + usage 统计 + mock 双峰重校准

## 项目目标

用多模态专家模型（Qwen-VL 类）对文档图片做关键信息提取，生成**带推理链的 CoT 数据**，与 ground truth 比对验证后筛出匹配样本，作为后训练语料。

核心流程：

```
样本(图片+prompt+ground truth)
  → 图片 base64 编码，构造多模态请求
  → 调用专家模型（QPM 限流 + 网络错误指数退避重试）
  → 从响应中提取 JSON 结果
  → 与 ground truth 比对
  → MISMATCH 时并发重试（抽卡），取匹配度最高者
  → 实时写入成功/失败文件 + checkpoint 断点恢复
```

## 当前状态

- `oldCode/CoTBuilder-V2.py`（857 行）— 从公司聊天软件 copy 的老版本，**仅作参考，不要修改**。由公司小模型生成，代码臃肿、功能不正常，且可能缺模块/有格式问题。
- **重构已完成（2026-07-28）**：新代码位于 `cotbuilder/` 包（config / ratelimit / client / extractor / matcher / generator / writer / metrics / batch / cli），mock 与测试位于 `mock/`、`tests/`。设计决策与防误解清单见 `doc/design.md`。
- **并发模型变更（用户确认）**：老代码的「MISMATCH 3 并发抽卡」已替换为**寿命模型**——样本内串行（同一样本在途请求恒 ≤ 1）、跨样本并发；`max_sample_attempts`（默认 3，MISMATCH 消耗）与 `network_max_attempts`（默认 5，网络/403 消耗）两本寿命账独立；MISMATCH 立即重排，网络错误退避（指数+jitter）到点才重排。
- 测试基线：`python -m pytest tests/ -v`（全部 mock，不触真实 API，253 项指标测试 + 2 项 slow 冒烟，实测结果见 `doc/test-results.md`）。环境：`.venv`（uv 创建，依赖 aiohttp / pytest / pytest-asyncio，pip 源用清华镜像）。
- **超时拆分 + 性能追踪（2026-07-29）**：实测发现 `total=120` 超时把思考型模型慢推理掐成大量 NETWORK_ERROR——`request_timeout` 默认改 600s、新增 `connect_timeout`（默认 15s）让真网络故障快速失败；新增 `metrics.py` 四段耗时追踪（限流排队/槽位排队/HTTP 飞行/退避）+ 有效 QPM 曲线 + `output/metrics.jsonl` + 控制台进度行（设计见 `doc/design.md` §6b）；mock 新增超长尾延迟档（`slow_response_rate` / `slow_latency`）。
- **30 样本 e2e 实测与修复（2026-07-29）**：实测报告 `doc/e2e_test_report.md`、调研结论 `doc/investigation-01-e2e-diagnosis.md`（追加记录持续补充）。根因：thinking 耗尽 32768 输出硬上限 → 43% `content=null`；`temperature=0.1` 偏离官方档是死循环疑似诱因。已落地：生成参数全部入 Config/CLI/summary（**默认 = 官方思考·精确档 0.6/0.95/top_k=20/pp=0**，`max_tokens=32768` 为服务端硬上限）、`LENGTH_TRUNCATED` 错误分类（finish_reason=length，不重试）、usage token 统计（summary.metrics.token_usage）、mock 双峰重校准（典型档 (4,30)s，empty/length_truncated 确定性绑定 slow_latency (230,320)s）。下一步：官方参数 A/B 复测 30 样本（观测 EMPTY 率 / STRICT 率 / 重试转化率）。
- **GATEWAY_ERROR + 超时收紧 + quality 块（2026-07-29）**：502/503/504 独立分类 `GATEWAY_ERROR`（网络账退避重试，单样本 ≤ `gateway_max_attempts`=2——实测 504 rtt 恒定 ≈360s，为网关超时墙截断的确定性长尾，满额重试是纯浪费，design.md §5.16）；`request_timeout` 默认 600→**400s**（网关墙 360s + 余量，低于 360 会错杀合法长推理，§5.17）；summary 新增 `quality` 块（match_score 均值 + 完成序每 10 个滑窗 + scored/unscored，看平均 KV 质量而不只是完美样本）。
- **降级场景已测（2026-07-28，用户追加）**：网络断连/概率 403/服务端延迟抖动导致有效 QPM 略低于标称值时，系统不失态、不补发突发、错误正确分类（`tests/test_degraded.py`，仅验证表现，未实现补偿机制）。
- **生产参数调整（2026-07-30）**：prompt 调优 v2.3→**v2.4**（缺失字段改 null 填充、去 5 步推理指令，见 `generator.SYSTEM_PROMPT_V2_4` 与 investigation-01 追加五）；超时改生产实测推荐值 `request_timeout` 400→**120s**（修复死循环后合法响应 4–31s，>120s 几乎必死，早掐释放并发槽；代价是慢失败归 NETWORK_ERROR 烧网络账，design.md §5.17 变迁史）、`connect_timeout` 15→**30s**。另 403 风暴已基本定性为**骑线振荡**：服务端窗口配额=50 且被拒请求疑似也计数，骑线 50/min 时拒绝自我维持、靠退避排空，单次运行 ≥2 次风暴；客户端 pacing 无过错。生产对策 qpm_limit 40–45 + max_concurrent ≥20–25（investigation-01 追加六、design.md §5.18；原始日志归档 `doc/logs/metrics-2026-07-29-e2e.jsonl`）。
- **model judge 改判工具（2026-07-30）**：`cotbuilder/judge.py`（`python -m cotbuilder.judge`）——可选后处理，**不进主流程**：读 run 输出目录的 failed_samples.json，只把 `comparison_result.differences` 中的失败 KV pair 发给同一模型纯文本改判（判定规则=定义什么是错，design.md §6c）；**保守改判**（全部 pair 判 match=true 才翻 success，缺 verdict 按未改判）；仅网络类错误退避重试；独立输出目录（checkpoint 续判）+ judge_summary.json。规则口径保持 STRICT 不变（§5.19）。测试 217（+14）。
- **merge + convert 离线工具（2026-08-03）**：`cotbuilder/merge.py`（`python -m cotbuilder.merge`）把 judge 结果并回原 run——翻转+搬移+标签（`judge_result` 键存在=被判过，原字段不动），支持反复 judge 循环；`cotbuilder/convert.py`（`python -m cotbuilder.convert`）转 ShareGPT 训练数据——gpt 轮 = Qwen3 式 **`<think>`/`<answer>` 双标签**（全来源答案统一 `<answer>` 包裹，vLLM 一条规则切分；推理链回收 reasoning_content → cot_response 剥离，design.md §5.21），可切 raw（原文，不参与双标签契约）/json；容器默认 JSON 数组（对齐 13 万条老数据微调输入）可切 jsonl；human 末尾按实际是否含推理自动加 `/think` `/no_think` 软开关标志；`--strip-fences` 删 raw 模式 ```json 围栏；无 CoT 总预算 = `--mix-ratio` × CoT 条数，**`--include-failed`（upheld/mismatch/all 档）failed 派生 /no_think+GT 硬样本优先填充、`--mix` 外部 ShareGPT 补齐缺口**（failed 的 cot/predicted 永不作训练目标）。`cotbuilder/combine.py`（`python -m cotbuilder.combine`）多路径哑合并——任意 run/judge 目录或单文件按 sample_id 去重（后赢）拼成标准两文件目录，可直接喂 convert/judge（与 merge 的语义合并分工见 design.md §6f）。三者纯离线只读（§5.20）。**全部 JSON 格式总览见 `doc/formats.md`**。测试 253（+36）。
- **原版 comparator 已对齐（2026-07-28）**：拿到缺失的 `oldCode/RobustJSONComparator.py`（只读）。原版宽松规则实现为 `MatcherConfig` 开关、默认关闭（默认 = audit-02 规格）；`Matcher.legacy()` / CLI `--legacy-matcher` 对齐原版行为；`comparison_result` schema 为原版超集。逐项对照与原版 4 个已修正 bug 见 `doc/comparator-compat.md`（改 matcher 前必读）。测试总数 173（171 + 2 slow）。

## 重构目标（已达成）

1. **更健壮**：老代码缺陷已修复（见下方问题清单的处置），全部指标有 mock 测试断言
2. **更简洁**：按职责拆分 9 个模块，单模块单职责，结果字典单点构造
3. **可测试**：用 **mock API 接口**完成指标测试（成功率、限流、重试、断点恢复等），不依赖真实模型服务

## 重构需求（正式版）

### R1. 【P0】重构并发与限流子系统

**背景**：老代码线上效果差的直接原因，是并发控制、QPM 限流与重试逻辑三者相互耦合，实际运行行为偏离设计预期。本模块是老代码中最复杂的部分，也是重构的首要目标。

**问题定位**：
- `qpm_semaphore = Semaphore(max_concurrent)` 一个信号量同时承担「并发上限」与「QPM 限流」两个职责；任务在 `_rate_limit()` 内**持有并发槽位等待 QPM 窗口滑动**，等待期间其他任务无法获取槽位，吞吐被无谓拉低
- 网络重试（每请求最多 5 次指数退避）与 MISMATCH 3 并发抽卡均会反复进入 `_rate_limit()`，单样本实际请求数由设计的 1 次膨胀至最多 1+3×5=16 次，QPM 统计与实际发出的请求完全脱钩
- 限流、重试、抽卡三条控制流互相嵌套，无法独立验证任一指标

**重构要求**：从最初设计意图（**严格控制 QPM 不超过上限、同时打满允许的并发度**）出发重新设计：
- 并发控制、QPM 限流、重试退避三者**职责分离**，各自可独立测试
- 任意时刻实际发出的请求速率不得超过 QPM 上限（含重试与抽卡产生的请求）
- 单样本的请求放大倍数应有明确上界并纳入统计口径

### R2. 代码结构重构（去冗余、模块化）

- 老代码 857 行单文件、抽象冗余、重复构造，对公司内部小上下文模型极不友好
- 按职责拆分模块（配置 / 比对 / 客户端 / 生成 / 写入 / 入口），每个模块单一职责、体量克制
- 消除重复代码与冗余抽象（如 result 字典 4 次重复构造、`RobustMatcher` 与 comparator 功能重叠）

### R3. 重构约束

- **数据层面与上下游完全兼容**：输入样本格式（`messages` / `conversations` 两种）、输出文件格式（`success_samples.json` / `failed_samples.json` / `checkpoint.json`）、结果字段结构均不得改变
- **核心管线不修改**：整体流程（编码 → 请求 → 提取 → 比对 → 抽卡 → 落盘）保持原有语义
- **具体实现从简**：以实现模型（公司内部小模型）的能力为基准，不追求精巧设计，满足设计指标即可
- **核心设计指标**：QPM 上限严格可控、并发度达标、比对校验正确、鲁棒性校验（网络重试 / 断点恢复 / 异常路径）正确

### R4. Mock API 建模约束（指标测试基准）

本代码来自实际线上系统，开发与测试期间无法访问真实模型服务，所有指标测试必须基于 mock API，建模约定如下：

- **单次请求延迟**：随机 30s–90s（接近真实多模态大模型推理延迟）
- **返回内容**：符合 API 响应格式即可（`choices[0].message.content` 含可解析 JSON），具体取值不重要
- mock 必须可配置：能模拟正常响应、网络错误、限流（403 QPM）、空响应、非法 JSON 等场景，以覆盖全部鲁棒性路径

**关键推论（测试预期的标尺）**：按 Little's Law 估算，平均延迟 60s 时要打满 QPM 50 需约 50 个在途请求；`max_concurrent=10` 时吞吐上限约 10 QPM —— **并发度是真实瓶颈，QPM 限流在常规配置下不应被触发**。若测试中 QPM 限流频繁先于并发打满而触发，或实际并发长期低于上限，即说明并发/限流设计仍有耦合问题。

### R5. 文档与可维护性要求（为后续小模型开发打基础）

本项目的核心并发/限流模块是后续所有开发的地基，后续维护者以公司内部小上下文模型为主，因此：

- **模块解耦清晰**：每个模块单一职责、体量克制，模块间依赖方向明确，可独立阅读理解
- **注释充分**：公共接口、类、关键算法（限流窗口、退避策略、抽卡选取）必须有 docstring 说明「做什么、为什么这么做、与其他模块的边界」
- **文档详细**：除 CLAUDE.md 外，需有面向后续开发者的设计文档，说明整体架构、各模块职责、并发/限流的设计决策与指标含义、如何跑 mock 测试
- **防误解**：容易踩坑的设计决策（如为什么限流不持有并发槽位）要在文档中显式说明，防止后续修改重新引入耦合

## 老代码已知问题清单

- 缺失 import：`os / json / time / asyncio / aiohttp / argparse`，无法直接运行
- 依赖仓库外模块 `evaluation.robust_json_comparator`（JSON 解析 + 比对逻辑需内置）
- `RobustFileWriter.save_result` 每个样本**全量读写整个 JSON 文件**，O(n²) IO
- `generate_cot_for_sample` 中 result 字典构造重复 4 次，函数过长
- `max_retries` 参数声明后从未使用（MISMATCH 实为固定 3 并发）
- 每次 API 调用新建 `aiohttp.ClientSession`，无连接复用
- `RobustMatcher` 与 comparator 比对功能重叠，属冗余分析
- `success_rate` 统计口径错误（分母包含 skipped 样本）
- `"attempts": 1` 硬编码，重试次数统计失真

## 开发约定

- 语言：Python 3，异步 IO（aiohttp / asyncio）
- 老代码文件只读，新代码另起结构
- 测试一律走 mock，不调用真实 API
- 指标性能数据需实际跑 mock 测试得出，不编造

## 快速导航

```bash
cd /Users/liwentao/Documents/开发/CoTBuilder
cat oldCode/CoTBuilder-V2.py        # 参考实现（只读）
cat doc/design.md                   # 设计文档（改并发/限流/匹配前必读 §5 防误解清单）
cat doc/formats.md                  # 全部 JSON 格式总览（输入/run/judge/merge/ShareGPT）
cat doc/test-results.md             # 测试结果档案（实测指标与复现方法）
ls cotbuilder/                      # 新代码包
.venv/bin/python -m pytest tests/   # 跑全部指标测试
```
