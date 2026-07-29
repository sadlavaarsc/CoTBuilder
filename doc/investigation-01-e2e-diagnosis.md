# CoTBuilder 调研结论报告 — 30 样本 e2e 诊断与修复方向

> 创建于 2026-07-29 ｜ 状态：**持续补充中**（后续结论追加在文末）
> 数据来源：[e2e_test_report.md](e2e_test_report.md)（30 样本真实 API 实测）
> 性质：本文是调研结论与决策依据的汇总，代码改动以 design.md / CLAUDE.md 为准。

---

## 一、框架健康度结论：并发/限流/寿命/观测无缺陷

报告的 57 次请求按代码语义完全拆平，三个独立数字互相咬合：

| 来源 | 计算 | 请求数 |
|---|---|---|
| 13 个 EMPTY 样本 × 1 次（代码不重试 EMPTY） | 13 initial | 13 |
| 9 个 MISMATCH 样本 × 3 次寿命耗尽 | 9 initial + 18 retry_quality | 27 |
| 8 个成功样本（含 5 次质量重试后通过） | 8 initial + 5 retry_quality | 13 |
| RATE_LIMITED 重试 | 4 retry_network | 4 |
| **合计** | 30 + 23 + 4 | **57** ✓ |

- NETWORK_ERROR **0 次**：超时拆分修复（request 600s / connect 15s）达到预期，
  EMPTY 样本 230–316s 全部跑完并拿到 `finish_reason=length`（旧 120s 超时会把
  它们掐成假网络错误并退避重试，浪费 3 倍以上时间）；
- `full_api_response` 落盘使根因诊断成为可能；metrics 四段耗时口径在真实
  数据上成立（rtt 83.3% / wait_limiter 16.2% / backoff 0.5% / wait_slot 0%）；
- metrics.jsonl 逐事件数据足以支撑事后推算（报告 §4 加速比分析即基于此）。

## 二、核心根因：thinking 耗尽输出预算（43.3% 样本报废）

### 2.1 已确认的事实

- **32768 是模型输出 token 硬上限**（256K 为输入上限）：客户端发
  `max_tokens=65536` 被服务端静默钳制到 32768，手工改 32768 行为无变化；
- thinking 与 content **共享** 32768 预算，且 thinking 先消耗：13 个
  EMPTY 样本全部是 `finish_reason=length + content=null`，
  reasoning_content 48k–67k 字符（含「让我们再看一眼」式死循环）；
- 该失败是**确定性的**（同一样本同一 prompt 必然复现），不是随机波动。

### 2.2 由此推翻的建议

- ❌ e2e 报告 §2.6「降低 max_tokens 到 4096–8192」：预算被 thinking 先吃掉，
  降预算只会让 content 更出不来。**杠杆不是总预算，是 thinking 占比**；
- ❌ e2e 报告 §3.6「EMPTY → quality retry 3 次」：对确定性失败重试是把
  43% 的浪费放大 3 倍。代码现有「EMPTY 不重试」（design.md §5.4）正确；
- ⚠️ e2e 报告 §2.4「sample_8/4 重试 3 次均同样失败」与 quota 分账矛盾：
  EMPTY 按代码不重试，attempts=3 的是 9 个 MISMATCH 耗尽样本。
  「确定性失败」结论方向正确，但「已用重试验证」的证据不存在。

### 2.3 修复方向（按性价比排序）

| # | 方向 | 成本 | 说明 |
|---|---|---|---|
| a | Prompt 限制 thinking 长度（如「推理 500 字内、每字段只验证一次」） | 零代码 | 抑制死循环，最先试 |
| b | 探测 API 是否支持 `thinking_budget` / `enable_thinking=false` | 一发请求 | thinking_budget 生效则最优雅；关 thinking 则 RTT 250s→~10s |
| c | 框架层：新增 `LENGTH_TRUNCATED` 错误分类（`finish_reason==length` 时归类） | 小改 | 观测先行，token 耗尽占比直接进 summary.outcomes |
| d | 框架层：LENGTH_TRUNCATED → 非 thinking 模式 fallback 补枪（每样本限 1 次） | 语义变更 | 推翻 §5.4，需改防误解清单 + 补测试；等 a–c 结果再定 |
| e | 按文档复杂度分流（简单样本直接非 thinking） | 优化项 | 最后考虑 |

### 2.4 实验顺序

