---
name: plc-trace-setup
description: 从 PLC XML 的显式步序引用生成汇川或 Sysmac 的 Trace 采集配置和变量清单，核验类型、下标及输出状态。
compatibility: opencode
---

# Trace 采集配置

## 触发

入口五从源码准备 Trace 采集通道时加载。

## 输入

- PLCopen、Sysmac IEC61131-10 或 SMC2 源码、用户指定的选择器和独立输出目录。入口语法及输出默认值见根目录 `AGENTS.md`。
- 选择器支持完整名称、`*`/`?` 通配、`re:` 或完整 CLI 的 `--regex`。通配中的方括号按字面匹配，正则匹配完整变量引用。可用 `--case-sensitive` 保留大小写匹配。

## 执行

1. 按 `AGENTS.md` 的入口五运行 `plc_cli.py trace-setup`。平台按源码格式和厂商头识别。已知客户端时可明确指定 `--format inovance` 或 `--format sysmac`。可通过 `--task-name` 提供实际任务名。
2. 从 XML 控制体取显式引用，保留完整路径和固定下标。动态下标列入审计。数组声明、FB 模板与别名不用于猜测实例通道。
3. Sysmac 依据变量声明和程序作用域解析整型通道。只有根声明缺失且类型已经用户或工程证实，才使用 `--default-type`。配置候选的步序身份由工程和实际采集核实。
4. 使用对应客户端导入配置，检查任务、触发、采样设置和通道容量。实测后保留原始导出和分卷名称。

## 验收

- `static_variables.txt` 保存完整的静态通道。`workflow_status.json` 报告静态通道数、动态下标、未解析类型、XML 问题、采样数及配置路径。
- `done` 生成 `<源码名>_trace_config.trace` 或 `.csv`，配置只含通道和采集参数。`needs_review` 提供清单和具体待核原因。
- 已有配置的显式重建使用 `--force` 归档同类产物。无可匹配的显式引用或厂商无法确定时报告实际错误。

## 协作

现场客户端将配置转成实测数据。入口四的 [Trace 跳变](../plc-trace-jump-analysis/SKILL.md) 使用实际导出的采样文件或分卷目录。
