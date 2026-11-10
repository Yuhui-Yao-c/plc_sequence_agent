---
name: plc-trace-setup
description: 按指定模式从 PLC 源文件提取步序变量，独立生成变量清单和厂商采集配置。
---

# Trace 采集配置

按根目录 AGENTS.md 的入口五执行 `XML地址 匹配模式 -t`。

1. 核验源码路径，保留用户指定的完整名称、通配模式或 `re:` 正则。通配模式中的方括号是字面字符。使用独立输出目录。
2. 执行 `python extract_step_variables.py --source "源码.xml" --pattern "匹配模式" --out "outputs/工序_trace_setup"`。
3. 根据命令输出交付 `static_variables.txt`、厂商配置和提示。匹配范围包括源码文本中的注释和字符串。动态下标只统计，Sysmac 缺少类型声明时沿用脚本的 `INT` 缺省值。

本轮以配置文件生成结束。
