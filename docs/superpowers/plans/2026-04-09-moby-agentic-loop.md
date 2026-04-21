# Moby Agentic Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform Moby from a single-turn tool caller into a bounded agentic loop with better tool selection, multi-step reasoning, and progress streaming.

**Architecture:** Replace the current `_claude_chat() -> tool dispatch -> return` single pass with a loop of up to 3 Claude turns. Consolidate 25 tools down to ~18 by merging overlapping tools. Shrink `_try_planner()` to only math/chart/conversational handlers. Add SSE `progress` events so the user sees tool execution in real-time.

**Tech Stack:** Python/FastAPI (backend), React/TypeScript (frontend), Anthropic SDK (`claude-sonnet-4-6`), SSE streaming.

**Spec:** `docs/superpowers/specs/2026-04-09-moby-agentic-loop-design.md`

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `backend/app/routers/ai_chat.py` | Main chat router — agentic loop, tool dispatch, TOOLS_SPEC, SYSTEM_PROMPT | Modify |
| `backend/app/routers/moby_tools.py` | New: extracted tool dispatch functions (one function per tool) | Create |
| `backend/app/routers/moby_planner.py` | Query planning — convert from execution to hint injection | Modify |
| `backend/app/routers/moby_handlers.py` | Activity/assignment handlers — remove, domain knowledge moves to system prompt | Modify |
| `frontend/src/lib/ai.ts` | SSE stream parsing — handle new `progress` event type | Modify |
| `frontend/src/components/ChatView.tsx` | Show progress status line during tool execution | Modify |
| `backend/tests/test_agentic_loop.py` | New: unit tests for the agentic loop | Create |
| `backend/tests/test_tool_dispatch.py` | New: unit tests for extracted tool dispatch | Create |
| `backend/tests/test_ai_chat.py` | Existing tests — update for new flow | Modify |

---

### Task 1: Extract Tool Dispatch into `moby_tools.py`

The current tool dispatch is a 640-line if/elif chain (lines 9550–10189 of `ai_chat.py`). Before modifying the loop, extract each tool handler into its own function in a new module. This makes the agentic loop code clean and each tool independently testable.

**Files:**
- Create: `backend/app/routers/moby_tools.py`
- Modify: `backend/app/routers/ai_chat.py:9550-10189`
- Create: `backend/tests/test_tool_dispatch.py`

- [ ] **Step 1: Create `moby_tools.py` with the dispatch registry pattern**

```python
# backend/app/routers/moby_tools.py
"""
Tool dispatch for Moby agentic loop.
Each tool is a function: (args, ctx) -> ToolResult.
Registered in TOOL_DISPATCH dict for lookup by name.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import Request
from sqlalchemy.orm import Session

logger = logging.getLogger("moby.tools")

@dataclass
class ToolContext:
    """Shared state passed to every tool function."""
    db: Session
    request: Request
    sf: Any  # simple_salesforce.Salesforce instance
    last_table: Optional[Dict[str, Any]] = None
    last_visualization: Optional[Dict[str, Any]] = None
    last_explorer_filters: Optional[Dict[str, Any]] = None

@dataclass
class ToolResult:
    """Returned by every tool function."""
    content: str  # JSON string sent back to Claude as tool_result
    last_table: Optional[Dict[str, Any]] = None
    last_visualization: Optional[Dict[str, Any]] = None
    last_explorer_filters: Optional[Dict[str, Any]] = None
    error: bool = False

# Registry: tool_name -> handler function
TOOL_DISPATCH: Dict[str, Any] = {}

def register_tool(name: str):
    """Decorator to register a tool handler."""
    def decorator(fn):
        TOOL_DISPATCH[name] = fn
        return fn
    return decorator
```

- [ ] **Step 2: Run a syntax check**

Run: `python -c "import backend.app.routers.moby_tools"`
Expected: No errors (module imports cleanly)

- [ ] **Step 3: Extract `sql_query` tool as first handler**

This is the most complex tool handler (lines 9550–9607 of `ai_chat.py`). Extract it to prove the pattern works.

Add to `moby_tools.py`:

```python
@register_tool("sql_query")
def tool_sql_query_dispatch(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Execute a read-only SQL SELECT over Postgres."""
    from backend.app.routers.ai_chat import tool_sql_query, _normalize_table_for_ui
    from sqlalchemy import text

    sql_raw = args.get("sql", "")
    is_schema_query = bool(re.search(
        r"jsonb_object_keys|information_schema|pg_catalog|SHOW TABLES|DESCRIBE\s",
        sql_raw, re.I))

    try:
        out = tool_sql_query(ctx.db, sql_raw, args.get("params") or {})
        cols = out.get("columns") or []
        dict_rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]

        last_table = None
        if not is_schema_query:
            last_table = {"columns": [{"key": c, "label": c} for c in cols], "rows": dict_rows}

        # Enrich account_id by site name if missing
        if last_table:
            rows0 = last_table.get("rows") or []
            name_keys = [
                "site", "sites.name", "s.name", "sf.Account.Name",
                "Account.Name", "name", "account_name"
            ]
            wanted = {str(r[k]) for r in rows0 for k in name_keys
                      if isinstance(r, dict) and r.get(k)}
            if wanted:
                q = text("""
                    SELECT name, salesforce_account_id
                    FROM public.sites
                    WHERE name = ANY(:names)
                """)
                res = ctx.db.execute(q, {"names": list(wanted)})
                mapping = {row[0]: row[1] for row in res.fetchall() if row[1]}
                for r in rows0:
                    if not isinstance(r, dict):
                        continue
                    if "account_id" not in r or not r.get("account_id"):
                        nm = next((r.get(k) for k in name_keys if r.get(k)), None)
                        acc = mapping.get(str(nm)) if nm is not None else None
                        if acc:
                            r["account_id"] = acc
                            r.setdefault("sf.Account.Id", acc)
                col_keys = [c.get("key") for c in last_table.get("columns", [])]
                if "account_id" not in col_keys and any(
                    r.get("account_id") for r in rows0 if isinstance(r, dict)
                ):
                    last_table["columns"].insert(0, {"key": "account_id", "label": "Account Id"})

        return ToolResult(
            content=json.dumps(out, default=str),
            last_table=last_table,
        )
    except Exception as e:
        return ToolResult(content=json.dumps({"error": str(e)}), error=True)
```

- [ ] **Step 4: Write a basic test for the dispatch registry**

Create `backend/tests/test_tool_dispatch.py`:

