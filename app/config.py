"""Runtime settings, read from environment variables or a local .env file."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Both are required, so a missing value stops the service at startup
    # and not on the first question.
    anthropic_api_key: str
    database_url: str

    anthropic_model: str = "claude-opus-5-5"
    # How hard the model thinks before it acts.
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    # One line is appended here for every question. See app/audit.py.
    audit_log_path: str = "audit.jsonl"

    # Limits that keep one question from running away with time, tokens or memory.
    max_steps: int = 10
    max_rows: int = 100
    max_result_chars: int = 40_000
    statement_timeout_ms: int = 15_000
    # For one whole question: every model call and every query together.
    request_timeout_seconds: float = 120
