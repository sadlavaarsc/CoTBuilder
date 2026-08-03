# CoTBuilder 设计文档（面向后续维护者）

> 读者设定：本文档写给后续维护本项目的小上下文模型与工程师。
> 每个模块单一职责、可独立阅读；「防误解清单」（§5）记录了容易踩坑的
> 设计决策，**修改并发/限流/匹配相关代码前必读**。

## 1. 这是什么

CoT 数据生产管线：用多模态专家模型对文档图片做关键信息提取，生成带推理链的
CoT 数据，与 Ground Truth（GT）比对验证后筛出匹配样本，作为后训练语料。

```
样本(图片+prompt+GT) → base64 编码构消息 → 限流放行 → 并发槽内发 HTTP 请求
  → 提取 JSON → 与 GT 比对（Matcher）
  → 通过：写入 success_samples.json
  → MISMATCH：消耗样本寿命，立即重排（串行重试）
  → 网络错误/403：消耗网络寿命，退避后重排
  → 寿命耗尽：按历史最优写入 failed_samples.json
  → 每样本落盘即更新 checkpoint（断点恢复）
```

## 2. 模块结构与依赖方向

依赖方向**单向**，禁止反向依赖：

```
cli → batch → generator → {client, matcher, extractor, writer}
                  client → ratelimit
                  matcher → extractor
batch → metrics（创建并注入 client / generator；它们独立构造时 metrics=None）
```

| 模块 | 职责（只有这一个职责） | 关键类型 |
|---|---|---|
| `config.py` | 全部运行参数的唯一出处 | `Config`（frozen dataclass） |
| `ratelimit.py` | 「何时可发起」与「多久后再试」 | `PacedRateLimiter`、`BackoffPolicy` |
| `client.py` | 唯一发 HTTP 的地方：单发 + 错误分类 | `ExpertModelClient`、`CallOutcome` |
| `extractor.py` | 从文本提取 JSON（响应与 GT 共用） | `extract_json()` |
| `matcher.py` | 验收判定 + 诊断分析（唯一组件） | `Matcher`、`SampleVerdict` |
| `generator.py` | 单样本寿命循环 | `SampleProcessor` |
| `writer.py` | 实时落盘 + checkpoint | `ResultWriter` |
| `batch.py` | 批量编排 + 指标汇总 | `BatchRunner` |
| `metrics.py` | 性能追踪：四段耗时 / 有效 QPM 曲线 | `Metrics`、`MetricsEvent` |
| `cli.py` | 参数解析入口 | `main()` |
| `mock/mock_server.py` | 指标测试基准（非生产代码） | `MockExpertServer` |

## 3. 并发模型（P0，审计报告 01 的重构核心）

### 3.1 三个互不知晓对方存在的组件

1. **PacedRateLimiter（ratelimit.py）**：匀速放行闸门。`acquire()` 在锁内
   O(1) 预约放行时刻 `t = max(now, last_grant + 60/qpm)`，随后**锁外**睡到 t。
   - 不变量：任意两次放行间隔 ≥ 60/qpm ⇒ 任意 60s 半开窗口 [t, t+60) 内
     放行 ≤ qpm（浮点边界容差 +1，见 §5.6）；冷启动最大突发 = 1。
   - **它不持有任何资源**，等待只挂起调用方协程自身。
2. **并发信号量（client.py）**：`Semaphore(max_concurrent)`，包裹**完整 HTTP
   生命周期**（获取 → 请求 → 读完 body → 释放）。
3. **寿命循环（generator.py）**：唯一的重试决策者。client 不知道「重试」
   的存在，每次调用就是一发请求。

**调用顺序不可交换**：先 `limiter.acquire()`（不持资源）→ 再拿信号量 →
发请求 → 释放信号量 → （如需重试）在**信号量之外**睡眠 → 重新走 acquire。

### 3.2 寿命（life）模型：样本内串行、跨样本并发

