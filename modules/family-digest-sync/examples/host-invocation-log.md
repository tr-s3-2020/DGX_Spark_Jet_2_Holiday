# 宿主调用留痕

- 时间：2026-09-28T14:34:31
- 技能：family-digest-sync
- 描述：把长者当天的健康观察与回忆片段，按授权范围脱敏后同步给家属；按紧急度分 P0 立即 / P1 当日 / P2 日报三档推送，并负责跨日趋势判定与发送回执。适用于生成家属日报、用药/安全风险即时告知、回忆摘要分享；不承担语音交互、健康信号提取……
- 宿主：本地 Python 宿主（模拟主控 Agent）
- 存储：var/host-demo.json；渠道：MockChannel（可替换为真实渠道）

以下每一步都是宿主通过统一入口 `FamilyDigestService.execute(operation, request)` 的**实际调用与真实返回**，不是手写样例。

## P0 用药/安全风险 —— `scenario_p0_medication.json`

**调用** `set_consent`

```json
{
  "elder_id": "e001",
  "scopes": [
    "health_summary",
    "medication_safety",
    "chronicle"
  ]
}
```

**调用** `record_signals`

```json
{
  "health_signals": 1,
  "safety_events": 1
}
```

**调用** `→ record_signals 结果`

```json
{
  "status": "ok",
  "data": {
    "accepted": 1,
    "skipped": [],
    "safety": 1
  },
  "meta": {
    "deduped": false
  },
  "error": null
}
```

**调用** `build_daily_digest`

```json
{
  "status": "ok",
  "data": {
    "card_id": "card:e001:2026-09-28:P0",
    "elder_id": "e001",
    "elder_name": "王奶奶",
    "for_date": "2026-09-28",
    "tier": "P0",
    "title": "【需要立即关注】用药/安全风险，建议尽快联系王奶奶",
    "sections": [
      {
        "heading": "健康观察",
        "body": "· 用药：我降压药今天能不能吃两颗（较明显）"
      },
      {
        "heading": "近期回忆",
        "body": "王奶奶年轻时在纺织厂做过挡车工，常提起厂区门口的那棵老槐树。"
      }
    ],
    "warnings": [],
    "acquisition": "acquired",
    "redacted": false,
    "sources": [
      "s001:t007"
    ],
    "status": "validated"
  },
  "meta": {
    "routing": {
      "tier": "P0",
      "reasons": [
        "medication_safety_blocked:MEDICATION_DOSE_INCREASE",
        "voice_safety_p0"
      ],
      "acquisition": "acquired",
      "degraded_suspected": false
    }
  },
  "error": null
}
```

**调用** `dispatch_digest`

```json
{
  "status": "ok",
  "data": {
    "card_id": "card:e001:2026-09-28:P0",
    "channel": "mock",
    "status": "sent",
    "receipt_id": "mock-30fff070",
    "attempts": 1,
    "reason": ""
  },
  "meta": {},
  "error": null
}
```

**家属实际收到的内容**

```text
【需要立即关注】用药/安全风险，建议尽快联系王奶奶

[健康观察]
· 用药：我降压药今天能不能吃两颗（较明显）

[近期回忆]
王奶奶年轻时在纺织厂做过挡车工，常提起厂区门口的那棵老槐树。
```

## P1 跨日趋势劣化 —— `scenario_p1_trend.json`

**调用** `set_consent`

```json
{
  "elder_id": "e001",
  "scopes": [
    "health_summary",
    "medication_safety",
    "chronicle"
  ]
}
```

**调用** `record_signals`

```json
{
  "health_signals": 4,
  "safety_events": 1
}
```

**调用** `→ record_signals 结果`

```json
{
  "status": "ok",
  "data": {
    "accepted": 4,
    "skipped": [],
    "safety": 1
  },
  "meta": {
    "deduped": false
  },
  "error": null
}
```

**调用** `build_daily_digest`

```json
{
  "status": "ok",
  "data": {
    "card_id": "card:e001:2026-09-28:P1",
    "elder_id": "e001",
    "elder_name": "王奶奶",
    "for_date": "2026-09-28",
    "tier": "P1",
    "title": "【今天需要留意】王奶奶 2026-09-28",
    "sections": [
      {
        "heading": "健康观察",
        "body": "· 行动：今天更沉了，出门买菜走不动（中等）\n· 睡眠：夜里醒了三四次（轻微）"
      },
      {
        "heading": "近期回忆",
        "body": "王奶奶最近常提起年轻时在纺织厂的事。"
      }
    ],
    "warnings": [],
    "acquisition": "acquired",
    "redacted": false,
    "sources": [
      "s3:t1",
      "s3:t9"
    ],
    "status": "validated"
  },
  "meta": {
    "routing": {
      "tier": "P1",
      "reasons": [
        "trend:mobility>=3d"
      ],
      "acquisition": "acquired",
      "degraded_suspected": false
    }
  },
  "error": null
}
```

**调用** `dispatch_digest`

```json
{
  "status": "ok",
  "data": {
    "card_id": "card:e001:2026-09-28:P1",
    "channel": "mock",
    "status": "sent",
    "receipt_id": "mock-83ffb3f8",
    "attempts": 1,
    "reason": ""
  },
  "meta": {},
  "error": null
}
```

