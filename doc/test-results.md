# CoTBuilder 测试结果档案

> 测试日期：2026-07-28 ｜ 环境：Python 3.12.9（`.venv`，uv 创建）、aiohttp 3.13.3、pytest 9.1.1、pytest-asyncio 1.4.0
> 原则（CLAUDE.md 开发约定）：指标性能数据一律实际跑 mock 测试得出，不编造。本文所有数字均为真实运行输出。
> 更新 2026-07-28：原版 comparator 对齐后新增 test_comparator_compat.py（46 项），总数 125 → **171**。

## 总览

| 套件 | 结果 | 耗时 |
|---|---|---|
| 全部指标测试（`pytest tests/`，slow 默认跳过） | **171 passed**，2 deselected | 39.5s |
| 近真实尺度冒烟（`pytest tests/ -m slow`） | **2 passed** | 25.5s |

分文件明细：

| 文件 | 通过数 | 覆盖 |
|---|---|---|
| test_matcher.py | 53 | 归一化正反例（audit-02 §7.1）、逐字段明细（§7.2）、判定一致性（§7.3）、三个永久回归用例（§7.4）、嵌套/类型、rank_key、aggregate |
| test_comparator_compat.py | 46 | 原版 schema 完备性、legacy 与原版判定对齐（15 语料）、已知差异固化（7 案例）、extractor 单引号兜底 |
| test_extractor.py | 12 | 直解/代码块/平衡括号嵌套提取/字符串内括号转义/垃圾文本 |
| test_ratelimit.py | 12 | paced 间隔、任意 60s 窗 ≤ qpm、并发等待者等差放行、窗口指标、退避上下界/cap/jitter 方差 |
| test_generator.py | 14 | 寿命语义（纯 MISMATCH==3、纯网络==5、混合≤8、分账桶）、一遍过、最优收尾、错误分类、结果字段兼容 |
| test_writer.py | 8 | 逐次落盘文件恒合法、定期重写去重、checkpoint 恢复与自愈重建 |
| test_client.py | 9 | 并发上限（服务端观测 max_in_flight）、限流饱和不占槽、1s 桶上界、错误分类、连接复用 |
| test_batch.py | 7 | 请求数守恒、时间包络、并发/串行精确等价、403 风暴恢复、断点恢复、success_rate 口径、输出兼容 |
| test_degraded.py | 5 | 降级场景：10% 断连、5% 概率 403、大延迟抖动、混合小故障、100% 断连干净失败 |
| test_mock_server.py | 5 | mock 自检：场景、观测端点、固定窗口 403、seed 确定性 |
| test_smoke.py（slow） | 2 | 真实 qpm=50 匀速性（间隔 ≥1.15s）、并发瓶颈验证 |
| **合计** | **173** | |

## 核心指标实测值

以下数值取自上述运行中的断言点，全部为断言通过的实际观测：

### 并发与限流（audit-01 §5 / 附录 A.5）

- **并发上限真实守住**：40 协程 / `max_concurrent=5` 场景，服务端观测 `max_in_flight = 5`（打满且未越界）；限流远慢于并发（qpm=60, conc=3）时在途仍 ≤ 3，8 请求到达间隔 ≥ 6.5s（证明等待限流的协程不占槽位）
- **样本内串行**：全部端到端测试中 `max_per_sample_in_flight ≤ 1`（client 侧逐样本在途计数）
- **匀速放行**：qpm=600（Δ=0.1s）下任意 1s 桶到达数 ≤ ⌈600/60⌉+1 = 11；冒烟测试真实 qpm=50 下相邻到达间隔全部 ≥ 1.15s（理论 1.2s，容忍 50ms 时钟抖动）
- **时间包络**：30 样本 / qpm=600 / 延迟 0.05–0.15s 场景，墙钟 ≤ 1.5×（29×0.1s + 0.15s）理论包络，断言通过

### 请求数守恒与寿命模型（audit-01 附录 B.3）

