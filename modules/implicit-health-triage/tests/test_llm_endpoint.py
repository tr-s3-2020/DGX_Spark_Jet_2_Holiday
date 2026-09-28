"""The real-LLM endpoint contract — for module 2 (health-signal extraction).

Module 2 is the only part of the skill whose quality depends on a model, and the
model is expected to change: DeepSeek while the DGX Spark endpoint is not ready,
then a self-hosted open-source model for the demo.  ``LLM_PROVIDER``,
``LLM_BASE_URL`` and ``LLM_MODEL`` are therefore an interface, not a detail, and
these tests pin it.

Everything here runs offline through ``httpx.MockTransport``.  That is enough to
cover the whole client — URL joining, headers, request body, response parsing
and every error path — so a broken swap is caught in CI rather than at the
venue.  What it deliberately does **not** cover is model *quality*: whether
DeepSeek or Qwen actually labels a sentence correctly is measured by
``scripts/check_model.py verify`` and ``scripts/run_eval.py`` against a live
endpoint, and is not asserted here.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from implicit_health_triage.llm.provider import (
    LLMProviderError,
    LLMTimeoutError,
    MockLLMProvider,
    OpenAICompatibleProvider,
    build_llm_provider,
)
from implicit_health_triage.settings import Settings

DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"


@pytest.fixture(autouse=True)
def _isolate_llm_environment(monkeypatch):
    """Make every test in this file hermetic against the ambient environment.

    ``Settings(_env_file=None)`` suppresses the ``.env`` file but **not**
    exported environment variables.  Without this fixture a developer who has
    ``LLM_PROVIDER=deepseek`` in their shell changes what "the default" means,
    and ``test_mock_stays_the_default`` fails for reasons that have nothing to
    do with the code.  Measured, not hypothetical: pointing ``LLM_PROVIDER`` at
    a black-hole endpoint made exactly that test fail.
    """

    for name in list(os.environ):
        if name.upper().startswith("LLM_"):
            monkeypatch.delenv(name, raising=False)


def _settings(**overrides) -> Settings:
    """A Settings object built in memory, ignoring any .env on the machine."""

    return Settings(_env_file=None, **overrides)


def _provider(**overrides) -> OpenAICompatibleProvider:
    kwargs = {
        "base_url": DEEPSEEK_BASE,
        "model": DEEPSEEK_MODEL,
        "api_key": "sk-test",
    }
    kwargs.update(overrides)
    return OpenAICompatibleProvider(**kwargs)


def _transport(handler):
    """Attach a MockTransport to a provider so no socket is opened."""

    def _with(provider: OpenAICompatibleProvider, handler=handler):
        provider._client = httpx.AsyncClient(
            base_url=provider._client.base_url,
            transport=httpx.MockTransport(handler),
            headers=dict(provider._client.headers),
            timeout=provider._client.timeout,
        )
        return provider

    return _with


def _reply(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": DEEPSEEK_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


# ---------------------------------------------------------------------------
# Choosing the backend
# ---------------------------------------------------------------------------


def test_deepseek_is_selectable_by_name():
    """`.env` must be able to say `deepseek` and get the right client."""

    provider = build_llm_provider(
        _settings(
            llm_provider="deepseek",
            llm_base_url=DEEPSEEK_BASE,
            llm_model=DEEPSEEK_MODEL,
            llm_api_key="sk-test",
        )
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert str(provider._client.base_url) == DEEPSEEK_BASE + "/"
    assert provider._model == DEEPSEEK_MODEL


def test_selfhosted_model_is_selectable_by_name():
    """The model that replaces DeepSeek is the same client, different env."""

    provider = build_llm_provider(
        _settings(
            llm_provider="openai_compatible",
            llm_base_url="http://127.0.0.1:8001/v1",
            llm_model="Qwen3-8B",
        )
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._model == "Qwen3-8B"


def test_mock_stays_the_default():
    assert isinstance(build_llm_provider(_settings()), MockLLMProvider)


def test_unknown_provider_names_the_valid_choices():
    with pytest.raises(LLMProviderError) as caught:
        build_llm_provider(_settings(llm_provider="gemini"))

    message = str(caught.value)
    assert "gemini" in message
    assert "deepseek" in message and "openai_compatible" in message


def test_missing_model_fails_loudly_rather_than_at_request_time():
    with pytest.raises(LLMProviderError, match="LLM_MODEL"):
        build_llm_provider(_settings(llm_provider="deepseek", llm_model=""))


# ---------------------------------------------------------------------------
# LLM_JSON_MODE must actually reach the request
# ---------------------------------------------------------------------------


def test_json_mode_is_off_by_default():
    """Endpoints that reject unknown fields must not receive response_format."""

    assert "response_format" not in _provider().build_payload("s", "u", 0.0)


def test_json_mode_setting_reaches_the_request_body():
    """Regression: `use_json_mode` used to be unreachable from configuration.

    The parameter existed on the constructor and the payload branch existed, but
    the factory never passed either, so `response_format` could not be turned on
    by any environment variable.  Extraction is scored on JSON validity, so this
    silently capped the metric.
    """

    provider = build_llm_provider(
        _settings(
            llm_provider="deepseek",
            llm_base_url=DEEPSEEK_BASE,
            llm_model=DEEPSEEK_MODEL,
            llm_json_mode=True,
        )
    )

    payload = provider.build_payload("sys", "user", 0.0, json_mode=True)
    assert payload["response_format"] == {"type": "json_object"}


async def test_prose_generation_never_requests_json_object():
    """Regression: JSON mode was applied globally and broke every reply.

    DeepSeek answers HTTP 400 — "Prompt must contain the word 'json' in some
    form to use 'response_format' of type 'json_object'" — because the
    responder's prompt is prose.  The responder then degrades to its canned
    fallback, so the demo still reported success and the failure was visible
    only in the log.  JSON mode must therefore be opt-in per call.
    """

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _reply("您今天感觉怎么样？")

    provider = _transport(handler)(_provider(use_json_mode=True))
    await provider.generate_text(system_prompt="你是陪伴助手", user_prompt="说句关心的话")
    await provider.aclose()

    assert "response_format" not in seen[0]


async def test_extraction_still_requests_json_object():
    """The other half of the pair: generate_json must keep asking for JSON."""

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _reply('{"type":"无","detail":"","severity":"轻微"}')

    provider = _transport(handler)(_provider(use_json_mode=True))
    await provider.generate_json(system_prompt="只输出 JSON", user_prompt="今天天气不错")
    await provider.aclose()

    assert seen[0]["response_format"] == {"type": "json_object"}


def test_json_mode_off_means_neither_call_asks_for_json():
    provider = _provider(use_json_mode=False)

    assert "response_format" not in provider.build_payload("s", "u", 0.0, json_mode=True)
    assert "response_format" not in provider.build_payload("s", "u", 0.5, json_mode=False)


def test_json_mode_defaults_to_off_in_settings():
    assert _settings().llm_json_mode is False


# ---------------------------------------------------------------------------
# The full round trip, offline
# ---------------------------------------------------------------------------


async def test_chat_completions_url_and_headers_are_correct():
    """A wrong path or a missing bearer token is the most common swap failure."""

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _reply('{"type":"身体不适"}')

    provider = _transport(handler)(_provider())
    raw = await provider.generate_json(system_prompt="sys", user_prompt="usr")
    await provider.aclose()

    assert seen["url"] == DEEPSEEK_BASE + "/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == DEEPSEEK_MODEL
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert raw == '{"type":"身体不适"}'


async def test_trailing_slash_in_base_url_does_not_double_up():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _reply("{}")

    provider = _transport(handler)(_provider(base_url=DEEPSEEK_BASE + "/"))
    await provider.generate_json(system_prompt="s", user_prompt="u")
    await provider.aclose()

    assert seen["url"] == DEEPSEEK_BASE + "/chat/completions"


async def test_extraction_temperature_is_deterministic():
    """Extraction must not sample — the same sentence has to score the same."""

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["temperature"] = json.loads(request.content)["temperature"]
        return _reply("{}")

    provider = _transport(handler)(_provider())
    await provider.generate_json(system_prompt="s", user_prompt="u")
    await provider.aclose()

    assert seen["temperature"] == 0.0


async def test_http_error_is_wrapped_with_the_body_for_debugging():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"error":{"message":"Invalid API key"}}')

    provider = _transport(handler)(_provider())
    with pytest.raises(LLMProviderError) as caught:
        await provider.generate_json(system_prompt="s", user_prompt="u")
    await provider.aclose()

    assert "401" in str(caught.value)
    assert "Invalid API key" in str(caught.value)


async def test_timeout_is_a_distinct_error_type():
    """Section 48.2 keeps timeouts separable so the caller can fail safe."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow", request=request)

    provider = _transport(handler)(_provider())
    with pytest.raises(LLMTimeoutError):
        await provider.generate_json(system_prompt="s", user_prompt="u")
    await provider.aclose()


async def test_unexpected_response_shape_is_rejected_not_guessed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    provider = _transport(handler)(_provider())
    with pytest.raises(LLMProviderError, match="shape"):
        await provider.generate_json(system_prompt="s", user_prompt="u")
    await provider.aclose()
