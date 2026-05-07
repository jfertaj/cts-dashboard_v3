"""Tiny shared text/id helpers for Moby.

Extracted from `app.routers.ai_chat` (Phase 5b refactor).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional


def _first_account_id_from_table(table: Optional[Dict[str, Any]]) -> Optional[str]:
    if not table or not isinstance(table.get("rows"), list):
        return None
    for r in table["rows"]:
        aid = r.get("account_id") or r.get("sf.Account.Id") or r.get("sf.AccountId") or r.get("Account.Id")
        if aid:
            return str(aid)


def _is_valid_sf_id(s: Optional[str], prefix: Optional[str] = None) -> bool:
    """Simple validator for Salesforce 15/18-char Ids (optionally enforce prefix)."""
    if not s or not isinstance(s, str):
        return False
    if not re.match(r"^[0-9A-Za-z]{15}(?:[0-9A-Za-z]{3})?$", s):
        return False
    if prefix and not s.startswith(prefix):
        return False
    return True


def _clean_text(s: str) -> str:
    """Strip artefacts like literal '<table>' from assistant text."""
    return re.sub(r"\s*<table>\s*", "", s or "").strip()