**家属实际收到的内容**

```text
【今天需要留意】王奶奶 2026-09-28

[健康观察]
· 行动：今天更沉了，出门买菜走不动（中等）
· 睡眠：夜里醒了三四次（轻微）

[近期回忆]
王奶奶最近常提起年轻时在纺织厂的事。
```

## P2 日常摘要 —— `scenario_p2_daily.json`

**调用** `set_consent`

```json
{
  "elder_id": "e001",
  "scopes": [
    "health_summary",
    "chronicle"
  ]
}
```

**调用** `record_signals`

```json
{
  "health_signals": 2,
  "safety_events": 0
}
```

**调用** `→ record_signals 结果`

```json
{
  "status": "ok",
  "data": {
    "accepted": 2,
    "skipped": [],
    "safety": 0
  },
  "meta": {
    "deduped": false
  },
  "error": null
}
```

**调用** `build_daily_digest`

```json
{
  "status": "ok",
  "data": {
    "card_id": "card:e001:2026-09-28:P2",
    "elder_id": "e001",
    "elder_name": "王奶奶",
    "for_date": "2026-09-28",
    "tier": "P2",
    "title": "【今日摘要】王奶奶 2026-09-28",
    "sections": [
      {
        "heading": "健康观察",
        "body": "· 食欲：今天吃了小半碗粥（轻微）"
      },
      {
        "heading": "近期回忆",
        "body": "（回忆摘要尚未生成，本次不展示）"
      }
    ],
    "warnings": [],
    "acquisition": "acquired",
    "redacted": false,
    "sources": [
      "s9:t2"
    ],
    "status": "validated"
  },
  "meta": {
    "routing": {
      "tier": "P2",
      "reasons": [
        "routine"
      ],
      "acquisition": "acquired",
      "degraded_suspected": false
    }
  },
  "error": null
}
```

**调用** `dispatch_digest`

```json
{
  "status": "ok",
  "data": {
    "card_id": "card:e001:2026-09-28:P2",
    "channel": "mock",
    "status": "sent",
    "receipt_id": "mock-5378a00e",
    "attempts": 1,
    "reason": ""
  },
  "meta": {},
  "error": null
}
```

**家属实际收到的内容**

```text
【今日摘要】王奶奶 2026-09-28

[健康观察]
· 食欲：今天吃了小半碗粥（轻微）

[近期回忆]
（回忆摘要尚未生成，本次不展示）
```

## 降级：模型未稳定返回 —— `scenario_degraded.json`

**调用** `set_consent`

```json
{
  "elder_id": "e001",
  "scopes": [
    "health_summary"
  ]
}
```

**调用** `record_signals`

```json
{
  "health_signals": 1,
  "safety_events": 0
}
```

**调用** `→ record_signals 结果`

```json
{
  "status": "ok",
  "data": {
    "accepted": 1,
    "skipped": [],
    "safety": 0
  },
  "meta": {
    "deduped": false
  },
  "error": null
}
```

**调用** `build_daily_digest`

```json
{
  "status": "degraded",
  "data": {
    "card_id": "card:e001:2026-09-28:P2",
    "elder_id": "e001",
    "elder_name": "王奶奶",
    "for_date": "2026-09-28",
    "tier": "P2",
    "title": "【今日摘要】王奶奶 2026-09-28",
    "sections": [
      {
        "heading": "健康观察",
        "body": "· 今天没有需要特别说明的健康观察。"
      },
      {
        "heading": "近期回忆",
        "body": "（未获得回忆摘要授权，本段不展示）"
      },
      {
        "heading": "说明",
        "body": "· 本次对话未识别出明确的健康信号，不等于没有问题。\n· 健康模块本次曾调用语义模型但未稳定返回，不能据此认定没有问题。"
      }
    ],
    "warnings": [
      "本次对话未识别出明确的健康信号，不等于没有问题。",
      "健康模块本次曾调用语义模型但未稳定返回，不能据此认定没有问题。"
    ],
    "acquisition": "no_signal",
    "redacted": false,
    "sources": [],
    "status": "validated"
  },
  "meta": {
    "routing": {
      "tier": "P2",
      "reasons": [
        "routine"
      ],
      "acquisition": "no_signal",
      "degraded_suspected": true
    },
    "warnings": [
      "本次对话未识别出明确的健康信号，不等于没有问题。",
      "健康模块本次曾调用语义模型但未稳定返回，不能据此认定没有问题。"
    ]
  },
  "error": null
}
```

**调用** `dispatch_digest`

```json
{
  "status": "ok",
  "data": {
    "card_id": "card:e001:2026-09-28:P2",
    "channel": "mock",
    "status": "sent",
    "receipt_id": "mock-039468b4",
    "attempts": 1,
    "reason": ""
  },
  "meta": {},
  "error": null
}
```

**家属实际收到的内容**

```text
【今日摘要】王奶奶 2026-09-28

[健康观察]
· 今天没有需要特别说明的健康观察。

[近期回忆]
（未获得回忆摘要授权，本段不展示）

[说明]
· 本次对话未识别出明确的健康信号，不等于没有问题。
· 健康模块本次曾调用语义模型但未稳定返回，不能据此认定没有问题。
```
