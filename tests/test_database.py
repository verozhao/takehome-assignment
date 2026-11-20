"""The database layer against a running Pagila (see the README for how to start it)."""

from pathlib import Path

import pytest

from app.config import Settings
from app.database import Database


async def connect(**overrides) -> Database:
    # DATABASE_URL comes from the environment or .env. No API key is needed here.
    return await Database.connect(Settings(anthropic_api_key="unused", **overrides))


@pytest.fixture
async def database():
    database = await connect()
    yield database
    await database.close()


async def test_schema_lists_tables_views_and_keys(database: Database):
    schema = await database.describe_schema()

    assert "table film\n" in schema
    assert "  title text" in schema
    assert "view film_list\n" in schema
    assert "PRIMARY KEY (film_id)" in schema
    assert "REFERENCES language(language_id)" in schema


async def test_schema_shows_a_partitioned_table_once(database: Database):
    schema = await database.describe_schema()

    assert "table payment\n" in schema
    assert "payment_p20" not in schema


async def test_schema_spells_out_enum_values_and_quotes_odd_names(database: Database):
    schema = await database.describe_schema()

    assert "rating mpaa_rating (one of: G, PG, PG-13, R, NC-17)" in schema
    assert '"zip code" text' in schema


def test_application_code_knows_nothing_about_this_database():
    source = "".join(path.read_text() for path in Path("app").glob("*.py")).lower()

    for name in ("pagila", "film", "actor", "rental", "payment", "customer"):
        assert name not in source


async def test_query_returns_columns_and_rows(database: Database):
    result = await database.run_query("SELECT film_id, title FROM film ORDER BY film_id LIMIT 2")

    assert result.error is None
    assert result.columns == ["film_id", "title"]
    assert result.rows == [[1, "ACADEMY DINOSAUR"], [2, "ACE GOLDFINGER"]]
    assert result.truncated is False


async def test_values_are_json_safe(database: Database):
    result = await database.run_query("SELECT 1.50::numeric AS amount, DATE '2024-01-02' AS day")

    assert result.rows == [["1.50", "2024-01-02"]]


async def test_columns_with_the_same_name_are_all_kept(database: Database):
    result = await database.run_query("SELECT 1 AS n, 2 AS n")

    assert result.columns == ["n", "n"]
    assert result.rows == [[1, 2]]


async def test_long_results_are_cut_off_and_flagged():
    database = await connect(max_rows=5)

    result = await database.run_query("SELECT film_id FROM film ORDER BY film_id")

    assert len(result.rows) == 5
    assert result.truncated is True
    await database.close()


async def test_oversized_results_are_refused():
    database = await connect(max_result_chars=200)

    result = await database.run_query("SELECT description FROM film LIMIT 50")

    assert "too large" in result.error
    assert result.rows == []
    await database.close()


async def test_sql_mistakes_come_back_as_errors(database: Database):
    result = await database.run_query("SELECT no_such_column FROM film")

    assert 'column "no_such_column" does not exist' in result.error


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM film_actor",
        "DELETE FROM film_actor RETURNING actor_id",
        "UPDATE film SET length = 0 RETURNING film_id",
        "INSERT INTO language (name) VALUES ('Klingon') RETURNING language_id",
        "WITH gone AS (DELETE FROM film_actor RETURNING *) SELECT count(*) FROM gone",
        "DROP TABLE film_actor",
        "CREATE TABLE scratch (id integer)",
        "SELECT * INTO scratch FROM language",
    ],
)
async def test_writes_are_rejected(database: Database, sql: str):
    before = await database.pool.fetchval("SELECT count(*) FROM film_actor")

    result = await database.run_query(sql)

    assert result.error is not None
    assert await database.pool.fetchval("SELECT count(*) FROM film_actor") == before
    assert await database.pool.fetchval("SELECT to_regclass('scratch')") is None


@pytest.mark.parametrize(
    "sql",
    [
        "SET statement_timeout = 0",
        "COPY (SELECT 1) TO PROGRAM 'true'",
        "DO $$ BEGIN PERFORM 1; END $$",
        "SELECT 1; SELECT 2",
    ],
)
async def test_statements_that_are_not_a_single_query_are_rejected(database: Database, sql: str):
    result = await database.run_query(sql)

    assert result.error is not None


async def test_slow_queries_are_cancelled():
    database = await connect(statement_timeout_ms=200)

    result = await database.run_query("SELECT pg_sleep(5)")

    assert "statement timeout" in result.error
    await database.close()


async def test_a_query_cannot_switch_the_timeout_off_for_later_queries():
    database = await connect(statement_timeout_ms=200)

    await database.run_query("SELECT set_config('statement_timeout', '0', false)")
    result = await database.run_query("SELECT pg_sleep(5)")

    assert "statement timeout" in result.error
    await database.close()
