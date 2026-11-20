"""The question-answering loop.

Claude decides which SQL to run, we run it and hand back the result, and this
repeats until Claude has enough to answer.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Sequence
from datetime import date, datetime
from typing import Literal, TypeVar

from anthropic import AsyncAnthropic
from anthropic.types import (
    Message,
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)
from pydantic import BaseModel

from app import audit
from app.config import Settings
from app.database import Database, QueryResult

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_TOKENS = 16_000

RUN_SQL_TOOL: ToolParam = {
    "name": "run_sql",
    "description": (
        "Run one read-only SQL query against the PostgreSQL database and get the rows back "
        "as JSON. Use it to look at the data and to compute the figures for your answer. "
        "Only a single statement that returns rows is accepted. Errors are returned as text "
        "so you can correct the query."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "purpose": {
                "type": "string",
                "description": "One sentence on what this query is meant to find out.",
            },
            "sql": {"type": "string", "description": "A single PostgreSQL SELECT statement."},
        },
        "required": ["purpose", "sql"],
        "additionalProperties": False,
    },
    "strict": True,
}


class RunSqlInput(BaseModel):
    """What the model passes to run_sql. The SDK hands it over as an untyped dict."""

    purpose: str
    sql: str


class AgentError(Exception):
    """The model stopped without producing an answer."""


class QuestionDeclined(AgentError):
    """The model refused the question itself."""


class ExecutedQuery(BaseModel):
    """A query the model ran and what came back. Together they are the audit trail of an answer."""

    purpose: str
    sql: str
    result: QueryResult


class Answer(BaseModel):
    # The id of the stored record of this answer. Set when the answer is handed over.
    id: str = ""
    answer: str
    queries: list[ExecutedQuery]


class Analyst:
    def __init__(
        self, client: AsyncAnthropic, database: Database, system_prompt: str, settings: Settings
    ) -> None:
        self.client = client
        self.database = database
        self.system_prompt = system_prompt
        self.settings = settings

    async def answer(
        self,
        question: str,
        caller: str | None = None,
        earlier: Sequence[audit.AnswerRecord] = (),
    ) -> Answer:
        """Work through the question and return the final answer."""
        async for event in self.stream(question, caller, earlier):
            if isinstance(event, Answer):
                return event
        # stream() always ends with an Answer or raises, so this line is never reached.
        raise AgentError("The loop ended without an answer.")

    async def stream(
        self,
        question: str,
        caller: str | None = None,
        earlier: Sequence[audit.AnswerRecord] = (),
    ) -> AsyncGenerator[ExecutedQuery | Answer, None]:
        """Work through the question, yielding each query as it finishes and the answer last.

        `earlier` holds the answers this question follows up on, oldest first.

        Every run leaves one record in the audit file, however it ends. The record of an
        answer is written before the answer is handed over, so no answer exists without one.
        """
        record_id = uuid.uuid4().hex
        # Local time with its offset, so the record shows the date the model was told.
        asked_at = datetime.now().astimezone()
        started = time.monotonic()
        queries: list[audit.QueryRecord] = []
        answer: Answer | None = None
        # What the record says if the caller goes away before the run ends.
        outcome: Literal["answered", "declined", "failed", "cancelled"] = "cancelled"
        detail: str | None = None
        try:
            async for event in self.work(question, earlier):
                if isinstance(event, Answer):
                    # Held back until its record is written, below.
                    answer, outcome = event, "answered"
                else:
                    queries.append(
                        audit.QueryRecord(
                            purpose=event.purpose,
                            sql=event.sql,
                            row_count=len(event.result.rows),
                            truncated=event.result.truncated,
                            error=event.result.error,
                            rows_sha256=audit.sha256(json.dumps(event.result.rows)),
                        )
                    )
                    yield event
        except QuestionDeclined as exc:
            outcome, detail = "declined", str(exc)
            raise
        except Exception as exc:
            outcome, detail = "failed", str(exc)
            raise
        finally:
            audit.append(
                self.settings.audit_log_path,
                audit.AnswerRecord(
                    id=record_id,
                    asked_at=asked_at,
                    seconds=round(time.monotonic() - started, 2),
                    caller=caller,
                    question=question,
                    follow_up_to=earlier[-1].id if earlier else None,
                    model=self.settings.anthropic_model,
                    reasoning_effort=self.settings.reasoning_effort,
                    prompt_sha256=audit.sha256(self.system_prompt),
                    queries=queries,
                    outcome=outcome,
                    detail=detail,
                    answer=answer.answer if answer else None,
                ),
            )
        if answer:
            answer.id = record_id
            yield answer

    async def work(
        self, question: str, earlier: Sequence[audit.AnswerRecord] = ()
    ) -> AsyncGenerator[ExecutedQuery | Answer, None]:
        """The loop itself: ask the model, run the queries it wants, repeat."""
        # A follow-up starts from the earlier questions and answers, rebuilt from their records.
        messages: list[MessageParam] = []
        for turn in earlier:
            messages.extend(as_messages(turn))
        messages.append({"role": "user", "content": question})
        queries: list[ExecutedQuery] = []
        system: list[TextBlockParam] = [
            # The schema makes this prompt long and it never changes, so cache it.
            {"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}},
            # The model does not know what day it is. The date sits outside the cached
            # block, so the cache survives the date changing.
            {"type": "text", "text": f"Today's date is {date.today().isoformat()}."},
        ]

        # One deadline for the whole question, on the event loop's clock.
        deadline = asyncio.get_running_loop().time() + self.settings.request_timeout_seconds

        for step in range(1, self.settings.max_steps + 1):
            response = await before(
                deadline,
                self.client.messages.create(
                    model=self.settings.anthropic_model,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    tools=[RUN_SQL_TOOL],
                    messages=messages,
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.settings.reasoning_effort},
                    # Also cache the conversation so far, which is resent on every step.
                    cache_control={"type": "ephemeral"},
                ),
            )
            log_step(step, response)

            if response.stop_reason == "refusal":
                raise QuestionDeclined("The model declined to answer this question.")
            if response.stop_reason == "end_turn":
                text = "".join(block.text for block in response.content if block.type == "text")
                if not text.strip():
                    raise AgentError("The model returned no answer.")
                yield Answer(answer=text, queries=queries)
                return
            if response.stop_reason != "tool_use":
                # Cut off by max_tokens: there is no trustworthy answer to return.
                raise AgentError(f"The model stopped early ({response.stop_reason}).")

            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[ToolResultBlockParam] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                call = RunSqlInput.model_validate(block.input)
                result = await before(deadline, self.database.run_query(call.sql))
                logger.info(
                    "run_sql: %s -> %s", call.sql, result.error or f"{len(result.rows)} rows"
                )
                executed = ExecutedQuery(purpose=call.purpose, sql=call.sql, result=result)
                queries.append(executed)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.as_tool_result(),
                        "is_error": result.error is not None,
                    }
                )
                yield executed
            # Every result of this turn goes back in one message.
            messages.append({"role": "user", "content": tool_results})

        raise AgentError(f"No answer after {self.settings.max_steps} steps.")


# Stands in for the rows of an earlier query. A record keeps the SQL and not the rows.
ROWS_NOT_KEPT = "The rows of this result were not kept. Run the query again if you need them."


def as_messages(turn: audit.AnswerRecord) -> list[MessageParam]:
    """One earlier question as the model sees it again: the question, the queries that
    ran and the answer. The rows are missing, so a follow-up queries again for its figures."""
    # Only an answered question can be followed up on. The HTTP layer checks that.
    assert turn.answer is not None
    calls: list[ToolUseBlockParam] = []
    results: list[ToolResultBlockParam] = []
    for number, query in enumerate(turn.queries):
        if query.error is None:
            call_id = f"{turn.id}_{number}"
            call = RunSqlInput(purpose=query.purpose, sql=query.sql)
            calls.append(
                {"type": "tool_use", "id": call_id, "name": "run_sql", "input": call.model_dump()}
            )
            results.append(
                {"type": "tool_result", "tool_use_id": call_id, "content": ROWS_NOT_KEPT}
            )
    messages: list[MessageParam] = [{"role": "user", "content": turn.question}]
    if calls:
        messages.append({"role": "assistant", "content": calls})
        messages.append({"role": "user", "content": results})
    messages.append({"role": "assistant", "content": turn.answer})
    return messages


async def before(deadline: float, work: Awaitable[T]) -> T:
    """Wait for `work`, but never past the deadline for the whole question."""
    try:
        async with asyncio.timeout_at(deadline):
            return await work
    except TimeoutError:
        raise AgentError("The time limit for one question was reached.") from None


def log_step(step: int, response: Message) -> None:
    usage = response.usage
    logger.info(
        "step %d: %s (tokens: %d input, %d cache write, %d cache read, %d output)",
        step,
        response.stop_reason,
        usage.input_tokens,
        usage.cache_creation_input_tokens or 0,
        usage.cache_read_input_tokens or 0,
        usage.output_tokens,
    )
