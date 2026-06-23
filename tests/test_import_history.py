"""Regression-safety suite for import_history.py.

Characterization tests: pin CURRENT behavior so regressions are caught immediately.
Crown jewels:
  (a) msg_uuid dedup via INSERT OR IGNORE + partial UNIQUE INDEX prevents double-counting.
  (b) _proxy_cutoff() boundary stops history from re-counting live-proxy records.

IMPORTANT: these tests NEVER touch the live tracker.db.  Every test creates its
own in-memory or temp-file sqlite3 connection and patches import_history.DB_PATH
(via monkeypatch) so any code path that opens the DB by path also goes to the temp file.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

import db  # noqa: F401  (fixture dependency)
import import_history as ih


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_db(monkeypatch) -> tuple[str, sqlite3.Connection]:
    """Create a fresh temp DB, init it via db.init_db(), patch both db.DB_PATH
    and import_history.DB_PATH, and return (path, open_connection)."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    path = f.name
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(ih, "DB_PATH", Path(path))
    db.init_db()
    conn = sqlite3.connect(path)
    ih.ensure_schema(conn)
    return path, conn


def _make_jsonl(lines: list[dict], tmp_path: Path) -> str:
    """Write a list of dicts as a JSONL file and return its path."""
    p = tmp_path / "fixture.jsonl"
    p.write_text("\n".join(json.dumps(d) for d in lines) + "\n", encoding="utf-8")
    return str(p)


def _assistant_line(
    msg_id: str,
    timestamp: str,
    model: str = "claude-sonnet-4-5",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_write: int = 0,
    cache_read: int = 0,
    stop_reason: str = "end_turn",
    tools: list | None = None,
) -> dict:
    """Build a well-formed Claude CLI JSONL assistant-type line."""
    content = []
    if tools:
        content = [{"type": "tool_use", "name": t} for t in tools]
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
            },
            "content": content,
            "stop_reason": stop_reason,
        },
    }


def _user_line(text: str) -> dict:
    """Build a Claude CLI JSONL user-type line."""
    return {"type": "user", "message": {"content": text}}


# ─────────────────────────────────────────────────────────────────────────────
# 1. calc_cost and _price
# ─────────────────────────────────────────────────────────────────────────────

class TestCalcCost:
    """calc_cost(model, input_tok, output_tok, cache_write, cache_read).

    Note: this is import_history.calc_cost, whose signature differs from
    db.calc_cost (different cache-write multiplier and no 1h TTL split).
    """

    MODEL = "claude-sonnet-4-5"  # input=$3/M, output=$15/M

    def test_input_tokens_only(self):
        # 1 M input @ $3/M → $3.0
        assert ih.calc_cost(self.MODEL, 1_000_000, 0, 0, 0) == pytest.approx(3.0)

    def test_output_tokens_only(self):
        # 1 M output @ $15/M → $15.0
        assert ih.calc_cost(self.MODEL, 0, 1_000_000, 0, 0) == pytest.approx(15.0)

    def test_cache_write_tokens(self):
        # cache_write cost = input * 1.25 → $3 * 1.25 = $3.75 per M
        assert ih.calc_cost(self.MODEL, 0, 0, 1_000_000, 0) == pytest.approx(3.75)

    def test_cache_read_tokens(self):
        # cache_read cost = input * 0.10 → $3 * 0.10 = $0.30 per M
        assert ih.calc_cost(self.MODEL, 0, 0, 0, 1_000_000) == pytest.approx(0.30)

    def test_combined(self):
        # 100 input + 50 output, no cache, standard model
        expected = (100 * 3.0 + 50 * 15.0) / 1_000_000
        assert ih.calc_cost(self.MODEL, 100, 50, 0, 0) == pytest.approx(expected)

    def test_zero_tokens_is_zero(self):
        assert ih.calc_cost(self.MODEL, 0, 0, 0, 0) == pytest.approx(0.0)

    def test_unknown_model_falls_back_to_default(self):
        # "default" pricing is input=$3.0/M, output=$15.0/M (same as sonnet-4-5)
        from db import PRICING
        default_p = PRICING["default"]
        expected = 1_000_000 * default_p["input"] / 1_000_000
        assert ih.calc_cost("totally-unknown-model-xyz", 1_000_000, 0, 0, 0) == pytest.approx(expected)

    def test_prefix_match_resolves_model(self):
        # "claude-sonnet-4-5-20250514" should prefix-match "claude-sonnet-4-5"
        # ... unless there's an exact match first. Regardless, it must not return 0.
        result = ih.calc_cost("claude-sonnet-4-5-20250514", 1_000_000, 0, 0, 0)
        assert result > 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 2. ensure_schema and _col_exists
