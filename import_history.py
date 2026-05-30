#!/usr/bin/env python3
from __future__ import annotations
"""
Import historical LLM usage from all local providers into tracker.db.
Runs every 5 minutes as a launchd daemon. Safe to run multiple times (idempotent).

Supported providers:
  claude-cli       ~/.claude/projects/**/*.jsonl          (JSONL, dedup by message.id)
  claude-desktop   ~/Library/.../local-agent-mode-sessions (JSONL, same format, no cutoff)
  openclaw         ~/.openclaw/agents/**/*.jsonl           (model.completed events)
  cline            ~/Library/.../saoudrizwan.claude-dev/tasks  (ui_messages.json)
  roo-code         ~/Library/.../rooveterinaryinc.roo-cline/tasks (same as cline)
  kilo-code        ~/Library/.../kilocode.kilo-code/tasks  (same as cline)

Deduplication:
  - claude-cli/desktop: msg_uuid = message.id (Anthropic API response ID)
  - openclaw:           msg_uuid = traceId:seq
  - cline/roo/kilo:     msg_uuid = provider:taskId:index

Claude CLI / VS Code / OpenClaw are also captured live by the proxy.
The cutoff prevents double-counting: only records BEFORE proxy start are imported from JSONL
for those providers. Claude Desktop has no cutoff (proxy never sees it).
"""

import json
import glob
import re
import sqlite3
import sys
import os
import time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
from db import PRICING as DB_PRICING, init_db as _init_db

DB_PATH  = Path(__file__).parent / "tracker.db"
HOME     = Path.home()
_DEFAULT = DB_PRICING["default"]

VSCODE_GLOBS = [
    str(HOME / "Library/Application Support/Code/User/globalStorage"),
    str(HOME / ".config/Code/User/globalStorage"),
    str(HOME / ".vscode-server/data/User/globalStorage"),
]


# ── Pricing ───────────────────────────────────────────────────────────────────

def _price(model: str) -> dict:
    p = DB_PRICING.get(model)
    if not p:
        for k, v in DB_PRICING.items():
            if model and model.startswith(k):
                return v
    return p or _DEFAULT


def calc_cost(model, input_tok, output_tok, cache_write, cache_read):
    p = _price(model)
    M = 1_000_000
    return (
        input_tok   * p["input"]          / M +
        output_tok  * p["output"]         / M +
        cache_write * p["input"] * 1.25   / M +
        cache_read  * p["input"] * 0.10   / M
    )


# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_schema(conn):
    if not _col_exists(conn, "msg_uuid"):
        conn.execute("ALTER TABLE requests ADD COLUMN msg_uuid TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_uuid "
        "ON requests(msg_uuid) WHERE msg_uuid IS NOT NULL"
    )
    conn.commit()


def _col_exists(conn, col):
    return col in {r[1] for r in conn.execute("PRAGMA table_info(requests)").fetchall()}


def _proxy_cutoff(conn) -> str | None:
    row = conn.execute(
        "SELECT MIN(ts) FROM requests "
        "WHERE source NOT IN ('claude-cli-history','claude-desktop-history','openclaw-history',"
        "'cline-history','roo-code-history','kilo-code-history')"
    ).fetchone()
    return row[0][:19] if (row and row[0]) else None


def _insert(conn, *, ts, source, model, input_tok, output_tok,
            cache_read, cache_write, stop_reason, tools, tool_count,
            msg_uuid, cost_override=None, prompt_preview=""):
    cost = cost_override if cost_override is not None else \
           calc_cost(model, input_tok, output_tok, cache_write, cache_read)
    tools_json = json.dumps(tools) if tools else None
    preview    = (prompt_preview or "")[:800]
    try:
        conn.execute("""
            INSERT OR IGNORE INTO requests
              (ts, source, model, input_tokens, output_tokens, cost_usd,
               duration_ms, status, cache_read_tokens, cache_creation_tokens,
               stop_reason, tool_call_count, tools_json, msg_uuid, prompt_preview)
            VALUES (?,?,?,?,?,?,0,200,?,?,?,?,?,?,?)
        """, (ts, source, model, input_tok, output_tok, cost,
              cache_read, cache_write, stop_reason, tool_count, tools_json, msg_uuid, preview))
        changed = conn.execute("SELECT changes()").fetchone()[0]
        # Backfill preview for records that already exist but have none
        if changed == 0 and preview:
            conn.execute(
                "UPDATE requests SET prompt_preview=? "
                "WHERE msg_uuid=? AND (prompt_preview IS NULL OR prompt_preview='')",
                (preview, msg_uuid),
            )
        return changed
    except sqlite3.Error:
        return 0


def _save_last_sync(conn):
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('last_import_ts',?)", (ts,))


# ── Provider: Claude CLI & Desktop ────────────────────────────────────────────

