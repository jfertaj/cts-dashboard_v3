# app/routers/ai_chat.py
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import List, Literal, Optional, Any, Dict, Tuple
import os, json, re
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

# OpenAI
from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError
from app.services.sf_labels import humanize_headers
from app.utils.soql_helpers import build_followup_accounts_query
import google.generativeai as genai
from google.ai.generativelanguage import Content, Part, FunctionCall, FunctionResponse
from google.protobuf.struct_pb2 import Struct

DEBUG = os.environ.get("AI_CHAT_DEBUG", "0") == "1"
INDEX_REFRESH_SEC = int(os.environ.get("AI_INDEX_REFRESH_SEC", "600"))
FIELDS_SF_JSON_PATH = os.environ.get("FIELDS_SF_JSON_PATH", "app/config/fields_opportunity_curated.json")
QUAL_ALIAS_JSON_PATH = os.environ.get("QUAL_ALIAS_JSON_PATH", "app/config/qualification_aliases.json")
EXPLORER_DRIVE_KM_PATH = "/api/explorer/search/within-drive-km"
EXPLORER_SEARCH_PATH   = "/api/explorer/search"

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

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
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
    "public.profiling_kv", "profiling_kv",
    "public.questionnaires", "questionnaires",
    "public.sections", "sections",
    "public.questions", "questions",
    "public.responses", "responses",
    "public.geonames_cities", "geonames_cities",
    "public.vw_site_metrics", "vw_site_metrics",
}


# ========= Índice unificado de métricas (SF + site_qual) =========

from time import time

_INDEX_CACHE: Dict[str, Any] = {"ts": 0, "index": {}, "sf_fields": {}}
# Limit for previewing aliases in the system hints (smaller → faster)
INDEX_PREVIEW_LIMIT = int(os.environ.get("AI_INDEX_PREVIEW_LIMIT", "60"))

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

def _top_matches(q: str, aliases: List[str], k: int = 5) -> List[str]:
    """
    Matching ligero: intersección de tokens normalizados + prefiero substrings.
    """
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
    if k in ("site","city","country","account_id"):
        return {"site":"Account Name","city":"City","country":"Country","account_id":"Account Id"}[k]
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
                # dynamic describe for Account
                if sf is not None:
                    acc_fields = _describe_fields(sf, 'Account')
                    if base not in {f'Account.{x}' for x in acc_fields} | SF_ALLOWED_DYNAMIC:
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
        "sf.Account.Name": "site",
        "sf.Account.ShippingCountry": "country",
        "sf.Account.ShippingCity": "city",
    }

    # 1) Recoge el orden original de claves visto en 'cols'
    orig_keys = [c.get("key") if isinstance(c, dict) else str(c) for c in cols]

    # 2) Calcula cuáles amigables existen realmente (por datos o por columnas)
    present = set()
    for r in norm_rows:
        if isinstance(r, dict):
            present.update(k for k, v in r.items() if v is not None)
    present.update(orig_keys)

    preferred = [k for k in ("account_id", "site", "country", "city") if k in present]

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
    url = str(request.base_url).rstrip("/") + EXPLORER_DRIVE_KM_PATH
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

def tool_explorer_search(
    request: Request,
    filters: Dict[str, Any],
    columns: Optional[List[str]] = None,
):
    """
    Proxy interno al endpoint /api/explorer/search.
    Acepta FilterGroup con reglas qual.*, sf.* y site.* y devuelve una tabla unificada.
    """
    url = str(request.base_url).rstrip("/") + EXPLORER_SEARCH_PATH
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


