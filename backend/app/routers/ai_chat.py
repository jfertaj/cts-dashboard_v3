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

# OpenAI
from openai import OpenAI
from app.services.sf_labels import humanize_headers
from app.utils.soql_helpers import build_followup_accounts_query

DEBUG = os.environ.get("AI_CHAT_DEBUG", "0") == "1"
INDEX_REFRESH_SEC = int(os.environ.get("AI_INDEX_REFRESH_SEC", "600"))
FIELDS_SF_JSON_PATH = os.environ.get("FIELDS_SF_JSON_PATH", "app/config/fields_opportunity_curated.json")
QUAL_ALIAS_JSON_PATH = os.environ.get("QUAL_ALIAS_JSON_PATH", "app/config/qualification_aliases.json")
EXPLORER_DRIVE_KM_PATH = "/api/explorer/search/within-drive-km"

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

# ====== Whitelists / Guards ======

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

    def _pull(tag: str) -> Optional[str]:
        # patrones: **table**: {json}  |  table: {json}  |  **visualization**: {json}
        pat = rf"(?:\*\*{tag}\*\*|{tag})\s*:\s*(\{{.*\}})"
        m = re.search(pat, content, flags=re.I | re.S)
        return m.group(1) if m else None

    # Quita artefactos tipo "<table> ... </table>" pegados por el modelo
    content = re.sub(r"(?is)</?table>", "", content or "")
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
    txt = re.sub(r"```(?:json)?[\s\S]*?```", "", txt)
    # 2️⃣ Elimina objetos o arrays JSON que quedaron incrustados
    txt = re.sub(r"(\{[\s\S]*?\}|\[[\s\S]*?\])", "", txt)
    # 3️⃣ Elimina llaves o corchetes sueltos
    txt = re.sub(r"[{}\[\]]+", "", txt)
    # 🔧 NUEVO: elimina líneas basura tipo comas sueltas o claves estilo JSON
    #   - líneas que sean solo comas/quotes/espacios
    txt = re.sub(r"(?m)^\s*[,\"']+\s*$", "", txt)
    #   - líneas que empiecen como una clave JSON:  "algo":
    txt = re.sub(r'(?m)^\s*"[A-Za-z0-9_. -]+"\s*:\s*$', "", txt)
    #   - comas que queden solas entre saltos de línea
    txt = re.sub(r"(?m)^\s*,\s*$", "", txt)
    # 4️⃣ Artefactos HTML como <table>…</table> que algunos modelos “imaginan”
    txt = re.sub(r"(?is)<table[\s\S]*?</table>", "", txt)
    # 5️⃣ Restos tipo: ,,,, "rows":   o  "rows":
    txt = re.sub(r'(?m)^\s*,*\s*"rows"\s*:\s*$', "", txt) 
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
        # intenta catálogo
        lbl = (_INDEX_CACHE.get("sf_fields") or {}).get(_normalize(core), {}) or {}
        if isinstance(lbl, dict) and lbl.get("label"):
            return lbl["label"]
        # último recurso: heurística específica para API names
        return _prettify_sf_field_name(core)
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
    # por defecto humaniza
    return _apply_common_rewrites(re.sub(r"[_]+"," ",k))

def _ok_table(name: str) -> bool:
    name = (name or "").strip().strip('"')
    if "." not in name:
        name = f"public.{name}"
    norm_allowed = {t if t.startswith("public.") else f"public.{t}" for t in ALLOWED_TABLES}
    return name.lower() in norm_allowed

