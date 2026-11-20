"""The question-answering loop.

Claude decides which SQL to run, we run it and hand back the result, and this
repeats until Claude has enough to answer.
"""

import logging

from anthropic import AsyncAnthropic
from anthropic.types import Message, MessageParam, ToolParam, ToolResultBlockParam
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


class AgentError(Exception):
    """The model stopped without producing an answer."""


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
    ):
        self.client = client
        self.database = database
        self.system_prompt = system_prompt
        self.settings = settings

    async def answer(self, question: str) -> Answer:
        messages: list[MessageParam] = [{"role": "user", "content": question}]
        queries: list[ExecutedQuery] = []

        for step in range(1, self.settings.max_steps + 1):
            response = await self.client.messages.create(
                model=self.settings.anthropic_model,
                max_tokens=MAX_TOKENS,
                # The schema makes this prompt long and it never changes, so cache it.
                system=[
                    {
                        "type": "text",
                        "text": self.system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[RUN_SQL_TOOL],
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self.settings.reasoning_effort},
                # Also cache the conversation so far, which is resent on every step.
                cache_control={"type": "ephemeral"},
            )
            log_step(step, response)

            if response.stop_reason == "end_turn":
                text = "".join(block.text for block in response.content if block.type == "text")
                return Answer(answer=text, queries=queries)
            if response.stop_reason != "tool_use":
                # max_tokens or refusal: there is no trustworthy answer to return.
                raise AgentError(f"The model stopped early ({response.stop_reason}).")

            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[ToolResultBlockParam] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                sql = block.input["sql"]
                result = await self.database.run_query(sql)
                logger.info("run_sql: %s -> %s", sql, result.error or f"{len(result.rows)} rows")
                queries.append(
                    ExecutedQuery(purpose=block.input["purpose"], sql=sql, result=result)
                )
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
