"""The HTTP layer. The analyst is replaced, so neither Postgres nor Claude is needed,
except in the last two tests, which start the real service against Pagila."""

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError, Answer, ExecutedQuery, QuestionDeclined
from app.database import QueryResult
from app.main import app, get_analyst


class FakeAnalyst:
    def __init__(self, outcome: Answer | Exception) -> None:
        self.outcome = outcome
        self.callers: list[str | None] = []

    async def answer(self, question: str, caller: str | None = None) -> Answer:
        self.callers.append(caller)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def stream(
        self, question: str, caller: str | None = None
    ) -> AsyncIterator[ExecutedQuery | Answer]:
        self.callers.append(caller)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        for query in self.outcome.queries:
            yield query
        yield self.outcome


@pytest.fixture
def client() -> Iterator[TestClient]:
    # No `with` block, so the startup code that connects to Postgres does not run.
    app.state.api_keys = {}
    yield TestClient(app)
    app.dependency_overrides.clear()


def use_analyst(outcome: Answer | Exception) -> FakeAnalyst:
    analyst = FakeAnalyst(outcome)
    app.dependency_overrides[get_analyst] = lambda: analyst
    return analyst


def test_ask_returns_the_answer_and_the_queries_behind_it(client: TestClient):
    query = ExecutedQuery(
        purpose="Count the films.",
        sql="SELECT count(*) FROM film",
        result=QueryResult(columns=["count"], rows=[[1000]]),
    )
    use_analyst(Answer(id="record-1", answer="There are 1,000 films.", queries=[query]))

    response = client.post("/ask", json={"question": "How many films are there?"})

    assert response.status_code == 200
    assert response.json() == {
        "id": "record-1",
        "answer": "There are 1,000 films.",
        "queries": [
            {
                "purpose": "Count the films.",
                "sql": "SELECT count(*) FROM film",
                "result": {
                    "columns": ["count"],
                    "rows": [[1000]],
                    "truncated": False,
                    "error": None,
                },
            }
        ],
    }


def test_stream_sends_each_query_and_then_the_answer(client: TestClient):
    query = ExecutedQuery(
        purpose="Count the films.",
        sql="SELECT count(*) FROM film",
        result=QueryResult(columns=["count"], rows=[[1000]]),
    )
    use_analyst(Answer(answer="There are 1,000 films.", queries=[query]))

    response = client.post("/ask/stream", json={"question": "How many films are there?"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [event.split("\ndata: ") for event in response.text.strip().split("\n\n")]
    assert [name for name, _ in events] == ["event: started", "event: query", "event: answer"]
    assert json.loads(events[1][1])["purpose"] == "Count the films."
    assert json.loads(events[2][1])["answer"] == "There are 1,000 films."


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (AgentError("No answer after 10 steps."), 502),
        (QuestionDeclined("The model declined to answer this question."), 422),
    ],
)
def test_stream_reports_a_failure_as_an_event(client: TestClient, failure: AgentError, status: int):
    use_analyst(failure)

    response = client.post("/ask/stream", json={"question": "Something hard"})

    # The HTTP status is already sent when the failure happens, so it arrives as an event
    # that carries the status the plain endpoint would have returned.
    assert response.status_code == 200
    last_event = response.text.strip().split("\n\n")[-1]
    assert (
        last_event
        == f"event: error\ndata: {json.dumps({'status': status, 'detail': str(failure)})}"
    )


def test_a_request_without_a_key_never_matches_an_empty_configured_key(client: TestClient):
    # The settings refuse an empty key. This guards the comparison itself.
    app.state.api_keys = {"": "alice"}
    analyst = use_analyst(Answer(answer="unused", queries=[]))

    no_key = client.post("/ask", json={"question": "Anything"})
    empty_key = client.get("/schema", headers={"Authorization": "Bearer "})

    assert [no_key.status_code, empty_key.status_code] == [401, 401]
    assert analyst.callers == []


def test_the_stream_endpoint_identifies_the_caller_too(client: TestClient):
    app.state.api_keys = {"key-for-bob": "bob"}
    analyst = use_analyst(Answer(answer="There are 1,000 films.", queries=[]))

    response = client.post(
        "/ask/stream",
        json={"question": "How many films are there?"},
        headers={"Authorization": "Bearer key-for-bob"},
    )

    assert response.status_code == 200
    assert analyst.callers == ["bob"]


def test_with_api_keys_set_a_request_without_a_valid_key_is_turned_away(client: TestClient):
    app.state.api_keys = {"key-for-alice": "alice"}
    analyst = use_analyst(Answer(answer="unused", queries=[]))
    question = {"question": "Anything"}

    no_key = client.post("/ask", json=question)
    wrong_key = client.post("/ask", json=question, headers={"Authorization": "Bearer nope"})
    stream = client.post("/ask/stream", json=question)
    schema = client.get("/schema")

    assert [r.status_code for r in (no_key, wrong_key, stream, schema)] == [401, 401, 401, 401]
    assert no_key.headers["www-authenticate"] == "Bearer"
    # Nobody without a key gets as far as the model.
    assert analyst.callers == []


def test_a_valid_api_key_identifies_the_caller(client: TestClient):
    app.state.api_keys = {"key-for-alice": "alice", "key-for-bob": "bob"}
    analyst = use_analyst(Answer(answer="There are 1,000 films.", queries=[]))

    response = client.post(
        "/ask",
        json={"question": "How many films are there?"},
        headers={"Authorization": "Bearer key-for-bob"},
    )

    assert response.status_code == 200
    assert analyst.callers == ["bob"]


@pytest.mark.parametrize("question", ["", "  \n "])
def test_an_empty_question_is_rejected(client: TestClient, question: str):
    use_analyst(Answer(answer="unused", queries=[]))

    response = client.post("/ask", json={"question": question})

    assert response.status_code == 422


def test_a_declined_question_is_the_callers_problem(client: TestClient):
    use_analyst(QuestionDeclined("The model declined to answer this question."))

    response = client.post("/ask", json={"question": "Something it will not touch"})

    assert response.status_code == 422
    assert response.json() == {"detail": "The model declined to answer this question."}


def test_a_failed_run_is_reported_as_bad_gateway(client: TestClient):
    use_analyst(AgentError("No answer after 10 steps."))

    response = client.post("/ask", json={"question": "Something hard"})

    assert response.status_code == 502
    assert response.json() == {"detail": "No answer after 10 steps."}


def test_a_claude_api_failure_is_bad_gateway_without_upstream_details(client: TestClient):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    use_analyst(anthropic.APIConnectionError(message="secret upstream detail", request=request))

    response = client.post("/ask", json={"question": "Anything"})

    assert response.status_code == 502
    assert "secret upstream detail" not in response.text


def test_the_service_refuses_to_start_when_it_cannot_keep_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "no-such-folder" / "audit.jsonl"))

    with pytest.raises(FileNotFoundError), TestClient(app):
        pass


def test_the_service_starts_and_gives_the_model_the_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")  # startup makes no call to Claude
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("API_KEYS", "{}")  # whatever the local .env says, login is off here

    with TestClient(app) as started:
        assert started.get("/health").json() == {"status": "ok"}
        schema = started.get("/schema").text
        assert "table film\n" in schema
        assert schema in app.state.analyst.system_prompt


def test_api_keys_from_the_environment_turn_login_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("API_KEYS", '{"a-long-key-for-alice": "alice"}')

    with TestClient(app) as started:
        assert started.get("/health").status_code == 200
        assert started.get("/schema").status_code == 401
        with_key = started.get("/schema", headers={"Authorization": "Bearer a-long-key-for-alice"})
        assert with_key.status_code == 200
