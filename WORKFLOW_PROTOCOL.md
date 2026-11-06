# OpenCode 执行协议 v9.25.1

本协议整理 v9.25 已有能力的调用和完成条件。Trace 处理通过 `trace_to_pairs.py` 统一分流，支持 TwinCAT .trace、Sysmac 表和同次 Sysmac 分卷目录。根 AGENTS.md 与各领域 Skill 的证据和隔离要求全部适用。

## 输入路由

| 用户输入 | 执行路径 |
|---|---|
| PLC 源码路径、变量模式、可选 Trace、可选人工表 | predict → status → 两类批次 → complete |
| 已有运行目录、继续/检查进度 | status；仅继续该清单尚未完成的批次 |
| 冻结预测 CSV、人工 xls/xlsx | coverage；只读预测和人工表 |
| 冻结预测 CSV、GT 文本表，可选源码/迁移图 | evaluate；严格单对匹配与分支诊断 |
| 查询已有标签/后继 | diagnose_labels.py / feedback_successor.py；只读查询，不补预测 |
| 重新显示已有交付表 | gen_workstation_xlsx.py；保留原预测与已有描述 |

路径包含空格时分别加引号。自然语言输入由 Agent 解释为参数，不将四个位置当作源码字符串。保留用户给定的变量模式；省略模式表示全量范围，不擅自缩窄。复用目录前检查清单中的 source、pattern、trace、manual 是否与本次意图相同；输入改变时使用新运行目录。不要用 --force 掩盖新旧任务混用。

业务上保持预测与只读评价两个独立模式；续作、查询和重渲染是已有任务的操作。Trace 与人工表分别可选。--manual 在 predict 中只登记路径，准备和模型批次不读取其内容；complete 先冻结最终预测，再自动衔接独立的 coverage 阶段。评价不得回流候选、边界或描述。人工 Excel 与 GT 文本分别采用 coverage 和 evaluate，不混用分数。

## 指引分工与能力加载

- `.opencode/agents/plc-sequence.md`：主 Agent 的能力索引、路由和执行闭环。
- `AGENTS.md`：当前两个模式、全局证据与交付要求；仍适用的历史规则已合并，版本演进查 CHANGELOG 和 Git 历史。
- `AGENT_PROMPT.md`：工艺判断、证据引用、事件处理和 B 档逐行描述协议。
- 本文件：阶段顺序、两类结果协议、断点续作与完成定义。
- `plc_cli.py status`：根据实际清单给出进度、错误、下一项输入及其 SHA256。

预测必须加载边界规范化、信号收尾、跨 FB、反馈后继四个 Skill；预测后处理工位身份时加载 plc-station-identity。提供 Trace 时加载 plc-trace-jump-analysis；提供人工 Excel 时加载 plc-coverage-evaluation。共七项均保留。Skill 工具不可用时直接读取同名 `.opencode/skills/<name>/SKILL.md`，如实报告工具加载限制。

## 阶段与状态

1. **准备**：运行 `python plc_cli.py predict --source "..." --pattern "..." --out "..." [--trace "..."] [--manual "..."]`。原执行顺序和全部原产物保留；新增 workflow_execution.json 记录源码、准备证据和两类批次输入指纹。准备阶段不读取人工表内容。
2. **获取工作**：运行 `python plc_cli.py status --run "..."`，读取 required_skills 与 next_work。`--all` 列出所有待处理批次。status 是只读检查，不生成描述、不改写批次、源码、预测或交付物。
3. **工艺批次**：逐批读取 `batches/*.input.json` 及 AGENT_PROMPT.md，作实际工艺判断。输出必须引用本批候选/事件；每个事件进入 steps 或 unresolved_events。保留缺证据的明确原因，不以数量控制筛选。
4. **描述批次**：逐批读取 `batches_desc/*.input.json`，优先源码注释和具体机构文档，用一句话描述主要动作。每个输入行必须给出描述或明确不可描述原因。不要调用历史 gen_*/write_* 规则脚本冒充模型处理。
5. **完成**：两类批次就绪后运行 `python plc_cli.py complete --run "..."`。它调用原 finish 引擎，再核查原子表、完整归档、lineage、条件、review、描述文件、交付 Excel，以及用户请求的 Trace/coverage。
6. **报告**：只有 COMPLETED 或 COMPLETED_WITH_REVIEW 才称已完成编排。后者必须同时报告边界、事件、工艺和描述复核项；完成不等于所有真实动作已证实。

