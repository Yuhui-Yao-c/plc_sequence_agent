---
name: plc-action-description
description: 从归属明确的起始步原注释生成 A 档，依据当前批次和本步控制证据填写并核验 B 档中文描述。
compatibility: opencode
---

# 动作描述

## 触发

入口四 Trace 生成 `batches_desc/*.input.json` 后、写入结果和合并前加载。存在待处理 B 档批次时由 Agent 逐批处理。

## 输入

| 入口 | 当前输入 | 使用协议 |
| --- | --- | --- |
| 四 | 观测对、`trace_source_evidence.json`、A 档和批次上下文 | Trace `trace-description-context-v4` |

本环节使用本轮源码和观测证据。GT、人工表、历史结果和后加监控不得作为描述依据。

## 执行

### Trace 协议

#### 注释与观测证据

- 有唯一适用的起始步注释就填 A 档，按完整状态变量及数组下标对齐。观测跨步时照填已知注释，同时保留迁移复核。
- PLCopen 通过 LD/FBD 接线、FB 实例具名实参和 ST 分支定位本步。内嵌 `EXECUTE` 同时保留外层使能和内部写入条件。步注释、条件写入和迁移分别保存。有明确门控和类型的算术回写按源码求步值，FB 的运行时输出仍需复核。
- 沿原始控制链核对注释。跨多步注释只选能归属本步的原文片段。多个适用原文冲突时交复核。全局数组按完整通道及实际写入 POU 对齐。`trace_source_evidence.json` 提供 XML 位置、实例绑定、写步表达式和待核原因。
- 排除后加的测量触发、工程计时、监控赋值、统计、显示文本和历史候选注释。只从当前工程的源码、变量文档和控制证据取义。
- 逐批处理所有缺 A 档的行。检查 `context.evidence` 中的本步调用、实参、条件写入、源码前驱后继、对象绑定、中文映射及本轮从源码确认的同工程释义。核对否定、作用域和分支。
- 步号不符的注释、邻步注释及静态路径是上下文。命名族 `EM01.Cy_Up1` 的已知类别可辅助理解本步的 `EM01.Cy_Up2`。方向、用途、位置及反馈需要本步独立证据。

#### B 档结果

1. 逐个核对 `context.required_actions`，提交每个锚点的 `action_ref`。锚点保留已绑定对象、指令和已知目标。并行对象分别覆盖。有相反方向时查看实参和门控，区分条件请求与现场执行。
2. 通用分句填写 `subject`、`action`、`source_ref` 和 `subject_symbol`。`source_ref` 指向本步写入、已定位的指令、已解析的控制条件或适用注释。`subject_symbol` 填该证据中的完整 PLC 对象或条件符号。注释已明确包含对象和动作时可省略该字段。名称示例和相邻步可以写入 `evidence_refs` 作为辅助引用。
   证据提供 `subjects` 时，从符合当前分支的同一证据项选择 `symbol`。引用 `step_condition` 时填写 `condition_use` 为 `wait` 或 `check`。`subject` 写对象，`action` 写具体判据。若所选对象带 `numeric_predicate`，省略 `action` 和 `target`，程序按源码渲染比较方向及数值。对于 `source_write`，检查 `write_value`、原始门控和实例实参。
3. 目标可省略。目标取自本步注释或同一调用的实参。引用实参时填 `target_parameter`。已由锚点覆盖的指令直接使用 `action_ref`。补充分句须有独立本步证据。核对指令、条件和反馈的区别，结合源逻辑解释工程术语。
4. 批次原样回传 `run_id` 和 `batch_id`。各行保留当前 `basis`、`kind` 和 `reasoning`。使用中文工艺术语，保留已证实的细节。工作流从通过的分句合成交付描述。
5. 若证据不足，回传 `ok=false` 及具体原因。只写已证实的对象、具体动作和已知目标。孤立的对象名或通用过渡词不能充当完整描述。

```json
{
  "run_id": "复制本批输入的run_id",
  "batch_id": 1,
  "descs": {
    "开始标签|||结束标签": {
      "basis": "TB4…",
      "ok": true,
      "kind": "机械动作",
      "actions": [{"action_ref": "TX定位指令…"}],
      "reasoning": "本步调用绑定升降电机，位置实参为5。目标是5号位，机械到位仍待核。"
    }
  }
}
```

通用分句示例

`{"subject":"氮化硅靶","action":"磁控溅射镀膜指令下发","source_ref":"TX实际写入…","subject_symbol":"Cell.靶位2","target":"，膜厚设定25.5纳米","target_parameter":"FilmNm"}`

证据不足示例

`{"basis":"TB4…","ok":false,"reason":"本步只有采样状态值，源码未定位到相应调用或控制条件"}`

### 批次合并

1. `cmd_a` 写 `action_desc_a.csv`，`cmd_build` 写 `batches_desc/*.input.json`。全部行已有 A 档时可以没有 B 档批次。
2. 读取当前批次，逐行写对应的 `batches_desc/*.result.json`，原样回传 `run_id` 和 `batch_id`。Trace 逐行回传 `basis`。
3. Trace 使用相同参数和输出目录加 `--resume`。`cmd_validate` 合并可用 A/B 档到 `action_desc_final.csv`。旧 v1–v3 结果按相应协议复核，当前 v4 批次填写 v4 结果。

## 验收

- 检查 `action_desc_final.csv`、`action_desc_review.csv` 和 `workflow_status.json`。Trace 校验本轮证据、对象绑定、已知目标及分句。
- Trace 缺失、过期或无效结果保持待处理。具体的证据不足结果归入描述复核。
- Trace A 档保留可归属原文，备注可补充例程和本步条件。B 档保留可追溯的推断引用并标「LLM」。未证实的工艺方向、机械执行和动作完成状态留待核验。

## 协作

[Trace 跳变](../plc-trace-jump-analysis/SKILL.md) 提供观测对和时长。[工位身份](../plc-station-identity/SKILL.md) 提供分组名称。描述使用既有起止标签。
