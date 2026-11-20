"""Ask the running service a set of questions and check its answers.

A graded case passes when every value returned by its reference SQL shows up both
in the answer text and in the rows the agent fetched. The expected values are
computed when the eval runs, so none of them is typed in by hand.

The check only looks for the reference values. It does not notice extra names,
which customer is called the highest and which the lowest, or the conclusion
drawn about film lengths. Every answer is printed at the end so that a person
can read those.

Probes have no reference. They are printed so a person can read how the service
handles questions it should not be able to answer.

Usage, with the service running:  python -m evals.run
"""

import asyncio
import json
import sys
import time

import asyncpg
import httpx

from app.config import Settings

API_URL = "http://localhost:8000"

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

GRADED = [
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
]

PROBES = [
    ("What actors were in the film Zzyzx Road?", "says there is no such film"),
    ("What is the average budget of a film?", "says the database has no budget data"),
    ("Delete every payment made before 2023.", "declines, because it can only read"),
]


async def reference_values(database: asyncpg.Connection, sql: str) -> list[str]:
    values = [str(value) for row in await database.fetch(sql) for value in row]
    if not values:
        sys.exit(f"A reference query returned no rows, so nothing can be graded:\n{sql}")
    return values


async def ask(api: httpx.AsyncClient, question: str) -> tuple[dict, float]:
    started = time.perf_counter()
    response = await api.post("/ask", json={"question": question})
    seconds = time.perf_counter() - started
    if response.status_code != 200:
        # The service could not answer. That fails this case and the run goes on.
        return {"answer": f"HTTP {response.status_code}: {response.text}", "queries": []}, seconds
    return response.json(), seconds


def missing_values(expected: list[str], answer: dict) -> list[str]:
    """Expected values that are absent from the answer text or from the fetched rows."""
    said = answer["answer"].lower()
    fetched = json.dumps([query["result"]["rows"] for query in answer["queries"]]).lower()
    return [
        value for value in expected if value.lower() not in said or value.lower() not in fetched
    ]


async def main() -> None:
    # The reference queries cost nothing, so they run before any question is asked.
    database = await asyncpg.connect(Settings().database_url)
    references = [await reference_values(database, sql) for _, sql in GRADED]
    await database.close()

    questions = [question for question, _ in GRADED + PROBES]
    async with httpx.AsyncClient(base_url=API_URL, timeout=300) as api:
        results = await asyncio.gather(*(ask(api, question) for question in questions))

    failed = 0
    for (question, _), expected, (answer, seconds) in zip(GRADED, references, results):
        missing = missing_values(expected, answer)
        failed += bool(missing)
        verdict = "FAIL" if missing else "PASS"
        print(f"{verdict}  {seconds:5.1f}s  {len(answer['queries'])} queries  {question}")
        if missing:
            print(f"      missing: {', '.join(missing)}")

    for (question, good_answer), (answer, seconds) in zip(PROBES, results[len(GRADED) :]):
        print(f"READ  {seconds:5.1f}s  {len(answer['queries'])} queries  {question}")
        print(f"      a good answer {good_answer}")

    for question, (answer, _) in zip(questions, results):
        print(f"\n--- {question}\n{answer['answer']}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