替代老代码的「MISMATCH 3 并发抽卡」。每个样本一个协程，协程内一次只发
一个请求（同一样本在途请求恒 ≤ 1，有指标断言）；并发度靠同时处理多个
样本实现。两本独立的寿命账（`Config.max_sample_attempts` 默认 3、
`network_max_attempts` 默认 5）：

| 结果 | 消耗 | 重排时机 | 配额分账桶 |
|---|---|---|---|
| 验收通过（STRICT/NORMALIZED_MATCH） | — | 结束，成功落盘 | initial 或对应 retry 桶 |
| MISMATCH | sample_life -1 | **立即**重排（节奏交给限流器） | retry_quality |
| 网络错误 / 403 / 429 | network_life -1 | **退避（指数+jitter）到点才重排** | retry_network |
| API_ERROR / EMPTY_RESPONSE / JSON 解析失败 | 不消耗 | 不重试，直接失败落盘 | — |

「桶」不是独立的调度器：限流器 + 并发槽的排队就是桶。每次重排都重新走
`client.call → limiter.acquire → semaphore`，与其他样本自然轮转。

寿命耗尽收尾：按 `Matcher.rank_key`（级别优先 STRICT>NORMALIZED>MISMATCH，
同级按匹配字段数）选**历史最优**尝试作为 failed 结果（error_type=MISMATCH）。

单样本请求数上界：`max_sample_attempts + network_max_attempts`（默认 8；
老代码为 20 且无账可查）。

### 3.3 错误分类按状态码驱动（client.py）

| 条件 | 分类 | 可重试 |
|---|---|---|
| 403 / 429 | RATE_LIMITED | ✓（尊重 Retry-After，与本地退避取 max） |
| 502 / 503 / 504 | GATEWAY_ERROR | ✓（网络账退避，但单样本重试 ≤ gateway_max_attempts=2，见 §5.16） |
| 其他 4xx/5xx | API_ERROR | ✗ |
| 连接错误 / 超时 | NETWORK_ERROR | ✓ |
| 200 但 content 为空且 finish_reason=length | LENGTH_TRUNCATED | ✗（thinking 耗尽预算，确定性失败，见 §5.4） |
| 200 但 content 为空（finish_reason≠length） | EMPTY_RESPONSE | ✗（显式决策，见 §5.4） |

## 4. 匹配器（审计报告 02 §4–§6 的完整实现）

### 4.1 归一化 N(text)——只做这些，其余即 bug

1. 全角→半角受控映射：U+FF01–FF5E 减 0xFEE0；U+3000→空格；￥→¥、￡→£；
   中文标点显式对照表（，：；？！（）【】""''、。《》／等）；
2. 删除紧邻标点**前后**的空白；首尾 strip；
3. 其余位置空格一律保留。

**明确不做**（R-N3）：大小写转换、删除货币符号、删除非标点相邻的内部
空格、数字格式等价（5.83≠5.830）、**禁用 unicodedata.NFKC**（会把 ™→TM、
①→1，把符号错误洗成一致）。

值类型规则：两端均 str 才走归一化；int/float 跨类型同值视为 STRICT
（bool 除外）；其余类型严格相等。防 `str(None)=="None"` 假阳性。

### 4.1b 与原版 RobustJSONComparator 的关系（2026-07-28 补齐）

原版模块已拿到（`oldCode/`，只读）。其默认开启的宽松规则全部实现为
`MatcherConfig` 开关、**默认关闭**（默认行为 = audit-02 规格）：
`case_insensitive / collapse_internal_spaces / normalize_numeric_commas /
unify_currency_extended / type_insensitive / trim_empty_fields`。
`Matcher.legacy()`（CLI `--legacy-matcher`）全开对齐原版，用于历史数据
对账。`comparison_result` schema 为原版返回结构的超集。逐项对照与原版的
4 个已修正 bug 见 **doc/comparator-compat.md**（改 matcher 前必读）。

### 4.2 逐字段三级判定与验收口径

