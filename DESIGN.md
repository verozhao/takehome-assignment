# Design notes: what this is, and the path to production

The README explains what I built and why. This note answers a different question: what
happens if this design is taken into production code, what I would add, in which order, and
what I would keep out.

## What this is

A goal-based agent that calls one tool.

- **Goal:** an answer to the question that is backed by query results.
- **Tool:** `run_sql(purpose, sql)`. The model chooses each query by what it still needs to
  know, reads the result, and decides whether it has enough.
- **State:** the schema and the conversation so far, inside one request. Nothing is kept
  between requests.
- **Limits:** steps, rows, result size and time are hard caps set in code.

It has no retrieval, no reranking, no model routing, no MCP, no self-hosted model, no memory
across requests and no second agent. Each of those solves a specific problem. This service
does not have those problems yet, and every component added also adds ways to fail and
things to evaluate.

## Is it too simple for production?

No. The loop is the part I would keep: one model, one guarded query tool, and the evidence
returned with the answer. What production needs sits around the loop, and most of it is not
AI. In the order I would build it:

1. **Caller identity, with the tenant as the database login.** Today every caller shares
   one read-only role. The model writes the SQL, so no prompt rule can decide who sees which
   rows. The database has to. The usual shortcut is one shared login plus row-level security
   keyed on a session setting. That is not a boundary here. I checked: the guard lets
   `SELECT set_config('app.tenant_id', '42', true)` through, as it must, because it is an
   ordinary `SELECT`. The tenant has to be the login itself. The service can now ask for
   an API key and its record names the caller. Giving each caller their own database login
   is the step that remains.
2. **A route for the data that leaves.** Every step sends the schema and up to 40,000
   characters of rows to an outside API. Client data needs retention and region terms with
   the model provider, or the same model hosted in the firm's own cloud account. Column
   grants keep personal data out at the source. The role script shows it on one column, the
   staff password hashes, and the schema text lists only what the login may read.
3. **A stored record of every answer.** The service appends one line per question to a
   file: the question, the model, a hash of the prompt, each SQL statement with a hash of
   its result, the outcome, the answer and the caller when login is on. Production needs
   a store with retention rules in place of a file. The SQL, the error text of a failed
   query and the answer can all contain values from the data, so the record and the log
   need the same care as the data itself.
4. **Limits per user.** One question is bounded: ten steps, 120 seconds for all its model
   calls and queries together, and caps on rows and result size. Nothing bounds a user yet.
   Production needs rate and spend limits per caller and a cap on concurrent questions.
5. **An eval that gates every change.** Postgres catches invalid SQL. Nothing catches valid
   SQL that answers a different question. The eval here grades twelve questions, can repeat
   each one, and fails its run when any attempt fails. That is the mechanism. Production
   needs a few hundred real questions behind it, run on every change of prompt or model.
6. **Business meaning.** The schema says which columns exist and not what the business
   means by them. This database already shows it: `active = 1` matches 966 customers and
   `activebool` matches 987, so "how many active customers" has two defensible answers.
   The service now passes column comments and a glossary file to the model, and the model
   says when it used a definition. What it cannot supply is the definitions. Someone who
   owns the metric has to decide which of those two columns means active.
7. **The user-facing work.** Streamed steps are in place. Next come provenance for each
   figure, conversation state, and an export for results larger than the row cap.

## The components I left out

For each one: the problem it solves, the trigger that would make me add it, and where it
plugs in.

**Retrieval.** It means three different things here.
- *Over the schema.* The trigger is an eval that shows wrong-table errors as tables grow.
  Size alone is a weak trigger, because a cached schema is cheap to resend. I would add a
  `describe_tables` tool beside `run_sql` and keep a one-line list of tables in the prompt.
  The cached prompt stays stable and the agent can recover from a miss by asking again.
- *Over definitions and verified queries.* The trigger is repeated eval failures on house
  terms. Reviewed question and SQL pairs go into the prompt as worked examples. This is the
  retrieval that pays most in a system like this.
- *Over table rows.* Never, for counts, sums and rankings. Those need the whole table.

**Reranking.** A second pass that reorders retrieved candidates. It only exists after
retrieval does, and only pays when the right item is found but ranked too low to fit.

