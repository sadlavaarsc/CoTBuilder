# 原版 RobustJSONComparator 兼容性对照

> 创建日期：2026-07-28 ｜ 背景：原验收判定器代码缺失期间，matcher 按 audit-02
> 规格重写；2026-07-28 拿到原模块 `oldCode/RobustJSONComparator.py`（只读）后
> 完成功能对齐。本文是新旧两实现的行为对照与差异文档。
> 决策（用户拍板）：**验收语义以原版功能为准，与 audit 违背的功能做成开关、
> 默认关闭；missing_fields ×2 bug 修正并文档化**。

## 1. 总体结论

- **结果 schema 已完全对齐**：`comparison_result` 是原版 `compare()` 返回结构的
  超集，下游按原 schema 的任意键读取都不会出错（`tests/test_comparator_compat.py`
  有键集与类型断言）；
- **默认行为 = audit-02 规格**（与重构落地时一致，零变化）；
- **`Matcher.legacy()` / CLI `--legacy-matcher` ≈ 原版行为**：全部宽松规则打开，
  用于历史数据对账与旧系统判定复现；
- 原版存在 4 个 bug/不自洽行为（§4），均**修正并文档化**，不复刻。

## 2. 归一化规则逐项对照

| 规则 | 原版（默认配置） | 新实现默认 | 开关（legacy 全开） |
|---|---|---|---|
| 全角字母/数字→半角 | ✓ | ✓（含全角符号区） | 始终开启 |
| 全角空格 U+3000 | ✗ | ✓ | 始终开启 |
| 中文标点→英文 | ✓（含《》／） | ✓（已补齐《》／） | 始终开启 |
| ￥→¥ 宽度变体 | ✓ | ✓ | 始终开启 |
| 标点相邻空格删除 | ✗ | ✓（audit R-N2） | `normalize_punct_adjacent_spaces`（默认 True，legacy 关） |
| 大小写不敏感 | ✓（lower） | ✗（audit R-N3） | `case_insensitive` |
| 内部多空格折叠 | ✓ | ✗ | `collapse_internal_spaces` |
| 数字分组逗号删除 | ✓（1,234→1234） | ✗（audit R-N3） | `normalize_numeric_commas` |
| €→EUR、£→GBP | ✓（但见 §4.2，默认配置下不自洽） | ✗ | `unify_currency_extended` |
| 货币符号 ¥ 保留 | ✓ | ✓ | — |
| NFKC | 未使用 | 禁用（防 ™→TM 误折叠） | — |

## 3. 判定语义逐项对照

| 维度 | 原版 | 新实现默认 | legacy |
|---|---|---|---|
| 比较粒度 | 顶层字段，嵌套 str(dict) 整体比 | 递归逐叶子（数组按位） | 同默认（§4.3） |
| 类型敏感 | ✗（str() 比较，100=="100"） | ✓（数值跨类型同值算 STRICT） | `type_insensitive` |
| pred 空字段 | 丢弃不计（trim_empty_fields） | 计入 KEY_MISSING/MISMATCH | `trim_empty_fields` |
| 验收口径 | is_match = 无 mismatch/missing/extra | STRICT 或 NORMALIZED_MATCH | 同默认 |
| 选优 | match_score | rank_key（级别+匹配字段数） | 同默认 |
| 嵌套全角值 | 不归一化（§4.3） | 逐叶子归一化 | 同默认 |

## 4. 原版 bug / 不自洽行为（已修正并文档化，不复刻）

### 4.1 missing_fields 多算一倍

`RobustJSONComparator.py:358-359` 对同一缺失字段连加两次（代码内注释
「多一个计数，简化统计」自认 hack）。新实现输出真实计数；与原版数据对账时
按「原版值 ÷ 2」换算（compat 测试的断言口径即如此）。

### 4.2 €→EUR 在默认配置下不自洽

