# backend/app/routers/salesforce_explorer.py
from __future__ import annotations
from sqlalchemy import literal
from typing import Any, Dict, List, Optional, Set, Literal, Tuple, Iterable
from fastapi import APIRouter, HTTPException, Request, Body, Depends
from pydantic import BaseModel, Field
import os, time, math, re, json, logging, threading
import httpx
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter
import hashlib

from simple_salesforce.exceptions import SalesforceExpiredSession, SalesforceAuthenticationFailed

from app.routers.salesforce_extras_batch import batch_fetch_account_extras

# DB
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.database import get_db
from app.models.site import Site
from app.models.site_qual import SiteQual
from app.models import Questionnaire, Question, Section, QuestionnaireType

# ====== OAuth helpers ======
from app.services.salesforce_oauth import (
    COOKIE_NAME, unsign_value, get_salesforce_from_session_id,
)

log = logging.getLogger("cts-backend")

salesforce_router = APIRouter(prefix="/api/salesforce", tags=["salesforce-explorer"])
explorer_router   = APIRouter(prefix="/api/explorer",   tags=["explorer"])

GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()

# ======================= CARGA DE CAMPOS (JSON CURADO SF) =======================

FIELDS_PATH = Path(__file__).parent.parent / "config" / "fields_opportunity_curated.json"
with open(FIELDS_PATH, encoding="utf-8") as f:
    FIELD_CONFIG: List[Dict[str, Any]] = json.load(f)

# Mapa de tipos por clave sin prefijo "sf."
TYPE_BY_KEY: Dict[str, str] = { f["key"].replace("sf.", ""): (f.get("type") or "string") for f in FIELD_CONFIG }

CURATED_ALLOWED: Set[str] = {
    f["key"].replace("sf.", "")
    for f in FIELD_CONFIG
    if f.get("source") == "sf"
}

MIN_ALLOWED: Set[str] = {
    # Opportunity básicos + Account para mapa
    "Id","Name","Type","StageName","IsClosed","CloseDate","AccountId",
    "Account.Id","Account.Name",
    "Account.BillingCity","Account.BillingCountry",
    "Account.ShippingCity","Account.ShippingCountry",
    "Account.ShippingLatitude","Account.ShippingLongitude",
}

ALLOWED_FIELDS: Set[str] = CURATED_ALLOWED | MIN_ALLOWED

NUMERIC_TYPES = {"double","number","int","integer","long","currency","percent"}

_DATE_FMTS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)

def _parse_date_any(x: Any) -> Optional[datetime]:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x
    s = str(x).strip()
    if not s:
        return None
    # Primero intenta ISO completo (no recortar la cadena)
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass  # <- NECESARIO: sin esto, el 'except' queda vacío y da IndentationError

    # Luego intenta los formatos conocidos (sin slicing)
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")

def _coerce_scalar(x: Any) -> Tuple[Any, str]:
    """ Devuelve (valor, tipo) con tipo in {'none','bool','num','date','str'} """
    if x is None:
        return None, "none"
    if isinstance(x, bool):
        return x, "bool"
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return x, "num"
    s = str(x).strip()
    if s.lower() in ("true", "false"):
        return (s.lower() == "true"), "bool"
    if _NUM_RE.match(s or ""):
        try:
            return (float(s) if "." in s else int(s)), "num"
        except Exception:
            pass
    d = _parse_date_any(s)
    if d is not None:
        return d, "date"
    return s, "str"

def _cmp(a: Any, b: Any) -> int:
    if type(a) is type(b):
        return 0 if a == b else (-1 if a < b else 1)
    sa, sb = str(a), str(b)
    return 0 if sa == sb else (-1 if sa < sb else 1)

def _normalize_list(v: Any) -> Iterable[Any]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return v
    s = str(v)
    return [p.strip() for p in s.split(",")] if "," in s else [v]

def _eval_qual_rule(value: Any, op: str, raw: Any) -> bool:
    op = (op or "").lower()
    v, tv = _coerce_scalar(value)

    # Null checks
    if op in ("is_null","isnull"):
        return v is None or (tv == "str" and str(v).strip() == "")
    if op in ("is_not_null","notnull"):
        return not (v is None or (tv == "str" and str(v).strip() == ""))

    # BETWEEN
    if op == "between":
        lo, hi = None, None
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            lo, hi = raw
        else:
            s = str(raw or "")
            lo, hi = (s.split("..",1) + [None])[:2] if ".." in s else (s.split(",",1) + [None])[:2]
        lo_v, _ = _coerce_scalar(lo); hi_v, _ = _coerce_scalar(hi)
        if v is None or lo_v is None or hi_v is None:
            return False
        return _cmp(lo_v, v) <= 0 and _cmp(v, hi_v) <= 0

    # IN / NOT IN
    if op in ("in","not_in"):
        items = [_coerce_scalar(x)[0] for x in _normalize_list(raw)]
        present = any(_cmp(v, it) == 0 for it in items)
        return present if op == "in" else (not present)

    # Binarios escalares
    tgt, _ = _coerce_scalar(raw)
    if op in ("equals","=","eq"):
        return _cmp(v, tgt) == 0
    if op in ("not_equals","!=","ne"):
        return _cmp(v, tgt) != 0
    if op in ("gt",">"):
        return _cmp(v, tgt) > 0
    if op in ("gte",">="):
        return _cmp(v, tgt) >= 0
    if op in ("lt","<"):
        return _cmp(v, tgt) < 0
    if op in ("lte","<="):
        return _cmp(v, tgt) <= 0

    # Strings
    vs = "" if v is None else str(v)
    ts = "" if tgt is None else str(tgt)
    if op == "contains":       return ts.lower() in vs.lower()
    if op == "not_contains":   return ts.lower() not in vs.lower()
    if op == "starts_with":    return vs.lower().startswith(ts.lower())
    if op == "ends_with":      return vs.lower().endswith(ts.lower())

    return True

def is_numeric_field(sf_field_no_prefix: str) -> bool:
    t = TYPE_BY_KEY.get(sf_field_no_prefix, "").lower()
    return t in NUMERIC_TYPES

# --- helpers para exponer campos de Qualification ---
def _slugify_question(text: str) -> str:
    import re
    t = (text or "").strip().lower()
    t = re.sub(r"\([^)]*\)", "", t)
    t = re.sub(r"[^a-z0-9]+", "_", t)
    t = re.sub(r"_+", "_", t).strip("_")
    return t or "q"

# === Overrides & filtros para el catálogo qual.* que muestra /api/explorer/fields ===
# Prefijos de grupo (sección) a excluir del Column Picker (p.ej. "4." -> irá a Profiling)
EXCLUDE_QUAL_GROUP_PREFIXES: tuple[str, ...] = ("1.", "PART I", "4.", "PART IV")

# Limite de “Describe” puros por subsección (independiente de versiones)
# clave = subcode con guiones bajos (p.ej. '3_4')
DESCRIBE_SLOTS_OVERRIDE: dict[str, int] = {
    "3_4": 2,   # Complex therapy Factors → 2 describes (labels largos)
    "3_5_1": 1, # Archiving → 1 describe
    "3_8": 1,   # Laboratory → 1 describe (label "Describe process")
}

# Cambios de etiquetas pedidos (clave = slug ya normalizado en qual.*)
# Si un override depende del grupo, usa dict anidado: {"_group": "Nombre del grupo", "label": "..."}
QUAL_LABEL_OVERRIDES: dict[str, dict | str] = {
    # 2.1 SOPs
    "comments": "Comments on SOPs or general institution",
    # 2.2 Recruitment & Consenting
    "meets_basic_criteria_per_gcp_ich": "Does the consenting process meet GCP requirements",
    "personal_conversation_with_physician": "Does the consenting process involve a Personal Conversation with Physician",
    "who_is_responsible": "Who is responsible for recruitment and consenting",
    # 2.3 Ethics & IRB
    "how_often_do_they_meet": "How often does the local ethics meet?",
    # 3.2 Study team
    "explanation": "Explanation of PI experience",
    "if_yes_who": "Who is involved with CGM training",
    # 3.3 Emergency care
    "acceptable_process": "Is the emergency response plan acceptable",
    "describe_details_of_site_s_process_for_handling_an_emergency":
        "Describe details of site’s process for handling an emergency",
    # 3.5.1 Archiving
    "if_yes_where_is_this_located": "Where are documents locked and stored",
    # 3.5.2 Examination rooms
    "if_yes_how_many": "# of dedicated exam rooms available",
    "how_many_beds": "# of beds available for extended stays",
    # 3.6 Pharmacy
    "location": {"_group": "3.6 Pharmacy / Preparation of Medication",
                 "label": "Location of the laminar flow hood for sterile preparation"},
}


# --- Helpers para formatear labels y grupos de claves qual.* nuevas ---
_SUBCODE_RE = re.compile(r"^\d+(?:_\d+)*$")          # 3, 3_5, 3_5_4 ...
_SUBSECT_CODE_EXTRACT_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)")

def _dots(s: str) -> str:
    return (s or "").replace("_", ".")

def _titlecase(s: str) -> str:
    s = (s or "").strip().replace("_", " ")
    return s[:1].upper() + s[1:] if s else s

def _build_subcode_to_group(db: Session) -> dict[str, str]:
    """
    Mapa '3_5_4' -> '3.5.4 Core IMP Storage Devices'
    Se basa en Question.subsection si empieza por el código.
    """
    rows = (
        db.query(Question.subsection)
          .filter(func.length(func.coalesce(Question.subsection, "")) > 0)
          .group_by(Question.subsection)
          .all()
    )
    out: dict[str, str] = {}
    for (subsec,) in rows:
        if not subsec:
            continue
        m = _SUBSECT_CODE_EXTRACT_RE.match(subsec)
        if not m:
            continue
        code_dots = m.group(1)                       # '3.5.4'
        code_unders = code_dots.replace(".", "_")    # '3_5_4'
        out.setdefault(code_unders, subsec.strip())
    return out

def _pretty_label_and_group_from_key(key_no_prefix: str,
                                     subcode_to_group: dict[str, str]) -> tuple[str, Optional[str]]:
    """
    key_no_prefix: p.ej. '3_5_4__fridge__temp_log' o '2_4__comments' o 'comments'
    Devuelve (label_legible, group_opcional)
    """
    parts = (key_no_prefix or "").split("__")
    # Caso con subcódigo + device + slug
    if len(parts) == 3 and _SUBCODE_RE.match(parts[0]):
        subcode, device, slug = parts
        label = f"{_titlecase(slug)} — {_titlecase(device)} ({_dots(subcode)})"
        group = subcode_to_group.get(subcode) or _dots(subcode)
        return label, group
    # Caso con subcódigo + slug (sin device)
    if len(parts) == 2 and _SUBCODE_RE.match(parts[0]):
        subcode, slug = parts
        label = f"{_titlecase(slug)} ({_dots(subcode)})"
        group = subcode_to_group.get(subcode) or _dots(subcode)
        return label, group
    # Compat: 'comments' o 'comments_2' antiguos
    m = re.match(r"^(comments)(?:_(\d+))?$", key_no_prefix)
    if m:
        idx = m.group(2)
        label = "Comments" if not idx else f"Comments #{idx}"
        return label, None
    # Fallback genérico
    return _titlecase(key_no_prefix), None


def _disambiguate_duplicate_labels(fields: list[dict]) -> list[dict]:
    """
    Si hay labels duplicadas (p.ej. varios 'Comments' o varios 'Describe'), añade un sufijo
    con el código de subsección usando la key:
      - qual.3_6_3__comments       -> Comments (3.6.3)
      - qual.2_4__comments_details -> Comments details (2.4)
    No cambia las keys, solo la etiqueta visible.
    """
    import re
    from collections import Counter

    counts = Counter()
    for f in fields:
        lbl = (f.get("label") or "").strip()
        if lbl:
            counts[lbl] += 1

    out: list[dict] = []
    for f in fields:
        k = str(f.get("key") or "")
        lbl = (f.get("label") or "").strip()

        # Si no está duplicado o no es qual.*, lo dejamos tal cual
        if counts.get(lbl, 0) <= 1 or not k.startswith("qual."):
            out.append(f)
            continue

        # Extrae subcode de la key si existe: qual.<sub>__...  -> <sub>
        raw = k[5:]  # sin 'qual.'
        parts = raw.split("__", 1)
        subcode = None
        if parts and re.match(r"^\d+(?:_\d+)*$", parts[0] or ""):
            subcode = parts[0].replace("_", ".")

        # Si no hay subcode en la key, intenta deducirlo del group (si empieza por "N.N")
        if not subcode:
            grp = (f.get("group") or "").strip()
            m = _SUBSECT_CODE_EXTRACT_RE.match(grp)
            if m:
                subcode = m.group(1)

        if subcode:
            f = {**f, "label": f"{lbl} ({subcode})"}

        out.append(f)

    return out


def _apply_qual_overrides(fields: list[dict]) -> list[dict]:
    """
    - Oculta secciones por prefijo (e.g., "4.")
    - Cambia labels según QUAL_LABEL_OVERRIDES
    - Prefija con "IMP " en 3.5.3 IMP Storage
    - Filtra cualquier 'qual.describe' genérico que pudiera colarse
    """
    out: list[dict] = []
    for f in fields:
        k = str(f.get("key") or "")
        grp = (f.get("group") or "").strip()

        # 0) Nunca publicar el genérico
        if k == "qual.describe":
            continue

        # 1) ocultar secciones 4.x
        if grp:
            gnorm = grp.strip().upper()
            if any(gnorm.startswith(p.upper()) for p in EXCLUDE_QUAL_GROUP_PREFIXES):
                continue

        # 2) overrides de label (tu lógica)
        if k.startswith("qual."):
            slug = k[5:]
            base = slug.split("__", 1)[0]
            ov = QUAL_LABEL_OVERRIDES.get(base) or QUAL_LABEL_OVERRIDES.get(slug)
            if isinstance(ov, str):
                f["label"] = ov
            elif isinstance(ov, dict):
                want_group = (ov.get("_group") or "").strip()
                if want_group and grp.strip() == want_group:
                    f["label"] = ov.get("label") or f.get("label")

            # 3) prefijo "IMP " en 3.5.3
            if grp.startswith("3.5.3"):
                lbl = f.get("label") or ""
                if not lbl.lower().startswith("imp "):
                    f["label"] = f"IMP {lbl}"

        out.append(f)
    return out


def _qual_groups_from_questions(db: Session) -> Dict[str, str]:
    """
    Devuelve { slug(question_text): group_label } donde group_label prioriza
    Question.subsection y, si no hay, Section.name. Sirve para agrupar qual.*.
    """
    out: Dict[str, str] = {}
    rows = (
        db.query(Question.question_text, Question.subsection, Section.name)
        .join(Section, Section.id == Question.section_id)
        .join(Questionnaire, Questionnaire.id == Section.questionnaire_id)
        .filter(Questionnaire.type == QuestionnaireType.qualification)
        .filter(func.length(func.coalesce(Question.question_text, "")) > 0)
        .limit(5000)
        .all()
    )
    for qt, subsec, secname in rows:
        slug = _slugify_question(qt or "")
        if not slug:
            continue
        group = (subsec or secname or "").strip()
        if not group:
            continue
        out.setdefault(slug, group)
    return out

def _qualification_field_defs(db: Session) -> list[dict]:
    q = (
        db.query(Question.question_text)
        .join(Section, Section.id == Question.section_id)
        .join(Questionnaire, Questionnaire.id == Section.questionnaire_id)
        .filter(Questionnaire.type == QuestionnaireType.qualification)
        .filter(func.length(func.coalesce(Question.question_text, "")) > 0)
        .group_by(Question.question_text)
        .limit(2000)
    )
    out = []
    for (qt,) in q.all():
        key = f"qual.{_slugify_question(qt)}"
        out.append({
            "key": key,
            "label": qt.strip(),
            "type": "string",
            "source": "qual",
        })
    return out

# ======================= si se necesitan extras =======================

def _needs_extras(payload: dict) -> bool:
    cols = payload.get("columns") or []
    if any(str(c).startswith("extra.") for c in cols):
        return True

    def scan(node) -> bool:
        if not node:
            return False
        if isinstance(node, dict):
            fld = node.get("field") or node.get("key")
            if fld and str(fld).startswith("extra."):
                return True
            return scan(node.get("rules")) or scan(node.get("children"))
        if isinstance(node, list):
            return any(scan(x) for x in node)
        return False

    return scan(payload.get("filters"))

# ======================= MODELOS Pydantic (request) =======================

