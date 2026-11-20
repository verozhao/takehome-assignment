"""HTTP API. POST /ask takes a question in plain English and returns the answer
together with the SQL that produced it."""

import logging
from contextlib import asynccontextmanager
from typing import Annotated

import anthropic
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.agent import AgentError, Analyst, Answer
from app.config import Settings
from app.database import Database
from app.prompt import build_system_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# The SDK's HTTP client logs every request. The agent already logs each step.
logging.getLogger("httpx2").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    database = await Database.connect(settings)
    # The schema is read once. Restart the service after a schema change.
    schema = await database.describe_schema()
    app.state.database = database
    app.state.schema = schema
    app.state.analyst = Analyst(
        client=anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
        database=database,
        system_prompt=build_system_prompt(schema, settings),
        settings=settings,
    )
    yield
    await database.close()


app = FastAPI(title="Ask your database", lifespan=lifespan)


class Question(BaseModel):
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


@app.exception_handler(AgentError)
async def agent_error(request: Request, exc: AgentError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(anthropic.APIError)
async def claude_api_error(request: Request, exc: anthropic.APIError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": f"Claude API error: {exc.message}"})
