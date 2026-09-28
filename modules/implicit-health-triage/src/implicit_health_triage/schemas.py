"""Data contracts for the ``implicit-health-triage`` skill.

The three core extraction fields required by the task image are preserved
verbatim: ``type`` / ``detail`` / ``severity``.

Every field carries a ``description`` and an ``example`` on purpose: these flow
straight into the OpenAPI document, so a neighbouring module can integrate by
reading ``http://<host>:8080/docs`` (or ``/openapi.json``) alone, without
opening this file.
"""

from __future__ import annotations

import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - Python 3.10 fallback
    from enum import Enum

    class StrEnum(str, Enum):
        """Minimal backport of :class:`enum.StrEnum` for Python 3.10."""

        def __str__(self) -> str:  # pragma: no cover - trivial
            return str(self.value)


class HealthType(StrEnum):
    """Category of an extracted health signal. **值本身即中文。**

    **This is a shallow hierarchy, not a flat list.** ``身体不适`` is the generic
    bodily-complaint type; ``疼痛``, ``睡眠``, ``食欲`` and ``行动能力`` are its more
    specific subtypes. Producers pick the most specific type that fits, falling
    back to ``身体不适`` only when none does.

    ::

        无                        无健康信息
        身体不适                  身体不适，类别不明确        <- 泛化类
          ├─ 疼痛                 明确疼痛
          ├─ 睡眠                 睡眠
          ├─ 食欲                 食欲
          └─ 行动能力             行走、平衡、跌倒
        用药                      用药相关
        其他体征                  血压、血糖等其他体征

    Why this matters to a consumer
    ------------------------------
    Asking "was anything physically bothering her?" by testing
    ``type == "身体不适"`` **misses every specific case**. Use the helpers below,
    or the equivalent sets, instead:

    ===========================================  ==========================================
    Question                                     Test
    ===========================================  ==========================================
    Any health content?                          ``type != "无"``
    Any bodily complaint?                        ``is_bodily_symptom(type)``
    Pain specifically?                           ``type in PAIN_TYPES``
    Pain, tolerating generic producers?          ``type in PAIN_OR_GENERIC``
    Medication-related?                          ``type == "用药"``
    ===========================================  ==========================================

    The task image only required the generic type; the subtypes are an
    extension. A producer that only emits ``身体不适`` is still valid, which is
    exactly why ``PAIN_OR_GENERIC`` exists.

    值本身已是中文，因此不再需要单独的展示映射表——展示层直接用这些值。
    """

    NONE = "无"
    SYMPTOM = "身体不适"
    SLEEP = "睡眠"
    PAIN = "疼痛"
    MEDICATION = "用药"
    MOBILITY = "行动能力"
    APPETITE = "食欲"
    OTHER = "其他体征"


#: Every type that represents a bodily complaint, generic or specific.
#: Deliberately excludes ``无``, ``用药`` and ``其他体征``.
SYMPTOM_FAMILY: frozenset[str] = frozenset(
    {
        HealthType.SYMPTOM.value,
        HealthType.PAIN.value,
        HealthType.SLEEP.value,
        HealthType.APPETITE.value,
        HealthType.MOBILITY.value,
    }
)

#: Pain, precisely. Use this when the producer reliably emits the subtype.
PAIN_TYPES: frozenset[str] = frozenset({HealthType.PAIN.value})

#: Pain, tolerating a producer that only emits the generic type — e.g. a module
#: or an older record that predates the subtype split.
PAIN_OR_GENERIC: frozenset[str] = frozenset(
    {HealthType.PAIN.value, HealthType.SYMPTOM.value}
)


def is_bodily_symptom(health_type: HealthType | str) -> bool:
    """True for any bodily complaint, including the fine-grained subtypes.

    ``is_bodily_symptom("pain")`` is ``True``. Testing ``== "symptom"`` is not
    equivalent and will miss it.
    """

    return str(health_type) in SYMPTOM_FAMILY


def is_pain(health_type: HealthType | str, *, include_generic: bool = False) -> bool:
    """True for a pain signal.

    ``include_generic=True`` also accepts the generic ``symptom``, for data that
    may come from a producer which does not emit subtypes.
    """

    return str(health_type) in (PAIN_OR_GENERIC if include_generic else PAIN_TYPES)


class Severity(StrEnum):
    """Internal structured tag. NOT a medical diagnosis or clinical conclusion.

    **值本身即中文**，因为最终是中文展示。三个值刻意避开临床分级措辞
    （「轻 / 中 / 重」这类一出现在家属界面上就会被读成医学结论）：

    ``轻微``
        轻微，或已按计划处理（例如已经按时服药）
    ``中等``
        明显影响日常活动，或持续存在
    ``需留意``
        剧烈、突发，或属于需要优先关注的信号
    """

    LOW = "轻微"
    MODERATE = "中等"
    HIGH = "需留意"


