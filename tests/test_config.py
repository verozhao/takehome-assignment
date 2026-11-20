"""The settings. A value that would leave the service unprotected stops it at startup."""

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.fixture(autouse=True)
def required_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")


def test_api_keys_are_read_as_a_mapping_of_key_to_caller(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("API_KEYS", '{"a-long-random-key": "alice"}')

    assert Settings().api_keys == {"a-long-random-key": "alice"}


@pytest.mark.parametrize(
    "api_keys",
    [
        '{"": "alice"}',  # what a deploy template gives when its variable is not set
        '{"too-short": "alice"}',
        '{"a-long-random-kéy": "alice"}',  # cannot arrive intact in an HTTP header
    ],
)
def test_api_keys_that_cannot_protect_the_service_stop_it(
    monkeypatch: pytest.MonkeyPatch, api_keys: str
):
    monkeypatch.setenv("API_KEYS", api_keys)

    with pytest.raises(ValidationError) as failure:
        Settings()

    # The message names the rule and prints none of the configured values.
    assert "at least 16 ASCII characters" in str(failure.value)
    assert "alice" not in str(failure.value)
