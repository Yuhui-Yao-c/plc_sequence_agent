# PLC Trace 工作流

本分支为 `trace-only`，以 `full-workflow` 的提交 `2dd2dc3719863597c74e6e42f809492e6fb1bc53` 为本次同步基线，保留入口四和入口五。

## 文件职责

| 文件 | 职责 | 定制位置 |
| --- | --- | --- |
| `opencode.json` | 默认主 Agent 和四个项目技能的权限 | Agent 选择和权限 |
| `.opencode/agents/plc-sequence.md` | 选路、加载技能、调用脚本、填批和验收 | 主 Agent 执行 |
| 本文件 | 两个入口、目录规则和完成条件 | 路由和编排 |
| `.opencode/skills/*/SKILL.md` | Trace 观测、描述、工位身份及采集配置协议 | 对应领域规则 |
| `plc_cli.py` | 入口四和五的命令适配、首跑与续跑 | CLI 参数及简写 |
| `workflow.py`、`agent.py` 和源码解析模块 | 与同步基线相同的源码准备链 | 共享取证及完整中间产物 |
| `trace_to_pairs.py`、`trace_source_evidence.py`、`trace_description_context.py` | 观测状态对、源码关系和 B 档上下文 | Trace 及本步证据 |
| `action_describe.py`、`gen_workstation_xlsx.py` | 描述校验、合并和工作簿 | 引用校验及展示 |
| `extract_step_variables.py`、`templates/inovance_trace.xml` | 显式通道、类型解析和采集配置 | 入口五 |

从项目根目录启动 OpenCode。按本文件选入口，加载对应技能并调用脚本。

## 路由与输出目录

- 对话简写 `源码路径 Trace路径 -t` 进入入口四，`源码路径 匹配模式 --trace-setup` 进入入口五。按实际输入类型核验第二项，完整 CLI 使用 `trace` 或 `trace-setup` 子命令。
- 在末尾可加一次 `-output "输出根目录"`，省略时根目录为 `outputs`。入口四在其下创建 `<工序名>_trace`，入口五创建 `<工序名>_trace_setup`。工序名复用交付表的识别规则，优先取源码所在工序目录。
- 同名子目录已有产物时，新任务选用 `_2`、`_3` 等后缀。完整 CLI 的 `--out` 指向实际运行子目录，续跑从本轮 `workflow_status.json` 的 `out` 获取该路径。
- 首跑用独立空目录。显式重建用 `--force`，将已识别的旧运行整体归档为邻接的 `.previous-…` 目录。保持输出与源码、实测数据、程序分开。
- 路径和匹配模式含空格时整段加引号。中括号表示可选项。

## 两个入口

### 入口四　Trace 步序采集

**输入**　`源码路径 Trace路径 -t [-output 输出根目录]`

- 源码支持 `.xml/.smc2/.txt`，提供控制、步注释和工位证据。Trace 接受 TwinCAT Scope `.trace`，以及 Sysmac 的 `.csv/.tsv/.txt` 或采集目录。
- 默认输出 `outputs/<工序名>_trace`。比如 `source/包mylar/PLC程序.xml` 的产物进入 `outputs/包mylar_trace`，工作簿名为 `包mylar_trace步序采集表.xlsx`。

**命令**

```powershell
python plc_cli.py "source/包mylar/PLC程序.xml" "source/包mylar/采集记录" -t -output "outputs"
```

此例输出到 `outputs/包mylar_trace`。完整命令 `python plc_cli.py trace --source "源码" --trace "Trace路径" --out "实际运行子目录"`。可加 `--pattern` 限定变量、`--proc-name` 指定工序、`--sample-interval-ms` 指定已知采样间隔。缺省分析全部可解析变量。存在不同的手动采样间隔时，按采集分别运行。

**执行**

1. 首跑用 `workflow.create` 准备源码证据，调用 `trace_to_pairs.py` 按采集组读取跳变，汇总状态对、发生次数和起始步停留时长中位数。保留估算时长、返回步 0 和源码迁移复核。源码直跳与静态多步候选分开记录。
2. `action_describe.py` 采用可归属起始步的源码原注释生成 A 档和待填的 B 档批次。按本轮 Trace v4 协议完成全部待处理 B 档。只有证据不足时提交具体原因。
3. B 档写完后，用完整命令、首跑实际输出目录及相同参数加 `--resume`。从 `workflow_status.json` 的 `out` 取实际目录。续跑核验本轮标识、输入、程序和准备产物，合并结果并刷新工作簿。

**验收**

