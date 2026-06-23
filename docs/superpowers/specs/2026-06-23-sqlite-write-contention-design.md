# Design — Fix SQLite write contention on `tracker.db`

**Date:** 2026-06-23
**Branch:** `fix/sqlite-write-contention` (off `main`)
**Status:** approved (brainstorming)
**Tracker task:** #19

---

## Problem

Concurrent writers to the single-file SQLite `tracker.db` occasionally fail with
`OperationalError: database is locked`. When this happens the HTTP response to the client is
unaffected, but that request's **usage/cost row is silently dropped** — `save_request` (`db.py`)
raises inside the proxy's `stream_upstream` `finalize` closure, which swallows and logs it:

```
[stream] finalize/accounting error (response unaffected): OperationalError: database is locked
```

Observed 3× in ~24h over ~3000 requests/day. The streaming work didn't cause this; its new
`[stream]` diagnostics merely made a pre-existing, previously-silent failure visible.

### The two writers

- **Proxy** (`db.py` `save_request`, via `proxy.py` `_record`) — frequent, short writes, one per
  request, possibly concurrent with itself (FastAPI async + threadpool).
- **Sync daemon** (`import_history.py` `import_all`, launchd `com.tokencost.sync`) — infrequent, but
  opens **one connection** (line ~710) held across the entire multi-provider import, committing
  per-provider.

### Root cause (corrected)

The handoff hypothesised "no `busy_timeout`". That is imprecise: Python's
`sqlite3.connect()` defaults to `timeout=5.0`, which already sets a 5s busy-timeout — a bare connect
*already* waits 5s for a lock. Getting `database is locked` *despite* that points to
**rollback-journal lock-upgrade deadlocks**: in the default (non-WAL) journal mode, when one
connection holds a read lock and another needs to upgrade to a write lock (or the long-running import
holds the write lock), SQLite returns `SQLITE_BUSY` **immediately, ignoring the timeout**, because
waiting would deadlock. **WAL mode structurally removes this** — readers never block the single
writer, and the writer doesn't deadlock on lock upgrade. WAL is therefore the primary fix; the other
layers are the safety net.

---

## Goal & durability bar

Near-zero loss, with a strong incremental fallback so a write almost never fails — and when it does,
it fails **safely**: the HTTP response is unaffected, nothing crashes, and the row is **recoverable**
(lands on a subsequent write). The request's critical path must not be held hostage to the DB.

---

## Approach: layered defense

Four layers, each catching what the previous misses. Scope covers **both writers**.

### Layer 1 — shared connection helper (`db.py`)

Add a single helper that all connections route through:

```python
def _connect(path=None):
    con = sqlite3.connect(path or DB_PATH, timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")   # explicit; matches the connect timeout
    con.execute("PRAGMA synchronous=NORMAL")  # WAL-safe, faster than FULL
    return con
```

- **Resolve `DB_PATH` at call time**, NOT as a default-arg value (`path=None` → `path or DB_PATH`),
  so tests that monkeypatch `db.DB_PATH` work. (A `def _connect(path=DB_PATH)` default would bind the
  live path at import time and defeat the `tmp_db` fixture.)
- Replace every bare `sqlite3.connect(DB_PATH)` in `db.py` (~22 sites) with `_connect()`.
- WAL is a persisted DB property (stored in the header) — setting it once suffices, but applying it
  per-connection is harmless belt-and-suspenders and keeps every connection self-consistent.
- `_open_sqlite_ro` in `import_history.py` (reads *other apps'* source DBs in read-only mode) is
  **left untouched** — it is unrelated to `tracker.db` write contention.

### Layer 2 — exponential backoff on the hot write (`db.py` `save_request`)

Wrap the `INSERT` (+ `commit`) in `save_request` — **not** the `connect()` — in a bounded
exponential-backoff retry. The lock surfaces on the write/commit, not on opening the connection.

- `max_retries=5`, `base_delay=0.1`, `max_delay=5.0`.
- Delay = `min(base_delay * 2**attempt, max_delay)`, plus ±10% jitter (avoid thundering-herd between
  concurrent proxy writes).
- Retry only when the `sqlite3.OperationalError` message contains `"locked"` / `"busy"`; re-raise
  anything else immediately.

### Layer 3 — append-only spill (the "strong incremental fallback")

If `save_request` *still* fails after all retries, do not lose the row:

- **Spill:** append the call's arguments as one JSON line to `tracker.spill.jsonl` (path derived from
  `DB_PATH` at call time, so `tmp_db` tests get a temp spill). The full `save_request` argument set is
  serialized so a drain is a faithful replay.