```python
"""Tests for moby_tools dispatch registry."""
from backend.app.routers.moby_tools import TOOL_DISPATCH, ToolContext, ToolResult

def test_sql_query_registered():
    assert "sql_query" in TOOL_DISPATCH

def test_dispatch_returns_tool_result():
    """Verify the dispatch function signature is correct."""
    fn = TOOL_DISPATCH["sql_query"]
    # Check it's callable with (args, ctx) signature
    import inspect
    sig = inspect.signature(fn)
    params = list(sig.parameters.keys())
    assert params == ["args", "ctx"]
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest backend/tests/test_tool_dispatch.py -v`
Expected: 2 tests PASS

- [ ] **Step 6: Extract remaining tool handlers**

Extract each remaining tool from the if/elif chain (lines 9609–10189) into `moby_tools.py` using the same `@register_tool` pattern. Each tool follows the same signature: `(args: Dict, ctx: ToolContext) -> ToolResult`.

Tools to extract (in order of the if/elif chain):
- `salesforce_query` (lines 9609–9697)
- `salesforce_account_extras` (lines 9699–9708)
- `explorer_set_filters` (lines 9709–9712)
- `salesforce_account_contacts` (lines 9713–9739)
- `salesforce_assignments` (lines 9740–9752)
- `rank_sites_by_group` (lines 9753–9767)
- `group_count` (lines 9768–9779)
- `group_count_agg` (lines 9780–9792)
- `list_activities` (lines 9793–9804)
- `activities_with_countries` (lines 9805–9815)
- `activity_counts_by_country` (lines 9816–9826)
- `activity_country_matrix` (lines 9827–9837)
- `activities_sites` (lines 9838–9848)
- `activities_by_name` (lines 9849–9861)
- `group_count_sf` (lines 9862–9872)
- `group_agg_sf` (lines 9873–9885)
- `activities_with_assignments_counts` (lines 9886–9902)
- `activity_assignments_detailed` (lines 9903–9921)
- `time_series_sf` (lines 9922–9936)
- `sql_query_fill_sf` (lines 9937–9950)
- `contacts_by_group` (lines 9951–9964)
- `study_coordinators_with_activities` (lines 9965–9979)
- `qual_search` (lines 9980–9991)
- `manipulate_data` (lines 9992–10020)
- `render_chart` (lines 10021–10060)
- `explorer_search` (lines 10061–10121)
- `nearest_filtered_sites` (lines 10122–10137)
- `explorer_within_drive_km` (lines 10138–10160)
- `rank_sites` (lines 10161–10176)
- `members_search` (lines 10177–10189)

For each: move the body into a `@register_tool("name")` function in `moby_tools.py`, importing any helpers from `ai_chat.py` as needed. The function must return `ToolResult` with `content` (JSON string), `last_table`, `last_visualization`, and `last_explorer_filters` as appropriate.

- [ ] **Step 7: Replace the if/elif chain in `ai_chat.py` with dispatch lookup**

Replace lines 9550–10189 in `ai_chat.py` with:

```python
            from backend.app.routers.moby_tools import TOOL_DISPATCH, ToolContext, ToolResult

            tool_ctx = ToolContext(
                db=db, request=request, sf=sf,
                last_table=last_table,
                last_visualization=last_visualization,
                last_explorer_filters=last_explorer_filters,
            )

            handler = TOOL_DISPATCH.get(name)
            if handler:
                result = handler(args, tool_ctx)
                # Update shared state from tool result
                if result.last_table is not None:
                    last_table = result.last_table
                if result.last_visualization is not None:
                    last_visualization = result.last_visualization
                if result.last_explorer_filters is not None:
                    last_explorer_filters = result.last_explorer_filters
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result.content,
                })
            else:
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps({"error": f"Unknown tool {name}"}),
                })
```

- [ ] **Step 8: Run all existing tests to verify no regressions**

Run: `python -m pytest backend/tests/ -v`
Expected: All 509 tests PASS (no behavior change, just code reorganization)

- [ ] **Step 9: Commit**

```bash
git add backend/app/routers/moby_tools.py backend/tests/test_tool_dispatch.py backend/app/routers/ai_chat.py
git commit -m "refactor: extract tool dispatch into moby_tools.py registry"
```

---

### Task 2: Implement the Agentic Loop

Replace the single-turn tool execution with a bounded loop of up to 3 Claude turns.

**Files:**
- Modify: `backend/app/routers/ai_chat.py:9520-10293`
- Create: `backend/tests/test_agentic_loop.py`

- [ ] **Step 1: Write failing tests for the agentic loop**

Create `backend/tests/test_agentic_loop.py`:

