# PLC Trace 工作流

本项目通过 OpenCode 完成两项任务。入口一分析 PLC 源码和实测 Trace，生成中文步序采集表。入口二从源码提取显式变量，生成采集通道配置。

当前简化版为 **12.1.0**，维护分支为 `trace-only`。运行 `python plc_cli.py --version` 查询本地版本。版本记录、分支用途和升级规则见 [版本与分支管理](docs/VERSIONING.md)。

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
| 二　Trace 采集配置 | `源码路径 模式1 [模式2 ...] --trace-setup [任务选项] [-output 输出根目录]` |

```text
"source/包mylar/PLC程序.xml" "source/包mylar/采集记录" -t -output "D:\工作成果"
"source/包mylar/PLC程序.xml" "m_nNextAutoStep[*]" --trace-setup -output "D:\工作成果"
```

两个任务分别输出到 `D:\工作成果\包mylar_trace\` 和 `D:\工作成果\包mylar_trace_setup\`。省略 `-output` 时使用 `outputs` 根目录。已有产物时自动选用带 `_2`、`_3` 等后缀的新子目录。

入口二可连续输入多个独立模式，每个模式用引号包住。命中任一模式的变量进入同一清单，完整变量名去重后自然排序。可混用完整名称、通配符和 `re:` 正则；单模式写法继续有效。

```powershell
python plc_cli.py "source/工程/PLC.xml" "Cell.Phase[*]" "re:Line\.Stage[0-9]+" --trace-setup -output "D:\工作成果"
```

### 汇川采样任务如何产生

入口二只需本次工程 XML 和变量匹配模式。生成器读取 XML 的任务名、任务类型、周期和 `pouInstance`，把已调度的程序、可定位的例程调用与所选通道的明确写入关联起来。

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

Trace 分析支持 TwinCAT Scope `.trace` 和 Sysmac 导出表、分卷目录，时长取起始步观测停留中位数。源码原注释优先进入 A 档，其余行由 Agent 按本步证据完成 B 档。

`workflow_status.json` 的 `out` 记录实际运行目录。`prepared_b_tier_pending` 时完成本轮 `batches_desc/*.result.json`，再以相同参数加 `--resume`。`done_with_review` 表示已出表且有待核项。显式重建用 `--force` 整体归档旧运行。

入口二的配置在客户端导入并完成现场采集后，可将实测导出交给入口一。类型或解析问题由 `needs_review` 状态报告。

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

入口一保留完整源码准备链。候选索引、事件、控制图、`prediction_*` 和 `batches/` 属于准备台账。Agent 的描述任务使用 `batches_desc/`，交付状态对读取 `trace_collection_pairs.csv`。

主交付为 `<工序名>_trace步序采集表.xlsx`。主表「时序拆解」沿用动作分解模板的双行表头、隐藏时间列、工位合并和 13.2 pt 等高数据行。每个完整通道独立分块。起始步能唯一定位到源码例程时，构成要素显示为「工位标识-例程原名」（如 `EM08-SC04_RUN焊印CCD检测`），批注给出完整通道和源码位置。同一通道切换例程时分段显示，原步序顺序保留。例程同名时显示通道，归属无法确认的步序保留工位分组。动作描述后是「描述来源」，末列「证据链」保留观测次数、停留中位数、审计定位、源码关系、动作变量及描述依据，长文本可点开「文本详情」。备注只写需解释的事项，例如简短原注所属流程、两次采样间可能经过的步、估算时长或待核的动作。普通行留空。

明确的待料步用黄底，堵料步用灰底。其它有依据的等待步只在「等待位标识」填 1。动作时间、完成时间、目标时长和三种编号缺少独立依据，均留白。「总时长」为该工位各行中位数之和。

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
