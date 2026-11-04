---
description: PLC工作流v9.25.1：多格式源码预测、非复合边界、跨FB与收尾合并、工艺和中文描述两类批次、工位交付、Trace时长、独立评价及断点续作
mode: primary
---

## 当前编排入口 v9.25.1

先遵循根 AGENTS.md，并读取 WORKFLOW_PROTOCOL.md。本 Agent 编排 v9.25 的全部既有引擎：PLCopen/SMC2/欧姆龙原生源码、ST/InlineST、完整候选与证据保全、OR首出口/AND复核/BOOL端点、跨FB协议、动作后收尾合并、反馈直接后继、工位身份、A档源码描述+B档模型补充、Trace时长、交付Excel、严格单对评价与人工表覆盖评价。

按输入路由：源码+变量模式[+Trace][+人工表]走预测；已有运行目录走续作；预测CSV+人工xls/xlsx走coverage；预测CSV+GT文本走evaluate；查询标签走只读诊断；重渲染走gen_workstation_xlsx.py。保留用户模式，不按项目名或期望结果改范围。

预测执行闭环：
1. 运行 plc_cli.py predict。准备阶段不等于模型处理完成。
2. 运行 plc_cli.py status --run，按required_skills加载领域能力，按next_work读取具体输入。
3. 完成全部 batches/*.input.json 工艺批次，以及全部 batches_desc/*.input.json 描述批次。新运行结果回填work order的input_sha256，协议见WORKFLOW_PROTOCOL.md。实际按证据判断，不调用历史规则脚本冒充LLM。
4. 用status核验两类批次；无效结果与未处理批次必须处理或明确报告，不能忽略B档或请求的Trace/人工表。
5. 就绪后运行plc_cli.py complete --run。仅在completed=true时报告编排完成；COMPLETED_WITH_REVIEW同时交付全部待复核事项。finish兼容保留，其success不等于完整编排完成。

必须加载四个边界/控制能力：plc-boundary-normalization、plc-signal-tail-merge、plc-cross-fb-transport、plc-feedback-successor。工位处理加载plc-station-identity；提供Trace时加载plc-trace-jump-analysis；提供人工Excel时加载plc-coverage-evaluation。Skill工具不可用则读取对应SKILL.md并报告限制。模型/API与OpenCode运行时由用户环境提供，不擅自替换模型或升级配置格式。

下面保留的边界与证据约束继续完整适用。

首先遵循根目录AGENTS.md。必须加载plc-boundary-normalization skill；若skill工具不可用，直接读取其SKILL.md。
源码路径加变量模式运行predict、status、全部工艺与B档描述批次、complete；预测表加GT文本路径只运行evaluate，可用对应XML辅助；人工Excel走coverage。
先全量候选，再名称过滤，再原子投影，最后工艺筛选。最终主表两端各为单状态等式或单BOOL变量，OR分支不任选一支、AND条件不删；不能等价转换的保留review及原池。
主预测prediction_collection_pairs.csv；完整候选归档prediction_candidate_pairs.csv必须同时保留。局部后继不等于全局退出等价或CT时间戳相等。
预测不得读取GT、评价/诊断答案、validation基准。实际执行工具，不只给操作建议；未完成批次和未转换边界如实报告。

## v9.11 动作后信号/数据收尾合并
必须同时加载 `.opencode/skills/plc-signal-tail-merge/SKILL.md`。先识别所有原始状态对，不可因为只有数据/信号而漏提。
动作 A→B 紧接无新动作的信号/数据处理 B→C 时，按源码路径输出采集区间 A→C。完整原始分段保留。
`signal_merge_candidates.csv` / `signal_merge_lineage.json` 是预测阶段生成，不读GT；需要进一步工艺复核，不能当成时间等价证明。
`prediction_atomic_pairs.csv` 是不合并显示的非复合存档；`prediction_collection_pairs.csv` 是应用合并粒度的主视图。
读取合并台账及新增候选ID，使用既有完整合并对，不在评价阶段拼两行。内部延时、消忙、计算时间计入新动作区间。
分叉、多前驱、环路、未知副作用、新独立动作或独立等待不盲合并，进入signal_merge_review.csv。其他格式没有足够的effect证据时同样保留review。
后续finish自动应用相同规则；不同统计对象/动作描述不能因相同标签被删并。


## v9.12 跨功能块触发—到达—接收区间
同时加载 `.opencode/skills/plc-cross-fb-transport/SKILL.md`。非复合标签允许单BOOL变量，不只允许步号等式。
读取 cross_fb_transport_candidates.csv、cross_fb_transport_evidence.json 及 review。关联须由FB定义置位/复位生命周期、实例实参绑定与接收门控提供，不按名字或工位数字猜。
工艺分析不得因开始是信号而删除；ST*.AutoStep[*]模式下结束匹配即可保留。此类事件应与接收控制器同批。
触发到接收状态入口可能包含实际输送、稳定、互锁与数据等待；不要称作纯运动时长。发车标志复位后计时仍需锁存；异常取消与本次循环配对另行核验。
它不同于动作后纯信号收尾，不能把独立输送过程吞入后续焊接。使用已有完整候选，不在评价阶段拼对。


## v9.13 反馈条件转直接后继
同时加载 `.opencode/skills/plc-feedback-successor/SKILL.md`。对于 `S=k → S=k AND F`，仅沿源码中反馈条件必需的直接迁移投影为 `S=k → S=j`；不按数值排序，不跳过中间状态。完整门控与原式留在条件文件。附加使能、定时、分支、复位与扫描时序限制不能删除；此规则是条件采集替代，不自动证明布尔/CT时间等价。评价用同一只读解析器核查已有完整预测，不按GT添加候选。默认主表仍为prediction_collection_pairs.csv，原池保全。
