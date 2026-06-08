"""Moby configuration constants.

Pure constants extracted from `app.routers.ai_chat` (Phase 1, refactor).
Movement is verbatim — no behavior change. `ai_chat.py` re-exports these
via shim imports so existing consumers (and tests patching
`app.routers.ai_chat.X`) keep working.
"""
import os

DEBUG = os.environ.get("AI_CHAT_DEBUG", "0") == "1"
INDEX_REFRESH_SEC = int(os.environ.get("AI_INDEX_REFRESH_SEC", "600"))
FIELDS_SF_JSON_PATH = os.environ.get(
    "FIELDS_SF_JSON_PATH", "app/config/fields_opportunity_curated.json"
)
QUAL_ALIAS_JSON_PATH = os.environ.get(
    "QUAL_ALIAS_JSON_PATH", "app/config/qualification_aliases.json"
)
EXPLORER_DRIVE_KM_PATH = "/api/explorer/search/within-drive-km"
EXPLORER_SEARCH_PATH = "/api/explorer/search"

# Max user turns to keep in history before truncating (each turn = 1 user + 1 assistant message).
# Keeps context focused and prevents unbounded token growth in long conversations.
MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "12"))

# Token budget for extended thinking on complex queries.
# The model may use up to this many tokens to reason before responding.
# max_tokens is automatically raised to budget + 8192 when thinking is enabled.
CLAUDE_THINKING_BUDGET = int(os.environ.get("CLAUDE_THINKING_BUDGET", "8000"))

# Sampling temperature for Claude calls. 0 = deterministic, to cut run-to-run
# variance in tool selection and generated SOQL/filters (a measured driver of
# "same question -> 8 rows vs 0 rows"). NOTE: extended thinking requires
# temperature=1, so this is only applied when thinking is OFF (see claude_client).
CLAUDE_TEMPERATURE = float(os.getenv("CLAUDE_TEMPERATURE", "0"))

# Agentic loop limits
# 5 turns (was 3) gives the loop room to detect a 0-row result and retry with a
# different formulation before exhausting its budget.
MOBY_MAX_AGENT_TURNS = int(os.getenv("MOBY_MAX_AGENT_TURNS", "5"))
MAX_TOOL_RESULT_TOKENS = int(os.getenv("MAX_TOOL_RESULT_TOKENS", "4000"))
MOBY_AGENT_TIMEOUT_S = int(os.getenv("MOBY_AGENT_TIMEOUT_S", "30"))

# Claude model + beta headers
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"

# Limit for previewing aliases in the system hints (smaller → faster)
INDEX_PREVIEW_LIMIT = int(os.environ.get("AI_INDEX_PREVIEW_LIMIT", "60"))
