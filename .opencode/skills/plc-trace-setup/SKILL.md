---
name: plc-trace-setup
description: 从 PLC 源码识别各层的真实步序变量，按可写实例、步值分派和写回生成汇川或 Sysmac 采集配置。支持显式指定多个变量模式。
compatibility: opencode
---

# Trace 采集配置

## 输入与发现

从[主 Agent](../../agents/plc-sequence.md)收到工程路径及用户指定的采集选项后，执行 `python plc_cli.py "工程.xml" --trace-setup`，保存返回的 `out`。自动发现只读本次源码。历史 Trace、人工表和历史配置可在结果产生后独立验收。

目标是找出能表示本工程执行到哪一步的具体变量。工位主步、命令内部步、工作区、并行子流程，以及初始化、复位、手动、通信等辅助流程内真正按阶段执行的游标都需要采集。一个父步可启动多个子步，两个功能块实例可在不同的可写实参上执行相同代码。沿已调用的功能块实例还原其持久成员，逐个保留程序限定名、实例名和固定下标。输入副本、临时变量和仅声明的实例须核对其可采集性。

## 源码复核

1. `scope_review_pending` 时读取该目录的 `scope_candidates.jsonl` 和 `step_variable_discovery.json`。首轮 `static_variables.txt` 是初选。每个 `sequence_witness` 可回到同一实例的 CASE 写回或同一例程与调用实例内接续的两个写点。查步骤所控制的输出、完成条件、调用实参及声明，判断这些数值是否代表正在执行的阶段。
2. 逐一核对 `scope_required`。再浏览 `single_write` 索引，按同一原始例程、声明用途和写点检查仅有单向写回的实例。特别留意短动作、调用方启动或收尾的步序。可引用声明用途或该写点的注释补录短序列。纯引用、未绑定形参、动态下标、只有声明以及没有可达写点的项保留具体缺口。工程中同族地址的数量或编号连续性都不能代替逐实例证据。
3. 区分执行游标和作为数据传送的命令号、结果码、计数、质量码、握手字等。两个数据值能互相切换时，核对读方在各值下执行的行为及写方的职责。确认属于数据时填写 `non_sequence`，说明读取、写回以及为何表示数据，引用本变量的一处迁移和声明说明、受控读写或另一个写点。缺少判断依据时填 `uncertain`。已有结构依据的通道即使层级不明也会保留。没有结构依据且用途不明的候选继续列在审计中。
4. 为每个候选选择角色。`station_auto` 是工位或独立作业单元的周期，`station_slot` 是同一工位正式接口下的其他槽位，`component_sequence` 是命令、工作区或部件内推进的步，`auxiliary_sequence` 是经核实的辅助流程步。后两种角色均引用本实例的迁移。工位类引用本通道的全部声明（含精确下标注释）和 `owner_scope` 的迁移，缺少用途文本时补充同例程的其他阶段、写出或可写绑定。`station_slot` 提供同一数组、同一工位程序的 `interface_peer`。兼容旧角色 `auxiliary`，仅表示功能归属。
5. 将判断写入本轮 `scope_results.json`。指纹使用本轮 `inventory_sha256`。每个 `scope_required` 恰有一项，单向写回的补录也需逐地址引用证据。`station_families` 为每个声称工位归属的变量族提供一个已核实的真实实例和边界说明，只有内部序列时可省略该字段。一个项目可同时使用多种接口。若叶数组的用途说明可证实该具体数组专门存放步序，可在一个已采纳的代表地址上写 `"dedicated_step_array": true` 并引用叶声明和写步。程序按实际数组、声明边界及源码固定引用补齐只读或只复位的槽位，分别统计独立执行步和补入的槽位。`step_array_coverage.csv` 保留每个已核实数组的声明范围，现场可据此复查仅在声明范围内的预留地址。

```json
{
  "inventory_sha256": "本轮指纹",
  "station_families": [{"example": "Line.Phase", "boundary": "该工位从接料到交料"}],
  "decisions": [
    {"name": "Line.Phase", "role": "station_auto", "owner_scope": "Line::Run", "basis": ["d1","t1","t2"], "reason": "核对工位运行例程的阶段与写回"},
    {"name": "Grip.Phase", "role": "component_sequence", "basis": ["t1","e1"], "reason": "夹取命令分步发出并等待夹紧到位"}
  ]
}
```

6. 执行 `python plc_cli.py trace-setup --source "工程.xml" --out "本轮实际目录" --scope-results "本轮实际目录/scope_results.json"`，复用首轮选项。程序重算源证据、校验逐实例引用，并在 `gated_by` 记录可证实的父步。逐个检查 `captured`、`excluded`、`review`。重复运行同一请求可读取已有结果。

## 输出与验收

- `static_variables.txt` 是采集地址清单。`capture_context.csv` 列出已定位的外层执行条件、所属例程及代表写点，例如用同一个步变量执行不同命令时的命令号和模式判定。工程师可按需要加采条件中的通道。
- 核对 `workflow_status.json` 中的 `static_variables`、`configured_variables`、`configuration_complete`。Sysmac 自动模式按源码声明类型生成配置。REAL 和 LREAL 存储的离散步可保留原类型，入口一核对源码写步依据并逐样本检查整数步值；小数样点作为缺口计数。部分通道缺少叶类型时交付 `*_trace_config_partial.csv` 和 `unresolved_types.csv`。从完整工程核准缺失的叶类型，完整地址清单可供现场继续配置。
- 汇川沿任务表、写入和调用链核对任务、采样间隔、分频与缓冲区。遇到任务冲突、解析限制、动态下标和未定用途，报告具体范围。`needs_review` 表示当前没有可交付配置。`done_with_review` 应同时交付配置及明确的缺口。配置采样数为 0，现场导出的实测记录才进入入口一。
- 用户显式指定完整地址、通配或 `re:` 模式时走显式入口，多个模式取并集。Sysmac 显式模式允许用已核实的 `--default-type` 补齐缺失的根变量类型。形参经绑定才得到的完整地址，自动模式可直接发现。
