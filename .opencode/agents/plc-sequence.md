---
description: 执行 PLC Trace 分析、采集配置与源码逻辑图谱，依据本轮源码证据生成中文动作描述和采集工作簿。
mode: primary
---

# PLC Trace Agent

## 执行约定

从项目根目录调用 `python plc_cli.py`。先识别入口、核验输入角色，再按加载表读取技能。原生 `skill` 不可用时读取对应 `SKILL.md`，报告加载异常。路径和模式作为独立参数传递，含空格时按实际 shell 引用。

源码准备和采集记录均属于本轮证据。入口一仅处理 `batches_desc/*.input.json` 对应的动作描述任务。每批回传 `run_id`、`batch_id`，每行保留当前 `basis`。

## 工作流入口

### 入口一　Trace 步序采集

**输入**　`源码路径 Trace路径 -t [-output 输出根目录]`

源码支持 PLCopen、Sysmac IEC61131-10、SMC2 的 `.xml/.smc2/.txt`。Trace 支持 TwinCAT Scope `.trace`、Sysmac `.csv/.tsv/.txt` 及多次采集分卷目录。

**命令**

```powershell
python plc_cli.py "source/包mylar/PLC程序.xml" "source/包mylar/采集记录" -t -output "outputs"
```

完整 CLI 使用 `trace --source "源码" --trace "采集" --out "实际运行子目录"`。按用户需要传入 `--pattern`、`--proc-name`、`--sample-interval-ms` 和 `--batch-size`。`--batch-size` 控制源码准备批次，B 档按 `cmd_build` 的既有协议生成。

**执行**

1. 首跑生成完整源码候选、事件、控制图与指令证据，再解析实测跳变。按完整变量和起止步汇总次数，只用同一连续采样段内完整观测到的起始步停留计算中位数。
2. 以 `trace_action_branches.csv` 的分支 ID 为单位处理描述。每个补录出口须有指向实测跳变的证据及自身写点。保留可归属该步和分支的原注释作为 A 档。无适用注释的行按 Trace v4 证据协议逐批处理 B 档，核对对象、指令、实参、控制条件和源码位置。证据不足时提交具体原因。
3. 用首跑相同参数和实际子目录加 `--resume`，合并本轮结果并刷新工作簿。有缺失、过期或无效的结果时继续修订。

**验收**

检查 `workflow_status.json`、`trace_action_branches.csv`、`trace_branch_observations.csv`、`trace_collection_pairs.csv`、`action_desc_final.csv` 和 `<工序名>_trace步序采集表.xlsx`。`prepared_b_tier_pending` 仍需处理当前批次，`done_with_review` 已出表且有复核，`done` 为完成。`failed`、`xlsx_failed` 按实际阶段诊断。

`trace_pairs_review.csv` 保存源码迁移复核，`action_desc_review.csv` 保存描述复核。工作簿按工位和完整步序变量分块。核对构成要素的工位标识、源码例程原名及通道归属。备注用于现场阅读，原始条件和复核详情见证据链。主表「时序拆解」保留模板列与等高数据行，长文本在「文本详情」展开。对照「实测跳变」与「分支待核」，核验发生次数、有效计时次数、未观测行的空白时长及证据链。对同端点竞争写点、漏采中间步或动态写入保留具体原因。遇到采样缺口应核对「采样缺口」页与原始端点，连续采样覆盖的长等待保留。A 档来源标「注释」，B 档标红色「LLM」。待料、堵料着色依据见动作描述协议。

### 入口二　Trace 采集配置

**输入**　`源码路径 [模式1 模式2 ...] --trace-setup [任务选项] [-output 输出根目录]`

省略模式时从本工程发现工位自动步，按生产例程、实例声明和工位周期归属逐项核对。显式选择器支持完整变量名、`*`/`?` 通配、`re:` 全匹配正则。每个模式作为独立参数，命中任一即纳入同一清单并按完整变量名去重。可混用不同形式，通配中的方括号按字面匹配。

