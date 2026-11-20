# Ask your database

A small FastAPI service that answers questions about a Postgres database in plain English.
You send a question. The service works out which SQL to run, runs it read-only, and returns
the answer together with every query and result the answer is based on.

It was built against the [Pagila](https://github.com/devrimgunduz/pagila) sample database,
but nothing in the code knows about Pagila. The schema is read from Postgres at startup.

## Example

```bash
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "Which customer has paid the most for rentals? What about least?"}'
```

```json
{
  "answer": "**Karl Seal (customer 526) has paid the most: $358.12 over 88 payments.** Among customers who have paid anything, **Mateo Scott (customer 624) has paid the least: $0.99 from a single payment.** [...] Two customers, Jennifer Mitchell (647) and Sofia Baker (978), have no payments and no rentals, so they paid $0. [...]",
  "queries": [
    {
      "purpose": "Find customers with highest and lowest total payments, including ties.",
      "sql": "WITH t AS (SELECT p.customer_id, SUM(p.amount) AS total, COUNT(*) AS n FROM payment p GROUP BY p.customer_id) [...]",
      "result": {
        "columns": ["customer_id", "first_name", "last_name", "total", "n"],
        "rows": [[526, "KARL", "SEAL", "358.12", 88], [624, "MATEO", "SCOTT", "0.99", 1]],
        "truncated": false,
        "error": null
      }
    }
  ]
}
```

The response above is shortened.

## Running it

You need Docker, Python 3.11 or newer and an Anthropic API key.

**1. Start Pagila**

```bash
git clone https://github.com/devrimgunduz/pagila
cd pagila && docker compose up -d && cd ..
```

**2. Create the read-only role the service connects with**

Run this from the root of this repository.

```bash
docker exec -i pagila psql -U postgres < sql/readonly_role.sql
```

**3. Install and configure**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Put your `ANTHROPIC_API_KEY` in `.env`. `DATABASE_URL` must name the database that holds
the tables. The assignment notes say Pagila loads into the default `postgres` database.
The current Pagila compose file loads into a database called `pagila`, so check which one
you have. If the service connects to a database without tables, it stops at startup and
says so.

**4. Run**

```bash
uvicorn app.main:app
```

Ask with `curl` as above, or open http://localhost:8000/docs.

| Endpoint | Purpose |
|---|---|
| `POST /ask` | Takes `{"question": "..."}`, returns the answer and the queries behind it |
| `GET /schema` | The schema text exactly as the model sees it |
| `GET /health` | Checks the database connection |

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
   `pg_catalog` and renders them as text. For Pagila that is about 230 lines.
2. A question goes to Claude with that schema and a single tool, `run_sql`.
3. Claude asks for queries. Each one is run, and the rows or the Postgres error message go
   back to Claude. Most questions take one to three queries.
4. When Claude has what it needs, it writes the answer. The response carries each query,
   why it was run and what it returned.

## Decisions and trade-offs

**An agent with one SQL tool, where a single text-to-SQL call would be simpler.**
Sample question (c) cannot be answered by one query written blind. You have to see which
years the data covers, then aggregate, then judge. The same loop lets the model check how a
title is spelled before filtering on it, and fix its own SQL when Postgres rejects it. The
price is latency and more than one model call per question.

**No RAG.** Retrieval over rows gives the wrong kind of answer for counts, sums and
rankings. Retrieval over the schema is not needed at this size, because the whole schema is
about 2,000 tokens and is cached. With hundreds of tables I would add a step that picks the
relevant tables first.

**Postgres validates the SQL.** Every statement is prepared inside a read-only transaction
before it runs. The real parser decides what is valid, a string holding two statements is
rejected, and anything that returns no columns (`SET`, `COPY`, `DO`) is refused. Writes fail
because the transaction is read-only. My first plan was a SQL parsing library. I dropped it
because it would reject valid Postgres syntax it does not know and would catch nothing the
database does not already catch.

**The read-only role is the real security boundary.** No statement check can stop a
superuser from calling a function such as `pg_read_file` inside a `SELECT`. So the
repository ships a role that can only read, and the service uses it by default. The
application checks still hold on their own: the database tests also pass when run as the
superuser.

**Limits on everything the model controls.** Ten steps per question, 100 rows and 40,000
characters per result, and a 15 second statement timeout. A result that is too large is
refused with a message telling the model to aggregate or select fewer columns. Pagila needs
this: its two JSONB tables hold documents of about 700 characters each, so 100 rows of them
would put 70,000 characters into the context.

**Every answer comes with its evidence.** A number you cannot trace is hard to trust, so the
response returns each query with its stated purpose and its rows. Decimal amounts are
returned as exact strings.

**No answer beats a doubtful answer.** If the model reaches the step limit, is cut off or
declines, the API returns 502 with the reason. It never returns a half-finished answer.

**A generic prompt.** The rules in the prompt are about analysing any database: check how
values are stored before filtering, group by keys because names repeat, report ties, check
what a time column covers before claiming a trend. A test fails if a Pagila table name ever
appears in the application code.

**Claude Opus 5.5 with adaptive thinking at high effort.** I put accuracy first. On the
questions below this took 6 to 16 seconds and about 2 to 3 cents per question. The schema
prompt is cached, so after the first call most input tokens are read from cache. Model and
effort are settings.

**No agent framework.** The loop is about 40 lines in `app/agent.py`. Writing it out keeps
every step visible and easy to debug.

## Tests and evaluation

```bash
pytest                 # the database tests need Pagila running
python -m evals.run    # live: needs the service running, costs about 20 cents
```

- `tests/test_agent.py` drives the loop with a scripted stand-in for Claude: a result fed
  back, a SQL error fed back for a retry, several queries in one turn, the step limit, an
  incomplete response.
- `tests/test_database.py` runs against Pagila: schema rendering, JSON-safe values,
  truncation, the size cap, the timeout, and a list of writes and non-query statements that
  must be rejected.
- `tests/test_api.py` covers the HTTP layer.
- `evals/run.py` asks the running service the four sample questions, one paraphrase and
  three probes. Expected values come from reference SQL executed when the eval runs. A case
  passes when each expected value appears in the answer and in the rows the agent fetched.

Latest run:

| Result | Time | Queries | Question |
|---|---|---|---|
| PASS | 6.5 s | 1 | What actors were in Chocolat Harry? |
| PASS | 8.3 s | 1 | Display the top 3 actors who have most appeared in films in the Children category |
| PASS | 11.7 s | 3 | Which three actors show up most often in kids' movies? |
| PASS | 15.9 s | 2 | A common criticism of modern movies is that they are too long. [...] |
| PASS | 12.2 s | 2 | Which customer has paid the most for rentals? What about least? |
| read | 8.8 s | 2 | What actors were in the film Zzyzx Road? |
| read | 7.1 s | 1 | What is the average budget of a film? |
| read | 11.3 s | 1 | Delete every payment made before 2023. |

The three probes have no reference SQL, so I judged them by reading. The service said there
is no such film and offered the closest titles, said the database holds no budget data, and
declined the delete because it can only read.

Three details the answers got right:

- (b) The top three are a clean three-way tie at 9 films, with nine more actors at 8. The
  answer reports both facts, and counts by actor id because two actors share a name.
- (c) The data covers 2006 to 2024 and film length is flat over that period, at about 0.08
  minutes more per year. The answer says the criticism is not supported here, and that the
  data holds no older films to compare with.
- (d) Two customers have no payments at all. The answer names the lowest paying customer
  and also mentions the two who paid nothing.

## Limitations and next steps

- Each request stands alone. Follow-up questions would need conversation state.
- The client waits for the whole answer. Streaming the steps would make the wait easier.
- The schema is read once at startup.
- The eval is small and the probes are judged by reading. With more time I would add more
  questions, a model-based judge for the open-ended ones, and repeat runs to measure
  consistency.
- A large schema would need table selection before the main loop, plus column comments and
  sample values in the schema text.
- The API has no authentication or rate limiting.
- The read-only role can read every column. Sensitive columns should be excluded with grants.
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
