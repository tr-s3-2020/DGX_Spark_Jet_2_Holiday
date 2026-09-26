from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from pydantic import Field, StrictBool, model_validator
from .models import Model


class Settings(Model):
    storage_path: str = ".data/memory.sqlite3"
    backend: str = "fixture"
    base_url: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    location: str = "local"
    timeout_seconds: float = Field(default=20, gt=0, le=120)
    max_attempts: int = Field(default=2, ge=1, le=2)
    total_budget_seconds: float = Field(default=45, gt=0, le=300)
    max_concurrency: int = Field(default=1, ge=1, le=4)
    max_pending_jobs: int = Field(default=64, ge=1, le=10000)
    max_queue_wait_seconds: float = Field(default=45, gt=0)
    session_idle_seconds: float = Field(default=1800, gt=0)
    max_session_turns: int = Field(default=512, ge=2, le=10000)
    max_session_chars: int = Field(default=500000, ge=16000)
    recent_ttl_seconds: float = Field(default=604800, gt=0)
    observation_ttl_seconds: float = Field(default=2592000, gt=0)
    inferred_ttl_seconds: float = Field(default=2592000, gt=0)
    fixture_delay_seconds: float = Field(default=0.05, ge=0, le=30)
    foreground_priority: StrictBool = True
    max_chronicle_chars: int = Field(default=200000, ge=1000)
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)

    @model_validator(mode="after")
    def backend_configuration(self):
        if self.backend not in {"fixture", "chat_completions"}:
            raise ValueError("unknown backend")
        if self.location not in {"local", "remote"}:
            raise ValueError("location must be local or remote")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("this release serves loopback only")
        if self.backend == "chat_completions":
            if not self.base_url or not self.model:
                raise ValueError("chat_completions requires base_url and model")
            url = urlparse(self.base_url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("base_url must be a credential-free HTTP(S) service URL")
            if self.location == "local" and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("local means loopback; use an SSH tunnel or declare remote")
        return self

    @classmethod
    def load(cls, path: str | None):
        return cls() if not path else cls.model_validate(json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class Principal:
    caller_id: str
    user_ids: frozenset[str]
    roles: frozenset[str] = frozenset({"host"})
    decision_refs: frozenset[str] = frozenset()


def configured_decision(principal: Principal, user_id: str, reference: str) -> bool:
    """Replace with the host's consent ledger adapter for real participants."""
    return user_id in principal.user_ids and reference in principal.decision_refs


DecisionVerifier = Callable[[Principal, str, str], bool]


def demo_principal() -> Principal:
    return Principal("local-demo", frozenset({"demo-elder"}), frozenset({"host", "maintenance"}), frozenset({"demo-consent", "demo-review"}))
