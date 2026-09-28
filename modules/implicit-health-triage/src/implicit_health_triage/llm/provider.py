"""Concrete LLM providers (task guide sections 6, 9, 47).

* :class:`MockLLMProvider` — deterministic, offline stand-in.  It exists so the
  whole pipeline (prompt assembly, JSON parsing, retry, fallback, Guardrails,
  API, demo) is runnable and testable *before* a model endpoint is chosen.  It is
  a keyword stub, **not** a language model: extraction accuracy measured against
  it says nothing about real model quality.
* :class:`OpenAICompatibleProvider` — talks to any OpenAI-compatible
  ``/chat/completions`` server, which covers vLLM, NVIDIA NIM, Ollama and a
  TensorRT-LLM OpenAI front end on DGX Spark.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..settings import Settings, get_settings
from .base import LLMProvider, LLMProviderError, LLMTimeoutError

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Reasoning ("thinking") blocks. Qwen3-style models emit these before the answer,
# and the reasoning text often contains braces of its own — which would otherwise
# be mistaken for the JSON object. Stripping them is a parser-level defence that
# works regardless of which backend or template produced the text.
_THINKING_PAIR_RE = re.compile(
    r"<(?:think|thinking|reasoning|analysis)>.*?</(?:think|thinking|reasoning|analysis)>",
    re.DOTALL | re.IGNORECASE,
)
_THINKING_OPEN_RE = re.compile(
    r"<(?:think|thinking|reasoning|analysis)>", re.IGNORECASE
)
#: Closing markers, including the special tokens some chat templates use
#: instead of a plain ``</think>``.
_THINKING_END_TOKENS: tuple[str, ...] = (
    "<|end_of_thinking|>",
    "<｜end▁of▁thinking｜>",
    "</think>",
    "</thinking>",
    "</reasoning>",
    "</analysis>",
)

# ---------------------------------------------------------------------------
# Offline stub
# ---------------------------------------------------------------------------

#: Exact reproductions of the few-shots documented in the task guide, so the
#: image example and the section 8.5 examples behave identically with or
#: without a real model.  Values are the canonical Chinese ones — see
#: ``schemas.HealthType`` / ``schemas.Severity``.
_FEW_SHOT_REPLIES: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "今天早上起来腿沉得很",
        {"type": "身体不适", "detail": "下肢沉重/乏力", "severity": "中等"},
    ),
    (
        "今天隔壁老李来找我下棋",
        {"type": "无", "detail": "", "severity": "轻微"},
    ),
    (
        "昨天晚上醒了好几次",
        {"type": "睡眠", "detail": "夜间多次醒来/睡眠质量下降", "severity": "中等"},
    ),
    (
        "今天膝盖还是有点疼",
        {"type": "疼痛", "detail": "膝盖疼痛，上楼时更明显", "severity": "中等"},
    ),
    (
        "我早上的药已经按时吃了",
        {"type": "用药", "detail": "已按时服用早晨药物", "severity": "轻微"},
    ),
)

#: Ordered keyword fallbacks: (pattern, type, detail, severity).
#:
#: The 行动能力 rule must come before the generic lower-limb rule, and it is the
#: one that keeps this stub honest about the prompt's taxonomy: 行动能力 is a
#: *subtype* of 身体不适 meaning 走路不稳/容易跌倒 (a fall risk), whereas plain
#: weakness is the generic type. Without it this stub could only ever emit 7 of
#: the 8 types the prompt defines, so `行动能力` was structurally untestable.
_KEYWORD_RULES: tuple[tuple[str, str, str, str], ...] = (
    (r"忘(?:了)?(?:吃|服)药|漏(?:吃|服)药|没吃药", "用药", "提及漏服/忘记服药", "中等"),
    (r"停药|不吃了|把药停", "用药", "提及停用药物", "中等"),
    # 行动能力：不稳 / 跌倒 / 需要搀扶，或走、站的时候才出现的腿软没劲。
    (
        r"不稳|摔倒|摔了|摔了?一跤|跌倒|跌了|扶(?:着|墙)|拄拐|搀|不利索"
        r"|(?:走|站)[^。！？；]{0,6}(?:发软|腿软|没劲|发颤|打晃)",
        "行动能力",
        "行走不稳/跌倒风险",
        "中等",
    ),
    (r"睡|醒|失眠|睡不着|梦", "睡眠", "睡眠情况异常", "中等"),
    (r"疼|痛|酸胀", "疼痛", "身体疼痛", "中等"),
    (r"吃不下|没胃口|不想吃|没食欲", "食欲", "食欲下降", "中等"),
    (r"胸口|胸闷|喘不上气|呼吸困难|心慌", "身体不适", "胸闷/呼吸不适", "需留意"),
    (r"头晕|发晕|有点晕|晕得|眼花", "身体不适", "头晕", "中等"),
    (r"腿沉|没劲|乏力|走不动|走两步|腿软|发软|站不稳|腿脚", "身体不适", "下肢沉重/乏力", "中等"),
    (r"咳嗽|咳得", "身体不适", "咳嗽", "中等"),
    (r"血压|血糖|心率", "其他体征", "提到血压/血糖等体征", "中等"),
    (r"药", "用药", "提及用药情况", "轻微"),
)


class MockLLMProvider(LLMProvider):
    """Deterministic offline provider. No network, no model, no randomness."""

    def __init__(self, responder_reply: str | None = None) -> None:
        self._responder_reply = responder_reply
        self.calls: list[tuple[str, str]] = []

    async def generate_json(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append(("json", user_prompt))

        target = self._extract_target(user_prompt)

        for needle, payload in _FEW_SHOT_REPLIES:
            if needle in target:
                return json.dumps(payload, ensure_ascii=False)

        for pattern, health_type, detail, severity in _KEYWORD_RULES:
            if re.search(pattern, target):
                return json.dumps(
                    {"type": health_type, "detail": detail, "severity": severity},
                    ensure_ascii=False,
                )

        return json.dumps(
            {"type": "无", "detail": "", "severity": "轻微"}, ensure_ascii=False
        )

    async def generate_text(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append(("text", user_prompt))

        if self._responder_reply is not None:
            return self._responder_reply

        if "type=睡眠" in user_prompt:
            return "昨晚没睡好，今天白天要不要找机会躺一会儿？您平时的药还是按原来的安排吃的吗？"
        if "type=疼痛" in user_prompt:
            return "听着就难受，您今天上楼的时候慢一点。平时的药还是照原来的安排吃的吧？"
        if "type=食欲" in user_prompt:
            return "胃口不好也别硬撑，想吃点什么顺口的？您平时的药还是按原来的安排吃着吗？"
        if "type=用药" in user_prompt:
            return "记着按医生说的吃就好。您今天身体还有别的不舒服吗？"

        return "听起来您今天走路比平时费劲些。您今天平时该吃的药都按原来的安排吃了吗？"

    @staticmethod
    def _extract_target(user_prompt: str) -> str:
        """Pull the elder's utterance out of the extraction prompt."""

        matches = re.findall(r"输入[:：]\s*\n(.+?)(?:\n\s*\n|\n输出)", user_prompt, re.DOTALL)
        if matches:
            return matches[-1].strip()
        return user_prompt


