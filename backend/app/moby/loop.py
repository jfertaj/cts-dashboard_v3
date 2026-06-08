"""Moby agentic loop machinery.

Extracted from `app.routers.ai_chat` (Phase 5b refactor).

Includes:
    _is_complex_query     — detects multi-condition queries warranting extended thinking
    _truncate_history     — caps conversation history to MAX_HISTORY_TURNS
    _dispatch_tool_calls  — applies dedup + DM-once + truncation to a batch of tool calls
    _agentic_loop         — bounded Claude tool-use loop (up to MOBY_MAX_AGENT_TURNS turns)
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import anthropic as _anthropic_sdk

from app.moby.config import (
    MAX_HISTORY_TURNS,
    MAX_TOOL_RESULT_TOKENS,
    MOBY_AGENT_TIMEOUT_S,
    MOBY_MAX_AGENT_TURNS,
)
from app.moby.helpers.debug import _dbg
from app.moby.streaming import _STREAM_Q
from app.moby.tools_spec import TOOLS_SPEC


def _keep_table(
    current: Optional[Dict[str, Any]],
    new: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Decide which `last_table` to keep when a new tool result arrives.

    Preserves the last NON-EMPTY table so a later turn/tool that returns an
    empty table (rows == []) — e.g. an aggregate or chart helper — does not
    clobber a previously-produced, populated table.

    Overwrite with `new` only when:
      - `new` is not None, AND
      - `new` has rows, OR we are not already holding a non-empty table.

    This keeps legitimate behaviour intact: a genuine 0-row result still
    surfaces when there is no prior non-empty table, and a non-empty
    transform/aggregate still replaces the prior table.
    """
    if new is None:
        return current
    if new.get("rows") or not (current and current.get("rows")):
        return new
    return current


def _is_complex_query(text: str) -> bool:
    """
    Return True for queries that benefit from extended thinking:
    - Nearest/closest + additional filter criteria
    - Two or more distinct clinical data concepts in the same question
    - Explicit multi-condition phrasing

    Queries handled by the deterministic planner never reach Claude,
    so we only need to catch the harder cases that do.
    """
    s = (text or "").lower()

    # Nearest / distance + any filter qualifier -> always complex
    if re.search(r"\b(nearest|closest|cerca\s+de|próxim\w*|vicino|nahe)\b", s) and \
       re.search(r"\b(with|that\s+have|which\s+have|que\s+tengan|stage|pharmacy|nd\b|overnight|pi\b)\b", s):
        return True

    # Count distinct clinical data concepts; >= 2 together = complex
    concepts = [
        bool(re.search(r"\bstage\s*[12]\b", s)),
        bool(re.search(r"\b(nd\b|newly.diagnosed|new.diagnos|recién.diagnos)", s)),
        bool(re.search(r"\b(pharmacy|farmacia|on.?site.?pharm)", s)),
        bool(re.search(r"\b(overnight|pernoctaci|stay\s+overnight)", s)),
        bool(re.search(r"\b(pi\b|principal.investigator|coord\w+)", s)),
        bool(re.search(r"\b(hla|typing)\b", s)),
        bool(re.search(r"\b(phase\s*[i123]|ct.?site|clinical.trial\s+site)", s)),
        bool(re.search(r"\b(assignment|mca|payment)\b", s)),
    ]
    if sum(concepts) >= 2:
        return True

    # Explicit multi-condition connectors in a data query
    if re.search(r"\b(and\s+(?:also|with|have)|además\s+de|y\s+también)\b", s) and \
       re.search(r"\b(site|center|centro|account)\b", s):
        return True

    return False


