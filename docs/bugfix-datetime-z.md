# 修复说明：Python 3.10 上带时区时间戳导致 `record_signals` 报错

**来源**：联调编排层 `modules/orchestrator` 于 2026-09-29 提交的 bug 报告（`skill4-issue-datetime-z`）
**严重度**：高（调用方传 tz-aware 时间即完全记不进去）
**状态**：✅ 已修复，Python **3.10.21** 实测通过

---

## 1. 根因（与报告一致）

`pydantic` 的 `model_dump(mode="json")` 把 **UTC 时间**序列化成 `"...Z"` 后缀，
而 `datetime.fromisoformat()` **3.11 才支持解析 `Z`**。代码用自己的 `fromisoformat`
去解析自己刚写出的 `Z`，在 3.10 上必然抛 `ValueError`。

`pyproject.toml` 声明 `requires-python = ">=3.10"` —— 声明支持 3.10，代码跑不了。

## 2. 修法：新增 `src/family_digest/timeutil.py`，一处收口

报告建议只做 `Z → +00:00` 的替换。**我额外多做了一步**，因为只替换会留下第二个坑：

```python
def iso_z_to_offset(value: str) -> str:   # Z -> +00:00（3.10 能解析）
def parse_datetime(value) -> datetime     # 兼容 Z 与 datetime 输入
def to_local_naive(dt) -> datetime        # tz-aware -> 本地墙钟 naive
def normalize_stamp(value) -> datetime    # 上面两步合一，落库前统一调用
```

### 为什么不只是替换 `Z`

替换 `Z` 只解决"能不能解析"，不解决**日期算错**。`local_date` 用 `occurred_at.date()`，
tz-aware 的 UTC 时间会按 **UTC** 取日期：

> 北京时间 9-29 早上 7 点 = UTC 9-28 23:00 → `local_date` 算成 **9-28**

日报是按天生成的，串一天意味着家属今天收到的其实是昨天的内容，而且跨日趋势的天数也会错位。

所以 `normalize_stamp` 在解析后统一 `astimezone()` 转本地墙钟再去掉 tzinfo：

- store 里**不再是 `Z`**，从源头消除 3.10 风险（不需要靠下游解析兜底）；
- `local_date` 按老人所在时区的"今天"算，符合直觉；
- 单地部署（主控与技能同机）下这是最合理的口径。

### 改动点

| 文件 | 改动 |
|---|---|
| `src/family_digest/timeutil.py` | **新增**，上述四个函数 |
| `service.py:76 / :86` | `_load_health` / `_load_safety` 改用 `normalize_stamp` |
| `adapters/upstream.py` | `to_digest_record` 的 `timestamp` 也走 `normalize_stamp`（B 也可能给 `Z`） |

入站和出站都走 `_load_*`，所以**一处改动同时解决入站解析和 store 往返**两个方向。

## 3. 验证证据（真实 Python 3.10.21，非模拟）

本机拉了 CPython **3.10.21** 实跑。同一份复现脚本、同一台 3.10：

| | 修复前（`6968c2d`） | 修复后 |
|---|---|---|
| `record_signals` | `error` — `ValueError: Invalid isoformat string: '2026-09-29T02:10:16.506185Z'` | `ok`，`accepted=1` |
| `build_daily_digest` | `degraded` | `ok`，`tier=P2` |
| 测试 | — | **68 passed**（3.10） |

测试数 54 → **68**（新增 `tests/test_timeutil.py`，14 项）。

## 4. 新增的回归测试怎么做到"不依赖本机版本"

本机默认是 3.13，其 `fromisoformat` 本来就认 `Z` —— **在高版本上跑通证明不了 3.10 兼容**。
所以 `tests/test_timeutil.py` 里做了两层：

1. `_py310_fromisoformat()`：模拟 3.10 严格行为，断言**未经处理**的 `Z` 字符串确实会抛错（证明 bug 仍被守住），
   而 `normalize_stamp` 之后的字符串能解析；
2. `test_full_chain_under_simulated_python310`：用 `monkeypatch` 把 `timeutil.datetime`
   换成 3.10 严格子类，跑完整条 `record_signals → build_daily_digest`。
   **这条在任意版本上都能守住 3.10 兼容性。**

另外补了：tz-aware 重复投递仍被去重、A 侧 safety 事件走同一路径、UTC 深夜不串天、
B 的 `Z` 时间戳可解析。

## 5. 配套：加了 CI（报告 §7 的建议）

`.github/workflows/tests.yml` 在 **3.10 / 3.11 / 3.12 / 3.13** 四个版本上跑测试。
报告指出这三个模块都栽在"声明支持 3.10 却用了 3.11 特性"：

| 模块 | 3.11 特性 | 状态 |
|---|---|---|
| `life-memoir-retriever` | `asyncio.timeout` | 已修 |
| `implicit-health-triage` | `datetime.UTC` | ⚠️ 待 Yunsheng 确认 |
| `family-digest-sync`（本单） | `fromisoformat` 解析 `Z` | ✅ 已修 |

**建议另外两个模块也把这条 CI 抄过去** —— 本机只有 3.10 的人一跑就暴露，但 CI 能在合并前拦住。

## 6. 给编排层的话

`orchestrator._json_safe()` 把入站 `Z` 换成 `+00:00` 的做法**可以保留，无害**，
但已经不再必要：skill4 现在自己能处理 `Z`。真正绕不过去的是 skill4 内部的 store 往返，
那部分已在模块内修掉。

如果发现别的模块也有类似问题，改法是通用的：**别让 `Z` 落到存储里**，
进库前统一成本地墙钟时间。
