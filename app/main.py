"""HTTP API. POST /ask takes a question in plain English and returns the answer
together with the SQL that produced it."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import anthropic
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agent import AgentError, Analyst, Answer, QuestionDeclined
from app.config import Settings
from app.database import Database
from app.prompt import build_system_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# The SDK's HTTP client logs every request. The agent already logs each step.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    database = await Database.connect(settings)
    # The schema is read once. Restart the service after a schema change.
    schema = await database.describe_schema()
    app.state.database = database
    app.state.schema = schema
    app.state.analyst = Analyst(
        client=anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key, timeout=settings.model_timeout_seconds
        ),
        database=database,
        system_prompt=build_system_prompt(schema, settings),
        settings=settings,
    )
    yield
    await database.close()


app = FastAPI(title="Ask your database", lifespan=lifespan)


class Question(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(
        min_length=1, max_length=2000, examples=["Which table has the most rows?"]
    )


def get_analyst(request: Request) -> Analyst:
    return request.app.state.analyst


@app.post("/ask")
async def ask(body: Question, analyst: Annotated[Analyst, Depends(get_analyst)]) -> Answer:
    return await analyst.answer(body.question)


@app.get("/schema", response_class=PlainTextResponse)
async def schema(request: Request) -> str:
    """The schema text exactly as the model sees it."""
    return request.app.state.schema


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    await request.app.state.database.pool.fetchval("SELECT 1")
    return {"status": "ok"}


@app.exception_handler(QuestionDeclined)
async def question_declined(request: Request, exc: QuestionDeclined) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(AgentError)
async def agent_error(request: Request, exc: AgentError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(anthropic.APIError)
async def claude_api_error(request: Request, exc: anthropic.APIError) -> JSONResponse:
    # The SDK's message quotes the upstream response, so it goes to the log and not to the caller.
    logger.error("Claude API call failed: %s", exc)
    return JSONResponse(
        status_code=502,
        content={"detail": "The request to the language model failed. See the service log."},
    )
