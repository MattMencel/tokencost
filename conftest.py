"""Shared pytest fixtures for the TokenCost suite.

Adds the repo root to sys.path so `import proxy`, `import db`, etc. resolve, and
provides the DB fixtures every DB-touching test reuses:

- ``tmp_db``        — points ``db.DB_PATH`` at a fresh temp file, runs init_db(),
                      cleans up afterwards. Never touches the live tracker.db.
- ``seed_requests`` — a factory that inserts rows into the temp DB with explicit
                      timestamps (save_request stamps ts=now(), which tests can't
                      control, so we insert raw SQL here).
"""
import datetime
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import db  # noqa: E402  (after sys.path tweak)


@pytest.fixture()
def tmp_db(monkeypatch):
    """Fresh, initialised SQLite DB pointed at by db.DB_PATH for one test."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    yield path
    os.unlink(path)


# Columns a seeded request row may set; everything else takes a DB default.
_SEED_DEFAULTS = {
    "source": "cli",
    "model": "claude-opus-4-8",
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "cache_creation_1h_tokens": 0,
    "cost_usd": 0.0,
    "duration_ms": 100,
    "status": 200,
    "user_agent": "",
    "stop_reason": None,
    "tool_call_count": 0,
    "tools_json": None,
    "effort": "standard",
    "prompt_preview": "",
    "msg_uuid": None,
    "auto_thinking": 0,
    "optimizations_json": None,
    "optimizer_savings_usd": 0.0,
}


@pytest.fixture()
def seed_requests(tmp_db):
    """Return ``insert(ts=..., **cols)`` to add request rows to the temp DB.

    ``ts`` accepts a datetime or ISO string; omit it to stamp a fixed baseline.
    Any column in ``_SEED_DEFAULTS`` can be overridden by keyword.
    """
    base = datetime.datetime(2026, 1, 1, 12, 0, 0)

    def insert(ts=None, **cols):
        if ts is None:
            ts = base
        if isinstance(ts, datetime.datetime):
            ts = ts.isoformat()
        row = dict(_SEED_DEFAULTS)
        unknown = set(cols) - set(row)
        if unknown:
            raise KeyError(f"unknown seed column(s): {sorted(unknown)}")
        row.update(cols)
        con = sqlite3.connect(tmp_db)
        con.execute(
            "INSERT INTO requests "
            "(ts,source,model,input_tokens,output_tokens,cache_read_tokens,"
            "cache_creation_tokens,cache_creation_1h_tokens,cost_usd,duration_ms,"
            "status,user_agent,stop_reason,tool_call_count,tools_json,effort,"
            "prompt_preview,msg_uuid,auto_thinking,optimizations_json,"
            "optimizer_savings_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, row["source"], row["model"], row["input_tokens"],
             row["output_tokens"], row["cache_read_tokens"],
             row["cache_creation_tokens"], row["cache_creation_1h_tokens"],
             row["cost_usd"], row["duration_ms"], row["status"],
             row["user_agent"], row["stop_reason"], row["tool_call_count"],
             row["tools_json"], row["effort"], row["prompt_preview"],
             row["msg_uuid"], row["auto_thinking"], row["optimizations_json"],
             row["optimizer_savings_usd"]),
        )
        con.commit()
        con.close()

    return insert
