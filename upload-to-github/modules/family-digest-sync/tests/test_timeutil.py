"""时间处理回归测试。

背景：联调编排层 2026-09-29 报的 bug —— pydantic 把 UTC 时间序列化成 ``...Z``，
而 Python **3.10** 的 ``datetime.fromisoformat()`` 不认 ``Z``，导致
``record_signals`` 直接 error、``build_daily_digest`` 失败。

本模块声明支持 ``>=3.10``，所以这里用 :func:`_py310_fromisoformat` **模拟 3.10
的严格行为**来断言——这样即使在 3.11+ 上跑测试，也能真正守住 3.10 兼容性。
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from family_digest import FamilyDigestService, Policy
from family_digest.adapters import MockChannel
from family_digest.adapters.upstream import normalize_b_output
from family_digest.models import HealthSignalRecord, SafetyEventRecord
from family_digest.timeutil import (
    iso_z_to_offset,
    normalize_stamp,
    parse_datetime,
    to_local_naive,
)


def _py310_fromisoformat(s: str) -> datetime:
    """模拟 Python 3.10：``Z`` 后缀直接抛 ValueError。"""
    if s.endswith(("Z", "z")):
        raise ValueError("Invalid isoformat string: 'Z' (Python 3.10)")
    return datetime.fromisoformat(s)


# --------------------------------------------------------------------------
# 单元：替换逻辑本身（高版本 Python 上也能验证）
# --------------------------------------------------------------------------


def test_iso_z_to_offset():
    assert iso_z_to_offset("2026-09-28T14:45:18Z") == "2026-09-28T14:45:18+00:00"
    assert iso_z_to_offset("2026-09-28T14:45:18.442572Z").endswith("+00:00")
    assert iso_z_to_offset("2026-09-28T14:45:18+08:00") == "2026-09-28T14:45:18+08:00"
    assert iso_z_to_offset("2026-09-28T14:45:18") == "2026-09-28T14:45:18"


def test_parse_datetime_z_is_utc():
    dt = parse_datetime("2026-09-28T14:45:18Z")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


def test_parse_datetime_passthrough():
    naive = datetime(2026, 9, 28, 14, 45, 18)
    assert parse_datetime(naive) is naive
    assert parse_datetime("2026-09-28T14:45:18") == naive


def test_to_local_naive():
    aware = datetime(2026, 9, 28, 14, 45, 18, tzinfo=timezone.utc)
    out = to_local_naive(aware)
    assert out.tzinfo is None                       # 落库后不会再产生 Z
    assert out == aware.astimezone().replace(tzinfo=None)
    naive = datetime(2026, 9, 28, 14, 45, 18)
    assert to_local_naive(naive) == naive           # naive 原样


def test_normalize_stamp_never_leaves_z():
    """normalize 出来的 datetime 序列化后绝不能带 Z。"""
    for value in (
        datetime(2026, 9, 28, 14, 45, 18, tzinfo=timezone.utc),
        "2026-09-28T14:45:18Z",
        "2026-09-28T14:45:18+00:00",
        datetime(2026, 9, 28, 14, 45, 18),
    ):
        assert not normalize_stamp(value).isoformat().endswith("Z")


# --------------------------------------------------------------------------
# 回归：pydantic 往返后的字符串，3.10 能不能解析
# --------------------------------------------------------------------------


def test_model_dump_output_is_parseable_on_py310():
    """核心回归：tz-aware 落库 → 读回的字符串，3.10 必须能解析。"""
    aware = datetime(2026, 9, 28, 14, 45, 18, tzinfo=timezone.utc)
    row = HealthSignalRecord(
        event_id="e1", elder_id="e1", occurred_at=aware
    ).model_dump(mode="json")
    # 未处理前确实是 Z（这就是 3.10 炸的原因）
    assert row["occurred_at"].endswith("Z")
    # 3.10 会抛 —— 用模拟函数验证（不再直接用 fromisoformat）
    with pytest.raises(ValueError):
        _py310_fromisoformat(row["occurred_at"])
    # 经 normalize 后必须能解析
    _py310_fromisoformat(normalize_stamp(row["occurred_at"]).isoformat())


def test_store_round_trip_never_writes_z(store):
    """落库再读回的字符串，3.10 必须能解析（这是 bug 的真正触发点）。"""
    aware = datetime(2026, 9, 28, 14, 45, 18, tzinfo=timezone.utc)
    rec = HealthSignalRecord(event_id="e2", elder_id="e1", occurred_at=aware)
    store.append_health(
        rec.model_copy(update={"occurred_at": normalize_stamp(aware)}).model_dump(
            mode="json"
        )
    )
    row = store.health_records()[0]
    assert not row["occurred_at"].endswith("Z")
    _py310_fromisoformat(row["occurred_at"])  # 3.10 不抛


# --------------------------------------------------------------------------
# 端到端：调用方传 tz-aware，整条链不能 error
# --------------------------------------------------------------------------


@pytest.fixture
def svc(store):
    s = FamilyDigestService(store=store, channel=MockChannel(), policy=Policy())
    s.execute("set_consent", {"elder_id": "e001",
                              "scopes": ["health_summary", "medication_safety"]})
    return s


def _tz_aware_row(elder_id="e001", when=None):
    stamp = when or datetime.now(timezone.utc)
    h = normalize_b_output(
        {"session_id": "s1", "turn_id": "t1",
         "health_signal": {"type": "symptom", "detail": "下肢沉重/乏力",
                           "severity": "moderate"},
         "safety": {"blocked": False, "rule_id": None},
         "response": {"mode": "health_care", "text": "x"},
         "metadata": {"semantic_backend": "mock", "qwen_called": False,
                      "latency_ms": 1.0}},
        elder_id=elder_id, occurred_at=stamp,
    )
    return h.model_dump(mode="json")  # 这里会产生 Z —— 正是 bug 的输入形态


def test_tz_aware_record_signals_is_ok(svc):
    row = _tz_aware_row()
    assert row["occurred_at"].endswith("Z")   # 确认复现条件成立
    res = svc.execute("record_signals", {"elder_id": "e001", "session_id": "s1",
                                         "turn_id": "t1", "health_signals": [row]})
    assert res["status"] == "ok", res.get("error")
    assert res["data"]["accepted"] == 1


def test_tz_aware_digest_is_not_error(svc):
    """bug 报告里这条是 degraded —— 修复后必须能正常出卡片。"""
    svc.execute("record_signals", {"elder_id": "e001", "session_id": "s1",
                                   "turn_id": "t1",
                                   "health_signals": [_tz_aware_row()]})
    local_day = to_local_naive(datetime.now(timezone.utc)).date().isoformat()
    res = svc.execute("build_daily_digest", {"elder_id": "e001",
                                             "date": local_day,
                                             "elder_name": "王奶奶"})
    assert res["status"] != "error", res.get("error")
    assert res["data"] is not None


def test_repeat_with_same_tz_aware_stamp_is_idempotent(svc):
    """同一 tz-aware 事件重复投递，第二次必须被去重跳过。"""
    row = _tz_aware_row()
    a = svc.execute("record_signals", {"elder_id": "e001", "session_id": "s1",
                                       "turn_id": "t1", "health_signals": [row]})
    b = svc.execute("record_signals", {"elder_id": "e001", "session_id": "s1",
                                       "turn_id": "t1", "health_signals": [row]})
    assert a["data"]["accepted"] == 1
    assert b["data"]["accepted"] == 0 and len(b["data"]["skipped"]) == 1


def test_tz_aware_safety_event_round_trip(store):
    """A 侧 safety 事件走同一条路径，不能只修健康信号。"""
    aware = datetime(2026, 9, 28, 14, 45, 18, tzinfo=timezone.utc)
    s = SafetyEventRecord(event_id="a1", elder_id="e1", occurred_at=aware,
                          safety_level="P0")
    row = s.model_dump(mode="json")
    assert row["occurred_at"].endswith("Z")
    _py310_fromisoformat(normalize_stamp(row["occurred_at"]).isoformat())


def test_local_date_uses_local_wall_clock():
    """UTC 深夜 ≠ 本地当天：日报按天生成，串天会让家属看到错的内容。"""
    # UTC 2026-09-28T16:00 → 东八区已是 2026-09-29 00:00
    aware = datetime(2026, 9, 28, 16, 0, 0, tzinfo=timezone.utc)
    rec = HealthSignalRecord(event_id="e3", elder_id="e1",
                             occurred_at=normalize_stamp(aware))
    assert rec.local_date == to_local_naive(aware).date()
    assert rec.occurred_at.tzinfo is None


def test_full_chain_under_simulated_python310(monkeypatch):
    """在**模拟的 3.10** 上跑完整条链 —— 不依赖本机 Python 版本。

    本机是 3.11+，``fromisoformat`` 本来就认 ``Z``，所以普通的端到端测试
    *证明不了* 3.10 兼容性。这里把 ``timeutil.datetime`` 换成 3.10 严格版
    （``Z`` 直接抛 ``ValueError``），再跑 record_signals → build_daily_digest。
    """
    import family_digest.timeutil as tu

    class Py310Datetime(datetime):
        @classmethod
        def fromisoformat(cls, s):  # 3.10 行为：不认 Z
            if isinstance(s, str) and s.endswith(("Z", "z")):
                raise ValueError("Invalid isoformat string: 'Z' (Python 3.10)")
            return super().fromisoformat(s)

    monkeypatch.setattr(tu, "datetime", Py310Datetime)

    import tempfile
    from family_digest.store import JsonStore

    with tempfile.TemporaryDirectory() as tmp:
        svc = FamilyDigestService(store=JsonStore(os.path.join(tmp, "s.json")),
                                  channel=MockChannel(), policy=Policy())
        svc.execute("set_consent", {"elder_id": "e001",
                                    "scopes": ["health_summary"]})
        row = _tz_aware_row()
        assert row["occurred_at"].endswith("Z")     # 复现条件成立
        r1 = svc.execute("record_signals", {"elder_id": "e001", "session_id": "s1",
                                            "turn_id": "t1",
                                            "health_signals": [row]})
        assert r1["status"] == "ok", r1.get("error")

        day = to_local_naive(datetime.now(timezone.utc)).date().isoformat()
        r2 = svc.execute("build_daily_digest", {"elder_id": "e001", "date": day,
                                                "elder_name": "王奶奶"})
        assert r2["status"] != "error", r2.get("error")


def test_b_digest_record_accepts_z_timestamp():
    """B 的 to_digest_record 时间戳也可能带 Z。"""
    r = normalize_b_output(
        {"timestamp": "2026-09-22T10:30:00Z", "type": "睡眠",
         "detail": "入睡困难", "severity": "中等"}, elder_id="e1")
    assert r.occurred_at.tzinfo is None
    assert r.occurred_at.hour == to_local_naive(
        datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)).hour
