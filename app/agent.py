"""The question-answering loop.

Claude decides which SQL to run, we run it and hand back the result, and this
repeats until Claude has enough to answer.
"""

import logging
from datetime import date

from anthropic import AsyncAnthropic
from anthropic.types import (
    Message,
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
)
from pydantic import BaseModel

from app.config import Settings
from app.database import Database, QueryResult

logger = logging.getLogger(__name__)

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

    async def answer(self, question: str) -> Answer:
        messages: list[MessageParam] = [{"role": "user", "content": question}]
        queries: list[ExecutedQuery] = []
        system: list[TextBlockParam] = [
            # The schema makes this prompt long and it never changes, so cache it.
            {"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}},
            # The model does not know what day it is. The date sits outside the cached
            # block, so the cache survives the date changing.
            {"type": "text", "text": f"Today's date is {date.today().isoformat()}."},
        ]

        for step in range(1, self.settings.max_steps + 1):
            response = await self.client.messages.create(
                model=self.settings.anthropic_model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=[RUN_SQL_TOOL],
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self.settings.reasoning_effort},
                # Also cache the conversation so far, which is resent on every step.
                cache_control={"type": "ephemeral"},
            )
            log_step(step, response)

            if response.stop_reason == "refusal":
                raise QuestionDeclined("The model declined to answer this question.")
            if response.stop_reason == "end_turn":
                text = "".join(block.text for block in response.content if block.type == "text")
                if not text.strip():
                    raise AgentError("The model returned no answer.")
                return Answer(answer=text, queries=queries)
            if response.stop_reason != "tool_use":
                # Cut off by max_tokens: there is no trustworthy answer to return.
                raise AgentError(f"The model stopped early ({response.stop_reason}).")

            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[ToolResultBlockParam] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                call = RunSqlInput.model_validate(block.input)
                result = await self.database.run_query(call.sql)
                logger.info(
                    "run_sql: %s -> %s", call.sql, result.error or f"{len(result.rows)} rows"
                )
                queries.append(ExecutedQuery(purpose=call.purpose, sql=call.sql, result=result))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.as_tool_result(),
                        "is_error": result.error is not None,
                    }
                )
            # Every result of this turn goes back in one message.
            messages.append({"role": "user", "content": tool_results})

        raise AgentError(f"No answer after {self.settings.max_steps} steps.")


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
