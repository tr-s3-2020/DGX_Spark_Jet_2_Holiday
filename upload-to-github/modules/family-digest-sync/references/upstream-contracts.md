# 上游字段与已知坑

## B：`implicit-health-triage`（Yunsheng）

进程内 `await skill.handle(TriageInput(...))`，或 HTTP `POST /v1/implicit-health-triage`。

取这几项：

| 字段 | 说明 |
|---|---|
| `health_signal.type` | `none/symptom/sleep/pain/medication/mobility/appetite/other` |
| `health_signal.detail` | ≤256 字 |
| `health_signal.severity` | `low/moderate/high` —— **内部标签，不是 P0/P1/P2** |
| `safety.blocked` / `safety.rule_id` | 用药安全是否阻断、命中规则 |
| `response.mode` | `passthrough/health_care/medication_safety` |
| `metadata.semantic_backend` | `none/mock/qwen` |
| `metadata.qwen_called` | 是否尝试调用过模型（超时也算 true） |

**坑（B 文档原文要点）**

- 模型 HTTP/超时错误或 JSON 校验失败，两次尝试后降级为 `none / 空 detail / low`，**协议里没有独立的降级标志**。下游不能据此认定老人没有问题。
- `medication_safety` 分支：`type=medication`、`severity=high`、`semantic_backend=none`、`qwen_called=false`，话术固定不可改写。
- 输入严格四字段 `session_id/turn_id/text/is_final`，**不能**把带 `user_id` 的扩展事件原样传给它。
- B 不做 P0/P1/P2 转换，不提供 `to_digest_record`；时间戳、持久化、趋势、分级**全部归 D**。

**D 的应对**：新增 `Acquisition` 字段自行判断 `acquired / no_signal / not_acquired`，`looks_degraded()` 标记"调用过模型却没结果"的疑似降级。

## A：`elderly-voice-duplex`（子阳）

只抛 `safety_level` 与文本。A 自己内建的围栏：

- P0：加倍服药、自行停药、剧烈胸痛/呼吸困难、跌倒起不来 → 拒绝建议 + 提议拨子女电话
- P1：头晕/头痛/恶心/腹痛/腿沉/失眠 → 关切 + 记录 + 告知子女

**坑**：A 的 P0/P1 与 B 的 `low/moderate/high` 是两套体系，**不能直接映射**。当前约定：两者都只作为 P0 的**触发来源**，其余分级由 D 的趋势逻辑决定。原文不落盘、不进卡片。

## C：`life-memoir-retriever`（卢易锋）

`get_chronicle(purpose="family_digest", format="structured"|"markdown")`，HTTP 前缀 `/v1/memory`，默认 8765。

- 返回 `{state: ready|not_ready, view_version, based_on_memory_revision, content, warnings}`
- 无 family_digest 授权**不准备**家属版；权限变化立即失效
- `degraded` ≠ 记忆不存在；`not_ready` 不阻塞发话
- 年谱不进 P0，也不作为健康判断依据

**坑**：C 明确向 D 索要"可见范围、读取方式、撤回与限制访问的执行方式"——这份回执由本技能的 `set_consent` + `ConsentScope` 回答。

## 待群里确认（不要单方面拍板）

1. A 的 P0/P1 ↔ B 的 low/moderate/high ↔ D 的 P0/P1/P2 唯一映射表
2. A 的医疗围栏 vs B 的 medication_safety 优先级
3. 首轮演示语言（A 最新提交改英文后端，B/C 仍中文）
4. `session_id/turn_id/user_id` 的生成与去重约定
5. 端口与 DGX 目录分配（B 建议 8080，C 默认 8765）
