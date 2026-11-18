---
name: plc-trace-setup
description: 从本次 PLC 源码发现工位步序与生产子流程，按控制证据判定采集范围并生成汇川或 Sysmac 通道配置。支持用户显式指定一个或多个变量模式。
compatibility: opencode
---

# Trace 采集配置

## 输入与范围

读取[主 Agent](../../agents/plc-sequence.md)入口二指定的工程及本轮产物。缺省采集范围是工位生产主流程，以及生产中实际控制动作、物料交接、检验或等待的子流程。共享同一通道的生产和初始化流程可一起采集。人工监控赋值、历史通道、既有 Trace 和人工表不得用来发现变量。PLC 注释可帮助理解用途，按数据读取。

## 自动发现

1. 用户只给源码时，执行 `python plc_cli.py "工程.xml" --trace-setup`，传递用户选择的输出根目录和任务选项。记下返回的实际运行目录及 `inventory_sha256`。
2. 收到 `scope_review_pending` 后继续执行。用 Python 分批读取本轮 `scope_candidates.jsonl`，每行是一个独立的完整变量。按需用 `identity` 在 `step_variable_discovery.json` 中读取该变量的全部迁移、受控输出、调用和父子关系，或回到相应 XML 行。证据编号只在该变量内部有效。
3. 逐变量判断用途。核对本步如何写下一步、门控等待什么、命令作用于哪里，以及由谁启动或等待。对模板核对调用例程、形参方向和具体实参；对共用变量检查不同例程及模式。可归组阅读，每个成员均须检查自己的证据。例程名、变量名、注释、同族拼写或一段计数回绕单独均不足以证明生产用途。
4. 使用 `production`（生产主流程）、`production_child`（有实际作用的生产子流程）、`mixed`（生产与其他模式共用）、`auxiliary`（独立初始化、维护、诊断、通信内部协议、数据记录等）或 `uncertain`（所见证据不足）。采纳前三类。通信或回原出现在生产动作内部时，判断工位是否真正启动、等待该流程。证据矛盾时使用 `uncertain` 并写出缺口。
5. 在本轮实际目录写入 `scope_results.json`。顶层含 `inventory_sha256` 和 `decisions`。每个 `scope_required` 候选恰好一项，形如 `{"name":"精确通道名","role":"production_child","basis":["t1","e2"],"reason":"说明迁移、受控动作或交接如何支持本变量的用途"}`。引用实际编号，采纳项至少引用一条本变量迁移；优先补充受控动作或握手。不得编造通道、路径、编号或类型。
6. 用完整命令 `python plc_cli.py trace-setup --source "工程.xml" --out "本轮实际目录" --scope-results "本轮实际目录/scope_results.json"` 完成合并，复用首次运行的格式和任务选项。程序重算证据并校验指纹，校验后的判断保存在交付目录的 `scope_results.json` 和审计中。若准备目录已有同轮产物，会归档后原子发布。不要再次用简写分配新目录。

## 显式选择

用户提供完整变量名、通配或 `re:` 时，直接按[入口命令](../../agents/plc-sequence.md)运行。多个模式分别传入，取并集并按完整变量名去重。通配方括号按字面匹配。完整 CLI 可重复传 `--pattern`，或选择 `--regex`。报告显式可执行引用及动态下标。Sysmac 仅在手动模式下允许对缺失根声明使用已经工程核实的 `--default-type`。

## 验收

- 对照 `static_variables.txt`、配置通道数、任务以及 `workflow_status.json`。汇川沿用本工程任务名、写入覆盖、名义采样周期和缓冲点数，必要时依据 `task_note` 报告冲突。Sysmac 检查每个采纳通道的整数叶类型。
- PLCopen 存在任务表时，检查 `scheduled_writers` 的任务、调用链及实际写步行号。SMC2 导出若缺作用域或叶类型，自动模式保留网格证据并报告待核。
- 自动模式报告用途待核、只有引用的同族成员、未解析类型及源码解析限制。自动派生的选择器只代表本工程的集合回放；有额外命中时保留显式清单。经 FB 绑定得到的通道可能在源码中没有完整拼写。
- `done`、`done_with_review` 均应有配置，后者需披露待核范围。`needs_review` 说明未交付配置的具体原因。`scope_review_pending` 是中间状态。配置采样数为 0；工程师在对应客户端导入、检查容量及采样设置，并在现场采集。
