"""Metric resolver helper extracted from ai_chat.py.

Phase 3 refactor — pure move, no behavior changes. Lazy imports for the
knowledge index + sibling helpers avoid circular deps with ai_chat.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from sqlalchemy.orm import Session


def _resolve_metric(alias_or_key: str, db: Session) -> Dict[str, Any]:
    """
    Resolve a free-text alias or raw key into:
      - {"source":"sf","field":"<SF API name>","label":"<nice label>"}
      - {"source":"site_qual","key":"<JSONB key>","label":"<nice label>"}
    """
    from app.routers.ai_chat import (
        _build_knowledge_index,
        _normalize,
        _top_matches,
    )

    cache = _build_knowledge_index(db)
    kidx = cache.get("index", {})
    sf_fields = cache.get("sf_fields", {})

    qn = _normalize(alias_or_key)
    if qn in kidx:
        meta = dict(kidx[qn])
    else:
        best = _top_matches(alias_or_key, list(kidx.keys()), k=1)
        meta = dict(kidx.get(best[0], {})) if best else {}

    # Fallbacks
    if not meta and re.match(r"^[A-Za-z0-9_]+__c$", alias_or_key or ""):
        meta = {"source":"sf","field": alias_or_key, "label": alias_or_key}
    if not meta:
        meta = {"source":"site_qual","key": alias_or_key, "label": alias_or_key}

    if meta.get("source") == "sf":
        f = meta.get("field","")
        lab = (sf_fields.get(_normalize(f)) or {}).get("label")
        if lab: meta["label"] = lab
    return meta
