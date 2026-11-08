# 变更记录（CHANGELOG）

## 2026-11-08 离线版同步汇总

本次把离线工程需要同步的修订集中为一个提交。恢复基线为 `c39ae381237ef5705b2f8065e549a41cb2e387e3`，功能实现取自 `ba626b8fea6766b733ee7b496dc175e3deb34830`，保留离线版 `源码路径 Trace路径 -t` 的对话入口。

### 内容

- 保留 Sysmac 原始文件名采集分组、独立时间和索引、通道并集、缺口切断及估算时长审计；继续支持原有 TwinCAT Scope `.trace`。
- 增加 `trace_source_evidence.py`：区分源码直跳、候选路径和待核关系，按精确状态变量及作用域关联本网络注释和指令。观测中间步未被采到时，保留原观测对，静态候选路径另记证据。
- 同步 `trace_to_pairs.py`、`action_describe.py`、`plc_cli.py` 和 `gen_workstation_xlsx.py`。Trace B 档结果校验 `basis`、主指令引用及批次行完整性；无充分证据时留空并进入复核。B 档接受项标为证据归纳候选，完成状态区分待处理和已交付待复核。
- 保留 `workflow.py` 的控制器正则预编译优化，以及 CLI 阶段进度输出。
- 统一 `AGENTS.md`、`.opencode/agents/plc-sequence.md`、动作描述及 Trace 跳变分析规则。对话 `-t` 只选入口四；完整 CLI 的 `predict -t` 保留兼容别名，变量清单推荐显式 `--tagvars`。
- 新增 `test_trace_to_pairs.py`，更新 README 的升级说明和模块清单。

### 升级要求

按本提交更新全部变更文件，使用新的输出目录运行 `python plc_cli.py trace --source ... --trace ... --out ...`。旧 `batches_desc/*.result.json` 需按当前输入重新生成；新结果完成后以同一组参数加 `--force` 续跑。运行输入、输出和本地 `node_modules` 不属于本次代码提交。

`VERSION` 沿用原迁移包 `9.28.0`，本次同步以提交及本日期记录标识。`trace_collection_pairs.csv` 增加源码关系，另输出 `trace_source_evidence.json`；下游应按列名读取，不能依赖固定列序或将 `done_with_review` 当作无待核项。

### 验证与范围

- 8 项回归测试通过，覆盖 Sysmac 采集分组、独立计时、通道筛选、缺口、改名归组、分卷冲突、源码候选路径及描述证据校验。
- 真实 8 个 CSV、4 次采集、75110 个样本：升级前后均为 33908 次跳变、928 个唯一状态对；逐对发生次数和时长统计一致。源码注释描述从 0 行增加到 221 行。
- 原有 `.trace` 样本：升级前后均为 1921 次跳变、174 个唯一状态对；逐对发生次数和时长统计一致。
- 上述验证复用了同一批已提取源码证据；本次汇总未重新运行完整大 XML 解析或完整 25 个 CSV，也没有人工逐行标注的动作描述准确率结果。

---
> 合并自 v3 ~ v9.28 的分版 CHANGELOG，最新版本在前。


---

# v9.28  (源文件: CHANGELOG_V9.28.md)

# V9.28 版本变更说明 — trace 步序采集模式

基线版本：V9.27（STCode 状态比较补充轨道）

## 1. 需求背景

用户需要以 trace 文件为主要处理对象，提取所有采样变量的跳变序列，形成
`var[idx]=from值 → var[idx]=to值` 状态对，生成 trace 步序采集表。例如某变量
曲线为 `EM06V.Process_steps[1]=0→100→110→120`，则产出 3 个相邻跳变对。

trace 文件的相邻跳变天然构成状态迁移对，与 predict 的
`prediction_collection_pairs.csv` 结构同构，下游 action_describe（A/B档动作描述）
和 gen_workstation_xlsx（交付表渲染）可零改动复用。

## 2. 核心设计：复用 trace_jump_analyzer + 候选管线 + 动作描述 + 交付表

关键洞察：trace 变量的相邻 jumps（`from_v → to_v`）与 predict 的状态迁移对
（`开始标签 → 结束标签`）结构同构。只要把 jumps 转成同构 CSV，下游管线不感知
数据来源差异。

因此本版本**不新建独立渲染器**，而是：
- 复用 `trace_jump_analyzer.parse_trace()` 解析 trace（UTF-16 TwinCAT Scope .trace）
- 新建 `trace_to_pairs.py` 做 jumps→pairs 转换（去重合并 + 中位数时长）
- 复用 `workflow.create()` 获取源码证据（动作变量映射/步注释/工位身份）
- 复用 `action_describe.cmd_a/cmd_build/cmd_validate` 生成 A/B 档动作描述
- 复用 `gen_workstation_xlsx.generate` 渲染交付表（sheet='trace步序采集表'）

## 3. 入口契约

**简写**：`XML地址 trace地址 -t`

```
python plc_cli.py trace --source "XML" --trace "trace文件" --out "outputs/目录" [--pattern "格式"] [--force]
```

- 新增 `trace` 子命令（与 predict/evaluate/coverage 并列第四入口）
- `-t` 在 trace 子命令下是可选习惯标志（agent 识别后忽略）
- 现有 predict 的 `-t`/`--tagvars` 步序变量提取语义不变
- 隔离守卫同 `_run_tagvars`：拒绝写入已有 predict/finish 产物的目录（`--force` 强制清理）

## 4. 新建文件

### trace_to_pairs.py

从 trace 文件提取跳变对并渲染交付表输入 CSV。

- **trace 解析**：复用 `trace_jump_analyzer.parse_trace()`，支持 UTF-16 TwinCAT Scope .trace
- **跳变对提取**：每变量的相邻 jumps → `(var=from值, var=to值)`，过滤 `from==to` 伪跳变
- **多周期去重合并**：按 `(var, from, to)` 三元组去重，统计发生次数/首末时间/时长列表
- **时长统计**：中位数（`statistics.median`，抗异常周期）+ min/max/首次/末次时间
- **周期检测**：复用 `detect_cycles()`，标注每对所属周期数
- **复位跳变**：`var→0` 保留，备注列标"复位退出"
- **源码证据关联**：`plcopen_action_trace.csv` 动作变量映射 + `step_comments.json` 步注释 + `station_identity.json` 工位身份
- **输出**：`trace_collection_pairs.csv`（列兼容 `gen_workstation_xlsx.generate` 输入 + 运行时列）+ `trace_variables.json` / `trace_jumps.csv` / `trace_segments.csv` / `trace_cycles.json` / `trace_pairs_review.csv` / `trace_pairs_summary.json`

## 5. 改动文件

### plc_cli.py

- 新增 `trace` 子命令（`--source` / `--trace` / `--out` / `--pattern` / `--force`）
- 新增 `_run_trace_mode()` 辅助函数编排完整流程
- argparse description 版本号 v9.27 → v9.28

### action_describe.py

- `cmd_a()` 的 `src_csv` fallback 链调整：`trace_collection_pairs.csv` 优先（trace 模式用 trace 跳变对，不用候选管线产物）→ `prediction_collection_pairs.csv` → `final_prediction_pairs.csv`
- predict 模式不生成 `trace_collection_pairs.csv`，fallback 链行为不变

### gen_workstation_xlsx.py

- `load_trace_runtime()` 扩展：优先读 `trace_collection_pairs.csv` 的 `中位数时长ms` 列，fallback 到 `trace_analysis/trace_step_runtime.csv` 的 `平均时长ms`
- trace 模式时长列自动回填 trace 中位数

### workflow.py

- `meta['version']` 从 `'9.27'` → `'9.28'`

### AGENTS.md / .opencode/agents/plc-sequence.md

- 新增入口四（trace步序采集）
- 新增 v9.28 节
- frontmatter description 补"trace步序采集模式(trace子命令)"
- 交付物清单补 trace 模式产物

## 6. _run_trace_mode() 编排（5步）

1. **隔离守卫**：检测 stale_markers，`--force` 清理残留
2. **workflow.create()**：全量解析→名称过滤→冻结候选（获取 `plcopen_action_trace.csv` 动作变量映射）
3. **源码证据提取**：`extract_variable_docs` / `extract_network_comments` / `extract_step_comments` / `station_identity.build_identity`（失败不阻断）
4. **trace_to_pairs.generate()**：parse_trace → jumps → 去重合并(中位数) → `trace_collection_pairs.csv`
5. **动作描述 + 交付表**：`cmd_a` → `cmd_build` → `cmd_validate` → `gen_workstation_xlsx.generate(sheet='trace步序采集表', out='<工序名>_trace步序采集表.xlsx')`

## 7. 不做什么（边界）

| 不做 | 理由 |
|---|---|
| 不新建独立 trace 渲染器 | gen_workstation_xlsx 已支持 sheet/out 参数，零改动复用 |
| 不组合跨变量对 | trace 是单变量内部跳变；跨工位交接是 predict 管线的事 |
| 不读 GT/人工表 | 隔离原则 |
| 不跑 finish/coverage | trace 已有运行时数据，不需 LLM 工艺分析或覆盖评价 |
| 不修改 predict 的 -t 语义 | trace 是平行第四入口，不污染现有入口 |

## 8. 验证（转接片 ZJPJIANKONG0906.trace）

| 指标 | 结果 |
|---|---|
| trace 变量数 | 30（25 active, 5 inactive）|
| 总跳变数 | 1921 |
| 检测周期数 | 37 |
| 唯一状态对（去重后）| 165 |
| 有动作变量映射 | 144 / 165（87%）|
| 有步注释（A档）| 133 / 165（81%）|
| 复位退出对 | 32 |
| 伪跳变(from==to)过滤 | 24 |
| 交付表工位分组 | 10 |
| 时长列回填 | 165/165（100%）|
| trace_duration_backfilled | true |

## 9. 文件清单

| 文件 | 操作 |
|---|---|
| `trace_to_pairs.py` | 新建 |
| `plc_cli.py` | 修改（+trace子命令 +_run_trace_mode +版本号）|
| `action_describe.py` | 修改（cmd_a fallback 链调整）|
| `gen_workstation_xlsx.py` | 修改（load_trace_runtime 扩展）|
| `workflow.py` | 修改（版本号）|
| `AGENTS.md` | 修改（+入口四 +v9.28节）|
| `.opencode/agents/plc-sequence.md` | 修改（frontmatter +入口四 +交付物）|
| `CHANGELOG_V9.28.md` | 新建 |

---

# v9.27  (源文件: CHANGELOG_V9.27.md)

# V9.27 版本变更说明 — STCode 状态比较补充轨道

基线版本：V9.26（步序变量清单提取 --tagvars/-t）

## 1. 问题背景

PLCopen 工程的 `<STCode>` 文本里含步序变量等值比较，形态如：
```
gaxActionTrig[3]:=(udt_EM01.adiAutoStep[1]=110);
IF udt_EM12_2.adiAutoStep[2]=310 THEN
```

LD 结构分析（`plcopen_controlflow.py` 的 MOVE+EQ block 接线）看不到这些——它们是
ST 文本里的条件比较，没有 LD 图结构。导致只在 STCode 出现的步序变量（如顶盖焊的
`udt_EM01.adiAutoStep[1]`）在候选里完全缺失。

验证：顶盖焊 `udt_EM01.adiAutoStep[1]=100` 在 STCode 里有 6 处比较，候选 CSV 里 0 处。

## 2. 核心设计：LD 变量名发现 + STCode 同名补充

**不穷举白名单，不硬编码变量名。**

- LD 结构分析已发现状态控制器变量名集合（如 `{adiAutoStep, Process_steps, AutoStep, ...}`）
- STCode 提取器用正则扫 `var[idx]=数字`（排除 `:=` 赋值），**只用 LD 发现的变量名集合过滤**
- `Location_Speed[2]=0`、`PcToPlc_INT[5]=1` 等通信数据被过滤（变量名不在 LD 集合里）
- `udt_EM01.adiAutoStep[1]=100` 被保留（`adiAutoStep` 在 LD 集合里）

验证（顶盖焊）：35 个 STCode 匹配 → 保留 23 个（全 `adiAutoStep`）→ 过滤 12 个（通信数据）。

## 3. 新建文件

### extract_stcode_transitions.py

- `generate(source, out_dir, ld_var_names)` → `{'candidates': [...], 'summary': {...}}`
- 正则：`(?<!:)([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\[(\d+)\]\s*=\s*(\d+)`
  - `(?<!:)` 排除 `:=` 赋值
  - 捕获：完整符号路径 + 下标 + 比较值
- 过滤：`_var_name_of(symbol)` 提取数组变量名，只保留在 `ld_var_names` 集合里的
- 去重：按 `(symbol, value, pou)` 三元组
- 产出候选行：状态 `stcode_state_compare`，开始标签 `var[idx]=value`，结束标签空（不组合迁移对）
- 证据POU：STCode 所属的 `<pou>`/`<action>` name
- 审计：`stcode_transition_candidates.csv` + `stcode_transition_summary.json`

## 4. 改动文件

### agent.py

PLCopen 路径，`signal_merge` proposals 追加后、`sequence_candidates.csv` 写入前：
1. 从 `rows`（已收集的 LD+transport+signal_merge 候选）提取变量名集合 `ld_var_names`
2. 调 `extract_stcode_transitions.generate(source, out, ld_var_names)`
3. 追加候选进 `rows`（用 `seen` 去重）
4. `report2` 新增 `stcode_transitions` 审计段

### AGENTS.md / .opencode/agents/plc-sequence.md

- 标题/frontmatter 版本号 v9.26 → v9.27
- 新增 v9.27 节

### workflow.py / plc_cli.py

- 版本号 9.26 → 9.27

## 5. 不做什么（边界）

| 不做 | 理由 |
|---|---|
| 不硬编码变量名白名单 | LD 变量名发现机制更通用，不穷举 |
| 不组合迁移对 | STCode 只产符号=值原子候选，迁移关系留给工艺分析 |
| 不修改 LD 候选 | 只增不减，LD 来源候选完全不变 |
| 不对 omron/smc2 调用 | STCode 是 PLCopen 格式特有；omron 用 CASE 已有处理 |
| 不读 GT | 隔离原则 |

