"""
Moby Phase 1 + 2 — Structured Query Planner
============================================
Converts a free-text user query into a MobyPlan: a typed dict with structured
intent, resolved countries, extracted filters, column requests, and ambiguities.

Design principles:
  - Pure functions — no I/O, no DB, no LLM calls.
  - Always returns a valid plan (never raises).
  - Conservative: prefers "unresolved" over guessing.
  - Designed as a foundation for later phases (export, semantic indexing, etc.)

Usage in ai_chat.py:
  plan = parse_query_plan(text, kindex, payload.last_filters, last_table)
  if can_short_circuit(plan):
      filters = plan_to_filter_group(plan)
      ... call tool_explorer_search directly ...
  else:
      hint = plan_to_system_hint(plan)
      msgs.append({"role": "system", "content": hint})
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, TypedDict

from app.utils.country_norms import (
    _ALIAS_TO_ISO2,
    SORTED_ALIASES as _SORTED_ALIASES,
    resolve_countries as _resolve_countries,
)


# ── Typed structures ───────────────────────────────────────────────────────────

class FilterSpec(TypedDict):
    field: str      # Resolved canonical key, e.g. "sf.C_Number_of_Stage1_Individuals_followed__c"
    operator: str   # "equals" | ">" | ">=" | "<" | "<=" | "contains"
    value: Any      # Scalar or list
    raw_term: str   # Original text snippet that triggered this filter


class MobyPlan(TypedDict):
    intent: str                    # table_query | export_csv | chart | column_request | followup | other
    countries: List[str]           # ISO2 codes, e.g. ["IT", "ES"]
    filters: List[FilterSpec]      # Resolved filter specs (only known-field patterns)
    requested_columns: List[str]   # Resolved field keys explicitly mentioned by user
    output_mode: str               # table | chart | map | text
    needs_export: bool             # User explicitly asked for CSV/Excel export
    unresolved_terms: List[str]    # Terms that could not be mapped to a field (no guessing)
    confidence: float              # 0.0 – 1.0: how reliable the plan is
    raw_text: str                  # Original user input
    last_filters_reused: bool      # True when followup reuses last_filters context


# ── Country resolution ─────────────────────────────────────────────────────────
# _ALIAS_TO_ISO2, _SORTED_ALIASES, _resolve_countries imported from app.utils.country_norms


# ── Known-field filter extraction ─────────────────────────────────────────────

_STAGE_FIELDS = {
    "1": "sf.C_Number_of_Stage1_Individuals_followed__c",
    "2": "sf.C_Number_of_Stage2_Individuals_followed__c",
}
_ND_FIELD       = "sf.C_Number_of_new_T1D_diagnosed_O_18__c"
_PHARMACY_FIELD = "qual.3_6__is_your_pharmacy_on_site_or_off_campus"
_OVERNIGHT_FIELD = "qual.3_5_2__overnight_stay"

_OP_MAP = {">=": ">=", "<=": "<=", ">": ">", "<": "<", "=": "equals", "==": "equals"}


def _extract_filters(text: str) -> tuple[List[FilterSpec], List[str]]:
    """
    Extract filter rules for known INNODIA field patterns.
    Returns (resolved_filters, unresolved_terms).
    Unresolved terms are noted when a concept is mentioned but its qualifier
    is ambiguous (e.g. pharmacy without on-site/off-campus).
    """
    s = text.lower()
    filters: List[FilterSpec] = []
    unresolved: List[str] = []

    # Stage 1 / Stage 2 (optional comparator + value)
    for n in ("1", "2"):
        m = re.search(rf"\bstage\s*{n}\b(?:\s*(>=|<=|>|<|=)\s*(\d+))?", s)
        if m:
            op_raw = m.group(1) or ">"
            val    = int(m.group(2)) if m.group(2) else 0
            filters.append({
                "field":    _STAGE_FIELDS[n],
                "operator": _OP_MAP.get(op_raw, op_raw),
                "value":    val,
                "raw_term": m.group(0).strip(),
            })

    # ND / newly diagnosed (optional comparator + value)
    m_nd = re.search(r"\b(?:nd\b|newly[\s\-]?diagnosed|new\s+t1d\s+diagnos)", s, re.I)
    if m_nd:
        m_val = re.search(r"(?:nd|newly[\s\-]?diagnosed)\s*(>=|<=|>|<)\s*(\d+)", s, re.I)
        if m_val:
            op  = _OP_MAP.get(m_val.group(1), m_val.group(1))
            val = int(m_val.group(2))
        else:
            op, val = ">", 0
        filters.append({
            "field":    _ND_FIELD,
            "operator": op,
            "value":    val,
            "raw_term": m_nd.group(0).strip(),
        })

    # Pharmacy on-site (qualifier required; bare "pharmacy" → unresolved)
    if re.search(r"\bpharmac", s):
        if re.search(r"\bon[\s\-]?site\b|\bon\s+campus\b", s):
            filters.append({
                "field":    _PHARMACY_FIELD,
                "operator": "equals",
                "value":    "On-site",
                "raw_term": "pharmacy on-site",
            })
        else:
            unresolved.append("pharmacy (on-site or off-campus not specified)")

    # Overnight stay
    if re.search(r"\bovernight\b", s):
        filters.append({
            "field":    _OVERNIGHT_FIELD,
            "operator": "equals",
            "value":    "Yes",
            "raw_term": "overnight stay",
        })

    return filters, unresolved


# ── Phase 2: Catalog-based filter extraction ──────────────────────────────────
# Extends Phase 1 hardcoded extractors to cover the full kindex field catalog.
# Uses context patterns ("with X", "without X", "X > N") to infer operators.

# Field aliases that should NOT trigger catalog extraction (too generic / already handled)
_FIELD_STOP_ALIASES = frozenset([
    "site", "sites", "center", "centers", "account", "accounts",
    "country", "countries", "city", "cities", "name", "type", "date",
    "stage", "stage 1", "stage 2", "nd", "newly diagnosed",  # hardcoded above
    "pharmacy", "overnight",                                   # hardcoded above
    "all", "any", "some", "the", "that", "which", "show", "list", "find",
    "active", "total", "count", "number", "value", "data", "result",
])


def _infer_filter_from_context(
    alias: str,
    canonical_key: str,
    pre: str,
    post: str,
) -> Optional[FilterSpec]:
    """
    Infer filter operator from the surrounding context of a field alias match.
    pre:  text before the alias (up to 40 chars), lowercased.
    post: text after the alias (up to 40 chars), lowercased.
    Returns None if no clear filter context is detected.
    """
    # "without X" / "no X" / "sin X" → is_empty
    if re.search(r"\b(without|no\s+|sin\s+|sans\s+|ohne\s+|without\s+any)\s*$", pre):
        return {
            "field":    canonical_key,
            "operator": "is_empty",
            "value":    None,
            "raw_term": f"without {alias}",
        }

    # "X > N" / "X >= N" / "X < N" / "X <= N" / "X = N"
    m_op = re.search(r"^\s*(>=|<=|>|<|=)\s*(\d+(?:\.\d+)?)", post)
    if m_op:
        op  = _OP_MAP.get(m_op.group(1), m_op.group(1))
        raw = m_op.group(2)
        val: Any = float(raw) if "." in raw else int(raw)
        return {
            "field":    canonical_key,
            "operator": op,
            "value":    val,
            "raw_term": f"{alias} {m_op.group(0).strip()}",
        }

    # "X is available / active / present / yes / true / on-site"
    if re.search(
        r"^\s+(?:is|are|=)\s+(?:available|active|present|enabled|yes|true|on[\s\-]?site)\b",
        post,
    ):
        return {
            "field":    canonical_key,
            "operator": "is_not_empty",
            "value":    None,
            "raw_term": alias,
        }

    # "with X" / "having X" / "that have X" / "con X" / "including X" → is_not_empty
    if re.search(
        r"\b(?:with|having|that\s+have|que\s+tienen?|con|includ(?:ing|e))\s*$",
        pre,
    ):
        return {
            "field":    canonical_key,
            "operator": "is_not_empty",
            "value":    None,
            "raw_term": alias,
        }

    return None  # No clear filter context detected


def _extract_catalog_filters(
    text: str,
    kindex: Dict[str, Dict],
    skip_fields: set,
) -> tuple[List[FilterSpec], List[str]]:
    """
    Scan text for field mentions from the full kindex catalog (Phase 2).
    Complements _extract_filters() which only covers 4 hardcoded patterns.

    Args:
        text:        Original user query.
        kindex:      Field alias→meta dict from _build_knowledge_index().
        skip_fields: Canonical field keys already resolved by hardcoded extractors.

    Returns:
        (resolved_filters, unresolved_terms).
        Conservative: only adds a filter when context is unambiguous.
    """
    s = text.lower()
    filters: List[FilterSpec] = []
    matched_spans: List[tuple] = []

    # Sort aliases longest-first for greedy matching
    sorted_aliases = sorted(kindex.keys(), key=len, reverse=True)

    for alias in sorted_aliases:
        # Skip too-short or stop-word aliases
        if len(alias) < 5 or alias in _FIELD_STOP_ALIASES:
            continue

        meta = kindex[alias]
        src   = meta.get("source", "sf")
        field = meta.get("field") or meta.get("key", "")
        if not field:
            continue

        canonical_key = f"{src}.{field}"
        if canonical_key in skip_fields:
            continue

        # Word-boundary match in lowercased text
        m = re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", s)
        if not m:
            continue

        # Avoid overlapping with already-matched spans
        span = m.span()
        if any(a <= span[0] < b or a < span[1] <= b for a, b in matched_spans):
            continue
        matched_spans.append(span)

        pre  = s[max(0, span[0] - 40): span[0]]
        post = s[span[1]: min(len(s), span[1] + 40)]

        spec = _infer_filter_from_context(alias, canonical_key, pre, post)
        if spec:
            filters.append(spec)
            skip_fields = skip_fields | {canonical_key}  # prevent duplicate

    return filters, []  # catalog extractor never produces unresolved (conservative)


# ── Phase 2: Multi-turn context merge ─────────────────────────────────────────

def can_followup_merge(plan: MobyPlan, last_filters: Optional[Dict]) -> bool:
    """
    True when:
      - The plan intent is 'followup'
      - last_filters has actual rules to build on
      - The plan brings something new (countries or filters) to add
      - No unresolved terms (we don't want to merge when ambiguous)
    """
    if not last_filters or not last_filters.get("rules"):
        return False
    if plan.get("intent") != "followup":
        return False
    if plan.get("unresolved_terms"):
        return False
    return bool(plan.get("countries") or plan.get("filters"))


def merge_with_last_filters(plan: MobyPlan, last_filters: Dict) -> Dict[str, Any]:
    """
    Merge new countries/filters from the plan into the existing FilterGroup.

    Strategy:
      - New non-country filters are AND-combined with existing ones (no duplicate fields).
      - Country rules are OR-expanded: if existing has IT and plan adds ES →
        the country rules become an OR sub-group [IT, ES].
      - Already-present filters (same field) are not duplicated.
    """
    existing_rules = list(last_filters.get("rules", []))

    # Separate existing rules into types
    flat_country_rules = [
        r for r in existing_rules
        if isinstance(r.get("field"), str) and r["field"] == "site.country"
    ]
    existing_sub_groups = [r for r in existing_rules if isinstance(r.get("rules"), list)]
    other_rules = [
        r for r in existing_rules
        if isinstance(r.get("field"), str) and r["field"] != "site.country"
    ]

    # Check if there's an existing OR sub-group of country rules
    existing_or_country_group = next(
        (g for g in existing_sub_groups
         if g.get("logic") == "OR"
         and all(r.get("field") == "site.country" for r in g.get("rules", []))),
        None,
    )
    other_sub_groups = [g for g in existing_sub_groups if g is not existing_or_country_group]

    # Collect all existing country ISO2 values (from flat rules OR from OR sub-group)
    if existing_or_country_group:
        existing_iso2s = [r["value"] for r in existing_or_country_group.get("rules", [])]
    else:
        existing_iso2s = [r["value"] for r in flat_country_rules]

    # Merge new countries (deduplicated)
    new_iso2s = [c for c in plan.get("countries", []) if c not in existing_iso2s]
    all_iso2s = existing_iso2s + new_iso2s
    all_country_rules = [
        {"field": "site.country", "operator": "equals", "value": iso2}
        for iso2 in all_iso2s
    ]

    # Existing filter fields (to prevent duplicate rules)
    existing_fields = {r.get("field") for r in other_rules if r.get("field")}

    # New non-country filter rules from plan
    new_filter_rules = [
        {"field": f["field"], "operator": f["operator"], "value": f["value"]}
        for f in plan.get("filters", [])
        if f["field"] not in existing_fields
    ]

    # Build merged rule list
    merged: List[Dict] = other_rules + other_sub_groups + new_filter_rules

    if len(all_country_rules) > 1:
        merged.append({"logic": "OR", "rules": all_country_rules})
    elif all_country_rules:
        merged = all_country_rules + merged

    return {"logic": "AND", "rules": merged}


# ── Phase 2: Clarification templates ──────────────────────────────────────────
# Maps unresolved term pattern → structured clarification options.
# Used by ai_chat.py to return a clarify object instead of calling Claude.

CLARIFICATION_TEMPLATES: Dict[str, Dict] = {
    "pharmacy (on-site or off-campus not specified)": {
        "question": "Which pharmacy setting are you looking for?",
        "options": [
            {
                "label": "On-site pharmacy",
                "query": "pharmacy on-site",
            },
            {
                "label": "Off-campus (nearby) pharmacy",
                "query": "pharmacy off-campus",
            },
        ],
    },
}


def build_clarification(plan: MobyPlan) -> Optional[Dict[str, Any]]:
    """
    If all unresolved terms have known clarification templates, return a clarify
    response dict (to be returned directly by chat_api without calling Claude).
    Returns None if any term lacks a template, or if plan has no unresolved terms,
    or if the intent is not a data query.
    """
    unresolved = plan.get("unresolved_terms", [])
    if not unresolved:
        return None
    if plan.get("intent") not in ("table_query", "filter_search"):
        return None

    # All unresolved terms must have a known template to auto-clarify
    templates = [CLARIFICATION_TEMPLATES.get(u) for u in unresolved]
    if not all(templates):
        return None  # At least one unknown ambiguity → let Claude handle it

    # Use the first template (most common case: single ambiguity)
    tmpl = templates[0]
    return {
        "answer": "<p>I need one more detail to complete the search.</p>",
        "clarify": {
            "question":    tmpl["question"],
            "options":     tmpl["options"],
            "plan_intent": plan.get("intent"),
            "plan_countries": plan.get("countries", []),
            "plan_filters": [
                {"field": f["field"], "operator": f["operator"], "value": f["value"]}
                for f in plan.get("filters", [])
            ],
        },
    }


# ── Intent detection ──────────────────────────────────────────────────────────

_EXPORT_RE = re.compile(
    r"\b(export|csv|excel|download|descargar|exportar|descarga|bajar|xlsx)\b", re.I
)
_CHART_RE = re.compile(
    r"\b(chart|graph|bar|pie|line|visuali[sz]e|plot|gr[aá]fico|barras)\b", re.I
)
_MAP_RE = re.compile(r"\b(map|mapa|leaflet)\b", re.I)
_DATA_QUERY_RE = re.compile(
    r"\b(show|list|find|get|give\s+me|dame|muestra|busca|ver|see|all|which|display)\b"
    r".{0,60}"
    r"\b(sites?|centers?|centros?|accounts?|cuentas?)\b",
    re.I | re.S,
)
_COLUMN_REQ_RE = re.compile(
    r"\b(add\s+col|show\s+(also|me\s+also|the\s+col)|include\s+(field|col)|"
    r"with\s+the\s+col|a[ñn]ade?\s+col|tambi[eé]n\s+muestra)\b",
    re.I,
)
_FOLLOWUP_RE = re.compile(
    r"^(and\b|also\b|what\s+about\b|now\b|y\b|también\b|ahora\b|además\b|"
    r"the\s+same\b|these\b|those\b|for\s+those\b|de\s+esos?\b|para\s+esos?\b)",
    re.I,
)


def _detect_intent(text: str, has_last_table: bool, has_last_filters: bool) -> str:
    if _EXPORT_RE.search(text):
        return "export_csv"
    if _CHART_RE.search(text):
        return "chart"
    if _COLUMN_REQ_RE.search(text):
        return "column_request"
    if (has_last_table or has_last_filters) and _FOLLOWUP_RE.match(text.strip()):
        return "followup"
    if _DATA_QUERY_RE.search(text):
        return "table_query"
    # Any data-oriented query mentioning INNODIA entities
    s = text.lower()
    if re.search(
        r"\b(sites?|centers?|centros?|stage|nd\b|pharmacy|overnight|"
        r"assignment|country|countries|qualifier|qualification)\b",
        s,
    ):
        return "table_query"
    return "other"


def _detect_output_mode(text: str, intent: str) -> str:
    if _CHART_RE.search(text):
        return "chart"
    if _MAP_RE.search(text):
        return "map"
    if intent in ("table_query", "filter_search", "column_request", "export_csv", "followup"):
        return "table"
    return "text"


# ── Column extraction ─────────────────────────────────────────────────────────

_STOP_WORDS = frozenset(
    "the a an and or is are with for of in to from that column field "
    "columns fields all any some this that these those me also show "
    "sites site centers centre center centros clinical clinicos "
    "csv excel xlsx data datos file format".split()
)


def _extract_requested_columns(
    text: str,
    kindex: Dict[str, Dict],
) -> tuple[List[str], List[str]]:
    """
    Resolve explicit column/field requests from the query.
    Returns (resolved_field_keys, unresolved_terms).
    Uses kindex (alias→meta dict from _build_knowledge_index) for resolution.
    """
    resolved: List[str] = []
    unresolved: List[str] = []

    # Match patterns like "with stage1, stage2 and pharmacy columns"
    # or "show also X and Y" or "include field Z"
    m = re.search(
        r"\b(?:with|show(?:\s+(?:also|me))?|include|add|export|columns?:?|fields?:?|also\s+show)\s+"
        r"((?:[\w][\w\s\-]*?)(?:,\s*(?:(?:and|y)\s+)?[\w][\w\s\-]*?)*)"
        r"(?:\b(?:for|from|in|where|that|which|when|and\s+(?:which|that|where))\b"
        r"|[.!?]|$)",
        text,
        re.I,
    )
    if not m:
        return resolved, unresolved

    raw = m.group(1)
    candidates = re.split(r"[,;]|\s+and\s+|\s+y\s+|\s+e\s+|\s+of\s+", raw, flags=re.I)

    for cand in candidates:
        cand = cand.strip().strip(".,;:\"'")
        if not cand or cand.lower() in _STOP_WORDS:
            continue

        cand_lower = cand.lower()
        # Normalize to kindex key format (spaces/hyphens → underscores)
        norm = re.sub(r"[\s\-]+", "_", cand_lower)

        def _add(meta_key: str) -> None:
            meta  = kindex[meta_key]
            src   = meta.get("source", "sf")
            field = meta.get("field") or meta.get("key", meta_key)
            key   = f"{src}.{field}"
            if key not in resolved:
                resolved.append(key)

        if norm in kindex:
            _add(norm)
        elif cand_lower in kindex:
            # Exact match on the original (e.g. short alias "pi" in kindex)
            _add(cand_lower)
        elif len(cand) < 3:
            continue  # skip short unrecognised candidates
        else:
            # Substring search: candidate ∈ key/label, OR kindex key ∈ candidate
            hits = [
                k for k in kindex
                if cand_lower in k
                or cand_lower in (kindex[k].get("label") or "").lower()
                or (len(k) >= 2 and re.search(r"\b" + re.escape(k) + r"\b", cand_lower))
            ]
            if hits:
                _add(hits[0])
            else:
                unresolved.append(cand)

    return resolved, unresolved


# ── Confidence scoring ────────────────────────────────────────────────────────

def _score_confidence(
    intent: str,
    countries: List,
    filters: List,
    columns: List,
    unresolved: List,
) -> float:
    """
    Score 0.0–1.0 reflecting how actionable the plan is without LLM help.
    Conservative: penalty for each unresolved term.
    """
    if intent == "other":
        return 0.1

    score = 0.40  # base for any non-other intent

    if countries:
        score += 0.15
    if filters:
        score += 0.15
    if columns:
        score += 0.05
    if not unresolved:
        score += 0.10  # bonus: no ambiguities
    if intent in ("export_csv",) and (filters or countries):
        score += 0.10
    if intent == "table_query" and (filters or countries) and not unresolved:
        score += 0.10  # reliable enough to potentially short-circuit

    # Penalty per unresolved term
    score -= min(0.20, len(unresolved) * 0.05)

    return round(max(0.0, min(1.0, score)), 2)


# ── Main entry point ──────────────────────────────────────────────────────────

_FALLBACK: MobyPlan = {
    "intent": "other",
    "countries": [],
    "filters": [],
    "requested_columns": [],
    "output_mode": "text",
    "needs_export": False,
    "unresolved_terms": [],
    "confidence": 0.0,
    "raw_text": "",
    "last_filters_reused": False,
}


def parse_query_plan(
    text: str,
    kindex: Dict[str, Dict],
    last_filters: Optional[Dict] = None,
    last_table: Optional[Dict] = None,
) -> MobyPlan:
    """
    Build a MobyPlan from free text + field catalog.

    Args:
        text:         Raw user message text.
        kindex:       Field alias→meta dict from _build_knowledge_index().
        last_filters: The FilterGroup from the previous query (payload.last_filters).
        last_table:   The table from the previous response (payload.last_table).

    Returns:
        A MobyPlan TypedDict. Never raises — falls back to "other" on any error.
    """
    if not text or not text.strip():
        return dict(_FALLBACK, raw_text=text or "")  # type: ignore

    try:
        has_last_table   = bool(last_table and last_table.get("rows"))
        has_last_filters = bool(last_filters)

        countries                   = _resolve_countries(text)
        filters, filter_unresolved  = _extract_filters(text)

        # Phase 2: also scan the full kindex catalog for field mentions
        _skip = {f["field"] for f in filters}
        _cat_filters, _ = _extract_catalog_filters(text, kindex, _skip)
        filters = filters + _cat_filters

        col_resolved, col_unresolved = _extract_requested_columns(text, kindex)

        # Detect snake_case tokens in the query (look like field names) that
        # couldn't be resolved by any extractor. Conservative: only flag tokens
        # that contain at least one underscore (e.g. active_biobank, crispr_kit_available).
        _resolved_fields_set = {f["field"] for f in filters}
        for _tok_m in re.finditer(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text.lower()):
            _tok = _tok_m.group(0)
            if (
                any(_tok in rf for rf in _resolved_fields_set)
                or _tok in kindex
                or _tok in filter_unresolved
            ):
                continue
            filter_unresolved.append(_tok)

        intent      = _detect_intent(text, has_last_table, has_last_filters)
        output_mode = _detect_output_mode(text, intent)
        needs_export = bool(_EXPORT_RE.search(text))

        # Deduplicate unresolved terms preserving order
        unresolved = list(dict.fromkeys(filter_unresolved + col_unresolved))

        # For followups with no new filters, flag that context should be reused
        last_filters_reused = (
            intent == "followup" and has_last_filters and not filters
        )

        confidence = _score_confidence(intent, countries, filters, col_resolved, unresolved)

        # Detect proximity/location intent — these must NOT short-circuit via explorer_search
        has_proximity = bool(re.search(
            r"\b(near(?:est)?|clos(?:est|e\s+to)|within\s+\d+\s*km|proximity|cercano|próximo|nahe|vicino)\b",
            text, re.I
        ))

        return {
            "intent":              intent,
            "countries":           countries,
            "filters":             filters,
            "requested_columns":   col_resolved,
            "output_mode":         output_mode,
            "needs_export":        needs_export,
            "unresolved_terms":    unresolved,
            "confidence":          confidence,
            "raw_text":            text,
            "last_filters_reused": last_filters_reused,
            "has_proximity":       has_proximity,
        }

    except Exception:
        return dict(_FALLBACK, raw_text=text or "")  # type: ignore


# ── Utilities for consumers ───────────────────────────────────────────────────

def plan_to_system_hint(plan: MobyPlan) -> str:
    """
    Format a MobyPlan as a concise system-message hint for Claude.
    Returns "" if the plan has no useful content (intent=other or very low confidence).
    """
    if not plan or plan.get("intent") == "other" or plan.get("confidence", 0) < 0.30:
        return ""

    parts = [f"[QUERY PLAN — confidence {plan['confidence']:.0%}]"]
    parts.append(f"Intent: {plan['intent'].replace('_', ' ')}")

    if plan.get("output_mode") and plan["output_mode"] != "text":
        parts.append(f"Output mode: {plan['output_mode']}")

    if plan.get("needs_export"):
        parts.append("User wants CSV export.")

    if plan.get("countries"):
        parts.append(f"Countries (ISO2): {', '.join(plan['countries'])}")

    if plan.get("filters"):
        flines = [
            f"  • {f['field']} {f['operator']} {f['value']!r}"
            for f in plan["filters"]
        ]
        parts.append("Resolved filters:\n" + "\n".join(flines))

    if plan.get("requested_columns"):
        parts.append(f"Requested columns: {', '.join(plan['requested_columns'])}")

    if plan.get("unresolved_terms"):
        parts.append(
            "Unresolved terms (do NOT guess — acknowledge as ambiguous or ask): "
            + ", ".join(plan["unresolved_terms"])
        )

    if plan.get("last_filters_reused"):
        parts.append("Previous query filters still in context (last_filters). Reuse and refine.")

    return "\n".join(parts)


def can_short_circuit(plan: MobyPlan) -> bool:
    """
    True if the plan is reliable enough to execute directly via explorer_search,
    bypassing the LLM entirely.

    Phase 1 threshold is conservative:
      - High confidence (≥ 0.80)
      - Intent is table_query
      - At least one filter OR at least one country resolved
      - No unresolved terms
      - Table output (not chart/map)
      - No proximity/location intent (those need nearest_filtered_sites, not explorer_search)
    """
    # Proximity queries must go through the agentic loop — explorer_search doesn't do distance
    if plan.get("has_proximity"):
        return False
    return (
        plan.get("confidence", 0) >= 0.80
        and plan.get("intent") == "table_query"
        and (len(plan.get("filters", [])) > 0 or len(plan.get("countries", [])) > 0)
        and len(plan.get("unresolved_terms", [])) == 0
        and plan.get("output_mode") == "table"
    )


def plan_to_filter_group(plan: MobyPlan) -> Dict[str, Any]:
    """
    Convert a MobyPlan into a FilterGroup dict compatible with POST /api/explorer/search.
    Multiple countries are nested as an OR sub-group; everything else is AND-combined.
    """
    filter_rules: List[Dict] = [
        {"field": f["field"], "operator": f["operator"], "value": f["value"]}
        for f in plan.get("filters", [])
    ]

    country_rules: List[Dict] = [
        {"field": "site.country", "operator": "equals", "value": iso2}
        for iso2 in plan.get("countries", [])
    ]

    if len(country_rules) > 1:
        # Multiple countries → OR sub-group combined with AND for other filters
        rules: List = filter_rules + [{"logic": "OR", "rules": country_rules}]
        logic = "AND"
    elif country_rules:
        rules = country_rules + filter_rules
        logic = "AND"
    else:
        rules = filter_rules
        logic = "AND"

    return {"logic": logic, "rules": rules}


# ── Phase 4: FilterGroup validation ────────────────────────────────────────────

_VALID_OPERATORS: frozenset = frozenset({
    "equals", "not_equals", "eq", "ne",
    ">", ">=", "<", "<=",
    "contains", "not_contains", "starts_with", "ends_with",
    "in", "not_in",
    "is_empty", "is_not_empty",
    "is_null", "is_not_null",  # synonyms for is_empty/is_not_empty
    "between",
})

_VALID_FIELD_PREFIXES = ("site.", "sf.", "qual.", "account_name", "country", "city", "extra.")

# Operators that don't require a value
_VALUELESS_OPERATORS = frozenset({"is_empty", "is_not_empty", "is_null", "is_not_null"})


def validate_filter_group(fg: Any, depth: int = 0) -> List[str]:
    """
    Validate a FilterGroup dict before sending to /api/explorer/search.

    Returns a list of error strings. An empty list means valid.

    Validated aspects:
    - logic is "AND", "OR", or a numbered expression like "1 AND (2 OR 3)"
    - each rule is a dict with field, operator, value
    - field uses a recognized prefix (site.*, sf.*, qual.*, extra.*)
    - operator is a known operator keyword
    - value is non-empty unless operator is is_empty/is_not_empty
    - nested sub-groups (rules containing {logic, rules}) are recursively validated
    - depth capped at 5 to prevent infinite recursion

    Pure function — no I/O, no DB, no LLM calls.
    """
    if depth > 5:
        return ["FilterGroup too deeply nested (max depth 5)"]
    if not isinstance(fg, dict):
        return [f"FilterGroup must be a dict, got {type(fg).__name__}"]

    errors: List[str] = []

    # Validate logic field
    logic = fg.get("logic", "AND")
    if not isinstance(logic, str):
        errors.append(f"logic must be a string, got {type(logic).__name__}")
    elif logic not in ("AND", "OR") and not re.search(r"\d", logic):
        errors.append(
            f"logic must be 'AND', 'OR', or a numbered expression like '1 AND (2 OR 3)', got: {logic!r}"
        )

    # Validate rules list
    rules = fg.get("rules", [])
    if not isinstance(rules, list):
        errors.append("rules must be a list")
        return errors

    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"rule[{i}] must be a dict")
            continue

        # Sub-group: has "rules" key → recurse
        if "rules" in rule:
            sub_errors = validate_filter_group(rule, depth + 1)
            errors.extend([f"rule[{i}].{e}" for e in sub_errors])
            continue

        # Leaf rule: field, operator, value
        field = rule.get("field")
        operator = rule.get("operator") or rule.get("op")
        value = rule.get("value")

        if not field:
            errors.append(f"rule[{i}] missing 'field'")
        elif not any(str(field).startswith(p) for p in _VALID_FIELD_PREFIXES):
            errors.append(
                f"rule[{i}] unrecognized field prefix: {field!r} "
                f"(expected site.*, sf.*, qual.*, extra.*)"
            )

        if not operator:
            errors.append(f"rule[{i}] missing 'operator'")
        elif operator not in _VALID_OPERATORS:
            errors.append(f"rule[{i}] unknown operator: {operator!r}")

        needs_value = operator not in _VALUELESS_OPERATORS if operator else True
        if needs_value and (value is None or value == ""):
            errors.append(
                f"rule[{i}] (field={field!r}, op={operator!r}) has empty/null value "
                f"but this operator requires one"
            )

    return errors
