# Moby — force-tool guard for tabular-intent queries

**Date:** 2026-04-22
**Branch:** `refactor/moby-agentic-loop`
**Scope:** Plan A of the "custom loop vs Claude Agent SDK" brainstorm (2026-04-22). Plan B (expose `TOOL_DISPATCH` as MCP) is scheduled separately.

## Problem

The Moby agentic loop (`backend/app/routers/ai_chat.py::_agentic_loop`, line 4770) is flaky on tabular-intent queries: Claude sometimes returns a correct textual answer without calling any tool that produces a renderable table. The frontend then shows no table artifact, and the regression rerun classifies the question as FAIL.

Evidence (`docs/moby-bulk-eval-20260421-delta.md`, `docs/moby-regression-rerun.json`):

| ID | Query | Symptom |
|---|---|---|
| SC06 | "Which sites in France have HLA typing capability available?" | Correct text, no table |
| SS01 | "Show me all sites where profiling form has been uploaded to the database" | "I wasn't able to answer..." — loop gives up |
| SS03 | "List sites where C_Profiling_Complete is true" | Claude self-diagnoses the field-type issue but does not re-query |

SC06/SS01/SS03 flip between PASS and FAIL across identical reruns, which is the blocker for merging `refactor/moby-agentic-loop` to `main`.

## Non-goals

- Migrating the loop to the Claude Agent SDK (evaluated and rejected on 2026-04-22 — see the brainstorm note in memory `project_state_2026_04_22_triage_r1.md`).
- Exposing `TOOL_DISPATCH` as an MCP server (Plan B, separate spec).
- Fixing the other 7 regressions in the 121-question bulk eval (SC01/SC09/CL05/SC10/SS04/FA01/RE01 are out of scope — they have distinct root causes).
- Touching `_try_planner`, `moby_planner.py`, or the system prompt.
- Adding CloudWatch metrics or latency SLAs.

## Design overview

Two-part fix, contained to one new module plus a minimal surface-area change in `ai_chat.py`:

1. **Tool-choice forcing in turn 1.** When the user's message has tabular intent (keyword-based detector in EN+ES), turn 1 of the loop calls Claude with `tool_choice="required"` and a whitelist of the four table-returning tools. This prevents Claude from answering from memory and prevents it from picking a non-table tool like `soql_query`.

2. **Post-loop retry on text-only outcome.** If the loop exits with a text answer but no table was ever populated AND the original query had tabular intent, one extra Claude call is made with the same whitelist + `tool_choice="required"` + a user-message hint stating the previous answer lacked a table. Cap is one retry; it does not count against `MOBY_MAX_AGENT_TURNS`.

Queries without tabular intent keep the existing behaviour (`tool_choice="auto"`, full `TOOLS_SPEC`, no retry). Queries that already produced a table in turn 1 keep the existing behaviour (fast-exit or normal synthesis turn).

## Components

### New: `backend/app/routers/moby_tool_policy.py`

Standalone module. No FastAPI, no DB, no Anthropic SDK imports. Pure functions + constants. ~80 lines.

