"""The HTTP layer. The analyst is replaced, so neither Postgres nor Claude is needed,
except in the last test, which starts the real service against Pagila."""

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError, Answer, ExecutedQuery, QuestionDeclined
from app.database import QueryResult
from app.main import app, get_analyst


class FakeAnalyst:
    def __init__(self, outcome: Answer | Exception):
        self.outcome = outcome

    async def answer(self, question: str) -> Answer:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def client():
    # No `with` block, so the startup code that connects to Postgres does not run.
    yield TestClient(app)
    app.dependency_overrides.clear()


def use_analyst(outcome: Answer | Exception) -> None:
    app.dependency_overrides[get_analyst] = lambda: FakeAnalyst(outcome)


def test_ask_returns_the_answer_and_the_queries_behind_it(client: TestClient):
    query = ExecutedQuery(
        purpose="Count the films.",
        sql="SELECT count(*) FROM film",
        result=QueryResult(columns=["count"], rows=[[1000]]),
    )
    use_analyst(Answer(answer="There are 1,000 films.", queries=[query]))

    response = client.post("/ask", json={"question": "How many films are there?"})

    assert response.status_code == 200
    assert response.json() == {
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


def test_the_service_starts_and_gives_the_model_the_schema(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")  # startup makes no call to Claude

    with TestClient(app) as started:
        assert started.get("/health").json() == {"status": "ok"}
        schema = started.get("/schema").text
        assert "table film\n" in schema
        assert schema in app.state.analyst.system_prompt
