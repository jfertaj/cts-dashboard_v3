"""
Tests for `_synthesis_fallback` and the loop-side guard that triggers it
when the agentic loop ends with empty text after >=1 successful tool call.

The trigger lives in `_agentic_loop` (last 4 lines before the final return),
the helper itself lives at `_synthesis_fallback`. We cover:

1. text=0 + prior tool_use >=1   → fallback fires + non-empty result returned.
2. text=0 + 0 prior tool_use      → fallback NOT fired (no regression on
   conversational empty-text edge cases).
3. fallback returns empty         → final text stays empty (caller falls
   through to the generic "I wasn't able to answer" string).
4. fallback raises                → propagation suppressed, returns None.
5. Lang hint propagates           → Spanish prompt for Spanish input.
"""
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers (kept small + local so this file is self-contained and easy to read)
# ---------------------------------------------------------------------------

def _text_response(text: str):
    msg = types.SimpleNamespace()
    msg.content = text
    msg.tool_calls = None
    choice = types.SimpleNamespace()
    choice.message = msg
    resp = types.SimpleNamespace()
    resp.choices = [choice]
    return resp


def _tool_response(tool_calls, text: str = ""):
    msg = types.SimpleNamespace()
    msg.content = text
    msg.tool_calls = tool_calls
    choice = types.SimpleNamespace()
    choice.message = msg
    resp = types.SimpleNamespace()
    resp.choices = [choice]
    return resp


def _make_tool_call(tid: str, name: str, args_json: str = "{}"):
    fn = types.SimpleNamespace()
    fn.name = name
    fn.arguments = args_json
    tc = types.SimpleNamespace()
    tc.id = tid
    tc.type = "function"
    tc.function = fn
    tc.model_dump = lambda: {
        "id": tid, "type": "function",
        "function": {"name": name, "arguments": args_json},
    }
    return tc


@pytest.fixture
def tool_ctx():
    from app.routers.moby_tools import ToolContext
    return ToolContext(
        db=MagicMock(), request=MagicMock(), sf=MagicMock(),
        last_table=None, last_visualization=None, last_explorer_filters=None,
        msgs=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        args={}, tool_call_id="",
    )


# ---------------------------------------------------------------------------
# Direct unit tests of `_synthesis_fallback`
# ---------------------------------------------------------------------------

@patch("app.routers.ai_chat._claude_chat")
def test_synthesis_fallback_returns_text_when_claude_synthesizes(mock_claude):
    from app.routers.ai_chat import _synthesis_fallback
    mock_claude.return_value = _text_response("Found 70 sites in Italy.")

    out = _synthesis_fallback(
        msgs=[{"role": "system", "content": "x"}],
        user_msg="how many sites in Italy?",
        prev_tools=2,
    )
    assert out == "Found 70 sites in Italy."

    # Sanity: invoked with force_no_tools and use_thinking=False
    _, kw = mock_claude.call_args
    assert kw["force_no_tools"] is True
    assert kw["use_thinking"] is False


@patch("app.routers.ai_chat._claude_chat")
def test_synthesis_fallback_returns_none_on_empty_text(mock_claude):
    """If the synth call also returns empty, return None (caller hits generic)."""
    from app.routers.ai_chat import _synthesis_fallback
    mock_claude.return_value = _text_response("")

    out = _synthesis_fallback(msgs=[], user_msg="x", prev_tools=1)
    assert out is None


@patch("app.routers.ai_chat._claude_chat")
def test_synthesis_fallback_returns_none_on_whitespace(mock_claude):
    from app.routers.ai_chat import _synthesis_fallback
    mock_claude.return_value = _text_response("   \n  ")
    assert _synthesis_fallback(msgs=[], user_msg="x", prev_tools=1) is None


@patch("app.routers.ai_chat._claude_chat")
def test_synthesis_fallback_swallows_exceptions(mock_claude):
    """Fallback failure must never propagate — return None, not raise."""
    from app.routers.ai_chat import _synthesis_fallback
    mock_claude.side_effect = RuntimeError("boom")
    assert _synthesis_fallback(msgs=[], user_msg="x", prev_tools=1) is None


