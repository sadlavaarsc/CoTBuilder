# CoTBuilder JSON 格式总览

> 创建于 2026-08-03 ｜ 用途：讲清系统现存全部 JSON 格式（输入 / 各阶段输出 / 辅助文件），
> 供下游消费（训练、可视化、再处理）与后续维护查阅。字段以代码为准，本文给出
> 产生方 / 消费方 / schema / 示例。

## 格式地图（数据流向）

```
输入样本 input.json ──► [cli 主流程] ──► run目录: success_samples.json + failed_samples.json
                                              │         (+ checkpoint.json / summary.json / metrics.jsonl)
                                              ▼
                                     [judge 改判] ──► judge目录: 同名两文件 + judge_summary.json
                                              │                （记录带 judge_result 标签块）
                                              ▼
                                     [merge 合并] ──► merged目录: 同名两文件 + merge_summary.json
                                              │
                                              ▼
                                     [convert 转换] ──► train.json / train.jsonl（ShareGPT）
                                                          (+ convert_summary.json)
```

judge / merge / combine / convert 均为**独立可选工具**，不进主流程；
merge/combine/convert 纯离线只读源目录。

**combine（多路径哑合并）**：`python -m cotbuilder.combine --inputs
<路径...> --output <目录>`——把任意多个 run/judge 目录或单文件按
**(输入路径, sample_id)** 分键去重（路径内后赢；跨路径同 id 全部
保留——sample_id 仅保证单 run 内唯一）拼接成标准两文件目录 +
combine_summary.json；不解读记录内容，输出可直接喂 convert/judge。
与 merge 的分工见 design.md §6f。

---

## 1. 输入样本（cli --input，JSON 数组）

两种格式等价，逐样本二选一（generator.py 优先读 `messages`）：

```json
[
  {
    "id": "sample_0",                          // 可选；缺省按位置 sample_{i}
    "messages": [
      {"role": "user",      "content": "<image>\n请提取图片中的关键信息"},
      {"role": "assistant", "content": "{\"发票号码\": \"J123\", ...}"}
    ],
    "images": ["/abs/path/invoice_0.jpg"]      // 本地路径列表，仅用 images[0]
  },
  {
    "id": "sample_1",
    "conversations": [
      {"from": "human", "value": "<image>\n请提取图片中的关键信息"},
      {"from": "gpt",   "value": "{\"发票号码\": \"J123\", ...}"}
    ],
    "images": ["/abs/path/invoice_1.jpg"]
  }
]
```

- 第 2 轮（assistant/gpt）= Ground Truth JSON 字符串；
- `<image>` 占位符可省（generator 会 strip）；`images` 为空时退化为纯文本请求；
- 消费方：`cotbuilder.cli`（`load_samples`，单文件 JSON 数组，非 JSONL）。

## 2. run 输出记录（success_samples.json / failed_samples.json）

JSON 数组，每条记录字段超集（generator._build_result，writer 原样落盘）：

| 字段 | 必现 | 含义 |
|---|---|---|
| `sample_id` | ✓ | 样本 id |
| `status` | ✓ | `success` / `failed`（writer 按此路由文件） |
| `attempts` | ✓ | 真实 HTTP 请求数 |
| `original_sample` | ✓ | 输入样本原样（§1 整包） |
| `cot_response` | 条件 | 模型输出原文（`message.content`，含 CoT + JSON） |
| `full_api_response` | 条件 | 完整 API 响应体（reasoning_content 若服务端返回则在其中） |
| `predicted_json` | 条件 | 提取出的 JSON dict |
| `ground_truth` | 条件 | GT dict |
| `comparison_result` | 条件 | 比对明细（§3）；`robust_match` 为同对象冗余键 |
| `match_level` | 条件 | STRICT / NORMALIZED / MISMATCH |
| `error` / `error_type` | 条件 | 失败信息；error_type ∈ MISMATCH / NETWORK_ERROR / RATE_LIMITED / GATEWAY_ERROR / API_ERROR / EMPTY_RESPONSE / LENGTH_TRUNCATED / JSON_PARSE_ERROR |

**条件字段按路径**（消费时必须先判存在）：
- 成功 / MISMATCH 耗尽：字段全（含 cot_response、predicted_json、comparison_result）。
  **历史例外**：2026-08-04 之前产出的 MISMATCH 耗尽记录缺 `ground_truth`
  （generator best 分支漏传，已修复；convert 端有 original_sample 回退兜底）；