def _truncate_history(messages: List[Any], max_turns: int = MAX_HISTORY_TURNS) -> List[Any]:
    """
    Keep only the last `max_turns` user messages and everything after them.
    Prevents unbounded context growth in long conversations.
    Works on both List[ChatMessage] and List[Dict] (role/content dicts).
    """
    def _role(m: Any) -> str:
        return m.role if hasattr(m, "role") else m.get("role", "")

    user_indices = [i for i, m in enumerate(messages) if _role(m) == "user"]
    if len(user_indices) <= max_turns:
        return messages
    cut_at = user_indices[-max_turns]
    _dbg("History truncated: kept last %d user turns (dropped first %d messages)", max_turns, cut_at)
    return messages[cut_at:]


def _dispatch_tool_calls(
    assistant_msg,
    msgs: List[Dict[str, Any]],
    tool_ctx: "ToolContext",
    seen_hashes: set,
    dm_called: bool,
    tool_calls_made: List[Dict[str, Any]],
    turn,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    """Dispatch every tool_call in assistant_msg, applying dedup + DM + truncation.

    Returns a tuple:
        (dm_called_after, last_table, last_viz, last_filters, table_produced_now)
    """
    import hashlib
    from app.routers.moby_tools import dispatch_tool

    _DM_TOOL_NAMES = {"nearest_filtered_sites", "explorer_within_drive_km"}
    _char_limit = MAX_TOOL_RESULT_TOKENS * 4

    last_table: Optional[Dict[str, Any]] = None
    last_visualization: Optional[Dict[str, Any]] = None
    last_explorer_filters: Optional[Dict[str, Any]] = None
    table_produced_now = False

    for tc in assistant_msg.tool_calls:
        name = tc.function.name
        args_json = tc.function.arguments or "{}"
        args = json.loads(args_json)
        tool_call_id = tc.id

        call_hash = hashlib.md5(f"{name}:{args_json}".encode()).hexdigest()
        if call_hash in seen_hashes:
            _dbg("Agentic loop: DEDUP skip %s (same args)", name)
            msgs.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps({
                    "error": f"Duplicate call to {name} with same arguments — skipped. Use different parameters or summarize what you have."
                }),
            })
            continue
        seen_hashes.add(call_hash)

        if name in _DM_TOOL_NAMES:
            if dm_called:
                _dbg("Agentic loop: DM tool %s blocked (already called a DM tool)", name)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps({
                        "error": "Distance Matrix tool already called this turn. Use the results from the previous call."
                    }),
                })
                continue
            dm_called = True

        _dbg("Agentic loop turn %s: TOOL %s args=%s", turn, name, args)
        tool_calls_made.append({"tool": name, "turn": turn})

        tool_ctx.args = args
        tool_ctx.tool_call_id = tool_call_id

        tool_result = dispatch_tool(name, tool_ctx)

        # Truncate oversized tool results in msgs immediately
        for _m in reversed(msgs):
            if _m.get("role") == "tool" and _m.get("tool_call_id") == tool_call_id:
                _content = _m.get("content", "")
                if len(_content) > _char_limit:
                    try:
                        _parsed = json.loads(_content)
                        if isinstance(_parsed, dict) and "rows" in _parsed:
                            _rows = _parsed["rows"]
                            if isinstance(_rows, list) and len(_rows) > 50:
                                _parsed["rows"] = _rows[:50]
                                _parsed["_truncated"] = f"Showing 50 of {len(_rows)} rows"
                                _m["content"] = json.dumps(_parsed, default=str)
                    except Exception:
                        pass
                break

        if tool_result.last_table is not None:
            last_table = _keep_table(last_table, tool_result.last_table)
            tool_ctx.last_table = last_table
            table_produced_now = True
        if tool_result.last_visualization is not None:
            last_visualization = tool_result.last_visualization
            tool_ctx.last_visualization = last_visualization
        if tool_result.last_explorer_filters is not None:
            last_explorer_filters = tool_result.last_explorer_filters
            tool_ctx.last_explorer_filters = last_explorer_filters

    return (dm_called, last_table, last_visualization, last_explorer_filters, table_produced_now)


