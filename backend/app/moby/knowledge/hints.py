"""Semantic hint generation for Moby system-prompt injection.

Extracted from `app.routers.ai_chat` (Phase 5b refactor).
"""
from __future__ import annotations

import os
from time import monotonic as _monotonic
from typing import Any, Dict, List

from app.moby.knowledge.text import _normalize


def _format_hints_block(hits: List[Dict[str, Any]]) -> str:
    """Render semantic field hits as a markdown bullet list for the system prompt."""
    lines = [
        "## Relevant fields (semantic hint, top-K=8)",
        "The following Salesforce fields are most semantically relevant to "
        "the user's question. Prefer these when calling tools, especially "
        "for multilingual queries.",
        "",
    ]
    for h in hits:
        key = h.get("key") or ""
        label = h.get("label") or ""
        lines.append(f"- `{key}` — {label}")
    return "\n".join(lines)


def _semantic_field_hints(user_msg: str) -> str:
    """Return a markdown block of top-K semantic field hits for system-prompt injection.

    Gated by MOBY_SEMANTIC_FIELD_RESOLVER=1. Any failure (missing index,
    Bedrock unavailable, exception) returns "" — no regression vs. baseline.
    """
    if os.getenv("MOBY_SEMANTIC_FIELD_RESOLVER", "0") != "1":
        return ""
    if not user_msg or not user_msg.strip():
        return ""
    t0 = _monotonic()
    try:
        from app.moby_semantic_resolver import SemanticFieldResolver
        resolver = SemanticFieldResolver()
        raw_hits = resolver.resolve(user_msg, top_n=4)
        # Score gate: drop noise (≥0.5 only). A/B test showed top-K below this
        # threshold introduced regressions on trivial English queries.
        hits = [h for h in raw_hits if h.get("score", 0.0) >= 0.5]
        ms = (_monotonic() - t0) * 1000.0
        keys_preview = [h.get("key") for h in hits]
        cache = getattr(resolver, "last_cache_status", "?")
        print(
            f"[moby-semantic] q={user_msg[:80]!r} top={keys_preview} "
            f"filtered_to={len(hits)}/{len(raw_hits)} cache={cache} latency={ms:.0f}ms",
            flush=True,
        )
        if not hits:
            return ""
        return _format_hints_block(hits)
    except Exception as exc:
        ms = (_monotonic() - t0) * 1000.0
        print(f"[moby-semantic] q={user_msg[:80]!r} ERROR={exc!r} latency={ms:.0f}ms", flush=True)
        return ""


def _top_matches(q: str, aliases: List[str], k: int = 5) -> List[str]:
    """
    Matching ligero: intersección de tokens normalizados + prefiero substrings.

    NOTE: the agentic loop (Claude tool-use) bypasses this helper entirely.
    Semantic guidance is injected into the system prompt via
    `_semantic_field_hints` (Option A wiring in `chat_api`). `_top_matches`
    is kept because the deterministic planner inside `chat_api` still calls
    it from ~6 sites (alias resolution for short-circuited intents).
    """
    qn = _normalize(q)
    qtokens = set(qn.split())
    scored = []
    for a in aliases:
        atoks = set(a.split())
        inter = len(qtokens & atoks)
        bonus = 1 if a in qn or qn in a else 0
        score = inter * 2 + bonus
        if score > 0:
            scored.append((score, a))
    scored.sort(reverse=True)
    return [a for _, a in scored[:k]]