**Dynamic model selection.** It solves cost and latency. The trigger is a bill someone
objects to, plus an eval that shows a cheaper setting passes a named class of questions. I
would pull the levers in this order: lower effort on the same model, then one cheaper model
per deployment, and a router last. A router that sends a hard question to the weak model
fails silently, and the same question should give the same number on two days.

**MCP.** A protocol for offering tools to model clients. It solves distribution and adds no
capability. The trigger is a second consumer, such as another team's assistant, that wants
the guarded query tool. Then `Database.run_query` gets a thin server around it and the loop
does not change. Inside one service that calls its own function it only adds a boundary.

**Self-hosted models (vLLM).** vLLM serves open-weight models behind an HTTP API. The
trigger is a contract that forbids sending data to an outside provider, after hosting the
same model in the firm's own cloud account was offered and refused. The cost is real: an
accuracy drop on multi-step SQL that the eval must measure first, and GPU operations. The
model call sits in one place, `Analyst.work`, so the first step is a small model
interface with two implementations.

**Conversation memory.** The trigger is the first chat interface. The store holds each
question, its final SQL and its answer, and code builds the earlier turns from those fields.
I would not replay the model's raw turns or let a model write the summary. Figures in a
follow-up should come from fresh queries, because data and permissions change between turns.

**Long-term memory.** The glossary and the verified query library are the long-term memory
worth having, and people review both. Memory that the model writes without review is a way
to repeat one wrong lesson in every later answer.

**Multiple agents.** One agent is enough for questions about one database. The first
variant worth having is a check that recomputes a key figure independently for high-stakes
answers, and I would start that check in code before giving it to a second model.

**Utility-based planning.** The agent follows hard limits and does not weigh alternatives
by cost. If queries get a real price, for example a warehouse billed per scan, the service
should estimate the cost of a query and refuse an expensive one before it runs. That gate
belongs in the service, where it can be tested.

## Showing the work while the user waits

A question takes 5 to 18 seconds. `POST /ask/stream` sends an event at once when the
question arrives, an event when each query finishes, and then the answer. On the film length
question the queries arrived after 3.7 and 8.1 seconds, where the plain endpoint is silent
for 17.

What it shows is the stated purpose of each query, its SQL and what came back. The purpose
is a claim the model wrote. The SQL and the row count are facts. I do not show the model's
reasoning. The API returns only a summary of it, a fluent explanation raises trust whether
or not the answer is right, and it carries provisional numbers an analyst may copy.

I also hold the answer until it is complete and do not stream it word by word. A turn can
still end in a refusal or a cut-off, and the README's rule is that the service never
returns a partial answer.

## Confidence

I would not show a confidence score, for the whole answer or for its parts. A model's stated
confidence is generated text like the rest of the answer. Research on it generally finds
models overconfident, and the most common error in this kind of system is valid SQL for a
question the user did not mean. The model is fully confident in exactly those answers.

What serves trust better, most useful first:

- **The interpretation as its own field.** Metric, population, period and filters, lifted
  out of the prose. It is the only item here that catches a misread question, because the
  analyst sees the reading in seconds.
- **Provenance for each figure, checked by code.** A link from each number in the answer to
  the query and cell it came from, with a count of the figures that could not be traced.
- **Facts the service observed.** Queries run, any result that was cut off, any query that
  failed and was rewritten, and the time the data was read.
- **One status derived from those facts.** Answered, answered with assumptions, the data
  cannot answer, or no answer.
- **A way to tie the figure out.** A control total beside each breakdown and an export of
  the underlying rows. Analysts trust a number they can reconcile.
- **Measured agreement for high-stakes questions.** Run the question twice and compare the
  results. Report disagreement as disagreement.

Each has a limit. Tracing proves a number exists in the rows and not that the query answered
the question. Two runs can share the same misreading.

## What I would not build

- A second version of this service with stubs for retrieval, MCP, routing and self-hosting.
  Components that are never exercised cannot be tested or evaluated. This note and one
  working slice show the path better.
- Any tool that sends data out, such as web search, email or write-back. Today the agent
  can send data nowhere except the answer, and that is its best security property.
- A cache that reuses a past answer for a similar question. Two near-identical questions
  with different dates or tenants must never share an answer.
- A framework around the loop. The loop is about 60 lines and every step is visible.
- Fine-tuning, until an eval shows a ceiling that definitions and examples cannot move.
- Memory that the model writes without review.
- A confidence percentage.