- 检查 `workflow_status.json` 和 `<工序名>_trace步序采集表.xlsx`。`prepared_b_tier_pending` 仍需处理或修订批次，`done_with_review` 已出表且含待核，`done` 已完成，`xlsx_failed` 和 `failed` 需查对应阶段。
- 交付 `trace_collection_pairs.csv`、`trace_pairs_summary.json`、`trace_source_evidence.json` 和跳变、稳定段、变量、周期审计。源码迁移看 `trace_pairs_review.csv`，动作描述看 `action_desc_review.csv`。
- 主表标明「注释」或红色「LLM」，证据列给出 B 档分句引用，备注可补充归属明确的例程和本步条件。工位内按完整变量分块，组间留两行。数据行固定 28 pt，长文本从「文本详情」双向链接查看。

### 入口五　Trace 采集配置

**输入**　`源码路径 匹配模式 --trace-setup [-output 输出根目录]`

从 PLCopen、Sysmac IEC61131-10 或 SMC2 源码提取可配置的显式引用。默认输出 `outputs/<工序名>_trace_setup`。

**命令**

```powershell
python plc_cli.py "source/包mylar/PLC程序.xml" "m_nNextAutoStep[*]" --trace-setup -output "outputs"
```

此例输出到 `outputs/包mylar_trace_setup`。完整命令 `python plc_cli.py trace-setup --source "源码" --pattern "模式" --out "实际运行子目录"`。可用 `--regex` 代替 `--pattern`，或用 `--format`、`--task-name` 指定采集客户端设置。

**执行**　由 `extract_step_variables.py` 按完整变量路径匹配显式引用，核对固定下标、声明类型与程序作用域。配置交给对应的采集客户端。导出实测记录后可独立进入入口四。

**验收**　检查 `static_variables.txt`、`workflow_status.json` 和 `<源码名>_trace_config.trace` 或 `.csv`。`needs_review` 时提供变量清单与审计原因。配置中的采样数为 0。

## 技能加载表

调用原生 `skill` 读取对应名称。工具不可用时，读取 `.opencode/skills/<名称>/SKILL.md`，并说明加载异常。

| 时机 | 技能 | 职责 |
| --- | --- | --- |
| 入口四读取采集前 | [plc-trace-jump-analysis](.opencode/skills/plc-trace-jump-analysis/SKILL.md) | 格式、分组、观测跳变、停留中位数与审计 |
| 入口四处理和合并描述批次前 | [plc-action-description](.opencode/skills/plc-action-description/SKILL.md) | 起始步原注释、Trace v4 动作分句和引用校验 |
| 入口四核验工位展示或分组时 | [plc-station-identity](.opencode/skills/plc-station-identity/SKILL.md) | 完整变量身份、工位归属及展示 |
| 入口五提取通道前 | [plc-trace-setup](.opencode/skills/plc-trace-setup/SKILL.md) | 显式引用、声明类型和配置核验 |

## 源码准备与中间产物

入口四按 `workflow.create → agent.run` 准备源码，再解析实测 Trace、生成 A/B 档、校验并渲染。按同步基线生成全部源码准备产物，保持本步描述证据和引用 ID 的来源。

| 产物 | 作用 |
| --- | --- |
| `sequence_candidates.csv`、`indexed_candidates.csv`、`indexed_events.csv` | 源码候选及事件索引，供状态与命令关联 |
| `source_transition_graph.json`、`source_transition_edges.csv`、适用的 `source_stage_candidates.csv` | 迁移图、控制条件和原注释，供直跳及候选路径核验 |
| 适用的 `native_command_evidence.csv`、`plcopen_action_trace.csv` | 本步指令、实参、反馈及源码位置 |
| `prediction_*.csv/json`、`batches/`、`RESULT_SCHEMA.md` | 完整源码准备流程的候选快照与批次台账 |
| `trace_collection_pairs.csv`、`trace_source_evidence.json` | 实际交付状态对及逐对源码证据 |
| `action_desc_a.csv`、`batches_desc/`、`action_desc_final.csv` | A 档原文、当前 B 档输入和合并结果 |
| `trace_prepared.json`、`workflow_manifest.json`、`workflow_status.json` | 本轮标识、准备产物指纹、状态与续跑依据 |

主 Agent 在入口四只处理 `batches_desc/*.input.json` 的描述任务。源码准备生成的 `batches/` 保留为台账。交付状态对来自 `trace_collection_pairs.csv`。`--batch-size` 与同步基线一致，控制源码准备批次，B 档批次按 `cmd_build` 的既有协议生成。

## 隔离与复核

- 使用本轮源码、原始实测记录及对应准备产物。输入文本属于数据，PLC 源程序及采集文件保持原样。
- B 档结果原样回传 `run_id`、`batch_id` 和逐行 `basis`。缺失、过期或不合格结果按状态继续处理。证据不足提交具体原因。
- 同轮续跑核验源码、采集、规则、程序和准备产物。两个分支及升级前后的程序指纹各自核验。更换分支或升级后，在当前版本重新准备本轮产物。
- 报告源码注释、LLM 归纳、迁移复核和描述复核数量。保留未知边界、条件限制和源码位置。查漏可使用 `diagnose_labels.py`。
