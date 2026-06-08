"""Anthropic Claude client wrapper for Moby.

Pure move from `app.routers.ai_chat` (Phase 1 refactor).
Behavior is unchanged — the function accepts the same signature, reads
the same env vars, and returns the same OpenAI-compatible adapter.

"""
import json
import os
from typing import Any, Dict, List, Optional

import anthropic as _anthropic_sdk  # noqa: F401  (kept for back-compat / direct callers)

from app.moby.config import CLAUDE_THINKING_BUDGET, CLAUDE_TEMPERATURE, DEBUG
from app.moby.streaming import _STREAM_Q
from app.moby.tools_spec import TOOLS_SPEC


def _get_anthropic_sdk():
    """Resolve the Anthropic SDK module dynamically.

    Tests patch `app.routers.ai_chat._anthropic_sdk` (the historic location).
    To honor that patch after the Phase 1 move we look it up there first
    and fall back to our local import otherwise.
    """
    try:
        from app.routers import ai_chat as _ai_chat_mod
        return getattr(_ai_chat_mod, "_anthropic_sdk", _anthropic_sdk)
    except Exception:
        return _anthropic_sdk


def _dbg(msg: str, *args):
    if DEBUG:
        try:
            print("[AI-CHAT]", msg % args if args else msg)
        except Exception:
            print("[AI-CHAT]", msg)


# --- OpenAI-compatible adapter (used by _claude_chat) ---
class OpenAICompatibleMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class OpenAICompatibleChoice:
    def __init__(self, message):
        self.message = message


class OpenAICompatibleResponse:
    def __init__(self, message):
        self.choices = [OpenAICompatibleChoice(message)]