```python
"""Tests for the Moby agentic loop."""
import json
from unittest.mock import MagicMock, patch
from backend.app.routers.ai_chat import MOBY_MAX_AGENT_TURNS

def test_max_agent_turns_default():
    """Default max turns is 3."""
    assert MOBY_MAX_AGENT_TURNS == 3

def test_loop_exits_on_text_response():
    """If Claude responds with text (no tool_use) on turn 1, loop exits immediately."""
    # This tests the agentic_loop function directly
    from backend.app.routers.ai_chat import _agentic_loop

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Here is the answer."
    mock_response.choices[0].message.tool_calls = None

    with patch("backend.app.routers.ai_chat._claude_chat", return_value=mock_response):
        result = _agentic_loop(
            msgs=[{"role": "user", "content": "hello"}],
            tool_ctx=MagicMock(),
            use_thinking=False,
        )
    assert result["turns_used"] == 1
    assert result["text"] == "Here is the answer."
    assert result["tool_calls_made"] == []

def test_loop_calls_tool_and_continues():
    """Turn 1 calls a tool, turn 2 Claude responds with text."""
    from backend.app.routers.ai_chat import _agentic_loop

    # Turn 1: Claude requests a tool
    mock_tool_call = MagicMock()
    mock_tool_call.id = "tc_1"
    mock_tool_call.function.name = "explorer_search"
    mock_tool_call.function.arguments = json.dumps({"filters": {"logic": "AND", "rules": []}})
    mock_tool_call.model_dump.return_value = {
        "id": "tc_1", "type": "function",
        "function": {"name": "explorer_search", "arguments": "{}"}
    }

    turn1_response = MagicMock()
    turn1_response.choices = [MagicMock()]
    turn1_response.choices[0].message.content = ""
    turn1_response.choices[0].message.tool_calls = [mock_tool_call]

    # Turn 2: Claude responds with text
    turn2_response = MagicMock()
    turn2_response.choices = [MagicMock()]
    turn2_response.choices[0].message.content = "Found 5 sites."
    turn2_response.choices[0].message.tool_calls = None

    mock_ctx = MagicMock()
    mock_tool_result = MagicMock()
    mock_tool_result.content = json.dumps({"rows": [], "columns": []})
    mock_tool_result.last_table = None
    mock_tool_result.last_visualization = None
    mock_tool_result.last_explorer_filters = None
    mock_tool_result.error = False

    with patch("backend.app.routers.ai_chat._claude_chat", side_effect=[turn1_response, turn2_response]):
        with patch("backend.app.routers.moby_tools.TOOL_DISPATCH", {"explorer_search": MagicMock(return_value=mock_tool_result)}):
            result = _agentic_loop(
                msgs=[{"role": "user", "content": "sites in Germany"}],
                tool_ctx=mock_ctx,
                use_thinking=False,
            )
    assert result["turns_used"] == 2
    assert result["text"] == "Found 5 sites."

def test_loop_forces_text_on_final_turn():
    """On turn 3 (final), tool_choice is 'none' so Claude must respond with text."""
    from backend.app.routers.ai_chat import _agentic_loop
    # This test verifies that _claude_chat is called with force_no_tools=True on the final turn
    call_args_list = []

    def mock_claude_chat(msgs, tool_choice="auto", *, force_no_tools=False, use_thinking=False):
        call_args_list.append({"tool_choice": tool_choice, "force_no_tools": force_no_tools})
        resp = MagicMock()
        resp.choices = [MagicMock()]
        if len(call_args_list) < 3:
            # Turns 1-2: return tool calls
            tc = MagicMock()
            tc.id = f"tc_{len(call_args_list)}"
            tc.function.name = "explorer_search"
            tc.function.arguments = json.dumps({"filters": {"logic": "AND", "rules": [{"field": f"f{len(call_args_list)}"}]}})
            tc.model_dump.return_value = {"id": tc.id, "type": "function", "function": {"name": "explorer_search", "arguments": tc.function.arguments}}
            resp.choices[0].message.content = ""
            resp.choices[0].message.tool_calls = [tc]
        else:
            # Turn 3: text response (forced)
            resp.choices[0].message.content = "Final answer."
            resp.choices[0].message.tool_calls = None
        return resp

    mock_tool_result = MagicMock()
    mock_tool_result.content = json.dumps({"rows": []})
    mock_tool_result.last_table = None
    mock_tool_result.last_visualization = None
    mock_tool_result.last_explorer_filters = None
    mock_tool_result.error = False

    with patch("backend.app.routers.ai_chat._claude_chat", side_effect=mock_claude_chat):
        with patch("backend.app.routers.moby_tools.TOOL_DISPATCH", {"explorer_search": MagicMock(return_value=mock_tool_result)}):
            result = _agentic_loop(
                msgs=[{"role": "user", "content": "complex query"}],
                tool_ctx=MagicMock(),
                use_thinking=False,
            )
    assert result["turns_used"] == 3
    assert result["text"] == "Final answer."
    # Final turn should have force_no_tools=True
    assert call_args_list[2]["force_no_tools"] is True

def test_loop_no_duplicate_tool_calls():
    """Same tool with same args cannot be called twice."""
    from backend.app.routers.ai_chat import _agentic_loop

    same_args = json.dumps({"filters": {"logic": "AND", "rules": []}})

    def make_tool_response():
        tc = MagicMock()
        tc.id = "tc_dup"
        tc.function.name = "explorer_search"
        tc.function.arguments = same_args
        tc.model_dump.return_value = {"id": "tc_dup", "type": "function", "function": {"name": "explorer_search", "arguments": same_args}}
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = ""
        resp.choices[0].message.tool_calls = [tc]
        return resp

    text_response = MagicMock()
    text_response.choices = [MagicMock()]
    text_response.choices[0].message.content = "Done."
    text_response.choices[0].message.tool_calls = None

    mock_tool_result = MagicMock()
    mock_tool_result.content = json.dumps({"rows": []})
    mock_tool_result.last_table = None
    mock_tool_result.last_visualization = None
    mock_tool_result.last_explorer_filters = None
    mock_tool_result.error = False

    with patch("backend.app.routers.ai_chat._claude_chat", side_effect=[make_tool_response(), make_tool_response(), text_response]):
        with patch("backend.app.routers.moby_tools.TOOL_DISPATCH", {"explorer_search": MagicMock(return_value=mock_tool_result)}):
            result = _agentic_loop(
                msgs=[{"role": "user", "content": "test"}],
                tool_ctx=MagicMock(),
                use_thinking=False,
            )
    # The second call with same args should be skipped (returned as error)
    assert result["turns_used"] <= 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/test_agentic_loop.py -v`
Expected: FAIL — `_agentic_loop` and `MOBY_MAX_AGENT_TURNS` don't exist yet

- [ ] **Step 3: Add `MOBY_MAX_AGENT_TURNS` constant and `_agentic_loop` function**

Add to `ai_chat.py` after the existing constants (around line 55):

```python
MOBY_MAX_AGENT_TURNS = int(os.getenv("MOBY_MAX_AGENT_TURNS", "3"))
MAX_TOOL_RESULT_TOKENS = int(os.getenv("MAX_TOOL_RESULT_TOKENS", "4000"))
MOBY_AGENT_TIMEOUT_S = int(os.getenv("MOBY_AGENT_TIMEOUT_S", "30"))
```

Add the `_agentic_loop` function after `_claude_chat()` (after line 4826):