def tool_list_all_activities(sf):
    """List all Activities (RT_Activity/Activity Opportunities). Returns up to 15 names/ids."""
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
    
    # Query Activities: include either RecordType or Type='Activity', cap to 15
    soql = (
        f"SELECT Id, Name "
        f"FROM Opportunity "
        f"WHERE (RecordType.DeveloperName IN ({rt_vals}) OR Type = 'Activity') "
        f"ORDER BY Name LIMIT 15"
    )
    recs = tool_salesforce_query(sf, soql).get("records", [])
    
    rows = []
    for r in recs:
        rows.append({
            "activity_name": r.get("Name"),
            "activity_id": r.get("Id"),
        })
    
    return _normalize_table_for_ui({
        "columns": [
            {"key":"activity_name","label":"Activity"},
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
    for r in recs:
        oid = r.get("C_Opportunity_Name__c")
        c = (r.get("C_Account__r") or {}).get("ShippingCountry")
        if not oid:
            continue
        agg_counts[oid] = agg_counts.get(oid, 0) + 1
        if oid not in agg_countries:
            agg_countries[oid] = set()
        if c:
            agg_countries[oid].add(c)
    rows = []
    for oid, cnt in agg_counts.items():
        rows.append({
            "activity_name": id_to_name.get(oid) or oid,
            "assignments": cnt,
            "countries": ", ".join(sorted(list(agg_countries.get(oid) or set()))),
            "activity_id": oid,
        })
    rows.sort(key=lambda r: r.get("assignments", 0), reverse=True)
    return {
        "columns": [
            {"key":"activity_name","label":"Activity"},
            {"key":"assignments","label":"Assignments"},
            {"key":"countries","label":"Countries"},
            {"key":"activity_id","label":"Activity Id"},
        ],
        "rows": rows,
    }


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
        soql_acc = "SELECT Id, Name, ShippingCity, ShippingCountry FROM Account WHERE " + " AND ".join(where) + " LIMIT 2000"
        recs = sf.query_all(soql_acc).get("records", [])
        for r in recs:
            aid = r.get("Id"); acc_ids.append(aid)
            acc_info[aid] = {"name": r.get("Name"), "city": r.get("ShippingCity"), "country": r.get("ShippingCountry")}
    if not acc_ids:
        return {"columns": [], "rows": []}
    ids_clause = ", ".join([f"'{i}'" for i in acc_ids])
    # 2) Study Coordinators via ACR and Contact.Title
    roles_env = [s.strip() for s in (os.environ.get("SF_CONTACT_ROLES", "").split(",")) if s.strip()]
    role_list = roles if roles and len(roles) else roles_env
    where_title = f" OR Contact.Title LIKE '%Coordinat%'"
    if title_contains:
        te = title_contains.replace("'", "\\'")
        where_title = f" OR Contact.Title LIKE '%{te}%' {where_title}"
    sc_rows: List[Dict[str, Any]] = []
    if role_list:
        role_vals = ", ".join([f"'{r}'" for r in role_list])
        soql = (
            "SELECT AccountId, ContactId, Role__c, Contact.Name, Contact.Email, Contact.Phone, Contact.Title "
            "FROM AccountContactRelation "
            f"WHERE AccountId IN ({ids_clause}) AND Role__c IN ({role_vals})"
        )
        sc_rows += sf.query_all(soql).get("records", [])
    # Fallback: Contact by Title (strict coordinator patterns)
    # Coordinator patterns (multilingual) and strict title filtering
    patterns = [
        "Coordinat",       # Coordinator, Coordinating, Co-ordinator
        "Coördinat",      # Dutch with diaeresis
        "Coordinador",    # ES
        "Coordinateur",   # FR
        "Koordinator",    # DE
    ]
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
    # Build per-account coordinators list (with source)
    sc_by_acc: Dict[str, List[Dict[str, Any]]] = {}
    for r in sc_rows:
        aid = r.get("AccountId"); c = r.get("Contact") or {}
        if _is_sc(r.get("Role__c"), (c or {}).get("Title")):
            sc_by_acc.setdefault(aid, []).append({
                "name": c.get("Name"), "email": c.get("Email"), "phone": c.get("Phone"), "title": c.get("Title"),
                "sc_source": "acr_role", "sc_role": r.get("Role__c"), "contact_id": r.get("ContactId"),
            })
    for r in sc_contacts:
        aid = r.get("AccountId")
        if _is_sc(None, r.get("Title")):
            sc_by_acc.setdefault(aid, []).append({
                "name": r.get("Name"), "email": r.get("Email"), "phone": r.get("Phone"), "title": r.get("Title"),
                "sc_source": "contact_title", "sc_role": None, "contact_id": r.get("Id"),
            })
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
            "name": "sql_query",
            "description": "Run a read-only SQL SELECT over Postgres (RDS). Use ONLY for tabular facts.",
            "parameters": {
                "type":"object",
                "properties":{
                    "sql":{"type":"string","description":"SQL SELECT with named params (e.g. :country)"},
                    "params":{"type":"object","additionalProperties": True}
                },
                "required":["sql"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "salesforce_query",
            "description": "Run a SOQL SELECT on Opportunity (and Account.*). Only allowed fields.",
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
            "description": "Return a chart spec that the frontend will render directly.",
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
                "Search clinical trial sites using a combined FilterGroup that can mix qual.*, sf.* and site.* fields in one call. "
                "Use this tool when the query requires filtering on BOTH qualification checklist fields (qual.*) AND Salesforce fields (sf.*) simultaneously. "
                "Examples: 'sites with on-site pharmacy AND >50 ND patients ≥18', 'sites with overnight stay AND Stage2>10 in Spain'. "
                "The filter format is a nested AND/OR group. Operators: equals, not_equals, >, >=, <, <=, contains, in, not_in, is_empty, is_not_empty, between. "
                "qual.* field keys use the format 'qual.<section_key>' (e.g. 'qual.3_6__is_your_pharmacy_on_site_or_off_campus'). "
                "sf.* field keys use 'sf.<SalesforceField>' (e.g. 'sf.C_Number_of_ND_Patients_O_18__c'). "
                "site.* field keys: 'site.country', 'site.city'. "
                "After getting results you can call render_chart to visualize them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "description": (
                            "FilterGroup: {logic: 'AND'|'OR', rules: [{field, operator, value}, ...]}. "
                            "Rules can be nested groups. "
                            "Example: {logic:'AND', rules:["
                            "{field:'qual.3_6__is_your_pharmacy_on_site_or_off_campus', operator:'equals', value:'On-site'},"
                            "{field:'sf.C_Number_of_ND_Patients_O_18__c', operator:'>', value:50}"
                            "]}"
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
            "description": "Find neighboring sites within a driving distance (km) from a base Salesforce Account, using the Explorer service. Use this for 'within X km', 'nearby', 'distance' queries.",
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
            "name": "rank_sites",
            "description": "Top-N ranking of sites by a metric. ALWAYS use this for ranking queries. Returns account_id, site name, country, city, and the metric value. Works with both SF fields and site_qual keys/aliases (e.g., 'patients under 18', 'newly diagnosed', 'Stage 2').",
            "parameters": {
                "type":"object",
                "properties":{
                    "metric":{"type":"string","description":"Alias or raw key (e.g., 'patients under 18', 'newly diagnosed under 18', 'new T1D <18', 'C_Number_of_T1D_Patients_currently_U_18__c')"},
                    "top_n":{"type":"integer","default":5},
                    "order":{"type":"string","enum":["asc","desc"],"default":"desc"}
                },
                "required":["metric"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"rank_sites_by_group",
            "description":"Top-N ranking PER GROUP (e.g., top 3 per country, top 5 per city). ONLY use when user explicitly says 'per country' or 'per city' or 'by country' or 'by city'. For global rankings (e.g., 'top 5 sites'), use rank_sites instead. Works with both SF fields and site_qual keys/aliases. Returns account_id, site name, country, city, and the metric value per group.",
            "parameters":{
                "type":"object",
                "properties":{
                    "metric":{"type":"string","description":"Alias or raw key (e.g., 'patients under 18', 'newly diagnosed', 'Stage 2', 'C_Number_of_T1D_Patients_currently_U_18__c')"},
                    "group_by":{"type":"string","enum":["country","city"],"default":"country"},
                    "top_n":{"type":"integer","default":3},
                    "order":{"type":"string","enum":["asc","desc"],"default":"desc"}
                },
                "required":["metric"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"group_count",
            "description":"Count sites grouped by country/city with optional site_qual filter.",
            "parameters":{
                "type":"object",
                "properties":{
                    "by":{"type":"array","items":{"type":"string","enum":["country","city"]}},
                    "where":{"type":"object"}
                },
                "required":["by"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "group_count_agg",
            "description": "Aggregations per country/city (avg/sum on site_qual metrics or ratio_exists).",
            "parameters":{
                "type":"object",
                "properties":{
                    "by":{"type":"array","items":{"type":"string","enum":["country","city"]}},
                    "metric":{"type":"string"},
                    "agg":{"type":"string","enum":["avg","sum","ratio_exists"],"default":"avg"}
                },
                "required":["by","agg"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "group_count_sf",
            "description": "Count Salesforce Accounts grouped by country/city (ALWAYS filters SubAccount Clinical and active).",
            "parameters": {
                "type": "object",
                "properties": {
                    "by": {"type": "array", "items": {"type": "string", "enum": ["country","city"]}}
                },
                "required": ["by"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_activities",
            "description": "List all Activities (Opportunities with RecordType RT_Activity). Returns Opportunity Name, Account Name, Open Date, Stage, Type. Use this when user asks for 'list of activities' or 'what activities exist' or 'show all activities' WITHOUT asking about countries/sites.",
            "parameters": {
                "type": "object",
                "properties": {}
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
            "name": "group_agg_sf",
            "description": "Aggregate Salesforce Opportunity field by country/city (sum/max/avg), using Account geography.",
            "parameters": {
                "type": "object",
                "properties": {
                    "by": {"type": "array", "items": {"type": "string", "enum": ["country","city"]}},
                    "field": {"type": "string"},
                    "agg": {"type": "string", "enum": ["sum","max","avg"], "default": "avg"}
                },
                "required": ["by","field"]
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
            "name":"time_series_sf",
            "description":"Time series over Opportunity (SF) for a numeric field (sum/max/avg) grouped by month/quarter/year.",
            "parameters":{
                "type":"object",
                "properties":{
                    "field":{"type":"string"},
                    "date_field":{"type":"string","default":"CloseDate"},
                    "period":{"type":"string","enum":["month","quarter","year"],"default":"month"},
                    "agg":{"type":"string","enum":["sum","max","avg"],"default":"sum"},
                    "last_n":{"type":"integer"}
                },
                "required":["field"]
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
]

# ====== System prompt ======
SCHEMA_HINT = """
POSTGRES (warehouse):
- public.sites(id, name, street, city, country, postcode, latitude, longitude, salesforce_account_id)
- public.site_qual(site_id -> sites.id, data JSONB)  // Qualification flattened key→value.
- public.profiling_kv(site_id -> sites.id, key TEXT, value TEXT)  // Profiling key-value store.
  Frequent keys:
    'C_Aware_of_any_Screening_Program__c', 'C_Center_for_Running_Early_Diagnosis__c',
    'C_Number_of_Stage1_Individuals_followed__c', 'C_Number_of_Stage2_Individuals_followed__c',
    'C_Number_of_T1D_Patients_currently_U_18__c', 'C_Number_of_T1D_Patients_currently_O_18__c',
    'C_Number_of_new_T1D_diagnosed_U_18__c', 'C_Number_of_new_T1D_diagnosed_O_18__c',
    plus many comments (keys containing 'comment').
  JSONB tips:
    - Safe casts, e.g. COALESCE(NULLIF(sq.data->>'C_Number_of_T1D_Patients_currently_U_18__c','')::int, 0) AS t1d_u18
    - For YES/NO strings, normalize to LOWER and compare to 'yes'.
  PROFILING_KV tips:
    - Use LEFT JOIN profiling_kv ON profiling_kv.site_id = sites.id AND profiling_kv.key = :key
    - Numeric cast: COALESCE(NULLIF(regexp_replace(profiling_kv.value,'[^0-9\\.\\-]','', 'g'),'')::numeric,0)

SALESFORCE (runtime):
- salesforce_query supports: Opportunity, Account, Contact, AccountContactRelation
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
- **CTS** = Clinical Trial Site. Account flag: INNODIA_Clinical_Trial_Site__c = true (also C_Accredited_Clinical_Trial_Site__c for accredited ones)
- **CS** = Clinical Site. Account flag: Clinical_Site_CS__c = true
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

TOOLS — WHEN TO USE WHAT

**CRITICAL ROUTING RULES:**

🔴 **GOLDEN RULE**: Use Postgres (sql_query) ONLY for site_qual questions. Everything else MUST use Salesforce.
- “Activities” are Salesforce Opportunities with RecordType.DeveloperName='Activity' (or Type='Activity'). NEVER use site_qual for Activities.
- Contacts (PI/SC) and anything about Accounts/Opportunities MUST query Salesforce first.

1. **Postgres (sql_query) - ONLY FOR site_qual qualification checklist data:**
   - Questions from qualification uploads (e.g., "overnight stay", "onsite pharmacy", "examination rooms")
   - Keys like "3.x__..." (e.g., "3.5.2__overnight_stay")
   - Qualification comments search
   - **NEVER use for**: site lists, site counts, geography, patient metrics, contacts, trials

2. **Salesforce (salesforce_query) - For EVERYTHING ELSE:**
   - Site lists, counts, geography → Query Account (RecordType.DeveloperName='SubAccount', C_Type__c='Clinical')
   - Patient metrics, Stage 1/2, screening → Query Opportunity
   - Contacts, PI, Study Coordinators → Query Contact/AccountContactRelation
   - Country/City → ALWAYS use Account.ShippingCountry/ShippingCity (NEVER sites.country/city)
   - **ALWAYS filter inactive**: (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)

3. **Example Postgres queries (site_qual ONLY):**
   - "Sites with overnight stay" → sql_query: SELECT sites.salesforce_account_id, sq.data->>'3.5.2__overnight_stay' FROM sites JOIN site_qual sq ...
   - "Search qualification comments" → qual_search(text='...')
   
4. **Example Salesforce queries (everything else):**
   - "How many sites?" → salesforce_query: SELECT COUNT(Id) FROM Account WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' ...
   - "Sites in Spain" → salesforce_query: SELECT Id, Name, ShippingCity, ShippingCountry FROM Account WHERE ... AND ShippingCountry='Spain'
   - "Patient metrics" → salesforce_query: SELECT Account.Name, C_Number_of_T1D_Patients_currently_U_18__c FROM Opportunity ...

**TOOL REFERENCE (simplified):**
- salesforce_query → Use for 95% of queries: sites, counts, geography, patients, contacts, trials
- sql_query → Use ONLY for site_qual qualification questions that don't need SF fields (standalone qual queries)
- explorer_search → Use when combining qual.* AND sf.* filters in a single query (replaces sql_query+salesforce_query separately)
- qual_search → Semantic search over qualification comments
- salesforce_account_extras → PI, CS flags, assignments, new diagnoses per Account
- salesforce_account_contacts → Contact lists (PI, Study Coordinator, etc.)
- rank_sites → Top-N sites by metric (auto-detects SF vs site_qual)
- render_chart → Generate bar/line/pie charts (call after explorer_search to visualize results)
 
IMPORTANT: If an answer requires data, you MUST call at least one appropriate tool to fetch real rows. Do not invent numbers.

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
2) **table**: {{ "columns":[{{"key":"...","label":"..."}},...], "rows":[{{...}},...] }}
   - Use raw numeric values (no thousand separators).
3) **visualization** (optional): {{ "type":"bar|line|pie|scatter","xKey":"...","yKeys":["..."],"data":[...], "meta":{{"title":"..."}} }}
4) **explorer_set_filters** when asked to “filter/show on the map”.

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
- When using salesforce_query with Account: include Account.Id (the backend will surface it as sf.Account.Id/account_id).
- When using sql_query for qualification data: SELECT sites.salesforce_account_id AS account_id and include it in the table.

QUERY ROUTING EXAMPLES (critical - follow these patterns exactly):

**BLOCK 1: Basic site queries (ALWAYS Salesforce Account, NEVER Postgres sites)**
• "How many sites?" / "Total sites" / "Total count" → salesforce_query: SELECT COUNT(Id) total FROM Account WHERE RecordType.DeveloperName = 'SubAccount' AND C_Type__c = 'Clinical' AND (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)
• "Show sites in Spain" / "All sites in [country]" → salesforce_query: SELECT Id, Name, ShippingCity, ShippingCountry FROM Account WHERE RecordType.DeveloperName = 'SubAccount' AND C_Type__c = 'Clinical' AND ShippingCountry = 'Spain' AND (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) ORDER BY Name
• "How many sites per country?" / "Sites by country" → salesforce_query: SELECT ShippingCountry country, COUNT(Id) sites FROM Account WHERE RecordType.DeveloperName = 'SubAccount' AND C_Type__c = 'Clinical' AND ShippingCountry != null AND (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) GROUP BY ShippingCountry ORDER BY COUNT(Id) DESC
• "Sites per city" → salesforce_query: SELECT ShippingCity city, ShippingCountry country, COUNT(Id) sites FROM Account WHERE RecordType.DeveloperName = 'SubAccount' AND C_Type__c = 'Clinical' AND ShippingCity != null AND (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) GROUP BY ShippingCity, ShippingCountry ORDER BY COUNT(Id) DESC
  **CRITICAL**: NEVER use Postgres sites table for these queries. ALWAYS use Salesforce Account. The sites table is incomplete (only qualification uploads).

**BLOCK 2: Patient metrics (ALWAYS Salesforce Opportunity fields)**
• "Average T1D patients under 18 per site" → salesforce_query: SELECT AVG(C_Number_of_T1D_Patients_currently_U_18__c) FROM Opportunity WHERE C_Number_of_T1D_Patients_currently_U_18__c != null
• "Top 5 sites with most T1D patients under 18" → rank_sites(metric='T1D patients under 18', top_n=5, order='desc')
• "Sites with more than 10 new T1D diagnoses" → salesforce_query with WHERE C_Number_of_new_T1D_diagnosed_U_18__c > 10 OR C_Number_of_new_T1D_diagnosed_O_18__c > 10

**BLOCK 3: Qualification checklist questions (ONLY use Postgres for these)**
• "Overnight stay facilities" → sql_query: SELECT s.salesforce_account_id, s.name, sq.data->>'3.5.2__overnight_stay' FROM sites s JOIN site_qual sq ON s.id = sq.site_id WHERE sq.data->>'3.5.2__overnight_stay' ILIKE 'Yes'
• "Ongoing clinical trials" → sql_query: SELECT s.salesforce_account_id, sq.data->>'3.1__count_ongoing_clinical_trials' FROM sites s JOIN site_qual sq ON s.id = sq.site_id WHERE sq.data->>'3.1__count_ongoing_clinical_trials' ...
• "On-site pharmacy" → sql_query: SELECT s.salesforce_account_id, s.name, sq.data->>'3.6__is_your_pharmacy_on_site_or_off_campus' FROM sites s JOIN site_qual sq ON s.id = sq.site_id WHERE sq.data->>'3.6__is_your_pharmacy_on_site_or_off_campus' ILIKE 'On-site%'
• "Search comments for X" → qual_search(text='X')
  **IMPORTANT**: These are THE ONLY queries that should use sql_query. Everything else uses Salesforce.

**BLOCK 4: HLA typing and Clinical Trials (Salesforce field, NOT site_qual)**
• "Sites with HLA typing" / "% with HLA typing per country" → salesforce_query: C_Is_HLA_typing_performed__c field from Opportunity
• "Ongoing clinical trials" / "Phase I/II/III trials" → salesforce_query: Check fields C_Phase_I_Type1__c, C_Phase_II_Type1__c, C_Phase_III_Type1__c (counts) or C_List_of_trial_name_or_sponsors_Type1__c (names).
• NEVER use site_qual for HLA typing or Clinical Trials unless asking for 'interest in conducting' checklists.

**BLOCK 5: Stage 1/2 and screening (Salesforce)**
• "Stage 2 individuals" / "Top sites by Stage 2" → salesforce_query: C_Number_of_Stage2_Individuals_followed__c from Opportunity
• "Average Stage 2 per country" → salesforce_query with GROUP BY Account.ShippingCountry

**BLOCK 6: Time series (Salesforce)**
• "Opportunity amount by quarter/month" → time_series_sf(field='Amount', period='quarter'|'month', last_n=8)

**BLOCK 7: Contacts (Salesforce)**
• "Who is the PI for [site]?" → First get Account.Id, then salesforce_account_extras(account_id)
• "Study Coordinators in Belgium" → salesforce_account_contacts with title_contains='Study Coordinator' and filter by country

**BLOCK 8: Combined SF-only queries**
• "Sites in Spain with HLA typing and >10 patients" → salesforce_query with WHERE Account.ShippingCountry='Spain' AND C_Is_HLA_typing_performed__c='Yes' AND (C_Number_of_T1D_Patients_currently_U_18__c + C_Number_of_T1D_Patients_currently_O_18__c) > 10

**BLOCK 9: Combined qual + SF queries → USE explorer_search**
Use `explorer_search` when the query mixes qualification checklist fields (qual.*) AND Salesforce metrics (sf.*) or site location (site.*).
This tool calls the Explorer's filter engine which handles the JOIN internally — do NOT use sql_query + salesforce_query separately for these.

• "Sites with on-site pharmacy AND ND patients ≥18 > 50"
  → explorer_search(filters={{logic:'AND', rules:[
      {{field:'qual.3_6__is_your_pharmacy_on_site_or_off_campus', operator:'equals', value:'On-site'}},
      {{field:'sf.C_Number_of_new_T1D_diagnosed_O_18__c', operator:'>', value:50}}
    ]}})

• "Sites with overnight stay AND Stage2 > 10 in Spain"
  → explorer_search(filters={{logic:'AND', rules:[
      {{field:'qual.3_5_2__overnight_stay', operator:'equals', value:'Yes'}},
      {{field:'sf.C_Number_of_Stage2_Individuals_followed__c', operator:'>', value:10}},
      {{field:'site.country', operator:'equals', value:'Spain'}}
    ]}})

• "Sites with pharmacy OR overnight stay"
  → explorer_search(filters={{logic:'OR', rules:[
      {{field:'qual.3_6__is_your_pharmacy_on_site_or_off_campus', operator:'equals', value:'On-site'}},
      {{field:'qual.3_5_2__overnight_stay', operator:'equals', value:'Yes'}}
    ]}})

• "Sites in Italy that have a pharmacy AND more than 100 T1D patients currently over 18"
  → explorer_search(filters={{logic:'AND', rules:[
      {{field:'site.country', operator:'equals', value:'Italy'}},
      {{field:'qual.3_6__is_your_pharmacy_on_site_or_off_campus', operator:'equals', value:'On-site'}},
      {{field:'sf.C_Number_of_T1D_Patients_currently_O_18__c', operator:'>', value:100}}
    ]}})

KEY qual field keys for explorer_search (use these exact values in the field property):
- Pharmacy on site: qual.3_6__is_your_pharmacy_on_site_or_off_campus  (value: 'On-site' or 'On-site pharmacy')
- Overnight stay: qual.3_5_2__overnight_stay  (value: 'Yes')
- Number of exam rooms: qual.3_3__number_of_examination_rooms  (operator: '>', value: numeric)
- Ongoing clinical trials: qual.3_1__count_ongoing_clinical_trials
Use INDEX above for other qual field keys (format: alias => qual.<key>).

KEY sf field keys for explorer_search:
- ND patients ≥18: sf.C_Number_of_new_T1D_diagnosed_O_18__c
- ND patients <18: sf.C_Number_of_new_T1D_diagnosed_U_18__c
- Current T1D ≥18: sf.C_Number_of_T1D_Patients_currently_O_18__c
- Current T1D <18: sf.C_Number_of_T1D_Patients_currently_U_18__c
- Stage 2: sf.C_Number_of_Stage2_Individuals_followed__c
- Stage 1: sf.C_Number_of_Stage1_Individuals_followed__c
- HLA typing: sf.C_Is_HLA_typing_performed__c  (value: 'Yes')
- Country: sf.Account.ShippingCountry  OR  site.country

AFTER explorer_search → you can call render_chart on the returned rows to produce a chart.
FOLLOW-UP on explorer_search results → if user says "of those, only in Spain", call explorer_search again adding the new rule to the same filters.

**BLOCK 10: Direct Account/Contact/Assignment queries (when needed)**
• "Show me all clinical subaccounts" → salesforce_query: SELECT Id, Name, ParentId, RecordType.DeveloperName, C_Type__c FROM Account WHERE RecordType.DeveloperName = 'SubAccount' AND C_Type__c = 'Clinical'
  NOTE: Use BLOCK 9 (explorer_search) for any query that combines qual.* with sf.* — do not use sql_query+salesforce_query separately.
• "List all contacts with email" → salesforce_query: SELECT Id, Name, Email, Phone, Title, Department, AccountId FROM Contact WHERE Email != null
• "Show account hierarchy for [account]" → salesforce_query: SELECT Id, Name, ParentId, C_Type__c FROM Account WHERE ParentId = '[parent_id]'
  NOTE: Use Account queries for all site lists/counts/geography (BLOCK 1). Use Opportunity for patient metrics (BLOCK 2). NEVER use Postgres sites table for basic counts.
• "Show assignments for [account/opportunity]" → salesforce_query: SELECT Id, Name, Assignment_Type__c, C_Assignment_Stage__c, C_MCA_Status__c, C_Payment_Done__c, C_Account__r.Name, C_Contact_Name__r.Name FROM Assignment__c WHERE C_Account__c = '[account_id]'
• "Assignments pending payment" → salesforce_query: SELECT Id, Name, C_Assignment_Stage__c, C_Payment_Done__c, C_Account__r.Name FROM Assignment__c WHERE C_Payment_Done__c = false
• "ND by country" → salesforce_query: SELECT Account.ShippingCountry, SUM(C_Number_of_new_T1D_diagnosed_O_18__c), SUM(C_Number_of_new_T1D_diagnosed_U_18__c) FROM Opportunity WHERE ... GROUP BY Account.ShippingCountry
• "DxLab sites" → salesforce_query: SELECT Id, Name, ShippingCountry FROM Account WHERE C_Deliver_Clinical_Grade_Services__c = true AND (Account_Inactive__c = false OR Account_Inactive__c = null)
• "CTS sites accredited" → salesforce_query: SELECT Id, Name, ShippingCountry FROM Account WHERE INNODIA_Clinical_Trial_Site__c = true AND C_Accredited_Clinical_Trial_Site__c = true

**BLOCK 11: Activity → Sites queries (via Assignment__c)**
Activities are Opportunities with RecordType.DeveloperName = 'RT_Activity'.
Sites participate in an Activity through Assignment__c records (Account_Assignment type).
The link chain is: Activity (Opportunity) ← C_Opportunity_Name__c — Assignment__c — C_Account__c → Account (Site).

KNOWN ACTIVITIES (exact Opportunity names — use LIKE for partial matches):
- "DETECT Pilot Sites" | "DETECT French Roll Out" | "DETECT Italian Roll Out"
- "Fabulinus CTS Team - Part A" | "Fabulinus CTS Team - Part B" | "FABULINUS Referral Partner Network"
- "Baricade Delay (JAJJ)" | "Baricade Preserve (JAJK)" | "Beta Preserve"
- "Diagnode-3 RP Team" | "Safeguard Trial Clinical Sites"

SOQL PATTERN for "sites in activity X":
```
SELECT C_Account__r.Id, C_Account__r.Name, C_Account__r.ShippingCountry, C_Account__r.ShippingCity,
       C_Assignment_Stage__c, Assignment_Type__c, C_Opportunity_Name__r.Name
FROM Assignment__c
WHERE C_Opportunity_Name__r.Name LIKE '%X%'
AND C_Opportunity_Name__r.RecordType.DeveloperName = 'RT_Activity'
AND RecordType.DeveloperName = 'Account_Assignment'
ORDER BY C_Account__r.ShippingCountry, C_Account__r.Name
```

AMBIGUITY HANDLING:
• If name matches multiple activities (e.g. "Fabulinus" → Part A + Part B + RP Network):
  → Return ALL grouped by activity name. Do NOT ask for clarification unless the user explicitly wants only one.
  → Add a summary bullet: "Found X sites across N activities: [activity names]"
• If "Detect" is queried: return all 3 DETECT activities grouped, with a count per sub-activity.

EXAMPLES:
• "Sites in Detect" / "Which sites participate in Detect?"
  → salesforce_query: SELECT C_Account__r.Id, C_Account__r.Name, C_Account__r.ShippingCountry, C_Account__r.ShippingCity, C_Opportunity_Name__r.Name, C_Assignment_Stage__c FROM Assignment__c WHERE C_Opportunity_Name__r.Name LIKE '%DETECT%' AND C_Opportunity_Name__r.RecordType.DeveloperName = 'RT_Activity' AND RecordType.DeveloperName = 'Account_Assignment' ORDER BY C_Opportunity_Name__r.Name, C_Account__r.ShippingCountry

• "Sites in Fabulinus Part A"
  → salesforce_query: ...WHERE C_Opportunity_Name__r.Name LIKE '%Fabulinus%Part A%'...

• "Sites in Fabulinus" (ambiguous → return both parts)
  → salesforce_query: ...WHERE C_Opportunity_Name__r.Name LIKE '%Fabulinus%' AND RecordType.DeveloperName = 'Account_Assignment'...
  → Group results in the answer by activity name (Part A / Part B / RP Network)

• "How many sites in Baricade?"
  → salesforce_query: SELECT C_Opportunity_Name__r.Name activity, COUNT(Id) sites FROM Assignment__c WHERE C_Opportunity_Name__r.Name LIKE '%Baricade%' AND C_Opportunity_Name__r.RecordType.DeveloperName = 'RT_Activity' AND RecordType.DeveloperName = 'Account_Assignment' GROUP BY C_Opportunity_Name__r.Name

• "All activities and their site counts"
  → salesforce_query: SELECT C_Opportunity_Name__r.Name activity, COUNT(Id) sites FROM Assignment__c WHERE C_Opportunity_Name__r.RecordType.DeveloperName = 'RT_Activity' AND RecordType.DeveloperName = 'Account_Assignment' GROUP BY C_Opportunity_Name__r.Name ORDER BY COUNT(Id) DESC

• "Is [site name] in Fabulinus?"
  → salesforce_query: SELECT Id, C_Opportunity_Name__r.Name FROM Assignment__c WHERE C_Account__r.Name LIKE '%[site]%' AND C_Opportunity_Name__r.Name LIKE '%Fabulinus%' AND C_Opportunity_Name__r.RecordType.DeveloperName = 'RT_Activity'

TABLE COLUMNS for activity queries: Activity, Site, Country, City, Assignment Stage (C_Assignment_Stage__c)

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
"""

# --- Adapter for Gemini ---
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

def _strip_unsupported_fields(schema: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(schema, dict):
        return schema
    # Remove additionalProperties and default as Gemini doesn't support them in FunctionDeclaration wrapper
    new_schema = {k: v for k, v in schema.items() if k not in ("additionalProperties", "default")}
    
    # Recurse for properties
    if "properties" in new_schema and isinstance(new_schema["properties"], dict):
         new_schema["properties"] = {
             pk: _strip_unsupported_fields(pv) 
             for pk, pv in new_schema["properties"].items()
         }
         # Remove properties if empty to avoid Schema validation errors
         if not new_schema["properties"]:
             del new_schema["properties"]
    
    # Clean 'required' list: remove keys that are not in 'properties'
    if "required" in new_schema and isinstance(new_schema["required"], list):
        props = new_schema.get("properties", {})
        new_schema["required"] = [k for k in new_schema["required"] if k in props]
        if not new_schema["required"]:
            del new_schema["required"]

    # Recurse for items (arrays)
    if "items" in new_schema and isinstance(new_schema["items"], dict):
        new_schema["items"] = _strip_unsupported_fields(new_schema["items"])

    # Recurse for type object
    if new_schema.get("type") == "object" and "properties" not in new_schema:
        # If object has no properties, it might be treated as generic Dict, ensuring no required
        if "required" in new_schema:
             del new_schema["required"]
        
    return new_schema

def _convert_tools_to_gemini(tools_spec):
    # Gemini uses a list of FunctionDeclarations wrapped in a 'tools' list
    funcs = []
    for t in tools_spec:
        f = t["function"]
        # Basic mapping. Gemini handles the JSON schema in 'parameters' quite well.
        # BUT we must strip 'additionalProperties' which causes ValueError
        params = f.get("parameters")
        if params:
            params = _strip_unsupported_fields(params)
            
        funcs.append(genai.types.FunctionDeclaration(
            name=f["name"],
            description=f.get("description"),
            parameters=params
        ))
    return funcs

def _validate_and_filter_tools(funcs):
    """
    Iteratively validates function declarations by trying to create a Tool with each one.
    Returns only the valid functions.
    """
    valid_funcs = []
    for f in funcs:
        try:
            # Try creating a Tool with just this single function
            # Note: Tool constructor expects a list of FunctionDeclarations
            genai.types.Tool(function_declarations=[f])
            valid_funcs.append(f)
        except Exception as e:
            _dbg("SKIPPING IMPOSSIBLE TOOL '%s': %s", f.name, e)
    
    if len(valid_funcs) < len(funcs):
        _dbg("Dropped %d tools due to schema validation errors.", len(funcs) - len(valid_funcs))
    
    return valid_funcs

def _gemini_chat(
    messages: List[Dict[str, Any]],
    tool_choice: str = "required",
    *,
    force_no_tools: bool = False,
):
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        _dbg("ERROR: GOOGLE_API_KEY not found. Fallback or fail.")
        # If no key, maybe fallback to OpenAI or raise error? 
        # For now let's raise error so user knows setup is incomplete
        raise Exception("GOOGLE_API_KEY not set")

    genai.configure(api_key=api_key)
    
    # 1. Extract System Instruction & Map Tool IDs
    system_instruction = None
    tool_id_map = {} # map tool_call_id -> function_name

    # First pass to find tool call ids
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                # tc is an object from OpenAI SDK or a dict? structure in ai_chat logic uses objects usually?
                # current logic in ai_chat constructs manual dicts for history?
                # Check lines 5119: msgs.append({"role":"tool"...})
                # Check lines 4964: assistant_msg.tool_calls (object)
                # But when appending to history (line 4998), it appends assistant_msg.content (str)
                # Wait, does ai_chat append the full tool_calls object to history?
                # Line 4998: msgs.append({"role": "assistant", "content": ..., "tool_calls": ...})?
                # I need to check how it saves assistant message.
                # Assuming standard OpenAI format: dict with 'tool_calls' list
                if hasattr(tc, 'id'):
                    tool_id_map[tc.id] = tc.function.name
                elif isinstance(tc, dict):
                     tool_id_map.get(tc.get("id"), tc.get("function", {}).get("name"))
                     # Dict structure: {'id': '...', 'function': {'name': '...'}}
                     tool_id_map[tc["id"]] = tc["function"]["name"]

    # 2. Build Gemini History
    history = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        
        if role == "system":
            system_instruction = content
        elif role == "user":
            history.append({"role": "user", "parts": [content]})
        elif role == "assistant":
            parts = []
            if content:
                parts.append(content)
            
            tcs = m.get("tool_calls")
            if tcs:
                for tc in tcs:
                    # Construct FunctionCall part
                    if hasattr(tc, 'function'): # Object
                        fname = tc.function.name
                        fargs = json.loads(tc.function.arguments)
                        # We also update map here if missed
                        if hasattr(tc, 'id'): tool_id_map[tc.id] = fname
                    else: # Dict
                        fname = tc["function"]["name"]
                        fargs = json.loads(tc["function"]["arguments"])
                        if "id" in tc: tool_id_map[tc["id"]] = fname
                    
                    parts.append(genai.types.content_types.Part(
                        function_call=genai.types.content_types.FunctionCall(name=fname, args=fargs)
                    ))
            
            history.append({"role": "model", "parts": parts})
            
        elif role == "tool":
            # Gemini 'function_response' must be sent by User
            tid = m.get("tool_call_id")
            fname = tool_id_map.get(tid)
            if not fname:
                _dbg("WARN: Unknown tool_call_id %s, skipping", tid)
                continue
            
            # Content is JSON string
            try:
                response_data = json.loads(content)
            except:
                response_data = {"result": str(content)}
                
            history.append({
                "role": "user",
                "parts": [
                    genai.types.content_types.Part(
                        function_response=genai.types.content_types.FunctionResponse(name=fname, response=response_data)
                    )
                ]
            })

    # 3. Configure Model
    # Gemini 1.5 Flash is fast and cheap
    # Tools
    tools_arg = []
    if not force_no_tools:
        # Pasa lista plana para evitar errores de constructor Tool(list)
        full_funcs = _convert_tools_to_gemini(TOOLS_SPEC)
        tools_arg = _validate_and_filter_tools(full_funcs)

    # Prefer GOOGLE_MODEL (user env), then GEMINI_MODEL (legacy), default to available model gemini-3-pro-preview
    model_name = os.environ.get("GOOGLE_MODEL", os.environ.get("GEMINI_MODEL", "gemini-3-pro-preview"))
    model = None
    
    # Intento 1: Con herramientas
    try:
        if tools_arg:
            # Create a Tool object from the list of FunctionDeclarations
            tool_obj = genai.types.Tool(function_declarations=tools_arg)
            model = genai.GenerativeModel(model_name, tools=[tool_obj], system_instruction=system_instruction)
    except Exception as e:
        _dbg("Gemini Tool Init Failed (falling back to no tools): %s", e)
        # Fallback explícito a sin herramientas
        tools_arg = []
    
    # Intento 2 (o por defecto): Sin herramientas si falló el anterior o no había
    if model is None:
        try:
            model = genai.GenerativeModel(model_name, tools=None, system_instruction=system_instruction)
        except Exception as e:
            _dbg("Gemini Model Init Critical Failure: %s", e)
            return OpenAICompatibleResponse(OpenAICompatibleMessage(f"System Error: Failed to initialize AI model. ({str(e)})"))

    # Tool Config
    # tool_choice='required' -> ANY
    # tool_choice='auto' -> AUTO
    tool_config = None
    if not force_no_tools and tools_arg: # Only apply tool_config if tools were successfully initialized
        mode = "AUTO"
        if tool_choice == "required":
            mode = "ANY"
        tool_config = {"function_calling_config": {"mode": mode}}
    
    # 4. Generate
    try:
        response = model.generate_content(history, tool_config=tool_config if tools_arg else None)
    except Exception as e:
        import traceback
        from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable, NotFound
        
        err_msg = f"AI Service Error: {str(e)}"
        if isinstance(e, ResourceExhausted):
            err_msg = "Quota Exceeded: The free tier quota for Gemini API has been exhausted. Please try again later."
        elif isinstance(e, ServiceUnavailable):
            err_msg = "Service Unavailable: Gemini API is currently down. Please try again later."
        elif isinstance(e, NotFound):
             err_msg = f"Model Not Found: {model_name} is unavailable or invalid."

        _dbg("Gemini Generate Error: %s\n%s", e, traceback.format_exc())
        return OpenAICompatibleResponse(OpenAICompatibleMessage(err_msg))

    # 5. Adapt Response to OpenAI format
    # Response might have text, or function calls, or both.
    
    try:
        cand = response.candidates[0]
        parts = cand.content.parts
    except:
         # Blocked or empty?
        _dbg("Gemini Empty/Blocked Response: %s", response)
        return OpenAICompatibleResponse(OpenAICompatibleMessage("I'm sorry, I couldn't generate a response."))
        
    final_text = []
    final_tool_calls = []
    
    # Mock class for ToolCall
    class MockFunction:
        def __init__(self, name, args_str):
            self.name = name
            self.arguments = args_str
    class MockToolCall:
        def __init__(self, id, name, args_str):
            self.id = id
            self.type = 'function'
            self.function = MockFunction(name, args_str)
            
        def model_dump(self):
            return {
                "id": self.id,
                "type": self.type,
                "function": {
                    "name": self.function.name,
                    "arguments": self.function.arguments
                }
            }

    for p in parts:
        if p.text:
            final_text.append(p.text)
        if p.function_call:
            # We need a dummy ID because OpenAI expects one to link the response later
            # We can generate a UUID or just use 'call_<random>'
            import uuid
            call_id = f"call_{str(uuid.uuid4())[:8]}"
            # args to json string
            # Convert protobuf args to dict recursively to handle RepeatedComposite
            def _proto_to_dict(obj):
                if hasattr(obj, "items"):
                    return {k: _proto_to_dict(v) for k, v in obj.items()}
                elif hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
                    return [_proto_to_dict(v) for v in obj]
                return obj

            args_dict = _proto_to_dict(p.function_call.args)
            args_str = json.dumps(args_dict)
            final_tool_calls.append(MockToolCall(call_id, p.function_call.name, args_str))
            
    content_str = "\n".join(final_text) if final_text else None
    
    # Debug
    _dbg("Gemini Response: text=%d chars, tools=%d", len(content_str or ""), len(final_tool_calls))
    
    return OpenAICompatibleResponse(OpenAICompatibleMessage(content_str, final_tool_calls if final_tool_calls else None))

# ====== Endpoint ======
def _sf_escape_value(v: str) -> str:
    try:
        return str(v).replace("'", "\\'")
    except Exception:
        return ""


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

    # Clarificador determinista para consultas ambiguas de "pacientes"
    def _needs_patient_clarification(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        s = (text or "").lower()
        _dbg(f"NeedsPatientClarification input: '{s}'")
        # Presencia del concepto pacientes
        has_pat = re.search(r"\b(pacientes|patients?|t1d)\b", s) is not None
        if not has_pat:
            return None
        # Señales de desambiguación ya especificadas
        # "paediatric"/"pediatric" implies under 18 -> no clarification needed
        if re.search(r"\b(pa?ediatric)\b", s):
            _dbg("Skipping clarification: 'paediatric' detected")
            return None
        specified_type = re.search(r"\b(new|newly|diagnos(ed)?|dx|actual(es)?|current(ly)?)\b", s) is not None
        specified_stage = re.search(r"\bstage\s*[12]\b", s) is not None
        specified_age = re.search(r"(<\s*18|\bunder\s*18\b|u\s*18|≥\s*18|\bo(ver)?\s*18\b|o\s*18|\b18\s*or\s*older)\b", s) is not None
        # Queries sobre agregaciones (average, total, sum) con edad especificada son suficientemente claras
        has_aggregation = re.search(r"\b(average|avg|total|sum|count|how\s+many)\b", s) is not None
        # Si especifica la edad, no pedir clarificación (asumimos "current" si no se especifica)
        if specified_age:
            return None
        # Si especifica el tipo Y la edad, no pedir clarificación
        if specified_type and specified_age:
            return None
        # Si especifica stage, tampoco pedir clarificación
        if specified_stage:
            return None
        # Si es una agregación con edad, no pedir clarificación
        if has_aggregation and specified_age:
            return None
        # Construye objeto clarify
        question = "Which type of patients are you referring to?"
        options = [
            {"label": "Currently under 18", "query": "currently T1D patients under 18"},
            {"label": "Currently 18 or older", "query": "currently T1D patients 18 or older"},
            {"label": "Newly diagnosed under 18", "query": "newly diagnosed T1D patients under 18"},
            {"label": "Newly diagnosed 18 or older", "query": "newly diagnosed T1D patients 18 or older"},
            {"label": "Currently (both ages)", "query": "currently T1D patients all ages"},
            {"label": "Newly diagnosed (both ages)", "query": "newly diagnosed T1D patients all ages"},
        ]
        return {"answer": "<p>Need one more detail.</p>", "clarify": {"question": question, "options": options}}

    # Aplica clarificadores
    try:
        last_text = payload.messages[-1].content if payload.messages else ""
        for _fn in (_needs_contact_clarification, _needs_report_wizard, _needs_patient_clarification):
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
    def _try_planner(user_text: str, has_table: bool = False) -> Optional[Dict[str, Any]]:
        if not user_text:
            return None
        s = user_text.lower()
        
        # INTENCIÓN DIRECTA: Study Coordinator(s) → llamar determinísticamente a study_coordinators_with_activities
        try:
            sc_intent = re.search(r"\bstudy\s*coordinators?\b", s) is not None
            pi_intent = re.search(r"\bprincipal\s*investigators?\b|\b\bpi\b", s) is not None
            if (sc_intent or pi_intent) and sf:
                # Parse optional country list: "in Germany and Spain"
                # Capture 'in <country>' anywhere, optionally followed by 'site(s)'
                m_c = re.search(r"\bin\s+([a-zA-ZÀ-ÿ'\- ,/&]+?)(?:\s+sites?\b|$)", s)
                countries = []
                if m_c:
                    raw = m_c.group(1).strip()
                    raw = re.sub(r"\b(and|y|e)\b", ",", raw, flags=re.I)
                    raw = raw.replace("/", ",").replace("&", ",")
                    parts = [p.strip() for p in raw.split(",") if p.strip()]
                    countries = [p.title() for p in parts]
                # Fallback: carry country context from last table if none provided
                try:
                    ltp = payload.last_table if hasattr(payload, 'last_table') else None
                    if (not countries) and ltp and isinstance(ltp.get("rows"), list):
                        vals = sorted({str(r.get("country") or "").strip() for r in ltp.get("rows") if isinstance(r, dict) and r.get("country")})
                        if 0 < len(vals) <= 3:
                            countries = vals
                except Exception:
                    pass
                if sc_intent:
                    table = tool_study_coordinators_with_activities(
                        sf,
                        account_ids=[],
                        countries=countries,
                        title_contains="Study Coordinator",
                        roles=[],
                        include_subaccounts=True,
                    )
                else:
                    # PI intent: prefer ACR role 'PI' and also match title
                    rows = tool_salesforce_account_contacts(
                        sf,
                        account_id="",
                        include_subaccounts=True,
                        role_contains=None,
                        roles=["PI"],
                        title_contains="Principal Investigator",
                        country=countries[0] if countries else None,
                        city=None,
                    )
                    table = rows
                rows = table.get("rows") or []
                accs = len({str(r.get("account_id")) for r in rows if isinstance(r, dict) and r.get("account_id")})
                ans = f"Found {len(rows)} coordinator(s) across {accs} account(s)."
                return {"answer": f"<p>{ans}</p>", "table": table}
        except Exception as _e:
            _dbg("WARN: planner study_coordinators failed: %s", _e)

        # INTENCIÓN DIRECTA: Activities
        try:
            # Acepta 'activities', 'activity' y typos frecuentes como 'activites'
            if sf and (re.search(r"\bactivit(?:y|ies)\b", s) or re.search(r"\bactivites\b", s) or ("activit" in s)):
                # 0) Prioridad: frases sobre ASSIGNMENTS (evita que 'list activities' intercepte)
                if re.search(r"activities?\s+with\s+assignments?", s):
                    # Parse filters (país, nombre quoted, fechas)
                    m_c = re.search(r"\bin\s+([a-zA-ZÀ-ÿ'\- ,/&]+)$", s)
                    countries = []
                    if m_c:
                        raw = m_c.group(1).strip()
                        raw = re.sub(r"\b(and|y|e)\b", ",", raw, flags=re.I)
                        raw = raw.replace("/", ",").replace("&", ",")
                        countries = [p.strip() for p in raw.split(",") if p.strip()]
                    m_q = re.search(r"['\"]([^'\"]{3,})['\"]", s)
                    name_like = m_q.group(1).strip() if m_q else None
                    m_last = re.search(r"last\s+(\d{1,3})\s*(days|day|months|month)", s)
                    last_days = last_months = None
                    if m_last:
                        n = int(m_last.group(1)); unit = m_last.group(2)
                        last_days = n if unit.startswith("day") else None
                        last_months = n if unit.startswith("month") else last_months
                    m_since = re.search(r"since\s+(\d{4}-\d{2}-\d{2})", s)
                    m_until = re.search(r"(until|to)\s+(\d{4}-\d{2}-\d{2})", s)
                    since_d = m_since.group(1) if m_since else None
                    until_d = (m_until.group(2) if m_until else None)
                    # Regla: detalle sólo si se pide explícitamente 'assignments list' o 'detailed'
                    wants_detail = bool(re.search(r"(with\s+assignments?\s+list|assignments?\s+list|\bdetailed\b|\bdetalle\b)", s))
                    if wants_detail:
                        tbl = tool_activity_assignments_detailed(
                            sf,
                            countries or None,
                            name_like,
                            last_n_days=last_days,
                            last_n_months=last_months,
                            since=since_d,
                            until=until_d,
                        )
                        return {"answer": "<p>Assignments per activity.</p>", "table": tbl}
                    else:
                        tbl = tool_activities_with_assignments_counts(
                            sf,
                            last_n_days=last_days,
                            last_n_months=last_months,
                            since=since_d,
                            until=until_d,
                        )
                        return {"answer": "<p>Activities with assignment counts.</p>", "table": tbl}
                # 1) Resto de intenciones con activities
                # Distinguish: "list activities" vs "sites with activities" or "activities by country"
                # Robust detection: tolerate typos like "acitvites"/"activties"
                has_list_word = bool(re.search(r"\b(list|show|all|what|which|lista|listar|mostrar|muestra)\b", s))
                has_activity_token = ("activ" in s) or bool(re.search(r"\bactivit(?:y|ies)\b|\bactividades\b", s))
                mentions_group_or_location = bool(re.search(r"\b(site|sites|sitio|sitios|country|countries|ciudad|ciudades|pa[ií]s|pa[ií]ses|by|per|por)\b", s))
                is_list_activities = has_list_word and has_activity_token and not mentions_group_or_location
                
                if is_list_activities:
                    # Just list the Activities themselves (names only, max 15)
                    table = tool_list_all_activities(sf)
                    rows = table.get("rows") or []
                    ans = f"Found {len(rows)} activity/activities."
                    return {"answer": f"<p>{ans}</p>", "table": table}
                else:
                    # Summary of activities (by activity name) → aggregated table (sites_total, countries)
                    if re.search(r"\b(summary|resumen|overview)\b", s) and re.search(r"activit", s):
                        tbl = tool_activity_country_matrix(sf, stacked=False)
                        rows = tbl.get("rows") or []
                        top = sorted(rows, key=lambda x: int(x.get("sites_total") or 0), reverse=True)[:3]
                        top_txt = ", ".join([f"{t.get('activity_name')}: {t.get('sites_total')}" for t in top]) if rows else ""
                        ans = f"{len(rows)} activities. Top: {top_txt}." if rows else "Activities summary."
                        return {"answer": f"<p>{ans}</p>", "table": tbl}
                    # Join: assignments + count of sites per activity
                    if re.search(r"assignments?", s) and re.search(r"\bcount", s) and re.search(r"sites?", s):
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
                        out = {"columns":[
                            {"key":"activity_name","label":"Activity"},
                            {"key":"assignments","label":"Assignments"},
                            {"key":"sites_total","label":"Sites"},
                            {"key":"countries","label":"Countries"},
                            {"key":"activity_id","label":"Activity Id"},
                        ], "rows": out_rows}
                        return {"answer": "<p>Assignments and site counts per activity.</p>", "table": out}
                    # Explicit: 'countries involved' or 'country breakdown' → detailed per-activity countries + summary + chart
                    if re.search(r"countries?\s+involv(ed|idos)|countries?\s+in\b|country\s+breakdown", s):
                        tbl = tool_activities_with_countries(sf)
                        rows_tbl = tbl.get("rows") or []
                        top = sorted(rows_tbl, key=lambda x: int(x.get("site_count") or 0), reverse=True)[:3]
                        top_txt = ", ".join([f"{t.get('opportunity_name')}: {t.get('site_count')}" for t in top])
                        ans = f"{len(rows_tbl)} activities. Top: {top_txt}." if rows_tbl else "Activities with participating countries."
                        # Also stacked viz
                        rows_s = tool_activity_country_matrix(sf, stacked=True).get("rows") or []
                        countries = sorted(set(r.get("country") for r in rows_s if r.get("country")))
                        stacked_data = []
                        activities = sorted(set(r.get("activity_name") for r in rows_s if r.get("activity_name")))
                        for act in activities:
                            rowd = {"activity_name": act}
                            for c in countries:
                                rowd[c] = sum(r.get("sites",0) for r in rows_s if r.get("activity_name")==act and r.get("country")==c)
                            stacked_data.append(rowd)
                        viz = tool_render_chart("bar", stacked_data, "activity_name", countries[:8], {"title":"Countries per Activity (Stacked)"}, {})
                        viz_cols = [{"key":"activity_name","label":"Activity"}] + [{"key": c, "label": _pretty_label(c)} for c in countries[:8]]
                        viz_tbl = {"columns": viz_cols, "rows": stacked_data}
                        return {"answer": f"<p>{ans}</p>", "table": tbl, "visualization": viz.get("visualization")}
                    # If the intent mentions country grouping → aggregate by country
                    if re.search(r"\b(by|per|por)\s+(country|pa[ií]s|pa[ií]ses)\b", s):
                        tbl = tool_activity_counts_by_country(sf)
                        rows = tbl.get("rows") or []
                        total = sum(int(r.get("activities") or 0) for r in rows)
                        top = sorted(rows, key=lambda x: int(x.get("activities") or 0), reverse=True)[:3]
                        top_txt = ", ".join([f"{t.get('country')}: {t.get('activities')}" for t in top])
                        viz = tool_render_chart("bar", rows, "country", ["activities"], {"title": "Activities by Country"}, {})
                        return {"answer": f"<p>Total {total}. Top: {top_txt}.</p>", "table": tbl, "visualization": viz.get("visualization")}
                    # 'activities with assignments' →
                    if re.search(r"activities?\s+with\s+assignments?", s):
                        # Parse optional filters: countries ("in ...") and activity name (quoted)
                        m_c = re.search(r"\bin\s+([a-zA-ZÀ-ÿ'\- ,/&]+)$", s)
                        countries = []
                        if m_c:
                            raw = m_c.group(1).strip()
                            raw = re.sub(r"\b(and|y|e)\b", ",", raw, flags=re.I)
                            raw = raw.replace("/", ",").replace("&", ",")
                            countries = [p.strip() for p in raw.split(",") if p.strip()]
                        m_q = re.search(r"['\"]([^'\"]{3,})['\"]", s)
                        name_like = m_q.group(1).strip() if m_q else None
                        # Parse date filters
                        m_last = re.search(r"last\s+(\d{1,3})\s*(days|day|months|month)", s)
                        last_days = last_months = None
                        if m_last:
                            n = int(m_last.group(1))
                            unit = m_last.group(2)
                            if unit.startswith("day"):
                                last_days = n
                            else:
                                last_months = n
                        m_since = re.search(r"since\s+(\d{4}-\d{2}-\d{2})", s)
                        m_until = re.search(r"until\s+(\d{4}-\d{2}-\d{2})|to\s+(\d{4}-\d{2}-\d{2})", s)
                        since_d = m_since.group(1) if m_since else None
                        until_d = (m_until.group(1) or m_until.group(2)) if m_until else None
                        # Only detailed when 'assignments list' or 'detailed' is present
                        wants_detail = bool(re.search(r"(with\s+assignments?\s+list|assignments?\s+list|\bdetailed\b|\bdetalle\b)", s))
                        if wants_detail:
                            tbl = tool_activity_assignments_detailed(
                                sf,
                                countries or None,
                                name_like,
                                last_n_days=last_days,
                                last_n_months=last_months,
                                since=since_d,
                                until=until_d,
                            )
                            return {"answer": "<p>Assignments per activity.</p>", "table": tbl}
                        else:
                            tbl = tool_activities_with_assignments_counts(
                                sf,
                                last_n_days=last_days,
                                last_n_months=last_months,
                                since=since_d,
                                until=until_d,
                            )
                            return {"answer": "<p>Activities with assignment counts.</p>", "table": tbl}
                    # Group countries by Opportunity/Activity → stacked chart preferred
                    if re.search(r"\b(group|by|per|por)\b[\s\S]*\b(opportunity\s*name|activity|activit(?:y|ies))\b", s) and re.search(r"\bcountr|pa[ií]s", s):
                        # Check if user wants stacked visualization
                        wants_stack = bool(re.search(r"\b(stack|stacked|segment|breakdown)\b", s))
                        if wants_stack or re.search(r"\b(chart|graph|bar|pie)\b", s):
                            # Stacked data format based on assignment counts per country
                            tbl = tool_activity_country_matrix(sf, stacked=True)
                            rows = tbl.get("rows") or []
                            countries = sorted(set(r.get("country") for r in rows if r.get("country")))
                            stacked_data = []
                            activities = sorted(set(r.get("activity_name") for r in rows if r.get("activity_name")))
                            for activity in activities:
                                row_data = {"activity_name": activity}
                                for country in countries:
                                    sites = sum(r.get("sites", 0) for r in rows if r.get("activity_name") == activity and r.get("country") == country)
                                    row_data[country] = sites
                                stacked_data.append(row_data)
                            viz = tool_render_chart("bar", stacked_data, "activity_name", countries[:8], {"title": "Countries per Activity (Stacked)"}, {})
                            # Devuelve la tabla exacta utilizada para el gráfico (pivot por activity × countries mostradas)
                            viz_cols = [{"key":"activity_name","label":"Activity"}] + [{"key": c, "label": _pretty_label(c)} for c in countries[:8]]
                            viz_tbl = {"columns": viz_cols, "rows": stacked_data}
                            return {"answer": f"<p>{len(activities)} activities with country breakdown.</p>", "table": viz_tbl, "visualization": viz.get("visualization")}
                        else:
                            # Aggregated view (also returns table)
                            tbl = tool_activity_country_matrix(sf, stacked=False)
                            rows = tbl.get("rows") or []
                            viz = tool_render_chart("bar", rows, "activity_name", ["sites_total"], {"title": "Sites per Activity"}, {})
                            return {"answer": f"<p>{len(rows)} activities found.</p>", "table": tbl, "visualization": viz.get("visualization")}
                    # Explicit phrasing: "activities by country"
                    # Tolerate typos like "activites" when matching this intent
                    if (("activ" in s) and re.search(r"\bby\s+country\b", s)) or re.search(r"\b(activities?|activity|activites)\b[^\n]*\bby\s+country\b", s):
                        tbl_agg = tool_activity_country_matrix(sf, stacked=False)
                        # Build stacked viz from counts per country
                        tbl_stack = tool_activity_country_matrix(sf, stacked=True)
                        rows_s = tbl_stack.get("rows") or []
                        countries = sorted(set(r.get("country") for r in rows_s if r.get("country")))
                        stacked_data = []
                        activities = sorted(set(r.get("activity_name") for r in rows_s if r.get("activity_name")))
                        for act in activities:
                            rowd = {"activity_name": act}
                            for c in countries:
                                rowd[c] = sum(r.get("sites",0) for r in rows_s if r.get("activity_name")==act and r.get("country")==c)
                            stacked_data.append(rowd)
                        viz = tool_render_chart("bar", stacked_data, "activity_name", countries[:8], {"title":"Countries per Activity (Stacked)"}, {})
                        return {"answer": f"<p>{len(activities)} activities with country breakdown.</p>", "table": tbl_agg, "visualization": viz.get("visualization")}
                    # Follow-up like: "make it a stacked barchart by activity name"
                    if re.search(r"\bstacked\b", s) and re.search(r"\bbar\b|\bbarchart\b|\bbar\s*chart\b", s) and re.search(r"activit", s):
                        tbl = tool_activity_country_matrix(sf, stacked=True)
                        rows = tbl.get("rows") or []
                        countries = sorted(set(r.get("country") for r in rows if r.get("country")))
                        stacked_data = []
                        activities = sorted(set(r.get("activity_name") for r in rows if r.get("activity_name")))
                        for activity in activities:
                            row_data = {"activity_name": activity}
                            for country in countries:
                                row_data[country] = sum(r.get("sites", 0) for r in rows if r.get("activity_name") == activity and r.get("country") == country)
                            stacked_data.append(row_data)
                        viz = tool_render_chart("bar", stacked_data, "activity_name", countries[:8], {"title": "Countries per Activity (Stacked)"}, {})
                        viz_cols = [{"key":"activity_name","label":"Activity"}] + [{"key": c, "label": _pretty_label(c)} for c in countries[:8]]
                        viz_tbl = {"columns": viz_cols, "rows": stacked_data}
                        return {"answer": f"<p>{len(activities)} activities with country breakdown.</p>", "table": viz_tbl, "visualization": viz.get("visualization")}
                    # Specific: 'participating in activity "X" in Y' → by-name resolver
                    # Also capture variants like "sites in Y participating in 'X'"
                    m_qname = re.search(r"participat\w*\s+.*?activity\s+['\"]([^'\"]{3,})['\"]|['\"]([^'\"]{3,})['\"]\s+activit", s)
                    m_c2 = re.search(r"\bin\s+([a-zA-ZÀ-ÿ'\- ,/&]+)$", s)
                    countries2 = []
                    if m_c2:
                        raw = m_c2.group(1).strip()
                        raw = re.sub(r"\b(and|y|e)\b", ",", raw, flags=re.I)
                        raw = raw.replace("/", ",").replace("&", ",")
                        parts = [p.strip() for p in raw.split(",") if p.strip()]
                        countries2 = [p.title() for p in parts]
                    if m_qname:
                        name = m_qname.group(1).strip()
                        byname = tool_sites_by_activity(sf, name=name, countries=countries2 or None, exact=False)
                        return {"answer": "<p>Sites by activity name.</p>", "table": byname}
                    # Otherwise list sites with activities (with optional country filter)
                    table = tool_sites_with_any_activity(sf, countries=countries2 or None)
                    rows = table.get("rows") or []
                    accs = len({str(r.get("account_id")) for r in rows if isinstance(r, dict) and r.get("account_id")})
                    ans = f"Found activities in {accs} account(s)."
                    return {"answer": f"<p>{ans}</p>", "table": table}
        except Exception as _e:
            _dbg("WARN: planner activities failed: %s", _e)
        
        # Detectar follow-ups de visualización (frases cortas con palabras clave)
        is_chart_followup = bool(
            re.search(r"\b(chart|graph|bar|pie|line|visuali[sz]e|display|show|plot)\b", s) and
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
            if re.search(r"\b%\s*of\s*sites\b", s) and re.search(r"\bhla\s*typing\b", s) and re.search(r"\bper\s*country\b|\bby\s*country\b", s):
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
                        "SELECT Account.Id, Account.ShippingCountry "
                        "FROM Opportunity "
                        "WHERE C_Is_HLA_typing_performed__c = true "
                        "AND Account.ShippingCountry != null "
                        "GROUP BY Account.Id, Account.ShippingCountry"
                    )
                    recs = tool_salesforce_query(sf, soql_hla).get("records", [])
                    hla_counts: Dict[str, int] = {}
                    seen = set()
                    for r in recs:
                        acc = r.get("Account") or {}
                        aid = acc.get("Id"); c = acc.get("ShippingCountry")
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
        is_basic_site_count = (
            wants_count and 
            re.search(r"\bsites?\b", s) and
            # 'have/has' are auxiliary verbs in English; don't treat them as filters
            not re.search(r"\b(with|having|where|que|con|que tienen)\b", s) and
            not re.search(r"\b(patient|stage|screening|hla|pharmacy|overnight|t1d|diagnosis|qualification)\b", s)
        )
        
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

    planned = _try_planner(
        (payload.messages[-1].content if payload.messages else "") or "",
        has_table=has_table_signal
    )
    if planned:
        return planned

    # ===== Determinista: "show table" → devolver la última tabla sin llamar al modelo =====
    try:
        last_text = (payload.messages[-1].content or "").lower() if payload.messages else ""
        asks_table = bool(re.search(r"\b(table|tabla|tabella|tabelle)\b|show\s+.*\btable\b|tabla\s*por\s*favor|mostrar\s+tabla", last_text))
        if asks_table:
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
    try:
        qtxt = (payload.messages[-1].content or "") if payload.messages else ""
        s = qtxt.lower()
        m_dist = re.search(r"(?:within|radius|radio|distancia|a)\s*(\d{2,4})\s*km\b", s)
        # city or reference site: "of <city>" or "of the clinical site of <city>"
        m_city = re.search(r"\b(?:of|from|de|desde)\s+(?:the\s+clinical\s+site\s+of\s+)?([a-zA-ZÀ-ÿ'\- ]{2,}?)(?:\s+(?:with|con)\b|\b)", s)
        if m_dist and m_city and sf:
            max_km = float(m_dist.group(1))
            city = m_city.group(1).strip().strip(". ")
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

    msgs: List[Dict[str, Any]] = [{"role":"system","content":SYSTEM_PROMPT}]
    msgs.append({
        "role":"system",
        "content": (
            "KNOWLEDGE INDEX (SF preferred over site_qual when both exist). "
            "Aliases → target field:\n" + INDEX_SNIPPET
        )
    })
    if payload.last_filters and payload.last_filters.get("rules"):
        msgs.append({
            "role": "system",
            "content": (
                "LAST FILTERS (FilterGroup that produced the current table — "
                "for follow-up queries, modify these and call explorer_search again with the updated filters):\n"
                + json.dumps(payload.last_filters, ensure_ascii=False)
            )
        })

    for m in payload.messages:
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
        
        aliases = list(kindex.keys())
        best = _top_matches(user_utterance, aliases, k=5)
        hints = []
        
        # Si el usuario pide contactos/roles (PI/SC/etc.), no uses planner; deja que el modelo invoque la tool de contactos
        role_terms = [
            "study coordinator","study nurse","principal investigator","pi","clinician","project manager","technician",
            "member representative","contact person","associate scientist","post-doc","phd student","head","fellow"
        ]
        if any(t in s for t in role_terms):
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
                "Do NOT call sql_query or salesforce_query again."
            )
        elif re.search(r"\b(chart|graph|bar|pie|line|visuali[sz]e)\b", user_utterance):
            # Petición de gráfico pero NO hay last_table (nueva conversación o follow-up sin datos)
            hints.append(
                "📊 **CHART WITHOUT DATA**: User wants a chart but no previous data is available in this session. "
                "You MUST first query the data (use salesforce_query for sites/counts, or sql_query for qualification data), "
                "and THEN call render_chart with the 'data' parameter containing the rows from that query. "
                "Do NOT call render_chart with empty or placeholder data."
            )
        
        # Si es un conteo básico de sitios, forzar Salesforce Account
        elif is_basic_site_query:
            hints.append(
                "🔴 **CRITICAL ROUTING**: This is a site list/count query. "
                "You MUST use salesforce_query with Account table. "
                "RULE: Use Postgres (sql_query) ONLY for site_qual qualification questions (pharmacy, overnight stay, etc.). "
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
                        f"{a} -> use salesforce_query (field: {meta.get('field')}). "
                        "SOQL rules: do NOT prefix fields with 'sf.'; use Account.ShippingCountry and "
                        "Account.ShippingCity for geography; to filter non-null use '!= null'."
                    )
                else:
                    hints.append(f"{a} -> use sql_query. JOIN sites s WITH site_qual sq ON s.id=sq.site_id. Filter on sq.data->>'{meta.get('key')}'. "
                                 "Use ILIKE for text comparisons (e.g. value ILIKE 'On-site%'). SELECT s.salesforce_account_id AS account_id.")
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
        
        # SIEMPRE añadir recordatorio de routing
        if not is_basic_site_query:  # ya tiene su hint específico
            hints.insert(0, "🔴 REMEMBER: Use Postgres (sql_query) ONLY for site_qual questions. Everything else uses Salesforce.")
        
        if hints:
            msgs.append({
                "role":"system",
                "content": (
                    "ROUTING HINTS for the last user request:\n- " + "\n- ".join(hints)
               )
            })
    
    # Si hay intención de datos → obligamos tool-calls
    tool_mode = "required" if data_intent else "auto"
    reinforced_once = False
    _dbg("ROUND call → tool_choice=%s | data_intent=%s", tool_mode, data_intent)
    try:
        resp = _gemini_chat(msgs, tool_choice=tool_mode)
    except (APITimeoutError, APIConnectionError, RateLimitError) as e:
        _dbg("OpenAI Error (1st attempt): %s", e)
        return {"answer": f"<p>Service temporarily unavailable (timeout/connection). Please try again later. ({str(e)[:50]})</p>"}
    choice = resp.choices[0]
    assistant_msg = choice.message
    _dbg("ASSISTANT content len=%s | tool_calls=%s",
         len(assistant_msg.content or ""), 
         [tc.function.name for tc in (assistant_msg.tool_calls or [])])

    # Heurística de intención de datos
    if not assistant_msg.tool_calls and data_intent and not reinforced_once:
        _dbg("No tool_calls but data_intent=True  refuerzo de sistema y reintento")
        msgs.append({
            "role": "system",
            "content": (
                "IMPORTANT: You must call at least one tool to fetch **real data**. "
                "Prefer salesforce_query for Salesforce fields when available; otherwise "
                "sql_query (Postgres) for site_qual JSONB metrics. "
                "Return a compact table (Site, Country, City, metric) and, if useful, a bar chart."
            )
        })
        reinforced_once = True
        # Reintento inmediato forzando herramientas
        try:
            resp = _gemini_chat(msgs, tool_choice="required")
        except (APITimeoutError, APIConnectionError, RateLimitError) as e:
            _dbg("OpenAI Error (retry): %s", e)
            return {"answer": f"<p>Service temporarily unavailable (timeout/connection) during retry. ({str(e)[:50]})</p>"}
        choice = resp.choices[0]
        assistant_msg = choice.message
        _dbg(
            "RETRY: content len=%s | tool_calls=%s",
            len(assistant_msg.content or ""),
            [tc.function.name for tc in (assistant_msg.tool_calls or [])]
        )

    # 1) Si el asistente pidió herramientas, debemos:
    #    a) añadir su mensaje al historial
    #    b) ejecutar cada tool y añadir un mensaje role="tool" con tool_call_id
    if assistant_msg.tool_calls:
        # a) Añadimos el mensaje del asistente que contiene tool_calls
        msgs.append({
            "role": "assistant",
            "content": assistant_msg.content or "",
            "tool_calls": [tc.model_dump() for tc in assistant_msg.tool_calls],  # mantiene el id
        })

        # b) Ejecutamos herramientas
        for tc in assistant_msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            tool_call_id = tc.id
            _dbg("TOOL CALL → %s args=%s", name, args)

            if name == "sql_query":
                try:
                    # Reset any previous table/visualization to avoid stale UI when a new tool runs
                    last_table = None
                    last_visualization = None
                    out = tool_sql_query(db, args.get("sql",""), args.get("params") or {})
                    cols = out.get("columns") or []
                    dict_rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
                    last_table = {"columns": [{"key": c, "label": c} for c in cols], "rows": dict_rows}
                    # --- NUEVO: enriquecer account_id por nombre de sitio si falta ---
                    try:
                        rows0 = last_table.get("rows") or []
                        # extrae posibles nombres de sitio
                        name_keys = [
                            "site","sites.name","s.name","sf.Account.Name","Account.Name","name","account_name"
                        ]
                        wanted = {str(r[k]) for r in rows0 for k in name_keys
                                  if isinstance(r, dict) and r.get(k)}
                        if wanted:
                            q = text("""
                                SELECT name, salesforce_account_id
                                FROM public.sites
                                WHERE name = ANY(:names)
                            """)
                            res = db.execute(q, {"names": list(wanted)})
                            mapping = {row[0]: row[1] for row in res.fetchall() if row[1]}
                            changed = False
                            for r in rows0:
                                if not isinstance(r, dict):
                                    continue
                                if "account_id" not in r or not r.get("account_id"):
                                    nm = next((r.get(k) for k in name_keys if r.get(k)), None)
                                    acc = mapping.get(str(nm)) if nm is not None else None
                                    if acc:
                                        r["account_id"] = acc
                                        r.setdefault("sf.Account.Id", acc)
                                        changed = True
                            if changed:
                                # Asegura columna al inicio si falta
                                col_keys = [c.get("key") for c in last_table.get("columns", [])]
                                if "account_id" not in col_keys:
                                    last_table["columns"].insert(0, {"key": "account_id", "label": "Account Id"})
                    except Exception as _e:
                        _dbg("WARN: enrich account_id by site-name failed: %s", _e)
                    _dbg("SQL RESULT: %s", json.dumps(out, default=str)[:500])
                    msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps(out, default=str)})
                except Exception as e:
                    import traceback
                    _dbg("SQL TOOL ERROR: %s\n%s", e, traceback.format_exc())
                    msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(e)})})

            elif name == "salesforce_query":
                try:
                    # Reset any previous table/visualization to avoid stale UI when a new tool runs
                    last_table = None
                    last_visualization = None
                    if not sf:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                    else:
                        soql_orig = args.get("soql","")
                        _validate_soql(soql_orig)
                        raw = tool_salesforce_query(sf, soql_orig)
                        # aplanamos para tabla
                        records = raw.get("records", []) if isinstance(raw, dict) else []
                        flat_rows, keys = [], set()
                        # Extraer contexto del SOQL para mejores etiquetas
                        soql_context = {}
                        # Detectar aliases explicitamente nombrados en el SOQL
                        alias_matches = re.findall(r"\b(AVG|COUNT|SUM|MIN|MAX)\([^)]+\)\s+(\w+)", soql_orig, re.I)
                        for agg_func, alias_name in alias_matches:
                            alias_lower = alias_name.lower()
                            if alias_lower in ("total", "count"):
                                soql_context[alias_name] = "Total"
                            elif alias_lower == "sites":
                                soql_context[alias_name] = "Sites"
                            elif alias_lower == "country":
                                soql_context[alias_name] = "Country"
                            elif alias_lower == "avg" or agg_func.upper() == "AVG":
                                if "T1D" in soql_orig and "U_18" in soql_orig:
                                    soql_context[alias_name] = "Average T1D Patients <18"
                                elif "T1D" in soql_orig and "O_18" in soql_orig:
                                    soql_context[alias_name] = "Average T1D Patients ≥18"
                                else:
                                    soql_context[alias_name] = "Average"
                        
                        # Fallback para expr0, expr1, etc. sin alias explícito
                        if "AVG(" in soql_orig and "T1D" in soql_orig and "U_18" in soql_orig and "expr0" not in soql_context:
                            soql_context["expr0"] = "Average T1D Patients <18"
                        elif "AVG(" in soql_orig and "T1D" in soql_orig and "O_18" in soql_orig and "expr0" not in soql_context:
                            soql_context["expr0"] = "Average T1D Patients ≥18"
                        elif "AVG(" in soql_orig and "T1D" in soql_orig and "expr0" not in soql_context:
                            soql_context["expr0"] = "Average T1D Patients"
                        elif "COUNT(" in soql_orig and "expr0" not in soql_context:
                            soql_context["expr0"] = "Total Count"
                        
                        for r in records:
                            flat = {}
                            for k, v in r.items():
                                if k == "attributes": continue
                                if isinstance(v, dict) and "attributes" in v:
                                    for kk, vv in v.items():
                                        if kk == "attributes": continue
                                        flat[f"sf.Account.{kk}"] = vv
                                        keys.add(f"sf.Account.{kk}")
                                else:
                                    flat[f"sf.{k}"] = v
                                    keys.add(f"sf.{k}")
                            flat_rows.append(flat)
                        # Columnas con etiquetas "humanas" usando contexto SOQL
                        sf_fields_map = _INDEX_CACHE.get("sf_fields") or {}
                        ordered = sorted(keys)
                        cols = []
                        for k in ordered:
                            # Usar contexto SOQL para nombres más específicos
                            bare_key = k.replace("sf.", "")
                            if bare_key.lower() in soql_context:
                                label = soql_context[bare_key.lower()]
                            else:
                                label = _pretty_label(k)
                            cols.append({"key": k, "label": label})
                        last_table = {"columns": cols, "rows": flat_rows}
                        # ---- NORMALIZACIÓN PARA BOTONES ----
                        last_table = _normalize_table_for_ui(last_table)
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps(raw, default=str)})
                except Exception as e:
                    msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(e)})})

            elif name == "salesforce_account_extras":
                    if not sf:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                    else:
                        out = tool_salesforce_account_extras(sf, args.get("account_id",""))
                        cols = out.get("columns") or []
                        dict_rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
                        last_table = {"columns": [{"key": c, "label": c} for c in cols], "rows": dict_rows}
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps(out, default=str)})

            elif name == "explorer_set_filters":
                    out = tool_explorer_set_filters(request, args)
                    msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps(out)})

            elif name == "salesforce_account_contacts":
                    if not sf:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                    else:
                        # Sanitize/auto-resolve account id to prevent malformed SOQL
                        acc_arg = args.get("account_id", "") or ""
                        acc_id = acc_arg.strip()
                        if not _is_valid_sf_id(acc_id, prefix="001"):
                            inferred = _first_account_id_from_table(last_table)
                            if _is_valid_sf_id(inferred, prefix="001"):
                                acc_id = inferred
                            else:
                                msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": "Missing or invalid account_id"})})
                                continue
                        out = tool_salesforce_account_contacts(
                            sf,
                            acc_id,
                            bool(args.get("include_subaccounts") or False),
                            args.get("role_contains"),
                            args.get("roles"),
                            args.get("title_contains"),
                            args.get("country"),
                            args.get("city")
                        )
                        last_table = out
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})

            elif name == "salesforce_assignments":
                    if not sf:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                    else:
                        out = tool_salesforce_assignments(
                            sf,
                            args.get("account_ids") or [],
                            bool(args.get("active_only") or False),
                            args.get("last_n_months"),
                        )
                        last_table = out
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})

            elif name == "rank_sites_by_group":
                    try:
                        out_table = tool_rank_sites_by_group(
                            db,
                            sf,
                            args.get("metric",""),
                            (args.get("group_by") or "country"),
                            int(args.get("top_n") or 3),
                            args.get("order") or "desc",
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "group_count":
                    try:
                        out_table = tool_group_count(
                            db,
                            args.get("by") or ["country"],
                            args.get("where") or {},
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "group_count_agg":
                    try:
                        out_table = tool_group_count_agg(
                            db,
                            args.get("by") or ["country"],
                            args.get("metric"),
                            args.get("agg") or "avg",
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "list_activities":
                    try:
                        if not sf:
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                        else:
                            out_table = tool_list_all_activities(sf)
                            last_table = out_table
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "activities_with_countries":
                    try:
                        if not sf:
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                        else:
                            out_table = tool_activities_with_countries(sf)
                            last_table = out_table
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "activity_counts_by_country":
                    try:
                        if not sf:
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                        else:
                            out_table = tool_activity_counts_by_country(sf)
                            last_table = out_table
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "activity_country_matrix":
                    try:
                        if not sf:
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                        else:
                            out_table = tool_activity_country_matrix(sf, bool(args.get("stacked") or False))
                            last_table = out_table
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "activities_sites":
                    try:
                        out_table = tool_sites_with_any_activity(
                            sf,
                            args.get("countries") or [],
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "activities_by_name":
                    try:
                        out_table = tool_sites_by_activity(
                            sf,
                            args.get("name",""),
                            args.get("countries") or [],
                            bool(args.get("exact") or False),
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "group_count_sf":
                    try:
                        out_table = tool_group_count_sf(
                            sf,
                            args.get("by") or ["country"],
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "group_agg_sf":
                    try:
                        out_table = tool_group_agg_sf(
                            sf,
                            args.get("by") or ["country"],
                            args.get("field") or "",
                            args.get("agg") or "avg",
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "activities_with_assignments_counts":
                    try:
                        if not sf:
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                        else:
                            out_table = tool_activities_with_assignments_counts(
                                sf,
                                last_n_days=args.get("last_n_days"),
                                last_n_months=args.get("last_n_months"),
                                since=args.get("since"),
                                until=args.get("until"),
                            )
                            last_table = out_table
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "activity_assignments_detailed":
                    try:
                        if not sf:
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                        else:
                            out_table = tool_activity_assignments_detailed(
                                sf,
                                args.get("countries") or [],
                                args.get("activity_contains"),
                                last_n_days=args.get("last_n_days"),
                                last_n_months=args.get("last_n_months"),
                                since=args.get("since"),
                                until=args.get("until"),
                            )
                            last_table = out_table
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "time_series_sf":
                    try:
                        out_table = tool_time_series_sf(
                            sf,
                            args.get("field",""),
                            args.get("date_field") or "CloseDate",
                            args.get("period") or "month",
                            args.get("agg") or "sum",
                            args.get("last_n"),
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "sql_query_fill_sf":
                    try:
                        out_table = tool_sql_query_fill_sf(
                            db,
                            sf,
                            args.get("sql",""),
                            args.get("account_fields") or [],
                            args.get("params") or {},
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "contacts_by_group":
                    try:
                        out_table = tool_contacts_by_group(
                            sf,
                            args.get("roles") or [],
                            args.get("title_contains"),
                            args.get("group_by") or "country",
                            int(args.get("top_n") or 1),
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "study_coordinators_with_activities":
                    try:
                        out_table = tool_study_coordinators_with_activities(
                            sf,
                            args.get("account_ids") or [],
                            args.get("countries") or [],
                            args.get("title_contains"),
                            args.get("roles") or [],
                            bool(args.get("include_subaccounts") or False),
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "qual_search":
                    try:
                        out_table = tool_qual_search(
                            db,
                            args.get("text",""),
                            int(args.get("limit") or 50),
                        )
                        last_table = out_table
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "manipulate_data":
                    try:
                        manipulated = tool_manipulate_data(
                            last_table,
                            args.get("operation", "group_others"),
                            args.get("threshold"),
                            args.get("value_column"),
                            args.get("label_column"),
                        )
                        # Actualizar last_table con datos manipulados
                        if "rows" in manipulated:
                            last_table = manipulated
                            _dbg("Updated last_table with manipulated data")
                            # Si ya había un gráfico, re‑render con los mismos ejes para reflejar la agrupación
                            try:
                                if last_visualization and isinstance(last_visualization, dict):
                                    vtype = last_visualization.get("type") or "bar"
                                    xk = last_visualization.get("xKey") or manipulated.get("label_column")
                                    yks = last_visualization.get("yKeys") or ([manipulated.get("value_column")] if manipulated.get("value_column") else [])
                                    data = manipulated.get("rows") or (last_table.get("rows") if isinstance(last_table, dict) else [])
                                    out_v = tool_render_chart(vtype, data, xk or "country", yks or [])
                                    last_visualization = out_v.get("visualization")
                                    _dbg("Re-rendered chart after manipulate_data with same axes")
                            except Exception as _re:
                                _dbg("WARN: could not re-render chart: %s", _re)
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps(manipulated)})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "render_chart":
                    # Si no vienen datos O vienen datos inventados (USA, etc.), usar last_table
                    chart_data = args.get("data")
                    if not chart_data or len(chart_data) == 0:
                        # No hay datos, usar last_table
                        if last_table and last_table.get("rows"):
                            chart_data = last_table.get("rows")
                            _dbg("render_chart: Using last_table data (%d rows)", len(chart_data))
                    elif isinstance(chart_data, list) and len(chart_data) > 0:
                        # Detectar si son datos inventados (ej: USA, Canada, UK que no están en nuestros datos)
                        sample_country = chart_data[0].get("Country") or chart_data[0].get("country")
                        if sample_country and sample_country.upper() in ("USA", "CANADA", "UK"):
                            # Datos inventados, usar last_table
                            if last_table and last_table.get("rows"):
                                chart_data = last_table.get("rows")
                                _dbg("render_chart: Detected fake data (USA/Canada/UK), using last_table data (%d rows)", len(chart_data))
                    
                    out = tool_render_chart(
                        args.get("kind"),
                        chart_data,
                        args.get("xKey"),
                        args.get("yKeys") or [],
                        args.get("meta") or {},
                        args.get("options") or {}
                    )
                    last_visualization = out.get("visualization")
                    # Asegura que la siguiente petición "show table" pueda usar exactamente los datos del gráfico
                    try:
                        xk = args.get("xKey")
                        yks = args.get("yKeys") or []
                        data = chart_data or []
                        if xk and isinstance(data, list):
                            cols = [{"key": str(xk), "label": _pretty_label(str(xk))}] + [
                                {"key": str(y), "label": _pretty_label(str(y))} for y in yks
                            ]
                            last_table = {"columns": cols, "rows": data}
                    except Exception as _e:
                        _dbg("WARN: could not cache viz table for show table: %s", _e)
                    msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps(out)})

            elif name == "explorer_search":
                    try:
                        used_filters = args.get("filters") or {"logic": "AND", "rules": []}
                        out = tool_explorer_search(
                            request,
                            filters=used_filters,
                            columns=args.get("columns") or [],
                        )
                        last_table = {"columns": out.get("columns") or [], "rows": out.get("rows") or []}
                        last_table = _normalize_table_for_ui(last_table)
                        last_explorer_filters = used_filters  # guardar para follow-ups
                        msgs.append({"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps(out, default=str)})
                    except Exception as ee:
                        msgs.append({"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            elif name == "explorer_within_drive_km":
                    # Autorrellena base_account_id desde la última tabla si no vino
                    base_id = args.get("base_account_id") or _first_account_id_from_table(last_table)
                    if not base_id:
                        msgs.append({
                            "role":"tool","tool_call_id": tool_call_id,
                            "content": json.dumps({"error":"Missing base_account_id and no previous results to infer it"})
                        })
                    else:
                        try:
                            out = tool_explorer_within_drive_km(
                                request,
                                base_account_id=base_id,
                                max_km=float(args.get("max_km") or 120),
                                filters=args.get("filters") or {},
                                columns=args.get("columns") or [],
                            )
                            last_table = {"columns": out.get("columns") or [], "rows": out.get("rows") or []}
                            last_table = _normalize_table_for_ui(last_table)
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps(out, default=str)})
                        except Exception as ee:
                            msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})                 

            elif name == "rank_sites":
                    try:
                        out_table = tool_rank_sites(
                            db,
                            sf,
                            args.get("metric",""),
                            int(args.get("top_n") or 5),
                            args.get("order") or "desc",
                        )
                        last_table = out_table
                        # no devolvemos toda la tabla por el canal "tool" (ya va en last_table),
                        # basta con confirmar ok para cerrar la ronda correctamente
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"ok": True})})
                    except Exception as ee:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": str(ee)})})

            else:
                msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error": f"Unknown tool {name}"})})
            # Intento de CIERRE: finalizar sin segunda llamada al modelo para evitar context_length_exceeded
            if last_table:
                # Preparar resumen numérico y top items
                rows = last_table.get("rows", [])
                total_rows = len(rows)
                cols = last_table.get("columns", [])
                def is_num(v):
                    try:
                        float(str(v).replace(",",""))
                        return True
                    except Exception:
                        return False
                numeric_col_key = None
                tokens = ("sites","count","total","patients","value")
                candidate_keys = [(c.get("key") if isinstance(c, dict) else str(c)) for c in cols]
                if rows and isinstance(rows[0], dict):
                    candidate_keys = list({*candidate_keys, *rows[0].keys()})
                for key in candidate_keys:
                    if not key: continue
                    kl = str(key).lower()
                    if any(t in kl.split(".")[-1] for t in tokens):
                        vals = [r.get(key) for r in rows if isinstance(r, dict)]
                        if any(is_num(v) for v in vals):
                            numeric_col_key = key; break
                if not numeric_col_key and rows and isinstance(rows[0], dict):
                    for key in rows[0].keys():
                        vals = [r.get(key) for r in rows if isinstance(r, dict)]
                        if any(is_num(v) for v in vals):
                            numeric_col_key = key; break
                total = 0.0
                if numeric_col_key:
                    try:
                        total = sum(float(str(r.get(numeric_col_key) or 0).replace(",","")) for r in rows if isinstance(r, dict))
                    except Exception:
                        total = 0.0
                # Dimensión para top (country/city/site)
                dim_key = None
                for k in ("country","city","site"):
                    if any((isinstance(c, dict) and c.get("key") == k) or (isinstance(c, str) and c == k) for c in cols):
                        dim_key = k; break
                if not dim_key and rows and isinstance(rows[0], dict):
                    for k in ("country","city","site"):
                        if k in rows[0]: dim_key = k; break
                top_txt = ""
                if dim_key and numeric_col_key and rows:
                    try:
                        top_sorted = sorted([r for r in rows if isinstance(r, dict)], key=lambda x: float(str(x.get(numeric_col_key) or 0).replace(",","")), reverse=True)[:3]
                        parts = [f"{str(t.get(dim_key))}: {str(t.get(numeric_col_key))}" for t in top_sorted]
                        if parts:
                            top_txt = " Top: " + ", ".join(parts[:2]) + (", " + parts[2] if len(parts) > 2 else "") + "."
                    except Exception:
                        pass
                if total and total_rows > 1:
                    answer_html = f"<p>{int(total) if isinstance(total,float) and total.is_integer() else total} total across {total_rows} groups.{top_txt}</p>"
                else:
                    answer_html = f"<p>Found {total_rows} result(s).</p>"
                out = {"answer": answer_html, "table": _normalize_table_for_ui(last_table)}
                if last_visualization:
                    out["visualization"] = last_visualization
                if last_explorer_filters:
                    out["last_filters"] = last_explorer_filters
                return out

    # Fallback: si agotamos rondas pero sí hay datos, devolvemos algo útil
    if last_table:
        rows = last_table.get("rows", [])
        result: Dict[str, Any] = {"answer": "<p>Here are the results.</p>"}
        if last_explorer_filters:
            result["last_filters"] = last_explorer_filters
        # Solo añadir tabla si tiene múltiples filas
        if rows and len(rows) > 1:
            result["table"] = _normalize_table_for_ui(last_table)
        want_chart = bool(re.search(r"\b(bar|chart)\b", (payload.messages[-1].content or "").lower()))
        if want_chart and "visualization" not in result:
            cols = [c.get("key") for c in last_table.get("columns", [])]
            rows = last_table.get("rows", [])
            non_dim = [k for k in cols if k and k.lower() not in {"site","country","city"}]
            def _is_numcol(k:str) -> bool:
                return all(
                    isinstance(r.get(k), (int,float)) or
                    str(r.get(k) or "").replace(",","").replace(".","").isdigit()
                    for r in rows
                )
            y_candidates = [k for k in non_dim if _is_numcol(k)]
            if y_candidates:
                result["visualization"] = {
                    "type":"bar",
                    "xKey":"site" if any((c or "").lower()=="site" for c in cols) else (cols[0] if cols else "site"),
                    "yKeys":[y_candidates[0]],
                    "data": rows,
                    "meta":{"title": f"Top sites by {y_candidates[0]}"},
                }
        return result
    return {"answer": "I couldn't complete the response. Could you rephrase the request?"}

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