## 6. 验证策略（三工程不退化）

| 工程 | 验证点 |
|---|---|
| 转接片 | 候选 ≥2772，符号 ≥54，新增 `EM06V.Process_steps[1]/[3]` |
| 顶盖焊 | 候选 >2544，符号 >52，新增 `udt_EM01.adiAutoStep[1]` |
| 超声波 | 候选不退化，LD 来源候选不变 |

## 7. 文件清单

| 文件 | 操作 |
|---|---|
| `extract_stcode_transitions.py` | 新建 |
| `agent.py` | 修改（+STCode 调用 +审计段） |
| `AGENTS.md` | 修改（标题+v9.27节） |
| `.opencode/agents/plc-sequence.md` | 修改（frontmatter） |
| `workflow.py` | 修改（版本号） |
| `plc_cli.py` | 修改（版本号） |
| `CHANGELOG_V9.27.md` | 新建 |

---

# v9.26  (源文件: CHANGELOG_V9.26.md)

# V9.26 版本变更说明 — 步序变量清单提取（--tagvars/-t，复用候选管线）

基线版本：V9.25（动作描述质量增强：变量文档注入 + 简短动作重点 + 缩进修复）

## 1. 需求背景

用户需要一个从原始 XML 中提取**所有步序变量符号**（`var[idx]`）的能力，例如
`udt_EM01.adiAutoStep[5]`、`EM01V.Process_steps[3]`，去重输出成一张表。

## 2. 核心设计：复用候选管线，不重新解析 XML

关键洞察：predict 的候选生成（`agent.py` → `indexed_candidates.csv`）已经从 XML 里
找全了所有 `var[idx]=value` 状态比较——这是它做"开始结束标签提取"的副产品。
**步序变量符号就是开始/结束标签去掉 `=value` 后缀**。

验证：顶盖焊 `indexed_candidates.csv` 2544 条候选，提取出 52 个唯一步序变量符号，
覆盖 EM01-EM20 全部工位。无需重新解析 XML。

因此本版本**不新建独立 XML 解析器**，而是：
- `-t` 仍走 predict 的 `workflow.create()`（全量解析→名称过滤→冻结候选）
- 候选生成完成后，从 `indexed_candidates.csv` 的开始/结束标签列提取符号
- 跳过批次/动作描述/交付表/trace/coverage

## 3. 入口契约

**简写**：`XML地址 匹配格式 -t`

```
python plc_cli.py predict --source "XML" --pattern "格式" --out "outputs/目录" --tagvars
```

- `-t` / `--tagvars` 是 predict 子命令的支路标志
- 检测到该标志时，predict **仍运行候选生成**，但跳过批次/动作描述/交付表/trace/coverage
- `-t` 优先于 `--manual` / `--trace`，不混用

## 4. 新建文件

### gen_stepvars_xlsx.py

从 `indexed_candidates.csv` 提取步序变量符号并渲染交付表。

- **符号提取**：正则 `([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\[\d+\])` 从开始/结束/原始开始/原始结束标签列提取 `var[idx]`，去掉 `=value`
- **通用性**：不限变量名，含 `adiAutoStep`/`AutoStep`/`Process_steps`/`iCmdStep` 等
- **去重统计**：按完整符号去重，统计出现次数
- **工位名关联**：优先 `station_identity.json` 的 display > `variable_documentation.json` 文档 > 实例名去 V 后缀（`EM01V`→`EM01`）兜底
- **排序**：`_em_sort_key` 按 EM/ST 数值排序（EM01<…<EM10，EM07_1<EM07_2）
- **输出**：`<工序名>_步序变量表.xlsx`（2列：工位名 | 步序变量）+ `step_variables.json` / `step_variables.csv`
- **样式**：对齐 v9.22 交付表标准（Arial 9 / 行高 18 / 关闭自动换行 / 表头浅灰底 / 工位名列纵向合并 / 冻结窗格 A4）

## 5. 改动文件

### plc_cli.py

- predict 子命令新增 `--tagvars` / `-t` 标志
- predict() 函数开头检测 `args.tagvars`，若为 True 调用 `_run_tagvars(src,out,args)` 支路返回
- 新增 `_run_tagvars()` 辅助函数：先调 `workflow.create()` 生成候选 → 提取变量文档 + 工位身份 → 调 `gen_stepvars_xlsx.generate` 提取符号并渲染
- argparse description 版本号 v9.25 → v9.26，补充 `--tagvars` 说明

### AGENTS.md / .opencode/agents/plc-sequence.md

- 入口一补充"特殊模式（-t 步序变量提取）"小节
- 末尾新增 v9.26 节
- frontmatter description 补"步序变量清单提取(-t)"

### workflow.py

- `meta['version']` 从 `'9.25'` → `'9.26'`

## 6. 不做什么（边界）

| 不做 | 理由 |
|---|---|
| 不新建独立 XML 解析器 | 候选管线已找全状态比较，步序变量是其副产品 |
| 不写新 skill | 纯确定性提取，无 LLM 判断 |
| 不集成进 finish | `-t` 在 predict 候选生成后短路，不经过 finish |
| 不生成候选对 | `-t` 只输出符号清单，候选对是 predict 正常流程的事 |
| 不读 GT | 隔离原则 |
| 不改 opencode.json | 无新 skill，无需授权 |

## 7. 文件清单

| 文件 | 操作 |
|---|---|
| `gen_stepvars_xlsx.py` | 新建 |
| `plc_cli.py` | 修改（+--tagvars +_run_tagvars +版本号） |
| `AGENTS.md` | 修改（标题+入口一+v9.26节） |
| `.opencode/agents/plc-sequence.md` | 修改（frontmatter+入口一+交付物） |
| `workflow.py` | 修改（版本号） |
| `CHANGELOG_V9.26.md` | 新建 |

---

# v9.25  (源文件: CHANGELOG_V9.25.md)

# CHANGELOG V9.25 — 动作描述质量增强：变量文档注入 + 简短动作重点 + 缩进修复

## 核心改动

### 1. 缩进 bug 修复（plc_cli.py）
**问题**：`extract_network_comments` 和 `extract_step_comments` 的 try/except 块被错误缩进嵌套在 `extract_variable_docs` 的 except 块内。当 `extract_variable_docs` 成功时（不抛异常），except 块不执行 → `step_comments.json` 从不生成 → A 档描述全空。

**修复**：三个 try/except 块改为平级独立，各自独立执行。

### 2. 变量文档注入（action_describe.py describe_token）
**问题**：`describe_token` 对 `EM01V.Cy_Up1.In.Auto_R` 只返回工位名 `EM01_顶盖上料模组`，不查 `Cy_Up1` 的变量文档。B 档 agent 拿不到"上料平移顶升气缸"等具体变量文档。

**修复**：在处理 rest（成员链）时，提取具体变量名（如 `Cy_Up1`、`Axis_Z1`）查 `variable_documentation.json`（6611 条），将变量文档注入 word 字段：
- `Cy_Up1` → `上料平移顶升气缸`（原 `EM01_顶盖上料模组`）
- `Cy_Location1` → `支架固定气缸1`（原 `EM01_顶盖上料模组`）
- `Axis_Z1` → `SV44_支架上料Z轴`（原 `EM01_顶盖上料模组`）
- `LinkOut_Request1` → `设备To物流线：请求上料`（原 `EM01_顶盖上料模组`）

### 3. member_patterns 扩展（action_wordbook.json）
新增/修改模式：
- `.*Cy_.*\.Auto_R` → `气缸复位`（原"气缸自动动作"）
- `.*Cy_.*\.Auto_V` → `气缸动作`（原"气缸自动动作"）
- `.*Axis.*\.Stop_Auto` → `伺服停止`（新增）
- `.*Axis.*\.JOG_Auto.*` → `伺服点动`（新增）
- `.*Axis.*\.Home_Auto` → `伺服回零`（新增）
- `.*Mo_.*\.StartRight` → `电机正转启动`（新增）
- `.*Mo_.*\.StartLeft` → `电机反转启动`（新增）
- `.*LinkOut_Request.*` → `送出请求信号`（新增）
- `.*Station\[N\]\.User_Alarm\[N\]` → `工位报警`（新增）

### 4. 简短动作重点（action_describe.py cmd_build + cmd_validate）
**问题**：B 档 agent 把一行所有 token 的 word+action 全部拼接，产生 54 字符超长描述（"顶盖堆栈出料横移调速电机进料侧电机正转启动顶盖堆栈进料横移调速电机出料侧电机正转启动"）。

**修复**：
- task 指令改为"一行只描述一个主要动作重点，不要把所有tokens拼接到一起"
- 明确优先级"伺服定位>气缸动作>信号，忽略次要信号和报警"
- `max_len` 60→25
- `cmd_validate` 超长阈值 80→30

### 5. cmd_build 注入步注释（action_describe.py）
- 每行增加 `step_comment` 字段（当前行 start/end 标签对应的源码步注释）
- prev/next 增加 `sc` 字段（相邻行的步注释）

### 6. B 档状态如实报告（workflow.py）
finish 报告 `b_tier_input_count`/`b_tier_result_count`/`b_tier_pending`，未处理时 stderr 报警。

## 文件变更

| 文件 | 变更 |
|---|---|
| `plc_cli.py` | 修复 extract_network_comments/extract_step_comments 缩进；版本 9.24.0→9.25.0 |
| `action_describe.py` | describe_token 注入变量文档；cmd_build 注入 step_comment+sc；task 指令+max_len+超长阈值 |
| `action_wordbook.json` | member_patterns 扩展（Cy_*.Auto_R、Axis.Stop_Auto、Mo_.StartRight 等） |
| `workflow.py` | B 档状态如实报告；版本 9.24→9.25 |
| `AGENTS.md` | 新增 v9.25 路由节 |

## 验证结果（转接片）

| 指标 | v9.24 | v9.25 |
|---|---|---|
| A 档有描述 | 0（缩进 bug） | 337 |
| B 档有描述 | 1488 | 1151 |
| 唯一描述 | 46 | 264 |
| 超长(>30字符) | 无数据 | 9（全是 A 档步注释原文） |
| B 档拼接超长 | 无数据 | 0 |
| 描述样本 | "伺服定位"(453次) | "请求上料信号""支架上料Z轴伺服定位""支架固定气缸复位" |
| 测试基线 | 8 failed / 282 passed | 8 failed / 282 passed（无回归） |

## 工作流一致性
所有工序走同一条路径，无工序专属逻辑：
```
predict
  ├─ extract_variable_docs / network_comments / step_comments（三个独立 try）
  ├─ cmd_a（读 collection，describe_token 注入变量文档）
  └─ cmd_build（新 task 指令 + max_len=25 + step_comment）

agent 处理 B 档批次（所有工序相同指令）

finish
  └─ cmd_validate（超长阈值 30，所有工序相同校验）
```

---

# v9.24  (源文件: CHANGELOG_V9.24.md)

# CHANGELOG V9.24 — 工作流重构：predict 生成 A档+B档，finish 一次完成

## 核心改动

### 问题
v9.22-v9.23 中 `cmd_a` + `cmd_build` 在 finish 阶段调用，导致：
1. B 档批次在 finish 才生成，agent 处理后必须再跑一次 finish
2. 第一次 finish 产出半成品（空描述交付表）
3. 第二次 finish 重复计算（cmd_a、cmd_build、trace、coverage 全部重跑）

### 修复
将 `cmd_a` + `cmd_build` 从 finish 移到 predict 阶段：

| 阶段 | v9.23（旧） | v9.24（新） |
|---|---|---|
| predict | 候选→原子化→合并→工艺批次 | 候选→原子化→合并→工艺批次 + **cmd_a + cmd_build** |
| agent | 工艺批次处理 | 工艺批次 + B档批次（可并行） |
| finish（第一次） | cmd_a + cmd_build + 验证 + 交付（半成品） | **cmd_validate + 验证 + 交付（成品）** |

### 重构后的流程
```
predict
  ├─ 解析 → 候选 → 原子化 → 合并 → prediction_collection_pairs.csv
  ├─ cmd_a（读 collection）→ action_desc_a.csv
  ├─ cmd_build → batches_desc/*.input.json
  └─ 工艺批次 batches/*.input.json

agent 处理工艺批次 + B档批次（可并行）

finish（一次完成）
  ├─ 验证工艺 → final_prediction_pairs.csv
  ├─ cmd_validate（合并 B+A）→ action_desc_final.csv
  ├─ trace分析
  ├─ 生成交付表（有描述，成品）
  └─ coverage评价
```

## 文件变更

| 文件 | 变更 |
|---|---|
| `action_describe.py` | cmd_a 改读 `prediction_collection_pairs.csv`（fallback 到 final）；删除 `cmd_btier_fallback` 死代码；空 DataFrame 保护 |
| `plc_cli.py` | predict 末尾调用 cmd_a + cmd_build；版本 9.23.0→9.24.0 |
| `workflow.py` | validate 删除 cmd_a/cmd_build 调用，只保留 cmd_validate；版本 9.23→9.24 |

## 验证结果

### 超声波验证（finish 一次完成）
- cmd_a 读 collection（615行）→ A档 194 有描述
- cmd_build 生成 11 批 input.json
- 旧 result.json 行号对齐（collection 与 final 行序 0 不一致）
- finish 直接 cmd_validate 合并：B档 421 / A档 194 / 待补充 0
- 覆盖率 94.1%（与之前一致）
- finish 不再重新跑 cmd_a/cmd_build

### 转接片验证（finish 一次完成）
- cmd_validate 合并：B档 1208 / A档 280 / 待补充 0
- 待核验 0 条
- trace 时长回填 ✓
- 覆盖率 98.7%

### 测试基线
8 failed / 282 passed（无回归）

## 关键设计决策
1. **cmd_a 读 collection 而非 candidate**：candidate 有 2772 行含 641 重复对，collection 是去重后的 1488 行——B 档工作量减少 46%
2. **fallback 到 final**：旧 run（predict 时没有 cmd_a）仍可在 finish 阶段工作
3. **行号对齐**：collection 与 final 行序完全一致（超声 615 行 0 不一致），旧 result.json 无需重新处理
4. **空候选保护**：配对机候选为 0 时 cmd_a 不报错