class Rule(BaseModel):
    field: str
    operator: str
    value: Any

class FilterQuery(BaseModel):
    logic: Literal["AND", "OR"] = "AND"
    rules: List[Rule] = Field(default_factory=list)
    columns: List[str] = Field(default_factory=list)

# ======================= HELPERS SF =======================

def _sf_query_all(sf, soql: str):
    try:
        res = sf.query_all(soql)
        return res.get("records", [])
    except (SalesforceExpiredSession, SalesforceAuthenticationFailed):
        raise HTTPException(status_code=401, detail="Salesforce session expired")
    except Exception as e:
        log.exception("SF query failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Salesforce query failed: {e}")

def _get_sf(request: Request):
    signed = request.cookies.get(COOKIE_NAME)
    session_id = unsign_value(signed) if signed else None
    sf = get_salesforce_from_session_id(session_id) if session_id else None
    if not sf:
        raise HTTPException(403, "No autenticado en Salesforce")
    return sf


# -------- describe() cache para validar campos ----------
_OPP_FIELD_SET: Optional[Set[str]] = None
_ACC_FIELD_SET: Optional[Set[str]] = None
_DESCRIBE_PERMISSIVE: bool = False
_DESCRIBE_LOCK = threading.RLock()

def _ensure_describes(sf):
    global _OPP_FIELD_SET, _ACC_FIELD_SET, _DESCRIBE_PERMISSIVE
    with _DESCRIBE_LOCK:
        if _OPP_FIELD_SET is None:
            try:
                _OPP_FIELD_SET = {f["name"] for f in sf.Opportunity.describe()["fields"]}
            except Exception as e:
                log.warning("Opportunity.describe() failed, enabling permissive mode: %s", e)
                _OPP_FIELD_SET = set()
                _DESCRIBE_PERMISSIVE = True
        if _ACC_FIELD_SET is None:
            try:
                _ACC_FIELD_SET = {f["name"] for f in sf.Account.describe()["fields"]}
            except Exception as e:
                log.warning("Account.describe() failed, enabling permissive mode: %s", e)
                _ACC_FIELD_SET = set()
                _DESCRIBE_PERMISSIVE = True

def _exists_on_opportunity(field_name: str) -> bool:
    if _DESCRIBE_PERMISSIVE:
        # Modo permisivo: confía en catálogo curado
        return True
    return field_name in (_OPP_FIELD_SET or set())

def _exists_on_account(field_name: str) -> bool:
    return field_name in (_ACC_FIELD_SET or set())

# ======================= WHERE BUILDER (SOQL) =======================

_OP_SYNONYM = {
    "eq": "equals",
    "neq": "not_equals",
    "icontains": "contains",
    "≥": ">=",
    "≤": "<=",
}

_OP_MAP = {
    "equals": "=", "not_equals": "!=",
    ">": ">", ">=": ">=", "<": "<", "<=": "<=",
}

def _norm_label_to_key(x: Any) -> str:
    if isinstance(x, dict):
        x = x.get("key") or x.get("value") or x.get("id") or ""
    s = str(x or "").strip()
    if s.startswith("[sf] "):   s = s[5:]
    if s.startswith("[site] "): s = s[8:]
    if s.startswith("[qual] "): s = s[7:]
    return s

def _safe_field(field: str) -> str:
    if field not in ALLOWED_FIELDS:
        if field.startswith("Account."):
            return field
        raise HTTPException(400, f"Campo no permitido: {field}")
    if field.startswith("Account."):
        return field
    if not _exists_on_opportunity(field):
        if _exists_on_account(field):
            return f"Account.{field}"
        raise HTTPException(400, f"Campo no existe en Opportunity: {field}")
    return field

def _escape_quotes(v: str) -> str:
    return v.replace("'", "\\'")

def _coerce_value_for_field(field_no_prefix: str, value: Any):
    if is_numeric_field(field_no_prefix):
        try:
            if value is None or value == "":
                return None, True
            return float(value), True
        except Exception:
            return value, False
    return value, False

BOOL_TYPES = {"boolean"}
DATE_TYPES = {"date", "datetime"}

def _soql_quote(v: Any, is_datetime: bool = False) -> str:
    # Booleans
    if isinstance(v, bool):
        return "true" if v else "false"
    # Numéricos
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    # Fechas
    d = _parse_date_any(v)
    if d is not None:
        if is_datetime:
            # Si vino "YYYY-MM-DD", completa a T00:00:00Z
            if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", v.strip()):
                return f"{v.strip()}T00:00:00Z"
            return d.strftime('%Y-%m-%dT%H:%M:%SZ')
        return d.strftime('%Y-%m-%d')
    # String
    s = str(v).replace("'", "\\'")
    return f"'{s}'"

def _field_type(field_no_prefix: str) -> str:
    return (TYPE_BY_KEY.get(field_no_prefix, "") or "").lower()

def _build_sf_where(q: FilterQuery) -> str:
    if not q or not q.rules:
        return ""
    clauses: List[str] = []
    for r in q.rules:
        if r.field.startswith("Account."):
            continue
        f = _safe_field(r.field)
        op_raw = r.operator
        op = _OP_SYNONYM.get(op_raw, op_raw).lower()
        f_no_prefix = f
        ftype = _field_type(f_no_prefix)

        # Null checks
        if op in ("is_null","isnull"):
            clauses.append(f"{f} = null"); continue
        if op in ("is_not_null","notnull"):
            clauses.append(f"{f} != null"); continue

        # BETWEEN
        if op == "between":
            lo, hi = None, None
            val = r.value
            if isinstance(val, (list, tuple)) and len(val) == 2:
                lo, hi = val
            else:
                s = str(val or "")
                if ".." in s: lo, hi = s.split("..",1)
                elif "," in s: lo, hi = s.split(",",1)
            if lo is None or hi is None:
                continue
            # numérico sin comillas; datetime con formateo; resto string
            if is_numeric_field(f_no_prefix):
                vlo, ok_lo = _coerce_value_for_field(f_no_prefix, lo)
                vhi, ok_hi = _coerce_value_for_field(f_no_prefix, hi)
                if ok_lo and isinstance(vlo, (int, float)) and ok_hi and isinstance(vhi, (int, float)):
                    clauses.append(f"({f} >= {vlo} AND {f} <= {vhi})")
                else:
                    # fallback a string (citado)
                    clauses.append(f"({f} >= '{_escape_quotes(str(lo or ''))}' AND {f} <= '{_escape_quotes(str(hi or ''))}')")
            elif ftype in DATE_TYPES:
                clauses.append(f"({f} >= {_soql_quote(lo, is_datetime=(ftype=='datetime'))} AND {f} <= {_soql_quote(hi, is_datetime=(ftype=='datetime'))})")
            else:
                clauses.append(f"({f} >= '{_escape_quotes(str(lo or ''))}' AND {f} <= '{_escape_quotes(str(hi or ''))}')")
            continue

        # IN / NOT IN
        if op in ("in","not_in"):
            # normaliza lista y elimina entradas vacías tras trim
            raw = r.value if isinstance(r.value, (list, tuple, set)) else str(r.value).split(",")
            arr = [ (str(v).strip() if not isinstance(v, (int,float,bool)) else v) for v in raw ]
            arr = [ v for v in arr if (str(v).strip() if not isinstance(v, (int,float,bool)) else True) ]             
            vals_parts: List[str] = []
            for v in arr:
                if is_numeric_field(f_no_prefix):
                    vv, ok = _coerce_value_for_field(f_no_prefix, v)
                    vals_parts.append(str(vv) if ok and isinstance(vv, (int, float)) else f"'{_escape_quotes(str(v))}'")
                elif ftype in DATE_TYPES:
                    vals_parts.append(_soql_quote(v, is_datetime=(ftype=='datetime')))
                else:
                    vals_parts.append(f"'{_escape_quotes(str(v))}'")
            vals = ", ".join(vals_parts)
            clauses.append(f"{f} {'IN' if op=='in' else 'NOT IN'} ({vals})")
            continue

        # Comparaciones y equals
        sop = _OP_MAP.get(op)
        if sop:
            # boolean
            if ftype in BOOL_TYPES:
                v = r.value
                if isinstance(v, str):
                    v = v.strip().lower() in ("true","1","yes","y")
                clauses.append(f"{f} {sop} {_soql_quote(bool(v))}")
                continue
            # numérico
            if is_numeric_field(f_no_prefix):
                v, ok = _coerce_value_for_field(f_no_prefix, r.value)
                if ok and isinstance(v, (int,float)):
                    clauses.append(f"{f} {sop} {v}")
                    continue
            # date/datetime
            if ftype in DATE_TYPES:
                clauses.append(f"{f} {sop} {_soql_quote(r.value, is_datetime=(ftype=='datetime'))}")
                continue
            # string
            sv = _escape_quotes(str(r.value or ""))
            clauses.append(f"{f} {sop} '{sv}'")
            continue

        # String LIKE ops
        sv = _escape_quotes(str(r.value or ""))
        if op == "contains":
            clauses.append(f"{f} LIKE '%{sv}%'"); continue
        if op == "not_contains":
            clauses.append(f"{f} NOT LIKE '%{sv}%'"); continue
        if op == "starts_with":
            clauses.append(f"{f} LIKE '{sv}%'"); continue
        if op == "ends_with":
            clauses.append(f"{f} LIKE '%{sv}'"); continue

        log.warning("Operador no soportado en SF where: %s", op)

    if not clauses:
        return ""
    glue = " AND " if (q.logic or "AND") == "AND" else " OR "
    # Devuelve SIEMPRE agrupado en paréntesis para proteger precedencias aguas arriba
    return f"({glue.join(clauses)})"

# ======================= UTILIDADES ACCOUNT =======================

# --- Normalización ligera de países (Europa + comunes)
_ISO2 = {
    "spain": "ES", "españa": "ES", "espana": "ES", "es": "ES",
    "portugal": "PT", "pt": "PT",
    "andorra": "AD", "ad": "AD",
    "france": "FR", "francia": "FR", "fr": "FR",
    "germany": "DE", "deutschland": "DE", "de": "DE",
    "italy": "IT", "italia": "IT", "it": "IT",
    "belgium": "BE", "belgië": "BE", "belgie": "BE", "belgique": "BE", "be": "BE",
    "netherlands": "NL", "nederland": "NL", "holland": "NL", "nl": "NL",
    "luxembourg": "LU", "luxemburg": "LU", "lu": "LU",
    "united kingdom": "GB", "uk": "GB", "gb": "GB",
    "ireland": "IE", "eire": "IE", "ie": "IE",
    "denmark": "DK", "danmark": "DK", "dk": "DK",
    "sweden": "SE", "sverige": "SE", "se": "SE",
    "norway": "NO", "norge": "NO", "no": "NO",
    "finland": "FI", "suomi": "FI", "fi": "FI",
    "poland": "PL", "polska": "PL", "pl": "PL",
    "czech republic": "CZ", "czechia": "CZ", "cesko": "CZ", "cz": "CZ",
    "slovakia": "SK", "slovensko": "SK", "sk": "SK",
    "hungary": "HU", "magyarország": "HU", "hu": "HU",
    "romania": "RO", "ro": "RO",
    "bulgaria": "BG", "bg": "BG",
    "greece": "GR", "ellada": "GR", "ελλάδα": "GR", "gr": "GR",
    "croatia": "HR", "hrvatska": "HR", "hr": "HR",
    "serbia": "RS", "srbija": "RS", "rs": "RS",
    "bosnia": "BA", "bosnia and herzegovina": "BA", "ba": "BA",
    "montenegro": "ME", "crna gora": "ME", "me": "ME",
    "north macedonia": "MK", "macedonia": "MK", "mk": "MK",
    "albania": "AL", "al": "AL",
    "estonia": "EE", "eesti": "EE", "ee": "EE",
    "latvia": "LV", "latvija": "LV", "lv": "LV",
    "lithuania": "LT", "lietuva": "LT", "lt": "LT",
    "cyprus": "CY", "kypros": "CY", "cy": "CY",
    "malta": "MT", "mt": "MT",
    # bordes frecuentes
    "switzerland": "CH", "suisse": "CH", "schweiz": "CH", "svizzera": "CH", "ch": "CH",
    "turkey": "TR", "türkiye": "TR", "tr": "TR",
}

# alias de ciudades por país (añade las que vayas viendo)
_CITY_ALIASES = {
    "ES": {
        "seville": "sevilla",
        "cordoba": "córdoba",
        "zaragoza": "zaragoza",  # ejemplo de no-cambio
    },
    "BE": { "antwerp": "antwerpen", "brussels": "bruxelles" },
    "DE": { "munich": "münchen", "cologne": "köln" },
}

def _country_norm(country: Optional[str]) -> Optional[str]:
    if not country: 
        return None
    s = str(country).strip().lower()
    if s in _ISO2:
         return _ISO2[s]
    if len(s) == 2:
         return s.upper()
    return s.title()  # devuelve legible si no conocemos el país

def _city_alias(city: Optional[str], country_iso2: Optional[str]) -> Optional[str]:
    if not city: 
        return None
    c = str(city).strip()
    iso = (country_iso2 or "").upper()
    m = _CITY_ALIASES.get(iso, {})
    rep = m.get(c.lower())
    return rep or c

def _acc_city_country(acc: Dict[str, Any]) -> Dict[str, Optional[str]]:
    city = acc.get("ShippingCity") or acc.get("BillingCity")
    country = acc.get("ShippingCountry") or acc.get("BillingCountry")
    iso = _country_norm(country)
    city = _city_alias(city, iso)
    return {"city": city, "country": iso or country}

def _acc_lat_lng(acc: Dict[str, Any]) -> Dict[str, Optional[float]]:
    lat = acc.get("ShippingLatitude") or acc.get("BillingLatitude")
    lng = acc.get("ShippingLongitude") or acc.get("BillingLongitude")
    return {"latitude": lat, "longitude": lng}

TYPE_ALIASES = [
    "Qualification", "CTS/CTU Qualification",
    "Profiling", "CTS/CTU Profiling", "CTS Profiling",
]
TYPE_IN = ", ".join(f"'{t}'" for t in TYPE_ALIASES)

def _norm_type(t: str | None) -> str | None:
    t = (t or "").lower()
    if "rofil" in t: return "profiling"
    if "qual"  in t: return "qualification"
    return None

def _build_account_map(sf, acc_ids: List[str]) -> Dict[str, Dict]:
    if not acc_ids:
        return {}

    def chunk(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i+n]

    # 🚨 Añadimos los flags de inactividad
    fields = """
        Id, Name,
        ShippingCity, ShippingCountry, ShippingLatitude, ShippingLongitude,
        BillingCity,  BillingCountry,  BillingLatitude,  BillingLongitude,
        Account_Inactive__c, Subaccount_Inactive__c
    """

    out: Dict[str, Dict] = {}
    for group in chunk(acc_ids, 150):
        vals = ", ".join(f"'{x}'" for x in group)
        recs = _sf_query_all(sf, f"SELECT {fields} FROM Account WHERE Id IN ({vals})")
        for a in recs:
            # ❌ Filtrar cuentas inactivas
            if a.get("Account_Inactive__c") or a.get("Subaccount_Inactive__c"):
                continue

            city = a.get("ShippingCity") or a.get("BillingCity")
            country = _country_norm(a.get("ShippingCountry") or a.get("BillingCountry"))
            city = _city_alias(city, country)
            lat = a.get("ShippingLatitude") or a.get("BillingLatitude")
            lng = a.get("ShippingLongitude") or a.get("BillingLongitude")

            out[a["Id"]] = {
                "name": a.get("Name"),
                "city": city, "country": country,
                "lat":  lat,  "lng":     lng,
            }
    return out

# =============== GEOCODING (con cache + persistencia) ===============

# Cache en memoria (address -> (lat, lng, expires_at))
_GEO_CACHE: Dict[str, Tuple[Optional[float], Optional[float], float]] = {}
_GEO_TTL_SECONDS = 60 * 60 * 24  # 24h
_CACHE_LOCK = threading.RLock()

def _geo_key(city: Optional[str], country: Optional[str]) -> str:
    parts = [(city or "").strip().lower(), (country or "").strip().lower()]
    return "|".join(parts)

def _geo_cache_get(city: Optional[str], country: Optional[str]) -> Tuple[Optional[float], Optional[float]] | None:
    k = _geo_key(city, country)
    with _CACHE_LOCK:
        tup = _GEO_CACHE.get(k)
    if not tup:
        return None
    lat, lng, exp = tup
    if time.time() > exp:
        with _CACHE_LOCK:
            _GEO_CACHE.pop(k, None)
        return None
    return (lat, lng)

