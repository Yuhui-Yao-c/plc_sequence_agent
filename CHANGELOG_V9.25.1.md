# v9.25.1 — OpenCode 编排与交付核验

日期：2026-11-04。基线：`ecf88acdf4aeabb76668e8f09d96e2bbe8df1a0e`，功能引擎继续为 v9.25。

## 问题与影响

Python 引擎已演进到 v9.25，主 Agent、README 和 VERSION 仍主要介绍 v9.13；根 AGENTS.md 的后续规则不足以替代清楚的调用路线。源码解析能力本身仍在，但模型可能漏用工位身份、A/B 描述、Trace 或覆盖评价，或者按旧入口过早报告完成。

三个已有领域 Skill 缺少 name/description 头部，影响依赖该元数据的运行时发现；opencode.json 仅显式允许两项，其余项受运行时默认策略影响，不能直接认定原来全部被禁用。

原 finish 的 success 主要表示工艺批次组装成功。B 档描述未齐、Excel/Trace/coverage 报错时，部分失败被记录在附属状态中，顶层 success 仍可能为真。旧结果也未绑定批次输入字节，复用目录时难以发现混入的历史结果。

## 当前编排

主 Agent 负责调用和工艺判断，七个领域 Skill 提供现有证据规则，确定性控制器核查执行进度和交付条件。默认顺序为：

`predict → status → 全部工艺批次 + 全部 B 档描述批次 → complete`

- 同步主 Agent、AGENTS.md、AGENT_PROMPT.md、README 和 VERSION，新增 WORKFLOW_PROTOCOL.md。保留历史规则正文及全部已有边界能力。
- 为工位身份、Trace、覆盖评价三个 Skill 补元数据；七项已有 Skill 均显式 allow。保持原权限模式、模型和运行时配置格式。
- predict 仅新增 workflow_execution.json，记录源码、可选 Trace、准备证据及两类批次输入的 SHA256。准备与状态检查均不读取人工表内容。
- status 只读返回两类进度、下一批输入、指纹和错误。检查批次 ID、输入变化、证据变化、漏行、越批引用及多余旧结果。新结果必须回填处理过的当前输入指纹；指纹不证明结果由模型生成。
- complete 先检查两类批次，调用原 finish 引擎，再检查原子表、完整归档、证据关联、复核、描述、Excel 及用户请求的 Trace/coverage。组装期间结果变化也不确认完成。
- 输出区分 WAITING_FOR_BATCHES、INVALID_RESULTS、DELIVERY_INCOMPLETE、COMPLETED_WITH_REVIEW 与 COMPLETED。缺证据仍可明确进入复核；交付齐全不等于全部物理动作已证实。
- finish、evaluate、coverage 兼容保留；旧目录可标记 legacy_unsealed 继续核验，不追认历史输入绑定。补充现有 .xls 评价器所需的 xlrd 依赖。

## 能力保留范围

| 既有能力 | 本次处理 |
|---|---|
| PLCopen、SMC2、欧姆龙原生适配；ST/InlineST、CASE 与赋值解析 | 原引擎保留 |
| 全量解析后过滤；glob 数组下标、re: 正则、任一端点匹配 | 规则不变 |
| 完整候选池、非复合主视图、原始归档 | 不删除弱候选以制造稳定性 |
| OR 首出口、AND 门控、未知写入阻断、BOOL 边界 | 原规范化与复核保留 |
| 跨 FB、动作后信号收尾、反馈直接后继 | 原实现和格式适用范围保留 |
| 工位身份、A 档源码描述、B 档模型补充、工位 Excel | 明确调用顺序并检查交付 |
| Trace exact/via_zero/loose 与时长回填 | 算法不变，失败必须报告 |
| 严格单对 evaluate、Excel 人工表 coverage | 分别路由和报告口径 |
| 缺证据、未解决事件、描述拒绝、边界复核 | 保留台账，允许带复核完成 |

## 验证

- 原基线 290 项测试全部通过；新增 35 项编排与元数据测试，合计 **325 项通过**。包括状态只读、两类批次、输入绑定、断点续作、请求交付失败、组装中结果变化、旧入口兼容，以及合成输入下新旧 CSV/条件文件/Excel 单元格一致性。
- 基线中 96 个受版本管理的 Python 文件，仅修改 plc_cli.py 的编排接入，以及 workflow.py 的只读检查开关；其余 94 个文件字节不变。
- 使用仓库转接片原 XML 与 Trace、模式 `*Process_steps[*]` 分别运行原版和新版 predict。原有 156 个产物中 **155 个逐字节一致**，全部 104 个模型批次输入包含在内。
- 另一个产物 source_transition_graph.json 仅 blockers 数组排列不同：两边均为 **10,181 条，含重复次数的记录集合完全相同**；其余字段相同。原实现集合枚举已有顺序差异，本补丁未改图引擎或忽略记录。
- 两边均为 2,772 条完整候选、1,488 条采集候选、1,591 个事件、73 个工艺批次、31 个 B 档批次；A 档 280 行，待 B 档 1,208 行。新版在这些真实批次尚未处理时报告 WAITING_FOR_BATCHES，complete 非零退出，没有生成最终表或完成回执。
- 逐文件哈希与验证范围见 `validation/orchestration_v9_25_1_regression.json`。

## 验证边界与既有局限

当前环境无 OpenCode 可执行程序及用户模型连接，因此没有执行这 104 个真实模型批次，也没有宣称转接片最终工艺输出已验收。测试中的模型结果仅为合成夹具。上述证据支持已验证范围内的能力与产物保留，不能扩展为任意模型、任意工程的绝对无退化保证。

原候选池仍包含弱邻接/复位假设，原子标签形式不是动作正确性的证明；Excel 仍以完整 final_prediction_pairs 为输入，不能把全部行数称作已确认动作。现有 ST 注释提取也可能遇到人工增加的监控代码，需要审查其来源。本补丁未借编排调整改变这些提取语义。

跨 FB 结构关联仍限于原有 PLCopen LD/FBD 支持，信号收尾的自动 effect 证据主要来自欧姆龙原生 InlineST；Trace 的 loose、简单时长汇总及 coverage 的放宽匹配仍须分别解释。新完成状态保证执行和产物核验，不能替代源码语义审查、真实模型验收或现场时序验证。