# ---------------------------------------------------------------------------
# Real backend
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI-compatible ``/chat/completions`` client."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "not-required",
        timeout_seconds: float = 30.0,
        use_json_mode: bool = False,
        disable_thinking: bool = False,
    ) -> None:
        if not model:
            raise LLMProviderError(
                "LLM_MODEL is empty. Set LLM_MODEL (and LLM_BASE_URL) to the local "
                "endpoint chosen for the DGX Spark demo, or keep LLM_PROVIDER=mock."
            )

        self._model = model
        self._use_json_mode = use_json_mode
        self._disable_thinking = disable_thinking
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        *,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        """Assemble the chat-completions body.

        Extracted so the request shape is testable without a live endpoint.

        ``json_mode`` is opt-in per call and must stay that way.  DeepSeek
        rejects ``response_format=json_object`` with HTTP 400 when the prompt
        does not contain the word "json", and the responder's prompt is prose.
        Requesting JSON globally therefore broke every reply while the
        extraction metric still looked perfect — the failure was only visible
        in the log, because the responder degrades to a canned fallback.
        """

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if self._use_json_mode and json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self._disable_thinking:
            # vLLM / SGLang extension understood by Qwen3-family chat templates.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    async def _chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        *,
        json_mode: bool = False,
    ) -> str:
        payload = self.build_payload(
            system_prompt, user_prompt, temperature, json_mode=json_mode
        )

        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as error:
            raise LLMTimeoutError(f"LLM request timed out: {error}") from error
        except httpx.HTTPError as error:
            raise LLMProviderError(f"LLM request failed: {error}") from error

        if response.status_code >= 400:
            raise LLMProviderError(
                f"LLM backend returned HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            body = response.json()
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as error:
            raise LLMProviderError(
                f"Unexpected LLM response shape: {response.text[:300]}"
            ) from error

    async def generate_json(self, *, system_prompt: str, user_prompt: str) -> str:
        """Structured call — the only one allowed to ask for a JSON body."""

        return await self._chat(system_prompt, user_prompt, temperature=0.0, json_mode=True)

    async def generate_text(self, *, system_prompt: str, user_prompt: str) -> str:
        """Prose call — must never carry response_format."""

        return await self._chat(system_prompt, user_prompt, temperature=0.5, json_mode=False)

    async def aclose(self) -> None:
        await self._client.aclose()


def strip_reasoning(raw: str) -> str:
    """Remove a model's reasoning block, keeping only the answer.

    Qwen3-family models (and any endpoint that leaves thinking enabled) emit
    something like::

         thinking用户说腿沉，可能归到身体不适……<｜end▁of▁thinking｜>{"type":"身体不适",...}

    The reasoning text can contain braces, so it must be removed *before* any
    attempt to locate the JSON object — otherwise the parser latches onto the
    wrong span. Both well-formed ``<tag>...</tag>`` pairs and unclosed openings
    (ended by a special token instead of a matching tag) are handled.
    """

    text = raw or ""

    # 1) well-formed <tag>...</tag> pairs
    text = _THINKING_PAIR_RE.sub("", text)

    # 2) an opening tag still present means the block never closed with a plain
    #    tag — drop everything up to and including the last closing marker.
    if _THINKING_OPEN_RE.search(text):
        cut = -1
        marker = ""
        for token in _THINKING_END_TOKENS:
            index = text.rfind(token)
            if index > cut:
                cut, marker = index, token
        text = text[cut + len(marker) :] if cut != -1 else ""

    # 3) stray closing markers left over from a partially templated response
    for token in _THINKING_END_TOKENS:
        text = text.replace(token, "")

    return text.strip()


def strip_json_fence(raw: str) -> str:
    """Extract the JSON object from a model answer (section 2.12 / 10).

    Handles the four failure shapes the guide warns about and that real
    endpoints actually produce: a Markdown fence, leading prose such as
    ``当然，以下是 JSON：``, trailing commentary, and a reasoning block emitted
    by a thinking model such as Qwen3.
    """

    text = strip_reasoning(raw)

    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    if text.startswith("{") and text.endswith("}"):
        return text

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]

    return text