```
① 发一枪 enable_thinking=false（同一样本）→ content 是否正常、精度掉多少
② 发一枪 thinking_budget=4096           → 参数是否被识别
③ 改 prompt 限制 thinking 长度           → 重跑 13 个 EMPTY 样本
④ 据 ①–③ 决定是否做框架层 c/d
```

## 三、MISMATCH 9/30（30%）——EMPTY 之后的主战场

1. **质量重试投入产出比不差**：23 次 retry_quality 中 5 次转化成成功，
   遇 MISMATCH 的样本约 1/3 靠重试救回；有效机制 = temperature=0.1 下
   thinking 路径的发散性。3 次寿命设定划算，不动；
2. **18 次重试打水漂**（9 个耗尽样本）：待分析同一样本 3 次尝试的
   predicted_json 差异度；若高度雷同，可 A/B 实验「重试时 temperature
   0.1→0.3」或 prompt 加「上次提取有误，请重新审视易错字段」（prompt 层，
   不动框架）；
3. **先分「模型错」还是「GT 错」再优化**：用 summary.json 的
   `gt_analysis.field_mismatch_distribution`（audit-02 §6）看字段级分布——
   系统性差一字（如 sample_7「往来港澳通行 vs 通行证」）怀疑 GT 口径或
   prompt 缺「证件名称输出全称」规则；分散随机才是模型问题；
4. **匹配器严格性不动**：差一字判 MISMATCH 是 audit-02 的刻意决策
   （后训练语料，放水=脏数据）；对账可用 `--legacy-matcher`。

## 四、性能与容量规划结论

### 4.1 实测延迟分布（推翻 mock 旧基准）

| 维度 | mock 旧建模 | 实测 |
|---|---|---|
| 典型档 | uniform(30, 90)s | **4–31s，p50≈10s**（LogNormal 状） |
| 慢档 | 随机附加任意响应 | **慢 = EMPTY，确定性绑定**（230–316s） |
| 平均延迟 | 60s | OK ~12s；混合 ≈ 0.7×12+0.3×250 ≈ 84s |

### 4.2 8000 样本规模的瓶颈推演（Little's Law）

| 场景 | 打满 QPM 50 需在途 | 瓶颈 |
|---|---|---|
| EMPTY 未修（30% 慢尾） | ~70 | max_concurrent 需 70+ |
| EMPTY 修复后（~12s） | ~10 | **QPM 50 成为唯一瓶颈** |

- EMPTY 修复后，8000 样本墙钟下限 = 8000/50 ≈ **160 分钟**，与并发无关；
  提速只有：提 QPM 配额 / 多 key / 降重试放大（成功率提升直接省 QPM 预算）；
- 报告 §4.3「加速比降到 1.5x」只适用 30 样本小批次；规模上去后并行让位于 QPM；
- peak_in_flight=22/30 是放行速率与完成速率的差值效应，非 bug（P2 不处理）。

### 4.3 口径说明

- `effective_qpm_max=54` 是 10s 桶粒度假象（9 放行 × 60/10），60s 窗口
  不变量才是真约束（design.md §5.6）；
- RATE_LIMITED 4 次（7%）：客户端 pacing 有测试保证，更可能是**共享 key**
  （他人请求计入同一预算）或服务端窗口比 60s 短；处理正确（Retry-After +
  backoff，全部恢复，backoff 占比 0.5%）。**不做自适应补偿**（用户既定决策：
  降级场景只测不补）；长跑若显著超 7%，先降 qpm_limit 到 40–45 或错峰。

## 五、待办清单（按行动顺序）

| # | 事项 | 类型 | 状态 |
|---|---|---|---|
| 1 | thinking 实验（§2.4 的 ①②③）+ 官方采样参数 A/B（追加记录 2026-07-29） | 实验 | 待做 |
| 2 | 过一遍 9 个 MISMATCH 的 differences + gt_analysis，分 GT 错/模型错 | 分析 | 待做 |
| 3 | 生成参数入 Config/CLI + 写进 summary.config：max_tokens（写实 32768）、temperature、top_p、top_k、presence_penalty、enable_thinking | 框架小改 | ✅ 2026-07-29（默认=官方思考·精确档） |
| 4 | `LENGTH_TRUNCATED` 错误分类（finish_reason==length 与真空响应拆开） | 框架小改 | ✅ 2026-07-29（不重试语义不变） |
| 5 | usage token 统计进 summary（13 空响应 × 32768 ≈ 42 万 tokens 浪费目前不可见） | 框架小改 | ✅ 2026-07-29（summary.metrics.token_usage） |
| 6 | mock 双峰重校准（典型档 (4,30)s + empty/length_truncated 绑定 slow_latency (230,320)s） | mock | ✅ 2026-07-29 |
| 7 | design.md §5.7 按「OK ~12s」重写 Little's Law 基准 | 文档 | ✅ 2026-07-29（另新增防误解 §5.14/15） |
| 8 | 重试时参数微调 A/B（已被官方采样参数实验吸收，见追加记录） | 实验 | 并入 #1 |
| 9 | LENGTH_TRUNCATED fallback 补枪（语义变更，改 §5.4 + 补测试） | 框架大改 | 待 #1 结果 |
| 10 | e2e 报告 §2.4 矛盾点在下次报告中澄清 | 文档 | 待做 |

