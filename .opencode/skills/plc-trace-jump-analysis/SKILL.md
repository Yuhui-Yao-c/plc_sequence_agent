---
name: plc-trace-jump-analysis
description: 识别 TwinCAT Scope 和 Sysmac 采集数据，核对观测跳变、多次采集的停留时长中位数及时间依据。
compatibility: opencode
---

# Trace 跳变与时长

## 触发

入口一读取实测记录时加载。

## 输入

| 入口 | 输入 | Python 模块 |
| --- | --- | --- |
| 一 | 源码和 TwinCAT Scope `.trace`、Sysmac 导出表或采集目录 | `plc_trace.trace.trace_to_pairs` |

TwinCAT Scope 文件是 UTF-16 XML，`TraceVariable` 下含 `Values`、`Timestamps`，相对时间单位为毫秒。`StartTime` 提供绝对基准。

Sysmac 输入是 `.csv/.tsv/.txt`，也可用含这类表的目录。采样行含 `Index`、`RawTime` 及带整型后缀的变量列，例如 `Cell.Phase[0]:INT`。采集配置文件无采样行，应先导出现场实测记录。

## 执行

### 入口一的观测状态对

1. 目录顶层保留 `YYYYMMDDhhmmss_00001.csv` 等原名。按前缀识别采集，按卷号读取，核验同次采集的通道、设置、`StartRawTime` 和递增索引。重命名的文件仅在采集归属可以唯一确认时读取。索引缺口切断连续段，重复分卷与冲突按审计处理。
2. 每组独立识别相邻样本跳变和稳定段。非活动通道保留元数据，长空闲段作为周期边界候选。不同采集与断开的索引段之间不构造跳变。保留返回步 0 的观测对。
3. `RawTime` 为纳秒。精度不足时用同次采集的精确时间重建，或使用已知的 `--sample-interval-ms`。采样依据、估算次数、采集 ID 和原始 Index 可追溯。
4. 按完整变量、起始步和结束步统计观测次数，取起始状态停留时长的中位数。源码直跳、静态多步路径、经步 0 和无法解析的关系分别核查。静态路径仅用于解释观测。

## 验收

| 入口 | 核对文件 | 核对内容 |
| --- | --- | --- |
| 一 | `trace_collection_pairs.csv`、`trace_pairs_summary.json`、`trace_pairs_review.csv` | 唯一状态对、发生次数、中位数、估算时长次数、采集分组和源码待核 |
| 一 | 本轮目录下 `trace_variables.json`、`trace_jumps.csv`、`trace_segments.csv`、`trace_cycles.json` | 活跃与未活动变量、采样跳变、连续段和周期候选 |

## 协作

[动作描述](../plc-action-description/SKILL.md) 使用本轮源码证据解释观测对。[采集配置](../plc-trace-setup/SKILL.md) 为前置现场采集提供通道。观测解析只读本轮原始采样。
