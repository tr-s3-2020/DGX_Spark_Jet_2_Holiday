# 四模块串联冒烟日志（A/B/C 真实报文格式 -> D）

- 生成时间：2026-09-28T17:51:51
- 输入来源：B=团队库 implicit-health-triage 样例；C=API.md 3.8 的 get_chronicle envelope；A=server.py 的 `{"safety": "P0"}` 消息
- C 状态：ready；叠加 A 的 P0：False；去掉 B 的用药阻断：False

## 调用轨迹

```json
[
  {
    "op": "set_consent",
    "result": {
      "elder_id": "e001",
      "scopes": [
        "health_summary",
        "medication_safety",
        "chronicle"
      ]
    }
  },
  {
    "op": "record_signals",
    "result": {
      "status": "ok",
      "data": {
        "accepted": 5,
        "skipped": [],
        "safety": 0
      },
      "meta": {
        "deduped": false
      },
      "error": null
    }
  },
  {
    "op": "build_daily_digest",
    "result": {
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
            "body": "· 身体不适：下肢沉重/乏力（中等）\n· 用药：询问增加降压药剂量（较明显）"
          },
          {
            "heading": "近期回忆",
            "body": "王奶奶常提起年轻时在纺织厂的日子，也说孙女上周来陪她包了饺子。"
          }
        ],
        "warnings": [],
        "acquisition": "acquired",
        "redacted": false,
        "sources": [
          "demo:t2",
          "demo:t3"
        ],
        "status": "validated"
      },
      "meta": {
        "routing": {
          "tier": "P0",
          "reasons": [
            "medication_safety_blocked:MEDICATION_DOSE_INCREASE"
          ],
          "acquisition": "acquired",
          "degraded_suspected": false
        }
      },
      "error": null
    }
  },
  {
    "op": "dispatch_digest",
    "result": {
      "status": "ok",
      "data": {
        "card_id": "card:e001:2026-09-28:P0",
        "channel": "mock",
        "status": "sent",
        "receipt_id": "mock-6d77992d",
        "attempts": 1,
        "reason": ""
      },
      "meta": {},
      "error": null
    }
  }
]
```

## 家属实际收到的内容

```text
【需要立即关注】用药/安全风险，建议尽快联系王奶奶

[健康观察]
· 身体不适：下肢沉重/乏力（中等）
· 用药：询问增加降压药剂量（较明显）

[近期回忆]
王奶奶常提起年轻时在纺织厂的日子，也说孙女上周来陪她包了饺子。
```

档位：`P0`　采集状态：`acquired`　卡片状态：`sent`
