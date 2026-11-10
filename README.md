# PLC Sequence Agent

PLC 步序采集 Agent：从 PLC 源码（PLCopen / 欧姆龙 IEC61131-10 / SMC2）和运行时 trace
中提取状态迁移对，生成步序采集表、动作描述与时长回填。

## 四个使用入口

在 OpenCode 对话中输入简写，Agent 自动识别并组装 `plc_cli.py` 命令：

| 入口 | 简写 | 命令 |
|---|---|---|
| 预测 | `XML地址 匹配格式 [trace地址] [人工表地址]` | `python plc_cli.py predict --source ... --pattern ... --out ... [--trace ...] [--manual ...]` |
| GT评价 | `预测CSV路径 GT路径 [源码XML]` | `python plc_cli.py evaluate --prediction ... --gt ... --out ... [--xml ...]` |
| 覆盖评价 | `预测CSV路径 人工表路径` | `python plc_cli.py coverage --prediction ... --manual ... --out ...` |
| trace步序采集 | `XML地址 trace地址 -t` | `python plc_cli.py trace --source ... --trace ... --out ... [--pattern ...] [--force \| --resume]` |
| Trace 采集配置 | `XML地址 变量模式 --trace-setup` | `python plc_cli.py trace-setup --source ... --pattern ... --out ...` |

详见 `AGENTS.md`（权威规则源）。

PLCopen Trace 从本轮 XML 的控制网络、FB 实例和 ST 分支取证。A 档保留归属明确的原注释，后加监控、统计和显示文本隔离。B 档按 v4 证据包组织动作分句，保留已绑定的执行机构、动作及目标位，分别核验多个对象。新工艺术语按当前工程释义。通用分句验证本步事实、对象绑定和目标实参，命名族仅提供辅助线索。默认输出 `<工序名称>_trace步序采集表.xlsx`，也可用 `--proc-name` 指定。源码修订以 Git 提交和 `CHANGELOG.md` 为准。

每个新任务使用空目录。`--force` 将旧输出目录整体归档，再创建新一轮。Agent 按当前批次填写结果，并回传输入的 `run_id` 和 `batch_id`。Trace 同轮用原参数加 `--resume`，预测同轮用 `finish`。对话简写仍为 `源码路径 Trace路径 -t`。

`prepared_b_tier_pending` 表示有缺失、过期或待修订的 B 档结果。`done_with_review` 表示工作簿已生成，源码迁移和动作语义复核分别记录。描述覆盖率和语义准确率分别核验。

## 从源码准备 Trace 采集

```powershell
python plc_cli.py trace-setup --source "source/工序/PLC.xml" --pattern "Cell.Phase[*]" --out "outputs/工序_trace_setup"
```

`static_variables.txt` 保存匹配到的完整变量名。通配符为 `*` 和 `?`，方括号按字面匹配。`--regex` 或 `re:` 可使用完整名称正则。XML 控制体中的固定下标和标量可纳入清单。动态下标、无法确认的类型和 SMC2 解析问题见 `workflow_status.json`。

默认识别汇川/CODESYS 导出及 Sysmac 格式。也可明确使用 `--format inovance` 或 `--format sysmac`。Sysmac 按变量声明解析整数类型及程序局部变量的限定名。已核实的缺失类型可通过 `--default-type` 提供。配置文件只有通道和采集参数。客户端导入后核对任务名、采样设置与通道容量，完成采集后将实测文件交给 `trace`。

Trace 工作簿数据行固定为 28 pt。长描述和证据保留全文，点击下划线可在「文本详情」查看。

## 安装

```powershell
python -m pip install -r requirements.txt
opencode
```

依赖：`lxml` `openpyxl` `pandas`

## 工作流

```
predict  : 源码解析 → 候选冻结 → 原子化 → 信号合并 → 工艺分析 → 批次 → finish → 交付表
trace    : trace跳变解析 → 状态对 → A/B档动作描述 → 交付表（时长回填）
evaluate : 预测CSV + GT → 匹配指标（只读，不修改预测）
coverage : 预测CSV + 人工表 → 覆盖率评价（只读）
```

预测与 trace 不读 GT；评价独立只读。

## 主要交付物

| 文件 | 说明 |
|---|---|
| `prediction_collection_pairs.csv` | 预测主表（非复合，应用合并粒度） |
| `prediction_candidate_pairs.csv` | 完整召回档案（不删） |
| `final_prediction_pairs.csv` | finish 最终主表 |
| `<工序名>_步序采集表.xlsx` | 交付表（预测侧） |
| `<工序名>_trace步序采集表.xlsx` | 交付表（trace 侧） |
| `评价/<工序>_覆盖评价.xlsx` | 覆盖评价（有人工表时） |

## 模块结构

根目录含 42 个 Python 模块，另有 2 个回归测试文件，按依赖层次组织（详见 `AGENTS.md` 模块概览章节）：

- **入口/编排**：`plc_cli.py` `workflow.py` `agent.py`
- **源码解析**：`omron_native.py` `smc2_*.py` `plcopen_*.py` `source_*.py` `extract_*.py`
- **边界/候选**：`boundary_*.py` `atomic_*.py` `candidate_filter.py` `recall.py` `successor_graph.py`
- **合并/后继**：`signal_tail_merge.py` `fb_transport.py` `feedback_successor.py`
- **动作描述/交付**：`action_describe.py` `gen_*_xlsx.py` `station_identity.py`
- **trace**：`trace_jump_analyzer.py` `trace_to_pairs.py` `trace_source_evidence.py` `trace_description_context.py`
- **评价**：`pair_evaluation.py` `atomic_evaluation.py` `评价器/gen_coverage_*.py`

## Skills

PLC 项目技能位于 `.opencode/skills/`，由 AGENTS.md 按入口加载。

- `plc-boundary-normalization` — 起止标签、OR/AND/NOT、最近后继
- `plc-signal-tail-merge` — 动作后信号/数据收尾合并
- `plc-cross-fb-transport` — 跨 FB 触发—到达—接收区间
- `plc-feedback-successor` — 反馈条件转直接后继
- `plc-station-identity` — 工位身份映射
- `plc-coverage-evaluation` — 人工表覆盖对比评价
- `plc-trace-jump-analysis` — 运行时 trace 跳变分析
- `plc-action-description` — 动作描述及分句证据
- `plc-trace-setup` — 从源码准备 Trace 采集配置

## 变更记录

见 `CHANGELOG.md`（v3 ~ v9.28）。
