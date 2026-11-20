"""Postgres access: describe the schema for the model and run its queries read-only."""

import json
from typing import Any

import asyncpg
from pydantic import BaseModel

from app.config import Settings

# Every table, view and materialized view on the search path, with its columns.
# Partitions are left out because their parent table already covers them, and so is
# a materialized view that was never populated, because it cannot be queried.
COLUMNS_SQL = """
SELECT cls.oid::regclass::text AS relation,
       CASE cls.relkind
           WHEN 'v' THEN 'view'
           WHEN 'm' THEN 'materialized view'
           ELSE 'table'
       END AS kind,
       quote_ident(att.attname) AS column_name,
       format_type(att.atttypid, att.atttypmod) AS data_type,
       (SELECT string_agg(enumlabel, ', ' ORDER BY enumsortorder)
        FROM pg_enum
        WHERE enumtypid = att.atttypid) AS enum_labels
FROM pg_class cls
JOIN pg_namespace ns ON ns.oid = cls.relnamespace
JOIN pg_attribute att ON att.attrelid = cls.oid
WHERE ns.nspname = ANY (current_schemas(false))
  AND cls.relkind IN ('r', 'p', 'v', 'm')
  AND NOT cls.relispartition
  AND cls.relispopulated
  AND att.attnum > 0
  AND NOT att.attisdropped
ORDER BY relation, att.attnum
"""

# Primary and foreign keys, already rendered by Postgres as SQL text.
KEYS_SQL = """
SELECT con.conrelid::regclass::text AS relation,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class cls ON cls.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = cls.relnamespace
WHERE ns.nspname = ANY (current_schemas(false))
  AND con.contype IN ('p', 'f')
  AND NOT cls.relispartition
ORDER BY relation, con.contype DESC, definition
"""


MAX_ERROR_CHARS = 2_000

# What can go wrong with a statement the model wrote. Postgres rejects it, or the driver
# cannot turn the result into Python values (a date past year 9999, a whole row as one
# column), or the query closed its own connection.
QUERY_ERRORS = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    asyncpg.InternalClientError,
    ValueError,
    ArithmeticError,
)


class QueryResult(BaseModel):
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated: bool = False
    error: str | None = None

    def as_tool_result(self) -> str:
        """The text the model sees as the outcome of its query."""
        if self.error:
            return f"Error: {self.error}"
        return json.dumps({"columns": self.columns, "rows": self.rows, "truncated": self.truncated})


class Database:
    def __init__(self, pool: asyncpg.Pool, max_rows: int, max_result_chars: int):
        self.pool = pool
        self.max_rows = max_rows
        self.max_result_chars = max_result_chars

    @classmethod
    async def connect(cls, settings: Settings) -> "Database":
        pool = await asyncpg.create_pool(
            settings.database_url,
            # A session default, so it is restored whenever a connection returns to the pool.
            server_settings={"statement_timeout": f"{settings.statement_timeout_ms}ms"},
            init=keep_interval_text,
        )
        return cls(pool, settings.max_rows, settings.max_result_chars)

    async def close(self) -> None:
        await self.pool.close()

    async def describe_schema(self) -> str:
        """Render the schema as plain text, one block per table or view."""
        async with self.pool.acquire() as conn:
            columns = await conn.fetch(COLUMNS_SQL)
            keys = await conn.fetch(KEYS_SQL)
            database_name = await conn.fetchval("SELECT current_database()")

        if not columns:
            raise RuntimeError(
                f'No tables found in database "{database_name}". Check that DATABASE_URL '
                "points at the database that holds the data."
            )

        blocks: dict[str, list[str]] = {}
        for row in columns:
            line = f"  {row['column_name']} {row['data_type']}"
            if row["enum_labels"]:
                line += f" (one of: {row['enum_labels']})"
            header = f"{row['kind']} {row['relation']}"
            blocks.setdefault(row["relation"], [header]).append(line)
        for row in keys:
            blocks[row["relation"]].append(f"  {row['definition']}")

        return "\n\n".join("\n".join(lines) for lines in blocks.values())

    async def run_query(self, sql: str) -> QueryResult:
        """Run one read-only query.

        Problems with the SQL itself are returned in `error` instead of raised,
        because the model reads them and corrects its own query.
        """
        # Failing to get a connection is not the model's mistake, so that is raised.
        async with self.pool.acquire() as conn:
            try:
                async with conn.transaction(readonly=True):
                    # Postgres parses the statement here, so a syntax error or several
                    # statements in one string fail before anything runs.
                    statement = await conn.prepare(sql)
                    columns = [attribute.name for attribute in statement.get_attributes()]
                    refusal = reason_to_refuse(columns, statement.get_parameters())
                    if refusal:
                        return QueryResult(error=refusal)
                    cursor = await statement.cursor()
                    # One row more than the cap tells us whether the result was cut off.
                    records = await cursor.fetch(self.max_rows + 1)
            except QUERY_ERRORS as exc:
                # Some messages quote the offending value, so an error can be as long as the data.
                return QueryResult(error=str(exc)[:MAX_ERROR_CHARS])

        rows = [list(record.values()) for record in records[: self.max_rows]]
        # Decimal, date and other Postgres types are not JSON types. Serialising once,
        # with str() as the fallback, gives the model and the API caller the same values.
        encoded = json.dumps(rows, default=str)
        if len(encoded) > self.max_result_chars:
            return QueryResult(
                error=(
                    f"The result is too large to return ({len(encoded):,} characters). "
                    "Select fewer columns, aggregate, or add a LIMIT."
                )
            )
        return QueryResult(
            columns=columns,
            rows=json.loads(encoded),
            truncated=len(records) > self.max_rows,
        )


def reason_to_refuse(columns: list[str], parameters: tuple) -> str | None:
    """Statements that Postgres accepts but that are not plain queries."""
    if not columns:
        # SET, COPY, DO and similar commands return no columns.
        return "Only queries that return rows are allowed."
    if columns == ["QUERY PLAN"]:
        # EXPLAIN ANALYZE executes its statement, and a CREATE TABLE AS under it
        # is not stopped by the read-only transaction.
        return "EXPLAIN is not allowed."
    if parameters:
        return "Placeholders such as $1 are not supported. Write the values into the SQL."
    return None


async def keep_interval_text(conn: asyncpg.Connection) -> None:
    # asyncpg turns an interval into a timedelta, which has no months, so "1 mon"
    # would arrive as 30 days. Keep the text Postgres prints instead.
    await conn.set_type_codec(
        "interval", schema="pg_catalog", encoder=str, decoder=str, format="text"
    )
