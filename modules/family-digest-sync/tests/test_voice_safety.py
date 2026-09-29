"""纯语音场景（A→D）：卡片不能把「出了 P0」说成「今天没事」。

来源：子扬（skill1 elderly-voice-duplex）2026-09-29 的联调报告。
根因是 D 的三处逻辑只认 B 的 health record、不认 A 的 safety event：

1. P0 标题只看 ``records.safety_blocked`` → 纯语音 P0 拿不到安全标题；
2. ``sources`` 只从 records 取 → 语音 P0 溯源为空；
3. ``summarize_acquisition`` 只看 records → 判成 ``not_acquired``，
   家属读到「本次未能获取健康信息」，与事实方向相反。

外加一条报告里没提但同源的：health 段为空时的兜底句
「今天没有需要特别说明的健康观察」在语音 P0 当天同样是反向误导。
"""

from datetime import datetime

from family_digest import Acquisition, ConsentScope, SafetyEventRecord, SafetyLevel, Tier
from family_digest.adapters import MockChannel
from family_digest.adapters.upstream import normalize_a_safety
from family_digest.cards import NO_SIGNAL_LINE, build_card, render_plain_text
from family_digest.models import ChronicleInput, HealthSignalRecord
from family_digest.routing import summarize_acquisition
from family_digest.store import JsonStore

DAY = datetime(2026, 9, 29, 10, 0, 0)
TODAY = DAY.date()
ALL_SCOPES = [
    ConsentScope.HEALTH_SUMMARY.value,
    ConsentScope.MEDICATION_SAFETY.value,
    ConsentScope.CHRONICLE.value,
]

MISLEADING = "本次未能获取健康信息"


def voice_p0(session="s1", turn="t3"):
    return SafetyEventRecord(
        event_id="a:p0",
        elder_id="e1",
        occurred_at=DAY,
        safety_level=SafetyLevel.P0,
        text="我那个降压药今天能不能吃两颗？",
        session_id=session,
        turn_id=turn,
    )


def _card(safety, *, tier=Tier.P0, granted=None, records=()):
    return build_card(
        card_id=f"card:e1:{TODAY}:{tier.value}",
        elder_id="e1",
        elder_name="王奶奶",
        for_date=TODAY,
        tier=tier,
        records=list(records),
        chronicle=ChronicleInput(),
        granted=granted if granted is not None else ALL_SCOPES,
        degraded_suspected=False,
        acquisition=summarize_acquisition(list(records), list(safety)),
        safety=list(safety),
    )


# --------------------------------------------------------------------------
# acquisition：老人开口说了风险句，就是采集到了
# --------------------------------------------------------------------------


def test_acquisition_is_acquired_when_only_voice_safety_exists():
    assert summarize_acquisition([], [voice_p0()]) is Acquisition.ACQUIRED


def test_acquisition_stays_not_acquired_without_any_input():
    assert summarize_acquisition([], []) is Acquisition.NOT_ACQUIRED


def test_acquisition_none_safety_event_does_not_count_as_acquired():
    none_evt = SafetyEventRecord(event_id="a:x", elder_id="e1", occurred_at=DAY)
    assert summarize_acquisition([], [none_evt]) is Acquisition.NOT_ACQUIRED


def test_voice_only_card_has_no_misleading_warning():
    card = _card([voice_p0()])
    assert card.acquisition is Acquisition.ACQUIRED
    assert card.warnings == []
    assert MISLEADING not in render_plain_text(card)


# --------------------------------------------------------------------------
# 标题与溯源
# --------------------------------------------------------------------------


def test_voice_p0_gets_medication_safety_title():
    card = _card([voice_p0()])
    assert "用药/安全风险" in card.title
    assert "建议尽快联系王奶奶" in card.title


def test_voice_p0_title_falls_back_without_consent():
    card = _card([voice_p0()], granted=[ConsentScope.HEALTH_SUMMARY.value])
    assert "用药/安全风险" not in card.title


def test_voice_p0_sources_include_session_and_turn():
    card = _card([voice_p0(session="sess-9", turn="turn-2")])
    assert "sess-9:turn-2" in card.sources