```python
from __future__ import annotations
import re
import unicodedata
from typing import Iterable

TABLE_RETURNING_TOOLS: frozenset[str] = frozenset({
    "explorer_search",
    "nearest_filtered_sites",
    "study_coordinators_with_activities",
    "members_search",
})

_POSITIVES: tuple[str, ...] = (
    # EN imperatives
    "list", "show", "show me", "give", "give me", "find", "display",
    "fetch", "get", "get me", "return", "search", "search for", "pull up",
    # ES imperatives
    "lista", "muestra", "muestrame", "dame", "ensena", "busca",
    "buscame", "encuentra", "saca", "sacame", "trae", "traeme",
    # EN interrogatives
    "how many", "how much", "which", "which ones",
    "what sites", "what members", "what centers", "what countries",
    "what coordinators",
    # ES interrogatives
    "cuantos", "cuantas", "cuales",
    "que sitios", "que miembros", "que centros", "que paises",
    # Markers
    "list of", "a list", "a table", "as a table", "as table",
    "in a table", "table of", "report", "overview",
    "lista de", "en tabla", "como tabla", "una tabla", "tabla de",
)

_BLACKLIST: tuple[str, ...] = (
    "summarize", "summary of", "explain", "what does", "what is the",
    "why", "how do i", "can you explain", "describe", "tell me about",
    "show me what", "show me how",
    "resume", "explica", "que significa", "por que",
    "como puedo", "como hago", "puedes explicar",
    "cuentame sobre", "ensename que", "ensename como",
)

def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()

def has_tabular_intent(user_msg: str) -> bool:
    """True if the query explicitly asks for a list/table/count/search result.

    Blacklist wins: conversational phrasings ("explain X", "what does Y mean")
    return False even if a positive keyword also matches.
    """
    if not user_msg:
        return False
    norm = _norm(user_msg)
    if any(p in norm for p in _BLACKLIST):
        return False
    return any(p in norm for p in _POSITIVES)

def filter_tools_spec(
    tools_spec: list[dict],
    whitelist: Iterable[str] = TABLE_RETURNING_TOOLS,
) -> list[dict]:
    """Return a copy of tools_spec containing only tools whose name is in the whitelist.

    Preserves order and shape of each spec entry. Unknown names in whitelist
    are silently ignored.
    """
    wl = frozenset(whitelist)
    return [t for t in tools_spec if t.get("function", {}).get("name") in wl]
```

### Changed: `backend/app/routers/ai_chat.py`

**`_claude_chat` signature (line 4528)** — add one kwarg:

```python
def _claude_chat(
    messages,
    tool_choice: str = "required",
    *,
    force_no_tools: bool = False,
    use_thinking: bool = False,
    tools_override: list[dict] | None = None,   # NEW
):
    ...
    # Line 4544, inside the `if not force_no_tools:` block:
    source_spec = tools_override if tools_override is not None else TOOLS_SPEC
    for t in source_spec:
        ...
```

When `tools_override` is passed, it replaces `TOOLS_SPEC` entirely for this call. Cache header still applies to the last tool in the override list (cache will miss on the first retry, then warm up if multiple retries happen — expected frequency: rare).

**`_agentic_loop` (line ~4770)** — three insertions:

1. Before the loop: `_tabular = has_tabular_intent(user_msg)`, `_whitelist_spec = filter_tools_spec(TOOLS_SPEC)` (cached at module level as a lazy constant), and `_table_produced_this_request = False`.
2. In turn 1 (line 4785): if `_tabular`, pass `tool_choice="required"` + `tools_override=_whitelist_spec`. Otherwise unchanged.
3. Inside `_dispatch_tool_calls`, whenever a tool result sets a non-None `last_table`, flip `_table_produced_this_request = True`. Follow-up queries can legitimately inherit a `last_table` from `tool_ctx`, so the condition for retry must be "this request produced a table", not "last_table is non-None".
4. After the loop body ends (after line ~4899, before the return): if `_tabular and text_out and not _table_produced_this_request`, run one retry as described in §Data flow.

### Refactor: extract `_dispatch_tool_calls` helper

Lines 4807–4876 (the block that iterates `assistant_msg.tool_calls`, runs dedup/DM guards, dispatches, truncates results, updates state) gets pulled into a private helper:

```python
def _dispatch_tool_calls(
    assistant_msg,
    msgs: list,
    tool_ctx,
    seen_hashes: set,
    dm_called: bool,
    tool_calls_made: list,
    turn: int,
) -> tuple[bool, dict|None, dict|None, dict|None]:
    """Returns (dm_called_now, last_table, last_viz, last_filters)."""
    ...
```

Reused by both the main loop and the retry block. Zero behaviour change — pure extraction.

## Data flow