字段级：`STRICT` / `NORMALIZED` / `MISMATCH` / `KEY_MISSING_IN_PRED` /
`KEY_MISSING_IN_GT`（嵌套递归，数组按位置对齐，乱序对齐留作扩展）。
样本级聚合：全 STRICT→STRICT；有 NORMALIZED 无 MISMATCH→NORMALIZED_MATCH；
任一 MISMATCH/KEY_MISSING→MISMATCH。
**验收口径**：STRICT 与 NORMALIZED_MATCH 均通过。验收与诊断由同一次
`compare()` 产出（结果中 `comparison_result is robust_match`），消灭双实现。

### 4.3 GT 交叉验证离线分析（§6）

`Matcher.aggregate(verdicts)` 在 batch 结束时调用一次，产出写入
`summary.json` 的 `gt_analysis`：字段级 MISMATCH 分布（识别 GT 系统性
错误）、NORMALIZED 占比（标注噪声率）、KEY_MISSING 方向统计。不进主流程。

## 5. 防误解清单（修改前必读）

1. **为什么限流不持有并发槽位**：老代码的信号量在限流检查内获取、发起
   HTTP 前释放，并发上限从未实现；等待窗口滑动时持槽睡眠又造成队头阻塞。
   本设计的铁律：**限流等待期间不持有任何资源**。顺序颠倒即回归老 bug。
2. **为什么重试睡眠在信号量之外**：退避可能睡几十秒，持槽睡眠会让
   max_concurrent 个槽位全部被「睡觉的协程」占死，吞吐归零。先释放、
   再睡、睡醒重新 acquire。
3. **为什么是两本寿命账而不是共享预算**：共享预算是老代码「403 风暴烧光
   样本额度」的机制（附录 B.3.4）。sample_life 只被 MISMATCH 消耗，
   network_life 只被网络/限流错误消耗，互不挤占。
4. **为什么 EMPTY_RESPONSE / LENGTH_TRUNCATED / API_ERROR 不重试**：
   空响应重试会引入无界请求放大且几乎不会自愈；非限流类 4xx/5xx 同理。
   LENGTH_TRUNCATED（2026-07-29 拆分出的分类）是 thinking 耗尽输出预算
   的确定性失败——实测同一样本必然复现，重试只是再烧一遍 250s/32768
   tokens（e2e 报告 §2）。保持与老代码一致的语义，改它需要先立项评估
   放大风险。
5. **为什么样本内不并发**：3 并发抽卡的请求放大正是触发服务端 403 风暴
   的直接原因，而风暴杀死的恰恰包括抽卡请求本身（附录 B.1 自我挫败）。
   串行重试 + 跨样本并发用更少的请求获得同样的多次采样机会。
6. **限流窗口的 +1 边界**：60/qpm 在浮点下可能略小于真值（如 1.2），
   理想对齐时钟下任意 60s 半开窗口最多放行 qpm+1。这是容差 1 的边界
   效应，与审计附录 A.2 关注的 2× 峰值（100QPM）性质不同；真实
   monotonic 时钟不会出现理想对齐。测试断言用 `≤ qpm + 1`。
7. **qpm_limit 与 max_concurrent 的关系**（Little's Law，2026-07-29 按
   e2e 实测重写）：真实 API 延迟呈**双峰**——OK 请求 4–31s（p50≈10s），
   thinking 耗尽的 LENGTH_TRUNCATED 230–316s（约占 30% 时混合均值
   ~84s）。推论分两档：
   - **EMPTY 问题修复后**（~12s 均值）：打满 QPM 50 只需 ~10 个在途，
     **QPM 才是瓶颈**，max_concurrent=15–20 即饱和；
   - **EMPTY 未修复**（30% 慢尾）：需要 ~70 个在途才能打满 QPM 50。
   小批次（如 30 样本）在途上限 = 样本数，两者都触不到，瓶颈是样本
   供给本身。若测试中限流频繁先于并发打满触发，或实际并发长期低于
   上限且无慢尾占位，说明限流与并发又被耦合了。