def _import_claude_jsonl(conn, patterns: list[str], source: str, cutoff: str | None):
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat, recursive=True))

    inserted = skipped = errors = 0

    for fpath in files:
        best: dict[str, dict] = {}
        # msg_id → user text that preceded this assistant turn
        user_before: dict[str, str] = {}
        last_user_text = ""
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        errors += 1
                        continue

                    dtype = d.get("type", "")

                    # Capture user messages for prompt preview
                    if dtype == "user":
                        content = d.get("message", {}).get("content", "")
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text = " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        else:
                            text = ""
                        text = " ".join(text.split())
                        if text:
                            last_user_text = text
                        continue

                    if dtype != "assistant":
                        continue

                    msg    = d.get("message", {})
                    usage  = msg.get("usage", {})
                    model  = msg.get("model", "")
                    msg_id = msg.get("id", "")
                    ts     = d.get("timestamp", "") or d.get("_audit_timestamp", "")

                    if not model or not ts or not msg_id:
                        continue

                    input_tok   = usage.get("input_tokens", 0) or 0
                    output_tok  = usage.get("output_tokens", 0) or 0
                    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
                    cache_read  = usage.get("cache_read_input_tokens", 0) or 0

                    if input_tok == 0 and output_tok == 0:
                        continue

                    # Only record user_before on first encounter of this msg_id
                    if msg_id not in best:
                        user_before[msg_id] = last_user_text

                    if cutoff and ts[:19] >= cutoff:
                        # Don't insert (proxy already captured it), but backfill
                        # prompt_preview on any existing -history record with this uuid
                        preview = user_before.get(msg_id, "")
                        if preview and msg_id:
                            conn.execute(
                                "UPDATE requests SET prompt_preview=? "
                                "WHERE msg_uuid=? AND (prompt_preview IS NULL OR prompt_preview='')",
                                (preview[:800], msg_id),
                            )
                        skipped += 1
                        continue

                    content = msg.get("content", [])
                    tools   = list({
                        b["name"] for b in content
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")
                    })
                    best[msg_id] = {
                        "ts": ts, "model": model,
                        "input_tok": input_tok, "output_tok": output_tok,
                        "cache_write": cache_write, "cache_read": cache_read,
                        "stop_reason": msg.get("stop_reason", ""),
                        "tools": tools, "tool_count": len(tools),
                        "msg_id": msg_id,
                    }
        except (OSError, PermissionError):
            continue

        for e in best.values():
            inserted += _insert(
                conn, ts=e["ts"], source=source, model=e["model"],
                input_tok=e["input_tok"], output_tok=e["output_tok"],
                cache_read=e["cache_read"], cache_write=e["cache_write"],
                stop_reason=e["stop_reason"], tools=e["tools"],
                tool_count=e["tool_count"], msg_uuid=e["msg_id"],
                prompt_preview=user_before.get(e["msg_id"], ""),
            )

    return inserted, skipped, errors


# ── Provider: OpenClaw ────────────────────────────────────────────────────────

def import_openclaw(conn, cutoff: str | None):
    patterns = [
        str(HOME / ".openclaw/agents/**/*.jsonl"),
        str(HOME / ".clawdbot/agents/**/*.jsonl"),
        str(HOME / ".moltbot/agents/**/*.jsonl"),
        str(HOME / ".moldbot/agents/**/*.jsonl"),
    ]
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat, recursive=True))

    inserted = skipped = errors = 0

    for fpath in files:
        try:
            # First pass: build traceId → prompt text map from prompt.submitted events
            prompts: dict[str, str] = {}
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "prompt.submitted":
                        continue
                    trace  = d.get("traceId", "")
                    prompt = (d.get("data") or {}).get("prompt", "")
                    if trace and prompt:
                        text = " ".join(str(prompt).split())
                        prompts[trace] = text[:800]

            # Second pass: process model.completed events
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        errors += 1
                        continue

                    if d.get("type") != "model.completed":
                        continue

                    ts      = d.get("ts", "")
                    model   = d.get("modelId", "")
                    trace   = d.get("traceId", "")
                    seq     = d.get("seq", 0)
                    data    = d.get("data", {})
                    usage   = data.get("usage", {})

                    if not ts or not model:
                        continue

                    input_tok   = usage.get("input", 0) or 0
                    output_tok  = usage.get("output", 0) or 0
                    cache_write = usage.get("cacheWrite", 0) or 0
                    cache_read  = usage.get("cacheRead", 0) or 0

                    if input_tok == 0 and output_tok == 0:
                        continue

                    msg_uuid  = f"{trace}:{seq}"
                    preview   = prompts.get(trace, "")

                    if cutoff and ts[:19] >= cutoff:
                        if preview and msg_uuid:
                            conn.execute(
                                "UPDATE requests SET prompt_preview=? "
                                "WHERE msg_uuid=? AND (prompt_preview IS NULL OR prompt_preview='')",
                                (preview, msg_uuid),
                            )
                        skipped += 1
                        continue

                    inserted += _insert(
                        conn, ts=ts, source="openclaw-history", model=model,
                        input_tok=input_tok, output_tok=output_tok,
                        cache_read=cache_read, cache_write=cache_write,
                        stop_reason="", tools=[], tool_count=0,
                        msg_uuid=msg_uuid,
                        prompt_preview=preview,
                    )
        except (OSError, PermissionError):
            continue

    return inserted, skipped, errors


# ── Provider: Cline / Roo Code / Kilo Code (VSCode extensions) ───────────────

def _find_vscode_extension_tasks(extension_id: str) -> list[Path]:
    """Find all task directories for a VSCode extension across all install locations."""
    tasks = []
    for base in VSCODE_GLOBS:
        p = Path(base) / extension_id / "tasks"
        if p.exists():
            tasks.extend(p.iterdir())
    return tasks


