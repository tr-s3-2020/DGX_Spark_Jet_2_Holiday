"""Data model contracts (task guide sections 7, 30).

Values are Chinese on the wire — see ``schemas.HealthType`` / ``schemas.Severity``.
The model layer accepts **only** the canonical Chinese values; the extractor
additionally normalises English and common Chinese variants onto them.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from implicit_health_triage.extractor import HealthExtractor
from implicit_health_triage.schemas import (
    PAIN_OR_GENERIC,
    PAIN_TYPES,
    SYMPTOM_FAMILY,
    ConversationTurn,
    HealthSignal,
    HealthSignalRecord,
    HealthType,
    ResponseMode,
    Severity,
    TriageRequest,
    TriageResult,
    is_bodily_symptom,
    is_pain,
    none_signal,
)


def test_health_signal_keeps_the_three_required_fields():
    signal = HealthSignal(type="身体不适", detail="下肢沉重/乏力", severity="中等")

    assert signal.type == "身体不适"
    assert signal.detail == "下肢沉重/乏力"
    assert signal.severity == "中等"


def test_health_signal_serialises_to_plain_json():
    payload = HealthSignal(
        type=HealthType.SYMPTOM, detail="下肢沉重/乏力", severity=Severity.MODERATE
    ).model_dump(mode="json")

    assert payload == {
        "type": "身体不适",
        "detail": "下肢沉重/乏力",
        "severity": "中等",
    }


def test_health_signal_defaults():
    signal = HealthSignal(type="无")

    assert signal.detail == ""
    assert signal.severity == "轻微"


def test_none_signal_is_a_fresh_object():
    first, second = none_signal(), none_signal()

    assert first == second
    assert first is not second


def test_health_signal_forbids_extra_fields():
    with pytest.raises(ValidationError):
        HealthSignal(type="无", detail="", severity="轻微", diagnosis="flu")


# ---------------------------------------------------------------------------
# Values are Chinese on the wire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["轻微", "中等", "需留意"])
def test_severity_enum_values(severity):
    assert HealthSignal(type="无", severity=severity).severity == severity


@pytest.mark.parametrize(
    "health_type",
    ["无", "身体不适", "睡眠", "疼痛", "用药", "行动能力", "食欲", "其他体征"],
)
def test_health_type_enum_values(health_type):
    assert HealthSignal(type=health_type).type == health_type


def test_every_enum_value_is_chinese():
    """The whole point of the Chinese-value decision — guard it."""

    for member in list(HealthType) + list(Severity):
        assert any("\u4e00" <= ch <= "\u9fff" for ch in member.value), member.value


def test_english_values_are_rejected_by_the_model():
    """English is normalised by the extractor, not accepted by the model.

    A caller that hand-builds a signal must use the canonical Chinese values.
    """

    for english in ("none", "symptom", "pain", "sleep", "medication"):
        with pytest.raises(ValidationError):
            HealthSignal(type=english)

    for english in ("low", "moderate", "high"):
        with pytest.raises(ValidationError):
            HealthSignal(type="无", severity=english)


def test_unknown_severity_is_rejected():
    with pytest.raises(ValidationError):
        HealthSignal(type="无", severity="critical")


def test_severity_values_avoid_clinical_wording():
    """`severity` is an internal routing tag, not a diagnosis.

    The values deliberately avoid a bare 「轻 / 中 / 重」 clinical scale.
    """

    forbidden = ("严重", "中度", "重度", "危急", "危重", "轻症", "重症", "病情")

    for member in Severity:
        for word in forbidden:
            assert word not in member.value, member.value


def test_detail_length_is_bounded():
    with pytest.raises(ValidationError):
        HealthSignal(type="身体不适", detail="x" * 257)


def test_triage_result_shape_matches_documented_contract():
    result = TriageResult(
        text="今天早上起来腿沉得很，买菜走两步就得歇着。",
        health_signal=HealthSignal(
            type="身体不适", detail="下肢沉重/乏力", severity="中等"
        ),
        guardrail_triggered=False,
        response_mode=ResponseMode.HEALTH_CARE,
        response="听起来您今天走路挺费劲的。您今天平时该吃的药都按原来的安排吃了吗？",
        rule_id=None,
    )

    payload = result.model_dump(mode="json")

    assert set(payload) == {
        "text",
        "health_signal",
        "guardrail_triggered",
        "response_mode",
        "response",
        "rule_id",
    }
    assert payload["rule_id"] is None
    assert payload["response_mode"] == "health_care"


def test_triage_request_defaults_to_final():
    request = TriageRequest(session_id="s1", turn_id="t1", text="你好")

    assert request.is_final is True


def test_conversation_turn_upstream_contract():
    turn = ConversationTurn(
        session_id="s001",
        turn_id="t018",
        text="我降压药今天能不能吃两颗？",
        timestamp="2026-09-22T10:30:00",
    )

    assert turn.speaker == "elder"
    assert turn.is_final is True


def test_health_signal_record_for_family_digest():
    record = HealthSignalRecord(
        timestamp="2026-09-22T10:30:00",
        type="身体不适",
        detail="下肢沉重/乏力",
        severity="中等",
    )

    assert record.model_dump(mode="json") == {
        "timestamp": "2026-09-22T10:30:00",
        "type": "身体不适",
        "detail": "下肢沉重/乏力",
        "severity": "中等",
    }


# ---------------------------------------------------------------------------
# The symptom hierarchy (decision C: keep subtypes, publish the parent/child
# relationship so a consumer counting "疼痛" does not silently miss data)
# ---------------------------------------------------------------------------


def test_symptom_family_contains_the_generic_and_its_subtypes():
    assert SYMPTOM_FAMILY == {"身体不适", "疼痛", "睡眠", "食欲", "行动能力"}


def test_symptom_family_excludes_the_non_complaint_types():
    assert SYMPTOM_FAMILY.isdisjoint({"无", "用药", "其他体征"})


def test_symptom_family_covers_every_health_type_except_the_three_outsiders():
    everything = {member.value for member in HealthType}
    assert everything - SYMPTOM_FAMILY == {"无", "用药", "其他体征"}


def test_is_bodily_symptom_accepts_the_generic_and_every_subtype():
    for health_type in SYMPTOM_FAMILY:
        assert is_bodily_symptom(health_type) is True, health_type


def test_is_bodily_symptom_rejects_the_outsiders():
    for health_type in ("无", "用药", "其他体征"):
        assert is_bodily_symptom(health_type) is False, health_type


def test_is_bodily_symptom_accepts_the_enum_member_too():
    assert is_bodily_symptom(HealthType.PAIN) is True


def test_is_pain_distinguishes_precise_from_generic():
    """The distinction the whole decision hinges on."""

    assert is_pain("疼痛") is True
    assert is_pain("身体不适") is False

    assert is_pain("身体不适", include_generic=True) is True
    assert is_pain("疼痛", include_generic=True) is True


def test_is_pain_rejects_non_pain_family_members():
    for health_type in ("睡眠", "食欲", "行动能力", "无", "用药", "其他体征"):
        assert is_pain(health_type, include_generic=True) is False, health_type


def test_pain_or_generic_is_exactly_pain_plus_symptom():
    assert PAIN_OR_GENERIC == PAIN_TYPES | {"身体不适"}


def test_counting_pain_by_equality_to_generic_would_miss_data():
    """Documents *why* the helpers exist, using a concrete regression."""

    knee_pain = HealthExtractor.parse(
        '{"type":"疼痛","detail":"膝盖疼痛","severity":"中等"}'
    )

    assert knee_pain is not None
    # The naive test a consumer might write:
    assert (knee_pain.type == "身体不适") is False  # ...would drop this signal
    # The correct tests:
    assert is_bodily_symptom(knee_pain.type) is True
    assert is_pain(knee_pain.type, include_generic=True) is True


# ---------------------------------------------------------------------------
# The extractor normalises model output onto the canonical Chinese values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("身体不适", "身体不适"),
        ("symptom", "身体不适"),
        ("症状", "身体不适"),
        ("疼痛", "疼痛"),
        ("pain", "疼痛"),
        ("痛", "疼痛"),
        ("睡眠", "睡眠"),
        ("sleep", "睡眠"),
        ("失眠", "睡眠"),
        ("用药", "用药"),
        ("medication", "用药"),
        ("食欲", "食欲"),
        ("appetite", "食欲"),
        ("行动能力", "行动能力"),
        ("mobility", "行动能力"),
        ("其他体征", "其他体征"),
        ("other", "其他体征"),
        ("无", "无"),
        ("none", "无"),
        ("没有", "无"),
    ],
)
def test_type_normalisation_covers_chinese_and_english(raw_type: str, expected: str):
    """A model may answer in either language; both must land on canonical values."""

    signal = HealthExtractor.parse(f'{{"type":"{raw_type}","detail":"x","severity":"中等"}}')

    assert signal is not None, raw_type
    assert signal.type == expected


@pytest.mark.parametrize(
    ("raw_severity", "expected"),
    [
        ("轻微", "轻微"),
        ("low", "轻微"),
        ("轻度", "轻微"),
        ("中等", "中等"),
        ("moderate", "中等"),
        ("medium", "中等"),
        ("中度", "中等"),
        ("需留意", "需留意"),
        ("high", "需留意"),
        ("严重", "需留意"),
        ("重度", "需留意"),
    ],
)
def test_severity_normalisation_covers_chinese_and_english(
    raw_severity: str, expected: str
):
    signal = HealthExtractor.parse(
        f'{{"type":"疼痛","detail":"x","severity":"{raw_severity}"}}'
    )

    assert signal is not None, raw_severity
    assert signal.severity == expected
