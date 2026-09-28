# 任务四 → 任务二：接口确认回执

**发件**：成员 4（Zoe），`family-digest-sync`
**收件**：Yunsheng，`implicit-health-triage`
**时间**：2026-09-28
**背景**：B 的 `docs/interfaces.md` 写明"需任务四负责人确认枚举与接收方式后再联调"。这份就是回执。

结论先行：**D 已按你的两种形态都做好了适配，并且已经用你 09-28 交付版的代码真跑通了联调**（`scripts/live_b_to_d.py`，日志在 `examples/live-b-to-d-log.md`）。下面三件事需要你确认或补一下。

---

## 1. 接收方式：D 消费原始报文，不用 `to_digest_record`

你提供了 `to_digest_record()`，但**D 不能把它当唯一数据源**，因为它会丢掉两样 D 必需的东西：

| 丢失项 | 后果 |
|---|---|
| `guardrail_triggered`（用药阻断） | **D 的 P0 源头之一断掉**。用药安全是最高优先级的家属告警，漏了这条等于最该通知的没通知 |
| `type=无` 的轮次返回 `None` | D 看不到"今天聊过但没识别出信号"，也无法据此判断 B 是否降级。D 的卡片规则是"不能把没数据说成没事"，数据源被掐断就无法执行 |

所以：**请直接把 `TriageOutput` / `TriageResponse` 原始报文给 D**（主控转发即可），归一化由 D 这边的 `adapters/upstream.py` 做。`to_digest_record` 的输出我也支持（不会报错），但只作为兜底。

## 2. 枚举：中英文我都收，D 内部用规范形

你当前库里**两套 schema 并存**，我两套都支持：

| | `task2/schemas.py`（CLI 实测吐的） | `schemas.py` + `schemas/*.json`（导出的） |
|---|---|---|
| type | `none/symptom/sleep/pain/medication/mobility/appetite/other` | `无/身体不适/睡眠/疼痛/用药/行动能力/食欲/其他体征` |
| severity | `low/moderate/high` | `轻微/中等/需留意` |
| mode | `passthrough/health_care/medication_safety` | `normal_chat/health_care/medication_safety` |
| 结构 | 扁平 `health_signal` / `safety` / `response` / `metadata` | 嵌套 `result: {...}` + `guardrail_triggered` |

D 内部的映射（已实现、已测试）：

```
无→none  身体不适→symptom  睡眠→sleep  疼痛→pain
用药→medication  行动能力→mobility  食欲→appetite  其他体征→other
轻微→low  中等→moderate  需留意→high
normal_chat→passthrough
```

**结论：不冲突，维持你文档定的英文扁平版。**（2026-09-28 更新）

你的 `docs/interfaces.md` 第 3 行写得很明确：

> 此为提供方交接协议……**不要使用旧版中文枚举或嵌套 `result` 协议。**

这与"团队最终要中文"是**两个层级**，不要混：

| 层级 | 语言 | 谁负责 |
|---|---|---|
| **展示层**（老人听到的话、家属收到的卡片） | **中文** | A 的 TTS、D 的卡片文案——都已是中文 |
| **传输层**（模块之间传的字段名和枚举值） | 英文 `none/symptom/low/...` | B 已定，且文档明令禁止中文枚举 |

D 一直按这个分层做的：内部用英文规范形做判定，**给家属看的文案全中文**，且措辞已对齐你的中文语义（`身体不适/睡眠/疼痛/用药/行动能力/食欲/其他体征`）。枚举是 `symptom` 还是 `身体不适`，家属永远看不到，但翻译成英文枚举能让主控和各模块的匹配稳定得多——所以**我支持你文档里的决定，不要改成中文枚举**。

⚠️ 唯一要请你做的是：把 `schemas/` 目录下导出的那套**中文 JSON Schema 删掉或标注为废弃**。它和你的 `interfaces.md` 互相打架，我这个适配层为了保险两套都收了，但别的模块的人看了会以为要对接中文套。

**另外**：你的 `HealthType` 文档里那句"不要用 `type == "身体不适"` 判断身体不适"——我照做了。D 的跨日趋势按**分组**判定（`身体不适/疼痛/睡眠/食欲/行动能力` 同属 bodily 组），`用药` 独立成组。这样"今天说腿沉、昨天说睡不着"能算成同一条趋势，不会被精确匹配打断。这条我写进测试了。

## 3. 请在新版里保留降级标志（这条比较要紧）

你的 `interfaces.md` 第 60 行写：

> 模型 HTTP/超时错误或 JSON 校验失败：最多两次尝试后按需求降级为 none/空 detail/low，HTTP 仍为 200，记录不含原文的警告。**当前返回协议没有独立降级标志，下游不能据此认定用户没有健康问题。**

