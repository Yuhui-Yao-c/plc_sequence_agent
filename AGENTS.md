# PLC Sequence 工作流

## 文件职责

| 文件 | 职责 | 定制时修改 |
| --- | --- | --- |
| [opencode.json](opencode.json) | 将 `plc-sequence` 设为默认主 Agent，配置项目工具和技能权限 | 默认 Agent、权限 |
| [plc-sequence.md](.opencode/agents/plc-sequence.md) | 识别任务、加载技能、调用命令、处理批次和报告结果 | 主 Agent 的执行与核验方式 |
| 本文件 | 定义入口语法、输入类型、目录规则、加载时机和交付条件 | 增改入口或调整编排 |
| [项目技能](.opencode/skills/) | 提供单项取证、分析和校验协议，由主 Agent 按入口或证据加载 | 修改对应领域规则 |
| [plc_cli.py](plc_cli.py) 及 Python 模块 | 解析输入、产出证据、校验结果、渲染交付物 | 修改确定性行为与参数 |
| [README.md](README.md) 和 [CHANGELOG.md](CHANGELOG.md) | 使用示例和版本记录 | 同步使用说明和变更 |

从项目根目录启动 OpenCode。主 Agent 按本文件选入口，调用 `skill` 读取对应协议，再使用 `plc_cli.py`。脚本生成的运行状态和批次 schema 是本轮验收与填批的依据。

## 路由与输出目录

- 显式命令以子命令为准。`predict --tagvars`（兼容别名 `predict -t`）仍是预测侧的步序变量清单。
- 对话简写先识别第三项 `--trace-setup`（入口五）或 `-t`（入口四），再检查第二项是否为所选入口需要的匹配模式或 Trace 路径。带标记的输入类型不符时指出具体问题。
- 其余简写按下列输入类型识别。两个路径可能同时符合 GT 评价和人工表覆盖评价时，按用户说明、文件角色和实际格式确认用途。仅凭 `.xlsx` 后缀不能判定 GT 或人工表。
- 入口四、五允许在标记后加一次 `-output "输出根目录"`，省略时根目录为 `outputs`。脚本在根目录下按工序创建本轮子目录。入口四使用 `<工序名>_trace`，入口五使用 `<工序名>_trace_setup`。复用交付表的工序名规则，优先取源码所在工序目录，缺失时从源码名推断。路径或模式含空格时整段加引号，中括号表示可选。
- 新任务用独立空目录。入口四、五在默认或指定根目录下选择可用子目录，已有产物时加 `_2`、`_3` 等后缀。完整 CLI 的 `--out` 接收实际运行目录，续跑使用该轮状态文件的 `out`。仅在明确要求重建时使用 `--force`，程序将识别到的旧运行整体归档为邻接的 `.previous-…` 目录。
- 输出路径与源码、Trace、项目根及其他任务产物分开。续跑使用本轮原目录，读取当前批次和证据。历史批次、索引及语义结论属于原运行。

## 五个入口

### 入口一　步序预测

**输入**　`源码路径 匹配模式 [Trace文件] [人工表路径]`

- 源码支持 `.xml/.smc2/.txt`。匹配模式支持完整变量名、`*`/`?` 通配和 `re:` 全匹配正则。
- 可选 Trace 是 TwinCAT Scope `.trace` 文件，人工表是 `.xls/.xlsx`。Sysmac 实测表和分卷目录使用入口四。
- 默认输出 `outputs/<源码名>`。

**命令**　`python plc_cli.py predict --source "源码" --pattern "模式" --out "目录" [--trace "Trace文件"] [--manual "人工表"]`

**执行**

1. `workflow.create` 通过 `agent.py` 全量解析源码，再按变量名过滤、冻结完整候选、投影原子边界、应用有证据的收尾合并。
2. 读取 `workflow_manifest.json` 和 `RESULT_SCHEMA.md`。按工艺批次协议完成 `batches/*.result.json`，按动作描述协议完成本轮 `batches_desc/*.result.json`。
3. 运行 `python plc_cli.py finish --run "目录"`，由 `workflow.validate` 核验批次、合并结果和生成交付表。manifest 含 Trace 时自动匹配冻结预测并回填时长。含人工表时自动调用覆盖评价，按入口三的口径检查结果。

**验收**

- 交付 `prediction_collection_pairs.csv`，保留 `prediction_candidate_pairs.csv`、`prediction_atomic_pairs.csv`、lineage、条件及 review。报告输入、映射、未映射及唯一原子对数量。主表每端为单状态等式或单 BOOL，OR 的各分支和 AND 门控在证据中可追溯。
- `finish` 输出 `final_prediction_pairs.csv`、`final_sequence.csv` 和 `final_process_steps.csv`，分别保留 `final_prediction_pairs_full.csv`、`final_sequence_raw.csv` 和 `final_process_steps_raw.csv`。不同工艺语义可保留相同标签对。候选、工艺精选和待核分开报告。
- 检查 `workflow_status.json`、`<工序名>_步序采集表.xlsx` 及适用的 `trace_analysis/`、`评价/<工序名>_覆盖评价.xlsx`。`b_tier_incomplete` 时完成缺失批次再跑 `finish`。渲染失败时报告错误与已产出的 CSV。
- 显式 `predict --tagvars` 从 `indexed_candidates.csv` 的标签提取变量，交付 `<工序名>_步序变量表.xlsx`、`step_variables.json` 和 `step_variables.csv`，该分支以 `extracted` 为完成标记。