def test_safety_without_turn_id_adds_no_bogus_source():
    evt = voice_p0()
    evt.turn_id = ""
    assert _card([evt]).sources == []


def test_voice_p1_maps_to_p1_notice():
    """A 判定「需要留意」必须映射成 D 的 P1，不能静默丢掉。"""
    from family_digest.routing import route

    evt = SafetyEventRecord(event_id="a:p1", elder_id="e1", occurred_at=DAY,
                            safety_level=SafetyLevel.P1)
    decision = route([], [evt], [], "e1", TODAY)
    assert decision.tier is Tier.P1
    assert decision.reasons == ["voice_safety_p1"]
    assert decision.acquisition is Acquisition.ACQUIRED


# --------------------------------------------------------------------------
# health 段兜底句
# --------------------------------------------------------------------------


def test_health_section_does_not_claim_nothing_happened_on_voice_p0():
    text = render_plain_text(_card([voice_p0()]))
    assert NO_SIGNAL_LINE not in text
    assert "需要立即关注的表述" in text


def test_health_section_out_of_scope_line_when_no_consent():
    card = _card([voice_p0()], granted=[ConsentScope.HEALTH_SUMMARY.value])
    body = [s["body"] for s in card.sections if s["heading"] == "健康观察"][0]
    assert "超出当前授权范围" in body
    assert NO_SIGNAL_LINE not in body


def test_health_section_keeps_original_line_when_really_empty():
    card = _card([], tier=Tier.P2)
    body = [s["body"] for s in card.sections if s["heading"] == "健康观察"][0]
    assert NO_SIGNAL_LINE in body


def test_health_records_still_render_normally_alongside_safety():
    rec = HealthSignalRecord(
        event_id="b1", elder_id="e1", occurred_at=DAY,
        signal_type="sleep", severity="moderate", detail="夜里醒了三次",
        session_id="sb", turn_id="tb", semantic_backend="mock",
    )
    card = _card([voice_p0()], records=[rec])
    body = [s["body"] for s in card.sections if s["heading"] == "健康观察"][0]
    assert "夜里醒了三次" in body
    assert NO_SIGNAL_LINE not in body
    assert set(card.sources) == {"s1:t3", "sb:tb"}


# --------------------------------------------------------------------------
# A 的适配器（session/turn 以前被吞了）
# --------------------------------------------------------------------------


def test_normalize_a_safety_keeps_session_and_turn():
    rec = normalize_a_safety(
        {"type": "final", "safety": "P0", "text": "能不能吃两颗",
         "session_id": "s7", "turn_id": "t11"},
        elder_id="e1",
        occurred_at=DAY,
    )
    assert rec.safety_level is SafetyLevel.P0
    assert rec.source_ref() == "s7:t11"


def test_normalize_a_safety_without_ids_yields_empty_ref():
    rec = normalize_a_safety({"safety": "P1", "text": "有点晕"}, elder_id="e1")
    assert rec.source_ref() == ""


# --------------------------------------------------------------------------
# 端到端：A 抛 P0 → D 出 P0 卡片，且 status 是 ok 不是 degraded
# --------------------------------------------------------------------------


def test_end_to_end_voice_p0(tmp_path):
    svc = __import__("family_digest").FamilyDigestService(
        store=JsonStore(str(tmp_path / "s.json")), channel=MockChannel()
    )
    svc.execute("set_consent", {"elder_id": "e1", "scopes": ALL_SCOPES})
    svc.execute("record_signals", {"safety_events": [voice_p0().model_dump(mode="json")]})

    res = svc.execute("build_daily_digest",
                      {"elder_id": "e1", "date": TODAY.isoformat()})
    assert res["status"] == "ok", res
    data = res["data"]
    assert data["tier"] == "P0"
    assert data["acquisition"] == "acquired"
    assert data["warnings"] == []
    assert "用药/安全风险" in data["title"]
    assert data["sources"] == ["s1:t3"]
    assert res["meta"]["routing"]["reasons"] == ["voice_safety_p0"]