原版归一化顺序为 **lower 先于货币映射**：`"€5" → lower → "€5" → 货币 →
"EUR5"`，而 `"EUR5" → lower → "eur5"` —— 两者永远不等。即 €→EUR 映射
只有在 `case_insensitive=False` 时才生效，默认配置下是死代码。
legacy 模式复刻了同样的顺序（`matcher.py` 有注释标注）。

### 4.3 嵌套结构 str(dict) 比较

原版不递归：`normalize_value` 对 dict/list 原样返回，比较时 `str(dict)`，
导致 ① 键序不同即误判不一致；② 嵌套值不归一化（全角嵌套值判不一致）。
新实现递归逐叶子比较并归一化，属修正而非差异对齐。

### 4.4 None 的曲折处理

- pred 为 None：被 `trim_empty_fields` 丢弃 → 记 missing（判不一致）；
- gt 为 None、pred 为 `"None"`：`normalize_value(None)` 原样返回后经
  `str()` 得 `"None"`，而 pred 被 lower 成 `"none"` → 判不一致。
两个方向都不一致，但机制完全不同且都非有意设计。新实现：类型敏感，
None 只与 None 相等（`str(None)=="None"` 假阳性已防）。

## 5. 差异案例速查（compat 测试同源）

| 案例 | 原版默认 | 新实现默认 | 说明 |
|---|---|---|---|
| `LAU` vs `Lau` | 一致 | **不一致** | audit R-N3：大小写是真实错误 |
| `1,000` vs `1000` | 一致 | **不一致** | audit R-N3：数字格式不等价 |
| `LAU, LAI` vs `LAU,LAI` | 不一致 | **一致** | audit R-N2：标点后空格是噪声 |
| `LAI  LI` vs `LAI LI` | 一致 | **不一致** | 内部空格承载语义 |
| `100` vs `"100"` | 一致 | **不一致** | 类型敏感 |
| `€5` vs `EUR5` | 不一致（§4.2） | 不一致 | 两边都不一致，理由不同 |
| `{"d":{"a":"１"}}` vs `{"d":{"a":"1"}}` | 不一致（§4.3） | **一致** | 嵌套递归归一化 |

## 6. legacy 模式用法（历史数据对账）

```python
from cotbuilder.matcher import Matcher
verdict = Matcher.legacy().compare(pred_dict, gt_dict)
# 与原版对账口径：missing_fields = 原版值 ÷ 2（§4.1）
```

```bash
python -m cotbuilder.cli --input samples.json --output out/ \
    --api-key <key> --legacy-matcher
```

对账建议：同一批历史样本分别用默认与 legacy 跑验收，diff `is_match`
不同的样本即为两口径的边界案例，可结合 `differences` 明细定位差异来源
（对照 §5 速查表归因）。

## 7. schema 对照

原版返回 10 个键，新实现全部保留并追加诊断字段：

| 原版键 | 新实现口径 |
|---|---|
| `is_match` | = is_accepted（STRICT 或 NORMALIZED_MATCH） |
| `match_score` | matched / (matched + mismatched)，同原版公式 |
| `is_partial` | !is_match && score ≥ 0.95，同原版 |
| `total_fields` | GT 叶子字段数（原版顶层口径的递归推广） |
| `matched_fields` | STRICT + NORMALIZED 叶子数 |
| `mismatched_fields` / `extra_fields` / `missing_fields` | 按叶子级 FieldVerdict 计数（missing 已修正 ×2） |
| `differences` | 同原版结构（mismatch 项含 gt_normalized / pred_normalized） |
| `error` | 恒 None（解析失败在 generator 层先行处理） |
| 新增：`match_level` / `matched_field_count` / `total_field_count` / `fields` | 三级判定与逐字段明细（含 STRICT/NORMALIZED） |

---

*关联文档：[audit-02](audit-02-robust-matching.md)（需求规格来源）、
[design.md](design.md)（整体设计）、[test-results.md](test-results.md)（测试档案）*
