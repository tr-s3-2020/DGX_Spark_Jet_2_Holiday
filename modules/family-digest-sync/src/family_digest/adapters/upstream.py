"""上游原始报文 -> D 内部模型的归一化层。

存在理由：A/B/C 各自的输出格式和 D 的内部契约并不一致，且都不提供 D 需要的
主键。这个模块负责把「队友真实吐出来的东西」翻译成 D 能吃的记录，让主控不必
在每个调用点手写字段搬运。

D 内部一律用**英文规范形**（none/symptom/...、low/moderate/high），
由这一层把上游的中文值翻译过来。这样上游枚举再变，D 的核心逻辑不用动。

B 当前（2026-09-28 交付版）有**两套 schema 并存**，必须都支持：

==========  ==============  ================================  ==================
入口        type / severity  结构                              阻断字段
==========  ==============  ================================  ==================
task2 实测  英文 none/...    扁平 health_signal/safety/        safety.blocked
             low/moderate    response/metadata
顶层 schema  中文 无/身体不适  {result:{...}} 嵌套               result.
            轻微/中等/需留意                                   guardrail_triggered
==========  ==============  ================================  ==================

另外 B 的 ``to_digest_record`` 会**丢掉两个 D 必需的信息**（见下方文档），
所以除非明确要求，D 应直接消费 TriageOutput/TriageResponse 原始报文。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from ..models import (
    ChronicleInput,
    HealthSignalRecord,
    SafetyEventRecord,
    SafetyLevel,
)

# --------------------------------------------------------------------------
# 中文枚举 -> D 内部规范形（英文）
# --------------------------------------------------------------------------

TYPE_ZH = {
    "无": "none",
    "身体不适": "symptom",
    "睡眠": "sleep",
    "疼痛": "pain",
    "用药": "medication",
    "行动能力": "mobility",
    "食欲": "appetite",
    "其他体征": "other",
}

SEVERITY_ZH = {"轻微": "low", "中等": "moderate", "需留意": "high"}

MODE_ZH = {
    "normal_chat": "passthrough",  # 新版把 passthrough 改名了
    "health_care": "health_care",
    "medication_safety": "medication_safety",
}

VALID_TYPES = set(TYPE_ZH.values())
VALID_SEVERITIES = set(SEVERITY_ZH.values())
VALID_MODES = set(MODE_ZH.values())


def _canon(value: Any, table: Dict[str, str], valid: set, default: str) -> str:
    """中文/英文都认，统一翻成规范形；认不出的给默认值，绝不崩。"""
    v = "" if value is None else str(value)
    if v in valid:
        return v
    return table.get(v, default)


def canon_type(v: Any) -> str:
    return _canon(v, TYPE_ZH, VALID_TYPES, "none")


def canon_severity(v: Any) -> str:
    return _canon(v, SEVERITY_ZH, VALID_SEVERITIES, "low")


def canon_mode(v: Any) -> str:
    return _canon(v, MODE_ZH, VALID_MODES, "passthrough")


def make_event_id(prefix: str, *parts: Any) -> str:
    """确定性主键：同样输入永远得到同样 event_id，重复投递才能被去重命中。"""
    raw = "|".join("" if p is None else str(p) for p in parts)
    if not raw.strip("|"):
        raw = "empty"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def _pick(d: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


# --------------------------------------------------------------------------
# B: implicit-health-triage
# --------------------------------------------------------------------------


def normalize_b_output(
    raw: Dict[str, Any],
    *,
    elder_id: str,
    occurred_at: Optional[datetime] = None,
) -> HealthSignalRecord:
    """把 B 的任一形态输出转成 HealthSignalRecord。

    自动识别三种形态：
    1. task2 实测（CLI / HTTP 当前实际吐的）::

        {"session_id","turn_id","health_signal":{"type","detail","severity"},
         "safety":{"blocked","rule_id"},
         "response":{"mode","text"},
         "metadata":{"semantic_backend","qwen_called","latency_ms"}}

    2. 顶层新版 schema（schemas/TriageResponse.schema.json）::

        {"session_id","turn_id","result":{"text","health_signal",
         "guardrail_triggered","response_mode","response","rule_id"}}

    3. ``to_digest_record`` 的输出 ``{"timestamp","type","detail","severity"}``
       —— 可接受但不推荐，见 :func:`normalize_b_digest_record` 的说明。

    B 样例里用 ``expected_output_subset`` 包裹，本函数会自动剥掉。
    """
    if "expected_output_subset" in raw:
        raw = raw["expected_output_subset"]

    # 形态 3：to_digest_record
    if "result" not in raw and "health_signal" not in raw and "timestamp" in raw:
        return normalize_b_digest_record(raw, elder_id=elder_id)

    session_id = str(_pick(raw, "session_id", default=""))
    turn_id = str(_pick(raw, "turn_id", default=""))

    # 形态 2：result 嵌套
    result = raw.get("result") if isinstance(raw.get("result"), dict) else None
    if result is not None:
        signal = result.get("health_signal") or {}
        mode = _pick(result, "response_mode", default="passthrough")
        blocked = bool(result.get("guardrail_triggered", False))
        rule_id = result.get("rule_id")
        # 新版 schema 没有 metadata，但降级标志可能直接挂在 result 或外层
        meta = result.get("metadata") or raw.get("metadata") or {}
    else:
        signal = raw.get("health_signal") or {}
        response = raw.get("response") or {}
        mode = _pick(response, "mode", default="passthrough")
        blocked = bool((raw.get("safety") or {}).get("blocked", False))
        rule_id = (raw.get("safety") or {}).get("rule_id")
        meta = raw.get("metadata") or {}

    backend = str(_pick(meta, "semantic_backend", default="none"))
    if backend not in ("none", "mock", "qwen"):
        backend = "none"

    # 降级标志：B 一旦加上，下面四个位置任一命中即可，D 零改动生效
    degraded_raw = _pick(raw, "degraded", default=None)
    if degraded_raw is None:
        degraded_raw = _pick(result or {}, "degraded", default=None)
    if degraded_raw is None:
        degraded_raw = _pick(meta, "degraded", default=None)
    if degraded_raw is None and meta.get("fallback") is True:
        degraded_raw = True
    degraded = None if degraded_raw is None else bool(degraded_raw)

    stamp = occurred_at or datetime.now()

    return HealthSignalRecord(
        event_id=make_event_id("b", elder_id, session_id, turn_id,
                               signal.get("type"), signal.get("detail")),
        elder_id=elder_id,
        occurred_at=stamp,
        session_id=session_id,
        turn_id=turn_id,
        signal_type=canon_type(signal.get("type")),
        detail=str(_pick(signal, "detail", default="")),
        severity=canon_severity(_pick(signal, "severity", default="low")),
        safety_blocked=blocked,
        rule_id=rule_id,
        response_mode=canon_mode(mode),
        semantic_backend=backend,
        qwen_called=bool(meta.get("qwen_called", False)),
        degraded=degraded,
    )


def normalize_b_digest_record(
    raw: Dict[str, Any],
    *,
    elder_id: str,
) -> HealthSignalRecord:
    """接受 B ``to_digest_record`` 的输出 ``{timestamp, type, detail, severity}``。

    ⚠️ **不要用这个作为唯一数据源** —— ``to_digest_record`` 会丢掉两样 D
    必需的东西：

    1. ``guardrail_triggered``（用药安全阻断）：这是 D 的 P0 源头之一，
       走 ``to_digest_record`` 会丢，P0 就漏了。
    2. ``type=无`` 的轮次直接返回 ``None``：D 因此看不到「今天聊过但没信号」
       的记录，也无法用 ``metadata`` 判断 B 是否降级。

    要用就用原始报文调 :func:`normalize_b_output`。
    """
    ts = raw.get("timestamp")
    if isinstance(ts, str) and ts:
        try:
            stamp = datetime.fromisoformat(ts)
        except ValueError:
            stamp = datetime.now()
    else:
        stamp = datetime.now()

    return HealthSignalRecord(
        event_id=make_event_id("b", elder_id, ts, raw.get("type"), raw.get("detail")),
        elder_id=elder_id,
        occurred_at=stamp,
        signal_type=canon_type(raw.get("type")),
        detail=str(raw.get("detail") or ""),
        severity=canon_severity(raw.get("severity")),
        safety_blocked=False,  # to_digest_record 不传，只能留空
        semantic_backend="none",
        qwen_called=False,
    )


def normalize_b_batch(
    raws: Iterable[Dict[str, Any]],
    *,
    elder_id: str,
    occurred_at: Optional[datetime] = None,
) -> List[HealthSignalRecord]:
    return [normalize_b_output(r, elder_id=elder_id, occurred_at=occurred_at)
            for r in raws]


def b_output_is_ignored(raw: Dict[str, Any]) -> bool:
    """B 返回 ignored / 出错的轮次不算数据，不能喂给 D。"""
    if not isinstance(raw, dict):
        return True
    if raw.get("status") == "ignored":
        return True
    if "result" not in raw and "health_signal" not in raw and "timestamp" not in raw:
        return True
    return False


# --------------------------------------------------------------------------
# A: elderly-voice-duplex
# --------------------------------------------------------------------------


def normalize_a_safety(
    raw: Dict[str, Any],
    *,
    elder_id: str,
    occurred_at: Optional[datetime] = None,
) -> SafetyEventRecord:
    """把 A 的 WebSocket 安全分级消息转成 SafetyEventRecord。

    raw 形如 ``{"type": "final", "safety": "P0", "text": "..."}``。
    A 不传 event_id，这里按 (session, turn, level) 确定性生成。
    """
    safety = raw.get("safety") or raw.get("safety_level") or "none"
    level = str(safety).upper()
    if level not in {"P0", "P1", "NONE", ""}:
        level = "none"
    session_id = str(_pick(raw, "session_id", default=""))
    turn_id = str(_pick(raw, "turn_id", default=""))
    stamp = occurred_at or datetime.now()

    return SafetyEventRecord(
        event_id=make_event_id("a", elder_id, session_id, turn_id, level),
        elder_id=elder_id,
        occurred_at=stamp,
        safety_level=SafetyLevel(level or "none"),
        text=str(_pick(raw, "text", default="")),
    )


# --------------------------------------------------------------------------
# C: life-memoir-retriever
# --------------------------------------------------------------------------


def normalize_c_chronicle(raw: Dict[str, Any]) -> ChronicleInput:
    """把 C 的 get_chronicle 响应转成 ChronicleInput。

    同时接受两种形态：
    - C 的真实 envelope：``{status, data:{state, view_version, content:{items,
      narrative}, warnings}}``
    - 已经展平过的：``{state, view_version, items, narrative, warnings}``

    C 的 ``status=degraded`` 与 ``CHRONICLE_NOT_READY`` 都会被转成 warnings 带进
    卡片，绝不静默当成「没有回忆」。
    """
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    content = data.get("content") if isinstance(data.get("content"), dict) else data

    warnings: List[str] = list(data.get("warnings") or raw.get("warnings") or [])
    status = str(raw.get("status") or "")
    if status == "degraded":
        warnings.append("回忆模块本次为部分可用（degraded），不代表没有回忆内容。")
    if data.get("state") == "not_ready":
        warnings.append("回忆年谱尚未生成（not_ready），本次未附带回忆内容。")

    return ChronicleInput(
        state=data.get("state") or "not_ready",
        view_version=data.get("view_version"),
        items=list(content.get("items") or []),
        narrative=str(content.get("narrative") or ""),
        warnings=warnings,
    )
