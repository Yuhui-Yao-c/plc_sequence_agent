---
description: 从 PLC 源码与 TwinCAT 或 Sysmac Trace 生成实测步序工作簿，并核验动作描述和源码证据。
mode: primary
---

遵循仓库根目录 `AGENTS.md`。项目入口是 `python plc_cli.py trace`，对话输入可写 `"源码路径" "Trace路径" -t`。引号内保留路径空格。用用户指定的输出目录，或给新任务分配独立空目录。

1. 调用 `skill` 加载 `plc-trace-jump-analysis`，核对输入并执行首跑。读取实际 `workflow_status.json`。
2. 有 B 档待处理或待修订时，调用 `skill` 加载 `plc-action-description`。逐批读取本轮 `batches_desc/*.input.json`，按当前协议写同名 `.result.json`。A 档源码原文由程序填充。
3. 原命令追加 `--resume` 后核验状态和复核记录。证据不足的行提交具体原因。交付文件以 `workflow_status.json` 中的实际路径为准。

若当前客户端的 `skill` 工具不可用，读取对应 `.opencode/skills/<名称>/SKILL.md` 继续本轮，并注明加载方式。运行状态、文件和数据以磁盘产物为准。
