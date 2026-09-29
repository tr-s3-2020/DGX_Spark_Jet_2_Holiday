---
name: family-digest-sync
description: 把长者当天的健康观察与回忆片段，按授权范围脱敏后同步给家属；按紧急度分 P0 立即 / P1 当日 / P2 日报三档推送，并负责跨日趋势判定与发送回执。适用于生成家属日报、用药/安全风险即时告知、回忆摘要分享；不承担语音交互、健康信号提取、记忆存储与诊断。
---

# 家属摘要同步

让家属知道"今天发生了什么"，但**不多知道一点**，也**不把没数据说成没事**。

## 什么时候用

- 主控拿到 B（implicit-health-triage）的健康信号或 A（elderly-voice-duplex）的安全分级后，需要同步给家属。
- 需要生成当日/定期家属摘要。
- 需要把 C（life-memoir-retriever）的回忆摘要在**已授权**前提下分享给家属。

## 什么时候不要用

- 要提取健康信号 → 那是 B。
- 要 ASR/TTS、轮次与打断 → 那是 A。
- 要写入或检索长期记忆、生成年谱 → 那是 C（本技能只读不写）。
- 要给医疗建议、改剂量 → **任何情况下都不做**；B 的用药安全话术必须原样透传。
- 老人没有授予家属可见范围 → 不生成、不推送。

## 每轮怎么用

1. **`set_consent`** —— 先登记家属可见范围（`health_summary` / `medication_safety` / `chronicle` / `source_text`）。没有授权就一事无成，这是硬门禁。
2. **`record_signals`** —— 把 B 的 `health_signal` + `safety` + `metadata` 与 A 的 `safety_level` 喂进来。重复 `event_id` 会被自动跳过。
3. **`build_daily_digest`** —— 生成当日卡片。幂等：同一老人同一天同一档位只会有一张。
   - 需要 C 的回忆时，先调 `get_chronicle(purpose="family_digest")`，把结果放进 `chronicle` 字段。
4. **`dispatch_digest`** —— 推送。失败自动重试（默认 3 次），用尽进 `dead_letter`，**必须让人工看见**。
5. **`get_digest`** —— 查卡片与发送状态。

```python
from family_digest import FamilyDigestService

svc = FamilyDigestService()              # 渠道/存储/阈值都可注入
svc.execute("set_consent", {"elder_id": "e001", "scopes": ["health_summary", "chronicle"]})
svc.execute("record_signals", {"health_signals": [...], "safety_events": [...]})
card = svc.execute("build_daily_digest", {"elder_id": "e001", "date": "2026-09-28",
                                          "elder_name": "王奶奶"})
svc.execute("dispatch_digest", {"card_id": card["data"]["card_id"], "to": "女儿"})
```

命令行：`python scripts/digest_cli.py --scenario examples/scenario_p1_trend.json --channel console`

### 直接吃队友的原始报文

A/B/C 的报文格式和本技能的内部字段并不一致（B 不给 `event_id`／`elder_id`／时间戳，C 的年谱是 `content` 嵌套，A 只抛 `{"safety":"P0"}`）。用 `adapters.upstream` 归一化，别手写字段搬运：

```python
from family_digest.adapters.upstream import (
    normalize_b_output, normalize_a_safety, normalize_c_chronicle)

rec = normalize_b_output(b_http_response, elder_id="e001")   # B 的 HTTP 响应体
evt = normalize_a_safety(a_ws_message, elder_id="e001")      # A 的 WebSocket 消息
chr_ = normalize_c_chronicle(c_get_chronicle_response)       # C 的 envelope

svc.execute("record_signals", {
    "health_signals": [rec.model_dump(mode="json")],
    "safety_events": [evt.model_dump(mode="json")],
})
svc.execute("build_daily_digest", {"elder_id": "e001", "date": "2026-09-28",
                                   "chronicle": chr_.model_dump(mode="json")})
```

- `event_id` 由 `(elder_id, session_id, turn_id, 信号内容)` 确定性生成 —— 同一轮重复投递仍然幂等。
- B 返回 `{"status":"ignored"}` 或 422 的用例**不要喂进来**，那不是健康数据。
- C 的 `degraded` / `not_ready` 会被转成卡片里的警告，不会静默当成「没有回忆」。

端到端证据：`python scripts/chain_smoke.py --with-a-safety`（用 B 的官方样例 + C 的 envelope 跑通全链路，日志落在 `examples/chain-smoke-log.md`）。

## 三档是怎么定的

| 档 | 触发 | 行为 |
|---|---|---|
| **P0** | B 的 `safety.blocked=true`（用药风险）或 A 的 `safety_level=P0` | 立即推送，不进日报排队 |
| **P1** | 单日 `severity=high`；同类信号连续 ≥3 天中度及以上；单日中度及以上 ≥3 条；明显偏离个人基线 | 当日摘要内高亮 |
| **P2** | 其余 | 每日固定时刻一份 |

**纪律：D 不自创 P0。** P0 的源头只有 A 的 P0 和 B 的用药安全阻断；趋势再差也只到 P1。

## 四条不能破的规矩

1. **降级 ≠ 没事。** B 的模型失败会降级成 `none/""/low` 且**没有降级标志**。因此只要 `acquisition != acquired`，卡片必须写明"本次未能获取"，**绝不出现"状况良好""一切正常"**。
2. **幂等。** `event_id` 去重 + 同老人同日同档位一张卡片 + 已发送不重复打扰。
3. **同意优先。** 无授权不生成；撤回立即生效，不因缓存绕过。
4. **最小披露。** 卡内文案一律先过脱敏（身份证/手机/银行卡/门牌），长文本截断。

## 失败时怎么办

- 渠道失败 → 重试至上限 → `dead_letter`，返回 `degraded`，需人工介入；不要静默丢弃。
- C 的回忆未就绪（`not_ready`）→ 该段显示"尚未生成"，**不要编造，也不要等待**。
- 没有授权 → `ignored`，原因 `no_family_digest_consent`。
- 未知操作 / 缺字段 → `error`，`error.code` 说明原因。

## 边界一句话

**D 只做映射和编排，不碰 A/B/C 已经占掉的判定权。** 别人判"有没有事"，D 决定"告不告诉家属、什么时候告诉、说到什么程度"。

详见 [宿主契约](references/host-contract.md) 与 [上游字段与坑](references/upstream-contracts.md)。
