# PLC Trace 工作流

本项目通过 OpenCode 完成两项任务。入口一分析 PLC 源码和实测 Trace，生成中文步序采集表。入口二从源码提取显式变量，生成采集通道配置。

当前简化版为 **10.1.0**，维护分支为 `trace-only`。运行 `python plc_cli.py --version` 查询本地版本。版本记录、分支用途和升级规则见 [版本与分支管理](docs/VERSIONING.md)。

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
| 二　Trace 采集配置 | `源码路径 匹配模式 --trace-setup [-output 输出根目录]` |

```text
"source/包mylar/PLC程序.xml" "source/包mylar/采集记录" -t -output "D:\工作成果"
"source/包mylar/PLC程序.xml" "m_nNextAutoStep[*]" --trace-setup -output "D:\工作成果"
```

两个任务分别输出到 `D:\工作成果\包mylar_trace\` 和 `D:\工作成果\包mylar_trace_setup\`。省略 `-output` 时使用 `outputs` 根目录。已有产物时自动选用带 `_2`、`_3` 等后缀的新子目录。

## 命令与续跑

根目录 `plc_cli.py` 是统一启动脚本。完整 CLI 的 `--out` 指定实际运行子目录。

```powershell
python plc_cli.py trace --source "source/包mylar/PLC程序.xml" --trace "source/包mylar/采集记录" --out "outputs/包mylar_trace"
python plc_cli.py trace-setup --source "source/包mylar/PLC程序.xml" --pattern "m_nNextAutoStep[*]" --out "outputs/包mylar_trace_setup"
```

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

主交付为 `<工序名>_trace步序采集表.xlsx`。表内按工位和完整变量分块，数据行高 28 pt，长文本通过「文本详情」查看。源码迁移、描述推断和估算时长分别保留依据与复核文件。

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
