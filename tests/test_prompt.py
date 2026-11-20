"""How definitions reach the model: the glossary file and comments from the database."""

from pathlib import Path

from app.config import Settings
from app.database import render_schema
from app.prompt import build_system_prompt


def settings(**overrides: str) -> Settings:
    return Settings(
        _env_file=None,
        anthropic_api_key="unused",
        database_url="postgresql://unused",
        **overrides,
    )


def test_a_glossary_file_becomes_a_definitions_section(tmp_path: Path):
    glossary = tmp_path / "glossary.md"
    glossary.write_text("Revenue: the sum of payment.amount.\n")

    prompt = build_system_prompt("table payment", settings(glossary_path=str(glossary)))

    assert "Definitions\n\nRevenue: the sum of payment.amount.\n\nDatabase schema" in prompt


def test_without_a_glossary_there_is_no_definitions_section():
    prompt = build_system_prompt("table payment", settings())

    assert "Definitions\n" not in prompt
    assert prompt.endswith("Database schema\n\ntable payment\n")


def test_comments_stored_in_the_database_follow_their_table_and_column():
    column = {
        "relation": "fund",
        "kind": "table",
        "relation_comment": "One row per fund\nand vintage.",
        "column_name": "irr",
        "data_type": "numeric(6,4)",
        "enum_labels": None,
        "column_comment": "Net of fees, as a fraction.",
    }
    plain = {**column, "column_name": "name", "data_type": "text", "column_comment": None}

    schema = render_schema([column, plain], [])

    assert schema == (
        "table fund  -- One row per fund and vintage.\n"
        "  irr numeric(6,4)  -- Net of fees, as a fraction.\n"
        "  name text"
    )
