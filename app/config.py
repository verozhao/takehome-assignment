"""Runtime settings, read from environment variables or a local .env file."""

from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_API_KEY_LENGTH = 16


class Settings(BaseSettings):
    # The settings hold secrets, so a failed check must not print the values it was given.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # Both are required, so a missing value stops the service at startup
    # and not on the first question.
    anthropic_api_key: str
    database_url: str
    # A text file of reviewed definitions for terms the schema cannot explain.
    glossary_path: str | None = None

    anthropic_model: str = "claude-opus-5-5"
    # How hard the model thinks before it acts.
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    # API keys and the caller each one identifies, as JSON: {"a-long-random-key": "alice"}.
    # Left empty, the service is open to anyone who can reach it.
    api_keys: dict[str, str] = {}

    # One line is appended here for every question. See app/audit.py.
    audit_log_path: str = "audit.jsonl"

    # Limits that keep one question from running away with time, tokens or memory.
    max_steps: int = 10
    max_rows: int = 100
    max_result_chars: int = 40_000
    statement_timeout_ms: int = 15_000
    # For one whole question: every model call and every query together.
    request_timeout_seconds: float = 120

    @field_validator("api_keys")
    @classmethod
    def keys_can_protect_the_service(cls, api_keys: dict[str, str]) -> dict[str, str]:
        # An empty key is what a deploy template produces when its variable is not set.
        # A key outside ASCII cannot arrive intact in an HTTP header.
        if any(len(key) < MIN_API_KEY_LENGTH or not key.isascii() for key in api_keys):
            raise ValueError(f"every API key needs at least {MIN_API_KEY_LENGTH} ASCII characters")
        return api_keys