def _geo_cache_put(city: Optional[str], country: Optional[str], lat: Optional[float], lng: Optional[float]) -> None:
    k = _geo_key(city, country)
    with _CACHE_LOCK:
        _GEO_CACHE[k] = (lat, lng, time.time() + _GEO_TTL_SECONDS)

def _extract_result_country_iso(result: dict) -> Optional[str]:
    try:
        for comp in result.get("address_components", []):
            if "country" in comp.get("types", []):
                code = comp.get("short_name")
                return code
    except Exception:
        return None
    return None

async def _geocode_city_country(
    city: Optional[str],
    country: Optional[str],
) -> Tuple[Optional[float], Optional[float]]:
    """
    Geocodifica 'city, country' con caché en memoria. Devuelve (lat, lng) o (None, None).
    """
    # Normalización ligera (si tienes helpers externos, cámbialos aquí)
    cty = (city or "").strip()
    cty = cty or None
    cty_key = cty.lower() if cty else None
    # Alias simples de ciudad problemáticas (opcional)
    CITY_ALIAS = {
        "seville": "Sevilla",
        "florence": "Firenze",
        "cologne": "Köln",
    }
    if cty_key in CITY_ALIAS:
        cty = CITY_ALIAS[cty_key]

    cnt = (country or "").strip()
    cnt = cnt or None

    # Cache
    cached = _geo_cache_get(cty, cnt)
    if cached is not None:
        return cached

    if not GOOGLE_API_KEY:
        log.warning("GEOCODING skipped: missing GOOGLE_MAPS_API_KEY")
        return (None, None)

    q_parts = [p for p in (cty, cnt) if p]
    if not q_parts:
        return (None, None)

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": ", ".join(q_parts), "key": GOOGLE_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
        data = r.json()
        if data.get("status") == "OK" and data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            lat = loc.get("lat")
            lng = loc.get("lng")
            _geo_cache_put(cty, cnt, lat, lng)
            return (lat, lng)
    except Exception as e:
        # No rompas el flujo por errores de red; cachea None para evitar golpes repetidos
        log.warning("GEOCODING error for %s, %s: %s", cty, cnt, e)

    _geo_cache_put(cty, cnt, None, None)
    return (None, None)

# ======================= QUAL.* (desde BD) =======================

def _duplicated_slug_order(db: Session, base_slug: str) -> list[str]:
    """
    Devuelve la lista de subsecciones (slugificadas) en el orden en que aparece
    ese mismo enunciado (mismo slug base) dentro del cuestionario Qualification.
    """
    rows = (
        db.query(Question.question_text, Question.subsection, Section.name)
        .join(Section, Section.id == Question.section_id)
        .join(Questionnaire, Questionnaire.id == Section.questionnaire_id)
        .filter(Questionnaire.type == QuestionnaireType.qualification)
        .all()
    )
    # recoge en orden
    order: list[str] = []
    seen = set()
    for qt, subsec, secname in rows:
        if _slugify_question(qt) != base_slug:
            continue
        grp = (subsec or secname or "").strip()
        gslug = _slugify_question(grp)
        if gslug and gslug not in seen:
            seen.add(gslug)
            order.append(gslug)
    return order

# 1) NUEVO: fan-out de DESCRIBE por fila, igual que comments
def _expand_describe_keys_for_row(row_data: dict, groups_by_slug: dict[str, str]) -> dict:
    """
    Normaliza 'describe' genéricos en la fila a claves con subcódigo:
      -> '<subcode>__describe', '<subcode>__describe_2', ...

    Reglas:
    - Nunca deja 'describe' ni 'describe_N' genéricos en la salida.
    - Si en la fila puede inferirse UN único subcódigo presente (p.ej. por otras
      claves como '<sub>__comments' u otras de la misma subsección), se usan
      esas claves destino aunque no existan previamente en row_data.
    - Si hay varios subcódigos en la fila y no es posible desambiguar,
      no toca los genéricos (para no perder datos).
    """
    import re

    if not row_data:
        return row_data

    out = dict(row_data)

    # 1) Recolecta posibles valores "genéricos" de describe
    generic_keys = [k for k in row_data.keys() if k == "describe" or k.startswith("describe_")]
    values = [row_data[k] for k in sorted(generic_keys) if row_data.get(k) not in (None, "")]
    if not values:
        return out

    # 2) Intenta inferir un único subcódigo presente en la fila mirando claves tipo '<sub>__...'
    #    (ej. '3_4__comments', '3_4__something_else', etc.)
    subcodes_in_row = set()
    for k in row_data.keys():
        m = re.match(r"^(\d+(?:_\d+)*)__", k or "")
        if m:
            subcodes_in_row.add(m.group(1))

    # Si no hay subcódigo claro o hay más de uno → no tocamos nada para no asumir mal
    if len(subcodes_in_row) != 1:
        return out

    sub = next(iter(subcodes_in_row))

    # 3) Distribuye a '<sub>__describe', '<sub>__describe_2', ... respetando el límite
    limit = DESCRIBE_SLOTS_OVERRIDE.get(sub, len(values)) or len(values)
    for i, v in enumerate(values[:limit], start=1):
        dk = f"{sub}__describe" if i == 1 else f"{sub}__describe_{i}"
        out[dk] = v

    # 4) Elimina los genéricos para que no "existan" en pasos posteriores
    for k in generic_keys:
        out.pop(k, None)

    return out


def _infer_qual_fields(db: Session, sample: int = 200) -> List[Dict[str, Any]]:
    """
    Construye el catálogo de campos qual.* a partir de:
      - Esquema de preguntas (para sembrar __comments y __describe "puros")
      - Datos reales en SiteQual (para tipado y fan-out por fila)
    Reglas clave:
      - NUNCA emitir 'qual.describe' genérico
      - Solo sembrar __describe cuando el texto de la pregunta es EXACTAMENTE 'Describe' o 'Describe N'
      - Las frases del tipo 'Describe the site's ...' NO se consideran 'describe' genérico (vendrán como campos distintos si los expandes)
    """
    import re

    # Carga de datos
    rows = db.execute(select(SiteQual)).scalars().all()

    keys_types: Dict[str, str] = {}
    groups_by_slug = _qual_groups_from_questions(db)

    # === 1) Recorremos el árbol de preguntas para sembrar '__comments' y '__describe' (puros) ===
    rows_q = (
        db.query(
            Question.id,
            Question.question_text,
            Question.subsection,   # p.ej. "3.4 Complex therapy Factors"
            Section.name,          # p.ej. "PART III – …"
            literal(0),
            literal(0),
        )
        .join(Section, Section.id == Question.section_id)
        .join(Questionnaire, Questionnaire.id == Section.questionnaire_id)
        .filter(Questionnaire.type == QuestionnaireType.qualification)
        .order_by(literal(0).asc(), literal(0).asc())
        .all()
    )

    # 1.a) __comments por subsección (tu lógica existente)
    seen_sub = set()
    for _, qt, subsec, secname, *_ in rows_q:
        if _slugify_question(qt) == "comments":
            subcode, _ = _extract_subcode_and_group((subsec or secname or "").strip())
            if subcode and subcode not in seen_sub:
                seen_sub.add(subcode)
                keys_types.setdefault(f"{subcode}__comments", "string")

    from collections import defaultdict
    describe_slots_by_sub: Dict[str, set[int]] = defaultdict(set)

    for _, qt, subsec, secname, *_ in rows_q:
        qtxt = (qt or "").strip()
        # ✅ match solo "Describe", "Describe:", "Describe N"
        m = re.fullmatch(r"(?i)describe[:]?(\s+\d+)?$", qtxt)
        if not m:
            continue
        subcode, _ = _extract_subcode_and_group((subsec or secname or "").strip())
        if not subcode:
            continue
        idx = 1
        if m.group(1):
            try:
                idx = int(m.group(1).strip())
            except Exception:
                idx = 1
        describe_slots_by_sub[subcode].add(idx)

    # 🔁 Asegura SEMPRE los slots definidos arriba, aunque no aparezcan en datos/árbol
    for subcode, forced_n in DESCRIBE_SLOTS_OVERRIDE.items():
        forced = set(range(1, forced_n + 1))
        describe_slots_by_sub[subcode] = (describe_slots_by_sub.get(subcode) or set()) | forced
        for idx in sorted(describe_slots_by_sub[subcode])[:forced_n]:
            key = f"{subcode}__describe" if idx == 1 else f"{subcode}__describe_{idx}"
            keys_types.setdefault(key, "string")

    # === 2) Fan-out por fila: comments + describe; y tipado ===
    for row in rows:
        row_data = row.data or {}

        # fan-out comments existente
        row_data = _expand_comments_keys_for_row(row_data, groups_by_slug)
        # fan-out describe (usa tu helper actual)
        row_data = _expand_describe_keys_for_row(row_data, groups_by_slug)

        for k, v in (row_data or {}).items():
            # --- FILTRO: mantener solo describe_N que estén en los slots permitidos (deduplicados por subsección) ---
            if "__describe" in k:
                base = k.split("__", 1)[0]  # "<sub>"
                idx = 1
                m_idx = re.search(r"__describe_(\d+)$", k)
                if m_idx:
                    try:
                        idx = int(m_idx.group(1))
                    except Exception:
                        idx = 1
                # si hay override, respétalo (1..N). Si no, usa los slots detectados.
                if base in DESCRIBE_SLOTS_OVERRIDE:
                    allowed_slots = set(range(1, DESCRIBE_SLOTS_OVERRIDE[base] + 1))
                else:
                    allowed_slots = describe_slots_by_sub.get(base) or {1}
                # si el índice no pertenece a los slots conocidos, lo ignoramos
                if idx not in allowed_slots:
                    continue

            t = "string"
            if isinstance(v, bool):
                t = "boolean"
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                t = "number"

            prev = keys_types.get(k)
            keys_types[k] = t if prev is None or prev == t else "string"

    # === 3) Etiquetado bonito ===
    subcode_to_group = _build_subcode_to_group(db)

    # Overrides de label para describe
    DESCRIBE_LABEL_OVERRIDES = {
        # 3.4 (dos Describe)
        "3_4__describe":   "Describe the site's experience Managing serious AEs",
        "3_4__describe_2": "Describe the site's experience with Phase 1 or Islet Transplant",
        # 3.5.1 (uno)
        "3_5_1__describe": "Describe",
        # 3.8 (uno, texto específico)
        "3_8__describe":   "Describe process",
    }

    out: List[Dict[str, Any]] = []
    for k, t in sorted(keys_types.items()):
        # Nunca emitas 'describe' genérico
        if k == "describe":
            continue

        label, group = _pretty_label_and_group_from_key(k, subcode_to_group)

        # Ajuste de labels SOLO para __describe*
        if "__describe" in k:
            if k in DESCRIBE_LABEL_OVERRIDES:
                label = DESCRIBE_LABEL_OVERRIDES[k]
            else:
                prefix = (group or "").split()[0] if group else ""
                label = f"Describe ({prefix})" if prefix else "Describe"

        item = {"key": f"qual.{k}", "label": label, "type": t, "source": "qual"}
        if group:
            item["group"] = group
        out.append(item)

    return out
    
def _extract_subcode_and_group(group_label: str | None) -> tuple[str | None, str | None]:
    """
    De un label de grupo como '2.2 Recruitment and Consenting' extrae:
      -> ('2_2', '2.2 Recruitment and Consenting')
    Si no hay código, devuelve (None, group_label).
    """
    if not group_label:
        return None, None
    m = _SUBSECT_CODE_EXTRACT_RE.match(group_label)
    if not m:
        return None, group_label.strip()
    code_dots = m.group(1)  # '2.2'
    return code_dots.replace(".", "_"), group_label.strip()

def _guess_row_group_from_keys(row_data: dict[str, Any], groups_by_slug: dict[str, str]) -> tuple[str | None, str | None]:
    """
    Dado un JSON de SiteQual (una sección), intenta deducir su grupo
    a partir de cualquiera de sus claves distintas de 'comments*'.
    Devuelve (subcode_unders, group_label) o (None, None).
    """
    for k in (row_data or {}).keys():
        base = re.sub(r"__(.*)$", "", k)       # por si viniera con '__device' o similar
        if base.lower().startswith("comments"):
            continue
        g = groups_by_slug.get(base)
        if g:
            return _extract_subcode_and_group(g)
    # Si sólo hay 'comments', igual intentamos con la propia clave
    # (no debería ocurrir, pero mantenemos compat).
    return None, None

def _expand_comments_keys_for_row(row_data: dict[str, Any],
                                  groups_by_slug: dict[str, str]) -> dict[str, Any]:
    """
    Clona el dict y, si existe 'comments' (o 'comments_#'), añade
    la clave con subcódigo: '2_2__comments' (y '2_2__comments_2', ...).
    No elimina las claves originales para mantener compatibilidad.
    """
    if not row_data:
        return {}
    out = dict(row_data)
    # ¿Hay alguna 'comments*'?
    comment_like = [(k, v) for k, v in row_data.items() if k.lower() == "comments" or k.lower().startswith("comments_")]
    if not comment_like:
        return out
    subcode, _grp = _guess_row_group_from_keys(row_data, groups_by_slug)
    if not subcode:
        return out  # no pudimos inferir la sección; no inventamos nada
    for k, v in comment_like:
        suffix = "" if k.lower() == "comments" else k[len("comments"):]  # '', '_2', '_3', ...
        out[f"{subcode}__comments{suffix}"] = v
    return out

# ======================= Collapse by Account iD =======================

def _parse_dt(x: Optional[str]) -> datetime:
    if not x:
        return datetime.min
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(x[:len(fmt)], fmt)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(x)
    except Exception:
        return datetime.min

def _is_closed_won_row(r: Dict[str, Any]) -> bool:
    d = r.get("data", {}) or {}
    stage = (d.get("sf.StageName") or d.get("StageName") or "").strip().lower()
    return stage == "closed won"

