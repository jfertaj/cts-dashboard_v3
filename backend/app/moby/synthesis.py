"""Synthesis fallback: a one-shot text-only Claude call to summarize prior
tool results when the agentic loop ends with empty assistant text.

Pure move from `app.routers.ai_chat` (Phase 1 refactor, commit 6149d56).
Behavior unchanged.

Indirection note: `_claude_chat` is resolved dynamically from the
`app.routers.ai_chat` module so existing tests that patch
`app.routers.ai_chat._claude_chat` keep working through the shim.
"""
from typing import Any, Dict, List, Optional

from app.moby.config import DEBUG


def _dbg(msg: str, *args):
    if DEBUG:
        try:
            print("[AI-CHAT]", msg % args if args else msg)
        except Exception:
            print("[AI-CHAT]", msg)


def _detect_lang_hint(text: str) -> str:
    """Cheap language hint for synthesis prompt. Returns 'es', 'it', 'fr', 'de', or 'en'."""
    t = (text or "").lower()
    if any(w in t for w in (" qué", "¿", "cuántos", "centros", "país", "españa", "italia ")):
        return "es"
    if any(w in t for w in ("quanti", " siti", "italia?", "nazione", "coordinatori")):
        return "it"
    if any(w in t for w in ("combien", "pédiatrique", "pays", "français")):
        return "fr"
    if any(w in t for w in ("wie viele", "standorte", "deutschland", "pädiatrisch")):
        return "de"
    return "en"


def _resolve_claude_chat():
    """Resolve `_claude_chat` via the ai_chat module so test patches apply."""
    from app.routers import ai_chat as _ai_chat_mod
    return _ai_chat_mod._claude_chat


def _synthesis_fallback(
    msgs: List[Dict[str, Any]],
    user_msg: str,
    prev_tools: int,
) -> Optional[str]:
    """One extra Claude call (no tools) to summarize prior tool results.

    Triggered when the agentic loop ends with empty text after >=1 successful tool_use.
    Returns the synthesized text, or None if it also fails / returns empty.
    """
    lang = _detect_lang_hint(user_msg)
    lang_word = {"es": "Spanish", "it": "Italian", "fr": "French", "de": "German", "en": "English"}[lang]
    prompt = (
        f"Please summarize the previous tool results in 2-3 sentences for the user. "
        f"Mention the key fields used and the actual numbers/findings. "
        f"Respond in {lang_word}."
    )
    msgs_extra = list(msgs) + [{"role": "user", "content": prompt}]
    _dbg("[AI-CHAT] synthesis-fallback triggered (prev_tools=%d, lang=%s)", prev_tools, lang)
    try:
        _claude_chat = _resolve_claude_chat()
        resp = _claude_chat(msgs_extra, tool_choice="auto", force_no_tools=True, use_thinking=False)
    except Exception as e:
        _dbg("[AI-CHAT] synthesis-fallback failed: %s", e)
        return None
    text = (resp.choices[0].message.content or "").strip()
    _dbg("[AI-CHAT] synthesis-fallback result text_len=%d", len(text))
    return text or None