```
user_msg arrives → chat_api()
       │
       ▼
   _try_planner() ── returns → response (bypass, unchanged path)
       │
       ▼ (no planner match)
   _tabular = has_tabular_intent(user_msg)
       │
       ▼
   _agentic_loop()
       │
   turn 1: if _tabular: tool_choice="required" + tools_override=WHITELIST
           else:        tool_choice="auto"  + full TOOLS_SPEC
       │
       ▼
   Claude response:
       ├─ has tool_calls → _dispatch_tool_calls → truncate → next turn or fast-exit
       └─ text only      → exit loop
       │
       ▼ (loop ended)
   if _tabular and text_out and not _table_produced_this_request:
       append user hint "previous answer lacked a table..."
       retry_resp = _claude_chat(tool_choice="required",
                                 tools_override=WHITELIST,
                                 use_thinking=False)
       if retry_resp has tool_calls:
           _dispatch_tool_calls(retry_resp, ...)   # flips _table_produced_this_request if a table lands
           if _table_produced_this_request:
               text_out = retry_resp.content or text_out
       # cap: 1 retry max
       │
       ▼
   return response
```

## Error handling

| Case | Handling |
|---|---|
| `APITimeoutError` / `APIConnectionError` / `RateLimitError` in retry | Caught by the same `except` block the main loop uses. `text_out` preserved. `_dbg("Retry failed: ...")` logged |
| Retry produces no tool calls (Claude again answers text-only) | Accept. `text_out` preserved. No second retry (cap=1) |
| Retry's tool call is dedup'd against `seen_hashes` from the main loop | `_dispatch_tool_calls` skips it. Retry effectively no-ops. `text_out` preserved |
| Retry's tool call is a DM tool but `dm_called=True` already | DM guard blocks. Retry no-ops. Accepted trade-off (cost protection wins) |
| `has_tabular_intent` raises (malformed input, encoding) | Wrap the call in try/except at the call site. On error, default to `False` (fall back to current behaviour) |
| Tool in this request returns `last_table={"rows":[], "columns":[...]}` | `_table_produced_this_request=True` → no retry. A "0 results" is a legitimate answer |
| `last_table` inherited from `tool_ctx` (follow-up query) but this turn produced no tool call | `_table_produced_this_request=False` → retry fires if `_tabular`. Correct: the user asked for a NEW table |
| `last_table` inherited from `tool_ctx`, Claude answers text-only referencing the inherited table ("how many rows?") | `has_tabular_intent` on "how many rows?" → `True` (matches "how many"). `_table_produced_this_request=False` → retry would fire. Mitigation: the math handler in `_try_planner` catches these before the loop runs. If it doesn't, the retry fires an `explorer_search` with empty filters and produces a fresh table — acceptable, slightly wasteful |

## Testing

### Unit: `backend/tests/test_tool_policy.py` (NEW, ~120 lines)

Pure function tests, no network, no mocks.

| Test | Covers |
|---|---|
| `test_has_tabular_intent_positives_en` | SC06/SS01/SS03/SC01/SC09/SS04 real queries + 6 synthetic → `True` |
| `test_has_tabular_intent_positives_es` | 8 ES queries → `True` |
| `test_has_tabular_intent_blacklist_wins` | "Explain what HLA means", "Show me what that error means", "Puedes explicar cómo" → `False` |
| `test_has_tabular_intent_conversational` | "hi", "thanks", "ok", "what?" → `False` |
| `test_has_tabular_intent_accents_normalized` | "cuántos" / "cuantos" / "CUÁNTOS" → `True` |
| `test_has_tabular_intent_cl05_edge_case` | "Are there any CTS sites in Romania?" → `False` (CL05 not covered, documented) |
| `test_filter_tools_spec_whitelist` | Synthetic spec of 5 tools, whitelist of 2 → returns only those 2 |
| `test_filter_tools_spec_empty_whitelist` | Empty whitelist → empty list |
| `test_filter_tools_spec_nonexistent_tool` | Whitelist includes tool not in spec → silently ignored |
| `test_table_returning_tools_constant` | All 4 entries in `TABLE_RETURNING_TOOLS` exist as function names in real `TOOLS_SPEC` |