SF_ALLOWED_FIELDS = {
    "Id","Name","Type","StageName","Amount","CloseDate","RecordTypeId",
    "CreatedDate","LastModifiedDate","LastActivityDate",
    "Account.Id","Account.Name","Account.ShippingCountry","Account.ShippingCity",
    "Account.ShippingLatitude","Account.ShippingLongitude",
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
    if not re.search(r"\bfrom\s+Opportunity\b", soql, re.I):
        raise HTTPException(400, "SOQL must query FROM Opportunity")

    m = re.search(r"select\s+(.*?)\s+from\s+Opportunity", soql, re.I | re.S)
    if not m:
        return
    raw_fields = m.group(1)
    fields = [f.strip() for f in raw_fields.split(",") if f.strip()]

    def norm(f: str) -> str:
        f = re.sub(r"\s+ASC|\s+DESC", "", f, flags=re.I)
        f = re.sub(r"\s+NULLS\s+(FIRST|LAST)", "", f, flags=re.I)
        f = f.split(" ")[0]
        return f

    bad = []
    for f in fields:
        base = norm(f)
        if re.match(r"^(count|sum|min|max|avg)\s*\(", base, re.I):
            continue
        # Whitelist (static + dynamic)
        allowed = SF_ALLOWED_FIELDS | SF_ALLOWED_DYNAMIC
        if base.startswith("Account."):
            if base not in allowed:
                bad.append(base)
            continue
        if base not in allowed:
            bad.append(base)
    # Intento de ampliar whitelist dinámicamente
    if bad and sf is not None:
        acc_fields = _describe_fields(sf, 'Account')
        opp_fields = _describe_fields(sf, 'Opportunity')
        for b in list(bad):
            if b.startswith('Account.') and b in {f'Account.{x}' for x in acc_fields}:
                SF_ALLOWED_DYNAMIC.add(b)
                bad.remove(b)
            elif b in opp_fields:
                SF_ALLOWED_DYNAMIC.add(b)
                bad.remove(b)
    if bad:
        raise HTTPException(400, f"SOQL field(s) not allowed: {', '.join(bad)}")

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


def tool_salesforce_account_contacts(sf, account_id: str, include_subaccounts: bool = False, role_contains: Optional[str] = None, roles: Optional[List[str]] = None, title_contains: Optional[str] = None):
    """Fetch contacts for an Account (and optionally its child Accounts).
    Supports role filtering via AccountContactRelation.Role__c (env SF_CONTACT_ROLES or param 'roles')
    and free-text filtering by Contact.Title.
    Returns a normalized table with account_id, account_name, contact_id, name, email, phone, title, department, role.
    """
    if not account_id:
        raise HTTPException(400, "Missing account_id")
    # Collect account ids
    account_ids = [account_id]
    try:
        if include_subaccounts:
            import os
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

    # Build IN clause
    ids_clause = ",".join([f"'{aid}'" for aid in set(account_ids)])

    # Resolve role list from param or env
    import os
    roles_env = [s.strip() for s in (os.environ.get("SF_CONTACT_ROLES", "").split(",")) if s.strip()]
    role_list = roles if roles and len(roles) else roles_env

    rows: List[Dict[str, Any]] = []

    if role_list:
        # Use AccountContactRelation when roles are specified
        role_vals = ", ".join([f"'{r}'" for r in role_list])
        soql = (
            "SELECT Id, AccountId, ContactId, Role__c, "
            "Contact.Name, Contact.Email, Contact.Phone, Contact.Title, Contact.Department, "
            "Account.Name "
            "FROM AccountContactRelation "
            f"WHERE AccountId IN ({ids_clause}) AND Role__c IN ({role_vals}) "
            + ("" if not title_contains else f" AND Contact.Title LIKE '%{title_contains.replace("'","\\'" )}%'") +
            " ORDER BY AccountId, Contact.Name"
        )
        raw = sf.query_all(soql)
        for r in raw.get("records", []):
            acc = r.get("Account") or {}
            c = r.get("Contact") or {}
            rows.append({
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
    else:
        # Fallback to Contact list with optional Title/Department contains
        fields = ["Id", "Name", "Email", "Phone", "Title", "Department", "AccountId", "Account.Name"]
        where = f"AccountId IN ({ids_clause})"
        if role_contains:
            # Heuristic: search in Title or Department
            rc = role_contains.replace("'", "\'")
            where += f" AND (Title LIKE '%{rc}%' OR Department LIKE '%{rc}%')"
        if title_contains:
            tc = title_contains.replace("'", "\'")
            where += f" AND Title LIKE '%{tc}%'"
        soql = f"SELECT {', '.join(fields)} FROM Contact WHERE {where} ORDER BY AccountId, Name"
        raw = sf.query_all(soql)
        for r in raw.get("records", []):
            acc = r.get("Account") or {}
            rows.append({
                "account_id": r.get("AccountId"),
                "site": acc.get("Name"),
                "contact_id": r.get("Id"),
                "contact_name": r.get("Name"),
                "email": r.get("Email"),
                "phone": r.get("Phone"),
                "title": r.get("Title"),
                "department": r.get("Department"),
            })

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
        ],
        "rows": rows,
    }
    return _normalize_table_for_ui(table)


def tool_rank_sites_by_group(
    db: Session,
    metric: str,
    group_by: Literal["country","city"] = "country",
    top_n: int = 3,
    order: Literal["asc","desc"] = "desc",
):
    """Top-N per group (country/city) for a site_qual metric."""
    meta = _resolve_metric(metric, db)
    if meta.get("source") != "site_qual":
        raise HTTPException(400, "rank_sites_by_group only supports site_qual metrics for now")
    key = meta.get("key")
    dir_sql = "ASC" if str(order).lower() == "asc" else "DESC"
    grp = "s.country" if group_by == "country" else "s.city"
    sql = f"""
        WITH scored AS (
          SELECT
            {grp} AS grp,
            s.salesforce_account_id AS account_id,
            s.name AS site,
            COALESCE(NULLIF(regexp_replace(sq.data->>:key, '[^0-9\\.\\-]', '', 'g'), '')::numeric, 0) AS metric,
            ROW_NUMBER() OVER (PARTITION BY {grp} ORDER BY COALESCE(NULLIF(regexp_replace(sq.data->>:key, '[^0-9\\.\\-]', '', 'g'), '')::numeric, 0) {dir_sql} NULLS LAST) AS rn
          FROM public.sites s
          LEFT JOIN public.site_qual sq ON sq.site_id = s.id
        )
        SELECT grp, account_id, site, metric
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
            {"key":f"qual.{key}","label": _pretty_label(f"qual.{key}")}
        ],
        "rows": [
            {"group": r.get("grp"), "account_id": r.get("account_id"), "site": r.get("site"), f"qual.{key}": r.get("metric")} for r in rows
        ],
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
        soql = f"SELECT Id, {', '.join(fields)} FROM Account WHERE Id IN ({', '.join([f'\'{i}\'' for i in ids])})"
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
        title_filter = f" AND Contact.Title LIKE '%{title_contains.replace("'","\\'") }%'"
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
    """Semantic search over qualification comments using GIN tsv index."""
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
      ORDER BY rank DESC
      LIMIT :lim
    """
    out = tool_sql_query(db, sql, {"q": text, "lim": int(limit)})
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
            rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity").strip()
            rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
            if not rt_list:
                rt_list = ["Activity"]
            if len(rt_list) == 1:
                rt_clause = f"RecordType.DeveloperName = '{rt_list[0]}'"
            else:
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

def tool_explorer_set_filters(request: Request, payload: Dict[str, Any]):
    return {"type": "explorer_action", "action": "set_filters", "payload": payload}

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
                    "include_subaccounts":{"type":"boolean","default":false},
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
                    "active_only":{"type":"boolean","default":false},
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
            "description": "Top-N ranking of sites by a metric (works with SF fields or site_qual keys/aliases).",
            "parameters": {
                "type":"object",
                "properties":{
                    "metric":{"type":"string","description":"Alias or raw key (e.g., 'new T1D <18', 'C_Number_of_T1D_Patients_currently_U_18__c')"},
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
            "description":"Top-N ranking per group (country/city) for a site_qual metric.",
            "parameters":{
                "type":"object",
                "properties":{
                    "metric":{"type":"string"},
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
        "type":"function",
        "function":{
            "name":"group_count_agg",
            "description":"Aggregations per country/city (avg/sum on site_qual metrics or ratio_exists).",
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
- salesforce_query only for Opportunity (+Account.*) whitelisted fields.
- salesforce_account_extras(account_id) for PI / CS flags / assignments / newDx.
- Country/City for Explorer come from Postgres.sites (NOT Account.Shipping*).
"""

SYSTEM_PROMPT = f"""
You are **Moby**, an analytics copilot for a clinical trial site explorer.

LANGUAGE
- Default to English.
- If the latest user message is clearly (>80%) in another language, reply in that language. Otherwise keep English.

DATA SOURCES & SCHEMA (do not expose credentials)
{SCHEMA_HINT}

TOOLS — WHEN TO USE WHAT
- sql_query → Postgres facts (sites, site_qual JSONB, questionnaires…). Use for country/city and any metric mirrored in the warehouse.
- salesforce_query → Salesforce Opportunity (+ Account.*), only whitelisted fields.
- salesforce_account_extras → Per-Account extras not in generic SOQL: PI (name/email/phone), CS flags, assignments count, latest new diagnoses.
- salesforce_account_contacts → Contacts for an Account (optionally including child Accounts). Use when the user asks for contact details (PI, Study Coordinator, Study Nurse, etc.). You can filter by roles (AccountContactRelation.Role__c via SF_CONTACT_ROLES or 'roles' param) and/or by Contact.Title text.
- salesforce_assignments → Assignments/ongoing trials per Account (stage/type/opportunity). Use for “what trials are they in?”.
- explorer_set_filters → Reflect a result set in Explorer (filters + columns).
- render_chart → Create a chart when asked or when visual comparison helps.
- rank_sites_by_group → Ranking per country/city for a site_qual metric.
- group_count → Counts per country/city with optional site_qual filter.
- explorer_within_drive_km → **Use for distance/radius/nearby/within X km/drive time** requests. If the user says
  “from those sites …” and doesn't specify a base, pick the first site from the last results as the base
  (mention it in the bullets). Provide columns such as Account.Id/Name/Country/City plus requested metrics.
 
IMPORTANT: If an answer requires data, you MUST call at least one appropriate tool to fetch real rows. Do not invent numbers.

GUARDRAILS
- Read-only only. Always use named parameters in SQL examples.
- JSONB numeric casts: COALESCE(NULLIF(sq.data->>'C_Number_of_T1D_Patients_currently_U_18__c','')::int, 0) AS t1d_u18
- YES/NO strings: normalize (LOWER(...)='yes' or ILIKE 'yes').
- Ordering by computed columns: in ORDER BY use the full expression or positional indices (e.g., ORDER BY 3 DESC), never the alias.

FORMAT RULES (very important)
- Do **not** paste JSON or tables inside the prose.
- Put human-readable bullets only in **answer**.
- Put tabular data only in **table** and charts only in **visualization**.
- If there are zero rows, still return an empty **table** and say “no results” in **answer**.

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
- Site (prefer sf.Account.Name; fallback sites.name)
- Country (sites.country)
- City (sites.city)
- Screening/follow-up when relevant: Stage1, Stage2
- Newly diagnosed last year: <18 and ≥18
- Current patients: <18 and ≥18
**ALWAYS include an identifier**:
- When using sql_query with sites/site_qual: SELECT sites.salesforce_account_id AS account_id and include it in the table.
- When using salesforce_query with Account.* fields: include Account.Id (the backend will surface it as sf.Account.Id/account_id).

SCENARIOS (patterns, adapt as needed)
A) Top-N / Ranking
   - Prefer the dedicated tool **rank_sites(metric, top_n, order)**. It accepts aliases or raw keys and works across SF and site_qual automatically.
   - If a tool cannot be used, fall back to Postgres/SOQL following guards; still keep the same output shape.
B) Screening overview
   - Show flags + Stage1/Stage2; filter where any screening flag true.
C) Feature selection (e.g., onsite pharmacy & overnight stay)
   - Use booleans; otherwise derive from comments with case-insensitive search.
D) PI / Assignments / CS flags
   - If AccountId known → salesforce_account_extras; return PIName/Email/Phone, flags, assignments_count, new_dx_u18/o18.
   - For full contact lists → salesforce_account_contacts (offer to include child Accounts when the user mentions “sub-accounts” or “clinical sites”).
   - For trial participation → salesforce_assignments (accept list of AccountIds).
E) Report mode
   - If the user asks for a “report” without specifics, ask 3 quick choices: dimensions (e.g., Country/City or Account), metrics (e.g., current patients <18/≥18, newly diagnosed, Stage1/2), and time window when applicable.
   - Then build a compact table (grouped) using sql_query (warehouse) or salesforce_query (SOQL) and optionally a chart.

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

def _openai_chat(
    messages: List[Dict[str, Any]],
    tool_choice: str = "required",
    *,
    force_no_tools: bool = False,
):
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
    }
    # Si NO forzamos sin herramientas, añadimos tools y tool_choice.
    # Si forzamos sin herramientas, NO incluimos ninguno de esos campos (el API falla si se manda tool_choice sin tools).
    if not force_no_tools:
        kwargs["tools"] = TOOLS_SPEC
        kwargs["tool_choice"] = tool_choice  # "auto" | "required"

    return client.chat.completions.create(**kwargs)