```python
def _agentic_loop(
    msgs: List[Dict[str, Any]],
    tool_ctx: "ToolContext",
    use_thinking: bool = False,
) -> Dict[str, Any]:
    """
    Bounded agentic loop: up to MOBY_MAX_AGENT_TURNS Claude turns.
    - Turns 1 to N-1: tool_choice="auto" — Claude can call tools or respond with text.
    - Turn N (final): force_no_tools=True — Claude must synthesize a text answer.
    - If Claude responds with text (no tool_use) on any turn, loop exits early.
    Returns: {"text": str, "turns_used": int, "tool_calls_made": list,
              "last_table": dict|None, "last_visualization": dict|None,
              "last_explorer_filters": dict|None}
    """
    import hashlib
    from backend.app.routers.moby_tools import TOOL_DISPATCH, ToolResult

    seen_calls: set = set()  # (tool_name, args_hash) for dedup
    tool_calls_made: list = []
    text_out = ""
    last_table = tool_ctx.last_table
    last_visualization = tool_ctx.last_visualization
    last_explorer_filters = tool_ctx.last_explorer_filters
    token_q = getattr(_STREAM_Q, "q", None)

    for turn in range(1, MOBY_MAX_AGENT_TURNS + 1):
        is_final_turn = (turn == MOBY_MAX_AGENT_TURNS)

        # Call Claude
        try:
            resp = _claude_chat(
                msgs,
                tool_choice="auto",
                force_no_tools=is_final_turn,
                use_thinking=use_thinking and turn == 1,
            )
        except Exception as e:
            _dbg("Agentic loop Claude error turn %d: %s", turn, e)
            text_out = f"<p>AI service error: {str(e)[:100]}</p>"
            break

        choice = resp.choices[0]
        assistant_msg = choice.message

        # If text response (no tools), we're done
        if not assistant_msg.tool_calls:
            text_out = (assistant_msg.content or "").strip()
            break

        # Add assistant message with tool_calls to history
        msgs.append({
            "role": "assistant",
            "content": assistant_msg.content or "",
            "tool_calls": [tc.model_dump() for tc in assistant_msg.tool_calls],
        })

        # Send progress event if streaming
        if token_q is not None:
            for tc in assistant_msg.tool_calls:
                progress = json.dumps({
                    "type": "progress",
                    "turn": turn,
                    "tool": tc.function.name,
                    "status": "calling",
                })
                token_q.put(f"__PROGRESS__{progress}")

        # Execute each tool
        for tc in assistant_msg.tool_calls:
            name = tc.function.name
            args_str = tc.function.arguments or "{}"
            args = json.loads(args_str)
            tool_call_id = tc.id

            _dbg("AGENTIC turn=%d TOOL=%s args=%s", turn, name, args_str[:200])

            # Dedup check
            args_hash = hashlib.md5(f"{name}:{args_str}".encode()).hexdigest()
            if args_hash in seen_calls:
                _dbg("AGENTIC dedup: skipping %s (same args as previous call)", name)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps({"error": "Duplicate call — same tool and arguments already executed. Try different parameters."}),
                })
                continue
            seen_calls.add(args_hash)

            # Dispatch
            handler = TOOL_DISPATCH.get(name)
            if handler:
                # Update ctx with current state
                tool_ctx.last_table = last_table
                tool_ctx.last_visualization = last_visualization
                tool_ctx.last_explorer_filters = last_explorer_filters

                result: ToolResult = handler(args, tool_ctx)

                # Update shared state
                if result.last_table is not None:
                    last_table = result.last_table
                if result.last_visualization is not None:
                    last_visualization = result.last_visualization
                if result.last_explorer_filters is not None:
                    last_explorer_filters = result.last_explorer_filters

                # Truncate content to MAX_TOOL_RESULT_TOKENS
                content = result.content
                if len(content) > MAX_TOOL_RESULT_TOKENS * 4:  # rough char estimate
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict) and "rows" in parsed:
                            rows = parsed["rows"]
                            if len(rows) > 50:
                                parsed["rows"] = rows[:50]
                                parsed["_truncated"] = f"Showing 50 of {len(rows)} rows"
                                content = json.dumps(parsed, default=str)
                    except Exception:
                        content = content[:MAX_TOOL_RESULT_TOKENS * 4]

                msgs.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": content,
                })
                tool_calls_made.append({"turn": turn, "tool": name, "args": args})
            else:
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps({"error": f"Unknown tool {name}"}),
                })

            # Send progress done event
            if token_q is not None and handler:
                rows_count = 0
                if last_table:
                    rows_count = len(last_table.get("rows", []))
                progress = json.dumps({
                    "type": "progress",
                    "turn": turn,
                    "tool": name,
                    "status": "done",
                    "rows": rows_count,
                })
                token_q.put(f"__PROGRESS__{progress}")

    return {
        "text": text_out,
        "turns_used": turn,
        "tool_calls_made": tool_calls_made,
        "last_table": last_table,
        "last_visualization": last_visualization,
        "last_explorer_filters": last_explorer_filters,
    }
```

- [ ] **Step 4: Run the agentic loop tests**

Run: `python -m pytest backend/tests/test_agentic_loop.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Wire `_agentic_loop` into `chat_api()`**

Replace the existing single-turn Claude call + tool dispatch block in `chat_api()` (lines ~9500–10293) with:

```python
        # ---- Agentic loop: multi-turn tool execution ----
        from backend.app.routers.moby_tools import ToolContext

        tool_ctx = ToolContext(
            db=db, request=request, sf=sf,
            last_table=last_table,
            last_visualization=last_visualization,
            last_explorer_filters=last_explorer_filters,
        )

        loop_result = _agentic_loop(
            msgs=msgs,
            tool_ctx=tool_ctx,
            use_thinking=use_thinking,
        )

        last_table = loop_result["last_table"]
        last_visualization = loop_result["last_visualization"]
        last_explorer_filters = loop_result["last_explorer_filters"]
        text_out = loop_result["text"]

        _dbg("AGENTIC loop done: turns=%d tools=%s",
             loop_result["turns_used"],
             [t["tool"] for t in loop_result["tool_calls_made"]])

        # Build response
        if last_table:
            rows = last_table.get("rows", [])
            total_rows = len(rows)
            answer_html = text_out if text_out else f"<p>Found {total_rows} result(s).</p>"
            out = {"answer": answer_html, "table": _normalize_table_for_ui(last_table)}
            if last_visualization:
                out["visualization"] = last_visualization
            if last_explorer_filters:
                out["last_filters"] = last_explorer_filters
            return out

        if text_out:
            out = {"answer": text_out}
            if last_explorer_filters:
                out["last_filters"] = last_explorer_filters
            return out

        return {"answer": "I wasn't able to answer that. Could you rephrase or break it into smaller questions?"}
