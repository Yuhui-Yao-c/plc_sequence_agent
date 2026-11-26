# PLC Trace 工作流

本项目通过 OpenCode 完成两项任务。入口一分析 PLC 源码和实测 Trace，生成中文步序采集表。入口二从源码自动发现步序变量或按指定模式提取变量，生成采集通道配置。

当前简化版为 **18.4.0**，维护分支为 `trace-only`。运行 `python plc_cli.py --version` 查询本地版本。版本记录、分支用途和升级规则见 [版本与分支管理](docs/VERSIONING.md)。

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

```text
"source/包mylar/PLC程序.xml" "source/包mylar/采集记录" -t -output "D:\工作成果"
"source/包mylar/PLC程序.xml" --trace-setup -output "D:\工作成果"
```

两个任务分别输出到 `D:\工作成果\包mylar_trace\` 和 `D:\工作成果\包mylar_trace_setup\`。省略 `-output` 时使用 `outputs` 根目录。入口二的候选、范围判断、最终配置和通道清单都在同一个工序子目录。同一源码、程序及采集选项重复提交时，校验已有文件后返回当前阶段。不同采集任务或原位置已被其他数据占用时才另选编号。入口一新建独立运行的目录规则见下文。

入口二省略模式时沿 XML 的写步、分支和功能块实参发现各层步序。首轮返回 `scope_review_pending`，输出有源码依据的候选地址 `trace_targets_preview.txt`。类型与采样任务可确定时，同目录立即生成文件名含 `_preview` 的零样本预配置。默认配置只收录能给工位生产工步独立计时的地址，以及经独立核验指定的步序地址。对每个入选动作，核对同一实例的写前步、写后步、动作和运行条件。分散在不同例程的写入与分派按实例和值关联，未知的写前值留空。独立运行 CLI 可按同一协议提交复核结果。[自动发现与合并协议](.opencode/skills/plc-trace-setup/SKILL.md)

判断采集范围时，从工位要计时的作业回到源码。比如翻转工位发出定位命令后同时移动 Z 轴，随后等待定位完成。主步标记的是抵达等待步后剩余的等待时间，夹具内部游标可以分别量两个气缸的定位耗时。若工艺表只记录工位等待，默认采工位主步，两段气缸定位保留为备查。若两条物料通道各自向下游交付一件工件，整组完成值无法标记每件的交接时刻，应保留各通道拥有实际边界的执行步。核对时沿完整实例地址、命令与工况检查，确定要单采某个内部地址时，可在自动发现中用独立 `--reference` 指定该完整路径，并检查正式配置回读结果。

在 `scope_results.json` 中，`use` 保存提交的用途判断，`effective_use` 记录程序核验后的用途，`admission` 决定该地址是否进入正式清单。若证据中出现 `exclusion_evidence.kind=enclosed_child`，可沿 `parent`、`start_links` 和 `wait_links` 找到父步与具体子实例，通过 `parent_writes`、`child_writes` 核对实际写步，并用 `handbacks` 查看父步放行后接管的执行器。备选表保留该子步的完整地址，便于诊断内部阶段。

显式模式匹配源码中的字面引用。也可连续输入多个独立模式，每个模式用引号包住。命中任一模式的变量进入同一清单，完整变量名去重后自然排序。可混用完整名称、通配符和 `re:` 正则；单模式写法继续有效。

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

`workflow_status.json` 的 `out` 记录实际运行目录。入口二收到 `scope_review_pending` 后，在该目录写入 `scope_results.json`，以同一组采集选项和 `--out`、`--scope-results` 合并。用途复核和补齐成员类型都在这个目录完成。已有部分配置时，保留程序给出的类型模板，将核实结果另存为 `member_types.csv`，复用原 `--out` 并传 `--member-types 本轮目录/member_types.csv`。程序读取本轮已核验的用途判断，刷新可交付文件。若本轮报告的 `legacy_timing_reviews` 大于零，说明部分历史判断缺少独立计时依据。对这些地址补判，将完整结果另存，再用同一 `--out` 和新的 `--scope-results` 合并，保留已核验判断及工位接口族。要重新独立判定时，可指定新输出根目录，或用完整命令在原目录显式 `--force` 归档重建。旧版已完成目录作为历史结果保留。旧版待复核任务可显式指定原目录续跑，程序重新核对源码、采集设置及候选指纹。

入口二在 `workflow_status.json` 中分别报告以下数量。

| 字段 | 可核对的内容 |
| --- | --- |
| `reviewed_candidates`、`scope_admission_counts` | 完成逐实例判断的记录数和处置分布。每个实例只计一次。 |
| `confirmed_production_variables`、`reference_only_variables` | 源码已确认的生产计时地址、仅因独立核验指定而纳入的地址。这两组互斥。 |
| `reference_selected_variables` | 出现在独立核验输入且最终入选的地址，包含与源码已确认地址的交集。 |
| `optional_variables`、`configured_variables` | 备选表的实际行数、正式配置回读的实际通道数。 |

入口一简写遇到已占用目录时另开编号。`prepared_b_tier_pending` 时完成本轮 `batches_desc/*.result.json`，再以相同参数加 `--resume`。`done_with_review` 表示已出表且有待核项。

入口二的配置在客户端导入并完成现场采集后，可将实测导出交给入口一。`static_variables.txt` 列出所有入选地址。Sysmac 配置缺少部分叶类型时，已核实的地址写入 `*_trace_config_partial.csv`，其余地址及原因见 `unresolved_types.csv`。程序回读待发布配置中的实际地址和类型。`configured_variables` 是实际写入数，`configuration_complete` 表示配置含全部入选地址。自动发现可加 `--reference 核验表.xlsx`，也支持起止条件 TSV、每行一个地址的文本和含“变量,类型”的 CSV。核验输入应来自人工工步起止表或已确认用于工步计时的通道。首轮候选和历史工作流自动生成的配置不能用于自证。独立核对地址清单与实际配置两处差集，精确地址的可信类型可以补齐缺失声明。若 XML 指向外部结构且缺成员定义，可从原工程核实 `missing_member_types.csv` 里的“数据类型”，另存后传 `--member-types 已核实成员.csv`。步值的范围和相似变量的类型都不足以补写声明。同一已声明结构的同名标量成员共用核实结果；不同结构、未知中间层和数组成员分别核对。已有带类型的具体地址也可以佐证同一外部成员。差集与解析缺口见状态文件的 `coverage`。有部分配置而缺已选地址时状态为 `needs_review`。Sysmac 也支持用 REAL 或 LREAL 存储整数步值的离散序列。入口一依据源码确认步序，逐样本核对整数值，小数样点记为该通道缺口。对于已核实的专用步序数组，备选清单列出仅有固定引用的槽位，状态文件区分独立执行步与仅有引用的槽位。其他预留槽位可按 `step_array_coverage.csv` 的声明边界复查。外层执行条件及 XML 写点见 `capture_context.csv`。

## 目录与职责

| 路径 | 内容 |
| --- | --- |
| `plc_cli.py`、`opencode.json` | 统一启动与 OpenCode 配置 |
| `AGENTS.md` | 项目通用规则和开发约定 |
| `.opencode/agents/plc-sequence.md` | 命名 Agent，集中维护两个入口和执行编排 |
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

主交付 `<工序名>_trace步序采集表.xlsx` 的「时序拆解」覆盖每组实测起止标签，列出发生次数、有效计时次数及起始步停留中位数。唯一对应源码出口时，动作与计时共用一行。跨步、存在多个可能写点或来源待核时，单列「实测跳变」，描述来源标为 Trace，备注给出分支判定及已找到的路径。与观测相关的源码出口另列，只有取得自身完整计时的出口才填时长。「实测跳变」保留所有观测端点及原始次数、中位数。「分支待核」列出多写点、竞争路线、动态目标、未能解释的观测和相关写点。每条补录行可追溯到实测样本与源码写点。未观测或归属待辨时，分支时长留空。各出口独立计时，工位汇总行不累加互斥路径的中位数。

「实测跳变」的跳变来源、源码步序路径和所属程序段可区分直接写步、连续写步与返回前序梯级。跨动作段需有父程序的显式调用和共同步值。上层程序赋初值、批量清零以及同终点的多个写点都保存在来源证据中。采样间隔内的写步提供动作线索，各中间步的运行时长由其自身的实测记录确定。

| 文件 | 用途 |
| --- | --- |
| `trace_collection_pairs.csv`、`trace_jumps.csv` | 原始端点统计与逐次跳变 |
| `trace_action_branches.csv`、`trace_branch_evidence.json` | 交付行、分支 ID、源码位置、判断条件及实测锚点 |
| `trace_branch_events.csv`、`trace_branch_observations.csv` | 采集 ID 与样本位置、分支归属、竞争写点与复核线索 |
| `source_branch_index.json` | 完整源码写点索引。再次分析同一源码时可在完整 `trace` 命令中传 `--branch-index <上次索引路径>`，程序校验源码、解析程序和索引内容后复用 |
| `batches_desc/`、`action_desc_final.csv` | 以分支 ID 关联的动作描述批次与最终文本 |

Sysmac 梯级注释按写点所在网络及起始步号对应到原文。相同步号可指向多个状态变量时，工作流保留复核。

例如，实测 `100 → 110` 归属一个判断后，同一判断可直达 120 的出口随表列出，时长留空。同通道独立动作中的 `300 → 310` 在采集首次涉及该动作时进入交付范围。对于同时存在直接和多步解释的端点，原始时间保存在「实测跳变」。

采样连续而步值不动的时段按实测保留。遇到缺口，每个通道从恢复后的首个样本重新跟踪。缺口两端的值供审计，不能据此补出中间路径。工作簿「采样缺口」和 `trace_sampling_gaps.json` 给出采集内时间、上下文端点和通道范围。每行证据链区分观测发生次数与有效计时次数。

自动发现的控制证据及用途结论见 `step_variable_discovery.json`，按通道阅读的摘要见 `scope_candidates.jsonl`。Sysmac 和 PLCopen 源码都区分“父步给子步写启动值”和“父步读子步完成值后推进”。摘要中的 `parent_links` 按父地址列出两类关系的证据编号，可在完整台账中核对实际写点及条件。对于含标准触发器的 Sysmac 有界 FOR 循环，解析器核对省略逻辑可能写到的变量，保留能够定位的独立写步。条件尚未完整解析的写点会标记 `guard_complete=false`。自动采集结果以正式 `static_variables.txt` 的完整变量名和导入文件回读数为准。升级后应按本轮证据复核工位计时边界。需诊断局部实例时，按完整绑定路径提交独立指定的地址。

入口二按状态中的 `trace_targets` 找到当前阶段的地址名单。首轮预配置路径在 `preview_configuration`，正式配置路径在 `configuration`，`needs_review` 也可能已有 `_partial.csv` 可交付。汇川输出 `.trace`，Sysmac 输出零样本 `.csv`。在 Sysmac Studio 中按地址登记目标并核对模板的采样任务。

## 开发与验证

18.4.0 通过 178 项自动化测试，用六份真实工程 XML 重新生成并回读入口二配置。配对机默认清单为 96 个地址，覆盖人工表 70/70。两个翻转定位子步均留在备选。将七个争议地址同时申报为生产计时，并以启动或整组到位作为其余五个地址的依据时，正式清单仍为 96 个。缺少两条外部类型声明使 73 个入选地址待补，实际写入 23 个，覆盖人工表中的 13 个。另四份现场 Trace 中的 150 个活跃通道全部进入默认配置，176 个登记通道中有 7 个本次未跳变的地址未入选。逐工程数据及换用其他写点后仍须复核的例子见[本版验证记录](docs/CHANGELOG.md)。

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
