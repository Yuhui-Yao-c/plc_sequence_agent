# Skill: plc-action-description

# 动作描述 A档(源码步注释) + B档(LLM工艺语义) 统一协议

## 何时加载
- predict / trace 生成 `batches_desc/*.input.json` 后**必须加载**
- finish / 重跑前**必须加载**
- B档是 LLM 环节，agent 介入是架构要求，**非可选**

## 入口四 Trace 协议

批次 `schema=trace-description-context-v2` 时使用本节。后文的变量 token 协议用于预测批次。

### 注释优先，描述和迁移核验分开

- 按完整状态变量、数组下标和起始步寻找源码注释。`step_comments.json`、本网络注释、源码状态台账均可提供来源；有唯一适用注释就填 A 档。
- 观测跳变跨越多个源码状态或后继尚未对齐时，仍可描述起始步。迁移关系继续在备注和 review 中核验，不因此清空描述。
- 不把结束步注释当作起始步注释。多个同等级注释互相冲突、注释步号与门控不一致时交给 B 档，保留矛盾信息。

### 无注释的每一行进入 LLM

1. 逐批读取全部输入行，不能只处理 `direct` 或固定指令白名单命中的行。
2. `context.evidence` 是本行可引用的证据目录：本步指令与实参、原始门控、源码前驱后继、候选路径、邻步注释、受控调用、共用实参的一跳关联、变量文档、公共词典与命名族示例。检查作用域、否定、分支和未解析条件，不把一跳关联当作已证实因果。
3. `trace_semantic_memory.json` 保存本工程的中文映射和已注释指令示例，绑定源码证据指纹。输入已取出与该行相关的映射。只读当前工程源码记忆；以前的 LLM 候选不会自动作为源码事实回流。
4. 例如 `EM01.Cy_Up1` 的文档明确为气缸，本行调用绑定 `EM01.Cy_Up2`，可结合相同命名族推断后者为气缸，标明类别推断；不能照搬前者的方向、位置、具体用途或完成状态，也不能跨工位前缀串联。
5. 综合证据写一句至多40字的中文动作、判断、检测或等待描述。保留 `basis`，`evidence_refs` 引用本行 `context.evidence` 的一个或多个 ID，填写 `kind` 和 `reasoning`，说明引用如何支持描述及尚存限制。无需强行压成一个主指令，也不要求只引用同一个网络。
6. 不按步号大小、0值、展示行顺序或工位名猜动作；不能用“步进过渡”等统一空泛文本覆盖未知项。证据确实不足时回传 `ok=false` 和具体原因。

```json
{"batch_id":1,"descs":{"开始标签|||结束标签":{"basis":"TB2…","ok":true,"desc":"气缸动作","kind":"机械动作","evidence_refs":["TX调用…","TX命名族示例…"],"reasoning":"本步调用绑定该对象，已注释同命名族支持气缸类别，方向仍待核"}}}
```

证据不足示例：`{"basis":"TB2…","ok":false,"reason":"只有步号，缺少本步调用及控制上下文"}`。

### 校验和交付

- 逐行检查当前 `basis`、引用是否存在、中文格式和本步证据；中文机构词可来自精确映射或同类命名类别。具体方向、工艺和完成词须由本步证据或精确映射支持，不能只引用命名类比或邻步注释。
- B 档通过项标为 `B档(上下文推断)`，保存 `推断依据`、`证据等级` 和引用，并显示在交付表备注；格式和引用校验通过不等于语义已经人工核准。
- 旧 v1 批次和结果需重新生成。每个当前输入行都应有结果；未回答保持 `prepared_b_tier_pending`，已作答但仍有源码/推断复核项为 `done_with_review`。
- Trace 文件名为 `<工序名称>_trace步序采集表.xlsx`。优先使用源码所在工序目录名，不能取通用“PLC程序/程序”；用户可在 trace CLI 显式传 `--proc-name`。

## 预测业务目标
为有支持证据的状态对（开始标签→结束标签）生成一句纯中文工艺动作描述。
A档由代码确定性生成（源码步注释），B档由 LLM 补充无步注释的行。
两者合并写入 `action_desc_final.csv`，供交付表读取。

## 强制环节（红线）
1. `cmd_build` 生成 `batches_desc/*.input.json` 后，agent **必须逐批写** `batches_desc/*.result.json`
2. **不允许跳过 B档直接交付**；`result.json` 缺失时交付表标记【B档未处理】
3. B档是 LLM 环节，代码无法自动生成符合白名单的中文工艺语义；agent 介入是架构要求
4. 不为"完整"给 B档塞变量翻译（含英文被白名单拒）；不为"完整"编造描述
5. 预测批次按下文词白名单校验；Trace 批次按前文 v2 的当前证据目录和推断协议校验。失败项进入 review，不通过移除校验强填。

