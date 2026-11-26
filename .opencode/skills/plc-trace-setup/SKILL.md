---
name: plc-trace-setup
description: 从本次 PLC 源码发现各层执行步，按真实生产动作和工件等待的计时边界生成采集配置，保留备选地址和证据。支持显式指定多个变量模式。
compatibility: opencode
---

# Trace 采集配置

## 输入与发现

从[主 Agent](../../agents/plc-sequence.md)收到工程和采集选项后运行 `python plc_cli.py "工程.xml" --trace-setup`，保存返回的 `out`。候选从本次源码发现。有人工起止表、已确认的地址清单或 Sysmac 通道表时，两轮都可传入同一 `--reference`。用它核对默认清单和最终配置，可按精确地址补齐已知类型。

1. 在 `scope_review_pending` 阶段读取 `scope_candidates.jsonl` 和 `step_variable_discovery.json`。核对 `trace_targets_preview.txt` 及 `preview_configuration` 的实际文件。预配置覆盖有源码证据、类型与采样设置均可确定的候选，正式清单在用途复核后生成。沿调度、调用、实参和固定下标查实际可写的地址。按实例核对工位主步、命令、工作区、并行子步和辅助流程。
2. 为每个 `scope_required` 给出一项判断，再检查 `single_write` 中只写过一个出口的短流程，尤其是由调用方启动或收尾的子步。用该实例的写点和声明用途或步骤注释补录。保留未绑定、动态下标和未定位写点等证据缺口。
3. 先判断地址在程序中如何使用。`role` 取 `station_auto`、`station_slot`、`component_sequence`、`auxiliary_sequence`、`uncertain` 或 `non_sequence`。命令号、计数、应答字也可能在几个数值间切换，核对谁读写它以及这些值控制什么。排除数据通道要引用它的迁移和受控读写、有说明的声明或另一写点。确认内部或辅助序列要引用本实例的迁移。
4. 为工位角色引用本地址的全部声明及 `owner_scope` 内写点，并为每个声称的工位接口族在 `station_families` 填一个具体实例和周期说明。无说明时核对同一例程的其他阶段、写出或可写绑定。`station_slot` 还须提供同一具体数组和工位程序的 `interface_peer`。经证实专门存步的有界叶数组可在已确认实例上标 `dedicated_step_array: true` 并引用声明和迁移。其他仅固定引用或清零的槽位保留在备选清单。

## 默认采集判断

从工件在该工位的一次作业出发，指出操作对象、实际动作或等待，以及哪一处写步标志动作开始、结束或阶段交接。对不同命令、产品模式、并行和旁路逐分支核对。实例的层级和名字不决定采集用途。

- `production` 表示该地址本身能为生产动作或需要独立计时的等待提供边界。记录 `boundaries`，每项含具体动作 `action`、本地址迁移编号 `write` 和一项 `via`。同一地址可有多项。对共用地址的各生产命令、模式及不同等待出口分别留代表写点。不要引用回原命令下同号步作为取料命令的证据。正常生产中的回安全位、等待扫码结果或工件出站均可属于生产动作。
- `via` 可取 `comment`（本写点的步骤注释）、`signal:完整信号名`（该出口条件中的命名反馈）、同阶段受控写出 `eN`、本通道有用途说明的声明或精确元素注释 `dN`、同一具体实例启动或等待子步的关系 `lN`。阅读写点、调用方和佐证原文，核实它们合起来能解释所填动作。通用的“初始”“等待”字样、步骤命名、数值递增、单次调用或家族中的另一成员，均不足以判断生产用途。
- `detail` 表示步骤解释已有动作的内部实现，例如业务步已涵盖扫码触发到结果返回，扫码器建连和收发握手可备查。子步实际区分夹胶、拉胶或另一段需要量时的操作时，保留该子步并指明对应写点。
- `service` 表示本实例只服务于初始化、调试、维护、诊断或设备后台通信。复用同一个功能块的其他实例分别核对。用于生产动作的通信反馈或模式信号可以出现在计时条件中。
- `unresolved` 表示目前不足以判定生产计时用途。填写现场可复核的缺口。提交用途判断后由程序检查排除证据。单向写步、完整分派或父子步有独立依据时，证据不足以排除的地址标为“待核，先采”。确认服务要核实每一写点的维护门控。排除通信细节要找到父动作发出的请求、接收的结果及具体子实例。每个非数据候选都应填写 `use`，缺省按待核处理。

