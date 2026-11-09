# Skill: plc-action-description

# 动作描述 A档(源码步注释) + B档(LLM工艺语义) 统一协议

## 何时加载
- predict / trace 生成 `batches_desc/*.input.json` 后**必须加载**
- finish / 重跑前**必须加载**
- B档是 LLM 环节，agent 介入是架构要求，**非可选**

## 入口四 Trace 协议

批次 `schema=trace-description-context-v4` 时使用本节。预测批次使用下文的变量 token 协议。

### 注释与观测证据

- 有唯一适用的起始步注释就填 A 档，按完整状态变量及数组下标对齐。观测跨步时照填已知注释，同时保留迁移复核。
- 逐批处理所有缺 A 档的行。检查 `context.evidence` 中的本步调用、实参、条件写入、源码前驱后继、对象绑定、中文映射及同工程记忆。核对否定、作用域和分支。
- 步号不符的注释、邻步注释及静态路径是上下文。命名族 `EM01.Cy_Up1` 的已知类别可辅助理解本步的 `EM01.Cy_Up2`。方向、用途、位置及反馈需要本步独立证据。

### B 档结果

1. 逐个核对 `context.required_actions`，提交每个锚点的 `action_ref`。锚点保留已绑定对象、指令和已知目标。并行对象分别覆盖。有相反方向时查看实参和门控，区分条件请求与现场执行。
2. 通用分句填写 `subject`、`action`、`source_ref` 和 `subject_symbol`。`source_ref` 指向本步写入、已定位的指令、已解析的控制条件或适用注释。`subject_symbol` 填该证据中的完整 PLC 对象或条件符号。注释已明确包含对象和动作时可省略该字段。名称示例和相邻步可以写入 `evidence_refs` 作为辅助引用。
3. 目标可省略。目标取自本步注释或同一调用的实参。引用实参时填 `target_parameter`。已由锚点覆盖的指令直接使用 `action_ref`。补充分句须有独立本步证据。核对指令、条件和反馈的区别，结合源逻辑解释工程术语。
4. 保留当前 `basis`、`kind` 和 `reasoning`。使用中文工艺术语，保留已证实的细节。工作流从通过的分句合成交付描述。
5. 若证据不足，回传 `ok=false` 及具体原因。避免以“夹子气缸”“设备动作”“步进过渡”充当完整描述。

```json
{
  "batch_id": 1,
  "descs": {
    "开始标签|||结束标签": {
      "basis": "TB4…",
      "ok": true,
      "kind": "机械动作",
      "actions": [{"action_ref": "TX定位指令…"}],
      "reasoning": "本步调用绑定升降电机，位置实参为5。目标是5号位，机械到位仍待核。"
    }
  }
}
```

通用分句示例。 `{"subject":"氮化硅靶","action":"磁控溅射镀膜指令下发","source_ref":"TX实际写入…","subject_symbol":"Cell.靶位2","target":"，膜厚设定25.5纳米","target_parameter":"FilmNm"}`

证据不足示例。 `{"basis":"TB4…","ok":false,"reason":"本步只有采样状态值，源码未定位到相应调用或控制条件"}`

### 校验与续跑

- 检查每个当前输入行的 `basis`、引用、动作对象及已知目标。通过项写入 `action_desc_final.csv`，保留依据和引用，并标为 LLM 推断待核。
- 旧 v1–v3 批次仍可复核。使用新协议时按 v4 批次填写当前结果。缺失、过期及无效分句保持待处理。有效的证据不足结果归入描述复核。
- 写完结果后重复同一条 `plc_cli.py trace` 命令。准备产物通过指纹核验后，续跑直接合并并刷新 `<工序名称>_trace步序采集表.xlsx`。

## 预测业务目标
为有支持证据的状态对（开始标签→结束标签）生成一句纯中文工艺动作描述。
A档由代码确定性生成（源码步注释），B档由 LLM 补充无步注释的行。
两者合并写入 `action_desc_final.csv`，供交付表读取。

## 强制环节（红线）
1. `cmd_build` 生成 `batches_desc/*.input.json` 后，agent **必须逐批写** `batches_desc/*.result.json`
2. **不允许跳过 B档直接交付**；`result.json` 缺失时交付表标记【B档未处理】
3. B档由 Agent 根据当前工程证据生成中文工艺语义。
4. 不为"完整"给 B档塞变量翻译（含英文被白名单拒）；不为"完整"编造描述
5. 预测批次按下文词白名单校验；Trace 批次按前文 v4 的当前证据目录和分句协议校验。失败项进入 review，不通过移除校验强填。

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
