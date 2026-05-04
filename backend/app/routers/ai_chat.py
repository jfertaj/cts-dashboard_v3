# app/routers/ai_chat.py
import os, sys

# Ensure "backend" package is importable whether run from repo root (tests)
# or from backend/ dir (uvicorn).
_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_repo_root = os.path.dirname(_backend_dir)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import List, Literal, Optional, Any, Dict, Tuple
import json, re, math
import threading
import queue as _std_queue
import httpx
import unicodedata
from sqlalchemy import text
from sqlalchemy.orm import Session


# DB
from app.database import get_db
# Salesforce (tu helper de sesión)
from app.salesforce import get_sf_client
# Extras SF (PI, flags CS, assignments, newDx…)
from app.routers.salesforce_extras import _account_extras_core
# Account map/enrichment helper (name, city, country, coords, active filtering)
from app.routers.salesforce_explorer import _build_account_map

from app.services.sf_labels import humanize_headers
from app.utils.soql_helpers import build_followup_accounts_query
from app.utils.country_norms import _ALIAS_TO_ISO2 as _CN_ALIAS, iso2_to_sf_name as _iso2_to_sf
import anthropic as _anthropic_sdk
from app.routers.moby_planner import (
    parse_query_plan,
    plan_to_system_hint,
    can_short_circuit,
    plan_to_filter_group,
    can_followup_merge,
    merge_with_last_filters,
    build_clarification,
    validate_filter_group,
)
from app.routers.moby_handlers import (
    HandlerContext as _HandlerContext,
    ActivityTools as _ActivityTools,
    handle_followup_activity_sponsor,
    handle_multi_activity_intersection,
    handle_assignment_sites,
    handle_activity,
)

DEBUG = os.environ.get("AI_CHAT_DEBUG", "0") == "1"
INDEX_REFRESH_SEC = int(os.environ.get("AI_INDEX_REFRESH_SEC", "600"))
FIELDS_SF_JSON_PATH = os.environ.get("FIELDS_SF_JSON_PATH", "app/config/fields_opportunity_curated.json")
QUAL_ALIAS_JSON_PATH = os.environ.get("QUAL_ALIAS_JSON_PATH", "app/config/qualification_aliases.json")
EXPLORER_DRIVE_KM_PATH = "/api/explorer/search/within-drive-km"
EXPLORER_SEARCH_PATH   = "/api/explorer/search"
# Max user turns to keep in history before truncating (each turn = 1 user + 1 assistant message).
# Keeps context focused and prevents unbounded token growth in long conversations.
MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "12"))
# Token budget for extended thinking on complex queries.
# The model may use up to this many tokens to reason before responding.
# max_tokens is automatically raised to budget + 8192 when thinking is enabled.
CLAUDE_THINKING_BUDGET = int(os.environ.get("CLAUDE_THINKING_BUDGET", "8000"))
# Agentic loop limits
MOBY_MAX_AGENT_TURNS = int(os.getenv("MOBY_MAX_AGENT_TURNS", "3"))
MAX_TOOL_RESULT_TOKENS = int(os.getenv("MAX_TOOL_RESULT_TOKENS", "4000"))
MOBY_AGENT_TIMEOUT_S = int(os.getenv("MOBY_AGENT_TIMEOUT_S", "30"))
# Thread-local used by the /chat/stream endpoint to receive token chunks
# from _claude_chat without changing any function signatures.
_STREAM_Q: threading.local = threading.local()

def _dbg(msg: str, *args):
    if DEBUG:
        try:
            print("[AI-CHAT]", msg % args if args else msg)
        except Exception:
            print("[AI-CHAT]", msg)


def _first_account_id_from_table(table: Optional[Dict[str, Any]]) -> Optional[str]:
    if not table or not isinstance(table.get("rows"), list):
        return None
    for r in table["rows"]:
        aid = r.get("account_id") or r.get("sf.Account.Id") or r.get("sf.AccountId") or r.get("Account.Id")
        if aid: return str(aid)

# Simple validator for Salesforce 15/18-char Ids (optionally enforce Account prefix '001')
import re as _re_id

def _is_valid_sf_id(s: Optional[str], prefix: Optional[str] = None) -> bool:
    if not s or not isinstance(s, str):
        return False
    if not _re_id.match(r"^[0-9A-Za-z]{15}(?:[0-9A-Za-z]{3})?$", s):
        return False
    if prefix and not s.startswith(prefix):
        return False
    return True

router = APIRouter(prefix="/api/ai", tags=["AI"])

def _clean_text(s: str) -> str:
    # Elimina restos como "<table>" del texto del asistente
    return re.sub(r"\s*<table>\s*", "", s or "").strip()

# ====== Tipos ======
Role = Literal["system","user","assistant","tool"]
class ChatMessage(BaseModel):
    role: Role
    content: str
    tool_name: Optional[str] = None  # solo para compat interna; OpenAI ignora esto

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    stream: bool = False
    last_table: Optional[Dict[str, Any]] = None    # Tabla de la respuesta anterior para follow-ups eficientes
    last_filters: Optional[Dict[str, Any]] = None  # FilterGroup que produjo la tabla anterior (para follow-ups con explorer_search)

# ====== Whitelists / Guards ======

# CTEs permitidos (usando el WITH) — importante para queries rank_sites_by_group
ALLOWED_CTES = {"scored", "q", "site_rows", "ranked"}

ALLOWED_TABLES = {
    "public.sites", "sites",
    "public.site_qual", "site_qual",
    # profiling_kv excluded: table is empty (sync not yet wired to startup).
    # Kept in rank_sites internal path but blocked from Claude-generated SQL
    # to avoid confusing 0-row results. Re-add when sync is active.
    "public.questionnaires", "questionnaires",
    "public.sections", "sections",
    "public.questions", "questions",
    "public.responses", "responses",
    "public.geonames_cities", "geonames_cities",
    "public.vw_site_metrics", "vw_site_metrics",
}


# ========= Índice unificado de métricas (SF + site_qual) =========

from time import time, monotonic as _monotonic

_INDEX_CACHE: Dict[str, Any] = {"ts": 0, "index": {}, "sf_fields": {}}
# Limit for previewing aliases in the system hints (smaller → faster)
INDEX_PREVIEW_LIMIT = int(os.environ.get("AI_INDEX_PREVIEW_LIMIT", "60"))

# Country alias map: lowercase alias → (SF full name, ISO2 code)
# Used by multiple planner handlers (pharmacy, country, nearest)
# _COUNTRY_MAP: alias → (sf_display_name, iso2)
# Derived from the canonical country_norms module — do not edit here.
_COUNTRY_MAP: Dict[str, tuple] = {
    alias: (_iso2_to_sf(iso2), iso2)
    for alias, iso2 in _CN_ALIAS.items()
}

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
    out = out.replace("> =","≥").replace("&gt;=","≥").replace(">=","≥")
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

def _load_sf_fields() -> Dict[str, Dict[str, str]]:
    """
    Devuelve: { normalized_alias: {"source":"sf","field":<api name>,"label":<label>} ... }
    Lee fields_opportunity_curated.json y crea sinónimos (label, key y variantes).
    """
    out: Dict[str, Dict[str, str]] = {}
    try:
        with open(FIELDS_SF_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        _dbg("WARN: cannot load %s: %s", FIELDS_SF_JSON_PATH, e)
        return out

    def add(alias: str, field: str, label: str):
        if not alias or not field:
            return
        out.setdefault(alias, {"source":"sf","field": field, "label": label})

    # Formatos esperados: [{"key":"C_Number_of_T1D...","label":"T1D Patients <18", ...}, ...]
    if isinstance(data, list):
        for it in data:
            key = (it.get("key") or "").strip()
            label = (it.get("label") or it.get("name") or key).strip()
            if not key:
                continue
            # alias principales
            add(_normalize(label), key, label)
            add(_normalize(key), key, label)
            # variantes sencillas
            label_alt = re.sub(r"[^a-zA-Z0-9 ]+", " ", label)
            add(_normalize(label_alt), key, label)
            key_alt = re.sub(r"__c$", "", key)
            add(_normalize(key_alt), key, label)
    return out

def _introspect_site_qual_keys(db: Session, limit: int = 500) -> Dict[str, Dict[str, str]]:
    """
    Devuelve: { normalized_alias: {"source":"site_qual","key":<jsonb key>} ... }
    Mira jsonb_object_keys(data) y añade alias normalizados.
    """
    out: Dict[str, Dict[str, str]] = {}
    sql = (
        "SELECT key, COUNT(*) AS cnt FROM ("
        "  SELECT jsonb_object_keys(data) AS key FROM public.site_qual"
        ") t GROUP BY key ORDER BY cnt DESC LIMIT :lim"
    )
    try:
        res = db.execute(text(sql), {"lim": limit})
        for key, _cnt in res.fetchall():
            if not key:
                continue
            base = _normalize(key)
            out.setdefault(base, {"source":"site_qual","key": key})
            # variante sin prefijos largos si los hubiera
            short = re.sub(r"^C[_\-]+", "", key)
            out.setdefault(_normalize(short), {"source":"site_qual","key": key})
    except Exception as e:
        _dbg("WARN: site_qual introspection failed: %s", e)
    return out

def _introspect_profiling_kv_keys(db: Session, limit: int = 500) -> Dict[str, Dict[str, str]]:
    """
    Devuelve: { normalized_alias: {"source":"profiling_kv","key":<kv key>} ... }
    Basado en DISTINCT key de profiling_kv.
    """
    out: Dict[str, Dict[str, str]] = {}
    sql = "SELECT key, COUNT(*) FROM public.profiling_kv GROUP BY key ORDER BY COUNT(*) DESC LIMIT :lim"
    try:
        res = db.execute(text(sql), {"lim": limit})
        for key, _cnt in res.fetchall():
            if not key:
                continue
            base = _normalize(key)
            out.setdefault(base, {"source":"profiling_kv","key": key})
            short = re.sub(r"^C[_\-]+", "", key)
            out.setdefault(_normalize(short), {"source":"profiling_kv","key": key})
    except Exception as e:
        _dbg("WARN: profiling_kv introspection failed: %s", e)
    return out

def _load_qual_aliases() -> Dict[str, str]:
    """Lee qualification_aliases.json si existe. Formato esperado:
    { "alias string": "JSONB_key", ... } o { "aliases": [{"alias":"...","key":"..."}, ...] }
    Devuelve dict alias_normalizado -> key
    """
    out: Dict[str, str] = {}
    try:
        with open(QUAL_ALIAS_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "aliases" not in data:
            for a, k in data.items():
                if a and k:
                    out[_normalize(str(a))] = str(k)
        elif isinstance(data, dict) and isinstance(data.get("aliases"), list):
            for it in data["aliases"]:
                alias = _normalize(str(it.get("alias") or ""))
                key = str(it.get("key") or "")
                if alias and key:
                    out[alias] = key
    except Exception as e:
        _dbg("WARN: cannot load qual aliases %s: %s", QUAL_ALIAS_JSON_PATH, e)
    return out


def _build_knowledge_index(db: Session) -> Dict[str, Any]:
    """
    Funde SF (curated) + site_qual. Si hay duplicidad de concepto, preferimos SF.
    """
    now = time()
    if (now - _INDEX_CACHE.get("ts", 0)) < INDEX_REFRESH_SEC and _INDEX_CACHE.get("index"):
        return _INDEX_CACHE

    sf_map = _load_sf_fields()  # normalized_alias -> {source:"sf", field, label}
    sq_map = _introspect_site_qual_keys(db)  # normalized_alias -> {source:"site_qual", key}
    prof_map = _introspect_profiling_kv_keys(db)  # normalized_alias -> {source:"profiling_kv", key}

    # Aliases curados de Qualification (alias -> key)
    qual_aliases = _load_qual_aliases()

    fused: Dict[str, Dict[str, str]] = {}
    # Primero metemos profiling y site_qual (warehouse)
    fused.update(prof_map)
    fused.update(sq_map)
    # Añadimos aliases curados para site_qual
    for alias, key in qual_aliases.items():
        fused[alias] = {"source":"site_qual","key": key}
    # Luego SF pisa (prioridad)
    for k, v in sf_map.items():
        fused[k] = v

    # Manual fallbacks (robust aliases) — NLP glossary: acronyms, abbreviations, domain terms
    # ND = Newly Diagnosed (T1D patients diagnosed last year)
    # T1D = Type 1 Diabetes
    # CTS = Clinical Trial Site
    # CS = Clinical Site
    # DxLab = Diagnostic Lab
    # Stage 1/2 = Pre-symptomatic T1D stages
    # PI = Principal Investigator
    # SC = Study Coordinator / Study Nurse
    # HLA = Human Leukocyte Antigen
    # PAC = Patient Advisory Council
    # RP = Referral Partner
    # IMP = Investigational Medicinal Product
    # GCP = Good Clinical Practice
    # MCA = Multi-Centre Agreement
    _nd_u18 = {"source": "sf", "field": "C_Number_of_new_T1D_diagnosed_U_18__c", "label": "New T1D diagnosed <18 (last year)"}
    _nd_o18 = {"source": "sf", "field": "C_Number_of_new_T1D_diagnosed_O_18__c", "label": "New T1D diagnosed ≥18 (last year)"}
    _cu18 = {"source": "sf", "field": "C_Number_of_T1D_Patients_currently_U_18__c", "label": "T1D patients currently <18"}
    _co18 = {"source": "sf", "field": "C_Number_of_T1D_Patients_currently_O_18__c", "label": "T1D patients currently ≥18"}
    _s1 = {"source": "sf", "field": "C_Number_of_Stage1_Individuals_followed__c", "label": "Stage 1 individuals followed"}
    _s2 = {"source": "sf", "field": "C_Number_of_Stage2_Individuals_followed__c", "label": "Stage 2 individuals followed"}
    _hla = {"source": "sf", "field": "C_Is_HLA_typing_performed__c", "label": "HLA typing performed"}
    _screened = {"source": "sf", "field": "C_Number_of_Individuals_screened_intotal__c", "label": "Individuals screened (total)"}
    _pi = {"source": "sf", "field": "C_Principal_Investigator__c", "label": "Principal Investigator"}
    _sc = {"source": "sf", "field": "C_Lead_Study_Coordinator_SC__c", "label": "Lead Study Coordinator (SC)"}
    manual_sf = {
        # Stage aliases
        _normalize("Stage 2"): _s2,
        _normalize("Stage 1"): _s1,
        _normalize("Stage I"): _s1,
        _normalize("Stage II"): _s2,
        _normalize("pre-symptomatic stage 1"): _s1,
        _normalize("pre-symptomatic stage 2"): _s2,
        _normalize("presymptomatic stage 1"): _s1,
        _normalize("presymptomatic stage 2"): _s2,
        # API name aliases
        _normalize("C_Number_of_Stage2_Individuals_followed__c"): _s2,
        _normalize("C_Number_of_Stage1_Individuals_followed__c"): _s1,
        _normalize("C_Number_of_new_T1D_diagnosed_U_18__c"): _nd_u18,
        _normalize("C_Number_of_new_T1D_diagnosed_O_18__c"): _nd_o18,
        _normalize("C_Number_of_T1D_Patients_currently_U_18__c"): _cu18,
        _normalize("C_Number_of_T1D_Patients_currently_O_18__c"): _co18,
        # HLA
        _normalize("HLA typing"): _hla,
        _normalize("HLA"): _hla,
        _normalize("HLA typing performed"): _hla,
        # Current T1D patients under care
        _normalize("patients under 18"): _cu18,
        _normalize("patients over 18"): _co18,
        _normalize("patients below 18"): _cu18,
        _normalize("patients above 18"): _co18,
        _normalize("T1D patients under 18"): _cu18,
        _normalize("T1D patients over 18"): _co18,
        _normalize("T1D patients <18"): _cu18,
        _normalize("T1D patients >=18"): _co18,
        _normalize("T1D patients currently under 18"): _cu18,
        _normalize("T1D patients currently over 18"): _co18,
        _normalize("current T1D patients under 18"): _cu18,
        _normalize("current T1D patients over 18"): _co18,
        _normalize("currently under 18"): _cu18,
        _normalize("currently over 18"): _co18,
        _normalize("current patients under 18"): _cu18,
        _normalize("current patients over 18"): _co18,
        _normalize("patients under care"): _co18,  # default to ≥18 when not specified
        _normalize("T1D patients"): _co18,
        # ND = Newly Diagnosed aliases
        _normalize("ND"): _nd_o18,  # default ND without age → ≥18
        _normalize("nd"): _nd_o18,
        _normalize("ND patients"): _nd_o18,
        _normalize("ND <18"): _nd_u18,
        _normalize("ND under 18"): _nd_u18,
        _normalize("ND below 18"): _nd_u18,
        _normalize("ND over 18"): _nd_o18,
        _normalize("ND >=18"): _nd_o18,
        _normalize("ND above 18"): _nd_o18,
        _normalize("ND juvenil"): _nd_u18,
        _normalize("ND adulto"): _nd_o18,
        _normalize("newly diagnosed"): _nd_o18,  # default → ≥18
        _normalize("newly diagnosed patients"): _nd_o18,
        _normalize("newly diagnosed under 18"): _nd_u18,
        _normalize("newly diagnosed over 18"): _nd_o18,
        _normalize("newly diagnosed below 18"): _nd_u18,
        _normalize("newly diagnosed above 18"): _nd_o18,
        _normalize("newly diagnosed <18"): _nd_u18,
        _normalize("newly diagnosed >=18"): _nd_o18,
        _normalize("new T1D diagnoses"): _nd_o18,
        _normalize("new T1D diagnoses under 18"): _nd_u18,
        _normalize("new T1D diagnoses over 18"): _nd_o18,
        _normalize("new T1D diagnosed"): _nd_o18,
        _normalize("new T1D diagnosed under 18"): _nd_u18,
        _normalize("new T1D diagnosed over 18"): _nd_o18,
        _normalize("new T1D <18"): _nd_u18,
        _normalize("new T1D >=18"): _nd_o18,
        _normalize("diagnoses last year"): _nd_o18,
        _normalize("diagnosed last year"): _nd_o18,
        _normalize("diagnosed last year under 18"): _nd_u18,
        _normalize("diagnosed last year over 18"): _nd_o18,
        _normalize("recien diagnosticados"): _nd_o18,
        _normalize("recién diagnosticados"): _nd_o18,
        _normalize("nuevos diagnosticados"): _nd_o18,
        # Screened
        _normalize("screened"): _screened,
        _normalize("individuals screened"): _screened,
        _normalize("total screened"): _screened,
        _normalize("screened total"): _screened,
        _normalize("screening"): _screened,
        # PI / SC
        _normalize("PI"): _pi,
        _normalize("principal investigator"): _pi,
        _normalize("investigator principal"): _pi,
        _normalize("SC"): _sc,
        _normalize("study coordinator"): _sc,
        _normalize("lead study coordinator"): _sc,
    }
    for a, meta in manual_sf.items():
        fused[a] = meta
    manual_qual = {
        _normalize("onsite pharmacy"): {"source": "site_qual", "key": "3_6__is_your_pharmacy_on_site_or_off_campus"},
    }
    for a, meta in manual_qual.items():
        fused[a] = meta

    _INDEX_CACHE["ts"] = now
    _INDEX_CACHE["index"] = fused
    _INDEX_CACHE["sf_fields"] = sf_map
    _dbg("INDEX built: %d aliases (sf=%d, sq=%d, prof=%d, qual_alias=%d)", len(fused), len(sf_map), len(sq_map), len(prof_map), len(qual_aliases))
    return _INDEX_CACHE

def _semantic_top_matches(q: str, aliases: List[str], k: int) -> Optional[List[str]]:
    """
    Optional semantic resolver path — gated by MOBY_SEMANTIC_FIELD_RESOLVER=1.

    Returns None when disabled or when the resolver fails for any reason
    (missing index, Bedrock unavailable, exception). Caller falls back to the
    legacy lexical matcher.
    """
    if os.getenv("MOBY_SEMANTIC_FIELD_RESOLVER", "0") != "1":
        return None
    try:
        from app.moby_semantic_resolver import SemanticFieldResolver
        results = SemanticFieldResolver().resolve(q, top_n=k)
    except Exception as exc:  # incl. BedrockUnavailable, IndexUnavailable
        _dbg("semantic resolver fallback: %s", exc)
        return None
    if not results:
        return None
    alias_set = set(aliases)
    out: List[str] = []
    for r in results:
        # Match against the alias list the legacy code passes in. Prefer the
        # normalized label, fall back to the key.
        for cand in (_normalize(r.get("label") or ""), _normalize(r.get("key") or "")):
            if cand and cand in alias_set and cand not in out:
                out.append(cand)
                break
        if len(out) >= k:
            break
    return out or None


def _format_hints_block(hits: List[Dict[str, Any]]) -> str:
    """Render semantic field hits as a markdown bullet list for the system prompt."""
    lines = [
        "## Relevant fields (semantic hint, top-K=8)",
        "The following Salesforce fields are most semantically relevant to "
        "the user's question. Prefer these when calling tools, especially "
        "for multilingual queries.",
        "",
    ]
    for h in hits:
        key = h.get("key") or ""
        label = h.get("label") or ""
        lines.append(f"- `{key}` — {label}")
    return "\n".join(lines)


def _semantic_field_hints(user_msg: str) -> str:
    """Return a markdown block of top-K semantic field hits for system-prompt injection.

    Gated by MOBY_SEMANTIC_FIELD_RESOLVER=1. Any failure (missing index,
    Bedrock unavailable, exception) returns "" — no regression vs. baseline.
    """
    if os.getenv("MOBY_SEMANTIC_FIELD_RESOLVER", "0") != "1":
        return ""
    if not user_msg or not user_msg.strip():
        return ""
    t0 = _monotonic()
    try:
        from app.moby_semantic_resolver import SemanticFieldResolver
        hits = SemanticFieldResolver().resolve(user_msg, top_n=8)
        ms = (_monotonic() - t0) * 1000.0
        keys_preview = [h.get("key") for h in hits]
        print(f"[moby-semantic] q={user_msg[:80]!r} top={keys_preview} latency={ms:.0f}ms", flush=True)
        if not hits:
            return ""
        return _format_hints_block(hits)
    except Exception as exc:
        ms = (_monotonic() - t0) * 1000.0
        print(f"[moby-semantic] q={user_msg[:80]!r} ERROR={exc!r} latency={ms:.0f}ms", flush=True)
        return ""


def _top_matches(q: str, aliases: List[str], k: int = 5) -> List[str]:
    """
    Matching ligero: intersección de tokens normalizados + prefiero substrings.
    """
    # unused: see Option A integration in chat_api (_semantic_field_hints).
    # The agentic loop bypasses _top_matches; semantic guidance is injected
    # into the system prompt instead. Kept here behind a no-op to avoid the
    # false signal of "semantic resolver returned nothing useful".
    # sem = _semantic_top_matches(q, aliases, k)
    # if sem:
    #     return sem
    qn = _normalize(q)
    qtokens = set(qn.split())
    scored = []
    for a in aliases:
        atoks = set(a.split())
        inter = len(qtokens & atoks)
        bonus = 1 if a in qn or qn in a else 0
        score = inter * 2 + bonus
        if score > 0:
            scored.append((score, a))
    scored.sort(reverse=True)
    return [a for _, a in scored[:k]]


def _extract_structured(content: str) -> Dict[str, Any]:
    """
    Si el modelo pegó 'table' o 'visualization' en el texto (sin tools),
    parseamos esos JSON y los devolvemos en claves separadas.
    """
    out: Dict[str, Any] = {"answer": content or ""}
    if not content:
        return out
    
    # CASO ESPECIAL: Si el contenido es JSON puro, parsearlo directamente
    if content.strip().startswith('{') and content.strip().endswith('}'):
        try:
            parsed = json.loads(content.strip())
            if isinstance(parsed, dict):
                # Si es un dict con answer/table/visualization, usarlo directamente
                if 'answer' in parsed or 'table' in parsed or 'visualization' in parsed:
                    _dbg("Detected pure JSON response from model")
                    return parsed
        except:
            pass  # No es JSON válido, continuar con el parsing normal

    def _pull(tag: str) -> Optional[str]:
        # patrones: **table**: {json}  |  table: {json}  |  **visualization**: {json}
        pat = rf"(?:\*\*{tag}\*\*|{tag})\s*:\s*(\{{.*\}})"
        m = re.search(pat, content, flags=re.I | re.S)
        return m.group(1) if m else None

    # Quita artefactos tipo "<table> ... </table>" y otros tags XML/HTML pegados por el modelo
    content = re.sub(r"(?is)</?table[^>]*>", "", content or "")
    content = re.sub(r"(?is)<columns?>.*?</columns?>", "", content or "")
    content = re.sub(r"(?is)<rows?>.*?</rows?>", "", content or "")
    content = re.sub(r"(?is)<column[^>]*>.*?</column>", "", content or "")
    content = re.sub(r"(?is)<row[^>]*>.*?</row>", "", content or "")
    # líneas basura como  ,,,,"rows":
    content = re.sub(r'(?m)^\s*[,"]*\s*rows\s*:\s*[,]?\s*$', "", content or "")
    # Propaga la versión limpiada al texto de salida
    out["answer"] = content
    tbl = _pull("table")
    viz = _pull("visualization")

    def _json_or_none(s: Optional[str]):
        if not s:
            return None
        try:
            return json.loads(s)
        except Exception:
            return None

    table_obj = _json_or_none(tbl)
    viz_obj   = _json_or_none(viz)

    if table_obj:
        # normalizamos a forma esperada por el front
        cols = table_obj.get("columns") or table_obj.get("Cols") or table_obj.get("COLUMNS")
        rows = table_obj.get("rows")    or table_obj.get("Rows") or table_obj.get("ROWS")
        if isinstance(cols, list) and isinstance(rows, list):
            out["table"] = {
                "columns": [{"key": c.get("key", c.get("name", str(i))), "label": c.get("label", c.get("name", c.get("key", str(i))))} if isinstance(c, dict) else {"key": str(c), "label": str(c)} for i, c in enumerate(cols)],
                "rows": rows,
            }
        # quitamos la sección del texto
        out["answer"] = re.sub(r"\*\*table\*\*.*?\}", "", out["answer"], flags=re.I | re.S)
        out["answer"] = re.sub(r"\btable\s*:\s*\{.*?\}", "", out["answer"], flags=re.I | re.S)

    if viz_obj:
        if isinstance(viz_obj, dict):
            # si vino envuelto { type, xKey, yKeys, data, meta }
            out["visualization"] = viz_obj
        # quitamos la sección del texto
        out["answer"] = re.sub(r"\*\*visualization\*\*.*?\}", "", out["answer"], flags=re.I | re.S)
        out["answer"] = re.sub(r"\bvisualization\s*:\s*\{.*?\}", "", out["answer"], flags=re.I | re.S)

    # Limpieza final de saltos/espacios sobrantes
    txt = out["answer"]

    # Elimina etiquetas sueltas <table> que quedaron antes del escape
    txt = re.sub(r"(?im)^\s*<table>\s*$", "", txt)
    txt = txt.replace("<table>", "")

    # --- Limpieza avanzada para quitar JSON crudo y artefactos ---
    # 1️⃣ Elimina bloques ```json ... ``` o ```...```
    txt = re.sub(r"```(?:json)?[\\s\\S]*?```", "", txt)
    # 2️⃣ SKIP: No eliminar todo el JSON, solo limpiamos residuos específicos abajo
    # 3️⃣ Elimina llaves o corchetes sueltos (pero no en líneas con contenido)
    txt = re.sub(r"(?m)^\\s*[{}\\[\\]]+\\s*$", "", txt)
    # 🔧 NUEVO: elimina líneas basura tipo comas sueltas o claves estilo JSON
    #   - líneas que sean solo comas/quotes/espacios
    txt = re.sub(r"(?m)^\s*[,\"']+\s*$", "", txt)
    #   - líneas que empiecen como una clave JSON:  "algo":
    txt = re.sub(r'(?m)^\s*"[A-Za-z0-9_. -]+"\s*:\s*$', "", txt)
    #   - comas que queden solas entre saltos de línea
    txt = re.sub(r"(?m)^\s*,+\s*$", "", txt)
    #   - secuencias largas de comas: ,,,,,,,
    txt = re.sub(r",{3,}", "", txt)
    #   - palabras clave JSON sueltas: "rows":, "visualization":, etc.
    txt = re.sub(r'\b(?:rows|columns|table|visualization|data|meta)\s*:\s*,*', "", txt)
    # 4️⃣ Artefactos HTML/XML como <table>…</table>, <columns>, <rows> que algunos modelos "imaginan"
    txt = re.sub(r"(?is)<table[\s\S]*?</table>", "", txt)
    txt = re.sub(r"(?is)<columns?[\s\S]*?</columns?>", "", txt)
    txt = re.sub(r"(?is)</?rows?[\s\S]*?>", "", txt)  # captura apertura y cierre
    txt = re.sub(r"(?is)<column[^>]*/?>", "", txt)
    txt = re.sub(r"(?is)<row[^>]*/?>", "", txt)
    # Eliminar tags sueltos como </rows> o <rows> que quedaron
    txt = re.sub(r"</?rows?>", "", txt)
    txt = re.sub(r"</?columns?>", "", txt)
    # 5️⃣ Restos tipo: ,,,, "rows":   o  "rows":  o  key="..." label="..."
    txt = re.sub(r'(?m)^\s*,*\s*"rows"\s*:\s*$', "", txt)
    txt = re.sub(r'\b(?:key|label)="[^"]*"', "", txt)
    # 6️⃣ Compacta múltiples saltos de línea
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()

    # --- Normalización de estilo textual ---
    # Añade bullets, limpia numeraciones, y prepara para HTML seguro
    txt = re.sub(r"(?m)^\s*[-•]\s*", "• ", txt)
    txt = re.sub(r"(?m)^\s*\d+\.\s*", lambda m: f"{m.group(0).strip()} ", txt)
    txt = txt.strip()

    # --- Conversión a HTML con listas ordenadas y no ordenadas ---
    import html

    def _lines(s: str) -> list[str]:
        return [ln.strip() for ln in (s or "").split("\n")]

    lines = [ln for ln in _lines(txt) if ln]

    # Detect ordered list: lines like "1. ...", "2. ..."
    is_ordered = len(lines) >= 2 and all(re.match(r"^\d+\.\s+", ln) for ln in lines)
    # Detect unordered list: leading bullet or dash
    is_unordered = (not is_ordered) and len(lines) >= 2 and all(re.match(r"^(?:[-•\u2022])\s+", ln) for ln in lines)

    if is_ordered:
        items = [re.sub(r"^\d+\.\s+", "", ln) for ln in lines]
        safe_items = [html.escape(it) for it in items]
        safe = "<ol>" + "".join(f"<li>{it}</li>" for it in safe_items) + "</ol>"
    elif is_unordered:
        items = [re.sub(r"^(?:[-•\u2022])\s+", "", ln) for ln in lines]
        safe_items = [html.escape(it) for it in items]
        safe = "<ul>" + "".join(f"<li>{it}</li>" for it in safe_items) + "</ul>"
    else:
        # Paragraphs: preserve single line breaks as <br>, double as new paragraphs
        safe = html.escape(txt)
        safe = re.sub(r"\n{2,}", "</p><p>", safe)
        safe = re.sub(r"(?<!>)\n(?!<)", "<br>", safe)
        safe = f"<p>{safe}</p>"

    out["answer"] = safe
    
    # Debug: log what we're returning
    _dbg("_extract_structured returning: answer_len=%d, has_table=%s, has_viz=%s",
         len(out.get("answer", "")), "table" in out, "visualization" in out)
    
    return out

# --------- Etiquetas “humanas” para columnas ----------
def _pretty_label(key: str) -> str:
    """
    Convierte claves técnicas en etiquetas legibles (no cambia las keys reales).
    """
    k = key or ""
    # sf.* -> usa label de catálogo cuando exista
    if k.startswith("sf."):
        core = k[3:]
        # Mapa rápido para Account.*
        if core == "Account.Name":       return "Account Name"
        if core == "Account.Id":         return "Account Id"
        if core == "Account.ShippingCountry": return "Country"
        if core == "Account.ShippingCity":    return "City"
        # Detectar expresiones de agregación de Salesforce (expr0, expr1, etc.)
        if re.match(r"^expr\d+$", core, re.I):
            return "Average"  # genérico para AVG, COUNT, etc.
        # intenta catálogo
        lbl = (_INDEX_CACHE.get("sf_fields") or {}).get(_normalize(core), {}) or {}
        if isinstance(lbl, dict) and lbl.get("label"):
            return lbl["label"]
        # último recurso: heurística específica para API names
        return _prettify_sf_field_name(core)
    # Detectar expresiones agregadas sin prefijo sf. (expr0, expr1)
    if re.match(r"^expr\d+$", k, re.I):
        return "Average"
    # qual.* -> humaniza
    if k.startswith("qual."):
        base = k.split(".",1)[1]
        base = re.sub(r"__c$", "", base).replace("_", " ")
        return _apply_common_rewrites(base)
    if k.startswith("profil.") or k.startswith("profiling."):
        base = k.split(".",1)[1]
        base = re.sub(r"__c$", "", base).replace("_", " ")
        return _apply_common_rewrites(base)
    # extra.* y otros
    if k.startswith("extra."):
        return k.split(".",1)[1].replace("_"," ").title()
    if k in ("site","city","country","account_id","distance_km"):
        return {"site":"Account Name","city":"City","country":"Country","account_id":"Account Id","distance_km":"Distance (km)"}[k]
    # Account fields sin prefijo (ShippingCity, ShippingCountry)
    if k == "ShippingCity": return "City"
    if k == "ShippingCountry": return "Country"
    if k.lower() == "shippingcity": return "City"
    if k.lower() == "shippingcountry": return "Country"
    # por defecto humaniza
    return _apply_common_rewrites(re.sub(r"[_]+"," ",k))

def _ok_table(name: str) -> bool:
    name = (name or "").strip().strip('"')
    # Permitir CTEs (solo nombre sin schema)
    if name.lower() in {c.lower() for c in ALLOWED_CTES}:
        return True
    if "." not in name:
        name = f"public.{name}"
    norm_allowed = {t if t.startswith("public.") else f"public.{t}" for t in ALLOWED_TABLES}
    return name.lower() in norm_allowed

SF_ALLOWED_FIELDS = {
    # Opportunity fields
    "Id","Name","Type","StageName","Amount","CloseDate","RecordTypeId",
    "RecordType.Name","RecordType.DeveloperName",
    "CreatedDate","LastModifiedDate","LastActivityDate",
    # Account fields (with Account. prefix for Opportunity queries)
    "Account.Id","Account.Name","Account.ShippingCountry","Account.ShippingCity",
    "Account.ShippingLatitude","Account.ShippingLongitude",
    "Account.RecordType.Name","Account.RecordType.DeveloperName","Account.C_Type__c",
    "Account.ParentId","Account.C_Member__c","Account.C_Member__r.Name",
    # Account fields (without prefix for direct Account queries)
    "ShippingCountry","ShippingCity","ShippingLatitude","ShippingLongitude",
    "ParentId","C_Member__c","C_Member__r.Name","C_Type__c",
    "Account_Inactive__c","Subaccount_Inactive__c",
    # Contact fields (for direct Contact queries)
    "Email","Phone","Title","Department","FirstName","LastName","AccountId",
    "Contact.Id","Contact.Name","Contact.Email","Contact.Phone","Contact.Title","Contact.Department",
    # AccountContactRelation fields
    "ContactId","Role__c","Contact.Name","Contact.Email","Contact.Phone","Contact.Title",
    "Account.Name",
    "C_Profiling_Complete__c","Qualification_Close_Date__c",
    "C_Number_of_Individuals_screened_intotal__c","C_Number_of_Stage1_Individuals_followed__c",
    "C_Number_of_Stage2_Individuals_followed__c",
    "C_Number_of_T1D_Patients_currently_U_18__c","C_Number_of_T1D_Patients_currently_O_18__c",
    "C_Number_of_new_T1D_diagnosed_U_18__c","C_Number_of_new_T1D_diagnosed_O_18__c",
    "C_PI_Experience_with_Immuno_Med__c","C_Is_HLA_typing_performed__c","C_Site_Has_A_Study_Nurse__c",
    "C_Interestedinconducting_clinical_trials__c","C_SC_Dedicate_To_The_Research_Center__c",
    "C_Population_Origin__c","C_Nbr_of_related_site_sub_Investigator__c",
    "C_Nbr_of_studies_PI_is_involved_PI_Sub_I__c","C_Deadline__c","C_Start_Date__c","C_Signature__c",
    "C_Comments__c","C_Additional_Comments__c","C_Certification_Output__c","C_Account_Verified__c",
    "C_Contact_Verified__c","C_All_Research_Staff_Needed_for_GCP__c","C_Aware_of_any_Screening_Program__c",
    "C_Center_for_Running_Early_Diagnosis__c",
    "C_Interest_about_setting_up_a_program__c","C_Lead_Study_Nurse_Dedicated_to_the_cent__c",
    "C_Primarily_Caring_for_all_study__c","C_Site_Linked_with_Patient_Org_or_PAC__c",
    "C_Centralized_Facility_Contact_Person__c","C_Contact_Provided__c",
    "C_Immuno_Medi_trial_names_or_sponsors__c","C_Lead_Study_Coordinator_SC__c",
    "C_Lead_Study_Nurse__c","C_List_of_Organization_or_PAC_names__c",
    "C_List_of_trial_name_or_sponsors_NonTyp1__c","C_List_of_trial_name_or_sponsors_Type1__c",
    "C_Phase_III_NonType1__c","C_Phase_III_Type1__c","C_Phase_II_NonType1__c","C_Phase_II_Type1__c",
    "C_Phase_I_NonType1__c","C_Phase_I_Type1__c","C_Principal_Investigator__c",
    "C_SC_Experience_in_T1D_Clinicla_Research__c","C_SC_Situation_Explanation__c",
    "C_Services_Provided_by_Centralized_Unit__c","C_Study_Nurse_Situation_Explanation__c",
    "C_Study_Nurse_Specialities__c","C_The_Funding_for_the_screening_program__c",
    "C_Under_Which_Program__c","C_Has_facility_to_conduct__c",
    "C_Send_patients_to_other_CTS_nearby__c","C_List_of_nearby_CTS__c",
}
# Campos adicionales permitidos dinámicamente (rellenado en runtime vía describe)
SF_ALLOWED_DYNAMIC: set[str] = set()

def _describe_fields(sf, obj: str) -> set[str]:
    try:
        desc = sf.__getattr__(obj).describe()
        names = {f.get('name') for f in desc.get('fields', [])}
        return {n for n in names if n}
    except Exception:
        return set()

def _validate_soql(soql: str, sf=None):
    if not re.match(r"^\s*select\s", soql, re.I):
        raise HTTPException(400, "SOQL must start with SELECT")
    
    # Permitir queries a Opportunity, Account, Contact, AccountContactRelation
    allowed_objects = ["Opportunity", "Account", "Contact", "AccountContactRelation"]
    table_name = None
    for obj in allowed_objects:
        if re.search(rf"\bfrom\s+{obj}\b", soql, re.I):
            table_name = obj
            break
    
    if not table_name:
        raise HTTPException(400, f"SOQL must query FROM one of: {', '.join(allowed_objects)}")

    m = re.search(rf"select\s+(.*?)\s+from\s+{table_name}", soql, re.I | re.S)
    if not m:
        return
    raw_fields = m.group(1)
    fields = [f.strip() for f in re.split(r",(?![^()]*\))", raw_fields) if f.strip()]

    def norm(f: str) -> str:
        f = re.sub(r"\s+ASC|\s+DESC", "", f, flags=re.I)
        f = re.sub(r"\s+NULLS\s+(FIRST|LAST)", "", f, flags=re.I)
        f = f.split(" ")[0]
        return f

    # Per-object allowlists
    allowed_account = {
        "Id","Name","ShippingCountry","ShippingCity","ShippingLatitude","ShippingLongitude",
        "ParentId","C_Member__c","C_Member__r.Name","C_Type__c","Account_Inactive__c","Subaccount_Inactive__c",
    }
    allowed_contact = {
        "Id","Name","Email","Phone","Title","Department","AccountId","Account.Name",
    }
    # Opportunity fields are in SF_ALLOWED_FIELDS minus explicit Account.* (those must be prefixed)
    allowed_opp = {f for f in (SF_ALLOWED_FIELDS | SF_ALLOWED_DYNAMIC) if not f.startswith("Account.")}

    bad = []
    for f in fields:
        base = norm(f)
        if re.match(r"^(count|sum|min|max|avg)\s*\(", base, re.I):
            continue
        if table_name == "Opportunity":
            # Allow Account.* when querying Opportunity
            if base.startswith("Account."):
                # dynamic describe for Account; fall back to static allowlist when describe fails
                static_acc = {f for f in SF_ALLOWED_FIELDS if f.startswith('Account.')}
                if sf is not None:
                    acc_fields = _describe_fields(sf, 'Account')
                    dynamic_acc = {f'Account.{x}' for x in acc_fields} if acc_fields else set()
                    if base not in (dynamic_acc | static_acc | SF_ALLOWED_DYNAMIC):
                        bad.append(base)
                else:
                    if base not in (SF_ALLOWED_FIELDS | SF_ALLOWED_DYNAMIC):
                        bad.append(base)
                continue
            if base not in allowed_opp:
                bad.append(base)
        elif table_name == "Account":
            # Only Account fields allowed
            if base not in allowed_account:
                bad.append(base)
        elif table_name == "Contact":
            if base not in allowed_contact:
                bad.append(base)
        elif table_name == "AccountContactRelation":
            # Conservative: allow only fields we reference in tools
            allowed_acr = {
                "Id","AccountId","ContactId","Role__c","Contact.Name","Contact.Email","Contact.Phone","Contact.Title","Contact.Department","Account.Name"
            }
            if base not in allowed_acr:
                bad.append(base)
    if bad:
        raise HTTPException(400, f"SOQL field(s) not allowed for {table_name}: {', '.join(bad)}")

# ====== Helpers UI / SOQL ======

def _ensure_soql_has_account_id(soql: str) -> str:
    """
    Si el SELECT contiene algún campo Account.* pero NO incluye Account.Id, lo inyectamos.
    Conserva el resto del SOQL (WHERE/ORDER BY/LIMIT).
    """
    try:
        m = re.search(r"^\s*select\s+(?P<select>.+?)\s+from\s+Opportunity(?P<tail>.*)$", soql, flags=re.I | re.S)
        if not m:
            return soql
        sel = m.group("select")
        tail = m.group("tail") or ""
        # detectar si hay Account.* en el select
        has_account_fields = re.search(r"\bAccount\.[A-Za-z0-9_]+\b", sel) is not None
        has_account_id = re.search(r"\bAccount\.Id\b", sel) is not None
        if has_account_fields and not has_account_id:
            # insertamos al principio del SELECT para no romper alias ni ORDER BY
            new_sel = "Account.Id, " + sel.strip()
            fixed = f"SELECT {new_sel} FROM Opportunity{tail}"
            return fixed
        return soql
    except Exception:
        return soql

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


def _normalize_table_for_ui(table: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Asegura que cada fila tenga:
      - account_id (si hay sf.Account.Id o salesforce_account_id, etc.)
      - sf.Account.Id (si hay account_id)
      - site / sf.Account.Name cuando es detectable
      - country / city (desprefijando sitios comunes)
    No cambia los labels, solo agrega claves adicionales en rows (y añade columns si no existen).
    """
    if not table or not isinstance(table, dict):
        return table
    cols = table.get("columns") or []
    rows = table.get("rows") or []
    if not isinstance(rows, list):
        return table

    col_keys = [c.get("key") if isinstance(c, dict) else str(c) for c in cols]
    col_set = set([str(k) for k in col_keys])

    def add_col(k: str):
        if k not in col_set:
            cols.append({"key": k, "label": _pretty_label(k)})
            col_set.add(k)

    id_candidates = [
        "sf.Account.Id", "sf.AccountId", "Account.Id",
        "account_id",
        "salesforce_account_id", "sites.salesforce_account_id", "s.salesforce_account_id",
        "sf_account_id", "sf.account.id", "sf_accountid",
    ]
    name_candidates = [
        "sf.Account.Name", "Account.Name", "sf.Name", "name", "sites.name", "s.name", "account_name",
    ]
    country_candidates = ["country", "sites.country", "s.country", "sf.Account.ShippingCountry", "Account.ShippingCountry"]
    city_candidates    = ["city", "sites.city", "s.city", "sf.Account.ShippingCity", "Account.ShippingCity"]

    norm_rows = []
    for r in rows:
        rd = dict(r) if isinstance(r, dict) else {}
        acc_id_val = next((rd[k] for k in id_candidates if k in rd and rd.get(k)), None)
        if acc_id_val:
            rd.setdefault("account_id", acc_id_val)
            add_col("account_id")
        site_name = next((rd[k] for k in name_candidates if k in rd and rd.get(k)), None)
        if site_name:
            rd.setdefault("site", site_name)
            add_col("site")
        for keys, std in ((country_candidates, "country"), (city_candidates, "city")):
            val = next((rd[k] for k in keys if k in rd and rd.get(k) not in (None, "")), None)
            if val is not None:
                rd.setdefault(std, val); add_col(std)
        norm_rows.append(rd)
    # --- De-duplicación y normalización final de columnas visibles ---
    # Preferimos claves amigables y eliminamos los equivalentes sf.Account.*
    friendly = {
        "sf.Account.Id": "account_id",
        "sf.AccountId": "account_id",
        "Account.Id": "account_id",
        "salesforce_account_id": "account_id",
        "sf.Account.Name": "site",
        "Account.Name": "site",
        "account_name": "site",
        "sf.Name": "site",
        "name": "site",
        "sf.Account.ShippingCountry": "country",
        "Account.ShippingCountry": "country",
        "ShippingCountry": "country",
        "sf.Account.ShippingCity": "city",
        "Account.ShippingCity": "city",
        "ShippingCity": "city",
    }

    # 1) Recoge el orden original de claves visto en 'cols'
    orig_keys = [c.get("key") if isinstance(c, dict) else str(c) for c in cols]

    # 2) Calcula cuáles amigables existen realmente (por datos o por columnas)
    present = set()
    for r in norm_rows:
        if isinstance(r, dict):
            present.update(k for k, v in r.items() if v is not None)
    present.update(orig_keys)

    preferred = [k for k in ("account_id", "site", "country", "city", "distance_km") if k in present]

    # 3) Elimina duplicados y mapea sf.Account.* → amigables
    def _normalize_key(k: str) -> str:
        return friendly.get(k, k)

    seen = set()
    final_keys = []

    # a) siempre primero las preferidas si existen
    for k in preferred:
        if k not in seen:
            seen.add(k); final_keys.append(k)

    # b) el resto respetando orden original, filtrando equivalentes sf.Account.*
    for k in orig_keys:
        nk = _normalize_key(k)
        # omite las sf.Account.* mapeadas cuando ya está su amigable
        if nk in preferred and nk in seen:
            continue
        if nk not in seen:
            seen.add(nk); final_keys.append(nk)

    # 4) Construye columnas finales con etiquetas bonitas
    cols = [{"key": k, "label": _pretty_label(k)} for k in final_keys]

    table["columns"] = cols
    table["rows"] = norm_rows
    return table

# ====== Explorer (drive km) ======
def tool_explorer_within_drive_km(
    request: Request,
    base_account_id: str,
    max_km: float,
    filters: Optional[Dict[str, Any]] = None,
    columns: Optional[List[str]] = None,
):
    """
    Proxy interno al endpoint /api/explorer/search/within-drive-km, preservando sesión/cookies.
    """
    url = f"http://127.0.0.1:8000{EXPLORER_DRIVE_KM_PATH}"
    payload = {
        "base_account_id": base_account_id,
        "max_km": max_km,
        "filters": filters or {"logic": "AND", "rules": []},
        "columns": columns or [],
    }
    # Reenvía cookies/sesión para que _get_sf use la sesión actual
    headers = {}
    if request:
        ck = request.headers.get("cookie")
        if ck: headers["cookie"] = ck
        auth = request.headers.get("authorization")
        if auth: headers["authorization"] = auth
    with httpx.Client(timeout=60.0) as cli:
        resp = cli.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"drive-km failed: {resp.text}")
    data = resp.json()
    # Construimos tabla básica desde data['rows'] (colapsadas por cuenta)
    rows = data.get("rows") or []
    cols: List[Dict[str,str]] = []
    if rows:
        keys = sorted({k for r in rows for k in r.keys()})
        cols = [{"key": k, "label": k} for k in keys]
    return {"columns": cols, "rows": rows, "meta": data.get("meta"), "base": data.get("base")}

def tool_members_search(
    request: Request,
    filters: Dict[str, Any],
    include_detail: bool = False,
) -> Dict[str, Any]:
    """Proxy interno al endpoint /api/members/search."""
    url = "http://127.0.0.1:8000/api/members/search"
    payload: Dict[str, Any] = {
        "filters": filters or {"logic": "AND", "rules": []},
    }
    headers: Dict[str, str] = {}
    if request:
        ck = request.headers.get("cookie")
        if ck:
            headers["cookie"] = ck
    with httpx.Client(timeout=60.0) as cli:
        resp = cli.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"members_search failed: {resp.text[:300]}")
    data = resp.json()
    rows = data.get("rows") or []
    # Build flat rows for table display
    flat_rows = []
    for r in rows:
        flat: Dict[str, Any] = {
            "account_id": r.get("account_id", ""),
            "account_name": r.get("account_name", ""),
            "country": r.get("country", ""),
            "city": r.get("city", ""),
        }
        d = r.get("data") or {}
        flat["Level of Membership"] = d.get("sf.C_Level_of_Membership__c") or ""
        flat["Status"] = d.get("sf.Account_Status__c") or ""
        flat["Representative"] = d.get("sf.C_Member_Representative__r.Name") or ""
        flat["# Sites"] = d.get("extra.SubAccountsCount", 0)
        flat["# Contacts"] = d.get("extra.ContactsCount", 0)
        # Proposed roles as compact string
        proposed = []
        if d.get("sf.Clinical_Site_CS__c"):            proposed.append("CS")
        if d.get("sf.C_Deliver_Clinical_Grade_Services__c"): proposed.append("DxLab")
        if d.get("sf.C_Perform_Cutting_Edge__c"):      proposed.append("LAB")
        if d.get("sf.C_Contribute_as_a_Patient_Organization__c"): proposed.append("PatOrg")
        flat["Proposed Roles"] = ", ".join(proposed) if proposed else "—"
        # Validated roles as compact string
        validated = []
        if d.get("sf.Clinical_Site_CS_validated__c"):            validated.append("CS")
        if d.get("sf.Clinical_Trial_Site_CTS_validated__c"):     validated.append("CTS")
        if d.get("sf.Diagnostic_Lab_DxLab_validated__c"):        validated.append("DxLab")
        if d.get("sf.Research_Mechanistic_Lab_LAB_validated__c"):validated.append("LAB")
        if d.get("sf.Patient_Organization_validated__c"):        validated.append("PatOrg")
        flat["Validated Roles"] = ", ".join(validated) if validated else "—"
        flat_rows.append(flat)

    # Column definitions
    cols = [
        {"key": "account_name",        "label": "Institution"},
        {"key": "country",             "label": "Country"},
        {"key": "city",                "label": "City"},
        {"key": "Level of Membership", "label": "Level of Membership"},
        {"key": "Status",              "label": "Status"},
        {"key": "Representative",      "label": "Representative"},
        {"key": "# Sites",             "label": "# Sites"},
        {"key": "# Contacts",          "label": "# Contacts"},
        {"key": "Proposed Roles",      "label": "Proposed Roles"},
        {"key": "Validated Roles",     "label": "Validated Roles"},
    ]
    return {
        "columns": cols,
        "rows": flat_rows[:500],
        "meta": {"total": len(rows)},
    }


def tool_explorer_search(
    request: Request,
    filters: Dict[str, Any],
    columns: Optional[List[str]] = None,
):
    """
    Proxy interno al endpoint /api/explorer/search.
    Acepta FilterGroup con reglas qual.*, sf.* y site.* y devuelve una tabla unificada.
    """
    # Phase 4: validate FilterGroup before executing — surface errors to Claude so it can fix them
    if filters:
        _vl_errors = validate_filter_group(filters)
        if _vl_errors:
            _dbg("WARN: invalid FilterGroup from tool call: %s", _vl_errors)
            return {
                "error": "invalid_filter_group",
                "validation_errors": _vl_errors,
                "message": (
                    f"The filter group has {len(_vl_errors)} validation error(s): "
                    + "; ".join(_vl_errors[:5])
                    + ". Please correct the filters and try again."
                ),
                "rows": [],
                "columns": [],
            }

    url = f"http://127.0.0.1:8000{EXPLORER_SEARCH_PATH}"
    # Columnas por defecto si no se especifican
    default_cols = [
        "sf.Account.Id", "sf.Account.Name",
        "sf.Account.ShippingCountry", "sf.Account.ShippingCity",
        "sf.C_Number_of_T1D_Patients_currently_U_18__c",
        "sf.C_Number_of_T1D_Patients_currently_O_18__c",
        "sf.C_Number_of_new_T1D_diagnosed_U_18__c",
        "sf.C_Number_of_new_T1D_diagnosed_O_18__c",
    ]
    payload = {
        "filters": filters or {"logic": "AND", "rules": []},
        "columns": columns or default_cols,
    }
    headers = {}
    if request:
        ck = request.headers.get("cookie")
        if ck:
            headers["cookie"] = ck
        auth = request.headers.get("authorization")
        if auth:
            headers["authorization"] = auth
    with httpx.Client(timeout=90.0) as cli:
        resp = cli.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"explorer_search failed: {resp.text[:300]}")
    data = resp.json()
    rows = data.get("rows") or []
    cols: List[Dict[str, str]] = []
    if rows:
        keys = list({k for r in rows for k in (r.get("data") or r).keys()})
        keys = sorted(keys)
        cols = [{"key": k, "label": k} for k in keys]
    # Aplanar data nested si viene en {account_id, account_name, data:{...}}
    flat_rows = []
    for r in rows:
        if "data" in r and isinstance(r["data"], dict):
            flat = {**r["data"]}
            for meta_k in ("account_id", "account_name", "country", "city"):
                if meta_k in r:
                    flat[meta_k] = r[meta_k]
        else:
            flat = dict(r)
        flat_rows.append(flat)
    return {
        "columns": cols,
        "rows": flat_rows[:300],
        "meta": {"total": len(rows)},
    }


def tool_nearest_filtered_sites(
    request: Request,
    location: str,
    filters: Optional[Dict[str, Any]] = None,
    top_n: int = 10,
    max_km: float = 1000,
    db: Optional[Any] = None,
) -> dict:
    """
    Find the nearest clinical sites to a city/address, optionally filtered by qual.* and sf.* fields.
    Returns sites sorted by straight-line (Haversine) distance in km.

    Fast path (no filters): queries local Site table directly — no Salesforce API call needed.
    Filtered path: calls /api/explorer/search internally (requires valid session cookie).
    """
    import math
    from app.models.site import Site as SiteModel

    # 1. Geocode location — try geonames_cities DB first (free, fast), Google API as fallback
    geo_lat, geo_lon, formatted = None, None, location

    # 1a. Try geonames_cities table (cities500.txt — ~200k cities with coords)
    try:
        if db:
            from app.models.geonames import GeonameCity
            # Parse "City, Country" or just "City"
            parts = [p.strip() for p in location.split(",")]
            city_name = parts[0]
            # Query by name, order by population DESC (largest city wins)
            q = db.query(GeonameCity).filter(
                GeonameCity.name.ilike(city_name)
            ).order_by(GeonameCity.population.desc())
            # If country hint provided, filter by it
            if len(parts) > 1:
                country_hint = parts[-1].strip()
                from app.utils.country_norms import resolve_countries
                resolved = resolve_countries(country_hint)
                if resolved:
                    iso2 = resolved[0].get("iso2", "")
                    if iso2:
                        q = q.filter(GeonameCity.country_code == iso2)
            geo_city = q.first()
            if geo_city and geo_city.latitude and geo_city.longitude:
                geo_lat, geo_lon = float(geo_city.latitude), float(geo_city.longitude)
                formatted = f"{geo_city.name}, {geo_city.country_code}"
                _dbg("NEAREST: geocoded '%s' via geonames_cities → %s (%.4f, %.4f, pop=%s)",
                     location, formatted, geo_lat, geo_lon, geo_city.population)
    except Exception as e:
        _dbg("WARN: geonames geocode failed: %s", e)

    # 1b. Fallback to Google Geocoding API (if geonames didn't find it)
    if geo_lat is None:
        google_key = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        region = os.environ.get("GOOGLE_REGION_BIAS", "")
        if google_key:
            try:
                with httpx.Client(timeout=10.0) as gcli:
                    gr = gcli.get(
                        "https://maps.googleapis.com/maps/api/geocode/json",
                        params={"address": location, "key": google_key, "region": region},
                    )
                    gj = gr.json()
                    if gj.get("results"):
                        loc = gj["results"][0]["geometry"]["location"]
                        geo_lat, geo_lon = float(loc["lat"]), float(loc["lng"])
                        formatted = gj["results"][0].get("formatted_address", location)
                        _dbg("NEAREST: geocoded '%s' via Google API → %s", location, formatted)
            except Exception as e:
                _dbg("WARN: Google geocode failed: %s", e)
    if geo_lat is None:
        return {"error": f"Could not geocode '{location}'."}

    # 2. Haversine distance helper
    def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        )
        return round(R * 2 * math.asin(math.sqrt(max(0.0, a))), 1)

    has_filters = bool(filters and filters.get("rules"))
    n_total_sites = 0  # set by whichever path runs, for diagnostics
    n_no_coords = 0

    # Check if we have a valid SF session cookie → prefer Explorer path (all 195+ sites with coords)
    # The local Site table only has ~45 sites (those with qualification uploads), so fast-path
    # misses most sites. Only fall back to fast-path when no SF session is available.
    _has_sf_cookie = bool(request and request.headers.get("cookie"))

    # 3a. Fast path: no filters AND no SF session → query local Site table + explorer geocache
    if not has_filters and db is not None and not _has_sf_cookie:
        _dbg("NEAREST: fast-path via local DB + geocache (no filters)")
        # Lazy-import geocache + country normalizer from explorer (already loaded in memory, no overhead)
        _geo_cache_get_fn = None
        _country_norm_fn = None
        try:
            from app.utils.geo_cache import _geo_cache_get as _geo_cache_get_fn  # noqa: F401
            from app.routers.salesforce_explorer import _country_norm as _country_norm_fn  # noqa: F401
        except Exception as _imp_err:
            _dbg("NEAREST: could not import geocache helpers: %s", _imp_err)

        sites_q = db.query(
            SiteModel.salesforce_account_id,
            SiteModel.name,
            SiteModel.city,
            SiteModel.country,
            SiteModel.latitude,
            SiteModel.longitude,
        ).filter(
            SiteModel.salesforce_account_id.isnot(None),
        ).all()

        enriched = []
        n_db_coords = 0
        n_cache_coords = 0
        n_no_coords = 0
        for s in sites_q:
            lat, lng = s.latitude, s.longitude
            if lat is None or lng is None:
                # Fallback: look up explorer's in-memory geocache (city|country key)
                if _geo_cache_get_fn is not None:
                    # Try as-is first, then with normalized country (e.g. "Spain" → "ES")
                    cached = _geo_cache_get_fn(s.city, s.country)
                    if not cached and _country_norm_fn is not None and s.country:
                        cached = _geo_cache_get_fn(s.city, _country_norm_fn(s.country))
                    if cached:
                        lat, lng = cached
                        n_cache_coords += 1
                    else:
                        n_no_coords += 1
                else:
                    n_no_coords += 1
            else:
                n_db_coords += 1
            if lat is None or lng is None:
                continue
            dist = haversine_km(geo_lat, geo_lon, float(lat), float(lng))
            if dist <= max_km:
                enriched.append({
                    "account_id": s.salesforce_account_id,
                    "account_name": s.name or "",
                    "country": s.country or "",
                    "city": s.city or "",
                    "distance_km": dist,
                })
        n_total_sites = len(sites_q)
        _dbg(
            "NEAREST: fast-path: %d total sites, db_coords=%d cache_coords=%d no_coords=%d within_%skm=%d",
            n_total_sites, n_db_coords, n_cache_coords, n_no_coords, int(max_km), len(enriched),
        )

    # 3b. Filtered path: call /api/explorer/search (requires session cookie)
    else:
        _dbg("NEAREST: filtered-path via internal HTTP (filters=%s)", bool(has_filters))
        url = f"http://127.0.0.1:8000{EXPLORER_SEARCH_PATH}"
        headers = {}
        if request:
            ck = request.headers.get("cookie")
            if ck:
                headers["cookie"] = ck
            auth = request.headers.get("authorization")
            if auth:
                headers["authorization"] = auth
        _nearest_sf_cols = [
            "sf.Account.Id", "sf.Account.Name", "sf.Account.ShippingCountry", "sf.Account.ShippingCity",
            "sf.C_Number_of_Stage1_Individuals_followed__c", "sf.C_Number_of_Stage2_Individuals_followed__c",
            "sf.C_Number_of_new_T1D_diagnosed_O_18__c", "sf.C_Number_of_new_T1D_diagnosed_U_18__c",
            "sf.C_Number_of_T1D_Patients_currently_O_18__c", "sf.C_Number_of_T1D_Patients_currently_U_18__c",
        ]
        with httpx.Client(timeout=90.0) as cli:
            resp = cli.post(
                url,
                json={"filters": filters or {"logic": "AND", "rules": []}, "columns": _nearest_sf_cols},
                headers=headers,
            )
        _dbg("NEAREST: explorer/search status=%d", resp.status_code)
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, f"explorer_search failed: {resp.text[:300]}")
        resp_json = resp.json()
        points = resp_json.get("points") or []
        point_coords: Dict[str, tuple] = {
            p["account_id"]: (float(p["lat"]), float(p["lng"]))
            for p in points
            if p.get("account_id") and p.get("lat") and p.get("lng")
        }
        enriched = []
        for row in resp_json.get("rows") or []:
            aid = row.get("account_id", "")
            if aid not in point_coords:
                continue
            lat, lng = point_coords[aid]
            dist = haversine_km(geo_lat, geo_lon, lat, lng)
            if dist > max_km:
                continue
            flat: Dict[str, Any] = {
                "account_id": aid,
                "account_name": row.get("account_name", ""),
                "country": row.get("country", ""),
                "city": row.get("city", ""),
                "distance_km": dist,
            }
            data = row.get("data") or {}
            flat.update(data)
            enriched.append(flat)

    enriched.sort(key=lambda r: r["distance_km"])
    enriched = enriched[: min(top_n, 50)]

    # Base columns always shown
    columns = [
        {"key": "distance_km", "label": f"Distance from {formatted} (km)"},
        {"key": "account_name", "label": "Site"},
        {"key": "country", "label": "Country"},
        {"key": "city", "label": "City"},
    ]
    # Auto-detect SF metric fields present in rows and add them as columns
    _sf_key_labels = {
        "sf.C_Number_of_Stage1_Individuals_followed__c": "Stage 1",
        "sf.C_Number_of_Stage2_Individuals_followed__c": "Stage 2",
        "sf.C_Number_of_new_T1D_diagnosed_O_18__c": "ND ≥18",
        "sf.C_Number_of_new_T1D_diagnosed_U_18__c": "ND <18",
        "sf.C_Number_of_T1D_Patients_currently_O_18__c": "T1D ≥18",
        "sf.C_Number_of_T1D_Patients_currently_U_18__c": "T1D <18",
    }
    _present_sf_keys = {k for row in enriched for k in row if k.startswith("sf.")}
    for sf_key, sf_label in _sf_key_labels.items():
        if sf_key in _present_sf_keys:
            columns.append({"key": sf_key, "label": sf_label})
    # Also include any sf.* fields explicitly mentioned in filter rules (reliable even when data is null)
    _already_col_keys = {c["key"] for c in columns}
    def _collect_filter_sf_fields_fn(f: dict) -> list:
        result: list = []
        if not f:
            return result
        for _r in f.get("rules") or []:
            if isinstance(_r, dict):
                _fld = _r.get("field", "")
                if _fld.startswith("sf."):
                    result.append(_fld)
                else:
                    result.extend(_collect_filter_sf_fields_fn(_r))
        return result
    for _ff in _collect_filter_sf_fields_fn(filters or {}):
        if _ff not in _already_col_keys:
            _lbl = _sf_key_labels.get(_ff) or _pretty_label(_ff)
            columns.append({"key": _ff, "label": _lbl})
            _already_col_keys.add(_ff)
    return {
        "columns": columns,
        "rows": enriched,
        "meta": {
            "total": len(enriched),
            "geocoded": formatted,
            "note": "Straight-line distances (km). Use Explorer nearby for driving distance.",
            "no_site_coords": n_no_coords,   # >0 means some sites had no geocoords (DB + cache both missed)
            "n_total_sites": n_total_sites,
        },
    }


# ====== Tools ======

# en app/routers/ai_chat.py, reemplaza tool_sql_query por esta versión:

def tool_sql_query(db: Session, sql: str, params: Optional[Dict[str, Any]] = None):
    _dbg("SQL >>> %s | params=%s", sql, params)
    if re.search(r"\b(insert|update|delete|alter|drop|create|grant|revoke|truncate)\b", sql, re.I):
        raise HTTPException(400, "Only read-only SELECT is allowed")

    suspects = re.findall(r"(?:from|join)\s+([A-Za-z0-9_\.]+)", sql, flags=re.I)
    for t in suspects:
        if not _ok_table(t):
            raise HTTPException(400, f"Table not allowed: {t}")

    # Geo guard: no Postgres geo/earthdistance functions (must use Explorer within-drive-km)
    geo_rx = re.compile(r"\bST_\w+\b|\bgeography\s*\(|\bpoint\s*\(|\bearthdistance\b|\bearth\s*\(|\bll_to_earth\b", re.I)
    if geo_rx.search(sql or ""):
        raise HTTPException(400, "Geospatial SQL is not allowed. Use explorer_within_drive_km for distance queries.")

    max_rows = int(os.environ.get("AI_MAX_ROWS", "1000"))
    def _exec(_sql: str):
        """
        Ejecuta un SELECT de forma segura. Si algo falla, hace rollback para
        sacar la sesión del estado 'aborted' y vuelve a propagar la excepción.
        """
        try:
            result = db.execute(text(_sql), params or {})
            cols = list(result.keys())
            rows_raw = result.fetchmany(max_rows + 1)
            truncated = len(rows_raw) > max_rows
            rows: List[List[Any]] = []
            for r in rows_raw[:max_rows]:
                if hasattr(r, "keys"):
                    rows.append([r[c] for c in cols])
                else:
                    rows.append(list(r))
            return {"columns": cols, "rows": rows, "truncated": truncated}
        except Exception as _e:
            # MUY IMPORTANTE: limpiar la transacción fallida
            try:
                db.rollback()
            except Exception:
                pass
            raise

    try:
        # Asegura que no venimos de un fallo anterior en la misma sesión
        try:
            db.rollback()
        except Exception:
            pass
        return _exec(sql)
    except Exception as e:
        # Fallback: si parece error por alias en ORDER BY → reescribir ORDER BY con expresiones
        msg = str(e)
        if "UndefinedColumn" in msg or "does not exist" in msg:
            # capturamos SELECT-list y cláusulas WHERE/ORDER BY
            m_sel = re.search(r"\bselect\s+(.*?)\s+from\s", sql, flags=re.I | re.S)
            if not m_sel:
                raise
            select_list = m_sel.group(1)
            m_where = re.search(r"\bwhere\s+(.*?)(?:\border\s+by\b|$)", sql, flags=re.I | re.S)
            m_order = re.search(r"\border\s+by\s+(.*)$", sql, flags=re.I)

            # mapa alias→expr a partir de "... expr AS alias"
            alias_map: Dict[str, str] = {}
            for part in re.split(r",(?![^\(\)]*\))", select_list):
                part = part.strip()
                m_as = re.search(r"(.+?)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)$", part, flags=re.I)
                if m_as:
                    expr = m_as.group(1).strip()
                    alias = m_as.group(2).strip()
                    alias_map[alias] = expr

            if not alias_map:
                raise

            def replace_aliases(expr: str) -> str:
                # Reemplaza palabras completas que coinciden con alias por (expr)
                def repl(m):
                    name = m.group(0)
                    return f"({alias_map[name]})" if name in alias_map else name
                # \b no funciona bien con _ en todas las versiones, usamos lookarounds
                pattern = r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, alias_map.keys())) + r")(?![A-Za-z0-9_])"
                return re.sub(pattern, repl, expr)

            sql_fixed = sql

            # WHERE: reemplazo directo de alias por expresión
            if m_where:
                where_txt = m_where.group(1)
                new_where = replace_aliases(where_txt)
                sql_fixed = re.sub(r"(\bwhere\s+).*?(?=\border\s+by\b|$)",
                                   r"\1" + new_where, sql_fixed, flags=re.I | re.S)

            # ORDER BY: rehacemos cada item preservando ASC/DESC/NULLS
            if m_order:
                order_by = m_order.group(1)
                new_items = []
                for item in order_by.split(","):
                    m_dir = re.search(r"\s+(ASC|DESC)\b", item, flags=re.I)
                    m_nulls = re.search(r"\bNULLS\s+(FIRST|LAST)\b", item, flags=re.I)
                    core = re.sub(r"\b(ASC|DESC)\b", "", item, flags=re.I)
                    core = re.sub(r"\bNULLS\s+(FIRST|LAST)\b", "", core, flags=re.I).strip()
                    core = replace_aliases(core)
                    rebuilt = core
                    if m_dir:   rebuilt += f" {m_dir.group(1)}"
                    if m_nulls: rebuilt += f" NULLS {m_nulls.group(1)}"
                    new_items.append(rebuilt)
                sql_fixed = re.sub(r"\border\s+by\s+.*$", "ORDER BY " + ", ".join(new_items), sql_fixed, flags=re.I)

            _dbg("SQL fallback (alias→expr) >>> %s", sql_fixed)
            # Antes de reintentar, limpiar cualquier estado de error previo
            try:
                db.rollback()
            except Exception:
                pass
            return _exec(sql_fixed)

        # si no pudimos arreglarlo, relanza
        # Limpia estado abortado antes de propagar
        try:
            db.rollback()
        except Exception:
            pass
        raise

def tool_salesforce_query(sf, soql: str):
    _dbg("SOQL (raw) >>> %s", soql)
    soql_plus = _ensure_soql_has_account_id(soql)
    fixed = _sanitize_soql_basic(soql_plus)
    if fixed != soql:
        _dbg("SOQL (fixed) >>> %s", fixed)
    _validate_soql(fixed, sf)
    raw = sf.query_all(fixed)
    _dbg("SOQL <<< records=%d", len(raw.get("records", [])) if isinstance(raw, dict) else -1)
    return raw

def tool_salesforce_account_extras(sf, account_id: str):
    _dbg("SF extras >>> account_id=%s", account_id)
    if not account_id:
        raise HTTPException(400, "Missing account_id")
    data = _account_extras_core(sf, account_id)
    flat = {
        "account_id": data.get("account_id"),
        "member_name": (data.get("member") or {}).get("name"),
        "pi_name": (data.get("pi") or {}).get("name"),
        "pi_email": (data.get("pi") or {}).get("email"),
        "pi_phone": (data.get("pi") or {}).get("phone"),
        "cs_clinical_site": (data.get("csContribution") or {}).get("INNODIA_Clinical_Trial_Site__c"),
        "cs_referral_outreach": (data.get("csContribution") or {}).get("Referral_Outreach_Site_Non_CTS__c"),
        "cs_eligible_detect": (data.get("csContribution") or {}).get("Elegible_for_DETECT_Site__c"),
        "assignments_count": int(len(data.get("assignments") or [])),
        "new_dx_u18": data.get("newDxUnder18"),
        "new_dx_o18": data.get("newDxOver18"),
    }
    _dbg("SF extras <<< member=%s | PI=%s | assignments=%d | new_u18=%s | new_o18=%s",
         flat.get("member_name"), flat.get("pi_name"),
         flat.get("assignments_count", 0), str(flat.get("new_dx_u18")), str(flat.get("new_dx_o18")))
    return {"columns": list(flat.keys()), "rows": [[flat[k] for k in flat.keys()]]}


def tool_salesforce_account_contacts(sf, account_id: str, include_subaccounts: bool = False, role_contains: Optional[str] = None, roles: Optional[List[str]] = None, title_contains: Optional[str] = None, country: Optional[str] = None, city: Optional[str] = None):
    """Fetch contacts by Account OR by geography when no Account Id is given.
    - If account_id is valid: returns contacts for that Account (and optionally child Accounts).
    - Else if country/city is provided (or a non-Id account_id that looks like a place), search Accounts in that geography
      restricted to Clinical SubAccounts, then return contacts filtered by roles/title.
    Returns a normalized table with account_id, site, contact_id, contact_name, email, phone, title, department, role.
    """
    import os

    def _list_contacts_for_accounts(ids: List[str]) -> List[Dict[str, Any]]:
        rows_local: List[Dict[str, Any]] = []
        if not ids:
            return rows_local
        ids_clause = ", ".join([f"'{i}'" for i in set(ids)])
        roles_env = [s.strip() for s in (os.environ.get("SF_CONTACT_ROLES", "").split(",")) if s.strip()]
        role_list = roles if roles and len(roles) else roles_env
        if role_list:
            role_vals = ", ".join([f"'{r}'" for r in role_list])
            soql = (
                "SELECT Id, AccountId, ContactId, Role__c, "
                "Contact.Name, Contact.Email, Contact.Phone, Contact.Title, Contact.Department, "
                "Account.Name "
                "FROM AccountContactRelation "
                f"WHERE AccountId IN ({ids_clause}) AND Role__c IN ({role_vals}) "
            )
            if title_contains:
                title_escaped = title_contains.replace("'", "\\'")
                # ampliar variantes: 'Study Coord', 'Coordinator', 'Co-ordinator', prefijo 'Coordinat'
                soql += (
                    f" AND (Contact.Title LIKE '%{title_escaped}%'"
                    f" OR Contact.Title LIKE '%Coordinat%')"
                )
            soql += " ORDER BY AccountId, Contact.Name"
            raw = sf.query_all(soql)
            for r in raw.get("records", []):
                acc = r.get("Account") or {}
                c = r.get("Contact") or {}
                rows_local.append({
                    "account_id": r.get("AccountId"),
                    "site": acc.get("Name"),
                    "contact_id": r.get("ContactId"),
                    "contact_name": c.get("Name"),
                    "email": c.get("Email"),
                    "phone": c.get("Phone"),
                    "title": c.get("Title"),
                    "department": c.get("Department"),
                    "role": r.get("Role__c"),
                })
            # Fallback/Union: si pedimos por título, incluye también Contact por Title
            if title_contains:
                fields = ["Id", "Name", "Email", "Phone", "Title", "Department", "AccountId", "Account.Name"]
                tc = title_contains.replace("'", "\'")
                where = f"AccountId IN ({ids_clause}) AND Title LIKE '%{tc}%'"
                soql2 = f"SELECT {', '.join(fields)} FROM Contact WHERE {where} ORDER BY AccountId, Name"
                raw2 = sf.query_all(soql2)
                for r in raw2.get("records", []):
                    acc = r.get("Account") or {}
                    rows_local.append({
                        "account_id": r.get("AccountId"),
                        "site": acc.get("Name"),
                        "contact_id": r.get("Id"),
                        "contact_name": r.get("Name"),
                        "email": r.get("Email"),
                        "phone": r.get("Phone"),
                        "title": r.get("Title"),
                        "department": r.get("Department"),
                    })
        else:
            fields = ["Id", "Name", "Email", "Phone", "Title", "Department", "AccountId", "Account.Name"]
            where = f"AccountId IN ({ids_clause})"
            if role_contains:
                rc = role_contains.replace("'", "\'")
                where += f" AND (Title LIKE '%{rc}%' OR Department LIKE '%{rc}%')"
            if title_contains:
                tc = title_contains.replace("'", "\'")
                where += f" AND (Title LIKE '%{tc}%' OR Title LIKE '%Coordinat%')"
            soql = f"SELECT {', '.join(fields)} FROM Contact WHERE {where} ORDER BY AccountId, Name"
            raw = sf.query_all(soql)
            for r in raw.get("records", []):
                acc = r.get("Account") or {}
                rows_local.append({
                    "account_id": r.get("AccountId"),
                    "site": acc.get("Name"),
                    "contact_id": r.get("Id"),
                    "contact_name": r.get("Name"),
                    "email": r.get("Email"),
                    "phone": r.get("Phone"),
                    "title": r.get("Title"),
                    "department": r.get("Department"),
                })
        # De-duplicar por contact_id
        uniq = {}
        for r in rows_local:
            cid = r.get("contact_id") or r.get("ContactId") or r.get("Id")
            uniq[cid or len(uniq)] = r
        return list(uniq.values())

    rows: List[Dict[str, Any]] = []

    # Path A: valid Account Id
    if account_id and _is_valid_sf_id(account_id, prefix="001"):
        account_ids = [account_id]
        try:
            if include_subaccounts:
                rt_sub_cfg = os.environ.get("SF_ACCOUNT_RT_SUB", "SubAccount").strip()
                rt_list = [s.strip() for s in rt_sub_cfg.split(",") if s.strip()]
                if not rt_list:
                    rt_list = ["SubAccount"]
                if len(rt_list) == 1:
                    rt_clause = f"RecordType.DeveloperName = '{rt_list[0]}'"
                else:
                    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
                    rt_clause = f"RecordType.DeveloperName IN ({rt_vals})"
                acct_type_field = os.environ.get("SF_ACCOUNT_TYPE_FIELD", "C_Type__c").strip() or "C_Type__c"
                clinical_val = os.environ.get("SF_ACCOUNT_TYPE_CLINICAL", "Clinical").strip() or "Clinical"
                soql_children = (
                    "SELECT Id FROM Account "
                    f"WHERE ParentId = '{account_id}' AND {rt_clause} "
                    f"AND {acct_type_field} = '{clinical_val}'"
                )
                res = sf.query_all(soql_children)
                for rec in res.get("records", []):
                    cid = rec.get("Id")
                    if cid:
                        account_ids.append(cid)
        except Exception as e:
            _dbg("WARN: listing subaccounts failed: %s", e)
        rows = _list_contacts_for_accounts(account_ids)
    else:
        # Path B: Geography search (country/city) when no valid Account Id
        geo_country = (country or "").strip() or (account_id if account_id and len(account_id) > 2 else "")
        geo_city = (city or "").strip()
        rt_sub_cfg = os.environ.get("SF_ACCOUNT_RT_SUB", "SubAccount").strip()
        rt_list = [s.strip() for s in rt_sub_cfg.split(",") if s.strip()] or ["SubAccount"]
        if len(rt_list) == 1:
            rt_clause = f"RecordType.DeveloperName = '{rt_list[0]}'"
        else:
            rt_vals = ", ".join([f"'{x}'" for x in rt_list])
            rt_clause = f"RecordType.DeveloperName IN ({rt_vals})"
        acct_type_field = os.environ.get("SF_ACCOUNT_TYPE_FIELD", "C_Type__c").strip() or "C_Type__c"
        clinical_val = os.environ.get("SF_ACCOUNT_TYPE_CLINICAL", "Clinical").strip() or "Clinical"
        inactive_clause = "(Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
        where_parts = [rt_clause, f"{acct_type_field} = '{clinical_val}'", inactive_clause]
        if geo_country:
            gc = geo_country.replace("'", "\'")
            where_parts.append(f"ShippingCountry = '{gc}'")
        if geo_city:
            gcity = geo_city.replace("'", "\'")
            where_parts.append(f"ShippingCity = '{gcity}'")
        if not geo_country and not geo_city:
            # Global search across all clinical subaccounts (limited)
            soql_accounts = "SELECT Id FROM Account WHERE " + " AND ".join(where_parts) + " LIMIT 1000"
        else:
            soql_accounts = "SELECT Id FROM Account WHERE " + " AND ".join(where_parts) + " LIMIT 500"
        acc_res = sf.query_all(soql_accounts)
        acc_ids = [r.get("Id") for r in acc_res.get("records", []) if r.get("Id")]
        rows = _list_contacts_for_accounts(acc_ids)

    # Enrich with Activities per Account (Activity Opportunities)
    try:
        acc_ids = sorted({str(r.get("account_id")) for r in rows if r.get("account_id")})
        if acc_ids:
            def _chunks(xs, n=120):
                buf=[]
                for x in xs:
                    if x: buf.append(x)
                    if len(buf)>=n:
                        yield buf; buf=[]
                if buf: yield buf
            act_by_acc: Dict[str, List[str]] = {}
            for ch in _chunks(acc_ids):
                ids_in = ", ".join(f"'{x}'" for x in ch)
                soql = (
                    "SELECT AccountId, Name FROM Opportunity "
                    f"WHERE AccountId IN ({ids_in}) AND (RecordType.DeveloperName = 'Activity' OR Type = 'Activity')"
                )
                res = sf.query_all(soql)
                for r0 in res.get("records", []):
                    aid = str(r0.get("AccountId") or "")
                    nm = (r0.get("Name") or "").strip()
                    if not aid or not nm:
                        continue
                    lst = act_by_acc.setdefault(aid, [])
                    if nm in lst or len(lst) >= 15:
                        continue
                    lst.append(nm)
            if act_by_acc:
                for r in rows:
                    aid = str(r.get("account_id") or "")
                    names = act_by_acc.get(aid) or []
                    if names:
                        r["activities_names"] = "; ".join(names)
                        r["activities_count"] = len(names)
    except Exception as _e:
        pass

    table = {
        "columns": [
            {"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},
            {"key":"contact_id","label":"Contact Id"},
            {"key":"contact_name","label":"Contact Name"},
            {"key":"email","label":"Email"},
            {"key":"phone","label":"Phone"},
            {"key":"title","label":"Title"},
            {"key":"department","label":"Department"},
            {"key":"role","label":"Role"},
            {"key":"activities_names","label":"Activities"},
            {"key":"activities_count","label":"Activities Count"},
        ],
        "rows": rows,
    }
    return _normalize_table_for_ui(table)


def tool_rank_sites_by_group(
    db: Session,
    sf,
    metric: str,
    group_by: Literal["country","city"] = "country",
    top_n: int = 3,
    order: Literal["asc","desc"] = "desc",
):
    """Top-N per group (country/city) for both SF and site_qual metrics."""
    meta = _resolve_metric(metric, db)
    dir_sql = "ASC" if str(order).lower() == "asc" else "DESC"
    grp_col = "country" if group_by == "country" else "city"

    # -- site_qual path --
    if meta.get("source") == "site_qual":
        key = meta.get("key")
        grp = f"s.{grp_col}"
        sql = f"""
            WITH scored AS (
              SELECT
                {grp} AS grp,
                s.salesforce_account_id AS account_id,
                s.name AS site,
                s.country,
                s.city,
                COALESCE(NULLIF(regexp_replace(sq.data->>:key, '[^0-9\\.\\-]', '', 'g'), '')::numeric, 0) AS metric,
                ROW_NUMBER() OVER (PARTITION BY {grp} ORDER BY COALESCE(NULLIF(regexp_replace(sq.data->>:key, '[^0-9\\.\\-]', '', 'g'), '')::numeric, 0) {dir_sql} NULLS LAST) AS rn
              FROM public.sites s
              LEFT JOIN public.site_qual sq ON sq.site_id = s.id
              WHERE {grp} IS NOT NULL
            )
            SELECT grp, account_id, site, country, city, metric
            FROM scored
            WHERE rn <= :top
            ORDER BY grp, metric {dir_sql}
        """
        out = tool_sql_query(db, sql, {"key": key, "top": int(top_n)})
        cols = out.get("columns") or []
        rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
        table = {
            "columns": [
                {"key":"group","label": group_by.title()},
                {"key":"account_id","label":"Account Id"},
                {"key":"site","label":"Account Name"},
                {"key":"country","label":"Country"},
                {"key":"city","label":"City"},
                {"key":f"qual.{key}","label": _pretty_label(f"qual.{key}")}
            ],
            "rows": [
                {
                    "group": r.get("grp"),
                    "account_id": r.get("account_id"),
                    "site": r.get("site"),
                    "country": r.get("country"),
                    "city": r.get("city"),
                    f"qual.{key}": r.get("metric")
                } for r in rows
            ],
        }
        return _normalize_table_for_ui(table)

    # -- SF path --
    field = meta.get("field")
    if not sf:
        raise HTTPException(400, "No active Salesforce session for SF ranking")
    grp_field = f"Account.Shipping{group_by.title()}"
    soql = f"""
        SELECT
            {grp_field} grp,
            Account.Id,
            Account.Name,
            Account.ShippingCountry,
            Account.ShippingCity,
            MAX({field}) metric
        FROM Opportunity
        WHERE {field} != null AND {grp_field} != null
        GROUP BY {grp_field}, Account.Id, Account.Name, Account.ShippingCountry, Account.ShippingCity
        ORDER BY {grp_field}, metric {dir_sql} NULLS LAST
        LIMIT 500
    """
    raw = tool_salesforce_query(sf, soql)
    records = raw.get("records", []) if isinstance(raw, dict) else []

    # Group and take top N per group
    from itertools import groupby
    grouped = {}
    for r in records:
        acc = r.get("Account") or {}
        # Try multiple ways to get the group value
        g = r.get("grp") or r.get("expr0")
        # If not found, try getting it from the Account directly
        if not g and group_by == "country":
            g = acc.get("ShippingCountry")
        elif not g and group_by == "city":
            g = acc.get("ShippingCity")
        if not g:
            continue
        grouped.setdefault(g, [])
        if len(grouped[g]) < int(top_n):
            grouped[g].append({
                "group": g,
                "account_id": acc.get("Id"),
                "site": acc.get("Name"),
                "country": acc.get("ShippingCountry"),
                "city": acc.get("ShippingCity"),
                f"sf.{field}": r.get("expr1") or r.get("metric"),
            })

    rows = [item for items in grouped.values() for item in items]
    table = {
        "columns": [
            {"key":"group","label": group_by.title()},
            {"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},
            {"key":"country","label":"Country"},
            {"key":"city","label":"City"},
            {"key":f"sf.{field}","label":_pretty_label(f"sf.{field}")},
        ],
        "rows": rows,
    }
    return _normalize_table_for_ui(table)


def tool_group_count(
    db: Session,
    by: List[Literal["country","city"]],
    where: Optional[Dict[str, Any]] = None,
):
    by = by or ["country"]
    cols = ["s.country" if b == "country" else "s.city" for b in by]
    sel = ", ".join(cols)
    grp = ", ".join(cols)
    where_sql = "1=1"
    params: Dict[str, Any] = {}
    if where and where.get("key"):
        meta = _resolve_metric(where.get("key"), db)
        if meta.get("source") != "site_qual":
            raise HTTPException(400, "group_count only supports site_qual filters for now")
        k = meta.get("key")
        if where.get("exists"):
            where_sql = f"(sq.data ? :wkey)"
            params["wkey"] = k
        else:
            op = str(where.get("op") or ">=").upper()
            if op not in {">","<",">=","<=","=","!="}:
                op = ">="
            where_sql = f"COALESCE(NULLIF(regexp_replace(sq.data->>:wkey, '[^0-9\\.\\-]', '', 'g'), '')::numeric, 0) {op} :wval"
            params.update({"wkey": k, "wval": where.get("value", 0)})
    sql = f"""
        SELECT {sel}, COUNT(*) AS sites
        FROM public.sites s
        LEFT JOIN public.site_qual sq ON sq.site_id = s.id
        WHERE {where_sql}
        GROUP BY {grp}
        ORDER BY {grp}
    """
    out = tool_sql_query(db, sql, params)
    c = out.get("columns") or []
    rows = [{c[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
    result_rows = []
    for r in rows:
        rr = {"sites": r.get("sites")}
        for i,b in enumerate(by):
            rr[b] = r.get(cols[i])
        result_rows.append(rr)
    return {"columns": [{"key":b, "label": b.title()} for b in by] + [{"key":"sites","label":"Sites"}], "rows": result_rows}


def tool_group_count_agg(
    db: Session,
    by: List[Literal["country","city"]],
    metric: Optional[str] = None,
    agg: Literal["avg","sum","ratio_exists"] = "avg",
):
    by = by or ["country"]
    cols = ["s.country" if b == "country" else "s.city" for b in by]
    sel = ", ".join(cols)
    grp = ", ".join(cols)
    if agg == "ratio_exists":
        if not metric:
            raise HTTPException(400, "metric required for ratio_exists")
        meta = _resolve_metric(metric, db)
        if meta.get("source") != "site_qual":
            raise HTTPException(400, "ratio_exists only supports site_qual")
        k = meta.get("key")
        sql = f"""
            SELECT {sel},
               COUNT(*) FILTER (WHERE sq.data ? :k) * 1.0 / NULLIF(COUNT(*),0) AS ratio
            FROM public.sites s
            LEFT JOIN public.site_qual sq ON sq.site_id = s.id
            GROUP BY {grp}
            ORDER BY {grp}
        """
        out = tool_sql_query(db, sql, {"k": k})
        c = out.get("columns") or []
        rows = [{c[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
        res = []
        for r in rows:
            rr = {"ratio": float(r.get("ratio") or 0)}
            for i,b in enumerate(by): rr[b] = r.get(cols[i])
            res.append(rr)
        return {"columns": [{"key":b,"label":b.title()} for b in by] + [{"key":"ratio","label":"Ratio"}], "rows": res}
    # avg/sum
    if not metric:
        raise HTTPException(400, "metric required for avg/sum")
    meta = _resolve_metric(metric, db)
    if meta.get("source") != "site_qual":
        raise HTTPException(400, "only site_qual supported here")
    k = meta.get("key")
    func = "AVG" if agg == "avg" else "SUM"
    sql = f"""
        SELECT {sel}, {func}(COALESCE(NULLIF(regexp_replace(sq.data->>:k, '[^0-9\\.\\-]', '', 'g'), '')::numeric, 0)) AS value
        FROM public.sites s
        LEFT JOIN public.site_qual sq ON sq.site_id = s.id
        GROUP BY {grp}
        ORDER BY {grp}
    """
    out = tool_sql_query(db, sql, {"k": k})
    c = out.get("columns") or []
    rows = [{c[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
    res = []
    for r in rows:
        rr = {"value": float(r.get("value") or 0)}
        for i,b in enumerate(by): rr[b] = r.get(cols[i])
        res.append(rr)
    return {"columns": [{"key":b,"label":b.title()} for b in by] + [{"key":"value","label":_pretty_label(f"qual.{k}") + f" ({agg})"}], "rows": res}


# ==== Salesforce groupers (counts and aggregations) ====

def tool_group_count_sf(
    sf,
    by: List[Literal["country","city"]] = ["country"],
):
    if not sf:
        raise HTTPException(400, "No SF session")
    by = by or ["country"]
    dim = by[0]
    # Always restrict to clinical subaccounts and active
    inactive_clause = "(Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
    if dim == "country":
        soql = (
            "SELECT ShippingCountry country, COUNT(Id) "
            "FROM Account "
            "WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
            "AND ShippingCountry != null AND " + inactive_clause + " "
            "GROUP BY ShippingCountry ORDER BY COUNT(Id) DESC"
        )
        raw = tool_salesforce_query(sf, soql)
        recs = raw.get("records", []) if isinstance(raw, dict) else []
        rows = [{"country": r.get("country"), "count": r.get("expr0") or r.get("count") or r.get("COUNT") } for r in recs]
        return {"columns": [{"key":"country","label":"Country"},{"key":"count","label":"Count"}], "rows": rows}
    else:
        # city: include country as context
        soql = (
            "SELECT ShippingCity city, ShippingCountry country, COUNT(Id) "
            "FROM Account "
            "WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
            "AND ShippingCity != null AND " + inactive_clause + " "
            "GROUP BY ShippingCity, ShippingCountry ORDER BY COUNT(Id) DESC"
        )
        raw = tool_salesforce_query(sf, soql)
        recs = raw.get("records", []) if isinstance(raw, dict) else []
        rows = [{"city": r.get("city"), "country": r.get("country"), "count": r.get("expr0") or r.get("count") } for r in recs]
        return {"columns": [{"key":"city","label":"City"},{"key":"country","label":"Country"},{"key":"count","label":"Count"}], "rows": rows}


def tool_group_agg_sf(
    sf,
    by: List[Literal["country","city"]] = ["country"],
    field: str = "",
    agg: Literal["sum","max","avg"] = "avg",
):
    if not sf:
        raise HTTPException(400, "No SF session")
    if not field:
        raise HTTPException(400, "Missing field")
    func = {"sum":"SUM","max":"MAX","avg":"AVG"}[agg]
    dim = (by or ["country"])[0]
    grp_field = "Account.ShippingCountry" if dim == "country" else "Account.ShippingCity"
    # Validate SOQL field usage (on Opportunity) via whitelist
    _validate_soql(f"SELECT {field} FROM Opportunity", sf)
    soql = f"""
        SELECT {grp_field} grp, {func}({field}) value
        FROM Opportunity
        WHERE {field} != null AND {grp_field} != null
        GROUP BY {grp_field}
        ORDER BY value DESC
    """
    raw = tool_salesforce_query(sf, soql)
    recs = raw.get("records", []) if isinstance(raw, dict) else []
    out_rows = []
    for r in recs:
        g = r.get("grp") or r.get("expr0")
        val = r.get("value") or r.get("expr1")
        if dim == "country":
            out_rows.append({"country": g, f"sf.{field}": val})
        else:
            out_rows.append({"city": g, f"sf.{field}": val})
    cols = (
        [{"key":"country","label":"Country"}] if dim=="country" else [{"key":"city","label":"City"}]
    ) + [{"key": f"sf.{field}", "label": _pretty_label(f"sf.{field}") + f" ({agg})"}]
    return {"columns": cols, "rows": out_rows}

def tool_time_series_sf(
    sf,
    field: str,
    date_field: str = "CloseDate",
    period: Literal["month","quarter","year"] = "month",
    agg: Literal["sum","max","avg"] = "sum",
    last_n: Optional[int] = None,
):
    if not sf:
        raise HTTPException(400, "No SF session")
    func = {"sum":"SUM","max":"MAX","avg":"AVG"}[agg]
    per_fn = {"month":"CALENDAR_MONTH","quarter":"CALENDAR_QUARTER","year":"CALENDAR_YEAR"}[period]
    where = f"WHERE {field} != null"
    if last_n and period in ("month","quarter"):
        # filtrar últimos N unidades aproximando por CreatedDate/CloseDate
        where += " AND LastModifiedDate = LAST_N_MONTHS:%d" % (last_n if period=="month" else last_n*3)
    soql = f"""
        SELECT {per_fn}({date_field}) per, {func}({field}) metric
        FROM Opportunity
        {where}
        GROUP BY {per_fn}({date_field})
        ORDER BY {per_fn}({date_field})
    """
    _validate_soql(f"SELECT {date_field} FROM Opportunity", sf)  # asegura date field permitido
    raw = tool_salesforce_query(sf, soql)
    recs = raw.get("records", []) if isinstance(raw, dict) else []
    rows = []
    for r in recs:
        rows.append({"period": r.get("expr0") or r.get("per"), f"sf.{field}": r.get("expr1") or r.get("metric")})
    return {"columns": [{"key":"period","label":"Period"},{"key":f"sf.{field}","label":_pretty_label(f"sf.{field}")}], "rows": rows}


def tool_sql_query_fill_sf(
    db: Session,
    sf,
    sql: str,
    account_fields: List[str],
    params: Optional[Dict[str, Any]] = None,
):
    """Ejecuta SQL (debe devolver account_id) y rellena columnas Account.* desde SF en lote."""
    base = tool_sql_query(db, sql, params or {})
    cols = base.get("columns") or []
    rows = [{cols[i]: v for i, v in enumerate(r)} for r in base.get("rows") or []]
    ids = list({str(r.get("account_id")) for r in rows if r.get("account_id")})
    if sf and ids and account_fields:
        fields = [f for f in account_fields if f and f != "Id"]
        ids_clause = ', '.join([f"'{i}'" for i in ids])
        soql = f"SELECT Id, {', '.join(fields)} FROM Account WHERE Id IN ({ids_clause})"
        accs = tool_salesforce_query(sf, soql).get("records", [])
        m = {a.get("Id"): a for a in accs}
        for r in rows:
            aid = str(r.get("account_id") or "")
            a = m.get(aid) or {}
            for f in fields:
                r[f"sf.Account.{f}"] = a.get(f)
    return {"columns": [{"key":k, "label": _pretty_label(k)} for k in (list(rows[0].keys()) if rows else cols)], "rows": rows}


def tool_contacts_by_group(
    sf,
    roles: Optional[List[str]] = None,
    title_contains: Optional[str] = None,
    group_by: Literal["country","city"] = "country",
    top_n: int = 1,
):
    if not sf:
        raise HTTPException(400, "No SF session")
    group_field = "Account.ShippingCountry" if group_by=="country" else "Account.ShippingCity"
    role_filter = ""
    if roles:
        role_vals = ", ".join([f"'{r}'" for r in roles])
        role_filter = f" AND Role__c IN ({role_vals})"
    title_filter = ""
    if title_contains:
        title_escaped = title_contains.replace("'", "\\'")
        title_filter = f" AND Contact.Title LIKE '%{title_escaped}%'"
    soql = f"""
        SELECT {group_field} grp, Contact.Name, Contact.Email, Contact.Phone, Role__c, Contact.Title, LastModifiedDate
        FROM AccountContactRelation
        WHERE {group_field} != null {role_filter} {title_filter}
        ORDER BY {group_field}, LastModifiedDate DESC
    """
    raw = tool_salesforce_query(sf, soql)
    recs = raw.get("records", []) if isinstance(raw, dict) else []
    out = {}
    for r in recs:
        g = r.get("Account", {}).get("ShippingCountry" if group_by=="country" else "ShippingCity")
        out.setdefault(g, [])
        if len(out[g]) < top_n:
            out[g].append({
                "group": g,
                "contact_name": (r.get("Contact") or {}).get("Name"),
                "email": (r.get("Contact") or {}).get("Email"),
                "phone": (r.get("Contact") or {}).get("Phone"),
                "role": r.get("Role__c"),
                "title": (r.get("Contact") or {}).get("Title"),
            })
    rows = [item for sub in out.values() for item in sub]
    return {"columns": [
        {"key":"group","label": group_by.title()},
        {"key":"contact_name","label":"Contact"},{"key":"email","label":"Email"},{"key":"phone","label":"Phone"},{"key":"role","label":"Role"},{"key":"title","label":"Title"}
    ], "rows": rows}


def tool_qual_search(
    db: Session,
    text: str,
    limit: int = 50,
):
    """Semantic search over qualification comments using GIN tsv index with ILIKE fallback."""
    if not text or not text.strip():
        return {"columns": [], "rows": []}
    sql = """
      WITH q AS (SELECT plainto_tsquery('simple', :q) AS query)
      SELECT s.salesforce_account_id AS account_id,
             s.name AS site,
             s.country,
             s.city,
             ts_rank(sq.comments_tsv, q.query) AS rank,
             ts_headline('simple', public.qual_concat_comments(sq.data), q.query) AS snippet
      FROM public.site_qual sq
      JOIN public.sites s ON s.id = sq.site_id, q
      WHERE sq.comments_tsv @@ q.query
         OR public.qual_concat_comments(sq.data) ILIKE :pat
      ORDER BY rank DESC
      LIMIT :lim
    """
    out = tool_sql_query(db, sql, {"q": text, "pat": f"%{text}%", "lim": int(limit)})
    c = out.get("columns") or []
    rows = [{c[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
    return {
        "columns": [
            {"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},
            {"key":"country","label":"Country"},
            {"key":"city","label":"City"},
            {"key":"rank","label":"Rank"},
            {"key":"snippet","label":"Snippet"}
        ],
        "rows": rows,
    }


def tool_salesforce_assignments(sf, account_ids: List[str], active_only: bool = False, last_n_months: Optional[int] = None):
    """Return assignments per Account using the existing extras core.
    Filters: active_only by simple heuristic on stage; last_n_months by created date when available.
    Also falls back to Opportunities with RecordType.DeveloperName='Activity' when the Assignment__c object is not populated.
    """
    if not account_ids:
        raise HTTPException(400, "Missing account_ids")
    out_rows: List[Dict[str, Any]] = []
    from datetime import datetime, timedelta
    cutoff = None
    if last_n_months and last_n_months > 0:
        cutoff = datetime.utcnow() - timedelta(days=int(last_n_months)*30)
    for aid in account_ids:
        acc_name: Optional[str] = None
        try:
            data = _account_extras_core(sf, aid)
            acc_name = (data.get("member") or {}).get("name") or data.get("account_name")
            assignments = data.get("assignments") or []
            for a in assignments:
                name = a.get("name") or a.get("opportunity_name") or a.get("id")
                stage = a.get("stage") or a.get("type") or ""
                created = a.get("created")
                if cutoff and created:
                    try:
                        dt = datetime.fromisoformat(str(created).replace("Z","+00:00"))
                        if dt < cutoff:
                            continue
                    except Exception:
                        pass
                if active_only and isinstance(stage, str) and stage:
                    if any(s in stage.lower() for s in ("closed", "won", "lost", "inactive")):
                        continue
                out_rows.append({
                    "account_id": aid,
                    "site": acc_name,
                    "assignment_name": name,
                    "stage": a.get("stage"),
                    "type": a.get("type"),
                    "opportunity_name": a.get("opportunity_name"),
                    "created": created,
                })
        except Exception as e:
            _dbg("WARN: extras for %s failed: %s", aid, e)
        # Fallback: Opportunities with RecordType 'Activity'
        try:
            fields = ["Id","Name","StageName","Type","CreatedDate","RecordType.DeveloperName"]
            import os
            rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity,RT_Activity").strip()
            rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
            # Ensure both Activity and RT_Activity are always included
            base = {"Activity", "RT_Activity"}
            rt_list = list(base | set(rt_list))
            rt_vals = ", ".join([f"'{x}'" for x in rt_list])
            rt_clause = f"RecordType.DeveloperName IN ({rt_vals})"
            soql = (
                f"SELECT {', '.join(fields)} FROM Opportunity "
                f"WHERE AccountId = '{aid}' AND {rt_clause} "
                f"ORDER BY CreatedDate DESC LIMIT 200"
            )
            recs = sf.query_all(soql).get("records", [])
            for r in recs:
                created = r.get("CreatedDate")
                if cutoff and created:
                    try:
                        dt = datetime.fromisoformat(str(created).replace("Z","+00:00"))
                        if dt < cutoff:
                            continue
                    except Exception:
                        pass
                stage = r.get("StageName")
                if active_only and isinstance(stage, str) and stage:
                    if any(s in stage.lower() for s in ("closed", "won", "lost", "inactive")):
                        continue
                out_rows.append({
                    "account_id": aid,
                    "site": acc_name,
                    "assignment_name": r.get("Name"),
                    "stage": stage,
                    "type": r.get("Type"),
                    "opportunity_name": r.get("Name"),
                    "created": created,
                })
        except Exception as e:
            _dbg("WARN: activity opp fallback failed for %s: %s", aid, e)
    table = {
        "columns": [
            {"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},
            {"key":"assignment_name","label":"Assignment"},
            {"key":"stage","label":"Stage"},
            {"key":"type","label":"Type"},
            {"key":"opportunity_name","label":"Opportunity"},
            {"key":"created","label":"Created"},
        ],
        "rows": out_rows,
    }
    return _normalize_table_for_ui(table)


# ---- Activities / Sites tools ----
def tool_sites_by_activity(
    sf,
    name: str,
    countries: Optional[List[str]] = None,
    exact: bool = False,
):
    if not sf:
        raise HTTPException(400, "No SF session")
    if not name or not str(name).strip():
        raise HTTPException(400, "Missing activity/opportunity name")
    
    # Build country filter (sanitize inputs)
    country_cond = ""
    def _clean_countries_local(arr: Optional[List[str]]) -> List[str]:
        if not arr:
            return []
        out: List[str] = []
        for c in arr:
            t = str(c or '').strip()
            if not t:
                continue
            t = re.sub(r"(?i)^activity\s+['\"]?.+?['\"]?\s+in\s+", "", t)
            if " in " in t.lower():
                t = t.split(" in ")[-1].strip()
            if any(x in t.lower() for x in ("activity","screen","stage")):
                continue
            out.append(t)
        return out
    if countries:
        safe = _clean_countries_local(countries)
        vals = ", ".join(["'" + str(c).strip().replace("'", "\\'") + "'" for c in safe if str(c).strip()])
        if vals:
            country_cond = f" AND C_Account__r.ShippingCountry IN ({vals})"
    
    nm = str(name).strip().replace("'", "\\'")
    # Case-insensitive matching against Assignment__c → C_Opportunity_Name__r.Name
    if exact:
        cond_name = f"C_Opportunity_Name__r.Name = '{nm}'"
    else:
        variants = {nm}
        try:
            variants.update({nm.lower(), nm.upper(), nm.title()})
            # Fuzzy: strip doubled consonants (e.g. "barricade" → "baricade")
            _dedup = re.sub(r'([bcdfghjklmnpqrstvwxyz])\1+', r'\1', nm.lower(), flags=re.I)
            if _dedup != nm.lower():
                variants.add(_dedup)
                variants.add(_dedup.title())
        except Exception:
            pass
        like_parts = [f"C_Opportunity_Name__r.Name LIKE '%{v}%'" for v in sorted(variants) if v]
        cond_name = " OR ".join(like_parts) if like_parts else f"C_Opportunity_Name__r.Name LIKE '%{nm}%'"
    
    # Prefer Assignment__c linkage (more accurate site participation)
    asn_soql = (
        "SELECT Id, C_Account__c, C_Account__r.Name, C_Account__r.ShippingCountry, C_Account__r.ShippingCity, "
        "C_Opportunity_Name__r.Name "
        "FROM Assignment__c "
        f"WHERE ({cond_name}) AND C_Account__c != null{country_cond}"
    )
    try:
        asn_recs = sf.query_all(asn_soql).get("records", [])
    except Exception as _e:
        asn_recs = []
    rows: List[Dict[str, Any]] = []
    if asn_recs:
        for r in asn_recs:
            acc = r.get("C_Account__r") or {}
            rows.append({
                "account_id": r.get("C_Account__c"),
                "site": acc.get("Name"),
                "country": acc.get("ShippingCountry"),
                "city": acc.get("ShippingCity"),
                "activity_name": (r.get("C_Opportunity_Name__r") or {}).get("Name"),
            })
    else:
        # Fallback to Opportunity direct match if no assignments found
        import os
        rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity,RT_Activity").strip()
        rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()] or ["Activity","RT_Activity"]
        if "Activity" not in rt_list: rt_list.append("Activity")
        if "RT_Activity" not in rt_list: rt_list.append("RT_Activity")
        rt_vals = ", ".join([f"'{x}'" for x in rt_list])
        country_sub = ""
        if country_cond:
            # convert to Account subquery filter form
            vals = re.search(r"IN \((.*)\)", country_cond)
            country_sub = f" AND ShippingCountry IN ({vals.group(1)})" if vals else ""
        soql = (
            "SELECT Id, AccountId, Name "
            "FROM Opportunity "
            "WHERE AccountId IN ("
            "  SELECT Id FROM Account "
            "  WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
            "    AND (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
            f"    {country_sub}"
            ") "
            f"AND (RecordType.DeveloperName IN ({rt_vals}) OR Type = 'Activity') "
            f"AND ( {cond_name.replace('C_Opportunity_Name__r.', '')} ) "
            "ORDER BY Name"
        )
        recs = tool_salesforce_query(sf, soql).get("records", [])
        acc_ids = sorted({r.get("AccountId") for r in recs if r.get("AccountId")})
        acc_map = _build_account_map(sf, acc_ids) if acc_ids else {}
        for r in recs:
            aid = r.get("AccountId")
            a = acc_map.get(aid, {})
            rows.append({
                "account_id": aid,
                "site": a.get("name"),
                "country": a.get("country"),
                "city": a.get("city"),
                "activity_name": r.get("Name"),
            })
    return _normalize_table_for_ui({
        "columns": [
            {"key":"account_id","label":"Account Id"},{"key":"site","label":"Account Name"},{"key":"country","label":"Country"},{"key":"city","label":"City"},{"key":"activity_name","label":"Activity"}
        ],
        "rows": rows,
    })


def tool_sites_with_any_activity(
    sf,
    countries: Optional[List[str]] = None,
):
    """Return sites (clinical SubAccounts) that have any Activity via Assignments.
    Uses RT_Activity Opportunities → Assignment__c → Clinical SubAccounts.
    Optional countries filter is sanitized to avoid phrases like 'those activities'.
    """
    if not sf:
        raise HTTPException(400, "No SF session")
    
    import os, re
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["RT_Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    
    # Get all RT_Activity Opportunities (these are the Activities)
    activity_soql = f"SELECT Id, Name FROM Opportunity WHERE RecordType.DeveloperName IN ({rt_vals})"
    activities = tool_salesforce_query(sf, activity_soql).get("records", [])
    activity_ids = [a.get("Id") for a in activities if a.get("Id")]
    
    if not activity_ids:
        return _normalize_table_for_ui({
            "columns": [
                {"key":"activity_name","label":"Activity"},{"key":"account_id","label":"Account Id"},
                {"key":"site","label":"Account Name"},{"key":"country","label":"Country"},{"key":"city","label":"City"}
            ],
            "rows": [],
        })
    
    # Get Assignments linking Activities to SubAccounts
    activity_id_list = ",".join([f"'{aid}'" for aid in activity_ids])
    assignment_soql = (
        f"SELECT Id, C_Opportunity_Name__c, C_Account__c, C_Opportunity_Name__r.Name "
        f"FROM Assignment__c "
        f"WHERE C_Opportunity_Name__c IN ({activity_id_list}) "
        f"AND C_Account__c != null"
    )
    # Use sf.query_all directly (Assignment__c not in allowed SOQL objects)
    _dbg("[ASSIGNMENT QUERY] %s", assignment_soql)
    assignments = sf.query_all(assignment_soql).get("records", [])
    
    # Build map: AccountId → [Activity names]
    account_activities: Dict[str, List[str]] = {}
    for asn in assignments:
        acc_id = asn.get("C_Account__c")
        activity_name = (asn.get("C_Opportunity_Name__r") or {}).get("Name") or "Unknown"
        if acc_id:
            if acc_id not in account_activities:
                account_activities[acc_id] = []
            account_activities[acc_id].append(activity_name)
    
    if not account_activities:
        return _normalize_table_for_ui({
            "columns": [
                {"key":"activity_name","label":"Activity"},{"key":"account_id","label":"Account Id"},
                {"key":"site","label":"Account Name"},{"key":"country","label":"Country"},{"key":"city","label":"City"}
            ],
            "rows": [],
        })
    
    # Sanitize countries input: drop generic words and keep plausible names only
    def _clean_countries(arr: Optional[List[str]]) -> List[str]:
        if not arr:
            return []
        bad = {"those","these","this","that","activity","activities","involved","with","the"}
        out: List[str] = []
        for c in arr:
            if not c:
                continue
            t = re.sub(r"\s+", " ", str(c)).strip()
            if not t:
                continue
            # strip quoted activity fragments: e.g., Activity 'Screen' in France -> France
            t = re.sub(r"(?i)^activity\s+['\"]?.+?['\"]?\s+in\s+", "", t)
            # if token contains " in ", take the last segment as country hint
            if " in " in t.lower():
                t = t.split(" in ")[-1].strip()
            # drop tokens that obviously aren't country names
            if any(x in t.lower() for x in ("activity","screen","with","stage")):
                continue
            if t.lower() in bad:
                continue
            if re.search(r"activities?$", t, flags=re.I):
                continue
            out.append(t)
        return out
    safe_countries = _clean_countries(countries)
    
    # Enrich Accounts (filter by Clinical SubAccount if needed)
    acc_ids = list(account_activities.keys())
    acc_id_list = ",".join([f"'" + str(aid) + "'" for aid in acc_ids])
    country_filter = ""
    if safe_countries:
        country_vals = ", ".join(["'" + str(c).strip().replace("'", "\\'") + "'" for c in safe_countries if str(c).strip()])
        if country_vals:
            country_filter = f" AND ShippingCountry IN ({country_vals})"
    
    acc_soql = (
        f"SELECT Id, Name, ShippingCity, ShippingCountry "
        f"FROM Account "
        f"WHERE Id IN ({acc_id_list}) "
        f"AND RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
        f"AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
        f"AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
        f"{country_filter}"
    )
    accounts = tool_salesforce_query(sf, acc_soql).get("records", [])
    
    # Build rows: one row per (site, activity) pair
    rows = []
    for acc in accounts:
        acc_id = acc.get("Id")
        activities_list = account_activities.get(acc_id, [])
        for activity_name in activities_list:
            rows.append({
                "activity_name": activity_name,
                "account_id": acc_id,
                "site": acc.get("Name"),
                "country": acc.get("ShippingCountry"),
                "city": acc.get("ShippingCity"),
            })
    
    return _normalize_table_for_ui({
        "columns": [
            {"key":"activity_name","label":"Activity"},{"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},{"key":"country","label":"Country"},{"key":"city","label":"City"}
        ],
        "rows": rows,
    })


def tool_list_all_activities(sf, name_like: str = None, account_name_like: str = None):
    """List Activities (RT_Activity/Activity Opportunities).
    If name_like is given, filters by Name LIKE '%name_like%' (up to 2000).
    If account_name_like is given, filters by Account.Name LIKE '%account_name_like%' (sponsor/pharma company).
    Otherwise returns up to 500 alphabetically."""
    if not sf:
        raise HTTPException(400, "No SF session")

    import os
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity,RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()] or ["Activity","RT_Activity"]
    if "Activity" not in rt_list:
        rt_list.append("Activity")
    if "RT_Activity" not in rt_list:
        rt_list.append("RT_Activity")
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])

    where = f"(RecordType.DeveloperName IN ({rt_vals}) OR Type = 'Activity')"
    if name_like:
        safe = name_like.replace("'", "\\'")
        where += f" AND Name LIKE '%{safe}%'"
    if account_name_like:
        safe_acc = account_name_like.replace("'", "\\'")
        where += f" AND Account.Name LIKE '%{safe_acc}%'"
    limit = 2000 if (name_like or account_name_like) else 500

    soql = f"SELECT Id, Name, Account.Name FROM Opportunity WHERE {where} ORDER BY Name LIMIT {limit}"
    recs = tool_salesforce_query(sf, soql).get("records", [])

    rows = [
        {
            "activity_name": r.get("Name"),
            "sponsor": (r.get("Account") or {}).get("Name") or "",
            "activity_id": r.get("Id"),
        }
        for r in recs
    ]

    return _normalize_table_for_ui({
        "columns": [
            {"key":"activity_name","label":"Activity"},
            {"key":"sponsor","label":"Sponsor / Account"},
            {"key":"activity_id","label":"Activity Id"},
        ],
        "rows": rows,
    })


def tool_activities_with_countries(sf):
    """List Activities with the countries that participate (via Assignments to SubAccounts).
    Returns: Opportunity Name, Account Name, Countries (comma-separated), Site Count.
    """
    if not sf:
        raise HTTPException(400, "No SF session")
    
    import os
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["RT_Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    
    # Get all RT_Activity Opportunities
    activity_soql = f"SELECT Id, Name, Account.Name FROM Opportunity WHERE RecordType.DeveloperName IN ({rt_vals})"
    activities = tool_salesforce_query(sf, activity_soql).get("records", [])
    activity_ids = [a.get("Id") for a in activities if a.get("Id")]
    
    if not activity_ids:
        return _normalize_table_for_ui({
            "columns": [
                {"key":"opportunity_name","label":"Opportunity Name"},
                {"key":"account_name","label":"Account Name"},
                {"key":"countries","label":"Countries"},
                {"key":"site_count","label":"Site Count"},
            ],
            "rows": [],
        })
    
    # Get Assignments linking Activities to SubAccounts
    activity_id_list = ",".join([f"'{aid}'" for aid in activity_ids])
    assignment_soql = (
        f"SELECT C_Opportunity_Name__c, C_Account__c, C_Account__r.ShippingCountry "
        f"FROM Assignment__c "
        f"WHERE C_Opportunity_Name__c IN ({activity_id_list}) "
        f"AND C_Account__c != null"
    )
    # Use sf.query_all directly (Assignment__c not in allowed SOQL objects)
    _dbg("[ASSIGNMENT QUERY] %s", assignment_soql)
    assignments = sf.query_all(assignment_soql).get("records", [])
    
    # Build map: ActivityId → {countries: set, site_count: int}
    activity_map: Dict[str, Dict[str, Any]] = {}
    for a in activities:
        aid = a.get("Id")
        activity_map[aid] = {
            "opportunity_name": a.get("Name"),
            "account_name": (a.get("Account") or {}).get("Name"),
            "countries": set(),
            "site_count": 0,
        }
    
    # Aggregate countries and site count per Activity
    for asn in assignments:
        activity_id = asn.get("C_Opportunity_Name__c")
        country = (asn.get("C_Account__r") or {}).get("ShippingCountry")
        if activity_id in activity_map:
            if country:
                activity_map[activity_id]["countries"].add(country)
            activity_map[activity_id]["site_count"] += 1
    
    # Build rows
    rows = []
    for aid, data in activity_map.items():
        countries_str = ", ".join(sorted(data["countries"])) if data["countries"] else "(no sites)"
        rows.append({
            "opportunity_name": data["opportunity_name"],
            "account_name": data["account_name"],
            "countries": countries_str,
            "site_count": data["site_count"],
            "activity_id": aid,
        })
    
    # Sort by opportunity name
    rows = sorted(rows, key=lambda x: x.get("opportunity_name", ""))
    
    return _normalize_table_for_ui({
        "columns": [
            {"key":"opportunity_name","label":"Opportunity Name"},
            {"key":"account_name","label":"Account Name"},
            {"key":"countries","label":"Countries"},
            {"key":"site_count","label":"Site Count"},
            {"key":"activity_id","label":"Activity Id"},
        ],
        "rows": rows,
    })


def tool_activity_counts_by_country(sf):
    """Aggregate number of activities per country (each activity counted once per country)."""
    table = tool_activities_with_countries(sf)
    rows = table.get("rows") or []
    # country -> set(activity_id)
    agg: Dict[str, set] = {}
    for r in rows:
        aid = str(r.get("activity_id") or "")
        cstr = str(r.get("countries") or "")
        if not aid or not cstr or cstr == "(no sites)":
            continue
        for c in [t.strip() for t in cstr.split(",") if t.strip()]:
            agg.setdefault(c, set()).add(aid)
    out_rows = [{"country": k, "activities": len(v)} for k, v in agg.items()]
    out_rows = sorted(out_rows, key=lambda x: x.get("activities", 0), reverse=True)
    return {
        "columns": [{"key":"country","label":"Country"},{"key":"activities","label":"Activities"}],
        "rows": out_rows,
    }


def tool_activity_country_matrix(sf, stacked: bool = False):
    """Per-activity country counts based on Assignment__c.
    If stacked=True, returns one row per (activity,country) with 'sites' (assignments) for stacked bars.
    If stacked=False, returns per-activity totals + country list.
    """
    if not sf:
        raise HTTPException(400, "No SF session")
    import os
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["RT_Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    act_soql = f"SELECT Id, Name FROM Opportunity WHERE RecordType.DeveloperName IN ({rt_vals})"
    acts = tool_salesforce_query(sf, act_soql).get("records", [])
    id_to_name = {a.get("Id"): a.get("Name") for a in acts if a.get("Id")}
    if not id_to_name:
        return {"columns":[],"rows":[]}
    ids_in = ", ".join([f"'{i}'" for i in id_to_name.keys()])
    asn_soql = (
        "SELECT C_Opportunity_Name__c, C_Account__r.ShippingCountry "
        f"FROM Assignment__c WHERE C_Opportunity_Name__c IN ({ids_in}) AND C_Account__c != null"
    )
    recs = sf.query_all(asn_soql).get("records", [])
    # Build counts per (activity,country)
    per_ac: Dict[tuple, int] = {}
    for r in recs:
        aid = r.get("C_Opportunity_Name__c"); c = (r.get("C_Account__r") or {}).get("ShippingCountry")
        if not aid: continue
        key = (aid, c or "")
        per_ac[key] = per_ac.get(key, 0) + 1
    if stacked:
        rows = []
        for (aid, country), cnt in per_ac.items():
            rows.append({
                "activity_name": id_to_name.get(aid) or aid,
                "country": country,
                "sites": cnt,
                "activity_id": aid,
            })
        return {"columns":[{"key":"activity_name","label":"Activity"},{"key":"country","label":"Country"},{"key":"sites","label":"Sites"},{"key":"activity_id","label":"Activity Id"}],"rows": rows}
    else:
        # Aggregate totals + list of countries
        by_act: Dict[str, Dict[str, Any]] = {}
        for (aid, country), cnt in per_ac.items():
            d = by_act.setdefault(aid, {"activity_name": id_to_name.get(aid) or aid, "countries_count":0, "countries": set(), "activity_id": aid, "sites_total":0})
            d["sites_total"] += cnt
            if country:
                d["countries"].add(country)
                d["countries_count"] = len(d["countries"])
        rows = []
        for aid, d in by_act.items():
            rows.append({
                "activity_name": d["activity_name"],
                "countries_count": d.get("countries_count",0),
                "countries": ", ".join(sorted(d.get("countries", set()))),
                "sites_total": d.get("sites_total",0),
                "activity_id": aid,
            })
        rows.sort(key=lambda x: x.get("sites_total",0), reverse=True)
        return {"columns":[{"key":"activity_name","label":"Activity"},{"key":"countries_count","label":"Countries"},{"key":"countries","label":"Country List"},{"key":"sites_total","label":"Sites"},{"key":"activity_id","label":"Activity Id"}],"rows": rows}


def tool_activities_with_assignments_counts(
    sf,
    *,
    last_n_days: Optional[int] = None,
    last_n_months: Optional[int] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """List activities with total assignments and participating countries.
    Supports date filters (CreatedDate) via last_n_days/last_n_months or since/until (YYYY-MM-DD).
    Returns columns: activity_name, assignments, countries, activity_id."""
    if not sf:
        raise HTTPException(400, "No SF session")
    import os
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["RT_Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    # Fetch Activities
    activity_soql = f"SELECT Id, Name FROM Opportunity WHERE RecordType.DeveloperName IN ({rt_vals})"
    acts = tool_salesforce_query(sf, activity_soql).get("records", [])
    id_to_name = {a.get("Id"): a.get("Name") for a in acts if a.get("Id")}
    if not id_to_name:
        return {"columns":[{"key":"activity_name","label":"Activity"},{"key":"assignments","label":"Assignments"},{"key":"countries","label":"Countries"}],"rows":[]}
    ids_in = ",".join([f"'{i}'" for i in id_to_name.keys()])
    # Assignments with Account country
    date_cond = ""
    if isinstance(last_n_days, int) and last_n_days > 0:
        date_cond = f" AND CreatedDate = LAST_N_DAYS:{int(last_n_days)}"
    elif isinstance(last_n_months, int) and last_n_months > 0:
        date_cond = f" AND CreatedDate = LAST_N_MONTHS:{int(last_n_months)}"
    else:
        rng = []
        if since:
            rng.append(f"CreatedDate >= {since}T00:00:00Z")
        if until:
            rng.append(f"CreatedDate <= {until}T23:59:59Z")
        if rng:
            date_cond = " AND " + " AND ".join(rng)
    asn_soql = (
        "SELECT C_Opportunity_Name__c, C_Account__r.ShippingCountry, CreatedDate "
        "FROM Assignment__c WHERE C_Opportunity_Name__c IN (" + ids_in + ") AND C_Account__c != null" + date_cond
    )
    recs = sf.query_all(asn_soql).get("records", [])
    agg_counts: Dict[str, int] = {}
    agg_countries: Dict[str, set] = {}
    agg_latest: Dict[str, str] = {}   # latest assignment CreatedDate per activity
    agg_earliest: Dict[str, str] = {} # earliest assignment CreatedDate per activity
    for r in recs:
        oid = r.get("C_Opportunity_Name__c")
        c = (r.get("C_Account__r") or {}).get("ShippingCountry")
        d = (r.get("CreatedDate") or "")[:10]  # YYYY-MM-DD
        if not oid:
            continue
        agg_counts[oid] = agg_counts.get(oid, 0) + 1
        if oid not in agg_countries:
            agg_countries[oid] = set()
        if c:
            agg_countries[oid].add(c)
        if d:
            if oid not in agg_latest or d > agg_latest[oid]:
                agg_latest[oid] = d
            if oid not in agg_earliest or d < agg_earliest[oid]:
                agg_earliest[oid] = d
    has_date_filter = bool(date_cond)
    rows = []
    for oid, cnt in agg_counts.items():
        earliest = agg_earliest.get(oid, "")
        latest   = agg_latest.get(oid, "")
        date_range = (f"{earliest} – {latest}" if earliest and latest and earliest != latest
                      else earliest or latest)
        rows.append({
            "activity_name": id_to_name.get(oid) or oid,
            "assignments": cnt,
            "countries": ", ".join(sorted(list(agg_countries.get(oid) or set()))),
            "date_range": date_range,
            "activity_id": oid,
        })
    rows.sort(key=lambda r: r.get("assignments", 0), reverse=True)
    cols = [
        {"key":"activity_name","label":"Activity"},
        {"key":"assignments","label":"Assignments"},
        {"key":"countries","label":"Countries"},
        {"key":"date_range","label":"Assignment Date Range"},
        {"key":"activity_id","label":"Activity Id"},
    ]
    return {"columns": cols, "rows": rows}


def tool_activity_assignments_detailed(
    sf,
    countries: Optional[List[str]] = None,
    activity_contains: Optional[str] = None,
    *,
    last_n_days: Optional[int] = None,
    last_n_months: Optional[int] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """Return detailed assignment rows for Activities (RT_Activity), with optional filters:
    - countries: list[str] to filter Account.ShippingCountry IN (...)
    - activity_contains: substring to filter Activity Name (LIKE '%...%')
    - last_n_days / last_n_months / since / until filters on CreatedDate
    Columns: activity_name, opportunity_id, account_id, account_name, country, city, stage, type, assignment_id, created.
    """
    if not sf:
        raise HTTPException(400, "No SF session")
    import os, re
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["RT_Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    # Get activity ids (optionally filter by name)
    name_filter = ""
    if activity_contains and str(activity_contains).strip():
        esc = str(activity_contains).strip().replace("'", "\\'")
        name_filter = f" AND Name LIKE '%{esc}%'"
    act_soql = f"SELECT Id, Name FROM Opportunity WHERE RecordType.DeveloperName IN ({rt_vals}){name_filter}"
    acts = tool_salesforce_query(sf, act_soql).get("records", [])
    ids = [a.get("Id") for a in acts if a.get("Id")]
    if not ids:
        return _normalize_table_for_ui({"columns":[{"key":"activity_name","label":"Activity"},{"key":"opportunity_id","label":"Opportunity Id"},{"key":"account_id","label":"Account Id"},{"key":"account_name","label":"Account"},{"key":"country","label":"Country"},{"key":"city","label":"City"},{"key":"stage","label":"Stage"},{"key":"type","label":"Type"},{"key":"assignment_id","label":"Assignment Id"},{"key":"created","label":"Created"}],"rows":[]})
    def _chunks(lst, n=120):
        buf=[]
        for x in lst:
            if x: buf.append(x)
            if len(buf)>=n:
                yield buf; buf=[]
        if buf: yield buf
    # sanitize countries
    def _clean_countries(arr: Optional[List[str]]) -> List[str]:
        bad = {"those","these","this","that","activities","activity","with","involved","the"}
        out=[]
        for c in (arr or []):
            t=str(c or '').strip()
            if not t: 
                continue
            # strip quoted activity fragments before country
            t = re.sub(r"(?i)^activity\s+['\"]?.+?['\"]?\s+in\s+", "", t)
            if " in " in t.lower():
                t = t.split(" in ")[-1].strip()
            if t.lower() in bad: 
                continue
            if any(x in t.lower() for x in ("activity","screen","stage")):
                continue
            if re.search(r"activities?$", t, flags=re.I): 
                continue
            out.append(t)
        return out
    safe_c = _clean_countries(countries)
    rows = []
    for chunk in _chunks(ids):
        ids_in = ", ".join([f"'{i}'" for i in chunk])
        where = [f"C_Opportunity_Name__c IN ({ids_in})", "C_Account__c != null"]
        if safe_c:
            cvals = ", ".join(["'" + s.replace("'","\\'") + "'" for s in safe_c])
            where.append(f"C_Account__r.ShippingCountry IN ({cvals})")
        if activity_contains and str(activity_contains).strip():
            esc = str(activity_contains).strip().replace("'", "\\'")
            where.append(f"C_Opportunity_Name__r.Name LIKE '%{esc}%'")
        # Date filters
        if isinstance(last_n_days, int) and last_n_days > 0:
            where.append(f"CreatedDate = LAST_N_DAYS:{int(last_n_days)}")
        elif isinstance(last_n_months, int) and last_n_months > 0:
            where.append(f"CreatedDate = LAST_N_MONTHS:{int(last_n_months)}")
        else:
            if since:
                where.append(f"CreatedDate >= {since}T00:00:00Z")
            if until:
                where.append(f"CreatedDate <= {until}T23:59:59Z")
        soql = (
            "SELECT Id, Name, C_Account__c, C_Account__r.Name, C_Account__r.ShippingCountry, C_Account__r.ShippingCity, "
            "C_Opportunity_Name__c, C_Opportunity_Name__r.Name, C_Assignment_Stage__c, Assignment_Type__c, CreatedDate "
            "FROM Assignment__c WHERE " + " AND ".join(where) + " ORDER BY CreatedDate DESC"
        )
        recs = sf.query_all(soql).get("records", [])
        for r in recs:
            rows.append({
                "activity_name": (r.get("C_Opportunity_Name__r") or {}).get("Name"),
                "opportunity_id": r.get("C_Opportunity_Name__c"),
                "account_id": r.get("C_Account__c"),
                "account_name": (r.get("C_Account__r") or {}).get("Name"),
                "country": (r.get("C_Account__r") or {}).get("ShippingCountry"),
                "city": (r.get("C_Account__r") or {}).get("ShippingCity"),
                "stage": r.get("C_Assignment_Stage__c"),
                "type": r.get("Assignment_Type__c"),
                "assignment_id": r.get("Id"),
                "created": r.get("CreatedDate"),
            })
    return _normalize_table_for_ui({
        "columns": [
            {"key":"activity_name","label":"Activity"},
            {"key":"opportunity_id","label":"Opportunity Id"},
            {"key":"account_id","label":"Account Id"},
            {"key":"account_name","label":"Account"},
            {"key":"country","label":"Country"},
            {"key":"city","label":"City"},
            {"key":"stage","label":"Stage"},
            {"key":"type","label":"Type"},
            {"key":"assignment_id","label":"Assignment Id"},
            {"key":"created","label":"Created"},
        ],
        "rows": rows,
    })


def tool_activity_sites_by_country(sf):
    if not sf:
        raise HTTPException(400, "No SF session")
    # Get AccountIds of Activities; then enrich Accounts and aggregate in Python
    import os
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity,RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["Activity", "RT_Activity"]
    if "Activity" not in rt_list: rt_list.append("Activity")
    if "RT_Activity" not in rt_list: rt_list.append("RT_Activity")
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    soql = (
        "SELECT AccountId "
        "FROM Opportunity "
        "WHERE AccountId IN ("
        "  SELECT Id FROM Account "
        "  WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
        "    AND (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) "
        ") "
        f"AND (RecordType.DeveloperName IN ({rt_vals}) OR Type = 'Activity') "
        "AND AccountId != null"
    )
    recs = tool_salesforce_query(sf, soql).get("records", [])
    acc_ids = sorted({r.get("AccountId") for r in recs if r.get("AccountId")})
    acc_map = _build_account_map(sf, acc_ids) if acc_ids else {}
    counts: Dict[str, int] = {}
    for aid in acc_ids:
        country = (acc_map.get(aid) or {}).get("country")
        if not country:
            continue
        counts[country] = counts.get(country, 0) + 1
    rows = [{"country": k, "sites": v} for k, v in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)]
    return {"columns": [{"key":"country","label":"Country"},{"key":"sites","label":"Sites"}], "rows": rows}


def tool_study_coordinators_with_activities(
    sf,
    account_ids: Optional[List[str]] = None,
    countries: Optional[List[str]] = None,
    title_contains: Optional[str] = None,
    roles: Optional[List[str]] = None,
    include_subaccounts: bool = False,
):
    """Return one row per (Account × Study Coordinator) with activities list.
    Columns: account_id, site, country, city, sc_name, sc_email, sc_phone, sc_title,
             activities (semicolon-separated), activities_count.
    """
    if not sf:
        raise HTTPException(400, "No SF session")
    # 1) Resolve candidate accounts (clinical subaccounts, active)
    inactive_clause = "(Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
    acc_ids: List[str] = []
    acc_info: Dict[str, Dict[str, Any]] = {}
    parent_ids: set = set()
    if account_ids and len(account_ids):
        acc_ids = list({a for a in account_ids if a})
        if acc_ids:
            in_list = ",".join([f"'{a}'" for a in acc_ids])
            soql_acc = f"SELECT Id, Name, ShippingCity, ShippingCountry FROM Account WHERE Id IN ({in_list})"
            recs = sf.query_all(soql_acc).get("records", [])
            for r in recs:
                acc_info[r.get("Id")] = {"name": r.get("Name"), "city": r.get("ShippingCity"), "country": r.get("ShippingCountry")}
    else:
        where = ["RecordType.DeveloperName='SubAccount'", "C_Type__c='Clinical'", inactive_clause]
        if countries:
            san_countries = ["'" + str(c).strip().replace("'", "\\'") + "'" for c in countries if str(c).strip()]
            esc = ", ".join(san_countries)
            if esc:
                where.append(f"ShippingCountry IN ({esc})")
        soql_acc = "SELECT Id, Name, ShippingCity, ShippingCountry, ParentId FROM Account WHERE " + " AND ".join(where) + " LIMIT 2000"
        recs = sf.query_all(soql_acc).get("records", [])
        for r in recs:
            aid = r.get("Id"); acc_ids.append(aid)
            acc_info[aid] = {"name": r.get("Name"), "city": r.get("ShippingCity"), "country": r.get("ShippingCountry")}
            if r.get("ParentId"):
                parent_ids.add(r.get("ParentId"))
        # Also fetch parent account info so coordinator rows can display site name/country
        if parent_ids:
            try:
                parent_clause = ", ".join([f"'{p}'" for p in parent_ids])
                soql_par = f"SELECT Id, Name, ShippingCity, ShippingCountry FROM Account WHERE Id IN ({parent_clause})"
                par_recs = sf.query_all(soql_par).get("records", [])
                for r in par_recs:
                    pid = r.get("Id")
                    if pid and pid not in acc_info:
                        acc_info[pid] = {"name": r.get("Name"), "city": r.get("ShippingCity"), "country": r.get("ShippingCountry")}
            except Exception:
                pass
    if not acc_ids:
        return {"columns": [], "rows": []}
    # Build search IDs: SubAccounts + their parent Accounts (contacts may be linked to either)
    _all_search_ids = list(acc_ids) + [p for p in parent_ids if p not in set(acc_ids)]
    ids_clause = ", ".join([f"'{i}'" for i in _all_search_ids])
    # 2) Study Coordinators via ACR and Contact.Title
    roles_env = [s.strip() for s in (os.environ.get("SF_CONTACT_ROLES", "").split(",")) if s.strip()]
    role_list = roles if roles and len(roles) else roles_env
    where_title = f" OR Contact.Title LIKE '%Coordinat%'"
    if title_contains:
        te = title_contains.replace("'", "\\'")
        where_title = f" OR Contact.Title LIKE '%{te}%' {where_title}"
    sc_rows: List[Dict[str, Any]] = []
    # Coordinator patterns (multilingual)
    patterns = [
        "Coordinat",       # Coordinator, Coordinating, Co-ordinator
        "Coördinat",      # Dutch with diaeresis
        "Coordinador",    # ES
        "Coordinateur",   # FR
        "Koordinator",    # DE
    ]
    acr_title_cond = " OR ".join([f"Contact.Title LIKE '%{p}%'" for p in patterns])
    # ACR path 1: by explicit role list
    if role_list:
        role_vals = ", ".join([f"'{r}'" for r in role_list])
        soql = (
            "SELECT AccountId, ContactId, Role__c, Contact.Name, Contact.Email, Contact.Phone, Contact.Title "
            "FROM AccountContactRelation "
            f"WHERE AccountId IN ({ids_clause}) AND Role__c IN ({role_vals})"
        )
        sc_rows += sf.query_all(soql).get("records", [])
    # ACR path 2: by coordinator title via AccountContactRelation (catches contacts linked via ACR, not just primary AccountId)
    try:
        soql_acr_title = (
            "SELECT AccountId, ContactId, Role__c, Contact.Name, Contact.Email, Contact.Phone, Contact.Title "
            "FROM AccountContactRelation "
            f"WHERE AccountId IN ({ids_clause}) AND ({acr_title_cond})"
        )
        acr_title_recs = sf.query_all(soql_acr_title).get("records", [])
        # Deduplicate against existing sc_rows by ContactId+AccountId
        existing_keys = {(r.get("AccountId"), r.get("ContactId")) for r in sc_rows}
        for r in acr_title_recs:
            k = (r.get("AccountId"), r.get("ContactId"))
            if k not in existing_keys:
                existing_keys.add(k)
                sc_rows.append(r)
    except Exception as _e_acr:
        _dbg("WARN: ACR title query failed: %s", _e_acr)
    # Fallback: Contact by Title (direct AccountId match)
    title_cond = " OR ".join([f"Title LIKE '%{p}%'" for p in patterns])
    soql_c = (
        "SELECT Id, AccountId, Name, Email, Phone, Title FROM Contact "
        f"WHERE AccountId IN ({ids_clause}) AND ({title_cond})"
    )
    if title_contains:
        te = title_contains.replace("'", "\\'")
        soql_c = (
            "SELECT Id, AccountId, Name, Email, Phone, Title FROM Contact "
            f"WHERE AccountId IN ({ids_clause}) AND (Title LIKE '%{te}%' OR {title_cond})"
        )
    sc_contacts = sf.query_all(soql_c).get("records", [])
    # Helper: strict check coordinator by role/title
    def _is_sc(role: Optional[str], title: Optional[str]) -> bool:
        r = (role or "").lower(); t = (title or "").lower()
        positives = ("coordinat", "coördinat", "coordinador", "coordinateur", "koordinator")
        negatives = ("manager", "head", "monitor", "administrator", "admin")
        if any(n in t for n in negatives):
            # if explicitly administrative without coordinator, drop
            if not any(p in t for p in positives):
                return False
        if any(p in r for p in positives):
            return True
        return any(p in t for p in positives)
    # Build per-account coordinators list (with source); deduplicate by contact_id
    sc_by_acc: Dict[str, List[Dict[str, Any]]] = {}
    _seen_cids: set = set()  # (account_id, contact_id) dedup
    for r in sc_rows:
        aid = r.get("AccountId"); c = r.get("Contact") or {}
        cid = r.get("ContactId")
        if _is_sc(r.get("Role__c"), (c or {}).get("Title")):
            key = (aid, cid)
            if key not in _seen_cids:
                _seen_cids.add(key)
                sc_by_acc.setdefault(aid, []).append({
                    "name": c.get("Name"), "email": c.get("Email"), "phone": c.get("Phone"), "title": c.get("Title"),
                    "sc_source": "acr_role", "sc_role": r.get("Role__c"), "contact_id": cid,
                })
    for r in sc_contacts:
        aid = r.get("AccountId")
        cid = r.get("Id")
        _ctitle = r.get("Title") or ""
        # Include if: explicitly matched by title_contains (e.g. "Investigator"), or passes coordinator check
        _explicit_match = bool(title_contains and title_contains.lower() in _ctitle.lower())
        if _explicit_match or _is_sc(None, _ctitle):
            key = (aid, cid)
            if key not in _seen_cids:
                _seen_cids.add(key)
                sc_by_acc.setdefault(aid, []).append({
                    "name": r.get("Name"), "email": r.get("Email"), "phone": r.get("Phone"), "title": r.get("Title"),
                    "sc_source": "contact_title", "sc_role": None, "contact_id": cid,
                })
    # Path 3: Lead SC from Opportunity.C_Lead_Study_Coordinator_SC__c (INNODIA-specific field)
    try:
        soql_opp_sc = (
            "SELECT AccountId, C_Lead_Study_Coordinator_SC__c, "
            "C_Lead_Study_Coordinator_SC__r.Name, C_Lead_Study_Coordinator_SC__r.Email, "
            "C_Lead_Study_Coordinator_SC__r.Phone, C_Lead_Study_Coordinator_SC__r.Title "
            f"FROM Opportunity WHERE AccountId IN ({ids_clause}) "
            "AND C_Lead_Study_Coordinator_SC__c != null "
            "LIMIT 2000"
        )
        opp_sc_recs = sf.query_all(soql_opp_sc).get("records", [])
        for r in opp_sc_recs:
            aid = r.get("AccountId")
            sc_contact = r.get("C_Lead_Study_Coordinator_SC__r") or {}
            cid = r.get("C_Lead_Study_Coordinator_SC__c")
            if not cid or not aid:
                continue
            key = (aid, cid)
            if key not in _seen_cids:
                _seen_cids.add(key)
                sc_by_acc.setdefault(aid, []).append({
                    "name": sc_contact.get("Name"), "email": sc_contact.get("Email"),
                    "phone": sc_contact.get("Phone"), "title": sc_contact.get("Title"),
                    "sc_source": "opp_lead_sc", "sc_role": "Lead SC", "contact_id": cid,
                })
    except Exception as _e_opp:
        _dbg("WARN: Opp lead SC query failed: %s", _e_opp)
    # 3) Activities by Account (names only). Accept RecordType OR Type = 'Activity'
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity,RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()] or ["Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    soql_act = (
        "SELECT Id, Name, AccountId, Type, RecordType.DeveloperName FROM Opportunity "
        f"WHERE AccountId IN ({ids_clause}) AND (RecordType.DeveloperName IN ({rt_vals}) OR Type = 'Activity') "
        "ORDER BY CreatedDate DESC LIMIT 5000"
    )
    opps = sf.query_all(soql_act).get("records", [])
    acts_by_acc: Dict[str, List[str]] = {}
    for o in opps:
        aid = o.get("AccountId"); acts_by_acc.setdefault(aid, []).append(o.get("Name"))
    # 4) Build rows: only accounts that have coordinators
    rows_out: List[Dict[str, Any]] = []
    for aid, scs in sc_by_acc.items():
        if not scs: continue
        info = acc_info.get(aid) or {}
        act_names = "; ".join(acts_by_acc.get(aid, [])[:10])
        act_count = len(acts_by_acc.get(aid, []))
        for sc in scs:
            rows_out.append({
                "account_id": aid,
                "site": info.get("name"),
                "country": info.get("country"),
                "city": info.get("city"),
                "sc_name": sc.get("name"),
                "sc_email": sc.get("email"),
                "sc_phone": sc.get("phone"),
                "sc_title": sc.get("title"),
                "sc_source": sc.get("sc_source"),
                "sc_role": sc.get("sc_role"),
                "contact_id": sc.get("contact_id"),
                "activities": act_names,
                "activities_count": act_count,
            })
    table = {
        "columns": [
            {"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},
            {"key":"country","label":"Country"},
            {"key":"city","label":"City"},
            {"key":"sc_name","label":"Coordinator"},
            {"key":"sc_email","label":"Email"},
            {"key":"sc_phone","label":"Phone"},
            {"key":"sc_title","label":"Title"},
            {"key":"sc_source","label":"Source"},
            {"key":"sc_role","label":"Role (ACR)"},
            {"key":"contact_id","label":"Contact Id"},
            {"key":"activities","label":"Activities"},
            {"key":"activities_count","label":"Activities Count"},
        ],
        "rows": rows_out,
    }
    return _normalize_table_for_ui(table)

def tool_explorer_set_filters(request: Request, payload: Dict[str, Any]):
    return {"type": "explorer_action", "action": "set_filters", "payload": payload}

def tool_manipulate_data(
    last_table: Optional[Dict[str, Any]],
    operation: Literal["group_others", "top_n", "filter", "group_by_region"],
    threshold: Optional[float] = None,
    value_column: Optional[str] = None,
    label_column: Optional[str] = None,
    filter_op: str = ">=",
    scheme: str = "eu_nse",
    regions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Manipula datos de last_table según la operación solicitada.
    - group_others: agrupa filas con valor < threshold bajo "Others".
    - top_n: ordena por valor DESC y devuelve las primeras N.
    - filter: mantiene filas según comparador filter_op con threshold.
    """
    if not last_table or not last_table.get("rows"):
        return {"error": "No data available to manipulate"}
    
    rows = last_table.get("rows", [])
    cols = last_table.get("columns", [])
    
    # Auto-detectar columnas si no se especifican
    if not value_column:
        # Buscar columna numérica común
        for key in ["sites", "count", "total", "patients", "value"]:
            if any(isinstance(r, dict) and any(key in k.lower() for k in r.keys()) for r in rows):
                for r in rows:
                    for k in r.keys():
                        if key in k.lower():
                            value_column = k
                            break
                    if value_column:
                        break
                break
    
    if not label_column:
        # Buscar columna de etiquetas
        for key in ["country", "city", "site", "name", "label"]:
            if any(isinstance(r, dict) and any(key in k.lower() for k in r.keys()) for r in rows):
                for r in rows:
                    for k in r.keys():
                        if key in k.lower():
                            label_column = k
                            break
                    if label_column:
                        break
                break
    
    if not value_column or not label_column:
        return {"error": "Could not auto-detect value and label columns"}
    
    _dbg("manipulate_data: operation=%s, threshold=%s, value_col=%s, label_col=%s, op=%s",
         operation, threshold, value_column, label_column, filter_op)
    
    # Normaliza a números para ordenar/filtrar sin romper otras columnas
    def to_num(v: Any) -> float:
        try:
            if isinstance(v, (int, float)):
                return float(v)
            return float(str(v).replace(",", "")) if v is not None else 0.0
        except Exception:
            return 0.0
    
    # crea copia para no mutar la entrada
    safe_rows = [dict(r) for r in rows if isinstance(r, dict)]
    result_rows: List[Dict[str, Any]] = []
    
    if operation == "group_others":
        threshold = threshold or 3
        others_sum = 0.0
        kept_rows: List[Dict[str, Any]] = []
        thr = float(threshold)
        for row in safe_rows:
            val_num = to_num(row.get(value_column))
            # Agrupa valores <= threshold (así "one site" con threshold=1 funciona)
            if val_num <= thr:
                others_sum += val_num
            else:
                kept_rows.append(row)
        result_rows = kept_rows
        # Añadir fila "Others"
        if others_sum > 0:
            others_row = {label_column: "Others", value_column: others_sum}
            # Copiar otras columnas como vacío para consistencia
            for k in (safe_rows[0].keys() if safe_rows else []):
                if k not in [label_column, value_column] and k not in others_row:
                    others_row[k] = ""
            result_rows.append(others_row)
    
    elif operation == "top_n":
        n = int(threshold or 10)
        ordered = sorted(safe_rows, key=lambda r: to_num(r.get(value_column)), reverse=True)
        result_rows = ordered[:n]
    
    elif operation == "filter":
        thr = float(threshold or 1)
        op = (filter_op or ">=").strip()
        for row in safe_rows:
            v = to_num(row.get(value_column))
            ok = (v >= thr) if op == ">=" else (v > thr) if op == ">" else (v <= thr) if op == "<=" else (v < thr) if op == "<" else (v == thr)
            if ok:
                result_rows.append(row)

    elif operation == "group_by_region":
        # Build mapping for EU North/South/East (names or ISO2; case-insensitive)
        reg_map: Dict[str, set] = {}
        def _norm(s: Any) -> str:
            return str(s or "").strip().lower()
        if regions and isinstance(regions, dict):
            for k, arr in regions.items():
                reg_map[k] = { _norm(x) for x in (arr or []) }
        else:
            if scheme == "eu_nse":
                north = {
                    # Nordics + Baltics + BeNeLux + UK/IE + DE (broad north)
                    "united kingdom","uk","gb","ireland","ie",
                    "denmark","dk","sweden","se","norway","no","finland","fi","iceland","is",
                    "estonia","ee","latvia","lv","lithuania","lt",
                    "belgium","be","netherlands","nl","luxembourg","lu",
                    "germany","de",
                }
                south = {
                    "spain","es","portugal","pt","italy","it","greece","gr","malta","mt","cyprus","cy","andorra","ad"
                }
                east = {
                    "poland","pl","czech republic","czechia","cz","slovakia","sk","hungary","hu",
                    "romania","ro","bulgaria","bg","croatia","hr","slovenia","si",
                    "serbia","rs","bosnia","bosnia and herzegovina","ba","montenegro","me",
                    "north macedonia","mk","albania","al"
                }
                reg_map = {"North": north, "South": south, "East": east}
            else:
                reg_map = {}
        # Aggregate
        sums: Dict[str, float] = {}
        counts: Dict[str, int] = {}
        for row in safe_rows:
            ctry = _norm(row.get(label_column))
            region = "Other"
            for rname, bucket in reg_map.items():
                if ctry in bucket:
                    region = rname
                    break
            if value_column:
                sums[region] = sums.get(region, 0.0) + to_num(row.get(value_column))
            else:
                counts[region] = counts.get(region, 0) + 1
        if value_column:
            result_rows = [{"region": k, value_column: v} for k, v in sums.items()]
            cols = [{"key":"region","label":"Region"},{"key":value_column,"label": value_column}]
        else:
            result_rows = [{"region": k, "sites": v} for k, v in counts.items()]
            cols = [{"key":"region","label":"Region"},{"key":"sites","label":"Sites"}]
    
    _dbg("manipulate_data: input=%d rows, output=%d rows", len(rows), len(result_rows))
    
    return {
        "columns": cols,
        "rows": result_rows,
        "operation_applied": operation,
        "threshold_used": threshold,
        "value_column": value_column,
        "label_column": label_column,
        "filter_op": filter_op,
    }

def tool_render_chart(
    kind: Literal["bar","line","scatter","pie"],
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

# ====== Spec de herramientas para el modelo ======
TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "soql_query",
            "description": (
                "Run a SOQL SELECT on Opportunity (and Account.*). "
                "When to use: COUNT/GROUP BY aggregations, traversal relationships (e.g. C_Member__r.*), "
                "or fields not in the Explorer catalog. "
                "When NOT to use: Do NOT use for site filtering — use explorer_search instead. "
                "Do NOT use for qualification data — use explorer_search with qual.* filters."
            ),
            "parameters": {
                "type":"object",
                "properties":{
                    "soql":{"type":"string","description":"SOQL SELECT ... FROM Opportunity ..."}
                },
                "required":["soql"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "salesforce_account_extras",
            "description": "Fetch extra info for a Salesforce Account: PI (name/email/phone), CS Contribution flags, latest new diagnoses and assignments count.",
            "parameters": {
                "type":"object",
                "properties":{
                    "account_id":{"type":"string"}
                },
                "required":["account_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "salesforce_account_contacts",
            "description": "List contacts for an Account (optionally including child Accounts). Useful for PI/SC/Study Nurse lookups.",
            "parameters": {
                "type":"object",
                "properties":{
                    "account_id":{"type":"string"},
                    "include_subaccounts":{"type":"boolean","default":False},
                    "role_contains":{"type":"string","description":"Filter by Title/Department contains (fallback when roles not provided)"},
                    "roles":{"type":"array","items":{"type":"string"},"description":"Filter by AccountContactRelation.Role__c; defaults to env SF_CONTACT_ROLES if set"},
                    "title_contains":{"type":"string","description":"Filter by Contact.Title (e.g., 'Study Coordinator', 'Nurse')"}
                },
                "required":["account_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "salesforce_assignments",
            "description": "List assignments per Account using Explorer extras (stage, type, opportunity, created).",
            "parameters": {
                "type":"object",
                "properties":{
                    "account_ids":{"type":"array","items":{"type":"string"}},
                    "active_only":{"type":"boolean","default":False},
                    "last_n_months":{"type":"integer","description":"Limit to recent assignments"}
                },
                "required":["account_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "explorer_set_filters",
            "description": "Tell the UI to update Explorer filters/columns per the given payload.",
            "parameters": {
                "type":"object",
                "properties":{
                    "filters":{"type":"object"},
                    "columns":{"type":"array","items":{"type":"string"}}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manipulate_data",
            "description": "Transform table data before charting: group small values, filter top N, or group by EU regions (North/South/East). Returns modified data ready for render_chart.",
            "parameters": {
                "type":"object",
                "properties":{
                    "operation":{"type":"string","enum":["group_others","top_n","filter","group_by_region"],"description":"Type of transformation"},
                    "threshold":{"type":"number","description":"For group_others: values below this are grouped. For top_n: number of items to keep."},
                    "value_column":{"type":"string","description":"Column name with numeric values (e.g., 'sites', 'count')"},
                    "label_column":{"type":"string","description":"Column name with labels (e.g., 'country', 'city')"},
                    "scheme":{"type":"string","enum":["eu_nse","custom"],"default":"eu_nse"},
                    "regions":{"type":"object","description":"Custom regions mapping: { North:[...], South:[...], East:[...] } (country names or ISO2)"}
                },
                "required":["operation"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "render_chart",
            "description": "Return a chart spec that the frontend will render directly. When NOT to use: for data retrieval — get data first with explorer_search, then visualize with this tool.",
            "parameters": {
                "type":"object",
                "properties":{
                    "kind":{"type":"string","enum":["bar","line","scatter","pie"]},
                    "data":{"type":"array","items":{"type":"object"}},
                    "xKey":{"type":"string"},
                    "yKeys":{"type":"array","items":{"type":"string"}},
                    "meta":{"type":"object"}
                },
                "required":["kind","data","xKey","yKeys"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "explorer_search",
            "description": (
                "DEFAULT tool for any question about clinical trial sites. "
                "Searches sites using FilterGroup with sf.* (Salesforce), qual.* (qualification), "
                "site.* (geography), and extra.* (assignments, activities) fields. "
                "When to use: ANY question about sites — filtering, listing, searching. This should be your FIRST choice. "
                "When NOT to use: Only skip this for SOQL aggregations (use soql_query) or member institutions (use members_search). "
                "Supports nested AND/OR logic. "
                "CRITICAL: ALWAYS express every filter as a rule — NEVER pass an empty filters object. "
                "Country filter: {field:'site.country', operator:'equals', value:'ES'} (ISO-2 codes). "
                "City filter: {field:'site.city', operator:'equals', value:'Barcelona'}. "
                "NESTED GROUPS: rules[] can contain sub-groups {logic, rules} for complex AND/OR. "
                "Examples: "
                "'sites in Spain' → filters={logic:'AND',rules:[{field:'site.country',operator:'equals',value:'ES'}]}. "
                "'Spain OR Italy with overnight' → filters={logic:'AND',rules:[{logic:'OR',rules:[{field:'site.country',op:'equals',value:'ES'},{field:'site.country',op:'equals',value:'IT'}]},{field:'qual.3_5_2__overnight_stay',operator:'equals',value:'Yes'}]}. "
                "'(Germany AND overnight) OR (France AND pediatric)' → filters={logic:'OR',rules:[{logic:'AND',rules:[{field:'site.country',operator:'equals',value:'DE'},{field:'qual.3_5_2__overnight_stay',operator:'equals',value:'Yes'}]},{logic:'AND',rules:[{field:'site.country',operator:'equals',value:'FR'},{field:'qual.3_5_2__overnight_stay',operator:'is_not_empty'}]}]}. "
                "'sites with pharmacy AND ND>50' → filters={logic:'AND',rules:[{field:'qual.3_6__is_your_pharmacy_on_site_or_off_campus',operator:'equals',value:'On-site'},{field:'sf.C_Number_of_new_T1D_diagnosed_O_18__c',operator:'>',value:50}]}. "
                "Operators: equals, not_equals, >, >=, <, <=, contains, in, not_in, is_empty, is_not_empty, between. "
                "ALWAYS use ISO-2 for country values: ES, IT, FR, DE, GB, BE, NL, PT, PL, CZ, SE, DK, NO, FI, AT, CH, IE. "
                "qual.* keys: 'qual.<section_key>' (e.g. 'qual.3_6__is_your_pharmacy_on_site_or_off_campus'). "
                "sf.* keys: 'sf.<ApiName>' (e.g. 'sf.C_Number_of_new_T1D_diagnosed_O_18__c'). "
                "site.* keys: 'site.country' (ISO-2), 'site.city'. "
                "After results you can call render_chart to visualize them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "description": (
                            "FilterGroup: {logic: 'AND'|'OR'|'<expr>', rules: [rule_or_group, ...]}. "
                            "Each element in rules[] is either a leaf rule {field, operator, value} "
                            "or a nested sub-group {logic:'AND'|'OR', rules:[...]}. "
                            "Country values MUST be ISO-2 (e.g. 'ES', 'IT', 'DE', 'FR', 'GB'). "
                            "Simple: {logic:'AND', rules:[{field:'site.country', operator:'equals', value:'ES'}]}. "
                            "Multi-country OR: {logic:'AND', rules:[{logic:'OR', rules:[{field:'site.country',operator:'equals',value:'ES'},{field:'site.country',operator:'equals',value:'IT'}]}, {field:'qual.3_5_2__overnight_stay',operator:'equals',value:'Yes'}]}. "
                            "Grouped: {logic:'OR', rules:[{logic:'AND', rules:[...]}, {logic:'AND', rules:[...]}]}."
                        )
                    },
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of column keys to include (sf.*, qual.*, site.*). Defaults to site name, country, city and patient counts."
                    }
                },
                "required": ["filters"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "explorer_within_drive_km",
            "description": "Find neighboring sites within a driving distance (km) from a base Salesforce Account, using the Explorer service. Use this for 'within X km', 'nearby', 'distance' queries. When NOT to use: for straight-line distance — use nearest_filtered_sites. For general search — use explorer_search.",
            "parameters": {
                "type":"object",
                "properties":{
                    "base_account_id":{"type":"string","description":"Salesforce Account Id for origin. If omitted, use the first account from the last result set."},
                    "max_km":{"type":"number","description":"Max driving distance in km"},
                    "filters":{"type":"object"},
                    "columns":{"type":"array","items":{"type":"string"}}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "nearest_filtered_sites",
            "description": (
                "Find nearest clinical sites to a city/address, optionally filtered by qual.*, sf.*, and extra.* fields. "
                "Returns sites sorted by straight-line distance (km). "
                "Supports ALL filter types in ONE call: qual.* (qualifications), sf.* (Salesforce fields), site.* (country/city), "
                "and extra.* (assignments — use extra.AssignmentsNames not_contains 'X' to exclude sites in a study, "
                "or extra.AssignmentsCount is_empty for sites not in any assignment). "
                "Use when user asks for 'nearest sites to X', 'closest to X', 'sites near X not in Y study'. "
                "IMPORTANT: combine proximity + filters in ONE call — do NOT make separate calls. "
                "When NOT to use: for general site search without proximity — use explorer_search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Reference city/address/landmark, e.g. 'Barcelona', 'Berlin, Germany', 'San Raffaele Hospital Milan'"
                    },
                    "filters": {
                        "type": "object",
                        "description": "Optional FilterGroup {logic:'AND'|'OR', rules:[...]} — same format as explorer_search. Use for qual.* and sf.* field filters."
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "How many nearest sites to return (default 10, max 50)",
                        "default": 10
                    },
                    "max_km": {
                        "type": "number",
                        "description": "Maximum straight-line radius in km (default 1000)",
                        "default": 1000
                    }
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rank_sites",
            "description": (
                "Top-N ranking of sites by a metric. ALWAYS use this for ranking queries. "
                "Returns account_id, site name, country, city, and the metric value. "
                "Works with both SF fields and site_qual keys/aliases (e.g., 'patients under 18', 'newly diagnosed', 'Stage 2'). "
                "When NOT to use: for filtering sites without ranking — use explorer_search."
            ),
            "parameters": {
                "type":"object",
                "properties":{
                    "metric":{"type":"string","description":"Alias or raw key (e.g., 'patients under 18', 'newly diagnosed under 18', 'new T1D <18', 'C_Number_of_T1D_Patients_currently_U_18__c')"},
                    "top_n":{"type":"integer","default":5},
                    "order":{"type":"string","enum":["asc","desc"],"default":"desc"},
                    "group_by":{"type":"string","enum":["country","city"],"description":"If provided, ranks top-N PER GROUP (e.g., top 3 per country). Omit for global ranking."}
                },
                "required":["metric"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"group_count",
            "description": (
                "Count or aggregate sites grouped by country/city. "
                "When to use: site counts per country/city, averages/sums of metrics per group, ratio of sites with a field. "
                "When NOT to use: for listing individual sites — use explorer_search."
            ),
            "parameters":{
                "type":"object",
                "properties":{
                    "by":{"type":"array","items":{"type":"string","enum":["country","city"]}},
                    "where":{"type":"object","description":"Optional site_qual filter for counting."},
                    "aggregation":{"type":"string","enum":["count","sum","avg","ratio"],"default":"count","description":"Type of aggregation. 'count' counts sites, 'sum'/'avg' aggregate a metric, 'ratio' computes ratio_exists."},
                    "metric":{"type":"string","description":"Required when aggregation is sum/avg/ratio. The metric key to aggregate."},
                    "source":{"type":"string","enum":["explorer","salesforce"],"default":"explorer","description":"Data source. 'salesforce' counts SF Accounts directly (SubAccount Clinical, active)."}
                },
                "required":["by"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_activities",
            "description": "List all Activities (Opportunities with RecordType RT_Activity). Returns Activity Name, Sponsor/Account, Activity Id. Use this when user asks for 'list of activities', 'what activities exist', 'show all activities', or 'activities where [company] is the sponsor/account'. Can filter by sponsor/account name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_name_like": {
                        "type": "string",
                        "description": "Filter activities by sponsor or account name (case-insensitive LIKE match). E.g. 'Sanofi' to find activities where the linked account/sponsor contains 'Sanofi'."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activities_with_countries",
            "description": "List Activities with the countries participating in each (via Assignments). Returns Opportunity Name, Account Name, Countries (comma-separated), Site Count. Use this when user asks 'which countries participate in each activity' or 'activities by country' or 'countries per activity'.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activity_counts_by_country",
            "description": "Aggregate number of activities per country (each activity counted once per country). Returns country and activities count.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activity_country_matrix",
            "description": "Per-activity country counts based on Assignment__c. If stacked=true, returns one row per (activity,country) with 'sites' (assignments) for stacked bars; else totals per activity with countries list.",
            "parameters": {
                "type": "object",
                "properties": {
"stacked": {"type": "boolean", "default": False}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activities_sites",
            "description": "List all clinical subaccounts (sites) that participate in any Activity via Assignments. Returns one row per (Site × Activity) with country and city. Use this when user asks for 'sites with activities' or 'activities by country'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "countries": {"type": "array", "items": {"type": "string"}}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activities_by_name",
            "description": "List clinical subaccounts that participate in a specific Activity (partial match by Name).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "countries": {"type": "array", "items": {"type": "string"}},
"exact": {"type": "boolean", "default": False}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sf_aggregate",
            "description": (
                "Aggregate Salesforce Opportunity fields — either grouped by geography or as a time series. "
                "When to use: sum/avg/max of SF fields by country/city, or trends over time (month/quarter/year). "
                "When NOT to use: for site-level data — use explorer_search. For qualification data — use explorer_search with qual.* filters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["aggregate", "time_series"], "default": "aggregate",
                             "description": "'aggregate' groups by country/city; 'time_series' groups by time period."},
                    "field": {"type": "string", "description": "SF Opportunity field API name to aggregate."},
                    "agg": {"type": "string", "enum": ["sum","max","avg"], "default": "avg"},
                    "by": {"type": "array", "items": {"type": "string", "enum": ["country","city"]},
                           "description": "Required for mode=aggregate. Group by country/city."},
                    "date_field": {"type": "string", "default": "CloseDate",
                                   "description": "For mode=time_series: the date field to group by."},
                    "period": {"type": "string", "enum": ["month","quarter","year"], "default": "month",
                               "description": "For mode=time_series: time granularity."},
                    "last_n": {"type": "integer",
                               "description": "For mode=time_series: limit to last N periods."}
                },
                "required": ["field"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activities_with_assignments_counts",
            "description": "List activities with total assignments and participating countries. Supports date filters (last_n_days/last_n_months or since/until). Returns activity_name, assignments, countries, activity_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "last_n_days": {"type": "integer"},
                    "last_n_months": {"type": "integer"},
                    "since": {"type": "string"},
                    "until": {"type": "string"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activity_assignments_detailed",
            "description": "Detailed assignment rows for activities with optional filters: countries, activity_contains (substring), and date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "countries": {"type": "array", "items": {"type": "string"}},
                    "activity_contains": {"type": "string"},
                    "last_n_days": {"type": "integer"},
                    "last_n_months": {"type": "integer"},
                    "since": {"type": "string"},
                    "until": {"type": "string"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "study_coordinators_with_activities",
            "description": "List Study Coordinators per Account (via AccountContactRelation + Contact.Title) and include activities (Opportunity RT=Activity) per account.",
            "parameters": {
                "type":"object",
                "properties":{
                    "account_ids":{"type":"array","items":{"type":"string"}},
                    "countries":{"type":"array","items":{"type":"string"}},
                    "title_contains":{"type":"string"},
                    "roles":{"type":"array","items":{"type":"string"}},
"include_subaccounts":{"type":"boolean","default":False}
                }
            }
        }
    },

    {
        "type":"function",
        "function":{
            "name":"sql_query_fill_sf",
            "description":"Run SQL (must return account_id) and fill Account.* fields in batch from Salesforce.",
            "parameters":{
                "type":"object",
                "properties":{
                    "sql":{"type":"string"},
                    "account_fields":{"type":"array","items":{"type":"string"}},
                    "params":{"type":"object"}
                },
                "required":["sql","account_fields"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"contacts_by_group",
            "description":"Top-N contacts per country/city filtered by roles/title (AccountContactRelation).",
            "parameters":{
                "type":"object",
                "properties":{
                    "roles":{"type":"array","items":{"type":"string"}},
                    "title_contains":{"type":"string"},
                    "group_by":{"type":"string","enum":["country","city"],"default":"country"},
                    "top_n":{"type":"integer","default":1}
                }
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"qual_search",
            "description":"Semantic search over qualification comments (GIN tsv).",
            "parameters":{
                "type":"object",
                "properties":{
                    "text":{"type":"string"},
                    "limit":{"type":"integer","default":50}
                },
                "required":["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "members_search",
            "description": (
                "Search INNODIA Member institutions (RecordType.DeveloperName = 'RT_Member') and their network roles, contacts, "
                "and linked SubAccount clinical sites. Use for ANY question about: "
                "member institutions, membership levels, proposed/validated network roles "
                "(CS/DxLab/LAB/CTS/Patient Organization), country leads, board members, "
                "institutional contacts, or sub-accounts linked to a member. "
                "When NOT to use: for clinical trial sites — use explorer_search. "
                "Examples: "
                "'how many member institutions?' → filters={logic:'AND',rules:[]}. "
                "'members in Italy' → filters={logic:'AND',rules:[{field:'site.country',operator:'equals',value:'Italy'}]}. "
                "'members with validated CTS role' → filters={logic:'AND',rules:[{field:'sf.Clinical_Trial_Site_CTS_validated__c',operator:'equals',value:true}]}. "
                "'members with proposed CS' → filters={logic:'AND',rules:[{field:'sf.Clinical_Site_CS__c',operator:'equals',value:true}]}. "
                "Filterable fields: site.country, site.city, sf.C_Level_of_Membership__c, sf.Account_Status__c, "
                "sf.Clinical_Site_CS__c, sf.C_Deliver_Clinical_Grade_Services__c, sf.C_Perform_Cutting_Edge__c, "
                "sf.C_Contribute_as_a_Patient_Organization__c, sf.Clinical_Site_CS_validated__c, "
                "sf.Clinical_Trial_Site_CTS_validated__c, sf.Diagnostic_Lab_DxLab_validated__c, "
                "sf.Research_Mechanistic_Lab_LAB_validated__c, sf.Patient_Organization_validated__c, "
                "extra.SubAccountsCount, extra.ContactsCount."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "description": (
                            "FilterGroup: {logic:'AND'|'OR', rules:[{field, operator, value},...]}. "
                            "Pass {logic:'AND',rules:[]} to return all members."
                        )
                    },
                    "include_detail": {
                        "type": "boolean",
                        "description": "If true, also fetches contacts and SubAccounts for each matched member (slower)."
                    }
                },
                "required": ["filters"]
            }
        }
    },
]

# ====== System prompt ======
SCHEMA_HINT = """
POSTGRES (warehouse):
- public.sites(id, name, street, city, country, postcode, latitude, longitude, salesforce_account_id)
- public.site_qual(site_id -> sites.id, data JSONB)  // Qualification flattened key→value.
  JSONB tips:
    - Safe casts, e.g. COALESCE(NULLIF(sq.data->>'C_Number_of_T1D_Patients_currently_U_18__c','')::int, 0) AS t1d_u18
    - For YES/NO strings, normalize to LOWER and compare to 'yes'.

SALESFORCE (runtime):
- soql_query supports: Opportunity, Account, Contact, AccountContactRelation
  - Opportunity: patient metrics, trials, qualification/profiling (primary for sites)
  - Account: **PRIMARY SOURCE for site lists, counts, and geography** (ShippingCountry, ShippingCity)
  - Contact: direct contact queries (name, email, phone, title, department)
  - AccountContactRelation: contact-account links with roles (PI, Study Coordinator, etc.)
- salesforce_account_extras(account_id) for PI / CS flags / assignments / newDx.
- **CRITICAL**: Country/City data comes from Account.ShippingCountry and Account.ShippingCity (NOT from Postgres sites table).
"""

SYSTEM_PROMPT = f"""
You are **Moby**, an analytics copilot for a clinical trial site explorer.

LANGUAGE
- Default to English.
- If the latest user message is clearly (>80%) in another language, reply in that language. Otherwise keep English.

DOMAIN GLOSSARY — INNODIA abbreviations and acronyms (memorize these):
- **ND** = Newly Diagnosed T1D patients (last year). Two SF fields: ≥18 → C_Number_of_new_T1D_diagnosed_O_18__c ; <18 → C_Number_of_new_T1D_diagnosed_U_18__c. Use BOTH when user says "ND" without age.
- **T1D** = Type 1 Diabetes. "T1D patients" = currently under care: ≥18 → C_Number_of_T1D_Patients_currently_O_18__c ; <18 → C_Number_of_T1D_Patients_currently_U_18__c
- **Stage 1** = Pre-symptomatic Stage 1 → sf.C_Number_of_Stage1_Individuals_followed__c
- **Stage 2** = Pre-symptomatic Stage 2 → sf.C_Number_of_Stage2_Individuals_followed__c
- **Screened** = Individuals screened in total → sf.C_Number_of_Individuals_screened_intotal__c
- **CTS** = Clinical Trial Site. Account flag: INNODIA_Clinical_Trial_Site__c = true (also C_Accredited_Clinical_Trial_Site__c for accredited ones). Subset of the clinical-site universe — only sites that passed CTS validation.
- **CS** = Clinical Site (abbreviation). Account flag: Clinical_Site_CS__c = true. A specific CS-validated subset.
- **"Clinical Site"** (natural-language, NOT the CS abbreviation) = the full universe of clinical sub-accounts, defined by `RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical'`. Use this broad filter when the user says "clinical site(s)" or just "site(s)" without a CTS/CS qualifier. The narrower CTS and CS flags above are strict subsets of this universe.
- **DxLab** = Diagnostic Lab. Account flag: C_Deliver_Clinical_Grade_Services__c = true
- **RP** = Referral Partner. Account flag: C_Referral_Clinical_Partner__c = true
- **PO** = Patient Organization. Account flag: C_Contribute_as_a_Patient_Organization__c = true
- **PI** = Principal Investigator → sf.C_Principal_Investigator__c (Opportunity) or Contact with Role='PI'
- **SC** = Study Coordinator → sf.C_Lead_Study_Coordinator_SC__c (Opportunity)
- **HLA** = Human Leukocyte Antigen typing → sf.C_Is_HLA_typing_performed__c (Opportunity)
- **PAC** = Patient Advisory Council (referenced in C_Site_Linked_with_Patient_Org_or_PAC__c)
- **IMP** = Investigational Medicinal Product (drug storage/handling in qual checklist)
- **GCP** = Good Clinical Practice (certification/training)
- **MCA** = Multi-Centre Agreement → Assignment.C_MCA_Status__c
- **Phase I/II/III** = Clinical trial phases → sf.C_Phase_I_Type1__c, C_Phase_II_Type1__c, C_Phase_III_Type1__c (T1D) and NonType1 variants
- **Profiling** workflow fields (ALL on Opportunity unless noted; several are DATES, not booleans — treat `not null` as "done"):
  - `C_Profiling_Complete__c` — **DATE** (not boolean). Use `!= null` for "profiling complete", `= null` for "NOT completed".
  - `Profiling_form_uploaded_to_DB__c` — boolean, true once the qualification form is loaded into the DB.
  - `C_Form_Questionnaire_sent__c` / `Form_Questionnaire_received__c` — booleans for questionnaire lifecycle.
  - `C_Date_First_Contact__c` / `C_Meeting_Date__c` — DATE fields; use `not null` / date-range filters.
  - `C_Profiling_Status__c` (Account) / `StageName` (Opportunity) — free-text pipeline stages.
- **Activity** = An Opportunity with RecordType.DeveloperName = 'RT_Activity'. Activities are specific programs/trials (Detect, Fabulinus, Baricade, Safeguard, etc.). Sites participate in an Activity via Assignment__c records.
- **Detect / DETECT** = Activity "DETECT Pilot Sites" + "DETECT French Roll Out" + "DETECT Italian Roll Out" — an early detection program
- **Fabulinus** = Activity "Fabulinus CTS Team - Part A" + "Fabulinus CTS Team - Part B" + "FABULINUS Referral Partner Network"
- **Baricade** = Activity "Baricade Delay (JAJJ)" + "Baricade Preserve (JAJK)"
- **Safeguard** = Activity "Safeguard Trial Clinical Sites"
- **Diagnode** = Activity "Diagnode-3 RP Team"

SF OBJECTS AVAILABLE:
- **Account** = Sites/organizations. Key custom fields: INNODIA_Clinical_Trial_Site__c, C_Type__c, C_Profiling_Status__c, C_Membership__c, Account_Status__c, Accredited__c, Screening_Program__c
- **Opportunity** = Profiling/qualification records linked to Account. Contains patient metrics (Stage 1/2, ND, current T1D), PI/SC contacts, trial counts, visit info
- **Contact** = People (PI, SC, Study Nurse, etc.) linked to Accounts via AccountContactRelation
- **Assignment__c** = Task assignments linked to Opportunity+Contact+Account. Fields: Assignment_Type__c, C_Assignment_Stage__c, C_MCA_Status__c, C_Payment_Done__c, C_Invoice_Received__c

DATA SOURCES & SCHEMA (do not expose credentials)
{SCHEMA_HINT}

## Tool Selection Decision Tree

Follow this tree for EVERY query. Start at the top.

1. Is this about MEMBER INSTITUTIONS (not clinical trial sites)?
   → YES: Use `members_search`
   → NO: Continue to 2

2. Is this a math operation on the last table (sum, average, count)?
   → YES: The system handles this automatically — just describe the operation
   → NO: Continue to 3

3. Is this about clinical trial SITES (filtering, listing, searching)?
   → YES: Use `explorer_search` (DEFAULT — handles sf.*, qual.*, site.*, extra.* fields)
   → NO: Continue to 4

4. Does the query mention a city/location AND proximity (near, closest, within X km)?
   → YES: Use `nearest_filtered_sites` — include ALL other conditions as filters in the SAME call
   → Example: "sites near Berlin with HLA typing not in any assignment" →
     nearest_filtered_sites(location="Berlin", filters={{logic:"AND", rules:[...all conditions...]}})
   → By existing site (Account ID): Use `explorer_within_drive_km`
   → NO proximity mentioned: Continue to 5

5. Is this a SOQL aggregation (COUNT, AVG, GROUP BY) that explorer_search cannot handle?
   → YES: Use `soql_query`
   → NO: Continue to 6

6. Is this about contacts, coordinators, or PIs?
   → YES: Use `salesforce_account_contacts` or `study_coordinators_with_activities`
   → NO: Continue to 7

7. Is this about rankings (top N by metric)?
   → YES: Use `rank_sites`
   → NO: Continue to 8

8. Is this about distributions (sites per country, averages by group)?
   → YES: Use `group_count`
   → NO: Use `soql_query` as a fallback for custom queries

IMPORTANT: When in doubt, use `explorer_search`. It is the most versatile tool.
IMPORTANT: You can call multiple tools in one turn and chain results across turns.
IMPORTANT: If an answer requires data, you MUST call at least one appropriate tool to fetch real rows. Do not invent numbers.
IMPORTANT: When calling a tool, ALSO include a brief text summary alongside the tool call (2-3 sentences). This enables faster responses. Example: call explorer_search AND write "Here are the 9 German sites. Hamburg has the highest ND count (70), while Hannover leads in Stage 1 (12)."

## How to Write Responses

Write like a knowledgeable colleague briefing someone, not like a search engine returning results. Your responses should:

1. **Lead with the key insight** — what's the headline? "There are 9 German sites, but only 3 have overnight stays." Not "Here are the results."
2. **Highlight notable data points** — top/bottom values, outliers, patterns. "Copenhagen stands out with 5,500 T1D patients — 5x more than the next site."
3. **Use natural grouping** — by country, by capability, by size. "The Belgian sites cluster around Brussels (4 within 25km), while the Dutch sites are spread across 3 cities."
4. **Be concise** — 2-4 sentences for simple queries, up to a short paragraph for complex ones. The table has the details; your text provides the story.
5. **Use HTML formatting** — <p>, <strong>, <em> for emphasis. Never use markdown.
6. **Mention the count** — always state how many results were found.
7. **Don't list every result** — that's what the table is for. Pick the 2-3 most interesting ones.

Bad: "<p>Found 15 result(s).</p>"
Good: "<p><strong>15 sites</strong> near Hamburg are not in any Baricade study. The closest is <strong>WilhelmStift</strong> (12.5 km), followed by Karlsburg (243 km) and Copenhagen (289 km). Results span 5 countries — Germany has the most (4 sites).</p>"

TOOL REFERENCE:
- explorer_search → DEFAULT for any site question: filtering, listing, searching sites (combines qual.* + sf.* + site.* in one query)
- soql_query → SOQL aggregations (COUNT/GROUP BY), traversals (C_Member__r.*), fields not in Explorer. ALWAYS filter inactive: (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null). Country/City → ALWAYS use Account.ShippingCountry/ShippingCity (NEVER sites.country/city)
- qual_search → Semantic search over qualification comments
- salesforce_account_extras → PI, CS flags, assignments, new diagnoses per Account
- salesforce_account_contacts → Contact lists (PI, Study Coordinator, etc.)
- rank_sites → Top-N sites by metric (with optional group_by for per-country/city ranking)
- group_count → Count/aggregate sites grouped by country/city
- sf_aggregate → Aggregate SF fields by geography or time series
- render_chart → Generate bar/line/pie charts (call after data fetch to visualize results)
- nearest_filtered_sites → Sites near a city/address (Haversine distance)
- explorer_within_drive_km → Sites within driving distance of a known Account
- members_search → Member institution queries

GOLDEN RULE: Use Postgres ONLY for site_qual questions. Everything else MUST use Salesforce. Activities, Contacts, Accounts, Opportunities → always Salesforce.

GUARDRAILS
- Read-only only. Always use named parameters in SQL examples.
- JSONB numeric casts: COALESCE(NULLIF(sq.data->>'C_Number_of_T1D_Patients_currently_U_18__c','')::int, 0) AS t1d_u18
- YES/NO strings: normalize (LOWER(...)='yes' or ILIKE 'yes').
- Ordering by computed columns: in ORDER BY use the full expression or positional indices (e.g., ORDER BY 3 DESC), never the alias.
- **ALWAYS order results with numeric values by that value DESC** (highest first) unless explicitly asked otherwise. Example: GROUP BY ShippingCountry ORDER BY COUNT(Id) DESC

FORMAT RULES (CRITICAL - follow exactly)
⛔ **NEVER put tabular data in the answer field:**
- Do **NOT** include markdown tables (| Country | Sites | format) in **answer**
- Do **NOT** paste JSON, XML, or HTML tags in **answer**
- Do **NOT** include raw data structures in **answer**
- Do **NOT** list rows of data in **answer**

✅ **What goes in answer:**
- **answer** should contain ONLY 2-4 sentences of summary text
- Mention the total/key insights (e.g., "177 active sites across 25 countries")
- Highlight top 2-3 items (e.g., "Italy has the most with 57 sites, followed by France with 28")
- NO tables, NO lists of all data

✅ **What goes in table:**
- ALL tabular data goes in the **table** structure (separate field)
- The UI will render it properly
- NEVER duplicate table data in answer text

✅ **Special cases:**
- If query returns exactly ONE row with a single value (like total count), include in **answer** prose and do NOT return a **table**. Example: "We have 177 active sites."
- When counting sites, always mention "active" since inactive sites are filtered out

OUTPUT SHAPE
1) **answer**: 2–4 numbered bullets summarizing the result.
2) **table**: {{ “columns”:[{{“key”:”...”,”label”:”...”}},...], “rows”:[{{...}},...] }}
   - Use raw numeric values (no thousand separators).
3) **visualization** (optional): {{ “type”:”bar|line|pie|scatter”,”xKey”:”...”,”yKeys”:[“...”],”data”:[...], “meta”:{{“title”:”...”}} }}
4) **explorer_set_filters** when asked to “filter/show on the map”.

EXPLORER INTEGRATION
Every table rendered in chat has an “🎯 Open in Explorer (filter)” button below it.
When user says “show on the map”, “show in explorer”, “ver en el mapa”, “muéstramelo en el mapa”, “open in map”:
→ Reply: “Click **🎯 Open in Explorer (filter)** below the table to view these sites on the interactive map and table.”
Do NOT try to render the map yourself — the button handles navigation automatically.
After nearest_filtered_sites results appear, remind the user they can click 🎯 to view those sites on the map.

DRIVE-KM ANSWERS
- When you use explorer_within_drive_km:
  - If the base_account_id was **inferred from the last results**, explicitly say it in bullet #1 as:
    “Base used: <Account.Name if known, else 'Unknown name'> (<Account.Id>), radius: <N> km.”
    The name can be taken from the previous table row that matches the Account.Id (keys to try:
    "sf.Account.Name", "Account.Name", "site", "account_name").
  - Then list 2–3 short findings (counts, notable neighbors, countries).
  - Do **not** paste JSON in the prose; keep the list clean.

DEFAULT COLUMNS (unless the user asks otherwise)
- Site (ALWAYS use Account.Name from Salesforce)
- Country (ALWAYS use Account.ShippingCountry from Salesforce)
- City (ALWAYS use Account.ShippingCity from Salesforce)
- Screening/follow-up when relevant: Stage1, Stage2
- Newly diagnosed last year: <18 and ≥18
- Current patients: <18 and ≥18
**ALWAYS include an identifier**:
- When using soql_query with Account: include Account.Id (the backend will surface it as sf.Account.Id/account_id).
- When using soql_query for qualification data: SELECT sites.salesforce_account_id AS account_id and include it in the table.

DOMAIN REFERENCE — FIELD KEYS AND QUERY PATTERNS

**CRITICAL**: NEVER use Postgres sites table for basic site counts/lists/geography. ALWAYS use Salesforce Account. The sites table only has qualification uploads.

explorer_search FIELD KEYS:
- qual.3_6__is_your_pharmacy_on_site_or_off_campus (value: 'On-site')
- qual.3_5_2__overnight_stay (value: 'Yes')
- qual.3_3__number_of_examination_rooms (operator: '>', value: numeric)
- qual.3_1__count_ongoing_clinical_trials
- sf.C_Number_of_new_T1D_diagnosed_O_18__c (ND ≥18)
- sf.C_Number_of_new_T1D_diagnosed_U_18__c (ND <18)
- sf.C_Number_of_T1D_Patients_currently_O_18__c (current T1D ≥18)
- sf.C_Number_of_T1D_Patients_currently_U_18__c (current T1D <18)
- sf.C_Number_of_Stage2_Individuals_followed__c (Stage 2)
- sf.C_Number_of_Stage1_Individuals_followed__c (Stage 1)
- sf.C_Is_HLA_typing_performed__c (value: 'Yes')
- sf.C_Profiling_Complete__c (profiling completion status)
- sf.INNODIA_Clinical_Trial_Site__c (boolean — CTS flag)
- sf.Clinical_Site_CS__c (boolean — CS flag)
- sf.C_Referral_Clinical_Partner__c (boolean — RP flag)
- extra.AssignmentsCount (number — how many assignments a site has)
- extra.AssignmentsNames (comma-separated list of assignment/activity names)
- qual.3_8__can_you_do_hla_typing (values: "Yes", "No") — HLA typing capacity
- qual.3_8__znt8 (values: "Yes", "No") — ZnT8 autoantibody testing
- qual.3_8__insulin (values: "Yes", "No") — Insulin autoantibody testing
- qual.3_8__gad65 (values: "Yes", "No") — GAD65 autoantibody testing
- qual.3_8__ia_2 (values: "Yes", "No") — IA-2 autoantibody testing
- qual.3_8__c_peptide (values: "Yes", "No") — C-peptide testing
- site.country (ISO-2 codes), site.city (city name string)
- sf.Account.ShippingCountry OR site.country (ISO-2 codes)
Use INDEX above for other qual field keys (format: alias => qual.<key>).

COUNTRY/CITY FILTERS in explorer_search:
- Always add country/city as explicit rules — NEVER pass empty filters when a location is mentioned.
- Use ISO-2 codes: ES=Spain, IT=Italy, FR=France, DE=Germany, GB=UK, BE=Belgium, NL=Netherlands, PT=Portugal, PL=Poland, CZ=Czech Republic, SE=Sweden, DK=Denmark, NO=Norway, FI=Finland, AT=Austria, CH=Switzerland, IE=Ireland.
- Multi-country → nest in OR sub-group: {{logic:'OR', rules:[{{field:'site.country',operator:'equals',value:'DE'}},{{field:'site.country',operator:'equals',value:'IT'}}]}}

NESTED AND/OR LOGIC in explorer_search:
- Rules can contain nested sub-groups {{logic, rules}} for full boolean logic.
- "Spain OR Italy, but only with overnight stay" → AND at top, OR sub-group for countries.
- Expression style: logic:'1 AND 2 OR 3' with flat rules (AND binds tighter; use nested groups for explicit parenthesization).

AFTER explorer_search → call render_chart to produce a chart if requested.
FOLLOW-UP → if user says "of those, only in Spain", call explorer_search again adding the new rule.

SOQL PATTERNS (for soql_query):
- Site counts: SELECT COUNT(Id) FROM Account WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' AND (Account_Inactive__c=false OR Account_Inactive__c=null) AND (Subaccount_Inactive__c=false OR Subaccount_Inactive__c=null)
- Sites by country: same + GROUP BY ShippingCountry ORDER BY COUNT(Id) DESC
- Patient metrics: SELECT from Opportunity with Account.RecordType.DeveloperName='SubAccount'
- HLA typing: C_Is_HLA_typing_performed__c from Opportunity (NEVER use site_qual for HLA)
- Phase I/II/III: C_Phase_I_Type1__c, C_Phase_II_Type1__c, C_Phase_III_Type1__c (counts) or C_List_of_trial_name_or_sponsors_Type1__c (names)
- Time series: sf_aggregate(mode='time_series', field='Amount', period='quarter'|'month', last_n=8)
- Stage 1/2 fields live on Account (NOT Opportunity)
- Contacts: salesforce_account_contacts with title_contains filter; or salesforce_account_extras for PI/assignments per Account

ACTIVITY QUERIES (via Assignment__c):
Activities = Opportunities with RecordType.DeveloperName='RT_Activity'.
Link chain: Activity (Opportunity) ← C_Opportunity_Name__c — Assignment__c — C_Account__c → Account (Site).

KNOWN ACTIVITIES (use LIKE for partial matches):
- DETECT: "DETECT Pilot Sites" | "DETECT French Roll Out" | "DETECT Italian Roll Out"
- Fabulinus: "Fabulinus CTS Team - Part A" | "Part B" | "FABULINUS Referral Partner Network"
- Baricade: "Baricade Delay (JAJJ)" | "Baricade Preserve (JAJK)" | "Beta Preserve"
- Others: "Diagnode-3 RP Team" | "Safeguard Trial Clinical Sites"

SOQL PATTERN for "sites in activity X":
SELECT C_Account__r.Id, C_Account__r.Name, C_Account__r.ShippingCountry, C_Account__r.ShippingCity,
       C_Assignment_Stage__c, Assignment_Type__c, C_Opportunity_Name__r.Name
FROM Assignment__c
WHERE C_Opportunity_Name__r.Name LIKE '%X%'
AND C_Opportunity_Name__r.RecordType.DeveloperName = 'RT_Activity'
AND RecordType.DeveloperName = 'Account_Assignment'
ORDER BY C_Account__r.ShippingCountry, C_Account__r.Name

If name matches multiple activities (e.g. "Fabulinus") → return ALL grouped by activity name, do NOT ask for clarification.
TABLE COLUMNS for activity queries: Activity, Site, Country, City, Assignment Stage.

PROXIMITY QUERIES:
- nearest_filtered_sites: by city name/address, supports filters and max_km. Distances are Haversine (straight-line).
- explorer_within_drive_km: by known Account ID, driving distance. Do NOT use for city-name queries.
After proximity results, remind user they can click the Open in Explorer button to view on the map.

CHART GENERATION (render_chart):
When user asks for a chart/visualization, you MUST: (1) fetch data with soql_query or explorer_search, (2) call render_chart with the data.
NEVER return ONLY a table when a chart is requested — always call render_chart after fetching data.

STYLE
- Be direct and neutral. Fall back gracefully between SF and Postgres and mention it briefly in bullets.
- Do **not** show internal SQL/SOQL unless explicitly requested.

CLARIFICATIONS
- If the user mentions “patients” without specifying whether they mean “currently” vs “newly diagnosed” and the age group (<18, ≥18), ask a single clarification question first and wait. Offer options: Currently <18, Currently ≥18, Newly diagnosed <18, Newly diagnosed ≥18, and both variants.
- Keep the clarification short (one sentence). Do not call any tools before the user chooses.

SCENARIOS (extended)
E) Screening program overview (all sites with screening program)
   - Select flags from site_qual (e.g., Aware_of_any_Screening_Program, Center_for_Running_Early_Diagnosis).
   - Include Stage1 and Stage2 counts, and, when relevant, newly diagnosed (<18, ≥18) and current patients (<18, ≥18).
   - Columns: Account.Id/Name/Country/City + Stage1 + Stage2 + NewlyDx<18 + NewlyDx≥18 + Current<18 + Current≥18.
F) Qualification features filter (onsite pharmacy, overnight stay)
   - Filter using site_qual boolean/text keys; return a compact table with IDs + Name/Country/City + NewlyDx (both ages) + Current (both ages) + PI Name + assignments_count when available.

Return only the data structures and short text described above; the UI handles rendering.

---

GROUNDING & UNCERTAINTY RULES (mandatory — follow at all times)

1. **Never fabricate data**: Only report numbers and facts retrieved via tool calls (Salesforce, Postgres, or the local sites DB). If a tool returns no rows, say "No data found" or "No sites match those criteria." Do NOT estimate, invent, or extrapolate numbers.

2. **No-results response**: When explorer_search or soql_query returns 0 rows, respond concisely:
   - "No sites match those criteria." (for site/filter queries)
   - "No data found for that query." (for metric queries)
   Do NOT add hedging phrases like "it appears there may be…" or "perhaps try…" unless you have a concrete alternative to suggest.

3. **Uncertainty about field names**: If you are unsure whether an API field name is correct, say so explicitly. Do NOT guess Salesforce API names — use the DOMAIN GLOSSARY and QUERY ROUTING EXAMPLES above. If a field is not listed, say "I'm not sure of the exact field name — please check the SF schema."

4. **Source citations**: When returning data, note the source briefly at the end of your answer:
   - Salesforce fields only → append "(Source: Salesforce)"
   - Qualification checklist (site_qual) only → append "(Source: Qualification DB)"
   - Both combined → append "(Source: Salesforce + Qual DB)"
   - Local geometry/distance only → append "(Source: Site coordinates DB)"
   Skip the citation only for pure conversational answers (no data involved).

5. **Context-referencing queries** ("how many?", "which ones?", "that list" without a noun): these refer to the rows in the last table shown. Do NOT re-query Salesforce — answer directly from the previous result context injected above. If the prior table doesn't contain the needed info, say so and offer to re-run a fresh query.

6. **Scope boundary**: This dashboard contains INNODIA network sites only. If a site or account name is not found in query results, respond "Not found in the INNODIA network data." Do NOT reference external databases, public registries, or non-INNODIA information.

7. **No markdown hallucination**: Do not add table rows, bullet items, or numbers beyond what the tool results contain. If a tool result has 5 rows, your table must have exactly 5 rows.

8. **Stale context**: If you lack the data needed to answer (e.g. the previous table is empty or the context window has no relevant data), ask the user to re-run the query rather than guessing. Example: "I don't have that data in the current context — please ask me to fetch it again."

---

MEMBER ACCOUNTS (use members_search tool for all queries about these)

Member = an institution/university/hospital that is an INNODIA network member.
RecordType.DeveloperName = 'RT_Member' on Account.
Key fields:
  C_Level_of_Membership__c — membership level (e.g. Full Member, Associate Member)
  Account_Status__c — account status
  C_Member_Representative__c → Contact (main institutional representative)
  SubAccounts linked via: WHERE C_Member__c = '<member_id>' AND RecordType.DeveloperName = 'SubAccount'

PROPOSED ROLES to play in the Network (boolean checkboxes on Member Account):
  Clinical Site (CS):              Clinical_Site_CS__c
  Diagnostic Lab (DxLab):          C_Deliver_Clinical_Grade_Services__c
  Research & Mechanistic Lab (LAB):C_Perform_Cutting_Edge__c
  Patient Organization:            C_Contribute_as_a_Patient_Organization__c

VALIDATED ROLES to play in the Network (boolean checkboxes on Member Account):
  Validated Clinical Site (CS):    Clinical_Site_CS_validated__c
  Validated Clinical Trial Site:   Clinical_Trial_Site_CTS_validated__c
  Validated Diagnostic Lab (DxLab):Diagnostic_Lab_DxLab_validated__c
  Validated Res & Mech Lab (LAB):  Research_Mechanistic_Lab_LAB_validated__c
  Validated Patient Organization:  Patient_Organization_validated__c

Key contact flags (on Contact objects linked to Member):
  C_Board_Member__c, C_Country_Lead__c, C_Voting_Rights__c

QUERY ROUTING for members:
- "how many member institutions" → members_search with empty rules → count rows
- "members in [country]" → members_search filter site.country
- "members with validated CTS" → members_search filter sf.Clinical_Trial_Site_CTS_validated__c = true
- "members with proposed DxLab role" → members_search filter sf.C_Deliver_Clinical_Grade_Services__c = true
- "members with both proposed CS and validated CTS" → members_search AND filter both boolean fields
- "contacts at [institution]" → members_search with name filter + include_detail=true
- "board members / country leads" → members_search + include_detail=true; filter by contact flag
- "how many sites does [member] have" → members_search + check extra.SubAccountsCount

## Common Query Patterns (for explorer_search)

### "Sites in [country]"
Filter: {{field: "site.country", operator: "equals", value: "ISO2_CODE"}}
Example for Germany: {{field: "site.country", operator: "equals", value: "DE"}}

### "Sites with [qualification feature]"
Use qual.* filter. For YES/NO fields use operator "contains" value "yes" (not "is_not_empty").

### "Sites near [city]"
Use nearest_filtered_sites with city parameter (not explorer_search).

### "Sites near [city] not in [study/activity]"
Use nearest_filtered_sites with location AND filters in ONE call. Do NOT make separate calls.
Example: "sites near Brussels not in INNODIA Master" →
nearest_filtered_sites(location="Brussels", filters={{logic:"AND", rules:[{{field:"extra.AssignmentsNames", operator:"not_contains", value:"INNODIA Master"}}]}})

### "Sites near [city] not in any assignment"
nearest_filtered_sites(location="city", filters={{logic:"AND", rules:[{{field:"extra.AssignmentsCount", operator:"is_empty"}}]}})

### "Sites not in any assignment"
Filter: {{field: "extra.AssignmentsCount", operator: "is_empty"}}

### "Sites not in [specific study/activity]"
Filter: {{field: "extra.AssignmentsNames", operator: "not_contains", value: "study name"}}

### "Sites in assignment X"
Filter: {{field: "extra.AssignmentsNames", operator: "contains", value: "X"}}

### "How many sites per country"
Use group_count with group_by="country".

### "Top N sites by [metric]"
Use rank_sites with the sf.* or qual.* metric field and limit=N.

### Multi-condition example
"German sites with overnight stays not in any assignment, ranked by ND" →
explorer_search with filters: site.country=DE AND qual.3_5_2__overnight_stay contains "Yes" AND extra.AssignmentsCount is_empty
Then present results sorted by ND values, or call rank_sites for formal ranking.
"""

def _is_complex_query(text: str) -> bool:
    """
    Return True for queries that benefit from extended thinking:
    - Nearest/closest + additional filter criteria
    - Two or more distinct clinical data concepts in the same question
    - Explicit multi-condition phrasing

    Queries handled by the deterministic planner never reach Claude,
    so we only need to catch the harder cases that do.
    """
    s = (text or "").lower()

    # Nearest / distance + any filter qualifier → always complex
    if re.search(r"\b(nearest|closest|cerca\s+de|próxim\w*|vicino|nahe)\b", s) and \
       re.search(r"\b(with|that\s+have|which\s+have|que\s+tengan|stage|pharmacy|nd\b|overnight|pi\b)\b", s):
        return True

    # Count distinct clinical data concepts; ≥ 2 together = complex
    concepts = [
        bool(re.search(r"\bstage\s*[12]\b", s)),
        bool(re.search(r"\b(nd\b|newly.diagnosed|new.diagnos|recién.diagnos)", s)),
        bool(re.search(r"\b(pharmacy|farmacia|on.?site.?pharm)", s)),
        bool(re.search(r"\b(overnight|pernoctaci|stay\s+overnight)", s)),
        bool(re.search(r"\b(pi\b|principal.investigator|coord\w+)", s)),
        bool(re.search(r"\b(hla|typing)\b", s)),
        bool(re.search(r"\b(phase\s*[i123]|ct.?site|clinical.trial\s+site)", s)),
        bool(re.search(r"\b(assignment|mca|payment)\b", s)),
    ]
    if sum(concepts) >= 2:
        return True

    # Explicit multi-condition connectors in a data query
    if re.search(r"\b(and\s+(?:also|with|have)|además\s+de|y\s+también)\b", s) and \
       re.search(r"\b(site|center|centro|account)\b", s):
        return True

    return False


def _truncate_history(messages: List[Any], max_turns: int = MAX_HISTORY_TURNS) -> List[Any]:
    """
    Keep only the last `max_turns` user messages and everything after them.
    Prevents unbounded context growth in long conversations.
    Works on both List[ChatMessage] and List[Dict] (role/content dicts).
    """
    def _role(m: Any) -> str:
        return m.role if hasattr(m, "role") else m.get("role", "")

    user_indices = [i for i, m in enumerate(messages) if _role(m) == "user"]
    if len(user_indices) <= max_turns:
        return messages
    cut_at = user_indices[-max_turns]
    _dbg("History truncated: kept last %d user turns (dropped first %d messages)", max_turns, cut_at)
    return messages[cut_at:]


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

    # 4. Call Claude API
    # When thinking is enabled we pass `betas` via extra_headers so the standard
    # messages.create() endpoint handles it without needing the beta namespace.
    # When _STREAM_Q.q is set (by /chat/stream endpoint), we use streaming mode:
    # text chunks are put into the queue so the SSE endpoint can yield them
    # progressively; the final Message object is used the same way as non-streaming.
    try:
        aclient = _anthropic_sdk.Anthropic(api_key=api_key)
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


def _dispatch_tool_calls(
    assistant_msg,
    msgs: List[Dict[str, Any]],
    tool_ctx: "ToolContext",
    seen_hashes: set,
    dm_called: bool,
    tool_calls_made: List[Dict[str, Any]],
    turn,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    """Dispatch every tool_call in assistant_msg, applying dedup + DM + truncation.

    Returns a tuple:
        (dm_called_after, last_table, last_viz, last_filters, table_produced_now)

    - dm_called_after: True if any DM tool was dispatched in this call.
    - last_table/viz/filters: latest values after dispatch (None if none set).
    - table_produced_now: True if any dispatched tool set a non-None last_table.
    """
    import hashlib
    from app.routers.moby_tools import dispatch_tool

    _DM_TOOL_NAMES = {"nearest_filtered_sites", "explorer_within_drive_km"}
    _char_limit = MAX_TOOL_RESULT_TOKENS * 4

    last_table: Optional[Dict[str, Any]] = None
    last_visualization: Optional[Dict[str, Any]] = None
    last_explorer_filters: Optional[Dict[str, Any]] = None
    table_produced_now = False

    for tc in assistant_msg.tool_calls:
        name = tc.function.name
        args_json = tc.function.arguments or "{}"
        args = json.loads(args_json)
        tool_call_id = tc.id

        call_hash = hashlib.md5(f"{name}:{args_json}".encode()).hexdigest()
        if call_hash in seen_hashes:
            _dbg("Agentic loop: DEDUP skip %s (same args)", name)
            msgs.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps({
                    "error": f"Duplicate call to {name} with same arguments — skipped. Use different parameters or summarize what you have."
                }),
            })
            continue
        seen_hashes.add(call_hash)

        if name in _DM_TOOL_NAMES:
            if dm_called:
                _dbg("Agentic loop: DM tool %s blocked (already called a DM tool)", name)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps({
                        "error": "Distance Matrix tool already called this turn. Use the results from the previous call."
                    }),
                })
                continue
            dm_called = True

        _dbg("Agentic loop turn %s: TOOL %s args=%s", turn, name, args)
        tool_calls_made.append({"tool": name, "turn": turn})

        tool_ctx.args = args
        tool_ctx.tool_call_id = tool_call_id

        tool_result = dispatch_tool(name, tool_ctx)

        # Truncate oversized tool results in msgs immediately
        for _m in reversed(msgs):
            if _m.get("role") == "tool" and _m.get("tool_call_id") == tool_call_id:
                _content = _m.get("content", "")
                if len(_content) > _char_limit:
                    try:
                        _parsed = json.loads(_content)
                        if isinstance(_parsed, dict) and "rows" in _parsed:
                            _rows = _parsed["rows"]
                            if isinstance(_rows, list) and len(_rows) > 50:
                                _parsed["rows"] = _rows[:50]
                                _parsed["_truncated"] = f"Showing 50 of {len(_rows)} rows"
                                _m["content"] = json.dumps(_parsed, default=str)
                    except Exception:
                        pass
                break

        if tool_result.last_table is not None:
            last_table = tool_result.last_table
            tool_ctx.last_table = last_table
            table_produced_now = True
        if tool_result.last_visualization is not None:
            last_visualization = tool_result.last_visualization
            tool_ctx.last_visualization = last_visualization
        if tool_result.last_explorer_filters is not None:
            last_explorer_filters = tool_result.last_explorer_filters
            tool_ctx.last_explorer_filters = last_explorer_filters

    return (dm_called, last_table, last_visualization, last_explorer_filters, table_produced_now)


# ====== Agentic loop ======

def _agentic_loop(
    msgs: List[Dict[str, Any]],
    tool_ctx: "ToolContext",
    *,
    use_thinking: bool = False,
    user_msg: str = "",
) -> Dict[str, Any]:
    """
    Bounded agentic loop: up to MOBY_MAX_AGENT_TURNS turns of
    Claude tool-use → dispatch → feed results back.

    On the final turn, forces text-only response (no tools).

    Returns dict with keys: text, turns_used, tool_calls_made,
    last_table, last_visualization, last_explorer_filters.
    """
    from app.routers.moby_tools import ToolContext, dispatch_tool
    from app.routers.moby_tool_policy import (
        has_tabular_intent,
        filter_tools_spec,
    )

    _tabular = False
    _whitelist_spec: Optional[List[Dict[str, Any]]] = None
    try:
        if has_tabular_intent(user_msg):
            _tabular = True
            _whitelist_spec = filter_tools_spec(TOOLS_SPEC)
            _dbg("Tabular intent detected (query=%r), forcing tool_choice=required", user_msg[:80])
    except Exception as _e:
        _dbg("has_tabular_intent failed, defaulting to non-tabular: %s", _e)

    start_time = _monotonic()
    seen_hashes: set = set()
    dm_called = False  # Distance Matrix tools can only be called once per message
    _DM_TOOL_NAMES = {"nearest_filtered_sites", "explorer_within_drive_km"}
    tool_calls_made: List[Dict[str, Any]] = []
    text_out: Optional[str] = None
    _table_produced_this_request = False

    last_table = tool_ctx.last_table
    last_visualization = tool_ctx.last_visualization
    last_explorer_filters = tool_ctx.last_explorer_filters

    max_turns = MOBY_MAX_AGENT_TURNS

    for turn in range(1, max_turns + 1):
        # Timeout guard
        elapsed = _monotonic() - start_time
        if elapsed > MOBY_AGENT_TIMEOUT_S:
            _dbg("Agentic loop timeout after %.1fs at turn %d", elapsed, turn)
            break

        is_final = (turn == max_turns)

        # Extended thinking only on turn 1
        think = use_thinking if turn == 1 else False

        _dbg("Agentic loop turn %d/%d (final=%s, thinking=%s)", turn, max_turns, is_final, think)

        try:
            if _tabular and turn == 1:
                # Anthropic API rejects thinking + tool_choice="required" together.
                # Force-tool implies Claude doesn't need to deliberate — just call.
                resp = _claude_chat(
                    msgs,
                    tool_choice="required",
                    force_no_tools=is_final,
                    use_thinking=False,
                    tools_override=_whitelist_spec,
                )
            else:
                resp = _claude_chat(msgs, tool_choice="auto", force_no_tools=is_final, use_thinking=think)
        except (_anthropic_sdk.APITimeoutError, _anthropic_sdk.APIConnectionError, _anthropic_sdk.RateLimitError) as e:
            _dbg("Anthropic API error in agentic loop turn %d: %s", turn, e)
            text_out = f"<p>Service temporarily unavailable. Please try again later. ({str(e)[:50]})</p>"
            break

        assistant_msg = resp.choices[0].message

        # No tool calls → extract text and exit
        if not assistant_msg.tool_calls:
            text_out = (assistant_msg.content or "").strip()
            _dbg("Agentic loop turn %d: text response (%d chars), exiting", turn, len(text_out))
            break

        # Append assistant message with tool_calls to conversation
        msgs.append({
            "role": "assistant",
            "content": assistant_msg.content or "",
            "tool_calls": [tc.model_dump() for tc in assistant_msg.tool_calls],
        })

        # Dispatch each tool call
        (dm_called, _lt, _lv, _lf, _produced_now) = _dispatch_tool_calls(
            assistant_msg, msgs, tool_ctx, seen_hashes, dm_called, tool_calls_made, turn,
        )
        if _lt is not None:
            last_table = _lt
        if _lv is not None:
            last_visualization = _lv
        if _lf is not None:
            last_explorer_filters = _lf
        if _produced_now:
            _table_produced_this_request = True

        # Fast exit: if tool(s) returned a good table AND Claude included a meaningful
        # text summary alongside the tool call, skip the synthesis turn (saves 3-8s).
        # If no companion text, let Claude do turn 2 to write a proper human-like summary.
        if last_table and last_table.get("rows"):
            companion_text = (assistant_msg.content or "").strip()
            if companion_text and len(companion_text) > 40:
                text_out = companion_text
                _dbg("Agentic loop: fast exit after turn %d — table + companion text (%d chars)", turn, len(companion_text))
                break

        # Send progress event if streaming
        token_q = getattr(_STREAM_Q, "q", None)
        if token_q is not None:
            try:
                progress = json.dumps({
                    "turn": turn,
                    "tools": [tc.function.name for tc in assistant_msg.tool_calls],
                })
                token_q.put(f"__PROGRESS__{progress}")
            except Exception:
                pass

    # Post-loop retry: one forced call if the loop exited text-only but the
    # user asked for a table and no table was produced this request.
    if _tabular and text_out and not _table_produced_this_request and _whitelist_spec is not None:
        _dbg("Retry triggered: loop exited text-only with tabular intent, re-calling with whitelist")
        msgs.append({
            "role": "user",
            "content": (
                "The previous answer lacked a table. The user asked for a list/table — "
                "you MUST call one of: explorer_search, nearest_filtered_sites, "
                "study_coordinators_with_activities, members_search."
            ),
        })
        try:
            retry_resp = _claude_chat(
                msgs,
                tool_choice="required",
                force_no_tools=False,
                use_thinking=False,
                tools_override=_whitelist_spec,
            )
        except (_anthropic_sdk.APITimeoutError, _anthropic_sdk.APIConnectionError, _anthropic_sdk.RateLimitError) as _e:
            _dbg("Retry failed: %s", _e)
        else:
            retry_msg = retry_resp.choices[0].message
            if retry_msg.tool_calls:
                msgs.append({
                    "role": "assistant",
                    "content": retry_msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in retry_msg.tool_calls],
                })
                (dm_called, _lt, _lv, _lf, _produced_now) = _dispatch_tool_calls(
                    retry_msg, msgs, tool_ctx, seen_hashes, dm_called, tool_calls_made, turn="retry",
                )
                token_q = getattr(_STREAM_Q, "q", None)
                if token_q is not None:
                    try:
                        progress = json.dumps({
                            "turn": "retry",
                            "tools": [tc.function.name for tc in retry_msg.tool_calls],
                        })
                        token_q.put(f"__PROGRESS__{progress}")
                    except Exception:
                        pass
                if _lt is not None:
                    last_table = _lt
                if _lv is not None:
                    last_visualization = _lv
                if _lf is not None:
                    last_explorer_filters = _lf
                if _produced_now:
                    _table_produced_this_request = True
                    retry_text = (retry_msg.content or "").strip()
                    if retry_text:
                        text_out = retry_text

    return {
        "text": text_out,
        "turns_used": turn,
        "tool_calls_made": tool_calls_made,
        "last_table": last_table,
        "last_visualization": last_visualization,
        "last_explorer_filters": last_explorer_filters,
    }


# ====== Endpoint ======
def _sf_escape_value(v: str) -> str:
    try:
        return str(v).replace("'", "\\'")
    except Exception:
        return ""


@router.post("/chat/stream")
def chat_stream_api(payload: ChatRequest, request: Request, db: Session = Depends(get_db)):
    """
    SSE streaming endpoint.
    Yields text tokens as Claude generates them, then a final 'done' event
    with the full JSON payload (answer, table, last_filters, visualization).

    Event format:
      data: {"type":"token","text":"<html chunk>"}
      data: {"type":"progress","turn":1,"tools":["explorer_search"]}
      data: {"type":"done","answer":"...","table":{...},...}
      data: {"type":"error","message":"..."}
    """
    from fastapi.responses import StreamingResponse as _SR

    token_q: _std_queue.Queue = _std_queue.Queue()
    result_holder: Dict[str, Any] = {}

    def worker():
        _STREAM_Q.q = token_q
        try:
            result_holder["r"] = chat_api(payload, request, db)
        except Exception as exc:
            import traceback
            _dbg("STREAM worker error: %s\n%s", exc, traceback.format_exc())
            result_holder["error"] = str(exc)
        finally:
            _STREAM_Q.q = None
            token_q.put(None)  # sentinel — signals end of stream

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    def generate():
        while True:
            try:
                chunk = token_q.get(timeout=180)
            except _std_queue.Empty:
                yield f"data: {json.dumps({'type':'error','message':'Stream timeout'})}\n\n"
                break
            if chunk is None:
                break
            # Progress events from agentic loop (prefixed with __PROGRESS__)
            if isinstance(chunk, str) and chunk.startswith("__PROGRESS__"):
                yield f"data: {chunk[12:]}\n\n"  # already JSON with "type":"progress"
            else:
                yield f"data: {json.dumps({'type':'token','text':chunk})}\n\n"

        t.join(timeout=10)
        if "error" in result_holder:
            yield f"data: {json.dumps({'type':'error','message':result_holder['error']})}\n\n"
        else:
            r = result_holder.get("r") or {}
            yield f"data: {json.dumps({'type':'done', **r}, default=str)}\n\n"

    return _SR(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering for SSE
            "Connection": "keep-alive",
        },
    )


@router.post("/chat")
def chat_api(payload: ChatRequest, request: Request, db: Session = Depends(get_db)):
    # Clarificador: incluir subcuentas clínicas para roles/contactos
    def _needs_contact_clarification(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        s = text.lower()
        # Skip si es petición de chart/visualización
        if re.search(r"\b(chart|graph|bar|pie|line|visuali[sz]e|plot)\b", s):
            return None
        # Evitar clarificador si no es un intento de contactos (p. ej., HLA, activities, stage)
        if re.search(r"\bhla\b|\btyping\b|\bactivit(?:y|ies)\b|\bstage\s*[12]\b", s):
            return None
        role_terms = [
            "study coordinator","coordinator","study nurse","principal investigator","pi","clinician","project manager","technician",
            "member representative","contact person","associate scientist","post-doc","phd student","head","fellow"
        ]
        # Nunca activar si la intención es sobre asignaciones
        if "assignment" in s or "assignments" in s:
            return None
        # No clarifier for Study Coordinator/PI intent: resolvemos directamente con la tool dedicada
        if any(t in s for t in ("study coordinator","coordinator","principal investigator","pi")):
            return None
        if not any(t in s for t in role_terms):
            return None
        if "subaccount" in s or "sub-account" in s or "child account" in s or "include sub" in s:
            return None
        return {
            "answer": "<p>Need one more detail.</p>",
            "clarify": {
                "question": "Include clinical sub‑accounts when searching contacts?",
                "options": [
                    {"label":"Yes, include clinical sub‑accounts","query":"contacts: include_subaccounts=true"},
                    {"label":"No, only the main account","query":"contacts: include_subaccounts=false"}
                ]
            }
        }

    # Clarificador: wizard de report
    def _needs_report_wizard(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        s = text.lower()
        # Do NOT trigger wizard on activities/assignments intents
        if "activit" in s or "assignment" in s:
            return None
        if not re.search(r"\breport|summary|resumen|informe\b", s):
            return None
        return {
            "answer": "<p>Select the report layout.</p>",
            "clarify": {
                "question": "Choose dimensions and metrics for the report.",
                "options": [
                    {"label":"By Country: Stage1 + Stage2","query":"Report: dim=country; metrics=stage1,stage2"},
                    {"label":"By City: Newly Dx <18 + ≥18","query":"Report: dim=city; metrics=new_dx_u18,new_dx_o18"},
                    {"label":"By Account: Current <18 + ≥18","query":"Report: dim=account; metrics=current_u18,current_o18"}
                ]
            }
        }

    # Aplica clarificadores
    try:
        last_text = payload.messages[-1].content if payload.messages else ""
        for _fn in (_needs_contact_clarification, _needs_report_wizard):
            clar = _fn(last_text)
            if clar:
                return clar
    except Exception:
        pass

    # Salesforce client (si hay sesión)
    try:
        sf = get_sf_client(request)
    except Exception:
        sf = None

    # ===== Índice de conocimiento y pistas de enrutamiento =====
    cache = _build_knowledge_index(db)
    kindex: Dict[str, Dict[str, str]] = cache["index"]
    # Breve volcado (capado) del índice para el modelo
    preview_items = []
    for alias, meta in list(kindex.items())[:INDEX_PREVIEW_LIMIT]:
        if meta.get("source") == "sf":
            preview_items.append(f"{alias} => sf.{meta.get('field')}")
        else:
            # Usar prefijo qual. para que el AI construya filtros compatibles con explorer_search
            key = meta.get("key") or meta.get("field") or ""
            preview_items.append(f"{alias} => qual.{key}")
    INDEX_SNIPPET = " | ".join(preview_items)

    # ===== Mini‑planificador determinista =====
    def _try_planner(user_text: str, has_table: bool = False, last_filters: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        if not user_text:
            return None
        s = user_text.lower()
        # Whether the query clearly continues a prior turn (Phase 2 followup merge handles these)
        _is_followup_prefix = bool(
            re.match(r"^(and\b|also\b|y\b|también\b|tambien\b|ahora\b|además\b|ademas\b)", s.strip())
            and last_filters
        )

        # MATH OPERATIONS on last_table (sum, average, min, max, double) — no Claude/SF call needed
        try:
            _math_intent = re.search(
                r"\b(sum|total|average|avg|mean|min(?:imum)?|max(?:imum)?|double|triple|half)\b", s
            )
            _has_new_data = re.search(
                r"\b(site[s]?|center[s]?|countr|cit[yi]|patient|stage|nd\b|t1d|hla|pharmac|"
                r"find|show|list|search|per\s+country|per\s+city"
                r"|in\s+(?!that\b|those\b|this\b|the\b|them\b|it\b|which\b)[a-z]{3,})\b", s
            )
            if _math_intent and not _has_new_data and has_table and last_table_from_payload:
                _m_rows = last_table_from_payload.get("rows") or []
                _m_cols = [c.get("key") if isinstance(c, dict) else str(c)
                           for c in (last_table_from_payload.get("columns") or [])]
                if _m_rows and _m_cols:
                    # Find numeric columns
                    _num_cols = [
                        _k for _k in _m_cols
                        if any(isinstance(r.get(_k), (int, float)) for r in _m_rows[:5]
                               if isinstance(r, dict))
                    ]
                    # Pick best column: prefer one whose name overlaps query words
                    _target_col = None
                    _query_words = set(re.findall(r"[a-z]{3,}", s))
                    for _nc in _num_cols:
                        _nc_words = set(re.findall(r"[a-z]{3,}", _nc.lower().replace("_", " ")))
                        if _nc_words & _query_words:
                            _target_col = _nc
                            break
                    if not _target_col and _num_cols:
                        _target_col = _num_cols[0]
                    if _target_col:
                        _nums = [float(r.get(_target_col) or 0)
                                 for r in _m_rows if isinstance(r, dict)]
                        _col_lbl = _pretty_label(_target_col)
                        _op = _math_intent.group(1).lower()
                        def _fmt(v: float) -> str:
                            return str(int(v)) if isinstance(v, float) and v.is_integer() else str(round(v, 2))
                        if _op in ("sum", "total"):
                            _r = sum(_nums)
                            return {"answer": f"<p>The total <strong>{_col_lbl}</strong> across {len(_nums)} rows is <strong>{_fmt(_r)}</strong>.</p>"}
                        elif _op in ("average", "avg", "mean"):
                            _r = sum(_nums) / len(_nums) if _nums else 0.0
                            return {"answer": f"<p>The average <strong>{_col_lbl}</strong> across {len(_nums)} rows is <strong>{_fmt(_r)}</strong>.</p>"}
                        elif _op in ("min", "minimum"):
                            _r = min(_nums) if _nums else 0.0
                            return {"answer": f"<p>The minimum <strong>{_col_lbl}</strong> is <strong>{_fmt(_r)}</strong>.</p>"}
                        elif _op in ("max", "maximum"):
                            _r = max(_nums) if _nums else 0.0
                            return {"answer": f"<p>The maximum <strong>{_col_lbl}</strong> is <strong>{_fmt(_r)}</strong>.</p>"}
                        elif _op == "double":
                            _total = sum(_nums)
                            if len(_nums) == 1:
                                _r = _nums[0] * 2
                                return {"answer": f"<p>Double of <strong>{_col_lbl}</strong> ({_fmt(_nums[0])}) = <strong>{_fmt(_r)}</strong>.</p>"}
                            else:
                                _r = _total * 2
                                return {"answer": f"<p>Sum of <strong>{_col_lbl}</strong> ({_fmt(_total)}) × 2 = <strong>{_fmt(_r)}</strong>.</p>"}
                        elif _op == "triple":
                            _total = sum(_nums)
                            _r = _total * 3
                            return {"answer": f"<p>Sum of <strong>{_col_lbl}</strong> ({_fmt(_total)}) × 3 = <strong>{_fmt(_r)}</strong>.</p>"}
                        elif _op == "half":
                            _total = sum(_nums)
                            _r = _total / 2
                            return {"answer": f"<p>Half of the total <strong>{_col_lbl}</strong> ({_fmt(_total)}) = <strong>{_fmt(_r)}</strong>.</p>"}
        except Exception as _me:
            _dbg("WARN: planner math handler failed: %s", _me)

        # REMOVED: show-on-map handler — migrated to Claude agentic loop
        # try:
        #     if re.search(r"\b(map|mapa)\b", s) and re.search(r"\b(show|ver|display|open|muestra|see|view)\b", s):
        #         return {
        #             "answer": "<p>Click <strong>🎯 Open in Explorer (filter)</strong> below the table to view these sites on the interactive map and table.</p>"
        #         }
        # except Exception as _e:
        #     _dbg("WARN: planner show-on-map handler failed: %s", _e)

        # EARLY: "show as [grouped] bar chart" with last_table provided → convert table directly (no LLM)
        try:
            if (has_table and payload.last_table and
                    re.search(r"\b(chart|bar|pie|graph|grouped|stacked)\b", s) and
                    len(s.split()) < 15):
                _lt = payload.last_table
                _lt_rows = _lt.get("rows") or []
                _lt_cols = _lt.get("columns") or []
                if _lt_rows and _lt_cols:
                    _lt_col_keys = [c.get("key") if isinstance(c, dict) else str(c) for c in _lt_cols]
                    _xkey_t = _lt_col_keys[0] if _lt_col_keys else "label"
                    _ykeys_t = [
                        _ck for _ck in _lt_col_keys[1:]
                        if any(isinstance(_r.get(_ck), (int, float)) for _r in _lt_rows[:5])
                    ]
                    if _ykeys_t:
                        _type_t = "pie" if re.search(r"\bpie\b", s) else "bar"
                        _viz_t = tool_render_chart(_type_t, _lt_rows, _xkey_t, _ykeys_t,
                                                   {"title": "Chart from table"}, {})
                        _dbg("Planner EARLY table-to-chart: type=%s xKey=%s yKeys=%s rows=%d",
                             _type_t, _xkey_t, str(_ykeys_t[:3]), len(_lt_rows))
                        return {
                            "answer": f"<p>Here is the {_type_t} chart.</p>",
                            "table": _normalize_table_for_ui(_lt),
                            "visualization": _viz_t.get("visualization"),
                        }
        except Exception as _e:
            _dbg("WARN: planner EARLY table-to-chart failed: %s", _e)

        # ═══════════════════════════════════════════════════════════════════════
        # ALL DETERMINISTIC HANDLERS BELOW DISABLED — migrated to Claude agentic loop.
        # The following handlers are preserved as dead code for domain logic reference.
        # They were disabled on 2026-04-09 as part of the agentic loop migration (Task 5).
        #
        # Disabled handlers:
        #   - Sites-per-country chart
        #   - HLA % per country chart
        #   - Member institutions (MB6, MB7, ST8, QI02, roles, contacts)
        #   - Study Coordinator / PI / contacts (SC, PI, OC, P04)
        #   - Percentage of sites in activity/assignment
        #   - Activity / assignment handlers (moby_handlers.py delegation)
        #   - Newly Diagnosed (ND) ranking/aggregation
        #   - T1D currently followed (threshold)
        #   - T1D patients ranking
        #   - Stage 1/2 by country aggregation
        #   - Stage 1/2 site list
        #   - Phase I/II/III clinical trials
        #   - Assignment queries (Explorer API)
        #   - Qual field queries (pharmacy, overnight, HLA, ZnT8, etc.)
        #   - Profiling date-diff aggregation (GQ16)
        #   - DR02 qual upload + no geocoordinates
        #   - Pipeline/status handlers (CTS-validated, profiling, referral, meeting dates)
        #   - Country sites handler
        #   - HLA typing in country
        #   - Sites-per-country chart (secondary)
        #   - Group count / rank by group
        # ═══════════════════════════════════════════════════════════════════════
        return None

        # REMOVED: sites-per-country chart — migrated to Claude agentic loop
        # try:
        #     _early_chart_country = (
        #         re.search(r"\b(chart|bar|pie|graph|gr[aá]fico|barras|visuali[sz]e|plot)\b", s) and
        #         re.search(r"\b(sites?|sitios?|centers?|centres?|clinical|cl[ií]nico[s]?|distributed)\b", s) and
        #         re.search(r"\b(country|countries|pa[ií]s|pa[ií]ses|por\s+pa[ií]s|per\s+country|by\s+country)\b", s) and
        #         not re.search(r"\bhla\b|\bstage\b|\bnd\b|activit|assignment|\bnewly\b|\bdiagnos\b|\bexplorer\b|\bsearch\s+for\b", s)
        #     )
        #     if _early_chart_country and sf:
        #         _soql_ec = (
        #             "SELECT ShippingCountry, COUNT(Id) "
        #             "FROM Account "
        #             "WHERE RecordType.DeveloperName='SubAccount' "
        #             "AND C_Type__c='Clinical' "
        #             "AND ShippingCountry != null "
        #             "AND (Account_Inactive__c=false OR Account_Inactive__c=null) "
        #             "AND (Subaccount_Inactive__c=false OR Subaccount_Inactive__c=null) "
        #             "GROUP BY ShippingCountry "
        #             "ORDER BY COUNT(Id) DESC"
        #         )
        #         _raw_ec = sf.query_all(_soql_ec)
        #         _recs_ec = _raw_ec.get("records", []) if isinstance(_raw_ec, dict) else []
        #         _rows_ec = [
        #             {"country": _r.get("ShippingCountry", ""),
        #              "sites": int(_r.get("expr0") or _r.get("COUNT(Id)") or 0)}
        #             for _r in _recs_ec if _r.get("ShippingCountry")
        #         ]
        #         if _rows_ec:
        #             _kind_ec = "pie" if re.search(r"\b(pie|circular|dona|donut|ring)\b", s) else "bar"
        #             _total_ec = sum(r["sites"] for r in _rows_ec)
        #             _tbl_ec = _normalize_table_for_ui({
        #                 "columns": [{"key": "country", "label": "Country"}, {"key": "sites", "label": "Sites"}],
        #                 "rows": _rows_ec,
        #             })
        #             _viz_ec = tool_render_chart(_kind_ec, _rows_ec, "country", ["sites"],
        #                                        {"title": "Clinical Sites per Country"}, {})
        #             _dbg("Planner EARLY sites-per-country chart (%s): %d countries", _kind_ec, len(_rows_ec))
        #             return {
        #                 "answer": f"<p>{_total_ec} sites across {len(_rows_ec)} countries.</p>",
        #                 "table": _tbl_ec,
        #                 "visualization": _viz_ec.get("visualization"),
        #             }
        # except Exception as _e:
        #     _dbg("WARN: planner EARLY sites-per-country chart failed: %s", _e)

        # REMOVED: HLA % per country chart — migrated to Claude agentic loop
        pass

        # INTENCIÓN DIRECTA: Member institutions → route to tool_members_search
        try:
            # Core member institution keywords
            _mem_inst_kw = bool(re.search(
                r"\bmember\s+institution[s]?\b|\bmember\s+universit|\binstitution[s]?\s+(?:in|from|of)\b"
                r"|\bnetwork\s+member[s]?\b|\binnodia\s+member[s]?\b",
                s, re.I
            ))
            # Role context: validated roles or specific lab/org types
            _mem_role_ctx = bool(re.search(
                r"\bdxlab[s]?\b|\bdiagnostic\s+lab[s]?\b|\bpatient\s+org(?:anization)?s?\b"
                r"|\bresearch.*lab[s]?\b|\bmechanistic.*lab[s]?\b|\blab.?validat\b"
                r"|\bcs\b.*\bvalidat|\bvalidat.*\bcs\b|\bcts\b.*\bvalidat|\bvalidat.*\bcts\b"
                r"|\brole[s]?\b", s, re.I))
            # "institutions" or "members" combined with role context → member query
            _mem_inst_intent = _mem_inst_kw or bool(
                _mem_role_ctx and re.search(r"\binstitution[s]?\b|\bmember[s]?\b", s, re.I)
                # Only block on "clinical site(s)" when it's the subject, not a role label
                and not re.search(r"\bcts\s+site[s]?\b", s, re.I)
                and not (re.search(r"\bclinical\s+site[s]?\b", s, re.I)
                         and not re.search(r"\bclinical\s+site\s+role\b|\bcs\s+role\b", s, re.I))
            )

            # MB6: "how many clinical subaccounts/sites does the [institution] have?"
            _mb6_m = re.search(
                r"\bhow\s+many\b.{0,60}?\b(?:subaccount[s]?|clinical\s+unit[s]?|site[s]?|center[s]?)\b"
                r".{0,60}?\b(?:does?\s+(?:the\s+)?|the\s+)?([A-Za-zÀ-ÿ][A-Za-z\s'\-]{2,30}?)\s+"
                r"(?:institution|member|university|hospital)\b",
                s, re.I)
            if _mb6_m:
                _mb6_name = _mb6_m.group(1).strip()
                _mb6_all = tool_members_search(request, filters={"logic": "AND", "rules": []})
                _mb6_inst = next(
                    (r for r in (_mb6_all.get("rows") or [])
                     if _mb6_name.lower() in str(r.get("account_name", "")).lower()),
                    None
                )
                if _mb6_inst:
                    _mb6_n = _mb6_inst.get("# Sites", 0)
                    _mb6_full = _mb6_inst.get("account_name", _mb6_name)
                    return {
                        "answer": f"<p>The <strong>{_mb6_full}</strong> institution has <strong>{_mb6_n}</strong> clinical subaccount(s).</p>",
                        "table": _normalize_table_for_ui({
                            "columns": [
                                {"key": "account_name", "label": "Institution"},
                                {"key": "# Sites", "label": "# Clinical Subaccounts"},
                            ],
                            "rows": [{"account_name": _mb6_full, "# Sites": _mb6_n}],
                        }),
                    }

            # MB7: "which institution does the site in [city] belong to?"
            _mb7_m = re.search(
                r"\bwhich\b.{0,50}?\binstitution\b.{0,50}?\bsite\s+in\s+([A-Za-zÀ-ÿ][A-Za-z\s'\-]{2,25}?)\b"
                r"(?:\s+belong|\?|$)"
                r"|\bsite\s+in\s+([A-Za-zÀ-ÿ][A-Za-z\s'\-]{2,25}?)\b.{0,50}?\bbelong.{0,15}?\binstitution\b",
                s, re.I)
            if _mb7_m and sf:
                _mb7_city = ((_mb7_m.group(1) or _mb7_m.group(2)) or "").strip()
                _mb7_city_safe = re.sub(r"['\"\\\n]", "", _mb7_city)  # sanitize for SOQL
                try:
                    _mb7_soql = (
                        f"SELECT Id, Name, ShippingCity, C_Member__c, C_Member__r.Name "
                        f"FROM Account "
                        f"WHERE RecordType.DeveloperName = 'SubAccount' "
                        f"AND (ShippingCity LIKE '%{_mb7_city_safe}%' OR Name LIKE '%{_mb7_city_safe}%') "
                        f"LIMIT 5"
                    )
                    _mb7_recs = sf.query_all(_mb7_soql).get("records", [])
                    if _mb7_recs:
                        _mb7_site = _mb7_recs[0]
                        _mb7_parent_obj = _mb7_site.get("C_Member__r") or {}
                        _mb7_parent_name = _mb7_parent_obj.get("Name", "Unknown")
                        _mb7_parent_id = _mb7_site.get("C_Member__c")
                        _mb7_all_mems = tool_members_search(request, filters={"logic": "AND", "rules": []})
                        _mb7_inst_row = next(
                            (r for r in (_mb7_all_mems.get("rows") or [])
                             if r.get("account_id") == _mb7_parent_id),
                            {"account_name": _mb7_parent_name, "country": "", "city": "", "# Sites": ""}
                        )
                        _mb7_site_name = _mb7_site.get("Name", f"site in {_mb7_city}")
                        return {
                            "answer": (
                                f"<p>The site <strong>{_mb7_site_name}</strong> in {_mb7_city} belongs to the "
                                f"INNODIA member institution <strong>{_mb7_parent_name}</strong>.</p>"
                            ),
                            "table": _normalize_table_for_ui({
                                "columns": [
                                    {"key": "account_name", "label": "Institution"},
                                    {"key": "country",      "label": "Country"},
                                    {"key": "city",         "label": "City"},
                                    {"key": "# Sites",      "label": "# Clinical Units"},
                                ],
                                "rows": [_mb7_inst_row],
                            }),
                        }
                    else:
                        return {"answer": f"<p>No INNODIA site found in <strong>{_mb7_city}</strong>.</p>", "table": {}}
                except Exception as _mb7_e:
                    _dbg("WARN: MB7 site-to-institution lookup failed: %s", _mb7_e)

            # ST8: "CTS sites that are also DxLab / Diagnostic Labs"
            # C_Deliver_Clinical_Grade_Services__c is on the RT_Member Account (parent institution),
            # not on the SubAccount — must traverse C_Member__r relationship in SOQL.
            _st8_cts_dxlab = bool(
                re.search(r"\bcts\s+site[s]?\b|\bclinical\s+trial\s+site[s]?\b", s, re.I)
                and re.search(r"\bdxlab[s]?\b|\bdiagnostic\s+lab[s]?\b|\blab.validat", s, re.I)
            )
            if _st8_cts_dxlab and sf:
                try:
                    _st8_soql = (
                        "SELECT Id, Name, ShippingCity, ShippingCountry, C_Member__r.Name "
                        "FROM Account "
                        "WHERE RecordType.DeveloperName = 'SubAccount' "
                        "AND INNODIA_Clinical_Trial_Site__c = true "
                        "AND C_Member__r.C_Deliver_Clinical_Grade_Services__c = true "
                        "AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
                        "LIMIT 100"
                    )
                    _st8_raw = tool_salesforce_query(sf, _st8_soql)
                    _st8_recs = _st8_raw.get("records") or []
                    _st8_rows = [
                        {
                            "account_name": r.get("Name", ""),
                            "city":         r.get("ShippingCity") or "",
                            "country":      r.get("ShippingCountry") or "",
                            "member":       (r.get("C_Member__r") or {}).get("Name") or "",
                        }
                        for r in _st8_recs
                    ]
                    return {
                        "answer": (
                            f"<p>Found <strong>{len(_st8_rows)}</strong> CTS site(s) whose parent "
                            f"institution has the Diagnostic Lab (DxLab) role.</p>"
                        ),
                        "table": _normalize_table_for_ui({
                            "columns": [
                                {"key": "account_name", "label": "Site"},
                                {"key": "city",         "label": "City"},
                                {"key": "country",      "label": "Country"},
                                {"key": "member",       "label": "Member Institution"},
                            ],
                            "rows": _st8_rows,
                        }),
                    }
                except Exception as _st8_e:
                    _dbg("WARN: ST8 CTS+DxLab handler failed: %s", _st8_e)

            # QI02: "sites that can run mechanistic studies (LAB-validated)"
            # C_Perform_Cutting_Edge__c is on the RT_Member (parent institution).
            # Query SubAccounts whose parent has this role, same pattern as ST8.
            _qi02_lab_sites = bool(
                re.search(r"\bsite[s]?\b|\bcenter[s]?\b", s, re.I)
                and re.search(r"\bmechanistic\b|\blab.?validat\b|\bresearch.*lab\b", s, re.I)
                and not re.search(r"\binstitution[s]?\b|\bmember[s]?\b", s, re.I)
            )
            if _qi02_lab_sites and sf:
                try:
                    _qi02_soql = (
                        "SELECT Id, Name, ShippingCity, ShippingCountry, C_Member__r.Name "
                        "FROM Account "
                        "WHERE RecordType.DeveloperName = 'SubAccount' "
                        "AND C_Type__c = 'Clinical' "
                        "AND C_Member__r.C_Perform_Cutting_Edge__c = true "
                        "AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
                        "AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) "
                        "ORDER BY ShippingCountry, Name "
                        "LIMIT 200"
                    )
                    _qi02_raw = tool_salesforce_query(sf, _qi02_soql)
                    _qi02_recs = _qi02_raw.get("records") or []
                    _qi02_rows = [
                        {
                            "account_name": r.get("Name", ""),
                            "city":         r.get("ShippingCity") or "",
                            "country":      r.get("ShippingCountry") or "",
                            "member":       (r.get("C_Member__r") or {}).get("Name") or "",
                        }
                        for r in _qi02_recs
                    ]
                    _dbg("Planner QI02 LAB sites: %d sites", len(_qi02_rows))
                    return {
                        "answer": (
                            f"<p>Found <strong>{len(_qi02_rows)}</strong> site(s) whose parent "
                            f"institution has the Research/Mechanistic Lab (LAB) role.</p>"
                        ),
                        "table": _normalize_table_for_ui({
                            "columns": [
                                {"key": "account_name", "label": "Site"},
                                {"key": "city",         "label": "City"},
                                {"key": "country",      "label": "Country"},
                                {"key": "member",       "label": "Member Institution"},
                            ],
                            "rows": _qi02_rows,
                        }),
                    }
                except Exception as _qi02_e:
                    _dbg("WARN: QI02 LAB sites handler failed: %s", _qi02_e)

            if _mem_inst_intent:
                from app.utils.country_norms import resolve_countries as _rc_mem, iso2_to_sf_name as _iso2sf_mem
                _mem_iso2s = _rc_mem(user_text)  # pass original text so uppercase ISO2 codes (e.g. "UK") are matched
                _mem_rules = [
                    {"field": "site.country", "operator": "equals", "value": iso2}
                    for iso2 in _mem_iso2s
                ]
                # Role-based filter rules.
                # Validated fields (Clinical_Site_CS_validated__c, etc.) are mostly empty in the SF org.
                # Use proposed role fields as the primary signal, validated as secondary for explicit "validated" queries.
                _mem_role_rules: list = []
                _wants_validated = bool(re.search(r"\bvalidat", s, re.I))
                if re.search(r"\bcs\b", s, re.I) and re.search(r"\bcts\b|\bclinical\s+trial", s, re.I):
                    # MB2: "both CS and CTS validated" — use validated fields (these do have some data)
                    _mem_role_rules.append({"field": "sf.Clinical_Site_CS_validated__c", "operator": "equals", "value": True})
                    _mem_role_rules.append({"field": "sf.Clinical_Trial_Site_CTS_validated__c", "operator": "equals", "value": True})
                elif re.search(r"\bcs\b", s, re.I) and _wants_validated:
                    _mem_role_rules.append({"field": "sf.Clinical_Site_CS_validated__c", "operator": "equals", "value": True})
                elif re.search(r"\bcts\b", s, re.I) and _wants_validated:
                    _mem_role_rules.append({"field": "sf.Clinical_Trial_Site_CTS_validated__c", "operator": "equals", "value": True})
                if re.search(r"\bdxlab[s]?\b|\bdiagnostic\s+lab[s]?\b", s, re.I):
                    # Use proposed field (validated is empty): C_Deliver_Clinical_Grade_Services__c = DxLab role
                    _mem_role_rules.append({"field": "sf.C_Deliver_Clinical_Grade_Services__c", "operator": "equals", "value": True})
                if re.search(r"\bresearch.*lab\b|\bmechanistic.*lab\b|\blab.?validat\b|\blab\s+member\b|\blab\s+role\b", s, re.I):
                    # Use proposed field: C_Perform_Cutting_Edge__c = Research/Mechanistic Lab role
                    _mem_role_rules.append({"field": "sf.C_Perform_Cutting_Edge__c", "operator": "equals", "value": True})
                if re.search(r"\bpatient\s+org(?:anization)?s?\b", s, re.I):
                    # Use proposed field: C_Contribute_as_a_Patient_Organization__c = Patient Org role
                    _mem_role_rules.append({"field": "sf.C_Contribute_as_a_Patient_Organization__c", "operator": "equals", "value": True})
                # OC3: "proposed Clinical Site role (but not validated yet)"
                _oc3_proposed_cs = bool(re.search(r"\bclinical\s+site\s+role\b|\bcs\s+role\b|\bproposed\s+(?:cs|clinical\s+site)\b", s, re.I))
                if _oc3_proposed_cs:
                    _mem_role_rules.append({"field": "sf.Clinical_Site_CS__c", "operator": "equals", "value": True})
                    # "not validated yet" / "but not validated" → exclude validated
                    _oc3_not_validated = bool(re.search(r"\bnot\s+(?:yet\s+)?validat|\bnot\s+validat|\bwithout\s+validat", s, re.I))
                    if _oc3_not_validated:
                        _mem_role_rules.append({"field": "sf.Clinical_Site_CS_validated__c", "operator": "not_equals", "value": True})
                _all_mem_rules = _mem_rules + _mem_role_rules
                _mem_filters = {"logic": "AND", "rules": _all_mem_rules} if _all_mem_rules else {"logic": "AND", "rules": []}
                _mem_result = tool_members_search(request, filters=_mem_filters, include_detail=False)
                _mem_rows = (_mem_result.get("rows") or []) if isinstance(_mem_result, dict) else []
                # City filter: "at the Edinburgh institution" / "the Paris member institution"
                _mem_city_m = re.search(
                    r"\bthe\s+([A-Z][A-Za-zÀ-ÿ'\-]{2,25})\s+(?:member|institution)\b"
                    r"|\bat\s+(?:the\s+)?([A-Z][A-Za-zÀ-ÿ'\-]{2,25})\s+(?:member|institution)\b",
                    user_text)
                if _mem_city_m:
                    _mem_city = (_mem_city_m.group(1) or _mem_city_m.group(2) or "").strip()
                    if _mem_city and not _rc_mem(_mem_city):  # not a country name
                        _mem_rows = [r for r in _mem_rows if _mem_city.lower() in str(r.get("account_name") or r.get("city") or "").lower()]
                        _mem_result = dict(_mem_result, rows=_mem_rows) if isinstance(_mem_result, dict) else {"rows": _mem_rows, "columns": []}
                # MB8: "clinical units/subaccounts linked to the [institution]" → fetch detail endpoint
                _mb8_wants_units = bool(
                    re.search(r"\bclinical\s+unit[s]?\b|\bsubaccount[s]?\b", s, re.I)
                    and re.search(r"\blinked\s+to\b|\bbelonging\s+to\b|\bof\s+the\b|\bunder\b", s, re.I)
                )
                if _mb8_wants_units and len(_mem_rows) >= 1:
                    _mb8_inst_row = _mem_rows[0]
                    _mb8_inst_id = _mb8_inst_row.get("account_id", "")
                    if re.fullmatch(r"[A-Za-z0-9]{15,18}", _mb8_inst_id):
                        _mb8_headers: dict = {}
                        _mb8_ck = request.headers.get("cookie")
                        if _mb8_ck:
                            _mb8_headers["cookie"] = _mb8_ck
                        with httpx.Client(timeout=30.0) as _mb8_cli:
                            _mb8_resp = _mb8_cli.get(
                                f"http://127.0.0.1:8000/api/members/{_mb8_inst_id}/detail",
                                headers=_mb8_headers,
                            )
                        if _mb8_resp.status_code == 200:
                            _mb8_detail = _mb8_resp.json()
                            _mb8_subs = _mb8_detail.get("subaccounts") or []
                            _mb8_sub_rows = [
                                {
                                    "account_name": sub.get("name", ""),
                                    "country":      sub.get("country", ""),
                                    "city":         sub.get("city", ""),
                                    "CTS":          "Yes" if sub.get("is_cts") else "No",
                                    "CS":           "Yes" if sub.get("is_cs") else "No",
                                    "Status":       sub.get("status") or "",
                                }
                                for sub in _mb8_subs
                            ]
                            _mb8_inst_name = _mb8_inst_row.get("account_name", "")
                            return {
                                "answer": (
                                    f"<p>Found <strong>{len(_mb8_subs)}</strong> clinical unit(s) linked to the "
                                    f"<strong>{_mb8_inst_name}</strong> member institution.</p>"
                                ),
                                "table": _normalize_table_for_ui({
                                    "columns": [
                                        {"key": "account_name", "label": "Clinical Unit"},
                                        {"key": "country",      "label": "Country"},
                                        {"key": "city",         "label": "City"},
                                        {"key": "CTS",          "label": "CTS"},
                                        {"key": "CS",           "label": "CS"},
                                        {"key": "Status",       "label": "Status"},
                                    ],
                                    "rows": _mb8_sub_rows,
                                }),
                            }

                # OC1/OC2: "contacts at the [institution]" → fetch member_contacts from detail endpoint
                _mem_wants_contacts = bool(
                    re.search(r"\bcontact[s]?\b|\bkey\s+contact[s]?\b", s, re.I)
                    and re.search(r"\bat\s+(?:the\s+)?[A-Z]|\bfor\s+(?:the\s+)?[A-Z]|\bat\s+the\b", user_text)
                    and not re.search(r"\bsubaccount[s]?\b|\bclinical\s+unit[s]?\b", s, re.I)
                )
                if _mem_wants_contacts and len(_mem_rows) >= 1:
                    _oc_inst = _mem_rows[0]
                    _oc_inst_id = _oc_inst.get("account_id", "")
                    if re.fullmatch(r"[A-Za-z0-9]{15,18}", _oc_inst_id):
                        _oc_hdrs: dict = {}
                        _oc_ck = request.headers.get("cookie")
                        if _oc_ck:
                            _oc_hdrs["cookie"] = _oc_ck
                        with httpx.Client(timeout=30.0) as _oc_cli:
                            _oc_resp = _oc_cli.get(
                                f"http://127.0.0.1:8000/api/members/{_oc_inst_id}/detail",
                                headers=_oc_hdrs,
                            )
                        if _oc_resp.status_code == 200:
                            _oc_detail = _oc_resp.json()
                            _oc_contacts = _oc_detail.get("member_contacts") or []
                            _oc_contact_rows = [
                                {
                                    "name":       c.get("name", ""),
                                    "title":      c.get("title") or "",
                                    "department": c.get("department") or "",
                                    "email":      c.get("email") or "",
                                    "phone":      c.get("phone") or "",
                                    "board_member": "Yes" if c.get("is_board_member") else "",
                                    "country_lead": "Yes" if c.get("is_country_lead") else "",
                                }
                                for c in _oc_contacts
                            ]
                            _oc_inst_name = _oc_inst.get("account_name", "")
                            return {
                                "answer": (
                                    f"<p>Found <strong>{len(_oc_contacts)}</strong> contact(s) at "
                                    f"<strong>{_oc_inst_name}</strong>.</p>"
                                ),
                                "table": _normalize_table_for_ui({
                                    "columns": [
                                        {"key": "name",        "label": "Name"},
                                        {"key": "title",       "label": "Title"},
                                        {"key": "department",  "label": "Department"},
                                        {"key": "email",       "label": "Email"},
                                        {"key": "phone",       "label": "Phone"},
                                        {"key": "board_member","label": "Board Member"},
                                        {"key": "country_lead","label": "Country Lead"},
                                    ],
                                    "rows": _oc_contact_rows,
                                }),
                            }

                # OC02 no-match: contacts requested but institution not found → helpful message
                if _mem_wants_contacts and len(_mem_rows) == 0 and _mem_city_m:
                    _oc_search_term = (_mem_city_m.group(1) or _mem_city_m.group(2) or "").strip()
                    # Find close matches from all members for suggestion
                    _all_mem = tool_members_search(request, filters={"logic": "AND", "rules": []}, include_detail=False)
                    _all_mem_rows = (_all_mem.get("rows") or []) if isinstance(_all_mem, dict) else []
                    # Suggest institutions matching by city or by name substring (min 4 chars)
                    _oc_term_l = _oc_search_term.lower()
                    _suggestions = [
                        r.get("account_name", "")
                        for r in _all_mem_rows
                        if _oc_term_l in str(r.get("city", "")).lower()
                        or (len(_oc_term_l) >= 4 and _oc_term_l in str(r.get("account_name", "")).lower())
                    ][:5]
                    _sug_txt = ""
                    if _suggestions:
                        _sug_txt = " Did you mean: " + ", ".join(f"<em>{s}</em>" for s in _suggestions) + "?"
                    return {
                        "answer": (
                            f"<p>No INNODIA member institution found matching <strong>{_oc_search_term}</strong>.{_sug_txt}</p>"
                        ),
                    }

                # H28/GQ20: "multiple subaccounts" / "3 or more subaccounts" → filter by threshold
                _mem_sub_thresh_m = re.search(
                    r"\b(\d+)\s+or\s+more\b|\bat\s+least\s+(\d+)\b",
                    s, re.I
                )
                _mem_multi_sub = bool(
                    (re.search(r"\bmultiple\b|\bmore\s+than\s+one\b|\bseveral\b|\b2\+\b", s) or _mem_sub_thresh_m)
                    and re.search(r"\bsubaccount[s]?\b|\bclinical\s+unit[s]?\b|\bclinical\s+site[s]?\b|\bcenter[s]?\b", s)
                )
                if _mem_multi_sub:
                    _min_sites = int(_mem_sub_thresh_m.group(1) or _mem_sub_thresh_m.group(2)) if _mem_sub_thresh_m else 2
                    _mem_rows_filtered = [r for r in _mem_rows if isinstance(r, dict) and int(r.get("# Sites") or 0) >= _min_sites]
                    _mem_result_filtered = {
                        "columns": _mem_result.get("columns", []) if isinstance(_mem_result, dict) else [],
                        "rows": _mem_rows_filtered,
                    }
                    _mem_ans = (
                        f"<p>Found <strong>{len(_mem_rows_filtered)}</strong> INNODIA member institution(s) "
                        f"with {_min_sites} or more clinical subaccounts.</p>"
                    )
                    return {"answer": _mem_ans, "table": _normalize_table_for_ui(_mem_result_filtered)}
                _country_str = ", ".join(_iso2sf_mem(c) for c in _mem_iso2s) if _mem_iso2s else "all countries"
                _role_desc = []
                if _mem_role_rules:
                    _role_map = {
                        "sf.Clinical_Site_CS__c": "proposed Clinical Site (CS)",
                        "sf.Clinical_Site_CS_validated__c": "CS-validated",
                        "sf.Clinical_Trial_Site_CTS_validated__c": "CTS-validated",
                        "sf.C_Deliver_Clinical_Grade_Services__c": "DxLab (Diagnostic Lab)",
                        "sf.C_Perform_Cutting_Edge__c": "Research/Mechanistic Lab",
                        "sf.C_Contribute_as_a_Patient_Organization__c": "Patient Organization",
                        "sf.Diagnostic_Lab_DxLab_validated__c": "DxLab-validated",
                        "sf.Research_Mechanistic_Lab_LAB_validated__c": "LAB-validated",
                        "sf.Patient_Organization_validated__c": "Patient-Org-validated",
                    }
                    _role_desc = []
                    for r in _mem_role_rules:
                        _lbl = _role_map.get(r["field"], r["field"])
                        if r.get("operator") in ("not_equals", "!="):
                            _lbl = f"not {_lbl}"
                        _role_desc.append(_lbl)
                _role_str = " and ".join(_role_desc) if _role_desc else ""
                _mem_ans = (
                    f"<p>Found <strong>{len(_mem_rows)}</strong> INNODIA member institution(s)"
                    + (f" in <strong>{_country_str}</strong>" if _mem_iso2s else "")
                    + (f" with <strong>{_role_str}</strong> role" if _role_str else "")
                    + ".</p>"
                )
                return {"answer": _mem_ans, "table": _normalize_table_for_ui(_mem_result) if isinstance(_mem_result, dict) else {}}
        except Exception as _e_mem:
            _dbg("WARN: member institution planner failed: %s", _e_mem)

        # INTENCIÓN DIRECTA: Study Coordinator(s) → llamar determinísticamente a study_coordinators_with_activities
        try:
            sc_intent = re.search(r"\bstudy\s*coordinators?\b", s) is not None
            pi_intent = re.search(r"\bprincipal\s*investigators?\b|\b\bpi\b|\binvestigators?\b", s) is not None
            # G7/PC05: generic "contacts" at sites in [country] — route to combined SC+PI
            _contact_generic = bool(
                re.search(r"\bcontact[s]?\b", s, re.I)
                and not re.search(r"\bfirst\s+contact\b", s, re.I)  # exclude pipeline "first contact"
                and re.search(r"\bsite[s]?\b", s, re.I)  # must mention sites (not institutions — those are OC1/OC2)
            )
            # "sites without / don't have coordinator" — detect sites lacking coordinators (P04)
            _sc_no_coord = bool(re.search(
                r"\bwithout\b|\bdon.?t\s+have\b|\bno\s+(?:dedicated\s+)?(?:study\s+)?coordinator"
                r"|\bno\s+(?:dedicated\s+)?(?:study\s+)?sc\b|\bnot\s+listed\b|\blacking\b", s, re.I)
                and (sc_intent or re.search(r"\bcoordinator[s]?\b", s)))
            if (sc_intent or pi_intent or _contact_generic) and not sf:
                return {"answer": "<p>⚠️ Contact/coordinator data requires an active Salesforce session. "
                                  "Please log in via the <strong>Salesforce</strong> menu to refresh your session.</p>"}
            if (sc_intent or pi_intent or _contact_generic) and sf:
                # Use resolve_countries() for robust country extraction (avoids matching "in" inside "investigators")
                from app.utils.country_norms import resolve_countries as _resolve_countries_sc
                _sc_iso2s = _resolve_countries_sc(user_text)  # pass original text for uppercase ISO2 matching
                countries = [_iso2_to_sf(c) for c in _sc_iso2s] if _sc_iso2s else []
                # Fallback: carry country context from last table if none provided
                try:
                    ltp = payload.last_table if hasattr(payload, 'last_table') else None
                    if (not countries) and ltp and isinstance(ltp.get("rows"), list):
                        vals = sorted({str(r.get("country") or "").strip() for r in ltp.get("rows") if isinstance(r, dict) and r.get("country")})
                        if 0 < len(vals) <= 3:
                            countries = vals
                except Exception:
                    pass
                # Extract city filter — "PI at the site in Barcelona" → city = "Barcelona"
                _sc_city: Optional[str] = None
                _city_m = re.search(r"\bin\s+(?:the\s+(?:site\s+(?:in|of)\s+)?)?([A-Z][A-Za-zÀ-ÿ'\-]{2,25})(?:\s*[,?]|\s*$)", user_text)
                if _city_m:
                    _cand = _city_m.group(1).strip()
                    # Don't treat a country name as a city
                    if not _resolve_countries_sc(_cand):
                        _sc_city = _cand
                if _contact_generic or (sc_intent and pi_intent):
                    # Generic "contacts" or both SC+PI: merge SC + PI contact results
                    _tbl_sc = tool_study_coordinators_with_activities(
                        sf, account_ids=[], countries=countries,
                        title_contains="Study Coordinator", roles=[], include_subaccounts=True,
                    )
                    _tbl_pi = tool_study_coordinators_with_activities(
                        sf, account_ids=[], countries=countries,
                        title_contains="Investigator", roles=[], include_subaccounts=True,
                    )
                    _all_rows = (_tbl_sc.get("rows") or []) + (_tbl_pi.get("rows") or [])
                    _seen_ckeys: set = set()
                    _merged: list = []
                    for _cr in _all_rows:
                        _ck = (_cr.get("account_id"), _cr.get("contact_id"))
                        if _ck not in _seen_ckeys:
                            _seen_ckeys.add(_ck)
                            _merged.append(_cr)
                    table = dict(_tbl_sc, rows=_merged)
                elif sc_intent:
                    table = tool_study_coordinators_with_activities(
                        sf,
                        account_ids=[],
                        countries=countries,
                        title_contains="Study Coordinator",
                        roles=[],
                        include_subaccounts=True,
                    )
                else:
                    # PI intent: use coordinator tool with broad "Investigator" title match
                    table = tool_study_coordinators_with_activities(
                        sf,
                        account_ids=[],
                        countries=countries,
                        title_contains="Investigator",
                        roles=[],
                        include_subaccounts=True,
                    )
                rows = table.get("rows") or []
                # Apply city filter if detected (P03: "PI at the site in Barcelona")
                if _sc_city:
                    rows = [r for r in rows if _sc_city.lower() in str(r.get("city") or "").lower()]
                    table = dict(table, rows=rows)
                # P04: "sites without coordinator" → invert: find sites NOT in coordinator list
                if _sc_no_coord and sf:
                    coord_account_ids = {str(r.get("account_id")) for r in rows if isinstance(r, dict) and r.get("account_id")}
                    # Get all CTS clinical accounts
                    _soql_all_cts = (
                        "SELECT Account.Id, Account.Name, Account.ShippingCountry, Account.ShippingCity "
                        "FROM Opportunity "
                        "WHERE Account.RecordType.DeveloperName='SubAccount' AND Account.C_Type__c='Clinical' "
                        "AND (Account.Account_Inactive__c = false OR Account.Account_Inactive__c = null) "
                        "AND (Account.Subaccount_Inactive__c = false OR Account.Subaccount_Inactive__c = null) "
                        "LIMIT 2000"
                    )
                    _raw_all = tool_salesforce_query(sf, _soql_all_cts)
                    _recs_all = (_raw_all.get("records") or []) if isinstance(_raw_all, dict) else []
                    rows_no_coord = []
                    for _r in _recs_all:
                        _acc = _r.get("Account") or {}
                        _aid = _acc.get("Id", "")
                        if _aid and _aid not in coord_account_ids:
                            rows_no_coord.append({
                                "account_id": _aid,
                                "account_name": _acc.get("Name", ""),
                                "country": _acc.get("ShippingCountry", ""),
                                "city": _acc.get("ShippingCity", ""),
                            })
                    _cols_nc = [
                        {"key": "account_name", "label": "Site"},
                        {"key": "country", "label": "Country"},
                        {"key": "city", "label": "City"},
                    ]
                    _tbl_nc = _normalize_table_for_ui({"columns": _cols_nc, "rows": rows_no_coord})
                    _dbg("Planner P04: %d sites without coordinator", len(rows_no_coord))
                    return {"answer": f"<p>Found <strong>{len(rows_no_coord)}</strong> site(s) with no coordinator listed.</p>",
                            "table": _tbl_nc}
                accs = len({str(r.get("account_id")) for r in rows if isinstance(r, dict) and r.get("account_id")})
                if _contact_generic:
                    ans = f"Found {len(rows)} contact(s) (Study Coordinators + Principal Investigators) across {accs} site(s)."
                elif _sc_city:
                    ans = f"Found {len(rows)} coordinator(s) in {_sc_city}."
                else:
                    ans = f"Found {len(rows)} coordinator(s) across {accs} account(s)."
                return {"answer": f"<p>{ans}</p>", "table": table}
        except Exception as _e:
            _dbg("WARN: planner study_coordinators failed: %s", _e)

        # ── Percentage of sites in activity/assignment ──────────
        try:
            _pct_intent = bool(re.search(r"\bpercent(?:age)?\b|\b%\b|\bratio\b|\bproportion\b", s, re.I))
            _pct_activity = bool(re.search(r"\bactivit|\bassignment|\bparticipat", s, re.I))
            if _pct_intent and _pct_activity and sf:
                # Total active clinical sites
                _pct_total_soql = (
                    "SELECT COUNT(Id) FROM Account "
                    "WHERE RecordType.DeveloperName='SubAccount' "
                    "AND C_Type__c='Clinical' "
                    "AND (Account_Inactive__c=false OR Account_Inactive__c=null) "
                    "AND (Subaccount_Inactive__c=false OR Subaccount_Inactive__c=null)"
                )
                _pct_total_raw = tool_salesforce_query(sf, _pct_total_soql)
                _pct_total_recs = _pct_total_raw.get("records", []) if isinstance(_pct_total_raw, dict) else []
                _pct_total = int(_pct_total_recs[0].get("expr0", 0)) if _pct_total_recs else 0
                # Sites with at least 1 assignment (SOQL doesn't support COUNT(DISTINCT), use GROUP BY)
                _pct_active_soql = (
                    "SELECT C_Account__c FROM Assignment__c GROUP BY C_Account__c"
                )
                _pct_active_raw = sf.query_all(_pct_active_soql)
                _pct_active_recs = _pct_active_raw.get("records", [])
                _pct_active = len(_pct_active_recs)
                if _pct_total > 0:
                    _pct_val = round(100.0 * _pct_active / _pct_total, 1)
                    _pct_rows = [
                        {"metric": "Sites with at least 1 activity", "count": _pct_active},
                        {"metric": "Total clinical sites", "count": _pct_total},
                        {"metric": "Percentage", "count": f"{_pct_val}%"},
                    ]
                    _pct_tbl = _normalize_table_for_ui({
                        "columns": [{"key": "metric", "label": "Metric"}, {"key": "count", "label": "Value"}],
                        "rows": _pct_rows,
                    })
                    _dbg("Planner percentage: %d/%d = %.1f%%", _pct_active, _pct_total, _pct_val)
                    return {
                        "answer": (
                            f"<p><strong>{_pct_val}%</strong> of clinical sites "
                            f"({_pct_active} of {_pct_total}) are participating in at least one activity.</p>"
                        ),
                        "table": _pct_tbl,
                    }
        except Exception as _e:
            _dbg("WARN: planner percentage failed: %s", _e)

        # ── Activity / Assignment handlers (extracted to moby_handlers.py) ──────────
        try:
            _act_ctx = _HandlerContext(
                query=user_text, s=s, sf=sf,
                last_table=last_table_from_payload,
                last_filters=last_filters, has_table=has_table,
                is_followup_prefix=_is_followup_prefix,
            )
            _act_tools = _ActivityTools(
                tool_sites_by_activity=tool_sites_by_activity,
                tool_sites_with_any_activity=tool_sites_with_any_activity,
                tool_list_all_activities=tool_list_all_activities,
                tool_activity_country_matrix=tool_activity_country_matrix,
                tool_activity_counts_by_country=tool_activity_counts_by_country,
                tool_activities_with_countries=tool_activities_with_countries,
                tool_activities_with_assignments_counts=tool_activities_with_assignments_counts,
                tool_activity_assignments_detailed=tool_activity_assignments_detailed,
                tool_render_chart=tool_render_chart,
                pretty_label=_pretty_label,
                dbg=_dbg,
            )
            _r = handle_followup_activity_sponsor(_act_ctx, _act_tools)
            if _r is not None:
                return _r
            _r = handle_multi_activity_intersection(_act_ctx, _act_tools)
            if _r is not None:
                return _r
            _r = handle_assignment_sites(_act_ctx, _act_tools)
            if _r is not None:
                return _r
            _r = handle_activity(_act_ctx, _act_tools)
            if _r is not None:
                return _r
        except Exception as _e:
            _dbg("WARN: planner activities failed: %s", _e)

        # INTENCIÓN DIRECTA: Newly Diagnosed (ND) ranking or aggregation → direct SF query
        try:
            _nd_intent = bool(re.search(r"\b(newly\s+diagnosed|new\s+diagnos|nd\b|newly\s*dx|newly\s*t1d)\b", s))
            _nd_by_country = bool(re.search(
                r"\b(per\s+country|by\s+country|por\s+pa[ií]s|por\s+pais|by\s+region"
                r"|which\s+country|country\s+has|country\s+with\s+the\s+most|most.*country|country.*most)\b", s))
            _nd_top = bool(re.search(r"\b(top|highest|most|mayor|más|mas|ranking|rank)\b", s))
            _nd_under18 = bool(re.search(r"\bunder\s*18\b|<\s*18|\bu18\b|\bni[ñn]os?|\bpediatr", s, re.I))
            _nd_thresh_m = re.search(
                r"\bmore\s+than\s+(\d+)\b|\bover\s+(\d+)\b|\bgreater\s+than\s+(\d+)\b"
                r"|\bat\s+least\s+(\d+)\b|>\s*(\d+)\b|\b(\d+)\s+or\s+more\b",
                s, re.I)
            _nd_threshold = int(next(g for g in _nd_thresh_m.groups() if g is not None)) if _nd_thresh_m else None
            if _nd_intent and not sf:
                return {"answer": "<p>⚠️ Newly Diagnosed T1D data is stored in Salesforce and requires an active session. "
                                  "Please log in via the <strong>Salesforce</strong> menu to refresh your session.</p>"}
            if _nd_intent and sf and (_nd_top or _nd_by_country or _nd_threshold is not None):
                # For threshold queries ("more than N"), fetch all matching sites
                # For "top N" queries, limit to N
                if _nd_threshold is not None and not _nd_top:
                    _nd_topn = 2000
                else:
                    _m_topn = re.search(r"\b(top\s+)?(\d+)\b", s)
                    _nd_topn = int(_m_topn.group(2)) if _m_topn else 10
                    _nd_topn = min(max(_nd_topn, 1), 50)
                if _nd_by_country:
                    # Fetch all sites with ND data, then aggregate in Python (GROUP BY SOQL has SF traversal issues)
                    soql_nd_agg = (
                        "SELECT Account.ShippingCountry, "
                        "C_Number_of_new_T1D_diagnosed_O_18__c, C_Number_of_new_T1D_diagnosed_U_18__c "
                        "FROM Opportunity "
                        "WHERE Account.RecordType.DeveloperName='SubAccount' "
                        "AND Account.C_Type__c='Clinical' "
                        "LIMIT 2000"
                    )
                    raw_nd_agg = tool_salesforce_query(sf, soql_nd_agg)
                    recs_nd_agg = raw_nd_agg.get("records", []) if isinstance(raw_nd_agg, dict) else []
                    _nd_country_totals: dict = {}
                    for r in recs_nd_agg:
                        _c = (r.get("Account") or {}).get("ShippingCountry") or ""
                        if not _c:
                            continue
                        _o18 = float(r.get("C_Number_of_new_T1D_diagnosed_O_18__c") or 0)
                        _u18 = float(r.get("C_Number_of_new_T1D_diagnosed_U_18__c") or 0)
                        if _c not in _nd_country_totals:
                            _nd_country_totals[_c] = {"o18": 0.0, "u18": 0.0}
                        _nd_country_totals[_c]["o18"] += _o18
                        _nd_country_totals[_c]["u18"] += _u18
                    _nd_sort_key = "sf.C_Number_of_new_T1D_diagnosed_U_18__c" if _nd_under18 else "sf.C_Number_of_new_T1D_diagnosed_O_18__c"
                    rows_nd_agg = sorted(
                        [{"country": c,
                          "sf.C_Number_of_new_T1D_diagnosed_O_18__c": v["o18"],
                          "sf.C_Number_of_new_T1D_diagnosed_U_18__c": v["u18"]}
                         for c, v in _nd_country_totals.items()],
                        key=lambda x: x[_nd_sort_key], reverse=True
                    )
                    cols_nd_agg = [
                        {"key": "country", "label": "Country"},
                        {"key": "sf.C_Number_of_new_T1D_diagnosed_O_18__c", "label": "ND ≥18 (sum)"},
                        {"key": "sf.C_Number_of_new_T1D_diagnosed_U_18__c", "label": "ND <18 (sum)"},
                    ]
                    tbl_nd_agg = _normalize_table_for_ui({"columns": cols_nd_agg, "rows": rows_nd_agg})
                    _dbg("Planner ND by country: %d countries", len(rows_nd_agg))
                    nd_out: Dict[str, Any] = {
                        "answer": f"<p>Newly Diagnosed T1D totals across {len(rows_nd_agg)} countries.</p>",
                        "table": tbl_nd_agg,
                    }
                    if re.search(r"\b(chart|bar|pie|graph|visuali[sz]e)\b", s):
                        _nd_viz_key = "sf.C_Number_of_new_T1D_diagnosed_O_18__c"
                        _nd_viz = tool_render_chart("bar", rows_nd_agg, "country",
                                                    [_nd_viz_key, "sf.C_Number_of_new_T1D_diagnosed_U_18__c"],
                                                    {"title": "Newly Diagnosed T1D per Country"}, {})
                        nd_out["visualization"] = _nd_viz.get("visualization")
                    return nd_out
                else:
                    # Top N sites by ND ≥18 or <18 depending on query
                    _nd_sf_field = "C_Number_of_new_T1D_diagnosed_U_18__c" if _nd_under18 else "C_Number_of_new_T1D_diagnosed_O_18__c"
                    _nd_age_label = "<18" if _nd_under18 else "≥18"
                    _nd_min_val = _nd_threshold if _nd_threshold is not None else 0
                    soql_nd = (
                        "SELECT Account.Id, Account.Name, Account.ShippingCountry, Account.ShippingCity, "
                        "C_Number_of_new_T1D_diagnosed_O_18__c, C_Number_of_new_T1D_diagnosed_U_18__c "
                        "FROM Opportunity "
                        "WHERE Account.RecordType.DeveloperName='SubAccount' "
                        "AND Account.C_Type__c='Clinical' "
                        f"AND {_nd_sf_field} > {_nd_min_val} "
                        f"ORDER BY {_nd_sf_field} DESC NULLS LAST "
                        f"LIMIT {_nd_topn}"
                    )
                    raw_nd = tool_salesforce_query(sf, soql_nd)
                    recs_nd = raw_nd.get("records", []) if isinstance(raw_nd, dict) else []
                    rows_nd = []
                    seen_nd: set = set()
                    for r in recs_nd:
                        acc = r.get("Account") or {}
                        aid = acc.get("Id") or ""
                        if not aid or aid in seen_nd:
                            continue
                        seen_nd.add(aid)
                        rows_nd.append({
                            "account_id": aid,
                            "account_name": acc.get("Name", ""),
                            "country": acc.get("ShippingCountry", ""),
                            "city": acc.get("ShippingCity", ""),
                            "sf.C_Number_of_new_T1D_diagnosed_O_18__c": r.get("C_Number_of_new_T1D_diagnosed_O_18__c"),
                            "sf.C_Number_of_new_T1D_diagnosed_U_18__c": r.get("C_Number_of_new_T1D_diagnosed_U_18__c"),
                        })
                    cols_nd = [
                        {"key": "account_name", "label": "Site"},
                        {"key": "country", "label": "Country"},
                        {"key": "city", "label": "City"},
                        {"key": "sf.C_Number_of_new_T1D_diagnosed_O_18__c", "label": "ND ≥18"},
                        {"key": "sf.C_Number_of_new_T1D_diagnosed_U_18__c", "label": "ND <18"},
                    ]
                    tbl_nd = _normalize_table_for_ui({"columns": cols_nd, "rows": rows_nd})
                    _dbg("Planner ND top %d: %d sites", _nd_topn, len(rows_nd))
                    return {
                        "answer": f"<p>Top <strong>{len(rows_nd)}</strong> sites by newly diagnosed T1D patients ({_nd_age_label}).</p>",
                        "table": tbl_nd,
                    }
        except Exception as _e:
            _dbg("WARN: planner ND handler failed: %s", _e)

        # INTENCIÓN DIRECTA: T1D currently followed (threshold filter) → internal explorer_search
        try:
            # Match: (t1d|type1|diabetes) near (currently|actualmente|seguimiento|followed)
            _cf_intent = bool(re.search(
                r"(t1d|type\s*1|diabetes).{0,60}(current(ly)?|actualmente|seguimiento|followed)"
                r"|(current(ly)?|actualmente|seguimiento|followed).{0,60}(t1d|type\s*1|diabetes)",
                s, re.I))
            if _cf_intent:
                if not sf:
                    return {"answer": "<p>⚠️ T1D patient data is stored in Salesforce and requires an active session. "
                                      "Please log in via the <strong>Salesforce</strong> menu to refresh your session.</p>"}
                _cf_field_o18 = "sf.C_Number_of_T1D_Patients_currently_O_18__c"
                _cf_field_u18 = "sf.C_Number_of_T1D_Patients_currently_U_18__c"
                # Age group
                _cf_under18 = bool(re.search(r"\bunder\s*18\b|<\s*18|u\s*18|\bni[ñn]os?\b|\bped", s, re.I))
                _cf_field = _cf_field_u18 if _cf_under18 else _cf_field_o18
                # Threshold extraction
                _m_cf_thresh = re.search(r"\b(more\s+than|over|greater\s+than|m[aá]s\s+de|mayor\s+de|mayor\s+a|superior\s+a|>)\s*(\d+)", s, re.I)
                _m_cf_atleast = re.search(r"\b(at\s+least|al\s+menos|>=|≥)\s*(\d+)", s, re.I)
                _m_cf_less = re.search(r"\b(less\s+than|fewer\s+than|menos\s+de|<)\s*(\d+)", s, re.I)
                _cf_rules = []
                if _m_cf_thresh:
                    _cf_rules.append({"field": _cf_field, "operator": ">", "value": int(_m_cf_thresh.group(2))})
                    _thresh_label = f">{_m_cf_thresh.group(2)}"
                elif _m_cf_atleast:
                    _cf_rules.append({"field": _cf_field, "operator": ">=", "value": int(_m_cf_atleast.group(2))})
                    _thresh_label = f">={_m_cf_atleast.group(2)}"
                elif _m_cf_less:
                    _cf_rules.append({"field": _cf_field, "operator": "<", "value": int(_m_cf_less.group(2))})
                    _thresh_label = f"<{_m_cf_less.group(2)}"
                else:
                    _cf_rules.append({"field": _cf_field, "operator": ">", "value": 0})
                    _thresh_label = ""
                _cf_url = f"http://127.0.0.1:8000{EXPLORER_SEARCH_PATH}"
                _cf_ck = request.headers.get("cookie") if request else None
                _cf_hdrs = {"cookie": _cf_ck} if _cf_ck else {}
                _cf_cols = ["sf.Account.Id", "sf.Account.Name", "sf.Account.ShippingCountry",
                            "sf.Account.ShippingCity", _cf_field_o18, _cf_field_u18]
                with httpx.Client(timeout=60.0) as _cf_cli:
                    _cf_resp = _cf_cli.post(
                        _cf_url,
                        json={"filters": {"logic": "AND", "rules": _cf_rules}, "columns": _cf_cols},
                        headers=_cf_hdrs,
                    )
                rows_cf = _cf_resp.json().get("rows") or []
                if rows_cf:
                    rows_cf.sort(key=lambda r: float((r.get("data") or {}).get(_cf_field_o18) or 0), reverse=True)
                    cols_cf = [
                        {"key": "account_name", "label": "Site"},
                        {"key": "country", "label": "Country"},
                        {"key": "city", "label": "City"},
                        {"key": _cf_field_o18, "label": "T1D ≥18 (currently)"},
                        {"key": _cf_field_u18, "label": "T1D <18 (currently)"},
                    ]
                    tbl_cf = _normalize_table_for_ui({"columns": cols_cf, "rows": rows_cf})
                    _dbg("Planner T1D currently %s: %d sites", _thresh_label, len(rows_cf))
                    return {
                        "answer": f"<p>Found <strong>{len(rows_cf)}</strong> site(s) with T1D patients currently followed"
                                  f"{(' ' + _thresh_label) if _thresh_label else ''}.</p>",
                        "table": tbl_cf,
                    }
        except Exception as _e:
            _dbg("WARN: planner T1D currently handler failed: %s", _e)

        # INTENCIÓN DIRECTA: T1D patients ranking/top sites → list sites sorted by T1D count desc
        try:
            _t1d_rank_intent = bool(
                re.search(r"(highest|top|most|ranking|rank)\b.{0,60}(t1d|type\s*1|diabetes).{0,30}patient", s, re.I) or
                re.search(r"\b(t1d|type\s*1|diabetes).{0,30}patient.{0,60}(highest|top|most|ranking|rank)", s, re.I) or
                re.search(r"(t1d|type\s*1|diabetes).{0,30}patient.{0,30}(seen|per\s+year|yearly|annual)", s, re.I)
            )
            # Skip if qual fields are also present — let qual handler combine T1D + qual filters
            # Also skip if "newly diagnosed" is present — let ND handler handle it
            _t1d_rank_has_qual = bool(re.search(
                r"\bhla\b|\bovernight\b|\bpharmac|\bznt8\b|\bautoantibod|\bnewly.diagnos|\bnew.diagnos|\bnd\b", s, re.I))
            if _t1d_rank_intent and not _t1d_rank_has_qual:
                if not sf:
                    return {"answer": "<p>⚠️ T1D patient data requires an active Salesforce session.</p>"}
                _t1dr_field_o18 = "sf.C_Number_of_T1D_Patients_currently_O_18__c"
                _t1dr_field_u18 = "sf.C_Number_of_T1D_Patients_currently_U_18__c"
                _t1dr_url = f"http://127.0.0.1:8000{EXPLORER_SEARCH_PATH}"
                _t1dr_ck = request.headers.get("cookie") if request else None
                _t1dr_hdrs = {"cookie": _t1dr_ck} if _t1dr_ck else {}
                _t1dr_cols = ["sf.Account.Id", "sf.Account.Name", "sf.Account.ShippingCountry",
                              "sf.Account.ShippingCity", _t1dr_field_o18, _t1dr_field_u18]
                with httpx.Client(timeout=60.0) as _t1dr_cli:
                    _t1dr_resp = _t1dr_cli.post(
                        _t1dr_url,
                        json={"filters": {"logic": "OR", "rules": [
                            {"field": _t1dr_field_o18, "operator": ">", "value": 0},
                            {"field": _t1dr_field_u18, "operator": ">", "value": 0},
                        ]}, "columns": _t1dr_cols},
                        headers=_t1dr_hdrs,
                    )
                _t1dr_rows = _t1dr_resp.json().get("rows") or []
                if _t1dr_rows:
                    _t1dr_rows.sort(
                        key=lambda r: float((r.get("data") or {}).get(_t1dr_field_o18) or 0) +
                                      float((r.get("data") or {}).get(_t1dr_field_u18) or 0),
                        reverse=True,
                    )
                    _t1dr_tbl = _normalize_table_for_ui({
                        "columns": [
                            {"key": "account_name", "label": "Site"},
                            {"key": "country", "label": "Country"},
                            {"key": _t1dr_field_o18, "label": "T1D ≥18"},
                            {"key": _t1dr_field_u18, "label": "T1D <18"},
                        ],
                        "rows": _t1dr_rows,
                    })
                    _dbg("Planner T1D ranking: %d sites", len(_t1dr_rows))
                    return {
                        "answer": f"<p>Found <strong>{len(_t1dr_rows)}</strong> sites with T1D patient data, sorted by highest count.</p>",
                        "table": _t1dr_tbl,
                    }
        except Exception as _e:
            _dbg("WARN: planner T1D ranking handler failed: %s", _e)

        # INTENCIÓN DIRECTA: Stage 1/2 by country (aggregation) → like ND by country
        try:
            _bc_has_s1 = bool(re.search(r"\bstage\s*1\b", s))
            _bc_has_s2 = bool(re.search(r"\bstage\s*2\b", s))
            _bc_by_country = bool(re.search(
                r"\b(per\s+country|by\s+country|por\s+pa[ií]s|por\s+pais|by\s+region|total|totals?"
                r"|which\s+countr|country\s+has|country\s+with|most.*countr|countr.*most"
                r"|highest.*countr|countr.*highest|breakdown\s+of\s+stage)\b", s))
            _bc_has_nearest = bool(re.search(r"\bnearest\b|\bclosest\b|\bnear\b|\bcerca\b|\bpróximo[s]?\b", s))
            if (_bc_has_s1 or _bc_has_s2) and _bc_by_country and not _bc_has_nearest:
                if not sf:
                    return {"answer": "<p>⚠️ Stage 1/2 patient data is stored in Salesforce and requires an active session. "
                                      "Please log in via the <strong>Salesforce</strong> menu to refresh your session.</p>"}
                soql_s12_bc = (
                    "SELECT Account.ShippingCountry, "
                    "C_Number_of_Stage1_Individuals_followed__c, "
                    "C_Number_of_Stage2_Individuals_followed__c "
                    "FROM Opportunity "
                    "WHERE Account.RecordType.DeveloperName='SubAccount' "
                    "AND Account.C_Type__c='Clinical' "
                    "LIMIT 2000"
                )
                raw_s12_bc = tool_salesforce_query(sf, soql_s12_bc)
                recs_s12_bc = raw_s12_bc.get("records", []) if isinstance(raw_s12_bc, dict) else []
                _s12_bc_totals: dict = {}
                for _r in recs_s12_bc:
                    _c = (_r.get("Account") or {}).get("ShippingCountry") or ""
                    if not _c:
                        continue
                    _s1v = float(_r.get("C_Number_of_Stage1_Individuals_followed__c") or 0)
                    _s2v = float(_r.get("C_Number_of_Stage2_Individuals_followed__c") or 0)
                    if _c not in _s12_bc_totals:
                        _s12_bc_totals[_c] = {"s1": 0.0, "s2": 0.0}
                    _s12_bc_totals[_c]["s1"] += _s1v
                    _s12_bc_totals[_c]["s2"] += _s2v
                _bc_sort_key = "sf.C_Number_of_Stage2_Individuals_followed__c" if (_bc_has_s2 and not _bc_has_s1) else "sf.C_Number_of_Stage1_Individuals_followed__c"
                rows_s12_bc = sorted(
                    [{"country": c,
                      "sf.C_Number_of_Stage1_Individuals_followed__c": v["s1"],
                      "sf.C_Number_of_Stage2_Individuals_followed__c": v["s2"]}
                     for c, v in _s12_bc_totals.items()],
                    key=lambda x: x[_bc_sort_key], reverse=True
                )
                cols_s12_bc = [
                    {"key": "country", "label": "Country"},
                    {"key": "sf.C_Number_of_Stage1_Individuals_followed__c", "label": "Stage 1 (sum)"},
                    {"key": "sf.C_Number_of_Stage2_Individuals_followed__c", "label": "Stage 2 (sum)"},
                ]
                tbl_s12_bc = _normalize_table_for_ui({"columns": cols_s12_bc, "rows": rows_s12_bc})
                _bc_label = "Stage 2" if (_bc_has_s2 and not _bc_has_s1) else ("Stage 1" if (_bc_has_s1 and not _bc_has_s2) else "Stage 1/2")
                _dbg("Planner Stage1/2 by country: %d countries", len(rows_s12_bc))
                _s12_out: Dict[str, Any] = {
                    "answer": f"<p>{_bc_label} totals across {len(rows_s12_bc)} countries.</p>",
                    "table": tbl_s12_bc,
                }
                if re.search(r"\b(chart|bar|pie|graph|grouped|visuali[sz]e)\b", s):
                    _s12_ykeys = []
                    if _bc_has_s1: _s12_ykeys.append("sf.C_Number_of_Stage1_Individuals_followed__c")
                    if _bc_has_s2: _s12_ykeys.append("sf.C_Number_of_Stage2_Individuals_followed__c")
                    if not _s12_ykeys:
                        _s12_ykeys = ["sf.C_Number_of_Stage1_Individuals_followed__c",
                                      "sf.C_Number_of_Stage2_Individuals_followed__c"]
                    _s12_viz = tool_render_chart("bar", rows_s12_bc, "country", _s12_ykeys,
                                                {"title": f"{_bc_label} Individuals Followed per Country"}, {})
                    _s12_out["visualization"] = _s12_viz.get("visualization")
                return _s12_out
        except Exception as _e:
            _dbg("WARN: planner stage1/2 by country failed: %s", _e)

        # INTENCIÓN DIRECTA: Stage 1/2 site list → direct SF query (no internal HTTP)
        try:
            has_s1 = bool(re.search(r"\bstage\s*1\b", s))
            has_s2 = bool(re.search(r"\bstage\s*2\b", s))
            asks_sites = bool(re.search(r"\bsite[s]?\b|\bcenter[s]?\b|\bcentro[s]?\b", s))
            # If query asks for nearest/closest, skip deterministic handler → let Gemini use nearest_filtered_sites tool
            _has_nearest = bool(re.search(r"\bnearest\b|\bclosest\b|\bnear\b|\bcerca\b|\bpróximo[s]?\b|\bproximite\b|\bvicino\b|\bnähe\b|\bnahe\b", s))
            # If query references specific countries, skip → let Phase 1 planner build the correct country+stage FilterGroup
            # For 2-letter ISO codes, require UPPERCASE in original text (avoids "me"→Montenegro, "no"→Norway, etc.)
            _s12_has_country = any(
                (re.search(rf"\b{re.escape(k.upper())}\b", user_text) if len(k) <= 2
                 else re.search(rf"\b{re.escape(k)}\b", s))
                for k in _COUNTRY_MAP
            )
            if (has_s1 or has_s2) and not _has_nearest and not sf:
                return {"answer": "<p>⚠️ Stage 1/2 patient data is stored in Salesforce and requires an active session. "
                                  "Please log in via the <strong>Salesforce</strong> menu to refresh your session.</p>"}
            # Skip if qual fields also present — let qual handler combine Stage+qual via Explorer
            _s12_has_qual = bool(re.search(
                r"\bovernight\b|\bpharmac|\bhla\b|\bznt8\b|\bautoantibod|\binsulin.*autoantibod"
                r"|\blong.*visit\b|\bdocument.*retain|\bemergency.*personnel", s, re.I))
            # Detect "NOT in any assignment" — handle deterministically via Explorer with extra.AssignmentsCount
            _s12_has_asn_filter = bool(re.search(
                r"\bnot\b.*\bassignment|\bassignment.*\bnot\b|\bnot\s+in\s+any\s+assign"
                r"|\bno\s+(?:current\s+)?assign|\bwithout\s+assign", s, re.I))
            # Detect "both ... AND" → requires both Stage 1 AND Stage 2 at same site
            _s12_both_and = bool(has_s1 and has_s2 and re.search(r"\bboth\b", s))
            # Detect threshold: "more than 50", "at least 30", "over 20", "> 10", "50 or more"
            _s12_thresh_m = re.search(
                r"\bmore\s+than\s+(\d+)\b|\bover\s+(\d+)\b|\bgreater\s+than\s+(\d+)\b"
                r"|\bat\s+least\s+(\d+)\b|>\s*(\d+)\b|\b(\d+)\s+or\s+more\b",
                s, re.I)
            _s12_threshold = int(next(g for g in _s12_thresh_m.groups() if g is not None)) if _s12_thresh_m else 0
            _s12_is_at_least = bool(re.search(r"\bat\s+least\b|\bor\s+more\b", s, re.I)) if _s12_thresh_m else False
            # Detect top-N: "top 10"
            _s12_topn_m = re.search(r"\btop\s+(\d+)\b", s, re.I)
            _s12_topn = int(_s12_topn_m.group(1)) if _s12_topn_m else None

            if (has_s1 or has_s2) and asks_sites and sf and not _has_nearest and not _s12_has_country and not _s12_has_qual and _s12_has_asn_filter:
                # X02: Stage1/2 with "NOT in any assignment" — call Explorer with AssignmentsCount check
                _x02_rules: list = []
                _s1f_x = "sf.C_Number_of_Stage1_Individuals_followed__c"
                _s2f_x = "sf.C_Number_of_Stage2_Individuals_followed__c"
                _thresh_x = _s12_threshold
                _x02_op = "gte" if _s12_is_at_least else "gt"
                if has_s1:
                    _x02_rules.append({"field": _s1f_x, "operator": _x02_op, "value": _thresh_x})
                if has_s2:
                    _x02_rules.append({"field": _s2f_x, "operator": _x02_op, "value": _thresh_x})
                _x02_rules.append({"field": "extra.AssignmentsCount", "operator": "is_empty"})
                _x02_cols = ["sf.Account.Name", _s1f_x, _s2f_x]
                _x02_filter = {"logic": "AND", "rules": _x02_rules}
                _x02_url = "http://127.0.0.1:8000/api/explorer/search"
                _x02_ck = request.headers.get("cookie") if request else None
                with httpx.Client(timeout=90.0) as _xc:
                    _xr = _xc.post(_x02_url, json={"filters": _x02_filter, "columns": _x02_cols},
                                   headers={"cookie": _x02_ck} if _x02_ck else {})
                rows_x02: list = []
                if _xr.status_code < 400:
                    for _er in (_xr.json().get("rows") or []):
                        _aid = _er.get("account_id", "")
                        if not _aid: continue
                        _ed = _er.get("data") or {}
                        rows_x02.append({
                            "account_id": _aid,
                            "account_name": _er.get("account_name", ""),
                            "country": _er.get("country", ""),
                            "city": _er.get("city", ""),
                            _s1f_x: _ed.get(_s1f_x),
                            _s2f_x: _ed.get(_s2f_x),
                        })
                cols_x02 = [
                    {"key": "account_name", "label": "Site"},
                    {"key": "country", "label": "Country"},
                    {"key": "city", "label": "City"},
                    {"key": _s1f_x, "label": "Stage 1"},
                    {"key": _s2f_x, "label": "Stage 2"},
                ]
                tbl_x02 = _normalize_table_for_ui({"columns": cols_x02, "rows": rows_x02})
                _thresh_lbl = f" (> {_thresh_x})" if _thresh_x else ""
                _stage_lbl = ("Stage 1 and Stage 2" if (has_s1 and has_s2)
                              else "Stage 1" if has_s1 else "Stage 2")
                _dbg("Planner X02 Stage+NOT-assign Explorer: %d sites", len(rows_x02))
                return {
                    "answer": f"<p>Found <strong>{len(rows_x02)}</strong> site(s) with {_stage_lbl}{_thresh_lbl} "
                              f"patients that have no current assignment.</p>",
                    "table": tbl_x02,
                    "last_filters": _x02_filter,
                }

            if (has_s1 or has_s2) and asks_sites and sf and not _has_nearest and not _s12_has_country and not _s12_has_qual:
                if _s12_both_and:
                    # Must have BOTH Stage 1 AND Stage 2
                    stage_cond = ("C_Number_of_Stage1_Individuals_followed__c > 0 "
                                  "AND C_Number_of_Stage2_Individuals_followed__c > 0")
                    sort_field = "C_Number_of_Stage1_Individuals_followed__c"
                else:
                    stage_cond_parts = []
                    thresh = _s12_threshold
                    _soql_op = ">=" if _s12_is_at_least else ">"
                    if has_s1:
                        stage_cond_parts.append(f"C_Number_of_Stage1_Individuals_followed__c {_soql_op} {thresh}")
                    if has_s2:
                        stage_cond_parts.append(f"C_Number_of_Stage2_Individuals_followed__c {_soql_op} {thresh}")
                    stage_cond = " OR ".join(stage_cond_parts)
                    sort_field = ("C_Number_of_Stage2_Individuals_followed__c"
                                  if (has_s2 and not has_s1) else "C_Number_of_Stage1_Individuals_followed__c")
                soql_s12 = (
                    "SELECT Account.Id, Account.Name, Account.ShippingCountry, Account.ShippingCity, "
                    "C_Number_of_Stage1_Individuals_followed__c, C_Number_of_Stage2_Individuals_followed__c "
                    "FROM Opportunity "
                    "WHERE Account.RecordType.DeveloperName='SubAccount' "
                    "AND Account.C_Type__c='Clinical' "
                    f"AND ({stage_cond}) "
                    f"ORDER BY {sort_field} DESC NULLS LAST "
                    "LIMIT 2000"
                )
                raw_s12 = tool_salesforce_query(sf, soql_s12)
                recs_s12 = raw_s12.get("records", []) if isinstance(raw_s12, dict) else []
                if recs_s12:
                    rows_s12 = []
                    for r in recs_s12:
                        acc = r.get("Account") or {}
                        rows_s12.append({
                            "account_id": acc.get("Id") or r.get("AccountId",""),
                            "account_name": acc.get("Name",""),
                            "country": acc.get("ShippingCountry",""),
                            "city": acc.get("ShippingCity",""),
                            "sf.C_Number_of_Stage1_Individuals_followed__c": r.get("C_Number_of_Stage1_Individuals_followed__c"),
                            "sf.C_Number_of_Stage2_Individuals_followed__c": r.get("C_Number_of_Stage2_Individuals_followed__c"),
                        })
                    # Apply top-N limit after sorting
                    if _s12_topn:
                        rows_s12 = rows_s12[:_s12_topn]
                    cols_s12 = [
                        {"key":"account_name","label":"Site"},
                        {"key":"country","label":"Country"},
                        {"key":"city","label":"City"},
                        {"key":"sf.C_Number_of_Stage1_Individuals_followed__c","label":"Stage 1"},
                        {"key":"sf.C_Number_of_Stage2_Individuals_followed__c","label":"Stage 2"},
                    ]
                    tbl12 = _normalize_table_for_ui({"columns": cols_s12, "rows": rows_s12})
                    n1 = sum(1 for r in rows_s12 if (r.get("sf.C_Number_of_Stage1_Individuals_followed__c") or 0) > 0)
                    n2 = sum(1 for r in rows_s12 if (r.get("sf.C_Number_of_Stage2_Individuals_followed__c") or 0) > 0)
                    if _s12_both_and:
                        label = "Stage 1 AND Stage 2"
                        _thresh_suffix = ""
                    elif _s12_threshold > 0:
                        label = ("Stage 1" if (has_s1 and not has_s2) else
                                 "Stage 2" if (has_s2 and not has_s1) else "Stage 1 or Stage 2")
                        _thresh_op_lbl = ">=" if _s12_is_at_least else ">"
                        _thresh_suffix = f" ({_thresh_op_lbl} {_s12_threshold})"
                    else:
                        label = ("Stage 1" if (has_s1 and not has_s2) else
                                 "Stage 2" if (has_s2 and not has_s1) else "Stage 1 or Stage 2")
                        _thresh_suffix = ""
                    _topn_suffix = f" (top {_s12_topn})" if _s12_topn else ""
                    _dbg("Planner Stage1/2: %d sites (both=%s, thresh=%s, top=%s)", len(rows_s12), _s12_both_and, _s12_threshold, _s12_topn)
                    return {
                        "answer": f"<p>Found <strong>{len(rows_s12)}</strong> sites{_topn_suffix} with {label} individuals followed{_thresh_suffix} "
                                  f"({n1} with Stage 1, {n2} with Stage 2).</p>",
                        "table": tbl12,
                    }
        except Exception as _e:
            _dbg("WARN: planner stage1/2 failed: %s", _e)

        # INTENCIÓN DIRECTA: Phase I/II/III clinical trials → internal explorer_search (proven path)
        try:
            _ct_phase1 = bool(re.search(r"\bphase\s*[i1]\b", s, re.I))
            _ct_phase2 = bool(re.search(r"\bphase\s*(?:ii|2)\b", s, re.I))
            _ct_phase3 = bool(re.search(r"\bphase\s*(?:iii|3)\b", s, re.I))
            _ct_t1d = bool(re.search(r"\btype\s*1\b|\bt1d\b|\bdiabet", s, re.I))
            _ct_asks = bool(re.search(r"\bsite[s]?\b|\bcenter[s]?\b|\bcentro[s]?\b|show|find|list|which|participat|involve|conduc", s))
            if (_ct_phase1 or _ct_phase2 or _ct_phase3) and (_ct_t1d or _ct_asks):
                if not sf:
                    return {"answer": "<p>⚠️ Clinical trial data is stored in Salesforce and requires an active session. "
                                      "Please log in via the <strong>Salesforce</strong> menu.</p>"}
                # Use internal explorer_search — same proven path Gemini uses (sf.C_Phase_*_Type1__c filter)
                _ct_rules = []
                if _ct_phase1:
                    _ct_rules.append({"field": "sf.C_Phase_I_Type1__c", "operator": "equals", "value": True})
                if _ct_phase2:
                    _ct_rules.append({"field": "sf.C_Phase_II_Type1__c", "operator": "equals", "value": True})
                if _ct_phase3:
                    _ct_rules.append({"field": "sf.C_Phase_III_Type1__c", "operator": "equals", "value": True})
                # Detect country filter — add site.country rule if specific country mentioned
                from app.utils.country_norms import resolve_countries as _resolve_ct_countries
                _ct_isos = _resolve_ct_countries(user_text)
                if _ct_isos:
                    if len(_ct_isos) == 1:
                        _ct_rules.append({"field": "site.country", "operator": "equals", "value": _ct_isos[0]})
                    else:
                        _ct_rules.append({"logic": "OR", "rules": [
                            {"field": "site.country", "operator": "equals", "value": _iso}
                            for _iso in _ct_isos
                        ]})
                # Only request Phase field columns (account_name/country/city come from top-level Explorer row)
                _ct_sf_cols = []
                if _ct_phase1:
                    _ct_sf_cols.append("sf.C_Phase_I_Type1__c")
                if _ct_phase2:
                    _ct_sf_cols.append("sf.C_Phase_II_Type1__c")
                if _ct_phase3:
                    _ct_sf_cols.append("sf.C_Phase_III_Type1__c")
                _ct_url = f"http://127.0.0.1:8000{EXPLORER_SEARCH_PATH}"
                _ct_hdrs = {}
                _ct_ck = request.headers.get("cookie") if request else None
                if _ct_ck:
                    _ct_hdrs["cookie"] = _ct_ck
                with httpx.Client(timeout=60.0) as _ct_cli:
                    _ct_resp = _ct_cli.post(
                        _ct_url,
                        json={"filters": {"logic": "OR" if len(_ct_rules) > 1 else "AND", "rules": _ct_rules},
                              "columns": _ct_sf_cols},
                        headers=_ct_hdrs,
                    )
                if _ct_resp.status_code >= 400:
                    raise RuntimeError(f"explorer_search failed: {_ct_resp.text[:200]}")
                rows_ct = _ct_resp.json().get("rows") or []
                if rows_ct:
                    cols_ct = [{"key": "account_name", "label": "Site"}, {"key": "country", "label": "Country"}, {"key": "city", "label": "City"}]
                    if _ct_phase1:
                        cols_ct.append({"key": "sf.C_Phase_I_Type1__c", "label": "Phase I (T1D)"})
                    if _ct_phase2:
                        cols_ct.append({"key": "sf.C_Phase_II_Type1__c", "label": "Phase II (T1D)"})
                    if _ct_phase3:
                        cols_ct.append({"key": "sf.C_Phase_III_Type1__c", "label": "Phase III (T1D)"})
                    tbl_ct = _normalize_table_for_ui({"columns": cols_ct, "rows": rows_ct})
                    _ct_by_country: dict = {}
                    for _r in (tbl_ct.get("rows") or []):
                        _c = _r.get("country", "")
                        _ct_by_country[_c] = _ct_by_country.get(_c, 0) + 1
                    _ct_top = sorted(_ct_by_country.items(), key=lambda x: x[1], reverse=True)[:5]
                    _n_ct = len(tbl_ct.get("rows") or [])
                    _dbg("Planner CT: %d sites via explorer_search (Phase I=%s, II=%s, III=%s)", _n_ct, _ct_phase1, _ct_phase2, _ct_phase3)
                    return {
                        "answer": f"<p>{_n_ct} total across {len(_ct_by_country)} groups. "
                                  f"Top: {', '.join(f'{c}: {n}.0' for c, n in _ct_top)}.</p>",
                        "table": tbl_ct,
                    }
        except Exception as _e:
            _dbg("WARN: planner CT handler failed: %s", _e)

        # INTENCIÓN DIRECTA: assignment queries → Explorer API (extra.AssignmentsNames / AssignmentsCount)
        try:
            _has_asn = bool(re.search(r"\bassignment[s]?\b|\bopportunit\w+\b|\binvolved\s+in\b|\bparticipating\s+(?:in|to)\b", s))
            _asks_sites_asn = bool(re.search(
                r"\bsite[s]?\b|\bcenter[s]?\b|\bwhich\b|\bshow\b|\bfind\b|\blist\b|\bactive\b|\binvolved\b", s))
            _is_phase_query = bool(re.search(r"\bphase\s*[i1]|\bphase\s*(?:ii|2)|\bphase\s*(?:iii|3)\b", s, re.I))
            # Detect "most/ranking" → route to activity counts (A04: which assignments have the most sites?)
            _asn_most_sites = bool(re.search(
                r"\bmost\s+site[s]?\b|\bmost\s+participation\b|\branking\b|\bhighest.*site[s]?\b"
                r"|\bmost\s+center[s]?\b|\blargest.*assignment[s]?\b", s, re.I))
            # Also detect "per country" / "by country" aggregation intent
            _asn_per_country = bool(re.search(r"\bper\s+country\b|\bby\s+country\b|\beach\s+country\b|\bhow\s+many.*country\b", s))
            # Also detect "near/close to" assignment intent → should route to km-of-assignment handler, not here
            _asn_near = bool(re.search(r"\bnear(?:\s+to)?\b|\bclos(?:e|est)\b|\bwithin\b|\baround\b", s, re.I))
            _asn_is_sponsor = bool(re.search(r"\bsponsore?(?:d|s|ing)?\b", s, re.I))
            if _has_asn and _asks_sites_asn and not _is_phase_query and not _asn_most_sites and not _asn_near and not _asn_is_sponsor:
                # Detect NOT modifier — includes "haven't", "never", "no assignment(s)"
                _asn_not = bool(re.search(
                    r"\bnot\b|\bno\b|\bexcluding\b|\bexclude\b|\bwithout\b"
                    r"|\bhaven.?t\b|\bnever\b|\bnot\s+been\b", s))
                # Detect temporal: "last N years", "past N years", "in the last N years"
                _asn_temporal_m = re.search(
                    r"\b(?:last|past)\s+(\d+)\s+year", s, re.I)
                _asn_temporal_years = int(_asn_temporal_m.group(1)) if _asn_temporal_m else None
                # Extract assignment name: look for Title-Cased words near "assignment"
                # e.g. "in the Beta Preserve assignment" → "Beta Preserve"
                _asn_name: Optional[str] = None
                _asn_name_m = re.search(
                    r'\bin\s+(?:the\s+)?([A-Z][A-Za-z0-9\s\-]{2,40}?)\s+assignment'
                    r'|\bthe\s+([A-Z][A-Za-z0-9\s\-]{2,40}?)\s+assignment',
                    user_text)
                if _asn_name_m:
                    _asn_name = (_asn_name_m.group(1) or _asn_name_m.group(2) or "").strip()
                # Also try: "involved in Beta Preserve" without "assignment" suffix
                if not _asn_name:
                    _asn_name_m2 = re.search(r'\binvolved\s+in\s+(?:the\s+)?([A-Z][A-Za-z0-9\s\-]{2,40})', user_text)
                    if _asn_name_m2:
                        _asn_name = _asn_name_m2.group(1).strip()
                # Detect country filter for "assignment in [Country]"
                from app.utils.country_norms import resolve_countries as _rc_asn
                _asn_countries = _rc_asn(user_text)
                # Build Explorer filter
                _asn_temporal_exclude = None
                if _asn_name:
                    _asn_op = "not_contains" if _asn_not else "contains"
                    _asn_filter = {"logic": "AND", "rules": [
                        {"field": "extra.AssignmentsNames", "operator": _asn_op, "value": _asn_name}
                    ]}
                elif _asn_not and _asn_temporal_years and sf:
                    # Temporal NOT: "no assignment in the last N years"
                    # Strategy: find accounts WITH assignments since cutoff, then return sites NOT in that set
                    from datetime import datetime, timedelta
                    _cutoff = (datetime.utcnow() - timedelta(days=_asn_temporal_years * 365)).strftime("%Y-%m-%dT00:00:00Z")
                    _recent_soql = (
                        f"SELECT C_Account__c FROM Assignment__c "
                        f"WHERE CreatedDate >= {_cutoff} "
                        f"GROUP BY C_Account__c"
                    )
                    _recent_recs = sf.query_all(_recent_soql).get("records", [])
                    _recent_accs = {str(r.get("C_Account__c")) for r in _recent_recs if r.get("C_Account__c")}
                    _dbg("Planner assignment temporal: %d accounts with assignments since %s", len(_recent_accs), _cutoff[:10])
                    # Get ALL sites via Explorer (no filter), then subtract
                    _asn_filter = {"logic": "AND", "rules": []}
                    _asn_temporal_exclude = _recent_accs  # stash for post-filter
                else:
                    # No specific name: NOT → is_null (no assignments ever), else → count>0 (active)
                    _asn_temporal_exclude = None
                    _asn_rules_list = [
                        {"field": "extra.AssignmentsCount",
                         "operator": "is_null" if _asn_not else "gt",
                         "value": 0}
                    ]
                    # Add country filter if detected ("assignment in Germany")
                    if _asn_countries:
                        for _iso in _asn_countries:
                            _asn_rules_list.append({"field": "site.country", "operator": "equals", "value": _iso})
                    _asn_filter = {"logic": "AND", "rules": _asn_rules_list}
                _asn_url = "http://127.0.0.1:8000/api/explorer/search"
                _asn_ck = request.headers.get("cookie") if request else None
                _asn_cols = ["sf.Account.Name", "extra.AssignmentsNames"]
                with httpx.Client(timeout=90.0) as _ac:
                    _ar = _ac.post(_asn_url, json={"filters": _asn_filter, "columns": _asn_cols},
                                   headers={"cookie": _asn_ck} if _asn_ck else {})
                if _ar.status_code < 400:
                    rows_asn: list = []
                    for _er in (_ar.json().get("rows") or []):
                        _aid = _er.get("account_id", "")
                        if not _aid:
                            continue
                        _ed = _er.get("data") or {}
                        rows_asn.append({
                            "account_id": _aid,
                            "account_name": _er.get("account_name", ""),
                            "country": _er.get("country", ""),
                            "city": _er.get("city", ""),
                            "extra.AssignmentsNames": _ed.get("extra.AssignmentsNames", ""),
                        })
                    # Temporal post-filter: exclude accounts with recent assignments
                    if _asn_temporal_years and _asn_temporal_exclude is not None:
                        rows_asn = [r for r in rows_asn if r.get("account_id") not in _asn_temporal_exclude]
                    # If per-country aggregation requested (H30 pattern), aggregate site counts
                    if _asn_per_country and not _asn_name:
                        from collections import Counter as _Counter
                        _country_counts = _Counter(r["country"] for r in rows_asn)
                        cols_asn = [
                            {"key": "country", "label": "Country"},
                            {"key": "site_count", "label": "Sites"},
                        ]
                        rows_agg = [{"country": c, "site_count": n}
                                    for c, n in sorted(_country_counts.items(), key=lambda x: -x[1])]
                        tbl_asn = _normalize_table_for_ui({"columns": cols_asn, "rows": rows_agg})
                        _label = "not active in" if _asn_not else "active in"
                        _ans_str = (f"<p>Found <strong>{len(rows_asn)}</strong> site(s) "
                                    f"{_label} assignments across <strong>{len(rows_agg)}</strong> countries.</p>")
                    else:
                        cols_asn = [
                            {"key": "account_name", "label": "Site"},
                            {"key": "city", "label": "City"},
                            {"key": "country", "label": "Country"},
                            {"key": "extra.AssignmentsNames", "label": "Assignments"},
                        ]
                        tbl_asn = _normalize_table_for_ui({"columns": cols_asn, "rows": rows_asn})
                        if _asn_name:
                            _op_str = "not in" if _asn_not else "in"
                            _ans_str = f"<p>Found <strong>{len(rows_asn)}</strong> site(s) {_op_str} the <em>{_asn_name}</em> assignment.</p>"
                        else:
                            if _asn_temporal_years:
                                _label = f"not in any assignment in the last {_asn_temporal_years} year(s)"
                            elif _asn_not:
                                _label = "not in any assignment"
                            else:
                                _label = "active in at least one assignment"
                            _country_suffix = ""
                            if _asn_countries:
                                from app.utils.country_norms import iso2_to_sf_name as _iso2sf_asn
                                _country_suffix = " in " + ", ".join(_iso2sf_asn(c) for c in _asn_countries)
                            _ans_str = f"<p>Found <strong>{len(rows_asn)}</strong> site(s) {_label}{_country_suffix}.</p>"
                    _dbg("Planner assignment Explorer: %d sites (name=%s, not=%s, per_country=%s)", len(rows_asn), _asn_name, _asn_not, _asn_per_country)
                    return {"answer": _ans_str, "table": tbl_asn, "last_filters": _asn_filter}
        except Exception as _e:
            _dbg("WARN: planner assignment handler failed: %s", _e)

        # INTENCIÓN DIRECTA: qual field queries → Explorer API (pharmacy, overnight, ZnT8, HLA, etc.)
        try:
            has_pharmacy   = bool(re.search(r"\bpharmac", s))
            has_overnight  = bool(re.search(r"\bovernight\b", s))
            has_znt8       = bool(re.search(r"\bznt8\b|\bzn[-\u2010]?t8\b", s, re.I))
            has_hla        = bool(re.search(r"\bhla\b", s, re.I))
            has_insulin_ab = bool(re.search(r"\binsulin\b.*\bautoantibod|\bautoantibod.*\binsulin", s, re.I))
            # Generic "autoantibodies" without ZnT8/insulin prefix → treat as ZnT8 (islet autoantibody test)
            if (not has_insulin_ab and not has_znt8
                    and re.search(r"\bautoantibod", s, re.I)):
                has_znt8 = True
            has_longday    = bool(re.search(r"\bsingle.day\b.*\bvisit|\bday.*visit.*\bhour|\b8.hour.*visit|\bvisit.*\b8.hour", s, re.I))
            has_documents  = bool(re.search(r"\bdocument[s]?.*\bretain|\bretain.*\bdocument", s, re.I))
            has_emergency  = bool(re.search(r"\bemergency\b.*\bpersonnel|\bemergency\b.*\barrive|\b30.minut", s, re.I))
            has_ogtt       = bool(re.search(r"\bogtt\b|\boral\s+glucose\b|\bglucose\s+tolerance\b|\bglucose\s+test|\bglucose\b.*\bcapacit|\bcapacit.*\bglucose", s, re.I))
            has_early_diag = bool(re.search(r"\bearly\s+diagnos|\bscreening\b.*\bdiagnos|\bdiagnos.*\bscreening\b|\bearly\s+(?:t1d\s+)?screen", s, re.I))
            has_cts_validated = bool(re.search(r"\bcts[\s\-]*validat|\bvalidat\w*\s+cts\b|\bclinical\s+trial\s+site\s+validat", s, re.I))
            has_profiling_complete = bool(re.search(
                r"\bprofiling\b.*\bcomplete[d]?\b|\bcomplete[d]?\b.*\bprofiling\b"
                r"|c_profiling_complete|profiling_complete", s, re.I))
            _has_any_qual_field = (has_pharmacy or has_overnight or has_znt8 or has_hla
                                  or has_insulin_ab or has_longday or has_documents or has_emergency
                                  or has_ogtt or has_early_diag)
            # CTS-validated and profiling-complete are SF-level qualifiers.
            # They activate the qual handler ONLY when combined with a real qual/SF field,
            # so that standalone "profiling complete" still routes to the pipeline handler
            # and standalone "CTS sites" can route to other handlers.
            asks_qual = (_has_any_qual_field
                         or has_cts_validated
                         or (has_profiling_complete and _has_any_qual_field))
            asks_sites2 = bool(re.search(
                r"\bsite[s]?\b|\bcenter[s]?\b|\bcentro[s]?\b"
                r"|show\b|find\b|list\b|which\b|those\b|offer\b|have\b|with\b|do\b", s))
            # Skip qual handler if nearest/within-km is the primary intent (let nearest handler fire)
            _qual_has_nearest = bool(re.search(
                r"\bnearest\b|\bclosest\b|\bwithin\s+\d+\s*km\b|\bdriving\s+distance\b", s))
            if asks_qual and asks_sites2 and not _qual_has_nearest:
                # Build Explorer filter rules for detected qual intents
                _qual_rules: list = []
                _qual_cols: list = ["sf.Account.Name"]
                _K_PHARM    = "qual.3_6__is_your_pharmacy_on_site_or_off_campus"
                _K_OVER     = "qual.3_5_2__overnight_stay"
                _K_ZNT8     = "qual.3_8__znt8"
                _K_HLA      = "sf.C_Is_HLA_typing_performed__c"
                _K_INS      = "qual.3_8__insulin"
                _K_LONGDAY  = "qual.3_5_2__longest_single_day_visit_that_could_be_accommodated_hours"
                _K_DOCS     = "qual.3_5_1__how_long_are_documents_retained_years"
                _K_EMERG    = "qual.3_3__acceptable_process"
                if has_pharmacy:
                    _qual_rules.append({"field": _K_PHARM, "operator": "equals", "value": "On-site"})
                    _qual_cols.append(_K_PHARM)
                if has_overnight:
                    _qual_rules.append({"field": _K_OVER, "operator": "contains", "value": "yes"})
                    _qual_cols.append(_K_OVER)
                if has_znt8:
                    _qual_rules.append({"field": _K_ZNT8, "operator": "contains", "value": "yes"})
                    _qual_cols.append(_K_ZNT8)
                if has_hla:
                    _qual_rules.append({"field": _K_HLA, "operator": "equals", "value": "Yes"})
                    _qual_cols.append(_K_HLA)
                if has_insulin_ab:
                    _qual_rules.append({"field": _K_INS, "operator": "contains", "value": "yes"})
                    _qual_cols.append(_K_INS)
                if has_longday:
                    _qual_rules.append({"field": _K_LONGDAY, "operator": "is_not_null"})
                    _qual_cols.append(_K_LONGDAY)
                if has_documents:
                    _qual_rules.append({"field": _K_DOCS, "operator": "is_not_null"})
                    _qual_cols.append(_K_DOCS)
                if has_emergency:
                    _qual_rules.append({"field": _K_EMERG, "operator": "is_not_null"})
                    _qual_cols.append(_K_EMERG)
                if has_ogtt:
                    _K_OGTT = "qual.3_8__glucose"
                    _qual_rules.append({"field": _K_OGTT, "operator": "contains", "value": "yes"})
                    _qual_cols.append(_K_OGTT)
                if has_early_diag:
                    _K_AAB = "qual.3_8__autoantibodies_aab"
                    _qual_rules.append({"field": _K_AAB, "operator": "contains", "value": "yes"})
                    _qual_cols.append(_K_AAB)
                if has_cts_validated:
                    _K_CTS = "sf.Account.INNODIA_Clinical_Trial_Site__c"
                    _qual_rules.append({"field": _K_CTS, "operator": "equals", "value": True})
                    _qual_cols.append(_K_CTS)
                if has_profiling_complete:
                    _K_PROF_C = "sf.C_Profiling_Complete__c"
                    _qual_rules.append({"field": _K_PROF_C, "operator": "is_not_null"})
                    _qual_cols.append(_K_PROF_C)
                # Extract country filter from query text (multi-country support)
                _qual_countries: list = []  # [(sf_name, iso2), ...]
                _qual_seen_sf: set = set()
                for _cn in sorted(_COUNTRY_MAP.keys(), key=len, reverse=True):
                    _cm = (re.search(rf"\b{re.escape(_cn.upper())}\b", user_text) if len(_cn) <= 2
                           else re.search(rf"\b{re.escape(_cn)}\b", s))
                    if _cm:
                        _sf_qn, _iso_q = _COUNTRY_MAP[_cn]
                        if _sf_qn not in _qual_seen_sf:
                            _qual_countries.append((_sf_qn, _iso_q))
                            _qual_seen_sf.add(_sf_qn)
                # Also add Stage 1/2 SF filter rules when detected alongside qual fields
                _q_has_s1 = bool(re.search(r"\bstage\s*1\b", s))
                _q_has_s2 = bool(re.search(r"\bstage\s*2\b", s))
                _q_both_stages = bool(_q_has_s1 and _q_has_s2 and re.search(r"\bboth\b", s))
                _q_stage_thresh_m = re.search(r"\b(?:more\s+than|over|greater\s+than|>)\s*(\d+)", s, re.I)
                _q_stage_thresh = int(_q_stage_thresh_m.group(1)) if _q_stage_thresh_m else 0
                if _q_has_s1 or _q_has_s2:
                    _s1f = "sf.C_Number_of_Stage1_Individuals_followed__c"
                    _s2f = "sf.C_Number_of_Stage2_Individuals_followed__c"
                    _sq_thresh = _q_stage_thresh
                    if _q_both_stages:
                        _qual_rules.append({"field": _s1f, "operator": "gt", "value": _sq_thresh})
                        _qual_rules.append({"field": _s2f, "operator": "gt", "value": _sq_thresh})
                    else:
                        if _q_has_s1:
                            _qual_rules.append({"field": _s1f, "operator": "gt", "value": _sq_thresh})
                        if _q_has_s2:
                            _qual_rules.append({"field": _s2f, "operator": "gt", "value": _sq_thresh})
                    # Add Stage columns to display
                    if _q_has_s1 and _s1f not in _qual_cols:
                        _qual_cols.append(_s1f)
                    if _q_has_s2 and _s2f not in _qual_cols:
                        _qual_cols.append(_s2f)
                # Add T1D patient count filter when T1D + threshold detected (and no Stage filter)
                # "T1D patients" = total (U18 + O18).  We filter on BOTH fields with OR
                # so a site qualifies if EITHER age group alone exceeds threshold, then
                # Python-side post-filter checks the SUM.
                _q_has_t1d_thresh = bool(re.search(r"\bt1d\b|\btype\s*[–\-]?1\b|\btype\s*1\s+diab", s, re.I))
                _q_t1d_under = bool(re.search(r"\bunder\s*18\b|\b<\s*18\b|\bu\.?18\b|\bchildren\b|\bpediatric\b|\bpaediatric\b", s, re.I))
                _q_t1d_over = bool(re.search(r"\bover\s*18\b|\b>\s*18\b|\bo\.?18\b|\badult[s]?\b|\b(?:>=?\s*18|18\+)\b", s, re.I))
                if _q_has_t1d_thresh and _q_stage_thresh_m is not None and not _q_has_s1 and not _q_has_s2:
                    _T1D_O18 = "sf.C_Number_of_T1D_Patients_currently_O_18__c"
                    _T1D_U18 = "sf.C_Number_of_T1D_Patients_currently_U_18__c"
                    if _q_t1d_under and not _q_t1d_over:
                        # Explicitly asks for under-18 only
                        _qual_rules.append({"field": _T1D_U18, "operator": "gt", "value": _q_stage_thresh})
                        if _T1D_U18 not in _qual_cols:
                            _qual_cols.append(_T1D_U18)
                    elif _q_t1d_over and not _q_t1d_under:
                        # Explicitly asks for over-18 only
                        _qual_rules.append({"field": _T1D_O18, "operator": "gt", "value": _q_stage_thresh})
                        if _T1D_O18 not in _qual_cols:
                            _qual_cols.append(_T1D_O18)
                    else:
                        # Generic "T1D patients" → fetch both columns, post-filter on sum
                        # No SOQL threshold on individual fields — threshold applied
                        # Python-side on (O18 + U18) after Explorer returns rows
                        if _T1D_O18 not in _qual_cols:
                            _qual_cols.append(_T1D_O18)
                        if _T1D_U18 not in _qual_cols:
                            _qual_cols.append(_T1D_U18)
                        _q_t1d_sum_thresh = _q_stage_thresh  # stash for post-filter
                all_rules: list = list(_qual_rules)
                if _qual_countries:
                    if len(_qual_countries) > 1:
                        all_rules = [{"logic": "OR", "rules": [
                            {"field": "site.country", "operator": "equals", "value": _iso_q}
                            for _, _iso_q in _qual_countries
                        ]}] + _qual_rules
                    else:
                        all_rules = [{"field": "site.country", "operator": "equals",
                                      "value": _qual_countries[0][1]}] + _qual_rules
                _qual_filter = {"logic": "AND", "rules": all_rules}
                # Call Explorer API internally (same source-of-truth as ground truth)
                _qual_url = "http://127.0.0.1:8000/api/explorer/search"
                _qual_ck = request.headers.get("cookie") if request else None
                _qual_hdrs = {"cookie": _qual_ck} if _qual_ck else {}
                with httpx.Client(timeout=90.0) as _qc:
                    _qr = _qc.post(_qual_url, json={"filters": _qual_filter, "columns": _qual_cols},
                                   headers=_qual_hdrs)
                rows_qual: list = []
                if _qr.status_code < 400:
                    for _er in (_qr.json().get("rows") or []):
                        _aid = _er.get("account_id", "")
                        if not _aid:
                            continue
                        _ed = _er.get("data") or {}
                        _row = {
                            "account_id": _aid,
                            "account_name": _er.get("account_name", ""),
                            "country": _er.get("country", ""),
                            "city": _er.get("city", ""),
                        }
                        for _ck in _qual_cols[1:]:
                            _row[_ck] = _ed.get(_ck)
                        rows_qual.append(_row)
                # Post-filter: T1D sum threshold (generic "T1D patients" = U18 + O18)
                if _q_has_t1d_thresh and _q_stage_thresh_m is not None and not _q_has_s1 and not _q_has_s2 and not _q_t1d_under and not _q_t1d_over:
                    _T1D_O18 = "sf.C_Number_of_T1D_Patients_currently_O_18__c"
                    _T1D_U18 = "sf.C_Number_of_T1D_Patients_currently_U_18__c"
                    rows_qual = [
                        r for r in rows_qual
                        if (float(r.get(_T1D_O18) or 0) + float(r.get(_T1D_U18) or 0)) > _q_stage_thresh
                    ]
                cols_qual = [
                    {"key": "account_name", "label": "Site"},
                    {"key": "city", "label": "City"},
                    {"key": "country", "label": "Country"},
                ]
                if has_pharmacy:   cols_qual.append({"key": _K_PHARM,   "label": "Pharmacy"})
                if has_overnight:  cols_qual.append({"key": _K_OVER,    "label": "Overnight Stay"})
                if has_znt8:       cols_qual.append({"key": _K_ZNT8,    "label": "ZnT8"})
                if has_hla:        cols_qual.append({"key": _K_HLA,     "label": "HLA Typing"})
                if has_insulin_ab: cols_qual.append({"key": _K_INS,     "label": "Insulin Ab"})
                if has_longday:    cols_qual.append({"key": _K_LONGDAY, "label": "Max Day (hrs)"})
                if has_documents:  cols_qual.append({"key": _K_DOCS,    "label": "Doc Retention (yrs)"})
                if has_emergency:  cols_qual.append({"key": _K_EMERG,   "label": "Emergency Process"})
                if has_ogtt:       cols_qual.append({"key": "qual.3_8__glucose", "label": "Glucose Testing"})
                if has_early_diag: cols_qual.append({"key": "qual.3_8__autoantibodies_aab", "label": "Autoantibody Screening"})
                if has_cts_validated: cols_qual.append({"key": "sf.Account.INNODIA_Clinical_Trial_Site__c", "label": "CTS Validated"})
                if has_profiling_complete: cols_qual.append({"key": "sf.C_Profiling_Complete__c", "label": "Profiling Complete"})
                if _q_has_s1:      cols_qual.append({"key": "sf.C_Number_of_Stage1_Individuals_followed__c", "label": "Stage 1"})
                if _q_has_s2:      cols_qual.append({"key": "sf.C_Number_of_Stage2_Individuals_followed__c", "label": "Stage 2"})
                if _q_has_t1d_thresh and _q_stage_thresh_m is not None and not _q_has_s1 and not _q_has_s2:
                    if _q_t1d_under and not _q_t1d_over:
                        cols_qual.append({"key": "sf.C_Number_of_T1D_Patients_currently_U_18__c", "label": "T1D <18"})
                    elif _q_t1d_over and not _q_t1d_under:
                        cols_qual.append({"key": "sf.C_Number_of_T1D_Patients_currently_O_18__c", "label": "T1D ≥18"})
                    else:
                        cols_qual.append({"key": "sf.C_Number_of_T1D_Patients_currently_O_18__c", "label": "T1D ≥18"})
                        cols_qual.append({"key": "sf.C_Number_of_T1D_Patients_currently_U_18__c", "label": "T1D <18"})
                tbl_qual = _normalize_table_for_ui({"columns": cols_qual, "rows": rows_qual})
                _cond_parts = []
                if has_pharmacy:   _cond_parts.append("on-site pharmacy")
                if has_overnight:  _cond_parts.append("overnight stay")
                if has_znt8:       _cond_parts.append("ZnT8 testing")
                if has_hla:        _cond_parts.append("HLA typing")
                if has_insulin_ab: _cond_parts.append("insulin autoantibody testing")
                if has_longday:    _cond_parts.append("long single-day visits")
                if has_documents:  _cond_parts.append("document retention data")
                if has_emergency:  _cond_parts.append("emergency personnel process")
                if has_ogtt:       _cond_parts.append("glucose testing (incl. OGTT)")
                if has_early_diag: _cond_parts.append("early diagnosis screening (autoantibody testing)")
                if has_cts_validated: _cond_parts.append("CTS-validated")
                if has_profiling_complete: _cond_parts.append("profiling complete")
                if _q_has_s1:      _cond_parts.append("Stage 1 patients")
                if _q_has_s2:      _cond_parts.append("Stage 2 patients")
                if _q_has_t1d_thresh and _q_stage_thresh_m is not None and not _q_has_s1 and not _q_has_s2:
                    _cond_parts.append(f"T1D patients > {_q_stage_thresh}")
                _cond_txt = " and ".join(_cond_parts)
                _loc_str = (f" in {', '.join(n for n, _ in _qual_countries)}"
                            if _qual_countries else "")
                _dbg("Planner qual Explorer: %d sites with %s%s", len(rows_qual), _cond_txt, _loc_str)
                return {
                    "answer": f"<p>Found <strong>{len(rows_qual)}</strong> site(s){_loc_str} with {_cond_txt}.</p>",
                    "table": tbl_qual,
                    "last_filters": _qual_filter,
                }
        except Exception as _e:
            _dbg("WARN: planner qual handler failed: %s", _e)

        # INTENCIÓN DIRECTA: Profiling date-diff aggregation by country (GQ16)
        # "How many days on average does profiling take per country (from first contact to meeting)?"
        # Must fire BEFORE the pipeline handler below, which would otherwise match via _pl_no_meeting
        # (regex \bfirst.contact.*meeting) and return a list of sites instead of an aggregation.
        try:
            _gq16_avg     = bool(re.search(
                r"\baverage\b|\bavg\b|\bmean\b|\bhow\s+many\s+days\b"
                r"|\bdays?\s+(?:on\s+)?average\b|\btime\s+between\b|\bduration\b",
                s, re.I,
            ))
            _gq16_profil  = bool(re.search(r"\bprofil", s, re.I))
            # "form sent + meeting/complete" is an equally specific signal — no "profiling" needed
            _gq16_form_sent_pair = (
                bool(re.search(r"\bform\s+sent\b", s, re.I))
                and bool(re.search(r"\bmeeting\b|\bprofil.*complete\b|\bcomplete.*profil\b", s, re.I))
            )
            _gq16_country = bool(re.search(r"\bby\s+country\b|\bper\s+country\b|\bcountry\b", s, re.I))
            if _gq16_avg and (_gq16_profil or _gq16_form_sent_pair) and _gq16_country:
                # Choose date pair based on phrasing:
                #   "form sent → profiling complete/closed/finalised" OR
                #   default "form sent → meeting" (canonical GQ16: "first contact to meeting")
                _gq16_use_complete = bool(re.search(
                    r"\bprofiling\s+complete\b|\bprofil.*\bcomplete\b|\bcomplete.*\bprofil\b"
                    r"|\bclos(?:e|ed)\b|\bfinalised?\b|\bfinished\b",
                    s, re.I,
                ))
                if _gq16_use_complete:
                    _gq16_sf1 = "sf.C_Form_Questionnaire_sent__c"
                    _gq16_sf2 = "sf.C_Profiling_Complete__c"
                    _gq16_lbl1, _gq16_lbl2 = "Form Sent", "Profiling Complete"
                else:
                    # Default: form sent → meeting date
                    _gq16_sf1 = "sf.C_Form_Questionnaire_sent__c"
                    _gq16_sf2 = "sf.C_Meeting_Date__c"
                    _gq16_lbl1, _gq16_lbl2 = "Form Sent", "Meeting Date"

                # Use Explorer API (same as pipeline/assignment handlers) — avoids direct SOQL issues
                _gq16_url = "http://127.0.0.1:8000/api/explorer/search"
                _gq16_ck = request.headers.get("cookie") if request else None
                _gq16_filter = {
                    "logic": "AND",
                    "rules": [
                        {"field": _gq16_sf1, "operator": "is_not_null"},
                        {"field": _gq16_sf2, "operator": "is_not_null"},
                    ],
                }
                _gq16_cols = ["sf.Account.Name", _gq16_sf1, _gq16_sf2]
                with httpx.Client(timeout=90.0) as _gq16_hx:
                    _gq16_resp = _gq16_hx.post(
                        _gq16_url,
                        json={"filters": _gq16_filter, "columns": _gq16_cols},
                        headers={"cookie": _gq16_ck} if _gq16_ck else {},
                    )
                if _gq16_resp.status_code >= 400:
                    _dbg("WARN: planner GQ16 Explorer call failed: %d", _gq16_resp.status_code)
                    # Fall through to pipeline handler
                else:
                    _gq16_explorer_rows = _gq16_resp.json().get("rows") or []

                    # Python-side date arithmetic — SOQL has no DATEDIFF()
                    from datetime import date as _gq16_date
                    from collections import defaultdict as _gq16_dd
                    _gq16_country_days: dict = _gq16_dd(list)
                    for _row in _gq16_explorer_rows:
                        _rc = _row.get("country") or "(unknown)"
                        _data = _row.get("data") or {}
                        _rd1 = _data.get(_gq16_sf1)
                        _rd2 = _data.get(_gq16_sf2)
                        if not (_rd1 and _rd2):
                            continue
                        try:
                            _diff = (_gq16_date.fromisoformat(str(_rd2)[:10])
                                     - _gq16_date.fromisoformat(str(_rd1)[:10])).days
                            if _diff >= 0:  # skip inverted/invalid date pairs
                                _gq16_country_days[_rc].append(_diff)
                        except Exception:
                            continue

                    if _gq16_country_days:
                        _gq16_rows = []
                        for _rc, _dl in sorted(_gq16_country_days.items()):
                            _gq16_rows.append({
                                "country":  _rc,
                                "avg_days": round(sum(_dl) / len(_dl)),
                                "n_sites":  len(_dl),
                                "min_days": min(_dl),
                                "max_days": max(_dl),
                            })
                        _gq16_rows.sort(key=lambda x: x["avg_days"])
                        _gq16_total  = sum(r["n_sites"] for r in _gq16_rows)
                        _gq16_overall = round(
                            sum(r["avg_days"] * r["n_sites"] for r in _gq16_rows) / _gq16_total
                        )
                        _dbg("Planner GQ16: %d countries, %d sites, overall avg %d days",
                             len(_gq16_rows), _gq16_total, _gq16_overall)
                        return {
                            "answer": (
                                f"<p>Average profiling duration "
                                f"(<em>{_gq16_lbl1}</em> → <em>{_gq16_lbl2}</em>) "
                                f"across <strong>{_gq16_total}</strong> sites in "
                                f"<strong>{len(_gq16_rows)}</strong> countr"
                                f"{'y' if len(_gq16_rows) == 1 else 'ies'}. "
                                f"Overall mean: <strong>{_gq16_overall}</strong> days. "
                                f"Table sorted by average (shortest first).</p>"
                            ),
                            "table": {
                                "columns": [
                                    {"key": "country",  "label": "Country"},
                                    {"key": "avg_days", "label": "Avg Days"},
                                    {"key": "n_sites",  "label": "Sites (n)"},
                                    {"key": "min_days", "label": "Min Days"},
                                    {"key": "max_days", "label": "Max Days"},
                                ],
                                "rows": _gq16_rows,
                            },
                        }
                    else:
                        # Both fields exist in Explorer but no paired date records — data not populated
                        _dbg("Planner GQ16: no paired records for %s / %s", _gq16_sf1, _gq16_sf2)
                        return {
                            "answer": (
                                f"<p>No sites found with both <em>{_gq16_lbl1}</em> "
                                f"(<code>{_gq16_sf1}</code>) and <em>{_gq16_lbl2}</em> "
                                f"(<code>{_gq16_sf2}</code>) dates recorded. "
                                f"These fields may not be populated for current sites.</p>"
                            )
                        }
        except Exception as _e:
            _dbg("WARN: planner GQ16 date-diff handler failed: %s", _e)

        # DR02: "qual upload + no geocoordinates" — cross-source join on local DB
        try:
            _dr02_qual_upload = bool(re.search(
                r"\bqual(?:ification)?\b.*\b(?:upload|form|submitted)\b"
                r"|\b(?:upload|form|submitted)\b.*\bqual(?:ification)?\b", s, re.I))
            _dr02_no_geo = bool(re.search(
                r"\bno\b.*\bgeo|\bwithout\b.*\bgeo|\bmissing\b.*\bgeo|\bdon.?t\b.*\bgeo"
                r"|\bno\b.*\bcoord|\bwithout\b.*\bcoord|\bmissing\b.*\bcoord|\bdon.?t\b.*\bcoord"
                r"|\bgeo.*\babsent|\bcoord.*\babsent|\bnot\s+yet\b.*\bgeo|\bnot\s+yet\b.*\bcoord",
                s, re.I))
            if _dr02_qual_upload and _dr02_no_geo and db:
                from app.models.site import Site as _SiteM
                from app.models.site_qual import SiteQual as _SQM
                from sqlalchemy import select as _sel
                _dr02_q = (
                    _sel(_SiteM.id, _SiteM.name, _SiteM.city, _SiteM.country,
                         _SiteM.salesforce_account_id, _SiteM.latitude, _SiteM.longitude)
                    .join(_SQM, _SQM.site_id == _SiteM.id)
                    .where((_SiteM.latitude.is_(None)) | (_SiteM.longitude.is_(None)))
                )
                _dr02_results = db.execute(_dr02_q).all()
                _dr02_rows = [
                    {"site": r.name, "city": r.city or "", "country": r.country or "",
                     "account_id": r.salesforce_account_id or "", "lat": r.latitude, "lng": r.longitude}
                    for r in _dr02_results
                ]
                _dr02_cols = [
                    {"key": "site", "label": "Site"},
                    {"key": "city", "label": "City"},
                    {"key": "country", "label": "Country"},
                ]
                _dr02_tbl = _normalize_table_for_ui({"columns": _dr02_cols, "rows": _dr02_rows})
                _dbg("Planner DR02 qual+no-geo: %d sites", len(_dr02_rows))
                return {
                    "answer": (
                        f"<p>Found <strong>{len(_dr02_rows)}</strong> site(s) with a qualification "
                        f"form uploaded but missing geocoordinates.</p>"
                    ),
                    "table": _dr02_tbl,
                }
        except Exception as _dr02_e:
            _dbg("WARN: DR02 qual+no-geo handler failed: %s", _dr02_e)

        # INTENCIÓN DIRECTA: SF pipeline/status field queries → Explorer API
        # Handles: CTS-validated, profiling complete, profiling uploaded, referral partner, meeting dates
        try:
            _pl_cts_valid    = bool(re.search(r"\bcts.validat|\bvalidat.*\bcts\b|\binnodia\s+cts\b", s, re.I))
            _pl_profiling_c  = bool(re.search(r"\bprofiling\b.*\bcomplete[d]?\b|\bcomplete[d]?\b.*\bprofiling\b|c_profiling_complete|profiling_complete", s, re.I))
            _pl_profiling_up = bool(re.search(r"\bprofiling.*\bupload|\bupload.*\bprofiling|\bprofiling.*\bdatabase\b|\bprofiling.*\bdb\b", s, re.I))
            _pl_referral_p   = bool(re.search(r"\breferral\b.*\bpartner|\bpartner\b.*\breferral|\breferral.clinical", s, re.I))
            _pl_no_meeting   = bool(re.search(r"\bno.meeting\b|\bno.meeting.date|\bmeeting.*\bnot.set|\bfirst.contact.*meeting|\bnot\s+had\s+a\s+meeting|\bwithout\s+(?:a\s+)?meeting", s, re.I))
            _pl_has_first_contact = bool(re.search(r"\bfirst\s+contact\b|\binitial\s+contact\b|\bcontacted\b.*\bno\s+meeting", s, re.I))
            _pl_quest_submitted = bool(re.search(r"\bquestionnaire\b.*\bsubmit|\bsubmit.*\bquestionnaire\b|\bform\b.*\bsubmit|\bsubmit.*\bform\b|\bquestionnaire\b.*\breceive[d]?\b|\breceive[d]?\b.*\bquestionnaire\b", s, re.I))
            _pl_prof_sent    = bool(re.search(r"\bprofiling.*\bsent\b|\bquestionnaire.*\bsent\b|\bform\b.*\bsent\b", s, re.I))
            _pl_not_received = bool(re.search(r"\bnot\s+(?:yet\s+)?received\b|\bwithout\s+(?:being\s+)?received\b|\bnot\s+(?:yet\s+)?(?:been\s+)?received\b", s, re.I))
            # Date qualifiers: "last year", "in 2024", "Q3 2024", "in the past year"
            _pl_date_last_year = bool(re.search(r"\blast\s+year\b|\bpast\s+year\b|\bin\s+the\s+last\s+12\s+months\b", s, re.I))
            _pl_date_year_m    = re.search(r"\bin\s+(20\d\d)\b|\b(20\d\d)\b(?!.*20\d\d)", s, re.I)
            _pl_date_year      = int(_pl_date_year_m.group(1) or _pl_date_year_m.group(2)) if _pl_date_year_m else None
            _pl_date_quarter_m = re.search(r"\bq([1-4])\s*(20\d\d)\b|\b(20\d\d)\s*q([1-4])\b", s, re.I)
            _pl_date_quarter   = None  # (year, start_date, end_date)
            if _pl_date_quarter_m:
                _q_qn  = int(_pl_date_quarter_m.group(1) or _pl_date_quarter_m.group(4))
                _q_yr  = int(_pl_date_quarter_m.group(2) or _pl_date_quarter_m.group(3))
                _q_starts = {1: (1,1), 2: (4,1), 3: (7,1), 4: (10,1)}
                _q_ends   = {1: (3,31), 2: (6,30), 3: (9,30), 4: (12,31)}
                _pl_date_quarter = (_q_yr, f"{_q_yr}-{_q_starts[_q_qn][0]:02d}-{_q_starts[_q_qn][1]:02d}",
                                    f"{_q_yr}-{_q_ends[_q_qn][0]:02d}-{_q_ends[_q_qn][1]:02d}")
            _pl_early_diag   = bool(re.search(r"\bearly.diagnos|\bscreening\b.*\bdiagnos|\bdiagnos.*\bscreening\b", s, re.I))
            _pl_qual_visit   = bool(re.search(r"\bqualif\w*\s+visit", s, re.I))
            _pl_qual_need    = bool(re.search(r"\bneed\b|\bstill\b|\bpending\b|\bawait|\brequir|\bnot\s+(?:yet\s+)?(?:had|done|complet)", s, re.I))
            # "profiling stage" = sites currently being profiled (profiling NOT complete, but have had contact/upload)
            # Note: exclude \bprofiling\s+sites?\b (too broad — catches F04 "profiling sites have submitted...")
            _pl_prof_stage   = bool(re.search(
                r"\bprofiling\s+stage\b|\bin\s+(?:the\s+)?profiling\b|\bcurrently\s+(?:in\s+)?profiling\b"
                r"|\bprofiling\s+pipeline\b|\bhow\s+many.*profiling\b", s, re.I))
            # "not CTS yet" modifier for profiling-complete queries
            _pl_not_cts      = bool(re.search(r"\bnot\s+(?:yet\s+)?cts\b|\baren.?t\s+(?:yet\s+)?cts\b|\bnot\s+yet\s+(?:a\s+)?cts\b|\bcts\b.*\bnot\s+yet\b", s, re.I))
            _pl_asks_sites   = bool(re.search(r"\bsite[s]?\b|\bcenter[s]?\b|\bwhich\b|\bshow\b|\bfind\b|\blist\b|\ball\b|\bmany\b|\bhow\b", s))
            # NOT modifier: "have NOT yet", "haven't", "not yet", "without"
            _pl_not = bool(re.search(r"\bnot\s+yet\b|\bhaven.?t\b|\bnot\s+complete|\bwithout\s+profiling\b|\bunprofiled\b", s, re.I))
            # Map detected intent → Explorer filter
            # "meeting date in Q3 2024" / "meeting date in 2024" — standalone date queries
            _pl_meeting_date_period = bool(
                re.search(r"\bmeeting\s+date\b|\bmeeting\s+(?:was|in|during|at)\b", s, re.I)
                and (_pl_date_quarter or _pl_date_year or _pl_date_last_year)
            )
            _pl_intent = (_pl_cts_valid or _pl_profiling_c or _pl_profiling_up
                          or _pl_referral_p or _pl_no_meeting or _pl_prof_sent
                          or _pl_prof_stage or _pl_meeting_date_period or _pl_quest_submitted
                          or _pl_qual_visit)
            # Note: _pl_early_diag intentionally excluded from _pl_intent — handled by qual handler
            # (maps to qual.3_8__autoantibodies_aab, not a pipeline field)
            if _pl_intent and _pl_asks_sites:
                _pl_rules: list = []
                _pl_cols: list = ["sf.Account.Name"]
                _pl_desc_parts: list = []
                if _pl_cts_valid:
                    _pl_rules.append({"field": "sf.Account.INNODIA_Clinical_Trial_Site__c", "operator": "equals", "value": True})
                    _pl_cols.append("sf.Account.INNODIA_Clinical_Trial_Site__c")
                    _pl_desc_parts.append("INNODIA CTS flag")
                if _pl_profiling_c:
                    # "NOT yet completed" → use is_null; "completed" → use is_not_null
                    _pc_op = "is_null" if _pl_not else "is_not_null"
                    _pl_rules.append({"field": "sf.C_Profiling_Complete__c", "operator": _pc_op})
                    _pl_cols.append("sf.C_Profiling_Complete__c")
                    _pl_desc_parts.append("profiling NOT complete" if _pl_not else "profiling complete")
                    # "aren't CTS yet" → add NOT-CTS filter alongside profiling complete
                    if _pl_not_cts:
                        _pl_rules.append({"field": "sf.Account.INNODIA_Clinical_Trial_Site__c", "operator": "equals", "value": False})
                        _pl_cols.append("sf.Account.INNODIA_Clinical_Trial_Site__c")
                        _pl_desc_parts.append("not yet CTS-validated")
                if _pl_prof_stage:
                    # "currently in profiling stage" = questionnaire sent but profiling NOT complete
                    _pl_rules.append({"field": "sf.C_Form_Questionnaire_sent__c", "operator": "is_not_null"})
                    _pl_rules.append({"field": "sf.C_Profiling_Complete__c", "operator": "is_null"})
                    _pl_cols.append("sf.C_Form_Questionnaire_sent__c")
                    _pl_cols.append("sf.C_Profiling_Complete__c")
                    _pl_desc_parts.append("currently in profiling stage")
                if _pl_profiling_up:
                    _pl_rules.append({"field": "sf.Profiling_form_uploaded_to_DB__c", "operator": "is_not_null"})
                    _pl_cols.append("sf.Profiling_form_uploaded_to_DB__c")
                    _pl_desc_parts.append("profiling form uploaded")
                if _pl_referral_p and sf:
                    # Direct SOQL — Referral_Outreach_Site_Non_CTS__c is on Account level
                    # and not reliably filterable via Explorer account_rules path.
                    _rp_soql = (
                        "SELECT Id, Name, ShippingCity, ShippingCountry "
                        "FROM Account "
                        "WHERE RecordType.DeveloperName='SubAccount' "
                        "AND C_Type__c='Clinical' "
                        "AND Referral_Outreach_Site_Non_CTS__c=true "
                        "AND (Account_Inactive__c=false OR Account_Inactive__c=null) "
                        "AND (Subaccount_Inactive__c=false OR Subaccount_Inactive__c=null) "
                        "ORDER BY ShippingCountry, Name"
                    )
                    _rp_raw = tool_salesforce_query(sf, _rp_soql)
                    _rp_recs = _rp_raw.get("records", []) if isinstance(_rp_raw, dict) else []
                    _rp_rows = [
                        {"account_id": r.get("Id", ""), "account_name": r.get("Name", ""),
                         "country": r.get("ShippingCountry", ""), "city": r.get("ShippingCity", "")}
                        for r in _rp_recs
                    ]
                    _rp_cols = [
                        {"key": "account_name", "label": "Site"},
                        {"key": "city", "label": "City"},
                        {"key": "country", "label": "Country"},
                    ]
                    _rp_tbl = _normalize_table_for_ui({"columns": _rp_cols, "rows": _rp_rows})
                    _dbg("Planner Referral/Clinical Partner: %d sites", len(_rp_rows))
                    return {
                        "answer": f"<p>Found <strong>{len(_rp_rows)}</strong> site(s) with Referral/Clinical Partner role.</p>",
                        "table": _rp_tbl,
                    }
                if _pl_no_meeting:
                    _pl_rules.append({"field": "sf.C_Meeting_Date__c", "operator": "is_null"})
                    _pl_cols.append("sf.C_Meeting_Date__c")
                    if _pl_has_first_contact:
                        _pl_rules.append({"field": "sf.C_Form_Questionnaire_sent__c", "operator": "is_not_null"})
                        _pl_cols.append("sf.C_Form_Questionnaire_sent__c")
                        _pl_desc_parts.append("first contact (form sent) but no meeting date")
                    else:
                        _pl_desc_parts.append("no meeting date")
                if _pl_prof_sent:
                    _pl_rules.append({"field": "sf.C_Form_Questionnaire_sent__c", "operator": "is_not_null"})
                    _pl_cols.append("sf.C_Form_Questionnaire_sent__c")
                    _pl_desc_parts.append("form/questionnaire sent")
                    if _pl_not_received:
                        _pl_rules.append({"field": "sf.Form_Questionnaire_received__c", "operator": "is_null"})
                        _pl_cols.append("sf.Form_Questionnaire_received__c")
                        _pl_desc_parts.append("not yet received")
                if _pl_quest_submitted:
                    # "questionnaire submitted" = Form_Questionnaire_received__c is_not_null
                    # (received by INNODIA = submitted by the site)
                    _pl_rules.append({"field": "sf.Form_Questionnaire_received__c", "operator": "is_not_null"})
                    _pl_cols.append("sf.Form_Questionnaire_received__c")
                    # If combined with "no meeting" / "not had a meeting", add meeting is_null
                    if _pl_no_meeting:
                        if "sf.C_Meeting_Date__c" not in _pl_cols:
                            _pl_rules.append({"field": "sf.C_Meeting_Date__c", "operator": "is_null"})
                            _pl_cols.append("sf.C_Meeting_Date__c")
                        _pl_desc_parts.append("questionnaire submitted but no meeting")
                    else:
                        _pl_desc_parts.append("questionnaire submitted")
                if _pl_qual_visit and _pl_qual_need and sf:
                    # "needs qualification visit" — cross-Opportunity RecordType query.
                    # C_Profiling_Complete__c lives on RT_Profiling_Opportunities,
                    # Qualification_Close_Date__c lives on RT_CTS_Accreditation.
                    # Single-SOQL approach produces false positives because both conditions
                    # are checked on the same Opportunity row. Fix: two separate queries + set difference.
                    _qv_country_clause = ""
                    _qv_country_label = ""
                    from app.utils.country_norms import resolve_countries as _rc_qv, iso2_to_sf_name as _iso2sf_qv
                    _qv_iso2s = _rc_qv(user_text)
                    if _qv_iso2s:
                        _qv_sf_names = [_iso2sf_qv(c) for c in _qv_iso2s]
                        _qv_in = ", ".join(f"'{n}'" for n in _qv_sf_names if n)
                        if _qv_in:
                            _qv_country_clause = f" AND Account.ShippingCountry IN ({_qv_in})"
                            _qv_country_label = " in " + "/".join(_qv_sf_names)
                    _qv_profiled = tool_salesforce_query(sf,
                        "SELECT AccountId, Account.Name, Account.ShippingCity, Account.ShippingCountry, "
                        "C_Profiling_Complete__c "
                        "FROM Opportunity "
                        "WHERE Type IN ('Profiling','CTS/CTU Profiling','CTS Profiling') "
                        "AND AccountId != null "
                        "AND C_Profiling_Complete__c != null"
                        + _qv_country_clause)
                    _qv_qualified = tool_salesforce_query(sf,
                        "SELECT AccountId "
                        "FROM Opportunity "
                        "WHERE Type IN ('Qualification','CTS/CTU Qualification','CTS Accreditation') "
                        "AND AccountId != null "
                        "AND Qualification_Close_Date__c != null"
                        + _qv_country_clause)
                    _qv_profiled_recs = (_qv_profiled.get("records") or []) if isinstance(_qv_profiled, dict) else []
                    _qv_qualified_ids = set()
                    for _qr in ((_qv_qualified.get("records") or []) if isinstance(_qv_qualified, dict) else []):
                        _qv_qualified_ids.add(_qr.get("AccountId"))
                    # Subtract: profiled accounts minus already-qualified accounts
                    _qv_seen: set = set()
                    _qv_rows: list = []
                    for _qr in _qv_profiled_recs:
                        _aid = _qr.get("AccountId", "")
                        if _aid in _qv_qualified_ids or _aid in _qv_seen:
                            continue
                        _qv_seen.add(_aid)
                        _acct = _qr.get("Account") or {}
                        _qv_rows.append({
                            "account_id": _aid,
                            "account_name": _acct.get("Name", ""),
                            "country": _acct.get("ShippingCountry", ""),
                            "city": _acct.get("ShippingCity", ""),
                            "data": {"sf.C_Profiling_Complete__c": _qr.get("C_Profiling_Complete__c", "")},
                        })
                    _qv_cols = [
                        {"key": "account_name", "label": "Site"},
                        {"key": "city", "label": "City"},
                        {"key": "country", "label": "Country"},
                        {"key": "sf.C_Profiling_Complete__c", "label": "Profiling Complete"},
                    ]
                    _qv_tbl = _normalize_table_for_ui({"columns": _qv_cols, "rows": _qv_rows})
                    _dbg("Planner qual-visit: %d profiled, %d qualified, %d need visit",
                         len(_qv_profiled_recs), len(_qv_qualified_ids), len(_qv_rows))
                    return {
                        "answer": (
                            f"<p>Found <strong>{len(_qv_rows)}</strong> site(s) with profiling complete "
                            f"but qualification visit not yet done{_qv_country_label}.</p>"
                        ),
                        "table": _qv_tbl,
                    }
                # RE6: "profiling complete in the last year" → date filter on Profiling_form_finalised_date__c
                if _pl_profiling_c and _pl_date_last_year and not _pl_not:
                    import datetime as _dt
                    _one_year_ago = (_dt.date.today() - _dt.timedelta(days=365)).isoformat()
                    _pl_rules.append({"field": "sf.Profiling_form_finalised_date__c", "operator": "gte", "value": _one_year_ago})
                    _pl_cols.append("sf.Profiling_form_finalised_date__c")
                    _pl_desc_parts.append(f"profiling finalised after {_one_year_ago}")
                elif _pl_profiling_c and _pl_date_year and not _pl_not:
                    _pl_rules.append({"field": "sf.Profiling_form_finalised_date__c", "operator": "gte", "value": f"{_pl_date_year}-01-01"})
                    _pl_rules.append({"field": "sf.Profiling_form_finalised_date__c", "operator": "lte", "value": f"{_pl_date_year}-12-31"})
                    _pl_cols.append("sf.Profiling_form_finalised_date__c")
                    _pl_desc_parts.append(f"profiling finalised in {_pl_date_year}")
                # RE8: "meeting date in Q3 2024" → date range on C_Meeting_Date__c
                if _pl_meeting_date_period and not _pl_no_meeting:
                    _pl_cols.append("sf.C_Meeting_Date__c")
                    if _pl_date_quarter:
                        _, _q_start, _q_end = _pl_date_quarter
                        _pl_rules.append({"field": "sf.C_Meeting_Date__c", "operator": "gte", "value": _q_start})
                        _pl_rules.append({"field": "sf.C_Meeting_Date__c", "operator": "lte", "value": _q_end})
                        _pl_desc_parts.append(f"meeting date {_q_start} – {_q_end}")
                    elif _pl_date_last_year:
                        import datetime as _dt
                        _one_year_ago = (_dt.date.today() - _dt.timedelta(days=365)).isoformat()
                        _pl_rules.append({"field": "sf.C_Meeting_Date__c", "operator": "gte", "value": _one_year_ago})
                        _pl_desc_parts.append(f"meeting date after {_one_year_ago}")
                    elif _pl_date_year:
                        _pl_rules.append({"field": "sf.C_Meeting_Date__c", "operator": "gte", "value": f"{_pl_date_year}-01-01"})
                        _pl_rules.append({"field": "sf.C_Meeting_Date__c", "operator": "lte", "value": f"{_pl_date_year}-12-31"})
                        _pl_desc_parts.append(f"meeting date in {_pl_date_year}")
                # Also add Stage 1/2 filter when detected alongside pipeline fields
                _pl_has_s1 = bool(re.search(r"\bstage\s*1\b", s))
                _pl_has_s2 = bool(re.search(r"\bstage\s*2\b", s))
                if _pl_has_s1:
                    _pl_rules.append({"field": "sf.C_Number_of_Stage1_Individuals_followed__c", "operator": "gt", "value": 0})
                    _pl_cols.append("sf.C_Number_of_Stage1_Individuals_followed__c")
                    _pl_desc_parts.append("Stage 1 patients")
                if _pl_has_s2:
                    _pl_rules.append({"field": "sf.C_Number_of_Stage2_Individuals_followed__c", "operator": "gt", "value": 0})
                    _pl_cols.append("sf.C_Number_of_Stage2_Individuals_followed__c")
                    _pl_desc_parts.append("Stage 2 patients")
                # Country filter (multi-country OR) — same pattern as qual handler
                _pl_countries: list = []
                _pl_seen_sf: set = set()
                for _cn in sorted(_COUNTRY_MAP.keys(), key=len, reverse=True):
                    _cm = (re.search(rf"\b{re.escape(_cn.upper())}\b", user_text) if len(_cn) <= 2
                           else re.search(rf"\b{re.escape(_cn)}\b", s))
                    if _cm:
                        _sf_pn, _iso_p = _COUNTRY_MAP[_cn]
                        if _sf_pn not in _pl_seen_sf:
                            _pl_countries.append((_sf_pn, _iso_p))
                            _pl_seen_sf.add(_sf_pn)
                _all_pl_rules: list = list(_pl_rules)
                if _pl_countries:
                    _pl_country_rule = (
                        {"logic": "OR", "rules": [
                            {"field": "site.country", "operator": "equals", "value": _iso_p}
                            for _, _iso_p in _pl_countries
                        ]} if len(_pl_countries) > 1
                        else {"field": "site.country", "operator": "equals", "value": _pl_countries[0][1]}
                    )
                    _all_pl_rules = [_pl_country_rule] + _pl_rules
                    _pl_desc_parts.append(f"in {'/'.join(n for n, _ in _pl_countries)}")
                _pl_filter = {"logic": "AND", "rules": _all_pl_rules}
                _pl_url = "http://127.0.0.1:8000/api/explorer/search"
                _pl_ck = request.headers.get("cookie") if request else None
                with httpx.Client(timeout=90.0) as _pc:
                    _pr = _pc.post(_pl_url, json={"filters": _pl_filter, "columns": _pl_cols},
                                   headers={"cookie": _pl_ck} if _pl_ck else {})
                rows_pl: list = []
                if _pr.status_code < 400:
                    for _er in (_pr.json().get("rows") or []):
                        _aid = _er.get("account_id", "")
                        if not _aid: continue
                        _ed = _er.get("data") or {}
                        _row = {"account_id": _aid, "account_name": _er.get("account_name", ""),
                                "country": _er.get("country", ""), "city": _er.get("city", "")}
                        for _ck in _pl_cols[1:]:
                            _row[_ck] = _ed.get(_ck)
                        rows_pl.append(_row)
                cols_pl = [{"key": "account_name", "label": "Site"},
                           {"key": "city", "label": "City"},
                           {"key": "country", "label": "Country"}]
                for _ck in _pl_cols[1:]:
                    cols_pl.append({"key": _ck, "label": _ck.split(".")[-1].replace("_", " ").title()})
                tbl_pl = _normalize_table_for_ui({"columns": cols_pl, "rows": rows_pl})
                _pl_desc = " + ".join(_pl_desc_parts) if _pl_desc_parts else "status filter"
                _dbg("Planner pipeline Explorer: %d sites (%s)", len(rows_pl), _pl_desc)
                return {
                    "answer": f"<p>Found <strong>{len(rows_pl)}</strong> site(s) matching: {_pl_desc}.</p>",
                    "table": tbl_pl,
                    "last_filters": _pl_filter,
                }
        except Exception as _e:
            _dbg("WARN: planner pipeline handler failed: %s", _e)

        # INTENCIÓN DIRECTA: "sites in [country]" → direct SF query or DB fallback (no internal HTTP)
        try:
            # _COUNTRY_MAP is defined at module level (see top of file)
            asks_country_sites = bool(re.search(
                r"\b(site[s]?|center[s]?|centre[s]?|centro[s]?|show|list|find|all|many|people|individuals?|patients?)\b", s
            ))
            # Skip if this is a followup query — Phase 2 merge handles "and also Germany" etc.
            if _is_followup_prefix:
                asks_country_sites = False
            # Skip if query is about Member institutions (not CTS sites) — let Claude route to members_search
            if re.search(r"\b(member[s]?|institution[s]?)\b", s):
                asks_country_sites = False
            # Skip if query is asking for nearest/closest — let nearest handler fire
            if re.search(r"\bnearest\b|\bclosest\b|\bnear(?:\s+to)?\b", s):
                asks_country_sites = False
            if asks_country_sites:
                # Match ALL countries in query (deduplicate by sf_name)
                _matched_countries: List[Tuple[str, str]] = []  # [(sf_name, iso), ...]
                _seen_sf_names: set = set()
                # Region expansion: region name → multiple ISO2 codes (must run before individual country scan)
                _REGION_EXPANSIONS: Dict[str, List[str]] = {
                    "nordic countries": ["SE", "NO", "DK", "FI", "IS"],
                    "northern europe":  ["SE", "NO", "DK", "FI"],
                    "scandinavia":      ["SE", "NO", "DK"],
                    "scandinavian":     ["SE", "NO", "DK"],
                    "nordic":           ["SE", "NO", "DK", "FI", "IS"],
                    "central europe":   ["DE", "AT", "CH", "CZ", "PL", "HU", "SK"],
                    "eastern europe":   ["PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI", "RS", "UA"],
                    "western europe":   ["FR", "BE", "NL", "LU", "GB", "IE", "PT", "ES"],
                    "southern europe":  ["IT", "ES", "PT", "GR", "HR", "SI", "MT"],
                    "dach region":      ["DE", "AT", "CH"],
                    "dach":             ["DE", "AT", "CH"],
                    "benelux":          ["BE", "NL", "LU"],
                    "iberian":          ["ES", "PT"],
                    "iberia":           ["ES", "PT"],
                    "baltic states":    ["EE", "LV", "LT"],
                    "baltic":           ["EE", "LV", "LT"],
                    "balkans":          ["HR", "BA", "RS", "ME", "MK", "AL", "SI", "BG", "GR", "RO"],
                }
                for _region_key in sorted(_REGION_EXPANSIONS.keys(), key=len, reverse=True):
                    if re.search(rf"\b{re.escape(_region_key)}\b", s, re.I):
                        for _r_iso2 in _REGION_EXPANSIONS[_region_key]:
                            _r_sf_name = _iso2_to_sf(_r_iso2)
                            if _r_sf_name not in _seen_sf_names:
                                _matched_countries.append((_r_sf_name, _r_iso2))
                                _seen_sf_names.add(_r_sf_name)
                for _cn in sorted(_COUNTRY_MAP.keys(), key=len, reverse=True):
                    _cm = (re.search(rf"\b{re.escape(_cn.upper())}\b", user_text) if len(_cn) <= 2
                           else re.search(rf"\b{re.escape(_cn)}\b", s))
                    if _cm:
                        _sf_name_c, _iso_c = _COUNTRY_MAP[_cn]
                        if _sf_name_c not in _seen_sf_names:
                            _matched_countries.append((_sf_name_c, _iso_c))
                            _seen_sf_names.add(_sf_name_c)
                # Back-compat aliases for single-country path
                _matched_country = _matched_countries[0][0] if _matched_countries else None
                _matched_sf_name = _matched_countries[0][0] if _matched_countries else None
                _matched_iso = _matched_countries[0][1] if _matched_countries else None
                if _matched_country and _matched_sf_name and not sf:
                    # SF session not available → fallback: query local sites table using ISO2 code
                    # (sites table stores ISO2 codes like "DE", not full names)
                    _fb_isos = [iso2.upper() for _, iso2 in _matched_countries]
                    _fb_placeholders = ", ".join(f":c{i}" for i in range(len(_fb_isos)))
                    db_country_q = text(
                        "SELECT s.salesforce_account_id, s.name, s.city, s.country "
                        "FROM public.sites s "
                        f"WHERE UPPER(s.country) IN ({_fb_placeholders}) "
                        "ORDER BY s.name ASC LIMIT 200"
                    )
                    db_country_rows_fb = db.execute(db_country_q, {f"c{i}": v for i, v in enumerate(_fb_isos)}).fetchall()
                    rows_country_fb = [
                        {"account_id": r[0] or "", "account_name": r[1] or "",
                         "city": r[2] or "", "country": r[3] or ""}
                        for r in db_country_rows_fb if r[0]
                    ]
                    cols_country_fb = [
                        {"key": "account_name", "label": "Site"},
                        {"key": "city", "label": "City"},
                        {"key": "country", "label": "Country"},
                    ]
                    tbl_country_fb = _normalize_table_for_ui({"columns": cols_country_fb, "rows": rows_country_fb})
                    _country_names_str_fb = ", ".join(n for n, _ in _matched_countries)
                    _dbg("Planner country DB fallback (no SF): %d sites in %s", len(rows_country_fb), _country_names_str_fb)
                    # Include last_filters (ISO2) so Phase 2 followup merge works correctly
                    if len(_matched_countries) > 1:
                        _country_lf = {
                            "logic": "AND",
                            "rules": [{"logic": "OR", "rules": [
                                {"field": "site.country", "operator": "equals", "value": iso2}
                                for _, iso2 in _matched_countries
                            ]}],
                        }
                    else:
                        _country_lf = {
                            "logic": "AND",
                            "rules": [{"field": "site.country", "operator": "equals", "value": _matched_iso}]
                        }
                    return {
                        "answer": f"<p>Found <strong>{len(rows_country_fb)}</strong> clinical site(s) in "
                                  f"<strong>{_country_names_str_fb}</strong> (from local database; "
                                  f"some metrics require a fresh Salesforce session).</p>",
                        "table": tbl_country_fb,
                        "last_filters": _country_lf,
                    }
                if _matched_country and _matched_sf_name and sf:
                    # Use Explorer API (site.country ISO2) instead of SOQL ShippingCountry filter.
                    # ShippingCountry SOQL filtering is unreliable for some countries (DE, GB, Nordic)
                    # because some SF accounts store ShippingCountry differently or it's blank.
                    # The Explorer's site.country filter uses the local DB (reliable ISO2 codes).
                    _expl_url = "http://127.0.0.1:8000/api/explorer/search"
                    _expl_ck = request.headers.get("cookie") if request else None
                    _expl_hdrs = {"cookie": _expl_ck} if _expl_ck else {}
                    if len(_matched_countries) > 1:
                        _expl_country_rules = [{"logic": "OR", "rules": [
                            {"field": "site.country", "operator": "equals", "value": _iso2c}
                            for _, _iso2c in _matched_countries
                        ]}]
                    else:
                        _expl_country_rules = [
                            {"field": "site.country", "operator": "equals", "value": _matched_iso}
                        ]
                    # Base columns: always include patient/stage counts
                    _expl_extra_cols: list = []
                    _expl_extra_row_keys: list = []
                    # Detect requested specific data → add corresponding columns
                    if re.search(r"\bprofil(?:ing)?\b", s, re.I):
                        _expl_extra_cols.append({"key": "sf.C_Profiling_Complete__c", "label": "Profiling Complete"})
                        _expl_extra_row_keys.append("sf.C_Profiling_Complete__c")
                    if re.search(r"\bmeeting\s+date\b", s, re.I):
                        _expl_extra_cols.append({"key": "sf.C_Meeting_Date__c", "label": "Meeting Date"})
                        _expl_extra_row_keys.append("sf.C_Meeting_Date__c")
                    if re.search(r"\bfirst\s+contact\b", s, re.I):
                        _expl_extra_cols.append({"key": "sf.C_Date_of_first_contact__c", "label": "First Contact"})
                        _expl_extra_row_keys.append("sf.C_Date_of_first_contact__c")
                    if re.search(r"\bcts\s+validat\b|\bcts-validat\b", s, re.I):
                        _expl_extra_cols.append({"key": "sf.Account.INNODIA_Clinical_Trial_Site__c", "label": "CTS"})
                        _expl_extra_row_keys.append("sf.Account.INNODIA_Clinical_Trial_Site__c")
                    # "how many stage 2 people in italy?" — Stage 1/2 already in base columns, but ensure sorted by them
                    _country_sort_by_s2 = bool(re.search(r"\bstage\s*2\b", s, re.I) and not re.search(r"\bstage\s*1\b", s, re.I))
                    _expl_payload = {
                        "filters": {"logic": "AND", "rules": _expl_country_rules},
                        "columns": [
                            "sf.C_Number_of_new_T1D_diagnosed_O_18__c",
                            "sf.C_Number_of_new_T1D_diagnosed_U_18__c",
                            "sf.C_Number_of_T1D_Patients_currently_O_18__c",
                            "sf.C_Number_of_T1D_Patients_currently_U_18__c",
                            "sf.C_Number_of_Stage1_Individuals_followed__c",
                            "sf.C_Number_of_Stage2_Individuals_followed__c",
                        ] + _expl_extra_row_keys,
                    }
                    with httpx.Client(timeout=90.0) as _c_cli:
                        _c_resp = _c_cli.post(_expl_url, json=_expl_payload, headers=_expl_hdrs)
                    rows_country = []
                    seen_cids: set = set()
                    if _c_resp.status_code < 400:
                        for _er in (_c_resp.json().get("rows") or []):
                            aid = _er.get("account_id", "")
                            if not aid or aid in seen_cids:
                                continue
                            seen_cids.add(aid)
                            _ed = _er.get("data") or {}
                            _base_row = {
                                "account_id": aid,
                                "account_name": _er.get("account_name", ""),
                                "country": _iso2_to_sf(_er.get("country", "")) or _er.get("country", ""),
                                "city": _er.get("city", ""),
                                "sf.C_Number_of_new_T1D_diagnosed_O_18__c": _ed.get("sf.C_Number_of_new_T1D_diagnosed_O_18__c"),
                                "sf.C_Number_of_new_T1D_diagnosed_U_18__c": _ed.get("sf.C_Number_of_new_T1D_diagnosed_U_18__c"),
                                "sf.C_Number_of_T1D_Patients_currently_O_18__c": _ed.get("sf.C_Number_of_T1D_Patients_currently_O_18__c"),
                                "sf.C_Number_of_T1D_Patients_currently_U_18__c": _ed.get("sf.C_Number_of_T1D_Patients_currently_U_18__c"),
                                "sf.C_Number_of_Stage1_Individuals_followed__c": _ed.get("sf.C_Number_of_Stage1_Individuals_followed__c"),
                                "sf.C_Number_of_Stage2_Individuals_followed__c": _ed.get("sf.C_Number_of_Stage2_Individuals_followed__c"),
                            }
                            # Add extra requested fields
                            for _xk in _expl_extra_row_keys:
                                _base_row[_xk] = _ed.get(_xk)
                            rows_country.append(_base_row)
                    if _country_sort_by_s2:
                        rows_country.sort(key=lambda r: -(r.get("sf.C_Number_of_Stage2_Individuals_followed__c") or 0))
                    else:
                        rows_country.sort(key=lambda r: r.get("account_name", ""))
                    cols_country = [
                        {"key": "account_name", "label": "Site"},
                        {"key": "city", "label": "City"},
                        {"key": "country", "label": "Country"},
                        {"key": "sf.C_Number_of_new_T1D_diagnosed_O_18__c", "label": "ND ≥18"},
                        {"key": "sf.C_Number_of_new_T1D_diagnosed_U_18__c", "label": "ND <18"},
                        {"key": "sf.C_Number_of_Stage1_Individuals_followed__c", "label": "Stage 1"},
                        {"key": "sf.C_Number_of_Stage2_Individuals_followed__c", "label": "Stage 2"},
                    ] + _expl_extra_cols
                    tbl_country = _normalize_table_for_ui({"columns": cols_country, "rows": rows_country})
                    _country_names_str = ", ".join(n for n, _ in _matched_countries)
                    _dbg("Planner country deterministic (SF query): %d sites in %s",
                         len(rows_country), _country_names_str)
                    # last_filters: OR for multi-country, single rule for one country
                    # Use ISO2 codes so Phase 2 followup merge can combine them correctly
                    if len(_matched_countries) > 1:
                        _country_sf_lf = {
                            "logic": "AND",
                            "rules": [{"logic": "OR", "rules": [
                                {"field": "site.country", "operator": "equals", "value": iso2}
                                for _, iso2 in _matched_countries
                            ]}],
                        }
                    else:
                        _country_sf_lf = {
                            "logic": "AND",
                            "rules": [{"field": "site.country", "operator": "equals", "value": _matched_iso}]
                        }
                    # Build a richer answer: top cities + key metrics summary
                    _city_counts: Dict[str, int] = {}
                    _s1_total = _s2_total = 0
                    for _r in rows_country:
                        _c = _r.get("city") or ""
                        if _c:
                            _city_counts[_c] = _city_counts.get(_c, 0) + 1
                        _s1_total += float(_r.get("sf.C_Number_of_Stage1_Individuals_followed__c") or 0)
                        _s2_total += float(_r.get("sf.C_Number_of_Stage2_Individuals_followed__c") or 0)
                    _top_cities = sorted(_city_counts.items(), key=lambda x: x[1], reverse=True)[:3]
                    _city_txt = ", ".join(f"{c} ({n})" for c, n in _top_cities) if _top_cities else ""
                    _ans_parts = [f"Found <strong>{len(rows_country)}</strong> clinical site(s) in <strong>{_country_names_str}</strong>."]
                    if _city_txt:
                        _ans_parts.append(f"Top cities: {_city_txt}.")
                    if _s1_total > 0 or _s2_total > 0:
                        _ans_parts.append(f"Stage 1: {int(_s1_total)}, Stage 2: {int(_s2_total)} individuals followed across all sites.")
                    return {
                        "answer": "<p>" + " ".join(_ans_parts) + "</p>",
                        "table": tbl_country,
                        "last_filters": _country_sf_lf,
                    }
        except Exception as _e:
            _dbg("WARN: planner country sites failed: %s", _e)

        # Detectar follow-ups de visualización (frases cortas con palabras clave)
        is_chart_followup = bool(
            # "show" alone is NOT a chart follow-up — "show me sites in X" is a data query
            re.search(r"\b(chart|graph|bar|pie|line|visuali[sz]e|display|plot)\b", s) and
            len(s.split()) < 15  # Frases cortas son follow-ups
        )
        # "show table" ya fue gestionado arriba; evita que caiga en follow-up genérico
        if re.search(r"\b(table|tabla|tabella|tabelle)\b", s):
            is_chart_followup = False

        # Determinista: "group countries by Opportunity Name" → matriz por actividad/país (sin exigir token "activity")
        try:
            if re.search(r"\bgroup\b[\s\S]*\bcountr", s) and re.search(r"opportunit\w*\s*name", s):
                tbl = tool_activity_country_matrix(sf, stacked=True)
                rows = tbl.get("rows") or []
                countries = sorted(set(r.get("country") for r in rows if r.get("country")))
                # build stacked data
                stacked_data = []
                activities = sorted(set(r.get("activity_name") for r in rows if r.get("activity_name")))
                for activity in activities:
                    rowd = {"activity_name": activity}
                    for c in countries:
                        rowd[c] = sum(r.get("sites",0) for r in rows if r.get("activity_name")==activity and r.get("country")==c)
                    stacked_data.append(rowd)
                viz = tool_render_chart("bar", stacked_data, "activity_name", countries[:8], {"title":"Countries per Activity (Stacked)"}, {})
                viz_cols = [{"key":"activity_name","label":"Activity"}] + [{"key": c, "label": _pretty_label(c)} for c in countries[:8]]
                viz_tbl = {"columns": viz_cols, "rows": stacked_data}
                return {"answer": f"<p>{len(activities)} activities with country breakdown.</p>", "table": viz_tbl, "visualization": viz.get("visualization")}
        except Exception as _e:
            _dbg("WARN: planner 'group countries by Opportunity Name' failed: %s", _e)
            pass

        # INTENCIÓN DIRECTA: "sites per country" + chart word → pie/bar chart
        try:
            _wants_chart_country = (
                re.search(r"\b(chart|bar|pie|graph|gr[aá]fico|barras|visuali[sz]e|plot)\b", s) and
                re.search(r"\b(sites?|sitios?|centers?|centres?|clinical|cl[ií]nico[s]?)\b", s) and
                re.search(r"\b(country|countries|pa[ií]s|pa[ií]ses|por\s+pa[ií]s|per\s+country|by\s+country)\b", s) and
                not re.search(r"\bhla\b|\bstage\b|\bnd\b|activit|assignment", s)
            )
            if _wants_chart_country and sf:
                soql_ctr = (
                    "SELECT ShippingCountry country, COUNT(Id) sites "
                    "FROM Account "
                    "WHERE RecordType.DeveloperName='SubAccount' "
                    "AND C_Type__c='Clinical' "
                    "AND ShippingCountry != null "
                    "AND (Account_Inactive__c=false OR Account_Inactive__c=null) "
                    "AND (Subaccount_Inactive__c=false OR Subaccount_Inactive__c=null) "
                    "GROUP BY ShippingCountry "
                    "ORDER BY COUNT(Id) DESC"
                )
                raw_ctr = tool_salesforce_query(sf, soql_ctr)
                recs_ctr = raw_ctr.get("records", []) if isinstance(raw_ctr, dict) else []
                rows_ctr = [
                    {"country": r.get("country", ""), "sites": int(r.get("sites") or r.get("expr0") or 0)}
                    for r in recs_ctr if r.get("country")
                ]
                if rows_ctr:
                    kind_ctr = "pie" if re.search(r"\b(pie|circular|dona|donut|ring)\b", s) else "bar"
                    total_ctr = sum(r["sites"] for r in rows_ctr)
                    tbl_ctr = _normalize_table_for_ui({
                        "columns": [{"key": "country", "label": "Country"}, {"key": "sites", "label": "Sites"}],
                        "rows": rows_ctr,
                    })
                    viz_ctr = tool_render_chart(kind_ctr, rows_ctr, "country", ["sites"],
                                               {"title": "Clinical Sites per Country"}, {})
                    _dbg("Planner sites-per-country chart (%s): %d countries", kind_ctr, len(rows_ctr))
                    return {
                        "answer": f"<p>{total_ctr} sites across {len(rows_ctr)} countries.</p>",
                        "table": tbl_ctr,
                        "visualization": viz_ctr.get("visualization"),
                    }
        except Exception as _e:
            _dbg("WARN: planner sites-per-country chart failed: %s", _e)

        # Si hay tabla previa y piden gráfico, NO usar el planner
        # También saltar si solo piden visualización sin especificar métrica/filtro
        chart_only_request = bool(
            re.search(r"\b(chart|graph|bar|pie|line)\b", s) and
            not re.search(r"\b(sites?|with|where|patient|stage|country|city|que|con)\b", s)
        )
        
        if is_chart_followup or chart_only_request or (has_table and re.search(r"\b(chart|graph|bar|pie)\b", s)):
            _dbg("Fast planner: chart follow-up detected, skipping planner to let LLM handle with render_chart")
            return None
        # Detectar group_by
        group_by = None
        if re.search(r"\b(por|per|by|cada)\s+(pa[ií]s|country)\b", s):
            group_by = "country"
        if re.search(r"\b(por|per|by|cada)\s+(ciudad|city)\b", s):
            group_by = group_by or "city"
        # Detectar conteo por grupo
        wants_count = bool(re.search(r"\bcount|cu[aá]ntos|how\s+many\b", s))

        # Caso específico: HLA typing en SF (sites in <country> with HLA typing)
        try:
            if re.search(r"\bhla\s*typing\b", s) and re.search(r"\bsites?\b", s) and re.search(r"\bin\s+[a-z]", s):
                # extrae país o lista
                tailm = re.search(r"\bin\s+([a-zA-ZÀ-ÿ'\- ,/&]+)\b", s)
                countries = []
                if tailm:
                    raw = tailm.group(1)
                    raw = re.sub(r"\b(and|y|e)\b", ",", raw, flags=re.I)
                    parts = [p.strip() for p in raw.split(",") if p.strip()]
                    countries = [p.title() for p in parts]
                if sf and countries:
                    vals = ", ".join([f"'{_sf_escape_value(c)}'" for c in countries])
                    soql = (
                        "SELECT Account.Id, Account.Name, Account.ShippingCountry, Account.ShippingCity "
                        "FROM Opportunity "
                        f"WHERE (Account.RecordType.DeveloperName='SubAccount') "
                        "AND Account.C_Type__c='Clinical' "
                        "AND (Account.Account_Inactive__c = false OR Account.Account_Inactive__c = null) "
                        "AND (Account.Subaccount_Inactive__c = false OR Account.Subaccount_Inactive__c = null) "
                        f"AND Account.ShippingCountry IN ({vals}) "
                        "AND C_Is_HLA_typing_performed__c = true "
                        "GROUP BY Account.Id, Account.Name, Account.ShippingCountry, Account.ShippingCity"
                    )
                    raw = tool_salesforce_query(sf, soql)
                    recs = raw.get("records", []) if isinstance(raw, dict) else []
                    rows = []
                    for r in recs:
                        acc = r.get("Account") or {}
                        rows.append({
                            "account_id": acc.get("Id"),
                            "site": acc.get("Name"),
                            "country": acc.get("ShippingCountry"),
                            "city": acc.get("ShippingCity"),
                        })
                    table = {"columns":[{"key":"account_id","label":"Account Id"},{"key":"site","label":"Account Name"},{"key":"country","label":"Country"},{"key":"city","label":"City"}], "rows": rows}
                    return {"answer": f"<p>{len(rows)} site(s) with HLA typing.</p>", "table": _normalize_table_for_ui(table)}
        except Exception as _e:
            _dbg("WARN: planner HLA sites failed: %s", _e)
            pass

        # Porcentaje HLA typing por país (ratio)
        try:
            if re.search(r"%\s*of\s*sites", s) and re.search(r"\bhla\s*typing\b", s) and re.search(r"\bper\s*country\b|\bby\s*country\b", s):
                if sf:
                    # 1) Total activos por país
                    soql_tot = (
                        "SELECT ShippingCountry country, COUNT(Id) total "
                        "FROM Account "
                        "WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
                        "AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
                        "AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) "
                        "AND ShippingCountry != null "
                        "GROUP BY ShippingCountry"
                    )
                    tot = tool_salesforce_query(sf, soql_tot).get("records", [])
                    totals = {r.get("country"): (r.get("expr0") or r.get("total") or 0) for r in tot}
                    # 2) Con HLA=true: obtener cuentas únicas desde Opportunity
                    soql_hla = (
                        "SELECT AccountId, Account.ShippingCountry "
                        "FROM Opportunity "
                        "WHERE C_Is_HLA_typing_performed__c = 'Yes' "
                        "AND Account.ShippingCountry != null"
                    )
                    recs = sf.query_all(soql_hla).get("records", [])  # no GROUP BY — dedup in Python
                    hla_counts: Dict[str, int] = {}
                    seen = set()
                    for r in recs:
                        aid = r.get("AccountId")
                        acc = r.get("Account") or {}
                        c = acc.get("ShippingCountry")
                        if not aid or not c or aid in seen:
                            continue
                        seen.add(aid)
                        hla_counts[c] = hla_counts.get(c, 0) + 1
                    rows = []
                    for country, total in totals.items():
                        has = hla_counts.get(country, 0)
                        ratio = float(has) / float(total) if float(total) else 0.0
                        rows.append({"country": country, "with_hla": has, "total": total, "ratio": round(ratio, 4)})
                    rows.sort(key=lambda x: x.get("ratio", 0), reverse=True)
                    table = {"columns":[{"key":"country","label":"Country"},{"key":"with_hla","label":"With HLA"},{"key":"total","label":"Total"},{"key":"ratio","label":"Ratio"}], "rows": rows}
                    viz = tool_render_chart("bar", rows, "country", ["with_hla"], {"title":"Sites with HLA typing per country"}, {})
                    return {"answer": f"<p>Computed HLA typing ratio for {len(rows)} countries.</p>", "table": table, "visualization": viz.get("visualization")}
        except Exception as _e:
            _dbg("WARN: planner HLA % per country failed: %s", _e)
            pass

        # HLA typing COUNT per country (bar chart) — "how many sites per country do HLA typing"
        try:
            if (re.search(r"\bhla\s*typing\b", s) and
                    re.search(r"per\s+country|by\s+country|por\s+pa[ií]s", s) and
                    not re.search(r"\bin\s+[a-z]", s)):
                if sf:
                    soql_hla2 = (
                        "SELECT AccountId, Account.ShippingCountry "
                        "FROM Opportunity "
                        "WHERE C_Is_HLA_typing_performed__c = 'Yes' "
                        "AND Account.ShippingCountry != null"
                    )
                    recs2 = sf.query_all(soql_hla2).get("records", [])  # no GROUP BY — dedup in Python
                    hla_ct: Dict[str, int] = {}
                    seen2: set = set()
                    for r in recs2:
                        aid = r.get("AccountId")
                        acc = r.get("Account") or {}
                        c = acc.get("ShippingCountry")
                        if not aid or not c or aid in seen2:
                            continue
                        seen2.add(aid)
                        hla_ct[c] = hla_ct.get(c, 0) + 1
                    rows_hla = sorted([{"country": c, "sites_with_hla": n} for c, n in hla_ct.items()],
                                      key=lambda x: x["sites_with_hla"], reverse=True)
                    tbl_hla = {"columns": [{"key": "country", "label": "Country"},
                                           {"key": "sites_with_hla", "label": "Sites with HLA Typing"}],
                               "rows": rows_hla}
                    viz_hla = tool_render_chart("bar", rows_hla, "country", ["sites_with_hla"],
                                               {"title": "Sites with HLA Typing per Country"}, {})
                    return {"answer": f"<p>{sum(r['sites_with_hla'] for r in rows_hla)} sites across {len(rows_hla)} countries perform HLA typing.</p>",
                            "table": tbl_hla, "visualization": viz_hla.get("visualization")}
        except Exception as _e:
            _dbg("WARN: planner HLA count per country chart failed: %s", _e)

        # Caso específico: "how many sites are in <country>" → conteo directo en SF (soporta múltiples países)
        m_in_country = re.search(r"\bhow\s+many\s+sites?\s+(are\s+)?in\s+([a-zA-ZÀ-ÿ'\- ,/&]+)\b", s)
        if m_in_country and sf:
            tail = (m_in_country.group(2) or "").strip().strip(" .")
            # Normaliza conectores: and, y, e, &, /, comas → comas; elimina 'in' suelto
            norm = re.sub(r"\b(in)\b", " ", tail, flags=re.I)
            norm = re.sub(r"\b(and|y|e)\b", ",", norm, flags=re.I)
            norm = norm.replace("/", ",").replace("&", ",")
            parts = [p.strip() for p in norm.split(",") if p.strip()]
            countries = []
            seen = set()
            for p in parts:
                c = p.title()
                if c and c.lower() not in seen:
                    seen.add(c.lower())
                    countries.append(c)
            if not countries:
                return None
            if len(countries) == 1:
                esc = _sf_escape_value(countries[0])
                soql = (
                    "SELECT COUNT(Id) total FROM Account "
                    "WHERE RecordType.DeveloperName = 'SubAccount' AND C_Type__c = 'Clinical' "
                    f"AND ShippingCountry = '{esc}' AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
                    f"AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
                )
                raw = tool_salesforce_query(sf, soql)
                recs = raw.get("records", []) if isinstance(raw, dict) else []
                val = (recs[0].get("expr0") or recs[0].get("total")) if recs else 0
                table = {"columns":[{"key":"total","label":"Total Sites"}], "rows":[{"total": val}]}
                ans = f"There are {val} active clinical trial sites in {countries[0]}."
                return {"answer": f"<p>{ans}</p>", "table": table}
            else:
                # Múltiples países: usar IN + GROUP BY
                esc_list = ", ".join([f"'{_sf_escape_value(c)}'" for c in countries])
                soql = (
                    "SELECT ShippingCountry country, COUNT(Id) total FROM Account "
                    "WHERE RecordType.DeveloperName = 'SubAccount' AND C_Type__c = 'Clinical' "
                    f"AND ShippingCountry IN ({esc_list}) AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
                    f"AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) "
                    "GROUP BY ShippingCountry ORDER BY COUNT(Id) DESC"
                )
                raw = tool_salesforce_query(sf, soql)
                recs = raw.get("records", []) if isinstance(raw, dict) else []
                rows = [{"country": r.get("country"), "count": r.get("expr0") or r.get("total") or r.get("COUNT") } for r in recs]
                table = {"columns": [{"key":"country","label":"Country"},{"key":"count","label":"Count"}], "rows": rows}
                total = sum(int(x.get("count") or 0) for x in rows)
                ans = f"Total {total} sites across {len(rows)} countries: " + ", ".join([f"{r['country']} {r['count']}" for r in rows[:2]]) + (", …" if len(rows) > 2 else ".")
                return {"answer": f"<p>{ans}</p>", "table": table}
        # Detectar top/order
        m_top = re.search(r"top\s*(\d{1,2})", s)
        top_n = int(m_top.group(1)) if m_top else (3 if group_by else 5)
        order = "desc" if re.search(r"(top|mayor|highest|largest|max)\b", s) else "asc" if re.search(r"(lowest|menor|min)\b", s) else "desc"
        
        # Detectar si es un conteo básico de sitios (sin métricas específicas)
        _has_validated_role = bool(re.search(r"\bvalidat\w*\s+role|\brole\s+validat|\bvalidat\w*\s+cts\b|\bvalidat\w*\s+cs\b", s, re.I))
        is_basic_site_count = (
            wants_count and
            re.search(r"\bsites?\b", s) and
            # 'have/has' are auxiliary verbs in English; don't treat them as filters
            not re.search(r"\b(with|having|where|que|con|que tienen)\b", s) and
            not re.search(r"\b(patient|stage|screening|hla|pharmacy|overnight|t1d|diagnosis|qualification)\b", s) and
            not _has_validated_role
        )
        
        # Sites per country with validated role → deterministic SOQL with role booleans
        if _has_validated_role and wants_count and sf:
            try:
                _vr_soql = (
                    "SELECT ShippingCountry, COUNT(Id) "
                    "FROM Account "
                    "WHERE RecordType.DeveloperName='SubAccount' "
                    "AND C_Type__c='Clinical' "
                    "AND ShippingCountry != null "
                    "AND (Account_Inactive__c=false OR Account_Inactive__c=null) "
                    "AND (Subaccount_Inactive__c=false OR Subaccount_Inactive__c=null) "
                    "AND (INNODIA_Clinical_Trial_Site__c=true "
                    "OR Clinical_Site_CS__c=true "
                    "OR Referral_Outreach_Site_Non_CTS__c=true "
                    "OR Elegible_for_DETECT_Site__c=true) "
                    "GROUP BY ShippingCountry "
                    "ORDER BY COUNT(Id) DESC"
                )
                _vr_raw = tool_salesforce_query(sf, _vr_soql)
                _vr_recs = _vr_raw.get("records", []) if isinstance(_vr_raw, dict) else []
                _vr_rows = [
                    {"country": r.get("ShippingCountry", ""), "count": int(r.get("expr0") or 0)}
                    for r in _vr_recs if r.get("ShippingCountry")
                ]
                _vr_total = sum(r["count"] for r in _vr_rows)
                _vr_tbl = _normalize_table_for_ui({
                    "columns": [{"key": "country", "label": "Country"}, {"key": "count", "label": "Sites"}],
                    "rows": _vr_rows,
                })
                _dbg("Planner sites-per-country with validated role: %d countries, %d sites", len(_vr_rows), _vr_total)
                return {
                    "answer": f"<p>Found <strong>{_vr_total}</strong> sites with at least one validated role across <strong>{len(_vr_rows)}</strong> countries.</p>",
                    "table": _vr_tbl,
                }
            except Exception as _e:
                _dbg("WARN: planner validated-role count failed: %s", _e)

        # Si es un conteo básico de sitios → resolver determinísticamente con Salesforce (no usar Postgres)
        if is_basic_site_count and sf:
            try:
                gb = group_by or "country"
                table = tool_group_count_sf(sf, [gb])
                # --- Auto‑summary: total sites, number of groups, top 2–3 ---
                rows = table.get("rows") or []
                # Detect columns
                label_col = "country" if gb == "country" else "city"
                if rows and label_col not in rows[0]:
                    # fallback: try case variations
                    for k in ("Country","City","site","label"):
                        if rows and isinstance(rows[0], dict) and k in rows[0]:
                            label_col = k
                            break
                value_col = None
                for cand in ("count","sites","value","patients"):
                    if rows and isinstance(rows[0], dict) and cand in rows[0]:
                        value_col = cand; break
                if not value_col and rows and isinstance(rows[0], dict):
                    # first numeric-looking
                    for k,v in rows[0].items():
                        try:
                            float(str(v).replace(",","")); value_col = k; break
                        except Exception:
                            pass
                # GQ02: threshold filter — "more than N" / "over N" / "at least N" / "N or more"
                _spc_thresh_m = re.search(
                    r"\bmore\s+than\s+(\d+)\b|\bover\s+(\d+)\b|\b>\s*(\d+)\b|\bat\s+least\s+(\d+)\b|\b(\d+)\s+or\s+more\b",
                    s, re.I
                )
                if _spc_thresh_m and value_col:
                    _spc_n = int(next(g for g in _spc_thresh_m.groups() if g is not None))
                    _is_at_least = bool(re.search(r"\bat\s+least\b|\bor\s+more\b", s, re.I))
                    rows = [
                        r for r in rows if isinstance(r, dict) and
                        (float(str(r.get(value_col) or 0).replace(",","")) >= _spc_n if _is_at_least
                         else float(str(r.get(value_col) or 0).replace(",","")) > _spc_n)
                    ]
                    table = dict(table, rows=rows)
                total = 0.0
                top_txt = ""
                try:
                    if value_col:
                        total = sum(float(str(r.get(value_col) or 0).replace(",","")) for r in rows if isinstance(r, dict))
                        top = sorted([r for r in rows if isinstance(r, dict)], key=lambda x: float(str(x.get(value_col) or 0).replace(",","")), reverse=True)[:3]
                        parts = [f"{str(t.get(label_col))} {str(t.get(value_col))}" for t in top if t.get(label_col) is not None]
                        if parts:
                            top_txt = " Top: " + ", ".join(parts[:2]) + (", " + parts[2] if len(parts) > 2 else "") + "."
                except Exception:
                    pass
                n_groups = len(rows)
                total_txt = int(total) if isinstance(total, float) and total.is_integer() else total
                ans = f"Total {total_txt} sites across {n_groups} {gb + ('ies' if gb=='city' else 's')}." + top_txt
                return {"answer": f"<p>{ans}</p>", "table": table}
            except Exception as e:
                _dbg("WARN: fast planner group_count_sf failed: %s", e)
                # si falla, seguimos con el flujo normal
        
        # Intentar encontrar una métrica/alias en el índice
        best = _top_matches(s, list(kindex.keys()), k=1)
        metric_alias = best[0] if best else None
        _dbg("Fast planner: group_by=%s, metric_alias=%s, top_n=%d", group_by, metric_alias, top_n)
        # Plan A: rank per group (si hay group_by y métrica)
        if group_by and metric_alias:
            # Evita colarse para intenciones de actividades (deben ir por el camino determinista con tabla+gráfico)
            if re.search(r"activ", s):
                pass
            else:
                try:
                    table = tool_rank_sites_by_group(db, sf, metric_alias, group_by, top_n, order)
                    ans = f"Ranked top {top_n} sites per {group_by} by '{metric_alias}'."
                    return {"answer": f"<p>{ans}</p>", "table": table}
                except Exception as e:
                    _dbg("WARN: fast planner rank_sites_by_group failed for metric=%s, group_by=%s: %s", metric_alias, group_by, e)
                    pass
        # Plan B: group count con filtro simple "with X" (SOLO para métricas de qualification, NO para sitios básicos)
        # IMPORTANT: HLA typing y Stage 1/2 deben resolverse en Salesforce, no en site_qual.
        sf_only_terms = re.search(r"\bhla\s*typing\b|\bstage\s*[12]\b", s, re.I) is not None
        if group_by and wants_count and not is_basic_site_count and not sf_only_terms:
            # buscar una posible condición exists: "with HLA typing", "with pharmacy"
            cond = None
            best2 = _top_matches(s, list(kindex.keys()), k=1)
            if best2:
                meta = kindex.get(best2[0], {})
                # SOLO usar group_count si hay un filtro de qualification específico
                if meta.get("source") == "site_qual" and best2[0]:
                    cond = {"key": meta.get("key"), "exists": True}
            # SOLO ejecutar si hay una condición de qualification (con filtro específico)
            if cond:
                try:
                    table = tool_group_count(db, [group_by], cond)
                    ans = f"Sites per {group_by} with {best2[0]}"
                    return {"answer": f"<p>{ans}</p>", "table": table}
                except Exception:
                    pass
        return None

    # Usar last_table del payload si está disponible (para follow-ups eficientes)
    last_table_from_payload = payload.last_table
    has_prior_table = last_table_from_payload is not None and len(last_table_from_payload.get("rows", [])) > 0
    
    # Visualización/cache de la última gráfica (usado para "show table" tras un chart)
    last_visualization: Optional[Dict[str, Any]] = None
    
    # Fallback: detectar si hay respuestas previas del asistente
    has_prior_assistant_response = False
    try:
        for msg in reversed(payload.messages[:-1]):
            if msg.role == "assistant":
                has_prior_assistant_response = True
                break
    except:
        pass
    
    # Para el fast planner, considerar ambas señales
    has_table_signal = has_prior_table or has_prior_assistant_response

    # Quick deterministic handling for activity summaries before planner (to avoid any misrouting)
    try:
        last_text_q = (payload.messages[-1].content or "").lower() if payload.messages else ""
        if last_text_q:
            # a) show table of activities by activity name → summary table (not detail)
            if re.search(r"\bshow\s+table\b", last_text_q) and re.search(r"activit", last_text_q) and re.search(r"by\s+activity\s+name", last_text_q):
                tbl = tool_activity_country_matrix(sf, stacked=False)
                return {"answer": "<p>Here is the table.</p>", "table": tbl}
            # b) summary of activities → summary table
            if re.search(r"\b(summary|resumen|overview)\b", last_text_q) and re.search(r"activit", last_text_q):
                tbl = tool_activity_country_matrix(sf, stacked=False)
                rows = tbl.get("rows") or []
                top = sorted(rows, key=lambda x: int(x.get("sites_total") or 0), reverse=True)[:3]
                top_txt = ", ".join([f"{t.get('activity_name')}: {t.get('sites_total')}" for t in top]) if rows else ""
                ans = f"{len(rows)} activities. Top: {top_txt}." if rows else "Activities summary."
                return {"answer": f"<p>{ans}</p>", "table": tbl}
            # c) table with assignments and count of sites per activity
            if re.search(r"assignments?", last_text_q) and re.search(r"\bcount\b", last_text_q) and re.search(r"sites?", last_text_q):
                t_assign = tool_activities_with_assignments_counts(sf)
                t_sites = tool_activity_country_matrix(sf, stacked=False)
                a_rows = t_assign.get("rows") or []
                s_rows = t_sites.get("rows") or []
                amap = {str(r.get("activity_id") or r.get("activity_name")): r for r in a_rows if r.get("activity_id") or r.get("activity_name")}
                out_rows = []
                for r in s_rows:
                    k = str(r.get("activity_id") or r.get("activity_name"))
                    a = amap.get(k) or {}
                    out_rows.append({
                        "activity_name": r.get("activity_name") or a.get("activity_name"),
                        "assignments": a.get("assignments") or 0,
                        "sites_total": r.get("sites_total") or 0,
                        "countries": r.get("countries") or a.get("countries"),
                        "activity_id": r.get("activity_id") or a.get("activity_id"),
                    })
                table_out = {"columns":[
                    {"key":"activity_name","label":"Activity"},
                    {"key":"assignments","label":"Assignments"},
                    {"key":"sites_total","label":"Sites"},
                    {"key":"countries","label":"Countries"},
                    {"key":"activity_id","label":"Activity Id"},
                ], "rows": out_rows}
                return {"answer": "<p>Assignments and site counts per activity.</p>", "table": table_out}
    except Exception as _qe:
        _dbg("WARN: quick summary pre-planner failed: %s", _qe)

    # ===== Option B: pre-compute plan BEFORE _try_planner to catch followup merges =====
    # This ensures "Germany too", "add Portugal", "what about Belgium?" etc. are handled
    # as followup merges even when they don't start with an explicit followup prefix.
    _qtxt_plan_pre = (payload.messages[-1].content or "") if payload.messages else ""
    _qplan_pre: Optional[Dict[str, Any]] = None
    _followup_handled = False
    if payload.last_filters:
        try:
            _qplan_pre = parse_query_plan(
                _qtxt_plan_pre, kindex,
                last_filters=payload.last_filters,
                last_table=last_table_from_payload,
            )
            if can_followup_merge(_qplan_pre, payload.last_filters):
                _merged_fg_pre = merge_with_last_filters(_qplan_pre, payload.last_filters)
                _dbg("PLAN Option-B followup merge (pre-planner): merged into %d rules", len(_merged_fg_pre.get("rules", [])))
                _merged_result_pre = tool_explorer_search(request, _merged_fg_pre, columns=None)
                _merged_rows_pre = (_merged_result_pre or {}).get("rows") or []
                _merged_tbl_pre = _normalize_table_for_ui({
                    "columns": _merged_result_pre.get("columns") or [],
                    "rows":    _merged_rows_pre,
                })
                if _merged_rows_pre:
                    _merged_ans_pre = f"Found <strong>{len(_merged_rows_pre)}</strong> site(s)"
                    if _qplan_pre.get("countries"):
                        _merged_ans_pre += f" now including {', '.join(_qplan_pre['countries'])}"
                else:
                    # 0 rows — return correct merged filter with empty table rather than
                    # letting Claude generate a different (potentially wrong) filter.
                    _merged_ans_pre = "No sites found with the combined filter"
                    if _qplan_pre.get("countries"):
                        _merged_ans_pre += f" (including {', '.join(_qplan_pre['countries'])})"
                _followup_handled = True
                return {
                    "answer":       f"<p>{_merged_ans_pre}.</p>",
                    "table":        _merged_tbl_pre,
                    "last_filters": _merged_fg_pre,
                }
        except Exception as _pre_e:
            _dbg("WARN: Option-B pre-planner followup failed: %s", _pre_e)

    planned = _try_planner(
        (payload.messages[-1].content if payload.messages else "") or "",
        has_table=has_table_signal,
        last_filters=payload.last_filters,
    )
    if planned:
        return planned

    # ===== Determinista: "show table" → devolver la última tabla sin llamar al modelo =====
    try:
        last_text = (payload.messages[-1].content or "").lower() if payload.messages else ""
        # Only match follow-up phrasings that refer to a prior result
        # ("show table", "as a table", "show as table", "tabla por favor"…).
        # Previously matched the bare word "table" anywhere, which caught fresh
        # requests like "give me a table of all CTS sites by country" and
        # trapped them in the follow-up path that has no data to return.
        asks_table = bool(re.search(
            r"(?:\bshow\s+(?:it\s+|that\s+|as\s+|the\s+|me\s+)?(?:as\s+)?(?:a\s+)?(?:table|tabla|tabella|tabelle))"
            r"|\b(?:as|en|como)\s+(?:a\s+|una\s+)?(?:table|tabla|tabella|tabelle)\b"
            r"|tabla\s*por\s*favor|mostrar\s+tabla",
            last_text,
        ))
        # Extra safety: only consume the query with the follow-up handler if
        # there is actually a prior table to reference. No prior state → let
        # the agentic loop take over and produce fresh data.
        has_prior_context = bool(payload.last_table and payload.last_table.get("rows"))
        if asks_table and has_prior_context:
            # 0) Petición explícita de resumen por actividad (ignorar last_table previo)
            if re.search(r"activit", last_text) and re.search(r"by\s+activity\s+name", last_text):
                tbl = tool_activity_country_matrix(sf, stacked=False)
                return {"answer": "<p>Here is the table.</p>", "table": tbl}
            # 0.a) Tabla de "activities by country" → usar agregación por país (no la pivot del gráfico)
            if re.search(r"activit", last_text) and (re.search(r"\bby\s+country\b", last_text) or re.search(r"\bcountry|pa[ií]s\b", last_text)):
                tbl = tool_activity_counts_by_country(sf)
                return {"answer": "<p>Here is the table.</p>", "table": tbl}
            # 1) Caso con last_table en payload → devolver sin más (si el front lo envía)
            if re.search(r"assignments?", last_text) and re.search(r"\bcount", last_text) and re.search(r"sites?", last_text):
                t_assign = tool_activities_with_assignments_counts(sf)
                t_sites = tool_activity_country_matrix(sf, stacked=False)
                a_rows = t_assign.get("rows") or []
                s_rows = t_sites.get("rows") or []
                amap = {str(r.get("activity_id") or r.get("activity_name")): r for r in a_rows if r.get("activity_id") or r.get("activity_name")}
                out_rows = []
                for r in s_rows:
                    k = str(r.get("activity_id") or r.get("activity_name"))
                    a = amap.get(k) or {}
                    out_rows.append({
                        "activity_name": r.get("activity_name") or a.get("activity_name"),
                        "assignments": a.get("assignments") or 0,
                        "sites_total": r.get("sites_total") or 0,
                        "countries": r.get("countries") or a.get("countries"),
                        "activity_id": r.get("activity_id") or a.get("activity_id"),
                    })
                return {"answer": "<p>Here is the table.</p>", "table": {"columns":[
                    {"key":"activity_name","label":"Activity"},
                    {"key":"assignments","label":"Assignments"},
                    {"key":"sites_total","label":"Sites"},
                    {"key":"countries","label":"Countries"},
                    {"key":"activity_id","label":"Activity Id"},
                ], "rows": out_rows}}
            # 1) Caso con last_table en payload → devolver sin más (si el front lo envía)
            if last_table_from_payload and len(last_table_from_payload.get("rows", [])) > 0:
                table_out = dict(last_table_from_payload)
                if table_out.get("columns"):
                    table_out["columns"] = [
                        {"key": (c.get("key") if isinstance(c, dict) else str(c)), "label": _pretty_label(c.get("key") if isinstance(c, dict) else str(c))}
                        for c in table_out.get("columns", [])
                    ]
                return {"answer": "<p>Here is the table.</p>", "table": table_out}
            # 2) Si existe un gráfico previo en este turno, reconstruye la tabla desde la visualización
            try:
                if last_visualization and isinstance(last_visualization, dict):
                    vx = last_visualization.get("xKey")
                    vys = last_visualization.get("yKeys") or []
                    vdata = last_visualization.get("data") or []
                    if vx and isinstance(vdata, list) and len(vdata) > 0:
                        cols = [{"key": str(vx), "label": _pretty_label(str(vx))}] + [
                            {"key": str(y), "label": _pretty_label(str(y))} for y in vys
                        ]
                        rows = []
                        for r in vdata:
                            rd = dict(r) if isinstance(r, dict) else {}
                            row = {str(vx): rd.get(vx)}
                            for y in vys:
                                row[str(y)] = rd.get(y)
                            rows.append(row)
                        return {"answer": "<p>Here is the table.</p>", "table": {"columns": cols, "rows": rows}}
            except Exception as _e:
                _dbg("WARN: show table from visualization failed: %s", _e)
            # 3) No hay last_table → reconstrucción determinista para casos comunes
            try:
                prev_txt = "\n".join([m.content.lower() for m in (payload.messages or [])[:-1] if m and m.content])
                if re.search(r"activit", prev_txt) and re.search(r"country|pa[ií]s", prev_txt):
                    tbl = tool_activity_counts_by_country(sf)
                    return {"answer": "<p>Here is the table.</p>", "table": tbl}
                if re.search(r"activit", prev_txt) and re.search(r"by\s+activity\s+name", prev_txt):
                    tbl = tool_activity_country_matrix(sf, stacked=False)
                    return {"answer": "<p>Here is the table.</p>", "table": tbl}
                # If previous request created a stacked activity chart, return the pivot used by that chart
                if re.search(r"stacked", prev_txt) and re.search(r"activit", prev_txt):
                    tbl_stack = tool_activity_country_matrix(sf, stacked=True)
                    rows = tbl_stack.get("rows") or []
                    countries = sorted(set(r.get("country") for r in rows if r.get("country")))
                    stacked_data = []
                    activities = sorted(set(r.get("activity_name") for r in rows if r.get("activity_name")))
                    for act in activities:
                        rowd = {"activity_name": act}
                        for c in countries:
                            rowd[c] = sum(r.get("sites",0) for r in rows if r.get("activity_name")==act and r.get("country")==c)
                        stacked_data.append(rowd)
                    cols = [{"key":"activity_name","label":"Activity"}] + [{"key": c, "label": _pretty_label(c)} for c in countries[:8]]
                    return {"answer": "<p>Here is the table.</p>", "table": {"columns": cols, "rows": stacked_data}}
            except Exception as _e:
                _dbg("WARN: show table fallback failed: %s", _e)
            # Último recurso
            return {"answer": "<p>No previous table available.</p>"}
    except Exception:
        pass

    # ===== Determinista (genérico): within X km + filtros SF y/o site_qual =====

    def _build_nearest_filters(text: str) -> Dict[str, Any]:
        """Parse common filter rules from free text for nearest_filtered_sites."""
        rules: list = []
        s_low = text.lower()
        # Stage 1/2 with optional comparator+value
        has_stage1 = bool(re.search(r"\bstage\s*1\b", s_low))
        has_stage2 = bool(re.search(r"\bstage\s*2\b", s_low))
        for stage_n in ("1", "2"):
            if not (has_stage1 if stage_n == "1" else has_stage2):
                continue
            m_sv = re.search(rf"\bstage\s*{stage_n}\b\s*(>=|<=|>|<)\s*(\d+)", s_low)
            if m_sv:
                rules.append({"field": f"sf.C_Number_of_Stage{stage_n}_Individuals_followed__c",
                               "operator": m_sv.group(1), "value": int(m_sv.group(2))})
            else:
                rules.append({"field": f"sf.C_Number_of_Stage{stage_n}_Individuals_followed__c",
                               "operator": ">", "value": 0})
        # OR logic for stages if both are mentioned together
        logic = "OR" if (has_stage1 and has_stage2 and not re.search(r"stage\s*1.{1,20}stage\s*2", s_low)) else "AND"
        # ND / newly diagnosed
        m_nd = re.search(r"\b(?:nd|newly[\s-]diagnosed)\b.{0,25}?(>=|<=|>|<)\s*(\d+)", s_low, re.I)
        if m_nd:
            rules.append({"field": "sf.C_Number_of_new_T1D_diagnosed_O_18__c",
                           "operator": m_nd.group(1), "value": int(m_nd.group(2))})
        elif re.search(r"\b(?:nd|newly[\s-]diagnosed)\b", s_low, re.I):
            rules.append({"field": "sf.C_Number_of_new_T1D_diagnosed_O_18__c",
                           "operator": ">", "value": 0})
        # Pharmacy on-site
        if re.search(r"\bpharmac", s_low):
            rules.append({"field": "qual.3_6__is_your_pharmacy_on_site_or_off_campus",
                           "operator": "equals", "value": "On-site"})
        # Overnight stay
        if re.search(r"\bovernight", s_low):
            rules.append({"field": "qual.3_5_2__overnight_stay", "operator": "equals", "value": "Yes"})
        # If mixing stage rules with other rules, outer logic must be AND
        if (has_stage1 or has_stage2) and len(rules) > (1 + int(has_stage1) + int(has_stage2) - 1):
            logic = "AND"
        return {"logic": logic, "rules": rules}

    # REMOVED: km-of-assignment handler — migrated to Claude agentic loop
    # ===== Determinista: within/in a X km OF [Assignment/Study Name] SITES/ACTIVITY =====
    # e.g. "sites within 100 km of Beta Preserve sites"
    #      "sites in a 120 km of any BetaPreserve activity"
    # Detects the "[Name] sites/activity" pattern (assignment/activity name, not a city) and builds
    # a deduplicated table of nearby INNODIA sites sorted by minimum driving distance.
    try:
        raise Exception("handler disabled")  # noqa: migrated to Claude agentic loop
        qtxt = (payload.messages[-1].content or "") if payload.messages else ""
        # Match "within N km ... of [Name] sites"  (EN: "within 100 km of Beta Preserve sites")
        # Also match Spanish: "a N km de ... [actividad/assignment/estudio] [Name]"
        #   e.g. "a 100 km de cualquier implicado en la actividad Safeguard"
        #        "sitios a 100 km de los participantes en el assignment Baricade"
        # P0: Multi-activity pattern — "N km of a [X] site and a [Y] site"
        # Must fire before P1 because P1's lazy quantifier stops at first "site" word.
        _m_multi_act = re.search(
            r"(?:within|in\s+a?\s*)(\d{2,4})\s*km\b.{0,20}\bof\s+"
            r"(?:an?\s+)([\w\s'\-]{2,40}?)\s+sites?\s*"
            r"(?:,\s*|\s+and\s+)(?:an?\s+)([\w\s'\-]{2,40}?)\s+sites?"
            r"(?:\s*(?:,\s*|\s+and\s+)(?:an?\s+)([\w\s'\-]{2,40}?)\s+sites?)?",
            qtxt, re.IGNORECASE,
        )
        if _m_multi_act:
            _ma_km = _m_multi_act.group(1)
            _ma_names = [g.strip() for g in _m_multi_act.groups()[1:] if g]
            _ma_combined = ", ".join(_ma_names)
            class _FakeMatchMultiAct:
                def __init__(self, km, name):
                    self._km = km; self._name = name
                def group(self, i):
                    return self._km if i == 1 else self._name
            m_assign_km = _FakeMatchMultiAct(_ma_km, _ma_combined)
        else:
            m_assign_km = re.search(
                r"(?:within|in\s+a?\s*)(\d{2,4})\s*km\b.{0,80}\bof\s+(?:any\s+)?([\w\s',\-]{2,60}?)\s+(?:study\s+|trial\s+|clinical\s+)?(?:sites?|activit(?:y|ies))\b",
                qtxt, re.IGNORECASE,
            )
        # Discard P1 match if extracted name is a stop word (e.g. "the", "a") — let P1c handle it
        _P1_STOP = {"the", "a", "an", "all", "any", "some", ""}
        if m_assign_km and m_assign_km.group(2).strip().lower() in _P1_STOP:
            m_assign_km = None
        if not m_assign_km:
            # P1b: inverted order — "[around|near|close to] NAME within/in a N km"
            # e.g. "find all sites around BetaPreserve within 100 km"
            #      "INNODIA sites near Safeguard activity within 80 km"
            _m1b = re.search(
                r"\b(?:around|near|close\s+to)\s+(?:any\s+)?([\w\s'\-]{2,40}?)\s*(?:activit(?:y|ies)|study|trial|sites?)?\s*(?:within|in\s+a?\s*)(\d{2,4})\s*km\b",
                qtxt, re.IGNORECASE,
            )
            if _m1b:
                # Clean up artifact: lazy name group may swallow keyword or "with" from "within"
                # Order matters: strip "with" (from "within") first, then trailing activity keyword
                _p1b_name = _m1b.group(1).strip()
                _p1b_name = re.sub(r"\s+with$", "", _p1b_name, flags=re.IGNORECASE).strip()
                _p1b_name = re.sub(r"\s+(?:activit(?:y|ies)|study|trial|sites?)\s*$", "", _p1b_name, flags=re.IGNORECASE).strip()
                class _FakeMatchP1b:
                    def __init__(self, km, name):
                        self._km = km; self._name = name
                    def group(self, i):
                        return self._km if i == 1 else self._name
                m_assign_km = _FakeMatchP1b(_m1b.group(2), _p1b_name)
        if not m_assign_km:
            # Spanish variant: "a N km de ... [actividad|assignment|estudio|trial] NAME"
            m_assign_km = re.search(
                r"\ba\s*(\d{2,4})\s*km\b.{0,100}\b(?:actividad|assignment|estudio|trial|activity)\s+([\w\s'\-]{2,40}?)\s*(?:\b(?:sites?|sitios?|centros?|implicados?|participantes?)\b|$)",
                qtxt, re.IGNORECASE,
            )
            if not m_assign_km:
                # Spanish variant 2: "... en la actividad NAME" + "a N km"
                m_assign_km_name = re.search(
                    r"\b(?:actividad|assignment|estudio|trial|activity)\s+([\w\s'\-]{2,40}?)\s*(?:\b(?:sites?|sitios?|centros?)\b|\?|$)",
                    qtxt, re.IGNORECASE,
                )
                m_assign_km_dist = re.search(r"\ba\s*(\d{2,4})\s*km\b", qtxt, re.IGNORECASE)
                if m_assign_km_name and m_assign_km_dist:
                    class _FakeMatch:
                        def __init__(self, km, name):
                            self._km = km; self._name = name
                        def group(self, i):
                            return self._km if i == 1 else self._name
                    m_assign_km = _FakeMatch(m_assign_km_dist.group(1), m_assign_km_name.group(1))
        if not m_assign_km:
            # Pattern P1c: "within N km of the sites assigned/belonging/in [NAME]"
            # e.g. "Find all sites within 150 km of the sites assigned to Safeguard"
            _m1c = re.search(
                r"(?:within|in\s+a?\s*)(\d{2,4})\s*km\b.{0,100}\bsites?\s+(?:assigned|belonging|in)\s+(?:to\s+)?(?:the\s+)?([\w\s'\-]{2,40}?)(?:\s*(?:\?|$|activity|assignment|study))",
                qtxt, re.IGNORECASE,
            )
            if _m1c:
                m_assign_km = _m1c
        if not m_assign_km:
            # Pattern P1d: "near/close to the sites involved/assigned in [NAME]" — no explicit km
            # e.g. "What CTS sites are near the sites involved in the Safeguard activity?"
            # Use default 100 km radius
            _m1d = re.search(
                r"\b(?:near|close\s+to|around)\s+(?:the\s+)?sites?\s+(?:involved|assigned|participating)\s+(?:to\s+|in\s+)?(?:the\s+)?([\w\s'\-]{2,40}?)\s*(?:activity|assignment|study|trial|\?|$)",
                qtxt, re.IGNORECASE,
            )
            if _m1d:
                class _FakeMatchP1d:
                    def __init__(self, name): self._name = name
                    def group(self, i): return "100" if i == 1 else self._name
                m_assign_km = _FakeMatchP1d(_m1d.group(1).strip())
        # Country adjective → ISO2 map for queries like "Beta Preserve italian sites"
        _COUNTRY_ADJ_MAP = {
            "italian": "IT", "spanish": "ES", "french": "FR", "german": "DE",
            "british": "GB", "dutch": "NL", "belgian": "BE", "swiss": "CH",
            "austrian": "AT", "swedish": "SE", "norwegian": "NO", "danish": "DK",
            "finnish": "FI", "polish": "PL", "czech": "CZ", "romanian": "RO",
            "hungarian": "HU", "portuguese": "PT", "greek": "GR", "turkish": "TR",
        }
        if m_assign_km and sf and db is not None:
            _km_limit = float(m_assign_km.group(1))
            _assign_name = m_assign_km.group(2).strip()
            # Remove leading articles properly (str.strip("the ") removes individual chars, not substring)
            for _art in ("the ", "a ", "an "):
                if _assign_name.lower().startswith(_art):
                    _assign_name = _assign_name[len(_art):].strip()
                    break
            # Un-CamelCase: "BetaPreserve" → "Beta Preserve" so it matches stored labels
            _assign_name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', _assign_name).strip()

            # Strip trailing country adjective and remember it as a reference-site filter
            # e.g. "Beta Preserve italian" → name="Beta Preserve", ref_country_iso="IT"
            _ref_country_iso: str = ""
            _words = _assign_name.split()
            if _words and _words[-1].lower() in _COUNTRY_ADJ_MAP:
                _ref_country_iso = _COUNTRY_ADJ_MAP[_words[-1].lower()]
                _assign_name = " ".join(_words[:-1]).strip()

            # Step 1 — find reference sites via explorer search (Assignment.Name filter)
            # NOTE: explorer_search only scans top-level rules for need_batch_extras, so nested
            # OR groups for sf.Assignment.Name are NOT handled correctly. For multi-activity
            # queries we make one search per activity and combine, which also gives us accurate
            # _ref_activity tags per site.
            _name_parts = [p.strip() for p in re.split(r'\s*[,/]\s*|\s+(?:or|and|y|o)\s+', _assign_name, flags=re.IGNORECASE) if p.strip()]
            _display_name = " / ".join(_name_parts) if len(_name_parts) > 1 else _assign_name
            _req_headers: Dict[str, str] = {}
            if request:
                _ck = request.headers.get("cookie")
                if _ck: _req_headers["cookie"] = _ck

            _ref_rows: list = []
            _ref_pts: Dict[str, tuple] = {}
            _ref_activity: Dict[str, str] = {}  # account_id → activity label(s)

            for _part in _name_parts:
                _part_rules: list = [{"field": "sf.Assignment.Name", "operator": "contains", "value": _part}]
                if _ref_country_iso:
                    _part_rules.append({"field": "site.country", "operator": "equals", "value": _ref_country_iso})
                _part_filter = {"logic": "AND", "rules": _part_rules}
                with httpx.Client(timeout=90.0) as _cli:
                    _pr = _cli.post(
                        f"http://127.0.0.1:8000{EXPLORER_SEARCH_PATH}",
                        # Request extra.AssignmentsNames so we can show specific activity names
                        json={"filters": _part_filter, "columns": ["extra.AssignmentsNames"]},
                        headers=_req_headers,
                    )
                if _pr.status_code >= 400:
                    raise Exception(f"explorer_search failed: {_pr.status_code}")
                _pj = _pr.json()
                for _row in (_pj.get("rows") or []):
                    _aid = str(_row.get("account_id", ""))
                    # Resolve specific assignment name(s) from extra.AssignmentsNames
                    # The field is stored as a "; "-separated string (e.g. "Fabulinus CTS Team - Part A; FABULINUS RP Network")
                    _rdata = _row.get("data", {})
                    _raw_acts = _rdata.get("extra.AssignmentsNames") or ""
                    _racts = [a.strip() for a in str(_raw_acts).split(";") if a.strip()] if _raw_acts else []
                    _matching = [a for a in _racts if _part.lower() in a.lower()]
                    _specific_label = "; ".join(_matching) if _matching else _part
                    if _aid in _ref_activity:
                        # Site appears in multiple activities — extend label
                        if _specific_label not in _ref_activity[_aid]:
                            _ref_activity[_aid] += f" / {_specific_label}"
                    else:
                        _ref_activity[_aid] = _specific_label
                        _ref_rows.append(_row)
                for _pt in (_pj.get("points") or []):
                    _paid = _pt.get("account_id")
                    if _paid and _pt.get("lat") and _pt.get("lng") and _paid not in _ref_pts:
                        _ref_pts[_paid] = (float(_pt["lat"]), float(_pt["lng"]))

            # Only proceed if we actually found sites with that assignment
            if _ref_rows:
                # Step 2 — call /api/explorer/search/nearby-multi with all base account IDs
                # This uses the full CTS account list (Opportunity records) as candidates,
                # which is much more complete than the local DB Site table.
                _base_ids = [str(r.get("account_id", "")) for r in _ref_rows if r.get("account_id")]
                _nm_resp: Optional[Dict[str, Any]] = None
                try:
                    with httpx.Client(timeout=180.0) as _nm_cli:
                        _nm_r = _nm_cli.post(
                            f"http://127.0.0.1:8000/api/explorer/search/nearby-multi",
                            json={
                                "base_account_ids": _base_ids,
                                "max_km": int(_km_limit),
                                "filters": {"logic": "AND", "rules": []},
                                "columns": [],
                            },
                            headers=_req_headers,
                        )
                    if _nm_r.status_code == 200:
                        _nm_resp = _nm_r.json()
                    else:
                        _dbg("nearby-multi returned %s — falling back to local DB", _nm_r.status_code)
                except Exception as _nm_e:
                    _dbg("nearby-multi call failed: %s — falling back to local DB", _nm_e)

                if _nm_resp is not None:
                    _nm_rows = _nm_resp.get("rows") or []
                    # Build neighbor coord lookup from nearby-multi points
                    _neighbor_coords: Dict[str, tuple] = {
                        str(_pt.get("account_id")): (float(_pt["lat"]), float(_pt["lng"]))
                        for _pt in (_nm_resp.get("points") or [])
                        if _pt.get("account_id") and _pt.get("lat") and _pt.get("lng")
                    }
                    # Build ref site name lookup (account_id → site name)
                    _ref_name_by_id: Dict[str, str] = {
                        str(_rr.get("account_id")): _rr.get("account_name", "")
                        for _rr in _ref_rows if _rr.get("account_id")
                    }

                    def _find_closest_ref_site(n_lat: float, n_lng: float):
                        """Return (ref_site_name, ref_activity_label) of geometrically closest ref site."""
                        _best_aid, _best_d = None, float("inf")
                        for _raid, (_rlat, _rlng) in _ref_pts.items():
                            _dlat = math.radians(_rlat - n_lat)
                            _dlng = math.radians(_rlng - n_lng)
                            _a = (math.sin(_dlat / 2) ** 2
                                  + math.cos(math.radians(n_lat)) * math.cos(math.radians(_rlat))
                                  * math.sin(_dlng / 2) ** 2)
                            _d = 2 * math.asin(math.sqrt(_a)) * 6371.0
                            if _d < _best_d:
                                _best_d, _best_aid = _d, _raid
                        if _best_aid:
                            return (
                                _ref_name_by_id.get(_best_aid, _display_name),
                                _ref_activity.get(_best_aid, _display_name),
                            )
                        return _display_name, _display_name

                    _flat_rows_out = []
                    for _nr in _nm_rows:
                        _naid = str(_nr.get("account_id", ""))
                        _ncoords = _neighbor_coords.get(_naid)
                        if _ncoords and _ref_pts:
                            _cref_name, _cref_act = _find_closest_ref_site(*_ncoords)
                        else:
                            _cref_name, _cref_act = _display_name, _display_name
                        _flat_rows_out.append({
                            "site": _nr.get("account_name", ""),
                            "account_id": _nr.get("account_id", ""),
                            "city": _nr.get("city", ""),
                            "country": _nr.get("country", ""),
                            "closest_ref": _cref_name,
                            "ref_activity": _cref_act,
                            "distance_km": round(_nr.get("data", {}).get("distance_km") or 0, 1),
                        })
                    _flat_rows_out.sort(key=lambda x: (
                        x.get("country") or "ZZ",
                        x.get("distance_km") if isinstance(x.get("distance_km"), (int, float)) else 99999,
                    ))
                    # Format distance: 0 → "<1"
                    for _r in _flat_rows_out:
                        if isinstance(_r.get("distance_km"), (int, float)) and _r["distance_km"] == 0:
                            _r["distance_km"] = "<1"
                    _dist_type = "driving"
                    _country_qualifier = f" in {_ref_country_iso}" if _ref_country_iso else ""
                    _cols = [
                        {"key": "site",         "label": "Site"},
                        {"key": "city",         "label": "City"},
                        {"key": "country",      "label": "Country"},
                        {"key": "distance_km",  "label": "Distance (km)"},
                        {"key": "closest_ref",  "label": "Nearest ref. site"},
                        {"key": "ref_activity", "label": "Ref. activity"},
                    ]
                    _ans = (
                        f"<p>Found <strong>{len(_flat_rows_out)} sites</strong> within "
                        f"<strong>{int(_km_limit)} km</strong> ({_dist_type}) of "
                        f"<strong>{_display_name}</strong> activity sites{_country_qualifier}. "
                        f"{len(_ref_rows)} {_display_name} reference sites excluded.</p>"
                    )
                    return {"answer": _ans, "table": {"columns": _cols, "rows": _flat_rows_out}}
                # nearby-multi failed → fall back to local DB approach
                from app.models.site import Site as _SiteM  # noqa
                _all_sites = db.query(
                    _SiteM.salesforce_account_id,
                    _SiteM.name,
                    _SiteM.city,
                    _SiteM.country,
                    _SiteM.latitude,
                    _SiteM.longitude,
                ).filter(_SiteM.salesforce_account_id.isnot(None)).all()
                def _hav2(la1, lo1, la2, lo2):
                    R = 6371.0
                    dlat = math.radians(la2 - la1)
                    dlon = math.radians(lo2 - lo1)
                    a = (math.sin(dlat/2)**2
                         + math.cos(math.radians(la1)) * math.cos(math.radians(la2))
                         * math.sin(dlon/2)**2)
                    return round(R * 2 * math.asin(math.sqrt(max(0.0, a))), 1)

                # Google Distance Matrix — sync version (reuses explorer cache)
                import hashlib as _hashlib
                _gkey = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
                try:
                    from app.routers.salesforce_explorer import (
                        _cache_get as _dmcache_get,
                        _cache_set as _dmcache_set,
                        MAX_DISTANCE_MATRIX_ELEMENTS as _DM_MAX_ELEMS,
                    )
                except Exception:
                    _dmcache_get = lambda k: None  # type: ignore
                    _dmcache_set = lambda k, v, ttl=86400: None  # type: ignore
                    _DM_MAX_ELEMS = 2000

                def _drive_matrix_sync(origin_ll: tuple, dests_ll: list) -> list:
                    """Sync Distance Matrix: returns list of Optional[float] km (driving).
                    Batches 25 destinations per request; results are cached 24 h.
                    Refuses to call Google if len(dests_ll) > MAX_DISTANCE_MATRIX_ELEMENTS
                    (one origin → one element per destination) to mirror the guard rail
                    applied in the Explorer endpoints."""
                    if not _gkey or not dests_ll:
                        return [None] * len(dests_ll)
                    if len(dests_ll) > _DM_MAX_ELEMS:
                        _dbg(
                            "DistanceMatrix guard rail tripped in Moby: %s destinations > cap %s — skipping API call",
                            len(dests_ll), _DM_MAX_ELEMS,
                        )
                        return [None] * len(dests_ll)
                    o_str = f"{origin_ll[0]},{origin_ll[1]}"
                    out: list = [None] * len(dests_ll)
                    with httpx.Client(timeout=30.0) as _dm_cli:
                        for _bi in range(0, len(dests_ll), 25):
                            _chunk = dests_ll[_bi:_bi + 25]
                            _d_str = "|".join(f"{la},{lo}" for la, lo in _chunk)
                            _ck = f"dm_km:{o_str}->{_hashlib.md5(_d_str.encode()).hexdigest()}"
                            _mx = _dmcache_get(_ck)
                            if _mx is None:
                                try:
                                    _r = _dm_cli.get(
                                        "https://maps.googleapis.com/maps/api/distancematrix/json",
                                        params={"origins": o_str, "destinations": _d_str,
                                                "mode": "driving", "key": _gkey},
                                    )
                                    _r.raise_for_status()
                                    _mx = _r.json()
                                    _dmcache_set(_ck, _mx, ttl=86400)
                                except Exception as _dme:
                                    _dbg("DistanceMatrix batch error: %s", _dme)
                                    continue
                            if (_mx or {}).get("status") != "OK":
                                _dbg("DistanceMatrix status=%s", (_mx or {}).get("status"))
                                continue
                            _elems = ((_mx.get("rows") or [{}])[0].get("elements") or [])
                            for _j in range(len(_chunk)):
                                _e = _elems[_j] if _j < len(_elems) else {}
                                if (_e or {}).get("status") == "OK":
                                    _dm = (_e.get("distance") or {}).get("value")
                                    if _dm is not None:
                                        out[_bi + _j] = round(float(_dm) / 1000.0, 1)
                    return out

                _use_drive = bool(_gkey)

                # Step 3 — for each reference site find nearby sites (parallelized)
                _ref_ids = {str(r.get("account_id", "")) for r in _ref_rows}

                def _process_one_ref(_ref_item):
                    """Process one reference site: find all INNODIA sites within _km_limit km."""
                    _rid           = str(_ref_item.get("account_id", ""))
                    _rname         = _ref_item.get("account_name", "")
                    _rcountry      = _ref_item.get("country", "")
                    _rcity         = _ref_item.get("city", "")
                    _rref_activity = _ref_activity.get(_rid, _display_name)
                    _rcoords       = _ref_pts.get(_rid)
                    if not _rcoords:
                        _rs = next((x for x in _all_sites if str(x.salesforce_account_id) == _rid), None)
                        if _rs and _rs.latitude and _rs.longitude:
                            _rcoords = (_rs.latitude, _rs.longitude)
                    if not _rcoords:
                        return [{"ref_site": _rname, "ref_country": _rcountry, "ref_city": _rcity,
                                 "ref_activity": _rref_activity,
                                 "nearby_site": "⚠ No coordinates", "nearby_city": "", "nearby_country": "", "distance_km": "—"}]
                    _rlat, _rlng = _rcoords
                    _candidates = [
                        _ns for _ns in _all_sites
                        if str(_ns.salesforce_account_id) not in _ref_ids
                        and _ns.latitude and _ns.longitude
                        and _hav2(_rlat, _rlng, float(_ns.latitude), float(_ns.longitude)) <= _km_limit * 2.0
                    ]
                    if _use_drive and _candidates:
                        _dest_ll = [(float(_ns.latitude), float(_ns.longitude)) for _ns in _candidates]
                        _drive_dists = _drive_matrix_sync((_rlat, _rlng), _dest_ll)
                        _nearby = [
                            (_drive_dists[_i], _candidates[_i].name or "", _candidates[_i].city or "",
                             _candidates[_i].country or "", str(_candidates[_i].salesforce_account_id or ""))
                            for _i in range(len(_candidates))
                            if _drive_dists[_i] is not None and _drive_dists[_i] <= _km_limit
                        ]
                    else:
                        _nearby = [
                            (_hav2(_rlat, _rlng, float(_ns.latitude), float(_ns.longitude)),
                             _ns.name or "", _ns.city or "", _ns.country or "",
                             str(_ns.salesforce_account_id or ""))
                            for _ns in _candidates
                        ]
                        _nearby = [x for x in _nearby if x[0] <= _km_limit]
                    _nearby.sort(key=lambda x: x[0])
                    if _nearby:
                        return [{"ref_site": _rname, "ref_country": _rcountry, "ref_city": _rcity,
                                 "ref_activity": _rref_activity,
                                 "nearby_site": _n, "nearby_city": _nc, "nearby_country": _nco,
                                 "nearby_account_id": _naid, "distance_km": _d}
                                for _d, _n, _nc, _nco, _naid in _nearby[:15]]
                    return [{"ref_site": _rname, "ref_country": _rcountry, "ref_city": _rcity,
                             "ref_activity": _rref_activity,
                             "nearby_site": f"— None within {int(_km_limit)} km —",
                             "nearby_city": "", "nearby_country": "", "nearby_account_id": "", "distance_km": "—"}]

                from concurrent.futures import ThreadPoolExecutor as _TPEX
                _flat_rows: list = []
                with _TPEX(max_workers=min(len(_ref_rows), 12)) as _pool:
                    for _ref_result in _pool.map(_process_one_ref, _ref_rows):
                        _flat_rows.extend(_ref_result)

                # Deduplicate: each INNODIA site appears once, keeping minimum distance
                # and the name of the closest reference site (so users see WHY it qualifies)
                _best: dict = {}
                for _r in _flat_rows:
                    _nk = _r.get("nearby_site", "")
                    _nd = _r.get("distance_km")
                    if not _nk or "None" in _nk or "⚠" in _nk:
                        continue
                    if _nk not in _best or (
                        isinstance(_nd, (int, float))
                        and isinstance(_best[_nk]["distance_km"], (int, float))
                        and _nd < _best[_nk]["distance_km"]
                    ):
                        _best[_nk] = {
                            "site": _nk,
                            "account_id": _r.get("nearby_account_id", ""),
                            "city": _r.get("nearby_city", ""),
                            "country": _r.get("nearby_country", ""),
                            "closest_ref": _r.get("ref_site", ""),
                            "ref_activity": _r.get("ref_activity", _display_name),
                            "distance_km": _nd,
                        }
                _flat_rows_out = sorted(
                    _best.values(),
                    key=lambda x: (
                        x.get("country") or "ZZ",   # primary: country alphabetically
                        x.get("distance_km") if isinstance(x.get("distance_km"), (int, float)) else 99999,
                    ),
                )

                # Round distances and format 0 → "<1"
                for _r in _flat_rows_out:
                    if isinstance(_r.get("distance_km"), (int, float)):
                        _r["distance_km"] = round(_r["distance_km"], 1)
                        if _r["distance_km"] == 0:
                            _r["distance_km"] = "<1"
                _cols = [
                    {"key": "site",        "label": "Site"},
                    {"key": "city",        "label": "City"},
                    {"key": "country",     "label": "Country"},
                    {"key": "distance_km", "label": "Distance (km)"},
                    {"key": "closest_ref", "label": "Nearest ref. site"},
                ]
                if len(_name_parts) > 1:
                    _cols.append({"key": "ref_activity", "label": "Ref. activity"})
                _dist_type = "driving" if _use_drive else "straight-line"
                _country_qualifier = f" in {_ref_country_iso}" if _ref_country_iso else ""
                _ans = (
                    f"<p>Found <strong>{len(_flat_rows_out)} sites</strong> within "
                    f"<strong>{int(_km_limit)} km</strong> ({_dist_type}) of "
                    f"<strong>{_display_name}</strong> activity sites{_country_qualifier}. "
                    f"{len(_ref_rows)} {_display_name} reference sites excluded."
                    f"</p>"
                )
                return {"answer": _ans, "table": {"columns": _cols, "rows": _flat_rows_out}}
            # _ref_rows empty → fall through to city-based handler
    except Exception as _e_assign_km:
        _dbg("WARN: within-km-of-assignment handler: %s", _e_assign_km)

    # REMOVED: generic within-km handler — migrated to Claude agentic loop
    try:
        raise Exception("handler disabled")  # noqa: migrated to Claude agentic loop
        qtxt = (payload.messages[-1].content or "") if payload.messages else ""
        s = qtxt.lower()
        m_dist = re.search(r"(?:within|radius|radio|distancia|a)\s*(\d{2,4})\s*km\b", s)
        # city or reference site: "of <city>" or "of the clinical site of <city>"
        m_city = re.search(r"\b(?:of|from|de|desde)\s+(?:the\s+clinical\s+site\s+of\s+)?([a-zA-ZÀ-ÿ'\-,& ]{2,}?)(?:\s+(?:with|con)\b|\s*$|\s*\?)", s)
        # Common English/Spanish words that look like a city after "of/from" but aren't
        _NON_CITY_WORDS = {"any", "some", "all", "each", "every", "the", "these", "those",
                           "any", "un", "una", "los", "las", "any", "sites", "site"}
        _city_candidate = m_city.group(1).strip().strip(". ") if m_city else None
        if _city_candidate and _city_candidate.lower().split()[0] in _NON_CITY_WORDS:
            _city_candidate = None  # "of any …" / "of some …" — not a real city

        # Multi-city extraction: split on ", " / " and " / " & "
        _mc_cities: list = []
        if _city_candidate:
            _mc_raw = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+|\s*&\s*", _city_candidate)
            _mc_cities = [c.strip() for c in _mc_raw if c.strip() and c.strip().lower() not in _NON_CITY_WORDS]

        if m_dist and _mc_cities and sf:
            max_km = float(m_dist.group(1))

            # Multi-city: run tool_nearest_filtered_sites for each city and union (OR semantics)
            # For dedup: keep min distance per site, track nearest origin city
            _filters_p1 = _build_nearest_filters(qtxt)
            _mc_best: dict = {}  # account_id → row dict (with distance_km and nearest_origin)
            _mc_geocoded: list = []
            _mc_any_ok = False

            for _mc_idx, _mc_city in enumerate(_mc_cities):
                try:
                    _out_mc = tool_nearest_filtered_sites(
                        request, location=_mc_city, filters=_filters_p1,
                        max_km=max_km, top_n=50, db=db,
                    )
                    if _out_mc.get("error"):
                        _dbg("WARN: nearest_filtered_sites multi-city error for '%s': %s", _mc_city, _out_mc["error"])
                        continue
                    _mc_meta = _out_mc.get("meta") or {}
                    _mc_geo_label = _mc_meta.get("geocoded") or _mc_city
                    # Use short city name (user-typed) for display, full geocoded for answer text
                    _mc_short_label = _mc_city.title()
                    _mc_geocoded.append(_mc_geo_label)
                    for _mc_row in (_out_mc.get("rows") or []):
                        _mc_aid = _mc_row.get("account_id", "")
                        _mc_dist = _mc_row.get("distance_km", 99999)
                        if _mc_aid not in _mc_best or _mc_dist < _mc_best[_mc_aid].get("distance_km", 99999):
                            _mc_row_copy = dict(_mc_row)
                            _mc_row_copy["nearest_origin"] = _mc_short_label
                            _mc_best[_mc_aid] = _mc_row_copy
                    _mc_any_ok = True
                except Exception as _mc_e:
                    _dbg("WARN: nearest_filtered_sites multi-city failed for '%s': %s", _mc_city, _mc_e)

            if _mc_any_ok and _mc_best:
                _mc_rows_sorted = sorted(_mc_best.values(), key=lambda r: r.get("distance_km", 99999))
                # Build columns — clean presentation, no account_id / no duplicate account_name
                _mc_wanted_keys = {"account_name", "city", "country", "distance_km", "nearest_origin"}
                _mc_rows_clean = [
                    {k: v for k, v in r.items() if k in _mc_wanted_keys}
                    for r in _mc_rows_sorted
                ]
                _mc_cols_base = [
                    {"key": "account_name", "label": "Site"},
                    {"key": "city",        "label": "City"},
                    {"key": "country",     "label": "Country"},
                    {"key": "distance_km", "label": "Distance (km)"},
                ]
                if len(_mc_cities) > 1:
                    _mc_cols_base.append({"key": "nearest_origin", "label": "Nearest to"})
                _mc_short_list = [c.title() for c in _mc_cities]
                _mc_label_short = ", ".join(_mc_short_list[:-1]) + " and " + _mc_short_list[-1] if len(_mc_short_list) > 1 else _mc_short_list[0] if _mc_short_list else ", ".join(_mc_cities)
                _mc_ans = (
                    f"<p>Found <strong>{len(_mc_rows_clean)} sites</strong> within "
                    f"<strong>{int(max_km)} km</strong> of "
                    f"<strong>{_mc_label_short}</strong>. Straight-line distances.</p>"
                )
                return {"answer": _mc_ans, "table": {"columns": _mc_cols_base, "rows": _mc_rows_clean}}
            elif _mc_any_ok:
                # Queries succeeded but 0 results
                pass  # fall through to old drive-km pipeline
            # If all cities failed geocoding, fall through too
            # 1) Base account in that city (clinical subaccounts, active)
            esc_city = _sf_escape_value(city)
            soql_city = (
                "SELECT Id FROM Account "
                "WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
                "AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
                "AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) "
                f"AND ShippingCity = '{esc_city}' LIMIT 1"
            )
            raw = tool_salesforce_query(sf, soql_city)
            base_id = (raw.get("records") or [{}])[0].get("Id") if isinstance(raw, dict) and raw.get("records") else None
            if base_id:
                # 2) Neighboring sites (Explorer)
                ex = tool_explorer_within_drive_km(request, base_account_id=base_id, max_km=max_km, filters=None, columns=None)
                rows_ex = ex.get("rows") or []
                if not rows_ex:
                    return {"answer": f"<p>No nearby sites (≤ {int(max_km)} km of {city}).</p>"}
                acc_ids = list({str(r.get("account_id") or r.get("sf.Account.Id") or r.get("Account.Id")) for r in rows_ex if isinstance(r, dict)})

                # 3) Build SF Opportunity filters from text
                opp_conds = []
                # Patients per year > N → current U18 + O18
                m_pat = re.search(r"(>=|<=|>|<|at\s+least|al\s+menos|como\s+m[ií]nimo|m[aá]s\s+de|menos\s+de)?\s*(\d{2,5})\s*(patients?|pacientes)\b", s)
                if m_pat:
                    rawop = (m_pat.group(1) or ">=").lower()
                    op = ">=" if re.search(r"at\s+least|al\s+menos|como\s+m[ií]nimo|>=", rawop) else ">" if re.search(r"m[aá]s\s+de|>", rawop) else "<=" if "<=" in rawop or "menos de" in rawop else "<" if "<" in rawop else ">="
                    val = int(m_pat.group(2))
                    expr = "(C_Number_of_T1D_Patients_currently_U_18__c + C_Number_of_T1D_Patients_currently_O_18__c)"
                    opp_conds.append(f"{expr} {op} {val}")
                # Newly diagnosed under 18
                if re.search(r"new(ly)?\s*diagnos(ed)?\b.*\b(under\s*18|<\s*18|u\s*18)", s):
                    opp_conds.append("C_Number_of_new_T1D_diagnosed_U_18__c > 0")
                # Stage filters
                m_stage = re.search(r"stage\s*(1|2)\b\s*(>=|<=|>|<)?\s*(\d+)?", s)
                if m_stage:
                    field = "C_Number_of_Stage1_Individuals_followed__c" if m_stage.group(1) == "1" else "C_Number_of_Stage2_Individuals_followed__c"
                    if m_stage.group(3):
                        op = m_stage.group(2) or ">="
                        opp_conds.append(f"{field} {op} {int(m_stage.group(3))}")
                    else:
                        opp_conds.append(f"{field} > 0")
                # PI Phase I involvement
                if re.search(r"pi\b.*\bphase\s*i\b|phase\s*i\b.*pi", s):
                    # Use numeric count if present; else boolean Phase I Type1
                    opp_conds.append("(C_Nbr_of_studies_PI_is_involved_PI_Sub_I__c > 0 OR C_Phase_I_Type1__c = true)")

                # 4) Execute Opp filter if any
                allowed_sf: set[str] = set(acc_ids)
                if opp_conds:
                    ids_clause = ",".join([f"'{i}'" for i in acc_ids])
                    where = " AND ".join(["AccountId IN (" + ids_clause + ")"] + opp_conds)
                    soql = f"SELECT AccountId FROM Opportunity WHERE {where} LIMIT 5000"
                    raw2 = tool_salesforce_query(sf, soql)
                    recs2 = raw2.get("records", []) if isinstance(raw2, dict) else []
                    allowed_sf = {r.get("AccountId") for r in recs2 if r.get("AccountId")}

                # 5) site_qual filters (generic EXISTS and numeric)
                allowed_sq: set[str] = set(acc_ids)
                # Detect simple "with/without <feature>" and numeric comparisons like ">= N <alias>"
                idx = _build_knowledge_index(db)["index"]
                # pick up to 2 best site_qual matches from the text
                aliases = [a for a, m in idx.items() if (m or {}).get("source") == "site_qual"]
                best_aliases = _top_matches(s, aliases, k=2)
                sq_conds = []  # list of tuples (key, op, value or None)
                for a in best_aliases:
                    meta = idx.get(a) or {}
                    k = meta.get("key")
                    if not k:
                        continue
                    if re.search(r"\b(with|con)\b\s+\b" + re.escape(a) + r"\b", s):
                        sq_conds.append((k, "exists", None))
                    elif re.search(r"\b(without|sin)\b\s+\b" + re.escape(a) + r"\b", s):
                        sq_conds.append((k, "not_exists", None))
                # numeric comparator like ">= 5 <alias>"
                m_num = re.findall(r"(>=|<=|>|<|=)\s*(\d+(?:[\.,]\d+)?)\s+([a-zA-Z0-9_\- ]{3,})", s)
                for op, val, tail in m_num:
                    # find alias match for tail
                    b = _top_matches(tail, aliases, k=1)
                    if b:
                        mk = (idx.get(b[0]) or {}).get("key")
                        if mk:
                            sq_conds.append((mk, op, float(val.replace(",","."))))
                # Apply sq filters if any
                if sq_conds:
                    where_parts = ["s.salesforce_account_id = ANY(:ids)"]
                    params = {"ids": acc_ids}
                    for i,(k,op,v) in enumerate(sq_conds):
                        if op == "exists":
                            where_parts.append(f"(sq.data ? :k{i})")
                            params[f"k{i}"] = k
                        elif op == "not_exists":
                            where_parts.append(f"NOT (sq.data ? :k{i})")
                            params[f"k{i}"] = k
                        else:
                            where_parts.append(f"COALESCE(NULLIF(regexp_replace(sq.data->>:k{i}, '[^0-9\\.\\-]', '', 'g'), '')::numeric, 0) {op} :v{i}")
                            params[f"k{i}"] = k
                            params[f"v{i}"] = v
                    sql = (
                        "SELECT s.salesforce_account_id AS account_id "
                        "FROM public.sites s JOIN public.site_qual sq ON sq.site_id = s.id "
                        "WHERE " + " AND ".join(where_parts)
                    )
                    out_sq = tool_sql_query(db, sql, params)
                    c = out_sq.get("columns") or []
                    rows_sq = [{c[i]: v for i, v in enumerate(r)} for r in out_sq.get("rows") or []]
                    allowed_sq = {str(r.get("account_id")) for r in rows_sq}

                # 6) Referral partner exclusions via extras if mentioned
                if re.search(r"\bnot\s+(a\s+)?referral\s+partner\b|\bno\s+sea\s+referral\b", s):
                    keep_ids = []
                    for aid in acc_ids:
                        try:
                            ex = tool_salesforce_account_extras(sf, aid)
                            cols = ex.get("columns") or []
                            row = {cols[i]: v for i, v in enumerate((ex.get("rows") or [[]])[0])} if ex.get("rows") else {}
                            # cs_referral_outreach false or None
                            if not row.get("cs_referral_outreach"):
                                keep_ids.append(aid)
                        except Exception:
                            continue
                    acc_ids = keep_ids

                # 7) Intersect all
                final_ids = [i for i in acc_ids if i in allowed_sf and i in allowed_sq]
                final_rows = [r for r in rows_ex if str((r or {}).get("account_id")) in final_ids]
                if not final_rows:
                    return {"answer": "<p>No sites found with those criteria.</p>"}
                table = {"columns": ex.get("columns") or [], "rows": final_rows}
                table = _normalize_table_for_ui(table)
                return {
                    "answer": f"<p>Found {len(table['rows'])} site(s) within ≤ {int(max_km)} km of {city} matching your filters.</p>",
                    "table": table,
                }
    except Exception as _e:
        _dbg("WARN: generic within-km pipeline failed: %s", _e)

    # REMOVED: nearest/closest handler — migrated to Claude agentic loop
    # ===== Determinista: nearest/closest N sites to <city> =====
    try:
        raise Exception("handler disabled")  # noqa: migrated to Claude agentic loop
        qtxt2 = (payload.messages[-1].content or "") if payload.messages else ""
        s2 = qtxt2.lower()
        # Detect "nearest N sites to <city>" or "closest N sites to <city>" or "N nearest sites to <city>"
        # Also handle "sites near <city>" / "near the <city> airport corridor"
        m_nearest = re.search(
            r"\b(?:nearest|closest|cerca(?:no)?s?|m[aá]s\s+cerca(?:no)?s?|near(?:\s+to)?|driving\s+distance)\b",
            s2, re.I
        )
        if m_nearest:
            # Extract top_n (optional digit before or after nearest/closest/sites)
            m_n = re.search(r"\b(\d+)\b", s2)
            top_n_det = int(m_n.group(1)) if m_n else 10
            top_n_det = min(max(top_n_det, 1), 50)
            # Extract max_km if "within X km" is in the query
            m_km = re.search(r"within\s+(\d+)\s*km", s2)
            max_km_det = float(m_km.group(1)) if m_km else 2000.0
            # Extract location: text after "to", "near", "of", "a" etc. (city must start with uppercase)
            # Handles "near Frankfurt", "near the Frankfurt airport", "to London", "of Munich" etc.
            m_loc = re.search(
                r"\b(?:to|near|of|cerca\s+de|desde|from|\ba\b)\s+(?:the\s+)?([A-ZÀ-Ö×Ø-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Ö×Ø-öø-ÿa-z'\-]+){0,3})"
                r"(?:\s+(?:airport|corridor|area|region|city|center|centre|downtown|hub)\b)?"
                r"(?:\s+(?:with|where|que|con|having|and|that|which|who)\b|\s*[.,?!]|\s*$)",
                qtxt2,
            )
            city_det = m_loc.group(1).strip() if m_loc else None
            if city_det and len(city_det) >= 2:
                _filters_p2 = _build_nearest_filters(qtxt2)
                _dbg("Deterministic nearest pipeline: top_n=%d, city=%s, max_km=%s, rules=%d",
                     top_n_det, city_det, max_km_det, len(_filters_p2.get("rules") or []))
                try:
                    out_near = tool_nearest_filtered_sites(
                        request,
                        location=city_det,
                        filters=_filters_p2,
                        top_n=top_n_det,
                        max_km=max_km_det,
                        db=db,
                    )
                    if out_near.get("error"):
                        raise RuntimeError(f"nearest error: {out_near['error']}")  # → fall to Gemini
                    rows_near = out_near.get("rows") or []
                    meta = out_near.get("meta") or {}
                    geocoded = meta.get("geocoded") or city_det
                    note = meta.get("note") or ""
                    if not rows_near:
                        # If we have sites in DB but couldn't resolve their geocoordinates → fall to Gemini
                        if meta.get("no_site_coords", 0) > 0 or meta.get("n_total_sites", 0) > 0:
                            _dbg("NEAREST: fast-path got 0 results but %d total sites, %d no-coords → Gemini",
                                 meta.get("n_total_sites", 0), meta.get("no_site_coords", 0))
                            raise RuntimeError("no_coords_fallback")  # → fall to Gemini
                        return {"answer": f"<p>No sites found within {int(max_km_det)} km of {geocoded}.</p>"}
                    ans_html = (
                        f"<p>Found <strong>{len(rows_near)}</strong> nearest clinical site(s) to "
                        f"<strong>{geocoded}</strong>, sorted by straight-line distance.</p>"
                    )
                    if note:
                        ans_html += f"<p><em>{note}</em></p>"
                    tbl_near = {"columns": out_near.get("columns") or [], "rows": rows_near}
                    tbl_near = _normalize_table_for_ui(tbl_near)
                    return {"answer": ans_html, "table": tbl_near}
                except Exception as _ne:
                    _dbg("WARN: deterministic nearest pipeline failed: %s", _ne)
                    # fall through to Gemini
    except Exception as _e:
        _dbg("WARN: nearest-sites fast-path failed: %s", _e)

    # ===== Phase 1 + 2 Structured Planner =====
    # Build a MobyPlan from the user query (pure regex + catalog, no LLM).
    # Used to: (a) short-circuit high-confidence table queries directly via
    # explorer_search, (b) inject a structured hint into Claude's system prompt,
    # (c) merge followup queries into last_filters, (d) auto-clarify ambiguities.
    _qtxt_plan = (payload.messages[-1].content or "") if payload.messages else ""
    # Reuse pre-computed plan from Option B block (already computed when last_filters present)
    if _qplan_pre is not None:
        _qplan = _qplan_pre
    else:
        _qplan = parse_query_plan(
            _qtxt_plan, kindex,
            last_filters=payload.last_filters,
            last_table=last_table_from_payload,
        )
    _dbg(
        "PLAN: intent=%s conf=%.2f countries=%s filters=%d unresolved=%s",
        _qplan["intent"], _qplan["confidence"], _qplan["countries"],
        len(_qplan["filters"]), _qplan["unresolved_terms"],
    )

    # Phase 2a: Followup merge — combine new countries/filters with last_filters
    # (skip if already handled by Option B pre-planner above)
    if not _followup_handled and can_followup_merge(_qplan, payload.last_filters):
        _merged_fg = merge_with_last_filters(_qplan, payload.last_filters)
        _dbg("PLAN followup merge: merged into %d rules", len(_merged_fg.get("rules", [])))
        try:
            _merged_result = tool_explorer_search(request, _merged_fg, columns=None)
            _merged_rows = (_merged_result or {}).get("rows") or []
            if _merged_rows:
                _merged_tbl = _normalize_table_for_ui({
                    "columns": _merged_result.get("columns") or [],
                    "rows":    _merged_rows,
                })
                _merged_ans = f"Found <strong>{len(_merged_rows)}</strong> site(s)"
                if _qplan.get("countries"):
                    _merged_ans += f" now including {', '.join(_qplan['countries'])}"
                return {
                    "answer":       f"<p>{_merged_ans}.</p>",
                    "table":        _merged_tbl,
                    "last_filters": _merged_fg,
                }
        except Exception as _merge_e:
            _dbg("WARN: planner followup merge failed: %s", _merge_e)
            # Fall through to short-circuit or Claude

    # Phase 2b: Clarification — return structured clarify response for known ambiguities
    _clarify = build_clarification(_qplan)
    if _clarify:
        _dbg("PLAN clarification triggered for: %s", _qplan["unresolved_terms"])
        return _clarify

    # Phase 1: Short-circuit — execute directly when plan is high-confidence table query
    if can_short_circuit(_qplan):
        _sc_fg = plan_to_filter_group(_qplan)
        try:
            _sc_cols = _qplan.get("requested_columns") or None
            _sc_result = tool_explorer_search(request, _sc_fg, columns=_sc_cols)
            _sc_rows = (_sc_result or {}).get("rows") or []
            if _sc_rows:
                _sc_tbl = _normalize_table_for_ui({
                    "columns": _sc_result.get("columns") or [],
                    "rows":    _sc_rows,
                })
                _sc_ans = f"Found <strong>{len(_sc_rows)}</strong> site(s)"
                if _qplan.get("countries"):
                    _sc_ans += f" in {', '.join(_qplan['countries'])}"
                _sc_ans += "."
                # Aggregation: when the user asked for a total / sum / count,
                # compute the relevant column totals and append to the answer.
                _agg_extra = _short_circuit_aggregation_lines(_qtxt_plan, _sc_rows)
                if _agg_extra:
                    _sc_ans += " " + " ".join(_agg_extra)
                _dbg("PLAN short-circuit: %d rows returned", len(_sc_rows))
                _sc_resp: Dict[str, Any] = {
                    "answer":       f"<p>{_sc_ans}</p>",
                    "table":        _sc_tbl,
                    "last_filters": _sc_fg,
                }
                # Phase 2c: CSV export — attach csv_data when user asked for export
                if _qplan.get("needs_export"):
                    import io, csv as _csv
                    _csv_buf = io.StringIO()
                    _all_cols = [c["key"] for c in (_sc_result.get("columns") or [])]
                    _w = _csv.DictWriter(_csv_buf, fieldnames=["account_name"] + _all_cols, extrasaction="ignore")
                    _w.writeheader()
                    for _r in _sc_rows:
                        _flat = {"account_name": _r.get("account_name", "")}
                        _flat.update(_r.get("data", {}))
                        _w.writerow(_flat)
                    _sc_resp["csv_data"] = _csv_buf.getvalue()
                    _dbg("PLAN short-circuit CSV: %d bytes", len(_sc_resp["csv_data"]))
                return _sc_resp
        except Exception as _sc_e:
            _dbg("WARN: planner short-circuit failed: %s", _sc_e)
            # Fall through to Claude

    msgs: List[Dict[str, Any]] = [{"role":"system","content":SYSTEM_PROMPT}]
    msgs.append({
        "role":"system",
        "content": (
            "KNOWLEDGE INDEX (SF preferred over site_qual when both exist). "
            "Aliases → target field:\n" + INDEX_SNIPPET
        )
    })

    # Option A: semantic field hints (Cohere Bedrock). Guides Claude on multilingual
    # queries before the agentic loop picks tools. No-op when flag is off or resolver fails.
    try:
        _user_msg_for_hints = (payload.messages[-1].content or "") if payload.messages else ""
        _hints_block = _semantic_field_hints(_user_msg_for_hints)
        if _hints_block:
            msgs.append({"role": "system", "content": _hints_block})
    except Exception as _hints_exc:
        _dbg("WARN: semantic field hints injection failed: %s", _hints_exc)
    if payload.last_filters and payload.last_filters.get("rules"):
        msgs.append({
            "role": "system",
            "content": (
                "LAST FILTERS (FilterGroup that produced the current table — "
                "for follow-up queries, modify these and call explorer_search again with the updated filters):\n"
                + json.dumps(payload.last_filters, ensure_ascii=False)
            )
        })

    # Inject plan hint so Claude has pre-resolved intent, countries, and filters
    _plan_hint = plan_to_system_hint(_qplan)
    if _plan_hint:
        msgs.append({"role": "system", "content": _plan_hint})

    # Inject a compact summary of the previous table so Claude can answer numeric follow-ups
    # (e.g. "what's the average?", "which has the highest?", "compare X vs Y") without re-fetching data.
    # Strategy: inject ALL rows for small tables (≤50 rows) so comparisons work correctly;
    # for large tables inject a 10-row sample plus column summary.
    if last_table_from_payload and isinstance(last_table_from_payload.get("rows"), list):
        _ctx_rows = last_table_from_payload.get("rows", [])
        if _ctx_rows:
            _ctx_cols = [c.get("key") if isinstance(c, dict) else str(c)
                         for c in (last_table_from_payload.get("columns") or [])]
            # Send all rows up to 500 (≈9 000 tokens, <5% of context window)
            # For larger tables send first 500 rows as a representative sample
            _ctx_sample = _ctx_rows if len(_ctx_rows) <= 500 else _ctx_rows[:500]
            _ctx_label = "Full data" if len(_ctx_rows) <= 500 else f"Sample (first 500 of {len(_ctx_rows)} rows)"
            msgs.append({
                "role": "system",
                "content": (
                    f"PREVIOUS TABLE CONTEXT ({len(_ctx_rows)} total rows):\n"
                    f"Columns: {', '.join(_ctx_cols)}\n"
                    f"{_ctx_label}:\n"
                    + json.dumps(_ctx_sample, default=str)
                )
            })

    for m in _truncate_history(payload.messages):
        msgs.append({"role": m.role, "content": m.content})

    # Inicializar last_table desde el payload si está disponible
    last_table: Optional[Dict[str, Any]] = last_table_from_payload if last_table_from_payload else None
    last_visualization: Optional[Dict[str, Any]] = None
    last_explorer_filters: Optional[Dict[str, Any]] = None
    
    # Si tenemos last_table del payload, informar al modelo
    if last_table and len(last_table.get("rows", [])) > 0:
        _dbg("Using last_table from payload (%d rows)", len(last_table.get("rows", [])))

    # ===== Determinista: follow‑ups sobre last_table (Others / Top N / Filter) =====
    try:
        user_txt = (payload.messages[-1].content or "").lower() if payload.messages else ""
        if last_table and last_table.get("rows"):
            rows = last_table.get("rows", [])
            cols = [c.get("key") if isinstance(c, dict) else str(c) for c in (last_table.get("columns") or [])]
            # --- Autodetección de columnas ---
            def autodetect_cols():
                value_col = None
                if any((c or "").lower()=="sites" for c in cols):
                    value_col = next((c for c in cols if (c or "").lower()=="sites"), None)
                if not value_col and rows:
                    for k in rows[0].keys():
                        try:
                            vals = [r.get(k) for r in rows if isinstance(r, dict)]
                            if all((isinstance(v,(int,float)) or str(v).replace(",",".").replace(".","").isdigit()) for v in vals):
                                value_col = k; break
                        except Exception:
                            pass
                label_col = None
                for pref in ("country","city","site","name","label"):
                    if any((c or "").lower()==pref for c in cols):
                        label_col = next((c for c in cols if (c or "").lower()==pref), None); break
                if not label_col and rows:
                    for k in rows[0].keys():
                        if k == value_col:
                            continue
                        v0 = rows[0].get(k)
                        if not isinstance(v0,(int,float)):
                            label_col = k; break
                return label_col, value_col
            label_col, value_col = autodetect_cols()

            # 1) Others grouping
            others_rx = re.compile(r"\b(group|agrupar|combinar|merge)\b[\s\S]*?(<|less\s+than|menor(?:es)?\s+de)\s*(\d+)\s*(sites|centros)?[\s\S]*?(others|otros)\b", re.I)
            m_others = others_rx.search(user_txt)
            if m_others and label_col and value_col:
                thr = int(m_others.group(3)) if m_others and m_others.group(3) else 3
                _dbg("Deterministic others-grouping: threshold=%d, label=%s, value=%s", thr, label_col, value_col)
                manipulated = tool_manipulate_data(last_table, "group_others", float(thr), value_col, label_col)
                if manipulated.get("rows"):
                    last_table = manipulated
                    chart_kind = (last_visualization or {}).get("type") or ("pie")
                    viz = tool_render_chart(chart_kind, manipulated.get("rows"), label_col, [value_col], {"title": f"{_pretty_label(label_col)} grouped (<{thr} as Others)"}, {})
                    last_visualization = viz.get("visualization")
                    try:
                        total = sum(float(r.get(value_col) or 0) for r in manipulated.get("rows") if isinstance(r, dict))
                    except Exception:
                        total = 0
                    ans_html = f"<p>Grouped { _pretty_label(label_col).lower() } with &lt; {thr} { _pretty_label(value_col).lower() } into ‘Others’. Total: {int(total) if isinstance(total, float) and total.is_integer() else total}.</p>"
                    return {"answer": ans_html, "visualization": last_visualization}

            # 2) Top N of previous
            top_rx = re.compile(r"\btop\s*(\d{1,2})\b|\b(los|las)\s*(\d{1,2})\s*(m[aá]s|mayores|grandes)\b|\b(topt)\b", re.I)
            m_top = top_rx.search(user_txt)
            if m_top and label_col and value_col:
                n = None
                for g in (1,3):
                    try:
                        if m_top.group(g):
                            n = int(m_top.group(g)); break
                    except Exception:
                        pass
                n = n or 5
                _dbg("Deterministic top_n: n=%d, label=%s, value=%s", n, label_col, value_col)
                manipulated = tool_manipulate_data(last_table, "top_n", float(n), value_col, label_col)
                if manipulated.get("rows"):
                    last_table = manipulated
                    viz = tool_render_chart("bar", manipulated.get("rows"), label_col, [value_col], {"title": f"Top {n} by { _pretty_label(value_col) }"}, {})
                    last_visualization = viz.get("visualization")
                    total = sum(float(r.get(value_col) or 0) for r in manipulated.get("rows") if isinstance(r, dict))
                    ans_html = f"<p>Showing top {n} { _pretty_label(label_col).lower() } by { _pretty_label(value_col).lower() }. Subtotal: {int(total) if total.is_integer() else total}.</p>" if isinstance(total,float) else f"<p>Showing top {n} by {_pretty_label(value_col)}.</p>"
                    return {"answer": ans_html, "visualization": last_visualization}

            # 3) Filter >= / > / <= / < on previous
            cmp_rx = re.compile(r"(>=|<=|>|<|at\s+least|como\s+m[ií]nimo|al\s+menos|m[aá]s\s+de|menos\s+de)\s*(\d+[\.,]?\d*)\s*(sites|centros|patients?)?", re.I)
            m_cmp = cmp_rx.search(user_txt)
            if m_cmp and label_col and value_col:
                raw_op = (m_cmp.group(1) or ">=").lower()
                num = m_cmp.group(2)
                try:
                    thr = float(num.replace(",", ""))
                except Exception:
                    thr = 0.0
                op = ">="
                if raw_op in {">=","<=",">","<"}:
                    op = raw_op
                elif re.search(r"at\s+least|como\s+m[ií]nimo|al\s+menos", raw_op):
                    op = ">="
                elif re.search(r"m[aá]s\s+de", raw_op):
                    op = ">"
                elif re.search(r"menos\s+de", raw_op):
                    op = "<"
                _dbg("Deterministic filter: %s %s on %s", op, thr, value_col)
                manipulated = tool_manipulate_data(last_table, "filter", thr, value_col, label_col, op)
                if manipulated.get("rows"):
                    last_table = manipulated
                    viz = tool_render_chart("bar", manipulated.get("rows"), label_col, [value_col], {"title": f"Filtered by {op} {thr}"}, {})
                    last_visualization = viz.get("visualization")
                    ans_html = f"<p>Filtered to { _pretty_label(value_col).lower() } {op} {thr}. Kept {len(manipulated.get('rows') or [])} groups.</p>"
                    return {"answer": ans_html, "visualization": last_visualization}

            # 4) Sorting asc/desc on previous
            if label_col and value_col:
                asc_rx = re.compile(r"\b(sort|ordenar|ordena)\b[\s\S]*\b(asc|ascending|ascendente|creciente|menor\s*a\s*mayor|lowest\s+first)\b", re.I)
                desc_rx = re.compile(r"\b(sort|ordenar|ordena)\b[\s\S]*\b(desc|descending|descendente|decreciente|mayor\s*a\s*menor|highest\s+first)\b", re.I)
                do_asc = bool(asc_rx.search(user_txt))
                do_desc = bool(desc_rx.search(user_txt))
                if do_asc or do_desc:
                    reverse = True if do_desc else False
                    _dbg("Deterministic sort: %s on %s", "DESC" if reverse else "ASC", value_col)
                    # Ordena y conserva columnas
                    def to_num(v: Any) -> float:
                        try:
                            if isinstance(v, (int, float)):
                                return float(v)
                            return float(str(v).replace(",", "")) if v is not None else 0.0
                        except Exception:
                            return 0.0
                    sorted_rows = sorted([dict(r) for r in rows if isinstance(r, dict)], key=lambda r: to_num(r.get(value_col)), reverse=reverse)
                    last_table = {"columns": last_table.get("columns") or [], "rows": sorted_rows}
                    viz = tool_render_chart("bar", sorted_rows, label_col, [value_col], {"title": f"Sorted by { _pretty_label(value_col) } {'DESC' if reverse else 'ASC'}"}, {})
                    last_visualization = viz.get("visualization")
                    ans_html = f"<p>Sorted by { _pretty_label(value_col).lower() } {'descending' if reverse else 'ascending'}.</p>"
                    return {"answer": ans_html, "visualization": last_visualization}

            # 5) Include/Exclude label values (countries/cities)
            if label_col:
                def _parse_label_list(txt: str) -> list[str]:
                    # captura todo tras países/ciudades y lo divide por comas y 'and/y/e'
                    tail = txt
                    # reemplazos comunes de conectores por comas
                    tail = re.sub(r"\b(and|y|e)\b", ",", tail, flags=re.I)
                    parts = [p.strip() for p in tail.split(",") if p.strip()]
                    # limpia comillas
                    parts = [re.sub(r"^[\"'\s]+|[\"'\s]+$", "", p) for p in parts]
                    # descarta palabras genéricas
                    bad = {"countries","países","pais","ciudades","cities","country","city"}
                    return [p for p in parts if p.lower() not in bad]
                # include variants
                inc_pat = re.compile(r"\b(only|solo|sólo|just|include|incluir|filter\s+to|filtra(?:r|do)?\s+por)\b[\s\S]*?\b(countries|pa[ií]ses|cities|ciudades)\b[\s:]*([^\n]+)$", re.I)
                exc_pat = re.compile(r"\b(exclude|excluir|sin|excepto?)\b[\s\S]*?(countries|pa[ií]ses|cities|ciudades)?[\s:]*([^\n]+)$", re.I)
                m_inc = inc_pat.search(user_txt)
                m_exc = exc_pat.search(user_txt)
                if (m_inc or m_exc):
                    want = []
                    exclude = False
                    text_list = None
                    if m_inc:
                        text_list = m_inc.group(3)
                        exclude = False
                    if m_exc and not m_inc:  # prefer include when both
                        text_list = m_exc.group(3)
                        exclude = True
                    if text_list:
                        wanted = set(x.lower() for x in _parse_label_list(text_list))
                        _dbg("Deterministic label %s: %s", "exclude" if exclude else "include", ", ".join(sorted(wanted)))
                        def norm(x: Any) -> str:
                            return str(x or "").strip().lower()
                        new_rows = []
                        for r in rows:
                            if not isinstance(r, dict):
                                continue
                            val = norm(r.get(label_col))
                            if (not exclude and val in wanted) or (exclude and val not in wanted):
                                new_rows.append(r)
                        last_table = {"columns": last_table.get("columns") or [], "rows": new_rows}
                        # fallback para serie Y si no hay value_col: toma la primera numérica
                        ykey = value_col
                        if not ykey and new_rows:
                            for k in (new_rows[0].keys() if isinstance(new_rows[0], dict) else []):
                                try:
                                    float(str(new_rows[0].get(k)).replace(",",""))
                                    ykey = k
                                    break
                                except Exception:
                                    continue
                        viz = tool_render_chart("bar", new_rows, label_col, [ykey] if ykey else [], {"title": ("Exclude" if exclude else "Include") + " filter"}, {})
                        last_visualization = viz.get("visualization")
                        ans_html = f"<p>{'Excluded' if exclude else 'Included only'} {len(wanted)} { _pretty_label(label_col).lower() } value(s). Kept {len(new_rows)} rows.</p>"
                        return {"answer": ans_html, "visualization": last_visualization}

            # 6) Coloring/highlighting by criteria (regions, explicit lists, generic terms)
            color_intent = re.compile(r"\b(colou?r|paint|highlight|resaltar|colorear|pintar|destacar)\b", re.I)
            if color_intent.search(user_txt) and label_col and (last_table.get("rows") if last_table else None):
                # Color to use
                color_map = {
                    "red": "#E74C3C", "rojo": "#E74C3C",
                    "blue": "#3498DB", "azul": "#3498DB",
                    "green": "#2ECC71", "verde": "#2ECC71",
                    "orange": "#F39C12", "naranja": "#F39C12",
                    "purple": "#8E44AD", "morado": "#8E44AD", "violeta": "#8E44AD",
                }
                color_word = None
                for w in color_map.keys():
                    if re.search(rf"\b{re.escape(w)}\b", user_txt):
                        color_word = w; break
                color_hex = color_map.get(color_word or "red", "#E74C3C")

                # Region sets (Europe-centric)
                REGIONS = {
                    "northern": {"denmark","sweden","norway","finland","iceland"},
                    "nordic":   {"denmark","sweden","norway","finland","iceland"},
                    "southern": {"spain","portugal","italy","greece","malta"},
                    "western":  {"united kingdom","ireland","france","belgium","netherlands","luxembourg"},
                    "eastern":  {"poland","czechia","slovakia","hungary","romania","bulgaria","ukraine","belarus"},
                    "central":  {"germany","austria","switzerland","czechia","slovakia","poland"},
                    "baltic":   {"estonia","latvia","lithuania"},
                    "benelux":  {"belgium","netherlands","luxembourg"},
                    "iberia":   {"spain","portugal"},
                    "british":  {"united kingdom","ireland"},
                    "balkan":   {"croatia","bosnia and herzegovina","serbia","montenegro","north macedonia","albania","slovenia","bulgaria","greece","romania"},
                }
                target: set[str] = set()
                # 6.a) Region keywords
                for reg, members in REGIONS.items():
                    if re.search(rf"\b{reg}\w*\b", user_txt):
                        target.update(members)
                # 6.b) Explicit list: countries ... <list>
                m_list = re.search(r"\b(countries|pa[ií]ses|ciudades|cities)\b[\s:]*([^\n]+)$", user_txt)
                if m_list:
                    tail = re.sub(r"\b(and|y|e)\b", ",", m_list.group(2))
                    for p in [t.strip().strip("\"'") for t in tail.split(",") if t.strip()]:
                        target.add(p.lower())
                # 6.c) Fallback: try to match any known label tokens in text
                if not target and last_table.get("rows"):
                    sample = last_table.get("rows")[0]
                    vals = {str(r.get(label_col)).strip().lower() for r in last_table.get("rows") if isinstance(r, dict)}
                    for v in vals:
                        if v and re.search(rf"\b{re.escape(v)}\b", user_txt):
                            target.add(v)
                if not target:
                    # No matches → keep existing chart, no error
                    pass
                # Apply coloring
                new_rows = []
                for r in (last_table.get("rows") or []):
                    if not isinstance(r, dict):
                        continue
                    rr = dict(r)
                    name = str(rr.get(label_col) or "").strip().lower()
                    if name in target:
                        rr["_color"] = color_hex
                    new_rows.append(rr)
                # Pick chart type; reuse if bar/pie, else bar
                kind = (last_visualization or {}).get("type") or "bar"
                if kind not in ("bar","pie"):
                    kind = "bar"
                viz = tool_render_chart(kind, new_rows, label_col, [value_col] if value_col else [], {"title": f"{_pretty_label(value_col or 'value')} (highlighted)"}, {})
                last_visualization = viz.get("visualization")
                return {"answer": "<p>Applied highlighting as requested.</p>", "visualization": last_visualization}
    except Exception as _e:
        _dbg("WARN: follow-up ops block failed: %s", _e)
        pass

    # ===== MAIN EXECUTION LOOP =====
    # Heurística de intención de datos
    user_utterance = (payload.messages[-1].content or "") if payload.messages else ""
    data_intent = bool(re.search(r"(top\s*\d+|rank|table|chart|sites?|patients?|screening|diagnosed|count|sum|average|media|promedio)", user_utterance))
    
    # Pista de enrutamiento por matches del índice
    if user_utterance:
        # DETECCIÓN: Petición de gráfico sobre datos existentes
        is_chart_request = bool(
            re.search(r"\b(chart|graph|bar|pie|line|visuali[sz]e|display|show|plot)\b", user_utterance) and
            last_table and len(last_table.get("rows", [])) > 0
        )
        
        # DETECCIÓN CRÍTICA: conteos básicos de sitios
        is_basic_site_query = bool(
            re.search(r"\b(how\s+many|count|total|list|show|all)\b.*\bsites?\b", user_utterance) and
            re.search(r"\b(country|countries|city|cities|per|by)\b", user_utterance) and
            not re.search(r"\b(patient|stage|screening|hla|pharmacy|overnight|t1d|diagnosis|qualification|with\s+)\b", user_utterance)
        )

        # DETECCIÓN CRÍTICA: nearest/closest sites to a city/address
        is_nearest_query = bool(
            re.search(r"\b(nearest|closest|cerca(no)?s?|m[aá]s\s+cerca|near|within\s+\d+\s*km\s+of|within\s+\d+\s*km\s+from)\b", user_utterance, re.I)
        )
        
        aliases = list(kindex.keys())
        best = _top_matches(user_utterance, aliases, k=5)
        hints = []
        
        # Si el usuario pide contactos/roles (PI/SC/etc.), no uses planner; deja que el modelo invoque la tool de contactos
        _role_pat = re.compile(
            r"\b(?:study\s+coordinator|study\s+nurse|principal\s+investigator"
            r"|clinician|project\s+manager|technician|member\s+representative"
            r"|contact\s+person|associate\s+scientist|post-doc|phd\s+student|fellow)\b"
            r"|\bPI\b",  # PI must be uppercase to avoid matching "typing", "pipeline", etc.
            re.I
        )
        if _role_pat.search(user_utterance):
            _dbg("Fast planner: contact/role intent detected, skipping planner")
            return None

        # Si es petición de gráfico sobre datos existentes, usar render_chart
        if is_chart_request:
            # Preparar resumen compacto de los datos
            rows_sample = last_table.get('rows', [])[:5]  # Primeras 5 filas como ejemplo
            cols_info = [c.get('key', '') for c in last_table.get('columns', [])]
            data_preview = json.dumps(rows_sample, default=str)[:500]  # Limitar tamaño
            
            hints.append(
                "📊 **CHART REQUEST**: User wants to visualize the previous result. "
                f"The table has {len(last_table.get('rows', []))} rows. Columns: {', '.join(cols_info)}. "
                f"Sample data (first 5 rows): {data_preview}. "
                "Use render_chart with 'data' parameter containing ALL rows from the previous table result. "
                "The data is available in the tool result from the previous call. "
                "Choose xKey (dimension like 'country'/'city'/'site') and yKeys (numeric like 'sites'/'count'/'patients'). "
                "Do NOT call soql_query again."
            )
        elif re.search(r"\b(chart|graph|bar|pie|line|visuali[sz]e)\b", user_utterance):
            # Petición de gráfico pero NO hay last_table (nueva conversación o follow-up sin datos)
            hints.append(
                "📊 **CHART WITHOUT DATA**: User wants a chart but no previous data is available in this session. "
                "You MUST first query the data (use soql_query for sites/counts, or explorer_search for qualification data), "
                "and THEN call render_chart with the 'data' parameter containing the rows from that query. "
                "Do NOT call render_chart with empty or placeholder data."
            )
        
        # Si es una query de "nearest/closest sites to <location>", forzar nearest_filtered_sites
        elif is_nearest_query:
            hints.append(
                "📍 **NEAREST SITES**: User wants sites near a location. "
                "You MUST use nearest_filtered_sites(location='<city/address>', top_n=<N>, max_km=<radius>). "
                "Do NOT use soql_query for this. "
                "Example: nearest_filtered_sites(location='Barcelona', top_n=5). "
                "Add filters={...} only if the user specifies additional field constraints."
            )

        # Si es un conteo básico de sitios, forzar Salesforce Account
        elif is_basic_site_query:
            hints.append(
                "🔴 **CRITICAL ROUTING**: This is a site list/count query. "
                "You MUST use soql_query with Account table. "
                "For site lists/counts/geography, ALWAYS use Salesforce Account: "
                "SELECT ... FROM Account WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
                "AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
                "AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null). "
                "Use Account.ShippingCountry/ShippingCity for geography. ORDER BY COUNT(Id) DESC."
            )
        
        if best:
            for a in best:
                meta = kindex.get(a, {})
                if meta.get("source") == "sf":
                    hints.append(
                        f"{a} -> use soql_query (field: {meta.get('field')}). "
                        "SOQL rules: do NOT prefix fields with 'sf.'; use Account.ShippingCountry and "
                        "Account.ShippingCity for geography; to filter non-null use '!= null'."
                    )
                else:
                    hints.append(f"{a} -> use explorer_search with qual.* filter (key: qual.{meta.get('key')}). "
                                 "Use equals/contains operators for text, > for numeric.")
        # Heurística de CONTACT ROLES / TITLES
        role_map = {
            r"study\\s*coordinator": ("Study Coordinator",),
            r"study\\s*nurse|nurse": ("Study Nurse", "Nurse"),
            r"principal\\s*investigator|\\bpi\\b": ("PI",),
            r"clinician": ("Clinician",),
            r"project\\s*manager": ("Project Manager",),
            r"technician": ("Technician",),
            r"member\\s*representative": ("Member Representative",),
            r"contact\\s*person(\\s*cctu)?": ("Contact person CCTU","Contact Person"),
            r"associate\\s*scientist": ("Associate Scientist",),
            r"post-?doc": ("Post-doc",),
            r"phd\\s*student": ("PhD student",),
            r"head": ("Head",),
            r"fellow": ("Fellow",),
        }
        matched_roles = []
        for pat, roles in role_map.items():
            if re.search(pat, user_utterance):
                matched_roles.extend(list(roles))
        if matched_roles:
            hints.append(
                "Contacts → use salesforce_account_contacts; roles=" + ", ".join(sorted(set(matched_roles))) +
                ". Consider include_subaccounts when they mention clinical sub-accounts."
            )
        
        # SIEMPRE añadir recordatorio de routing (excepto cuando ya hay hint específico)
        if not is_basic_site_query and not is_nearest_query:
            hints.insert(0, "🔴 REMEMBER: Use Postgres (sql_query) ONLY for site_qual questions. Everything else uses Salesforce.")
        
        if hints:
            msgs.append({
                "role":"system",
                "content": (
                    "ROUTING HINTS for the last user request:\n- " + "\n- ".join(hints)
               )
            })
    
    # Pure conversational follow-up: no tools needed — answer from conversation history + table context.
    # Detects short queries that reference prior results without requesting new data.
    _is_pure_context = (
        len(user_utterance.split()) <= 14 and
        not re.search(
            r"\b(site[s]?|center[s]?|countr|cit[yi]|patient|stage|nd\b|t1d|hla|pharmac|"
            r"find|show|list|search|activit|assignment|nearest|closest)\b",
            user_utterance.lower()
        ) and
        (has_prior_assistant_response or has_prior_table) and
        re.search(
            r"\b(that|those|they|it|this|the\s+result|you\s+just|that\s+number|those\s+sites"
            r"|the\s+table|last|previous|above|of\s+that|of\s+those|from\s+that|of\s+them"
            r"|odd|even|prime|double|triple|half|percent|ratio)\b",
            user_utterance.lower()
        )
    )
    if _is_pure_context:
        _dbg("Pure conversational follow-up detected → force_no_tools=True")
        try:
            _ctx_resp = _claude_chat(msgs, force_no_tools=True)
            _ctx_text = (_ctx_resp.choices[0].message.content or "").strip()
            if _ctx_text:
                return _extract_structured(_ctx_text)
        except Exception as _ctx_e:
            _dbg("WARN: force_no_tools conversational call failed: %s", _ctx_e)
            # Fall through to normal tool-based flow

    # Enable extended thinking for complex multi-condition queries.
    _complex = _is_complex_query(user_utterance)
    _dbg("Agentic loop -> data_intent=%s | thinking=%s", data_intent, _complex)

    # If data_intent is set, inject a reinforcement hint so Claude uses tools
    if data_intent:
        msgs.append({
            "role": "system",
            "content": (
                "IMPORTANT: You must call at least one tool to fetch **real data**. "
                "Prefer explorer_search as your default tool; use soql_query for SOQL aggregations. "
                "Return a compact table (Site, Country, City, metric) and, if useful, a bar chart."
            )
        })

    # --- Inject planner hints into system prompt for the agentic loop ---
    try:
        # parse_query_plan returns MobyPlan: countries=List[str] (ISO2), filters=List[FilterSpec]
        _hint_plan = parse_query_plan(user_utterance, kindex)
        _planner_hints = []
        if _hint_plan.get("countries"):
            _iso_list = _hint_plan["countries"]  # already List[str] of ISO2 codes
            _planner_hints.append(f"Detected countries: {', '.join(_iso_list)}. Use site.country filter with these ISO2 codes.")
        if _hint_plan.get("filters"):
            _filter_strs = [
                f"{f.get('field', '?')} {f.get('operator', '?')} {f.get('value', '')}"
                for f in _hint_plan["filters"]
            ]
            _planner_hints.append(f"Suggested filters: {', '.join(_filter_strs)}. Verify these match the user's intent.")
        if _hint_plan.get("intent") and _hint_plan["intent"] != "other":
            _planner_hints.append(f"Detected intent: {_hint_plan['intent']}.")

        if _planner_hints:
            _hint_block = "\n".join(f"- {h}" for h in _planner_hints)
            _hint_msg = f"\n\n[SYSTEM HINTS — use as starting points, verify against user's question]\n{_hint_block}"
            for _m in msgs:
                if _m.get("role") == "system":
                    _m["content"] += _hint_msg
                    break
            _dbg("Planner hints injected: %d hints", len(_planner_hints))
    except Exception:
        pass  # Hints are optional — don't let planner errors break the chat

    # --- Agentic loop replaces the old single-turn tool dispatch ---
    from app.routers.moby_tools import ToolContext as _TC, dispatch_tool as _dispatch_tool

    _tool_ctx = _TC(
        db=db,
        request=request,
        sf=sf,
        last_table=last_table,
        last_visualization=last_visualization,
        last_explorer_filters=last_explorer_filters,
        msgs=msgs,
        args={},
        tool_call_id="",
    )

    loop_result = _agentic_loop(msgs=msgs, tool_ctx=_tool_ctx, use_thinking=_complex, user_msg=user_utterance)

    last_table = loop_result["last_table"]
    last_visualization = loop_result["last_visualization"]
    last_explorer_filters = loop_result["last_explorer_filters"]
    text_out = loop_result["text"]

    _dbg("Agentic loop done: turns=%d, tools=%s, text=%d chars, has_table=%s",
         loop_result["turns_used"],
         [t["tool"] for t in loop_result["tool_calls_made"]],
         len(text_out or ""),
         bool(last_table))

    # Build response
    if last_table:
        rows = last_table.get("rows", [])
        if text_out:
            answer_html = text_out
        else:
            # Fallback: build a basic but informative summary from the table data
            n = len(rows)
            cols = [c.get("key","") if isinstance(c,dict) else str(c) for c in last_table.get("columns",[])]
            # Try to find country distribution
            countries = {}
            for r in rows:
                if isinstance(r, dict):
                    c = r.get("country","")
                    if c:
                        countries[c] = countries.get(c, 0) + 1
            parts = [f"<p><strong>{n} site{'s' if n != 1 else ''}</strong> found"]
            if countries and len(countries) <= 8:
                top_c = sorted(countries.items(), key=lambda x: -x[1])[:3]
                c_str = ", ".join(f"{c} ({cnt})" for c, cnt in top_c)
                parts[0] += f" across {len(countries)} {'countries' if len(countries) > 1 else 'country'}: {c_str}"
                if len(countries) > 3:
                    parts[0] += f" and {len(countries)-3} more"
            parts[0] += ".</p>"
            answer_html = "".join(parts)
        out: Dict[str, Any] = {"answer": answer_html, "table": _normalize_table_for_ui(last_table)}
        if last_visualization:
            out["visualization"] = last_visualization
        if last_explorer_filters:
            out["last_filters"] = last_explorer_filters
        return out

    if text_out:
        out = {"answer": text_out}
        if last_explorer_filters:
            out["last_filters"] = last_explorer_filters
        return out

    return {"answer": "I wasn\'t able to answer that. Could you rephrase or break it into smaller questions?"}

# ====== SOQL sanitizer (post-model, pre-validate) ======
def _sanitize_soql_basic(soql: str) -> str:
    """
    Corrige errores comunes que el modelo puede introducir:
    - elimina prefijo ficticio 'sf.' delante de campos
    - reemplaza Account.Country/City por Account.ShippingCountry/Account.ShippingCity
    - normaliza NULL/Null → null para comparaciones
    - fuerza alias COUNT(Id) a 'count' (evita 'sites')
    """
    s = soql or ""
    # quitar 'sf.' sólo delante de identificadores (no tocar strings)
    s = re.sub(r"\bsf\.", "", s)
    # geografía correcta según whitelist
    s = re.sub(r"\bAccount\.Country\b", "Account.ShippingCountry", s)
    s = re.sub(r"\bAccount\.City\b", "Account.ShippingCity", s)
    # normalizar NULL literales
    s = re.sub(r"\bNULL\b", "null", s)
    # alias de COUNT(Id) → 'count'
    s = re.sub(r"(?i)(count\s*\(\s*id\s*\)\s+)sites\b", r"\1count", s)
    s = re.sub(r"(?i)(count\s*\(\s*id\s*\)\s+)(\w+)\b", lambda m: m.group(1) + ("count" if m.group(2).lower()=="sites" else m.group(2)), s)
    return s

# ====== Generic metric resolver & Top-N ranking tool ======
def _resolve_metric(alias_or_key: str, db: Session) -> Dict[str, Any]:
    """
    Resolve a free-text alias or raw key into:
      - {"source":"sf","field":"<SF API name>","label":"<nice label>"}
      - {"source":"site_qual","key":"<JSONB key>","label":"<nice label>"}
    """
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

def tool_rank_sites(
    db: Session,
    sf,
    metric: str,
    top_n: int = 5,
    order: Literal["asc","desc"] = "desc",
):
    """
    Generic Top-N ranking across SF and site_qual.
    Returns a normalized table with account_id, site, country, city and the metric column.
    """
    meta = _resolve_metric(metric, db)
    dir_sql = "ASC" if str(order).lower() == "asc" else "DESC"

    # -- site_qual path (warehouse) --
    if meta.get("source") == "site_qual":
        key = meta.get("key")
        sql = f"""
            SELECT
                s.salesforce_account_id AS account_id,
                s.name AS site,
                s.country,
                s.city,
                COALESCE(
                    NULLIF(regexp_replace(sq.data->>:key, '[^0-9\\.\\-]', '', 'g'), '')::numeric,
                    0
                ) AS metric
            FROM public.sites s
            LEFT JOIN public.site_qual sq ON sq.site_id = s.id
            ORDER BY metric {dir_sql} NULLS LAST
            LIMIT :top_n
        """
        out = tool_sql_query(db, sql, {"key": key, "top_n": int(top_n)})
        cols = out.get("columns") or []
        rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
        table = {
            "columns": [
                {"key":"account_id","label":"Account Id"},
                {"key":"site","label":"Account Name"},
                {"key":"country","label":"Country"},
                {"key":"city","label":"City"},
                {"key":f"qual.{key}","label":_pretty_label(f"qual.{key}")},
            ],
            "rows": [
                {
                    "account_id": r.get("account_id"),
                    "site": r.get("site"),
                    "country": r.get("country"),
                    "city": r.get("city"),
                    f"qual.{key}": r.get("metric")
                } for r in rows
            ],
        }
        return _normalize_table_for_ui(table)
    elif meta.get("source") == "profiling_kv":
        key = meta.get("key")
        sql = f"""
            SELECT
                s.salesforce_account_id AS account_id,
                s.name AS site,
                s.country,
                s.city,
                COALESCE(
                    NULLIF(regexp_replace(p.value, '[^0-9\\.\\-]', '', 'g'), '')::numeric,
                    0
                ) AS "{key}"
            FROM public.sites s
            LEFT JOIN public.profiling_kv p
              ON p.site_id = s.id AND p.key = :key
            ORDER BY "{key}" {dir_sql} NULLS LAST
            LIMIT :top_n
        """
        out = tool_sql_query(db, sql, {"key": key, "top_n": int(top_n)})
        cols = out.get("columns") or []
        rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
        metric_key = f"qual.{key}"
        for r in rows:
            if key in r:
                r[metric_key] = r.pop(key)
        table = {
            "columns": [
                {"key":"account_id","label":"Account Id"},
                {"key":"site","label":"Account Name"},
                {"key":"country","label":"Country"},
                {"key":"city","label":"City"},
                {"key":metric_key,"label":_pretty_label(metric_key)},
            ],
            "rows": rows,
        }
        return _normalize_table_for_ui(table)

    # -- SF path --
    field = meta.get("field")
    if not sf:
        raise HTTPException(400, "No active Salesforce session for SF ranking")
    soql = f"""
        SELECT
            Account.Id,
            Account.Name,
            Account.ShippingCountry,
            Account.ShippingCity,
            MAX({field}) metric
        FROM Opportunity
        WHERE {field} != null
        GROUP BY Account.Id, Account.Name, Account.ShippingCountry, Account.ShippingCity
        ORDER BY metric {dir_sql} NULLS LAST
        LIMIT {int(top_n)}
    """
    raw = tool_salesforce_query(sf, soql)
    records = raw.get("records", []) if isinstance(raw, dict) else []
    rows = []
    for r in records:
        acc = r.get("Account") or {}
        rows.append({
            "account_id": acc.get("Id"),
            "site": acc.get("Name"),
            "country": acc.get("ShippingCountry"),
            "city": acc.get("ShippingCity"),
            f"sf.{field}": r.get("expr0") or r.get("metric"),
        })
    table = {
        "columns": [
            {"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},
            {"key":"country","label":"Country"},
            {"key":"city","label":"City"},
            {"key":f"sf.{field}","label":_pretty_label(f"sf.{field}")},
        ],
        "rows": rows,
    }
    return _normalize_table_for_ui(table)