class ResponseMode(StrEnum):
    """Which branch of the skill produced the response.

    - ``normal_chat``       no health content; ``response`` is an empty string
    - ``health_care``       health signal found; ``response`` is a caring reply
    - ``medication_safety`` medication request blocked; ``response`` is a fixed
                            safety sentence that must be spoken verbatim

    ``response_mode`` 是**程序分支标识**，不是展示内容，因此保持英文——
    它只用于 ``if`` 判断，从不出现在老人或家属看到的界面上。
    """

    NORMAL_CHAT = "normal_chat"
    HEALTH_CARE = "health_care"
    MEDICATION_SAFETY = "medication_safety"


class HealthSignal(BaseModel):
    """The exact structure specified by the task image.

    ``severity`` is only an internal structured label used for downstream
    routing. It never represents a clinical judgement.
    """

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        json_schema_extra={
            "example": {
                "type": "身体不适",
                "detail": "下肢沉重/乏力",
                "severity": "中等",
            }
        },
    )

    type: HealthType = Field(
        description=(
            "健康信号类别。none=无健康信息；symptom=其他身体不适；sleep=睡眠；"
            "pain=明确疼痛；medication=用药；mobility=行动能力；appetite=食欲；other=其他体征。"
        ),
        examples=["symptom"],
    )
    detail: str = Field(
        default="",
        max_length=256,
        description="简短中文概括，来自用户实际表达。type=无 时为空串。",
        examples=["下肢沉重/乏力"],
    )
    severity: Severity = Field(
        default=Severity.LOW,
        description=(
            "内部排序标签，**不是医学诊断结论**。low=轻微或已按计划处理；"
            "moderate=明显影响日常活动或持续存在；high=剧烈、突发或危险信号。"
        ),
        examples=["moderate"],
    )


def none_signal() -> HealthSignal:
    """Return a fresh ``type=无`` signal (avoids a shared mutable default)."""

    return HealthSignal(type=HealthType.NONE, detail="", severity=Severity.LOW)


def medication_guard_signal(reason: str = "涉及具体用药调整询问") -> HealthSignal:
    """Signal attached to a request the medication guardrail blocked."""

    return HealthSignal(type=HealthType.MEDICATION, detail=reason, severity=Severity.HIGH)


class TriageResult(BaseModel):
    """Unified skill output (task guide section 7.2)."""

    model_config = ConfigDict(
        use_enum_values=True,
        json_schema_extra={
            "example": {
                "text": "今天早上起来腿沉得很，买菜走两步就得歇着。",
                "health_signal": {
                    "type": "身体不适",
                    "detail": "下肢沉重/乏力",
                    "severity": "中等",
                },
                "guardrail_triggered": False,
                "response_mode": "health_care",
                "response": "听起来您今天走路比平时费劲些。您今天平时该吃的药都按原来的安排吃了吗？",
                "rule_id": None,
            }
        },
    )

    text: str = Field(
        description="原样回显的输入文本。",
        examples=["今天早上起来腿沉得很，买菜走两步就得歇着。"],
    )
    health_signal: HealthSignal = Field(description="结构化健康信号（核心三字段）。")
    guardrail_triggered: bool = Field(
        description="是否被用药安全围栏拦截。为 true 时 response 是固定安全话术。",
        examples=[False],
    )
    response_mode: ResponseMode = Field(
        description="走了哪条分支，决定调用方如何处理 response。",
        examples=["health_care"],
    )
    response: str = Field(
        description=(
            "**可直接送 TTS 播报**的文本。"
            "normal_chat 时为空串（闲聊交回主对话系统，空串不要播报）；"
            "medication_safety 时为固定常量，必须原样播报，禁止其他模型补话。"
        ),
        examples=["听起来您今天走路比平时费劲些。您今天平时该吃的药都按原来的安排吃了吗？"],
    )
    rule_id: str | None = Field(
        default=None,
        description="仅拦截时为 MEDICATION_CHANGE_REQUEST，其余情况为 null。",
        examples=["MEDICATION_CHANGE_REQUEST"],
    )