8. **时钟统一用 time.monotonic**：wall clock 的 NTP 跳变会破坏放行间隔
   不变量。mock server 记录到达时间戳同样用 monotonic。
9. **extractor 的行为修正**：老代码第三步用非贪婪正则 `\{.*?\}`，嵌套
   JSON（明细行）必截断。新版改平衡括号扫描（`extractor._scan_balanced`），
   这是有意的行为修正，不是可还原的「重构」。
10. **已删除 `--max-retries`**：老代码该参数声明后从未生效。寿命参数为
    `--max-sample-attempts` / `--network-max-attempts`。R3 只约束数据
    兼容，CLI 参数不在兼容范围。
11. **writer 调用约束**：`save()` 只允许发生在 batch 的 as_completed 主
    循环（事件循环天然串行），因此 writer 无锁。若未来改成多线程/多循环
    写出，必须先给 writer 加锁。
12. **超时拆分：total 管慢推理，connect 管真故障**：2026-07-29 实测，
    思考型模型（`enable_thinking=true`）推理可超过 2 分钟，
    `ClientTimeout(total=120)` 把正常慢推理掐成了大量 NETWORK_ERROR
    （时间戳算术验证：间隔 = 120s + 退避，严丝合缝）——当时的结论是
    必须放大 total（见 §5.17 的变迁史）。现在 `request_timeout`
    （默认 120s，2026-07-30 生产实测推荐值，推导见 §5.17）只约束总
    时长，`connect`/`sock_connect`（默认 30s）约束连接建立——真网络
    故障半分钟内快速失败，不陪跑 request_timeout。
13. **metrics.record 内禁止 await**：事件记录发生在 client 信号量持有
    期间（并发关键路径），任何挂起都可能引入新的耦合。事件先入内存
    buffer，由 batch 主循环顺带 flush 落盘。同理，metrics 只观测、
    不进任何决策路径；各模块独立构造时 metrics=None（零开销）。
14. **max_tokens=32768 是服务端硬上限**：Qwen3.6-35B 输出上限 32768
    （输入 256K），发更大被静默钳制（实测发 65536 实际生效 32768）。
    thinking 与 content 共享此预算且 thinking 先消耗——**调大无效，
    调小让 content 更出不来**；要压 thinking 只能从 prompt / 采样参数 /
    thinking 预算参数入手（见 doc/investigation-01-e2e-diagnosis.md §2）。
15. **采样参数默认 = 官方「思考·精确任务」档**（0.6/0.95/top_k=20/pp=0）：
    原 temp=0.1 严重偏离官方建议，是 thinking 死循环的疑似诱因、也让
    重试近乎确定性。改默认值前先读 investigation 报告的 A/B 实验设计；
    「照抄原文」保真度由 STRICT 率护栏监控，不要为压重复直接把
    presence_penalty 拉到 1.5（那是通用档，精确任务档官方建议 0）。
16. **GATEWAY_ERROR 为什么重试但要封顶**：502/503/504 是网关层故障，
    与 NETWORK_ERROR 同族（瞬时基础设施问题，换时刻重试可能成功），
    不该进 API_ERROR 等死。但实测 504 的 rtt 恒定 ≈360s——多为网关
    超时阈值截断（thinking 极端长尾，与 LENGTH_TRUNCATED 同根），
    满额 network_life 重试 = 单样本最多 30 分钟纯浪费。故走网络账
    退避，同时单样本重试 ≤ `gateway_max_attempts`（默认 2）；官方档
    temp=0.6 下 thinking 轨迹有方差，1–2 次重试是有真实胜率的小赌注。
