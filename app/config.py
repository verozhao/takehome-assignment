"""Runtime settings, read from environment variables or a local .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required, so that a missing key stops the service at startup and not on the first question.
    anthropic_api_key: str
    anthropic_model: str = "claude-opus-5-5"
    # How hard the model thinks before it acts: low, medium or high.
    reasoning_effort: str = "high"

    # The read-only role from sql/readonly_role.sql.
    database_url: str = "postgresql://analyst:analyst@localhost:5432/postgres"

    # Limits that keep one question from running away with time, tokens or memory.
    max_steps: int = 10
    max_rows: int = 100
    max_result_chars: int = 40_000
    statement_timeout_ms: int = 15_000