- **Drain:** re-insert each spilled line via the same `INSERT OR IGNORE`, then truncate the spill
  file. Drain runs (a) at the start of the next successful `save_request`, and (b) on proxy startup
  (`lifespan`, after `init_db()`).
- **Concurrency:** a module-level `threading.Lock` guards spill write + drain so concurrent in-process
  saves can't double-drain or interleave a partial write. Spilled rows are by definition *not* in the
  DB, so draining once and truncating is safe; `INSERT OR IGNORE` keeps replay idempotent for rows
  that carry a `msg_uuid`.
- **Failure of the spill itself** is caught and logged but never propagated — the response stays
  unaffected (same contract as today, but now loss is near-impossible rather than routine).

### Layer 4 — sync daemon (`import_history.py`)

Route the `tracker.db` write connection (line ~710 `import_all`) through the shared helper so it is
WAL-consistent and shares the busy-timeout. With WAL + busy_timeout on **both** sides, whichever
writer is momentarily blocked now *waits* rather than failing instantly. `import_all` already commits
per-provider, keeping write-lock hold time short; no deeper restructuring of its connection lifecycle
is needed. Reuse `db._connect` directly (`from db import _connect`, called with `import_history`'s own
`DB_PATH`) so there is a single source of truth for the PRAGMAs — do not duplicate the pragma list.

---

## Files touched

| File | Change |
|------|--------|
| `db.py` | Add `_connect()`; route ~22 connect sites through it; add backoff + spill to `save_request`; add spill-drain helper |
| `proxy.py` | Drain spill on startup in `lifespan` (after `init_db()`) |
| `import_history.py` | Route `tracker.db` write connection through shared WAL/busy_timeout setup |
| `.gitignore` | Add `tracker.db-wal`, `tracker.db-shm`, `tracker.spill.jsonl` |
| `VERSION`, `RELEASE.md` | Bump version; changelog entry |
| `tests/` | New tests (below) |

---

## Testing

Use existing `tmp_db` / `seed_requests` fixtures in `conftest.py`. **Never** touch the live
`tracker.db`.

1. **WAL applied:** `_connect()` on a fresh DB → `PRAGMA journal_mode` reads back `wal`;
   `PRAGMA busy_timeout` reads back the configured value.
2. **Backoff success:** a connection/cursor stub raises `OperationalError("database is locked")` once,
   then succeeds → `save_request` inserts exactly one row, no exception. (Patch `time.sleep` to keep
   the test fast.)
3. **Backoff exhaustion → spill:** writes always raise `locked` → after `max_retries`, the row is
   appended to the spill file, `save_request` returns normally (no raise).
4. **Drain idempotency:** seed a spill file with N rows → drain inserts N rows and truncates the spill;
   a second drain is a no-op; replaying a row with an existing `msg_uuid` does not duplicate.
5. **Concurrency / zero-loss:** N threads call `save_request` while a simulated long writer holds the
   lock; assert `rows_in_db + rows_in_spill == rows_attempted` (no row lost). After releasing the
   writer and draining, `rows_in_db == rows_attempted`.
6. **`import_all` smoke:** import path opens its `tracker.db` connection in WAL mode (read back
   `journal_mode`).

Baseline before this work: 345 passing (`./run-tests.sh`). All new tests must pass alongside.

---

## Non-goals / YAGNI

- **No single-writer queue / writer process / IPC** (Approach C). The proxy daemon and the sync
  launchd job are separate processes; cross-process serialization is heavy for a personal,
  single-machine tool and unnecessary once WAL removes the structural deadlock.
- **No change to sync cadence or the launchd plist.** WAL + short transactions make concurrent
  operation safe; retiming the daemon is not required.
- **No schema or pricing changes.**

---

## Risks & notes

- **WAL sidecar files** (`tracker.db-wal`, `tracker.db-shm`) appear next to the DB — must be
  gitignored (done in this change). They are normal and auto-managed; `wal_autocheckpoint` defaults
  (1000 pages) are fine for this volume.
- **WAL requires all accessors on the same host** (no network filesystem). True here (single
  machine), so this is safe.
- **`synchronous=NORMAL`** under WAL can lose the *last* committed transaction only on OS crash /
  power loss (not on app crash). Acceptable for usage analytics; the spill is not a WAL substitute and
  doesn't change this.
- Behavior change → **bump `VERSION`** and update `RELEASE.md` per the `CLAUDE.md` pre-deploy
  checklist. Keep runtime artifacts gitignored; no personal paths in tracked files.