17. **request_timeout 取值变迁与当前逻辑（120s，2026-07-30）**：
    - v1（120s）：把合法慢推理（死循环未修复时 thinking 可 >2min）掐成
      大量 NETWORK_ERROR——错杀；
    - v2（400s）：「必须大于网关墙 360s，否则错杀合法长推理 + 破坏
      GATEWAY_ERROR 分类」——该推导的前提是**死循环未修复**，合法响应
      最长可达 ~359s；
    - v3（120s，当前）：官方采样档修复死循环后，合法响应实测分布收敛到
      **4–31s（p50≈10s）**，120s 已留 ~4× 余量；超过 120s 的请求几乎
      必然已死于 thinking 耗尽（LENGTH_TRUNCATED 230–316s）或正走向
      网关墙（504@360s）。提前掐断 = 并发槽提前 ~200s 释放 + 重试更
      早发起（官方档 temp=0.6 下重试有方差、有真实胜率）。
      **代价（有意接受）**：>120s 的慢失败不再保留 LENGTH_TRUNCATED /
      GATEWAY_ERROR 精细分类，统一归 NETWORK_ERROR(timeout) 烧网络账
      （日志中 `TimeoutError, elapsed≈120` 可辨）；单样本死时间上限由
      ~316s 降到 ~120s。若未来延迟分布右移（更大模型/更长文档），需
      重新评估此值——判断依据是 OK 响应的 p99 而非网关墙。
18. **403 风暴 = 骑线振荡（已基本定性，只测不补）**：2026-07-29
    生产跑批 403 风暴经 metrics.jsonl 全量重建（investigation-01
    追加六）：客户端 pacing 100% 无越界（含被拒请求任意 60s 窗 ≤ 50）；
    第一个 403 恰好是第 51 个发送（t0+60s），最强解释是**服务端窗口
    配额=50 且被拒请求也计数**——骑线 50/min 时拒绝自我维持，只有
    我方退避降速才能排空，恢复后队列推满又触发下一次（单次运行 ≥2
    次风暴）。两个推论：① **403 的指数退避是稳定性机制不是礼貌**，
    改成立即重排会在「拒绝计数」的服务器上活锁；② **qpm_limit 不要
    设骑线值**——生产取 40–45（标称 50 的 80–90%），max_concurrent
    配套 ≥20–25（Little's Law，实测 in_flight 峰值 26）。安全性无虞：
    403 只烧网络账、有 network_life 硬顶、快速失败不占槽。
19. **judge 改判不改变规则匹配器口径**：judge.py 是独立后处理层——
    主流程 summary 的 gt_analysis/quality 仍以规则判定（STRICT）为准，
    改判率单独在 judge_summary.json 观测。不要为了让两个口径「一致」
    去放宽 matcher：STRICT 是验收口径（不自骗），judge 是捞数据手段。
    同理，**judge 缺 verdict 的 pair 按未改判处理**（保守默认，防模型
    漏判误判成功）——不要把「缺 verdict」宽松化成「视为通过」。
20. **merge/convert 是纯离线只读工具**：merge.py 与 convert.py 不触
    网络、不改源目录任何字节（merge 的 --output 禁止与 --run 同目录，
     argparse 层直接报错）。合并只按 sample_id join、按 status 路由——
    **不要**在 merge 里「顺手」修字段/补字段：标签语义就是
    `judge_result` 键的存在性，原记录（含规则判定的 comparison_result）
    必须原样保留，否则反复 judge 与口径审计会失真。
21. **thinking 文本回收优先级不可颠倒**：convert 的 thinking 模式按
    ① full_api_response 的 reasoning_content → ② cot_response 剥离
    JSON span（find_json_span）取推理链；两者皆空则**不加 <thinking>
    包裹**。不要为了让标签非空去拼接 predicted_json 或复述答案——
    假推理链进训练数据比没有推理链更糟。服务端是否返回
    reasoning_content 取决于部署（client 保留了完整 body，两条路都在）。

## 6. 内建指标（观测口径）

`summary.json` 的 `metrics` 字段（审计报告 01 §5.5：老代码完全无观测）：