- 零错误场景：30 样本 → **恰好 30 次请求**，quota 分账 `initial=30, retry_quality=0, retry_network=0`
- 混合场景（确定性 mock，20 样本）：`initial == 20`，`arrivals == Σ attempts == quota 三桶之和`，单样本请求上界 3 次（首次 + 2 次质量重试）
- 极端断连（100%）：5 样本 × 3 次网络寿命 = **恰好 15 次请求**（`initial=5, retry_network=10`），全部以 `NETWORK_ERROR` 干净失败，`attempts` 为真实值 3（老代码硬编码 1 已修复）
- 对比老代码：单样本请求放大上界由 **20 次（无账可查）降为 8 次（3 样本寿命 + 5 网络寿命，独立分账）**

### 负优化不复现（audit-01 附录 B.3.3）

- 确定性 mock（按请求内容哈希分配命运）下 `max_concurrent=1` vs `10` 两次运行：**成功数与逐样本状态精确相等**（非统计等价，是逐样本恒等）

### 403 风暴恢复（audit-01 附录 A.5.3）

- 风暴模式（启动后 1.0s 内全部 403）+ 10 样本：最终**全部成功**；全程任意 1s 桶无越界（无同步突发）；403 重试全部记入 `retry_network` 桶（`retry_quality=0`，预算隔离成立）；墙钟 ≤ 风暴时长 + 稳态包络 + 3s 余量

### 降级场景（有效 QPM 略低于标称值时的表现，用户追加要求）

- 10% 断连：重试补上后 20 样本全部成功，有效 QPM ≤ 标称 ×1.05，无突发补发
- 5% 概率 403：全部成功，403 只烧网络账
- 大延迟抖动（0.05–0.8s，qpm 不介入）：并发打满 ≥ 9/10（R4 推论——并发是瓶颈时限流不应先触发）
- 混合小故障（断连 5% + 空响应 3% + 非法 JSON 3%）：30 样本全部落定，失败样本 error_type 均为已知类别
- 说明：按当前需求**未实现**针对此类现象的补偿机制，仅验证系统不失态、不死锁、可恢复

### 匹配器（audit-02 §7）

- 规格符合性：R-N1（全角字母/数字/符号、￥、U+3000、中文标点表）与 R-N2（标点前后空格、strip）逐条正反例通过；R-N3 规格外差异（大小写、缺 ¥、内部空格破坏、5.83 vs 5.830、™ 不经 NFKC 折叠）全部判 MISMATCH
- 三个永久回归用例固化：`LAU` vs `Lau`、`¥5.83` vs `5.83`、`LAU, LAI LI` vs `LAU,LAIL I`
- 判定一致性：结果中 `robust_match is comparison_result`（同一对象，消灭双实现）

### 断点恢复与口径

- 跑 3 个后 cancel：崩溃瞬间结果文件仍为合法 JSON（就地追加的崩溃安全性）；重跑 `skipped=3`、最终 6 个 id 无重复
- checkpoint 删除/损坏：从结果文件自愈重建 processed_ids，断言通过
- `success_rate` 分母不含 skipped：8 样本（4 skipped + 新 4 中 2 成功）→ 实测 `0.5`（若老口径则为 0.25）

## 复现方法

```bash
cd /Users/liwentao/Documents/开发/CoTBuilder
.venv/bin/python -m pytest tests/ -v            # 125 项指标测试（~40s）
.venv/bin/python -m pytest tests/ -v -m slow    # 2 项近真实尺度冒烟（~26s）
```

依赖安装（如重建环境）：`uv venv .venv && uv pip install --python .venv/bin/python --index-url https://pypi.tuna.tsinghua.edu.cn/simple aiohttp pytest pytest-asyncio`

---

*关联文档：[design.md](design.md)（设计决策与防误解清单）、[audit-01](audit-01-concurrency.md) / [audit-02](audit-02-robust-matching.md)（验收指标来源）*
