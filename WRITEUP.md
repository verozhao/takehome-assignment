# Write-up

## 1. Process, assumptions and decisions

**Understand the data first.** I started Pagila, read the schema and wrote reference SQL
for the four sample questions by hand, so I knew the right answers before the model
answered anything. That showed where the questions are hard. Titles are stored in capitals.
Two actors share a name. The top Children actors tie. Two customers have no payments.
Release years run from 2006 to 2024 and film length does not move across them.

Assumptions:
- "Whatever is contained in the database" means the service must not be written for Pagila.
- Read access is enough. The service never needs to change data.
- Each question stands alone. I did not build conversation memory.

**Pick the architecture.** I chose an agent loop with a single SQL tool. A single
text-to-SQL call is cheaper and faster, but it cannot look at the data before it answers and
cannot recover from a bad query. RAG fits documents better than counts and rankings. The
trade-off I accepted is 5 to 18 seconds and a few cents per question.

**Give the model the schema.** The service reads the catalog at startup and puts the whole
schema in the system prompt, which is cached. This is simple and enough for about 25 tables
and views. It would not scale to hundreds without a table selection step.

**Make database access safe.** The service connects as a read-only role, runs each query in
a read-only transaction, lets Postgres itself validate the statement, and caps steps, rows,
result size and time. My first plan used a SQL parsing library. I dropped it because the
database already enforces almost everything it would have checked. The review later found
the exception, `EXPLAIN ANALYZE`, which needed one extra check.

**Design the response.** The API returns the answer with every query, its purpose and its
rows, so a reader can check any number. If the model cannot finish, the API returns an
error. It never returns a partial answer.

**Test at three levels.** Unit tests drive the loop with a scripted model. Database tests
run against Pagila, including a list of statements that must be rejected. A live eval asks
the sample questions and compares the answers with reference SQL.

**Tools.** I used an AI coding assistant throughout, as the FAQ allows. I used it to
critique the design from several angles, to draft code that I then read and tested, and to
run an adversarial review of the finished code. I checked each review finding before acting
on it. The review found real problems. The most serious was that `EXPLAIN ANALYZE` can
create a table inside a read-only transaction, which the service now refuses.

## 2. Am I happy with the solution?

Mostly yes. It answers the four sample questions correctly, including the parts that are
easy to get wrong: the three-way tie, the two customers with no payments, and the fact that
the data shows no growth in film length. It is safe by default, every answer can be
audited, and the code is small enough to read in one sitting.

I am less happy with three things. The eval has only eight questions, and three of them have
no reference answer, so I judged those by reading. A question takes 5 to 18 seconds. The
streaming endpoint shows each query as it runs, but the answer still arrives in one piece.
Follow-up questions do not work because requests are independent.

## 3. What would I do differently?

- Write the eval set before the agent and make it larger, so every prompt change is measured.
- Compare cheaper models and lower effort settings against that eval before settling on the
  most capable model.
- Return the reading of the question as its own field, and trace each figure in the answer
  to its query in code. `DESIGN.md` explains why I prefer that to a confidence score.
- Add conversation state for follow-up questions.

## 4. Where did I get stuck?

**Database setup.** The assignment notes say Pagila loads into the default `postgres`
database. The current Pagila compose file needs Postgres 18 and loads into a database called
`pagila`. On my machine the container also stopped halfway through its init scripts. I read
the container log, found the failing step and ran the last two steps by hand. I then made
the service fail at startup with a clear message when it finds no tables, since the next
person will hit the same thing.

**How to check the model's SQL.** I went back and forth between parsing the SQL myself and
trusting the database. What settled it was a list of writes and non-query statements, run
once as the read-only role and once as the superuser. Preparing each statement, refusing
any that returns no columns, and running the rest in a read-only transaction rejected all
of them, with less code than a parser. The review later added `EXPLAIN` and placeholders to
that list, and each needed one more check.

**A wrong assumption about the data.** From the classic version of this dataset I expected
every film to carry the same release year, which would have made the film length question
unanswerable. My reference query showed 19 release years, from 2006 to 2024. The prompt
asks the model for the same check: look at what a time column covers before describing a
trend.