## 六、对 e2e 报告的评价与勘误

- 数字自洽性：quota 三账与 57 请求完全对平，观测数据可信；
- 勘误 ①：§2.4 重试证据不存在（见 §2.2）；
- 勘误 ②：§2.6 降 max_tokens 建议方向错误（见 §2.2）；
- 勘误 ③：§3.6 EMPTY quality retry 建议应拒绝（见 §2.2）；
- 勘误 ④：§4.2 串行估算用 53 次请求（漏 4 次 RATE_LIMITED），9.8x 略有
  水分但方向正确；
- 采纳：双峰延迟建模、错误率基准（EMPTY 23% / RATE_LIMITED 7%）、
  P0 优先级判断。

---

## 追加记录（新结论补充在下方，注明日期）

### 2026-07-29 ｜ Qwen3.6-35B 官方采样超参（用户提供）

官方建议（按模式×任务分两档四类）：

| 模式 / 任务 | 参数 |
|---|---|
| 思考模式·通用任务 | temperature=1.0, top_p=0.95, top_k=20, min_p=0, **presence_penalty=1.5** |
| 思考模式·精确任务（如代码） | temperature=0.6, top_p=0.95, top_k=20, presence_penalty=0 |
| 非思考模式·通用 | temperature=0.7, top_p=0.8, top_k=20, presence_penalty=1.5 |
| 非思考模式·推理任务 | temperature=1.0, top_p=1.0, top_k=40, presence_penalty=2.0 |

**当前代码**：`temperature=0.1, top_p=0.9`（client.py 硬编码），
`top_k` / `presence_penalty` **未发送**。

#### 分析结论

1. **temperature=0.1 严重偏离官方建议，且可能是 thinking 死循环的诱因
   之一**。思考模式下过低温采样是重复循环的典型诱因（官方对思考模式
   的建议全部 ≥0.6，通用档甚至配 presence_penalty=1.5 专门压重复）。
   §2.1 观察到的「让我们再看一眼 ×数十次」很可能是采样参数病，
   不 purely 是模型行为——**修采样参数可能同时缓解 EMPTY 根因**；
2. **重试近乎确定性的问题同源**：temp=0.1 下重试输出高度雷同，
   §三.2 的「18 次重试打水漂」与「5 次转化成功」都是在低温下取得的——
   换官方参数后重试多样性应显著提升，**质量重试 ROI 有上升空间**；
3. **首选预设：思考模式·精确任务档**（temperature=0.6, top_p=0.95,
   top_k=20, presence_penalty=0）。信息提取是「照抄原文」型精确任务，
   通用档 temp=1.0 + pp=1.5 对字段保真度有风险；若死循环仍频发，
   再试通用档（pp=1.5 是专门的抗重复手段）；
4. **非思考模式预设是 fallback（§2.3-d）的配套参数**：若实验 ①
   （enable_thinking=false）可行，fallback 补枪应带非思考·通用档参数，
   而不是复用思考档；
5. **代价与收益**：更高温度 = 输出不可复现性上升，但管线有 Matcher
   验收兜底，多样性正是重试（抽卡）所需要的；风险点在「照抄原文」
   保真度，需用 STRICT 率监控。

#### 实验设计（并入 §2.4 实验顺序）

```
A. 思考·精确档（0.6/0.95/top_k=20/pp=0）重跑 30 样本
   → 对比基线：EMPTY 率（目标：死循环消失或大幅下降）、STRICT 率（不能掉）
B. 若 A 后 EMPTY 仍高 → 思考·通用档（1.0/0.95/top_k=20/pp=1.5）再跑
C. 若 B 仍无效 → 回到 prompt 限制 thinking 长度（§2.3-a）与非思考 fallback
观测指标：EMPTY 率、STRICT/NORMALIZED 率、同一样本多次尝试的输出差异度、
         重试转化率（retry_quality → 成功的比例，基线 5/23）
```

