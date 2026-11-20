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
    # Who asked, from the API key. None when the service runs without API keys, and in
    # records written before login existed.
    caller: str | None = None
    question: str
    # The id of the answer this question followed up on, if any.
    follow_up_to: str | None = None
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
    with open(open_for_append(path), "a", encoding="utf-8") as file:
        file.write(record.model_dump_json() + "\n")


def find(path: str, record_id: str) -> AnswerRecord | None:
    """Look one record up by its id. The file is scanned, which a real store would index."""
    with open(path, encoding="utf-8") as file:
        for line in file:
            # A cheap test first, so that only a line naming the id is parsed.
            if record_id in line:
                record = AnswerRecord.model_validate_json(line)
                if record.id == record_id:
                    return record
    return None


def earlier_turns(path: str, record_id: str, limit: int) -> list[AnswerRecord]:
    """The answer with this id and the answers it followed up on, oldest first."""
    turns: list[AnswerRecord] = []
    next_id: str | None = record_id
    while next_id and len(turns) < limit:
        record = find(path, next_id)
        if record is None:
            break
        turns.append(record)
        next_id = record.follow_up_to
    return turns[::-1]


def ensure_writable(path: str) -> None:
    """Fail at startup if the record file cannot be written, and not after a question ran."""
    os.close(open_for_append(path))


def open_for_append(path: str) -> int:
    # A new file is readable by the service's own user only, because records can hold data.
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