def _agentic_loop(
    msgs: List[Dict[str, Any]],
    tool_ctx: "ToolContext",
    *,
    use_thinking: bool = False,
    user_msg: str = "",
) -> Dict[str, Any]:
    """
    Bounded agentic loop: up to MOBY_MAX_AGENT_TURNS turns of
    Claude tool-use -> dispatch -> feed results back.

    On the final turn, forces text-only response (no tools).

    Returns dict with keys: text, turns_used, tool_calls_made,
    last_table, last_visualization, last_explorer_filters.
    """
    from app.routers.moby_tools import ToolContext, dispatch_tool  # noqa: F401
    from app.routers.moby_tool_policy import (
        has_tabular_intent,
        filter_tools_spec,
    )
    # Late-bind through ai_chat shim so tests can patch
    # app.routers.ai_chat._claude_chat / _synthesis_fallback / _monotonic
    from app.routers import ai_chat as _ai
    _claude_chat = _ai._claude_chat
    _synthesis_fallback = _ai._synthesis_fallback
    _monotonic = _ai._monotonic

    _tabular = False
    _whitelist_spec: Optional[List[Dict[str, Any]]] = None
    try:
        if has_tabular_intent(user_msg):
            _tabular = True
            _whitelist_spec = filter_tools_spec(TOOLS_SPEC)
            _dbg("Tabular intent detected (query=%r), forcing tool_choice=required", user_msg[:80])
    except Exception as _e:
        _dbg("has_tabular_intent failed, defaulting to non-tabular: %s", _e)

    start_time = _monotonic()
    seen_hashes: set = set()
    dm_called = False  # Distance Matrix tools can only be called once per message
    _DM_TOOL_NAMES = {"nearest_filtered_sites", "explorer_within_drive_km"}
    tool_calls_made: List[Dict[str, Any]] = []
    text_out: Optional[str] = None
    _table_produced_this_request = False

    last_table = tool_ctx.last_table
    last_visualization = tool_ctx.last_visualization
    last_explorer_filters = tool_ctx.last_explorer_filters

    max_turns = MOBY_MAX_AGENT_TURNS

    for turn in range(1, max_turns + 1):
        # Timeout guard
        elapsed = _monotonic() - start_time
        if elapsed > MOBY_AGENT_TIMEOUT_S:
            _dbg("Agentic loop timeout after %.1fs at turn %d", elapsed, turn)
            break

        is_final = (turn == max_turns)

        # Extended thinking only on turn 1
        think = use_thinking if turn == 1 else False

        _dbg("Agentic loop turn %d/%d (final=%s, thinking=%s)", turn, max_turns, is_final, think)

        try:
            if _tabular and turn == 1:
                # Anthropic API rejects thinking + tool_choice="required" together.
                resp = _claude_chat(
                    msgs,
                    tool_choice="required",
                    force_no_tools=is_final,
                    use_thinking=False,
                    tools_override=_whitelist_spec,
                )
            else:
                resp = _claude_chat(msgs, tool_choice="auto", force_no_tools=is_final, use_thinking=think)
        except (_anthropic_sdk.APITimeoutError, _anthropic_sdk.APIConnectionError, _anthropic_sdk.RateLimitError) as e:
            _dbg("Anthropic API error in agentic loop turn %d: %s", turn, e)
            text_out = f"<p>Service temporarily unavailable. Please try again later. ({str(e)[:50]})</p>"
            break

        assistant_msg = resp.choices[0].message

        # No tool calls -> extract text and exit
        if not assistant_msg.tool_calls:
            text_out = (assistant_msg.content or "").strip()
            _dbg("Agentic loop turn %d: text response (%d chars), exiting", turn, len(text_out))
            break

        # Append assistant message with tool_calls to conversation
        msgs.append({
            "role": "assistant",
            "content": assistant_msg.content or "",
            "tool_calls": [tc.model_dump() for tc in assistant_msg.tool_calls],
        })

        # Dispatch each tool call
        (dm_called, _lt, _lv, _lf, _produced_now) = _dispatch_tool_calls(
            assistant_msg, msgs, tool_ctx, seen_hashes, dm_called, tool_calls_made, turn,
        )
        if _lt is not None:
            last_table = _keep_table(last_table, _lt)
        if _lv is not None:
            last_visualization = _lv
        if _lf is not None:
            last_explorer_filters = _lf
        if _produced_now:
            _table_produced_this_request = True

        # Fast exit: if tool(s) returned a good table AND Claude included a meaningful
        # text summary alongside the tool call, skip the synthesis turn (saves 3-8s).
        if last_table and last_table.get("rows"):
            companion_text = (assistant_msg.content or "").strip()
            if companion_text and len(companion_text) > 40:
                text_out = companion_text
                _dbg("Agentic loop: fast exit after turn %d — table + companion text (%d chars)", turn, len(companion_text))
                break

        # Send progress event if streaming
        token_q = getattr(_STREAM_Q, "q", None)
        if token_q is not None:
            try:
                progress = json.dumps({
                    "turn": turn,
                    "tools": [tc.function.name for tc in assistant_msg.tool_calls],
                })
                token_q.put(f"__PROGRESS__{progress}")
            except Exception:
                pass

    # Post-loop retry: one forced call if the loop exited text-only but the
    # user asked for a table and no table was produced this request.
    if _tabular and text_out and not _table_produced_this_request and _whitelist_spec is not None:
        _dbg("Retry triggered: loop exited text-only with tabular intent, re-calling with whitelist")
        # Derive the offered tool names from the actual whitelist spec (single
        # source of truth) so the prompt never drifts from TABLE_RETURNING_TOOLS.
        _offered_names = sorted(
            t.get("function", {}).get("name", "") for t in _whitelist_spec
        )
        _offered_list = ", ".join(n for n in _offered_names if n)
        msgs.append({
            "role": "user",
            "content": (
                "The previous answer lacked a table. The user asked for a list/table — "
                f"you MUST call one of: {_offered_list}."
            ),
        })
        try:
            retry_resp = _claude_chat(
                msgs,
                tool_choice="required",
                force_no_tools=False,
                use_thinking=False,
                tools_override=_whitelist_spec,
            )
        except (_anthropic_sdk.APITimeoutError, _anthropic_sdk.APIConnectionError, _anthropic_sdk.RateLimitError) as _e:
            _dbg("Retry failed: %s", _e)
        else:
            retry_msg = retry_resp.choices[0].message
            if retry_msg.tool_calls:
                msgs.append({
                    "role": "assistant",
                    "content": retry_msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in retry_msg.tool_calls],
                })
                (dm_called, _lt, _lv, _lf, _produced_now) = _dispatch_tool_calls(
                    retry_msg, msgs, tool_ctx, seen_hashes, dm_called, tool_calls_made, turn="retry",
                )
                token_q = getattr(_STREAM_Q, "q", None)
                if token_q is not None:
                    try:
                        progress = json.dumps({
                            "turn": "retry",
                            "tools": [tc.function.name for tc in retry_msg.tool_calls],
                        })
                        token_q.put(f"__PROGRESS__{progress}")
                    except Exception:
                        pass
                if _lt is not None:
                    last_table = _keep_table(last_table, _lt)
                if _lv is not None:
                    last_visualization = _lv
                if _lf is not None:
                    last_explorer_filters = _lf
                if _produced_now:
                    _table_produced_this_request = True
                    retry_text = (retry_msg.content or "").strip()
                    if retry_text:
                        text_out = retry_text

    # Synthesis fallback
    if (not text_out or not text_out.strip()) and len(tool_calls_made) >= 1:
        synth = _synthesis_fallback(msgs, user_msg, prev_tools=len(tool_calls_made))
        if synth:
            text_out = synth

    return {
        "text": text_out,
        "turns_used": turn,
        "tool_calls_made": tool_calls_made,
        "last_table": last_table,
        "last_visualization": last_visualization,
        "last_explorer_filters": last_explorer_filters,
    }
