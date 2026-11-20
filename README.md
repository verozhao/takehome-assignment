# Ask your database

A small FastAPI service that answers plain-English questions about a Postgres database.
You send a question. The service works out which SQL to run, runs it read-only, and returns
the answer together with every query and result the answer is based on.

It was built against the [Pagila](https://github.com/devrimgunduz/pagila) sample database,
but nothing in the service code knows about Pagila. The schema is read from Postgres at
startup.

## Example

```bash
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "Which customer has paid the most for rentals? What about least?"}'
```

```json
{
  "answer": "**Karl Seal (customer 526) has paid the most: $358.12 across 88 payments. Mateo Scott (customer 624) has paid the least of anyone who has paid at all: $0.99 for a single payment.** [...] Two of the 999 customers have paid nothing. They are Jennifer Mitchell (647) and Sofia Baker (978) [...]",
  "queries": [
    {
      "purpose": "Rank customers by total payments, find top and bottom with ties, and count customers with no payments.",
      "sql": "WITH t AS (SELECT c.customer_id, c.first_name, c.last_name, COALESCE(SUM(p.amount),0) AS total, COUNT(p.payment_id) AS n FROM customer c LEFT JOIN payment p ON p.customer_id = c.customer_id GROUP BY [...]",
      "result": {
        "columns": ["customer_id", "first_name", "last_name", "total", "n", "rk_hi", "rk_lo", "customers", "no_payments", "orphan_payments"],
        "rows": [[526, "KARL", "SEAL", "358.12", 88, 1, 999, 999, 2, 0], "[...]"],
        "truncated": false,
        "error": null
      }
    },
    {
      "purpose": "Check rental counts for the lowest-paying customers.",
      "sql": "SELECT c.customer_id, COUNT(r.rental_id) AS rentals FROM customer c LEFT JOIN rental r ON r.customer_id=c.customer_id WHERE c.customer_id IN (624,647,978) GROUP BY c.customer_id ORDER BY c.customer_id",
      "result": {
        "columns": ["customer_id", "rentals"],
        "rows": [[624, 1], [647, 0], [978, 0]],
        "truncated": false,
        "error": null
      }
    }
  ]
}
```

The response is shortened where marked with `[...]`.

## Running it

You need Docker, Python 3.11 or newer and an Anthropic API key.

**1. Start Pagila**

```bash
git clone https://github.com/devrimgunduz/pagila
cd pagila && docker compose up -d && cd ..
```

**2. Create the read-only role the service connects with**

Run this from the root of this repository. It is safe to run again, and worth doing after
pulling a newer version, because the grants in it can change.

```bash
docker exec -i pagila psql -U postgres -d pagila < sql/readonly_role.sql
```

**3. Install and configure**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Put your `ANTHROPIC_API_KEY` in `.env`.

A Pagila cloned today loads its data into a database called `pagila`, and steps 2 and 3
assume that. The assignment notes describe an older setup that loads into the default
`postgres` database. If that is yours, use `-d postgres` in step 2 and end `DATABASE_URL`
in `.env` with `/postgres`. You have the current setup if
`docker exec pagila psql -U postgres -d pagila -c '\dt'` lists the tables.

**4. Run**

```bash
uvicorn app.main:app
```

Ask with `curl` as above, or open http://localhost:8000/docs.

| Endpoint | Purpose |
|---|---|
| `POST /ask` | Takes `{"question": "..."}`, returns the answer and the queries behind it |
| `POST /ask/stream` | The same, sent as events while the service works |
| `GET /schema` | The schema text exactly as the model sees it |
| `GET /health` | Checks the database connection |

**If something goes wrong**

- *"No tables found in database"* at startup: `DATABASE_URL` names the wrong database. See
  the note in step 3.
- *Step 2 fails because the `pagila` container is not running*: I hit this on macOS.
  `docker logs pagila` shows the init stopping at "bad interpreter: Permission denied". The
  film data is loaded by then, so `docker start pagila` brings it up. The last two init
  steps are skipped, so the two JSONB package tables stay empty and the pg_partman setup
  does not run. None of the sample questions need either. I finished them by hand inside the
  container, with `pg_restore -U postgres -d pagila` on the two `.backup` files in
  `/docker-entrypoint-initdb.d/` and `psql -U postgres -d pagila -f` on
  `4-pg-partman-setup.sql` in the same folder.

## Watching it work

A question takes 5 to 18 seconds. `POST /ask/stream` shows what the service is doing in
the meantime. It sends a `started` event at once, a `query` event when each query finishes,
and then an `answer` event with the same body as `POST /ask`.

```bash
curl -N -s localhost:8000/ask/stream -H 'content-type: application/json' \
  -d '{"question": "Are films getting longer over time?"}'
```

```
event: started
data: {}

event: query
data: {"purpose": "Check coverage of release_year and length in film table.", "sql": "[...]", "result": {"[...]"}}

event: query
data: {"purpose": "Compute linear trend and period comparisons of film length.", "sql": "[...]", "result": {"[...]"}}

event: answer
data: {"answer": "[...]", "queries": ["[...]"]}
```

On the film length question the two queries arrived after 3.7 and 8.1 seconds and the answer
after 17.3. A failure arrives as an `error` event that carries the status `POST /ask` would
have returned. A stream that ends without an `answer` or `error` event means the service
itself failed.

## How it works

```
 question
    |
    v
 POST /ask ---> Claude, with the working rules and the schema as system prompt
                  |   ^
     run_sql(purpose, sql)   rows as JSON, or the Postgres error
                  v   |
               Postgres, through a read-only role, a read-only transaction and limits
                  |
                  v
 answer + every query with its purpose and result
```

1. At startup the service reads tables, views, columns, enum values and keys from
   `pg_catalog` and renders them as text. For Pagila that is about 225 lines. It lists only
   the columns the connected role may read, so grants decide what the model knows about.
2. A question goes to Claude with that schema and a single tool, `run_sql`.
3. Claude asks for queries. Each one is run, and the rows or the Postgres error message go
   back to Claude. Most questions take one to three queries.
4. When Claude has what it needs, it writes the answer. The response carries each query,
   why it was run and what it returned.

## Decisions and trade-offs

**An agent with one SQL tool.** A single text-to-SQL call would be simpler, but the film
length question cannot be answered by one query written blind. You have to see which years
the data covers, then aggregate, then judge. The same loop lets the model check how a title
is spelled before filtering on it, and fix its own SQL when Postgres rejects it. The price
is latency and more than one model call per question.

**No RAG.** Retrieval over rows gives the wrong kind of answer for counts, sums and
rankings. Retrieval over the schema is not needed at this size, because the whole prompt,
schema included, is about 3,800 tokens and is cached. With hundreds of tables I would add a
step that picks the relevant tables first.

**Postgres validates the SQL.** Every statement is prepared inside a read-only transaction
before it runs. The real parser decides what is valid, and a string holding two statements
is rejected. Anything that returns no columns (`SET`, `COPY`, `DO`) is refused, and so are
placeholders. Writes fail because the transaction is read-only. `EXPLAIN` is refused as
well, because `EXPLAIN ANALYZE` can create a table even inside a read-only transaction. My
first plan was a SQL parsing library. I dropped it because it would reject valid Postgres
syntax it does not know. The one gap it would have closed is `EXPLAIN ANALYZE`, and a single
check on the result columns closes that.

**The read-only role is the real security boundary.** No statement check can stop a
superuser from calling a function such as `pg_read_file` inside a `SELECT`. So the
repository ships a role that can read the tables in the `public` schema and nothing else,
and the service is meant to connect with it. The application checks are a second layer. The
database tests pass as the superuser too, which shows the checks block `INSERT`, `UPDATE`,
`DELETE` and DDL statements on their own. A superuser can still call a function that
writes, such as `pg_create_physical_replication_slot`, or switch the session role for later
queries with `set_config`. The read-only role is refused all of these.

**Limits on everything the model controls.** Ten steps per question, 100 rows and 40,000
characters per result, a 15-second statement timeout and a 120-second timeout on each
attempt at a model call. A result that is too large is refused with a message telling the model to aggregate
or select fewer columns. Pagila needs this: its two JSONB package tables hold documents of
about 700 characters each, so 100 rows of them would put 70,000 characters into the
context. The size check runs after the rows are fetched, so the memory one query can use is
bounded by the row cap and the timeout.

**Every answer comes with its evidence.** A number you cannot trace is hard to trust, so the
response returns each query with its stated purpose and its rows. Decimal amounts are
returned as exact strings, and intervals as the text Postgres prints.

**An error is better than a doubtful answer.** If the model reaches the step limit or runs
out of output tokens, the API returns 502 with the reason. If the model refuses the
question, the API returns 422. It never returns a partial answer.

**A generic prompt.** The rules in the prompt are about analysing any database: check how
values are stored before filtering, group by keys because names repeat, report ties, check
what a time column covers before claiming a trend. A test fails if `pagila` or one of its
main table names appears in the service code. Each request also tells the model today's
date, which it cannot know and which questions about recent periods depend on.

**Claude Opus 5.5 with adaptive thinking at high effort.** I put accuracy first. Over three
runs of the questions below, a question took 5 to 18 seconds. It cost 1 to 6 cents, about 3
on average. The schema prompt is cached, so after the first call most input tokens are read
from cache. Model and effort are settings.

**No agent framework.** The loop is about 60 lines in `app/agent.py`. Writing it out keeps
every step visible and easy to debug.

## Tests and evaluation

```bash
pytest                 # 54 tests. Most of them need Pagila running.
mypy app evals         # type check
python -m evals.run    # live: needs the service running, costs about 30 cents
```

- `tests/test_agent.py` drives the loop with a scripted stand-in for Claude: a result fed
  back, a SQL error fed back for a retry, several queries in one turn, the step limit, a
  cut-off response, an empty response, a refusal and the event stream.
- `tests/test_database.py` runs against Pagila: schema rendering, JSON-safe values,
  truncation, the size cap, the timeout, and a list of writes and non-query statements that
  must be rejected. These tests send real `DELETE` and `UPDATE` statements, so run them as a
  superuser only against a Pagila you can throw away.
- `tests/test_api.py` covers the HTTP layer and starts the real service once.
- `evals/run.py` asks the running service the four sample questions, one paraphrase and
  three probes. Expected values come from reference SQL executed when the eval runs. A case
  passes when each expected value appears in the answer and in the rows the agent fetched.

A sample run:

| Result | Time | Queries | Question |
|---|---|---|---|
| PASS | 5.4 s | 1 | What actors were in Chocolat Harry? |
| PASS | 9.3 s | 1 | Display the top 3 actors who have most appeared in films in the Children category |
| PASS | 11.2 s | 2 | Which three actors show up most often in kids' movies? |
| PASS | 17.8 s | 2 | A common criticism of modern movies is that they are too long. [...] |
| PASS | 12.1 s | 2 | Which customer has paid the most for rentals? What about least? |
| READ | 9.9 s | 2 | What actors were in the film Zzyzx Road? |
| READ | 7.6 s | 1 | What is the average budget of a film? |
| READ | 6.5 s | 1 | Delete every payment made before 2023. |

All five graded questions passed in each of three runs. The three probes have no reference
SQL, so I judged them by reading. The service said there is no such film and listed the
closest titles, said the database holds no budget data, and declined the delete because it
can only read. For the film length question the check covers only the years and the average
length, so I read its conclusion as well.

Three details the answers got right:

- Top 3 Children actors: the top three are a clean three-way tie at 9 films, and nine more
  actors have 8. The answer reports the tie and that the next group has 8 films. It counts
  by actor id because two actors share a name.
- Film length: the data covers 2006 to 2024 and length is flat over that period, at about
  0.08 minutes more per year. The answer says the criticism is not supported here, and that
  the data holds no older films to compare with.
- Highest and lowest paying customer: two customers have no payments at all. The answer
  names the lowest-paying customer and also names the two who paid nothing.

## Limitations and next steps

[DESIGN.md](DESIGN.md) covers the path from here to production: what I would add, in which
order, and what I would keep out. The limits of the code as it stands:

- Each request stands alone. Follow-up questions would need conversation state.
- `POST /ask/stream` shows each query as it finishes, but the two longest waits stay
  silent: the first model turn and the writing of the answer.
- The schema is read once at startup.
- The eval is small, and the probes and the film length conclusion are judged by reading.
  With more time I would add more questions, a model-based judge for the open-ended ones,
  and repeat runs to measure consistency.
- A large schema would need table selection before the main loop, plus column comments and
  sample values in the schema text.
- The API has no authentication or rate limiting.
- Column grants decide what the model can see and read. The role script withholds one
  column as an example, the staff password hashes. A real deployment needs that review for
  every table.
- Text stored in the database reaches the model. The prompt tells the model to treat it as
  data and the connection cannot write, which limits the damage. It is not a full defence
  against prompt injection.

## Layout

```
app/
  main.py        FastAPI app and endpoints
  agent.py       the tool loop
  prompt.py      the system prompt
  database.py    schema description and guarded query execution
  config.py      settings
sql/readonly_role.sql
tests/
evals/run.py
```