# ─────────────────────────────────────────────────────────────────────────────

class TestEnsureSchema:
    """ensure_schema(conn) must add msg_uuid column and create the partial UNIQUE index."""

    def _base_conn(self) -> sqlite3.Connection:
        """Open an in-memory DB with the minimum table definition (no msg_uuid)."""
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, source TEXT, model TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                status INTEGER DEFAULT 200,
                user_agent TEXT, stop_reason TEXT,
                tool_call_count INTEGER DEFAULT 0,
                tools_json TEXT,
                prompt_preview TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        return conn

    def test_adds_msg_uuid_column(self):
        conn = self._base_conn()
        assert not ih._col_exists(conn, "msg_uuid")
        ih.ensure_schema(conn)
        assert ih._col_exists(conn, "msg_uuid")

    def test_idempotent_second_call(self):
        """Calling ensure_schema twice must not raise."""
        conn = self._base_conn()
        ih.ensure_schema(conn)
        ih.ensure_schema(conn)  # should not raise
        assert ih._col_exists(conn, "msg_uuid")

    def test_unique_index_created(self):
        conn = self._base_conn()
        ih.ensure_schema(conn)
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(requests)").fetchall()}
        assert "idx_msg_uuid" in indexes

    def test_col_exists_false_for_missing_column(self):
        conn = self._base_conn()
        assert not ih._col_exists(conn, "nonexistent_column_xyz")

    def test_col_exists_true_for_known_column(self):
        conn = self._base_conn()
        assert ih._col_exists(conn, "ts")


# ─────────────────────────────────────────────────────────────────────────────
# 3. _insert dedup
# ─────────────────────────────────────────────────────────────────────────────

