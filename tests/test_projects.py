"""projects.py — Claude Code JSONL → per-project / session / shell-command rollups.

Characterization tests: pin current behaviour of the pure helpers and the
get_project_stats aggregation. CLAUDE_DIR is redirected to a temp fixture dir so
nothing reads the real ~/.claude/projects, and the module cache is cleared per
test so results never bleed across cases.
"""
import json

import pytest

import projects


# ── _cost ─────────────────────────────────────────────────────────────────────

def test_cost_input_only():
    # opus-4-7 input = $5/MTok → 1M input = $5.00
    assert projects._cost("claude-opus-4-7", 1_000_000, 0, 0, 0) == pytest.approx(5.0)


def test_cost_output_only():
    # opus-4-7 output = $25/MTok → 1M output = $25.00
    assert projects._cost("claude-opus-4-7", 0, 1_000_000, 0, 0) == pytest.approx(25.0)


def test_cost_cache_read_is_0_10x_input():
    assert projects._cost("claude-opus-4-7", 0, 0, 1_000_000, 0) == pytest.approx(0.5)


def test_cost_cache_write_is_1_25x_input():
    assert projects._cost("claude-opus-4-7", 0, 0, 0, 1_000_000) == pytest.approx(6.25)


def test_cost_unknown_model_uses_default():
    # default = input 3.0 / output 15.0
    assert projects._cost("nope", 1_000_000, 0, 0, 0) == pytest.approx(3.0)


# ── _abbrev ───────────────────────────────────────────────────────────────────

def test_abbrev_strips_home_prefix(monkeypatch):
    monkeypatch.setattr(projects, "HOME_PREFIX", "/home/me/")
    assert projects._abbrev("/home/me/code/app") == "code/app"


def test_abbrev_leaves_non_home_path(monkeypatch):
    monkeypatch.setattr(projects, "HOME_PREFIX", "/home/me/")
    assert projects._abbrev("/etc/config") == "/etc/config"


# ── _first_cmd ─────────────────────────────────────────────────────────────────

def test_first_cmd_basic():
    assert projects._first_cmd("git status") == "git"


def test_first_cmd_strips_wrapper():
    assert projects._first_cmd("sudo apt update") == "apt"


def test_first_cmd_basenames_path():
    assert projects._first_cmd("/usr/bin/python -V") == "python"


def test_first_cmd_rejects_flag_first_token():
    assert projects._first_cmd("--help") is None


def test_first_cmd_empty_is_none():
    assert projects._first_cmd("   ") is None


# ── _cutoff_ts ─────────────────────────────────────────────────────────────────

def test_cutoff_all_is_none():
    assert projects._cutoff_ts("all") is None


def test_cutoff_7d_returns_iso_string():
    # Don't pin the exact instant (clock-dependent); just confirm a cutoff exists.
    assert isinstance(projects._cutoff_ts("7d"), str)


# ── get_project_stats (integration with a fixture dir) ─────────────────────────

@pytest.fixture()
def claude_dir(tmp_path, monkeypatch):
    """Redirect CLAUDE_DIR at a temp tree and clear the period cache."""
    monkeypatch.setattr(projects, "CLAUDE_DIR", str(tmp_path))
    monkeypatch.setattr(projects, "HOME_PREFIX", "/home/me/")
    projects._cache.clear()
    return tmp_path


def _write_jsonl(claude_dir, project_subdir, filename, events):
    d = claude_dir / project_subdir
    d.mkdir(parents=True, exist_ok=True)
    path = d / filename
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return path


def _assistant(cwd, sid, ts, model="claude-opus-4-7", inp=0, out=0, cr=0, cw=0,
               content=None):
    return {
        "type": "assistant",
        "cwd": cwd,
        "sessionId": sid,
        "timestamp": ts,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp, "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
            },
            "content": content or [],
        },
    }


def test_by_project_aggregates_cost_and_calls(claude_dir):
    _write_jsonl(claude_dir, "proj-a", "s.jsonl", [
        _assistant("/home/me/proj-a", "s1", "2026-01-01T10:00:00Z", inp=1_000_000),
        _assistant("/home/me/proj-a", "s1", "2026-01-01T10:01:00Z", out=1_000_000),
    ])
    res = projects.get_project_stats("all")
    assert len(res["by_project"]) == 1
    p = res["by_project"][0]
    assert p["path"] == "proj-a"
    assert p["calls"] == 2
    assert p["sessions"] == 1
    # 1M input ($5) + 1M output ($25) = $30
    assert p["cost"] == pytest.approx(30.0)
    assert p["avg_per_session"] == pytest.approx(30.0)


def test_top_sessions_sorted_by_cost_desc(claude_dir):
    _write_jsonl(claude_dir, "proj-a", "a.jsonl", [
        _assistant("/home/me/proj-a", "cheap", "2026-01-01T10:00:00Z", inp=1_000_000),
    ])
    _write_jsonl(claude_dir, "proj-b", "b.jsonl", [
        _assistant("/home/me/proj-b", "pricey", "2026-01-01T11:00:00Z", out=1_000_000),
    ])
    res = projects.get_project_stats("all")
    costs = [s["cost"] for s in res["top_sessions"]]
    assert costs == sorted(costs, reverse=True)
    assert res["top_sessions"][0]["path"] == "proj-b"   # $25 > $5


def test_shell_commands_counted_from_bash_tool_use(claude_dir):
    content = [
        {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "git log"}},
        {"type": "tool_use", "name": "Read", "input": {"file": "x"}},  # not Bash
    ]
    _write_jsonl(claude_dir, "proj-a", "s.jsonl", [
        _assistant("/home/me/proj-a", "s1", "2026-01-01T10:00:00Z", content=content),
    ])
    res = projects.get_project_stats("all")
    cmds = {c["name"]: c["count"] for c in res["shell_commands"]}
    assert cmds == {"git": 2}


def test_non_assistant_and_missing_cwd_skipped(claude_dir):
    _write_jsonl(claude_dir, "proj-a", "s.jsonl", [
        {"type": "user", "cwd": "/home/me/proj-a", "message": {}},      # not assistant
        _assistant("", "s1", "2026-01-01T10:00:00Z", inp=1_000_000),    # no cwd
    ])
    res = projects.get_project_stats("all")
    assert res["by_project"] == []


def test_cutoff_filters_old_events(claude_dir):
    _write_jsonl(claude_dir, "proj-a", "s.jsonl", [
        _assistant("/home/me/proj-a", "s1", "2020-01-01T00:00:00Z", inp=1_000_000),
    ])
    # 7d cutoff is far after 2020 → the event is filtered out.
    res = projects.get_project_stats("7d")
    assert res["by_project"] == []


def test_empty_dir_returns_empty_rollups(claude_dir):
    res = projects.get_project_stats("all")
    assert res["by_project"] == []
    assert res["top_sessions"] == []
    assert res["shell_commands"] == []
    assert res["period"] == "all"