问题是：**新版 `TriageResult` 里连 `metadata` 都没有了**（旧版 `task2` 还有 `metadata.semantic_backend` / `qwen_called`）。这意味着新版下 D 彻底无从判断"B 这次是正常跑完没信号，还是模型挂了被降级成无信号"。

D 的卡片有一条硬规则：**只要不能确认拿到了数据，就绝不能写"状况良好/一切正常"**。没有降级标志，D 只能在每次降级时都对家属说"本次未能获取"，这会让日报的可信度下降——家属会习惯性忽略。

**请求**：新版 `TriageResult` 增加一个字段，任选其一即可：

```python
degraded: bool = False          # 推荐：一个布尔值就够
# 或保留 metadata
metadata: {"semantic_backend": "none|mock|qwen", "qwen_called": bool}
```

D 这边已经用 `Acquisition(acquired / no_signal / not_acquired)` 三态做好了区分，只要有标志就能立刻接上，不需要你改别的。

**并且我已经把接口预留好了**：`adapters/upstream.py` 会在这四处找降级标志——

```
顶层 degraded / result.degraded / metadata.degraded / metadata.fallback
```

你任选一处加上，D **零改动即刻生效**（已有测试守着：`test_explicit_degraded_flag_on_result`）。在你加之前，我退而用 `metadata.semantic_backend + qwen_called` 启发式推断——能挡住大部分情况，但**不如一个布尔值准**。

顺带说明不加的代价：D 只能保守处理，即"只要没拿到信号就对家属说本次未能获取"。日报天天这么写，家属会习惯性忽略，**最后连真出事那条也一起漏看**。所以这个字段看着小，实际决定日报有没有人看。

---

## 4. 我这边的状态

- `modules/family-digest-sync/` 已交付，50 项测试全过。
- `scripts/live_b_to_d.py`：**直接 subprocess 起你的 CLI 拿真实输出** → 归一化 → 跑 D 全链路。跑法：
  ```
  set TRIAGE_GUARDRAILS_ENABLED=false    # 退到纯规则，免装 nemoguardrails
  set SEMANTIC_BACKEND=mock
  python scripts/live_b_to_d.py
  ```
- 实测三句的输入输出：

| 老人说的话 | B 的输出 | D 的判定 |
|---|---|---|
| 我降压药今天能不能吃两颗？ | `medication / high / blocked=true` | **P0** 立即推送 |
| 今天早上起来腿沉得很… | `symptom / moderate` | 计入 bodily 趋势 |
| 今天菜市场青菜挺便宜。 | `none / low` | 不作为健康内容 |

如果你改了枚举或结构，只要告诉我，我改适配层就行 —— **D 的核心逻辑不用动**，这是适配层存在的意义。

---

## 5. 你的文档和代码有四处对不上（麻烦同步一下）

这里说的不是"写错字"，是**别人照着文档对接会接错**。D 是消费方，我只能靠读代码确认真实行为——别的模块不一定有这个耐心。

| # | 文档写的（`docs/interfaces.md`） | 代码实际 | 影响 |
|---|---|---|---|
| 1 | 第 84 行：不提供 `to_digest_record` | `integration.py` 里**已经有了** | 下游不知道可以调，也不知道它丢字段，可能误用 |
| 2 | 第 3 行：不要用中文枚举/嵌套 `result` | `schemas/` 目录导出的是**中文枚举 + 嵌套 result** 的 JSON Schema | 两套协议并存，对接方不知道该接哪个 |
| 3 | 第 32-34 行：英文 `none/low` | 同上，与 2 是同一处矛盾 | —— |
| 4 | 第 60 行：承认"当前没有独立降级标志" | 属实（不是 bug，是待办） | 见第 3 节，请求补 `degraded` |

**第 1 条单独说清楚**：`to_digest_record` 这个函数本身没问题，问题在于它会**丢两样东西**——

- 丢 `guardrail_triggered`（用药阻断）。这是最要命的一条：老人问"降压药能不能吃两颗"，B 正确拦截了，但走 `to_digest_record` 之后这个拦截标志没了，D 就不知道要发 P0。**最该通知家属的那条反而漏了。**
- `type=无` 的轮次直接返回 `None`。D 因此看不到"今天其实聊过，只是没识别出信号"，也就没法判断你是"正常跑完"还是"模型挂了被降级"。

所以 D 消费你的原始报文（`TriageOutput`），`to_digest_record` 我也支持（不会报错），只当兜底。这点写在第 1 节了，这里重复一次是因为它和第 1 条文档缺失是同一件事的两面。
