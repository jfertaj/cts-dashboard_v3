"""Moby (AI assistant) modules.

Modular extraction of `app.routers.ai_chat`. See `docs/refactor-ai-chat-plan.md`.
Phase 1: pure code moves with re-export shims in `ai_chat.py` to preserve
existing import paths (tests and consumers).
"""