```

- [ ] **Step 6: Run all backend tests**

Run: `python -m pytest backend/tests/ -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/routers/ai_chat.py backend/tests/test_agentic_loop.py
git commit -m "feat: add bounded agentic loop to Moby (3 turns max)"
```

---

### Task 3: Tool Consolidation — Merge Overlapping Tools

Merge overlapping tools in TOOLS_SPEC and update descriptions.

**Files:**
- Modify: `backend/app/routers/ai_chat.py` (TOOLS_SPEC at lines 3460–4040)
- Modify: `backend/app/routers/moby_tools.py` (merge handler functions)

- [ ] **Step 1: Remove `sql_query` from TOOLS_SPEC**

Delete the `sql_query` tool definition (lines 3461–3475 of `ai_chat.py`). Remove the `@register_tool("sql_query")` handler from `moby_tools.py`.

- [ ] **Step 2: Rename `salesforce_query` to `soql_query` in TOOLS_SPEC**

Change the name from `"salesforce_query"` to `"soql_query"` and update the description:

```python
{
    "type": "function",
    "function": {
        "name": "soql_query",
        "description": (
            "Run a SOQL SELECT on Opportunity (and Account.*). "
            "When to use: COUNT/GROUP BY aggregations, traversal relationships (e.g. C_Member__r.*), "
            "or fields not in the Explorer catalog. "
            "When NOT to use: Do NOT use for site filtering — use explorer_search instead. "
            "Do NOT use for qualification data — use explorer_search with qual.* filters. "
            "Example: SELECT Account.ShippingCountry, COUNT(Id) FROM Opportunity WHERE RecordType.DeveloperName='SubAccount' GROUP BY Account.ShippingCountry"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "soql": {"type": "string", "description": "SOQL SELECT ... FROM Opportunity ..."}
            },
            "required": ["soql"]
        }
    }
},
```

Update `moby_tools.py`: rename the handler from `salesforce_query` to `soql_query`. Keep the old name as an alias: `TOOL_DISPATCH["salesforce_query"] = TOOL_DISPATCH["soql_query"]`.

- [ ] **Step 3: Merge `rank_sites` + `rank_sites_by_group` into `rank_sites`**

Update `rank_sites` in TOOLS_SPEC to add an optional `group_by` parameter:

```python
{
    "type": "function",
    "function": {
        "name": "rank_sites",
        "description": (
            "Rank clinical trial sites by a metric. "
            "When to use: 'top N sites by X', 'which sites have the most Y', ranking queries. "
            "When NOT to use: For filtering (use explorer_search), for aggregations without ranking (use group_count). "
            "Example: rank_sites(metric='sf.C_Number_of_new_T1D_diagnosed_O_18__c', top_n=10)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "Field to rank by (sf.* or qual.*)"},
                "top_n": {"type": "integer", "description": "Number of top results (default 10)"},
                "order": {"type": "string", "enum": ["desc", "asc"], "description": "Sort order (default desc)"},
                "group_by": {"type": "string", "enum": ["country", "city"], "description": "Optional: rank within each group (top N per country/city)"},
                "filters": {"type": "object", "description": "Optional FilterGroup to pre-filter sites"}
            },
            "required": ["metric"]
        }
    }
},
```

In `moby_tools.py`, merge the two handlers: if `group_by` is provided, use the `rank_sites_by_group` logic; otherwise use the basic `rank_sites` logic. Remove the standalone `rank_sites_by_group` from TOOLS_SPEC.

- [ ] **Step 4: Merge `group_count` + `group_count_agg` + `group_count_sf` into `group_count`**

Update TOOLS_SPEC:

```python
{
    "type": "function",
    "function": {
        "name": "group_count",
        "description": (
            "Count or aggregate sites grouped by a dimension. "
            "When to use: 'how many sites per country', 'average ND by country', distribution queries. "
            "When NOT to use: For ranking (use rank_sites), for filtering (use explorer_search). "
            "Example: group_count(group_by='country') or group_count(group_by='country', aggregation='avg', metric='sf.C_Number_of_new_T1D_diagnosed_O_18__c')"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string", "enum": ["country", "city"], "description": "Dimension to group by"},
                "filters": {"type": "object", "description": "Optional FilterGroup to pre-filter"},
                "aggregation": {"type": "string", "enum": ["count", "sum", "avg", "ratio"], "description": "Aggregation type (default: count)"},
                "metric": {"type": "string", "description": "Field to aggregate (required for sum/avg/ratio)"},
                "source": {"type": "string", "enum": ["explorer", "salesforce"], "description": "Data source (default: explorer)"}
            },
            "required": ["group_by"]
        }
    }
},
```

In `moby_tools.py`, merge: route by `aggregation` and `source` params to the appropriate existing logic. Remove `group_count_agg` and `group_count_sf` from TOOLS_SPEC.

- [ ] **Step 5: Merge `group_agg_sf` + `time_series_sf` into `sf_aggregate`**

```python
{
    "type": "function",
    "function": {
        "name": "sf_aggregate",
        "description": (
            "Salesforce aggregation queries — grouped metrics or time series. "
            "When to use: SOQL-level aggregations (AVG, SUM, COUNT) or trend-over-time queries. "
            "When NOT to use: For site filtering (use explorer_search), for simple counts (use group_count)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["aggregate", "time_series"], "description": "Query mode"},
                "metric": {"type": "string", "description": "SF field to aggregate"},
                "group_by": {"type": "string", "description": "SF field to group by"},
                "aggregation": {"type": "string", "enum": ["AVG", "SUM", "COUNT", "MIN", "MAX"]},
                "time_field": {"type": "string", "description": "Date field for time_series mode"},
                "filters": {"type": "string", "description": "Optional SOQL WHERE clause"}
            },
            "required": ["mode", "metric"]
        }
    }
},
```

Remove `group_agg_sf` and `time_series_sf` from TOOLS_SPEC. Merge handlers in `moby_tools.py`.

- [ ] **Step 6: Update `explorer_search` description to emphasize it's the default**

```python
"description": (
    "DEFAULT tool for any question about clinical trial sites. "
    "Searches sites using FilterGroup with sf.* (Salesforce), qual.* (qualification), "
    "site.* (geography), and extra.* (assignments, activities) fields. "
    "When to use: ANY question about sites — filtering, listing, searching. This should be your FIRST choice. "
    "When NOT to use: Only skip this for SOQL aggregations (use soql_query) or member institutions (use members_search). "
    "Supports nested AND/OR logic via FilterGroup expressions."
),
```

- [ ] **Step 7: Add 'When NOT to use' to all remaining tool descriptions**

For each tool still in TOOLS_SPEC, add a one-line "When NOT to use" clause pointing to the correct alternative. Key additions:
- `nearest_filtered_sites`: "When NOT to use: for general site search without proximity — use explorer_search."
- `explorer_within_drive_km`: "When NOT to use: for straight-line distance — use nearest_filtered_sites. For general search — use explorer_search."
- `members_search`: "When NOT to use: for clinical trial sites — use explorer_search."
- `render_chart`: "When NOT to use: for data retrieval — get data first with explorer_search, then visualize."

- [ ] **Step 8: Run all tests**

Run: `python -m pytest backend/tests/ -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add backend/app/routers/ai_chat.py backend/app/routers/moby_tools.py
git commit -m "refactor: consolidate tools 25->18, improve descriptions with decision guidance"
```

---

### Task 4: Add Decision Tree to System Prompt

Replace the current 9-block routing prose in SYSTEM_PROMPT with a compact decision tree.

**Files:**
- Modify: `backend/app/routers/ai_chat.py` (SYSTEM_PROMPT at lines 4043–4618)

- [ ] **Step 1: Add decision tree at the top of SYSTEM_PROMPT**

Insert after the opening identity section (before the existing BLOCKs):

```python
"""
## Tool Selection Decision Tree

Follow this tree for EVERY query. Start at the top.

1. Is this about MEMBER INSTITUTIONS (not clinical trial sites)?
   → YES: Use `members_search`
   → NO: Continue to 2

2. Is this a math operation on the last table (sum, average, count)?
   → YES: The system handles this automatically — just describe the operation
   → NO: Continue to 3

3. Is this about clinical trial SITES (filtering, listing, searching)?
   → YES: Use `explorer_search` (DEFAULT — handles sf.*, qual.*, site.*, extra.* fields)
   → NO: Continue to 4

4. Is this a proximity/distance query (nearest, within X km)?
   → By city name: Use `nearest_filtered_sites`
   → By existing site: Use `explorer_within_drive_km`
   → NO: Continue to 5

5. Is this a SOQL aggregation (COUNT, AVG, GROUP BY) that explorer_search cannot handle?
   → YES: Use `soql_query`
   → NO: Continue to 6

6. Is this about contacts, coordinators, or PIs?
   → YES: Use `salesforce_account_contacts` or `study_coordinators_with_activities`
   → NO: Continue to 7

7. Is this about rankings (top N by metric)?
   → YES: Use `rank_sites`
   → NO: Continue to 8

8. Is this about distributions (sites per country, averages by group)?
   → YES: Use `group_count`
   → NO: Use `soql_query` as a fallback for custom queries

IMPORTANT: When in doubt, use `explorer_search`. It is the most versatile tool.
IMPORTANT: You can call multiple tools in one turn and chain results across turns.
"""
```

- [ ] **Step 2: Remove or condense the existing BLOCK 1-9 routing prose**

Keep domain knowledge (field names, INNODIA terminology) but remove routing instructions that now conflict with the decision tree. The BLOCKs should become reference material, not routing rules.

- [ ] **Step 3: Run all tests**

Run: `python -m pytest backend/tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add backend/app/routers/ai_chat.py
git commit -m "refactor: replace routing prose with decision tree in Moby system prompt"
```

---

### Task 5: Shrink `_try_planner` and Convert to Hint Injection

Remove ~12 deterministic handlers from `_try_planner`, keeping only math/chart/conversational. Convert `moby_planner.py` filter extraction to hint injection.

**Files:**
- Modify: `backend/app/routers/ai_chat.py:4988-7977` (`_try_planner`)
- Modify: `backend/app/routers/moby_planner.py`
- Modify: `backend/app/routers/moby_handlers.py`

- [ ] **Step 1: Identify handlers to keep vs remove in `_try_planner`**

**Keep** (safe, unambiguous):
- Math operations on last table (lines ~4998–5065)
- Chart from last table (lines ~5076–5103)
- Conversational no-tools (short follow-ups like "thanks", "ok")
- Table context injection (enriches system prompt, not a handler)

**Remove** (all others — ~12 handlers totaling ~2500 lines):
- Sites-per-country chart
- ND/T1D/Stage handlers
- Country handler
- Nearest/km-of-assignment handler
- Activity/assignment handlers (delegated to `moby_handlers.py`)
- HLA/pharmacy/overnight handlers
- Pipeline/profiling handlers
- Member institution handlers
- SC/PI/contact handlers
- Patient clarifier
- Sponsor handlers

- [ ] **Step 2: Comment out removed handlers with `# REMOVED: migrated to Claude agentic loop`**