def _pick_best(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    cw = [r for r in rows if _is_closed_won_row(r)]
    if cw:
        return max(
            cw,
            key=lambda r: (
                _parse_dt(r["data"].get("sf.CloseDate") or r["data"].get("CloseDate")),
                _parse_dt(r["data"].get("sf.CreatedDate") or r["data"].get("CreatedDate")),
                _parse_dt(r["data"].get("sf.LastModifiedDate") or r["data"].get("LastModifiedDate")),
            ),
        )
    return max(
        rows,
        key=lambda r: (
            _parse_dt(r["data"].get("sf.CloseDate") or r["data"].get("CloseDate")),
            _parse_dt(r["data"].get("sf.CreatedDate") or r["data"].get("CreatedDate")),
            _parse_dt(r["data"].get("sf.LastModifiedDate") or r["data"].get("LastModifiedDate")),
        ),
    )

def _classify_kind_from_row(r: Dict[str, Any]) -> str:
    t = (r.get("opportunity_type") or "").strip().lower()
    if t in ("profiling", "qualification"):
        return t
    data = r.get("data", {}) or {}
    if any(str(k).startswith("qual.") for k in data.keys()):
        return "qualification"
    if any(str(k).startswith("sf.") for k in data.keys()):
        return "profiling"
    return "unknown"

def _account_key_and_name(r: Dict[str, Any]) -> Tuple[str, str]:
    acc_id = r.get("account_id") or ""
    acc_name = r.get("account_name") or ""
    if acc_id:
        return str(acc_id), str(acc_name)
    d = r.get("data", {}) or {}
    acc_id = d.get("sf.Account.Id") or d.get("sf.AccountId") or d.get("AccountId") or ""
    acc_name = acc_name or d.get("sf.Account.Name") or d.get("AccountName") or ""
    return str(acc_id), str(acc_name)

def _flatten_sf_inplace(d: Dict[str, Any]) -> None:
    sf_obj = d.get("sf")
    if isinstance(sf_obj, dict):
        for kk, vv in sf_obj.items():
            fk = f"sf.{kk}"
            if fk not in d or d.get(fk) in (None, "", []):
                d[fk] = vv

def collapse_rows_by_account(raw_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Colapsa filas por account_id.
    - opportunity_types: "Profiling, Qualification" si hay ambas; "Profiling" o "Qualification" si solo una.
    - opportunity_type (compat): "both" | "profiling" | "qualification" | "unknown"
    - data: mergea primero SF y luego QUAL (no pisa sf.* con sf.* más recientes salvo que elijas otra política).
    """
    buckets: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    names: Dict[str, str] = {}
    countries: Dict[str, str] = {}
    cities: Dict[str, str] = {}
    types_by_acc: Dict[str, Set[str]] = {}

    def _norm(t: Optional[str]) -> Optional[str]:
        t = (t or "").strip().lower()
        if "rofil" in t: return "profiling"
        if "qual"  in t: return "qualification"
        return None

    for r in raw_rows:
        acc_id, acc_name = _account_key_and_name(r)
        if not acc_id:
            acc_id = f"name::{acc_name}" if acc_name else "unknown"
        names.setdefault(acc_id, acc_name or "")
        if r.get("country"): countries[acc_id] = r["country"]
        if r.get("city"):    cities[acc_id]    = r["city"]

        # Clasifica por kind y acumula tipos
        kind = _classify_kind_from_row(r)
        buckets.setdefault(acc_id, {}).setdefault(kind, []).append(r)
        tnorm = _norm(r.get("opportunity_type"))
        if tnorm:
            types_by_acc.setdefault(acc_id, set()).add(tnorm)

    def _choose_best(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return _pick_best(rows)

    out: List[Dict[str, Any]] = []
    for acc_id, kinds in buckets.items():
        best_sf = _choose_best(kinds.get("profiling", []))
        best_q  = _choose_best(kinds.get("qualification", []))
        base    = best_sf or best_q or next(iter(kinds.get("unknown", [])), None)
        if not base:
            continue

        # Merge de data:
        # 1) Copia todo lo de best_sf (incluye sf.*)
        merged_data: Dict[str, Any] = {}
        if best_sf:
            merged_data.update(best_sf.get("data", {}) or {})

        # 2) De best_q: siempre trae qual.* (y cualquier no-sf.*)
        if best_q:
            bq = (best_q.get("data", {}) or {})
            for k, v in bq.items():
                if str(k).startswith("qual.") or not str(k).startswith("sf."):
                    merged_data[k] = v

            # 3) NUEVO: Completar sf.* faltantes desde best_q SIN pisar valores ya presentes
            for k, v in bq.items():
                if str(k).startswith("sf."):
                    cur = merged_data.get(k, None)
                    # considera vacío: None, "", [], pero NO el 0
                    is_empty = (cur is None) or (isinstance(cur, str) and cur.strip() == "") or cur == []
                    if is_empty:
                        merged_data[k] = v

        # --- COMPLETAR GENERICAMENTE CAMPOS VACÍOS DESDE CUALQUIER FILA DE LA CUENTA ---
        def _is_empty(v):
            # 0 debe contarse como valor; None/""/[] son vacíos
            return (v is None) or (isinstance(v, str) and v.strip() == "") or v == []

        # Todas las filas de la cuenta (profiling + qualification + unknown)
        all_rows_for_acc = (
            kinds.get("profiling", []) + kinds.get("qualification", []) + kinds.get("unknown", [])
        )

        # Ordena filas por “calidad”: Closed Won primero y más recientes delante
        def _row_score(r: Dict[str, Any]):
            d = (r.get("data") or {})
            return (
                1 if _is_closed_won_row(r) else 0,
                _parse_dt(d.get("sf.CloseDate") or d.get("CloseDate")),
                _parse_dt(d.get("sf.CreatedDate") or d.get("CreatedDate")),
                _parse_dt(d.get("sf.LastModifiedDate") or d.get("LastModifiedDate")),
            )
        rows_ordered = sorted(all_rows_for_acc, key=_row_score, reverse=True)

        # Unión de todas las claves presentes en cualquier fila de la cuenta
        keys_union: Set[str] = set()
        for rr in all_rows_for_acc:
            keys_union.update((rr.get("data") or {}).keys())

        # Para cada fila ordenada por calidad, rellena cualquier key aún vacía
        for rr in rows_ordered:
            rd = (rr.get("data") or {})
            
            # si llega un objeto 'sf', aplanarlo primero
            if isinstance(rd.get("sf"), dict):
                for kk, vv in rd["sf"].items():
                    fk = f"sf.{kk}"
                    cur = merged_data.get(fk)
                    if cur in (None, "") or cur == []:
                        merged_data[fk] = vv

            # luego el merge genérico existente
            for k, v in rd.items():
                if k == "sf" and isinstance(v, dict):
                    continue
                cur = merged_data.get(k)
                if (cur is None) or (isinstance(cur, str) and cur.strip() == "") or cur == []:
                    if v not in (None, "") and v != []:
                        merged_data[k] = v

        # Tipos concatenados
        typs = sorted(types_by_acc.get(acc_id, set()))
        if typs == {"profiling", "qualification"}:
            opp_type = "both"
        elif "qualification" in typs and "profiling" not in typs:
            opp_type = "qualification"
        elif "profiling" in typs and "qualification" not in typs:
            opp_type = "profiling"
        else:
            opp_type = "unknown"

        # Etiqueta concatenada legible
        label_map = {"profiling": "Profiling", "qualification": "Qualification"}
        opportunity_types = ", ".join(label_map[t] for t in typs) if typs else ""

        _flatten_sf_inplace(merged_data)

        out.append({
            "account_id": acc_id,
            "account_name": names.get(acc_id, "") or base.get("account_name") or "",
            "country": countries.get(acc_id) or base.get("country") or "",
            "city":    cities.get(acc_id)    or base.get("city")    or "",
            "opportunity_type": opp_type,           # compat
            "opportunity_types": opportunity_types, # NUEVO: "Profiling, Qualification"
            "data": merged_data,
        })
    return out

# ======================= Account extra fields =======================


ACCOUNT_EXTRA_FIELDS: Dict[str, Dict[str, Any]] = {
    # CS contribution (booleans)
    "INNODIA_Clinical_Trial_Site__c": {"type": "boolean", "label": "CS: Clinical Trial Site"},
    "Clinical_Site_CS__c": {"type": "boolean", "label": "CS: Clinical Site"},
    "Referral_Outreach_Site_Non_CTS__c": {"type": "boolean", "label": "CS: Referral/Outreach (non-CTS)"},
    "Elegible_for_DETECT_Site__c": {"type": "boolean", "label": "CS: Eligible for DETECT"},
    # Otros
    "Subaccount_Inactive__c": {"type": "boolean", "label": "Subaccount Inactive"},
    "Key_Identifier__c": {"type": "string", "label": "Key Identifier"},
    "CTU_Status__c": {"type": "string", "label": "CTU Status"},
    "Accredited__c": {"type": "boolean", "label": "Accredited"},
    "Accredited_Date__c": {"type": "date", "label": "Accredited Date"},
    "ShippingAddress": {"type": "string", "label": "Shipping Address"},  # compuesto -> string
    "Description": {"type": "string", "label": "Description"},
    "C_Contribution_to_INNODIA__c": {"type": "string", "label": "Contribution to INNODIA"},  # multipicklist -> "A;B;C"
    # Nota: MemberName y HasPI se calculan aparte
}
ACCOUNT_EXTRA_KEYS: Set[str] = set(ACCOUNT_EXTRA_FIELDS.keys())

def _format_shipping_address(row: Dict[str, Any]) -> str:
    parts = [
        row.get("ShippingStreet"),
        row.get("ShippingCity"),
        row.get("ShippingState"),
        row.get("ShippingPostalCode"),
        row.get("ShippingCountry"),
    ]
    return ", ".join([str(p) for p in parts if p])

def _fetch_account_extras(sf, acc_ids: List[str], fields_needed: Set[str]) -> Dict[str, Dict[str, Any]]:
    """
    Devuelve: { AccountId: {field: value, ...} } solo para los 'fields_needed' de Account.
    Maneja ShippingAddress como campos atómicos y devuelve un string formateado.
    """
    if not acc_ids or not fields_needed:
        return {}

    # Expandir ShippingAddress a componentes SOQL
    soql_fields: Set[str] = set()
    for f in fields_needed:
        if f == "ShippingAddress":
            soql_fields.update({"ShippingStreet", "ShippingCity", "ShippingState", "ShippingPostalCode", "ShippingCountry"})
        else:
            soql_fields.add(f)

    # Seguridad: que existan en Account (si describe fallara, intentamos igualmente)
    try:
        _ = sf.Account.describe()
    except Exception:
        pass

    out: Dict[str, Dict[str, Any]] = {}
    def chunk(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i+n]

    for group in chunk(acc_ids, 150):
        vals = ", ".join(f"'{x}'" for x in group)
        select_fields = ", ".join(sorted(soql_fields | {"Id"}))
        rows = _sf_query_all(sf, f"SELECT {select_fields} FROM Account WHERE Id IN ({vals})")
        for r in rows:
            aid = r.get("Id")
            if not aid:
                continue
            d: Dict[str, Any] = {}
            for f in fields_needed:
                if f == "ShippingAddress":
                    d[f] = _format_shipping_address(r)
                else:
                    v = r.get(f)
                    # Multi-picklist en SF suele venir como "A;B;C"
                    if f == "C_Contribution_to_INNODIA__c" and isinstance(v, str):
                        d[f] = v  # dejamos string; filters con contains/in funcionarán
                    else:
                        d[f] = v
            out[str(aid)] = d
    return out

# ======================= ENDPOINTS =======================

@salesforce_router.get("/map/bootstrap")
async def map_bootstrap(request: Request, db: Session = Depends(get_db)):
    # Normalizador rápido de país → ISO2 (mejor reemplazar por util/tabla formal)
    def _country_norm(c):
        if not c: 
            return c
        s = str(c).strip()
        M = {
            "spain": "ES", "españa": "ES", "es": "ES",
            "united states": "US", "usa": "US", "us": "US",
            "belgium": "BE", "belgië": "BE", "belgie": "BE", "be": "BE",
            "netherlands": "NL", "nederland": "NL", "nl": "NL",
            "germany": "DE", "deutschland": "DE", "de": "DE",
            "france": "FR", "fr": "FR", "italy": "IT", "it": "IT",
        }
        key = s.lower()
        return M.get(key, s.upper() if len(s) == 2 else s)

    try:
        sf = _get_sf(request)
        opps = _sf_query_all(sf, f"""
            SELECT Id, Name, Type, StageName, IsClosed, CloseDate, AccountId
            FROM Opportunity
            WHERE Type IN ({TYPE_IN}) AND AccountId != null
        """)
        if not opps:
            return []

        acc_ids = sorted({o.get("AccountId") for o in opps if o.get("AccountId")})
        acc_map = _build_account_map(sf, acc_ids)

        site_rows = db.execute(
            select(Site.salesforce_account_id, Site.name, Site.city, Site.country, Site.latitude, Site.longitude)
            .where(Site.salesforce_account_id.isnot(None))
        ).all()
        site_by_acc: Dict[str, Dict[str, Any]] = {}
        for sacc, sname, scity, scountry, slat, slng in site_rows:
            site_by_acc[str(sacc)] = {
                "site_name": sname, "city": scity, "country": scountry, "lat": slat, "lng": slng
            }

        badges: Dict[str, Dict[str, bool]] = {}
        for o in opps:
            aid = o.get("AccountId");  t = _norm_type(o.get("Type"))
            if not aid: 
                continue
            # ⛔ Si la cuenta fue filtrada por inactividad, no la mostramos
            if aid not in acc_map:
                continue
            b = badges.setdefault(aid, {"profiling": False, "qualification": False})
            if t == "profiling": b["profiling"] = True
            elif t == "qualification": b["qualification"] = True

        out = []
        for aid, acc in acc_map.items():
            # 1) Coordenadas directas de SF
            lat, lng = acc.get("lat"), acc.get("lng")

            # 2) Si faltan, geocodifica SOLO con city/country de SF (normalizados)
            if lat is None or lng is None:
                glat, glng = await _geocode_city_country(acc.get("city"), acc.get("country"))
                if glat is not None and glng is not None:
                    lat, lng = glat, glng

            # 3) Si aún faltan, prueba coordenadas guardadas en DB de Sites
            if (lat is None or lng is None) and aid in site_by_acc:
                lat = lat or site_by_acc[aid].get("lat")
                lng = lng or site_by_acc[aid].get("lng")

            # 4) Último recurso: geocodifica con city/country de la DB
            if (lat is None or lng is None) and aid in site_by_acc:
                sc = site_by_acc[aid]
                glat, glng = await _geocode_city_country(sc.get("city"), sc.get("country"))
                if glat is not None and glng is not None:
                    lat, lng = glat, glng

            # 2) City/Country para geocoding: PRIORIDAD Salesforce (Account)
            acc_city     = acc.get("city")
            acc_country  = _country_norm(acc.get("country"))
            site_city    = site_by_acc.get(aid, {}).get("city")
            site_country = _country_norm(site_by_acc.get(aid, {}).get("country"))
            city0        = acc_city or site_city
            country0     = acc_country or site_country

            # 3) Si faltan coords → geocode. Primero con SF; solo si falta, con Site.
            if (lat is None or lng is None):
                g_city, g_country = acc_city, acc_country
                # Guardarraíl para Sevilla: si city es Sevilla/Seville y SF no trae país o no es ES, prueba ES primero
                if g_city and str(g_city).lower() in ("sevilla", "seville") and g_country not in ("ES",):
                    try:
                        glat, glng = await _geocode_city_country(g_city, "ES")
                        if glat is not None and glng is not None:
                            lat, lng = glat, glng
                    except Exception:
                        pass
                if lat is None or lng is None:
                    glat, glng = await _geocode_city_country(g_city or site_city, g_country)
                    lat, lng = glat, glng
                # Si aún nada, intenta con Site explícitamente
                if (lat is None or lng is None) and (site_city or site_country):
                    glat, glng = await _geocode_city_country(site_city, site_country)
                    lat, lng = glat, glng
            if lat is None or lng is None:
                continue

            # 4) Datos mostrados: también prioriza SF (consistencia)
            city_out    = acc_city or site_city
            country_out = acc_country or site_country

            out.append({
                "account_id": aid,
                "site":       acc.get("name"),
                "city":       city_out,
                "country":    country_out,
                "latitude":   lat,
                "longitude":  lng,
                "hasQualification": badges.get(aid, {}).get("qualification", False),
                "hasProfiling":     badges.get(aid, {}).get("profiling", False),
            })
        return out
    except HTTPException as he:
        if he.status_code in (401, 403): raise
        log.exception("map_bootstrap failed (HTTP %s): %s", he.status_code, he.detail)
        return []
    except Exception as e:
        log.exception("map_bootstrap failed: %s", e)
        return []

@explorer_router.get("/fields")
def explorer_fields(db: Session = Depends(get_db)):
    # --- site.* ---
    site = [
        {"key": "site.country", "label": "Country", "type": "string", "source": "site", "group": "Site"},
        {"key": "site.city",    "label": "City",    "type": "string", "source": "site", "group": "Site"},
    ]

    # --- sf.* (ya curado) ---
    sf_fields = FIELD_CONFIG  # mantiene tus grupos y metadatos actuales

    # --- qual.* de JSONB + grupos desde QA ---
    qual_fields = _infer_qual_fields(db)
    groups = _qual_groups_from_questions(db)
    # --- en explorer_fields(), justo donde asignas el group a cada qual.* ---
    for f in qual_fields:
        k = f.get("key") or ""
        if not k.startswith("qual."):
            continue
        # si ya traemos group desde _infer_qual_fields, respétalo
        if "group" not in f or not f["group"]:
            slug = k[5:]
            base = slug.split("__", 1)[0]
            g = groups.get(base) or groups.get(slug)
            if g:
                f["group"] = g
        f.setdefault("source", "qual")

    # ➜ Aplica overrides/ocultaciones solo al catálogo mostrado
    qual_fields = _apply_qual_overrides(qual_fields)


    # ➜ Nueva pasada: si hay etiquetas repetidas (p.ej. varios "Comments"),
    #    desambiguar añadiendo el código de subsección en la etiqueta.
    #    (No cambia las keys, solo el label visible)
    if qual_fields:
        qual_fields = _disambiguate_duplicate_labels(qual_fields)


    # --- Account.* extras visibles en UI (los que ya tenías) ---
    account_fields_cfg: List[Dict[str, Any]] = []
    for name, meta in ACCOUNT_EXTRA_FIELDS.items():
        account_fields_cfg.append({
            "key":   f"Account.{name}",
            "label": meta.get("label") or name.replace("_", " "),
            "type":  meta.get("type") or "string",
            "source":"account",
            "group": "Account",
        })
    # Conveniencia: MemberName y HasPI (Account.*)
    account_fields_cfg += [
        {"key": "Account.MemberName", "label": "Member (lookup name)", "type": "string",  "source": "account", "group": "Account"},
        {"key": "Account.HasPI",      "label": "Has PI",               "type": "boolean", "source": "account", "group": "Account"},
    ]

    # --- NUEVO: Catálogo extra.* (Salesforce extras batch) ---
    # Estos campos los rellena batch_fetch_account_extras() y se pueden filtrar/mostrar.
    extras_fields = [
        {"key": "extra.MemberName",                      "label": "Member (Account)",                 "type": "string",  "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.PIName",                          "label": "PI Name",                          "type": "string",  "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.PIEmail",                         "label": "PI Email",                         "type": "string",  "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.PIPhone",                         "label": "PI Phone",                         "type": "string",  "source": "extra", "group": "Salesforce Extras"},

        # Flags de contribución a INNODIA (desde Account)
        {"key": "extra.Clinical_Site_CS__c",             "label": "INNODIA Clinical Site",            "type": "boolean", "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.INNODIA_Clinical_Trial_Site__c",  "label": "INNODIA Clinical Trial Site",      "type": "boolean", "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.Referral_Outreach_Site_Non_CTS__c",    "label": "Referral & Outreach Site (Non-CTS)","type": "boolean","source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.Elegible_for_DETECT_Site__c",           "label": "Eligible for DETECT Site",         "type": "boolean", "source": "extra", "group": "Salesforce Extras"},

        # Métrica útil
        {"key": "extra.AssignmentsCount",                "label": "Assignments (count)",              "type": "number",  "source": "extra", "group": "Salesforce Extras"},
    ]

    # Unificar y deduplicar manteniendo el primero que aparezca
    seen, out = set(), []
    for f in site + sf_fields + account_fields_cfg + qual_fields + extras_fields:
        k = f.get("key")
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(f)

    log.info(
        "explorer_fields: qual=%s account_extras=%s extra=%s",
        len(qual_fields), len(account_fields_cfg), len(extras_fields)
    )
    return {"fields": out}

@explorer_router.post("/search")
async def explorer_search(
    payload: Dict = Body(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    sf = _get_sf(request)
    _ensure_describes(sf)

    filters = payload.get("filters") or {"logic": "AND", "rules": []}
    columns = payload.get("columns") or []

    debug = bool(payload.get("debug"))

    def _op_norm(op: str) -> str:
        return _OP_SYNONYM.get(op, op)

    def _str(v: Any) -> str:
        return "" if v is None else str(v)

    # -------- Clasificación de reglas --------
    site_rules: List[Rule] = []
    sf_rules: List[Rule] = []
    qual_rules: List[Rule] = []
    account_rules: List[Rule] = []
    member_rules: List[Rule] = []
    haspi_rules: List[Rule] = []
    extra_rules: List[Rule] = []

    need_member = False
    need_haspi = False
    need_account_extras = False
    need_batch_extras = False

    def _norm_label_to_key_safe(k: str) -> str:
        try:
            return _norm_label_to_key(k)
        except Exception:
            return k

    for r in (filters.get("rules") or []):
        f = _norm_label_to_key_safe(r.get("field") or "")
        op = r.get("op") or r.get("operator") or "equals"
        val = r.get("value")

        if f in ("sf.Account.MemberName", "Account.MemberName"):
            member_rules.append(Rule(field="sf.Account.MemberName", operator=op, value=val))
            need_member = True
            continue
        if f in ("sf.Account.HasPI", "Account.HasPI"):
            haspi_rules.append(Rule(field="sf.Account.HasPI", operator=op, value=val))
            need_haspi = True
            continue

        if f.startswith("extra."):
            extra_rules.append(Rule(field=f, operator=op, value=val))
            need_batch_extras = True
        elif f.startswith("site."):
            site_rules.append(Rule(field=f, operator=op, value=val))
        elif f.startswith("sf."):
            nf = f[3:]
            if nf.startswith("Account."):
                account_rules.append(Rule(field=nf[8:], operator=op, value=val))
                need_account_extras = True
            else:
                sf_rules.append(Rule(field=nf, operator=op, value=val))
        elif f.startswith("Account."):
            account_rules.append(Rule(field=f[8:], operator=op, value=val))
            need_account_extras = True
        elif f.startswith("qual."):
            qual_rules.append(Rule(field=f[5:], operator=op, value=val))
        else:
            if f in ALLOWED_FIELDS and not f.startswith("Account."):
                sf_rules.append(Rule(field=f, operator=op, value=val))

    sf_filter = FilterQuery(logic=filters.get("logic", "AND"), rules=sf_rules)

    # -------- Columnas solicitadas --------
    requested_cols: List[str] = []
    requested_account_cols: Set[str] = set()
    requested_extra_cols: Set[str] = set()

    for c in (columns or []):
        k = _norm_label_to_key_safe(c)
        requested_cols.append(k)
        if k in ("sf.Account.Member__c", "Account.Member__c", "sf.Account.MemberName", "Account.MemberName"):
            need_member = True
        if k in ("sf.Account.HasPI", "Account.HasPI"):
            need_haspi = True
        if k.startswith("Account."):
            requested_account_cols.add(k[8:])
            need_account_extras = True
        if k.startswith("extra."):
            requested_extra_cols.add(k)
            need_batch_extras = True

    # ===========================================================
    # 1) OPP “normales” (Qualification / Profiling)
    # ===========================================================
    base_where  = f"Type IN ({TYPE_IN}) AND AccountId != null"
    extra_where = _build_sf_where(sf_filter)
    if extra_where and not extra_where.strip().startswith("("):
        extra_where = f"({extra_where})"
    where_sql   = f"WHERE {base_where}" + (f" AND {extra_where}" if extra_where else "")

    opp_fields: Set[str] = {
        "Id","Name","Type","StageName","IsClosed","CloseDate","AccountId",
        # añadimos recordtype para permitir filtros/columnas cuando se trabaje con Activities
        "RecordType.Name","RecordType.DeveloperName"
    }
    acc_fields: Set[str] = set()

    for r in sf_rules:
        if not r.field.startswith("Account.") and _exists_on_opportunity(r.field):
            opp_fields.add(r.field)

    for k in requested_cols:
        if not k.startswith("sf."):
            continue
        fld = k[3:]
        if _exists_on_opportunity(fld):
            opp_fields.add(fld)
        elif _exists_on_account(fld):
            acc_fields.add(fld)

    if acc_fields:
        acc_fields.update({"Id","Name"})

    # permite campos de relación que tu helper no “conoce”
    _ALLOW_REL = {"RecordType.Name", "RecordType.DeveloperName"}
    opp_fields_valid = {f for f in opp_fields if _exists_on_opportunity(f) or f in _ALLOW_REL}
    acc_fields_valid = {f for f in acc_fields if _exists_on_account(f)}
    select_parts = sorted(opp_fields_valid) + [f"Account.{f}" for f in sorted(acc_fields_valid)]
    if not select_parts:
        raise HTTPException(400, "No hay campos válidos para seleccionar en Opportunity/Account")

    select_fields = ", ".join(select_parts)
    soql_opps = f"SELECT {select_fields} FROM Opportunity {where_sql}"
    if debug: print("[/search][SOQL opps]", soql_opps)
    opps = _sf_query_all(sf, soql_opps) or []

    # ===========================================================
    # 2) Activities + Assignments -> subcuentas
    # ===========================================================
    # En tu org el RecordType se llama RT_Activity; acepta ambos por si acaso
    ACT_RT = ("Activity", "RT_Activity")
    in_clause = ", ".join("'" + x.replace("'", "\\'") + "'" for x in ACT_RT)

    act_where = (
        "WHERE AccountId != null "
        "AND RecordType.DeveloperName IN (" + in_clause + ")"
    )
    if extra_where:
        act_where += f" AND {extra_where}"

    soql_acts = f"SELECT Id, Name, AccountId, RecordType.DeveloperName FROM Opportunity {act_where}"
    if debug: print("[/search][SOQL acts]", soql_acts)
    activities = _sf_query_all(sf, soql_acts) or []

    assignments_by_opp: Dict[str, List[Dict[str, Any]]] = {}
    if activities:
        def _chunks(xs, n=120):
            buf=[]
            for x in xs:
                if x: buf.append(x)
                if len(buf)>=n:
                    yield buf; buf=[]
            if buf: yield buf

        opp_ids = [a["Id"] for a in activities if a.get("Id")]
        for chunk in _chunks(opp_ids):
            ids_in = ", ".join(f"'{x}'" for x in chunk)
            soql = (
                "SELECT Id, Name, C_Account__c, "
                "C_Opportunity_Name__c, C_Opportunity_Name__r.Name, "
                "Assignment_Type__c, C_Assignment_Stage__c, CreatedDate "
                f"FROM Assignment__c WHERE C_Opportunity_Name__c IN ({ids_in}) "
                "ORDER BY CreatedDate DESC"
            )
            if debug: print("[/search][SOQL assigns]", soql)
            recs = _sf_query_all(sf, soql) or []
            for r in recs:
                oid = r.get("C_Opportunity_Name__c")
                aid = r.get("C_Account__c")
                if not oid or not aid:
                    continue
                assignments_by_opp.setdefault(oid, []).append({
                    "id": r.get("Id"),
                    "name": r.get("Name"),
                    "account_id": aid,
                    "opportunity_id": oid,
                    "opportunity_name": (r.get("C_Opportunity_Name__r") or {}).get("Name"),
                    "stage": r.get("C_Assignment_Stage__c"),
                    "atype": r.get("Assignment_Type__c"),
                    "created": r.get("CreatedDate"),
                })

    # ===========================================================
    # 3) Accounts implicadas (incluye subcuentas de assignments)
    # ===========================================================
    acc_ids = set(o.get("AccountId") for o in opps if o.get("AccountId"))
    for lst in assignments_by_opp.values():
        for a in lst:
            if a.get("account_id"):
                acc_ids.add(a["account_id"])
    acc_ids = sorted({x for x in acc_ids if x})
    if not acc_ids:
        # Nada que pintar; devuelve debug para inspección
        out = {"points": [], "rows": []}
        if debug:
            out["_debug"] = {
                "filters_received": filters,
                "soql_opps": soql_opps,
                "soql_acts": soql_acts,
                "opps_count": len(opps),
                "activities_count": len(activities),
            }
        return out

    acc_map = _build_account_map(sf, acc_ids)

    # ===========================================================
    # 4) Sites locales + Qualification JSON
    # ===========================================================
    site_rows = db.execute(
        select(Site.id, Site.name, Site.city, Site.country, Site.salesforce_account_id, Site.latitude, Site.longitude)
    ).all()
    site_by_acc: Dict[str, Dict[str, Any]] = {}
    site_by_id: Dict[int, Dict[str, Any]] = {}
    for sid, sname, scity, scountry, sacc, slat, slng in site_rows:
        if sacc:
            site_by_acc[str(sacc)] = {
                "site_id": sid, "site_name": sname, "city": scity, "country": scountry, "lat": slat, "lng": slng
            }
        site_by_id[int(sid)] = {"lat": slat, "lng": slng, "city": scity, "country": scountry}

    qual_rows = db.execute(select(SiteQual.site_id, SiteQual.data)).all()
    groups_by_slug = _qual_groups_from_questions(db)
    qual_by_site: Dict[int, Dict[str, Any]] = {
        sid: (
            lambda _d: _expand_describe_keys_for_row(
                _expand_comments_keys_for_row(_d, groups_by_slug),
                groups_by_slug
            )
        )((data or {}))
        for sid, data in qual_rows
    }

    # ===========================================================
    # 5) Extras de Account / Member / HasPI
    # ===========================================================
    member_by_acc: Dict[str, Optional[str]] = {}
    if need_member and acc_ids:
        acc_ids_str = ",".join([f"'{x}'" for x in acc_ids])
        soql = "SELECT Id, C_Member__c, C_Member__r.Name FROM Account WHERE Id IN ({})".format(acc_ids_str)
        accs_extra = _sf_query_all(sf, soql)
        for a in accs_extra:
            aid = a.get("Id")
            mname = (a.get("C_Member__r") or {}).get("Name")
            member_by_acc[str(aid)] = mname or None

    pi_accounts: Set[str] = set()
    if need_haspi and acc_ids:
        acc_ids_str = ",".join([f"'{x}'" for x in acc_ids])
        soql = (
            "SELECT AccountId FROM AccountContactRelation "
            f"WHERE AccountId IN ({acc_ids_str}) AND Role__c = 'PI'"
        )
        acrs = _sf_query_all(sf, soql)
        for r in acrs:
            if r.get("AccountId"):
                pi_accounts.add(str(r.get("AccountId")))

    account_fields_needed: Set[str] = {r.field for r in account_rules}
    account_fields_needed |= requested_account_cols
    supported = set(ACCOUNT_EXTRA_FIELDS.keys()) | {"MemberName", "HasPI", "ShippingAddress"}
    fields_to_fetch = {f for f in account_fields_needed if f in supported and f not in {"MemberName", "HasPI"}}

    account_extras_by_acc: Dict[str, Dict[str, Any]] = {}
    if need_account_extras and acc_ids and fields_to_fetch:
        account_extras_by_acc = _fetch_account_extras(sf, acc_ids, fields_to_fetch)

    extras_map: Dict[str, Dict[str, Any]] = {}
    if need_batch_extras and acc_ids:
        extras_map = batch_fetch_account_extras(sf, list({x for x in acc_ids if x}))

    # ===========================================================
    # 6) Helpers de filtrado post-query
    # ===========================================================
    def pass_site(acc: Dict[str, Any]) -> bool:
        if not site_rules:
            return True
        def match_one(rule: Rule) -> bool:
            val = ""
            if rule.field == "site.city":    val = (acc.get("city") or "") or ""
            if rule.field == "site.country": val = (acc.get("country") or "") or ""
            op = _op_norm(rule.operator); s = _str(rule.value)
            if   op in ("equals","="):      return val == s
            elif op in ("not_equals","!="): return val != s
            elif op == "contains":          return s.lower() in val.lower()
            elif op == "not_contains":      return s.lower() not in val.lower()
            elif op == "starts_with":       return val.lower().startswith(s.lower())
            elif op == "ends_with":         return val.lower().endswith(s.lower())
            return True
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = [match_one(r) for r in site_rules]
        return all(res) if glue_and else any(res)

    def pass_qual(qual_data: Dict[str, Any]) -> bool:
        if not qual_rules:
            return True
        results = []
        for qr in qual_rules:
            v = qual_data.get(qr.field)
            results.append(_eval_qual_rule(v, qr.operator, qr.value))
        glue_and = (filters.get("logic") or "AND") == "AND"
        return all(results) if glue_and else any(results)

    def pass_member(aid: str) -> bool:
        if not member_rules:
            return True
        def eval_rule_text(actual: str, rule: Rule) -> bool:
            op = _op_norm(rule.operator); s = _str(rule.value)
            if   op in ("equals","="):      return (actual or "") == s
            elif op in ("not_equals","!="): return (actual or "") != s
            elif op == "contains":          return s.lower() in (actual or "").lower()
            elif op == "not_contains":      return s.lower() not in (actual or "").lower()
            elif op == "starts_with":       return (actual or "").lower().startswith(s.lower())
            elif op == "ends_with":         return (actual or "").lower().endswith(s.lower())
            return True
        actual = member_by_acc.get(str(aid)) or ""
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = [eval_rule_text(actual, r) for r in member_rules]
        return all(res) if glue_and else any(res)

    def pass_haspi(aid: str) -> bool:
        if not haspi_rules:
            return True
        def as_bool(x: Any) -> bool:
            if isinstance(x, bool): return x
            s = _str(x).strip().lower()
            return s in ("1","true","yes","y","t")
        actual = str(aid) in pi_accounts
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = []
        for r in haspi_rules:
            op = _op_norm(r.operator); want = as_bool(r.value)
            if   op in ("equals","="):      res.append(actual == want)
            elif op in ("not_equals","!="): res.append(actual != want)
            else:                            res.append(True)
        return all(res) if glue_and else any(res)

    def pass_extra(aid: str) -> bool:
        if not extra_rules:
            return True
        vals = extras_map.get(str(aid), {}) if extras_map else {}
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = []
        for er in extra_rules:
            actual = vals.get(er.field)
            res.append(_eval_qual_rule(actual, er.operator, er.value))
        return all(res) if glue_and else any(res)

    def pass_account(aid: str) -> bool:
        if not account_rules:
            return True
        vals = dict(account_extras_by_acc.get(str(aid), {}))
        if need_member: vals["MemberName"] = member_by_acc.get(str(aid))
        if need_haspi:  vals["HasPI"]      = (str(aid) in pi_accounts)
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = []
        for ar in account_rules:
            actual = vals.get(ar.field)
            res.append(_eval_qual_rule(actual, ar.operator, ar.value))
        return all(res) if glue_and else any(res)

    # ===========================================================
    # 7) Construcción de filas de OPP normales
    # ===========================================================
    rows = []
    for o in opps:
        aid = o.get("AccountId")
        if not aid:             continue
        if aid not in acc_map:  continue  # filtradas por inactividad
        acc = acc_map.get(aid) or {}
        if not pass_site(acc):  continue

        site_info = site_by_acc.get(str(aid))
        site_id   = site_info["site_id"] if site_info else None
        qual_data = qual_by_site.get(site_id, {}) if site_id else {}

        if not pass_qual(qual_data): continue
        if not pass_member(aid):     continue
        if not pass_haspi(aid):      continue
        if not pass_account(aid):    continue
        if not pass_extra(aid):      continue

        data: Dict[str, Any] = {}
        for k in requested_cols:
            # site.*
            if k == "site.city":        data[k] = acc.get("city");    continue
            if k == "site.country":     data[k] = acc.get("country"); continue

            # Account.*
            if k.startswith("Account."):
                sub = k[8:]
                if   sub == "Id":                 data[k] = aid
                elif sub == "Name":               data[k] = acc.get("name")
                elif sub == "ShippingCity":       data[k] = acc.get("city")
                elif sub == "ShippingCountry":    data[k] = acc.get("country")
                elif sub == "ShippingLatitude":   data[k] = acc.get("lat")
                elif sub == "ShippingLongitude":  data[k] = acc.get("lng")
                elif sub in {"BillingCity","BillingCountry","BillingLatitude","BillingLongitude"}:
                    if   sub == "BillingCity":      data[k] = acc.get("city")
                    elif sub == "BillingCountry":   data[k] = acc.get("country")
                    elif sub == "BillingLatitude":  data[k] = acc.get("lat")
                    elif sub == "BillingLongitude": data[k] = acc.get("lng")
                elif sub in {"Member__c"}:         data[k] = None
                elif sub in {"MemberName"}:        data[k] = member_by_acc.get(str(aid)) if need_member else None
                elif sub in {"HasPI"}:             data[k] = (str(aid) in pi_accounts)   if need_haspi  else None
                else:
                    data[k] = (account_extras_by_acc.get(str(aid), {}) or {}).get(sub)
                continue

            # qual.*
            if k.startswith("qual."):   data[k] = qual_data.get(k[5:]); continue

            # sf.*
            if k.startswith("sf."):
                fld = k[3:]
                if _exists_on_opportunity(fld):
                    data[k] = o.get(fld)
                elif _exists_on_account(fld):
                    data[k] = (o.get("Account") or {}).get(fld)
                else:
                    data[k] = None
                continue

            # extra.*
            if k.startswith("extra."):
                data[k] = (extras_map.get(str(aid), {}) or {}).get(k); continue

            data[k] = o.get(k)

        _flatten_sf_inplace(data)
        rows.append({
            "account_id": aid,
            "account_name": acc.get("name"),
            "country": acc.get("country"),
            "city": acc.get("city"),
            "opportunity_type": _norm_type(o.get("Type")),
            "data": data,
        })

    # ===========================================================
    # 8) Oportunidad “proxy” por Account para filas de Assignment
    # ===========================================================
    requested_sf_fields: Set[str] = {
        k[3:] for k in requested_cols if k.startswith("sf.") and _exists_on_opportunity(k[3:])
    }
    sf_proxy_by_acc: Dict[str, Dict[str, Any]] = {}
    if requested_sf_fields and assignments_by_opp:
        proxy_acc_ids = sorted({
            a.get("account_id")
            for lst in assignments_by_opp.values() for a in lst
            if a.get("account_id")
        })
        if proxy_acc_ids:
            def _chunk(xs, n=120):
                buf=[]; 
                for x in xs:
                    if x: buf.append(x)
                    if len(buf)>=n: yield buf; buf=[]
                if buf: yield buf
            proxy_fields = {"Id","Name","AccountId","Type","RecordType.DeveloperName"} | requested_sf_fields
            fields_sql = ", ".join(sorted(proxy_fields))
            for ch in _chunk(proxy_acc_ids):
                ids_in = ", ".join(f"'{x}'" for x in ch)
                soql = (
                    f"SELECT {fields_sql} FROM Opportunity "
                    f"WHERE AccountId IN ({ids_in}) "
                    # preferimos Profiling/Qualification recientes; si no hay, cogemos la más reciente no-Activity
                    f"AND (Type IN ({TYPE_IN}) OR RecordType.DeveloperName != 'Activity') "
                    "ORDER BY AccountId, LastModifiedDate DESC"
                )
                recs = _sf_query_all(sf, soql) or []
                for r in recs:
                    aid = str(r.get("AccountId") or "")
                    if aid and aid not in sf_proxy_by_acc:
                        sf_proxy_by_acc[aid] = r  # primera = más reciente por el ORDER BY

    # ===========================================================
    # 9) Filas sintéticas por subcuenta de Assignments (Activities)
    # ===========================================================
    if assignments_by_opp:
        present = {r["account_id"] for r in rows if r.get("account_id")}
        for oid, assigns in assignments_by_opp.items():
            for a in assigns:
                aid = a.get("account_id")
                if not aid or aid in present:
                    continue
                if aid not in acc_map:
                    continue  # filtradas por inactividad
                acc = acc_map.get(aid) or {}
                if not pass_site(acc):  continue

                site_info = site_by_acc.get(str(aid))
                site_id   = site_info["site_id"] if site_info else None
                qual_data = qual_by_site.get(site_id, {}) if site_id else {}

                if not pass_qual(qual_data): continue
                if not pass_member(aid):     continue
                if not pass_haspi(aid):      continue
                if not pass_account(aid):    continue
                if not pass_extra(aid):      continue

                proxy = sf_proxy_by_acc.get(str(aid), {})
                data: Dict[str, Any] = {}
                for k in requested_cols:
                    if k == "site.city":        data[k] = acc.get("city");    continue
                    if k == "site.country":     data[k] = acc.get("country"); continue
                    if k.startswith("Account."):
                        sub = k[8:]
                        if   sub == "Id":                 data[k] = aid
                        elif sub == "Name":               data[k] = acc.get("name")
                        elif sub == "ShippingCity":       data[k] = acc.get("city")
                        elif sub == "ShippingCountry":    data[k] = acc.get("country")
                        elif sub == "ShippingLatitude":   data[k] = acc.get("lat")
                        elif sub == "ShippingLongitude":  data[k] = acc.get("lng")
                        elif sub in {"BillingCity","BillingCountry","BillingLatitude","BillingLongitude"}:
                            if   sub == "BillingCity":      data[k] = acc.get("city")
                            elif sub == "BillingCountry":   data[k] = acc.get("country")
                            elif sub == "BillingLatitude":  data[k] = acc.get("lat")
                            elif sub == "BillingLongitude": data[k] = acc.get("lng")
                        elif sub in {"Member__c"}:         data[k] = None
                        elif sub in {"MemberName"}:        data[k] = member_by_acc.get(str(aid)) if need_member else None
                        elif sub in {"HasPI"}:             data[k] = (str(aid) in pi_accounts)   if need_haspi  else None
                        else:
                            data[k] = (account_extras_by_acc.get(str(aid), {}) or {}).get(sub)
                        continue
                    if k.startswith("qual."):   data[k] = qual_data.get(k[5:]); continue
                    if k.startswith("extra."):  data[k] = (extras_map.get(str(aid), {}) or {}).get(k); continue
                    if k.startswith("sf."):
                        fld = k[3:]
                        # Mostrar el nombre de la Activity cuando se pide [sf] Opportunity Name
                        if fld == "Name":
                            data[k] = a.get("opportunity_name")
                        else:
                            data[k] = proxy.get(fld) if proxy else None
                        continue
                    data[k] = None
                _flatten_sf_inplace(data)

                rows.append({
                    "account_id": aid,
                    "account_name": acc.get("name"),
                    "country": acc.get("country"),
                    "city": acc.get("city"),
                    "opportunity_type": "Activity (Assignment)",
                    "data": data,
                })
                present.add(aid)

    # ===========================================================
    # 10) Colapso por subcuenta y markers
    # ===========================================================
    rows = collapse_rows_by_account(rows)

    badges: Dict[str, Dict[str, bool]] = {}
    for r in rows:
        aid = r.get("account_id")
        if not aid:
            continue
        b = badges.setdefault(aid, {"profiling": False, "qualification": False})
        types_str = (r.get("opportunity_types") or "").lower()
        if "profiling" in types_str:     b["profiling"] = True
        if "qualification" in types_str: b["qualification"] = True

    acc_map_final = _build_account_map(sf, [r["account_id"] for r in rows if r.get("account_id")])
    points = []
    for r in rows:
        aid = r.get("account_id")
        accd = acc_map_final.get(aid) or {}
        # 1) Coordenadas de SF
        lat, lng = accd.get("lat"), accd.get("lng")
        # 2) Si faltan -> geocode con city/country de SF (normalizados dentro del helper)
        if lat is None or lng is None:
            glat, glng = await _geocode_city_country(accd.get("city"), accd.get("country"))
            if glat is not None and glng is not None:
                lat, lng = glat, glng
        # 3) Si aún faltan -> coordenadas de la DB (Site)
        if lat is None or lng is None:
            s_info = site_by_acc.get(str(aid)) or {}
            lat = lat if lat is not None else s_info.get("lat")
            lng = lng if lng is not None else s_info.get("lng")
        # 4) Último recurso -> geocode con city/country de DB
        if (lat is None or lng is None) and site_by_acc.get(str(aid)):
            s_info = site_by_acc.get(str(aid)) or {}
            glat, glng = await _geocode_city_country(s_info.get("city"), s_info.get("country"))
            if glat is not None and glng is not None:
                lat, lng = glat, glng
        if lat is None or lng is None:
            continue
        points.append({
            "lat": lat, "lng": lng,
            "account_id": aid,
            "account_name": accd.get("name") or r.get("account_name"),
            "city": accd.get("city") or r.get("city"),
            "country": accd.get("country") or r.get("country"),
            "badges": badges.get(aid, {"profiling": False, "qualification": False}),
        })

    resp = {"points": points, "rows": rows}
    if debug:
        # Muestras no sensibles para diagnóstico
        resp["_debug"] = {
            "filters_received": filters,
            "soql_opps": soql_opps,
            "soql_acts": soql_acts,
            "opps_count": len(opps),
            "activities_count": len(activities),
            "assignments_by_opp_counts": {k: len(v) for k, v in list(assignments_by_opp.items())[:5]},
            "first_opps": [{ "Id": o.get("Id"), "Name": o.get("Name"), "Type": o.get("Type")} for o in opps[:5]],
            "first_acts": [{ "Id": a.get("Id"), "Name": a.get("Name"), "RT": a.get("RecordType",{}).get("DeveloperName") if isinstance(a.get("RecordType"), dict) else a.get("RecordType.DeveloperName")} for a in activities[:5]],
        }
    return resp

# Alias legacy
@salesforce_router.post("/search/sites")
async def legacy_search_sites(payload: dict = Body(...), request: Request = None, db: Session = Depends(get_db)):
    return await explorer_search(payload, request, db)


# #+# ---------------------------------------------------------------------------
# #+# 🔧 Registro explícito de /api/explorer/columns/fill (por si un import parcial
# #+#     dejara sin registrar el decorador).
# #+# ---------------------------------------------------------------------------
# try:
#     # Si la función ya existe (como en tu código), simplemente re-exponla aquí.
#     # FastAPI ignora duplicados exactos; si no existiera, esto falla y no rompe nada.
#     explorer_router.add_api_route(
#         path="/columns/fill",
#         endpoint=explorer_fill_columns,
#         methods=["POST"],
#         name="explorer_fill_columns",
#         tags=["explorer"],
#     )
# except NameError:
#     # En caso de que el nombre no exista por un import parcial, no rompemos el módulo;
#     # así podrás ver el error en logs y corregir el import.
#     pass



# ---------- Búsqueda de cuentas por nombre (para el linker) ----------
class AccountSearchBody(BaseModel):
    query: str


@explorer_router.post("/columns/fill")
async def explorer_fill_columns(
    payload: Dict = Body(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Devuelve valores para columnas solicitadas, sin filtros.
    Úsalo cuando el usuario añade columnas para rellenar la tabla
    con los accounts ya visibles.
    payload:
      {
        "account_ids": ["001...", "001..."],   # opcional; si falta, usa todos los accounts activos con Opp CTS
        "columns": ["sf.StageName","sf.CloseDate","Account.HasPI","qual.xxx","extra.MemberName"]
      }
    """
    sf = _get_sf(request)
    _ensure_describes(sf)

    columns: List[str] = payload.get("columns") or []
    account_ids: List[str] = [str(x) for x in (payload.get("account_ids") or [])]

    if not columns:
        return {"rows": []}
    # si no pasan accounts y nadie ha abierto la tabla todavía, no hagas nada costoso
    # if not account_ids:
    #     return {"rows": []}

    # Universo de cuentas objetivo
    #account_ids: List[str] = [str(x) for x in (payload.get("account_ids") or [])]
    if not account_ids:
        # Todos los accounts con CTS (y luego filtramos inactivos)
        recs = _sf_query_all(sf, f"""
            SELECT AccountId FROM Opportunity
            WHERE Type IN ({TYPE_IN}) AND AccountId != null
            GROUP BY AccountId
        """)
        account_ids = [str(r.get("AccountId")) for r in recs if r.get("AccountId")]

    # Filtra inactivas y saca city/country/coords
    acc_map = _build_account_map(sf, account_ids)
    active_acc_ids = sorted(acc_map.keys())
    if not active_acc_ids:
        return {"rows": []}

    # --- Clasificación de columnas y flags ---
    def norm_key(x: Any) -> str:
        if isinstance(x, dict):
            x = x.get("key") or x.get("value") or x.get("id") or ""
        s = str(x or "").strip()
        if s.startswith("[sf] "):   s = s[5:]
        if s.startswith("[site] "): s = s[8:]
        if s.startswith("[qual] "): s = s[7:]
        return s

    requested_cols: List[str] = [norm_key(c) for c in columns]
    need_member = any(k in ("sf.Account.MemberName","Account.MemberName","Account.Member__c","sf.Account.Member__c") for k in requested_cols)
    need_haspi  = any(k in ("sf.Account.HasPI","Account.HasPI") for k in requested_cols)
    need_account_extras = any(k.startswith("Account.") for k in requested_cols)
    need_batch_extras   = any(k.startswith("extra.")   for k in requested_cols)

    # sf.* seleccionables
    opp_fields: Set[str] = {"Id","Name","Type","StageName","IsClosed","CloseDate","AccountId"}
    acc_fields: Set[str] = set()
    for k in requested_cols:
        if not k.startswith("sf."):
            continue
        fld = k[3:]
        if _exists_on_opportunity(fld):
            opp_fields.add(fld)
        elif _exists_on_account(fld):
            acc_fields.add(fld)
    if acc_fields:
        acc_fields.update({"Id","Name"})  # mínimos de relación

    opp_fields_valid = {f for f in opp_fields if _exists_on_opportunity(f) or f.startswith("Account.")}
    select_parts = sorted(opp_fields_valid) + [f"Account.{f}" for f in sorted(acc_fields)]
    select_fields = ", ".join(select_parts)

    # WHERE: limitar a nuestras cuentas activas
    vals = ", ".join(f"'{x}'" for x in active_acc_ids)
    where_sql = f"WHERE Type IN ({TYPE_IN}) AND AccountId != null AND AccountId IN ({vals})"

    opps = _sf_query_all(sf, f"SELECT {select_fields} FROM Opportunity {where_sql}")

    # Mapa site -> qual
    site_rows = db.execute(
        select(Site.id, Site.salesforce_account_id)
        .where(Site.salesforce_account_id.isnot(None))
    ).all()
    site_id_by_acc: Dict[str, int] = {str(sacc): int(sid) for sid, sacc in site_rows}
    qual_rows = db.execute(select(SiteQual.site_id, SiteQual.data)).all()
    groups_by_slug = _qual_groups_from_questions(db)
    qual_by_site: Dict[int, Dict[str, Any]] = {
        sid: _expand_comments_keys_for_row(data or {}, groups_by_slug) for sid, data in qual_rows
    }

    # Member / HasPI
    member_by_acc: Dict[str, Optional[str]] = {}
    if need_member:
        vals2 = ",".join(f"'{x}'" for x in active_acc_ids)
        rows = _sf_query_all(sf, f"SELECT Id, C_Member__c, C_Member__r.Name FROM Account WHERE Id IN ({vals2})")
        for a in rows:
            member_by_acc[str(a.get("Id"))] = (a.get("C_Member__r") or {}).get("Name") or None

    pi_accounts: Set[str] = set()
    if need_haspi:
        vals2 = ",".join(f"'{x}'" for x in active_acc_ids)
        rows = _sf_query_all(sf, f"SELECT AccountId FROM AccountContactRelation WHERE AccountId IN ({vals2}) AND Role__c = 'PI'")
        for r in rows:
            if r.get("AccountId"):
                pi_accounts.add(str(r.get("AccountId")))

    # Extras de Account (Account.* configurados)
    account_fields_needed: Set[str] = set()
    for k in requested_cols:
        if k.startswith("Account."):
            account_fields_needed.add(k.split(".",1)[1])
    supported = set(ACCOUNT_EXTRA_FIELDS.keys()) | {"MemberName","HasPI","ShippingAddress"}
    fields_to_fetch = {f for f in account_fields_needed if f in supported and f not in {"MemberName","HasPI"}}
    account_extras_by_acc: Dict[str, Dict[str, Any]] = {}
    if need_account_extras and active_acc_ids and fields_to_fetch:
        account_extras_by_acc = _fetch_account_extras(sf, active_acc_ids, fields_to_fetch)

    # extra.* batch
    extras_map: Dict[str, Dict[str, Any]] = {}
    if need_batch_extras and active_acc_ids:
        extras_map = batch_fetch_account_extras(sf, active_acc_ids)

    # Construir filas crudas mínimas (como en search, sin filtros)
    rows_raw: List[Dict[str, Any]] = []
    for o in opps:
        aid = o.get("AccountId")
        if not aid or aid not in acc_map:
            continue
        acc = acc_map.get(aid) or {}
        sid = site_id_by_acc.get(str(aid))
        qual_data = qual_by_site.get(sid, {}) if sid else {}

        data: Dict[str, Any] = {}
        for k in requested_cols:
            # site.*
            if k == "site.city":
                data[k] = acc.get("city"); continue
            if k == "site.country":
                data[k] = acc.get("country"); continue

            # Account.*
            if k.startswith("Account."):
                sub = k.split(".",1)[1]
                if   sub == "Id":               data[k] = aid
                elif sub == "Name":             data[k] = acc.get("name")
                elif sub == "ShippingCity":     data[k] = acc.get("city")
                elif sub == "ShippingCountry":  data[k] = acc.get("country")
                elif sub == "ShippingLatitude": data[k] = acc.get("lat")
                elif sub == "ShippingLongitude":data[k] = acc.get("lng")
                elif sub in {"BillingCity","BillingCountry","BillingLatitude","BillingLongitude"}:
                    data[k] = acc.get({"BillingCity":"city","BillingCountry":"country","BillingLatitude":"lat","BillingLongitude":"lng"}[sub])
                elif sub == "MemberName":       data[k] = member_by_acc.get(str(aid)) if need_member else None
                elif sub == "HasPI":            data[k] = (str(aid) in pi_accounts) if need_haspi else None
                else:
                    data[k] = (account_extras_by_acc.get(str(aid), {}) or {}).get(sub)
                continue

            # qual.*
            if k.startswith("qual."):
                data[k] = qual_data.get(k[5:]); continue

            # sf.*
            if k.startswith("sf."):
                fld = k[3:]
                if _exists_on_opportunity(fld):
                    data[k] = o.get(fld)
                elif _exists_on_account(fld):
                    data[k] = (o.get("Account") or {}).get(fld)
                else:
                    data[k] = None
                continue

            # extra.*
            if k.startswith("extra."):
                data[k] = (extras_map.get(str(aid), {}) or {}).get(k); continue

            # fallback
            data[k] = o.get(k)

        # Normaliza sf.* embebido si viniera como objeto
        _flatten_sf_inplace(data)

        rows_raw.append({
            "account_id": aid,
            "account_name": acc.get("name"),
            "country": acc.get("country"),
            "city": acc.get("city"),
            "opportunity_type": _norm_type(o.get("Type")),
            "data": data,
        })

    # Colapsa por cuenta para devolver UNA fila por cuenta
    rows = collapse_rows_by_account(rows_raw)
    # (Mantenemos solo las columnas pedidas en data)
    trimmed = []
    for r in rows:
        d = r.get("data") or {}
        trimmed.append({
            "account_id": r.get("account_id"),
            "account_name": r.get("account_name"),
            "city": r.get("city"),
            "country": r.get("country"),
            "data": {k: d.get(k) for k in requested_cols}
        })

    return {"rows": trimmed}



@explorer_router.post("/accounts/search")
def search_accounts(body: AccountSearchBody, request: Request):
    q = (body.query or "").strip()
    if not q:
        return {"rows": []}

    sf = _get_sf(request)
    q_safe = q.replace("'", "\\'")

    soql = f"""
        SELECT Id, Name, BillingCity, BillingCountry, ShippingCity, ShippingCountry
        FROM Account
        WHERE Name LIKE '%{q_safe}%'
            AND (Account_Inactive__c = false OR Account_Inactive__c = null)
            AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)
        LIMIT 25
    """
    recs = _sf_query_all(sf, soql)

    rows = []
    for r in recs:
        city = r.get("ShippingCity") or r.get("BillingCity") or "-"
        country = r.get("ShippingCountry") or r.get("BillingCountry") or "-"
        rows.append({
            "id": r.get("Id"),
            "name": r.get("Name"),
            "city": city,
            "country": country,
        })
    return {"rows": rows}

# === Endpoint: vecinos por distancia de conducción (km) con Google Matrix ===
# Cache simple en memoria para resultados de Distance Matrix (puedes migrarlo a Redis)
_dm_cache: Dict[str, Tuple[Any, float, int]] = {}  # key -> (val, created_ts, ttl)

def _cache_get(k: str):
    with _CACHE_LOCK:
        v = _dm_cache.get(k)
    if not v: return None
    val, ts, ttl = v
    if time.time() - ts > ttl:
        with _CACHE_LOCK:
            _dm_cache.pop(k, None)
        return None
    return val

def _cache_set(k: str, val: Any, ttl: int = 3600):
    with _CACHE_LOCK:
        _dm_cache[k] = (val, time.time(), ttl)

async def _drive_km_matrix(origin: Tuple[float,float], dests: List[Tuple[float,float]]) -> List[Optional[float]]:
    """
    Distancia de conducción (km) por destino usando Google Distance Matrix.
    Devuelve None si un destino falla.
    """
    if not GOOGLE_API_KEY:
        raise HTTPException(status_code=500, detail="Missing GOOGLE_MAPS_API_KEY")
    if not dests:
        return []

    o_str = f"{origin[0]},{origin[1]}"
    out: List[Optional[float]] = [None] * len(dests)
    batch_size = 25  # ajusta según tu plan/cuotas

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(dests), batch_size):
            chunk = dests[i:i+batch_size]
            d_str = "|".join([f"{lat},{lng}" for (lat, lng) in chunk])

            ck_hash = hashlib.md5(d_str.encode()).hexdigest()
            ck = f"dm_km:{o_str}->{ck_hash}"
            mx = _cache_get(ck)
            if mx is None:
                params = {
                    "origins": o_str,
                    "destinations": d_str,
                    "mode": "driving",
                    "key": GOOGLE_API_KEY,
                }
                log.debug("DistanceMatrix request: origins=%s, dest_count=%s", o_str, len(chunk))
                r = await client.get("https://maps.googleapis.com/maps/api/distancematrix/json", params=params)
                r.raise_for_status()
                mx = r.json()
                _cache_set(ck, mx, ttl=3600)
            if mx.get("status") != "OK":
                continue
            elems = (mx.get("rows", [{}])[0] or {}).get("elements", [])
            for j, _ in enumerate(chunk):
                e = elems[j] if j < len(elems) else {}
                if e.get("status") != "OK":
                    continue
                dist_m = (e.get("distance") or {}).get("value")  # metros
                if dist_m is not None:
                    out[i + j] = round(float(dist_m) / 1000.0, 3)  # km
    return out

@explorer_router.post("/search/within-drive-km")
async def explorer_search_within_drive_km(
    payload: Dict = Body(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    1) Calcula TODOS los vecinos (por distancia de conducción) desde base_account_id hasta max_km.
       -> 'neighbors_all' incluye coords + nombre + city/country + extras de Account (MemberName/HasPI y los solicitados/filtrados).
    2) Aplica filtros sf/site/qual/account sobre ese subconjunto y devuelve 'rows' y 'points' ya filtrados.
    """
    sf = _get_sf(request)
    _ensure_describes(sf)

    base_account_id: str = payload.get("base_account_id") or ""
    max_km: Optional[float] = payload.get("max_km")
    if not base_account_id:
        raise HTTPException(status_code=400, detail="base_account_id is required")
    if not max_km or max_km <= 0:
        max_km = 120.0

    # -------- filtros del Filter Builder --------
    filters = payload.get("filters") or {"logic": "AND", "rules": []}
    columns = payload.get("columns") or []
    debug = bool(payload.get("debug"))

    site_rules: List[Rule] = []
    sf_rules:   List[Rule] = []
    qual_rules: List[Rule] = []
    account_rules: List[Rule] = []
    # NUEVO: reglas extra.*
    extra_rules: List[Rule] = []

    # Reglas especiales
    member_rules: List[Rule] = []
    haspi_rules: List[Rule] = []
    need_member = False
    need_haspi  = False
    need_batch_extras = False

    # Extras de Account (p.ej. Accredited__c, ShippingAddress, etc.)
    need_account_fields = False
    account_fields_needed: Set[str] = set()
    requested_account_cols: Set[str] = set()

    def _norm_label_to_key_safe(k: str) -> str:
        try:
            return _norm_label_to_key(k)
        except Exception:
            return k

    # Clasificación de reglas
    for r in (filters.get("rules") or []):
        f = _norm_label_to_key_safe(r.get("field") or "")
        op = r.get("op") or r.get("operator") or "equals"
        val = r.get("value")

        if f in ("sf.Account.MemberName", "Account.MemberName"):
            member_rules.append(Rule(field="Account.MemberName", operator=op, value=val))
            need_member = True
            continue
        if f in ("sf.Account.HasPI", "Account.HasPI"):
            haspi_rules.append(Rule(field="Account.HasPI", operator=op, value=val))
            need_haspi = True
            continue

        if f.startswith("site."):
            site_rules.append(Rule(field=f, operator=op, value=val))
        elif f.startswith("qual."):
            qual_rules.append(Rule(field=f[5:], operator=op, value=val))
        elif f.startswith("extra."):
            extra_rules.append(Rule(field=f, operator=op, value=val))
            need_batch_extras = True
        elif f.startswith("sf."):
            nf = f[3:]
            if nf.startswith("Account."):
                fld = nf.split(".", 1)[1]
                account_rules.append(Rule(field=f"Account.{fld}", operator=op, value=val))
                need_account_fields = True
                account_fields_needed.add(fld)
            else:
                sf_rules.append(Rule(field=nf, operator=op, value=val))
        elif f.startswith("Account."):
            fld = f.split(".",1)[1]
            account_rules.append(Rule(field=f, operator=op, value=val))
            need_account_fields = True
            account_fields_needed.add(fld)
        else:
            if f in ALLOWED_FIELDS and not f.startswith("Account."):
                sf_rules.append(Rule(field=f, operator=op, value=val))

    sf_filter = FilterQuery(logic=filters.get("logic", "AND"), rules=sf_rules)

    # Columnas solicitadas
    requested_cols: List[str] = []
    requested_extra_cols: Set[str] = set()
    for c in (columns or []):
        k = _norm_label_to_key_safe(c)
        requested_cols.append(k)
        if k in ("sf.Account.Member__c", "Account.Member__c", "sf.Account.MemberName", "Account.MemberName"):
            need_member = True
        if k in ("sf.Account.HasPI", "Account.HasPI"):
            need_haspi = True
        if k.startswith("Account."):
            requested_account_cols.add(k.split(".",1)[1])
            need_account_fields = True
        if k.startswith("extra."):
            requested_extra_cols.add(k)
            need_batch_extras = True

    # -------- universo de cuentas con coords (Sites ∪ Accounts) --------
    site_rows = db.execute(
        select(Site.salesforce_account_id, Site.latitude, Site.longitude, Site.city, Site.country, Site.id)
        .where(Site.salesforce_account_id.isnot(None))
    ).all()
    site_by_acc: Dict[str, Dict[str, Any]] = {}
    site_id_by_acc: Dict[str, int] = {}
    for acc, lat, lng, city, country, sid in site_rows:
        site_by_acc[str(acc)] = {"lat": lat, "lng": lng, "city": city, "country": country}
        site_id_by_acc[str(acc)] = int(sid)

    # Cuentas que tienen Opps CTS (para acotar universo de vecinos)
    acc_ids_all = [r["AccountId"] for r in _sf_query_all(
        sf, f"SELECT AccountId FROM Opportunity WHERE Type IN ({TYPE_IN}) AND AccountId != null GROUP BY AccountId"
    )]
    # Filtramos inactivas mediante _build_account_map
    acc_map_all = _build_account_map(sf, acc_ids_all)

    # Merge coords: SIEMPRE prioriza Salesforce; Site como fallback (sólo activas)
    coords_by_acc: Dict[str, Dict[str, Any]] = {}
    for aid in set(list(site_by_acc.keys()) + acc_ids_all):
        if aid not in acc_map_all:
            continue  # descarta inactivas
        a = acc_map_all.get(aid, {})
        s = site_by_acc.get(aid, {})
        # SF → primero; DB(Site) → segundo
        lat = a.get("lat") if a.get("lat") is not None else s.get("lat")
        lng = a.get("lng") if a.get("lng") is not None else s.get("lng")
        city = a.get("city") or s.get("city")
        country = a.get("country") or s.get("country")
        coords_by_acc[aid] = {"lat": lat, "lng": lng, "city": city, "country": country}

    # Geocoding si faltan coords
    for aid, info in list(coords_by_acc.items()):
        if (info.get("lat") is None or info.get("lng") is None) and (info.get("city") or info.get("country")):
            glat, glng = await _geocode_city_country(info.get("city"), info.get("country"))
            if glat is not None and glng is not None:
                info["lat"], info["lng"] = glat, glng
                coords_by_acc[aid] = info

    # -------- base (coords) --------
    acc_map_base = _build_account_map(sf, [base_account_id])
    if base_account_id not in acc_map_base:
        raise HTTPException(status_code=400, detail="Base account is inactive")

    # Primero Salesforce; si falta, luego Site
    acc = acc_map_base.get(base_account_id) or {}
    base_info = {"lat": acc.get("lat"), "lng": acc.get("lng"), "city": acc.get("city"), "country": acc.get("country")}
    if base_info.get("lat") is None or base_info.get("lng") is None:
        s_fallback = site_by_acc.get(str(base_account_id)) or {}
        base_info["lat"] = base_info["lat"] if base_info["lat"] is not None else s_fallback.get("lat")
        base_info["lng"] = base_info["lng"] if base_info["lng"] is not None else s_fallback.get("lng")
        if not base_info.get("city"):    base_info["city"] = s_fallback.get("city")
        if not base_info.get("country"): base_info["country"] = s_fallback.get("country")
    lat0, lng0 = base_info.get("lat"), base_info.get("lng")
    if lat0 is None or lng0 is None:
        glat, glng = await _geocode_city_country(base_info.get("city"), base_info.get("country"))
        lat0, lng0 = glat, glng
    if lat0 is None or lng0 is None:
        raise HTTPException(status_code=404, detail="Base account has no geolocation")

    # -------- 1) Vecinos por distancia (SIN filtros) --------
    dests: List[Tuple[float, float]] = []
    dest_accs: List[str] = []
    for acc, info in coords_by_acc.items():
        if acc == str(base_account_id):
            continue
        lat_i, lng_i = info.get("lat"), info.get("lng")
        if lat_i is None or lng_i is None:
            continue
        dests.append((float(lat_i), float(lng_i)))
        dest_accs.append(acc)

    dists_km = await _drive_km_matrix((float(lat0), float(lng0)), dests)
    neighbor_accs = [acc for acc, km in zip(dest_accs, dists_km) if km is not None and km <= float(max_km)]

    # === Extras también para neighbors_all ===
    fields_to_fetch_neighbors: Set[str] = (account_fields_needed | requested_account_cols) - {"MemberName", "HasPI"}
    account_extras_for_neighbors: Dict[str, Dict[str, Any]] = {}
    if need_account_fields and neighbor_accs and fields_to_fetch_neighbors:
        account_extras_for_neighbors = _fetch_account_extras(sf, neighbor_accs, fields_to_fetch_neighbors)

    # Batch extras (extra.*) para vecinos
    extras_map_neighbors: Dict[str, Dict[str, Any]] = {}
    if need_batch_extras and neighbor_accs:
        extras_map_neighbors = batch_fetch_account_extras(sf, neighbor_accs)

    member_by_acc_neighbors: Dict[str, Optional[str]] = {}
    if need_member and neighbor_accs:
        vals = ",".join(f"'{a}'" for a in neighbor_accs)
        rows = _sf_query_all(sf, f"SELECT Id, C_Member__c, C_Member__r.Name FROM Account WHERE Id IN ({vals})")
        for a in rows:
            member_by_acc_neighbors[str(a.get("Id"))] = (a.get("C_Member__r") or {}).get("Name") or None

    pi_neighbors: Set[str] = set()
    if need_haspi and neighbor_accs:
        vals = ",".join(f"'{a}'" for a in neighbor_accs)
        rows = _sf_query_all(sf, f"SELECT AccountId FROM AccountContactRelation WHERE AccountId IN ({vals}) AND Role__c = 'PI'")
        for r in rows:
            if r.get("AccountId"):
                pi_neighbors.add(str(r.get("AccountId")))

    # Sólo vecinos ACTIVOS (según _build_account_map)
    acc_map_neighbors = _build_account_map(sf, neighbor_accs) if neighbor_accs else {}
    active_neighbor_ids = list(acc_map_neighbors.keys())

    # Construcción de neighbors_all (sólo activos)
    neighbors_all: List[Dict[str, Any]] = []
    for acc in active_neighbor_ids:
        accd = acc_map_neighbors.get(acc)  # activo
        # 1) Coords de SF
        lat, lng = accd.get("lat"), accd.get("lng")
        # 2) Si faltan -> geocode con city/country de SF
        if lat is None or lng is None:
            glat, glng = await _geocode_city_country(accd.get("city"), accd.get("country"))
            if glat is not None and glng is not None:
                lat, lng = glat, glng
        # 3) Si aún faltan -> coords del Site
        if lat is None or lng is None:
            s_info = site_by_acc.get(acc) or {}
            lat = lat if lat is not None else s_info.get("lat")
            lng = lng if lng is not None else s_info.get("lng")
        # 4) Último recurso -> geocode con city/country del Site
        if (lat is None or lng is None) and site_by_acc.get(acc):
            s_info = site_by_acc.get(acc) or {}
            glat, glng = await _geocode_city_country(s_info.get("city"), s_info.get("country"))
            if glat is not None and glng is not None:
                lat, lng = glat, glng
        if lat is None or lng is None:
            continue

        extras: Dict[str, Any] = {}
        if need_member:
            extras["MemberName"] = member_by_acc_neighbors.get(str(acc))
        if need_haspi:
            extras["HasPI"] = (str(acc) in pi_neighbors)
        if need_account_fields:
            extras.update(account_extras_for_neighbors.get(str(acc), {}))
        if need_batch_extras:
            # añade sólo las columnas pedidas de extra.* si hay
            if requested_extra_cols:
                xall = extras_map_neighbors.get(str(acc), {}) or {}
                extras.update({k: xall.get(k) for k in requested_extra_cols})

        neighbors_all.append({
            "lat": lat, "lng": lng,
            "account_id": acc,
            "account_name": accd.get("name"),
            "city": accd.get("city") or (site_by_acc.get(acc) or {}).get("city"),
            "country": accd.get("country") or (site_by_acc.get(acc) or {}).get("country"),
            "extras": extras,
        })

    # Si no hay vecinos activos, devolvemos bootstrap mínimo
    if not active_neighbor_ids:
        return {
            "base": {"account_id": base_account_id, "lat": lat0, "lng": lng0},
            "neighbors_all": neighbors_all,
            "points": [],
            "rows": [],
            "meta": {"mode": "drive_km", "max_km": max_km}
        }

    # -------- 2) Filtrar SOLO sobre vecinos activos --------
    vals = ", ".join(f"'{a}'" for a in active_neighbor_ids)
    base_where = f"Type IN ({TYPE_IN}) AND AccountId != null AND AccountId IN ({vals})"
    extra_where = _build_sf_where(sf_filter)
    where_sql = f"WHERE {base_where}" + (f" AND {extra_where}" if extra_where else "")

    # Campos Opp mínimos + por reglas/columnas (con soporte Account.* a través de relación)
    opp_fields: Set[str] = {"Id", "Name", "Type", "StageName", "IsClosed", "CloseDate", "AccountId"}

    # Reglas SF: sólo las que son de Opportunity
    for r in sf_rules:
        if r.field.startswith("Account."):
            continue
        if _exists_on_opportunity(r.field):
            opp_fields.add(r.field)

    # Columnas pedidas: Opportunity vs Account
    for k in requested_cols:
        if not k.startswith("sf."):
            continue
        fld = k[3:]
        if _exists_on_opportunity(fld):
            opp_fields.add(fld)
        elif _exists_on_account(fld):
            opp_fields.add(f"Account.{fld}")

    opp_fields_valid = {f for f in opp_fields if _exists_on_opportunity(f)}
    if (set(opp_fields) - opp_fields_valid):
        log.warning("Omitiendo campos inexistentes en Opportunity: %s", ", ".join(sorted(set(opp_fields) - opp_fields_valid)))

    select_fields = ", ".join(sorted(opp_fields_valid))
    opps = _sf_query_all(sf, f"SELECT {select_fields} FROM Opportunity {where_sql}")
    if not opps:
        return {
            "base": {"account_id": base_account_id, "lat": lat0, "lng": lng0},
            "neighbors_all": neighbors_all,
            "points": [],
            "rows": [],
            "meta": {"mode": "drive_km", "max_km": max_km}
        }

    # Mapa de Accounts (de los opps filtrados) -> también filtra inactivas
    acc_ids = sorted({o.get("AccountId") for o in opps if o.get("AccountId")})
    acc_map = _build_account_map(sf, acc_ids)

    # Qualification JSON por site
    qual_rows = db.execute(select(SiteQual.site_id, SiteQual.data)).all()
    groups_by_slug = _qual_groups_from_questions(db)
    qual_by_site: Dict[int, Dict[str, Any]] = {
        sid: _expand_comments_keys_for_row(data or {}, groups_by_slug) for sid, data in qual_rows
    }

    # Member/HasPI y extras sobre el subconjunto filtrado
    member_by_acc: Dict[str, Optional[str]] = {}
    if need_member and acc_ids:
        vals2 = ",".join(f"'{x}'" for x in acc_ids)
        rows = _sf_query_all(sf, f"SELECT Id, C_Member__c, C_Member__r.Name FROM Account WHERE Id IN ({vals2})")
        for a in rows:
            member_by_acc[str(a.get("Id"))] = (a.get("C_Member__r") or {}).get("Name") or None

    pi_accounts: Set[str] = set()
    if need_haspi and acc_ids:
        vals2 = ",".join(f"'{x}'" for x in acc_ids)
        rows = _sf_query_all(sf, f"SELECT AccountId FROM AccountContactRelation WHERE AccountId IN ({vals2}) AND Role__c = 'PI'")
        for r in rows:
            if r.get("AccountId"):
                pi_accounts.add(str(r.get("AccountId")))

    fields_to_fetch = ((account_fields_needed | requested_account_cols) - {"MemberName","HasPI"})
    account_extras_by_acc: Dict[str, Dict[str, Any]] = {}
    if need_account_fields and acc_ids and fields_to_fetch:
        account_extras_by_acc = _fetch_account_extras(sf, acc_ids, fields_to_fetch)

    # Batch extras (extra.*) sobre el subconjunto filtrado
    extras_map: Dict[str, Dict[str, Any]] = {}
    if need_batch_extras and acc_ids:
        extras_map = batch_fetch_account_extras(sf, acc_ids)

    # --- helpers de filtrado post-query ---
    def pass_site(acc: Dict[str, Any]) -> bool:
        if not site_rules:
            return True
        def match_one(rule: Rule) -> bool:
            val = ""
            if rule.field == "site.city":    val = (acc.get("city")    or "")
            if rule.field == "site.country": val = (acc.get("country") or "")
            op = _OP_SYNONYM.get(rule.operator, rule.operator); s = str(rule.value or "")
            if op in ("equals","="): return val == s
            if op in ("not_equals","!="): return val != s
            if op == "contains": return s.lower() in val.lower()
            if op == "not_contains": return s.lower() not in val.lower()
            if op == "starts_with": return val.lower().startswith(s.lower())
            if op == "ends_with": return val.lower().endswith(s.lower())
            return True
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = [match_one(r) for r in site_rules]
        return all(res) if glue_and else any(res)

    def pass_qual(qual_data: Dict[str, Any]) -> bool:
        if not qual_rules:
            return True
        res = [_eval_qual_rule(qual_data.get(qr.field), qr.operator, qr.value) for qr in qual_rules]
        return all(res) if (filters.get("logic") or "AND") == "AND" else any(res)

    def pass_member(aid: str) -> bool:
        if not member_rules:
            return True
        actual = member_by_acc.get(str(aid)) or ""
        def eval_one(rule: Rule) -> bool:
            op = _OP_SYNONYM.get(rule.operator, rule.operator); s = str(rule.value or "")
            if op in ("equals","="): return actual == s
            if op in ("not_equals","!="): return actual != s
            if op == "contains": return s.lower() in actual.lower()
            if op == "not_contains": return s.lower() not in actual.lower()
            if op == "starts_with": return actual.lower().startswith(s.lower())
            if op == "ends_with": return actual.lower().endswith(s.lower())
            return True
        res = [eval_one(r) for r in member_rules]
        return all(res) if (filters.get("logic") or "AND") == "AND" else any(res)

    def pass_haspi(aid: str) -> bool:
        if not haspi_rules:
            return True
        actual = str(aid) in pi_accounts
        def as_bool(x: Any) -> bool:
            if isinstance(x, bool): return x
            s = str(x).strip().lower()
            return s in ("1","true","yes","y","t")
        res = []
        for r in haspi_rules:
            op = _OP_SYNONYM.get(r.operator, r.operator); want = as_bool(r.value)
            if op in ("equals","="):      res.append(actual == want)
            elif op in ("not_equals","!="): res.append(actual != want)
            else:                         res.append(True)
        return all(res) if (filters.get("logic") or "AND") == "AND" else any(res)

    def pass_account(aid: str) -> bool:
        if not account_rules:
            return True
        vals = dict(account_extras_by_acc.get(str(aid), {}))
        if need_member: vals["MemberName"] = member_by_acc.get(str(aid))
        if need_haspi:  vals["HasPI"] = (str(aid) in pi_accounts)
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = []
        for ar in account_rules:
            fld = ar.field.split(".",1)[1]  # "Account.X" -> "X"
            res.append(_eval_qual_rule(vals.get(fld), ar.operator, ar.value))
        return all(res) if glue_and else any(res)

    # NUEVO: filtros extra.*
    def pass_extra(aid: str) -> bool:
        if not extra_rules:
            return True
        vals = extras_map.get(str(aid), {}) if extras_map else {}
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = []
        for er in extra_rules:
            actual = vals.get(er.field)  # keys 'extra.*'
            res.append(_eval_qual_rule(actual, er.operator, er.value))
        return all(res) if glue_and else any(res)

    # --- construir filas crudas ---
    rows: List[Dict[str, Any]] = []
    for o in opps:
        aid = o.get("AccountId")
        if not aid:
            continue
        if aid not in acc_map:      # evita inactivas
            continue
        acc = acc_map.get(aid) or {}
        if not pass_site(acc):      continue

        sid = site_id_by_acc.get(str(aid))
        qual_data = qual_by_site.get(sid, {}) if sid else {}
        if not pass_qual(qual_data): continue
        if not pass_member(aid):     continue
        if not pass_haspi(aid):      continue
        if not pass_account(aid):    continue
        if not pass_extra(aid):      continue

        data: Dict[str, Any] = {}
        for k in requested_cols:
            if k == "site.city":       data[k] = acc.get("city"); continue
            if k == "site.country":    data[k] = acc.get("country"); continue
            if k.startswith("Account."):
                sub = k.split(".",1)[1]
                if   sub == "Id":                   data[k] = aid
                elif sub == "Name":                 data[k] = acc.get("name")
                elif sub == "ShippingCity":         data[k] = acc.get("city")
                elif sub == "ShippingCountry":      data[k] = acc.get("country")
                elif sub == "ShippingLatitude":     data[k] = acc.get("lat")
                elif sub == "ShippingLongitude":    data[k] = acc.get("lng")
                elif sub in {"BillingCity","BillingCountry","BillingLatitude","BillingLongitude"}:
                    data[k] = acc.get({"BillingCity":"city","BillingCountry":"country","BillingLatitude":"lat","BillingLongitude":"lng"}[sub])
                elif sub == "MemberName":           data[k] = member_by_acc.get(str(aid)) if need_member else None
                elif sub == "HasPI":                data[k] = (str(aid) in pi_accounts) if need_haspi else None
                else:
                    data[k] = (account_extras_by_acc.get(str(aid), {}) or {}).get(sub)
                continue
            if k.startswith("qual."):  data[k] = qual_data.get(k[5:]); continue
            if k.startswith("sf."):    data[k] = o.get(k[3:]); continue
            if k.startswith("extra."):
                data[k] = (extras_map.get(str(aid), {}) or {}).get(k); continue
            data[k] = o.get(k)

        _flatten_sf_inplace(data)

        rows.append({
            "account_id": aid,
            "account_name": acc.get("name"),
            "country": acc.get("country"),
            "city": acc.get("city"),
            "opportunity_type": _norm_type(o.get("Type")),
            "data": data,
        })

    # Colapsa a una fila por cuenta
    rows = collapse_rows_by_account(rows)

    # Badges + puntos (solo los que CUMPLEN filtros)
    badges: Dict[str, Dict[str, bool]] = {}
    for r in rows:
        aid = r.get("account_id")
        if not aid: continue
        b = badges.setdefault(aid, {"profiling": False, "qualification": False})
        t = (r.get("opportunity_type") or "").lower()
        if t in ("profiling", "both"): b["profiling"] = True
        if t in ("qualification", "both"): b["qualification"] = True

    acc_map_final = _build_account_map(sf, [r["account_id"] for r in rows if r.get("account_id")])
    points = []
    for r in rows:
        aid = r.get("account_id")
        accd = acc_map_final.get(aid) or {}
        # 1) SF coords
        lat, lng = accd.get("lat"), accd.get("lng")
        # 2) Geocode con SF city/country si faltan
        if lat is None or lng is None:
            glat, glng = await _geocode_city_country(accd.get("city"), accd.get("country"))
            if glat is not None and glng is not None:
                lat, lng = glat, glng
        # 3) Coords de DB (Site)
        if lat is None or lng is None:
            s_info = site_by_acc.get(str(aid)) or {}
            lat = lat if lat is not None else s_info.get("lat")
            lng = lng if lng is not None else s_info.get("lng")
        # 4) Geocode con city/country de DB
        if (lat is None or lng is None) and site_by_acc.get(str(aid)):
            s_info = site_by_acc.get(str(aid)) or {}
            glat, glng = await _geocode_city_country(s_info.get("city"), s_info.get("country"))
            if glat is not None and glng is not None:
                lat, lng = glat, glng
        if lat is None or lng is None:
            continue
        points.append({
            "lat": lat, "lng": lng,
            "account_id": aid,
            "account_name": accd.get("name") or r.get("account_name"),
            "city": accd.get("city") or r.get("city"),
            "country": accd.get("country") or r.get("country"),
            "badges": badges.get(aid, {"profiling": False, "qualification": False}),
        })

    return {
        "base": {"account_id": base_account_id, "lat": lat0, "lng": lng0},
        "neighbors_all": neighbors_all,   # ahora sólo activos
        "points": points,                  # vecinos que cumplen filtros
        "rows": rows,                      # colapsados por cuenta
        "meta": {"mode": "drive_km", "max_km": max_km}
    }