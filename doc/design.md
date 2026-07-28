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
| 其他 4xx/5xx | API_ERROR | ✗ |
| 连接错误 / 超时 | NETWORK_ERROR | ✓ |
| 200 但 content 为空 | EMPTY_RESPONSE | ✗（显式决策，见 §5.4） |

## 4. 匹配器（审计报告 02 §4–§6 的完整实现）

### 4.1 归一化 N(text)——只做这些，其余即 bug

1. 全角→半角受控映射：U+FF01–FF5E 减 0xFEE0；U+3000→空格；￥→¥、￡→£；
   中文标点显式对照表（，：；？！（）【】""''、。等）；
2. 删除紧邻标点**前后**的空白；首尾 strip；
3. 其余位置空格一律保留。

**明确不做**（R-N3）：大小写转换、删除货币符号、删除非标点相邻的内部
空格、数字格式等价（5.83≠5.830）、**禁用 unicodedata.NFKC**（会把 ™→TM、
①→1，把符号错误洗成一致）。

值类型规则：两端均 str 才走归一化；int/float 跨类型同值视为 STRICT
（bool 除外）；其余类型严格相等。防 `str(None)=="None"` 假阳性。

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
4. **为什么 EMPTY_RESPONSE / API_ERROR 不重试**：空响应重试会引入无界
   请求放大且几乎不会自愈；非限流类 4xx/5xx 同理。保持与老代码一致的
   语义，改它需要先立项评估放大风险。
5. **为什么样本内不并发**：3 并发抽卡的请求放大正是触发服务端 403 风暴
   的直接原因，而风暴杀死的恰恰包括抽卡请求本身（附录 B.1 自我挫败）。
   串行重试 + 跨样本并发用更少的请求获得同样的多次采样机会。
6. **限流窗口的 +1 边界**：60/qpm 在浮点下可能略小于真值（如 1.2），
   理想对齐时钟下任意 60s 半开窗口最多放行 qpm+1。这是容差 1 的边界
   效应，与审计附录 A.2 关注的 2× 峰值（100QPM）性质不同；真实
   monotonic 时钟不会出现理想对齐。测试断言用 `≤ qpm + 1`。
7. **qpm_limit 与 max_concurrent 的关系**（Little's Law）：打满 QPM 50、
   平均延迟 60s 需要约 50 个在途请求；`max_concurrent=10` 时吞吐上限
   约 10 QPM。**常规配置下并发是瓶颈，限流不应被触发**；若测试中限流
   频繁先于并发打满触发，或实际并发长期低于上限，说明两者又被耦合了。
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

mock server 侧 `/_stats`：每请求到达时间戳（monotonic+wall）、outcome、
`max_in_flight`（服务端视角的并发上限断言点）。

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
| 冒烟 | test_smoke（slow） | 真实量级 qpm=50 + 秒级延迟 |

mock 时间尺度策略：延迟默认 (30,90)s 贴近真实；测试把**时间常数整体
缩小**（延迟 0.05–0.2s + qpm 同步放大），用真实 event loop 几秒跑完，
测的是真实 asyncio 行为而非虚拟时钟。`deterministic_by_content=True` 时
mock 按请求内容哈希分配命运，同一样本在任意并发交错下结果一致——
「并发 vs 串行成功率精确相等」由此可断言。

真实入口（连通专家模型服务后）：

```bash
python -m cotbuilder.cli --input samples.json --output out/ --api-key <key>
```

## 8. 数据兼容性（R3，不得破坏）

- 输入：`messages` / `conversations` 两种样本格式均支持；
- 输出：`success_samples.json` / `failed_samples.json`（JSON 数组）与
  `checkpoint.json`（`{timestamp, processed_ids}`）格式与老代码一致；
- 结果字段：`sample_id / status / attempts / original_sample /
  cot_response / full_api_response / predicted_json / ground_truth /
  comparison_result / robust_match / error / error_type` 全部保留，
  仅新增 `match_level` 与 comparison_result 内的逐字段明细；
- `attempts` 现在是真实 HTTP 请求数（老代码硬编码 1，属修复而非变更）；
- `summary.json` 为新增汇总文件（老代码有同名字段结构，属超集）。
