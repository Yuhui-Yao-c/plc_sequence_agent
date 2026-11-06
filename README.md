# PLC Sequence Agent v9.28

PLC 步序采集 Agent：从 PLC 源码（PLCopen / 欧姆龙 IEC61131-10 / SMC2）和运行时 trace
中提取状态迁移对，生成步序采集表、动作描述与时长回填。

## 四个使用入口

在 OpenCode 对话中输入简写，Agent 自动识别并组装 `plc_cli.py` 命令：

| 入口 | 简写 | 命令 |
|---|---|---|
| 预测 | `XML地址 匹配格式 [trace地址] [人工表地址]` | `python plc_cli.py predict --source ... --pattern ... --out ... [--trace ...] [--manual ...]` |
| GT评价 | `预测CSV路径 GT路径 [源码XML]` | `python plc_cli.py evaluate --prediction ... --gt ... --out ... [--xml ...]` |
| 覆盖评价 | `预测CSV路径 人工表路径` | `python plc_cli.py coverage --prediction ... --manual ... --out ...` |
| trace步序采集 | `XML地址 trace地址 -t` | `python plc_cli.py trace --source ... --trace ... --out ... [--pattern ...] [--force]` |

详见 `AGENTS.md`（权威规则源）。

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

运行时核心 39 个 .py，按依赖层次组织（详见 `AGENTS.md` 模块概览章节）：

- **入口/编排**：`plc_cli.py` `workflow.py` `agent.py`
- **源码解析**：`omron_native.py` `smc2_*.py` `plcopen_*.py` `source_*.py` `extract_*.py`
- **边界/候选**：`boundary_*.py` `atomic_*.py` `candidate_filter.py` `recall.py` `successor_graph.py`
- **合并/后继**：`signal_tail_merge.py` `fb_transport.py` `feedback_successor.py`
- **动作描述/交付**：`action_describe.py` `gen_*_xlsx.py` `station_identity.py`
- **trace**：`trace_jump_analyzer.py` `trace_to_pairs.py`
- **评价**：`pair_evaluation.py` `atomic_evaluation.py` `评价器/gen_coverage_*.py`

## Skills

7 个 PLC skill 在 `.opencode/skills/`，由 AGENTS.md 按场景加载：

- `plc-boundary-normalization` — 起止标签、OR/AND/NOT、最近后继
- `plc-signal-tail-merge` — 动作后信号/数据收尾合并
- `plc-cross-fb-transport` — 跨 FB 触发—到达—接收区间
- `plc-feedback-successor` — 反馈条件转直接后继
- `plc-station-identity` — 工位身份映射
- `plc-coverage-evaluation` — 人工表覆盖对比评价
- `plc-trace-jump-analysis` — 运行时 trace 跳变分析

## 变更记录

见 `CHANGELOG.md`（v3 ~ v9.28）。