def _parse_cline_tokens_from_text(text: str) -> dict | None:
    """
    Cline writes token data as a JSON object in the 'text' field of api_req_started events.
    Format: {"request":"...", "tokensIn":N, "tokensOut":N, "cacheWrites":N, "cacheReads":N, "cost":N}
    """
    if not text:
        return None
    try:
        d = json.loads(text)
        if isinstance(d, dict) and ("tokensIn" in d or "tokensOut" in d):
            return d
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def import_cline_family(conn, extension_id: str, source: str, cutoff: str | None):
    """
    Import from Cline-family VSCode extensions (Cline, Roo Code, Kilo Code).
    Each task directory contains ui_messages.json with api_req_started events.
    Dedup key: source:taskId:eventIndex
    """
    task_dirs = _find_vscode_extension_tasks(extension_id)
    inserted = skipped = errors = 0

    for task_dir in task_dirs:
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name
        ui_file = task_dir / "ui_messages.json"
        if not ui_file.exists():
            continue

        # Get model from api_conversation_history.json if available
        model = "cline-auto"
        conv_file = task_dir / "api_conversation_history.json"
        if conv_file.exists():
            try:
                conv = json.loads(conv_file.read_text())
                if isinstance(conv, list):
                    for msg in conv:
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            m = re.search(r"<model>(.*?)</model>", content)
                            if m:
                                model = m.group(1).strip()
                                break
            except Exception:
                pass

        # Fallback: modelInfo in first ui message
        try:
            messages = json.loads(ui_file.read_text())
        except Exception:
            errors += 1
            continue

        if not isinstance(messages, list):
            continue

        # Extract model from modelInfo field
        for msg in messages:
            mi = msg.get("modelInfo")
            if mi and isinstance(mi, dict):
                raw_model = mi.get("modelId", "")
                if raw_model:
                    # openrouter/anthropic/claude-sonnet-4.5 → claude-sonnet-4-5
                    if "/" in raw_model:
                        raw_model = raw_model.split("/")[-1].replace(".", "-")
                    model = raw_model
                break

        # Parse api_req_started events
        api_index = 0
        for msg in messages:
            if msg.get("say") != "api_req_started":
                continue

            tokens = _parse_cline_tokens_from_text(msg.get("text", ""))
            if not tokens:
                api_index += 1
                continue

            input_tok   = int(tokens.get("tokensIn", 0) or 0)
            output_tok  = int(tokens.get("tokensOut", 0) or 0)
            cache_write = int(tokens.get("cacheWrites", 0) or 0)
            cache_read  = int(tokens.get("cacheReads", 0) or 0)
            cost_raw    = float(tokens.get("cost", 0) or 0)

            if input_tok == 0 and output_tok == 0 and cost_raw == 0:
                api_index += 1
                continue

            # Timestamp: Cline stores unix ms in 'ts'
            ts_ms = msg.get("ts", 0)
            if ts_ms:
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
            else:
                ts = datetime.now(timezone.utc).isoformat()

            if cutoff and ts[:19] >= cutoff:
                skipped += 1
                api_index += 1
                continue

            msg_uuid = f"{source}:{task_id}:{api_index}"
            # Use reported cost if available, else calculate
            cost_override = cost_raw if cost_raw > 0 else None

            inserted += _insert(
                conn, ts=ts, source=source, model=model,
                input_tok=input_tok, output_tok=output_tok,
                cache_read=cache_read, cache_write=cache_write,
                stop_reason="", tools=[], tool_count=0,
                msg_uuid=msg_uuid, cost_override=cost_override,
            )
            api_index += 1

    return inserted, skipped, errors


# ── Provider: Pi / OMP ───────────────────────────────────────────────────────

def import_pi_omp(conn, base_dir: str, source: str):
    """Pi and OMP: JSONL with message.usage fields (identical schema)."""
    files = glob.glob(str(HOME / base_dir / "**/*.jsonl"), recursive=True) + \
            glob.glob(str(HOME / base_dir / "*.jsonl"))
    inserted = skipped = errors = 0
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for idx, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        errors += 1
                        continue
                    msg   = d.get("message", {})
                    usage = msg.get("usage", {}) if isinstance(msg, dict) else {}
                    if not usage:
                        continue
                    inp = usage.get("input_tokens", 0) or usage.get("input", 0) or 0
                    out = usage.get("output_tokens", 0) or usage.get("output", 0) or 0
                    if inp == 0 and out == 0:
                        continue
                    ts        = d.get("timestamp", d.get("ts", ""))
                    resp_id   = d.get("responseId", "")
                    model     = (d.get("source") or {}).get("provider", "pi") if isinstance(d.get("source"), dict) else "pi"
                    msg_uuid  = f"{source}:{fpath}:{resp_id or idx}"
                    inserted += _insert(conn, ts=ts or "1970-01-01T00:00:00Z",
                        source=source, model=model,
                        input_tok=inp, output_tok=out,
                        cache_read=0, cache_write=0,
                        stop_reason="", tools=[], tool_count=0, msg_uuid=msg_uuid)
        except (OSError, PermissionError):
            continue
    return inserted, skipped, errors


# ── Provider: Qwen CLI ───────────────────────────────────────────────────────

def import_qwen(conn):
    dirs = glob.glob(str(HOME / ".qwen/projects/*/chats/")) + \
           ([str(Path(os.environ["QWEN_DATA_DIR"]) / "projects/*/chats/")]
            if "QWEN_DATA_DIR" in os.environ else [])
    files = []
    for d in dirs:
        files.extend(glob.glob(d + "*.jsonl"))
    inserted = errors = 0
    for fpath in files:
        session_id = Path(fpath).stem
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        errors += 1
                        continue
                    msg   = d.get("message", d)
                    inp   = msg.get("promptTokenCount", 0) or 0
                    out   = (msg.get("candidatesTokenCount", 0) or 0) + (msg.get("thoughtsTokenCount", 0) or 0)
                    cache = msg.get("cachedContentTokenCount", 0) or 0
                    # cached tokens are inside promptTokenCount; subtract to get real input
                    inp   = max(0, inp - cache)
                    if inp == 0 and out == 0:
                        continue
                    ts       = d.get("timestamp", "")
                    uuid_val = d.get("uuid", "")
                    msg_uuid = f"qwen:{session_id}:{uuid_val}"
                    inserted += _insert(conn, ts=ts or "1970-01-01T00:00:00Z",
                        source="qwen-history", model="qwen-auto",
                        input_tok=inp, output_tok=out,
                        cache_read=cache, cache_write=0,
                        stop_reason="", tools=[], tool_count=0, msg_uuid=msg_uuid)
        except (OSError, PermissionError):
            continue
    return inserted, 0, errors


