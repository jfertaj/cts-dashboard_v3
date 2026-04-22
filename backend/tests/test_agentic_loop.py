"""
Tests for the Moby agentic loop (_agentic_loop).

Mocks _claude_chat and dispatch_tool to test loop control flow
without needing a real Claude API key or Salesforce connection.
"""
import json
import types
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers to build mock Claude responses
# ---------------------------------------------------------------------------

def _make_mock_function(name: str, args_str: str):
    fn = types.SimpleNamespace()
    fn.name = name
    fn.arguments = args_str
    return fn


def _make_mock_tool_call(tid: str, name: str, args: dict):
    tc = types.SimpleNamespace()
    tc.id = tid
    tc.type = "function"
    tc.function = _make_mock_function(name, json.dumps(args))
    tc.model_dump = lambda: {
        "id": tc.id,
        "type": tc.type,
        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
    }
    return tc


def _text_response(text: str):
    """Simulate a Claude response with text only (no tool calls)."""
    msg = types.SimpleNamespace()
    msg.content = text
    msg.tool_calls = None
    choice = types.SimpleNamespace()
    choice.message = msg
    resp = types.SimpleNamespace()
    resp.choices = [choice]
    return resp


def _tool_response(tool_calls, text: str = ""):
    """Simulate a Claude response with tool calls."""
    msg = types.SimpleNamespace()
    msg.content = text
    msg.tool_calls = tool_calls
    choice = types.SimpleNamespace()
    choice.message = msg
    resp = types.SimpleNamespace()
    resp.choices = [choice]
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tool_ctx():
    """Minimal ToolContext for testing."""
    from backend.app.routers.moby_tools import ToolContext, ToolResult
    return ToolContext(
        db=MagicMock(),
        request=MagicMock(),
        sf=MagicMock(),
        last_table=None,
        last_visualization=None,
        last_explorer_filters=None,
        msgs=[{"role": "system", "content": "test"}, {"role": "user", "content": "hello"}],
        args={},
        tool_call_id="",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_max_agent_turns_default():
    """MOBY_MAX_AGENT_TURNS defaults to 3."""
    from backend.app.routers.ai_chat import MOBY_MAX_AGENT_TURNS
    assert MOBY_MAX_AGENT_TURNS == 3


def test_max_tool_result_tokens_default():
    """MAX_TOOL_RESULT_TOKENS defaults to 4000."""
    from backend.app.routers.ai_chat import MAX_TOOL_RESULT_TOKENS
    assert MAX_TOOL_RESULT_TOKENS == 4000


def test_agent_timeout_default():
    """MOBY_AGENT_TIMEOUT_S defaults to 30."""
    from backend.app.routers.ai_chat import MOBY_AGENT_TIMEOUT_S
    assert MOBY_AGENT_TIMEOUT_S == 30


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_exits_on_text_response(mock_dispatch, mock_claude, tool_ctx):
    """Claude returns text on turn 1 => loop exits with turns_used=1, no tool calls."""
    from backend.app.routers.ai_chat import _agentic_loop

    mock_claude.return_value = _text_response("<p>Hello!</p>")

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx)

    assert result["text"] == "<p>Hello!</p>"
    assert result["turns_used"] == 1
    assert result["tool_calls_made"] == []
    mock_dispatch.assert_not_called()
    # _claude_chat called once with auto tool_choice
    mock_claude.assert_called_once()
    _, kwargs = mock_claude.call_args
    assert kwargs.get("force_no_tools") is False


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_calls_tool_and_continues_to_synthesis(mock_dispatch, mock_claude, tool_ctx):
    """Turn 1: tool call returns table but no companion text → turn 2: Claude synthesizes."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tools import ToolResult

    tc1 = _make_mock_tool_call("tc_1", "salesforce_query", {"soql": "SELECT Id FROM Account"})
    mock_claude.side_effect = [
        _tool_response([tc1]),  # turn 1: tool call, no companion text
        _text_response("<p>Found 10 sites across 3 countries. Germany leads with 5.</p>"),  # turn 2: synthesis
    ]
    mock_dispatch.return_value = ToolResult(
        last_table={"rows": [{"id": "001"}], "columns": [{"key": "id", "label": "Id"}]},
    )

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx)

    assert result["turns_used"] == 2
    assert len(result["tool_calls_made"]) == 1
    assert result["tool_calls_made"][0]["tool"] == "salesforce_query"
    assert result["last_table"] is not None
    assert "10 sites" in result["text"]
    mock_dispatch.assert_called_once()

@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_fast_exits_with_companion_text(mock_dispatch, mock_claude, tool_ctx):
    """Turn 1: tool call + companion text (>40 chars) → fast exit."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tools import ToolResult

    tc1 = _make_mock_tool_call("tc_1", "explorer_search", {"filters": {}})
    # Claude includes text alongside the tool call
    resp = _tool_response([tc1])
    resp.choices[0].message.content = "Here are the 9 German sites. Hamburg leads with 650 T1D patients under 18."
    mock_claude.side_effect = [resp]
    mock_dispatch.return_value = ToolResult(
        last_table={"rows": [{"id": "001"}], "columns": [{"key": "id", "label": "Id"}]},
    )

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx)

    # Fast exit: companion text present → use it, skip turn 2
    assert result["turns_used"] == 1
    assert "9 German sites" in result["text"]
    assert mock_claude.call_count == 1


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_forces_text_on_final_turn(mock_dispatch, mock_claude, tool_ctx):
    """On turn 3 (final), _claude_chat is called with force_no_tools=True."""
    from backend.app.routers.ai_chat import _agentic_loop, MOBY_MAX_AGENT_TURNS
    from backend.app.routers.moby_tools import ToolResult

    # Turn 1: tool call
    tc1 = _make_mock_tool_call("tc_1", "salesforce_query", {"soql": "SELECT Id FROM Account"})
    # Turn 2: different tool call
    tc2 = _make_mock_tool_call("tc_2", "sql_query", {"sql": "SELECT * FROM sites"})
    # Turn 3: forced text (no tools)
    mock_claude.side_effect = [
        _tool_response([tc1]),
        _tool_response([tc2]),
        _text_response("<p>Summary of results.</p>"),
    ]
    mock_dispatch.return_value = ToolResult(last_table={"rows": [], "columns": []})

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx)

    assert result["turns_used"] == MOBY_MAX_AGENT_TURNS
    assert result["text"] == "<p>Summary of results.</p>"

    # Verify the 3rd call had force_no_tools=True
    calls = mock_claude.call_args_list
    assert len(calls) == 3
    # Turn 3 (index 2) should have force_no_tools=True
    _, kwargs3 = calls[2]
    assert kwargs3["force_no_tools"] is True
    # Turn 1 and 2 should have force_no_tools=False
    _, kwargs1 = calls[0]
    assert kwargs1["force_no_tools"] is False
    _, kwargs2 = calls[1]
    assert kwargs2["force_no_tools"] is False


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_no_duplicate_tool_calls(mock_dispatch, mock_claude, tool_ctx):
    """Same tool+args called twice in same turn => second is skipped with error."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tools import ToolResult

    same_args = {"soql": "SELECT Id FROM Account"}
    tc1 = _make_mock_tool_call("tc_1", "salesforce_query", same_args)
    tc2 = _make_mock_tool_call("tc_2", "salesforce_query", same_args)  # duplicate

    mock_claude.side_effect = [
        _tool_response([tc1, tc2]),
        _text_response("<p>Done.</p>"),
    ]
    mock_dispatch.return_value = ToolResult()

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx)

    # dispatch_tool should only be called once (second was deduped)
    assert mock_dispatch.call_count == 1
    assert result["text"] == "<p>Done.</p>"

    # Check that a dedup error message was appended for tc_2
    tool_msgs = [m for m in tool_ctx.msgs if m.get("role") == "tool"]
    dedup_msgs = [m for m in tool_msgs if "Duplicate call" in m.get("content", "")]
    assert len(dedup_msgs) == 1
    assert dedup_msgs[0]["tool_call_id"] == "tc_2"


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_dedup_across_turns(mock_dispatch, mock_claude, tool_ctx):
    """Same tool+args across different turns => second is skipped."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tools import ToolResult

    same_args = {"soql": "SELECT Id FROM Account"}
    tc1 = _make_mock_tool_call("tc_1", "salesforce_query", same_args)
    tc2 = _make_mock_tool_call("tc_2", "salesforce_query", same_args)

    mock_claude.side_effect = [
        _tool_response([tc1]),   # Turn 1
        _tool_response([tc2]),   # Turn 2: same tool+args
        _text_response("<p>Done.</p>"),  # Turn 3
    ]
    mock_dispatch.return_value = ToolResult()

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx)

    # Only one actual dispatch
    assert mock_dispatch.call_count == 1


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_extended_thinking_only_turn_1(mock_dispatch, mock_claude, tool_ctx):
    """Extended thinking is only passed on turn 1."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tools import ToolResult

    tc1 = _make_mock_tool_call("tc_1", "salesforce_query", {"soql": "SELECT Id FROM Account"})
    mock_claude.side_effect = [
        _tool_response([tc1]),
        _text_response("<p>Done.</p>"),
    ]
    mock_dispatch.return_value = ToolResult()

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx, use_thinking=True)

    calls = mock_claude.call_args_list
    assert len(calls) == 2
    # Turn 1: use_thinking=True
    _, kwargs1 = calls[0]
    assert kwargs1["use_thinking"] is True
    # Turn 2: use_thinking=False
    _, kwargs2 = calls[1]
    assert kwargs2["use_thinking"] is False


@patch("backend.app.routers.ai_chat._monotonic")
@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_timeout(mock_dispatch, mock_claude, mock_mono, tool_ctx):
    """Loop exits when timeout is exceeded."""
    from backend.app.routers.ai_chat import _agentic_loop, MOBY_AGENT_TIMEOUT_S
    from backend.app.routers.moby_tools import ToolResult

    # Simulate time progression: start=0, turn 1 check=0, turn 2 check=TIMEOUT+1
    mock_mono.side_effect = [0.0, 0.0, float(MOBY_AGENT_TIMEOUT_S + 1)]

    tc1 = _make_mock_tool_call("tc_1", "salesforce_query", {"soql": "SELECT Id FROM Account"})
    mock_claude.side_effect = [
        _tool_response([tc1]),
        _text_response("should not reach"),
    ]
    mock_dispatch.return_value = ToolResult()

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx)

    # Should have exited after turn 1 due to timeout before turn 2
    assert result["turns_used"] == 2  # loop var is 2 when timeout triggers
    assert mock_claude.call_count == 1  # only turn 1 executed


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_updates_state_from_tool_result(mock_dispatch, mock_claude, tool_ctx):
    """Tool results update last_table/visualization/explorer_filters."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tools import ToolResult

    table = {"rows": [{"id": "001"}], "columns": [{"key": "id"}]}
    viz = {"type": "bar", "data": []}
    filters = {"rules": []}

    tc1 = _make_mock_tool_call("tc_1", "explorer_search", {"filters": {}})
    mock_claude.side_effect = [
        _tool_response([tc1]),
        _text_response("<p>Results.</p>"),
    ]
    mock_dispatch.return_value = ToolResult(
        last_table=table,
        last_visualization=viz,
        last_explorer_filters=filters,
    )

    result = _agentic_loop(msgs=tool_ctx.msgs, tool_ctx=tool_ctx)

    assert result["last_table"] == table
    assert result["last_visualization"] == viz
    assert result["last_explorer_filters"] == filters


