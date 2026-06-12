from __future__ import annotations
"""
Request-level cost optimizations:
- Auto cache_control on large system prompts
- Deduplication of identical requests within 5-second window
"""

import hashlib
import json

# ── Request deduplication cache (5 sec window) ──────────────────────────────────
_dedup_cache: dict = {}  # hash → (response, timestamp)
_DEDUP_TTL_SEC = 5

# ── Tool result cache (per-session, cleared on new session) ────────────────────
_tool_result_cache: dict = {}  # (tool_name, tool_input_hash) → (result_text, timestamp)
_TOOL_CACHE_TTL_SEC = 300  # 5 minutes within a session

# ── Per-session message tracking (detect new sessions, enforce max limit) ────
_last_message_count: int = 0
_message_count_threshold: int = 40  # force trim if exceeds this


def dedup_check(body_bytes: bytes, now: float) -> tuple:
    """
    Check if identical request was processed <5s ago.
    Returns (cached_response, req_hash) if found, else (None, req_hash).
    """
    req_hash = hashlib.sha256(body_bytes).hexdigest()
    if req_hash in _dedup_cache:
        cached_resp, cached_ts = _dedup_cache[req_hash]
        if now - cached_ts < _DEDUP_TTL_SEC:
            return cached_resp, req_hash
        else:
            del _dedup_cache[req_hash]
    return None, req_hash


def dedup_cache_response(req_hash: str, response: bytes, now: float):
    """Store successful response in dedup cache."""
    _dedup_cache[req_hash] = (response, now)


def tool_result_get(tool_name: str, tool_input: dict, now: float) -> str | None:
    """Check if we've seen this exact tool call recently. Returns cached result or None."""
    input_hash = hashlib.sha256(json.dumps(tool_input, sort_keys=True).encode()).hexdigest()
    cache_key = (tool_name, input_hash)

    if cache_key in _tool_result_cache:
        cached_result, cached_ts = _tool_result_cache[cache_key]
        if now - cached_ts < _TOOL_CACHE_TTL_SEC:
            return cached_result
        else:
            del _tool_result_cache[cache_key]
    return None


def tool_result_cache(tool_name: str, tool_input: dict, result_text: str, now: float):
    """Store tool result for future dedup."""
    input_hash = hashlib.sha256(json.dumps(tool_input, sort_keys=True).encode()).hexdigest()
    cache_key = (tool_name, input_hash)
    _tool_result_cache[cache_key] = (result_text, now)


def tool_cache_clear():
    """Clear tool cache (call when session ends)."""
    global _tool_result_cache
    _tool_result_cache.clear()


def enforce_max_messages(body_data: dict) -> tuple:
    """
    Detect new sessions and enforce per-session max message count.
    Returns (modified_body_data, trimmed_count_if_applied).
    """
    global _last_message_count
    messages = body_data.get("messages", [])
    current_count = len(messages)

    # Detect new session: message count dropped (user cleared or new session started)
    if current_count < _last_message_count * 0.5:
        _last_message_count = current_count
        return body_data, 0

    _last_message_count = current_count

    # If exceeds threshold, trim to last 30 messages (keep protected ones)
    if current_count > _message_count_threshold:
        protected_indices = set(range(current_count - 3, current_count))
        for i, msg in enumerate(messages):
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                        protected_indices.add(i)

        trimmed = []
        for i in range(current_count - 1, -1, -1):
            if i in protected_indices or len(trimmed) < 30:
                trimmed.insert(0, messages[i])

        trimmed_count = current_count - len(trimmed)
        body_data["messages"] = trimmed
        return body_data, trimmed_count

    return body_data, 0