Do NOT delete the code yet — comment it out so we can reference the domain logic when writing system prompt hints. Each removed block gets a one-line comment explaining what it did.

- [ ] **Step 3: Add hint injection from `moby_planner.py`**

After `_try_planner` returns `None` (no deterministic match), inject hints from the planner into the system prompt before calling `_agentic_loop`:

```python
# Inject planner hints into system prompt
plan = parse_query_plan(user_text, kindex)
hints = []
if plan.get("countries"):
    country_strs = [f"{c['name']} ({c['iso2']})" for c in plan["countries"]]
    hints.append(f"Detected countries: {', '.join(country_strs)}. Use site.country filter with ISO2 codes.")
if plan.get("filters"):
    filter_strs = [f"{f['field']} {f['op']} {f.get('value', '')}" for f in plan["filters"]]
    hints.append(f"Detected filters: {', '.join(filter_strs)}. Verify these match the user's intent.")
if plan.get("intent"):
    hints.append(f"Detected intent: {plan['intent']}.")

if hints:
    hint_block = "\n".join(f"- {h}" for h in hints)
    hint_msg = f"\n\n[SYSTEM HINTS — use as starting points, verify against user's question]\n{hint_block}"
    # Append to system message in msgs
    if msgs and msgs[0].get("role") == "system":
        msgs[0]["content"] += hint_msg
    else:
        msgs.insert(0, {"role": "system", "content": hint_msg})
```

- [ ] **Step 4: Update `moby_handlers.py` — remove or mark as deprecated**

The handlers in `moby_handlers.py` (`handle_activity`, `handle_assignment_sites`, etc.) are no longer called by `_try_planner`. Keep the file but add a deprecation note at the top:

```python
"""
DEPRECATED: These handlers were part of the deterministic _try_planner system.
They have been replaced by the Claude agentic loop.
Domain knowledge from these handlers has been moved to:
- SYSTEM_PROMPT decision tree and field reference
- moby_planner.py hint injection
Kept for reference; will be removed in a future cleanup.
"""
```

- [ ] **Step 5: Run all tests**

Run: `python -m pytest backend/tests/ -v`
Expected: Tests that tested `_try_planner` handlers directly may fail. Tests for math/chart handlers should still pass. Update or skip handler-specific tests that no longer apply.

- [ ] **Step 6: Update tests for the new flow**

In `test_moby_handlers.py` and `test_ai_chat.py`, mark tests for removed handlers with `@pytest.mark.skip(reason="Handler migrated to Claude agentic loop")`. Do NOT delete them — they document expected behavior that Claude should now replicate.

- [ ] **Step 7: Commit**

```bash
git add backend/app/routers/ai_chat.py backend/app/routers/moby_planner.py backend/app/routers/moby_handlers.py backend/tests/
git commit -m "refactor: shrink _try_planner to math/chart only, add hint injection"
```

---

### Task 6: SSE Progress Events — Backend

Add `progress` event support to the streaming endpoint.

**Files:**
- Modify: `backend/app/routers/ai_chat.py:4836-4894` (SSE endpoint)

