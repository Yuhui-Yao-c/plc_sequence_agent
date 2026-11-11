# PLC Trace 工作流

`trace-only` 提供入口四 Trace 步序采集与入口五 Trace 采集配置。本次同步基线为 `full-workflow` 的 [2dd2dc3](https://github.com/Yuhui-Yao-c/plc_sequence_opencode_v9.25_newbuild/commit/2dd2dc3719863597c74e6e42f809492e6fb1bc53)。

## 启动

在本分支的项目根目录执行。

```powershell
python -m pip install -r requirements.txt
opencode
```

## 两个入口

下列简写可直接发给 OpenCode，也可在终端前加 `python plc_cli.py`。路径和模式含空格时整段加引号。

| 入口 | 简写 | 完整 CLI |
| --- | --- | --- |
| 四　Trace 步序采集 | `源码路径 Trace路径 -t [-output 输出根目录]` | `python plc_cli.py trace --source "源码" --trace "采集" --out "实际运行子目录"` |
| 五　Trace 采集配置 | `源码路径 匹配模式 --trace-setup [-output 输出根目录]` | `python plc_cli.py trace-setup --source "源码" --pattern "模式" --out "实际运行子目录"` |

```text
"source/包mylar/PLC程序.xml" "source/包mylar/采集记录" -t -output "D:\工作成果"
"source/包mylar/PLC程序.xml" "m_nNextAutoStep[*]" --trace-setup -output "D:\工作成果"
```

两个任务分别生成 `D:\工作成果\包mylar_trace\` 和 `D:\工作成果\包mylar_trace_setup\`。省略 `-output` 时以 `outputs` 为根目录，按同样规则创建工序子目录。已有产物时自动加 `_2`、`_3` 等后缀。

## 交付与续跑

入口四支持 TwinCAT Scope `.trace` 和 Sysmac `.csv/.tsv/.txt`、多次采集分卷目录。按完整变量及起止步汇总观测次数和起始步停留中位数。工作簿使用 `<工序名>_trace步序采集表.xlsx`，按工位和步序变量分块，数据行 28 pt，长文本通过「文本详情」双向查看。

有可归属的起始步原注释时采用 A 档。其余行按与同步基线相同的 Trace v4 协议处理 B 档，保留对象、指令、实参、条件及源码引用。`prepared_b_tier_pending` 表示当前批次仍需处理，`done_with_review` 表示已生成工作簿并有复核记录。

完成当前 `batches_desc/*.result.json` 后，用首跑相同参数和实际运行子目录加 `--resume`。具体目录见 `workflow_status.json` 的 `out`。显式重建用 `--force` 归档旧运行。程序与批次的本轮指纹须匹配。

入口五生成 `static_variables.txt`、`workflow_status.json` 和可用的 `<源码名>_trace_config.trace` 或 `.csv`。类型或解析问题进入 `needs_review`。配置导入客户端并采集后，实测导出可交给入口四。

## 源码与中间产物

`plc_cli.py` 只注册 `trace` 和 `trace-setup`。源码准备仍调用与基线相同的 `workflow.create`，保留完整候选索引、事件、控制图、原注释和指令台账。`prediction_*`、`batches/` 等文件也是这条共享准备链的产物，主 Agent 的描述任务使用 `batches_desc/`。

核心取证、解析、描述校验和工作簿模块沿用基线原文件。共享编排函数保持相同参数和处理步骤。工序命名、分卷校验、采样估算、复核状态、输出归档和续跑约束按相同协议执行。

文件职责、调用关系、产物用途和四个技能的加载时机见 [AGENTS.md](AGENTS.md)。本分支的修改记录见 [CHANGELOG.md](CHANGELOG.md)。

## 验证

```powershell
python -m unittest discover -q
opencode debug config
opencode debug agent plc-sequence
opencode debug skill
```

对齐新基线时，应检查共享模块和编排函数，再以同一源码、Trace 和参数逐对比较观测、时长、原注释、B 档上下文、复核与工作簿。入口五逐字节比较变量清单和采集配置。验证跨分支续跑的指纹隔离。
