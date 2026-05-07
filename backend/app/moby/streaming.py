"""Streaming helpers for Moby SSE chat endpoint.

Phase 1 extraction: only `_STREAM_Q` (threading.local) lives here for now.
The SSE generator inside `chat_stream_api` will move in a later phase
together with the chat endpoint itself.

`_STREAM_Q.q` is the implicit channel between `chat_stream_api` (which
sets it) and `_claude_chat` (which reads it to decide whether to stream
text chunks). This thread-local coupling is documented in the refactor
plan as a known wart to address in a later phase.
"""
import threading

# Thread-local used by the /chat/stream endpoint to receive token chunks
# from _claude_chat without changing any function signatures.
_STREAM_Q: threading.local = threading.local()
