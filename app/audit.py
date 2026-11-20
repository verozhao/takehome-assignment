"""A stored record of every question: what was asked, what ran and what was answered.

One JSON line per question is appended to a file, and lines are never rewritten.
A record holds the SQL text and a hash of each result. The rows themselves are
not copied out of the database, but the SQL, the error text of a failed query
and the answer can all contain values from it.
"""

import hashlib
import os
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class QueryRecord(BaseModel):
    purpose: str
    sql: str
    row_count: int
    truncated: bool
    error: str | None
    # Lets someone run the SQL again later and check that it returns the same data.
    rows_sha256: str


class AnswerRecord(BaseModel):
    id: str
    asked_at: datetime
    seconds: float
    question: str
    model: str
    reasoning_effort: str
    # Identifies the exact rules and schema text the model was given.
    prompt_sha256: str
    queries: list[QueryRecord]
    outcome: Literal["answered", "declined", "failed", "cancelled"]
    detail: str | None
    answer: str | None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def append(path: str, record: AnswerRecord) -> None:
    # A new file is readable by the service's own user only, because records can hold data.
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with open(descriptor, "a", encoding="utf-8") as file:
        file.write(record.model_dump_json() + "\n")
