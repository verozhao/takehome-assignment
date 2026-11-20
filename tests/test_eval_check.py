"""The eval's own checks: when an expected value counts as present in an answer, and
how the judge's reply is read."""

import pytest

from evals.run import missing_values, read_verdict, reference_values, stands_alone


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("10", "The longest films run 185 minutes and 10 films are that long."),
        ("358.12", "Karl Seal paid $358.12."),
        ("76367.30", "Payments came to 76,367.30 in 2022."),
        ("Karl Seal", "**KARL SEAL** (customer 526) paid the most."),
        # The reference rounds an average to 115. An answer may be more exact.
        ("115", "The average length is 115.3 minutes."),
    ],
)
def test_a_value_counts_when_it_stands_alone(value: str, text: str):
    assert stands_alone(value, text)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("10", "The newest film is from 2010."),
        ("10", "There are 100 films."),
        ("10", "Length rose by 0.10 minutes a year."),
        ("185", "The query covered 1850 rows."),
    ],
)
def test_a_value_inside_a_longer_number_does_not_count(value: str, text: str):
    assert not stands_alone(value, text)


def test_a_value_must_be_in_the_answer_and_in_the_fetched_rows():
    fetched = {"result": {"rows": [[185, 10]]}}

    said_and_fetched = {"answer": "185 minutes, and 10 films are that long.", "queries": [fetched]}
    only_said = {"answer": "185 minutes, and 10 films are that long.", "queries": []}
    only_fetched = {"answer": "185 minutes. The newest is from 2010.", "queries": [fetched]}

    assert missing_values(["185", "10"], said_and_fetched) == []
    assert missing_values(["185", "10"], only_said) == ["185", "10"]
    assert missing_values(["185", "10"], only_fetched) == ["10"]


def test_the_expected_values_are_every_value_of_every_reference_row_as_text():
    rows = [{"name": "Drama", "films": 152}, {"name": "Music", "films": 152}]

    assert reference_values(rows) == ["Drama", "152", "Music", "152"]


@pytest.mark.parametrize(
    ("reply", "correct"),
    [
        ("CORRECT The answer names both customers with the right totals.", True),
        ("CORRECT. Both figures match.", True),
        ("**Correct** The count matches.", True),
        ("WRONG The highest and the lowest are swapped.", False),
        ("WRONG\nIt leads with 288 and the reference says 241.", False),
        ("The answer looks correct.", False),
        ("", False),
    ],
)
def test_only_a_reply_that_starts_with_correct_passes(reply: str, correct: bool):
    assert read_verdict(reply)[0] is correct


def test_the_reason_is_what_follows_the_verdict():
    reply = "WRONG\nIt leads with 288 and the reference says 241."

    assert read_verdict(reply) == (False, "It leads with 288 and the reference says 241.")
