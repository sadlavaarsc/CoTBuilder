# CoTBuilder

> CoT（Chain-of-Thought）数据生产管线：用多模态专家模型对文档图片做关键信息提取，
> 生成**带推理链的 CoT 数据**，与 ground truth 规则比对 + 模型 judge 改判后筛出匹配样本，
> 组装为 ShareGPT 格式训练数据（用于 4B 模型后训练）。

## 它做什么

```
samples.json（图片 + prompt + ground truth）
  │  ① cli      多模态请求（QPM 限流 / 并发 / 指数退避重试，寿命模型双账）
  ▼
run/ ──► ② judge   把规则判负的 diff KV 发给模型改判（保守翻转）
  │      ③ merge   judge 结果并回 run 数据（标签可反复 judge）
  ▼      ④ convert 转 ShareGPT：<think>/<answer> 双标签 + /think 软开关
train.json（训练框架直接读）
```

- **生成层**：样本内串行、跨样本并发；MISMATCH 消耗样本寿命（默认 3 次质量重试），
  网络/限流错误消耗网络寿命（默认 5 次退避重试），两本账独立——403 风暴不伤通过率；
- **判定层**：规则比对（STRICT 口径）为主，model judge 可选后处理兜底（只救规则冤案，
  不改判据）；
- **组装层**：merge / combine / convert 全为纯离线工具，随便重跑不触网。

## Quick Start

```bash
# 环境（uv 或 venv 均可；pip 源建议清华镜像）
uv venv .venv && .venv/bin/pip install aiohttp pytest pytest-asyncio

# 跑测试（全部 mock，不触真实 API；253 项 + 2 项 slow 冒烟）
.venv/bin/python -m pytest tests/ -q

# 生产闭环（推荐参数，详见 doc/workflow.md）
python -m cotbuilder.cli --input samples.json --output run1/ \
    --api-key <key> --qpm-limit 40 --max-concurrent 25
python -m cotbuilder.judge --input run1/ --output judge1/ --api-key <key> --qpm-limit 40
python -m cotbuilder.merge --run run1/ --judge judge1/ --output merged1/
python -m cotbuilder.convert --input merged1/ --output train.json \
    --include-failed upheld --mix-ratio 1.0
```

输入样本格式（`messages` / `conversations` 两种，JSON 数组）见
[doc/formats.md §1](doc/formats.md)；数据切分是上游职责，不在本项目范围。

## 文档索引

| 文档 | 内容 |
|---|---|
| [doc/workflow.md](doc/workflow.md) | **闭环流程说明**：四步命令 + 推荐参数 + 每步验收检查点（先看这个） |
| [doc/formats.md](doc/formats.md) | 全部 JSON 格式参考（输入 / run / judge / merge / ShareGPT / 辅助文件） |
| [doc/design.md](doc/design.md) | 设计文档：架构、并发/限流决策、**§5 防误解清单**（改代码前必读） |
| [doc/investigation-01-e2e-diagnosis.md](doc/investigation-01-e2e-diagnosis.md) | 30 样本 e2e 实测诊断（死循环根因、403 骑线振荡机理） |
| [doc/audit-01-concurrency.md](doc/audit-01-concurrency.md) / [audit-02](doc/audit-02-robust-matching.md) | 老代码审计（重构动机） |
| [doc/comparator-compat.md](doc/comparator-compat.md) | 新 matcher 与原版 RobustJSONComparator 逐项对照（改 matcher 前必读） |
| [doc/test-results.md](doc/test-results.md) | 测试档案：实测指标与复现方法 |
| [CLAUDE.md](CLAUDE.md) | 项目状态与开发约定 |

## 代码结构

```
cotbuilder/   新代码包（单模块单职责）
  config / ratelimit / client / extractor / matcher /
  generator / writer / metrics / batch / cli        —— 主流程
  judge / polish                                    —— 触网后处理（改判 / 润色修复）
  merge / convert / combine                         —— 纯离线工具链
mock/         可配置 mock API（正常/网络错/403/空响应/超长尾延迟…）
tests/        286 项指标测试，全部走 mock
oldCode/      老版本参考实现（只读，不要修改）
```

## 开发约定

- 测试一律走 mock，不调用真实 API；性能指标必须实测得出，不编造；
- `oldCode/` 只读；核心管线语义不变（编码 → 请求 → 提取 → 比对 → 重试 → 落盘）；
- 改并发/限流/匹配前必读 [doc/design.md §5](doc/design.md) 防误解清单。
