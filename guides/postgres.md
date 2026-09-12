---
topic: postgres
triggers: writing SQL, schema or migrations, connection pooling, a job queue or background worker, an upsert, a race between two writers, a slow query, deploying a schema change on Railway
source: docs/Update_Railway_PostgreSQL_Instructions.md
verified: 2026-09-11
---

# PostgreSQL

Ordered by how expensive the mistake is. Most of these are silent: the code
looks right and loses data under concurrency.

## Connections

One pool per process per destination, created at startup, closed at graceful
shutdown. A pool per request, per repository object or per server render is the
most common cause of connection exhaustion.

A pool limit is local to one process. The real ceiling is:

```
peak instances x processes per instance x pool max
  + dedicated listeners + workers outside those pools
  + migration, maintenance and monitoring connections
```

Four instances, two processes each, pool of six is 48 connections before any
worker. A rolling deploy runs old and new at once, so double it during the
overlap.

Raising `max_connections` usually increases resource pressure without improving
throughput. Bound application concurrency, not just socket count.

### Timeouts are several different limits

| Limit | Bounds |
|---|---|
| Connection establishment | DNS, TCP, TLS, auth |
| Pool acquisition | Waiting for a free connection |
| `lock_timeout` | One lock acquisition wait |
| `statement_timeout` | Statement execution, including waits |
| `idle_in_transaction_session_timeout` | A client stalled inside a transaction |
| Request deadline | The whole operation, retries included |

Keep lock waits shorter than the statement budget when failing fast on
contention is what you want. Interactive traffic and migrations need different
budgets. There is no universally correct five-second timeout.

Racing a query against an application-side timer abandons the caller while the
query keeps running and keeps its locks. Cancellation has to reach the driver.
Never return a connection to the pool with an unresolved operation on it.

### PgBouncer

Transaction pooling breaks anything that assumes session state survives:
session `SET`, session advisory locks, `LISTEN`, SQL `PREPARE`/`EXECUTE`,
persistent temp tables. Put `LISTEN` on a dedicated direct connection, and run
migrations over a session-affinity route.

Set per-request state transaction-locally on the checked-out connection. Do not
blanket-disable prepared statements on old advice; test the actual driver and
pooler combination.

## Transactions

Keep the boundary tight around the invariant. No network calls, uploads, model
inference or long computation inside a transaction.

Every statement in a transaction must run on the same checked-out connection.
Issuing `BEGIN` through a pool does not put subsequent pool calls in that
transaction.

A network failure during `COMMIT` leaves the outcome **unknown**. Do not assume
rollback and retry a non-idempotent action. Reconcile through a stable operation
id.

### Pick the mechanism that matches the race

| Race | Mechanism |
|---|---|
| Duplicate logical record | Unique constraint plus explicit conflict handling |
| Increment or decrement | Atomic SQL update with the bound in the SQL |
| Stale edit | Compare-and-swap on a version column |
| Short multi-step edit of existing rows | Row locks, same lock order everywhere |
| Rule across a set of rows | Serializable, with whole-transaction retry |
| Singleton maintenance action | Advisory lock, same key everywhere |

Read-then-write in application code is not made safe by wrapping it in a
transaction. Put the condition in the statement:

```sql
UPDATE app.inventory
SET available = available - $3::integer
WHERE tenant_id = $1::uuid AND sku = $2::text
  AND $3::integer > 0 AND available >= $3::integer
RETURNING available;
```

Zero rows means it did not happen. Never turn a zero-row result into a success.

### Isolation promises less than people think

Read Committed takes a fresh snapshot **per statement**, so two statements in
one transaction can see different data. Repeatable Read gives a stable snapshot
and still allows write skew. Serializable rejects conflicting executions and
requires you to retry the whole transaction.

`SELECT ... FOR UPDATE` locks the rows it returns. It cannot lock a row that
does not exist yet, so absence and range rules need a unique or exclusion
constraint, or a lock on a common parent.

### Retry by error code, in one place

| Code | Response |
|---|---|
| `40001` serialization failure | Retry the whole transaction, reads included |
| `40P01` deadlock | Bounded whole-transaction retry, then inspect lock order |
| `23505` unique violation | Resolve the business conflict; do not blind-retry |
| `23503` / `23514` | Bad input or a real defect |
| `55P03` lock unavailable | Depends whether NOWAIT was deliberate |
| `57014` query canceled | Establish who cancelled before retrying |
| class `08` | Reconnect, then reconcile any ambiguous write |

Exponential backoff with jitter, an attempt ceiling, and an overall deadline.
Retries at the HTTP, service, ORM, transaction and queue layers multiply: four
layers retrying three times is 81 executions of one request. Own the retry at
one layer.

Log error codes and operation ids, never SQL parameters holding customer data.

### Upserts

`INSERT ... ON CONFLICT` with an explicit business key. Choose which columns a
duplicate may update and which are immutable, so a late-arriving older event
cannot overwrite a newer one, and a null cannot replace a real value.

`DO NOTHING RETURNING` returns nothing for a duplicate, so the clever
one-statement insert-or-select CTE returns no row under contention. Follow with
a separate statement on a fresh snapshot. `MERGE` has different concurrency
semantics and is not a drop-in.

