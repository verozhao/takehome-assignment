"""Ask the running service a set of questions and check its answers.

A graded case has to pass two checks. Every value returned by its reference SQL must
show up both in the answer text and in the rows the agent fetched. A judge model must
also agree that the answer states those reference facts correctly, each attached to
the right thing. The judge catches what the first check cannot see, such as the highest
and the lowest swapped, or an answer that leads with a wrong figure and mentions the
right one in passing. The expected values are computed when the eval runs, so none of
them is typed in by hand.

The judge compares the answer with the reference result and nothing else. It does not
grade extra detail or the conclusion drawn about film lengths. Every answer is printed
at the end so that a person can read those.

Probes have no reference. They are printed so a person can read how the service
handles questions it should not be able to answer.

Usage, with the service running:

    python -m evals.run              ask every question once
    python -m evals.run --repeat 3   ask every question three times, to see how stable it is

The exit status is 1 when any graded attempt fails, so the eval can gate a change
to the prompt or the model.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from statistics import mean

import asyncpg
import httpx
from anthropic import AsyncAnthropic

from app.config import Settings

API_URL = "http://localhost:8000"
QUESTIONS_AT_ONCE = 6

JUDGE_MODEL = "claude-opus-5-5"
JUDGE_MAX_TOKENS = 4_000
JUDGE_PROMPT = """\
You are grading an answer to a question about a database.

Question:
{question}

Reference SQL, written by hand:
{sql}

Its result, which is the truth:
{rows}

Answer to grade:
{answer}

Does the answer state the facts in the reference result correctly, each attached to the \
right thing? The highest and the lowest must not be swapped. The figure the answer gives \
as its answer must equal the reference figure, so an answer that leads with a different \
figure is wrong even if the reference figure appears somewhere in it. When the question can \
be read in two ways and the answer gives both readings with clear labels, it is correct if \
one of them matches the reference. Extra detail beyond the reference is fine, and the \
wording and number format are free.

Start your reply with one word, CORRECT or WRONG. Then give the reason in one sentence."""

ACTORS_IN_ONE_FILM = """
SELECT a.first_name, a.last_name
FROM film f
JOIN film_actor fa USING (film_id)
JOIN actor a USING (actor_id)
WHERE f.title = 'CHOCOLAT HARRY'
"""

# rank() keeps everyone who is tied for a place in the top three.
TOP_ACTORS_IN_CHILDREN_FILMS = """
SELECT first_name, last_name
FROM (
    SELECT a.first_name, a.last_name, rank() OVER (ORDER BY count(*) DESC) AS place
    FROM actor a
    JOIN film_actor fa USING (actor_id)
    JOIN film_category fc USING (film_id)
    JOIN category c USING (category_id)
    WHERE c.name = 'Children'
    GROUP BY a.actor_id
) ranked
WHERE place <= 3
"""

FILM_LENGTH_QUESTION = (
    "A common criticism of modern movies is that they are too long. Can you analyze "
    "film lengths over time and determine if that criticism is fair"
)

# The years the data covers and the overall average length must both be stated.
FILM_LENGTH_FACTS = """
SELECT min(release_year), max(release_year), round(avg(length))
FROM film
"""

# Customers with no payment at all are left out, because the question asks who has paid.
HIGHEST_AND_LOWEST_PAYING_CUSTOMER = """
WITH totals AS (
    SELECT c.first_name, c.last_name, sum(p.amount) AS total
    FROM customer c
    JOIN payment p USING (customer_id)
    GROUP BY c.customer_id
)
SELECT first_name, last_name, total
FROM totals
WHERE total = (SELECT max(total) FROM totals)
   OR total = (SELECT min(total) FROM totals)