# ── Provider: Kimi ───────────────────────────────────────────────────────────

def import_kimi(conn):
    base = Path(os.environ.get("KIMI_SHARE_DIR", str(HOME / ".kimi"))) / "sessions"
    files = list(base.glob("*/wire.jsonl")) + list(base.glob("**/wire.jsonl"))
    inserted = errors = 0
    for fpath in files:
        session_id = fpath.parent.name
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for idx, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        errors += 1
                        continue
                    usage = d.get("token_usage") or d.get("StatusUpdate", {}).get("token_usage", {})
                    if not usage:
                        continue
                    inp   = usage.get("input_other", 0) or 0
                    out   = usage.get("output", 0) or 0
                    cr    = usage.get("input_cache_read", 0) or 0
                    cw    = usage.get("input_cache_creation", 0) or 0
                    if inp == 0 and out == 0:
                        continue
                    ts       = d.get("timestamp", "")
                    if isinstance(ts, (int, float)) and ts > 1e10:
                        ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
                    elif isinstance(ts, (int, float)):
                        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    msg_id   = d.get("message_id", "")
                    msg_uuid = f"kimi:{session_id}:{msg_id or idx}"
                    inserted += _insert(conn, ts=ts or "1970-01-01T00:00:00Z",
                        source="kimi-history", model="kimi-auto",
                        input_tok=inp, output_tok=out,
                        cache_read=cr, cache_write=cw,
                        stop_reason="", tools=[], tool_count=0, msg_uuid=msg_uuid)
        except (OSError, PermissionError):
            continue
    return inserted, 0, errors


# ── Provider: Mistral Vibe ───────────────────────────────────────────────────

def import_mistral_vibe(conn):
    base = Path(os.environ.get("VIBE_HOME", str(HOME / ".vibe"))) / "logs/session"
    inserted = errors = 0
    for meta_file in base.glob("*/meta.json"):
        try:
            meta = json.loads(meta_file.read_text())
            stats = meta.get("stats", {})
            inp   = stats.get("session_prompt_tokens", 0) or 0
            out   = stats.get("session_completion_tokens", 0) or 0
            if inp == 0 and out == 0:
                continue
            cost  = stats.get("session_cost", None)
            ts    = meta.get("start_time", meta.get("created_at", ""))
            sid   = meta.get("session_id", meta_file.parent.name)
            model = (meta.get("active_model") or {}).get("name", "mistral-auto") \
                    if isinstance(meta.get("active_model"), dict) else "mistral-auto"
            inserted += _insert(conn, ts=ts or "1970-01-01T00:00:00Z",
                source="mistral-vibe-history", model=model,
                input_tok=inp, output_tok=out,
                cache_read=0, cache_write=0,
                stop_reason="", tools=[], tool_count=0,
                msg_uuid=f"mistral-vibe:{sid}",
                cost_override=float(cost) if cost else None)
        except (OSError, PermissionError, json.JSONDecodeError, Exception):
            errors += 1
    return inserted, 0, errors


# ── Provider: IBM Bob (same format as Cline) ─────────────────────────────────

def import_ibm_bob(conn):
    return import_cline_family(conn, "ibm.bob-code", "ibm-bob-history", None)


# ── Provider: SQLite helpers ──────────────────────────────────────────────────

def _open_sqlite_ro(path: Path):
    """Open SQLite in read-only mode (avoids locking issues with running apps)."""
    uri = f"file:{path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


# ── Provider: Crush ───────────────────────────────────────────────────────────

def import_crush(conn):
    registry = Path(os.environ.get("CRUSH_GLOBAL_DATA",
                    str(HOME / ".local/share/crush"))) / "projects.json"
    inserted = errors = 0
    if not registry.exists():
        return 0, 0, 0
    try:
        projects = json.loads(registry.read_text())
    except Exception:
        return 0, 0, 1
    for proj in (projects if isinstance(projects, list) else []):
        db_path = Path(proj.get("path", "")) / (proj.get("data_dir", ".crush")) / "crush.db"
        if not db_path.exists():
            continue
        try:
            db = _open_sqlite_ro(db_path)
            rows = db.execute("""
                SELECT id, prompt_tokens, completion_tokens, updated_at, created_at,
                       (SELECT model FROM messages WHERE session_id=sessions.id
                        GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1) as model
                FROM sessions
                WHERE parent_session_id IS NULL
                  AND (prompt_tokens > 0 OR completion_tokens > 0)
            """).fetchall()
            db.close()
            for sid, inp, out, upd, cre, model in rows:
                ts = upd or cre or ""
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                inserted += _insert(conn, ts=str(ts) or "1970-01-01T00:00:00Z",
                    source="crush-history", model=model or "crush-auto",
                    input_tok=inp or 0, output_tok=out or 0,
                    cache_read=0, cache_write=0,
                    stop_reason="", tools=[], tool_count=0,
                    msg_uuid=f"crush:{sid}")
        except Exception:
            errors += 1
    return inserted, 0, errors