| 状态 | 含义与下一步 |
|---|---|
| WAITING_FOR_BATCHES | 两类批次尚未齐全，按 next_work 继续 |
| INVALID_INPUTS / INVALID_RESULTS | 输入、证据或结果协议不一致；保留现场，定位具体文件再处理 |
| READY_TO_FINISH | 两类结果已齐全且通过前置核验，可 complete |
| DELIVERY_INCOMPLETE | 原组装或请求的交付能力未成功，报告错误与已有产物 |
| COMPLETED_WITH_REVIEW | 交付齐全且保留明确复核事项 |
| COMPLETED | 交付齐全，当前记录中没有上述复核事项；仍非现场物理完成证明 |

`finish` 作为 v9.25 兼容入口保留，行为不收紧。它的 success 是原工艺组装状态，不能代替新版完整编排的 completed。旧运行目录缺少执行指纹时可兼容核验，status 明示 legacy_unsealed，不追认历史来源。

## 工艺结果协议

沿用运行目录 RESULT_SCHEMA.md 的所有工艺字段，在新指纹运行中另填 input_sha256。batch_id 和指纹取自 status 的该条 work order。

```json
{
  "batch_id": "B00001",
  "input_sha256": "该批输入文件的SHA256",
  "steps": [],
  "unresolved_events": [],
  "notes": "本批判断与限制"
}
```

steps 的每条记录仍填写过程类型、统计对象、起点含义、终点含义、工艺依据、边界依据、起止标签和证据 ID。不能删除 AND、选择性遗漏 OR、按数值邻接推断后继，或将反馈条件替代声称为布尔/时间等价。新的边界需先建立有来源的候选，不能随意拼接。

## 描述结果协议

```json
{
  "batch_id": 1,
  "input_sha256": "该批输入文件的SHA256",
  "descs": {
    "0": {"ok": true, "desc": "等待上料许可", "refs": [], "kind": "工艺等待"},
    "1": {"ok": false, "reason": "缺少可支持动作方向的源码文档"}
  }
}
```

descs 的键必须与本批 rows.id 一一对应，不能漏行、重复跨批或增加其他行。refs 只能引用该行 tokens.tok。遵循输入中的中文、长度和主要动作指引；现有 cmd_validate 继续执行原词汇校验，拒绝项仍进入 action_desc_review.csv，A 档和原候选保留。

指纹只绑定输入与结果，不能证明结果由模型生成。禁止仅给旧结果补指纹来绕过重新判断。确实处理了当前输入后才写对应结果；单批原子写入，互不覆盖。可独立处理不同批次，汇总始终由同一入口核验。

## 保持的能力与解释边界

- 全量源码解析、名称过滤、候选冻结、非复合投影、原始归档及复核保全顺序不变。
- PLCopen、SMC2、欧姆龙原生适配器，以及 ST 双轨解析全部保留。
- OR 首出口、未知写入阻断、AND、BOOL 端点、跨 FB 协议和动作后收尾规则不变。
- A/B 描述通过独立文件关联 Excel，不改写原预测来提高描述或评价指标。
- TwinCAT 的 exact/via_zero/loose 是各自的运行匹配口径。Sysmac 以完整起止标签关联观测步序对，按起始步停留时长中位数回填并标记估算。
- coverage 的路径/写法/粒度覆盖与 evaluate 的严格单对口径分别报告，不混算。
- 交付表依旧读取 final_prediction_pairs.csv；原池含弱候选，不能将行数称为全部已证实真实动作。
- 词典、运行时、模型和权限模式均不因本次编排调整而替换。不得为稳定性删能力、少处理批次或伪造描述。