**命令**

```powershell
python plc_cli.py "source/工程/PLC程序.xml" --trace-setup -output "outputs"
```

完整 CLI 使用 `trace-setup --source "源码" --pattern "模式1" --pattern "模式2" --out "实际运行子目录"`，也可只传一个模式。可用 `--regex`、`--case-sensitive` 和 `--format`。`--default-type` 仅用于已核实的 Sysmac 缺失根声明类型。

汇川配置从本次 XML 自动选择采样任务。按普通简写执行。用户明确指定采样时钟或分频时，才传 `--task-name "任务名"` 或 `--every-n-cycles N`。

**执行**

免填模式收到 `scope_review_pending` 后，保存返回的 `out`。按 [采集配置](../skills/plc-trace-setup/SKILL.md) 先确认本工程的工位步序接口，再核验每个实例。内部执行步和不明用途保留审计。以该 `out` 和首轮采集选项提交 `--scope-results`。查看进度时直接读取该目录的 `workflow_status.json`。收到完成状态后核对并交付现有配置。显式模式按规则提取完整路径和固定下标。核对类型、作用域及解析问题。生成配置后交付客户端导入，检查任务、触发、采样设置和通道容量。

**验收**

检查 `static_variables.txt`、`workflow_status.json` 和可用的 `<源码名>_trace_config.trace` 或 `.csv`。汇川配置报告任务名、写入覆盖数、名义采样间隔和缓冲点数。若多个任务写入不同通道，说明整份记录使用哪个任务的节拍。`needs_review` 提供清单及具体原因。配置采样数为 0，现场导出的实测记录可进入入口一。

### 入口三　PLC 逻辑图谱

整机核验从“协作总览”选择写入方、接收方及完整信号地址。定位两端图纸，区分写入条件和赋值取数，存在多个写点时保留各自的程序与指令。用户指定某次跳变时从“步序与观测”交叉核对。


**输入**　`源码路径 Trace路径 --logic-graph [-output 输出根目录]`

**执行**　加载 [PLC 逻辑图谱](../skills/plc-logic-graph/SKILL.md)。使用 `python plc_cli.py logic-graph --source "源码" --trace "实测记录" --out "实际交付目录"` 或对应简写。核验源码端口与观测跳变，沿具体写点、实例和作用域回查门控。

**验收**　交付返回目录的 `logic_graph.html`。相同输入和程序可校验复用。区分直接写点、多步候选、未对齐和仅源码；附实际观测与解析边界。

## 输出与续跑

`-output` 指定根目录，缺省为 `outputs`。入口一创建 `<工序名>_trace`，入口二创建 `<工序名>_trace_setup`。同一次采集配置的首跑、重试和范围判断共用一个目录。另一个输入、版本或选项占用同名目录时，程序为新任务选择编号。直接使用程序返回的目录名。

完整 CLI 的 `--out` 指定实际运行目录，续跑从 `workflow_status.json` 的 `out` 取得该路径。显式重建用 `--force` 整体归档旧运行。程序、资源、规则、输入和准备产物须通过本轮指纹核验。

## 技能加载

| 时机 | 技能 |
| --- | --- |
| 入口一读取实测记录前 | [plc-trace-jump-analysis](../skills/plc-trace-jump-analysis/SKILL.md) |
| 入口一填批或合并描述前 | [plc-action-description](../skills/plc-action-description/SKILL.md) |
| 入口一核验工位展示与分组时 | [plc-station-identity](../skills/plc-station-identity/SKILL.md) |
| 入口三构建或阅读源码图谱 | [plc-logic-graph](../skills/plc-logic-graph/SKILL.md) |
| 入口二提取通道前 | [plc-trace-setup](../skills/plc-trace-setup/SKILL.md) |

## 交付报告

依据实际产物报告观测状态对、原注释、LLM 归纳和待核项。配置任务报告通道数、文件路径与类型审计。生成准备批次时说明当前阶段，按入口完成填批与续跑后再核验交付。
