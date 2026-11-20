"""The HTTP layer, with the analyst replaced so neither Postgres nor Claude is needed."""

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError, Answer, ExecutedQuery
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


def test_an_empty_question_is_rejected(client: TestClient):
    use_analyst(Answer(answer="unused", queries=[]))

    response = client.post("/ask", json={"question": ""})

    assert response.status_code == 422


def test_a_failed_run_is_reported_as_bad_gateway(client: TestClient):
    use_analyst(AgentError("No answer after 10 steps."))

    response = client.post("/ask", json={"question": "Something hard"})

    assert response.status_code == 502
    assert response.json() == {"detail": "No answer after 10 steps."}