class ConversationTurn(BaseModel):
    """Upstream contract produced by ``elderly-voice-duplex`` (section 32).

    Use this envelope when calling the skill from Python. HTTP callers use
    :class:`TriageRequest` instead, which carries the same fields minus
    ``speaker`` / ``timestamp``.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": "s001",
                "turn_id": "t018",
                "speaker": "elder",
                "text": "今天早上起来腿沉得很，买菜走两步就得歇着。",
                "timestamp": "2026-09-22T10:30:00",
                "is_final": True,
            }
        }
    )

    session_id: str = Field(
        description="会话 ID，**有语义**：服务端按它维护最近 3 轮窗口以识别承前文追问。同一次通话必须同值。",
        examples=["s001"],
    )
    turn_id: str = Field(description="通话内唯一的轮次 ID，用于日志追踪。", examples=["t018"])
    speaker: Literal["elder", "bot"] = Field(
        default="elder", description="说话人。本模块只处理 elder。", examples=["elder"]
    )
    text: str = Field(
        description="老人这一轮的**成句文本**。本模块不处理音频。",
        examples=["今天早上起来腿沉得很，买菜走两步就得歇着。"],
    )
    timestamp: str | None = Field(
        default=None, description="ISO-8601 时间戳；本模块只透传。", examples=["2026-09-22T10:30:00"]
    )
    is_final: bool = Field(
        default=True,
        description="是否为成句。**false 不会被处理**，直接返回 ignored_partial。",
        examples=[True],
    )


class HealthSignalRecord(BaseModel):
    """What ``family-digest-sync`` consumes (section 33)."""

    model_config = ConfigDict(
        use_enum_values=True,
        json_schema_extra={
            "example": {
                "timestamp": "2026-09-22T10:30:00",
                "type": "身体不适",
                "detail": "下肢沉重/乏力",
                "severity": "中等",
            }
        },
    )

    timestamp: str = Field(
        description="信号产生时间。不传则由 to_digest_record 取当前 UTC 时间。",
        examples=["2026-09-22T10:30:00"],
    )
    type: HealthType = Field(description="同 HealthSignal.type。", examples=["symptom"])
    detail: str = Field(description="同 HealthSignal.detail。", examples=["下肢沉重/乏力"])
    severity: Severity = Field(
        description="同 HealthSignal.severity。仅作排序输入，**P0/P1/P2 分级由 digest 自行决定**。",
        examples=["moderate"],
    )


class TriageRequest(BaseModel):
    """HTTP request body for ``POST /v1/triage`` (section 30)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": "s1",
                "turn_id": "t1",
                "text": "我降压药今天能不能吃两颗？",
                "is_final": True,
            }
        }
    )

    session_id: str = Field(
        description="会话 ID，**有语义**：服务端按它维护最近 3 轮窗口以识别承前文追问。不同老人不要混用。",
        examples=["s1"],
    )
    turn_id: str = Field(description="通话内唯一的轮次 ID。", examples=["t1"])
    text: str = Field(
        description="老人这一轮的成句文本。空字符串可以传，会返回 normal_chat。",
        examples=["我降压药今天能不能吃两颗？"],
    )
    is_final: bool = Field(
        default=True,
        description="是否为成句。**false 时直接返回 {\"status\":\"ignored_partial\"}**，不抽取也不拦截。",
        examples=[True],
    )


class TriageResponse(BaseModel):
    """HTTP response body for ``POST /v1/triage`` (section 29.2)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": "s1",
                "turn_id": "t1",
                "result": {
                    "text": "我降压药今天能不能吃两颗？",
                    "health_signal": {
                        "type": "用药",
                        "detail": "涉及具体用药调整询问",
                        "severity": "需留意",
                    },
                    "guardrail_triggered": True,
                    "response_mode": "medication_safety",
                    "response": "这个涉及具体的用药剂量，我不能替您决定增加、减少或停止服药。请先按照医生给您的处方或药品说明来服用，如果不确定，最好联系医生或药师确认。",
                    "rule_id": "MEDICATION_CHANGE_REQUEST",
                },
            }
        }
    )

    session_id: str = Field(description="原样回显请求中的 session_id。", examples=["s1"])
    turn_id: str = Field(description="原样回显请求中的 turn_id。", examples=["t1"])
    result: TriageResult = Field(description="核心结果。见 TriageResult。")


class PartialIgnoredResponse(BaseModel):
    """Returned when ``is_final=false`` (section 31).

    Distinguish this from :class:`TriageResponse` by the presence of the
    ``result`` field: this shape has ``status`` instead.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"session_id": "s1", "turn_id": "t1", "status": "ignored_partial"}
        }
    )

    session_id: str = Field(description="原样回显请求中的 session_id。", examples=["s1"])
    turn_id: str = Field(description="原样回显请求中的 turn_id。", examples=["t1"])
    status: Literal["ignored_partial"] = Field(
        default="ignored_partial",
        description="固定值。表示这是 ASR 未成句的片段，未被处理。",
        examples=["ignored_partial"],
    )
