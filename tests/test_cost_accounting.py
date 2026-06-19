"""v1.1.4 — cache-write TTL pricing accuracy + observed-TTL detection.

Covers calc_cost (5m vs 1h multipliers), the response parser's 1h-split
extraction, and _pause_analysis's observed-TTL derivation.
"""
import json

import pytest

import db
import proxy


# ── db.calc_cost ─────────────────────────────────────────────────────────────

def test_1h_write_is_2x_input():
    # 1M tokens, all 1h → $5/MTok * 2.0 = $10.00
    assert db.calc_cost("claude-opus-4-8", 0, 0, 0, 1_000_000, 1_000_000) == pytest.approx(10.0)


def test_5m_write_is_1_25x_input():
    # all 5m (1h portion = 0) → $5/MTok * 1.25 = $6.25
    assert db.calc_cost("claude-opus-4-8", 0, 0, 0, 1_000_000, 0) == pytest.approx(6.25)


def test_mixed_5m_and_1h():
    # 500k 5m (1.25x) + 500k 1h (2x) → (0.5*1.25 + 0.5*2.0) * 5 = $8.125
    assert db.calc_cost("claude-opus-4-8", 0, 0, 0, 1_000_000, 500_000) == pytest.approx(8.125)


def test_cache_read_is_0_10x_input():
    # 1M read tokens → $5/MTok * 0.10 = $0.50
    assert db.calc_cost("claude-opus-4-8", 0, 0, 1_000_000, 0, 0) == pytest.approx(0.5)


def test_back_compat_without_1h_arg():
    # Old call sites that omit the 1h argument keep pricing writes at 1.25x.
    assert db.calc_cost("claude-opus-4-8", 0, 0, 0, 1_000_000) == pytest.approx(6.25)


def test_1h_never_cheaper_than_5m():
    cost_5m = db.calc_cost("claude-opus-4-8", 0, 0, 0, 1_000_000, 0)
    cost_1h = db.calc_cost("claude-opus-4-8", 0, 0, 0, 1_000_000, 1_000_000)
    assert cost_1h > cost_5m


# ── proxy._parse_anthropic — 1h-split extraction ─────────────────────────────
# Return tuple: (model, in, out, cache_read, cache_creation, cache_creation_1h,
#                stop, tools, tool_names, msg_id)

def test_json_extracts_1h_portion():
    body = json.dumps({
        "model": "claude-opus-4-8",
        "usage": {
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_input_tokens": 1000,
            "cache_creation": {"ephemeral_5m_input_tokens": 200,
                               "ephemeral_1h_input_tokens": 800},
        },
    }).encode()
    res = proxy._parse_anthropic(body, "application/json")
    assert res[4] == 1000   # total cache_creation
    assert res[5] == 800    # 1h portion


def test_json_without_breakdown_defaults_1h_zero():
    body = json.dumps({
        "model": "m",
        "usage": {"cache_creation_input_tokens": 500},
    }).encode()
    res = proxy._parse_anthropic(body, "application/json")
    assert res[4] == 500
    assert res[5] == 0      # back-compat: no breakdown → 1h=0


def test_sse_extracts_1h_portion():
    sse = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"model":"claude-opus-4-8",'
        '"id":"msg_x","usage":{"input_tokens":3,'
        '"cache_creation_input_tokens":1000,'
        '"cache_creation":{"ephemeral_1h_input_tokens":1000}}}}\n\n'
    ).encode()
    res = proxy._parse_anthropic(sse, "text/event-stream")
    assert res[5] == 1000


# ── db._pause_analysis — observed TTL ────────────────────────────────────────
# Uses the shared tmp_db + seed_requests fixtures from conftest.py.

import datetime


def _seed_session(seed_requests, cw, cw_1h, n=5, gap_min=10):
    """Insert n requests spaced gap_min apart (a session in the 5–60 min band)."""
    base = datetime.datetime(2026, 1, 1, 12, 0, 0)
    for i in range(n):
        seed_requests(
            ts=base + datetime.timedelta(minutes=gap_min * i),
            model="m", input_tokens=10, output_tokens=5,
            cache_creation_tokens=cw, cache_creation_1h_tokens=cw_1h,
        )


def test_observed_1h_when_writes_are_1h(seed_requests):
    _seed_session(seed_requests, cw=1000, cw_1h=1000)   # 100% 1h
    pa = db._pause_analysis("all")
    assert pa["cache_1h_pct"] == 100
    assert pa["observed_ttl"] == "1h"


def test_observed_5min_on_split_less_history(seed_requests):
    _seed_session(seed_requests, cw=1000, cw_1h=0)      # old rows: no 1h split
    pa = db._pause_analysis("all")
    assert pa["observed_ttl"] == "5 min"


def test_observed_mixed(seed_requests):
    _seed_session(seed_requests, cw=1000, cw_1h=500)    # 50% 1h
    pa = db._pause_analysis("all")
    assert pa["observed_ttl"] == "mixed"