def _get_message_content_text(content) -> str:
    """Extract all text from message content (handles str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    text_parts.append("[image]")
            elif isinstance(block, str):
                text_parts.append(block)
        return "".join(text_parts)
    return ""


def has_recent_tool_results(messages: list) -> bool:
    """Check if last 4 messages contain any tool_use or tool_result blocks."""
    recent = messages[-4:] if len(messages) >= 4 else messages
    for msg in recent:
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                    return True
    return False


def _count_message_tokens(msg: dict) -> int:
    """Rough token count for a message: role (1) + content."""
    tokens = 1
    content = msg.get("content", "")
    if isinstance(content, str):
        tokens += len(content) // 4
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text", "")
                if text:
                    tokens += len(text) // 4
    return max(1, tokens)


def should_throttle_stream(body_data: dict) -> bool:
    """Check if response should be throttled (stream=true and large request)."""
    if not body_data.get("stream"):
        return False
    messages = body_data.get("messages", [])
    total_tokens = sum(len(_get_message_content_text(m.get("content", ""))) // 4 for m in messages)
    return total_tokens > 10000


def throttle_stream_delay_ms(body_data: dict) -> int:
    """Calculate delay between stream chunks in milliseconds."""
    if not body_data.get("stream"):
        return 0
    messages = body_data.get("messages", [])
    total_tokens = sum(len(_get_message_content_text(m.get("content", ""))) // 4 for m in messages)
    if total_tokens < 5000:
        return 0
    elif total_tokens < 20000:
        return 10
    else:
        return 25


def trim_old_messages(body_data: dict, max_input_tokens: int = 50000) -> tuple:
    """
    Remove oldest non-critical messages if total tokens > max_input_tokens.
    Preserves: system, last 3 messages, and messages with tool_use/tool_result.
    Returns (modified_body_data, token_saved) if trimmed, else (body_data, 0).
    """
    messages = body_data.get("messages", [])
    if len(messages) <= 3:
        return body_data, 0

    total_tokens = sum(_count_message_tokens(m) for m in messages)
    if total_tokens <= max_input_tokens:
        return body_data, 0

    # Protect last 3 messages and any with tool blocks
    protected_indices = set(range(len(messages) - 3, len(messages)))
    for i, msg in enumerate(messages):
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                    protected_indices.add(i)

    # Remove oldest unprotected messages until under budget
    trimmed = []
    for i in range(len(messages) - 1, -1, -1):
        if i in protected_indices or len(trimmed) < 3:
            trimmed.insert(0, messages[i])
        else:
            total_tokens -= _count_message_tokens(messages[i])
            if total_tokens <= max_input_tokens:
                break

    tokens_saved = sum(_count_message_tokens(m) for m in messages) - sum(_count_message_tokens(m) for m in trimmed)
    body_data["messages"] = trimmed
    return body_data, tokens_saved


def complexity_score(body_data: dict) -> int:
	"""
	Estimate request complexity 0-10 based on:
	- number of messages
	- total content length
	- presence of tool calls
	- message diversity
	"""
	score = 0
	messages = body_data.get("messages", [])

	# Message count: +1 per 2 messages (max 4)
	score += min(len(messages) // 2, 4)

	# Content length: +1 per 20k chars (max 4)
	total_chars = sum(len(_get_message_content_text(m.get("content", ""))) for m in messages)
	score += min(total_chars // 20000, 4)

	# Has tool_use or tool_result: +2
	if any(m.get("content") for m in messages):
		for msg in messages:
			content = msg.get("content", [])
			if isinstance(content, list):
				for block in content:
					if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
						score += 2
						break

	return min(score, 10)


_ERROR_KEYWORDS = ("error", "failed", "traceback", "exception", "invalid", "syntax error",
                   "attributeerror", "typeerror", "valueerror", "keyerror", "nameerror",
                   "cannot", "not found", "refused", "denied", "undefined")


def _has_tool_errors(messages: list) -> bool:
    """Return True if any tool_result in last 6 messages contains error keywords."""
    recent = messages[-6:] if len(messages) >= 6 else messages
    for msg in recent:
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    # tool_result content can be str or list of blocks
                    inner = block.get("content", "")
                    if isinstance(inner, list):
                        inner = " ".join(b.get("text", "") for b in inner if isinstance(b, dict))
                    if any(kw in (inner or "").lower() for kw in _ERROR_KEYWORDS):
                        return True
    return False


def auto_enable_thinking(body_data: dict) -> tuple:
    """
    Auto-enable extended thinking when tool chain has errors.
    Only activates if thinking is not already set.
    Returns (modified_body_data, opt_tag_or_None).
    """
    # Skip if thinking already set by client
    if body_data.get("thinking"):
        return body_data, None

    messages = body_data.get("messages", [])
    if not messages:
        return body_data, None

    if _has_tool_errors(messages):
        complexity = complexity_score(body_data)
        budget = 5000 if complexity >= 5 else 3000
        body_data["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return body_data, ("think ", f"auto-enabled thinking (tool errors detected, budget={budget}, complexity={complexity})")

    return body_data, None


def limit_thinking_budget(body_data: dict) -> tuple:
	"""
	Auto-limit budget_tokens for thinking mode based on complexity.
	Returns (modified_body_data, optimization_tag_if_applied).
	"""
	thinking = body_data.get("thinking")
	if not thinking:
		return body_data, None

	if isinstance(thinking, dict) and "budget_tokens" not in thinking:
		complexity = complexity_score(body_data)
		if complexity < 4:
			thinking["budget_tokens"] = 2000
			return body_data, ("thinking", f"limited budget to 2k tokens (complexity {complexity})")
		elif complexity < 7:
			thinking["budget_tokens"] = 5000
			return body_data, ("thinking", f"limited budget to 5k tokens (complexity {complexity})")

	return body_data, None


def optimize_request(body_data: dict) -> tuple:
    """
    Apply all cost optimizations to the request body.
    Returns (modified_body_data, [("tag", "message"), ...]) for logging.
    """
    optimizations = []

    # Limit thinking budget if client already requested thinking
    body_data, thinking_opt = limit_thinking_budget(body_data)
    if thinking_opt:
        optimizations.append(thinking_opt)

    # 1. Auto cache_control on system prompt if not already cached
    if "system" in body_data:
        system = body_data["system"]
        if isinstance(system, str) and len(system) > 1000:
            if "cache_control" not in body_data:
                body_data["cache_control"] = {"type": "ephemeral"}
                optimizations.append(("cache", f"auto-caching system prompt (~{len(system)} chars)"))

    # 2. Auto cache_control on large user messages (if not already cached)
    messages = body_data.get("messages", [])
    if messages and "cache_control" not in body_data:
        last_msg = messages[-1]
        if last_msg.get("role") == "user":
            content = last_msg.get("content")
            content_text = _get_message_content_text(content)
            content_len = len(content_text)
            # Cache user messages > 5000 chars (roughly 1250+ tokens)
            if content_len > 5000:
                body_data["cache_control"] = {"type": "ephemeral"}
                optimizations.append(("cache", f"auto-caching user message (~{content_len} chars)"))

    return body_data, optimizations


def calculate_optimization_savings(optimizations: list, model: str, input_tokens: int,
                                    output_tokens: int, cache_read_tokens: int) -> tuple:
    """
    Calculate actual savings from optimizations.
    Returns (optimizations_json, total_savings_usd) where optimizations_json is a list
    of dicts with {type, saved_usd, ...details}.

    Requires pricing info from db.py's PRICING dict.
    """
    from db import PRICING

    result = []

    for tag, msg in optimizations:
        tag_clean = tag.strip()
        saved = 0

        if tag_clean == "routing":
            # Extract model change from message: "model1 → model2"
            if "→" in msg:
                parts = msg.split("→")
                orig = parts[0].strip().split()[-1]  # last word before arrow
                routed = parts[1].strip().split()[0]  # first word after arrow

                orig_price = PRICING.get(orig, {})
                routed_price = PRICING.get(routed, {})

                if orig_price and routed_price:
                    orig_cost = (input_tokens * orig_price.get("input", 0) +
                                output_tokens * orig_price.get("output", 0)) / 1_000_000
                    routed_cost = (input_tokens * routed_price.get("input", 0) +
                                  output_tokens * routed_price.get("output", 0)) / 1_000_000
                    saved = max(0, orig_cost - routed_cost)

                    result.append({
                        "type": "routing",
                        "from": orig,
                        "to": routed,
                        "saved_usd": round(saved, 6)
                    })

        elif tag_clean == "cache":
            # Cache savings: read_tokens × (input_price - cache_read_price)
            # Assume 90% savings on cache read (0.10× cost)
            if cache_read_tokens > 0:
                model_price = PRICING.get(model, {})
                input_price = model_price.get("input", 0)
                cache_read_price = input_price * 0.1  # 90% cheaper
                saved = (cache_read_tokens * (input_price - cache_read_price)) / 1_000_000
                saved = max(0, saved)

                result.append({
                    "type": "cache",
                    "read_tokens": cache_read_tokens,
                    "saved_usd": round(saved, 6)
                })

        elif tag_clean == "think":
            # Thinking budget limited: rough estimate
            # If complexity low (2k budget) vs high (30k) = save ~8k output tokens
            if "complexity" in msg:
                saved = 0.02  # Conservative estimate
                result.append({
                    "type": "thinking",
                    "reason": msg,
                    "saved_usd": round(saved, 6)
                })

        elif tag_clean == "session":
            # Session trim: rough estimate from message
            if "trimmed" in msg and "messages" in msg:
                try:
                    import re
                    match = re.search(r"trimmed (\d+)", msg)
                    if match:
                        trimmed_msgs = int(match.group(1))
                        # Rough: ~200 tokens per message, input price
                        model_price = PRICING.get(model, {})
                        input_price = model_price.get("input", 0)
                        trimmed_tokens = trimmed_msgs * 200
                        saved = (trimmed_tokens * input_price) / 1_000_000
                        saved = max(0, saved)

                        result.append({
                            "type": "trim",
                            "messages_removed": trimmed_msgs,
                            "saved_usd": round(saved, 6)
                        })
                except:
                    pass

    import json
    return json.dumps(result), sum(s.get("saved_usd", 0) for s in result)
