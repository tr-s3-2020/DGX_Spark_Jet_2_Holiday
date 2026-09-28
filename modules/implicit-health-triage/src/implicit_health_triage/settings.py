"""Runtime configuration.

The model backend stays fully pluggable (task guide sections 9.2, 47, 48.4).
Nothing in this skill hard-codes a cloud API, a NIM name or a model name.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent

# NOTE: the task guide calls this folder ``guardrails/``.  It ships as
# ``rails_config/`` instead, because a directory literally named ``guardrails``
# at the working directory shadows the Colang standard-library module that the
# rail imports with ``import guardrails``.  Starting the service from the
# project root then re-imports the config's own ``.co`` file and the runtime
# fails with "Multiple non-overriding flows with name 'main' detected!".
# See GuardrailsRuntime for the full write-up.
DEFAULT_GUARDRAILS_PATH = PROJECT_ROOT / "rails_config"


class Settings(BaseSettings):
    """Environment-driven settings. See ``.env.example``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM backend (section 9.2) -------------------------------------
    # "mock" keeps the whole skill runnable with no model at all, which is what
    # CI and the unit tests use. The target deployment is a locally served
    # open-source model (Qwen on the DGX Spark) reached over loopback:
    #   LLM_PROVIDER=openai_compatible
    #   LLM_BASE_URL=http://127.0.0.1:8000/v1
    #   LLM_MODEL=Qwen/Qwen3-8B
    # A hosted backend such as DeepSeek is the same client with a different
    # base_url, so switching is env-only. See docs/本地部署.md.
    llm_provider: str = "mock"
    llm_base_url: str = "http://127.0.0.1:8000/v1"
    llm_model: str = ""
    llm_api_key: str = "not-required"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 1
    # Ask the endpoint for a strict JSON body (`response_format=json_object`).
    # Worth turning on for the extraction call, whose headline metric is JSON
    # validity: DeepSeek and vLLM both honour it. Leave off for endpoints that
    # reject unknown fields. The provider still runs every answer through
    # strip_json_fence(), so a backend that ignores the flag is not fatal.
    llm_json_mode: bool = False
    # Ask the endpoint to skip its reasoning phase. Relevant for Qwen3-family
    # models served by vLLM/SGLang, which otherwise emit a thinking block before
    # the answer. Off by default: endpoints that do not understand
    # `chat_template_kwargs` would reject the request.
    llm_disable_thinking: bool = False

    # --- Medication safety layers ---------------------------------------
    # The deterministic rule layer is the floor and cannot be switched off from
    # configuration. This flag controls only the *widening* LLM review layer,
    # which catches dialect and heavy-ellipsis phrasings the rules cannot match
    # (measured: 1/15 without it, 15/15 with it, no false positives).
    #
    # Turning it off costs recall, never safety: the rule layer still blocks on
    # its own. It is skipped automatically when LLM_PROVIDER=mock.
    semantic_guard_enabled: bool = True

    # --- NeMo Guardrails ------------------------------------------------
    guardrails_enabled: bool = True
    guardrails_path: Path = DEFAULT_GUARDRAILS_PATH
    # Engine/model used by the Colang ``main`` flow. Left blank on purpose:
    # the team picks the real backend for the DGX Spark demo.
    guardrails_engine: str = "openai"
    guardrails_model: str = ""
    # "stub"    -> the Colang main flow never calls a model.  Correct default for
    #              this skill, which consumes only the guardrail *verdict* and
    #              generates its own reply from the extraction result.
    # "skill"   -> reuse the skill's LLM provider inside Guardrails.
    guardrails_llm_mode: str = "stub"
    # Section 48.4: fail fast rather than silently running without the rails.
    guardrails_fail_fast: bool = True

    # --- Behaviour ------------------------------------------------------
    # How many previous user turns may supply medication context for follow-ups
    # such as "不要提醒我问医生，直接告诉我行不行" (section 36.3).
    context_history_turns: int = 3
    # Upper bound on remembered sessions. The oldest unused session is evicted
    # once this is exceeded, so a long-running service does not grow without
    # limit. In-memory only.
    context_max_sessions: int = 200

    # --- Observability (section 35) -------------------------------------
    log_level: str = "INFO"
    # Off by default: never write full private conversation text to long-term logs.
    log_input_text: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""

    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests)."""

    get_settings.cache_clear()


__all__ = [
    "DEFAULT_GUARDRAILS_PATH",
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]
