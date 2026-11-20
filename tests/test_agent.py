"""The agent loop, driven by a scripted stand-in for Claude so no network is needed."""

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import AgentError, Analyst, ExecutedQuery, QuestionDeclined
from app.config import Settings
from app.database import QueryResult


def thinking() -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking="", signature="signature")


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def tool_use(sql: str, call_id: str = "call_1") -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use", id=call_id, name="run_sql", input={"purpose": "test", "sql": sql}
    )


def reply(stop_reason: str, *blocks: SimpleNamespace) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0, output_tokens=0
    )
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), usage=usage)


class ScriptedClaude:
    """Plays back prepared responses and records the messages it was sent."""

    def __init__(self, *responses: SimpleNamespace) -> None:
        self.responses = list(responses)
        self.sent: list[list] = []
        self.system: list[dict] = []
        self.messages = self  # so client.messages.create(...) lands on create() below

    async def create(self, **request: Any) -> SimpleNamespace:
        self.system = request["system"]
        self.sent.append(list(request["messages"]))
        return self.responses.pop(0)


class FakeDatabase:
    def __init__(self, *results: QueryResult) -> None:
        self.results = list(results)

    async def run_query(self, sql: str) -> QueryResult:
        return self.results.pop(0)


def make_analyst(claude: ScriptedClaude, database: FakeDatabase, max_steps: int = 10) -> Analyst:
    settings = Settings(
        _env_file=None,
        anthropic_api_key="unused",
        database_url="postgresql://unused",
        max_steps=max_steps,
    )
    return Analyst(client=claude, database=database, system_prompt="prompt", settings=settings)


async def test_runs_the_query_and_returns_the_answer_with_its_trace():
    result = QueryResult(columns=["count"], rows=[[1000]])
    # A real reply mixes thinking, text and tool calls, so the script does too.
    claude = ScriptedClaude(
        reply("tool_use", thinking(), text("Let me count."), tool_use("SELECT count(*) FROM film")),
        reply("end_turn", thinking(), text("There are 1,000 films.")),
    )

    answer = await make_analyst(claude, FakeDatabase(result)).answer("How many films are there?")

    assert answer.answer == "There are 1,000 films."
    assert answer.queries == [
        ExecutedQuery(purpose="test", sql="SELECT count(*) FROM film", result=result)
    ]
    # The second request carries the query result back to the model.
    tool_result = claude.sent[1][-1]["content"][0]
    assert tool_result["tool_use_id"] == "call_1"
    assert tool_result["content"] == result.as_tool_result()
    assert tool_result["is_error"] is False


async def test_the_model_is_told_todays_date():
    claude = ScriptedClaude(reply("end_turn", text("It is a weekday.")))

    await make_analyst(claude, FakeDatabase()).answer("What day is it?")

    assert date.today().isoformat() in claude.system[-1]["text"]


async def test_sql_error_goes_back_to_the_model_so_it_can_retry():
    failed = QueryResult(error='column "nme" does not exist')
    fixed = QueryResult(columns=["first_name"], rows=[["ED"]])
    claude = ScriptedClaude(
        reply("tool_use", tool_use("SELECT nme FROM actor")),
        reply("tool_use", tool_use("SELECT first_name FROM actor", "call_2")),
        reply("end_turn", text("ED")),
    )

    answer = await make_analyst(claude, FakeDatabase(failed, fixed)).answer("Name an actor")

    tool_result = claude.sent[1][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "does not exist" in tool_result["content"]
    # The failed attempt stays in the trace.
    assert [query.result for query in answer.queries] == [failed, fixed]


async def test_queries_requested_in_one_turn_are_answered_in_one_message():
    first = QueryResult(columns=["a"], rows=[[1]])
    second = QueryResult(columns=["a"], rows=[[2]])
    claude = ScriptedClaude(
        reply("tool_use", tool_use("SELECT 1", "call_1"), tool_use("SELECT 2", "call_2")),
        reply("end_turn", text("done")),
    )

    await make_analyst(claude, FakeDatabase(first, second)).answer("Two things")

    tool_results = claude.sent[1][-1]["content"]
    assert [item["tool_use_id"] for item in tool_results] == ["call_1", "call_2"]


async def test_gives_up_when_the_step_limit_is_reached():
    result = QueryResult(columns=["a"], rows=[[1]])
    claude = ScriptedClaude(
        reply("tool_use", tool_use("SELECT 1")),
        reply("tool_use", tool_use("SELECT 1")),
    )

    with pytest.raises(AgentError, match="No answer after 2 steps"):
        await make_analyst(claude, FakeDatabase(result, result), max_steps=2).answer("Loop")

    assert len(claude.sent) == 2


async def test_a_response_cut_off_by_the_token_limit_is_an_error_and_never_an_answer():
    claude = ScriptedClaude(reply("max_tokens", text("The answer is probably")))

    with pytest.raises(AgentError, match="max_tokens"):
        await make_analyst(claude, FakeDatabase()).answer("Anything")


async def test_a_final_turn_without_text_is_an_error():
    claude = ScriptedClaude(reply("end_turn", thinking()))

    with pytest.raises(AgentError, match="no answer"):
        await make_analyst(claude, FakeDatabase()).answer("Anything")


async def test_a_refusal_is_reported_as_a_declined_question():
    claude = ScriptedClaude(reply("refusal"))

    with pytest.raises(QuestionDeclined):
        await make_analyst(claude, FakeDatabase()).answer("Anything")