| 指标 | 含义 | 健康基准 |
|---|---|---|
| `total_http_requests` | 实际发出的 HTTP 请求总数 | == quota 三桶之和 |
| `quota` | 配额分账：initial / retry_quality / retry_network | initial == 处理样本数 |
| `outcomes` | 按错误分类的请求结果计数 | — |
| `peak_in_flight` | 在途请求峰值（client 侧） | ≤ max_concurrent，常规应贴近 |
| `max_per_sample_in_flight` | 单样本在途峰值 | 恒 ≤ 1（样本内串行） |
| `amplification` | 请求放大倍数 = 总请求 / 处理样本数 | ≤ max_sample+network 寿命之和 |
| `token_usage` | usage 累计：prompt/completion tokens + 有 usage 的响应数 | LENGTH_TRUNCATED 浪费的 completion_tokens 在此可见 |

`summary.json` 另有 `quality` 块（平均 KV 质量，2026-07-29 新增）：
`match_score_mean`（有 verdict 样本的 match_score 均值——看整体质量
而不只是完美样本）、`samples_scored` / `samples_unscored`（网络/网关/
空响应类失败无分数）、`match_score_mean_by_window`（按完成顺序每 10
个样本一窗的均值，观察长跑中的质量漂移）。

mock server 侧 `/_stats`：每请求到达时间戳（monotonic+wall）、outcome、
`max_in_flight`（服务端视角的并发上限断言点）、`done_monotonic`
（响应完成时刻，服务端侧延迟可算）。

## 6b. 性能追踪（metrics.py，2026-07-29 新增）

背景：实测发现性能问题时，终态计数器无法回答「时间花在限流排队 / 槽位
排队 / HTTP 飞行 / 退避哪一段」「有效 QPM 随时间的曲线」。本模块把每次
请求拆成四段 monotonic 打点：

```
t0 → limiter.acquire() → t1 → semaphore 获取 → t2 → HTTP 完成 → t3
     [wait_limiter]           [wait_slot]          [rtt]
```

- **事件流**：每请求一条 `{ts, sample_id, kind:"request", outcome,
  quota_kind, wait_limiter, wait_slot, rtt}`；退避一条 `{kind:"backoff",
  backoff}`。`record_*` 不 await（防误解 §5.13）。
- **有效 QPM 口径**：HTTP 段起点（t2，限流放行 + 拿到槽位之后）按
  `metrics_interval`（默认 10s）分桶，桶内发起数 × 60/interval。
  这是「实际打到服务端的速率」，与限流器放行口径一致。
- **落盘**：`output/metrics.jsonl`（每事件一行，batch 主循环每次
  writer.save 时顺带增量 flush）。全新一轮（无 checkpoint）时先删旧文件；
  断点续跑则继续追加。JSONL 追加写，无结果文件的就地数组问题。
- **终态报告**：进 `summary.json` 的 `metrics.performance`：
  `rtt_p50/p95/p99`、`phase_totals`/`phase_shares`（四段耗时总额与占比，
  定位瓶颈段）、`effective_qpm_mean/min`、`buckets`（完整曲线）。
- **控制台进度行**：reporter 协程按 `progress_log_interval`（默认 30s，
  0=关）输出：`in_flight=8 eff_qpm=42.3 completed=17/100 rtt_p50=61s`。
  in_flight 实时值只能来自 client.stats（metrics 只在请求完成时记事件）。
- **接线**：`BatchRunner.__init__` 创建 Metrics 注入 client 与 processor；
  各模块独立构造时 metrics=None，零开销零行为变化，保持可独立测试。

典型排查路径：有效 QPM 曲线塌陷 → 看 phase_shares——wait_limiter 大是
限流绑定（正常，qpm 配置即瓶颈）；wait_slot 大是并发不足；rtt 大是服务
端慢；backoff 大是错误率高。配合 metrics.jsonl 逐事件可精确定位样本。

## 6c. Model judge 后处理工具（judge.py，2026-07-30 新增）