- JSON 提取失败：有 cot_response，**无** predicted_json/comparison_result；
- 纯网络失败（network_exhausted 等）：**只有** error/error_type（+ground_truth），
  无 cot_response/predicted_json——**这正是 convert 默认只读 success 文件的原因**；
- 终态 API 失败（EMPTY/LENGTH_TRUNCATED）：有 full_api_response，无 cot_response。

## 3. comparison_result（比对明细，judge 的数据源）

```json
{
  "is_match": false, "match_score": 0.75, "is_partial": true,
  "total_fields": 4, "matched_fields": 3,
  "mismatched_fields": 1, "extra_fields": 0, "missing_fields": 0,
  "match_level": "MISMATCH",
  "differences": [
    {"field": "发票号码", "type": "mismatch",
     "ground_truth": "J123", "predicted": "J-123",
     "gt_normalized": "...", "pred_normalized": "..."}
  ],
  "fields": [{"field": "...", "level": "STRICT", "pred_value": "...", "gt_value": "..."}]
}
```

- `differences[].type` ∈ `mismatch` / `missing` / `extra`（normalized 键仅 mismatch 项有）；
- 消费方：judge（只取 differences 三元组改判）、summary 的 gt_analysis/quality 块。

## 4. judge 输出（judge 目录，同名两文件 + judge_summary.json）

记录 = §2 原记录浅拷贝 + status 可能翻转 + **judge_result 标签块**：

```json
{
  "...": "§2 原字段全部保留（comparison_result 等不动）",
  "status": "success",
  "judge_result": {
    "overturned": true,
    "pairs": [{"field": "发票号码", "predicted": "J-123", "ground_truth": "J123"}],
    "attempts": 1,
    "verdicts": [{"field": "发票号码", "match": true, "reason": "连字符无实义"}],
    "content": "<judge 模型原始输出>",
    "error": "...", "failure": "judge_parse_failed | network_exhausted | terminal_error"
  }
}
```

- `verdicts`/`content` 仅在拿到模型响应时存在；`error`/`failure` 仅失败时存在；
- 路由：overturned → success_samples.json；其余 → failed_samples.json；
- judge_summary.json：total_records/judged/overturned/upheld/各失败分桶/
  skipped_no_differences/skipped_resume/overturn_rate + config + metrics。

## 5. 合并输出（merged 目录，同名两文件 + merge_summary.json）

格式与 §2/§4 完全一致（merge 不改任何字段），语义：

- merged success = run success（**无标签**）+ judge 改判成功（**有 judge_result**）；
- merged failed = judge 维持原判/判失败（**有 judge_result**）+ 未覆盖 run failed（无标签）；
- **标签判读规则**：`"judge_result" in record` ⟺ 该记录被 judge 判过；
  `judge_result.overturned` ⟺ 被改判成功。下游可视化/过滤只看这个键；
- merge_summary.json：run_success/run_failed/judged_overturned/judged_upheld/
  judged_error/untouched_failed/orphaned/collision/final_success/final_failed
  + duplicate_run_failed_ids/duplicate_judged_ids（2026-08-04 新增：
  重复 sample_id 防御性计数，正常应均为 0）；
- **反复 judge**：merged 目录可直接作为 judge 的 `--input` 再判一轮，
  再 merge 叠加（新 judge_result 覆盖旧块）。

## 6. ShareGPT 训练数据（convert 输出，train.json / train.jsonl）

```json
[
  {
    "conversations": [
      {"from": "human", "value": "<image>\n请提取图片中的关键信息\n/think"},
      {"from": "gpt",   "value": "<think>先看发票号码，再看总价。</think>\n<answer>{\n  \"发票号码\": \"J123\",\n  \"总价\": \"¥5.83\"\n}</answer>"}
    ],
    "images": ["/abs/path/invoice_0.jpg"]
  }
]
```

- 容器：**默认 JSON 数组**（`--format json`，对齐 13 万条老数据微调输入）；
  `--format jsonl` = 每行一个样本；