---

# v9.23  (源文件: CHANGELOG_V9.23.md)

# CHANGELOG V9.23 — 运行时 trace 跳变分析与时长回填

## 新增功能

### 1. trace_jump_analyzer.py（新建）
解析 TwinCAT Scope View 导出的 `.trace` 文件（UTF-16 LE XML，.NET XmlSerializer 格式），
提取每个 `TraceVariable` 的步值跳变（jump）与稳定段（segment），将预测步序对与
trace 中的实际跳变匹配，给出该步序在运行时的发生次数、首次/末次/平均/最小/最大时长。

**Trace 格式**：
- UTF-16 LE with BOM
- 每个变量 `<TraceVariable VarName="..." Type="System.Double">` 含 `<Values>` 和 `<Timestamps>`
- 时间戳单位毫秒，事件驱动采样（非固定周期）
- 部分变量可能完全未活动（值恒为 0），不报错

**匹配规则**（优先级递减）：
1. **精确匹配**（exact）：trace 中存在 from=val_start → to=val_end 的直接跳变
2. **经0中转匹配**（via_zero）：trace 中存在 from→0→to 序列，且 0 段时长 ≤1000ms
3. **松散匹配**（loose）：trace 中存在 from→to 但中间有其他状态
4. 以上均不匹配 → review 标注 `predicted_but_not_observed`

**时长定义**：预测对 A→B 的时长 = trace 中 B 状态段的 start_ts − A 状态段的 start_ts
（即从 A 状态开始到 B 状态开始的时长，等同于 A 状态持续时长）。多周期取平均值。

**输出文件**（run 目录下 `trace_analysis/` 子目录）：
| 文件 | 内容 |
|---|---|
| trace_variables.json | 变量元数据：样本数、时间范围、活动/未活动、跳变数、唯一值 |
| trace_jumps.csv | 所有跳变：变量、from值、to值、时间戳(ms)、样本索引 |
| trace_segments.csv | 稳定段：变量、值、起始ms、结束ms、时长ms |
| trace_cycles.json | 检测到的生产周期边界（基于长 idle >5s 分段） |
| trace_prediction_mapping.csv | 预测对 ↔ trace 跳变匹配详情 |
| trace_step_runtime.csv | 每对预测标签的实测时长（开始标签, 结束标签, 平均时长ms, 发生次数, 匹配类型） |
| trace_review.csv | 未匹配预测、trace 独有跳变、跨变量对、BOOL标签 |

### 2. plc_cli.py predict --trace 参数
predict 子命令新增 `--trace` 可选参数，传入 .trace 文件路径，存入 workflow_manifest.json。
参数顺序：`--source XML --pattern 匹配格式 --out 输出目录 --trace trace地址 --manual 人工表地址`。
finish 检测到 manifest 中的 trace 时自动运行分析。

### 3. workflow.py validate() 集成
finish 流程在生成交付表之前，检测 manifest.trace，自动调用 trace_jump_analyzer.analyze()，
将分析结果写入 `trace_analysis/` 子目录，并将 stats 记入 workflow_status.json。

### 4. gen_workstation_xlsx.py 时长回填
- 新增 `load_trace_runtime(run)`：从 `trace_analysis/trace_step_runtime.csv` 读取 (开始标签, 结束标签) → 平均时长ms 映射
- 交付表 D 列（时长(ms)）：从 trace_runtime_map 回填，无 trace 时留空
- 总时长行：累加该工位组所有数据行的 trace 时长，有 trace 数据时填入合计
- note 说明文字：有 trace 时显示"时长(ms)与总时长由运行时trace跳变分析自动回填"

### 5. SKILL.md
新建 `.opencode/skills/plc-trace-jump-analysis/SKILL.md`，定义 trace 分析的加载条件、
业务目标、格式说明、跳变/稳定段定义、匹配规则、时长定义、隔离规则、文件输出、边界情况。

## 隔离规则
| 规则 | 说明 |
|---|---|
| 不读人工表（GT） | trace skill 不读人工采集表；coverage 评价独立 |
| 不生成/修改预测 | trace 只验证已有预测，不新增/修改/删除预测对 |
| 不修改源码 | trace 是只读输入 |
| 评价隔离 | trace 匹配结果不回流 coverage 评价 |
| trace ≠ 源码逻辑证明 | trace 证明"这一跳确实发生了"，不证明"源码迁移逻辑正确" |

## 验证结果（转接片）
- trace 文件：ZJPJIANKONG0906.trace（9MB，UTF-16，30 变量，15772 样本/变量）
- 预测对：1488 条
- 匹配成功：181 条（exact:102, via_zero:60, loose:19）
- 未匹配：1307 条（trace 只监测 30 个变量中的 25 个活动变量，预测覆盖更多变量）
- trace 独有跳变：728 条（如实记入 review）
- 交付表时长回填：133 个数据行有实测时长，53 个总时长行有合计
- 覆盖率：98.7%（157/159，与无 trace 时一致，trace 不影响覆盖率）
- 测试基线：8 failed / 282 passed（无回归）

## 版本号
- plc_cli.py: 9.22.0 → 9.23.0
- workflow.py: 9.22 → 9.23
- prediction_provenance.json: 新增 trace 字段

## 文件变更
| 文件 | 变更 |
|---|---|
| trace_jump_analyzer.py | 新建（解析+匹配引擎，约 300 行） |
| .opencode/skills/plc-trace-jump-analysis/SKILL.md | 新建 |
| plc_cli.py | predict 新增 --trace 参数，version 9.23.0 |
| workflow.py | create() 接收 trace 参数，validate() 集成 trace 分析，集成 cmd_btier_fallback 兜底 |
| action_describe.py | 新增 cmd_btier_fallback() 函数，B 档 result.json 缺失时自动生成规则描述 |
| gen_workstation_xlsx.py | 新增 load_trace_runtime()，时长列与总时长行回填 |
| AGENTS.md | 新增 v9.23 路由节 |

## B 档处理一致性（不允许规则脚本冒充 LLM）
**原则**：所有工序的 B 档动作描述必须走同一条路径——agent 逐批处理 `batches_desc/*.input.json` 写 `result.json`，不允许规则脚本兜底冒充 LLM 输出。

**修复**：
- 删除 workflow.py 中的 `cmd_btier_fallback` 调用，finish 不再自动生成 result.json
- finish 如实报告 B 档状态：`b_tier_input_count`、`b_tier_result_count`、`b_tier_pending`（待 agent 处理的批次数）
- B 档未处理时 finish 报警："B档未处理: N批 input.json 待 agent 逐批处理写 result.json 后重跑 finish；当前交付表动作描述为空（仅 A 档步注释）"
- 转接片 31 批 1208 行由 agent 逐批处理生成 result.json（与超声/包mylar/顶盖焊同一路径）

**五工序 B 档一致性**：

| 工序 | input | result | 描述来源 | 有描述 | 含英文 |
|---|---|---|---|---|---|
| 超声 | 11 | 11 | B档421+A档194 | 609/615 | 0 |
| 包mylar | 2 | 2 | A档1836+B档51 | 1887/1887 | 0 |
| 顶盖焊 | 27 | 27 | B档1057+A档12 | 1066/1069 | 0 |
| 转接片 | 31 | 31 | B档1208+A档280 | 1488/1488 | 26(A档CCD/XY/XR) |

---

# v9.22  (源文件: CHANGELOG_V9.22.md)

# V9.22 版本变更说明 — B档动作描述接入finish工作流 + 交付表美观化

日期：2026-11-01
基线版本：V9.21（动作描述纯中文化：通用步注释提取器 + A档优先步注释 + B档LLM补充）
工程背景：V9.21 实现了动作描述纯中文化，但 B 档批次（batches_desc）的生成（cmd_build）和合并（cmd_validate）未接入 finish 工作流——需手动运行脚本。同时交付表存在视觉问题：85%+行被淡黄色填色（失去标记意义）、行高不统一（自动撑高导致高矮不一）、块间空行导致表格不连续。本版将 B 档完整接入 finish 工作流，并对交付表做整体美观化。
测试基线：8 failed / 282 passed（与 V9.21 基线一致，全程未引入新失败）。

## 变更清单

### 1. B档动作描述接入finish工作流（workflow.py + action_describe.py）
- **workflow.py validate()**：在 `cmd_a()` 之后增加 `cmd_build()` 调用，自动生成 `batches_desc/*.input.json`（B档批次输入文件）。
- **action_describe.py cmd_build()**：不再删除已有 `*.result.json`（只清理 `*.input.json`），让 agent 处理结果在 finish 重跑时保留。
- finish 工作流现在完整覆盖动作描述全流程：
  1. `cmd_a()` → A档步注释（自动）
  2. `cmd_build()` → B档批次输入（自动）  ← 新接入
  3. agent 逐批处理 `batches_desc/*.input.json` → `*.result.json`（需agent介入）
  4. `cmd_validate()` → 合并B+A（finish检测到result.json时自动）
- 与工艺批次（batches/）模式完全一致：finish生成input → agent处理 → finish再组装。

### 2. 交付表美观化（gen_workstation_xlsx.py）
- **去掉黄色填色**：85%+行被填色失去标记意义 → 全部去掉填色（0行黄色）。
- **待复核标记移至备注列**：用红色加粗字体 `【待复核】` 标记（仅动作描述为空的行），不再用背景色。
- **统一行高**：所有数据行固定18（不再自动撑高），标题行22/16/20。
- **统一字体**：全表 Arial 9（修复混合 Calibri 11 问题）。
- **去掉块间空行**：表格紧凑连续，工位组间仅用"总时长"行分隔。
- **关闭自动换行**：`wrap_text=False`（配合固定行高，不再高矮不一）。
- **列宽优化**：动作描述42（最宽），标签26，工位18，备注22。
- **泛化注释过滤扩展**：_GENERIC_COMMENT 新增"三色灯/指示灯/蜂鸣器/急停/安全门/光电开关"等，备注列只展示最具体的1条注释。

### 3. 动作描述英文清理增强（action_describe.py clean_step_comment）
- 步注释中的下划线全部去掉（"机台上料_Z轴下降" → "机台上料Z轴下降"）。
- 元素/质量缩写替换为中文（NG→不良、OK→合格、Cu→铜、Al→铝、CCD→视觉检测、ID→编号、EIP→空）。
- 变量名级英文清理（≥2字母的ASCII序列删除，保留A1/B0/B14等工位编号）。
- extract_step_comments.py 同步增强 `_clean_comment()`：去下划线+缩写替换。

## 验证结果

### B档工作流完整验证（超声）

| 步骤 | 结果 |
|---|---|
| 第一次 finish（无B档result） | A档615行，b_tier_batches_built=True，b_tier=False |
| agent 处理B档 | 11批427行 |
| 第二次 finish（有B档result） | **B档421 + A档194 = 615行合并**，b_tier=True |
| 含英文 | **0** |
| 覆盖率 | 94.1%（自动运行） |

### 交付表美观化验证

| 指标 | V9.21（改前） | V9.22（改后） |
|---|---|---|
| 黄色填色行 | 518/612（85%+） | **0** |
| 行高 | None（自动撑高，高矮不一） | **统一18** |
| 字体 | 混合 Arial 9 + Calibri 11 | **统一 Arial 9** |
| 块间空行 | 有 | **去掉** |
| 待复核标记 | 背景色 + 描述后`[D-边界待核验]` | **备注列红色`【待复核】`** |
| wrap_text | True（自动换行撑高） | **False**（固定行高） |

### 动作描述质量（三工序）

| 工序 | 总行数 | 有描述 | 含英文 | 待人工补充 |
|---|---|---|---|---|
| 超声 | 615 | 612 (99.5%) | 0 | 3 (0.5%) |
| 包mylar | 1887 | 1887 (100%) | 0 | 0 |
| 顶盖焊 | 1069 | 1066 (99.7%) | 0 | 3 (0.3%) |

### 测试基线
8 failed / 282 passed（与 V9.21 一致，无回归）

## 版本号 9.21.0 → 9.22.0

## 完整工作流（现在）

```
① predict
   ├─ 源码解析 → 候选池
   ├─ step_comments.json（步注释提取）
   ├─ batches/B*.input.json（工艺批次）
   └─ manifest
        ↓
② agent 处理 batches/B*.input.json → batches/B*.result.json
        ↓
③ finish（第一次）
   ├─ 组装工艺批次 → final_prediction_pairs.csv
   ├─ cmd_a() → A档步注释
   ├─ cmd_build() → batches_desc/*.input.json（B档批次）
   ├─ 检测 batches_desc/*.result.json
   │   ├─ 无 → 复制A档为final
   │   └─ 有 → cmd_validate() 合并B+A
   ├─ 生成交付表
   └─ 自动coverage（有manual时）
        ↓
④ agent 处理 batches_desc/*.input.json → batches_desc/*.result.json
        ↓
⑤ finish（第二次）
   ├─ cmd_a() → A档
   ├─ cmd_build() → B档批次（保留已有result.json）
   ├─ 检测 result.json → 有 → cmd_validate() 合并B+A
   ├─ 生成交付表（A+B版）
   └─ 自动coverage
```

---

# v9.21  (源文件: CHANGELOG_V9.21.md)

# V9.21 版本变更说明 — 动作描述纯中文化：通用步注释提取器 + A档优先步注释 + B档LLM补充

日期：2026-10-31
基线版本：V9.20（交付表精简 + 工位分组修复 + A档优先源码步注释 + --manual自动评价）
工程背景：V9.20 的 A 档描述在包mylar（codesys导出步注释写入CSV列）上效果很好，但超声/转接片的 CSV"动作描述"列全是工作流模板文本（"相邻已观察状态"等），A 档 fallback 到变量翻译，描述列充满英文变量名（"SV16·AbsEx[N]"），达不到交付要求。用户要求"动作描述必须纯中文，不含变量名/英文，向人工表看齐"。
根因分析发现：源码 XML 的 ST 代码中有大量步级 `//中文注释`（如 `abEM08ActionStart[3] :=ST01.AutoStep[2]=210; //Z轴下降`），人工表 97.7% 的描述逐字来自这些注释，但预测管线从未提取它们。
测试基线：8 failed / 282 passed（与 V9.20 基线一致，全程未引入新失败）。

