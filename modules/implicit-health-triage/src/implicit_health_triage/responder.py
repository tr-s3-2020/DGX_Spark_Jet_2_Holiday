"""Caring reply for ordinary health signals (task guide sections 25, 26, 27).

The response policy is exactly the one in the task image: 顺势关切并确认用药,
严禁给出医疗建议 — express concern, optionally confirm whether the elder took
their usual medication as prescribed, never give medical advice.
"""

from __future__ import annotations

from .llm.base import LLMProvider, LLMProviderError, LLMTimeoutError
from .logging_utils import get_logger
from .schemas import HealthSignal

#: Section 26 system prompt.
RESPONDER_SYSTEM_PROMPT = """
你是一个陪伴老人聊天的助手。

当前系统已经从用户的话中发现健康相关信息。

你的任务只有：
1. 用自然、温和的方式表达关切；
2. 可以简单确认用户平时应该服用的药是否按原来的安排服用了；
3. 回答适合语音播放；
4. 控制在 1~2 句话。

严禁：
- 诊断疾病；
- 猜测疾病；
- 建议增加药量；
- 建议减少药量；
- 建议停药；
- 建议换药；
- 给出具体治疗方案；
- 声称用户一定没事或一定有某种疾病；
- 使用专业医疗术语或给出具体药物名称与剂量。
""".strip()


class HealthResponder:
    """Generate a short, warm, advice-free reply for a detected health signal."""

    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm

    async def respond(
        self,
        *,
        text: str,
        signal: HealthSignal,
    ) -> str:
        prompt = (
            f"用户原话：\n{text}\n\n"
            "结构化健康信息：\n"
            f"type={signal.type}\n"
            f"detail={signal.detail}\n"
            f"severity={signal.severity}\n\n"
            "生成一句自然、简短、温和的关切回复。"
        )

        try:
            reply = await self.llm.generate_text(
                system_prompt=RESPONDER_SYSTEM_PROMPT,
                user_prompt=prompt,
            )
        except (LLMTimeoutError, LLMProviderError) as error:
            # Section 48.5: a failed health reply falls back to fixed wording.
            get_logger().warning("responder failed: %s", error)
            return ""

        return (reply or "").strip()


__all__ = ["RESPONDER_SYSTEM_PROMPT", "HealthResponder"]
