"""Pure-text helpers for Moby knowledge layer.

Extracted from `app.routers.ai_chat` (Phase 5b refactor). No external state.
"""
from __future__ import annotations

import re


def _normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[_\-\/:]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


# ------- Field name prettification helpers -------
def _smart_title(s: str) -> str:
    """Title-case but preserve acronyms like T1D, CS, PI and comparison signs."""
    if not s:
        return s
    out = s.title()
    # restore acronyms
    out = re.sub(r"\bT1d\b", "T1D", out)
    out = re.sub(r"\bPi\b", "PI", out)
    out = re.sub(r"\bCs\b", "CS", out)
    out = out.replace("> =", "≥").replace("&gt;=", "≥").replace(">=", "≥")
    return out


def _apply_common_rewrites(txt: str) -> str:
    """
    Normalize frequent Salesforce-style phrases into human-friendly text.
    E.g. 'C_Number_of_new_T1D_diagnosed_U_18__c' → 'Newly Diagnosed T1D <18'
    """
    t = txt or ""
    # remove double underscores and trailing __c
    t = re.sub(r"__c$", "", t)
    # Normalize separators
    t = t.replace("__", "_")
    t = re.sub(r"[_\s]+", " ", t).strip()

    # Frequent domain-specific rewrites (order matters)
    t = re.sub(r"\bNumber Of New T1d Diagnosed\b", "Newly Diagnosed T1D", t, flags=re.I)
    t = re.sub(r"\bNumber Of T1d Patients Currently\b", "T1D Patients Currently", t, flags=re.I)
    t = re.sub(r"\bNumber Of T1d Patients\b", "T1D Patients", t, flags=re.I)
    t = re.sub(r"\bStage\s*1\b", "Stage 1", t, flags=re.I)
    t = re.sub(r"\bStage\s*2\b", "Stage 2", t, flags=re.I)

    # Under/Over 18 → symbols
    t = re.sub(r"\b(U\s*[_ ]?\s*18|Under\s*18)\b", "<18", t, flags=re.I)
    t = re.sub(r"\b(O\s*[_ ]?\s*18|Over\s*18)\b", "≥18", t, flags=re.I)

    # Clean residual leading C / custom prefixes
    t = re.sub(r"^\s*C\s+", "", t)

    # Compact multiple spaces
    t = re.sub(r"\s{2,}", " ", t).strip()
    return _smart_title(t)


def _prettify_sf_field_name(core: str) -> str:
    """
    Heuristic prettifier for Salesforce API names when curated labels are not available.
    Keeps domain terms (T1D, Stage 1/2, <18, ≥18) readable.
    """
    if not core:
        return core
    # Remove object prefix when present (Account.)
    c = re.sub(r"^Account\.", "", core)
    # Strip __c and replace underscores with spaces first
    c = re.sub(r"__c$", "", c)
    c = c.replace("_", " ")
    return _apply_common_rewrites(c)