class TestInsertDedup:
    """INSERT OR IGNORE with partial UNIQUE INDEX on msg_uuid WHERE NOT NULL."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.path, self.conn = _make_db(monkeypatch)
        yield
        self.conn.close()
        os.unlink(self.path)

    def _insert(self, msg_uuid, ts="2026-01-01T10:00:00"):
        return ih._insert(
            self.conn,
            ts=ts,
            source="claude-cli-history",
            model="claude-sonnet-4-5",
            input_tok=100,
            output_tok=50,
            cache_read=0,
            cache_write=0,
            stop_reason="end_turn",
            tools=[],
            tool_count=0,
            msg_uuid=msg_uuid,
        )

    def test_same_uuid_second_insert_is_ignored(self):
        r1 = self._insert("msg_dupe001")
        r2 = self._insert("msg_dupe001")
        assert r1 == 1
        assert r2 == 0  # INSERT OR IGNORE fired

    def test_same_uuid_only_one_row_in_db(self):
        self._insert("msg_dupe002")
        self._insert("msg_dupe002")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM requests WHERE msg_uuid='msg_dupe002'"
        ).fetchone()[0]
        assert count == 1

    def test_different_uuids_both_inserted(self):
        r1 = self._insert("msg_aaa001", ts="2026-01-01T10:00:00")
        r2 = self._insert("msg_bbb002", ts="2026-01-01T10:01:00")
        assert r1 == 1
        assert r2 == 1
        count = self.conn.execute(
            "SELECT COUNT(*) FROM requests WHERE msg_uuid IN ('msg_aaa001','msg_bbb002')"
        ).fetchone()[0]
        assert count == 2

    def test_null_uuid_not_deduped(self):
        """Partial index is WHERE msg_uuid IS NOT NULL — NULL values are never deduped."""
        r1 = self._insert(None, ts="2026-01-01T10:00:00")
        r2 = self._insert(None, ts="2026-01-01T10:00:00")
        assert r1 == 1
        assert r2 == 1  # second NULL also inserts — no unique constraint applies
        count = self.conn.execute(
            "SELECT COUNT(*) FROM requests WHERE msg_uuid IS NULL"
        ).fetchone()[0]
        assert count == 2

    def test_cost_calculated_when_no_override(self):
        """_insert must compute cost from calc_cost when cost_override is None."""
        ih._insert(
            self.conn,
            ts="2026-01-01T10:00:00",
            source="claude-cli-history",
            model="claude-sonnet-4-5",
            input_tok=1_000_000,
            output_tok=0,
            cache_read=0,
            cache_write=0,
            stop_reason="end_turn",
            tools=[],
            tool_count=0,
            msg_uuid="msg_cost_check",
        )
        row = self.conn.execute(
            "SELECT cost_usd FROM requests WHERE msg_uuid='msg_cost_check'"
        ).fetchone()
        assert row is not None
        # 1M input @ claude-sonnet-4-5 → $3.0
        assert row[0] == pytest.approx(3.0)

    def test_cost_override_used_when_provided(self):
        ih._insert(
            self.conn,
            ts="2026-01-01T10:00:00",
            source="cline-history",
            model="claude-sonnet-4-5",
            input_tok=1_000_000,
            output_tok=1_000_000,
            cache_read=0,
            cache_write=0,
            stop_reason="end_turn",
            tools=[],
            tool_count=0,
            msg_uuid="msg_override_check",
            cost_override=0.042,
        )
        row = self.conn.execute(
            "SELECT cost_usd FROM requests WHERE msg_uuid='msg_override_check'"
        ).fetchone()
        assert row[0] == pytest.approx(0.042)


# ─────────────────────────────────────────────────────────────────────────────
# 4. _proxy_cutoff
# ─────────────────────────────────────────────────────────────────────────────

class TestProxyCutoff:
    """_proxy_cutoff returns MIN(ts) of non-history sources, or None."""

    # Sources that ARE considered history (cutoff ignores them)
    HISTORY_SOURCES = [
        "claude-cli-history",
        "claude-desktop-history",
        "openclaw-history",
        "cline-history",
        "roo-code-history",
        "kilo-code-history",
    ]
    # A source that is NOT history (proxy-captured)
    LIVE_SOURCE = "cli"

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.path, self.conn = _make_db(monkeypatch)
        yield
        self.conn.close()
        os.unlink(self.path)

    def _seed(self, ts: str, source: str):
        self.conn.execute(
            "INSERT INTO requests (ts, source, model, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, ?, 'claude-sonnet-4-5', 100, 50, 0.001)",
            (ts, source),
        )
        self.conn.commit()

    def test_empty_db_returns_none(self):
        assert ih._proxy_cutoff(self.conn) is None

    def test_history_only_returns_none(self):
        for source in self.HISTORY_SOURCES:
            self._seed("2026-01-01T09:00:00", source)
        assert ih._proxy_cutoff(self.conn) is None

    def test_proxy_row_returns_its_ts(self):
        self._seed("2026-01-02T10:00:00", self.LIVE_SOURCE)
        result = ih._proxy_cutoff(self.conn)
        assert result == "2026-01-02T10:00:00"

    def test_returns_min_of_proxy_rows(self):
        """With multiple proxy rows, cutoff is the EARLIEST one."""
        self._seed("2026-01-03T10:00:00", self.LIVE_SOURCE)
        self._seed("2026-01-02T08:00:00", self.LIVE_SOURCE)
        self._seed("2026-01-05T15:00:00", self.LIVE_SOURCE)
        result = ih._proxy_cutoff(self.conn)
        assert result == "2026-01-02T08:00:00"

    def test_history_rows_do_not_affect_cutoff(self):
        """History rows earlier than proxy rows don't pull the cutoff back."""
        self._seed("2026-01-01T00:00:00", "claude-cli-history")  # earlier but history
        self._seed("2026-01-02T10:00:00", self.LIVE_SOURCE)
        result = ih._proxy_cutoff(self.conn)
        assert result == "2026-01-02T10:00:00"

    def test_cutoff_is_truncated_to_19_chars(self):
        """MIN(ts) may have sub-second precision; ensure output is truncated to 19 chars."""
        self._seed("2026-01-02T10:00:00.123456+00:00", self.LIVE_SOURCE)
        result = ih._proxy_cutoff(self.conn)
        assert result is not None
        assert len(result) == 19

    def test_import_respects_cutoff_skips_newer(self, tmp_path):
        """Records with ts >= cutoff are skipped during import."""
        cutoff = "2026-01-01T10:30:00"
        self._seed(cutoff, self.LIVE_SOURCE)  # set proxy cutoff

        lines = [
            _assistant_line("msg_before", "2026-01-01T10:00:00.000Z"),  # before cutoff → import
            _assistant_line("msg_after",  "2026-01-01T11:00:00.000Z"),  # after cutoff → skip
        ]
        jpath = _make_jsonl(lines, tmp_path)

        computed_cutoff = ih._proxy_cutoff(self.conn)
        inserted, skipped, _ = ih._import_claude_jsonl(
            self.conn, [jpath], "claude-cli-history", computed_cutoff
        )
        self.conn.commit()

        assert inserted == 1
        assert skipped == 1
        uuids = {r[0] for r in self.conn.execute("SELECT msg_uuid FROM requests WHERE source='claude-cli-history'").fetchall()}
        assert "msg_before" in uuids
        assert "msg_after" not in uuids


