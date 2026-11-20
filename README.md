# PLC Trace 工作流

本项目通过 OpenCode 提供三个入口。入口一分析 PLC 源码和实测 Trace，生成中文步序采集表。入口二从源码自动发现步序变量或按指定模式提取变量，生成采集通道配置。入口三从源码和实测记录构建可交互的 PLC 逻辑图谱。

当前版本为 **15.0.0**，维护分支为 `logic_graph`。双入口基线保存在 `trace-only`。运行 `python plc_cli.py --version` 查询本地版本。版本记录、分支用途和升级规则见 [版本与分支管理](docs/VERSIONING.md)。

## 启动

在项目根目录执行。

```powershell
python -m pip install -r requirements.txt
opencode
```

在 OpenCode 中输入以下简写。含空格的路径和匹配模式用引号包住。

| 入口 | 输入 |
| --- | --- |
| 一　Trace 步序采集 | `源码路径 Trace路径 -t [-output 输出根目录]` |
| 二　Trace 采集配置 | `源码路径 [模式1 模式2 ...] --trace-setup [任务选项] [-output 输出根目录]` |
| 三　PLC 逻辑图谱 | `源码路径 Trace路径 --logic-graph [-output 输出根目录]` |

```text
"source/包mylar/PLC程序.xml" "source/包mylar/采集记录" -t -output "D:\工作成果"
"source/包mylar/PLC程序.xml" --trace-setup -output "D:\工作成果"
```

