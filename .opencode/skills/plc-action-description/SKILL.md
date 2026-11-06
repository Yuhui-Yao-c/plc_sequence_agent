# Skill: plc-action-description

# 动作描述 A档(源码步注释) + B档(LLM工艺语义) 统一协议

## 何时加载
- predict / trace 生成 `batches_desc/*.input.json` 后**必须加载**
- finish / 重跑前**必须加载**
- B档是 LLM 环节，agent 介入是架构要求，**非可选**

## 业务目标
为每个状态对（开始标签→结束标签）生成一句纯中文工艺动作描述。
A档由代码确定性生成（源码步注释），B档由 LLM 补充无步注释的行。
两者合并写入 `action_desc_final.csv`，供交付表读取。

## 强制环节（红线）
1. `cmd_build` 生成 `batches_desc/*.input.json` 后，agent **必须逐批写** `batches_desc/*.result.json`
2. **不允许跳过 B档直接交付**；`result.json` 缺失时交付表标记【B档未处理】
3. B档是 LLM 环节，代码无法自动生成符合白名单的中文工艺语义；agent 介入是架构要求
4. 不为"完整"给 B档塞变量翻译（含英文被白名单拒）；不为"完整"编造描述
5. agent 写的 result.json 不符白名单 → cmd_validate 拒进 review，行描述空（质量门控，不绕过）

## A档边界（确定性，代码生成）
- 优先级：① `step_comments.json` 命中 → 用；② CSV"动作描述"列步注释 → 用；③ 都不命中 → **留空给B档**
- 无步注释的行 A档必然空，靠 B档补
- v9.21 起：A档不塞变量翻译（含英文会被白名单拒），变量翻译只进"动作变量"引用列

## B档处理协议
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
| trace | trace step4 cmd_a | trace step4 cmd_build | **检测result.json：无则停止+提示** | 有则 cmd_validate | 有则渲染完整/无则A档兜底 |
| 重跑 | cmd_a（确定性不变）| cmd_build（不删result.json，v9.22）| agent成果保留 | cmd_validate | 渲染 |

**幂等保证**：`cmd_build` 不删除已有 `result.json`（v9.22），agent 处理成果在重跑时保留。

## 产物链
```
action_desc_a.csv (A档)
  → batches_desc/*.input.json (B档批次，cmd_build生成)
  → batches_desc/*.result.json (agent逐批写)
  → action_desc_final.csv (cmd_validate合并B+A)
  → 交付表动作描述列
action_desc_review.csv (B档校验失败行)
```

## 与现有 SKILL 的关系
| 现有 SKILL | 关系 |
|---|---|
| plc-boundary-normalization | A/B档不改变边界规则，只描述已确定的步序对 |
| plc-trace-jump-analysis | trace模式复用同一A/B档协议 |
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
# trace 自动 cmd_a + cmd_build，检测 result.json：
#   无 → 渲染A档兜底交付表 + 标记 prepared_b_tier_pending + 停止
#   有 → cmd_validate 合并 + 渲染完整交付表
```

## 边界情况
| 情况 | 处理 |
|---|---|
| 全部行有步注释 | cmd_build 输出"0行需LLM补充"，无B档批次 |
| 源码无步注释且agent不介入 | A档全空，B档未处理，交付表标【B档未处理】 |
| agent写的result.json不符白名单 | cmd_validate拒进review，行描述空 |
| --force重跑 | cmd_build不删result.json，agent成果保留 |
