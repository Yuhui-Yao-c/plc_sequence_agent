---
name: plc-trace-setup
description: 按一个或多个匹配模式从 PLC XML 的显式步序引用生成汇川或 Sysmac 的 Trace 采集配置和变量清单，核验类型、下标及输出状态。
compatibility: opencode
---

# Trace 采集配置

## 触发

入口二从源码准备 Trace 采集通道时加载。

## 输入

- PLCopen、Sysmac IEC61131-10 或 SMC2 源码、用户指定的选择器和独立输出目录。入口语法及输出规则见[主 Agent](../../agents/plc-sequence.md)。
- 选择器支持完整名称、`*`/`?` 通配、`re:` 或完整 CLI 的 `--regex`。多个模式分别作为独立参数，完整 CLI 重复传 `--pattern`。取并集并按完整变量名去重，可混用通配和 `re:`。通配中的方括号按字面匹配，正则匹配完整变量引用。可用 `--case-sensitive` 保留大小写匹配。

## 执行

1. 按[主 Agent](../../agents/plc-sequence.md)的入口二运行 `plc_cli.py trace-setup`。平台按源码格式和厂商头识别。已知客户端时可明确指定 `--format inovance` 或 `--format sysmac`。
2. 从 XML 控制体取显式引用，保留完整路径和固定下标。动态下标列入审计。数组声明、FB 模板与别名不用于猜测实例通道。
3. Sysmac 依据变量声明和程序作用域解析整型通道。只有根声明缺失且类型已经用户或工程证实，才使用 `--default-type`。配置候选的步序身份由工程和实际采集核实。
4. 汇川采样任务按 XML 声明核对。现场指定任务用 `--task-name`，复用已有采样设置用 `--task-from-trace`。后者只读取样例记录的任务名和分频。显式 `--every-n-cycles` 优先。未指定时优先采用唯一声明，多任务时可沿用已核对的模板任务。任务与其直接调度程序的关系由 `pouInstance` 提供，同一全局变量可以由另一个任务采样。
5. 使用对应客户端导入配置，检查关联的应用、任务、触发、采样设置和通道容量。实测后保留原始导出和分卷名称。

## 验收

- `static_variables.txt` 保存完整的静态通道。`workflow_status.json` 的 `patterns` 记录本轮全部模式，同时报告静态通道数、动态下标、未解析类型、XML 问题、采样数及配置路径。类型缺失或功能块路径未解析时按实际问题交付复核信息。
- `done` 生成 `<源码名>_trace_config.trace` 或 `.csv`，配置只含通道和采集参数。`needs_review` 提供清单和具体待核原因。
- 汇川状态中的 `declared_tasks`、`task_verified_in_xml`、`every_n_cycles` 和 `nominal_sample_interval_ms` 分别记录源码声明、任务名核对、采样分频和可计算的名义间隔。若 `task_note` 有待核说明，交付时一并列出。任务重名或无法自动选取时按具体候选核对。
- 已有配置的显式重建使用 `--force` 归档同类产物。无可匹配的显式引用或厂商无法确定时报告实际错误。

## 协作

现场客户端将配置转成实测数据。入口一的 [Trace 跳变](../plc-trace-jump-analysis/SKILL.md) 使用实际导出的采样文件或分卷目录。
