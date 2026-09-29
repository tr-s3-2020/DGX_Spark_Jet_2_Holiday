"""上游归一化层测试：证明 D 吃得下 A/B/C 的真实报文格式。"""

from datetime import datetime

import pytest

from family_digest.adapters.upstream import (
    b_output_is_ignored,
    make_event_id,
    normalize_a_safety,
    normalize_b_digest_record,
    normalize_b_output,
    normalize_c_chronicle,
)
from family_digest.models import SafetyLevel

STAMP = datetime(2026, 9, 28, 9, 0, 0)

# B 的真实响应体（取自团队库 implicit-health-triage 样例 t2）
B_T2 = {
    "session_id": "demo",
    "turn_id": "t2",
    "health_signal": {"type": "symptom", "detail": "下肢沉重/乏力", "severity": "moderate"},
    "safety": {"blocked": False, "rule_id": None},
    "response": {"mode": "health_care", "text": "..."},
    "metadata": {"semantic_backend": "mock", "qwen_called": False},
}

# B 的用药阻断（样例 t3）
B_T3 = {
    "session_id": "demo",
    "turn_id": "t3",
    "health_signal": {"type": "medication", "detail": "询问增加降压药剂量", "severity": "high"},
    "safety": {"blocked": True, "rule_id": "medication_dose_increase"},
    "response": {"mode": "medication_safety", "text": "..."},
    "metadata": {"semantic_backend": "none", "qwen_called": False},
}


def test_b_output_normalized():
    r = normalize_b_output(B_T2, elder_id="e001", occurred_at=STAMP)
    assert r.elder_id == "e001"
    assert r.session_id == "demo" and r.turn_id == "t2"
    assert r.signal_type.value == "symptom"
    assert r.severity.value == "moderate"
    assert r.safety_blocked is False
    assert r.response_mode.value == "health_care"


def test_b_blocked_carries_rule():
    r = normalize_b_output(B_T3, elder_id="e001", occurred_at=STAMP)
    assert r.safety_blocked is True
    assert r.rule_id == "medication_dose_increase"


def test_b_missing_ids_get_deterministic_event_id():
    """B 不提供 event_id —— D 必须自己造，且同一轮必须造出同一个，否则去重失效。"""
    a = normalize_b_output(B_T2, elder_id="e001", occurred_at=STAMP)
    b = normalize_b_output(B_T2, elder_id="e001", occurred_at=STAMP)
    assert a.event_id == b.event_id
    assert a.event_id.startswith("b:")


def test_different_turns_get_different_event_ids():
    a = normalize_b_output(B_T2, elder_id="e001", occurred_at=STAMP)
    b = normalize_b_output(B_T3, elder_id="e001", occurred_at=STAMP)
    assert a.event_id != b.event_id


def test_event_id_stable_helper():
    assert make_event_id("x", "a", "b") == make_event_id("x", "a", "b")
    assert make_event_id("x", "a", "b") != make_event_id("x", "a", "c")