# ─────────────────────────────────────────────────────────────────────────────
# 5. _import_claude_jsonl
# ─────────────────────────────────────────────────────────────────────────────

class TestImportClaudeJsonl:
    """End-to-end tests for the Claude CLI / Desktop JSONL importer."""

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        self.path, self.conn = _make_db(monkeypatch)
        self.tmp_path = tmp_path
        yield
        self.conn.close()
        os.unlink(self.path)

    def _import(self, lines, cutoff=None, source="claude-cli-history"):
        jpath = _make_jsonl(lines, self.tmp_path)
        result = ih._import_claude_jsonl(self.conn, [jpath], source, cutoff)
        self.conn.commit()
        return result

    def _rows(self, where="1=1"):
        return self.conn.execute(
            f"SELECT msg_uuid, input_tokens, output_tokens, "
            f"cache_creation_tokens, cache_read_tokens, source "
            f"FROM requests WHERE {where} ORDER BY ts"
        ).fetchall()

    # ── basic import ─────────────────────────────────────────────────────────

    def test_basic_import_count(self):
        lines = [
            _assistant_line("msg_a", "2026-01-01T10:00:00.000Z"),
            _assistant_line("msg_b", "2026-01-01T11:00:00.000Z"),
        ]
        inserted, skipped, errors = self._import(lines)
        assert inserted == 2
        assert skipped == 0
        assert errors == 0

    def test_token_values_stored_correctly(self):
        lines = [
            _assistant_line(
                "msg_tok",
                "2026-01-01T10:00:00.000Z",
                input_tokens=1234,
                output_tokens=567,
                cache_write=89,
                cache_read=10,
            )
        ]
        self._import(lines)
        rows = self._rows("msg_uuid='msg_tok'")
        assert len(rows) == 1
        _, inp, out, cw, cr, _ = rows[0]
        assert inp == 1234
        assert out == 567
        assert cw == 89
        assert cr == 10

    def test_source_label_stored(self):
        lines = [_assistant_line("msg_src", "2026-01-01T10:00:00.000Z")]
        self._import(lines, source="claude-desktop-history")
        rows = self._rows("msg_uuid='msg_src'")
        assert rows[0][5] == "claude-desktop-history"

    def test_skips_zero_token_lines(self):
        """Lines where all token counts are 0 must be skipped silently."""
        lines = [
            _assistant_line("msg_zero", "2026-01-01T10:00:00.000Z",
                            input_tokens=0, output_tokens=0)
        ]
        inserted, skipped, errors = self._import(lines)
        assert inserted == 0
        assert errors == 0

    def test_skips_synthetic_model(self):
        """model='<synthetic>' lines must be dropped."""
        line = _assistant_line("msg_synth", "2026-01-01T10:00:00.000Z",
                               model="<synthetic>")
        inserted, _, _ = self._import([line])
        assert inserted == 0

    def test_skips_missing_timestamp(self):
        """Lines with no timestamp must be skipped."""
        line = {
            "type": "assistant",
            "message": {
                "id": "msg_nots",
                "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "content": [],
            },
        }
        inserted, _, _ = self._import([line])
        assert inserted == 0

    # ── dedup: re-import ─────────────────────────────────────────────────────

    def test_reimport_does_not_double_count(self):
        """Running the same import twice must not add extra rows."""
        lines = [
            _assistant_line("msg_dedup1", "2026-01-01T10:00:00.000Z"),
            _assistant_line("msg_dedup2", "2026-01-01T11:00:00.000Z"),
        ]
        jpath = _make_jsonl(lines, self.tmp_path)

        ih._import_claude_jsonl(self.conn, [jpath], "claude-cli-history", None)
        self.conn.commit()
        ih._import_claude_jsonl(self.conn, [jpath], "claude-cli-history", None)
        self.conn.commit()

        count = self.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        assert count == 2

    def test_reimport_returns_zero_inserted(self):
        lines = [_assistant_line("msg_ri01", "2026-01-01T10:00:00.000Z")]
        jpath = _make_jsonl(lines, self.tmp_path)
        ih._import_claude_jsonl(self.conn, [jpath], "claude-cli-history", None)
        self.conn.commit()
        inserted, _, _ = ih._import_claude_jsonl(
            self.conn, [jpath], "claude-cli-history", None
        )
        assert inserted == 0

    # ── cutoff ───────────────────────────────────────────────────────────────

    def test_cutoff_boundary_exactly_equal_is_skipped(self):
        """ts[:19] >= cutoff → skip; ts exactly equal to cutoff is skipped."""
        cutoff = "2026-01-01T10:30:00"
        lines = [
            _assistant_line("msg_eq", "2026-01-01T10:30:00.000Z"),  # == cutoff → skip
        ]
        inserted, skipped, _ = self._import(lines, cutoff=cutoff)
        assert inserted == 0
        assert skipped == 1

    def test_cutoff_one_second_before_is_imported(self):
        cutoff = "2026-01-01T10:30:00"
        lines = [
            _assistant_line("msg_just_before", "2026-01-01T10:29:59.000Z"),  # < cutoff → import
        ]
        inserted, skipped, _ = self._import(lines, cutoff=cutoff)
        assert inserted == 1
        assert skipped == 0

    def test_no_cutoff_imports_all(self):
        lines = [
            _assistant_line("msg_nc1", "2026-01-01T10:00:00.000Z"),
            _assistant_line("msg_nc2", "2099-12-31T23:59:59.000Z"),  # far future
        ]
        inserted, skipped, _ = self._import(lines, cutoff=None)
        assert inserted == 2
        assert skipped == 0

    # ── streaming duplicate accumulation ─────────────────────────────────────

    def test_streaming_duplicate_takes_max_tokens(self):
        """Same msg_id appearing twice in the file: take MAX of each field."""
        lines = [
            _assistant_line("msg_stream", "2026-01-01T10:00:00.000Z",
                            input_tokens=1000, output_tokens=0),
            _assistant_line("msg_stream", "2026-01-01T10:00:00.000Z",
                            input_tokens=0, output_tokens=500),
        ]
        inserted, _, _ = self._import(lines)
        # Only one DB row despite two lines
        assert inserted == 1
        rows = self._rows("msg_uuid='msg_stream'")
        assert len(rows) == 1
        _, inp, out, _, _, _ = rows[0]
        assert inp == 1000
        assert out == 500

    # ── prompt preview ───────────────────────────────────────────────────────

    def test_prompt_preview_captured_from_preceding_user_line(self):
        lines = [
            _user_line("What is the capital of France?"),
            _assistant_line("msg_preview", "2026-01-01T10:00:00.000Z"),
        ]
        self._import(lines)
        row = self.conn.execute(
            "SELECT prompt_preview FROM requests WHERE msg_uuid='msg_preview'"
        ).fetchone()
        assert row is not None
        assert row[0] == "What is the capital of France?"

    def test_prompt_preview_absent_when_no_user_line(self):
        lines = [
            _assistant_line("msg_no_preview", "2026-01-01T10:00:00.000Z"),
        ]
        self._import(lines)
        row = self.conn.execute(
            "SELECT prompt_preview FROM requests WHERE msg_uuid='msg_no_preview'"
        ).fetchone()
        assert row is not None
        assert row[0] == "" or row[0] is None

    # ── empty / malformed JSONL ───────────────────────────────────────────────

    def test_malformed_json_increments_errors(self, tmp_path):
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text("not json at all\n{also bad\n", encoding="utf-8")
        inserted, skipped, errors = ih._import_claude_jsonl(
            self.conn, [str(bad_file)], "claude-cli-history", None
        )
        assert inserted == 0
        assert errors == 2  # two bad lines

    def test_nonexistent_pattern_returns_zero(self):
        inserted, skipped, errors = ih._import_claude_jsonl(
            self.conn, ["/nonexistent/path/**/*.jsonl"], "claude-cli-history", None
        )
        assert inserted == 0
        assert skipped == 0
        assert errors == 0

    def test_empty_file_returns_zero(self, tmp_path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        inserted, skipped, errors = ih._import_claude_jsonl(
            self.conn, [str(empty)], "claude-cli-history", None
        )
        assert inserted == 0
        assert errors == 0

    # ── cost correctness ─────────────────────────────────────────────────────

    def test_cost_computed_correctly_for_imported_row(self):
        """Imported row cost_usd must match calc_cost output."""
        lines = [
            _assistant_line(
                "msg_cost_verify",
                "2026-01-01T10:00:00.000Z",
                model="claude-sonnet-4-5",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_write=0,
                cache_read=0,
            )
        ]
        self._import(lines)
        row = self.conn.execute(
            "SELECT cost_usd FROM requests WHERE msg_uuid='msg_cost_verify'"
        ).fetchone()
        # claude-sonnet-4-5: $3.0/M input → $3.0 for 1M tokens
        assert row[0] == pytest.approx(3.0)


class TestSharedConnect:
    def test_import_history_uses_shared_connect(self):
        import import_history as _ih
        import db as _db
        # The importer must reuse db._connect (single source of truth for the
        # WAL/busy_timeout PRAGMAs), not define its own.
        assert _ih._connect is _db._connect