## 变更清单

### 1. 通用步注释提取器 extract_step_comments.py（新建）
- **不针对具体工序**，基于 XML 格式自动判断提取规则：
  - PLCopen（超声/转接片/顶盖焊）：解析 `<STCode>` 标签，正则提取 `:= ... var[idx]=value ...; //中文` 行的步号和注释
  - omron CASE（配对机）：解析 `<BodyContent xsi:type="smcext:StSection">` 下的 `<ST>` 文本，提取 `CASE 变量 OF` 块内的 `数字://中文` 和 `strStepInfo:='中文'`
  - omron FBD（包mylar）：无文本步注释，返回空（走 CSV 列路径）
- 输出 `step_comments.json`：`{状态变量=步值: 中文描述}`
- 多候选择优：同一步号有多条注释（ActionStart=动作描述、ActionDone=完成、计时类）时，优先保留工艺描述类，过滤"计时开始/结束"
- `_clean_comment()` 安全清理：如果正则误匹配了整行代码，用 `rsplit('//', 1)` 只保留注释部分

### 2. predict 集成步注释提取（plc_cli.py）
- predict 阶段自动调用 `extract_step_comments.extract()`，生成 `step_comments.json`（失败不阻断预测，仅 stderr 警告）

### 3. A 档描述优先步注释（action_describe.py cmd_a）
- 新增 `load_step_comments(run)` 加载 `step_comments.json`
- 描述优先级：
  1. `step_comments.json` 查步注释（超声188/转接片280命中）
  2. CSV"动作描述"列（包mylar 1836命中，clean_step_comment 过滤模板）
  3. **留空，不再塞变量翻译**（v9.21 核心改动——变量翻译只进"动作变量"引用列，不进描述列）
- 结果：有描述的行全部是纯中文工艺语义，无英文/变量名

### 4. B 档 LLM 补充增强（action_describe.py cmd_build + cmd_validate）
- `cmd_build()`：只处理 A 档描述为空的行（待 LLM 补充），批次输入增加前后行步注释作为上下文
- `cmd_validate()`：
  - 白名单扩展：新增 `_GENERIC_MECH` 通用机构词集（Z轴/X轴/Y轴/R轴/气缸/电夹爪/取料位/放料位等）
  - 英文校验更严格：描述中不允许任何 ASCII 标识符（≥2字符），确保无变量名泄漏
  - 中文词校验放宽：允许通用机构词（不要求在 tokens 里）

### 5. 交付表修复（gen_workstation_xlsx.py）
- desc_map 加载：NaN → 空字符串（不再显示"nan"）
- `_row_desc()`：空描述时不再 fallback 到 `step_text()`（变量翻译），描述列留空

## 验证结果

### 超声
| 指标 | V9.20（改前） | V9.21（改后） |
|---|---|---|
| 有描述行 | 615（全部有，含变量翻译） | 188（纯中文步注释） |
| 含英文/变量名 | **430** | **3**（仅Cu/AL1元素符号） |
| 描述样例 | `SV16_机台上芯Z轴·AbsEx[N]` | `机台上料_Z轴下降` |
| 描述样例 | `机台上芯电爪·张开; …·夹紧; …·气动下降` | `电夹爪放料` |
| 待 LLM 补充 | 0 | 427 |

### 转接片
| 指标 | V9.20（改前） | V9.21（改后） |
|---|---|---|
| 有描述行 | 1488（全部有，含变量翻译） | 280（纯中文步注释） |
| 含英文 | 全部含变量名 | **4**（仅CCD） |
| 描述样例 | `EM01·Cy_Separate1.In.Auto_R` | `搬运定位气缸伸出` |
| 待 LLM 补充 | 0 | 1208 |

### 包mylar（不劣化）
| 指标 | V9.20（改前） | V9.21（改后） |
|---|---|---|
| 有描述行 | 1836 | **1836**（不变） |
| 描述来源 | CSV步注释 | CSV步注释（不变） |
| 工位组数 | 20 | **20**（不变） |

### 测试基线
8 failed / 282 passed（与 V9.20 一致，无回归）

## 版本号 9.20.0 → 9.21.0

## 五工序步注释提取覆盖

| 工序 | 格式 | 步注释条数 | A档命中 | 待LLM补充 |
|---|---|---|---|---|
| 超声 | PLCopen | 145 | 188行 | 427行 |
| 转接片 | PLCopen | 148 | 280行 | 1208行 |
| 顶盖焊 | PLCopen | 16 | 9行 | 1060行 |
| 配对机 | omron CASE | 971 | 待验证 | 待验证 |
| 包mylar | omron FBD | 0 | 1836行(CSV) | 51行 |

---

# v9.20  (源文件: CHANGELOG_V9.20.md)

# V9.20 版本变更说明 — 交付表精简 + 工位分组修复 + A档优先源码步注释 + --manual自动评价

日期：2026-10-30
基线版本：V9.19（finish 自动 A 档动作描述 + coverage 覆盖评价命令）
工程背景：V9.19 虽完成 A 档自动化与 coverage 命令，但交付存在三个问题——(1) 交付表 12 列过宽且含大量空列；(2) codesys 数组型状态机（m_nNextAutoStep[N]）导致 1887 行塌缩成 1 个工位组；(3) A 档完全忽略源码步注释（覆盖率 97.5%），只堆砌英文变量名；此外评价器与工作流未打通。本版针对性修复。
测试基线：8 failed / 282 passed（与 V9.19 基线一致，全程未引入新失败）。

## 变更清单

### 1. 交付表精简为 7 列 + 染色简化（gen_workstation_xlsx.py）
- **列结构**：12 列 → 7 列（B-H），删除 动作/开始适用/动作时间/完成时间/目标时长 共 5 列。
- 保留列：构成要素 | 动作描述 | 时长(ms) | 动作变量（引用） | 开始标签 | 结束标签 | 备注。
- **染色简化**：4 色优先级（D红/B橙/C蓝/A黄）→ 单一淡黄色（FFF9E6），仅对 D（边界待核验）和 A（动作描述不完整）染色；B（跨工序步）和 C（合并区间）不再染色——二者本质不是错误。
- **标注方式**：动作描述后追加 `[D-边界待核验]` `[A-动作描述不完整]` 文字标注，替代多色方案。
- 说明栏（note）同步更新为淡黄色图例说明。

### 2. 数组型状态机工位分组修复（station_identity.py + gen_workstation_xlsx.py）
- **问题**：codesys 工程 `m_nNextAutoStep[N]`（N=2~20）共用一个变量根，station_identity 只产生 1 条记录，交付表 1887 行全归 1 个工位组。
- **station_identity.py 新增**：
  - `_extract_controller_pou_map()`：从 prediction_candidate_pairs.csv 提取 控制器×证据POU 的频次映射。
  - `_pou_area_to_station()`：从 POU 区域名（如 `EM02_电芯上料_滚轮线_L02_L04`）提取工位编号和中文名。
  - `build_identity()` 数组展开：对 `m_nNextAutoStep[N]` 这类多下标控制器，每个下标独立建立身份，取该下标最高频的 POU 区域作为工位名来源。
- **gen_workstation_xlsx.py 新增**：
  - `workstation()` 优先从 `构成要素` 的 POU 路径直提（`EMxx_name::SCxx_...`），无需 station_identity 也能正确分组。
  - `_变量根` 不再 strip 数组下标，保留 `m_nNextAutoStep[2]` 形式以便 si_map 精确查找。
  - `_row_sort()` 支持数组索引键（先查带下标键，再查裸根 fallback）。
- **效果**：包mylar 从 1 个工位组 → 20 个工位组（EM01_物流线 … EM20_治具回流），全部中文命名。

### 3. A 档优先使用源码步注释（action_describe.py）
- **问题**：A 档 `cmd_a()` 只读动作变量映射，从不读 `final_prediction_pairs.csv` 的 `动作描述`列——而该列在 codesys 工程中携带源码步注释，覆盖率 97.5%。
- **新增 `clean_step_comment()`**：
  - 去掉 `步N-` / `N-` 前缀（如 `步0-模式跳转` → `模式跳转`）。
  - 取多行注释的第一行。
  - **过滤工作流模板文本**（"相邻已观察状态"、"源码局部状态迁移"、"触发...到达信号...参与接收条件"等）——这些不是源码步注释，需 fallback 到变量翻译。
- **`cmd_a()` 优先级**：① 有源码步注释 → 清洗后使用；② 无注释但有动作变量 → 变量翻译（原逻辑）；③ 无注释无变量 → 步进保持/迁移模板。
- **效果**（包mylar）：步注释 1836 / 变量翻译 39 / 模板 12（改前：变量翻译 100%）。
- **不退化保证**（超声）：超声的 `动作描述`列全是工作流模板，`clean_step_comment()` 过滤后步注释=0，全部走变量翻译——与改前完全一致（变量翻译 432 / 模板 183）。

### 4. predict --manual 可选参数 + finish 自动 coverage（plc_cli.py + workflow.py）
- **predict 新增 `--manual`**：可选参数，传入人工采集表路径，存入 `workflow_manifest.json` 的 `manual` 字段。
- **finish 自动 coverage**：`validate()` 检测 manifest 有 `manual` 且文件存在时，自动调用 `gen_coverage_eval.main_with()` 运行覆盖评价，结果写入 `评价/<工序>_覆盖评价.xlsx`，状态记入 `workflow_status.json` 的 `coverage` 段。
- 不传 `--manual` 时行为与之前完全一致；传了则 finish 自动评价。
- 遵循预测/评价隔离：predict 不读人工表内容，只记录路径；coverage 在 finish 阶段（批次完成后）才读。

## 验证结果

### 包mylar 完整验证

| 指标 | V9.19（改前） | V9.20（改后） |
|---|---|---|
| 工位组数 | 1（塌缩） | **20**（EM01-EM20 全中文） |
| 描述来源 | 变量翻译 100% | **步注释 97.5%** + 变量翻译 2% + 模板 0.6% |
| 描述样例 | `步进保持（m_nNextAutoStep[0]: 0→10）` | `模式跳转` |
| 描述样例 | `InOut·=iScanNgCount_A; InOut·=MachineNum[N]...` | `等待上位机扫码完成结果` |
| 描述样例 | `Auto_Threadmedian[0]·BoolData[N]` | `屏蔽判断` |
| 覆盖率 | 未运行 | **99.7%（329/330）** |

### 超声不劣化验证

| 指标 | V9.19（改前） | V9.20（改后） |
|---|---|---|
| 工位组数 | 61 | **61**（不变） |
| 描述来源 | 变量翻译 432 + 模板 183 | **变量翻译 432 + 模板 183**（不变） |
| 测试基线 | 8 failed / 282 passed | **8 failed / 282 passed**（不变） |

## 版本号 9.19.0 → 9.20.0

## 新工作流用法

```bash
# 预测（可选传入人工表）
python plc_cli.py predict --source 源码.xml --pattern "m_nNextAutoStep[*]" --out outputs/工序 --manual source/工序/人工表.xlsx

# 批次处理后 finish（自动 A档描述 + 交付表 + 自动 coverage）
python plc_cli.py finish --run outputs/工序
```

---

# v9.19  (源文件: CHANGELOG_V9.19.md)

# V9.19 版本变更说明 — finish 自动 A 档动作描述 + coverage 覆盖评价命令

日期：2026-10-30
基线版本：V9.18（盲区标色 + 网络注释提取 + 气缸/伺服词典扩展）
工程背景：V9.18 完成了标色和注释提取，但动作描述（action_describe.py）从未被 finish 自动调用，评价器（gen_coverage_eval）也未集成进 CLI。工程师要求"有人工表即评价"。本版将 A 档动作描述自动集成进 finish，新增 coverage 命令封装覆盖评价器，并对超声波跑完整工作流验证。
测试基线：8 failed / 282 passed（与 V9.18 基线一致，全程未引入新失败）。

## 变更清单

### 1. finish 自动生成 A 档动作描述（workflow.py + gen_workstation_xlsx.py）
- **workflow.py validate()** 在生成交付表前自动调用 `action_describe.cmd_a()`（纯源码事实模板，不需 LLM，只读 predict 产物，不接触 GT）。
- 若 `batches_desc/*.result.json` 存在（B 档已跑），自动调 `cmd_validate()` 合并；否则将 A 档复制为 `action_desc_final.csv` 供交付表读取。
- `workflow_status.json` 新增 `action_desc` 审计段（a_tier/b_tier/error）。
- **gen_workstation_xlsx.py** desc_map 加载增强：`action_desc_final.csv` 不存在时 fallback 到 `action_desc_a.csv`（A 档兜底）；列名适配（A档有"A档描述"列，自动重命名为"动作描述"）。

### 2. 新增 coverage 命令（plc_cli.py + 评价器/gen_coverage_eval.py）
- `plc_cli.py` 新增 `coverage` 子命令，封装 `评价器/gen_coverage_eval.py`。
- `gen_coverage_eval.py` 核心逻辑提取为 `main_with()` 函数（可被 CLI 直接调用，不需 argparse）。
- 用法：`python plc_cli.py coverage --prediction ... --manual ... --out ... --proc-name ... --kind codesys|mylar|auto`
- 修复了 `out.mkdir()` 把 xlsx 文件路径当目录创建的 bug（改为 `out.parent.mkdir()`）。
- 遵循预测/评价隔离原则：coverage 是独立命令，不在 finish 里自动跑（AGENTS.md 要求"预测与评价建议在独立会话执行"）。

### 3. 版本号 9.18.0 → 9.19.0

## 超声波完整工作流验证

| 步骤 | 命令 | 结果 |
|---|---|---|
| finish | `python plc_cli.py finish --run outputs/超声_XM22PPM_L5B` | success=true；A档615行（机械动作237/步进155/动作+信号127/信号68/迁移28）；交付表61工位组 |
| 交付表动作描述 | 615/615 行有A档描述，0模板文本 | 气缸"张开/夹紧/气动下降"、伺服"AbsEx"、步进保持 |
| 标色 | D=361 B=28 C=0 A=0（A档覆盖后模板文本归零） | 浅红边界+浅橙跨工序 |
| coverage | `python plc_cli.py coverage ...` | 覆盖率 94.1%（160/170，未覆盖10） |

