# Bug 报告：`family-digest-sync` 在 Python 3.10 上无法处理带时区的时间戳

**模块**：`modules/family-digest-sync`（skill 4 / 任务四）
**负责人**：Zoe
**发现者**：联调编排层（`modules/orchestrator`）
**严重度**：**高** —— 调用方只要传带时区的 `occurred_at`，`record_signals` 直接返回 `error`，
健康信号完全记不进去；`build_daily_digest` 也无法生成家属卡片。
**影响版本**：当前 `origin/zoe-module-4`（`6968c2d`）

---

## 1. 一句话根因

`pydantic` 的 `model_dump(mode="json")` 会把 **UTC 时间**序列化成 `"...Z"` 后缀，
而 **Python 3.10 的 `datetime.fromisoformat()` 不认 `Z` 后缀**（3.11 才加入支持）。
代码用 `fromisoformat` 去解析自己刚写出的 `Z`，于是在 3.10 上必然抛
`ValueError: Invalid isoformat string`。

而 `pyproject.toml` 声明的是 `requires-python = ">=3.10"`——**声明支持 3.10，代码跑不了**。

## 2. 精确位置（三处，同一根因）

`src/family_digest/service.py`：

| 行 | 代码 | 方向 |
|---|---|---|
| 76 | `payload["occurred_at"] = datetime.fromisoformat(payload["occurred_at"])` | `_load_health`，**入站** |
| 86 | 同上 | `_load_safety`，**入站** |
| 136 | `self.store.append_health(r.model_dump(mode="json"))` | **写入 store，Z 从这里产生** |
| 139 | `self.store.append_safety(s.model_dump(mode="json"))` | 同上 |
| 158 / 162 | `_load_health(...)` / `_load_safety(...)` 从 store 读回 | **出站**，读回的又是 Z |

链条：调用方传 tz-aware 时间 → pydantic 序列化成 `Z` → `_load_*` 解析失败（入站炸）；
即使入站绕开，store 里存的是 `Z` → `build_daily_digest` 读回再解析（出站炸）。

## 3. 最小复现

```python
import os, sys, tempfile
from datetime import datetime, timezone
sys.path.insert(0, 'src')
from family_digest import FamilyDigestService, Policy
from family_digest.adapters import MockChannel
from family_digest.adapters.upstream import normalize_b_output
from family_digest.store import JsonStore

stamp = datetime.now(timezone.utc)          # ← 关键是带时区
svc = FamilyDigestService(store=JsonStore(os.path.join(tempfile.mkdtemp(), 's.json')),
                          channel=MockChannel(), policy=Policy())
svc.execute('set_consent', {'elder_id': 'e001',
                            'scopes': ['health_summary', 'medication_safety']})
h = normalize_b_output({'session_id': 's1', 'turn_id': 't1',
    'health_signal': {'type': 'symptom', 'detail': '下肢沉重/乏力',
                      'severity': 'moderate'},
    'safety': {'blocked': False, 'rule_id': None},
    'response': {'mode': 'health_care', 'text': 'x'},
    'metadata': {'semantic_backend': 'mock', 'qwen_called': False,
                 'latency_ms': 1.0}}, elder_id='e001', occurred_at=stamp)

row = h.model_dump(mode='json')
print(row['occurred_at'])          # '2026-09-28T14:45:18.442572Z'  ← Z 后缀

print(svc.execute('record_signals', {'elder_id': 'e001', 'session_id': 's1',
      'turn_id': 't1', 'health_signals': [row]})['status'])
# error   ← 入站就炸

print(svc.execute('build_daily_digest', {'elder_id': 'e001',
      'date': '2026-09-28', 'elder_name': '王奶奶'})['status'])
# error / degraded   ← 出站也炸
```

**实测输出**（本机 Python 3.10.12）：

```
传入的 occurred_at (tz-aware): datetime.datetime(2026, 9, 28, 14, 45, 18, 442572, tzinfo=datetime.timezone.utc)
model_dump 后的 occurred_at: '2026-09-28T14:45:18.442572Z'
1) record_signals     -> error
2) build_daily_digest -> degraded
```

> 若把 `occurred_at` 换成 naive 时间（`datetime.now()`，无 tzinfo），
> pydantic 序列化出不带后缀的字符串，3.10 能解析——**所以不传时区就复现不了**，
> 这也是现有测试没抓到的原因（见 §5）。

## 4. 建议修法

加一个小工具函数，两处 `_load_*` 都改用它（改动约 5 行）：

```python
def _parse_dt(value):
    """解析 ISO 时间戳，兼容 pydantic 输出的 Z 后缀（3.10 的 fromisoformat 不认）。"""
    if isinstance(value, str) and value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)
```

然后 service.py:76 / :86 改成：

```python
if isinstance(payload.get("occurred_at"), str):
    payload["occurred_at"] = _parse_dt(payload["occurred_at"])
```

**或者**（二选一，取决于你们的定位）：

- 把 `requires-python` 提到 `">=3.11"`，明确不支持 3.10；
- 写入 store 时统一用 `model_dump(mode="json")` 之前先 `replace(tzinfo=None)`，
  让往返都是 naive 字符串，彻底不出现 `Z`。

**更彻底的做法**是别用 JSON 字符串往返：`HealthSignalRecord.occurred_at` 本身就是
`datetime`，store 里存字符串再解析回来是白绕一圈。但那是重构，先按上面最小改动修即可。

## 5. 为什么现有 54 个测试没抓到

`tests/` 里所有 `occurred_at` 都是 **naive 时间**：

```python
# tests/test_routing.py:21
"occurred_at": datetime.fromisoformat(f"2026-09-{day}T09:00:00")
# tests/test_routing.py:39
occurred_at=datetime.fromisoformat("2026-09-28T09:00:00")
```

naive 时间序列化出来是 `'2026-09-28T09:00:00'`，没有 `Z`，3.10 解析正常。
**建议补一个 tz-aware 的回归用例**：

```python
def test_tz_aware_timestamps_round_trip():
    """pydantic 会把 UTC 时间写成 Z 后缀，3.10 的 fromisoformat 解析不了。"""
    from datetime import datetime, timezone
    ...
    stamp = datetime.now(timezone.utc)
    # record_signals 必须 ok，build_daily_digest 必须不 error
```

## 6. 联调侧现状（供参考）

编排层 `modules/orchestrator/orchestrator.py` 里有个 `_json_safe()`，把**入站** payload 的
`Z` 统一替换成 `+00:00`，所以调用方那边已经不炸。但它绕不过 skill4 **自己内部**的
store 往返（`model_dump` 写进去、`_load_*` 读回来），所以 `build_daily_digest` 仍然失败。
**这个 bug 必须在 skill4 内修**，外部无法绕过。

## 7. 同类问题提醒

联调过程中在另外两个模块发现过**完全同源**的问题，都是"声明支持 3.10 但用了 3.11 特性"：

| 模块 | 3.11 特性 | 状态 |
|---|---|---|
| `life-memoir-retriever` | `asyncio.timeout` | ✅ 已修（`_compat.py` + `async-timeout` 条件依赖），并把声明改为 `>=3.10` |
| `implicit-health-triage` | `datetime.UTC` | ⚠️ 我本地打过兼容补丁，需 Yunsheng 确认 |
| `family-digest-sync`（本单） | `datetime.fromisoformat` 解析 `Z` | ⏳ 待修 |

建议三个模块统一加一条 CI：在 **3.10** 上跑一遍测试。本机只有 3.10，这三个 bug 都是一跑就暴露。