# ── Provider: Goose ───────────────────────────────────────────────────────────

def import_goose(conn):
    db_path = Path(os.environ.get("XDG_DATA_HOME",
                   str(HOME / ".local/share"))) / "goose/sessions/sessions.db"
    if not db_path.exists():
        return 0, 0, 0
    inserted = errors = 0
    try:
        db = _open_sqlite_ro(db_path)
        rows = db.execute("""
            SELECT id, accumulated_input_tokens, accumulated_output_tokens, updated_at
            FROM sessions
            WHERE accumulated_input_tokens > 0 OR accumulated_output_tokens > 0
        """).fetchall()
        db.close()
        for sid, inp, out, upd in rows:
            ts = upd or ""
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts, tz=timezone.utc).isoformat()
            inserted += _insert(conn, ts=str(ts) or "1970-01-01T00:00:00Z",
                source="goose-history", model="goose-auto",
                input_tok=inp or 0, output_tok=out or 0,
                cache_read=0, cache_write=0,
                stop_reason="", tools=[], tool_count=0,
                msg_uuid=f"goose:{sid}")
    except Exception:
        errors += 1
    return inserted, 0, errors


# ── Provider: OpenCode ────────────────────────────────────────────────────────

def import_opencode(conn):
    xdg = os.environ.get("XDG_DATA_HOME", str(HOME / ".local/share"))
    db_files = glob.glob(str(Path(xdg) / "opencode/opencode*.db"))
    inserted = errors = 0
    for db_file in db_files:
        try:
            db = _open_sqlite_ro(Path(db_file))
            # OpenCode stores message parts with usage data
            rows = db.execute("""
                SELECT m.id, m.session_id, m.time,
                       p.content
                FROM message m
                JOIN part p ON p.message_id = m.id
                WHERE p.type = 'text' AND m.role = 'assistant'
            """).fetchall()
            db.close()
            for mid, sid, ts, content in rows:
                try:
                    data = json.loads(content) if isinstance(content, str) else {}
                    usage = data.get("usage", {})
                    inp  = usage.get("input", 0) or 0
                    out  = usage.get("output", 0) or 0
                    cr   = (usage.get("cache") or {}).get("read", 0) or 0
                    cw   = (usage.get("cache") or {}).get("write", 0) or 0
                    if inp == 0 and out == 0:
                        continue
                    model = data.get("model", "opencode-auto")
                    if isinstance(ts, (int, float)):
                        ts = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts, tz=timezone.utc).isoformat()
                    inserted += _insert(conn, ts=str(ts) or "1970-01-01T00:00:00Z",
                        source="opencode-history", model=model,
                        input_tok=inp, output_tok=out,
                        cache_read=cr, cache_write=cw,
                        stop_reason="", tools=[], tool_count=0,
                        msg_uuid=f"opencode:{sid}:{mid}")
                except Exception:
                    errors += 1
        except Exception:
            errors += 1
    return inserted, 0, errors


# ── Provider: Warp ────────────────────────────────────────────────────────────

def import_warp(conn):
    db_path = Path(os.environ.get("WARP_DB_PATH", "")) if "WARP_DB_PATH" in os.environ else None
    if not db_path or not db_path.exists():
        candidates = [
            HOME / "Library/Group Containers/2BBY89MBSN.dev.warp/Library/Application Support/dev.warp.Warp-Stable/warp.sqlite",
            HOME / "Library/Group Containers/2BBY89MBSN.dev.warp/Library/Application Support/dev.warp.Warp-Preview/warp.sqlite",
        ]
        db_path = next((p for p in candidates if p.exists()), None)
    if not db_path:
        return 0, 0, 0
    inserted = errors = 0
    try:
        db = _open_sqlite_ro(db_path)
        rows = db.execute("""
            SELECT q.conversation_id, q.exchange_id, q.start_ts,
                   q.model_id, q.input_tokens, q.output_tokens
            FROM ai_queries q
            WHERE q.input_tokens > 0 OR q.output_tokens > 0
        """).fetchall()
        db.close()
        for cid, eid, ts, model, inp, out in rows:
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts, tz=timezone.utc).isoformat()
            inserted += _insert(conn, ts=str(ts) or "1970-01-01T00:00:00Z",
                source="warp-history", model=model or "warp-auto",
                input_tok=inp or 0, output_tok=out or 0,
                cache_read=0, cache_write=0,
                stop_reason="", tools=[], tool_count=0,
                msg_uuid=f"warp:{cid}:{eid}")
    except Exception:
        errors += 1
    return inserted, 0, errors


# ── Provider: Gemini CLI ─────────────────────────────────────────────────────

