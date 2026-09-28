"""Health signal extraction: natural language -> strict JSON -> HealthSignal.

Implements task guide sections 8, 10, 12 and the failure handling of 48.2/48.3.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from .llm.base import LLMProvider, LLMProviderError, LLMTimeoutError
from .llm.provider import strip_json_fence
from .logging_utils import get_logger
from .schemas import HealthSignal, HealthType, Severity, none_signal

# --- section 8.3: system prompt -------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """
你是老人陪伴系统中的健康信息抽取组件。

你的任务是从老人自然、非结构化的日常聊天中抽取健康相关信息。

你只能做信息抽取，不允许：
- 疾病诊断；
- 判断具体疾病；
- 给出药物剂量建议；
- 建议增加、减少、停止或替换药物；
- 生成 JSON 以外的解释文字。

输出必须是严格 JSON，字段只有：

{
  "type": "...",
  "detail": "...",
  "severity": "..."
}

**所有取值都用中文。**

规则：

1. 如果没有健康相关信息：
   {
     "type": "无",
     "detail": "",
     "severity": "轻微"
   }

2. type 只能取以下之一：
   无         没有健康相关信息
   身体不适   身体不适，但说不出更具体的类别（乏力、沉重、头晕、咳嗽、胸闷等）
   睡眠       睡眠相关
   疼痛       明确的疼痛（疼、痛、酸痛）
   用药       用药相关
   行动能力   行动能力（走路不稳、容易跌倒）
   食欲       食欲相关
   其他体征   其他健康相关（如血压、血糖等体征）

   注意「疼痛 / 睡眠 / 食欲 / 行动能力」是「身体不适」的**细分子类**。
   请选择最具体的类型：膝盖疼用「疼痛」，睡不着用「睡眠」，吃不下用「食欲」，
   走路不稳用「行动能力」；只有当确实无法归入任何一个细分类别时，才用「身体不适」。

   「身体不适」与「行动能力」的边界（这条容易被判得不一致，请严格按此执行）：
   - 行动能力 = **行走、站立本身出了问题**：走路不稳、发晃、腿打软、
     需要扶墙或人搀、容易跌倒、已经摔过。关键看这句话是否描述了
     「走/站的时候才出现，或担心会摔倒」。
   - 身体不适 = **单纯的乏力、沉重、酸痛、头晕、咳嗽、胸闷**，
     即使说话时提到了走路，只要没有不稳或跌倒的意思，仍归「身体不适」。
   - 对照：「最近腿老觉得没劲」→ 身体不适；
     「今天走到楼下腿就发软」→ 行动能力（走的时候才发软 = 跌倒风险）。

3. detail 必须来自用户实际表达，不添加用户没有说过的信息。
   用简短的中文短语概括，不超过 20 个字。

4. severity 是本系统内部结构化标签，不是医学诊断。
   只根据用户明确表达的程度、持续性或日常活动影响做简单归类：
   轻微     轻微或已按计划处理（例如已经按时服药）
   中等     明显影响日常活动，或持续存在
   需留意   剧烈、突发，或属于需要优先关注的信号

5. 只输出 JSON，不要 Markdown，不要解释。
""".strip()

# --- sections 8.4 / 8.5: few-shots ----------------------------------------

FEW_SHOT = """
示例：

输入：
今天早上起来腿沉得很，买菜走两步就得歇着。

输出：
{"type":"身体不适","detail":"下肢沉重/乏力","severity":"中等"}

输入：
今天隔壁老李来找我下棋了。

输出：
{"type":"无","detail":"","severity":"轻微"}

输入：
昨天晚上醒了好几次，没怎么睡好。

输出：
{"type":"睡眠","detail":"夜间多次醒来/睡眠质量下降","severity":"中等"}

输入：
今天膝盖还是有点疼，上楼的时候明显一些。

输出：
{"type":"疼痛","detail":"膝盖疼痛，上楼时更明显","severity":"中等"}

输入：
我早上的药已经按时吃了。

