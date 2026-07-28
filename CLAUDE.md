# CoTBuilder — CLAUDE.md

> 创建于 2026-07-28 ｜ 用途：CoT 数据生产管线（用于模型后训练）

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

## 重构目标（待进行）

1. **更健壮**：修复老代码缺陷（见下方问题清单），消除隐性 bug
2. **更简洁**：砍掉冗余抽象和重复代码，控制整体规模
3. **可测试**：用 **mock API 接口**完成指标测试（成功率、限流、重试、断点恢复等），不依赖真实模型服务

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
cat oldCode/CoTBuilder-V2.py   # 参考实现
```