def import_gemini(conn):
    dirs = glob.glob(str(HOME / ".gemini/tmp/*/chats/")) + \
           glob.glob(str(HOME / ".gemini/tmp/*/*/chats/"))
    files = []
    for d in dirs:
        files.extend(glob.glob(d + "*.jsonl"))
        files.extend(glob.glob(d + "*.json"))
    inserted = errors = 0
    for fpath in files:
        try:
            raw = open(fpath, encoding="utf-8", errors="replace").read().strip()
            if not raw:
                continue
            records = json.loads(raw) if raw[0] in "[{" and raw[0] == "[" else \
                      [json.loads(l) for l in raw.splitlines() if l.strip()]
            session_tokens: dict[str, dict] = {}
            for item in records:
                if not isinstance(item, dict):
                    continue
                sid  = item.get("sessionId", Path(fpath).stem)
                msg  = item.get("message", {})
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usageMetadata", {})
                if not usage:
                    continue
                prompt_total = usage.get("promptTokenCount", 0) or 0
                cached       = usage.get("cachedContentTokenCount", 0) or 0
                out          = usage.get("candidatesTokenCount", 0) or 0
                inp          = max(0, prompt_total - cached)
                if inp == 0 and out == 0:
                    continue
                ts   = item.get("timestamp", "")
                model = msg.get("modelVersion", msg.get("model", "gemini-auto"))
                if sid not in session_tokens or \
                   (inp + out) > (session_tokens[sid]["inp"] + session_tokens[sid]["out"]):
                    session_tokens[sid] = {"inp": inp, "out": out,
                                           "cr": cached, "ts": ts, "model": model}
            for sid, tok in session_tokens.items():
                inserted += _insert(conn,
                    ts=tok["ts"] or "1970-01-01T00:00:00Z",
                    source="gemini-history", model=tok["model"],
                    input_tok=tok["inp"], output_tok=tok["out"],
                    cache_read=tok["cr"], cache_write=0,
                    stop_reason="", tools=[], tool_count=0,
                    msg_uuid=f"gemini:{sid}")
        except (OSError, PermissionError, json.JSONDecodeError, Exception):
            errors += 1
    return inserted, 0, errors


# ── Provider: Codex ───────────────────────────────────────────────────────────

def import_codex(conn):
    """Codex: session-level totals from turn_context/response_item events."""
    base = Path(os.environ.get("CODEX_HOME", str(HOME / ".codex"))) / "sessions"
    files = glob.glob(str(base / "**/*.jsonl"), recursive=True)
    inserted = errors = 0
    for fpath in files:
        try:
            session_id = ""
            total_inp = total_out = 0
            model = "codex-auto"
            ts = ""
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = d.get("type", "")
                    p = d.get("payload", {})
                    if t == "session_meta":
                        session_id = p.get("id", "")
                        ts = p.get("timestamp", d.get("timestamp", ""))
                        if not (p.get("originator", "").startswith("codex")):
                            break  # not a codex session
                    elif t == "turn_context":
                        model = p.get("model", model)
                    elif t == "response_item":
                        # response_item may contain usage in some versions
                        usage = p.get("usage", {})
                        if usage:
                            total_inp += usage.get("input_tokens", 0) or 0
                            total_out += usage.get("output_tokens", 0) or 0
            if session_id and (total_inp > 0 or total_out > 0):
                inserted += _insert(conn,
                    ts=ts or "1970-01-01T00:00:00Z",
                    source="codex-history", model=model,
                    input_tok=total_inp, output_tok=total_out,
                    cache_read=0, cache_write=0,
                    stop_reason="", tools=[], tool_count=0,
                    msg_uuid=f"codex:{session_id}")
        except (OSError, PermissionError):
            errors += 1
    return inserted, 0, errors


# ── Provider: Copilot ─────────────────────────────────────────────────────────

def import_copilot(conn):
    """GitHub Copilot: JSONL transcripts in VS Code workspaceStorage."""
    patterns = [
        str(HOME / "Library/Application Support/Code/User/workspaceStorage/*/GitHub.copilot-chat/transcripts/*.jsonl"),
        str(HOME / ".config/Code/User/workspaceStorage/*/GitHub.copilot-chat/transcripts/*.jsonl"),
        str(HOME / ".copilot/session-state/*.jsonl"),
    ]
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))
    inserted = errors = 0
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        errors += 1
                        continue
                    data = d.get("data", {})
                    out  = data.get("outputTokens", 0) or 0
                    if out == 0:
                        continue
                    # Input not reliably available; use outputTokens only
                    ts   = d.get("timestamp", "")
                    mid  = d.get("id", "")
                    inserted += _insert(conn,
                        ts=ts or "1970-01-01T00:00:00Z",
                        source="copilot-history", model="copilot-auto",
                        input_tok=0, output_tok=out,
                        cache_read=0, cache_write=0,
                        stop_reason="", tools=[], tool_count=0,
                        msg_uuid=f"copilot:{fpath}:{mid}")
        except (OSError, PermissionError):
            errors += 1
    return inserted, 0, errors


# ── Provider: Droid (Factory CLI) ────────────────────────────────────────────

def import_droid(conn):
    """Droid: session-level totals split across assistant messages."""
    base = Path(os.environ.get("FACTORY_DIR", str(HOME / ".factory"))) / "sessions"
    files = glob.glob(str(base / "**/*.jsonl"), recursive=True)
    inserted = errors = 0
    for fpath in files:
        if ".factory/" in fpath and "/sessions/" not in fpath:
            continue
        try:
            session_id = ""
            token_usage: dict = {}
            assistant_msgs = []
            ts_first = ""
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not session_id:
                        session_id = d.get("sessionId", d.get("session_id", ""))
                    ts = d.get("timestamp", d.get("ts", ""))
                    if ts and not ts_first:
                        ts_first = ts
                    p = d.get("payload", d)
                    if "tokenUsage" in p:
                        token_usage = p["tokenUsage"]
                    mid = d.get("messageId", d.get("id", ""))
                    role = (p.get("role", "") or "").lower()
                    if role == "assistant" or d.get("type") == "assistant":
                        assistant_msgs.append(mid)
            total_inp = token_usage.get("inputTokens", token_usage.get("input", 0)) or 0
            total_out = token_usage.get("outputTokens", token_usage.get("output", 0)) or 0
            if not session_id or (total_inp == 0 and total_out == 0):
                continue
            # Split evenly (approximate) — store as one session record
            inserted += _insert(conn,
                ts=ts_first or "1970-01-01T00:00:00Z",
                source="droid-history", model="droid-auto",
                input_tok=total_inp, output_tok=total_out,
                cache_read=0, cache_write=0,
                stop_reason="", tools=[], tool_count=0,
                msg_uuid=f"droid:{session_id}")
        except (OSError, PermissionError):
            errors += 1
    return inserted, 0, errors