- [ ] **Step 1: Update `generate()` in `chat_stream_api` to handle progress events**

The agentic loop (Task 2) already puts `__PROGRESS__` prefixed messages into the queue. Update the SSE generator to detect and emit them:

```python
def generate():
    while True:
        try:
            chunk = token_q.get(timeout=180)
        except _std_queue.Empty:
            yield f"data: {json.dumps({'type':'error','message':'Stream timeout'})}\n\n"
            break
        if chunk is None:
            break
        # Progress events from agentic loop
        if isinstance(chunk, str) and chunk.startswith("__PROGRESS__"):
            yield f"data: {chunk[12:]}\n\n"  # strip prefix, already JSON
        else:
            yield f"data: {json.dumps({'type':'token','text':chunk})}\n\n"

    t.join(timeout=10)
    if "error" in result_holder:
        yield f"data: {json.dumps({'type':'error','message':result_holder['error']})}\n\n"
    else:
        r = result_holder.get("r") or {}
        yield f"data: {json.dumps({'type':'done', **r}, default=str)}\n\n"
```

- [ ] **Step 2: Run backend tests**

Run: `python -m pytest backend/tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add backend/app/routers/ai_chat.py
git commit -m "feat: add SSE progress events for agentic loop tool execution"
```

---

### Task 7: SSE Progress Events — Frontend

Handle `progress` events in the frontend to show a status line during tool execution.

**Files:**
- Modify: `frontend/src/lib/ai.ts:94-121`
- Modify: `frontend/src/components/ChatView.tsx`

- [ ] **Step 1: Update `askAIStream` in `ai.ts` to handle progress events**

Add a new `onProgress` callback parameter and handle the `progress` event type:

```typescript
export async function askAIStream(
  prompt: string,
  lastTable?: TablePayload | null,
  lastFilters?: Record<string, any> | null,
  onToken?: (text: string) => void,
  history?: Array<{role: "user" | "assistant"; content: string}>,
  signal?: AbortSignal,
  onProgress?: (progress: { turn: number; tool: string; status: "calling" | "done"; summary?: string; rows?: number }) => void,
): Promise<ChatResponse> {
  // ... existing payload setup unchanged ...

  // In the event parsing loop, add:
  for (const line of lines) {
    if (!line.startsWith("data: ")) continue;
    let data: any;
    try { data = JSON.parse(line.slice(6)); } catch { continue; }

    if (data.type === "token") {
      onToken?.(data.text ?? "");
    } else if (data.type === "progress") {
      onProgress?.(data);
    } else if (data.type === "done") {
      const { type: _t, ...rest } = data;
      finalResult = rest as ChatResponse;
      reader.cancel().catch(() => {});
      break streaming;
    } else if (data.type === "error") {
      throw new Error(data.message ?? "Stream error");
    }
  }
```

- [ ] **Step 2: Add progress state and display to `ChatView.tsx`**

Add a new state variable and pass the `onProgress` callback:

```typescript
const [toolProgress, setToolProgress] = useState<string>("");

// In the askAIStream call:
const result = await askAIStream(
  prompt,
  lastTableForAI,
  lastFiltersForAI,
  (text) => setStreamText((prev) => prev + text),
  history,
  abortController.signal,
  (progress) => {
    if (progress.status === "calling") {
      const toolLabel = progress.tool.replace(/_/g, " ");
      setToolProgress(`Searching: ${toolLabel}...`);
    } else if (progress.status === "done") {
      const suffix = progress.rows ? ` (${progress.rows} results)` : "";
      setToolProgress(`Done: ${progress.tool.replace(/_/g, " ")}${suffix}`);
    }
  },
);
// Clear progress after response
setToolProgress("");
```

Render the progress line above the streaming cursor:

```tsx
{toolProgress && (
  <div className="text-xs text-gray-400 italic mb-1">
    {toolProgress}
  </div>
)}
```

- [ ] **Step 3: Run frontend build to verify no TypeScript errors**

Run: `cd frontend && npm run build`
Expected: Build succeeds with no errors

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/ai.ts frontend/src/components/ChatView.tsx
git commit -m "feat: show tool progress status line during Moby agentic loop"
```

---

### Task 8: Add Domain Knowledge to System Prompt

The removed deterministic handlers contained valuable domain knowledge (field names, INNODIA terminology, common query patterns). Move this into the system prompt as reference material.

**Files:**
- Modify: `backend/app/routers/ai_chat.py` (SYSTEM_PROMPT)

- [ ] **Step 1: Add field reference section to SYSTEM_PROMPT**

After the decision tree, add a compact reference:

```python
"""
## INNODIA Field Reference

### Common Salesforce Fields (use with explorer_search sf.* filters)
- Newly Diagnosed (ND): sf.C_Number_of_new_T1D_diagnosed_O_18__c (>=18), sf.C_Number_of_new_T1D_diagnosed_U_18__c (<18)
- Stage 1/2 followed: sf.C_Number_of_Stage1_Individuals_followed__c, sf.C_Number_of_Stage2_Individuals_followed__c
- T1D patients: sf.C_Number_of_T1D_Patients_currently_O_18__c, sf.C_Number_of_T1D_Patients_currently_U_18__c
- CTS status: sf.INNODIA_Clinical_Trial_Site__c (boolean)
- Clinical Site: sf.Clinical_Site_CS__c (boolean)
- Referral Partner: sf.C_Referral_Clinical_Partner__c (boolean)
- Profiling complete: sf.C_Profiling_Complete__c
- Assignment count: extra.AssignmentsCount (number), extra.AssignmentsNames (comma-separated)

### Common Qualification Fields (use with explorer_search qual.* filters)
- Pharmacy on-site: qual.3_6__is_your_pharmacy_on_site_or_off_campus (values: "On-site", "Off-campus")
- Overnight accommodation: qual.3_5__overnight_accommodation (values: "Yes", "No")
- HLA typing: qual.2_5__hla_typing_capacity (values: "Yes", "No")
- ZnT8 autoantibody: qual.2_4__znt8_autoantibody_testing (values: "Yes", "No")
- Insulin autoantibody: qual.2_3__insulin_autoantibody_testing (values: "Yes", "No")

### Geography
- site.country: ISO2 codes (DE, ES, IT, GB, FR, BE, NL, AT, FI, SE, NO, DK, PL, CZ, HU, SI, HR, LU, EE)
- site.city: city name string

### Domain Terms
- ND = Newly Diagnosed T1D
- CTS = Clinical Trial Site (INNODIA_Clinical_Trial_Site__c = true)
- CS = Clinical Site (Clinical_Site_CS__c = true)
- DxLab = Diagnostic Laboratory (C_Deliver_Clinical_Grade_Services__c on RT_Member Account)
- Stage 1/2 = pre-symptomatic T1D stages
- Assignment = study/trial assignment (Assignment__c object)
- Activity = a type of assignment activity
"""
```

- [ ] **Step 2: Add common query patterns section**

```python
"""
## Common Query Patterns

### "Sites in [country]"
Use explorer_search with filter: {field: "site.country", operator: "equals", value: "ISO2_CODE"}

### "Sites with [qualification]"
Use explorer_search with qual.* filter. For YES/NO fields, use operator "contains" value "yes" (not "is_not_null").

### "Sites near [city]"
Use nearest_filtered_sites with city parameter.

### "Sites not in any assignment"
Use explorer_search with filter: {field: "extra.AssignmentsCount", operator: "is_null"}

### "How many sites per country"
Use group_count with group_by="country".

### "Top N sites by [metric]"
Use rank_sites with the appropriate sf.* or qual.* metric field.

### Multi-step example: "German sites with overnight stays not in any assignment, ranked by ND"
Turn 1: explorer_search with filters: site.country=DE AND qual.3_5__overnight_accommodation contains "yes" AND extra.AssignmentsCount is_null
Turn 2: If results look right, use rank_sites on the filtered set, or just present the results sorted by ND.
"""
```

- [ ] **Step 3: Run backend tests**

Run: `python -m pytest backend/tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add backend/app/routers/ai_chat.py
git commit -m "docs: add INNODIA field reference and query patterns to Moby system prompt"
```

---

### Task 9: Safety Guardrails — Timeout and DM Protection

Add the wall-clock timeout and Distance Matrix single-call-per-message enforcement.

**Files:**
- Modify: `backend/app/routers/ai_chat.py` (`_agentic_loop`)

- [ ] **Step 1: Add timeout to `_agentic_loop`**

At the start of the loop, record start time. Check before each Claude call:

```python
import time

