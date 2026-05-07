"""Debug logging helper for Moby (Phase 3 refactor).

Extracted verbatim from `app.routers.ai_chat`. The original location keeps a
shim re-export so any test that patches `app.routers.ai_chat._dbg` still works.
"""

from app.moby.config import DEBUG


def _dbg(msg: str, *args):
    if DEBUG:
        try:
            print("[AI-CHAT]", msg % args if args else msg)
        except Exception:
            print("[AI-CHAT]", msg)