# ── Fuzzy backfill: fill prompt_preview for proxy records from JSONL ──────────

def _backfill_previews_from_jsonl(conn) -> int:
    """
    Match proxy-captured records (msg_uuid IS NULL, no preview) to their
    corresponding JSONL entries using (model, input_tokens, output_tokens, ts[:16]).
    Returns number of records updated.
    """
    from collections import defaultdict

    cutoff_row = conn.execute("""
        SELECT MIN(ts) FROM requests
        WHERE source NOT IN ('claude-cli-history','claude-desktop-history','openclaw-history',
                             'cline-history','roo-code-history','kilo-code-history')
    """).fetchone()
    cutoff = cutoff_row[0][:19] if (cutoff_row and cutoff_row[0]) else None
    if not cutoff:
        return 0

    # Collect existing msg_uuids to avoid UNIQUE conflicts
    existing_uuids = {r[0] for r in conn.execute(
        "SELECT msg_uuid FROM requests WHERE msg_uuid IS NOT NULL")}

    # Read all Claude CLI JSONL files and collect post-cutoff entries
    msg_map: dict[str, dict] = {}
    patterns = [
        str(HOME / ".claude/projects/**/*.jsonl"),
        str(HOME / "Library/Application Support/Claude/projects/**/*.jsonl"),
    ]
    files: list[str] = []
    for pat in patterns:
        files.extend(glob.glob(pat, recursive=True))

    for fpath in files:
        last_user = ""
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    dtype = d.get("type", "")
                    if dtype == "user":
                        content = d.get("message", {}).get("content", "")
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text = " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        else:
                            text = ""
                        text = " ".join(text.split())
                        if text:
                            last_user = text
                        continue
                    if dtype != "assistant":
                        continue
                    msg    = d.get("message", {})
                    msg_id = msg.get("id", "")
                    ts     = d.get("timestamp", "")
                    if not msg_id or not ts or ts[:19] < cutoff:
                        continue
                    if msg_id in existing_uuids or msg_id in msg_map:
                        continue
                    u = msg.get("usage", {})
                    inp = u.get("input_tokens", 0) or 0
                    out = u.get("output_tokens", 0) or 0
                    if inp == 0 and out == 0:
                        continue
                    msg_map[msg_id] = {
                        "ts16":   ts[:16],
                        "ts13":   ts[:13],
                        "inp":    inp,
                        "out":    out,
                        "model":  msg.get("model", ""),
                        "prompt": last_user[:800],
                    }
        except (OSError, PermissionError):
            continue

    if not msg_map:
        return 0

    # Index by (model, inp, out, ts[:16]) for fast lookup
    by_key: dict = defaultdict(list)
    for mid, e in msg_map.items():
        by_key[(e["model"], e["inp"], e["out"], e["ts16"])].append((mid, e["prompt"]))

    # Fetch proxy records without preview
    proxy = conn.execute("""
        SELECT id, ts, model, input_tokens, output_tokens
        FROM requests
        WHERE msg_uuid IS NULL
          AND (prompt_preview IS NULL OR prompt_preview = '')
          AND input_tokens > 0
        ORDER BY ts
    """).fetchall()

    updated = 0
    for rid, ts, model, inp, out in proxy:
        ts16 = ts[:16]
        ts13 = ts[:13]
        # Try exact minute match first, then same hour
        candidates = by_key.get((model, inp, out, ts16), [])
        if not candidates:
            candidates = [
                (mid, p) for key, matches in by_key.items()
                if key[0] == model and key[1] == inp and key[2] == out and key[3][:13] == ts13
                for mid, p in matches
            ]
        if len(candidates) == 1:
            mid, prompt = candidates[0]
            try:
                conn.execute(
                    "UPDATE requests SET prompt_preview=?, msg_uuid=? WHERE id=?",
                    (prompt, mid, rid),
                )
                # Prevent reuse of this uuid for other records
                by_key[(model, inp, out, ts16)] = [(m, p) for m, p in by_key.get((model, inp, out, ts16), []) if m != mid]
                updated += 1
            except Exception:
                pass

    conn.commit()
    return updated


# ── Main ──────────────────────────────────────────────────────────────────────

