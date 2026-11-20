"""The agent loop, driven by a scripted stand-in for Claude so no network is needed."""

import asyncio
import hashlib
import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import AgentError, Analyst, Answer, ExecutedQuery, QuestionDeclined
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
        self.seconds_per_reply = 0.0
        self.messages = self  # so client.messages.create(...) lands on create() below

    async def create(self, **request: Any) -> SimpleNamespace:
        self.system = request["system"]
        self.sent.append(list(request["messages"]))
        await asyncio.sleep(self.seconds_per_reply)
        return self.responses.pop(0)


class FakeDatabase:
    def __init__(self, *results: QueryResult) -> None:
        self.results = list(results)
        self.seconds_per_query = 0.0

    async def run_query(self, sql: str) -> QueryResult:
        await asyncio.sleep(self.seconds_per_query)
        return self.results.pop(0)


def make_analyst(
    claude: ScriptedClaude,
    database: FakeDatabase,
    max_steps: int = 10,
    request_timeout_seconds: float = 120,
    audit_log_path: str | Path = os.devnull,
) -> Analyst:
    settings = Settings(
        _env_file=None,
        anthropic_api_key="unused",
        database_url="postgresql://unused",
        max_steps=max_steps,
        request_timeout_seconds=request_timeout_seconds,
        audit_log_path=str(audit_log_path),
    )
    return Analyst(client=claude, database=database, system_prompt="prompt", settings=settings)


def records_in(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


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


async def test_stream_yields_each_query_as_soon_as_it_finishes():
    result = QueryResult(columns=["count"], rows=[[1000]])
    query = ExecutedQuery(purpose="test", sql="SELECT count(*) FROM film", result=result)
    claude = ScriptedClaude(
        reply("tool_use", tool_use("SELECT count(*) FROM film")),
        reply("end_turn", text("There are 1,000 films.")),
    )
    stream = make_analyst(claude, FakeDatabase(result)).stream("How many films are there?")

    first = await anext(stream)

    assert first == query
    # The query is handed over before the model is asked again.
    assert len(claude.sent) == 1
    [last] = [event async for event in stream]
    assert isinstance(last, Answer)
    assert last.answer == "There are 1,000 films."
    assert last.queries == [query]


async def test_every_answer_leaves_a_record(tmp_path: Path):
    result = QueryResult(columns=["count"], rows=[[1000]])
    claude = ScriptedClaude(
        reply("tool_use", tool_use("SELECT count(*) FROM film")),
        reply("end_turn", text("There are 1,000 films.")),
    )
    analyst = make_analyst(claude, FakeDatabase(result), audit_log_path=tmp_path / "audit.jsonl")

    answer = await analyst.answer("How many films are there?")

    [record] = records_in(tmp_path / "audit.jsonl")
    # The caller gets the id of the record, so an answer can be traced back later.
    assert record["id"] == answer.id
    assert record["question"] == "How many films are there?"
    assert record["outcome"] == "answered"
    assert record["answer"] == "There are 1,000 films."
    assert record["prompt_sha256"] == hashlib.sha256(b"prompt").hexdigest()
    # The SQL is kept. The rows are not: only how many there were and a hash of them.
    assert record["queries"] == [
        {
            "purpose": "test",
            "sql": "SELECT count(*) FROM film",
            "row_count": 1,
            "truncated": False,
            "error": None,
            "rows_sha256": hashlib.sha256(b"[[1000]]").hexdigest(),
        }
    ]


async def test_no_answer_is_handed_over_when_its_record_cannot_be_written(tmp_path: Path):
    claude = ScriptedClaude(reply("end_turn", text("There are 1,000 films.")))
    unwritable = tmp_path / "no-such-folder" / "audit.jsonl"
    stream = make_analyst(claude, FakeDatabase(), audit_log_path=unwritable).stream("How many?")

    with pytest.raises(FileNotFoundError):
        await anext(stream)


async def test_a_run_without_an_answer_leaves_a_record_too(tmp_path: Path):
    result = QueryResult(columns=["a"], rows=[[1]])
    claude = ScriptedClaude(
        reply("tool_use", tool_use("SELECT 1")),
        reply("refusal"),
    )
    analyst = make_analyst(claude, FakeDatabase(result), audit_log_path=tmp_path / "audit.jsonl")

    with pytest.raises(QuestionDeclined):
        await analyst.answer("Anything")

    [record] = records_in(tmp_path / "audit.jsonl")
    assert record["outcome"] == "declined"
    assert record["answer"] is None
    assert len(record["queries"]) == 1


async def test_a_failed_run_is_recorded_with_its_reason(tmp_path: Path):
    claude = ScriptedClaude(reply("max_tokens", text("The answer is probably")))
    analyst = make_analyst(claude, FakeDatabase(), audit_log_path=tmp_path / "audit.jsonl")

    with pytest.raises(AgentError):
        await analyst.answer("Anything")

    [record] = records_in(tmp_path / "audit.jsonl")
    assert record["outcome"] == "failed"
    assert "max_tokens" in record["detail"]


async def test_a_caller_who_leaves_early_is_recorded_as_cancelled(tmp_path: Path):
    result = QueryResult(columns=["a"], rows=[[1]])
    claude = ScriptedClaude(reply("tool_use", tool_use("SELECT 1")))
    analyst = make_analyst(claude, FakeDatabase(result), audit_log_path=tmp_path / "audit.jsonl")
    stream = analyst.stream("Anything")

    await anext(stream)
    await stream.aclose()

    [record] = records_in(tmp_path / "audit.jsonl")
    assert record["outcome"] == "cancelled"
    assert len(record["queries"]) == 1


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


async def test_a_slow_model_is_stopped_at_the_time_limit_for_the_question():
    claude = ScriptedClaude(reply("end_turn", text("Too late.")))
    claude.seconds_per_reply = 5
    analyst = make_analyst(claude, FakeDatabase(), request_timeout_seconds=0.05)

    with pytest.raises(AgentError, match="time limit"):
        await analyst.answer("Anything")


async def test_slow_queries_count_towards_the_same_time_limit():
    result = QueryResult(columns=["a"], rows=[[1]])
    claude = ScriptedClaude(
        reply("tool_use", tool_use("SELECT 1", "call_1"), tool_use("SELECT 2", "call_2")),
        reply("end_turn", text("Too late.")),
    )
    database = FakeDatabase(result, result)
    database.seconds_per_query = 0.04
    analyst = make_analyst(claude, database, request_timeout_seconds=0.06)

    # Each query alone fits in the limit. The two together do not.
    with pytest.raises(AgentError, match="time limit"):
        await analyst.answer("Anything")

    assert len(claude.sent) == 1


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
