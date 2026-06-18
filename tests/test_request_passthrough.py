"""v1.1.3 — request-mutation boundary (docs/adr/0001).

The proxy forwards the client's request verbatim except for routing
normalization (strip what the cheaper target rejects) and the opt-in cache
injection (only when the client caches nothing). These tests pin that boundary.
"""
import proxy
import optimizer


# ── _normalize_for_downgrade ─────────────────────────────────────────────────

def test_effort_stripped_when_routing_to_haiku():
    body = {
        "model": "claude-haiku-4-5-20251001",
        "output_config": {"effort": "high"},
        "thinking": {"type": "enabled", "budget_tokens": 2000},
    }
    headers = {"anthropic-beta": "context-1m-2025-08-07,oauth-2025-04-20"}
    proxy._normalize_for_downgrade(body, headers, "claude-haiku-4-5-20251001")
    # effort gone, empty output_config cleaned up
    assert "output_config" not in body
    # context-1m removed but the other beta token preserved
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    # passthrough: thinking + budget_tokens untouched
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2000}


def test_top_level_effort_stripped_on_haiku():
    body = {"effort": "high"}
    proxy._normalize_for_downgrade(body, {}, "claude-haiku-4-5-20251001")
    assert "effort" not in body


def test_effort_preserved_when_routing_to_sonnet():
    # Sonnet 4.6 accepts the effort parameter — it must NOT be stripped.
    body = {"model": "claude-sonnet-4-6", "output_config": {"effort": "high"}}
    proxy._normalize_for_downgrade(body, {}, "claude-sonnet-4-6")
    assert body["output_config"]["effort"] == "high"


def test_context_1m_header_dropped_when_sole_token():
    headers = {"anthropic-beta": "context-1m-2025-08-07"}
    proxy._normalize_for_downgrade({}, headers, "claude-haiku-4-5-20251001")
    assert "anthropic-beta" not in headers


def test_context_1m_stripped_even_for_sonnet_route():
    # Header normalization fires on any downgrade (safe: simple prompts
    # never need 1M context); effort is the only target-specific part.
    headers = {"anthropic-beta": "context-1m-2025-08-07,oauth-2025-04-20"}
    proxy._normalize_for_downgrade({}, headers, "claude-sonnet-4-6")
    assert headers["anthropic-beta"] == "oauth-2025-04-20"


# ── optimizer._has_cache_control ─────────────────────────────────────────────

def test_has_cache_control_top_level():
    assert optimizer._has_cache_control({"cache_control": {"type": "ephemeral"}})


def test_has_cache_control_system_block():
    body = {"system": [{"type": "text", "text": "x",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}
    assert optimizer._has_cache_control(body)


def test_has_cache_control_tool_block():
    body = {"tools": [{"name": "t", "cache_control": {"type": "ephemeral"}}]}
    assert optimizer._has_cache_control(body)


def test_has_cache_control_message_block():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]}]}
    assert optimizer._has_cache_control(body)


def test_has_cache_control_none():
    body = {"system": "plain", "messages": [{"role": "user", "content": "hi"}]}
    assert not optimizer._has_cache_control(body)


# ── cache injection boundary ──────────────────────────────────────────────────

def test_skips_injection_when_client_already_caches():
    # Client (e.g. Claude Code) caches at the block level with 1h TTL — the
    # proxy must NOT inject a top-level (5m) cache_control and collide.
    body = {
        "system": [{"type": "text", "text": "x" * 2000,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    out, _opts = optimizer.optimize_request(body)
    assert "cache_control" not in out


def test_injects_when_client_does_not_cache():
    # Non-caching client with a large system prompt still gets the optimization.
    body = {"system": "x" * 2000, "messages": [{"role": "user", "content": "hi"}]}
    out, _opts = optimizer.optimize_request(body)
    assert out.get("cache_control") == {"type": "ephemeral"}


# ── thinking passthrough ──────────────────────────────────────────────────────

def test_budget_tokens_preserved():
    # The current API requires budget_tokens for thinking.enabled — the
    # optimizer must not strip it.
    body = {"thinking": {"type": "enabled", "budget_tokens": 5000}}
    out, _tag = optimizer.limit_thinking_budget(body)
    assert out["thinking"]["budget_tokens"] == 5000