def import_all(verbose: bool = True) -> dict:
    _init_db()
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    cutoff = _proxy_cutoff(conn)

    results = {}

    # 1. Claude CLI (with proxy cutoff — proxy captures live traffic)
    ins, skip, err = _import_claude_jsonl(conn, [
        str(HOME / ".claude/projects/*/*.jsonl"),
        str(HOME / "Library/Application Support/Claude/projects/*/*.jsonl"),
    ], "claude-cli-history", cutoff)
    results["Claude CLI"] = (ins, skip, err)

    # 2. Claude Desktop (NO cutoff — proxy never sees Desktop sessions)
    ins, skip, err = _import_claude_jsonl(conn, [
        str(HOME / "Library/Application Support/Claude/local-agent-mode-sessions/**/*.jsonl"),
        str(HOME / ".config/Claude/local-agent-mode-sessions/**/*.jsonl"),
    ], "claude-desktop-history", None)
    results["Claude Desktop"] = (ins, skip, err)

    # 3. OpenClaw (with proxy cutoff)
    ins, skip, err = import_openclaw(conn, cutoff)
    results["OpenClaw"] = (ins, skip, err)

    # 4. Cline VSCode extension (no cutoff — proxy doesn't route Cline)
    ins, skip, err = import_cline_family(
        conn, "saoudrizwan.claude-dev", "cline-history", None)
    results["Cline"] = (ins, skip, err)

    # 5. Roo Code (same format as Cline)
    ins, skip, err = import_cline_family(
        conn, "rooveterinaryinc.roo-cline", "roo-code-history", None)
    results["Roo Code"] = (ins, skip, err)

    # 6. Kilo Code (same format as Cline)
    ins, skip, err = import_cline_family(
        conn, "kilocode.kilo-code", "kilo-code-history", None)
    results["Kilo Code"] = (ins, skip, err)

    # 7. IBM Bob (same JSON format as Cline)
    ins, skip, err = import_ibm_bob(conn)
    results["IBM Bob"] = (ins, skip, err)

    # 8. Pi (~/.pi/agent/sessions/)
    ins, skip, err = import_pi_omp(conn, ".pi/agent/sessions", "pi-history")
    results["Pi"] = (ins, skip, err)

    # 9. OMP (~/.omp/agent/sessions/) — identical schema to Pi
    ins, skip, err = import_pi_omp(conn, ".omp/agent/sessions", "omp-history")
    results["OMP"] = (ins, skip, err)

    # 10. Qwen CLI
    ins, skip, err = import_qwen(conn)
    results["Qwen"] = (ins, skip, err)

    # 11. Kimi
    ins, skip, err = import_kimi(conn)
    results["Kimi"] = (ins, skip, err)

    # 12. Mistral Vibe
    ins, skip, err = import_mistral_vibe(conn)
    results["Mistral Vibe"] = (ins, skip, err)

    # 13. Crush (SQLite per-project)
    ins, skip, err = import_crush(conn)
    results["Crush"] = (ins, skip, err)

    # 14. Goose (SQLite)
    ins, skip, err = import_goose(conn)
    results["Goose"] = (ins, skip, err)

    # 15. OpenCode (SQLite)
    ins, skip, err = import_opencode(conn)
    results["OpenCode"] = (ins, skip, err)

    # 16. Warp (SQLite — macOS only)
    ins, skip, err = import_warp(conn)
    results["Warp"] = (ins, skip, err)

    # 17. Gemini CLI
    ins, skip, err = import_gemini(conn)
    results["Gemini"] = (ins, skip, err)

    # 18. Codex (OpenAI CLI)
    ins, skip, err = import_codex(conn)
    results["Codex"] = (ins, skip, err)

    # 19. GitHub Copilot
    ins, skip, err = import_copilot(conn)
    results["Copilot"] = (ins, skip, err)

    # 20. Droid / Factory CLI
    ins, skip, err = import_droid(conn)
    results["Droid"] = (ins, skip, err)

    # Backfill prompt_preview for proxy-captured records by matching JSONL files
    backfilled = _backfill_previews_from_jsonl(conn)
    if backfilled:
        results["_backfill"] = (backfilled, 0, 0)

    _save_last_sync(conn)
    conn.commit()
    conn.close()
    return results


_VERSION_CACHE = Path(__file__).parent / ".version_cache.json"
_GITHUB_REPO   = "mr-beaver/tokencost"

def check_version_and_cache():
    import json, urllib.request
    local_ver_path = Path(__file__).parent / "VERSION"
    try:
        current = local_ver_path.read_text().strip()
    except Exception:
        return

    # re-check only once per day
    if _VERSION_CACHE.exists():
        try:
            cached = json.loads(_VERSION_CACHE.read_text())
            age = time.time() - cached.get("checked_at", 0)
            if age < 86400 and cached.get("current") == current:
                return  # still fresh
        except Exception:
            pass

    try:
        url = f"https://raw.githubusercontent.com/{_GITHUB_REPO}/main/VERSION"
        req = urllib.request.Request(url, headers={"User-Agent": "tokencost"})
        with urllib.request.urlopen(req, timeout=5) as r:
            latest = r.read().decode().strip()
        result = {
            "current":    current,
            "latest":     latest,
            "up_to_date": latest == current,
            "checked_at": time.time(),
            "update_cmd": f"cd {Path(__file__).parent} && git pull && bash onbording.sh",
        }
        _VERSION_CACHE.write_text(json.dumps(result))
    except Exception:
        pass


if __name__ == "__main__":
    silent = "--silent" in sys.argv
    if not silent:
        print("  Scanning provider logs...")

    t0 = time.time()
    results = import_all()
    elapsed = time.time() - t0

    total_new  = sum(r[0] for r in results.values())
    total_skip = sum(r[1] for r in results.values())

    if not silent:
        for provider, (ins, skip, err) in results.items():
            if ins or skip:
                line = f"  {provider}: +{ins} new"
                if skip:
                    line += f"  (skipped {skip})"
                print(line)
        print(f"  {'─'*38}")
        print(f"  Total new: {total_new}  ({elapsed:.1f}s)")
    elif total_new > 0:
        # Silent mode: only print if something changed (for log file)
        print(f"[import] +{total_new} new records from {sum(1 for r in results.values() if r[0])} providers")

    check_version_and_cache()