复核私有步序时，先读 `activation` 的初值、同值写入、可执行入口和未解析引用。检查上层例程能否发起这个实例。入口完全依赖父步而父步的来源缺失时保留地址并记录缺口。父步发出请求并读取多个子设备的结果时，逐个核对各子实例完成和复位的时刻。现场采不到某个分支，或父步能量到整体等待，都不足以据此移出子步。

为只在维护模式运行的实例填写 `service_evidence.kind=mode` 和源码中的 `gate`。核实每个入口都依赖此条件、失去模式会撤销运行使能、所引用的执行指令确实受该使能控制。后台设备服务填 `kind=equipment`，同时核实实际调用处对服务用途的说明、被调例程执行的具体指令，以及它与工件请求的关系。两种判断都填写 `purpose` 和 `operation`，格式为 `{"line": 物理XML行号, "text": "该行原文片段"}`。前者引用工程注释，后者引用可执行指令。源码不足时保留地址，复用同一定义的其他实例各自判断。

将判断保存到本轮 `scope_results.json`。复制本轮的 `inventory_sha256` 和 `activation_sha256`，程序合并时重算校验。证据编号均在各自地址内查找。以下为格式示例。

```json
{
  "inventory_sha256": "本轮指纹",
  "activation_sha256": "本轮启动依据指纹",
  "decisions": [
    {"name":"Grip.Phase","role":"component_sequence","basis":["t2","e1"],"reason":"夹取命令等待夹爪到位后进入移送","use":"production","boundaries":[{"action":"夹爪夹紧到位","write":"t2","via":"signal:Grip.Closed"}]},
    {"name":"Meter.Phase","role":"auxiliary_sequence","basis":["t1"],"reason":"电表周期轮询并刷新能耗数据","use":"service"}
  ]
}
```

复用首轮选项执行 `python plc_cli.py trace-setup --source "工程.xml" --out "本轮实际目录" --scope-results "本轮实际目录/scope_results.json"`。程序重算源证据、逐实例校验边界，并保留可证实的父步 `gated_by`。

独立清单中若有本次未跳变的数组通道，核对同一数组实例的执行步、元素类型和声明上下界。即使程序未单独写出该下标，程序也可按独立清单精确补回有声明依据的地址。缺少声明或下标越界时，在 `coverage` 报告差集。

## 输出与验收

- `static_variables.txt` 包含已确认生产步与待核先采地址。写入和分派分散时查看 `dispatch_writes` 的两处位置，写前值留空。`capture_context.csv` 记录每个已核实边界的动作、步值、命令/工况、完整导出条件及其解析情况、出处。表中写步示例是源码证据，现场 Trace 才提供实测时长。复现条件时可加采其中的命令、模式和反馈。
- `optional_variables.csv` 给出备选地址、用途、理由、类型及出处。`step_array_coverage.csv` 给出专用数组边界。全量候选、其他出口、数据排除和解析范围保存在发现台账。
- 核对 `workflow_status.json` 的 `static_variables`、`optional_variables`、`configured_variables` 和 `configuration_complete`。Sysmac 按声明类型生成配置，缺失的生产地址类型在 `unresolved_types.csv`。可写入部分配置时文件名含 `_partial`。已确认以 REAL/LREAL 存离散步值的通道沿用原类型。
- 汇川按源码任务、调度和可定位写点选择采样设置。`needs_review` 表示缺配置、部分配置或独立核验有缺口，查看 `coverage` 的逐地址差集、未核实条件与源码解析缺口。区分 `configuration_complete`（全部入选地址已实际写入）与 `coverage_complete`（本次输入可核验范围内没有剩余缺口）。无论状态，准确报告实际文件路径。若 `configuration` 已存在，同时交付该文件和 `trace_targets` 全部目标，列明已配置和缺失的数量。采集配置样本数为 0。
- 显式指定地址、通配或 `re:` 模式时仍按用户的选择生成配置。多模式取并集。若 XML 只引用外部结构，提供 `missing_member_types.csv`。由完整工程核实后另存，使用 `--member-types 已核实成员.csv`。可沿用本源码的 `scope_results.json`，以 `-output` 选择独立运行目录。已核实成员只适用于能沿声明链定位到同一结构和标量成员的实例。
- Sysmac 显式模式可用已核实的 `--default-type` 补齐缺失的根变量类型。
