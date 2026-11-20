"""HTTP API. POST /ask takes a question in plain English and returns the answer
together with the SQL that produced it. POST /ask/stream sends the same thing
step by step, so a client can show the work while it waits."""

import json
import logging
import secrets
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Annotated

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from app import audit
from app.agent import AgentError, Analyst, Answer, ExecutedQuery, QuestionDeclined
from app.config import Settings
from app.database import Database
from app.prompt import build_system_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# The SDK's HTTP client logs every request. The agent already logs each step.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# The SDK's own message quotes the upstream response, so callers get this and the log gets the rest.
CLAUDE_FAILURE = "The request to the language model failed. See the service log."


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    audit.ensure_writable(settings.audit_log_path)
    if not settings.api_keys:
        logger.warning("No API_KEYS set. Anyone who can reach this service can ask.")
    database = await Database.connect(settings)
    # The schema is read once. Restart the service after a schema change.
    schema = await database.describe_schema()
    app.state.api_keys = settings.api_keys
    app.state.audit_log_path = settings.audit_log_path
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
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(
        min_length=1, max_length=2000, examples=["Which table has the most rows?"]
    )
    # For a follow-up question: the id of the answer it continues.
    follow_up_to: str | None = Field(default=None, pattern="^[0-9a-f]{32}$")


def get_analyst(request: Request) -> Analyst:
    return request.app.state.analyst


bearer = HTTPBearer(auto_error=False)


def get_caller(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str | None:
    """Who is asking, from the API key. None when the service runs without API keys."""
    api_keys: dict[str, str] = request.app.state.api_keys
    if not api_keys:
        return None
    presented = credentials.credentials.encode() if credentials else b""
    for key, caller in api_keys.items():
        # compare_digest takes the same time whether the keys match or not.
        if secrets.compare_digest(presented, key.encode()):
            return caller
    raise HTTPException(
        status_code=401,
        detail="A valid API key is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


Caller = Annotated[str | None, Depends(get_caller)]

# How far back a follow-up question reaches.
MAX_EARLIER_TURNS = 5


def earlier_turns(
    request: Request, follow_up_to: str | None, caller: str | None
) -> list[audit.AnswerRecord]:
    """The answers a follow-up question builds on, oldest first. Empty for a first question."""
    if follow_up_to is None:
        return []
    turns = audit.earlier_turns(request.app.state.audit_log_path, follow_up_to, MAX_EARLIER_TURNS)
    # A run that gave no answer, or another caller's answer, is treated as not being there.
    if not turns or any(turn.outcome != "answered" or turn.caller != caller for turn in turns):
        raise HTTPException(
            status_code=404, detail="There is no answer with that id to follow up on."
        )
    return turns


@app.post("/ask")
async def ask(
    body: Question,
    request: Request,
    analyst: Annotated[Analyst, Depends(get_analyst)],
    caller: Caller,
) -> Answer:
    earlier = earlier_turns(request, body.follow_up_to, caller)
    return await analyst.answer(body.question, caller, earlier)


@app.post("/ask/stream")
async def ask_stream(
    body: Question,
    request: Request,
    analyst: Annotated[Analyst, Depends(get_analyst)],
    caller: Caller,
) -> StreamingResponse:
    """The same result as POST /ask, as server-sent events: `started` at once, one
    `query` event when each query finishes, then one `answer` event carrying the
    full response, or one `error` event."""
    earlier = earlier_turns(request, body.follow_up_to, caller)
    events = server_sent_events(analyst.stream(body.question, caller, earlier))
    return StreamingResponse(events, media_type="text/event-stream")


async def server_sent_events(
    stream: AsyncGenerator[ExecutedQuery | Answer, None],
) -> AsyncIterator[str]:
    # Sent at once, so the client knows the question arrived before the first model turn ends.
    yield sse("started", "{}")
    # Closing the stream when the client leaves lets it record how the run ended.
    async with aclosing(stream) as events:
        try:
            async for event in events:
                name = "answer" if isinstance(event, Answer) else "query"
                yield sse(name, event.model_dump_json())
        # The response has already started with status 200. A failure travels as an event
        # that carries the status the plain endpoint would have returned.
        except QuestionDeclined as exc:
            yield sse("error", json.dumps({"status": 422, "detail": str(exc)}))
        except AgentError as exc:
            yield sse("error", json.dumps({"status": 502, "detail": str(exc)}))
        except anthropic.APIError as exc:
            logger.error("Claude API call failed: %s", exc)
            yield sse("error", json.dumps({"status": 502, "detail": CLAUDE_FAILURE}))


def sse(event: str, data: str) -> str:
    """One server-sent event. `data` is a JSON document on a single line."""
    return f"event: {event}\ndata: {data}\n\n"


@app.get("/schema", response_class=PlainTextResponse, dependencies=[Depends(get_caller)])
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
    logger.error("Claude API call failed: %s", exc)
    return JSONResponse(status_code=502, content={"detail": CLAUDE_FAILURE})