@patch("app.routers.ai_chat._claude_chat")
def test_synthesis_fallback_lang_hint_spanish(mock_claude):
    """Spanish trigger words → 'Spanish' lang word in the prompt."""
    from app.routers.ai_chat import _synthesis_fallback
    mock_claude.return_value = _text_response("Hay 70 centros en Italia.")
    _synthesis_fallback(msgs=[], user_msg="¿cuántos centros hay en España?", prev_tools=1)

    msgs_arg, *_ = mock_claude.call_args[0]
    assert msgs_arg[-1]["role"] == "user"
    assert "Spanish" in msgs_arg[-1]["content"]


# ---------------------------------------------------------------------------
# Loop-side guard: the trigger condition lives in `_agentic_loop`
# ---------------------------------------------------------------------------

@patch("app.routers.ai_chat._claude_chat")
@patch("app.routers.moby_tools.dispatch_tool")
def test_loop_does_not_call_synth_when_no_prior_tools(mock_dispatch, mock_claude, tool_ctx):
    """Empty text + 0 tool_use  →  no synthesis call (no regression)."""
    from app.routers.ai_chat import _agentic_loop

    mock_claude.return_value = _text_response("")
    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx)

    # Exactly one Claude call: the loop's first turn. No synthesis turn.
    assert mock_claude.call_count == 1
    assert (result["text"] or "") == ""


@patch("app.routers.ai_chat._claude_chat")
@patch("app.routers.moby_tools.dispatch_tool")
def test_loop_calls_synth_when_text_empty_and_prior_tool_use(
    mock_dispatch, mock_claude, tool_ctx
):
    """Tool ran successfully, final text is empty → synthesis fires."""
    from app.routers.ai_chat import _agentic_loop
    from app.routers.moby_tools import ToolResult

    tc = _make_tool_call("t1", "explorer_search", "{}")
    # Loop sequence: turn 1 tool → turn 2 empty-text exits loop. Then synth fires
    # because tool_calls_made >= 1 and text_out is empty.
    mock_claude.side_effect = [
        _tool_response([tc], text=""),
        _text_response(""),
        _text_response("9 sites in France."),  # synthesis fallback
    ]
    mock_dispatch.return_value = ToolResult(
        last_table={"rows": [{"a": 1}], "columns": [{"key": "a"}]},
    )

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx, user_msg="sites in France")
    assert result["text"] == "9 sites in France."
    assert mock_claude.call_count == 3  # 2 loop turns + 1 synthesis


@patch("app.routers.ai_chat._claude_chat")
@patch("app.routers.moby_tools.dispatch_tool")
def test_loop_synth_returns_empty_falls_through(mock_dispatch, mock_claude, tool_ctx):
    """Synth also returns empty → text_out stays empty (caller produces generic)."""
    from app.routers.ai_chat import _agentic_loop
    from app.routers.moby_tools import ToolResult

    tc = _make_tool_call("t1", "explorer_search", "{}")
    mock_claude.side_effect = [
        _tool_response([tc], text=""),
        _text_response(""),
        _text_response(""),  # synth also empty
    ]
    mock_dispatch.return_value = ToolResult(last_table={"rows": [{"a": 1}], "columns": ["a"]})

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx, user_msg="sites in France")
    assert (result["text"] or "") == ""


@patch("app.routers.ai_chat._claude_chat")
@patch("app.routers.moby_tools.dispatch_tool")
def test_loop_synth_exception_falls_through(mock_dispatch, mock_claude, tool_ctx):
    """Synth raises → suppressed, text_out stays empty, no propagation."""
    from app.routers.ai_chat import _agentic_loop
    from app.routers.moby_tools import ToolResult

    tc = _make_tool_call("t1", "explorer_search", "{}")
    # First 2 calls drive the loop; the 3rd raises during the synth attempt.
    mock_claude.side_effect = [
        _tool_response([tc], text=""),
        _text_response(""),
        RuntimeError("synth boom"),
    ]
    mock_dispatch.return_value = ToolResult(last_table={"rows": [{"a": 1}], "columns": ["a"]})

    # Must not raise.
    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx, user_msg="sites in France")
    assert (result["text"] or "") == ""
