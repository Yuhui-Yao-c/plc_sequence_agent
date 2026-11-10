# PLC Sequence Agent

使用 OpenCode 从 PLC 源码和实测 Trace 生成步序、中文动作描述和采集工作簿。全量工作流包含预测、评价、实测 Trace 分析和采集配置。

## 启动

在项目根目录执行。

```powershell
python -m pip install -r requirements.txt
opencode
```

## 五个入口

下表的简写可直接发给 OpenCode。中括号标记可选项，含空格的路径或模式用引号包住。

| 入口 | OpenCode 对话简写 | 命令 |
| --- | --- | --- |
| 一　步序预测 | `源码路径 匹配模式 [Trace文件] [人工表路径]` | `predict` → 填批 → `finish` |
| 二　GT 评价 | `预测CSV路径 GT路径 [源码路径]` | `evaluate` |
| 三　人工表覆盖评价 | `预测CSV路径 人工表路径 [输出xlsx路径]` | `coverage` |
| 四　Trace 步序采集 | `源码路径 Trace路径 -t [-output 输出目录]` | `trace` |
| 五　Trace 采集配置 | `源码路径 匹配模式 --trace-setup [-output 输出目录]` | `trace-setup` |

完整参数和验收规则见 [AGENTS.md](AGENTS.md)。预测侧可用 `python plc_cli.py predict … --tagvars` 单独输出步序变量表。

### 选择 Trace 输出目录

入口四可分析汇川等工程的 `.trace` 和欧姆龙 Sysmac 导出表或分卷目录。入口五从源码准备采集配置。

```text
"source/包mylar/PLC程序.xml" "source/包mylar/采集记录" -t -output "D:\PLC 输出\包 mylar 实测"
"source/包mylar/PLC程序.xml" "m_nNextAutoStep[*]" --trace-setup -output "D:\PLC 输出\包 mylar 配置"
```

省略 `-output` 时，入口四输出到 `outputs/<源码名>`，入口五输出到 `outputs/<源码名>_trace_setup`。默认目录被占用时选择带数字后缀的新目录。两种简写也可在终端加 `python plc_cli.py` 执行。完整 CLI 使用 `--out`。

## 主要交付物

| 入口 | 交付物 |
| --- | --- |
| 一 | `prediction_collection_pairs.csv`、完整候选档案、`final_prediction_pairs.csv`、`<工序名>_步序采集表.xlsx` |
| 二 | `gt_match_metrics.json`、匹配审计和可用的 `gt_match.xlsx` |
| 三 | 彩色覆盖评价工作簿，含逐条对比、汇总、全量预测 |
| 四 | `<工序名>_trace步序采集表.xlsx`、`trace_collection_pairs.csv`、观测与源码复核 |
| 五 | `static_variables.txt`、`workflow_status.json` 和可用的 `<源码名>_trace_config.trace` 或 `.csv` |

Trace 步序采集取起始步观测停留时长的中位数。数据行固定为 28 pt。长文本在「文本详情」查看。`prepared_b_tier_pending` 表示本轮 B 档仍需处理，完成后用原参数加 `--resume` 合并。`done_with_review` 表示工作簿已生成，相关复核保留在输出目录。

采集配置仅含通道和采集参数。导入实际客户端后核对任务、触发、采样和通道容量。客户端导出的实测记录可交给入口四。

## 定制与验证

修改入口、默认路径或加载条件时，从 [AGENTS.md](AGENTS.md) 的「文件职责」和「技能加载表」定位修改位置。需要改变入口四或五默认目录时，同步 `plc_cli.py` 简写解析和回归用例。

在项目根目录用 OpenCode 查看实际配置、主 Agent 和可发现的项目技能。

```powershell
opencode debug config
opencode debug agent plc-sequence
opencode debug skill
python -m unittest discover -q
```

历次修改见 [CHANGELOG.md](CHANGELOG.md)。
