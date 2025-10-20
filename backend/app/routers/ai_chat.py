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

DEBUG = os.environ.get("AI_CHAT_DEBUG", "0") == "1"
INDEX_REFRESH_SEC = int(os.environ.get("AI_INDEX_REFRESH_SEC", "600"))
FIELDS_SF_JSON_PATH = os.environ.get("FIELDS_SF_JSON_PATH", "app/config/fields_opportunity_curated.json")
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

def _build_knowledge_index(db: Session) -> Dict[str, Any]:
    """
    Funde SF (curated) + site_qual. Si hay duplicidad de concepto, preferimos SF.
    """
    now = time()
    if (now - _INDEX_CACHE.get("ts", 0)) < INDEX_REFRESH_SEC and _INDEX_CACHE.get("index"):
        return _INDEX_CACHE

    sf_map = _load_sf_fields()  # normalized_alias -> {source:"sf", field, label}
    sq_map = _introspect_site_qual_keys(db)  # normalized_alias -> {source:"site_qual", key}

    fused: Dict[str, Dict[str, str]] = {}
    # Primero metemos site_qual
    fused.update(sq_map)
    # Luego SF pisa (prioridad)
    for k, v in sf_map.items():
        fused[k] = v

    _INDEX_CACHE["ts"] = now
    _INDEX_CACHE["index"] = fused
    _INDEX_CACHE["sf_fields"] = sf_map
    _dbg("INDEX built: %d aliases (sf=%d, sq=%d)", len(fused), len(sf_map), len(sq_map))
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

    # --- Conversión básica a HTML (para renderizado visual en ChatView) ---
    # Sustituimos saltos de línea dobles por párrafos y simples por <br>
    import html
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
        # último recurso: quitar __c y humanizar
        core = re.sub(r"^Account\.", "", core)
        core = re.sub(r"__c$", "", core)
        core = core.replace("_", " ")
        return core.replace(" T1D ", " T1D ").title()
    # qual.* -> humaniza
    if k.startswith("qual."):
        base = k.split(".",1)[1]
        base = re.sub(r"__c$", "", base).replace("_", " ")
        return base.title()
    # extra.* y otros
    if k.startswith("extra."):
        return k.split(".",1)[1].replace("_"," ").title()
    if k in ("site","city","country","account_id"):
        return {"site":"Account Name","city":"City","country":"Country","account_id":"Account Id"}[k]
    # por defecto humaniza
    return re.sub(r"[_]+"," ",k).title()

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
    "C_Center_for_Running_Early_Diagnosis__c","C_Centralized_Clinical_Trial_Facility__c",
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

def _validate_soql(soql: str):
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
        if base.startswith("Account."):
            if base not in SF_ALLOWED_FIELDS:
                bad.append(base)
            continue
        if base not in SF_ALLOWED_FIELDS:
            bad.append(base)
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
    # --- De-duplicación de columnas visibles ---
    # Si existen ambas 'sf.Account.Id' y 'account_id', mantener solo 'account_id'
    keys_now = [c["key"] for c in cols]
    if "sf.Account.Id" in keys_now and "account_id" in keys_now:
        cols = [c for c in cols if c["key"] != "sf.Account.Id"]
    # Reasignar
    # Quita columnas duplicadas "sf.Account.*" si ya tenemos sus amigables
    friendly = {"sf.Account.Id":"account_id","sf.Account.Name":"site",
                "sf.Account.ShippingCountry":"country","sf.Account.ShippingCity":"city"}
    cols = [c for c in cols if friendly.get(c.get("key")) is None] + \
           [ {"key": fk, "label": _pretty_label(fk)} for fk in ("account_id","site","country","city") if fk in col_set ]
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
    # Aseguramos que si se piden Account.* campos, también venga Account.Id
    soql_plus = _ensure_soql_has_account_id(soql)
    fixed = _sanitize_soql_basic(soql_plus)
    if fixed != soql:
        _dbg("SOQL (fixed) >>> %s", fixed)
    _validate_soql(fixed)
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
        "cs_clinical_site": (data.get("csContribution") or {}).get("Clinical_Site_CS__c"),
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
]

# ====== System prompt ======
SCHEMA_HINT = """
POSTGRES (warehouse):
- public.sites(id, name, street, city, country, postcode, latitude, longitude, salesforce_account_id)
- public.site_qual(site_id -> sites.id, data JSONB)  // Qualification/Profiling flattened key→value.
  Frequent keys:
    'C_Aware_of_any_Screening_Program__c', 'C_Center_for_Running_Early_Diagnosis__c',
    'C_Number_of_Stage1_Individuals_followed__c', 'C_Number_of_Stage2_Individuals_followed__c',
    'C_Number_of_T1D_Patients_currently_U_18__c', 'C_Number_of_T1D_Patients_currently_O_18__c',
    'C_Number_of_new_T1D_diagnosed_U_18__c', 'C_Number_of_new_T1D_diagnosed_O_18__c',
    plus many comments (keys containing 'comment').
  JSONB tips:
    - Safe casts, e.g. COALESCE(NULLIF(sq.data->>'C_Number_of_T1D_Patients_currently_U_18__c','')::int, 0) AS t1d_u18
    - For YES/NO strings, normalize to LOWER and compare to 'yes'.

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
- explorer_set_filters → Reflect a result set in Explorer (filters + columns).
- render_chart → Create a chart when asked or when visual comparison helps.
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
   - Prefer Postgres (sites + site_qual) unless SF session is active **and** the metric is an allowed SF field.
   - Order with DESC NULLS LAST (or equivalent) and LIMIT N.
   - Table + optional bar chart (x=Site, y=the metric).
B) Screening overview
   - Show flags + Stage1/Stage2; filter where any screening flag true.
C) Feature selection (e.g., onsite pharmacy & overnight stay)
   - Use booleans; otherwise derive from comments with case-insensitive search.
D) PI / Assignments / CS flags
   - If AccountId known → salesforce_account_extras; return PIName/Email/Phone, flags, assignments_count, new_dx_u18/o18.

STYLE
- Be direct and neutral. Fall back gracefully between SF and Postgres and mention it briefly in bullets.
- Do **not** show internal SQL/SOQL unless explicitly requested.

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
            if best:
                hints = []
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
                msgs.append({
                    "role":"system",
                    "content": (
                        "ROUTING HINTS for the last user request:\n- " + "\n- ".join(hints) +
                        "\nIf both SF and site_qual exist for the same concept, prefer SF."
                   )
                })
        
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
                    sf_fields_map = _INDEX_CACHE.get("sf_fields") or {}
                    human_cols = []
                    seen = set()
                    for c in structured2["table"]["columns"]:
                        key = c.get("key") if isinstance(c, dict) else str(c)
                        if key in seen: continue
                        seen.add(key)
                        human_cols.append({"key": key, "label": _pretty_label(key, sf_fields_map)})
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

