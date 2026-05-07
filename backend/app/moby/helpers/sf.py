"""Salesforce-side utility helpers for Moby.

Extracted from `app.routers.ai_chat` (Phase 5b refactor).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List


def _describe_fields(sf, obj: str) -> set[str]:
    try:
        desc = sf.__getattr__(obj).describe()
        names = {f.get('name') for f in desc.get('fields', [])}
        return {n for n in names if n}
    except Exception:
        return set()


def _sf_escape_value(v: str) -> str:
    try:
        return str(v).replace("'", "\\'")
    except Exception:
        return ""


_AGGREGATION_INTENT_RE = re.compile(
    r"\b(total|how\s+many|sum|count|cu[aá]nto[s]?|cantidad)\b",
    re.I,
)

# Map of (user-text matcher, row-data key, label) for the columns Moby
# typically aggregates when the user asks for a total/sum/count.
_AGGREGATION_TARGETS: List[tuple] = [
    (re.compile(r"newly[\s\-]?diagnosed.*?(?:<\s*=?\s*18|under\s*18|u\s*18|pediatric|child)", re.I | re.S),
     "sf.C_Number_of_new_T1D_diagnosed_U_18__c", "Newly Diagnosed T1D &lt;18"),
    (re.compile(r"newly[\s\-]?diagnosed.*?(?:>\s*=?\s*18|over\s*18|o\s*18|adult)", re.I | re.S),
     "sf.C_Number_of_new_T1D_diagnosed_O_18__c", "Newly Diagnosed T1D ≥18"),
    (re.compile(r"\bnewly[\s\-]?diagnosed\b|\bnd\b", re.I),
     "sf.C_Number_of_new_T1D_diagnosed_O_18__c", "Newly Diagnosed T1D ≥18"),
    (re.compile(r"(?:t1d|type\s*1).{0,20}current.{0,20}(?:<\s*=?\s*18|under\s*18|pediatric|child)", re.I | re.S),
     "sf.C_Number_of_T1D_Patients_currently_U_18__c", "T1D Currently &lt;18"),
    (re.compile(r"(?:t1d|type\s*1).{0,20}current.{0,20}(?:>\s*=?\s*18|over\s*18|adult)", re.I | re.S),
     "sf.C_Number_of_T1D_Patients_currently_O_18__c", "T1D Currently ≥18"),
    (re.compile(r"\bstage\s*1\b", re.I),
     "sf.C_Number_of_Stage1_Individuals_followed__c", "Stage 1 individuals followed"),
    (re.compile(r"\bstage\s*2\b", re.I),
     "sf.C_Number_of_Stage2_Individuals_followed__c", "Stage 2 individuals followed"),
]


def _short_circuit_aggregation_lines(user_text: str, rows: List[Dict[str, Any]]) -> List[str]:
    """
    When the user asked for a total / how many / sum / count, scan the rows
    for the numeric columns referenced by the question and produce a short
    "Total X: N (across M sites with reported values)." sentence per match.

    Returns at most 2 lines to keep the answer compact.
    """
    if not rows or not _AGGREGATION_INTENT_RE.search(user_text or ""):
        return []
    seen_keys: set = set()
    out: List[str] = []
    for matcher, row_key, label in _AGGREGATION_TARGETS:
        if row_key in seen_keys:
            continue
        if not matcher.search(user_text):
            continue
        total = 0.0
        n_with_value = 0
        for r in rows:
            data = r.get("data") if isinstance(r, dict) else None
            v = (data or {}).get(row_key) if isinstance(data, dict) else r.get(row_key)
            if v is None:
                continue
            try:
                total += float(v)
                n_with_value += 1
            except (TypeError, ValueError):
                continue
        if n_with_value > 0:
            seen_keys.add(row_key)
            out.append(
                f"Total <strong>{label}</strong>: <strong>{int(total)}</strong> "
                f"(across {n_with_value} of {len(rows)} sites with reported values)."
            )
        if len(out) >= 2:
            break
    return out