"""

RENTALS_STILL_OUT = "SELECT count(*) FROM rental WHERE return_date IS NULL"

# Two categories share first place, and the answer has to name both.
CATEGORIES_WITH_THE_MOST_FILMS = """
SELECT name, films
FROM (
    SELECT c.name, count(*) AS films, rank() OVER (ORDER BY count(*) DESC) AS place
    FROM film_category fc
    JOIN category c USING (category_id)
    GROUP BY c.category_id
) ranked
WHERE place = 1
"""

CUSTOMERS_WHO_NEVER_PAID = """
SELECT c.first_name, c.last_name
FROM customer c
WHERE NOT EXISTS (SELECT 1 FROM payment p WHERE p.customer_id = c.customer_id)
"""

ACTORS_WHO_SHARE_A_NAME = """
SELECT first_name, last_name
FROM actor
GROUP BY first_name, last_name
HAVING count(*) > 1
"""

YEAR_WITH_THE_MOST_PAYMENTS = """
SELECT extract(year FROM payment_date)::integer, sum(amount)
FROM payment
GROUP BY 1
ORDER BY 2 DESC
LIMIT 1
"""

LONGEST_FILMS = """
SELECT max(length), count(*) FILTER (WHERE length = (SELECT max(length) FROM film))
FROM film
"""

RATING_WITH_THE_MOST_FILMS = """
SELECT rating, count(*)
FROM film
GROUP BY rating
ORDER BY 2 DESC
LIMIT 1
"""

GRADED = [
    # The four sample questions from the assignment, and one paraphrase.
    ("What actors were in Chocolat Harry?", ACTORS_IN_ONE_FILM),
    (
        "Display the top 3 actors who have most appeared in films in the Children category",
        TOP_ACTORS_IN_CHILDREN_FILMS,
    ),
    ("Which three actors show up most often in kids' movies?", TOP_ACTORS_IN_CHILDREN_FILMS),
    (FILM_LENGTH_QUESTION, FILM_LENGTH_FACTS),
    (
        "Which customer has paid the most for rentals? What about least?",
        HIGHEST_AND_LOWEST_PAYING_CUSTOMER,
    ),
    # Questions that each hide one of the traps the prompt warns about.
    ("How many rentals have not been returned yet?", RENTALS_STILL_OUT),
    ("Which film category has the most films?", CATEGORIES_WITH_THE_MOST_FILMS),
    ("Which customers have never made a payment?", CUSTOMERS_WHO_NEVER_PAID),
    ("Do any two actors have exactly the same name? Who?", ACTORS_WHO_SHARE_A_NAME),
    (
        "In which year did payments add up to the most, and how much was it?",
        YEAR_WITH_THE_MOST_PAYMENTS,
    ),
    ("How long is the longest film, and how many films are that long?", LONGEST_FILMS),
    ("Which rating has the most films, and how many?", RATING_WITH_THE_MOST_FILMS),
]

PROBES = [
    ("What actors were in the film Zzyzx Road?", "says there is no such film"),
    ("What is the average budget of a film?", "says the database has no budget data"),
    ("Delete every payment made before 2023.", "declines, because it can only read"),
]

Attempt = tuple[dict, float]
# What the judge said about one answer: whether it is correct, and why.
Verdict = tuple[bool, str]


async def reference_rows(database: asyncpg.Connection, sql: str) -> list[dict]:
    rows = [dict(row) for row in await database.fetch(sql)]
    if not rows:
        sys.exit(f"A reference query returned no rows, so nothing can be graded:\n{sql}")
    return rows


def reference_values(rows: list[dict]) -> list[str]:
    return [str(value) for row in rows for value in row.values()]


async def ask(api: httpx.AsyncClient, free_slot: asyncio.Semaphore, question: str) -> Attempt:
    async with free_slot:
        started = time.perf_counter()
        response = await api.post("/ask", json={"question": question})
        seconds = time.perf_counter() - started
    if response.status_code != 200:
        # The service could not answer. That fails this attempt and the run goes on.
        return {"answer": f"HTTP {response.status_code}: {response.text}", "queries": []}, seconds
    return response.json(), seconds


def comparable(text: str) -> str:
    # "76,367.30" in an answer and 76367.30 in a result are the same figure.
    return text.lower().replace(",", "")


def stands_alone(value: str, text: str) -> bool:
    """Whether the text holds the value as a whole word or number. 10 is not in 2010 or 0.10.

    An answer may give a figure more exactly than the reference does, so 115 is in 115.3.
    """
    pattern = rf"(?<![\w.]){re.escape(comparable(value))}(?!\w)"
    return re.search(pattern, comparable(text)) is not None


def missing_values(expected: list[str], answer: dict) -> list[str]:
    """Expected values that are absent from the answer text or from the fetched rows."""
    fetched = json.dumps([query["result"]["rows"] for query in answer["queries"]])
    return [
        value
        for value in expected
        if not stands_alone(value, answer["answer"]) or not stands_alone(value, fetched)
    ]


async def judge(
    claude: AsyncAnthropic,
    free_slot: asyncio.Semaphore,
    question: str,
    sql: str,
    rows: list[dict],
    answer: str,
) -> Verdict:
    """Ask the judge model whether the answer states the reference facts correctly."""
    prompt = JUDGE_PROMPT.format(
        question=question, sql=sql.strip(), rows=json.dumps(rows, default=str), answer=answer
    )
    async with free_slot:
        response = await claude.messages.create(
            model=JUDGE_MODEL,
            max_tokens=JUDGE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    # The model may think first, so the reply is whatever text blocks it sent.
    return read_verdict("".join(block.text for block in response.content if block.type == "text"))


def read_verdict(reply: str) -> Verdict:
    """Split the judge's reply into its verdict and its reason. Only a reply that starts
    with CORRECT passes, so an empty or unclear reply fails the attempt."""
    words = reply.split(maxsplit=1)
    first_word = words[0] if words else ""
    reason = words[1] if len(words) > 1 else ""
    return first_word.strip("*.:,").upper() == "CORRECT", reason


async def main(repeat: int) -> None:
    settings = Settings()
    # The reference queries cost nothing, so they run before any question is asked.
    database: asyncpg.Connection = await asyncpg.connect(settings.database_url)
    references = [await reference_rows(database, sql) for _, sql in GRADED]
    await database.close()

    questions = [question for question, _ in GRADED + PROBES]
    free_slot = asyncio.Semaphore(QUESTIONS_AT_ONCE)
    # With login on, the service wants one of its API keys.
    key = next(iter(settings.api_keys), None)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(base_url=API_URL, timeout=600, headers=headers) as api:
        attempts = await asyncio.gather(
            *(ask(api, free_slot, question) for question in questions for _ in range(repeat))
        )
    # The attempts come back in the order they were asked: `repeat` of them per question.
    by_question = [attempts[start : start + repeat] for start in range(0, len(attempts), repeat)]

    claude = AsyncAnthropic(api_key=settings.anthropic_api_key)
    verdicts = await asyncio.gather(
        *(
            judge(claude, free_slot, question, sql, rows, answer["answer"])
            for (question, sql), rows, tries in zip(GRADED, references, by_question)
            for answer, _ in tries
        )
    )
    judged = [verdicts[start : start + repeat] for start in range(0, len(verdicts), repeat)]

    failed_cases = 0
    for (question, _), rows, tries, opinions in zip(GRADED, references, by_question, judged):
        expected = reference_values(rows)
        missing = [missing_values(expected, answer) for answer, _ in tries]
        passed = sum(not values and correct for values, (correct, _) in zip(missing, opinions))
        failed_cases += passed < repeat
        result = "PASS" if passed == repeat else "FAIL"
        seconds = mean(seconds for _, seconds in tries)
        print(f"{result}  {passed}/{repeat}  {seconds:5.1f}s  {question}")
        for values, (correct, reason) in zip(missing, opinions):
            if values:
                print(f"      missing: {', '.join(values)}")
            if not correct:
                print(f"      judged wrong: {reason}")

    for (question, good_answer), tries in zip(PROBES, by_question[len(GRADED) :]):
        seconds = mean(seconds for _, seconds in tries)
        print(f"READ       {seconds:5.1f}s  {question}")
        print(f"      a good answer {good_answer}")

    print(f"\n{len(GRADED) - failed_cases} of {len(GRADED)} graded questions passed every attempt.")
    for question, tries in zip(questions, by_question):
        print(f"\n--- {question}\n{tries[0][0]['answer']}")

    sys.exit(1 if failed_cases else 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ask the running service the eval questions.")
    parser.add_argument("--repeat", type=int, default=1, help="how often to ask each question")
    asyncio.run(main(parser.parse_args().repeat))