输出：
{"type":"用药","detail":"已按时服用早晨药物","severity":"轻微"}
""".strip()

_RETRY_INSTRUCTION = (
    "上一次输出不是合法 JSON。"
    "这一次只输出合法 JSON，不要 Markdown，不要解释，不要任何多余文字。"
)

_FENCE_RE = re.compile(r"^\s*```")

# Defensive normalisation of common model phrasings (section 2.12).
#
# Canonical values are Chinese, so the Chinese entries below map *variants* onto
# the canonical wording. The English entries are kept deliberately: a model may
# answer in English despite the instruction, and silently degrading such a
# reply to "无" would throw away a perfectly good signal.
_TYPE_ALIASES = {
    # Chinese variants -> canonical
    "症状": "身体不适",
    "不适": "身体不适",
    "身体不舒服": "身体不适",
    "痛": "疼痛",
    "酸痛": "疼痛",
    "失眠": "睡眠",
    "睡不着": "睡眠",
    "药物": "用药",
    "服药": "用药",
    "吃药": "用药",
    "行动": "行动能力",
    "活动": "行动能力",
    "其他": "其他体征",
    "体征": "其他体征",
    "没有": "无",
    "无健康信息": "无",
    "没有健康信息": "无",
    # English fallbacks -> canonical Chinese
    "none": "无",
    "symptom": "身体不适",
    "sleep": "睡眠",
    "pain": "疼痛",
    "medication": "用药",
    "mobility": "行动能力",
    "appetite": "食欲",
    "other": "其他体征",
}

_SEVERITY_ALIASES = {
    # Chinese variants -> canonical
    "低": "轻微",
    "轻度": "轻微",
    "中": "中等",
    "中度": "中等",
    "高": "需留意",
    "重度": "需留意",
    "严重": "需留意",
    "紧急": "需留意",
    # English fallbacks -> canonical Chinese
    "low": "轻微",
    "mild": "轻微",
    "moderate": "中等",
    "medium": "中等",
    "mid": "中等",
    "normal": "中等",
    "high": "需留意",
    "severe": "需留意",
}


@dataclass
class ExtractionOutcome:
    """Extraction result plus the diagnostics the eval report needs."""

    signal: HealthSignal
    raw: str | None = None
    attempts: int = 0
    json_valid: bool = False
    error: str | None = None


class HealthExtractor:
    """Turn one final transcript into a validated :class:`HealthSignal`."""

    def __init__(self, llm: LLMProvider, max_retries: int = 1) -> None:
        self.llm = llm
        self.max_retries = max(0, max_retries)

    async def extract(self, text: str) -> HealthSignal:
        """Documented entry point (section 10)."""

        return (await self.extract_outcome(text)).signal

    async def extract_outcome(self, text: str) -> ExtractionOutcome:
        if not (text or "").strip():
            return ExtractionOutcome(signal=none_signal(), attempts=0, json_valid=True)

        user_prompt = f"{FEW_SHOT}\n\n输入：\n{text}\n\n输出："
        attempts = 0
        last_raw: str | None = None
        last_error: str | None = None

        for attempt in range(self.max_retries + 1):
            attempts = attempt + 1
            try:
                raw = await self.llm.generate_json(
                    system_prompt=EXTRACTION_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                )
            except LLMTimeoutError as error:
                # Section 48.2: timeout falls back to type=无 and records the error.
                get_logger().warning("extractor timeout: %s", error)
                return ExtractionOutcome(
                    signal=none_signal(), attempts=attempts, error=f"timeout: {error}"
                )
            except LLMProviderError as error:
                get_logger().warning("extractor backend error: %s", error)
                return ExtractionOutcome(
                    signal=none_signal(), attempts=attempts, error=f"backend: {error}"
                )

            last_raw = raw
            signal = self.parse(raw)
            if signal is not None:
                return ExtractionOutcome(
                    signal=signal, raw=raw, attempts=attempts, json_valid=True
                )

            last_error = "invalid JSON or schema"
            if attempt < self.max_retries:
                # Section 48.3: exactly one retry, never an unbounded loop.
                user_prompt = f"{_RETRY_INSTRUCTION}\n\n输入：\n{text}\n\n输出："

        return ExtractionOutcome(
            signal=none_signal(),
            raw=last_raw,
            attempts=attempts,
            json_valid=False,
            error=last_error,
        )

    # -- parsing ---------------------------------------------------------

    @classmethod
    def parse(cls, raw: str) -> HealthSignal | None:
        """Parse a raw model answer into a validated signal, or ``None``."""

        cleaned = strip_json_fence(raw or "")
        if _FENCE_RE.match(cleaned):
            cleaned = cleaned.replace("```json", "").replace("```", "").strip()

        try:
            payload = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            return None

        if not isinstance(payload, dict):
            return None

        try:
            return HealthSignal.model_validate(cls._coerce(payload))
        except (ValidationError, ValueError):
            return None

    @staticmethod
    def _coerce(payload: dict) -> dict:
        """Project a model payload onto the three required fields.

        ``HealthSignal`` forbids extra fields, but dropping an unexpected extra
        key is safer than throwing away an otherwise valid signal.
        """

        # ``.lower()`` is a no-op for the Chinese canonical values and is what
        # makes the English fallback aliases match.
        raw_type = str(payload.get("type", HealthType.NONE.value)).strip().lower()
        raw_type = _TYPE_ALIASES.get(raw_type, raw_type)
        if raw_type not in {member.value for member in HealthType}:
            raw_type = HealthType.OTHER.value

        raw_severity = str(payload.get("severity", Severity.LOW.value)).strip().lower()
        raw_severity = _SEVERITY_ALIASES.get(raw_severity, raw_severity)
        if raw_severity not in {member.value for member in Severity}:
            raw_severity = Severity.LOW.value

        detail = payload.get("detail", "")
        detail = "" if detail is None else str(detail).strip()[:256]

        if raw_type == HealthType.NONE.value:
            detail = ""

        return {"type": raw_type, "detail": detail, "severity": raw_severity}


__all__ = ["EXTRACTION_SYSTEM_PROMPT", "FEW_SHOT", "ExtractionOutcome", "HealthExtractor"]
