"""Chart-rendering tool implementation.

Pure move from `app.routers.ai_chat` (Phase 4 refactor). Behavior is
unchanged. Re-exported by `ai_chat.py` so existing call sites and test
mocks (`@patch("app.routers.ai_chat.tool_render_chart")`) keep working.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional


def tool_render_chart(
    kind: Literal["bar", "line", "scatter", "pie"],
    data: Any,
    xKey: str,
    yKeys: List[str],
    meta: Optional[Dict[str, Any]] = None,
    options: Optional[Dict[str, Any]] = None,
):
    # --- normalize numeric series (so the ChartModal never says "No numeric columns detected") ---
    rows = data or []
    if isinstance(rows, list) and isinstance(yKeys, list):
        norm = []
        for r in rows:
            rr = dict(r) if isinstance(r, dict) else r
            if isinstance(rr, dict):
                for y in yKeys:
                    v = rr.get(y)
                    if not isinstance(v, (int, float)) and v is not None:
                        try:
                            rr[y] = float(str(v).replace(",", ""))
                        except Exception:
                            rr[y] = 0
            norm.append(rr)
        rows = norm

    return {
        "type": "chart",
        "visualization": {
            "type": kind,
            "xKey": xKey,
            "yKeys": yKeys,
            "data": rows,
            "meta": meta or {},
        }
    }
