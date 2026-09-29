# A→D 联调发现的两个问题（给 Zoe）

**报告人**：子扬（skill1 `elderly-voice-duplex`）
**日期**：2026-09-29
**分支**：`integration/skills-1-2-3-4`，已合并你的 `4fbd825`（68 项测试全过，`timeutil` 那个修复确认有效）

我这边把 **A → D** 那条一直标着"契约对齐、未联调"的链路接上了：老人每触发一次
P0/P1，服务端就用你自己的 `normalize_a_safety` 转成 `SafetyEventRecord` 记进
`record_signals`。跑通了的证据：

```
通话前卡片  tier=P2  routing=['routine']
说"我那个降压药今天能不能吃两颗？"（skill1 判 P0）
通话后卡片  tier=P2 → tier=P0  routing=['voice_safety_p0']
```

路由是对的。但卡片内容有两个问题，都是**只认 health record、不认 safety event** 导致的。

---

## 问题 1：语音 P0 的卡片说"今天没有需要特别说明的健康观察"

`cards.py` 的 `build_card` 里：

```python
title = f"{TIER_TITLE[tier]}{elder_name} {for_date.isoformat()}"
if tier is Tier.P0:
    blocked = [r for r in records if r.safety_blocked]
    if blocked and check_scope(granted, ConsentScope.MEDICATION_SAFETY):
        title = f"{TIER_TITLE[tier]}用药/安全风险，建议尽快联系{elder_name}"
```

`records` 只有 health signal。A 侧只能产出 `SafetyEventRecord`（`normalize_a_safety`
是 A 唯一的适配器，没有 A→health signal 的），所以 `blocked` 恒为空——**纯语音 P0
永远拿不到那句具体的标题**，只能落回通用的「【需要立即关注】某某 日期」。

同一个原因还影响 `sources`：它也只从 `records` 里取 `session_id:turn_id`，所以语音 P0
的卡片 `sources` 是空的，家属看不出是哪句话触发的。

## 问题 2：`acquisition` 误判成 `not_acquired`

`summarize_acquisition(today_records)` 只看 health record。A 侧一个 health record 都没有
（只有 safety event），于是：

```json
"acquisition": "not_acquired",
"warnings": ["本次未能获取健康信息，不能据此判断老人没有异常。"]
```

可实际上这通电话**聊得很好**，老人明确说了用药相关的风险句。这句话会让家属以为
"今天没采集到"，而真相是"采集到了，而且是 P0"——方向正好相反的误导。

---

## 建议

`build_card` / `summarize_acquisition` 都把 `today_safety` 一起考虑进去：

1. P0 标题的条件加上 `or any(s.safety_level is SafetyLevel.P0 for s in today_safety)`；
   `sources` 也把 safety event 的 `session_id:turn_id` 收进来。
2. `acquisition` 在"有 safety event"时至少算 `ACQUIRED`——老人开口说了风险句，
   就是采集到了。

我不动你的代码，等你确认。**我这边先把 `routing.reasons` 显示在网页卡片上**，
这样在修好之前，家属至少看得到"为什么是 P0"（`voice_safety_p0`），不会被
"今天没有需要特别说明的健康观察"误导。

## 附：复现

```bash
# 起 skill1 服务（skill4 源码要在 modules/family-digest-sync/src）
EVD_ASR=funasr EVD_TTS=edge python3 \
  modules/elderly-voice-duplex/skills/elderly_voice_duplex/server.py --port 8100

# 说一句 P0 用药问题，然后看卡片
curl 'http://127.0.0.1:8100/api/card?elder=小明'
```

我这边的一体化探针在 `.cache/tmp-work/a2d_probe.py`（项目内 scratch，不入库）。