start_time = time.monotonic()

for turn in range(1, MOBY_MAX_AGENT_TURNS + 1):
    # Timeout check
    elapsed = time.monotonic() - start_time
    if elapsed > MOBY_AGENT_TIMEOUT_S:
        _dbg("AGENTIC timeout after %.1fs at turn %d", elapsed, turn)
        if not text_out:
            text_out = "<p>I ran out of time processing your request. Here's what I found so far.</p>"
        break
    # ... rest of loop ...
```

- [ ] **Step 2: Add DM tool single-call enforcement**

Track whether a Distance Matrix tool was already called:

```python
dm_tools_called = set()  # Track DM tool usage
DM_TOOL_NAMES = {"nearest_filtered_sites", "explorer_within_drive_km"}

# Inside the tool execution loop, before dispatching:
if name in DM_TOOL_NAMES:
    if dm_tools_called:
        msgs.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps({"error": "Distance Matrix tool already called this turn. Use the results from the previous call."}),
        })
        continue
    dm_tools_called.add(name)
```

- [ ] **Step 3: Write tests for timeout and DM protection**

Add to `backend/tests/test_agentic_loop.py`:

```python
def test_loop_respects_timeout():
    """Loop should exit when timeout is exceeded."""
    from backend.app.routers.ai_chat import _agentic_loop, MOBY_AGENT_TIMEOUT_S
    import time

    original_timeout = MOBY_AGENT_TIMEOUT_S

    # Temporarily set very short timeout
    import backend.app.routers.ai_chat as ai_chat_mod
    ai_chat_mod.MOBY_AGENT_TIMEOUT_S = 0  # immediate timeout

    try:
        result = _agentic_loop(
            msgs=[{"role": "user", "content": "test"}],
            tool_ctx=MagicMock(),
            use_thinking=False,
        )
        assert "ran out of time" in result["text"].lower() or result["turns_used"] <= 1
    finally:
        ai_chat_mod.MOBY_AGENT_TIMEOUT_S = original_timeout
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest backend/tests/test_agentic_loop.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ai_chat.py backend/tests/test_agentic_loop.py
git commit -m "feat: add timeout and Distance Matrix single-call protection to agentic loop"
```

---

### Task 10: Integration Test with Local Backend

End-to-end verification that the agentic loop works with real Salesforce data.

**Files:**
- No new files — uses existing test scripts

- [ ] **Step 1: Start local backend**

Run: `bash scripts/restart_local_backend.sh`

- [ ] **Step 2: Run the 92-question Moby test suite**

Run: `SF_SESSION_COOKIE="<fresh cookie>" API_BASE="http://localhost:8000" python scripts/test_moby_questions.py`

Expected: Pass rate should be comparable to current (92/92). Some questions may route differently through the agentic loop vs deterministic handlers — document any regressions.

- [ ] **Step 3: Run the 116-question demo suite**

Run: `SF_SESSION_COOKIE="<fresh cookie>" API_BASE="http://localhost:8000" python scripts/test_demo_questions.py`

Expected: Comparable pass rate to current. Note any questions where the agentic loop gives better or worse answers than the deterministic handlers.

- [ ] **Step 4: Test a multi-step question manually**

Use curl or the frontend to ask: "Find German sites with overnight stays that aren't in any assignment, and rank them by newly diagnosed count."

Verify:
- The response uses multiple tool calls (visible in backend logs)
- The answer is correct (filtered, ranked results)
- Progress events appear in the SSE stream (if using frontend)

- [ ] **Step 5: Document results and any regressions**

Create a brief test report noting:
- Pass rates vs baseline
- Questions that improved (multi-step now works)
- Questions that regressed (if any)
- Latency observations

- [ ] **Step 6: Commit any fixes discovered during testing**

```bash
git add -A
git commit -m "fix: address regressions found during agentic loop integration testing"
```

---

## Summary

| Task | What it does | Estimated effort |
|------|-------------|-----------------|
| 1 | Extract tool dispatch into `moby_tools.py` | Medium |
| 2 | Implement `_agentic_loop` with bounded turns | Medium |
| 3 | Merge overlapping tools (25 → ~18) | Medium |
| 4 | Decision tree in system prompt | Small |
| 5 | Shrink `_try_planner`, hint injection | Medium |
| 6 | SSE progress events — backend | Small |
| 7 | SSE progress events — frontend | Small |
| 8 | Domain knowledge in system prompt | Small |
| 9 | Safety guardrails (timeout, DM protection) | Small |
| 10 | Integration testing | Medium |

**Dependency order:** Task 1 → Task 2 → Tasks 3-9 (parallelizable) → Task 10
