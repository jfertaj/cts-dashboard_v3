"""
Tests for `_claude_chat` — the Anthropic API wrapper.

We mock `_anthropic_sdk.Anthropic` and capture the kwargs passed into
`messages.create` (or `messages.stream`) so we can assert on:

- tool_choice mapping (required → {"type":"any"}, auto → {"type":"auto"}).
- force_no_tools removes the tools key entirely.
- tools_override replaces TOOLS_SPEC and only the named tools are sent.
- use_thinking attaches `thinking` + `betas` headers.
- Streaming path used when `_STREAM_Q.q` is set.
- API errors are swallowed → returns an OpenAICompatibleResponse with the
  error string in `.choices[0].message.content` (does not raise).
"""
import os
import queue
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_message(text: str = "ok", tool_uses=None, stop_reason: str = "end_turn"):
    """Build a minimal Anthropic SDK Message-shape object."""
    blocks = []
    if text:
        blocks.append(types.SimpleNamespace(type="text", text=text))
    for tu in tool_uses or []:
        blocks.append(types.SimpleNamespace(
            type="tool_use", id=tu["id"], name=tu["name"], input=tu.get("input", {})
        ))
    return types.SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=types.SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


@pytest.fixture
def captured_kwargs():
    """Mock SDK that captures kwargs passed to messages.create/stream."""
    captured = {"create": None, "stream": None, "headers": None}

    client = MagicMock()

    def _fake_create(**kw):
        captured["create"] = kw
        return _fake_message("hi from claude")

    def _fake_stream(**kw):
        captured["stream"] = kw
        # Build a context manager that mimics the streaming response.
        ctx = MagicMock()
        ctx.__enter__ = lambda self: ctx
        ctx.__exit__ = lambda self, *a: False
        ctx.text_stream = iter(["he", "llo"])
        ctx.get_final_message = lambda: _fake_message("hello")
        return ctx

    # Capture extra_headers too — passed positionally as a kwarg by _claude_chat.
    def _record_headers(orig):
        def _wrapped(**kw):
            captured["headers"] = kw.get("extra_headers")
            return orig(**kw)
        return _wrapped

    client.messages.create = _record_headers(_fake_create)
    client.messages.stream = _record_headers(_fake_stream)
    return client, captured


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------

@patch("app.routers.ai_chat._anthropic_sdk")
def test_tool_choice_required_maps_to_any(mock_sdk, captured_kwargs):
    from app.routers.ai_chat import _claude_chat
    client, captured = captured_kwargs
    mock_sdk.Anthropic.return_value = client

    _claude_chat([{"role": "user", "content": "hi"}], tool_choice="required")

    assert captured["create"] is not None
    assert captured["create"]["tool_choice"] == {"type": "any"}


@patch("app.routers.ai_chat._anthropic_sdk")
def test_tool_choice_auto_maps_to_auto(mock_sdk, captured_kwargs):
    from app.routers.ai_chat import _claude_chat
    client, captured = captured_kwargs
    mock_sdk.Anthropic.return_value = client

    _claude_chat([{"role": "user", "content": "hi"}], tool_choice="auto")

    assert captured["create"]["tool_choice"] == {"type": "auto"}


@patch("app.routers.ai_chat._anthropic_sdk")
def test_force_no_tools_strips_tools_key(mock_sdk, captured_kwargs):
    from app.routers.ai_chat import _claude_chat
    client, captured = captured_kwargs
    mock_sdk.Anthropic.return_value = client

    _claude_chat([{"role": "user", "content": "hi"}], force_no_tools=True)

    assert "tools" not in captured["create"], "force_no_tools must omit tools entirely"
    assert "tool_choice" not in captured["create"]


