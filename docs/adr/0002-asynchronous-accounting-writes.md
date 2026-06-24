# 0002 — Accounting writes are asynchronous and eventually consistent

- Status: Accepted
- Date: 2026-06-23

## Context

TokenCost logs every proxied request's token usage to a single-file SQLite
`tracker.db`. The write (`db.save_request`) ran synchronously on the proxy's
asyncio event loop — inline in the request handler. Two problems followed:

1. Under concurrent access (proxy vs. the `import_history` sync daemon, or
   concurrent proxy requests), writes intermittently failed with
   `OperationalError: database is locked` and the usage row was silently dropped.
   Root cause: the default rollback-journal mode returns `SQLITE_BUSY` immediately
   on a lock-upgrade deadlock, bypassing the connection's busy-timeout.
2. Any wait inside the write blocked the event loop, stalling *all* concurrent
   agent requests — and these are agentic clients (Claude Code, etc.) whose
   latency must not depend on metric bookkeeping.

The usage rows are dashboard metrics only; no agent decision depends on them, and
much of the data is independently re-derivable by the importer from source JSONL.

## Decision

Accounting writes are **asynchronous and eventually consistent**:

- The request handler enqueues a usage record on a bounded in-process queue
  (`put_nowait`, O(1)) and returns. The agent never waits on the DB.
- A single daemon **writer thread** drains the queue and calls the (synchronous)
  `db.save_request`. One writer ⇒ no proxy-internal write contention.
- All `tracker.db` connections use **WAL + `busy_timeout=3000`** via `db._connect`,
  so the off-path writer waits out the importer instead of failing.
- On a failed write or a full queue: **log and drop the row** — no spill file, no
  retry loop. On graceful shutdown the queue is drained (bounded join); a hard
  kill may lose the in-memory backlog.

## Consequences

- An agent request is never blocked or failed by `tracker.db`.
- The dashboard may lag the live request by milliseconds (seconds under a stall);
  acceptable for usage metrics.
- Rows can be lost on a hard process kill or sustained DB outage — an accepted
  trade-off for metric data, not billing data (cost is notional; see ADR-0001 and
  CONTEXT.md).
- A single writer thread is a single point of failure, mitigated by a crash-proof
  per-row loop: a bad row is logged and skipped, never killing the thread.
- Rejected alternatives: synchronous write with long busy_timeout (blocks the loop);
  exponential-backoff retry (redundant with busy_timeout, stacks into multi-second
  stalls); spill/dead-letter file (over-engineering for eventually-consistent
  metrics); `aiosqlite` (a dependency that just hides the same thread+queue).
