---
name: plc-trace-jump-analysis
description: 运行 PLC Trace 步序采集并核验观测状态对、停留中位时长、Sysmac 分卷和源码迁移复核。
---

# Trace 步序采集

- 使用 `AGENTS.md` 的命令和本轮输入。先确认源码是 PLCopen、Sysmac IEC61131-10 或 SMC2 导出，采集是 TwinCAT Scope `.trace` 或 Sysmac 表、目录。
- Sysmac 同目录可包含多次采集。保留 `YYYYMMDDhhmmss_00001.csv` 这样的原始文件名。同次采集核对分卷、通道、`StartRawTime` 和 `Index`，遇索引缺口切断连续段。采集配置不一致或归组不明时按程序错误排查。
- 按同变量的相邻观测跳变组成状态对。时长是起始状态的观测停留时间，按完整变量名和起止步值聚合，交付表显示中位数。保留到步 0 的观测。不要合并不同采集的边界、按步号插值或用静态路径补充未观测跳变。
- `RawTime` 精度不足时，仅在导出信息或明确提供的采样间隔支持时重建。查 `trace_pairs_summary.json` 的采集分组和时间依据，查 `trace_collection_pairs.csv` 的估算时长次数。
- 核验实际生成的 `trace_collection_pairs.csv`、`trace_pairs_summary.json` 和 `workflow_status.json`。源码直跳、静态路径和未对齐的记录可在 `trace_source_evidence.json` 和 `trace_pairs_review.csv` 查阅。静态路径是复核线索，工位行时长合计仅供表内汇总。