前两个入口分别输出到 `D:\工作成果\包mylar_trace\` 和 `D:\工作成果\包mylar_trace_setup\`。省略 `-output` 时使用 `outputs` 根目录。入口二的候选、范围判断、最终配置和通道清单都在同一个工序子目录。同一源码、程序及采集选项重复提交时，校验已有文件后返回当前阶段。不同采集任务或原位置已被其他数据占用时才另选编号。入口一新建独立运行的目录规则见下文。

入口三输出到 `<工序名>_logic_graph/`。双击 `logic_graph.html`，在“协作总览”选择写入程序和接收程序的交点，按信号查两端指令。“整机图纸”可按程序、例程、网络逐层展开，也可展开全工程。转到“步序与观测”可从一次跳变反查写点。整个文件可复制到离线的现代浏览器单独打开。证据分级和 JSON 结构见 [图谱说明](docs/logic-graph.md)。

入口二省略模式时，根据源码中的状态分支、写回和功能块实参发现具体通道。OpenCode 按实际控制层级确认工位或独立作业单元的自动周期。同一正式工位数组中承载局部动作的生产槽位可逐项核实。设备内部另设的执行步、许可和通信状态保留在审计里。多个独立步序接口可同时入选。单独使用 CLI 时，首次返回 `scope_review_pending`，需提供本轮用途判断才能完成选择。[自动发现与合并协议](.opencode/skills/plc-trace-setup/SKILL.md)

也可连续输入多个独立模式，每个模式用引号包住。命中任一模式的变量进入同一清单，完整变量名去重后自然排序。可混用完整名称、通配符和 `re:` 正则；单模式写法继续有效。

```powershell
python plc_cli.py "source/工程/PLC.xml" "Cell.Phase[*]" "re:Line\.Stage[0-9]+" --trace-setup -output "D:\工作成果"
```

### 汇川采样任务如何产生

入口二以本次工程 XML 为输入。生成器读取 XML 的任务名、任务类型、周期和 `pouInstance`，把已调度的程序、可定位的例程调用与所选通道的明确写入关联起来。

| XML 情况 | 默认结果 |
| --- | --- |
| 唯一任务声明 | 使用该任务的工程名称 |
| 同一资源有多个任务 | 对每个任务所写的不同通道计数，选择有写入证据且覆盖数唯一最高的任务。所有已选通道进入同一条采集记录 |
| 没有任务声明、同名任务不能区分、覆盖并列或写入任务跨资源 | 保留变量清单，在 `workflow_status.json` 说明 `needs_review` 的原因。可补充完整工程导出，或使用已核实的 `--task-name` |

`.trace` 的 `Record.TaskName` 指定一个采样任务。默认 `EveryNCycles=1`。已知周期时，名义采样间隔为任务周期乘以分频。自动缓冲按本工作流的 2 秒窗口计算，点数为 `max(10, ceil(2000 / 名义间隔毫秒) + 1)`。事件任务、周期缺失或冲突、无法编码点数时保留模板缓冲值，并记录其来源。分频与缓冲窗口属于采集配置策略。历史采样任务、触发设置和时间戳精度可能由工程师在客户端单独选择。

`declared_tasks[].channel_writes` 给出每个任务覆盖的通道、程序调用路径及写入所在 XML 元素的行号。`channels_without_task_write` 列出缺少已调度写入证据的通道。这些通道仍保留在变量清单中。任务的工程对象 ID 和优先级保存在状态文件，客户端从当前应用提供可选任务列表。

需要使用另一个采样时钟时，可在简写或完整 CLI 中指定工程内的任务名和分频。

```powershell
python plc_cli.py "source/工程/PLC.xml" "Cell.Phase[*]" --trace-setup
python plc_cli.py "source/工程/PLC.xml" "Cell.Phase[*]" --trace-setup --task-name "ScanTask" --every-n-cycles 2
```

## 命令与续跑

根目录 `plc_cli.py` 是统一启动脚本。完整 CLI 的 `--out` 指定实际运行子目录。

```powershell
python plc_cli.py trace --source "source/包mylar/PLC程序.xml" --trace "source/包mylar/采集记录" --out "outputs/包mylar_trace"
python plc_cli.py trace-setup --source "source/包mylar/PLC程序.xml" --pattern "m_nNextAutoStep[*]" --out "outputs/包mylar_trace_setup"
```

入口二完整 CLI 可重复指定 `--pattern`。状态文件的 `patterns` 保存本轮全部模式；单模式时保留原 `pattern` 字符串，多模式时该字段为 `null`，以 `patterns` 为准。多模式选择仍核验作用域、类型和动态下标，不自动推定缺失类型或展开功能块模板。

Trace 分析支持 TwinCAT Scope `.trace` 和 Sysmac 导出表、分卷目录，时长取同一连续采样段内、进入和离开起始步均有观测的停留中位数。源码原注释优先进入 A 档，其余行由 Agent 按本步证据完成 B 档。

`workflow_status.json` 的 `out` 记录实际运行目录。入口二收到 `scope_review_pending` 后，在该目录写入 `scope_results.json`，以同一组采集选项和 `--out`、`--scope-results` 合并。普通合并在原处完成。要重新独立判定时，可指定新输出根目录，或用完整命令在原目录显式 `--force` 归档重建。旧版已完成目录作为历史结果保留。旧版待复核任务可显式指定原目录续跑，程序重新核对源码、采集设置及候选指纹。

入口一简写遇到已占用目录时另开编号。`prepared_b_tier_pending` 时完成本轮 `batches_desc/*.result.json`，再以相同参数加 `--resume`。`done_with_review` 表示已出表且有待核项。

入口二的配置在客户端导入并完成现场采集后，可将实测导出交给入口一。类型或解析问题由 `needs_review` 状态报告。

## 目录与职责

| 路径 | 内容 |
| --- | --- |
| `plc_cli.py`、`opencode.json` | 统一启动与 OpenCode 配置 |
| `AGENTS.md` | 项目通用规则和开发约定 |
| `.opencode/agents/plc-sequence.md` | 命名 Agent，集中维护三个入口和执行编排 |
| `.opencode/skills/` | 四个按需加载的领域协议 |
| `plc_trace/source/` | 多厂商 XML、ST、LD/FBD 解析及源码事件 |
| `plc_trace/analysis/` | 控制图、候选、边界与条件分析 |
| `plc_trace/trace/` | 实测解析、状态对与源码关系 |
| `plc_trace/description/` | A/B 档证据、批次、描述校验与合并 |
| `plc_trace/output/` | 工位身份、工序命名和工作簿 |
| `plc_trace/capture/`、`plc_trace/runtime/` | 通道配置与本轮准备、指纹和归档 |
| `plc_trace/resources/` | 词表和采集模板 |
| `tests/` | 回归测试及临时样本构造 |
| `docs/` | 当前变更及历史记录 |

从 [.opencode/agents/plc-sequence.md](.opencode/agents/plc-sequence.md) 调整入口与编排，从对应技能调整领域协议。目录迁移和版本对齐依据见 [当前变更](docs/CHANGELOG.md) 与 [历史记录](docs/history.md)。

## 源码证据与交付物

入口一先按完整变量、实例、采集和样本定位实测跳变，再从动作写点的实际分岔补齐其他出口。ST 使用嵌套判断和分支位置。LD/FBD 使用同一比较输出的接线和互斥条件。内部步沿同一状态分派的专属支路延伸，在汇合、外部入口和源码执行范围边界停止。对另一个通道，仅在父步与具体子步都约束了同一可写实例调用时收录。

主交付 `<工序名>_trace步序采集表.xlsx` 的「时序拆解」按分支列行，保留工位、动作描述、起止标签和证据链，新增本次状态、适用工况、转移条件、关联实测和源码位置。「实测跳变」保留所有观测端点及原始次数、中位数。「分支待核」列出多写点、竞争路线、动态目标、未能解释的观测和相关写点。每条补录行可追溯到实测样本与源码写点。未观测或归属待辨时，分支时长留空。各出口独立计时，工位汇总行不累加互斥路径的中位数。

| 文件 | 用途 |
| --- | --- |
| `trace_collection_pairs.csv`、`trace_jumps.csv` | 原始端点统计与逐次跳变 |
| `trace_action_branches.csv`、`trace_branch_evidence.json` | 交付行、分支 ID、源码位置、判断条件及实测锚点 |
| `trace_branch_events.csv`、`trace_branch_observations.csv` | 采集 ID 与样本位置、分支归属、竞争写点与复核线索 |
| `source_branch_index.json` | 完整源码写点索引。再次分析同一源码时可在完整 `trace` 命令中传 `--branch-index <上次索引路径>`，程序校验源码、解析程序和索引内容后复用 |
| `batches_desc/`、`action_desc_final.csv` | 以分支 ID 关联的动作描述批次与最终文本 |

例如，实测 `100 → 110` 归属一个判断后，同一判断可直达 120 的出口随表列出，时长留空。同通道独立动作中的 `300 → 310` 在采集首次涉及该动作时进入交付范围。对于同时存在直接和多步解释的端点，原始时间保存在「实测跳变」。

采样连续而步值不动的时段按实测保留。遇到缺口，每个通道从恢复后的首个样本重新跟踪。缺口两端的值供审计，不能据此补出中间路径。工作簿「采样缺口」和 `trace_sampling_gaps.json` 给出采集内时间、上下文端点和通道范围。每行证据链区分观测发生次数与有效计时次数。

自动发现的控制证据及用途结论见 `step_variable_discovery.json`，按通道阅读的摘要见 `scope_candidates.jsonl`。自动采集以 `admission=captured` 的完整变量为准。升级后需重新判断工位归属；显式模式仍可指定需要的内部变量。

入口二交付 `static_variables.txt`、`workflow_status.json` 和可用的 `<源码名>_trace_config.trace` 或 `.csv`。

## 开发与验证

```powershell
python -m unittest discover -s tests -t . -q
opencode debug config
opencode debug agent plc-sequence
opencode debug skill
```

内部工具用完整模块名执行。

```powershell
python -m plc_trace.diagnostics --run "outputs/包mylar_trace" --start "Cell.Phase[1]=10" --out "outputs/诊断"
python -m plc_trace.description.action_describe validate --run "outputs/包mylar_trace"
```

完整源码包解压到新的项目目录，现有输入可按绝对路径引用。程序指纹递归覆盖 Python 包、资源、命名 Agent、技能和项目配置，更新后按当前版本重新准备旧批次。比较版本时核对状态对、时长、原注释、B 档证据、复核和工作簿，并单独校验本轮标识与指纹。