def _claude_chat(
    messages: List[Dict[str, Any]],
    tool_choice: str = "required",
    *,
    force_no_tools: bool = False,
    use_thinking: bool = False,
    tools_override: Optional[List[Dict[str, Any]]] = None,
):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise Exception("ANTHROPIC_API_KEY not set")

    model_name = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

    # 1. Convert TOOLS_SPEC (OpenAI format) → Claude format
    claude_tools = []
    if not force_no_tools:
        source_spec = tools_override if tools_override is not None else TOOLS_SPEC
        for t in source_spec:
            f = t["function"]
            params = f.get("parameters") or {"type": "object", "properties": {}}
            claude_tools.append({
                "name": f["name"],
                "description": f.get("description", ""),
                "input_schema": params,
            })

    # 2. Separate system prompt and build Claude message list
    system_parts: List[str] = []
    claude_messages: List[Dict[str, Any]] = []

    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role")
        content = m.get("content") or ""

        if role == "system":
            system_parts.append(content)
            i += 1

        elif role == "user":
            claude_messages.append({"role": "user", "content": content})
            i += 1

        elif role == "assistant":
            tcs = m.get("tool_calls")
            if tcs:
                parts: List[Dict[str, Any]] = []
                if content:
                    parts.append({"type": "text", "text": content})
                for tc in tcs:
                    if hasattr(tc, "function"):
                        name = tc.function.name
                        args = json.loads(tc.function.arguments or "{}")
                        tid = tc.id
                    elif isinstance(tc, dict):
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        args = json.loads(fn.get("arguments", "{}"))
                        tid = tc.get("id", "")
                    else:
                        continue
                    parts.append({"type": "tool_use", "id": tid, "name": name, "input": args})
                claude_messages.append({"role": "assistant", "content": parts})
            else:
                claude_messages.append({"role": "assistant", "content": content})
            i += 1

        elif role == "tool":
            # Collect consecutive tool-result messages into one user turn
            tool_results: List[Dict[str, Any]] = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tm = messages[i]
                raw = tm.get("content") or ""
                # Keep as string for Claude tool_result content
                try:
                    parsed = json.loads(raw)
                    result_str = json.dumps(parsed, default=str) if not isinstance(parsed, str) else parsed
                except Exception:
                    result_str = str(raw)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tm.get("tool_call_id", ""),
                    "content": result_str,
                })
                i += 1
            claude_messages.append({"role": "user", "content": tool_results})

        else:
            i += 1

    system_text = "\n\n".join(system_parts) if system_parts else None

    # 3. Build API call kwargs with prompt caching
    # The system prompt (~4 600 tokens) and TOOLS_SPEC (~18 tools) are large static payloads
    # that are identical across every request — perfect candidates for ephemeral caching.
    # Cache hits cost ~10% of normal input tokens and return ~2× faster.
    kwargs: Dict[str, Any] = {
        "model": model_name,
        "max_tokens": 8192,
        "messages": claude_messages,
    }

    # System: pass as a list of content blocks so we can attach cache_control.
    # Minimum cacheable size for Sonnet is 1024 tokens; our prompt is ~4 600 ✓
    if system_text:
        kwargs["system"] = [
            {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
        ]

    if claude_tools:
        # Cache breakpoint on the last tool — caches the entire tools list as a prefix.
        cached_tools = [t.copy() for t in claude_tools]
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
        kwargs["tools"] = cached_tools
        kwargs["tool_choice"] = {"type": "any"} if tool_choice == "required" else {"type": "auto"}

    # Extended thinking — enabled for complex multi-condition queries.
    # Claude reasons silently before choosing tools/writing its answer.
    # budget_tokens = max internal reasoning tokens (not billed as output).
    # max_tokens is raised so the response fits after the thinking budget.
    if use_thinking and CLAUDE_THINKING_BUDGET > 0:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": CLAUDE_THINKING_BUDGET}
        kwargs["max_tokens"] = CLAUDE_THINKING_BUDGET + 8192
        kwargs["betas"] = ["interleaved-thinking-2025-05-14"]
        _dbg("Extended thinking ENABLED — budget=%d tokens", CLAUDE_THINKING_BUDGET)

    # Pin a deterministic temperature to reduce run-to-run variance in tool
    # selection and generated SOQL/filters. The Anthropic API requires
    # temperature=1 whenever extended thinking is enabled, so only set it when
    # thinking is OFF (the "thinking" key is absent).
    if "thinking" not in kwargs:
        kwargs["temperature"] = CLAUDE_TEMPERATURE

    # 4. Call Claude API
    # When thinking is enabled we pass `betas` via extra_headers so the standard
    # messages.create() endpoint handles it without needing the beta namespace.
    # When _STREAM_Q.q is set (by /chat/stream endpoint), we use streaming mode:
    # text chunks are put into the queue so the SSE endpoint can yield them
    # progressively; the final Message object is used the same way as non-streaming.
    try:
        aclient = _get_anthropic_sdk().Anthropic(api_key=api_key)
        betas = kwargs.pop("betas", None)
        extra_headers = {"anthropic-beta": ",".join(betas)} if betas else {}
        token_q = getattr(_STREAM_Q, "q", None)

        if token_q is not None:
            # Streaming mode — yield text chunks into the queue
            with aclient.messages.stream(**kwargs, extra_headers=extra_headers) as stream:
                for text_chunk in stream.text_stream:
                    token_q.put(text_chunk)
                response = stream.get_final_message()
        else:
            response = aclient.messages.create(**kwargs, extra_headers=extra_headers)

        # Log cache efficiency stats
        if DEBUG and hasattr(response, "usage"):
            u = response.usage
            cached_read  = getattr(u, "cache_read_input_tokens",    0) or 0
            cached_write = getattr(u, "cache_creation_input_tokens", 0) or 0
            _dbg(
                "Token usage → input:%d cache_write:%d cache_read:%d output:%d",
                u.input_tokens, cached_write, cached_read, u.output_tokens,
            )
    except Exception as e:
        import traceback
        _dbg("Claude API Error: %s\n%s", e, traceback.format_exc())
        return OpenAICompatibleResponse(OpenAICompatibleMessage(f"AI Service Error: {str(e)}"))

    # 5. Parse response content blocks → MockToolCall adapters
    class MockFunction:
        def __init__(self, name: str, args_str: str):
            self.name = name
            self.arguments = args_str

    class MockToolCall:
        def __init__(self, tid: str, name: str, args_str: str):
            self.id = tid
            self.type = "function"
            self.function = MockFunction(name, args_str)

        def model_dump(self) -> Dict[str, Any]:
            return {
                "id": self.id,
                "type": self.type,
                "function": {
                    "name": self.function.name,
                    "arguments": self.function.arguments,
                },
            }

    text_parts: List[str] = []
    tool_calls: List[MockToolCall] = []

    for block in (response.content or []):
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            args_str = json.dumps(block.input or {}, default=str)
            tool_calls.append(MockToolCall(block.id, block.name, args_str))
        # "thinking" blocks are internal reasoning — skipped intentionally

    content_str = "\n".join(text_parts) if text_parts else None

    if DEBUG:
        thinking_blocks = [b for b in (response.content or []) if getattr(b, "type", "") == "thinking"]
        thinking_chars = sum(len(getattr(b, "thinking", "") or "") for b in thinking_blocks)
        _dbg("Claude Response: text=%d chars, tools=%d, thinking=%d chars, stop=%s",
             len(content_str or ""), len(tool_calls), thinking_chars, response.stop_reason)
    else:
        _dbg("Claude Response: text=%d chars, tools=%d stop=%s",
             len(content_str or ""), len(tool_calls), response.stop_reason)

    return OpenAICompatibleResponse(OpenAICompatibleMessage(content_str, tool_calls if tool_calls else None))
