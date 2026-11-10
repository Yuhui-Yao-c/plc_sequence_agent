---
name: plc-action-description
description: 核对 Trace 本轮源码证据，为缺起始步注释的状态对填写 B 档动作分句和证据引用，并续跑交付。
---

# Trace 动作描述

### 注释与观测证据

- 有唯一适用的起始步注释就填 A 档，按完整状态变量及数组下标对齐。观测跨步时照填已知注释，同时保留迁移复核。
- PLCopen 的证据包包含 XML 网络、FB 实例及内嵌 ST 写入。沿原始控制链核对注释，排除后加监控、统计、显示文本和旧候选注释。原文冲突时结合本步控制证据，保留复核原因。
- 逐批处理所有缺 A 档的行。检查 `context.evidence` 中的本步调用、实参、条件写入、源码前驱后继、对象绑定、中文映射及同工程记忆。核对否定、作用域和分支。
- 步号不符的注释、邻步注释及静态路径是上下文。命名族 `EM01.Cy_Up1` 的已知类别可辅助理解本步的 `EM01.Cy_Up2`。方向、用途、位置及反馈需要本步独立证据。

### B 档结果

1. 逐个核对 `context.required_actions`，提交每个锚点的 `action_ref`。锚点保留已绑定对象、指令和已知目标。并行对象分别覆盖。有相反方向时查看实参和门控，区分条件请求与现场执行。
2. 通用分句填写 `subject`、`action`、`source_ref` 和 `subject_symbol`。`source_ref` 指向本步写入、已定位的指令、已解析的控制条件或适用注释。`subject_symbol` 填该证据中的完整 PLC 对象或条件符号。注释已明确包含对象和动作时可省略该字段。名称示例和相邻步可以写入 `evidence_refs` 作为辅助引用。
   证据提供 `subjects` 时，从符合当前分支的同一证据项选择 `symbol`。引用 `step_condition` 时填写 `condition_use` 为 `wait` 或 `check`。`subject` 写对象，`action` 写具体判据。若所选对象带 `numeric_predicate`，省略 `action` 和 `target`，程序按源码渲染比较方向及数值。对于 `source_write`，检查 `write_value`、原始门控和实例实参。
3. 目标可省略。目标取自本步注释或同一调用的实参。引用实参时填 `target_parameter`。已由锚点覆盖的指令直接使用 `action_ref`。补充分句须有独立本步证据。核对指令、条件和反馈的区别，结合源逻辑解释工程术语。
4. 批次原样回传 `run_id` 和 `batch_id`。各行保留当前 `basis`、`kind` 和 `reasoning`。使用中文工艺术语，保留已证实的细节。工作流从通过的分句合成交付描述。
5. 若证据不足，回传 `ok=false` 及具体原因。避免以“夹子气缸”“设备动作”“步进过渡”充当完整描述。

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

通用分句示例。 `{"subject":"氮化硅靶","action":"磁控溅射镀膜指令下发","source_ref":"TX实际写入…","subject_symbol":"Cell.靶位2","target":"，膜厚设定25.5纳米","target_parameter":"FilmNm"}`

证据不足示例。 `{"basis":"TB4…","ok":false,"reason":"本步只有采样状态值，源码未定位到相应调用或控制条件"}`

### 校验与续跑

- 检查每个当前输入行的 `basis`、引用、动作对象及已知目标。通过项写入 `action_desc_final.csv`，保留依据和引用，并标为 LLM 推断待核。
- 当前批次按 v4 协议填写。缺失、过期及无效分句保持待处理。有效的证据不足结果归入描述复核。
- 写完结果后用相同参数加 `--resume`。准备产物通过指纹核验后，续跑直接合并并刷新 `<工序名称>_trace步序采集表.xlsx`。