- **gpt 轮 = Qwen3 式双标签**（2026-08-03 用户拍板）：答案统一
  `<answer>{JSON}</answer>` 包裹（三种来源同口径，vLLM 等下游一条规则
  即可切出 answer 段），有推理链时前置 `<think>推理链</think>`；
  标签名 `--think-tag` / `--answer-tag` 可覆盖；
- gpt 轮三种 `--gpt-mode`：
  | mode | gpt value |
  |---|---|
  | `thinking`（默认） | `<think>推理链</think>` + `<answer>{JSON}</answer>`；推理链取 `reasoning_content` → 回退 `cot_response` 剥离 JSON；为空则仅 `<answer>` |
  | `raw` | `cot_response` 原文不动（**不参与双标签提取契约**；`--strip-fences` 可删 ```json 围栏） |
  | `json` | 纯 `<answer>{JSON}</answer>`（无推理） |
- **thinking 软开关标志**（2026-08-03 追加）：human 轮末尾按 gpt 轮**实际
  是否含推理**自动加 `/think`（thinking 模式有推理链、raw 模式）或
  `/no_think`（json 模式、推理链为空）；`--think-flag` / `--no-think-flag`
  自定义文案，空串禁用；已有旧标志行会被剥掉后重加（不重复不矛盾）；
- **无 CoT 预算与填充优先级**（2026-08-03 追加）：`--mix-ratio R` 定义无
  CoT 总条数 = `int(R × CoT 条数)`；**failed 派生优先填充、填不满由
  `--mix` 外部数据补齐**，排布顺序 CoT → failed 派生 → mix 混入
  （抽样固定种子 42 可复现）；
- **failed 派生入训**（`--include-failed SOURCE`）：把 failed_samples.json
  中符合条件的记录转为 `/no_think` + **GT 答案**的硬样本。档位：
  `upheld`（默认，judge 维持原判=规则+模型双重确认）/ `mismatch`
  （有规则 diff 证据）/ `all`（任何带 GT dict 的记录，无 GT 的终态
  失败跳过）。**failed 的 cot_response/predicted_json 永不作训练目标**
  （错误产物，拼「错误推理+正确答案」是负样本）。GT 优先取记录的
  `ground_truth` 字段，缺失时**回退从 original_sample 提取**
  （2026-08-04 新增：救修复前产出的缺 GT 旧记录；救回条数计入
  convert_summary 的 `failed_gt_recovered`）；
- **外部无 CoT 混入**（`--mix <shareGPT文件>`）：.json/.jsonl 均可，每条
  混入样本自动剥 `<think>`/`<thinking>` 推理段与已有 `<answer>` 包装、
  统一重包 `<answer>`（不双包）+ human 末尾加 `/no_think`；
- human 轮 = original_sample 的 prompt；`<image>` 缺失时自动前置（images 非空）；
  `images` 原样透传（本地路径列表，训练框架自行加载）；
- 缺必需素材（predicted_json / cot_response）的记录跳过，计入
  convert_summary.json 的 `skipped`；
- 扩展说明：Alpaca / OpenAI messages 微调格式未实现——如需新增，在
  convert.py 加一个 gpt-mode 式的输出分支即可，schema 集中在这一个文件。

## 7. 辅助文件速查

| 文件 | 产生方 | 内容 | 详见 |
|---|---|---|---|
| `checkpoint.json` | writer | `{timestamp, processed_ids}`，断点恢复（删了可从结果文件自愈重建） | design.md §3 |
| `summary.json` | batch | 计数 + 完整 config + metrics（quota/outcomes/token_usage/performance）+ gt_analysis + quality | design.md §6 |
| `judge_summary.json` | judge | 改判计数 + overturn_rate + config + metrics | design.md §6c |
| `merge_summary.json` | merge | 合并计数对账 | §5 本文 |
| `combine_summary.json` | combine | 多路径拼接计数（total_in/deduped[路径内]/cross_path_id_collisions/final_*） | design.md §6f |
| `convert_summary.json` | convert | 转换计数 + mode/format（含 failed_used/failed_gt_recovered/mixed_in） | §6 本文 |
| `metrics.jsonl` | metrics | 逐请求事件流（ts/sample_id/kind/quota_kind/wait_limiter/wait_slot/rtt/backoff） | design.md §6b |

---

*关联文档：[design.md](design.md)（设计决策与防误解清单 §5）、
[comparator-compat.md](comparator-compat.md)（comparison_result 与原版对照）*
