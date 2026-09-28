# 宿主契约（v0.1）

宿主（主控 Agent）按本文调用本技能。所有调用走统一入口：

```python
svc = FamilyDigestService(store=..., channel=..., policy=...)
envelope = svc.execute(operation, request)
```

返回统一 envelope：`{"status": ok|degraded|ignored|error, "data": ..., "meta": ..., "error": None|{code,message,retryable}}`

## 操作列表

| operation | request 关键字段 | data |
|---|---|---|
| `set_consent` | `elder_id`, `scopes[]` | `{elder_id, scopes}` |
| `record_signals` | `health_signals[]`, `safety_events[]` | `{accepted, skipped, safety}` |
| `build_daily_digest` | `elder_id`, `date`, `elder_name?`, `chronicle?`, `consents?` | 卡片对象 |
| `dispatch_digest` | `card_id`, `to?` | `{card_id, channel, status, receipt_id, attempts}` |
| `get_digest` | `card_id` | 卡片对象 |

## 调用顺序与依赖

```
set_consent ──► record_signals ──► build_daily_digest ──► dispatch_digest ──► get_digest
                     ▲                     ▲
              B 的 TriageOutput      C 的 get_chronicle(purpose="family_digest")
              A 的 safety_level
```

- 每个操作都可独立调用；`build_daily_digest` 在无授权时返回 `ignored`。
- `build_daily_digest` 幂等：同 `elder_id + date + tier` 返回同一张卡片（`meta.idempotent=true`）。
- `dispatch_digest` 幂等：已 `sent` 的卡片再次调用不重复发送。

## 超时与重试

- 本技能内部只负责**渠道发送**重试（默认 3 次），不做上游语义重试。
- 宿主负责端到端超时；`build_daily_digest` 是纯本地计算，无网络依赖（除非注入了模型润色）。
- 死信卡片不会自动复活，需人工处理后再调用 `dispatch_digest`。

## 状态含义

| status | 含义 | 宿主该做什么 |
|---|---|---|
| `ok` | 正常完成 | 继续 |
| `degraded` | 完成了但信息不完整（未获取健康信息 / 发送失败） | 不要当成成功；把 `meta.warnings` 暴露出来 |
| `ignored` | 条件不满足主动跳过（无授权 / 空载荷） | 检查授权配置 |
| `error` | 出错 | 看 `error.code`，不要无界重试 |

## 部署要求

- Python 3.10+，依赖仅 `pydantic>=2`。
- 存储默认写在模块内 `var/family_digest_store.json`，可用 `store_path` 覆盖；正式版替换 `store.py` 即可换数据库。
- 渠道默认 `MockChannel`；接真实微信/短信时实现 `Channel.send()` 注入即可，上层不动。