## Background work

An idempotency key identifies the logical operation, not the delivery attempt.
Reusing a key with different input is a conflict, not a cache hit.

For work entirely inside Postgres: begin, insert the idempotency record under a
unique key, do the mutation and record the result in the same transaction,
commit. Never commit a "processed" flag before the work — the crash window
between them loses the job silently.

For an external effect, use a transactional outbox: write the business change
and the outbox row in one transaction, and let a dispatcher deliver it after.
Delivery is then **at least once**, because the dispatcher can crash after the
external call and before the acknowledgement. The receiver needs an idempotency
key or deduplication; no transaction can make an external API call atomic.

### A jobs table that survives a crashed worker

Claim with `FOR UPDATE SKIP LOCKED`, a bounded batch, a fresh unpredictable
lease token, and a lease expiry. Commit the claim before doing the work:

```sql
WITH candidates AS (
  SELECT id FROM app.jobs
  WHERE status = 'ready' AND run_at <= now()
  ORDER BY run_at, id
  LIMIT $2::integer
  FOR UPDATE SKIP LOCKED
)
UPDATE app.jobs AS j
SET status = 'running', attempts = j.attempts + 1,
    lease_token = $1::uuid,
    lease_until = clock_timestamp() + interval '60 seconds'
FROM candidates AS c WHERE j.id = c.id
RETURNING j.id, j.payload, j.lease_token;
```

Completion must prove current ownership, or a stale worker that woke up late
marks someone else's job done:

```sql
UPDATE app.jobs SET status = 'done', lease_token = NULL, lease_until = NULL
WHERE id = $1::bigint AND status = 'running'
  AND lease_token = $2::uuid AND lease_until > clock_timestamp()
RETURNING id;
```

Zero rows means it no longer owns the job. A recovery worker returns expired
leases to `ready` with backoff, or to `dead` past the attempt limit.

The lease protects queue ownership. It does not undo an external side effect a
paused worker performs on waking. `SKIP LOCKED` deliberately gives an incomplete
view and must never be used for a general consistent read.

`SKIP LOCKED` across several workers gives no FIFO guarantee, globally or per
conversation. Serialize only the scope that genuinely needs ordering.

`LISTEN`/`NOTIFY` wakes workers. It is not a replayable queue; the table stays
the source of truth and a reconnecting listener must reconcile.

## Queries and indexes

Parameterize values. Identifiers and sort directions cannot be parameters, so
allowlist them. Never concatenate user or model output into SQL.

Count database calls per operation to find N+1. Watch for a join that multiplies
rows and corrupts pagination or aggregates.

Keyset pagination, not `OFFSET`, for anything large:

```sql
SELECT id, body, created_at FROM app.messages
WHERE tenant_id = $1::uuid AND conversation_id = $2::uuid
  AND (created_at, id) < ($3::timestamptz, $4::bigint)
ORDER BY created_at DESC, id DESC
LIMIT $5::integer;
```

The unique tie-breaker is what makes the order unambiguous. Preserve full
timestamp precision in the cursor: rounding through JavaScript milliseconds
skips rows. Bind the cursor to the authorized tenant.

Index selection:

| Index | Fits | Cost |
|---|---|---|
| B-tree | Equality, range, ordering, uniqueness | Write and storage |
| Composite | Combined filter and order | Column order must match the workload |
| Partial | A stable hot subset, such as ready jobs | Query predicate must imply the index predicate |
| Expression | A consistently used expression | Query must use a compatible expression |
| GIN | JSONB containment, arrays, full text | Write and maintenance cost |
| BRIN | Huge, physically correlated tables | Lossy, rechecks |

Leading equality columns, then range and order columns. "Most selective first"
is not the rule. A partial index on `status = 'ready'` needs that predicate
literal in the query; a generic `status = $1` plan cannot prove it applies.

Primary and unique constraints already build indexes. Check existing definitions
before adding another. Do not drop an index because `idx_scan = 0` — statistics
reset, replicas differ, and constraints need theirs.

Read plans as evidence: `EXPLAIN (ANALYZE, BUFFERS, SETTINGS)`, comparing
estimated against actual rows, loops, rows removed by filter, and sort spills. A
sequential scan is the right plan for a small table.

`EXPLAIN ANALYZE` **executes the statement**. Wrapping it in `ROLLBACK` does not
remove the load, the locks, the WAL, the sequence advance, or any external
function effect.

A fast plan still coexists with slow pool acquisition, too many round trips, or
large result decoding. Measure end to end.

## Migrations

Migrations are the authoritative history: in version control, generated SQL
reviewed, tested by rebuilding from scratch **and** by upgrading a representative
existing database. Never edit an applied shared migration; add a corrective one.

One runner, serialized across competing deploys. A pre-deploy hook on every
service is not a global singleton, and running migrations from every replica's
startup path is a race. Verify the framework's migration lock works through your
pooler.

## Railway

Query real state rather than inferring it from config: `railway status --json`,
`railway variables`, `railway logs`. `railway run` injects the live service
variables. Confirm whether PgBouncer is actually deployed rather than assuming.
After shipping, wait for a healthy terminal state and read the runtime logs for
the new commit.