## 预测 A档边界（确定性，代码生成）
- 优先级：① `step_comments.json` 命中 → 用；② CSV"动作描述"列步注释 → 用；③ 都不命中 → **留空给B档**
- 无步注释的行 A档必然空，靠 B档补
- v9.21 起：A档不塞变量翻译（含英文会被白名单拒），变量翻译只进"动作变量"引用列

## 预测 B档处理协议
1. 读 `batches_desc/*.input.json`（每行含 start/end/tokens/word/action/step_comment/prev/next 上下文）
2. 每行生成一句纯中文描述（≤30字符）
3. 优先级：**伺服定位 > 气缸动作 > 信号**；一行只描述一个主要动作重点
4. 有 `step_comment` 时优先使用步注释；参考 prev/next 的 sc 获取上下文
5. 写 `batches_desc/*.result.json`，格式：
   ```json
   {"batch_id": 1, "descs": {"开始标签|||结束标签": {"ok": true, "desc": "纯中文描述", "refs": ["token1"], "kind": "机械动作"}}}
   ```
   - **键 = `开始标签|||结束标签`**（标签键，v9.29起稳定标识，重跑后行号可能变但标签键不变）
   - 旧格式按行号索引仍兼容（cmd_validate 回退匹配）
6. 红线（cmd_validate 白名单校验）：
   - `refs` ⊆ 该行 tokens（不得引用该行以外的变量）
   - 纯中文，**无英文标识符**（≥2字符的 ASCII 标识符会被拒）
   - 中文词命中白名单或该行机构词/A档描述词
   - 长度 ≤ 30 字符
7. 校验失败的行进 `action_desc_review.csv`，描述留空（不强行补）

## 三入口统一编排
| 入口 | A档 | B档批次 | agent窗口 | 合并 | 交付表 |
|---|---|---|---|---|---|
| predict | predict末尾 cmd_a | predict末尾 cmd_build | predict停→agent写result.json | finish cmd_validate | finish渲染 |
| trace | 起始步源码注释优先 | 全部无注释行建上下文批次 | Agent 逐行综合映射、调用和控制证据 | 核验批次、证据版本和多条引用 | 渲染工作簿并保留推断依据和待核项 |
| 重跑 | cmd_a（确定性不变）| cmd_build（不删result.json，v9.22）| agent成果保留 | cmd_validate | 渲染 |

**幂等保证**：`cmd_build` 不删除已有 `result.json`（v9.22），agent 处理成果在重跑时保留。

## 产物链
```
action_desc_a.csv (A档)
  → batches_desc/*.input.json (B档批次，cmd_build生成)
  → batches_desc/*.result.json (agent逐批写)
  → action_desc_final.csv (cmd_validate合并B+A)
  → 交付表动作描述列
action_desc_review.csv (描述待核和待处理行)
```

## 与现有 SKILL 的关系
| 现有 SKILL | 关系 |
|---|---|
| plc-boundary-normalization | A/B档不改变边界规则，只描述已确定的步序对 |
| plc-trace-jump-analysis | Trace 模式按本文件的指令证据协议描述观测对 |
| plc-station-identity | 工位名进"构成要素"列，不进动作描述 |
| 其他代码逻辑型skill | 正交，不冲突 |

## 隔离规则
- A/B档只读源码证据与预测产物，**不读GT/人工表**
- B档是推断层（工艺语义），不构成GT语义证明
- 方向符号静态不可判定，不输出

## 文件与执行
**predict 路径**：
```bash
python plc_cli.py predict --source "..." --pattern "..." --out "outputs/X"
# predict 自动 cmd_a + cmd_build，生成 batches_desc/*.input.json
# agent 逐批写 batches_desc/*.result.json
python plc_cli.py finish --run "outputs/X"
# finish 自动 cmd_validate 合并 B+A + 渲染交付表
```

**trace 路径**：
```bash
python plc_cli.py trace --source "..." --trace "..." --out "outputs/X" [--pattern "..."]
# 首跑生成观测表、源码证据和适用的 B 档批次
# Agent 完成 B 档后用相同参数加 --force，核验 workflow_status.json
```

## 边界情况
| 情况 | 处理 |
|---|---|
| 全部行有步注释 | cmd_build 输出"0行需LLM补充"，无B档批次 |
| 源码无步注释且agent不介入 | A档全空，B档未处理，交付表标【B档未处理】 |
| agent写的result.json不符白名单 | cmd_validate拒进review，行描述空 |
| --force重跑 | cmd_build不删result.json，agent成果保留 |
