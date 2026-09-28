from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Isolated from legacy .env, which may configure a live external backend.
    model_config = SettingsConfigDict(
        env_file=".env.task2", env_file_encoding="utf-8", extra="ignore"
    )
    semantic_backend: Literal["mock", "qwen"] = "mock"
    qwen_base_url: str = "http://127.0.0.1:8000/v1"
    qwen_model: str = ""
    qwen_api_key: str = "EMPTY"
    qwen_timeout_seconds: float = Field(default=30, gt=0)
    triage_guardrails_enabled: bool = True

    @model_validator(mode="after")
    def require_model(self):
        if self.semantic_backend == "qwen" and not self.qwen_model.strip():
            raise ValueError("QWEN_MODEL is required for SEMANTIC_BACKEND=qwen")
        return self