def build_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """Factory driven by ``LLM_PROVIDER`` (section 9.2).

    Every real backend — the hosted DeepSeek API used while the Spark endpoint
    is not ready, and the self-hosted open-source model that replaces it — goes
    through the same :class:`OpenAICompatibleProvider`.  Swapping the model is
    therefore an ``.env`` change only; no business code moves.  ``deepseek`` is
    accepted as an explicit alias purely so a reviewer reading ``.env`` can see
    which backend is live without knowing that DeepSeek speaks the OpenAI wire
    format.
    """

    settings = settings or get_settings()
    provider = (settings.llm_provider or "mock").strip().lower()

    if provider == "mock":
        return MockLLMProvider()

    if provider in {
        "openai",
        "openai_compatible",
        "vllm",
        "nim",
        "ollama",
        "local",
        "tensorrt_llm",
        "deepseek",
    }:
        return OpenAICompatibleProvider(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.llm_timeout_seconds,
            use_json_mode=settings.llm_json_mode,
            disable_thinking=settings.llm_disable_thinking,
        )

    raise LLMProviderError(
        f"Unknown LLM_PROVIDER {settings.llm_provider!r}. "
        "Use 'mock', 'deepseek' or 'openai_compatible'."
    )


__all__ = [
    "MockLLMProvider",
    "OpenAICompatibleProvider",
    "build_llm_provider",
    "strip_json_fence",
]
