"""NeMo Guardrails runtime (task guide sections 17-23, 48.4).

Design notes
------------

**Why the config folder is ``rails_config/`` and not ``guardrails/``.**
The task guide names it ``guardrails/``.  That name is unusable in practice:
the ``input rails`` flow must ``import guardrails`` (the Colang
standard-library module), and a directory called ``guardrails`` sitting in the
process working directory shadows it.  When the service is started from the
project root the loader then re-reads the config's own ``.co`` file through
that import, and the runtime dies with::

    Multiple non-overriding flows with name 'main' detected! There can only be one!

Measured directly: identical config content in the same process yields a
``main`` flow count of 1 with a neutral working directory and 2 with the
project root as working directory.  Renaming the folder removes the shadowing.

**Pre-generation blocking.**  ``generate_async(..., options={"rails": ["input"]})``
runs the Colang ``input rails`` flow before any model generation.  When the
medication rail fires it aborts the flow and returns the fixed safety wording,
so the dangerous turn never reaches a generative model.  Verified empirically
against nemoguardrails 0.24.1.

**Why not ``check_async``.**  ``LLMRails.check_async`` looks like the natural
fit, but it is unusable with Colang 2.x in this release: it always injects
``options["log"] = {"activated_rails": True}`` and the 2.x runtime rejects that
with ``The `log` option is not supported for Colang 2.0 configurations``.
Passing ``rail_types=[RailType.INPUT]`` additionally fails with
``RailTypeNotConfiguredError`` because that path validates the Colang-1.x
``rails.input.flows`` list, which a 2.x config never populates.

**One ``LLMRails`` per process.**  Section 22: initialization compiles the
Colang flows, so it must not happen per request.

**Fail fast.**  Section 48.4: if the rails cannot be built we raise rather than
silently serving the demo without the safety fence.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .guardrails_llm import build_guardrails_llm
from .logging_utils import get_logger
from .safety.safe_templates import MEDICATION_SAFETY_RESPONSE
from .settings import Settings, get_settings

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_WHITESPACE_RE = re.compile(r"\s+")

#: Report label for the blocking flow, used by the demo panel.
MEDICATION_SAFETY_RAIL = "input rails"

# Materialised config directories are shared process-wide.
#
# Building a *second* ``RailsConfig`` from a *second* copy of the same config is
# not safe: the Colang loader registers flows by their path inside the config,
# so loading a different directory containing the same
# ``rails/medical_safety.co`` makes the runtime report
# "Multiple non-overriding flows with name 'main' detected!".
# Two runtimes in one process are normal (the FastAPI app plus a test fixture),
# so the resolved directory is cached and reused.
_resolved_config_dirs: dict[tuple, Path] = {}
_resolved_config_lock = threading.Lock()


def _config_cache_key(source: Path, mapping: dict[str, str]) -> tuple:
    return (str(source.resolve()), tuple(sorted(mapping.items())))


@dataclass(frozen=True)
class GuardrailVerdict:
    """Outcome of running the NeMo Guardrails input rail."""

    blocked: bool
    response: str = ""
    rail: str | None = None
    status: str | None = None
    error: str | None = None


def _normalise(text: str) -> str:
    return _WHITESPACE_RE.sub("", text or "")


class _DropMessageFilter(logging.Filter):
    """Drop exactly one expected log line, leaving everything else intact."""

    def __init__(self, needle: str) -> None:
        super().__init__()
        self._needle = needle

    def filter(self, record: logging.LogRecord) -> bool:
        return self._needle not in record.getMessage()


def _quiet_benign_continuation_noise() -> None:
    """Silence two known-benign Colang messages that would pollute the demo log.

    1. ``nemoguardrails.colang.v2_x.runtime.statemachine`` logs a full traceback
       when ``$flows`` ends up ``None``.  With ``activate llm continuation``
       (task guide section 21) the runtime also tries to synthesise a bot-action
       flow for every turn the input rail lets through.  This skill does not
       consume that generated reply — it produces its own from the extraction
       result — so the synthesis is expected to yield nothing usable.  The
       *safety* verdict is unaffected: the ``input rails`` flow has already run
       and completed by then, which the test suite asserts.

    2. ``nemoguardrails.rails.llm.llmrails`` warns that an LLM passed to the
       constructor overrides the one in ``config.yml``.  That is exactly the
       intended setup here: the model is injected from the environment
       (sections 19, 47), so the placeholder in the YAML is meant to be ignored.

    Both are dropped by message filter rather than by raising a whole logger's
    level, so genuine problems in those modules still surface.  Initialisation
    failures are raised as exceptions regardless (section 48.4).
    """

    logging.getLogger("nemoguardrails.colang.v2_x.runtime.statemachine").setLevel(logging.ERROR)

    llmrails_logger = logging.getLogger("nemoguardrails.rails.llm.llmrails")
    if not any(isinstance(f, _DropMessageFilter) for f in llmrails_logger.filters):
        llmrails_logger.addFilter(
            _DropMessageFilter("Both an LLM was provided via constructor")
        )


def _extract_content(response: Any) -> str:
    """Pull the assistant text out of whatever ``generate_async`` returned."""

    if response is None:
        return ""

    if isinstance(response, str):
        return response

    payload = getattr(response, "response", response)

    if isinstance(payload, str):
        return payload

    if isinstance(payload, dict):
        return str(payload.get("content", "") or "")

    if isinstance(payload, (list, tuple)):
        for message in reversed(payload):
            if isinstance(message, dict) and message.get("role") == "assistant":
                return str(message.get("content", "") or "")
        if payload and isinstance(payload[-1], dict):
            return str(payload[-1].get("content", "") or "")

    return ""


class GuardrailsRuntime:
    """Owns the process-wide ``LLMRails`` instance and the resolved config."""

    def __init__(
        self,
        settings: Settings | None = None,
        llm: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._llm_override = llm
        self._rails: Any | None = None
        self._lock = threading.Lock()
        self._config_dir: Path | None = None

    # -- lifecycle -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self._settings.guardrails_enabled)

    def _substitution_map(self) -> dict[str, str]:
        model = (
            self._settings.guardrails_model
            or self._settings.llm_model
            or "unset-model"
        )
        return {
            "GUARDRAILS_ENGINE": self._settings.guardrails_engine or "openai",
            "GUARDRAILS_MODEL": model,
        }

    def resolve_config_dir(self) -> Path:
        """Copy the shipped config, substituting ``${...}`` placeholders.

        The repository keeps the documented placeholder form (section 19) while
        the effective model stays environment-driven (sections 9.2, 47).
        """

        if self._config_dir is not None:
            return self._config_dir

        source = Path(self._settings.guardrails_path)
        if not source.is_dir():
            raise FileNotFoundError(f"Guardrails config not found: {source}")

        mapping = self._substitution_map()
        key = _config_cache_key(source, mapping)

        with _resolved_config_lock:
            cached = _resolved_config_dirs.get(key)
            if cached is not None and cached.is_dir():
                self._config_dir = cached
                return cached

            target = Path(tempfile.mkdtemp(prefix="iht-guardrails-"))
            shutil.copytree(source, target, dirs_exist_ok=True)

            config_file = target / "config.yml"
            content = config_file.read_text(encoding="utf-8")
            content = _PLACEHOLDER_RE.sub(
                lambda match: mapping.get(match.group(1), match.group(0)), content
            )
            config_file.write_text(content, encoding="utf-8")

            _resolved_config_dirs[key] = target
            self._config_dir = target
            return target

    def _ensure_rails(self) -> Any:
        if self._rails is not None:
            return self._rails

        with self._lock:
            if self._rails is not None:
                return self._rails

            # Imported lazily so the deterministic layers stay importable
            # without the heavy dependency tree.
            from nemoguardrails import LLMRails, RailsConfig

            from .guardrails_embeddings import register as register_embeddings

            # Must happen before RailsConfig/RailsConfig validation so the
            # `iht_hashing` engine named in config.yml resolves.
            register_embeddings()
            _quiet_benign_continuation_noise()

            config_dir = self.resolve_config_dir()
            llm = self._llm_override or build_guardrails_llm(
                mode=self._settings.guardrails_llm_mode,
            )

            try:
                config = RailsConfig.from_path(str(config_dir))
                self._rails = LLMRails(config, llm=llm)
            except Exception as error:  # pragma: no cover - startup failure path
                message = f"NeMo Guardrails failed to initialize: {error}"
                if self._settings.guardrails_fail_fast:
                    raise RuntimeError(message) from error
                get_logger().error(message)
                return None

            get_logger().info(
                "NeMo Guardrails initialized (colang=%s, dir=%s)",
                getattr(config, "colang_version", "?"),
                config_dir,
            )
            return self._rails

    # -- verdicts --------------------------------------------------------

    async def check_input(self, text: str) -> GuardrailVerdict:
        """Run the Colang ``input rails`` flow on one user turn."""

        if not self.enabled:
            return GuardrailVerdict(blocked=False, status="disabled")

        rails = self._ensure_rails()
        if rails is None:
            return GuardrailVerdict(blocked=False, status="unavailable")

        try:
            from nemoguardrails.exceptions import LLMCallException
        except ImportError:  # pragma: no cover - API drift guard
            LLMCallException = None  # type: ignore[assignment,misc]

        try:
            response = await rails.generate_async(
                messages=[{"role": "user", "content": text}],
                options={"rails": ["input"]},
            )
        except Exception as error:
            if LLMCallException is not None and isinstance(error, LLMCallException):
                # The input rails ran first and let this turn through; only the
                # (unused) continuation generation failed.  Not a safety failure.
                get_logger().warning("guardrails continuation failed: %s", error)
                return GuardrailVerdict(blocked=False, status="passed_generation_failed")

            message = f"NeMo Guardrails input rail failed: {error}"
            if self._settings.guardrails_fail_fast:
                raise RuntimeError(message) from error
            get_logger().error(message)
            return GuardrailVerdict(blocked=False, status="error", error=message)

        content = _extract_content(response)
        blocked = _normalise(content) == _normalise(MEDICATION_SAFETY_RESPONSE)

        return GuardrailVerdict(
            blocked=blocked,
            response=content if blocked else "",
            rail=MEDICATION_SAFETY_RAIL if blocked else None,
            status="blocked" if blocked else "passed",
        )

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        """Release this runtime's ``LLMRails``.

        The materialised config directory is intentionally **not** deleted: it is
        shared process-wide (see ``_resolved_config_dirs``) and another runtime
        may still be using it.  It lives under the OS temp directory, which is
        reclaimed by the operating system.
        """

        self._rails = None

    @staticmethod
    def cleanup_shared_config_dirs() -> None:
        """Delete the cached config directories. Intended for test teardown."""

        with _resolved_config_lock:
            for target in _resolved_config_dirs.values():
                shutil.rmtree(target, ignore_errors=True)
            _resolved_config_dirs.clear()


__all__ = ["MEDICATION_SAFETY_RAIL", "GuardrailVerdict", "GuardrailsRuntime"]
