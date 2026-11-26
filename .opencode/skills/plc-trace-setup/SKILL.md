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

逐个实例回答“哪一项生产操作，用这个地址的哪两个值计时”。先查当前值在哪处分派、哪个条件或调用写入后继值，再查该实例所属的工位、生产命令、运行模式及可执行入口。人工表存在时，对照工步的开始和结束标签，读清其中的命令、条件和反馈。人工表之外的源码分支也按同一标准核验。记录未观测模式与不完整的解析证据。

| XML 中的关系 | 用途判断 |
| --- | --- |
| 工位步停在等待阶段，收到子流程完成值后推进 | 工位步给出工步耗时。将子流程的完成值记入工位步的出口条件。 |
| 子步与父步运行重叠，子步自身的取值划分了需要分开计时的生产操作 | 查实启动条件及子步在该模式下执行的操作，保留有独立计时边界的子步。 |
| 设备内部细分请求、应答、收发或复位 | 填 `detail`，说明工位用哪个地址量哪一项操作。某个请求在多台设备并行运行，本身不足以认定每台设备内部都有一项生产工步。 |
| 手动单循环、标定或后台诊断 | 追查实际实例的入口和执行指令，有依据时填 `service`。同一定义的其他实例分别判断。 |
| 读到了状态分派或反馈，尚无法证明归属哪项生产工步 | 填 `unresolved` 并指出缺哪个写点、绑定关系、生产入口或计时边界。保留在备选表中复核。 |

`production` 需要本实例的两组依据。

1. 在 `boundaries` 填写具体动作 `action`、本地址迁移编号 `write` 和佐证 `via`。同一地址可有多项。分别核对不同生产命令、模式和等待出口。共用步号时检查外层命令。佐证可以是本写点的步骤注释 `comment`、出口中的具体反馈 `signal:完整信号名`、同阶段受控写出 `eN`、有用途说明的声明或元素注释 `dN`，也可引用处于同一阶段的父子关系 `lN`。
2. 在 `timing_basis` 填写 `scope`（工位工步用 `workstation`，独立生产子步用 `production_substep`）、计时的对象和操作 `operation`，以及上面某个本实例写点 `boundary_write`。指出该步的进入或离开如何划定这项操作。对独立生产子步，或被上层启动、等待的地址，补充 `parent_relation`，写清上层计量范围和子步要单列的生产操作。该实例有上层控制链接时，在 `parent_link` 填与该实例相连的 `lN`。没有可见上层链接时，独立生产子步仍要说明其直接生产动作依据。被父步读取一次、与工件存在关联、内部还有更细的动作、单独测量有助排障，这些事实都不足以证明它承担一道独立生产工步。

对用途存疑的私有变量，检查 `activation` 的初值、同值写入、启动入口、未解析引用。对维护模式，可填 `service_evidence.kind=mode` 和 `gate`，核实启动、撤销及受控指令。后台设备服务用 `kind=equipment`，核实实际调用处的用途与被调指令。两种判断都填写 `purpose` 和 `operation`，每项用 `{"line": 物理XML行号, "text": "该行原文片段"}` 分别引用注释与可执行指令。缺少有效入口、父步启动或类型证据时，逐项记录缺口。外围路径写中文、变量带 Step 或位于功能块内部均不能决定其用途。

将判断保存到本轮 `scope_results.json`，复制本轮的 `inventory_sha256` 和 `activation_sha256`。证据编号在各自的候选内查找。下例假定 `t2` 为夹爪闭合后进入下一生产阶段的实际写点，`l1` 对应本实例的父步。

```json
{
  "inventory_sha256": "本轮指纹",
  "activation_sha256": "本轮启动依据指纹",
  "decisions": [
    {
      "name": "Grip.Phase",
      "role": "component_sequence",
      "basis": ["t2", "e1"],
      "reason": "自动装配时，夹爪夹紧后进入移送",
      "use": "production",
      "boundaries": [{"action": "夹爪夹紧到位", "write": "t2", "via": "signal:Grip.Closed"}],
      "timing_basis": {
        "scope": "production_substep",
        "operation": "装配工位夹紧工件",
        "boundary_write": "t2",
        "parent_relation": "上层记录整次装配。夹紧和移送是分别计时的两项操作，t2 为夹紧结束。",
        "parent_link": "l1"
      }
    }
  ]
}
```

复用首轮选项执行 `python plc_cli.py trace-setup --source "工程.xml" --out "本轮实际目录" --scope-results "本轮实际目录/scope_results.json"`。程序逐实例核验计时边界与上层关系。旧用途判断中缺少本轮所需计时依据的生产候选留待复核。

独立清单中若有本次未跳变的数组通道，核对同一数组实例的执行步、元素类型和声明上下界。即使程序未单独写出该下标，程序也可按独立清单精确补回有声明依据的地址。缺少声明或下标越界时，在 `coverage` 报告差集。

## 输出与验收

- `static_variables.txt` 列出正式采集的生产计时地址和独立核验指定的具体步序地址。写入和分派分散时查看 `dispatch_writes` 的两处位置，写前值留空。`capture_context.csv` 记录每个已核实边界的动作、步值、命令/工况、完整导出条件及其解析情况、出处。表中写步示例是源码证据，现场 Trace 才提供实测时长。复现条件时可加采其中的命令、模式和反馈。
- `optional_variables.csv` 给出备选地址、用途、理由、类型及出处。`step_array_coverage.csv` 给出专用数组边界。全量候选、其他出口、数据排除和解析范围保存在发现台账。
- 核对 `workflow_status.json` 的 `static_variables`、`optional_variables`、`configured_variables` 和 `configuration_complete`。Sysmac 按声明类型生成配置，缺失的生产地址类型在 `unresolved_types.csv`。可写入部分配置时文件名含 `_partial`。已确认以 REAL/LREAL 存离散步值的通道沿用原类型。
- 汇川按源码任务、调度和可定位写点选择采样设置。`needs_review` 表示缺配置、部分配置或独立核验有缺口，查看 `coverage` 的逐地址差集、未核实条件与源码解析缺口。区分 `configuration_complete`（全部入选地址已实际写入）与 `coverage_complete`（本次输入可核验范围内没有剩余缺口）。无论状态，准确报告实际文件路径。若 `configuration` 已存在，同时交付该文件和 `trace_targets` 全部目标，列明已配置和缺失的数量。采集配置样本数为 0。
- 显式指定地址、通配或 `re:` 模式时仍按用户的选择生成配置。多模式取并集。若 XML 只引用外部结构，提供 `missing_member_types.csv`。保留原模板，将完整工程核实的成员声明另存为 `member_types.csv`。以原 `--out` 加 `--member-types 本轮目录/member_types.csv` 补齐配置。可与用途判断一起提交，也可在部分配置生成后复用该目录已核验的用途判断。步值范围和同名变量不足以推断成员类型。核实的成员只适用于沿声明链定位到同一结构及标量成员的实例。
- Sysmac 显式模式可用已核实的 `--default-type` 补齐缺失的根变量类型。
