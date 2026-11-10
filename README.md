# PLC Trace 步序采集

用 PLC 源码和运行时 Trace 生成观测状态对、动作描述和步序工作簿。

## 使用

```powershell
python -m pip install -r requirements.txt
python plc_cli.py trace --source "source/工程/PLC.xml" --trace "source/工程/采集" --out "outputs/工程_trace"
```

源码支持 PLCopen、欧姆龙 IEC61131-10 XML 和 SMC2。采集支持 TwinCAT Scope `.trace` 以及 Sysmac Studio 表和分卷目录。可用 `--pattern` 筛选完整步序变量，用 `--proc-name` 指定工序名。

在仓库根目录打开 OpenCode 后，可在对话中输入 `"源码路径" "Trace路径" -t`。主 Agent 是 `plc-sequence`，按流程加载 Trace 跳变分析和动作描述两项技能。详细协议见 [AGENTS.md](AGENTS.md)。

在 OpenCode 1.x 中，可从仓库根目录运行 `opencode debug agent plc-sequence` 和 `opencode debug skill`，分别查看实际加载的主 Agent 与 `plc-trace-jump-analysis`、`plc-action-description` 的发现结果。

首跑使用空目录。B 档缺项由 Agent 按本轮 `batches_desc/*.input.json` 填写，再用原命令加 `--resume` 合并。需要在同一路径重新运行时，`--force` 先归档旧运行。

## 交付

| 文件 | 用途 |
|---|---|
| `<工序名>_trace步序采集表.xlsx` | 观测状态对、停留中位数、A/B 描述和证据。主表行高统一，超长文本通过链接查看。 |
| `trace_collection_pairs.csv`、`trace_pairs_summary.json` | 观测状态对和采集汇总 |
| `trace_source_evidence.json`、`trace_pairs_review.csv` | 源码证据和迁移复核 |
| `action_desc_final.csv`、`action_desc_review.csv` | 最终描述和描述复核 |
| `workflow_status.json` | 运行状态和实际工作簿路径 |

仅重导已有 Trace 工作簿时，可以复用已落盘的状态对和描述。

```powershell
python gen_workstation_xlsx.py --pairs "outputs/工程_trace/trace_collection_pairs.csv" --out "新版步序采集表.xlsx"
```

原命令中的 `--prediction` 仍可作为 `--pairs` 的别名，用于重导 Trace 状态对。

版本记录见 [CHANGELOG.md](CHANGELOG.md)。