### 覆盖率详情
- 直接匹配 68 / 多段可合并 25 / 已覆盖·拆分粒度不同 47 / 已覆盖·写法不一致 20 / 未覆盖 10
- 未覆盖根因：拆分粒度不同 47 / NOT离开写法 20 / 跨工位转移·程序无对应 6 / 端点存在但路径不连通 4

## 为什么之前没跑

| 问题 | 原因 | V9.19 解决方案 |
|---|---|---|
| 动作描述没跑 | action_describe.py 是独立 CLI，从未被 finish 调用 | finish 自动调 cmd_a()（A档），B档可选 |
| 评价器没集成 | gen_coverage_eval.py 是独立脚本，未进 CLI | 新增 coverage 命令封装 |
| 交付表动作描述列空 | desc_map 只读 action_desc_final.csv（从不存在） | fallback 到 action_desc_a.csv |

## 与工作流的关系
- A 档动作描述是源码事实模板（机构词[文档]+动作词[词典]+引用），不读 GT/人工表，符合预测隔离。
- B 档（LLM 工艺语义）仍需 agent 逐批处理，不能在 CLI 自动完成；finish 检测到 batches_desc/*.result.json 时自动合并。
- coverage 命令读预测 + 人工表，不修改预测，独立于 finish。

---

# v9.18  (源文件: CHANGELOG_V9.18.md)

# V9.18 版本变更说明 — 盲区标色 + 网络注释提取 + 气缸/伺服词典扩展

日期：2026-10-30
基线版本：V9.17（工位身份映射 station_identity）
工程背景：工程师使用工作流后提出三点意见：①步序按EM排序（V9.16已完成）②盲区/不完整内容行标色 ③动作描述完整（尤其气缸/伺服，提取XML注释，没有就标色）。本版实施 P0（标色框架）+ P1（XML网络注释提取）+ P2（气缸/伺服词典扩展）。
测试基线：8 failed / 282 passed（与 V9.17 基线一致，全程未引入新失败）。

## 变更清单

### 1. P0：盲区标色框架（gen_workstation_xlsx.py）
交付表数据行根据4类盲区判定标注背景色，便于现场人工复核：
- **D类（浅红 FFE6E6）**：边界待核验——该行"来源候选ID"在 `final_atomic_review.csv` 中（无法投影的边界）。
- **B类（浅橙 FFE0CC）**：跨工序步——开始标签变量根≠结束标签变量根（含BOOL开始标签无`=`号的回退提取）。
- **C类（浅蓝 E6F3FF）**：合并区间——"合并ID"非空，需人工确认合并合理性。
- **A类（浅黄 FFF9E6）**：动作描述不完整——有动作变量但动作描述是模板文本。
- **颜色优先级**：D > B > C > A（最严重的覆盖）。
- 说明行（row 2）末尾自动加颜色图例及各类计数。
- 标色只读 predict 产物，不修改预测数据，不影响评价。

**验证结果**：
| 工序 | D(边界) | B(跨工序) | C(合并) | A(描述) | 无色 |
|---|---|---|---|---|---|
| 顶盖焊 | 772 | 0 | 0 | 103 | 194 |
| 超声波 | 361 | 28 | 0 | 159 | 67 |

### 2. P1：XML网络注释提取（extract_network_comments.py + plc_cli.py）
- 新建 `extract_network_comments.py`：从 PLCopen XML 的 `<action>` 元素提取 `<comment>` 网络注释，过滤无意义内容（"网络注释"/"//..."等），去重后输出 `network_comments.json`（action名→[注释列表]）。
- `plc_cli.py predict` 自动调用（失败不阻断，仅 stderr 警告）；版本号 9.17.0→**9.18.0**。
- `gen_workstation_xlsx.py` 新增 `load_network_comments()` + `_best_comments()`，在交付表"备注"列展示该行构成要素对应的网络注释（取最具体的1-2条，过滤泛化注释）。

**验证结果**：
| 工序 | 有注释的action | 注释总数 | 构成要素匹配率 | 交付表有备注行 |
|---|---|---|---|---|
| 顶盖焊 | 91 | 426 | 35/37 | 1038/1069 (97%) |
| 超声波 | 134 | 1013 | 48/48 (100%) | 615/615 (100%) |

### 3. P2：气缸/伺服词典扩展（action_wordbook.json）
扩展通用命名模式（IEC61131-3 + 行业惯例，非逐个工程定制）：
- **members 新增 13 项**：Auto_R/Auto_V/Manual_V（气缸动作）、Location_Auto（伺服定位）、Clamping/Unclamp（夹紧/松开）、MC_MoveAbsolute/Relative/Velocity/Home/Stop/Power（伺服运动控制）。
- **member_patterns 新增 10 项**：`.*Cyl.*\.Auto_R/V/Manual_V`（气缸）、`Axis.*\.Location_Auto.*`（伺服定位）、`.*Clamping.*/.*Unclamp.*`（夹具）、`.*\.MC_Move/Home/Stop/Power`（伺服控制）。
- **roots 新增 6 项**：CylIn/CylOut（气缸）、Axis1In/2In/3In/In（伺服轴）。
- **verbs_whitelist 新增 2 项**：定位、使能。
- 不编造具体动作方向（Auto_R/V 标注为"气缸自动动作"而非猜测伸出/退回），让网络注释或人工复核补充。

### 4. P3：动作描述生成集成（后续工作）
P3 计划将网络注释（P1产物）集成到 `action_describe.py` A档生成，作为动作描述的优先来源。当前需完整跑 `action_describe a/build/validate` 全流程才能验证效果，作为后续工作推进。当前网络注释已在交付表"备注"列展示（P1），工程师可参考。

## 验证汇总
- 测试基线 8 failed / 282 passed 不变。
- 顶盖焊交付表：56工位组、0无中文、标色正确（D=772/B=0/A=103）、备注97%覆盖。
- 超声波交付表：61工位组、0无中文、标色正确（D=361/B=28/A=159）、备注100%覆盖。
- action_wordbook.json 仍是有效 JSON，新模式 re.fullmatch 验证通过。

---

# v9.17  (源文件: CHANGELOG_V9.17.md)

# V9.17 版本变更说明 — 工位身份映射（station_identity）

日期：2026-10-29
基线版本：V9.16（变量文档自动提取 + 交付表工位命名/排序修复）
工程背景：V9.16 修复了顶盖焊交付表质量问题，但方案是"逐个工程加正则前缀"（`udt_EMxx`→`EMxx`），不具备通用性——未来新工程出现 `fbEMxx`/`gvar_EMxx` 等新命名模式时仍需改代码。本版从架构层面解决：**predict 阶段建立工位身份映射，展示层只读不猜**。
测试基线：8 failed / 282 passed（与 V9.16 基线一致，全程未引入新失败）。

## 问题本质
V9.16 之前的架构有一个结构性缺陷：
```
predict 阶段（create）         展示层（gen_workstation_xlsx）
├─ 提取候选（构成要素=POU名）   ├─ 临时 split('.')[0] 算变量根
├─ 边界规范化                  ├─ 临时 _station_code() 猜编号（逐个加正则）
├─ 原子化                      ├─ 临时 workstation() 拼工位名（三级优先级）
└─ 不建立工位身份映射          └─ 无文档时静默用裸名 → 交付表才发现问题
```
工位命名完全是展示层的临时计算，predict 从不建立"状态变量→工位身份"映射，也没有任何工位身份产物。

## 变更清单

### 1. 新增 station_identity.py（predict 阶段工位身份解析）
- 从 `prediction_candidate_pairs.csv` 的"控制器"列提取**实际使用的**变量根（只处理状态机锚点变量，不处理辅助数据结构）。
- 从 `variable_documentation.json` 查文档（stations 子集 + all 全量 fallback）。
- 生成 `station_identity.json`：每个变量根 → `{display, code, doc, sort_key, source, needs_review}`。
- **身份键 = 变量根完整名**，不剥离命名空间前缀（`HJXHX_ST08` ≠ `ST08`）。
- **显示编号剥离**仅限 IEC61131-3 标准类型前缀（`udt_`/`st`/`St`/`ST` 后跟大写字母），命名空间前缀保留。
- **排序键**从变量根最后一个数字组提取 `[主号, 子号]`（`EM07_1`→`[7,1]`）。
- **无文档**→`needs_review=true`，`display=变量根本身`，不静默用裸名。
- 可独立 CLI 运行：`python station_identity.py --run outputs/<run>`。

### 2. plc_cli.py predict 集成（失败不阻断）
- predict 在 var_docs 提取后调用 `station_identity.build_identity()`，生成 `station_identity.json`。
- `candidate_audit.json` 增加 `station_identity` 审计段（total_var_roots / with_documentation / without_documentation / without_documentation_roots）。
- 无文档变量根时 stderr 预警（predict 完成后即可发现，不等交付表）。
- 版本号 9.16.0 → 9.17.0。

### 3. gen_workstation_xlsx.py 优先读 station_identity（fallback 零退化）
- 新增 `load_station_identity(run)` 加载 `station_identity.json`。
- `workstation()` 增加 `si_map` 参数，优先级：
  1. 构成要素 SC04 中文后缀（子工位粒度，用 station_identity 的 code + 子工位名）
  2. station_identity display（工位粒度，如 `EM04_长边清洗工位`）
  3. v9.16 var_docs fallback（station_identity 不存在时）
  4. v9.16 裸 code + section fallback（最后兜底）
- 排序键优先用 station_identity 的 `sort_key`，无映射时用 `_em_sort_key`。
- **v9.16 逻辑完整保留为 fallback**，无 `station_identity.json` 时零退化。

### 4. SKILL 文档（plc-station-identity）
- 新建 `.opencode/skills/plc-station-identity/SKILL.md`。
- 规则：身份键=完整变量根、标准类型前缀剥离、无文档进 review、展示层只读。

## 验证结果

| 工序 | station_identity roots | 有文档 | 无文档 | 工位数 | 无中文 | 与 v9.16 一致 |
|---|---|---|---|---|---|---|
| 顶盖焊 | 22 | 22 | 0 | 56 | 0 | ✓ |
| 超声波 | 18 | 18 | 0 | 61 | 0 | ✓ |

### Fallback 验证（无 station_identity.json）
临时移走 station_identity.json 后重跑：工位数/无中文数/排序**完全一致**（走 v9.16 var_docs 路径），零退化。

### 通用性验证
- `_display_code()` 只剥离 `udt_`/`st`/`St`/`ST` 标准类型前缀，不逐个枚举工程前缀。
- `HJXHX_ST08`（铝焊2）和 `ST08`（滚筒线）身份不塌缩（超声波实际只用 ST01-ST18，HJXHX_STxx 是辅助数据结构不在候选控制器里）。
- 未来 `fbEM05`/`gvar_EM12_3` 等新命名模式：`fb`/`gvar` 不是标准类型前缀 → 保留完整变量根作为身份键，不塌缩。

## 为什么这个方案通用

| 未来场景 | V9.16（逐个加正则） | V9.17（身份映射） |
|---|---|---|
| 新工程变量根 `fbEM05` | 展示层加正则 `fbEM` | station_identity 提取文档，code=`fbEM05`（保留），display=`fbEM05_文档` |
| 新工程变量根无文档 | 交付表裸名，事后发现 | predict 审计报警，station_identity review 可见 |
| 新工程有多个命名空间前缀 | 可能身份塌缩 | 身份键=完整变量根，不塌缩 |
| 新工程 POU 名带中文 | 凑合能用 | station_identity + 构成要素后缀双路径，更精确 |

核心转变：**从"展示层猜名字 + 逐个工程加正则" → "predict 阶段建身份 + 展示层只读 + 无文档进 review"**。

---

# v9.16  (源文件: CHANGELOG_V9.16.md)

# V9.16 版本变更说明 — 变量文档自动提取 + 交付表工位命名/排序修复

日期：2026-10-29
基线版本：V9.15（AGENTS.md v9.15 路由节）
工程背景：顶盖焊（omron Sysmac，98MB）交付表质量差——177 行工位名裸 `SC04_Run` 无中文、步序混排；超声波（PLCopen，89MB）质量可接受。本版针对**工位命名、编号提取、排序**三个根因修复，同时补全 predict 阶段变量文档自动提取。
测试基线：8 failed / 282 passed（与 V9.15 基线一致，预存失败与本版无关，全程未引入新失败）。

## 根因归纳（对照人工表 source\顶盖焊\步序时长采集-XM2-L5-顶盖焊-20261012.xlsx）

| 根因 | 顶盖焊现象 | 超声波为何不受影响 |
|---|---|---|
| **R1：variable_documentation.json 缺失** | predict 从未调用 extract_variable_docs.py；13 个 EM（177 行）构成要素是裸 `SC04_Run` 无中文 | 构成要素自带中文后缀（`SC04_RUN_铜上盘`），不依赖 var_docs |
| **R2：_station_code() 不识别 udt_EMxx** | 工位名带 `udt_` 前缀（`udt_EM20_内侧翻转z`） | 变量根是 `STxx`，恰好原样返回符合预期 |
| **R3：排序键工位名塌缩** | 13 个 EM 塌缩成 1 个 `SC04_Run` 块，12 个 EM 步号混排 | 60 个工位都有中文独立块，排序自然有序 |

## 变更清单

### 1. predict 自动提取变量文档（extract_variable_docs.py + plc_cli.py）
- `extract_variable_docs.py` 核心逻辑提取为可 import 的 `extract(source, out)` 函数；`main()` 调用它（CLI 行为不变）。
- `plc_cli.py predict` 在 `create()` 之后自动调用 `extract_variable_docs.extract()`，生成 run 目录 `variable_documentation.json`；**失败不阻断预测**（try/except，仅 stderr 警告），因为这是展示增强而非预测必需。
- 版本号 9.15.0 → 9.16.0；CLI description 同步。
- 对已有 run（顶盖焊/超声波）手动补生成 variable_documentation.json 验证效果。

### 2. _station_code() 扩展 udt_EMxx 识别 + NaN 防泄漏（gen_workstation_xlsx.py）
- 新增正则分支 `^udt_(EM\d+(?:_\d+)?)$ → EM\d+`，剥离 `udt_` 前缀（如 `udt_EM07_1` → `EM07_1`），对齐人工表编号格式。
- **NaN 防泄漏**：pandas 把 None 转成 float NaN（truthy），`str(NaN)='nan'` 曾泄漏到工位名前缀（`nan_循环线上片`）。修复：`_station_code()` 对 `'nan'/'none'/空` 返回 `''`；`workstation()` 入口用 `pd.notna()` 守卫；`generate()` 的 `_变量根` 计算用 `pd.notna(v) and v` 代替 `if v`。

### 3. workstation() 优先级调整：子工位粒度优先（gen_workstation_xlsx.py）
- **原优先级**：var_docs（工位粒度，如 `ST06_铜侧换盘`）> 构成要素中文后缀（子工位粒度，如 `ST06_铜上盘`）。导致超声波补 var_docs 后子工位被合并（61→33 工位），丢失铜上盘/铜下盘/铜盘移栽区分。
- **新优先级**：构成要素 SC04 中文后缀（子工位粒度，最细）> var_docs（工位粒度）> 非中文后缀兜底 > 裸 code 分组。这样：
  - 超声波 ST06：构成要素 `SC04_RUN_铜上盘` 有中文 → `ST06_铜上盘`（保持子工位粒度）
  - 顶盖焊 EM04：构成要素裸 `SC04_Run` 无中文 → var_docs 命中 → `EM04_长边清洗工位`
  - 顶盖焊 EM19：构成要素 `SC04_Run1` 后缀 `1` 非中文 → 不阻断 var_docs → `EM19_下料机器人工位`

### 4. 裸 SC04_Run fallback 按变量根分组（gen_workstation_xlsx.py）
- 当构成要素无中文、var_docs 也无命中时，用 `_station_code(变量根)` 作为编号前缀（如 `EM04_SC04_Run`），保证**每个 EM 是独立工位块**，不再 12 个 EM 塌缩成 1 个混排块。
- 这是 S1（var_docs）失效时的兜底，保证排序不崩。

### 5. 排序键 EM/ST 编号数值提取（gen_workstation_xlsx.py）
- 新增 `_em_sort_key(name)`：从工位名提取 `(EM|ST)(\d+)(?:_(\d+))?` 数值键，使 `EM01 < EM02 < … < EM10`（而非字符串序 `EM10 < EM2`）和 `EM07_1 < EM07_2`。
- `generate()` 排序键从 `['_工位', '_变量', '_序']` 改为 `['_工位序', '_变量', '_序']`；`groupby(sort=False)` 保持已排序顺序。

## 验证结果

| 工序 | 改前工位数 | 改前无中文 | 改后工位数 | 改后无中文 | 改善 |
|---|---|---|---|---|---|
| 顶盖焊 | 46 | 3（177 行） | 56 | 0 | 全中文，EM01→EM20 数值排序，子工位粒度保留 |
| 超声波 | 61 | 1（9 行） | 61 | 0 | ST02 获中文名 `二次定位`，子工位粒度不退化，`nan_` 前缀消除 |

- 顶盖焊工位名对照人工表：EM01_长边上料工位→EM01_托盘上料Y/磁驱上料Y（子工位）、EM04_长边清洗工位、EM19_下料机器人工位等，全部匹配。
- 超声波工位名保持：ST06_铜上盘/铜下盘/铜盘移栽（子工位粒度未合并）、ST02_二次定位（var_docs 补全）。
- 测试基线 8 failed / 282 passed 不变。

---

# v9.15  (源文件: CHANGELOG_V9.15.md)

# V9.15 版本变更说明 — 交付表命名固化 + 动作描述生成（A/B 档）

日期：2026-10-28
基线版本：V9.14（AGENTS.md 已加路由节）
工程背景：超声波（PLCopen）与配对机（omron Sysmac）两工程全流程已交付；本版聚焦**最终交付物（工位步序采集表）的命名固化、自动生成与动作描述升级**。
测试基线：8 failed / 282 passed（与 V9.14 基线一致，预存失败与本版无关，全程未引入新失败）。

## 变更清单

### 1. 交付表命名固化（gen_workstation_xlsx.py）
- 交付文件固定命名 **`<工序名>_步序采集表.xlsx`**，缺省输出到 prediction 同目录；`--out` 从 required 降为可选。
- 新增 `--proc-name`；缺省从 prediction 目录名推断并剥离 `_vN` 后缀（`配对机_v2`→`配对机`），推断值打印可核对。
- 表内同步对齐交付语义：标题行 `<工序名> 步序采集表（预测交付）`，说明行增加 `工序:` 字段，sheet 名默认 `步序采集表`。
- AGENTS.md 调用示例同步更新。

### 2. finish 自动生成工位表：接线验证 + auto 分组修复
- `workflow.py` finish 成功后自动调用 `gen_workstation_xlsx.generate()`（此前接线已写但从未被真实 run 执行）；本版对超声波/配对机两 run **端到端重跑 finish 验证**，`workflow_status.json` 的 `workstation_xlsx` 记录（工序名/组数/行数/SHA16）落地。
- `pick_group_by()` auto 判定修复：原逻辑要求构成要素**字面含 `SC04`**，导致 omron 工程（构成要素为 `stEM310.ProcessCtrl.iCmdStep` 类结构体路径）被误判到"证据POU"（仅 4 组粗粒度）。修复为：**构成要素有实际区段命名（非空占比 ≥50%）即用构成要素**；仅当构成要素缺失/全空且有证据POU列时才用证据POU。AGENTS.md 措辞同步。

### 3. 中文工位名落地（承接 V9.14 会话尾部，本版验证收口）
- 新工具 `extract_variable_docs.py`：从 PLCopen/omron 双格式源码提取变量级 documentation（流式 iterparse、全局/局部作用域合并、`sStationConrol` 工位结构体单列），输出 run 目录 `variable_documentation.json`（超声波 69 stations/7847 all；配对机 1867）。
- `clean_doc()` 修复：`<GrpComment_zh-cn>` 提取 + `<Sub/>` 后监控 JSON 截断（HJXHX_ST04/ST05 等嵌套文档清理完整）。
- 工位名优先级：开始标签变量根的源码文档（ST13→A贴胶工位）→ 构成要素中带中文的工艺区段 → 区段原名。两交付表工位组 **100% 中文**（SC04_Run 残留归零）。

### 4. 动作描述生成（新模块 `action_describe.py` + 词典 `action_wordbook.json`）
交付表"步序"列从动作变量列表升级为**动作描述**（方案论证后按 A+B 档落地）：

- **A 档（源码事实模板，零 LLM）**：机构词（变量文档四级匹配：精确 token→数组根→基名→前缀，文档缺失用 roots 兜底词典）+ 动作词（成员词典，含 omron 两级成员链 `Pick.bCmd_A`→"取料·动作命令A"，modules 词典 20 项、members 58 项、member_patterns 正则 6 条）+ 纯迁移行标"步进保持"。两工程 614+648 全行覆盖。
- **B 档（LLM 工艺语义，批次机制）**：`build` 生成 `batches_desc/*.input.json`（含 A 档句、tokens 文档、prev/next 跨行上下文、白名单规则）→ OpenCode/agent 逐批写 `*.result.json` → `validate` 白名单校验合并。
- **白名单校验器（防编造三闸）**：① refs ⊆ 该行 tokens；② 纯 ASCII 标识符（≥3 字符或含 `.`/`[`）必须在 tok/机构词/A 档描述中出现过，子串放行（`CCD`⊂`CCD1`）；③ 中文连续串必须命中动词白名单/成员词/该行机构词。失败进 `action_desc_review.csv` 并 A 档兜底。
- **词典纪律修正**：前缀回退收紧（键基串 ≥6 字符，防 `bEM` 短前缀吞语义）；`CallPut/PickOK/PutOK` 信号名细分（呼叫放置/拾取完成/放置完成，member_patterns 正则）；受影响 31 行**主动降级** A 档兜底并记入 review。

### 5. 交付表结构升级（generate 集成 desc_map）
- 列结构：**工位 | 动作描述 | 动作变量（引用） | 开始标签 | 结束标签**（5 列）；`run/action_desc_final.csv` 存在时 col C 自动升级为动作描述，col D 为完整动作变量引用（不再截断，步进行引用列留空）。
- 说明行记录 B/A 档条数比例 + SHA256；`workflow_status.json` 的 `workstation_xlsx` 新增 `b_desc_rows` 与 `desc_sources`。
- 终验：超声波 614/614、配对机 648/648 中文动作描述 100%，标签对与 final CSV 逐行一致；配对机 B 档 475 + A 档 173（review 31 条为词典修正主动降级）；超声波 B 档 429 + A 档 185（review 2 条为真实越界拦截）。

### 6. 交付表格式对齐现场模板（追加变更）
- 以 `source\转接片\4.厦门2转接片 22PPM转接片焊接机步序采集表-20260905步序拆解(已自动还原).xls` 的"时序拆解"sheet 为模板重构渲染：
  - 列结构 12 列：**构成要素 | 动作 | 开始适用(s) | 动作时间(s) | 完成时间(s) | 动作描述 | 时长(ms) | 目标时长(ms) | 动作变量（引用） | 开始标签 | 结束标签 | 备注**；
  - 现场分块习惯：工位块首行写名并**纵向合并至块尾**，块尾加 **"总时长"行**（时长列预测侧不填），块间空行；
  - 时间列（开始适用/动作时间/完成时间/时长/目标时长）为现场实测项，**表头保留、值留空**；"动作"列（源码动作分解）与"备注"列同样留空待填；
  - 内容仍全部来自预测产物：动作描述（A/B 档）、动作变量引用、开始/结束标签。
- 终验：两表块尾=总时长 28/28、32/32；时间列全空；标签对与 final CSV 逐行一致；中文描述 100%。
- 评价侧不受影响（评价仍直接读 CSV）。

## 证据边界（不因升级而模糊）
- 动作描述的工艺语义（取料/放料类）是**保守归纳的推断层**：每条带源码引用、过白名单校验，但不构成对 GT 语义的证明；方向符号（↓/↑/→）静态不可判定，按论证结论不输出。
- review 文件（`action_desc_review.csv`）如实保留：超声波 2 条（行外词/编造 token 拦截）、配对机 31 条（词典修正主动降级）。
- 预测隔离红线不变：动作描述生成不读 GT/人工表；人工表仅在评价侧对照。

## 新增/修改文件
- 新增：`action_describe.py`、`action_wordbook.json`、`extract_variable_docs.py`、`CHANGELOG_V9.15.md`、`PROJECT_SNAPSHOT_V9.15.txt`
- 修改：`gen_workstation_xlsx.py`（命名固化 + desc_map 集成 + pick_group_by 修复 + clean_doc 修复）、`workflow.py`（版本号）、`plc_cli.py`（版本号）、`AGENTS.md`（标题 v9.15 + 交付表/auto 分组/动作描述路由节）
- 运行产物（不入快照）：`outputs/<run>/variable_documentation.json`、`action_desc_a.csv`、`batches_desc/`、`action_desc_final.csv`、`action_desc_review.csv`、`<工序名>_步序采集表.xlsx`

---

# v9.14  (源文件: CHANGELOG_V9.14.md)

# V9.14 版本更新说明 — omron ST 解析增强

日期：2026-10-28
基础版本：V9.13（AGENTS.md 现行路由）
触发背景：配对机工程（omron Sysmac IEC61131-10，工艺以 ST 状态机编写）predict 0 候选。
根因四层：① smcext:StSection 类型未被 ST body 白名单收录（268/397 个 ST 块从未进入解析器）；
② STParser 对函数调用/字符串赋值整块阻断；③ 条件语法不支持变量-变量比较、顺序比较、XOR、
括号比较、进制字面量；④ st_tokens 把紧贴代码的行内注释吞进符号 token。

## 变更清单

### 1. omron_native.py — ST 块入口：枚举 → 结构特征识别 + 未知登记
- ST body 识别改为双轨：`xsi:type` 枚举（ST/STBody/StructuredText/StSection，兼容旧变体）
  **或** BodyContent 存在直接 `<ST>` 子节点（IEC 61131-10 保证的文本载体，未来任何厂商
  命名都能命中）。仅查直接子节点，LdSection/LD 内的 InlineST 不会误入此路径。
- 每个 BodyContent 强制归类进 `native_parse_audit.bodycontent_audit`：
  `parsed_st` / `rung_path`（由 Rung 消费标记判定）/ `skipped_unknown`。
  未读类型显式 warning，不再静默丢弃。
- 取文改为叶子 `<ST>` 文本优先（多 ST 子节点拼接），无 ST 子节点回退 itertext。

### 2. successor_graph.py — STParser 双轨（strict）
- `strict=True`（默认）：Rung 内 InlineST 尾段行为**与 v9.11 完全一致**——不可证语句
  （调用/无参语句/字符串赋值/表达式 RHS/RETURN）阻断整段。尾段合并的零副作用前提不变，
  由既有测试锁定（test_return_blocks_linear_recovery、test_opaque_rhs_blocks_recovery 等）。
- `strict=False`（仅 omron BodyContent 大状态机）：上述语句降级为 `skipped` 记录
  （token 位置/原因/原文 ≤200 字符），body 其余可证写保留：
  - `st_call_may_write_state` / `st_opaque_statement_may_write_state`
  - `st_string_assignment_unmodeled_effects`（步号提示文本 strStepInfo 等）
  - `st_expression_assignment_unmodeled_effects`（bBusy := a OR b 等）
  - RETURN **仍阻断**（其后语句本扫描不可达，提取会伪造线性迁移，v9.11 设计）。
- skipped 计数进 `native_parse_audit.counts.skipped_st_statements`；
  omron parse_st 把 skipped 存入 inline_slices 并写 warning；
  `signal_tail_merge.py` 新增否决理由 `skipped_st_call_may_write_state`——
  含未建模调用的尾段不参与 A→C 合并。
- `case_context` 贯穿 CASE 分支体内嵌套 IF/ELSIF/ELSE 递归（分支标签永不被误当语句）。
- ST_TOKEN 字符类排除 `/`：`AND//注释` 不再把整行注释吞成符号 token；
  除法本就不支持，行为不变差。
- PLCopen 路径（超声波工程）保持 strict=True：已交付的 94.1% 评价结果不受影响。

### 3. boundary_logic.py — 条件语法扩展（全部为"不透明原子"）
新增原子，共同约束：**保留原文、不锚定正状态、satisfiability 记 possible（非证明）**：
- `veq`/`vne`：变量-变量等式/不等式（含 `x = TRUE`）
- `gt/lt/ge/le`：变量 op 整数顺序比较；整数在左时自动交换翻转（`10 > x` → `x<10`）
- `xor`：XOR 节点，**不做布尔化**（按位异或 ≠ 布尔异或），优先级 OR > XOR > AND
- `opaque_cmp`：括号表达式与值比较（`(a XOR b) = 2#1111` 整体保留原文）
- `2#/8#/16#` 进制字面量（`integer()` 正确换基，如 `16#0980` → 2432，可正常锚定）
- negate 映射：veq↔vne、gt↔le、lt↔ge；未识别类型仍走 NOT 包裹默认分支。
- 下游涟漪已核查：comparisons()/substitute()/positive_states() 仅收 eq/ne（新原子自动
  排除，保守正确）；compatible() 未知类型记 possible；fb_transport.required_signals()
  未知类型返回空集；feedback_successor._reduce_true() 原样保留。

### 4. signal_tail_merge.py
- 新增合并否决理由 `skipped_st_call_may_write_state`（见 2）。

## 兼容性与验证
- 全量测试：基线 8 failed / 282 passed（预存失败与本轮无关）→ **每步改动后保持不变**；
  过程中两处回归（RETURN 阻断、opaque RHS 阻断）被 v9.11 测试即时拦截并回退/改为双轨。
- 包mylar（同格式 LD 工程）：bodycontent_audit 全归类（LdSection 444/LD 25/ST 15，
  skipped_unknown=0）；transitions 3216→3632（strict=False 对其 ST/InlineST 的同向增益）。
- 超声波（PLCopen）：路径行为不变。
- 配对机重跑（模式 `stEM*.ProcessCtrl.iCmdStep`）：
  ST bodies 65→333；transitions 2→1944（其中 ProcessCtrl.iCmdStep 663 条，覆盖 28 台 EM）；
  完整候选 0→**1155**；原子对 648；EM100 `0→10` 守卫与源码逐字一致。
  经 141 批语义判定 + finish：事件 5691/5691 全覆盖，steps 663（工艺等待155/检测测量176/
  交接数据126/机械动作57/内部辅助80/未分类69），final_prediction_pairs.csv 交付。

## 已知限制（如实）
- 99 个 ST 块仍解析失败：72 个 FOR 循环（方案外未动）、7 个空括号条件、其余零星
  （缺 THEN、变量-变量顺序比较等）——全部在 blockers/audit 可见，未静默。
- skipped 语句（约 2.9 万条次，含 strStepInfo 文本、调用、表达式赋值）效果未建模，
  仅作审计记录；不参与状态锚定。
- cross_fb 适配器仍仅支持 PLCopen LD/FBD；omron ST 工程跨 FB 输送候选为 0 属预期。
- signal_merge proposals=0 属预期（本工程工艺在 Body 级 ST 状态机，非 LD+InlineST 尾段结构）。

### 5. 交付链：finish 自动生成工位步序交付表
- gen_workstation_xlsx.py 暴露 generate()（返回统计字典）；workflow.validate(write=True)
  在成功组装后自动输出 <工序名>_步序采集表.xlsx（工序名从运行目录名推断去 _vN 后缀）；
  分组auto（构成要素含SC04命名→构成要素，否则有证据POU列→证据POU）；
  工序名/来源/行数/分组/SHA256与"排序仅为展示"声明写入表头注记。
- 依赖缺失或生成失败仅 stderr 警告并记入 workflow_status.json，CSV 交付物不受影响；
  手动重渲染：python gen_workstation_xlsx.py --prediction ... [--group-by auto|构成要素|证据POU|控制器]。

---

# v9.13  (源文件: CHANGELOG_v9_13.md)

# v9.13 — 到位反馈直接后继

- 新增 feedback_successor.py：限定同一状态锚点＋正向BOOL反馈条件，沿完整源码门控的直接赋值寻找后继。
- 结构判定反馈在当前状态下对每条匹配边都是必需条件；OR旁路不伪装为反馈完成；不使用变量名白名单。
- 保留额外使能、完整条件、作用域、竞争/未知写入和采样限制。自环、动态目标、非支持表达式失败关闭。
- AtomicProjector用于预测中的已有候选/工艺草表，也用于独立GT投影；投影后的行仍保持非复合。
- atomic_evaluation / pair_evaluation 对已有完整单对增加“有条件的迁移替代”解释和参考行号；严格分子不放宽。
- 新增 prediction_feedback_successor_audit.csv；原候选、GT和源图不改写。
- 新增Skill及35项通用测试。历史255项测试保持通过。
- 不声称这次新发现已存在的状态对；不把布尔关系或变量名当物理到位证明。

---

# v9.12  (源文件: CHANGELOG_v9_12.md)

# v9.12 — 触发BOOL与跨功能块输送区间

## 根因及修复
1. PLCopen MOVE的匿名输出变量可能藏在connectionPointOut/expression中；此前严格图忽略这一结构。本次修复直接端口表达式读取，匿名比较参数仍按顺序保留。
2. 先前原子视图仅接受状态等式，误将合法的单BOOL发车起点送入待核验。现在单状态等式与单BOOL都能原样保留；AND/OR/NOT仍不可直接删条件。
3. 新增fb_transport.py：从FB定义set/reset生命周期发现发车标志、共同命令和目的到达信号，再绑定每个调用实例，关联接收MOVE的必要到达门控。
4. 修复信号起点事件的批次分组：跟随结束状态控制器，不进入无关事件批。
5. 保留复杂门控的预算限制；超限明确报告，不冒充完整条件。修复真实输出端口读取后不会因重复OR共享前缀指数展开而卡住。

## 验证
255项测试通过，含25项新增测试；随机化变量、数值、缺定义、实例错配、OR旁路、负向到达、未知块、非首接收阶段、BOOL原子化、一对一和完整组装。
完整超声波XML：v9.11过滤完整对481；v9.12为1283。非复合主视图481→622，旧完整对和旧原子对均无丢失。
新增28个跨FB候选，全部进入过滤主表。用户本轮指定的一对从无到有，独立评价严格标签匹配1/1；不是全项目覆盖率。
欧姆龙原生XML：完整候选3793、采集主表1887，两个文件与v9.11逐字节一致；51/52动作后信号收尾合并保持。
Python真实源码与CLI准备均已执行。未执行真实OpenCode/GLM全部工艺批次，不伪造final_process_steps。

## 限制
自动跨FB协议层当前仅PLCopen LD/FBD；结构关系不等于软件触发导致物理到达的形式化证明。
终点可能包含物料、安全、数据等接收条件；实际计时需要上升沿锁存、本次循环/动子ID配对及异常取消。
未运行完整三项目GT回归。本轮GT只有用户提供的1条，其他28项是源码候选而非人工正确标签。

---

# v9.11  (源文件: CHANGELOG_v9_11.md)

# v9.11

- 原生XML的InlineST结构化解析增加受限的标量比较与算术赋值支持，不修改GT标签语法。
- TON输出和数值条件使用符号依赖保留，guard_complete=false，不提升为全局后继等价。
- 新增signal_tail_merge.py：源码动作前驱+正面数据效果+唯一局部路径；支持连续收尾，阻断分叉、回环、新动作、独立等待、未知副作用。
- 主采集视图合并动作与收尾；完整原式/原子候选不删除，并保留路径、条件、源SHA256。
- 新增OpenCode skill，集成predict、finish；评价不能根据GT进行合并或修改预测。
- 对内嵌ST的自动effects支持仍有限；通用角色启发式仍需工艺核验。

---

# v9.10  (源文件: CHANGELOG_v9_10.md)

# v9.10 — 非复合状态对、OR集合skill与独立分支诊断

## 已实现
- 新增OpenCode项目skill：`.opencode/skills/plc-boundary-normalization/SKILL.md`。
- 新增atomic_boundaries.py：主视图两端仅允许单状态等式；严格区分原子形式、局部后继与全局退出等价。
- 以源图求集合第一次退出；多个入口/出口按实际路径分行，不做笛卡尔积，不选数值邻接或更远汇合点。
- OR入口间有内部迁移时防止重复启动计时，进入review；AND/普通反馈不能删除条件或平移整段过程。
- 聚合相同单对来源，输出逐输入lineage、条件与逐跳证据JSON、待核验表。输入保全按台账检查，而非比较去重视图行数。
- predict后新增prediction_atomic_pairs.csv；旧prediction_candidate_pairs.csv仍是完整无损召回档案。
- finish的final_prediction_pairs.csv / final_sequence.csv / final_process_steps.csv改为原子视图；原式保留为*_full / *_raw。
- 新增atomic_evaluation.py，自动按输入表头切换；严格一个原始GT对一个已有完整候选，不拼行。多分支齐全和有条件后继只作诊断。
- 评价Excel增加原子分支表与分类颜色；不修改GT、分母或预测。
- 新增35项测试，总计196项通过。旧v9.7保全测试迁移到无损final_prediction_pairs_full.csv及lineage，保持原检查意图。

## 本轮真实源码回归
完整欧姆龙原生XML重新运行；源码SHA256为2dae58114ff273f7b9bbd1bf1ab7d0b2b435808ed284186e0be98a757a176447。
原始完整候选3601条，与v9.9基线字节一致。3426条来源映射到1887组唯一原子对，175条不能可靠投影，全部保留复核。
本轮1887条的端点形式全部通过；其中1657组带局部明确迁移来源，230组仅保留原始明确标签对而路径/门控未证明。
旧原池GT标签分数仍411/523，不是动作/CT等价分数。原子表严格10、条件替代370、多分支齐全诊断59、其他84。
不得将10+370+59算作严格覆盖；也不能把新视图数量1887与旧来源数量3601直接比较为漏提。

## 未实现 / 限制
- 本次没有提高旧原池标签覆盖率；没有完成所有AND复合事件的单状态别名证明。
- 全局复位、动态赋值和未知功能块仍可阻止严格NOT等价转换；局部后继候选保留这些阻断项。
- 没有运行真实OpenCode/GLM的229个工艺整理批次，最终工艺表通过的是模拟批次集成测试。
- 只重跑本次欧姆龙原生工程，不把其他项目历史缓存当新端到端成绩。
- 未修复GT省略变量、跨控制器或周期疑问；评价中保留原式与原始记录。

---

# v9.9  (源文件: CHANGELOG_v9_9.md)

# v9.9 — 原生IEC61131-10导出适配

## 修复
- 新增按XML根/命名空间的内容分流，保留SMC2与PLCopen旧路径；未知格式明确失败。
- 新增原生Rung、FbdObject/LdObject、DataSource/DataSink、parameterName与端口ID解析。
- 输出端口按网络局部索引，不跨网络/POU串号；ENO与Out分开。
- 完整串并联/取反门控与未知路径分级；未知FB不能用“同网络”冒充因果。
- 原生纯等待和InlineST、独立ST纳入状态/命令证据；注释不作为状态定义。
- 预测/评价共用源图适配，GT不参与生成；一对一彩色评价保留。
- 新增只读原生EQ独立普查工具validate_native_run.py。
- 主预测仍为全量解析→名称过滤→完整候选→最后工艺精选；模型不许删除主候选。

## 验证口径
161项测试通过；对用户95,031,592字节原生XML复现旧版3条，并使用新版本完整重跑。
精确数量、源码SHA256、已恢复标签、语法状态保全和未解析限制见TEST_REPORT.md与本轮结果包。
未运行真实OpenCode/GLM全部批次；未计算本次源码版本的GT真实动作召回率。

---

# v9.8  (源文件: CHANGELOG_v9_8.md)

# v9.8 — SMC2局部门控、等待事件与标签分层诊断

- 新增 `smc2_source.py`：带位置的XML/JSON网络读取；坏块显式审计并保守恢复，不能默默截断后续网络。
- 新增 `smc2_controlflow.py`：CLs/VLs局部条件图，输入/输出分离、并联OR、取反触点、完整源状态到MOVE；未知函数输出、边沿、动态写入保留blocker。
- 在受限内嵌IST中保留外部梯形图门控；无法解释的ST不当成已证明控制边。
- 以同一个源码模型输出状态台账、注释冲突、阶段表和等待事件，预测入口没有GT参数。
- 修复同控制器注释被其他步骤污染；注释中的数字永不用于补状态。
- 修复分批按候选/事件两个列表独立切片导致的证据分离；溢出时仅复制必要证据上下文，不增加主候选。
- 修复Boolean可满足性判断把OR分支中的不同状态错误当成AND矛盾的情况；超限只保守判可能。
- 新增 `diagnose_labels.py` 只读诊断：源码是否观察到状态、主候选是否已有完整对、精选是否存在/未运行。
- 评价XML入口接受XML文本内容的.txt；继续独立读源图，不回写预测。
- 延续候选数量保护、GT一对完整候选匹配和彩色背景。新增源阶段不会再额外复制成通用迁移行来膨胀数量。

本轮不是“把用户反馈的每个步号直接补进去”。已在候选中的记录必须如实报告；源码中没观察到的记录只进诊断，不猜后继。

---

# v9.7  (源文件: CHANGELOG_v9_7.md)

# v9.7
- 新增严格边界表达式树；支持整数类型前缀、布尔交换律、De Morgan、状态集合退出，不默补GT错误。
- 新增源码局部迁移图（PLCopen连线及受限ST），未知writer与作用域歧义显式阻塞。
- 新增最近逻辑首出口规范化：非数字邻接；多分支同一行OR；复合退出跳过集合内部状态。
- 规范化不减候选行；保存原始边界、证据ID、完整guard及复核原因；工艺精选仍后置。
- 用显式工艺契约替代最终精选中的关键词充分条件：采集范围、统计对象、起止意义和来源。
- 评价对预测、GT分别规范化，一条GT一条候选对，形式命中与源码局部模型命中分开。
- 修复GT多行单元格/重复表头读取；超声波本轮输入恢复170条，不再沿用161分母。
- 保持整行彩色Excel；新增蓝色源码模型命中与橙色GT写法待核验。
- 不使用真实项目GT生成预测。移除主预测仓库内的旧真实GT缓存。
- 未宣称支持SMC2严格后继推导；未取得三工程原XML做端到端重跑；未调用本地GLM/OpenCode。

---

# v9.6  (源文件: CHANGELOG_v9_6.md)

# v9.6 回归修复
- 恢复先全量候选，后变量模式过滤，最后工艺语义整理的次序。
- 新增 `prediction_candidate_pairs.csv` 和 `final_prediction_pairs.csv`，消除工艺筛选导致主预测被截断的问题。
- `evaluate --xml` 可独立从PLCopen原XML提取有连线证据的迁移边用于GT候选搜索；不写回预测，不接受GT做预测。
- 仍不把有连线迁移等同于采集时间严格等价。
- 修复仅基于历史代码和局部测试；三个原始工程XML未提供则不声明覆盖率改善。

---

# v9.5  (源文件: CHANGELOG_v9_5.md)

# v9.5 — 从状态EQ向下游寻找有连接证据的工艺事件

- 新增 `plcopen_action_trace.py`：流式解析LD/FBD，构建refLocalId边，从状态EQ沿下游BFS搜索动作线圈、检测/等待相关块、定时器和MOVE；保留XML节点路径；排除孤立元件；限制深度。
- `agent.py`预测流程自动输出 `plcopen_action_trace.csv`、`plcopen_action_trace_summary.json`；`workflow_manifest.json`包含新增trace统计。
- 禁止把连线可达性认定为真值等价或物理完成；无真实工艺动作证据的步号邻接仍保留在回查池，不自动升级成最终动作。
- 新增2项连线搜索回归测试，全库56项测试通过。
- 预测/评价严格分离、候选一对一评价与整行彩色Excel保留自v9.3。
- 未取得三工程完整原始XML，本轮不声明三项目新召回率。

---

# v9.4  (源文件: CHANGELOG_v9_4.md)

# v9.4 工艺动作优先与证据筛选

- 新增 `process_semantics.py`，将候选标为执行机构、工艺检测/等待、纯状态假设、待验证局部迁移等类别；输出 `candidate_process_audit.csv`, `process_focus_queue.csv`, `state_seed_trace_queue.csv`, `process_focus_metrics.json`。
- `workflow.py prepare` 自动生成工艺审查队列；不会从候选总池删除任何记录。
- `workflow.py assemble` 保留完整 `final_sequence.csv`，新增工艺优先 `final_process_steps.csv`，未晋级项放入 `process_review_queue.csv`；这是基于模型已有工艺描述和来源事件的保守筛选，不是完整PLC动作证明。
- 更新OpenCode默认协议：EQ作种子，BFS看相关元件，但必须解析连接语义，纯状态邻接不作为真实工艺动作。
- GT评价路径与预测隔离，已有一对一彩色评价功能保持不变。
- 三项目回归利用之前上传的候选文件，没有重新执行三个大XML和大模型端到端流程。

**风险提示**：PLCopen旧候选往往只有相邻状态，此时重点在 `state_seed_trace_queue.csv` 回查原XML，不能依赖动作关键词过滤直接生成最终表。

---

# v9.3  (源文件: CHANGELOG_v9_3.md)

# v9.3
- 全局一对一GT到预测候选标签对匹配；重复预测对不能多次计分。
- 多等级Excel整行底色和匹配冲突显式输出。
- 保持预测与GT评价隔离。

本轮缓存回归：超声波45/161（重复GT冲突14），转接片122/174，Mylar161/309；非源码重新解析。

本轮缓存回归：超声波45/161（重复GT冲突14），转接片122/174，Mylar161/309；非源码重新解析。

本轮缓存回归：超声波45/161（重复GT冲突14），转接片122/174，Mylar161/309；非源码重新解析。

---

# v9.2  (源文件: CHANGELOG_v9_2.md)

# v9.2：预测与客观评估隔离

- 单入口 `plc_cli.py`：`predict` / `evaluate` / `finish` 三条命令，分别对应两个功能和预测组装步骤。
- 预测不接受GT，也不读取GT；GT仅在evaluate中加载，永不反向影响候选。
- OpenCode AGENTS.md 按两种输入路由：`XML | 通配符` -> 预测；`预测CSV | GT` -> 评价。
- 评价指标只针对提供的预测文件；支持候选表与 `final_sequence.csv`，不能混用两者的匹配数。
- 预测、评价记录SHA256；评价导出逐条GT/TopK候选及xlsx。
- 核心静态解析算法沿用v9.1；本版解决任务路由、重复性和防GT泄漏，不声称三项目真实动作召回率提高。

---

# v9.1  (源文件: CHANGELOG_v9_1.md)

# v9.1 — GT可检索性、可解释的边界匹配

- 新增`gt_search.py`：GT逐行、预测逐候选证据分级检索；安全规范化`INT#100`/`100`、忽略大小写及空白；保留预测原文件行号。
- 精确起止匹配与同起点不同终点、反馈相关、仅状态原子相关分层报告。
- 可选接入PLCopen控制流关系作为**有条件的替代证据**，绝不自动推断同周期采样一致。
- 预构建状态倒排索引，适合大型候选集；来源保留，不向预测注入GT。
- 三项目对比采用上传的v7.2筛选候选缓存，而不是v9.1原始源码重跑；仅代表GT检索器在旧候选上的检索结果。

---

# v9  (源文件: CHANGELOG_v9.md)

# v9 — PLCopen 显式连线局部控制关系

## 新增
- `plcopen_controlflow.py`：针对 PLCopen LD/FBD，通过 `refLocalId` 从数值 MOVE 的 EN 引脚追踪到前级 EQ 比较、触点和布尔功能块，输出 `plcopen_control_relations.csv` 与摘要。
- 仅在沿明确连线发现**同一个目标状态变量**的 EQ 条件且路径无未知节点时，生成 `wired_move_guard_local` 状态候选；未解析的路径单列审计，不能当作已验证。
- 保留 v8 高召回相邻状态假设、事件图和原有过滤；新版本仅在有证据时额外提供强一级的关系。
- 对 AND/OR/NOT 中的 OR、NOT 不做随意等价推理，而是降为不完整证据；未从 EN 连线追溯到状态的不当作因果关系。

## 局限
- 仅支持 PLCopen 标准 LD/FBD 的部分连接写法；厂商扩展、跨POU调用、复杂OR分支和 ST CASE/IF 的精确CFG仍待扩展。
- `wired_move_guard_local` 不证明执行器完成，也不证明 `Step=100 AND PosOK` 与 `Step=110` 时间戳相同。
- 现有环境只有三项目之前上传的候选、事件及人工文件，**没有三份完整原始PLC工程 XML**，因此没有真实三工程 v9 端到端指标。Excel沿用上轮候选基线并明确标注。
- 所有人工参考仅在对照工具中使用，绝不参与候选生成。

---

# v8  (源文件: CHANGELOG_v8.md)

# v8：计时边界证据审计与人工标注异常保护

- 新增 `temporal_audit.py`：从**原始候选及事件图**独立建立 `timing_relations.csv`，不将同网络共现断言为同扫描执行、严格布尔等价或时间戳相等。
- 新增人工标注差异诊断：将缺口细分为状态证据、反馈证据、跨控制器、控制流边界待核验。**没有实际源码反证时绝不把人工表标记为错误**；只对缺口提出复查建议。
- `workflow.py prepare` 自动输出 `timing_relations.csv`，并在manifest里记录关系数量。
- 无特定项目PLC变量/具体数值的逻辑硬编码；变量模式仍由用户指定，过滤仅影响候选展示/整理，不修改全量证据。
- v8没有解决PLCopen LD网络的完整连接/使能链还原；这部分须继续做源码原位连接追踪。用户上传的三个本地结果是候选/事件图，不能冒充新源码的端到端分析。

---

# v7.2  (源文件: CHANGELOG_v7_2.md)

# v7.2 时间采集边界 + 通配变量过滤 + 本地三项目回归

- `candidate_filter.py`: 变量符号级 glob 匹配，支持 `ST*.AutoStep[*]`、`EM*.Process_steps[*]` 和 `m_nNextAutoStep[*]`；开始或结束任一端命中即可。`[]` 按字面处理，`*`、`?` 是通配符；不根据动作描述、POU名称错误命中。
- `agent.py --pattern` 产生全量和筛选版CSV；`workflow.py prepare --pattern` 只把筛选候选送给OpenCode。`assemble` 另导出筛选后最终步序表，并保留 `final_sequence_unfiltered.csv` 审计。
- `timing_equivalence.py` 保守分析 `Step=100 AND Feedback` 与 `Step=110` 的“条件成立后扫描内迁移”关系，不声称事件时间戳无条件相等，也不将相邻状态猜测计为到位反馈。
- 加入10项新单元测试；旧测试保留。
- 本地回归输入为用户上传的三个项目 **候选CSV和人工标签**，不是原始XML；未在本轮重新验证源码解析器和GLM-5.2端到端工艺整理。
- 人工结果可能有不同清洗口径（尤其超声波本次输入实际解析161条），严禁用历史170条替换本次实际分母。

---

# v7.1  (源文件: CHANGELOG_v7_1.md)

# v7.1 回归口径修复（2026-10-21）

**问题定位**：v7发出的三项目对照表来自历史候选回归，不是OpenCode逐批工艺整理后的最终表。GLM-5.2运行结果不具备逐字复现保证，两者不得混称同一版本的同一指标。

**修改**：
- 增加 `benchmark_v7_1.py`：确定性、独立于LLM的候选对照，明确区分“起点已定位/反馈终点缺失”与“未覆盖”；退出状态不能顶替同状态下PosOK/Done反馈完成。
- 增加 `compare_local.py`：同一人工基准分别检查候选基线与本地 `final_sequence.csv`，逐条说明差别，避免将不同输出阶段直接比较。
- `test_v7_1.py` 回归用例包括 ST09、ST01 的反馈边界形式，变量和值只存在测试中，候选解析逻辑中不硬编码。
- 更新默认OpenCode协议，要求保留反馈边界缺失，不要以状态退出自动填充反馈；用户可提供本地最终CSV产生可验证差异报告。

**没有声称解决的能力**：当源PLCopen XML不可用、或解析器不能推导使能到反馈时，不能从人工答案合成完成反馈候选；GLM-5.2生成的动作描述和分组仍可能非确定性。超声波/转接片没有重新进行完整源码端到端运行。

---

# v7  (源文件: CHANGELOG_v7.md)

# v7
- 新增 `workflow.py`：可追溯ID、按控制器分批、断点续跑manifest、事件覆盖核验、最终CSV和待确认清单导出。
- OpenCode默认主Agent改为“一条源码路径→解析→所有批次整理→组装最终草表”工作流。
- 防止遗漏：所有被提取的事件必须有步骤引用或进入 unresolved_events；拒绝错误批次/无效ID/无根据的起止标签。
- 不把 `co_location_only` 当成动作因果证明。
- 只升级任务编排与输出审计，不声称改进v6底层动作识别率；三项目基线不可直接视为v7端到端改进。

---

# v6  (源文件: CHANGELOG_v6.md)

# v6 更新（2026-10-21）
- 新增跨格式 `event_graph_v6.py`，包含 source_unit、guard、command、arguments、feedback、next_state、evidence 列。
- PLCopen LD/FBD 非标准块调用与线圈事件提取；ST布尔赋值事件提取。
- SMC2 JSON网络调用/线圈与反馈统一提取。
- 候选层追加 action_event_unverified 和 feedback_same_body_hypothesis 两类带审计标签的候选。
- 新增 benchmark_v6.py 及 export_match_excel.py，生成逐条匹配 Excel。
- 保留真实性限制：以上新增关系不等于已证明的控制流、完成时刻或全部物理动作召回。

---

# v5  (源文件: CHANGELOG_v5.md)

# v5 修改
- 新增 `event_inventory.py`：SMC2独立扫描有参数的函数块/函数调用与线圈，记录同网络状态比较、反馈、位置及调用参数，保留原始证据。
- 更新 `agent.py`：状态候选之外保留独立动作调用候选，并生成审计报告。无法确认结束事件时留空，不伪造终点。
- 新增 `benchmark_v5.py`：布尔规范化、双分支退出 De Morgan 等价、同网络状态路径的宽松指标，仍明确不等于真实动作覆盖。
- 新增5项回归测试，总计13项通过。
- 本轮仅SMC2项目重新读取原始源码；前两个项目复用缓存候选作为比较基线。

---

# v4  (源文件: CHANGELOG_v4.md)

# v4
- Added streaming extraction of SMC2 JSON CDATA ladder/FBD networks.
- Added generic state-exit candidates `NOT (step=n)` and same-network MOVE-target candidates.
- Added independent TSV/Markdown label benchmark with three distinct statuses.
- Re-ran third project on original source; replayed former projects from cached v3 extracted candidates, clearly labeled as such.
- All uncovered records retained; no program-specific names, indexes, or label rows are embedded in extractor.

---

# v3  (源文件: CHANGELOG_v3.md)

## v3 2026-10-20
- Introduced fast indexed single-pass scan of PLCopen LD comparators and numeric MOVE/ST assignments, avoiding v2 deep graph traversal timeout on 72 MB test XML.
- Generic controller discovery based on observed multiple numeric state comparisons, not `AutoStep` or hard-coded station names.
- Exposed all endpoint hypotheses distinctly as `adjacency_hypothesis`/`reset_hypothesis` rather than presenting them as verified transitions.
- Added independent benchmark with exact vs both-state-anchor metrics and row-by-row misses.
- Tested transfer-plate XML directly; replayed ultrasonic **cached evidence** because its original XML was unavailable for a fresh rerun.
- WARNING: action coverage remains unverified. This is an improvement to coverage observability, not a promise of 100% recall.