**定位**：独立可选工具，**不在正常工作流内**。规则匹配器（STRICT 口径）
判失败的样本中混有「语义一致但字面有差异」的误判（GT 标注质量：空格/
连字符/字段内顺序，如 `J-123` vs `J123`）。judge 用同一个模型、纯文本
（不看图）对失败样本做改判。

```bash
python -m cotbuilder.judge --input <run输出目录> --output <judge目录> \
    --api-key <key>            # --input 也可直接给 failed_samples.json
```

设计决策：

- **只判失败 KV pair**：judge 输入 = `comparison_result.differences`
  （字段/提取值/标准答案三元组），不发整份 JSON——大部分失败样本只错
  一两个 key，输入极小省时间省 token；
- **判定语义 = 「定义什么是错」**（用户定调，见 JUDGE_SYSTEM_PROMPT）：
  多出/缺失影响实义的内容、字符识别不一致 → 不一致；空白、顺序、
  无义符号差异 → 忽略；null ≈ 空字符串 ≈ "无" ≈ "N/A"；
- **保守改判**：样本改判 ⟺ 每个失败 pair 都有对应 verdict 且全部
  match=true；模型漏判的字段按未改判处理（防漏判误判成功）；
- **只做网络重试**：NETWORK_ERROR/RATE_LIMITED/GATEWAY_ERROR 退避重试
  （复用 BackoffPolicy + network_max_attempts）；judge 判 false 不是
  错误、不重试；API_ERROR/EMPTY/LENGTH_TRUNCATED 终态维持原判；
- **输出独立目录**（ResultWriter 复用，checkpoint 断点续判免费获得）：
  改判成功 → success_samples.json（原记录 + judge_result 块，含
  original_sample/cot_response 可直接作训练数据）；其余 →
  failed_samples.json（维持原判，不丢数据）；judge_summary.json
  观测改判率与失败分桶；
- **复用同一套生产采样参数**（官方精确档 + thinking 开）：比对任务同样
  受益于思考链，口径与主流程一致。

## 6d. judge 结果合并工具（merge.py，2026-08-03 新增）

**定位**：纯离线只读工具，把 judge 改判结果并回原 run 数据，产出
「规则 + judge」最终口径的数据集目录。**不在正常工作流内**。

```bash
python -m cotbuilder.merge --run <run目录> --judge <judge目录> \
    --output <合并目录>        # --output 禁止与 --run 相同（argparse 报错）
```

设计决策：

- **翻转 + 搬移 + 标签**（用户拍板）：judge 改判成功 → merged success；
  维持原判/判失败 → merged failed；未覆盖的 run 记录原样。**不修改任何
  已有字段**——judge_result 块的存在即「被判过」标签（见 §5.20）；
- **只 join 不加工**：按 sample_id join、按 status 路由；orphaned
  （judge 有 run 无）跳过、collision（与 run success 撞 id）以 run 为准，
  均计数进 merge_summary.json 对账；
- **反复 judge 循环**：merged 目录的 failed_samples.json 可直接作 judge
  --input 再判一轮，再 merge 叠加（新 judge_result 覆盖旧块）；
- 原子落盘（tmp + os.replace，仿 writer._full_rewrite），无 checkpoint
  ——一次性转换，重跑即幂等。

## 6e. ShareGPT 数据集转换工具（convert.py，2026-08-03 新增）

**定位**：纯离线只读工具，把 run/merged 输出转为下游训练框架
（LLaMA-Factory 等）可读的 ShareGPT 格式。**不在正常工作流内**。

```bash
python -m cotbuilder.convert --input <合并目录> --output train.json
# 默认 gpt-mode=thinking + format=json；--gpt-mode raw|json，--format jsonl
```

设计决策：

- **gpt 轮三形态**（默认 thinking，用户拍板）：`<thinking>推理链</thinking>`
  + 纯 JSON / cot_response 原文 / 纯 predicted_json；推理链回收优先级与
  「空则不加包裹」规则见 §5.21；
