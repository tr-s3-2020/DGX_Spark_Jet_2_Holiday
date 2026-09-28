"""family-digest-sync：家属摘要同步 Skill 的数据契约 v0.1。

上游枚举一律沿用 A/B/C 的取值，D 不另造一套；
只有 `Acquisition` 和 `Tier` 是 D 新增的，用于补上 B「降级无标志」的坑。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# 上游枚举（来自 B implicit-health-triage / A elderly-voice-duplex）
# --------------------------------------------------------------------------


class SignalType(str, Enum):
    """B 的 health_signal.type，取值与 B 文档一致。"""

    NONE = "none"
    SYMPTOM = "symptom"
    SLEEP = "sleep"
    PAIN = "pain"
    MEDICATION = "medication"
    MOBILITY = "mobility"
    APPETITE = "appetite"
    OTHER = "other"


class Severity(str, Enum):
    """B 的 health_signal.severity。注意：这是内部标签，不是 P0/P1/P2。"""

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


SEVERITY_ORDER = {Severity.LOW: 1, Severity.MODERATE: 2, Severity.HIGH: 3}

# B 的 type 是**浅层次结构**，不是扁平列表（B 的 HealthType 文档明确警告）：
#   无 → 身体不适（泛化）→ 疼痛 / 睡眠 / 食欲 / 行动能力（子类）
#   用药 / 其他体征（独立）
# 所以「是不是同一类问题」必须按组判断：一个只吐「身体不适」的 producer 和
# 一个吐「疼痛」的 producer 说的是同一件事，精确相等会把连续趋势误判为中断。
BODILY_TYPES = frozenset({"symptom", "pain", "sleep", "appetite", "mobility"})


def signal_group(signal_type: "SignalType | str") -> str:
    """把 B 的 type 归到趋势分组：bodily / medication / other / none。"""
    v = signal_type.value if isinstance(signal_type, SignalType) else str(signal_type)
    if v == "none":
        return "none"
    if v == "medication":
        return "medication"
    if v == "other":
        return "other"
    return "bodily" if v in BODILY_TYPES else "other"


class SafetyLevel(str, Enum):
    """A 抛出的 safety_level。"""

    P0 = "P0"
    P1 = "P1"
    NONE = "none"


class ResponseMode(str, Enum):
    PASSTHROUGH = "passthrough"
    HEALTH_CARE = "health_care"
    MEDICATION_SAFETY = "medication_safety"


# --------------------------------------------------------------------------
# D 新增
# --------------------------------------------------------------------------


class Acquisition(str, Enum):
    """本次健康信息到底拿到没有。

    B 协议里模型失败会降级为 none/空 detail/low，且**没有独立降级标志**。
    D 因此必须自己区分「真没事」和「没拿到」，否则会把降级写成「状况良好」。
    """

    ACQUIRED = "acquired"        # 拿到了可用的健康观察
    NO_SIGNAL = "no_signal"      # 正常跑完，本次未识别出健康信号（≠ 健康正常）
    NOT_ACQUIRED = "not_acquired"  # 未跑/被拦截/未获取，禁止写成「没事」


class Tier(str, Enum):
    """D 的通知档位。"""

    P0 = "P0"  # 立即推送
    P1 = "P1"  # 当日高亮
    P2 = "P2"  # 日报


class ConsentScope(str, Enum):
    HEALTH_SUMMARY = "health_summary"
    MEDICATION_SAFETY = "medication_safety"
    CHRONICLE = "chronicle"
    SOURCE_TEXT = "source_text"


class CardStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    SCHEDULED = "scheduled"
    SENT = "sent"
    RETRYING = "retrying"
    DEAD_LETTER = "dead_letter"
    ACKNOWLEDGED = "acknowledged"


class DispatchStatus(str, Enum):
    SENT = "sent"
    RETRYING = "retrying"
    DEAD_LETTER = "dead_letter"


# --------------------------------------------------------------------------
# 输入记录
# --------------------------------------------------------------------------


class HealthSignalRecord(BaseModel):
    """一条来自 B 的 TriageOutput（只取 D 需要的字段）。"""

    event_id: str
    elder_id: str
    occurred_at: datetime
    session_id: str = ""
    turn_id: str = ""
    signal_type: SignalType = SignalType.NONE
    detail: str = ""
    severity: Severity = Severity.LOW
    safety_blocked: bool = False
    rule_id: Optional[str] = None
    response_mode: ResponseMode = ResponseMode.PASSTHROUGH
    semantic_backend: Literal["none", "mock", "qwen"] = "none"
    qwen_called: bool = False
    #: B 显式降级标志。B 2026-09-28 版还没有这个字段，因此默认 None ——
    #: 一旦 B 加上 ``degraded``（或 ``metadata.degraded``），适配层会自动填进来，
    #: D 的判定立刻切到显式值，核心逻辑不用改。None 时退回启发式推断。
    degraded: Optional[bool] = None

    @property
    def local_date(self) -> date:
        return self.occurred_at.date()

    def acquisition(self) -> Acquisition:
        if self.signal_type is not SignalType.NONE:
            return Acquisition.ACQUIRED
        if self.degraded is True:
            # B 明说这次没拿到结果 —— 绝不是「老人没事」
            return Acquisition.NOT_ACQUIRED
        if self.degraded is False:
            return Acquisition.NO_SIGNAL
        # 没有标志，只能靠后端信息推断
        if self.semantic_backend == "none" and not self.qwen_called:
            return Acquisition.NOT_ACQUIRED
        return Acquisition.NO_SIGNAL

    def looks_degraded(self) -> bool:
        """是否高度疑似 B 的降级路径（模型挂了被当成「无信号」）。"""
        if self.degraded is not None:
            return bool(self.degraded) and self.signal_type is SignalType.NONE
        # 启发式：模型被调用过却没出结果
        return (
            self.signal_type is SignalType.NONE
            and self.qwen_called
            and self.semantic_backend == "qwen"
        )


class SafetyEventRecord(BaseModel):
    """一条来自 A 的 safety_level 抛出。"""

    event_id: str
    elder_id: str
    occurred_at: datetime
    safety_level: SafetyLevel = SafetyLevel.NONE
    text: str = Field(default="", description="原文仅进程内使用，不落盘、不进卡片原文")

    @property
    def local_date(self) -> date:
        return self.occurred_at.date()


class ChronicleInput(BaseModel):
    """来自 C 的 get_chronicle(purpose='family_digest')。"""

    state: Literal["ready", "not_ready"] = "not_ready"
    view_version: Optional[str] = None
    items: List[Dict[str, Any]] = Field(default_factory=list)
    narrative: str = ""
    warnings: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


class DigestCard(BaseModel):
    card_id: str
    elder_id: str
    elder_name: str = "老人"
    for_date: date
    tier: Tier
    title: str
    sections: List[Dict[str, str]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    acquisition: Acquisition = Acquisition.NOT_ACQUIRED
    redacted: bool = False
    sources: List[str] = Field(default_factory=list)
    status: CardStatus = CardStatus.DRAFT


class DispatchResult(BaseModel):
    card_id: str
    channel: str
    status: DispatchStatus
    receipt_id: Optional[str] = None
    attempts: int = 0
    reason: str = ""


class Policy(BaseModel):
    """可调阈值。默认值是提案，群里确认后改这里。"""

    trend_days: int = 3              # 连续 N 天同类信号 → P1
    trend_min_severity: Severity = Severity.MODERATE
    sameday_count_threshold: int = 3  # 单日中度及以上条数 ≥ N → P1
    baseline_window_days: int = 7
    baseline_deviation: float = 0.5   # 超出基线均值 50% → P1
    retry_limit: int = 3