def test_b_sample_wrapper_is_unwrapped():
    r = normalize_b_output({"expected_output_subset": B_T2}, elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "symptom"


def test_a_safety_normalized():
    s = normalize_a_safety({"type": "final", "safety": "P0", "session_id": "s1",
                            "turn_id": "t7", "text": "能不能吃两颗"},
                           elder_id="e001", occurred_at=STAMP)
    assert s.safety_level is SafetyLevel.P0
    assert s.event_id.startswith("a:")


def test_a_unknown_level_falls_back_to_none():
    s = normalize_a_safety({"safety": "P9"}, elder_id="e1", occurred_at=STAMP)
    assert s.safety_level is SafetyLevel.NONE


def test_c_envelope_is_flattened():
    env = {
        "status": "ok",
        "data": {
            "state": "ready", "view_version": "v1", "format": "structured",
            "content": {"items": [{"title": "a"}], "narrative": "故事"},
            "warnings": [],
        },
    }
    c = normalize_c_chronicle(env)
    assert c.state == "ready"
    assert c.items == [{"title": "a"}]
    assert c.narrative == "故事"


def test_c_flat_form_still_works():
    c = normalize_c_chronicle({"state": "ready", "items": [], "narrative": "n"})
    assert c.narrative == "n"


def test_c_degraded_is_not_silently_empty():
    """C 的 degraded/not_ready 必须变成卡片里的警告，不能被当成「没有回忆」。"""
    env = {"status": "degraded",
           "data": {"state": "not_ready", "view_version": None, "content": None,
                    "warnings": ["CHRONICLE_NOT_READY"]}}
    c = normalize_c_chronicle(env)
    assert c.state == "not_ready"
    assert c.items == [] and c.narrative == ""
    assert any("degraded" in w for w in c.warnings)
    assert any("not_ready" in w or "尚未生成" in w for w in c.warnings)


@pytest.mark.parametrize("bad", [{}, {"data": None}, {"data": {"content": None}}])
def test_c_malformed_never_crashes(bad):
    c = normalize_c_chronicle(bad)
    assert c.state in ("ready", "not_ready")


# --------------------------------------------------------------------------
# B 2026-09-28 交付版：两套 schema 并存
# --------------------------------------------------------------------------

# 新版（schemas/TriageResponse.schema.json）：中文枚举 + result 嵌套
B_NEW_MEDICATION = {
    "session_id": "s1",
    "turn_id": "t1",
    "result": {
        "text": "我降压药今天能不能吃两颗？",
        "health_signal": {"type": "用药", "detail": "涉及具体用药调整询问",
                          "severity": "需留意"},
        "guardrail_triggered": True,
        "response_mode": "medication_safety",
        "response": "这个涉及具体的用药剂量，我不能替您决定……",
        "rule_id": "MEDICATION_CHANGE_REQUEST",
    },
}

B_NEW_SYMPTOM = {
    "session_id": "s1",
    "turn_id": "t2",
    "result": {
        "text": "今天早上起来腿沉得很",
        "health_signal": {"type": "身体不适", "detail": "下肢沉重/乏力",
                          "severity": "中等"},
        "guardrail_triggered": False,
        "response_mode": "health_care",
        "response": "听起来您今天走路比平时费劲些……",
        "rule_id": None,
    },
}

B_NEW_CHAT = {
    "session_id": "s1",
    "turn_id": "t3",
    "result": {
        "text": "今天菜市场青菜挺便宜。",
        "health_signal": {"type": "无", "detail": "", "severity": "轻微"},
        "guardrail_triggered": False,
        "response_mode": "normal_chat",
        "response": "",
        "rule_id": None,
    },
}


def test_b_new_schema_medication_blocked():
    r = normalize_b_output(B_NEW_MEDICATION, elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "medication"
    assert r.severity.value == "high"          # 需留意 -> high
    assert r.safety_blocked is True            # guardrail_triggered
    assert r.rule_id == "MEDICATION_CHANGE_REQUEST"
    assert r.response_mode.value == "medication_safety"


def test_b_new_schema_symptom():
    r = normalize_b_output(B_NEW_SYMPTOM, elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "symptom"    # 身体不适 -> symptom
    assert r.severity.value == "moderate"      # 中等 -> moderate
    assert r.response_mode.value == "health_care"


def test_b_new_schema_no_signal():
    r = normalize_b_output(B_NEW_CHAT, elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "none"       # 无 -> none
    assert r.severity.value == "low"           # 轻微 -> low
    assert r.response_mode.value == "passthrough"  # normal_chat -> passthrough


def test_b_old_schema_still_works():
    """task2 的实测形态（英文枚举、扁平结构）必须继续可用。"""
    r = normalize_b_output(B_T2, elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "symptom"
    assert r.severity.value == "moderate"
    assert r.response_mode.value == "health_care"
    assert r.semantic_backend == "mock"


def test_both_schemas_agree_on_the_same_event():
    """同一件事的两种写法，归一化后必须一致（除 backend 字段新版没有）。"""
    old = normalize_b_output(B_T2, elder_id="e1", occurred_at=STAMP)
    new = normalize_b_output(B_NEW_SYMPTOM, elder_id="e1", occurred_at=STAMP)
    assert old.signal_type is new.signal_type
    assert old.severity is new.severity
    assert old.response_mode is new.response_mode


def test_unknown_enum_never_crashes():
    r = normalize_b_output(
        {"session_id": "s", "turn_id": "t",
         "health_signal": {"type": "乱码", "detail": "x", "severity": "未知"},
         "safety": {"blocked": False}, "response": {"mode": "???"},
         "metadata": {}},
        elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "none"
    assert r.severity.value == "low"
    assert r.response_mode.value == "passthrough"


def test_ignored_output_is_detected():
    assert b_output_is_ignored({"status": "ignored",
                                "reason": "partial_or_empty"}) is True
    assert b_output_is_ignored(B_T2) is False
    assert b_output_is_ignored(B_NEW_MEDICATION) is False


def test_to_digest_record_shape_accepted_but_loses_blocked():
    """to_digest_record 的输出能吃，但 guardrail_triggered 必然丢失 ——

    这是为什么 D 不该把它当唯一数据源：P0 的一个源头会断。
    """
    r = normalize_b_output(
        {"timestamp": "2026-09-22T10:30:00", "type": "用药",
         "detail": "询问增加降压药剂量", "severity": "需留意"},
        elder_id="e1")
    assert r.signal_type.value == "medication"
    assert r.severity.value == "high"
    assert r.safety_blocked is False  # 丢了
    assert r.occurred_at == datetime(2026, 9, 22, 10, 30, 0)


def test_to_digest_record_timestamp_parsed():
    r = normalize_b_digest_record(
        {"timestamp": "2026-09-22T10:30:00", "type": "睡眠",
         "detail": "入睡困难", "severity": "中等"}, elder_id="e1")
    assert r.signal_type.value == "sleep"
    assert r.local_date.isoformat() == "2026-09-22"


# --------------------------------------------------------------------------
# B 2026-09-28 交付版：两套 schema 并存
# --------------------------------------------------------------------------

# 新版（schemas/TriageResponse.schema.json）：中文枚举 + result 嵌套
B_NEW_MEDICATION = {
    "session_id": "s1",
    "turn_id": "t1",
    "result": {
        "text": "我降压药今天能不能吃两颗？",
        "health_signal": {"type": "用药", "detail": "涉及具体用药调整询问",
                          "severity": "需留意"},
        "guardrail_triggered": True,
        "response_mode": "medication_safety",
        "response": "这个涉及具体的用药剂量，我不能替您决定……",
        "rule_id": "MEDICATION_CHANGE_REQUEST",
    },
}

B_NEW_SYMPTOM = {
    "session_id": "s1",
    "turn_id": "t2",
    "result": {
        "text": "今天早上起来腿沉得很",
        "health_signal": {"type": "身体不适", "detail": "下肢沉重/乏力",
                          "severity": "中等"},
        "guardrail_triggered": False,
        "response_mode": "health_care",
        "response": "听起来您今天走路比平时费劲些……",
        "rule_id": None,
    },
}

B_NEW_CHAT = {
    "session_id": "s1",
    "turn_id": "t3",
    "result": {
        "text": "今天菜市场青菜挺便宜。",
        "health_signal": {"type": "无", "detail": "", "severity": "轻微"},
        "guardrail_triggered": False,
        "response_mode": "normal_chat",
        "response": "",
        "rule_id": None,
    },
}


def test_b_new_schema_medication_blocked():
    r = normalize_b_output(B_NEW_MEDICATION, elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "medication"
    assert r.severity.value == "high"          # 需留意 -> high
    assert r.safety_blocked is True            # guardrail_triggered
    assert r.rule_id == "MEDICATION_CHANGE_REQUEST"
    assert r.response_mode.value == "medication_safety"


def test_b_new_schema_symptom():
    r = normalize_b_output(B_NEW_SYMPTOM, elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "symptom"    # 身体不适 -> symptom
    assert r.severity.value == "moderate"      # 中等 -> moderate
    assert r.response_mode.value == "health_care"


def test_b_new_schema_no_signal():
    r = normalize_b_output(B_NEW_CHAT, elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "none"       # 无 -> none
    assert r.severity.value == "low"           # 轻微 -> low
    assert r.response_mode.value == "passthrough"  # normal_chat -> passthrough


def test_b_old_schema_still_works():
    """task2 的实测形态（英文枚举、扁平结构）必须继续可用。"""
    r = normalize_b_output(B_T2, elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "symptom"
    assert r.severity.value == "moderate"
    assert r.response_mode.value == "health_care"
    assert r.semantic_backend == "mock"


def test_both_schemas_agree_on_the_same_event():
    """同一件事的两种写法，归一化后必须一致（除 backend 字段新版没有）。"""
    old = normalize_b_output(B_T2, elder_id="e1", occurred_at=STAMP)
    new = normalize_b_output(B_NEW_SYMPTOM, elder_id="e1", occurred_at=STAMP)
    assert old.signal_type is new.signal_type
    assert old.severity is new.severity
    assert old.response_mode is new.response_mode


def test_unknown_enum_never_crashes():
    r = normalize_b_output(
        {"session_id": "s", "turn_id": "t",
         "health_signal": {"type": "乱码", "detail": "x", "severity": "未知"},
         "safety": {"blocked": False}, "response": {"mode": "???"},
         "metadata": {}},
        elder_id="e1", occurred_at=STAMP)
    assert r.signal_type.value == "none"
    assert r.severity.value == "low"
    assert r.response_mode.value == "passthrough"


def test_ignored_output_is_detected():
    assert b_output_is_ignored({"status": "ignored",
                                "reason": "partial_or_empty"}) is True
    assert b_output_is_ignored(B_T2) is False
    assert b_output_is_ignored(B_NEW_MEDICATION) is False


def test_to_digest_record_shape_accepted_but_loses_blocked():
    """to_digest_record 的输出能吃，但 guardrail_triggered 必然丢失 ——

    这是为什么 D 不该把它当唯一数据源：P0 的一个源头会断。
    """
    r = normalize_b_output(
        {"timestamp": "2026-09-22T10:30:00", "type": "用药",
         "detail": "询问增加降压药剂量", "severity": "需留意"},
        elder_id="e1")
    assert r.signal_type.value == "medication"
    assert r.severity.value == "high"
    assert r.safety_blocked is False  # 丢了
    assert r.occurred_at == datetime(2026, 9, 22, 10, 30, 0)


def test_to_digest_record_timestamp_parsed():
    r = normalize_b_digest_record(
        {"timestamp": "2026-09-22T10:30:00", "type": "睡眠",
         "detail": "入睡困难", "severity": "中等"}, elder_id="e1")
    assert r.signal_type.value == "sleep"
    assert r.local_date.isoformat() == "2026-09-22"


# --------------------------------------------------------------------------
# 降级标志：B 一旦加上 degraded，D 零改动生效（当前 B 还没有，走启发式推断）
# --------------------------------------------------------------------------

def _new_chat(**over) -> dict:
    """B 新版「闲聊、无信号」报文，可覆盖任意层加 degraded。"""
    base = {"session_id": "s1", "turn_id": "t9",
            "result": {"text": "今天菜市场青菜挺便宜。",
                       "health_signal": {"type": "无", "detail": "", "severity": "轻微"},
                       "guardrail_triggered": False,
                       "response_mode": "normal_chat", "response": "", "rule_id": None}}
    base.update(over)
    return base


def test_explicit_degraded_flag_on_result():
    r = normalize_b_output(_new_chat(result={
        "text": "", "health_signal": {"type": "无", "detail": "", "severity": "轻微"},
        "guardrail_triggered": False, "response_mode": "normal_chat",
        "response": "", "rule_id": None, "degraded": True}),
        elder_id="e1", occurred_at=STAMP)
    assert r.degraded is True
    assert r.looks_degraded() is True
    assert r.acquisition().value == "not_acquired"   # 绝不能说成「没事」


def test_explicit_degraded_false_overrides_heuristics():
    """B 明说没降级，就不要再靠 qwen_called 瞎猜。"""
    r = normalize_b_output(
        {"session_id": "s", "turn_id": "t",
         "health_signal": {"type": "无", "detail": "", "severity": "轻微"},
         "safety": {"blocked": False}, "response": {"mode": "passthrough"},
         "metadata": {"semantic_backend": "qwen", "qwen_called": True},
         "degraded": False},
        elder_id="e1", occurred_at=STAMP)
    assert r.degraded is False
    assert r.looks_degraded() is False
    assert r.acquisition().value == "no_signal"


def test_degraded_absent_falls_back_to_heuristics():
    r = normalize_b_output(_new_chat(), elder_id="e1", occurred_at=STAMP)
    assert r.degraded is None
    assert r.acquisition().value == "not_acquired"  # 无 metadata、无信号 -> 保守


def test_chinese_labels_match_b_vocabulary():
    """D 给家属看的措辞必须与 B 的中文枚举口径一致（最终对外以中文为准）。"""
    from family_digest.dedup import SIGNAL_LABEL
    from family_digest.models import SignalType
    assert SIGNAL_LABEL[SignalType.MOBILITY] == "行动能力"
    assert SIGNAL_LABEL[SignalType.OTHER] == "其他体征"
    assert SIGNAL_LABEL[SignalType.SYMPTOM] == "身体不适"