@patch("backend.app.routers.ai_chat._anthropic_sdk")
def test_claude_chat_tools_override_replaces_tools_spec(mock_sdk):
    """When tools_override is passed, claude_tools is built from the override, not TOOLS_SPEC."""
    from backend.app.routers.ai_chat import _claude_chat

    captured = {}
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = lambda **kw: captured.update(kw) or _fake_anthropic_response()
    mock_sdk.Anthropic.return_value = mock_client

    override = [{
        "type": "function",
        "function": {"name": "explorer_search", "description": "x", "parameters": {"type": "object", "properties": {}}},
    }]

    import os
    os.environ["ANTHROPIC_API_KEY"] = "test-key"

    _claude_chat([{"role": "user", "content": "hi"}], tools_override=override)

    tools_passed = captured.get("tools") or []
    names = [t["name"] for t in tools_passed]
    assert names == ["explorer_search"], f"Expected only explorer_search, got {names}"


def _fake_anthropic_response():
    """Minimal response object mimicking the Anthropic SDK Message shape."""
    resp = types.SimpleNamespace()
    resp.content = [types.SimpleNamespace(type="text", text="ok")]
    resp.stop_reason = "end_turn"
    resp.usage = types.SimpleNamespace(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    return resp


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_forces_tool_choice_when_tabular_intent(mock_dispatch, mock_claude, tool_ctx):
    """Tabular intent → turn 1 uses tool_choice='required' and tools_override whitelist."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tool_policy import TABLE_RETURNING_TOOLS

    mock_claude.return_value = _text_response("<p>empty</p>")

    _agentic_loop(
        msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
        user_msg="List sites in Spain",
    )

    assert mock_claude.called
    _, kwargs = mock_claude.call_args_list[0]
    assert kwargs.get("tool_choice") == "required", f"Got tool_choice={kwargs.get('tool_choice')}"
    override = kwargs.get("tools_override")
    assert override is not None, "tools_override must be passed when tabular"
    names = {t["function"]["name"] for t in override}
    assert names == TABLE_RETURNING_TOOLS


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_uses_auto_when_not_tabular(mock_dispatch, mock_claude, tool_ctx):
    """Non-tabular query → existing behaviour: tool_choice='auto', no override."""
    from backend.app.routers.ai_chat import _agentic_loop

    mock_claude.return_value = _text_response("<p>hi</p>")

    _agentic_loop(
        msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
        user_msg="Explain what CTS means",
    )

    _, kwargs = mock_claude.call_args_list[0]
    assert kwargs.get("tool_choice") == "auto"
    assert kwargs.get("tools_override") is None