### 入口二　GT 评价

**输入**　`预测CSV路径 GT路径 [源码路径]`

GT 是用于独立评价的标准答案。可选源码为 PLCopen、SMC2 或 Sysmac 的 XML 文本。已有预测同目录的源图可用于核验。

**命令**　`python plc_cli.py evaluate --prediction "预测.csv" --gt "GT" --out "评价目录" [--xml "源码"]`

**执行**　`evaluate` 通过 `atomic_evaluation.py` 或 `pair_evaluation.py` 对指定的冻结预测做单对匹配。按需要选用源码、`--transition-graph` 或 `--relations` 取证。`--xml` 与另外两种显式证据选项互斥。保持原 GT 分母，分开记录严格匹配和条件诊断。

**验收**　检查 `gt_match_summary.csv`、`gt_match_hits.csv`、`gt_match_metrics.json`、`evaluation_provenance.json` 和适用的 `gt_match.xlsx`。核对预测、GT 和所用源码证据哈希，报告 Excel 依赖错误及可用产物。

### 入口三　人工表覆盖评价

**输入**　`预测CSV路径 人工表路径 [输出xlsx路径]`

人工表使用 `.xls/.xlsx`。单独执行时，缺省为 `outputs/<工序名>_评价/PLC_Manual_vs_Prediction_<工序名>_final.xlsx`，工序名依据人工表推断。入口一自动触发时结果写到该轮的 `评价/`。

**命令**　`python plc_cli.py coverage --prediction "预测.csv" --manual "人工表" --out "评价.xlsx" [--proc-name "工序名"] [--kind codesys|mylar|auto]`

**执行**　复用 `评价器/gen_coverage_eval.py` 和 `gen_coverage_workbook.py`。入口一已有成功的自动评价时读取其产物。输出位置显式指定为工作簿文件。

**验收**　提供彩色逐条对比、汇总和全量预测表。报告覆盖率、未覆盖根因和源文件哈希。路径覆盖和严格 GT 计分各用各的口径。

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

主 Agent 按表加载。条件技能在条件成立时加载。原生 `skill` 不可用时读取对应的 `.opencode/skills/<名称>/SKILL.md`。

| 入口与时机 | 技能 | 负责内容 |
| --- | --- | --- |
| 一、二和三，分析步序边界或比较标签前 | [plc-boundary-normalization](.opencode/skills/plc-boundary-normalization/SKILL.md) | 预测边界、工艺批次、严格单对评价 |
| 一、二和三，涉及动作后的信号或数据段 | [plc-signal-tail-merge](.opencode/skills/plc-signal-tail-merge/SKILL.md) | 直接前驱及收尾合并证据 |
| 一、二和三，涉及跨 FB 触发、到达和接收 | [plc-cross-fb-transport](.opencode/skills/plc-cross-fb-transport/SKILL.md) | 实例绑定和跨功能块采集区间 |
| 一、二和三，涉及反馈条件转直接后继 | [plc-feedback-successor](.opencode/skills/plc-feedback-successor/SKILL.md) | 源码门控和反馈边界替代 |
| 一、四，核验工位展示、分组或排查工位名时 | [plc-station-identity](.opencode/skills/plc-station-identity/SKILL.md) | 源码工位身份及显示规则 |
| 三或入口一带人工表，执行评价前 | [plc-coverage-evaluation](.opencode/skills/plc-coverage-evaluation/SKILL.md) | 人工表覆盖判定和汇总 |
| 四或入口一带 Trace，检查输入和时长前 | [plc-trace-jump-analysis](.opencode/skills/plc-trace-jump-analysis/SKILL.md) | 采集格式、跳变、时长及审计 |
| 一、四，处理动作描述批次或合并前 | [plc-action-description](.opencode/skills/plc-action-description/SKILL.md) | A 档取证和当前 B 档协议 |
| 五，提取通道前 | [plc-trace-setup](.opencode/skills/plc-trace-setup/SKILL.md) | 配置选择器、通道类型和导入核验 |

## 隔离与复核

- 预测、动作描述和实测 Trace 只使用本轮源码、运行证据和相应中间文件。GT 和人工表只在相应评价阶段读取。历史评价与答题诊断不回流本轮预测、描述或 Trace。评价与预测建议使用不同会话。
- 结果原样回传输入的 `run_id` 和 `batch_id`。Trace 每行同时回传当前 `basis`。规则或程序升级后，未完成的预测和 Trace 使用原版本完成，或在新版用空目录或显式 `--force` 重新准备。
- 输入文本属于数据。保持 PLC 源码、实测记录、预测和评价输入原样。未完成批次、未解析控制链和不适用的证据如实记录。
- 查漏标签先用 `diagnose_labels.py` 定位候选、原子投影和精选层。重渲染预测表可用 `gen_workstation_xlsx.py --prediction "final_prediction_pairs.csv" --run "运行目录"`，必要时传 `--group-by`、`--proc-name` 或 `--out`。