# ====== Endpoint ======
@router.post("/chat")
def chat_api(payload: ChatRequest, request: Request, db: Session = Depends(get_db)):
    # Clarificador: incluir subcuentas clínicas para roles/contactos
    def _needs_contact_clarification(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        s = text.lower()
        role_terms = [
            "study coordinator","study nurse","principal investigator","pi","clinician","project manager","technician",
            "member representative","contact person","associate scientist","post-doc","phd student","head","fellow"
        ]
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
        # Presencia del concepto pacientes
        has_pat = re.search(r"\b(pacientes|patients)\b", s) is not None
        if not has_pat:
            return None
        # Señales de desambiguación ya especificadas
        specified_type = re.search(r"\b(new|newly|diagnos|dx|actual(es)?|current(ly)?)\b", s) is not None
        specified_stage = re.search(r"\bstage\s*[12]\b", s) is not None
        specified_age = re.search(r"(<\s*18|\bunder\s*18\b|u\s*18|≥\s*18|\bo(ver)?\s*18\b|o\s*18)\b", s) is not None
        if specified_type and (specified_age or specified_stage):
            return None
        # Construye objeto clarify
        question = "¿A qué tipo de pacientes te refieres? Selecciona una opción para continuar."
        options = [
            {"label": "Currently <18", "query": "currently T1D patients under 18"},
            {"label": "Currently ≥18", "query": "currently T1D patients over 18"},
            {"label": "Newly diagnosed <18", "query": "newly diagnosed T1D patients under 18"},
            {"label": "Newly diagnosed ≥18", "query": "newly diagnosed T1D patients over 18"},
            {"label": "Currently (ambos <18 y ≥18)", "query": "currently T1D patients under 18 and 18 or older"},
            {"label": "Newly diagnosed (ambos <18 y ≥18)", "query": "newly diagnosed T1D patients under 18 and 18 or older"},
        ]
        return {"answer": "<p>Necesito una pequeña aclaración.</p>", "clarify": {"question": question, "options": options}}

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
    for alias, meta in list(kindex.items())[:150]:
        if meta.get("source") == "sf":
            preview_items.append(f"{alias} => sf.{meta.get('field')}")
        else:
            preview_items.append(f"{alias} => site_qual.{meta.get('key')}")
    INDEX_SNIPPET = " | ".join(preview_items)

    # ===== Mini‑planificador determinista =====
    def _try_planner(user_text: str) -> Optional[Dict[str, Any]]:
        if not user_text:
            return None
        s = user_text.lower()
        # Detectar group_by
        group_by = None
        if re.search(r"\b(por|per|by|cada)\s+(pa[ií]s|country)\b", s):
            group_by = "country"
        if re.search(r"\b(por|per|by|cada)\s+(ciudad|city)\b", s):
            group_by = group_by or "city"
        # Detectar conteo por grupo
        wants_count = bool(re.search(r"\bcount|cu[aá]ntos|how\s+many\b", s))
        # Detectar top/order
        m_top = re.search(r"top\s*(\d{1,2})", s)
        top_n = int(m_top.group(1)) if m_top else (3 if group_by else 5)
        order = "desc" if re.search(r"(top|mayor|highest|largest|max)\b", s) else "asc" if re.search(r"(lowest|menor|min)\b", s) else "desc"
        # Intentar encontrar una métrica/alias en el índice
        best = _top_matches(s, list(kindex.keys()), k=1)
        metric_alias = best[0] if best else None
        # Plan A: rank per group (si hay group_by y métrica)
        if group_by and metric_alias:
            try:
                table = tool_rank_sites_by_group(db, metric_alias, group_by, top_n, order)
                ans = f"Ranked top {top_n} sites per {group_by} by '{metric_alias}'."
                return {"answer": f"<p>{ans}</p>", "table": table}
            except Exception:
                pass
        # Plan B: group count con filtro simple "with X"
        if group_by and wants_count:
            # buscar una posible condición exists: "with HLA typing", "with pharmacy"
            cond = None
            best2 = _top_matches(s, list(kindex.keys()), k=1)
            if best2:
                meta = kindex.get(best2[0], {})
                if meta.get("source") == "site_qual":
                    cond = {"key": meta.get("key"), "exists": True}
            where = cond or {}
            try:
                table = tool_group_count(db, [group_by], where)
                ans = f"Sites per {group_by}" + (f" with {best2[0]}" if cond else "")
                return {"answer": f"<p>{ans}</p>", "table": table}
            except Exception:
                pass
        return None

    planned = _try_planner((payload.messages[-1].content if payload.messages else "") or "")
    if planned:
        return planned

    msgs: List[Dict[str, Any]] = [{"role":"system","content":SYSTEM_PROMPT}]
    msgs.append({
        "role":"system",
        "content": (
            "KNOWLEDGE INDEX (SF preferred over site_qual when both exist). "
            "Aliases → target field:\n" + INDEX_SNIPPET
        )
    })

    for m in payload.messages:
        msgs.append({"role": m.role, "content": m.content})

    last_table: Optional[Dict[str, Any]] = None
    last_visualization: Optional[Dict[str, Any]] = None

    # Hasta 6 “rondas” de tool-calls
    reinforced_once = False
    for _ in range(6):
        # Heurística de intención de datos (antes de llamar al modelo)
        user_utterance = (payload.messages[-1].content or "").lower() if payload.messages else ""
        data_intent = bool(re.search(r"(top\s*\d+|rank|table|chart|sites?|patients?|screening|diagnosed|count|sum|average|media|promedio)", user_utterance))
        
        # Pista de enrutamiento por matches del índice
        if user_utterance:
            aliases = list(kindex.keys())
            best = _top_matches(user_utterance, aliases, k=5)
            hints = []
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
                        hints.append(f"{a} -> use sql_query over site_qual JSONB (key: {meta.get('key')}). "
                                     "Also SELECT sites.salesforce_account_id AS account_id.")
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
            if hints:
                msgs.append({
                    "role":"system",
                    "content": (
                        "ROUTING HINTS for the last user request:\n- " + "\n- ".join(hints) +
                        "\nIf both SF and site_qual exist for the same concept, prefer SF."
                   )
                })
        
        # Si hay intención de datos → obligamos tool-calls
        tool_mode = "required" if data_intent else "auto"
        
        # Si hay intención de datos → obligamos tool-calls
        tool_mode = "required" if data_intent else "auto"
        _dbg("ROUND call → tool_choice=%s | data_intent=%s", tool_mode, data_intent)
        resp = _openai_chat(msgs, tool_choice=tool_mode)
        choice = resp.choices[0]
        assistant_msg = choice.message
        _dbg("ASSISTANT content len=%s | tool_calls=%s",
             len(assistant_msg.content or ""), 
             [tc.function.name for tc in (assistant_msg.tool_calls or [])])

        # Heurística de intención de datos
        if not assistant_msg.tool_calls and data_intent and not reinforced_once:
            _dbg("No tool_calls but data_intent=True → refuerzo de sistema y reintento")
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
            # seguimos al siguiente loop para que el modelo elija tools
            continue

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
                    msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps(out, default=str)})

                elif name == "salesforce_query":
                    if not sf:
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps({"error":"No active Salesforce session"})})
                    else:
                        _validate_soql(args.get("soql",""))
                        raw = tool_salesforce_query(sf, args.get("soql",""))
                        # aplanamos para tabla
                        records = raw.get("records", []) if isinstance(raw, dict) else []
                        flat_rows, keys = [], set()
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
                        # Columnas con etiquetas “humanas”
                        sf_fields_map = _INDEX_CACHE.get("sf_fields") or {}
                        ordered = sorted(keys)
                        cols = [{"key": k, "label": _pretty_label(k)} for k in ordered]
                        last_table = {"columns": cols, "rows": flat_rows}
                        # ---- NORMALIZACIÓN PARA BOTONES ----
                        last_table = _normalize_table_for_ui(last_table)
                        msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps(raw, default=str)})

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
                        out = tool_salesforce_account_contacts(
                            sf,
                            args.get("account_id",""),
                            bool(args.get("include_subaccounts") or False),
                            args.get("role_contains"),
                            args.get("roles"),
                            args.get("title_contains")
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

                elif name == "render_chart":
                    out = tool_render_chart(
                        args.get("kind"),
                        args.get("data"),
                        args.get("xKey"),
                        args.get("yKeys") or [],
                        args.get("meta") or {},
                        args.get("options") or {}
                    )
                    last_visualization = out.get("visualization")
                    msgs.append({"role":"tool","tool_call_id": tool_call_id, "content": json.dumps(out)})

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

            # Intento de CIERRE: si ya tenemos datos, pedimos al modelo que finalice sin tools
            if last_table:
                msgs.append({
                    "role": "system",
                    "content": (
                        "Finalize now. Use ONLY the available tool results already provided. "
                        "Return the final JSON with keys: answer, table, and optionally visualization. "
                        "The 'answer' MUST be short prose only (no JSON, no code blocks, no inline tables, no <table> tags). "
                        "Put tabular data ONLY in 'table' and charts ONLY in 'visualization'. "
                        "Do NOT call any tools."
                    ),
                })
                resp2 = _openai_chat(msgs, tool_choice="none", force_no_tools=True)
                final_msg = resp2.choices[0].message
                raw2 = final_msg.content or ""
                structured2 = _extract_structured(raw2)
                # Etiquetado humano final (por si el modelo inventa columnas nuevas)
                if structured2.get("table", {}).get("columns"):
                    human_cols = []
                    seen = set()
                    for c in structured2["table"]["columns"]:
                        key = c.get("key") if isinstance(c, dict) else str(c)
                        if key in seen:
                            continue
                        seen.add(key)
                        human_cols.append({"key": key, "label": _pretty_label(key)})
                    structured2["table"]["columns"] = human_cols
                # Completar con lo que ya tenemos
                if last_table and "table" not in structured2:
                    # Aplica labels bonitas a todas las columnas antes de devolver
                    if last_table.get("columns"):
                        last_table["columns"] = [
                            {"key": c.get("key"), "label": _pretty_label(c.get("key"))}
                            for c in last_table.get("columns", [])
                        ]
                    structured2["table"] = last_table
                if last_visualization and "visualization" not in structured2:
                    structured2["visualization"] = last_visualization
                # Si el usuario pidió bar/chart y aún no hay gráfico, generamos uno sencillo
                want_chart = bool(re.search(r"\b(bar|chart)\b", (payload.messages[-1].content or "").lower()))
                if want_chart and "visualization" not in structured2 and last_table:
                    cols = [c.get("key") for c in last_table.get("columns", [])]
                    rows = last_table.get("rows", [])
                    # primer campo numérico que no sea site/country/city
                    non_dim = [k for k in cols if k and k.lower() not in {"site","country","city"}]
                    def _is_numcol(k:str) -> bool:
                        return all(
                            isinstance(r.get(k), (int,float)) or
                            str(r.get(k) or "").replace(",","").replace(".","").isdigit()
                            for r in rows
                        )
                    y_candidates = [k for k in non_dim if _is_numcol(k)]
                    if y_candidates:
                        structured2["visualization"] = {
                            "type":"bar",
                            "xKey":"site" if any((c or "").lower()=="site" for c in cols) else (cols[0] if cols else "site"),
                            "yKeys":[y_candidates[0]],
                            "data": rows,
                            "meta":{"title": f"Top sites by {y_candidates[0]}"},
                        }
                return structured2
            # si aún no hay datos, seguimos al siguiente loop para otra ronda
            continue
            

        # 2) Si NO hay tool_calls, ya es la respuesta final → normalizamos
        raw = assistant_msg.content or ""
        structured = _extract_structured(raw)
        # Si antes ejecutamos tools (last_table/viz), prevalecen los reales
        if last_table and "table" not in structured:
            structured["table"] = last_table
        if last_visualization and "visualization" not in structured:
            structured["visualization"] = last_visualization
        return structured

    # Fallback: si agotamos rondas pero sí hay datos, devolvemos algo útil
    if last_table:
        result: Dict[str, Any] = {
            "answer": "<p>Here are the results.</p>",
            "table": _normalize_table_for_ui(last_table)  # asegurar normalización también aquí
        }
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
    """
    s = soql or ""
    # quitar 'sf.' sólo delante de identificadores (no tocar strings)
    s = re.sub(r"\bsf\.", "", s)
    # geografía correcta según whitelist
    s = re.sub(r"\bAccount\.Country\b", "Account.ShippingCountry", s)
    s = re.sub(r"\bAccount\.City\b", "Account.ShippingCity", s)
    # normalizar NULL literales
    s = re.sub(r"\bNULL\b", "null", s)
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
                ) AS "{key}"
            FROM public.sites s
            LEFT JOIN public.site_qual sq ON sq.site_id = s.id
            ORDER BY "{key}" {dir_sql} NULLS LAST
            LIMIT :top_n
        """
        out = tool_sql_query(db, sql, {"key": key, "top_n": int(top_n)})
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