#### 对代码的要求

采样四参数（temperature / top_p / top_k / presence_penalty）必须随
§五.3 一起入 Config + CLI + summary.config——否则上述 A/B 实验无法
不改代码进行，且实验条件不可追溯（本次 32768 事件的同类教训）。

### 2026-07-29（二）｜ 504 Gateway Timeout 的 rtt 高度统一在 ~360s

用户实测观察：被分类为 API_ERROR 的 504 响应，rtt 全部统一在 360s 附近。

#### 判定

**网关超时阈值截断**（阈值 ≈ 360s），非随机瞬时故障。判据：瞬时 504 的
rtt 应时长不一，阈值截断则恒定 ≈ 网关超时值。

#### 推论

1. **504 是 thinking 过长问题的极端尾部**，与 LENGTH_TRUNCATED 同根：
   thinking 在 32768 token 内耗尽 → 230–316s 以 LENGTH_TRUNCATED 返回；
   thinking 更久 → 撞 360s 网关墙 → 504。三个档位：
   正常 OK（4–31s）→ LENGTH_TRUNCATED（230–316s）→ 504（360s）；
2. **360s < request_timeout=600s**：客户端能收到 504 响应体（不是自己
   超时），分类链路正常，问题只在「504 落进 API_ERROR 不重试」；
3. **重试价值取决于 thinking 路径的发散性**：旧 temp=0.1 下 504 近乎
   确定性（重试≈必撞墙）；官方档 temp=0.6 下重试有真实方差，换一条
   thinking 轨迹可能在 360s 内跑完——重试有效性随采样参数修复而提升，
   但单次重试成本最高 360s，代价不小；
4. **正解仍是压 thinking**（同 §2.3）：把长尾拉回 360s 以内，504 与
   LENGTH_TRUNCATED 会一起消失；若后续做「非思考 fallback 补枪」
   （§2.3-d），504 与 LENGTH_TRUNCATED 应**共享同一条 fallback 路径**——
   两者本质都是「thinking 太久导致响应没出来」，一头撞 token 墙、
   一头撞网关墙。

#### 决策（✅ 2026-07-29 已落地）

GATEWAY_ERROR（502/503/504）独立分类 + 消耗 network_life 退避重试，
单样本重试 ≤ `gateway_max_attempts`（默认 2，CLI `--gateway-max-attempts`）；
同时落地：`request_timeout` 默认 600→**400s**（网关墙 360s + 40s 余量，
低于 360s 会错杀合法长推理并破坏分类口径，见 design.md §5.16/17）、
summary 新增 `quality` 块（match_score 均值 + 完成序每 10 个滑窗 +
scored/unscored 计数，看平均 KV 质量而不只是完美样本）。
- 落地后重点观察：采样参数 A/B 后 504 率是否随 thinking 变短而下降
  （预期：与 EMPTY 率同步下降）；GATEWAY_ERROR 的 rtt 是否持续钉在
  360s（验证网关阈值是否固定）。

### 2026-07-29（三）｜ 输出速率实测与 request_timeout 合理区间

用户实测：p50 rtt ≈ 30s；手动测试 6000 token 纯输出仅 ~15s
（≈400 token/s）。

#### 推论：request_timeout 600s 过长，应缩短到「网关墙 + 余量」

1. **360s 网关墙是响应时长的真实上界**：超过 360s 的请求已被网关
   杀死（504），客户端等 600s 毫无意义——多等的 240s 全是死时间；
2. **但不能低于 360s**：合法响应可以跑到 ~359s（thinking 在 token 墙
   前完成、但超过网关墙前的任何时刻）。客户端超时 < 360s 会把这些
   响应掐成 NETWORK_ERROR，既错杀又破坏分类（504 的 GATEWAY_ERROR
   语义更优——可分类、可分别决策重试）；
3. **合理默认值 ≈ 400s**（网关墙 360s + 40s 余量：32768 token 响应体
   传输 + 网关自身抖动）。搭配：真连接故障仍由 connect_timeout=15s
   快速失败；输出速率 400 token/s 意味着即使顶满 32768 token，
   纯生成也仅 ~82s，时长大头是 thinking 等待而非传输；
4. 待验证：360s 网关阈值是网关固定配置还是随负载浮动——跑批时
   观察 GATEWAY_ERROR 的 rtt 是否持续钉在 360s。