- **容器默认 JSON 数组**（对齐用户 13 万条老数据微调输入），JSONL 可选；
- **默认只读 success_samples.json**（训练数据语义；judge 改判成功的记录
  经 merge 后就在 success 里，天然包含）；调试用 --input 指定文件；
- human 轮 = original_sample 的 prompt（messages/conversations 两格式
  与 generator 同取值源）；`<image>` 占位符缺失时前置；images 路径透传；
- extractor 新增 find_json_span（纯加法，extract_json 改为委托它，
  行为不变）——cot_response 的「推理链 / JSON 答案」切分与主流程提取
  共用同一份平衡括号扫描。

## 7. 如何跑测试（全部走 mock，不触真实 API）

```bash
cd /Users/liwentao/Documents/开发/CoTBuilder
source .venv/bin/activate   # 或直接使用 .venv/bin/python

python -m pytest tests/ -v            # 全部指标测试（slow 默认跳过）
python -m pytest tests/ -v -m slow    # 近真实尺度冒烟（真实 qpm=50、秒级延迟）
```

测试分层：

| 层 | 文件 | 覆盖 |
|---|---|---|
| 纯单元（fake clock/rng） | test_ratelimit / test_matcher / test_extractor | 限流不变量、退避分布、归一化正反例、JSON 提取 |
| 组件集成（mock server） | test_client / test_generator / test_writer | 并发上限、paced、错误分类、寿命语义、断点恢复 |
| 端到端指标 | test_batch / test_degraded | 请求数守恒、时间包络、并发不劣化、403 风暴恢复、降级场景（网络抖动致有效 QPM 略降）、兼容与口径 |
| 后处理工具 | test_judge / test_merge / test_convert | judge 改判（mock server）；merge/convert 纯离线纯函数 + tmp_path 文件断言（无 mock） |
| 冒烟 | test_smoke（slow） | 真实量级 qpm=50 + 秒级延迟 |

mock 时间尺度策略：延迟默认 (4,30)s 贴近实测 OK 分布（2026-07-29 e2e：
4–31s、p50≈10s）；测试把**时间常数整体缩小**（延迟 0.05–0.2s + qpm
同步放大），用真实 event loop 几秒跑完，测的是真实 asyncio 行为而非
虚拟时钟。`deterministic_by_content=True` 时
mock 按请求内容哈希分配命运，同一样本在任意并发交错下结果一致——
「并发 vs 串行成功率精确相等」由此可断言。

超长尾建模（2026-07-29 按 e2e 实测校准）：`empty` / `length_truncated`
outcome **确定性**使用 `slow_latency`（默认 (230,320)s，实测 thinking
耗尽响应 230–316s）——慢=token 耗尽，二者强绑定；`slow_response_rate`
（默认 0）保留为其他 outcome 附加慢延迟的独立旋钮（超时拆分测试用）。
`length_truncated` 响应复刻实测模式：content=null +
finish_reason=length + completion_tokens=32768。

真实入口（连通专家模型服务后）：

```bash
python -m cotbuilder.cli --input samples.json --output out/ --api-key <key>
```

## 8. 数据兼容性（R3，不得破坏）

> **全部 JSON 格式的字段级总览见 [formats.md](formats.md)**（输入样本 /
> run 输出 / comparison_result / judge / merge / ShareGPT / 辅助文件）。
> 本节只列不得破坏的兼容约束。

- 输入：`messages` / `conversations` 两种样本格式均支持；
- 输出：`success_samples.json` / `failed_samples.json`（JSON 数组）与
  `checkpoint.json`（`{timestamp, processed_ids}`）格式与老代码一致；
- 结果字段：`sample_id / status / attempts / original_sample /
  cot_response / full_api_response / predicted_json / ground_truth /
  comparison_result / robust_match / error / error_type` 全部保留，
  仅新增 `match_level` 与 comparison_result 内的逐字段明细；
- `attempts` 现在是真实 HTTP 请求数（老代码硬编码 1，属修复而非变更）；
- `summary.json` 为新增汇总文件（老代码有同名字段结构，属超集）。
