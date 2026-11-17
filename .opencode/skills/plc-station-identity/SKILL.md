---
name: plc-station-identity
description: 从本轮源码声明和变量文档建立工位身份，核对工位显示名、分组及排序。
compatibility: opencode
---

# 工位身份与展示

## 触发

入口一核验工位展示或排查工位名、分组与排序时加载。

## 输入

1. 源码准备产物 `prediction_candidate_pairs.csv` 的控制器及 Trace 的实际观测变量提供状态机身份。只用实际锚点，辅助结构不单独建立工位身份。
2. `variable_documentation.json` 的 `stations` 和 `all` 提供源码声明和文档。`stations` 是类型名含 `station` 的子集，`all` 提供全量回退。
3. Trace 还可根据本步控制证据定位 POU 或例程归属，结合完整变量通道展示。

## 执行

### 身份键
- **身份键 = 变量根完整名**（如 `udt_EM04`、`HJXHX_ST08`），不剥离命名空间前缀。
  `HJXHX_ST08`（铝焊线）和 `ST08`（滚筒线）是**不同工位**，剥离前缀会塌缩身份。
- **显示编号剥离**仅限 IEC61131-3 标准类型前缀（`udt_`/`st`/`St`/`ST` 后跟大写字母）；
  命名空间前缀（`HJXHX_`/`TJXHX_`/`AlWeld_`/`CuWeld_` 等）保留。
- **排序键**从变量根最后一个数字组提取 `[主号, 子号]`（仅排序用，不影响显示/身份）。

### 工位展示

1. 保留源码工位身份和完整步序变量的分组。逐对检查从起始步发出的直接迁移或候选路径首段。缺少对应迁移时，依次核对起始步内的写入、本步指令、选中的起始步原注。同级证据能唯一定位所属程序与例程时，以「工位标识-源码例程原名」展示，保留大小写、后缀和子工位号。嵌套功能块用调用者所属例程。Sysmac 使用程序及其区段。
2. 显示名有重复前缀时按确认的源码作用域和文档归属处理，保持分组身份。缺映射时兼容变量文档和默认排序。排序仅供查阅。
3. 同一通道内例程发生变化时，按步序原顺序分段。多个通道共用同一显示名时，在显示名下列出完整通道。缺乏唯一例程依据时沿用工位分组。分组合并格的批注列出完整通道和可确认的源码位置，下标按数值排序。
4. 无文档时保留原变量根供复核，避免按某个项目的前缀正则推断工位。

## 验收

检查本轮 `station_identity.json`：
```json
{
  "udt_EM04": {
    "display": "EM04_长边清洗工位",
    "code": "EM04",
    "doc": "长边清洗工位",
    "sort_key": [4, 0],
    "source": "variable_documentation",
    "needs_review": false
  }
}
```
- `candidate_audit.json` 增加 `station_identity` 审计段：total_var_roots / with_documentation / without_documentation / without_documentation_roots。
- 无文档的变量根 `needs_review=true`，`display=变量根本身`，在交付报告中保留待核提示。

## 协作

工位身份由源码证据建立。工作簿只读映射。主表的「构成要素」用于分组，动作描述按[动作描述](../plc-action-description/SKILL.md)处理。