### Unit: extension of `backend/tests/test_agentic_loop.py` (~80 lines added)

Reuses existing `mock_dispatch`, `mock_claude`, `tool_ctx` fixtures.

| Test | Covers |
|---|---|
| `test_loop_forces_tool_choice_when_tabular_intent` | Turn 1 invoked with `tool_choice="required"` + whitelist |
| `test_loop_uses_auto_when_conversational` | No regression on existing behaviour |
| `test_loop_retry_on_text_only_with_tabular_intent` | Retry fires, produces tool call, fills `last_table` |
| `test_loop_retry_noop_without_tabular_intent` | No retry when intent is conversational |
| `test_loop_retry_noop_if_table_produced_this_request` | No retry when a tool call in this request populated `last_table` |
| `test_loop_retry_fires_when_inherited_table_but_no_new_tool` | `tool_ctx.last_table` pre-populated (follow-up), turn 1 returns text-only with `_tabular=True`, retry fires |
| `test_loop_retry_respects_dm_guard` | DM guard blocks the retry's DM tool, no crash |
| `test_loop_retry_max_one` | Second text-only response does NOT trigger a second retry |
| `test_loop_progress_event_retry` | `__PROGRESS__` SSE event emitted for the retry turn |

### Regression gate (merge requirement, manual)

Run `python scripts/rerun_regressions.py` three consecutive times with a fresh SF cookie. **Merge requirement:** SC06, SS01, and SS03 must be PASS in all 3 runs. The other 7 queries (SC01/SC09/CL05/SC10/SS04/FA01/RE01) are out of scope — they can remain in their current state (FAIL is acceptable if it was already FAIL before this change).

Cost: ~3 runs × 10 queries × ~$0.15/query ≈ **$4.50**.

### Post-merge smoke (not a gate)

`python scripts/moby_bulk_eval.py` over the full 121-question suite. Only run if Anthropic budget allows. Confirms no regression against `docs/moby-bulk-eval-initial.yaml`. Cost: ~$15–20.

## Observability

Three `_dbg()` lines added at the key decision points (same format as the rest of `ai_chat.py`):

```python
_dbg("Tabular intent detected (query=%r), forcing tool_choice=required", user_msg[:80])
_dbg("Turn 1 used whitelist: %d tools", len(tools_override))
_dbg("Retry triggered: loop exited text-only with tabular intent, re-calling with whitelist")
```

No new CloudWatch metrics. If post-merge we see the retry firing more than ~5% of queries, follow-up adds a counter.

## Rollout

- Single commit on `refactor/moby-agentic-loop`.
- Commit message: `fix(moby): force tool_choice + retry for tabular-intent queries`.
- No feature flag. New behaviour only activates when `has_tabular_intent==True`; all other queries are untouched.
- Rollback = `git revert` of the commit.
- The merge to `main` closes the refactor branch blocker identified in `project_state_2026_04_21_ship.md`.

## Cost impact

| Scenario | Extra cost per query |
|---|---|
| Conversational query (no tabular intent) | $0 — unchanged |
| Tabular query that succeeded in turn 1 | ≈ $0.006 — whitelist breaks tools-prompt cache once |
| Tabular query that needed a retry | ≈ $0.02 — one extra Claude call with the smaller tool set |

Amortised over the 121-question eval assuming ~3% retry rate: < $0.05 / eval run. Insignificant.

## Open questions

None — all scope and edge cases have been enumerated above.

## Links

- Memory `project_state_2026_04_22_triage_r1.md` — open brainstorm that produced this plan.
- Memory `project_state_2026_04_21_ship.md` — refactor branch context, 10 regression list.
- `docs/moby-bulk-eval-20260421-delta.md` — regression evidence.
- `docs/moby-regression-rerun.json` — last rerun (2026-04-22, 5/10 PASS due to flakiness).
- `scripts/rerun_regressions.py` — the 10-question merge gate.
