# B → D 真实联调日志

- 生成时间：2026-09-28T15:08:16
- B 目录：`C:\Users\16324\Desktop\NV\DGX_Spark_Jet_2_Holiday-docs-task2-collaboration-handoff\DGX_Spark_Jet_2_Holiday-docs-task2-collaboration-handoff\modules\implicit-health-triage`
- B 运行方式：`python -m implicit_health_triage.task2.cli`（TRIAGE_GUARDRAILS_ENABLED=false 退到纯规则，SEMANTIC_BACKEND=mock）
- 本日志里的 B 输出是**当场跑出来的**，不是手抄样例

## B 的原始输出

```json
[
  {
    "utterance": "我降压药今天能不能吃两颗？",
    "b_raw_output": {
      "session_id": "live",
      "turn_id": "t1",
      "health_signal": {
        "type": "medication",
        "detail": "询问增加降压药剂量",
        "severity": "high"
      },
      "safety": {
        "blocked": true,
        "rule_id": "MEDICATION_DOSE_INCREASE"
      },
      "response": {
        "mode": "medication_safety",
        "text": "这个涉及具体的用药剂量，我不能替您决定增加、减少或停止服药。请先按照医生给您的处方或药品说明来服用，如果不确定，最好联系医生或药师确认。"
      },
      "metadata": {
        "semantic_backend": "none",
        "qwen_called": false,
        "latency_ms": 5.911
      }
    }
  },
  {
    "utterance": "今天早上起来腿沉得很，买菜走两步就得歇着。",
    "b_raw_output": {
      "session_id": "live",
      "turn_id": "t2",
      "health_signal": {
        "type": "symptom",
        "detail": "下肢沉重/乏力",
        "severity": "moderate"
      },
      "safety": {
        "blocked": false,
        "rule_id": null
      },
      "response": {
        "mode": "health_care",
        "text": "听起来您今天提到下肢沉重、没什么力气，确实有些不舒服。您平时该吃的药都按原来的安排吃了吗？"
      },
      "metadata": {
        "semantic_backend": "mock",
        "qwen_called": false,
        "latency_ms": 24.443
      }
    }
  },
  {
    "utterance": "今天菜市场青菜挺便宜。",
    "b_raw_output": {
      "session_id": "live",
      "turn_id": "t3",
      "health_signal": {
        "type": "none",
        "detail": "",
        "severity": "low"
      },
      "safety": {
        "blocked": false,
        "rule_id": null
      },
      "response": {
        "mode": "passthrough",
        "text": null
      },
      "metadata": {
        "semantic_backend": "mock",
        "qwen_called": false,
        "latency_ms": 22.539
      }
    }
  },
  {
    "utterance": "这两天晚上翻来覆去睡不着。",
    "b_raw_output": {
      "session_id": "live",
      "turn_id": "t4",
      "health_signal": {
        "type": "sleep",
        "detail": "睡眠不佳",
        "severity": "moderate"
      },
      "safety": {
        "blocked": false,
        "rule_id": null
      },
      "response": {
        "mode": "health_care",
        "text": "听起来您最近睡得不太好，您愿意再说说吗？"
      },
      "metadata": {
        "semantic_backend": "mock",
        "qwen_called": false,
        "latency_ms": 19.345
      }
    }
  }
]
```

## 归一化后进入 D 的记录

```json
[
  {
    "event_id": "b:385af71803b8",
    "elder_id": "e001",
    "occurred_at": "2026-09-28T09:00:00",
    "session_id": "live",
    "turn_id": "t1",
    "signal_type": "medication",
    "detail": "询问增加降压药剂量",
    "severity": "high",
    "safety_blocked": true,
    "rule_id": "MEDICATION_DOSE_INCREASE",
    "response_mode": "medication_safety",
    "semantic_backend": "none",
    "qwen_called": false,
    "degraded": null
  },
  {
    "event_id": "b:810ffd38794b",
    "elder_id": "e001",
    "occurred_at": "2026-09-28T09:00:00",
    "session_id": "live",
    "turn_id": "t2",
    "signal_type": "symptom",
    "detail": "下肢沉重/乏力",
    "severity": "moderate",
    "safety_blocked": false,
    "rule_id": null,
    "response_mode": "health_care",
    "semantic_backend": "mock",
    "qwen_called": false,
    "degraded": null
  },
  {
    "event_id": "b:375364106b14",
    "elder_id": "e001",
    "occurred_at": "2026-09-28T09:00:00",
    "session_id": "live",
    "turn_id": "t3",
    "signal_type": "none",
    "detail": "",
    "severity": "low",
    "safety_blocked": false,
    "rule_id": null,
    "response_mode": "passthrough",
    "semantic_backend": "mock",
    "qwen_called": false,
    "degraded": null
  },
  {
    "event_id": "b:667eeb52f72c",
    "elder_id": "e001",
    "occurred_at": "2026-09-28T09:00:00",
    "session_id": "live",
    "turn_id": "t4",
    "signal_type": "sleep",
    "detail": "睡眠不佳",
    "severity": "moderate",
    "safety_blocked": false,
    "rule_id": null,
    "response_mode": "health_care",
    "semantic_backend": "mock",
    "qwen_called": false,
    "degraded": null
  },
  {
    "event_id": "b:0b521fa54361",
    "elder_id": "e001",
    "occurred_at": "2026-09-27T09:00:00",
    "session_id": "hist",
    "turn_id": "h1",
    "signal_type": "symptom",
    "detail": "下肢沉重/乏力",
    "severity": "moderate",
    "safety_blocked": false,
    "rule_id": null,
    "response_mode": "health_care",
    "semantic_backend": "mock",
    "qwen_called": false,
    "degraded": null
  },
  {
    "event_id": "b:cf28baca3a42",
    "elder_id": "e001",
    "occurred_at": "2026-09-26T09:00:00",
    "session_id": "hist",
    "turn_id": "h2",
    "signal_type": "pain",
    "detail": "走路发沉",
    "severity": "moderate",
    "safety_blocked": false,
    "rule_id": null,
    "response_mode": "health_care",
    "semantic_backend": "mock",
    "qwen_called": false,
    "degraded": null
  }
]
```

## 路由与推送

```json
{
  "routing": {
    "tier": "P0",
    "reasons": [
      "medication_safety_blocked:MEDICATION_DOSE_INCREASE"
    ],
    "acquisition": "acquired",
    "degraded_suspected": false
  },
  "dispatch": {
    "status": "ok",
    "data": {
      "card_id": "card:e001:2026-09-28:P0",
      "channel": "mock",
      "status": "sent",
      "receipt_id": "mock-f63273ca",
      "attempts": 1,
      "reason": ""
    },
    "meta": {},
    "error": null
  }
}
```

## 家属实际收到的内容

```text
【需要立即关注】用药/安全风险，建议尽快联系王奶奶

[健康观察]
· 用药：询问增加降压药剂量（较明显）
· 身体不适：下肢沉重/乏力（中等）
· 睡眠：睡眠不佳（中等）

[近期回忆]
（回忆摘要尚未生成，本次不展示）
```

档位：`P0`　采集状态：`acquired`　卡片状态：`sent`
