---
name: plc-trace-setup
description: 从 PLC XML 的显式步序引用生成汇川或 Sysmac 的 Trace 采集配置和变量清单，核验类型、下标及输出状态。
---

# Trace 采集配置

适用于还未采集、需要把源码变量编入采集配置的任务。按根目录 AGENTS.md 的入口五执行。

1. 确认源码路径、变量选择器和独立输出目录。使用用户提供的完整名称、通配模式或正则。通配模式中的方括号是字面字符，正则使用完整引用匹配。
2. 执行 `python plc_cli.py trace-setup --source "源码.xml" --pattern "变量模式" --out "outputs/工序_trace_setup"`。厂商不明时按实际采集客户端指定 `--format inovance` 或 `--format sysmac`，任务名可用 `--task-name` 指定。
3. 读取 `workflow_status.json` 和 `static_variables.txt`。按实际结果报告静态通道、动态下标、类型冲突、解析问题和生成文件。`needs_review` 时先核对源码声明与实际采集路径。仅在用户或工程声明已证实类型时，对缺少根声明的 Sysmac 通道使用 `--default-type`。
4. 配置无采样行。导入对应客户端后核对任务、触发、采样设置和可用通道数。采集结束后，将现场软件导出的实测文件或分卷目录交给入口四。

按 XML 控制体的实际名称取显式引用，保留完整路径和固定下标。动态下标列入审计。数组声明、功能块模板和别名不展开成猜测的实例通道。匹配结果用于配置候选，步序身份仍由工程及运行数据核实。