@patch("app.routers.ai_chat._anthropic_sdk")
def test_tools_override_replaces_full_tools_spec(mock_sdk, captured_kwargs):
    """Whitelist via tools_override → only the named tools are passed."""
    from app.routers.ai_chat import _claude_chat
    client, captured = captured_kwargs
    mock_sdk.Anthropic.return_value = client

    override = [{
        "type": "function",
        "function": {
            "name": "explorer_search",
            "description": "x",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    _claude_chat([{"role": "user", "content": "hi"}], tools_override=override)

    names = [t["name"] for t in captured["create"]["tools"]]
    assert names == ["explorer_search"]


@patch("app.routers.ai_chat._anthropic_sdk")
def test_use_thinking_attaches_thinking_block_and_beta_header(mock_sdk, captured_kwargs):
    from app.routers.ai_chat import _claude_chat, CLAUDE_THINKING_BUDGET
    client, captured = captured_kwargs
    mock_sdk.Anthropic.return_value = client

    _claude_chat([{"role": "user", "content": "hi"}], use_thinking=True)

    if CLAUDE_THINKING_BUDGET > 0:
        assert captured["create"]["thinking"]["type"] == "enabled"
        assert captured["create"]["thinking"]["budget_tokens"] == CLAUDE_THINKING_BUDGET
        # max_tokens raised to budget + 8192
        assert captured["create"]["max_tokens"] == CLAUDE_THINKING_BUDGET + 8192
        assert captured["headers"] == {"anthropic-beta": "interleaved-thinking-2025-05-14"}


@patch("app.routers.ai_chat._anthropic_sdk")
def test_thinking_and_forced_tool_choice_are_not_combined(mock_sdk, captured_kwargs):
    """Quick-win Fix 2: the Anthropic API returns a 400 ('Thinking may not be
    enabled when tool_choice forces tool use.') when extended thinking is
    enabled at the same time as a forced tool_choice ({"type": "any"}).

    SC03/FA01/GL04/MC03 (complex multi-condition queries) trip this when a
    caller passes use_thinking=True with the default tool_choice="required".
    Guard inside _claude_chat: when thinking is on, never force tool use.
    """
    from app.routers.ai_chat import _claude_chat, CLAUDE_THINKING_BUDGET
    client, captured = captured_kwargs
    mock_sdk.Anthropic.return_value = client

    # use_thinking=True AND tool_choice="required" (would force {"type": "any"})
    _claude_chat([{"role": "user", "content": "hi"}],
                 tool_choice="required", use_thinking=True)

    create = captured["create"]
    assert create is not None
    if CLAUDE_THINKING_BUDGET > 0:
        # Thinking is enabled...
        assert create.get("thinking", {}).get("type") == "enabled"
        # max_tokens must stay strictly greater than the thinking budget.
        assert create["max_tokens"] > create["thinking"]["budget_tokens"]
        # ...so tool_choice must NOT force tool use (no {"type": "any"}).
        assert create.get("tool_choice") != {"type": "any"}, (
            "thinking + forced tool_choice → Anthropic 400"
        )


@patch("app.routers.ai_chat._anthropic_sdk")
def test_streaming_path_used_when_stream_q_set(mock_sdk, captured_kwargs):
    from app.routers.ai_chat import _claude_chat, _STREAM_Q
    client, captured = captured_kwargs
    mock_sdk.Anthropic.return_value = client

    q: "queue.Queue[str]" = queue.Queue()
    _STREAM_Q.q = q
    try:
        resp = _claude_chat([{"role": "user", "content": "hi"}])
    finally:
        _STREAM_Q.q = None

    # Stream branch should have populated queue with chunks
    chunks = []
    while not q.empty():
        chunks.append(q.get_nowait())
    assert chunks == ["he", "llo"]
    # Non-stream `messages.create` must NOT have been called
    assert captured["create"] is None
    assert captured["stream"] is not None
    # Final message text returned
    assert resp.choices[0].message.content == "hello"


@patch("app.routers.ai_chat._anthropic_sdk")
def test_api_error_returns_compat_response_not_raise(mock_sdk):
    """Any unexpected SDK exception is caught + converted to a text response."""
    from app.routers.ai_chat import _claude_chat

    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("kaboom")
    mock_sdk.Anthropic.return_value = client

    resp = _claude_chat([{"role": "user", "content": "hi"}])
    text = resp.choices[0].message.content
    assert "AI Service Error" in text
    assert "kaboom" in text


@patch("app.routers.ai_chat._anthropic_sdk")
def test_response_parsing_extracts_text_and_tool_uses(mock_sdk):
    """tool_use blocks become MockToolCall objects exposed via .tool_calls."""
    from app.routers.ai_chat import _claude_chat

    client = MagicMock()
    client.messages.create.return_value = _fake_message(
        text="Found 9 sites.",
        tool_uses=[
            {"id": "tu_1", "name": "explorer_search", "input": {"q": "FR"}},
        ],
    )
    mock_sdk.Anthropic.return_value = client

    resp = _claude_chat([{"role": "user", "content": "list sites"}])
    msg = resp.choices[0].message
    assert msg.content == "Found 9 sites."
    assert msg.tool_calls is not None and len(msg.tool_calls) == 1
    assert msg.tool_calls[0].function.name == "explorer_search"
    # Args should round-trip as JSON string
    import json as _json
    assert _json.loads(msg.tool_calls[0].function.arguments) == {"q": "FR"}


@patch("app.routers.ai_chat._anthropic_sdk")
def test_system_messages_are_concatenated_into_system_kwarg(mock_sdk, captured_kwargs):
    from app.routers.ai_chat import _claude_chat
    client, captured = captured_kwargs
    mock_sdk.Anthropic.return_value = client

    _claude_chat([
        {"role": "system", "content": "S1"},
        {"role": "system", "content": "S2"},
        {"role": "user", "content": "hi"},
    ])

    sys_block = captured["create"]["system"]
    # System is passed as a list of content blocks with cache_control
    assert isinstance(sys_block, list)
    assert "S1" in sys_block[0]["text"] and "S2" in sys_block[0]["text"]
    assert sys_block[0]["cache_control"] == {"type": "ephemeral"}


@patch("app.routers.ai_chat._anthropic_sdk")
def test_missing_api_key_raises(mock_sdk, monkeypatch):
    """No ANTHROPIC_API_KEY → explicit error so caller sees the misconfig."""
    from app.routers.ai_chat import _claude_chat

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(Exception, match="ANTHROPIC_API_KEY"):
        _claude_chat([{"role": "user", "content": "hi"}])
