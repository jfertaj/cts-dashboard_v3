# backend/app/routers/salesforce_explorer.py
from __future__ import annotations
from sqlalchemy import literal
from typing import Any, Dict, List, Optional, Set, Literal, Tuple, Iterable
from fastapi import APIRouter, HTTPException, Request, Body, Depends
from pydantic import BaseModel, Field
import os, time, math, re, json, logging, threading, asyncio
import httpx
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter
import hashlib
from dataclasses import dataclass

from simple_salesforce.exceptions import (
    SalesforceExpiredSession, SalesforceAuthenticationFailed,
    SalesforceMalformedRequest, SalesforceGeneralError,
    SalesforceRefusedRequest, SalesforceResourceNotFound,
)

from app.routers.salesforce_extras_batch import batch_fetch_account_extras
from app.utils.country_norms import norm_to_iso2, iso2_to_display, ISO2_TO_DISPLAY as _ISO2_TO_DISPLAY

from app.routers.filter_engine import (
    Rule, FilterQuery,
    _OP_SYNONYM, _OP_MAP,
    _DATE_FMTS, _NUM_RE,
    _parse_date_any, _coerce_scalar, _cmp, _normalize_list,
    _eval_qual_rule, _qual_get,
    _flatten_filter_rules, _filter_group_to_expr,
    _is_logic_expr, _eval_logic_expr_be,
    _norm_label_to_key,
)
from app.utils.geo_cache import (
    _GEO_CACHE, _geo_key, _save_geo_cache_file,
    _geo_cache_get, _geo_cache_put,
    _extract_result_country_iso, _haversine_km,
)

# DB
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.database import get_db
from app.models.site import Site
from app.models.site_qual import SiteQual
from app.models import Questionnaire, Question, Section, QuestionnaireType
from app.parser import qualification as qual_parser

# ====== OAuth helpers ======
from app.services.salesforce_oauth import (
    COOKIE_NAME, unsign_value, get_salesforce_from_session_id,
)

log = logging.getLogger("cts-backend")

salesforce_router = APIRouter(prefix="/api/salesforce", tags=["salesforce-explorer"])
explorer_router   = APIRouter(prefix="/api/explorer",   tags=["explorer"])

GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()

# Guard rail: max billable elements (origins × destinations) per Distance Matrix request.
# Prevents runaway costs from wide searches.  Override via env var if needed.
MAX_DISTANCE_MATRIX_ELEMENTS = int(os.getenv("MAX_DISTANCE_MATRIX_ELEMENTS", "2000"))
# Max candidates per base to send to Distance Matrix after haversine pre-filter
DM_CANDIDATES_PER_BASE = int(os.getenv("DM_CANDIDATES_PER_BASE", "20"))

# ======================= CARGA DE CAMPOS (JSON CURADO SF) =======================

FIELDS_PATH = Path(__file__).parent.parent / "config" / "fields_opportunity_curated.json"
with open(FIELDS_PATH, encoding="utf-8") as f:
    FIELD_CONFIG: List[Dict[str, Any]] = json.load(f)

# Mapa de tipos por clave sin prefijo "sf." (skip comment-only entries)
TYPE_BY_KEY: Dict[str, str] = { f["key"].replace("sf.", ""): (f.get("type") or "string") for f in FIELD_CONFIG if "key" in f }

CURATED_ALLOWED: Set[str] = {
    f["key"].replace("sf.", "")
    for f in FIELD_CONFIG
    if "key" in f and f.get("source") == "sf"
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

# _DATE_FMTS, _NUM_RE, _parse_date_any, _coerce_scalar, _cmp, _normalize_list,
# _eval_qual_rule, _qual_get — moved to filter_engine.py


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


def _sf_bool(value: Any) -> bool:
    """
    Salesforce booleans pueden venir como True/False, 0/1, o strings "true"/"false".
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        val = value.strip().lower()
        if val in {"", "0", "false", "no", "n"}:
            return False
        return val in {"1", "true", "yes", "y"}
    return bool(value)


def _normalize_label_lookup_key(label: str) -> str:
    import re
    return re.sub(r"\s+", " ", (label or "").strip()).lower()


def _normalize_subcode_token(code: str | None) -> set[str]:
    if not code:
        return {None}
    raw = (code or "").strip()
    if not raw:
        return {None}
    out = {None}
    variants = {raw, raw.replace(".", "_"), raw.replace("_", "."), raw.lower(), raw.upper()}
    for v in variants:
        if not v:
            continue
        out.add(v.strip())
        out.add(v.strip().lower())
    return out


def _register_override_label(index: dict, label: str, slug: str, subcode: str | None = None) -> None:
    if not label or not slug:
        return
    lbl_key = _normalize_label_lookup_key(label)
    bucket = index.setdefault(lbl_key, {})
    for code_variant in _normalize_subcode_token(subcode):
        if code_variant not in bucket:
            bucket[code_variant] = set()
        bucket[code_variant].add(slug)
    # También registra sin signos de interrogación finales (si aplica)
    if label.endswith("?"):
        _register_override_label(index, label.rstrip("?").strip(), slug, subcode)


_DEVICE_PRETTY_LOOKUP = {
    _normalize_label_lookup_key(pretty): slug
    for slug, pretty in (qual_parser.DEVICE_PRETTY or {}).items()
}
_KNOWN_DEVICE_SLUGS = set((qual_parser.DEVICE_PRETTY or {}).keys())
_DEVICE_SUFFIXES = sorted(
    {suffix for mapping in (qual_parser.DEVICE_TEMPERATURE_SUFFIX or {}).values() for suffix in mapping.values()},
    key=len,
    reverse=True,
)


@dataclass(frozen=True)
class _SlugGuess:
    base_slug: str
    device_slug: str | None = None


def _split_device_from_label(label: str) -> tuple[str | None, str]:
    for sep in (" — ", " – ", " - ", " -- ", " —", " –", " -"):
        if sep in label:
            left, right = label.split(sep, 1)
            device_name = left.strip()
            rest = right.strip()
            device_slug = _DEVICE_PRETTY_LOOKUP.get(_normalize_label_lookup_key(device_name)) or _slugify_question(device_name)
            return device_slug, rest
    return None, label


def _strip_known_suffixes(text: str) -> str:
    t = text.strip()
    for suffix in _DEVICE_SUFFIXES:
        if suffix and t.endswith(suffix):
            return t[: -len(suffix)].rstrip()
    return t


def _lookup_override_slugs(label: str, subcode: str | None) -> set[str]:
    if not label:
        return set()
    key = _normalize_label_lookup_key(label)
    entry = _LABEL_OVERRIDE_INDEX.get(key)
    if not entry:
        return set()
    out: set[str] = set()
    candidates = [None]
    if subcode:
        candidates.extend(_normalize_subcode_token(subcode))
    for cand in candidates:
        if cand in entry:
            out.update(entry[cand])
    return out


def _guess_slugs_for_question(question_text: str, subcode: str | None) -> list[_SlugGuess]:
    txt = (question_text or "").strip()
    if not txt:
        return []

    # Prefijo IMP opcional
    core = txt[4:].strip() if txt.upper().startswith("IMP ") else txt

    device_slug, remainder = _split_device_from_label(core)
    cleaned = _strip_known_suffixes(remainder).strip()

    variants = {core.strip(), remainder.strip(), cleaned.strip(), cleaned.strip().rstrip("?")}
    slugs: set[_SlugGuess] = set()
    for variant in variants:
        if not variant:
            continue
        for slug in _lookup_override_slugs(variant, subcode):
            if slug:
                slugs.add(_SlugGuess(slug, device_slug))

    if slugs:
        return sorted(slugs, key=lambda g: (g.device_slug or "", g.base_slug))

    # Fallback al slugify normal
    fallback_slug = _slugify_question(cleaned or core)
    return [_SlugGuess(fallback_slug, device_slug)]

# === Overrides & filtros para el catálogo qual.* que muestra /api/explorer/fields ===
# Prefijos de grupo (sección) a excluir del Column Picker (p.ej. "4." -> irá a Profiling)
EXCLUDE_QUAL_GROUP_PREFIXES: tuple[str, ...] = ("1.", "PART I", "4.", "PART IV")

# Keys JSONB históricos (sin prefijo de sección) que aparecen en uploads antiguos pero tienen
# un campo canónico section-prefixed equivalente en el catálogo actual. El 4º fallback de
# _qual_get resuelve el campo canónico hacia estos keys viejos, así que el catálogo solo
# debe mostrar el canónico y excluir estos duplicados.
QUAL_FIELD_BLACKLIST: frozenset[str] = frozenset({
    # Superseded by qual.3_8__insulin → fallback strips prefix → "insulin"
    "are_insulin_tests_available",
    # Superseded by qual.3_8__gad65 → fallback strips prefix → "gad65"
    "are_gad65_tests_available",
    # Superseded by qual.3_8__autoantibodies_aab → 4th fallback strips "_aab" → "autoantibodies"
    "autoantibodies",
    # Superseded by qual.3_3__estimate_arrival_time_of_emergency_personnel_min → 5th fallback strips "_min"
    "estimate_arrival_time_of_emergency_personnel",
    # Superseded by qual.3_5_1__how_long_are_documents_retained_years → 5th fallback strips "_years"
    "how_long_are_documents_retained",
    # Superseded by qual.3_5_2__longest_single_day_visit_that_could_be_accommodated_hours → strips "_hours"
    "longest_single_day_visit_that_could_be_accommodated",
    # Superseded by qual.3_8__znt8 → fallback strips prefix → "znt8"
    "znt8",
    # Old full-name keys that appear in early uploads before section-prefixed keys were introduced
    "are_znt8_tests_available",
})

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
    "2_1__comments": "Comments on SOPs or general",
    "2_2__comments": "Comments on consent process or age of enrollment",
    # 2.2 Recruitment & Consenting
    "meets_basic_criteria_per_gcp_ich": "Does the consenting process meet GCP requirements",
    "personal_conversation_with_physician": "Does the consenting process involve a Personal Conversation with Physician",
    "who_is_responsible": "Who is responsible for recruitment and consenting",
    "2_4__comments": "Comments on Contracting Process",
    "3_8__comments": "Comments on the site lab resources or accreditation",
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
    # 3.7.1 storage device clarifications
    "3_7_1__fridge__available": "Fridge — available (2-8ºC)",
    "3_7_1__fridge__backup_plan_for_fridge_failure": "Fridge — Backup plan for fridge failure (2-8ºC)",
    # 3.7.2 Additional devices/equipment
    "blood_pressure_monitor": {"_group": "3.7.2 Additional devices/equipment",
                               "label": "Blood pressure monitor available?"},
    "centrifuge": {"_group": "3.7.2 Additional devices/equipment",
                   "label": "Centrifuge available?"},
    "ecg_device": {"_group": "3.7.2 Additional devices/equipment",
                   "label": "ECG device available?"},
    "monitor_for_oxygen_saturation": {"_group": "3.7.2 Additional devices/equipment",
                                      "label": "Monitor for oxygen saturation available?"},
    "monitor_for_respiratory_rate": {"_group": "3.7.2 Additional devices/equipment",
                                     "label": "Monitor for respiratory rate available?"},
    "temperature_range": {"_group": "3.7.2 Additional devices/equipment",
                          "label": "Temperature range (°C)"},
    "by_whom": {"_group": "3.7.2 Additional devices/equipment",
                "label": "Who conducts regular device checks?"},
    "regular_official_device_checks_for_monitoring_devices": {
        "_group": "3.7.2 Additional devices/equipment",
        "label": "Are regular official device checks for monitoring devices conducted?",
    },
    "certificate_label_provided_by_vendor_to_site": {
        "_group": "3.7.2 Additional devices/equipment",
        "label": "Are certificates provided after routine device checks?",
    },
    "how_often": {"_group": "3.7.2 Additional devices/equipment",
                  "label": "How often are regular device checks conducted?"},
    # 3.8 Laboratory
    "availability": {"_group": "3.8 Laboratory",
                     "label": "Is the lab available centrally or locally?"},
    "certificate_available": {"_group": "3.8 Laboratory",
                              "label": "Do you have a local or external lab certificate available?"},
    "is_there_an_sop": {"_group": "3.8 Laboratory",
                        "label": "Is there an SOP for preparing blood samples?"},
    "describe_process": {"_group": "3.8 Laboratory",
                         "label": "Describe process for preparing blood samples"},
    "fbc": {"_group": "3.8 Laboratory", "label": "Are FBC tests available?"},
    "hba1c": {"_group": "3.8 Laboratory", "label": "Is HbA1c testing available?"},
    "c_peptide": {"_group": "3.8 Laboratory", "label": "Is C-peptide testing available?"},
    "glucose": {"_group": "3.8 Laboratory", "label": "Is glucose testing available?"},
    "insulin": {"_group": "3.8 Laboratory",
                 "label": "Are insulin tests available?"},
    "insulin_2": {"_group": "3.8 Laboratory",
                   "label": "Are insulin autoantibody tests available?"},
    "peripheral_blood_mononuclear_cells": {"_group": "3.8 Laboratory",
                                           "label": "Are PBMC tests available?"},
    "autoantibodies": {"_group": "3.8 Laboratory", "label": "Are autoantibody tests available?"},
    "gad65": {"_group": "3.8 Laboratory", "label": "Are GAD65 tests available?"},
    "znt8": {"_group": "3.8 Laboratory", "label": "Are ZnT8 tests available?"},
    "ia_2": {"_group": "3.8 Laboratory", "label": "Are IA-2 tests available?"},
    "possibility_to_have_pcr_tests_performed": {"_group": "3.8 Laboratory",
                                                "label": "Is PCR testing available?"},
    "can_you_do_hla_typing": {"_group": "3.8 Laboratory",
                              "label": "Is HLA typing available?"},
    "who_will_prepare_the_blood_samples": {"_group": "3.8 Laboratory",
                                           "label": "Who prepares the blood samples?"},
}


def _build_label_override_index() -> dict[str, dict[str | None, set[str]]]:
    idx: dict[str, dict[str | None, set[str]]] = {}

    def _register_list(labels, slug_base: str, subcode: str | None = None):
        if not labels:
            return
        for i, lbl in enumerate(labels):
            slug = slug_base if i == 0 else f"{slug_base}_{i+1}"
            _register_override_label(idx, lbl, slug, subcode)

    # Comentarios específicos
    for subcode, mapping in (qual_parser.COMMENT_LABEL_OVERRIDES or {}).items():
        if isinstance(mapping, dict):
            for slug_base, val in mapping.items():
                if isinstance(val, list):
                    _register_list(val, slug_base or "comments", subcode)
                else:
                    _register_override_label(idx, val, slug_base or "comments", subcode)
        elif isinstance(mapping, list):
            _register_list(mapping, "comments", subcode)
        else:
            _register_override_label(idx, mapping, "comments", subcode)

    # Describe overrides
    for subcode, mapping in (qual_parser.DESCRIBE_LABEL_OVERRIDES or {}).items():
        if isinstance(mapping, dict):
            for slug_base, val in mapping.items():
                if isinstance(val, list):
                    _register_list(val, slug_base or "describe", subcode)
                else:
                    _register_override_label(idx, val, slug_base or "describe", subcode)
        elif isinstance(mapping, list):
            _register_list(mapping, "describe", subcode)
        else:
            _register_override_label(idx, mapping, "describe", subcode)

    # Genéricos (lab, pharmacy, etc.)
    for subcode, mapping in (qual_parser.GENERIC_LABEL_OVERRIDES or {}).items():
        for slug_base, val in (mapping or {}).items():
            if isinstance(val, list):
                _register_list(val, slug_base, subcode)
            else:
                _register_override_label(idx, val, slug_base, subcode)

    # Overrides específicos con clave completa (incluye device)
    for key_full, label in (qual_parser.SPECIFIC_LABEL_OVERRIDES or {}).items():
        key_txt = str(key_full or "").strip()
        if not key_txt:
            continue
        parts = key_txt.split("__")
        if len(parts) < 2:
            continue
        subcode = parts[0].replace(".", "_").strip()
        base_slug = parts[-1].strip()
        if base_slug:
            _register_override_label(idx, label, base_slug, subcode)

    # Overrides definidos en este módulo
    for slug, val in QUAL_LABEL_OVERRIDES.items():
        if isinstance(val, dict):
            label = val.get("label")
            if label:
                _register_override_label(idx, label, slug, None)
        else:
            _register_override_label(idx, val, slug, None)

    return idx


_LABEL_OVERRIDE_INDEX = _build_label_override_index()


# --- Helpers para formatear labels y grupos de claves qual.* nuevas ---
_SUBCODE_RE = re.compile(r"^\d+(?:_\d+)*$")          # 3, 3_5, 3_5_4 ...
_SUBSECT_CODE_EXTRACT_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)")


def _normalize_qual_key(raw: str) -> str:
    if not raw:
        return raw
    text = str(raw)
    parts = text.split("__", 1)
    head = parts[0].strip().replace(".", "_")
    if len(parts) == 1:
        return head
    tail = parts[1].strip()
    return f"{head}__{tail}"

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

def _build_qual_question_catalog(db: Session) -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, str]]]]:
    """
    Devuelve dos catálogos:
      - by_key:    clave canónica (sin prefijo qual.) -> metadata
      - by_slug:   slug base -> [metadata,...] (para resolver empates por subcode/device)
    """

    rows = (
        db.query(
            Question.question_text,
            Question.subsection,
            Question.question_number,
            Section.name,
        )
        .join(Section, Section.id == Question.section_id)
        .join(Questionnaire, Questionnaire.id == Section.questionnaire_id)
        .filter(Questionnaire.type == QuestionnaireType.qualification)
        .filter(func.length(func.coalesce(Question.question_text, "")) > 0)
        .limit(10_000)
        .all()
    )

    catalog_by_key: dict[str, dict[str, str]] = {}
    catalog_by_slug: dict[str, list[dict[str, str]]] = defaultdict(list)

    for qtext, subsection, qnum, secname in rows:
        question = (qtext or "").strip()
        if not question:
            continue

        section = (secname or "").strip()
        if section.upper().startswith("PART I"):
            continue

        slug = _slugify_question(question)
        if not slug:
            continue

        subsection_label = (subsection or "").strip()
        subcode = None
        if subsection_label:
            m = _SUBSECT_CODE_EXTRACT_RE.match(subsection_label)
            if m:
                subcode = m.group(1).replace('.', '_')
        alt_code = None
        if qnum:
            m = _SUBSECT_CODE_EXTRACT_RE.match(str(qnum))
            if m:
                alt_code = m.group(1).replace('.', '_')
        code_candidates = []
        if subcode:
            code_candidates.append(subcode)
        if alt_code and alt_code not in code_candidates:
            code_candidates.append(alt_code)

        slug_guesses = _guess_slugs_for_question(question, subcode or alt_code)
        if not slug_guesses:
            slug_guesses = [_SlugGuess(_slugify_question(question), None)]

        for guess in slug_guesses:
            meta = {
                "question": question,
                "subsection": subsection_label,
                "section": section,
                "subcode": (subcode or alt_code or ""),
                "base_slug": guess.base_slug,
                "device": guess.device_slug or "",
            }

            catalog_by_slug[guess.base_slug].append(meta)

            base_key = _normalize_qual_key(guess.base_slug)
            catalog_by_key.setdefault(base_key, meta)

            for code in code_candidates or [""]:
                if not code:
                    continue
                parts = [code]
                if guess.device_slug:
                    parts.append(guess.device_slug)
                parts.append(guess.base_slug)
                canon = _normalize_qual_key("__".join(parts))
                catalog_by_key.setdefault(canon, meta)

    return catalog_by_key, catalog_by_slug


def _split_canonical_qual_key(key_no_prefix: str) -> tuple[str | None, str | None, str]:
    """
    Descompone '3_7_1__fridge__temperature_log' en ('3_7_1','fridge','temperature_log')
    """
    raw = (key_no_prefix or "").strip()
    if not raw:
        return None, None, ""
    parts = raw.split("__")
    subcode = None
    device = None
    base = ""
    if parts and _SUBCODE_RE.match(parts[0]):
        subcode = parts[0]
        parts = parts[1:]
    if parts:
        if len(parts) >= 2 and parts[0] in _KNOWN_DEVICE_SLUGS:
            device = parts[0]
            base = "__".join(parts[1:]) or ""
        else:
            base = "__".join(parts) or ""
    return subcode, device, base

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
    Añade el código de subsección a TODAS las columnas qual.* para consistencia:
      - qual.3_6_3__comments       -> Comments (3.6.3)
      - qual.2_4__comments_details -> Comments details (2.4)
      - qual.2_1__comments         -> Comments on SOPs or general (2.1)
    No cambia las keys, solo la etiqueta visible.
    
    EXCEPCIONES:
    - No se aplica a labels que ya contienen " — " (dispositivos)
    - No se aplica a labels que empiezan con "IMP " 
    - No se aplica si el label ya tiene un código de sección al final
    """
    import re

    out: list[dict] = []
    for f in fields:
        k = str(f.get("key") or "")
        lbl = (f.get("label") or "").strip()

        # Solo aplicar a columnas qual.*
        if not k.startswith("qual."):
            out.append(f)
            continue
        
        # NO añadir sufijo si:
        # 1. Ya tiene dispositivo (contiene " — ")
        # 2. Es un campo IMP
        # 3. Ya tiene un código de sección al final tipo "(3.5.4)"
        if (" — " in lbl or 
            lbl.startswith("IMP ") or
            re.search(r"\(\d+\.\d+(?:\.\d+)?\)\s*$", lbl)):
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

        # Añade el sufijo si tenemos un subcode válido
        if subcode:
            f = {**f, "label": f"{lbl} ({subcode})"}

        out.append(f)

    return out


def _apply_qual_overrides(fields: list[dict]) -> list[dict]:
    """
    - Oculta secciones por prefijo (e.g., "4.", "PART I")
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
        
        # 0b) Filtrar PART I por clave (formato I__*)
        if k.startswith("qual.I__"):
            continue

        # 1) ocultar secciones por grupo o qual_section
        if grp:
            gnorm = grp.strip().upper()
            if any(gnorm.startswith(p.upper()) for p in EXCLUDE_QUAL_GROUP_PREFIXES):
                continue
        qsection = (f.get("qual_section") or "").strip()
        if qsection:
            snorm = qsection.upper()
            if any(snorm.startswith(p.upper()) for p in EXCLUDE_QUAL_GROUP_PREFIXES):
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
        slug = _slugify_question(qt)
        key = f"qual.{slug}"
        out.append({
            "key": key,
            "label": (qt or "").strip(),
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

# Rule, FilterQuery — moved to filter_engine.py

# ======================= HELPERS SF =======================

def _sf_query_all(sf, soql: str):
    try:
        res = sf.query_all(soql)
        return res.get("records", [])
    except (SalesforceExpiredSession, SalesforceAuthenticationFailed):
        raise HTTPException(status_code=401, detail="Salesforce session expired")
    except SalesforceMalformedRequest as e:
        # Invalid SOQL (field doesn't exist, syntax error, etc.) — log detail internally
        log.error("SF malformed request: %s | soql snippet: %.200s", e, soql)
        raise HTTPException(status_code=400, detail="Salesforce query error: invalid field or filter. Check the filter configuration.")
    except (SalesforceGeneralError, SalesforceRefusedRequest) as e:
        log.error("SF general/refused error: %s", e)
        raise HTTPException(status_code=500, detail="Salesforce returned an error. Please try again.")
    except SalesforceResourceNotFound:
        raise HTTPException(status_code=404, detail="Salesforce resource not found.")
    except Exception as e:
        log.exception("SF query unexpected failure")
        raise HTTPException(status_code=500, detail="Salesforce query failed. Please try again.")

def _get_sf(request: Request):
    signed = request.cookies.get(COOKIE_NAME)
    if not signed:
        raise HTTPException(401, "Not logged in to Salesforce. Please connect via the Salesforce menu.")
    session_id = unsign_value(signed)
    if not session_id:
        raise HTTPException(401, "Salesforce session invalid or expired. Please log in again.")
    sf = get_salesforce_from_session_id(session_id)
    if not sf:
        raise HTTPException(401, "Salesforce session expired. Please log in again.")
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
# _OP_SYNONYM, _OP_MAP, _flatten_filter_rules, _filter_group_to_expr,
# _is_logic_expr, _eval_logic_expr_be, _norm_label_to_key — moved to filter_engine.py


def _strip_account_prefix(field: str) -> str:
    f = field
    if f.startswith("sf."):
        f = f[3:]
    if f.startswith("Account."):
        f = f.split(".", 1)[1]
    return f

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
        f = _safe_field(r.field)
        # Normalize special parent-field to direct lookup where appropriate
        if f == "Account.Id":
            f = "AccountId"
        op_raw = r.operator
        op = _OP_SYNONYM.get(op_raw, op_raw).lower()
        f_no_prefix = f
        ftype = _field_type(f_no_prefix)

        # Guardrail: skip rules where value is empty/None for operators that require a value.
        # Prevents malformed SOQL (e.g. "Stage2 = ''") that causes Salesforce to return 400.
        _needs_value = op not in ("is_empty", "is_not_empty", "is_null", "isnull", "is_not_null", "notnull")
        if _needs_value:
            _val = r.value
            _empty = (_val is None) or (_val == "") or (isinstance(_val, (list, tuple)) and not any(
                str(v).strip() for v in _val if not isinstance(v, (int, float, bool))
            ) and not any(isinstance(v, (int, float, bool)) for v in _val))
            if _empty:
                log.debug("Skipping sf rule with empty value: field=%s op=%s", r.field, op)
                continue

        # Guardrail: AccountId must NOT use LIKE ops (contains/starts/ends) and must not be empty
        if f == "AccountId":
            val_str = str(r.value or "").strip()
            if not val_str:
                # skip empty AccountId filters to avoid LIKE '%%' or invalid operators
                continue
            if op in ("contains","not_contains","starts_with","ends_with"):
                # skip unsupported id operators
                continue

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
            # Special case: equals/not_equals with comma-separated list → IN/NOT IN
            if op in ("equals", "not_equals"):
                raw_val = r.value
                # Accept list/tuple/set or comma-separated string
                is_listy = isinstance(raw_val, (list, tuple, set))
                as_str = str(raw_val or "")
                if is_listy or ("," in as_str):
                    seq = list(raw_val) if is_listy else [p.strip() for p in as_str.split(",")]
                    seq = [x for x in seq if (str(x).strip() if not isinstance(x, (int,float,bool)) else True)]
                    vals_parts: List[str] = []
                    for v in seq:
                        if is_numeric_field(f_no_prefix):
                            vv, ok = _coerce_value_for_field(f_no_prefix, v)
                            vals_parts.append(str(vv) if ok and isinstance(vv, (int,float)) else f"'{_escape_quotes(str(v))}'")
                        elif ftype in DATE_TYPES:
                            vals_parts.append(_soql_quote(v, is_datetime=(ftype=='datetime')))
                        else:
                            vals_parts.append(f"'{_escape_quotes(str(v))}'")
                    vals = ", ".join(vals_parts)
                    clauses.append(f"{f} {'IN' if op=='equals' else 'NOT IN'} ({vals})")
                    continue
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

# ===== Helper: Prefix Account.* rules for WHERE usage =====
def _prefix_account_rules_for_where(rules: List[Rule]) -> List[Rule]:
    out: List[Rule] = []
    for r in rules or []:
        f = r.field or ""
        f = f if f.startswith("Account.") else f"Account.{f}"
        out.append(Rule(field=f, operator=r.operator, value=r.value))
    return out


# ======================= UTILIDADES ACCOUNT =======================

# Country normalisation — imported from canonical module.
# _ISO2_TO_DISPLAY and norm_to_iso2 imported at top of file.

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
    return norm_to_iso2(country)

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

def _safe_int(value: Any) -> Optional[int]:
    if value in (None, "", [], {}):
        return None
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int,)):
            return int(value)
        return int(float(value))
    except Exception:
        return None


def _split_assignment_names(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw)
    return [p.strip() for p in s.split(";") if p and p.strip()]


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
        Account_Inactive__c, Subaccount_Inactive__c,
        Clinical_Site_CS__c, INNODIA_Clinical_Trial_Site__c,
        Referral_Outreach_Site_Non_CTS__c, Elegible_for_DETECT_Site__c
    """

    out: Dict[str, Dict] = {}
    for group in chunk(acc_ids, 150):
        vals = ", ".join(f"'{x}'" for x in group)
        recs = _sf_query_all(sf, f"SELECT {fields} FROM Account WHERE Id IN ({vals})")
        for a in recs:
            inactive = _sf_bool(a.get("Account_Inactive__c")) or _sf_bool(a.get("Subaccount_Inactive__c"))
            if inactive:
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
                "cs": {
                    "clinical": _sf_bool(a.get("INNODIA_Clinical_Trial_Site__c") or a.get("Clinical_Site_CS__c")),
                    "referral": _sf_bool(a.get("Referral_Outreach_Site_Non_CTS__c")),
                    "detect": _sf_bool(a.get("Elegible_for_DETECT_Site__c")),
                },
            }
    return out

# =============== GEOCODING (con cache + persistencia) ===============
# _GEO_CACHE, _GEO_TTL_SECONDS, _geo_key, _save_geo_cache_file,
# _geo_cache_get, _geo_cache_put, _extract_result_country_iso — moved to geo_cache.py

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
        .filter(~Section.name.ilike("PART I%"))
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

        for raw_key, v in (row_data or {}).items():
            k = _normalize_qual_key(raw_key)
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

    # Asegura que todos los campos detectados en el catálogo de preguntas aparecen aunque SiteQual no tenga datos
    catalog_by_key, catalog_by_slug = _build_qual_question_catalog(db)
    for key_full in catalog_by_key.keys():
        if "__" in key_full:
            keys_types.setdefault(key_full, "string")

    # === 3) Etiquetado con textos del cuestionario ===
    subcode_to_group = _build_subcode_to_group(db)
    keys_all = sorted(set(keys_types.keys()) | set(catalog_by_key.keys()))

    # Deduplicate: prefer versioned keys (e.g., '2_1__comments') over base keys (e.g., 'comments')
    # Build a map of base_slug -> best_key to eliminate duplicates at the source
    key_priority_map: Dict[str, str] = {}
    for raw_key in keys_all:
        canonical = _normalize_qual_key(raw_key)
        if canonical == "describe" or "__describe" in canonical:
            continue
        if canonical.upper().startswith("I__"):
            continue
        
        # Extract the base slug (part after __ if present, otherwise the whole key)
        if "__" in canonical:
            base_slug = canonical.split("__", 1)[1]  # e.g., "2_1__comments" -> "comments"
            # Prefer keys with section prefixes (they're more specific)
            if base_slug not in key_priority_map or "__" not in key_priority_map[base_slug]:
                key_priority_map[base_slug] = canonical
        else:
            # Base key without section prefix
            if canonical not in key_priority_map:
                key_priority_map[canonical] = canonical
    
    # Now iterate only through the deduplicated keys
    out: List[Dict[str, Any]] = []
    processed_keys = set()
    for raw_key in keys_all:
        canonical = _normalize_qual_key(raw_key)
        if canonical == "describe" or "__describe" in canonical:
            continue

        if canonical.upper().startswith("I__"):
            continue
        
        # Skip if this is a base key that has a better versioned alternative
        if "__" not in canonical and canonical in key_priority_map:
            if key_priority_map[canonical] != canonical:
                continue  # Skip the base key, we'll use the versioned one

        # Skip old-style JSONB keys that are superseded by canonical section-prefixed keys
        # (the canonical key resolves back to these via _qual_get's 4th fallback)
        if canonical in QUAL_FIELD_BLACKLIST:
            continue

        # Skip if we've already processed this exact key
        if canonical in processed_keys:
            continue
        processed_keys.add(canonical)

        subcode_part, device_part, base_part = _split_canonical_qual_key(canonical)

        meta = catalog_by_key.get(canonical)
        if not meta and base_part:
            candidates = catalog_by_slug.get(base_part) or []
            if len(candidates) == 1:
                meta = candidates[0]
            elif candidates:
                for candidate in candidates:
                    if candidate.get("subcode") == (subcode_part or ""):
                        if not device_part or candidate.get("device") == (device_part or ""):
                            meta = candidate
                            break
                if not meta:
                    meta = candidates[0]

        section_label = (meta.get("section") if meta else "") or ""
        if section_label.upper().startswith("PART I"):
            continue

        t = keys_types.get(raw_key, keys_types.get(canonical, "string"))
        label = None
        group = None

        if meta:
            qtext = (meta.get("question") or "").strip()
            subsection = (meta.get("subsection") or "").strip()
            if qtext:
                label = qtext
            if section_label and subsection:
                group = f"{section_label} › {subsection}"
            elif subsection:
                group = subsection
            elif section_label:
                group = section_label

        if not label or not group:
            pretty_label, pretty_group = _pretty_label_and_group_from_key(canonical, subcode_to_group)
            if not label:
                label = pretty_label
            if not group:
                group = pretty_group

        override_slug_key = base_part or canonical
        override = QUAL_LABEL_OVERRIDES.get(canonical) or QUAL_LABEL_OVERRIDES.get(override_slug_key)
        if isinstance(override, str):
            label = override
        elif isinstance(override, dict):
            label = override.get("label") or label
            want_group = (override.get("_group") or "").strip()
            if want_group:
                group = want_group

        # Ajuste de labels para secciones con dispositivos (temperaturas + prefijos IMP)
        # IMPORTANTE: El parser YA incluye el dispositivo en el question_text con formato:
        #   "<Pretty Device> — <Pregunta>" (ej: "Room Temperature (RT) — Lockable?")
        # Así que NO debemos modificar el label si ya viene del catálogo (meta)
        # Solo aplicamos este ajuste si el label fue generado automáticamente sin meta
        if device_part and not meta:
            dotted_sub = subcode_part.replace("_", ".") if subcode_part else ""
            suffix_map = (qual_parser.DEVICE_TEMPERATURE_SUFFIX or {}).get(dotted_sub, {})
            pretty_name = (qual_parser.DEVICE_PRETTY or {}).get(device_part, _titlecase(device_part))
            suffix = suffix_map.get(device_part, "")
            target_device = f"{pretty_name}{suffix}"
            if label:
                # Si no tiene "—", añadimos el dispositivo
                if " — " not in label:
                    label = f"{target_device} — {label}"
            else:
                label = target_device

        if subcode_part == "3_5_4" and label and not label.upper().startswith("IMP "):
            label = f"IMP {label}"

        item = {"key": f"qual.{canonical}", "label": label, "type": t, "source": "qual"}
        if group:
            item["group"] = group
        if section_label:
            item["qual_section"] = section_label
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
    # Account name — needed for Python-side pass_account() "contains" checks
    "Name": {"type": "string", "label": "Account Name"},
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
        # 1) Trae TODAS las subcuentas clínicas activas (sitios) directamente de Account
        acc_recs = _sf_query_all(sf, f"""
            SELECT Id
            FROM Account
            WHERE RecordType.DeveloperName = 'SubAccount'
              AND C_Type__c = 'Clinical'
              AND (Account_Inactive__c = false OR Account_Inactive__c = null)
              AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)
        """)
        if not acc_recs:
            return []

        acc_ids = [str(a.get("Id")) for a in acc_recs if a.get("Id")]
        acc_map = _build_account_map(sf, acc_ids)
        active_acc_ids = list(acc_map.keys())
        extras_map = batch_fetch_account_extras(sf, active_acc_ids) if active_acc_ids else {}

        # 2) Opcional: enriquecer con Opportunities (para badges y métricas) limitando a esas cuentas
        if active_acc_ids:
            vals = ", ".join(f"'{x}'" for x in active_acc_ids)
            opps = _sf_query_all(sf, f"""
                SELECT Id, Name, Type, StageName, IsClosed, CloseDate, AccountId,
                       C_Number_of_new_T1D_diagnosed_U_18__c, C_Number_of_new_T1D_diagnosed_O_18__c
                FROM Opportunity
                WHERE Type IN ({TYPE_IN}) AND AccountId IN ({vals})
            """)
        else:
            opps = []

        badges: Dict[str, Dict[str, bool]] = {}
        nd_counts: Dict[str, Dict[str, Optional[int]]] = {}
        for o in opps:
            aid = o.get("AccountId")
            t = _norm_type(o.get("Type"))
            if not aid:
                continue
            if aid not in acc_map:
                continue

            if t == "profiling":
                u18 = _safe_int(o.get("C_Number_of_new_T1D_diagnosed_U_18__c"))
                o18 = _safe_int(o.get("C_Number_of_new_T1D_diagnosed_O_18__c"))
                if u18 is not None or o18 is not None:
                    nd = nd_counts.setdefault(aid, {"u18": None, "o18": None})
                    if u18 is not None:
                        nd["u18"] = u18
                    if o18 is not None:
                        nd["o18"] = o18

            b = badges.setdefault(aid, {"profiling": False, "qualification": False})
            if t == "profiling":
                b["profiling"] = True
            elif t == "qualification":
                b["qualification"] = True

        for aid, counts in nd_counts.items():
            if aid in acc_map:
                acc_map[aid].setdefault("nd_counts", counts)

        site_rows = db.execute(
            select(Site.salesforce_account_id, Site.name, Site.city, Site.country, Site.latitude, Site.longitude)
            .where(Site.salesforce_account_id.isnot(None))
        ).all()
        site_by_acc: Dict[str, Dict[str, Any]] = {}
        for sacc, sname, scity, scountry, slat, slng in site_rows:
            site_by_acc[str(sacc)] = {
                "site_name": sname, "city": scity, "country": scountry, "lat": slat, "lng": slng
            }

        out = []
        for aid, acc in acc_map.items():
            if aid in nd_counts:
                acc.setdefault("nd_counts", nd_counts[aid])
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

            extras_entry = (extras_map.get(aid) or {})
            assignments_list = _split_assignment_names(extras_entry.get("extra.AssignmentsNames"))
            assign_count = _safe_int(extras_entry.get("extra.AssignmentsCount"))
            nd_meta = (acc.get("nd_counts") or {})
            # Prefer CS flags coming from extras_map (batch_fetch_account_extras) when available;
            # fall back to any precomputed acc.get('cs'). There are two variants historically
            # used for extra keys (some helpers return keys like "extra.CS_...", others
            # like "extra.Clinical_Site_CS__c" or "extra.INNODIA_Clinical_Trial_Site__c").
            # Be permissive and check multiple possible keys.
            def _get_extra_bool(e: dict, *keys: str) -> bool:
                for k in keys:
                    if k in e and e.get(k) is not None:
                        return bool(e.get(k))
                return False

            cs_from_acc = acc.get("cs") or {}
            cs_from_extra = {
                "clinical": _get_extra_bool(extras_entry, "extra.CS_Clinical_Site_CS__c", "extra.Clinical_Site_CS__c", "extra.INNODIA_Clinical_Trial_Site__c"),
                "referral": _get_extra_bool(extras_entry, "extra.CS_Referral_Outreach_Site__c", "extra.Referral_Outreach_Site_Non_CTS__c"),
                "detect": _get_extra_bool(extras_entry, "extra.CS_Eligible_DETECT__c", "extra.Elegible_for_DETECT_Site__c"),
            }
            # merge, prefer extras when truthy; otherwise fallback to acc map
            cs_flags = {
                "clinical": bool(cs_from_extra.get("clinical") or bool(cs_from_acc.get("clinical"))),
                "referral": bool(cs_from_extra.get("referral") or bool(cs_from_acc.get("referral"))),
                "detect": bool(cs_from_extra.get("detect") or bool(cs_from_acc.get("detect"))),
            }

            out.append({
                "account_id": aid,
                "site":       acc.get("name"),
                "city":       city_out,
                "country":    country_out,
                "latitude":   lat,
                "longitude":  lng,
                "hasQualification": badges.get(aid, {}).get("qualification", False),
                "hasProfiling":     badges.get(aid, {}).get("profiling", False),
                "cs": {
                    "clinical": bool(cs_flags.get("clinical")),
                    "referral": bool(cs_flags.get("referral")),
                    "detect": bool(cs_flags.get("detect")),
                },
                "meta": {
                    "pi_name": extras_entry.get("extra.PIName"),
                    "pi_email": extras_entry.get("extra.PIEmail"),
                    "pi_phone": extras_entry.get("extra.PIPhone"),
                    "assignments": assignments_list,
                    "assignments_count": assign_count,
                    "nd_u18": nd_meta.get("u18"),
                    "nd_o18": nd_meta.get("o18"),
                    # also include CS flags inside meta for compatibility/debugging
                    # Use the real Salesforce API name `INNODIA_Clinical_Trial_Site__c` but
                    # keep legacy fallbacks. IMPORTANT: fall back to the already-computed
                    # `cs_flags` so meta.csContribution cannot contradict the top-level `cs`.
                    "csContribution": {
                        # Ensure meta.csContribution reflects the computed cs_flags so it
                        # cannot contradict the top-level `cs` used by the map. This
                        # prefers explicit extras when they are present AND truthy, but
                        # ultimately coerces to the merged cs_flags booleans to keep
                        # consistency between meta and top-level cs.
                        "INNODIA_Clinical_Trial_Site__c": bool(cs_flags.get("clinical")),
                        "Referral_Outreach_Site_Non_CTS__c": bool(cs_flags.get("referral")),
                        "Elegible_for_DETECT_Site__c": bool(cs_flags.get("detect")),
                    },
                },
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
    # Oculta duplicados que ya existen como columnas fijas (Country/City) y campos no deseados
    sf_fields = [
        f for f in sf_fields
        if (
            f.get("key")
            not in {
                "sf.Account.ShippingCountry",
                "sf.Account.ShippingCity",
                "sf.Account.ShippingLatitude",
                "sf.Account.ShippingLongitude",
                "sf.C_Account_Verified__c",
                "sf.AccountId",
            }
        )
    ]
    # Prefer a single identifier: sf.Account.Id (relationship Id)
    has_acc_id = any((f.get("key") or "") == "sf.Account.Id" for f in sf_fields)
    if not has_acc_id:
        sf_fields = list(sf_fields) + [
            {"key": "sf.Account.Id", "label": "Account Id", "type": "string", "source": "sf", "group": "Salesforce"},
        ]
    # Añade Member (Account) como clave sf.MemberName (desde lookup C_Member__c)
    sf_fields.append({"key": "sf.MemberName", "label": "Member (Account)", "type": "string", "source": "sf", "group": "Salesforce"})

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
        # Hide fields not desired in the table selector
        # Also hide the 3 that duplicate sf.Account.* entries already in the JSON curated catalog
        if name in {"C_Account_Verified__c", "C_Contribution_to_INNODIA__c", "ShippingAddress",
                    "INNODIA_Clinical_Trial_Site__c",  # duplicate of sf.Account.INNODIA_Clinical_Trial_Site__c
                    "Clinical_Site_CS__c",             # duplicate of sf.Account.Clinical_Site_CS__c
                    "Accredited__c"}:                  # duplicate of sf.Account.Accredited__c
            continue
        account_fields_cfg.append({
            "key":   f"Account.{name}",
            "label": meta.get("label") or name.replace("_", " "),
            "type":  meta.get("type") or "string",
            "source":"account",
            "group": "Account",
        })
    # Note: Account.MemberName removed — sf.MemberName (in sf_fields above) covers the same field

    # --- NUEVO: Catálogo extra.* (Salesforce extras batch) ---
    # Estos campos los rellena batch_fetch_account_extras() y se pueden filtrar/mostrar.
    extras_fields = [
        {"key": "extra.PIName",                          "label": "PI Name",                          "type": "string",  "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.PIEmail",                         "label": "PI Email",                         "type": "string",  "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.PIPhone",                         "label": "PI Phone",                         "type": "string",  "source": "extra", "group": "Salesforce Extras"},

        # Metrics
        {"key": "extra.AssignmentsCount",                "label": "Assignments (count)",              "type": "number",  "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.AssignmentsNames",                "label": "Assignments (names)",              "type": "string",  "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.ActivitiesCount",                 "label": "Activities (count)",               "type": "number",  "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.ActivitiesNames",                 "label": "Activities (names)",               "type": "string",  "source": "extra", "group": "Salesforce Extras"},
        {"key": "extra.HasActivity",                     "label": "Has Activity",                      "type": "boolean", "source": "extra", "group": "Salesforce Extras"},
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
    extra_rules: List[Rule] = []

    need_member = False
    need_account_extras = False
    need_batch_extras = False

    # Contador de diagnósticos nuevos por cuenta (se usa más adelante tanto en vecinos como en filas finales)
    nd_counts_by_acc: Dict[str, Dict[str, Optional[int]]] = {}

    def _norm_label_to_key_safe(k: str) -> str:
        try:
            return _norm_label_to_key(k)
        except Exception:
            return k

    # Convert nested sub-groups to expression format BEFORE flattening,
    # so inner AND/OR logic is preserved in the expression evaluator.
    filters = _filter_group_to_expr(filters)

    _flat_rules_raw = _flatten_filter_rules(filters)
    site_rule_indices: List[int] = []
    sf_rule_indices: List[int] = []
    qual_rule_indices: List[int] = []
    account_rule_indices: List[int] = []
    member_rule_indices: List[int] = []
    extra_rule_indices: List[int] = []

    for _orig_idx, r in enumerate(_flat_rules_raw):
        _rule_1idx = _orig_idx + 1
        f = _norm_label_to_key_safe(r.get("field") or "")
        op = r.get("op") or r.get("operator") or "equals"
        val = r.get("value")

        if f in ("sf.Account.MemberName", "Account.MemberName"):
            member_rules.append(Rule(field="sf.Account.MemberName", operator=op, value=val))
            member_rule_indices.append(_rule_1idx)
            need_member = True
            continue
        # (HasPI filtering removed - field deprecated)

        if f.startswith("extra."):
            extra_rules.append(Rule(field=f, operator=op, value=val))
            extra_rule_indices.append(_rule_1idx)
            need_batch_extras = True
        elif f.startswith("site."):
            site_rules.append(Rule(field=f, operator=op, value=val))
            site_rule_indices.append(_rule_1idx)
        elif f.startswith("sf."):
            nf = f[3:]
            if nf.startswith("Account."):
                account_rules.append(Rule(field=nf[8:], operator=op, value=val))
                account_rule_indices.append(_rule_1idx)
                need_account_extras = True
            elif nf.startswith("Assignment."):
                # Assignment__c fields are not queryable in Opportunity SOQL.
                # sf.Assignment.Name → Python-side check against extra.AssignmentsNames
                if nf == "Assignment.Name":
                    _aop = "contains" if op in ("equals", "=") else ("not_contains" if op in ("not_equals", "!=") else op)
                    extra_rules.append(Rule(field="extra.AssignmentsNames", operator=_aop, value=val))
                    extra_rule_indices.append(_rule_1idx)
                    need_batch_extras = True  # must fetch assignment data from SF
                # Other sf.Assignment.* fields (Type, Stage, MCA, Payment) silently skipped
            else:
                sf_rules.append(Rule(field=nf, operator=op, value=val))
                sf_rule_indices.append(_rule_1idx)
        elif f.startswith("Account."):
            account_rules.append(Rule(field=f[8:], operator=op, value=val))
            account_rule_indices.append(_rule_1idx)
            need_account_extras = True
        elif f.startswith("qual."):
            qual_rules.append(Rule(field=f[5:], operator=op, value=val))
            qual_rule_indices.append(_rule_1idx)
        elif f.startswith("Assignment."):
            # Frontend strips "sf." prefix: "sf.Assignment.Name" → "Assignment.Name"
            if f == "Assignment.Name":
                _aop = "contains" if op in ("equals", "=") else ("not_contains" if op in ("not_equals", "!=") else op)
                extra_rules.append(Rule(field="extra.AssignmentsNames", operator=_aop, value=val))
                extra_rule_indices.append(_rule_1idx)
                need_batch_extras = True
        else:
            if f in ALLOWED_FIELDS and not f.startswith("Account."):
                sf_rules.append(Rule(field=f, operator=op, value=val))
                sf_rule_indices.append(_rule_1idx)

    # ── Multi-value AND override for same-field string rules ────
    # When multiple rules target the SAME sf field with string ops (contains,
    # starts_with, etc.) under AND logic, each condition may match a DIFFERENT
    # Opportunity/record for the same Account. A single SOQL WHERE with AND
    # requires ONE record to match all conditions — which is impossible when
    # values live on separate records (e.g., Opp Name "DETECT" vs "Safeguard").
    # Strategy: per unique field with ≥2 string-op rules, run a pre-query per
    # rule to find matching AccountIds, intersect them, then inject the
    # intersection into the main query.
    _raw_logic = filters.get("logic") or "AND"
    _STRING_OPS = {"contains", "not_contains", "starts_with", "ends_with"}
    _pre_query_acc_ids: Optional[Set[str]] = None
    # Only apply for simple AND or all-AND expressions (e.g. "1 AND 2 AND 3").
    # Complex expressions with OR like "(1 AND 2) OR 3" cannot use simple
    # intersection — the OR branch would be incorrectly excluded.
    _is_simple_and = (
        _raw_logic == "AND"
        or (_is_logic_expr(_raw_logic) and not re.search(r"\bOR\b", _raw_logic, re.I))
    )
    if _is_simple_and:
        # Group sf_rules by field — only string-op rules
        _field_groups: Dict[str, List[tuple]] = {}
        for i, r in enumerate(sf_rules):
            if r.operator in _STRING_OPS:
                _field_groups.setdefault(r.field, []).append((i, r))
        # Process grouped rules. For "Name" field: ≥1 rule triggers pre-query
        # (Activity Opps are linked via Assignment__c, not the main SOQL).
        # For other fields: ≥2 rules (multi-value same-field AND).
        _remove_indices: Set[int] = set()
        for _fld, _rules in _field_groups.items():
            _min_rules = 1 if _fld == "Name" else 2
            if len(_rules) < _min_rules:
                continue
            _per_rule_aids: List[Set[str]] = []
            for _idx, _nr in _rules:
                _nw = _build_sf_where(FilterQuery(logic="AND", rules=[_nr]))
                if not _nw:
                    continue
                # 1) Qualification/Profiling Opportunities: AccountId links to site
                _opp_soql = f"SELECT AccountId FROM Opportunity WHERE Type IN ({TYPE_IN}) AND AccountId != null AND ({_nw})"
                # 2) Activity Opportunities: linked to sites via Assignment__c
                _asn_nw = _nw.replace("Name ", "C_Opportunity_Name__r.Name ")
                _asn_soql = f"SELECT C_Account__c FROM Assignment__c WHERE {_asn_nw} AND C_Account__c != null"
                _aids: Set[str] = set()
                try:
                    _opp_recs = await asyncio.to_thread(_sf_query_all, sf, _opp_soql)
                    _aids.update(r.get("AccountId") for r in (_opp_recs or []) if r.get("AccountId"))
                except Exception:
                    pass
                try:
                    _asn_recs = await asyncio.to_thread(_sf_query_all, sf, _asn_soql)
                    _aids.update(r.get("C_Account__c") for r in (_asn_recs or []) if r.get("C_Account__c"))
                except Exception:
                    pass
                if _aids or True:  # always append (empty set = valid "no matches")
                    _per_rule_aids.append(_aids)
            if _per_rule_aids:
                _intersection = _per_rule_aids[0]
                for _s in _per_rule_aids[1:]:
                    _intersection &= _s
                # Merge with any previous pre-query intersection
                if _pre_query_acc_ids is None:
                    _pre_query_acc_ids = _intersection
                else:
                    _pre_query_acc_ids &= _intersection
                _remove_indices.update(i for i, _ in _rules)
        if _remove_indices:
            sf_rules = [r for i, r in enumerate(sf_rules) if i not in _remove_indices]
            sf_rule_indices = [idx for i, idx in enumerate(sf_rule_indices) if i not in _remove_indices]

    merged_rules = sf_rules + _prefix_account_rules_for_where(account_rules)
    # For expression-based logic (e.g. "1 AND (2 OR 3)"), SOQL uses OR (safe over-inclusion)
    # Python-side pass_* functions do exact evaluation using the expression.
    _soql_logic: str = _raw_logic if _raw_logic in ("AND", "OR") else "OR"
    sf_filter = FilterQuery(logic=_soql_logic, rules=merged_rules)

    # -------- Columnas solicitadas --------
    requested_cols: List[str] = []
    requested_account_cols: Set[str] = set()
    requested_extra_cols: Set[str] = set()
    active_acc_ids: List[str] = []  # ensure downstream branches always see a defined list

    for c in (columns or []):
        k = _norm_label_to_key_safe(c)
        requested_cols.append(k)
        if k in ("sf.Account.Member__c", "Account.Member__c", "sf.Account.MemberName", "Account.MemberName"):
            need_member = True
        # HasPI removed from filters/columns
        acct_field = None
        if k.startswith("Account."):
            acct_field = _strip_account_prefix(k)
        elif k.startswith("sf.Account."):
            acct_field = _strip_account_prefix(k)
        if acct_field:
            requested_account_cols.add(acct_field)
            need_account_extras = True
        if k.startswith("extra."):
            requested_extra_cols.add(k)
            need_batch_extras = True

    # ===========================================================
    # 1) OPP “normales” (Qualification / Profiling)
    # ===========================================================
    base_where  = f"Type IN ({TYPE_IN}) AND AccountId != null"
    # Inject pre-query AccountId intersection (multi-Name AND)
    if _pre_query_acc_ids is not None:
        if not _pre_query_acc_ids:
            # Empty intersection → no results possible
            return {"rows": [], "columns": [], "points": []}
        _aid_in = ", ".join(f"'{a}'" for a in sorted(_pre_query_acc_ids))
        base_where += f" AND AccountId IN ({_aid_in})"
    extra_where = _build_sf_where(sf_filter)
    if extra_where and not extra_where.strip().startswith("("):
        extra_where = f"({extra_where})"
    where_sql   = f"WHERE {base_where}" + (f" AND {extra_where}" if extra_where else "")

    opp_fields: Set[str] = {
        "Id","Name","Type","StageName","IsClosed","CloseDate","AccountId",
        # añadimos recordtype para permitir filtros/columnas cuando se trabaje con Activities
        "RecordType.Name","RecordType.DeveloperName",
        "C_Number_of_new_T1D_diagnosed_U_18__c","C_Number_of_new_T1D_diagnosed_O_18__c"
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
    # Run blocking SF HTTP call in a thread so other async tasks aren't starved
    opps = await asyncio.to_thread(_sf_query_all, sf, soql_opps) or []

    # ===========================================================
    # 2) Activities + Assignments -> subcuentas
    # ===========================================================
    # En la org: los "activities" son Opportunities con RecordType = Activity/RT_Activity.
    ACT_RT = ("Activity", "RT_Activity")
    _rt_in = ", ".join("'" + x.replace("'", "\\'") + "'" for x in ACT_RT)

    # Activity SOQL:
    # - When a name/sf filter is applied (extra_where), skip the SubAccount restriction
    #   so sponsor-owned Activities (e.g. "Beta Preserve" linked to Sanofi) are found.
    #   The name filter itself keeps the result set small.
    # - Without a filter, restrict to SubAccount clinical accounts for performance.
    if extra_where:
        act_where = (
            "WHERE AccountId != null "
            "AND (RecordType.DeveloperName IN (" + _rt_in + ") OR Type = 'Activity') "
            f"AND {extra_where}"
        )
    else:
        act_where = (
            "WHERE AccountId != null "
            "AND (RecordType.DeveloperName IN (" + _rt_in + ") OR Type = 'Activity') "
            "AND AccountId IN (SELECT Id FROM Account "
            "  WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
            "    AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
            "    AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
            ")"
        )

    soql_acts = (
        "SELECT Id, Name, AccountId, RecordType.DeveloperName "
        "FROM Opportunity " + act_where
    )
    if debug: print("[/search][SOQL activities]", soql_acts)
    activities = await asyncio.to_thread(_sf_query_all, sf, soql_acts) or []

    # Mapa de nombres de Activity por subcuenta (aunque no existan Assignment__c)
    activities_names_by_acc: Dict[str, List[str]] = defaultdict(list)
    for a in activities:
        aid = str(a.get("AccountId") or "")
        nm = (a.get("Name") or "").strip()
        if not aid or not nm:
            continue
        if nm in activities_names_by_acc[aid]:
            continue
        if len(activities_names_by_acc[aid]) >= 15:
            continue
        activities_names_by_acc[aid].append(nm)

    assignments_by_opp: Dict[str, List[Dict[str, Any]]] = {}
    nd_counts_by_acc: Dict[str, Dict[str, Optional[int]]] = {}

    def _chunks(xs, n=120):
        buf=[]
        for x in xs:
            if x: buf.append(x)
            if len(buf)>=n:
                yield buf; buf=[]
        if buf: yield buf
        
    def _fetch_assignments_map(sf, activity_ids: list[str]) -> tuple[
        Dict[str, list[Dict[str, Any]]],  # by_subaccount[AccountId] = [assignments...]
        Dict[str, list[Dict[str, Any]]],  # by_activity[ActivityId]  = [assignments...]
    ]:
        """
        Lee Assignment__c que enlaza subcuentas con actividades (opportunities RT_Activity).
        Devuelve dos mapas para conveniencia.
        Campos usados (ajusta si tus API names difieren):
        - C_Account__c                   -> Subaccount Id
        - C_Account__r.Name             -> Subaccount Name
        - C_Opportunity_Name__c         -> Activity Opportunity Id
        - C_Opportunity_Name__r.Name    -> Activity Name (trial/program)
        - Status__c (opcional)
        """
        if not activity_ids:
            return {}, {}
        by_sub: Dict[str, list[Dict[str, Any]]] = {}
        by_act: Dict[str, list[Dict[str, Any]]] = {}
        for chunk in _chunks(activity_ids, 120):
            ids_in = ", ".join(f"'{x}'" for x in chunk)
            soql = (
                "SELECT Id, Name, "
                "C_Account__c, C_Account__r.Name, "
                "C_Opportunity_Name__c, C_Opportunity_Name__r.Name "
                "FROM Assignment__c "
                f"WHERE C_Opportunity_Name__c IN ({ids_in})"
            )
            rows = _sf_query_all(sf, soql) or []
            for a in rows:
                sub_id   = a.get("C_Account__c")
                sub_name = (a.get("C_Account__r") or {}).get("Name")
                act_id   = a.get("C_Opportunity_Name__c")
                act_name = (a.get("C_Opportunity_Name__r") or {}).get("Name")
                item = {
                    "assignment_id": a.get("Id"),
                    "assignment_name": a.get("Name"),
                    "activity_id": act_id,
                    "activity_name": act_name,
                    "subaccount_id": sub_id,
                    "subaccount_name": sub_name,
                    "status": a.get("Status__c"),
                }
                if sub_id:
                    by_sub.setdefault(sub_id, []).append(item)
                if act_id:
                    by_act.setdefault(act_id, []).append(item)
        return by_sub, by_act

    assignments_by_sub: Dict[str, list[Dict[str, Any]]] = {}
    assignments_by_act: Dict[str, list[Dict[str, Any]]] = {}
    if activities:
        act_ids = [a["Id"] for a in activities if a.get("Id")]
        assignments_by_sub, assignments_by_act = _fetch_assignments_map(sf, act_ids)
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

    assignments_names_by_acc: Dict[str, List[str]] = {}
    if assignments_by_opp:
        names_map: Dict[str, List[str]] = defaultdict(list)
        for assigns in assignments_by_opp.values():
            for a in assigns:
                acc_id = a.get("account_id")
                if not acc_id:
                    continue
                base = (a.get("name") or "").strip()
                if not base:
                    continue
                act = (a.get("opportunity_name") or "").strip()
                label = f"{base} ({act})" if act else base
                key = str(acc_id)
                bucket = names_map.setdefault(key, [])
                if label in bucket:
                    continue
                if len(bucket) >= 15:
                    continue
                bucket.append(label)
        assignments_names_by_acc = names_map

    for o in opps:
        aid = o.get("AccountId")
        if not aid:
            continue
        if _norm_type(o.get("Type")) != "profiling":
            continue
        u18 = _safe_int(o.get("C_Number_of_new_T1D_diagnosed_U_18__c"))
        o18 = _safe_int(o.get("C_Number_of_new_T1D_diagnosed_O_18__c"))
        if u18 is None and o18 is None:
            continue
        bucket = nd_counts_by_acc.setdefault(aid, {"u18": None, "o18": None})
        if u18 is not None:
            bucket["u18"] = u18
        if o18 is not None:
            bucket["o18"] = o18

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

    acc_map = await asyncio.to_thread(_build_account_map, sf, acc_ids)
    # Lista de cuentas activas (IDs) usada en pasos posteriores. Se declara
    # aquí para garantizar que la variable exista en todos los bloques
    # posteriores que la referencian (evita UnboundLocalError si no se
    # inicializaba en alguna rama del flujo).
    active_acc_ids = sorted(acc_map.keys())

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

    # HasPI / PI-account derived data removed: not querying AccountContactRelation here

    account_fields_needed: Set[str] = {r.field for r in account_rules}
    account_fields_needed |= requested_account_cols
    supported = set(ACCOUNT_EXTRA_FIELDS.keys()) | {"MemberName", "ShippingAddress"}
    fields_to_fetch = {f for f in account_fields_needed if f in supported and f not in {"MemberName"}}

    account_extras_by_acc: Dict[str, Dict[str, Any]] = {}
    if need_account_extras and acc_ids and fields_to_fetch:
        account_extras_by_acc = _fetch_account_extras(sf, acc_ids, fields_to_fetch)

    extras_map: Dict[str, Dict[str, Any]] = {}
    if need_batch_extras and acc_ids:
        extras_map = await asyncio.to_thread(batch_fetch_account_extras, sf, list({x for x in acc_ids if x}))
    if assignments_names_by_acc:
        for acc_id, names in assignments_names_by_acc.items():
            if not names:
                continue
            entry = extras_map.setdefault(str(acc_id), {})
            entry["extra.AssignmentsNames"] = "; ".join(names)
            entry.setdefault("extra.AssignmentsCount", len(names))
    # Injeta también Activities (sin Assignment)
    if activities_names_by_acc:
        for acc_id, names in activities_names_by_acc.items():
            if not names:
                continue
            entry = extras_map.setdefault(str(acc_id), {})
            entry["extra.ActivitiesNames"] = "; ".join(names)
            entry.setdefault("extra.ActivitiesCount", len(names))
            entry.setdefault("extra.HasActivity", True)

    # ===========================================================
    # 6) Helpers de filtrado post-query
    # ===========================================================
    def pass_site(acc: Dict[str, Any]) -> bool:
        if not site_rules:
            return True
        def match_one(rule: Rule) -> bool:
            val = ""
            if rule.field == "site.city":    val = (acc.get("city") or "")
            if rule.field == "site.country": val = (acc.get("country") or "")
            op = _op_norm(rule.operator); s = _str(rule.value)
            # is empty / is not empty
            if op in ("is_null", "isnull", "is_empty"):
                return val == ""
            if op in ("is_not_null", "notnull", "is_not_empty"):
                return val != ""
            # Normalize country values to ISO for equality/set operators
            if rule.field == "site.country" and op in ("equals", "=", "not_equals", "!=", "in", "not_in"):
                val = (_country_norm(val) or val).upper()
                if op not in ("in", "not_in"):
                    s = (_country_norm(s) or s).upper()
            # in / not_in (comma-separated values)
            if op in ("in", "not_in"):
                raw_items = [x.strip() for x in str(rule.value or "").split(",") if x.strip()]
                if rule.field == "site.country":
                    items = [(_country_norm(x) or x).upper() for x in raw_items]
                    present = val.upper() in items
                else:
                    items = [x.lower() for x in raw_items]
                    present = val.lower() in items
                return present if op == "in" else not present
            # For site.country string ops also match against English display name
            # so "Germany" / "Ger" / "ger" all match the stored ISO2 "DE".
            if rule.field == "site.country" and op in (
                "equals","=","not_equals","!=","contains","not_contains","starts_with","ends_with"
            ):
                iso = (_country_norm(val) or val).upper()
                display = _ISO2_TO_DISPLAY.get(iso, iso)
                s_iso = (_country_norm(s) or "").upper()
                # Normalize search string to ISO too if it's a full country name
                s_norm = s_iso if s_iso else s
                def _country_match(v: str, needle: str) -> bool:
                    vl, nl = v.lower(), needle.lower()
                    # For partial-match ops, use the raw (un-normalized) search string
                    # so "Italy" doesn't become "it" and falsely match "Switzerland" / "Lithuania"
                    raw = s.lower()
                    if   op in ("equals","="):       return iso.lower() == nl or display.lower() == nl
                    elif op in ("not_equals","!="):  return iso.lower() != nl and display.lower() != nl
                    elif op == "contains":           return raw in iso.lower() or raw in display.lower()
                    elif op == "not_contains":       return raw not in iso.lower() and raw not in display.lower()
                    elif op == "starts_with":        return iso.lower().startswith(raw) or display.lower().startswith(raw)
                    elif op == "ends_with":          return iso.lower().endswith(raw) or display.lower().endswith(raw)
                    return True
                return _country_match(iso, s_norm)
            if   op in ("equals","="):      return val.lower() == s.lower()
            elif op in ("not_equals","!="): return val.lower() != s.lower()
            elif op == "contains":          return s.lower() in val.lower()
            elif op == "not_contains":      return s.lower() not in val.lower()
            elif op == "starts_with":       return val.lower().startswith(s.lower())
            elif op == "ends_with":         return val.lower().endswith(s.lower())
            return True
        logic_str = filters.get("logic") or "AND"
        res = [match_one(r) for r in site_rules]
        if _is_logic_expr(logic_str):
            # Expression-based: build per-rule results dict and evaluate
            expr_results = {site_rule_indices[i]: res[i] for i in range(len(site_rules))}
            # Multi-country override: a site can't be in two countries simultaneously,
            # so multiple country rules are always OR-combined (same as AND-mode below).
            country_pos = [i for i, r in enumerate(site_rules) if r.field == "site.country"]
            if len(country_pos) > 1:
                country_pass = any(res[i] for i in country_pos)
                for i in country_pos:
                    expr_results[site_rule_indices[i]] = country_pass
            return _eval_logic_expr_be(logic_str, expr_results)
        # Multiple "site.country equals X" rules under AND logic is semantically nonsensical
        # (a site can't be in two countries). Treat country rules as OR, city rules as AND.
        glue_and = logic_str == "AND"
        if glue_and:
            country_idx = [i for i, r in enumerate(site_rules) if r.field == "site.country"]
            if len(country_idx) > 1:
                country_pass = any(res[i] for i in country_idx)
                other_pass = all(res[i] for i in range(len(res)) if i not in set(country_idx))
                return country_pass and other_pass
        return all(res) if glue_and else any(res)

    def pass_qual(qual_data: Dict[str, Any]) -> bool:
        if not qual_rules:
            return True
        results = [_eval_qual_rule(_qual_get(qual_data, qr.field), qr.operator, qr.value)
                   for qr in qual_rules]
        logic_str = filters.get("logic") or "AND"
        if _is_logic_expr(logic_str):
            expr_results = {qual_rule_indices[i]: results[i] for i in range(len(qual_rules))}
            return _eval_logic_expr_be(logic_str, expr_results)
        glue_and = logic_str == "AND"
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
        res = [eval_rule_text(actual, r) for r in member_rules]
        logic_str = filters.get("logic") or "AND"
        if _is_logic_expr(logic_str):
            expr_results = {member_rule_indices[i]: res[i] for i in range(len(member_rules))}
            return _eval_logic_expr_be(logic_str, expr_results)
        glue_and = logic_str == "AND"
        return all(res) if glue_and else any(res)

    # pass_haspi removed: HasPI filtering deprecated/removed

    def pass_extra(aid: str) -> bool:
        if not extra_rules:
            return True
        vals = extras_map.get(str(aid), {}) if extras_map else {}
        res = []
        for er in extra_rules:
            actual = vals.get(er.field)
            res.append(_eval_qual_rule(actual, er.operator, er.value))
        logic_str = filters.get("logic") or "AND"
        if _is_logic_expr(logic_str):
            expr_results = {extra_rule_indices[i]: res[i] for i in range(len(extra_rules))}
            return _eval_logic_expr_be(logic_str, expr_results)
        glue_and = logic_str == "AND"
        return all(res) if glue_and else any(res)

    def pass_account(aid: str) -> bool:
        if not account_rules:
            return True
        vals = dict(account_extras_by_acc.get(str(aid), {}))
        vals.setdefault("Id", str(aid))  # sf.Account.Id filter — always available
        if need_member:
            vals["MemberName"] = member_by_acc.get(str(aid))
        res = [_eval_qual_rule(vals.get(ar.field), ar.operator, ar.value) for ar in account_rules]
        logic_str = filters.get("logic") or "AND"
        if _is_logic_expr(logic_str):
            expr_results = {account_rule_indices[i]: res[i] for i in range(len(account_rules))}
            return _eval_logic_expr_be(logic_str, expr_results)
        glue_and = logic_str == "AND"
        return all(res) if glue_and else any(res)

    # Helper: applies all Python-side pass_* checks respecting root AND/OR logic.
    # For AND (default): all categories must pass.
    # For OR: at least one category with actual rules must pass.
    _root_logic = filters.get("logic") or "AND"
    _root_and = _root_logic == "AND"
    def passes_row_checks(acc: Dict[str, Any], qual_data: Dict[str, Any], aid: str) -> bool:
        # Expression mode: each pass_* evaluates its category rules using the expression.
        # SF rules are pre-filtered by SOQL → treated as True for any surviving row.
        # This correctly handles same-category expressions; cross-category defaults to AND.
        if _is_logic_expr(_root_logic):
            return (pass_site(acc) and pass_qual(qual_data) and
                    pass_member(aid) and pass_account(aid) and pass_extra(aid))
        if _root_and:
            return (pass_site(acc) and pass_qual(qual_data) and
                    pass_member(aid) and pass_account(aid) and pass_extra(aid))
        checks = []
        if site_rules:    checks.append(pass_site(acc))
        if qual_rules:    checks.append(pass_qual(qual_data))
        if member_rules:  checks.append(pass_member(aid))
        if account_rules: checks.append(pass_account(aid))
        if extra_rules:   checks.append(pass_extra(aid))
        return any(checks) if checks else True

    # ===========================================================
    # 7) Construcción de filas de OPP normales
    # ===========================================================
    rows = []
    for o in opps:
        aid = o.get("AccountId")
        if not aid:             continue
        if aid not in acc_map:  continue  # filtradas por inactividad
        acc = acc_map.get(aid) or {}
        site_info = site_by_acc.get(str(aid))
        site_id   = site_info["site_id"] if site_info else None
        qual_data = qual_by_site.get(site_id, {}) if site_id else {}
        if not passes_row_checks(acc, qual_data, aid): continue

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
                # HasPI removed
                else:
                    # First from direct Account extras fetch
                    val = (account_extras_by_acc.get(str(aid), {}) or {}).get(sub)
                    # Fallback to batch extras (same booleans used in the map)
                    if val is None and sub in {"INNODIA_Clinical_Trial_Site__c","Referral_Outreach_Site_Non_CTS__c","Elegible_for_DETECT_Site__c","Clinical_Site_CS__c"}:
                        ex = (extras_map.get(str(aid), {}) or {})
                        val = ex.get(f"extra.{sub}")
                    data[k] = val
                continue

            # qual.*
            if k.startswith("qual."):
                data[k] = _qual_get(qual_data, k[5:])
                continue

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
                data[k] = (extras_map.get(str(aid), {}) or {}).get(k)
                continue

            data[k] = o.get(k)

        names_for_acc = assignments_names_by_acc.get(str(aid), [])
        if names_for_acc:
            data.setdefault("extra.AssignmentsNames", "; ".join(names_for_acc))
            data.setdefault("extra.AssignmentsCount", len(names_for_acc))
        act_for_acc = activities_names_by_acc.get(str(aid), [])
        if act_for_acc:
            data.setdefault("extra.ActivitiesNames", "; ".join(act_for_acc))
            data.setdefault("extra.ActivitiesCount", len(act_for_acc))
            data.setdefault("extra.HasActivity", True)

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
                site_info = site_by_acc.get(str(aid))
                site_id   = site_info["site_id"] if site_info else None
                qual_data = qual_by_site.get(site_id, {}) if site_id else {}
                if not passes_row_checks(acc, qual_data, aid): continue

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
                        elif sub in {"HasPI"}:             data[k] = None
                        else:
                            val = (account_extras_by_acc.get(str(aid), {}) or {}).get(sub)
                            if val is None and sub in {"INNODIA_Clinical_Trial_Site__c","Referral_Outreach_Site_Non_CTS__c","Elegible_for_DETECT_Site__c","Clinical_Site_CS__c"}:
                                ex = (extras_map.get(str(aid), {}) or {})
                                val = ex.get(f"extra.{sub}")
                            data[k] = val
                        continue
                    if k.startswith("qual."):   data[k] = _qual_get(qual_data, k[5:]); continue
                    if k.startswith("extra."):
                        data[k] = (extras_map.get(str(aid), {}) or {}).get(k)
                        continue
                    if k.startswith("sf."):
                        fld = k[3:]
                        # Mostrar el nombre de la Activity cuando se pide [sf] Opportunity Name
                        if fld == "Name":
                            data[k] = a.get("opportunity_name")
                        else:
                            data[k] = proxy.get(fld) if proxy else None
                        continue
                    data[k] = None
                names_for_acc = assignments_names_by_acc.get(str(aid), [])
                if names_for_acc:
                    data.setdefault("extra.AssignmentsNames", "; ".join(names_for_acc))
                    data.setdefault("extra.AssignmentsCount", len(names_for_acc))
                    data.setdefault("extra.HasActivity", True)

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

        # 9b) Filas sintéticas por subcuentas con Activities aunque no haya Assignment
        if activities:
            accs_with_acts = {str(a.get("AccountId")) for a in activities if a.get("AccountId")}
            for aid in sorted(accs_with_acts):
                if not aid or aid in present:  # ya añadida arriba
                    continue
                if aid not in acc_map:
                    continue  # filtradas por inactividad
                acc = acc_map.get(aid) or {}
                site_info = site_by_acc.get(str(aid))
                site_id   = site_info["site_id"] if site_info else None
                qual_data = qual_by_site.get(site_id, {}) if site_id else {}
                if not passes_row_checks(acc, qual_data, aid): continue

                # Proxy SF fields from most recent non-Activity, if requested
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
                        else:
                            data[k] = (account_extras_by_acc.get(str(aid), {}) or {}).get(sub)
                        continue
                    if k.startswith("qual."):   data[k] = _qual_get(qual_data, k[5:]); continue
                    if k.startswith("extra."):
                        data[k] = (extras_map.get(str(aid), {}) or {}).get(k)
                        continue
                    if k.startswith("sf."):
                        fld = k[3:]
                        if fld == "Name":
                            # usa el primer activity name como nombre de Opp
                            data[k] = (activities_names_by_acc.get(str(aid)) or [None])[0]
                        else:
                            data[k] = proxy.get(fld) if proxy else None
                        continue
                    data[k] = None

                # Añade etiquetas de Activities
                names_for_acc = activities_names_by_acc.get(str(aid), [])
                if names_for_acc:
                    data.setdefault("extra.ActivitiesNames", "; ".join(names_for_acc))
                    data.setdefault("extra.ActivitiesCount", len(names_for_acc))

                _flatten_sf_inplace(data)

                rows.append({
                    "account_id": aid,
                    "account_name": acc.get("name"),
                    "country": acc.get("country"),
                    "city": acc.get("city"),
                    "opportunity_type": "Activity",
                    "data": data,
                })
                present.add(aid)

    # ===========================================================
    # 10) Colapso por subcuenta y markers
    # ===========================================================
    # --- Prefetch maps for Account.* and extra.* so we can inject values even if not used in WHERE ---
    account_extras_map: Dict[str, Dict[str, Any]] = {}
    if need_account_extras and requested_account_cols:
        # Filter out virtual/derived fields (MemberName, HasPI) which are not
        # real Account columns in Salesforce and would break the SOQL SELECT.
        # Only request keys that are present in ACCOUNT_EXTRA_FIELDS or the
        # special composed ShippingAddress.
        safe_fields = {f for f in requested_account_cols if f in ACCOUNT_EXTRA_KEYS or f == "ShippingAddress"}
        if safe_fields:
            try:
                account_extras_map = _fetch_account_extras(sf, active_acc_ids, safe_fields)
            except Exception as e:
                log.warning("/explorer/search: _fetch_account_extras failed: %s", e)
                account_extras_map = {}

    extra_batch_map: Dict[str, Dict[str, Any]] = {}
    if need_batch_extras and active_acc_ids:
        try:
            extra_batch_map = batch_fetch_account_extras(sf, active_acc_ids) or {}
        except Exception as e:
            log.warning("/explorer/search: batch_fetch_account_extras failed: %s", e)
            extra_batch_map = {}
    if assignments_names_by_acc:
        for acc_id, names in assignments_names_by_acc.items():
            if not names:
                continue
            entry = extra_batch_map.setdefault(str(acc_id), {})
            entry["extra.AssignmentsNames"] = "; ".join(names)
            entry.setdefault("extra.AssignmentsCount", len(names))
    if activities_names_by_acc:
        for acc_id, names in activities_names_by_acc.items():
            if not names:
                continue
            entry = extra_batch_map.setdefault(str(acc_id), {})
            entry["extra.ActivitiesNames"] = "; ".join(names)
            entry.setdefault("extra.ActivitiesCount", len(names))

    rows = collapse_rows_by_account(rows)

    # --- Inject Account.* + extra.* + Member into returned rows ---
    if rows:
        for r in rows:
            aid = str(r.get("account_id") or "")
            if not aid:
                continue
            r.setdefault("data", {})
            data = r["data"]

            # 1) Account.* columns explicitly requested
            if requested_account_cols:
                acc_vals = (account_extras_map.get(aid, {}) or {})
                for f in requested_account_cols:
                    # Keep the exact front-end key format: "Account.<Field>"
                    data[f"Account.{f}"] = acc_vals.get(f)

            # 2) extra.* columns explicitly requested
            if requested_extra_cols:
                ex_vals = (extra_batch_map.get(aid, {}) or {})
                for full_key in requested_extra_cols:
                    fetched = ex_vals.get(full_key)
                    if fetched is not None:
                        if full_key not in data or data.get(full_key) in (None, "", []):
                            data[full_key] = fetched
                    else:
                        if full_key not in data:
                            data[full_key] = None

            # 3) MemberName if requested
            if need_member:
                m = (extra_batch_map.get(aid, {}) or {}).get("extra.MemberName")
                if m is None:
                    m = member_by_acc.get(aid)
                data["Account.MemberName"] = m
                data["sf.MemberName"] = m
                # also expose as extra.MemberName if not present
                if "extra.MemberName" not in data:
                    data["extra.MemberName"] = m

            # 3b) Always expose Account Id in both notations for table compatibility
            data.setdefault("Account.Id", aid)
            data.setdefault("sf.Account.Id", aid)

            names_for_acc = assignments_names_by_acc.get(aid, [])
            if names_for_acc:
                data.setdefault("extra.AssignmentsNames", "; ".join(names_for_acc))
                data.setdefault("extra.AssignmentsCount", len(names_for_acc))
            acts_for_acc = activities_names_by_acc.get(aid, [])
            if acts_for_acc:
                data.setdefault("extra.ActivitiesNames", "; ".join(acts_for_acc))
                data.setdefault("extra.ActivitiesCount", len(acts_for_acc))

    badges: Dict[str, Dict[str, bool]] = {}
    for r in rows:
        aid = r.get("account_id")
        if not aid:
            continue
        b = badges.setdefault(aid, {"profiling": False, "qualification": False})
        types_str = (r.get("opportunity_types") or "").lower()
        if "profiling" in types_str:     b["profiling"] = True
        if "qualification" in types_str: b["qualification"] = True

    acc_ids_final = [r["account_id"] for r in rows if r.get("account_id")]
    acc_map_final = _build_account_map(sf, acc_ids_final)
    for aid, counts in nd_counts_by_acc.items():
        if aid in acc_map_final:
            acc_map_final[aid].setdefault("nd_counts", counts)
    map_extra = extra_batch_map if extra_batch_map else (
        batch_fetch_account_extras(sf, acc_ids_final) if acc_ids_final else {}
    )
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
        str_aid = str(aid) if aid is not None else ""
        extras_entry = (map_extra.get(str_aid) or map_extra.get(aid) or {})

        def _extra_bool(entry: Dict[str, Any], *keys: str) -> bool:
            for key in keys:
                if key in entry and entry.get(key) is not None:
                    return bool(entry.get(key))
            return False

        cs_from_acc = accd.get("cs") or {}
        cs_from_extra = {
            "clinical": _extra_bool(
                extras_entry,
                "extra.CS_Clinical_Site_CS__c",
                "extra.Clinical_Site_CS__c",
                "extra.INNODIA_Clinical_Trial_Site__c",
            ),
            "referral": _extra_bool(
                extras_entry,
                "extra.CS_Referral_Outreach_Site__c",
                "extra.Referral_Outreach_Site_Non_CTS__c",
            ),
            "detect": _extra_bool(
                extras_entry,
                "extra.CS_Eligible_DETECT__c",
                "extra.Elegible_for_DETECT_Site__c",
            ),
        }
        cs_flags = {
            "clinical": bool(cs_from_extra.get("clinical") or cs_from_acc.get("clinical")),
            "referral": bool(cs_from_extra.get("referral") or cs_from_acc.get("referral")),
            "detect": bool(cs_from_extra.get("detect") or cs_from_acc.get("detect")),
        }

        assignments_list = _split_assignment_names(extras_entry.get("extra.AssignmentsNames"))
        assign_count = _safe_int(extras_entry.get("extra.AssignmentsCount"))
        nd_meta = accd.get("nd_counts") or {}

        points.append({
            "lat": lat, "lng": lng,
            "account_id": aid,
            "account_name": accd.get("name") or r.get("account_name"),
            "city": accd.get("city") or r.get("city"),
            "country": accd.get("country") or r.get("country"),
            "badges": badges.get(aid, {"profiling": False, "qualification": False}),
            "cs": {
                "clinical": bool(cs_flags.get("clinical")),
                "referral": bool(cs_flags.get("referral")),
                "detect": bool(cs_flags.get("detect")),
            },
            "meta": {
                "pi_name": extras_entry.get("extra.PIName"),
                "pi_email": extras_entry.get("extra.PIEmail"),
                "pi_phone": extras_entry.get("extra.PIPhone"),
                "assignments": assignments_list,
                "assignments_count": assign_count,
                "nd_u18": nd_meta.get("u18"),
                "nd_o18": nd_meta.get("o18"),
                "csContribution": {
                    "INNODIA_Clinical_Trial_Site__c": bool(cs_flags.get("clinical")),
                    "Referral_Outreach_Site_Non_CTS__c": bool(cs_flags.get("referral")),
                    "Elegible_for_DETECT_Site__c": bool(cs_flags.get("detect")),
                },
            },
        })

    # Build columns metadata from requested_cols (or auto-detect from first row)
    def _col_label(key: str) -> str:
        """Generate a human-readable label from a field key."""
        s = key
        for pfx in ("sf.Account.", "sf.", "qual.", "site.", "extra.", "account_"):
            if s.startswith(pfx):
                s = s[len(pfx):]
                break
        # Strip trailing __c
        s = re.sub(r"__c$", "", s)
        # Strip section prefixes like "3_6__"
        s = re.sub(r"^\d+_\d+_*\d*__", "", s)
        return s.replace("_", " ").strip().title()

    if requested_cols:
        _always = [
            {"key": "account_name", "label": "Site", "type": "text"},
            {"key": "country",      "label": "Country", "type": "text"},
            {"key": "city",         "label": "City", "type": "text"},
        ]
        _req_meta = [
            {"key": k, "label": _col_label(k), "type": "text"}
            for k in requested_cols
            if k not in {"account_name", "country", "city"}
        ]
        _col_meta = _always + _req_meta
    elif rows:
        # Auto-detect from first row's data keys
        _always = [
            {"key": "account_name", "label": "Site", "type": "text"},
            {"key": "country",      "label": "Country", "type": "text"},
            {"key": "city",         "label": "City", "type": "text"},
        ]
        _data_keys = list((rows[0].get("data") or {}).keys())
        _col_meta = _always + [
            {"key": k, "label": _col_label(k), "type": "text"} for k in _data_keys
        ]
    else:
        _col_meta = []

    resp = {"points": points, "rows": rows, "columns": _col_meta}
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
                "columns": ["sf.StageName","sf.CloseDate","qual.xxx","extra.MemberName"]
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
    requested_account_cols: Set[str] = set()
    requested_extra_cols: Set[str] = set()
    need_member = any(k in ("sf.Account.MemberName","Account.MemberName","Account.Member__c","sf.Account.Member__c") for k in requested_cols)
    need_account_extras = False
    need_batch_extras   = False
    nd_counts_by_acc: Dict[str, Dict[str, Optional[int]]] = {}

    for k in requested_cols:
        if k.startswith("Account.") or k.startswith("sf.Account."):
            fld = _strip_account_prefix(k)
            if fld:
                requested_account_cols.add(fld)
                need_account_extras = True
        if k.startswith("extra."):
            requested_extra_cols.add(k)
            need_batch_extras = True

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
        sid: (
            lambda _d: _expand_describe_keys_for_row(
                _expand_comments_keys_for_row(_d, groups_by_slug),
                groups_by_slug
            )
        )((data or {}))
        for sid, data in qual_rows
    }

    # Member / HasPI
    member_by_acc: Dict[str, Optional[str]] = {}
    if need_member:
        vals2 = ",".join(f"'{x}'" for x in active_acc_ids)
        rows = _sf_query_all(sf, f"SELECT Id, C_Member__c, C_Member__r.Name FROM Account WHERE Id IN ({vals2})")
        for a in rows:
            member_by_acc[str(a.get("Id"))] = (a.get("C_Member__r") or {}).get("Name") or None

    # PI/account derived data (HasPI) removed — we don't query AccountContactRelation here

    # Extras de Account (Account.* configurados)
    account_fields_needed: Set[str] = set()
    for k in requested_cols:
        if k.startswith("Account.") or k.startswith("sf.Account."):
            acct_field = _strip_account_prefix(k)
            if acct_field:
                account_fields_needed.add(acct_field)
    supported = set(ACCOUNT_EXTRA_FIELDS.keys()) | {"MemberName","ShippingAddress"}
    fields_to_fetch = {f for f in account_fields_needed if f in supported and f not in {"MemberName"}}
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

        if _norm_type(o.get("Type")) == "profiling":
            u18 = _safe_int(o.get("C_Number_of_new_T1D_diagnosed_U_18__c"))
            o18 = _safe_int(o.get("C_Number_of_new_T1D_diagnosed_O_18__c"))
            if u18 is not None or o18 is not None:
                bucket = nd_counts_by_acc.setdefault(aid, {"u18": None, "o18": None})
                if u18 is not None:
                    bucket["u18"] = u18
                if o18 is not None:
                    bucket["o18"] = o18

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
                elif sub == "HasPI":            data[k] = None
                else:
                    data[k] = (account_extras_by_acc.get(str(aid), {}) or {}).get(sub)
                continue

            # qual.*
            if k.startswith("qual."):
                data[k] = _qual_get(qual_data, k[5:])
                continue

            # sf.*
            if k.startswith("sf."):
                fld = k[3:]
                if _exists_on_opportunity(fld):
                    data[k] = o.get(fld)
                elif _exists_on_account(fld):
                    data[k] = (o.get("Account") or {}).get(fld)
                elif fld == "MemberName":
                    # Lookup name from Account.C_Member__r.Name (pre-fetched into member_by_acc or extras)
                    data[k] = member_by_acc.get(str(aid)) or (extras_map.get(str(aid), {}) or {}).get("extra.MemberName")
                else:
                    data[k] = None
                continue

            # extra.*
            if k.startswith("extra."):
                data[k] = (extras_map.get(str(aid), {}) or {}).get(k); continue

            # fallback
            data[k] = o.get(k)

        # Normaliza sf.* embebido si viniera como objeto
        names_for_acc = (extras_map.get(str(aid), {}) or {}).get("extra.AssignmentsNames")
        if names_for_acc and isinstance(names_for_acc, str):
            data.setdefault("extra.AssignmentsNames", names_for_acc)
            try:
                count_val = len([x for x in names_for_acc.split(";") if x.strip()])
            except Exception:
                count_val = None
            if count_val is not None:
                data.setdefault("extra.AssignmentsCount", count_val)
        _flatten_sf_inplace(data)

        rows_raw.append({
            "account_id": aid,
            "account_name": acc.get("name"),
            "country": acc.get("country"),
            "city": acc.get("city"),
            "opportunity_type": _norm_type(o.get("Type")),
            "data": data,
        })


    # --- Prefetch maps for Account.* and extra.* so we can inject values even if not used in WHERE ---
    account_extras_map: Dict[str, Dict[str, Any]] = {}
    if need_account_extras and requested_account_cols:
        try:
            safe_fields = {f for f in requested_account_cols if f in ACCOUNT_EXTRA_FIELDS or f == "ShippingAddress"}
            if safe_fields:
                account_extras_map = _fetch_account_extras(sf, active_acc_ids, safe_fields)
            else:
                account_extras_map = {}
        except Exception as e:
            log.warning("/explorer/search: _fetch_account_extras failed: %s", e)
            account_extras_map = {}

    extra_batch_map: Dict[str, Dict[str, Any]] = {}
    if need_batch_extras and active_acc_ids:
        try:
            extra_batch_map = batch_fetch_account_extras(sf, active_acc_ids) or {}
        except Exception as e:
            log.warning("/explorer/search: batch_fetch_account_extras failed: %s", e)
            extra_batch_map = {}

    # Colapsa por cuenta para devolver UNA fila por cuenta
    rows = collapse_rows_by_account(rows_raw)
    
    # --- Inject Account.* + extra.* + Member/HasPI into returned rows ---
    if rows:
        for r in rows:
            aid = str(r.get("account_id") or "")
            if not aid:
                continue
            r.setdefault("data", {})
            data = r["data"]

            # 1) Account.* columns explicitly requested
            if requested_account_cols:
                acc_vals = (account_extras_map.get(aid, {}) or {})
                for f in requested_account_cols:
                    # Keep the exact front-end key format: "Account.<Field>"
                    data[f"Account.{f}"] = acc_vals.get(f)

            # 2) extra.* columns explicitly requested
            if requested_extra_cols:
                ex_vals = (extra_batch_map.get(aid, {}) or {})
                for full_key in requested_extra_cols:
                    fetched = ex_vals.get(full_key)
                    if fetched is None:
                        # Fallback mirror for Account CS booleans so filters work even if requested via extra.* or Account.*
                        if full_key in {"extra.INNODIA_Clinical_Trial_Site__c","extra.Referral_Outreach_Site_Non_CTS__c","extra.Elegible_for_DETECT_Site__c"}:
                            # derive from acc_map.cs flags when extras didn’t return explicitly
                            fld = full_key.split(".",1)[1]
                            fetched = (extra_batch_map.get(aid, {}) or {}).get(full_key)
                            if fetched is None:
                                cs_flags = (acc_map.get(aid, {}) or {}).get("cs") or {}
                                if fld == "INNODIA_Clinical_Trial_Site__c": fetched = bool(cs_flags.get("clinical"))
                                if fld == "Referral_Outreach_Site_Non_CTS__c": fetched = bool(cs_flags.get("referral"))
                                if fld == "Elegible_for_DETECT_Site__c": fetched = bool(cs_flags.get("detect"))
                    if fetched is not None:
                        if full_key not in data or data.get(full_key) in (None, "", []):
                            data[full_key] = fetched
                    else:
                        if full_key not in data:
                            data[full_key] = None

            # 3) MemberName if requested
            if need_member:
                m = (extra_batch_map.get(aid, {}) or {}).get("extra.MemberName")
                if m is None:
                    m = member_by_acc.get(aid)
                data["Account.MemberName"] = m
                data["sf.MemberName"] = m
                # also expose as extra.MemberName if not present
                if "extra.MemberName" not in data:
                    data["extra.MemberName"] = m

            # 3b) Always expose Account Id in both notations for table compatibility
            # Force-set to avoid previous None values from earlier branches
            data["Account.Id"] = aid
            data["sf.Account.Id"] = aid

            names_val = (extra_batch_map.get(aid, {}) or {}).get("extra.AssignmentsNames")
            if names_val:
                data.setdefault("extra.AssignmentsNames", names_val)
                count_val = (extra_batch_map.get(aid, {}) or {}).get("extra.AssignmentsCount")
                if count_val is None:
                    try:
                        count_val = len([x for x in str(names_val).split(";") if x.strip()])
                    except Exception:
                        count_val = None
                if count_val is not None:
                    data.setdefault("extra.AssignmentsCount", count_val)


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

    # --- Construye 'points' para el mapa con las cuentas filtradas ---
    acc_ids_final = [rr["account_id"] for rr in trimmed if rr.get("account_id")]
    acc_map_final = _build_account_map(sf, acc_ids_final)
    for aid, counts in nd_counts_by_acc.items():
        if aid in acc_map_final:
            acc_map_final[aid].setdefault("nd_counts", counts)
    map_extra = batch_fetch_account_extras(sf, acc_ids_final) if acc_ids_final else {}
    points = []
    for rr in trimmed:
        aid = rr.get("account_id")
        accd = acc_map_final.get(aid) or {}
        lat, lng = accd.get("lat"), accd.get("lng")
        if lat is None or lng is None:
            glat, glng = await _geocode_city_country(accd.get("city"), accd.get("country"))
            if glat is not None and glng is not None:
                lat, lng = glat, glng
        if lat is None or lng is None:
            continue
        cs_flags = accd.get("cs") or {}
        extras_entry = (map_extra.get(aid) or {})
        assignments_list = _split_assignment_names(extras_entry.get("extra.AssignmentsNames"))
        assign_count = _safe_int(extras_entry.get("extra.AssignmentsCount"))
        nd_meta = accd.get("nd_counts") or {}
        points.append({
            "lat": lat, "lng": lng,
            "account_id": aid,
            "account_name": accd.get("name") or rr.get("account_name"),
            "city": accd.get("city") or rr.get("city"),
            "country": accd.get("country") or rr.get("country"),
            "badges": {"profiling": False, "qualification": False},
            "cs": {
                "clinical": bool(cs_flags.get("clinical")),
                "referral": bool(cs_flags.get("referral")),
                "detect": bool(cs_flags.get("detect")),
            },
            "meta": {
                "pi_name": extras_entry.get("extra.PIName"),
                "pi_email": extras_entry.get("extra.PIEmail"),
                "pi_phone": extras_entry.get("extra.PIPhone"),
                "assignments": assignments_list,
                "assignments_count": assign_count,
                "nd_u18": nd_meta.get("u18"),
                "nd_o18": nd_meta.get("o18"),
            },
        })

    return {"points": points, "rows": trimmed}



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
# NOTE: _dm_cache is process-local (one dict per uvicorn worker).
# With workers=4, Distance Matrix results are not shared across processes.
# Each worker fills its own cache independently, so the same route may be
# fetched up to 4 times before all workers warm up.
# If Google DM quota becomes a bottleneck, replace with a shared Redis cache.
_dm_cache: Dict[str, Tuple[Any, float, int]] = {}  # key -> (val, created_ts, ttl)
_CACHE_LOCK = threading.RLock()  # lock for _dm_cache (thread-safe within a process)

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
    Reintenta hasta 3 veces con backoff exponencial en errores 429/5xx.
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
                # Retry with exponential backoff for transient errors (429, 5xx)
                _dm_response = None
                for _attempt in range(3):
                    try:
                        r = await client.get(
                            "https://maps.googleapis.com/maps/api/distancematrix/json",
                            params=params,
                        )
                        if r.status_code == 429 or r.status_code >= 500:
                            wait = 2 ** _attempt  # 1s, 2s, 4s
                            log.warning(
                                "DistanceMatrix HTTP %s, retrying in %ss (attempt %s/3)",
                                r.status_code, wait, _attempt + 1,
                            )
                            await asyncio.sleep(wait)
                            continue
                        r.raise_for_status()
                        _dm_response = r.json()
                        break
                    except httpx.TimeoutException:
                        wait = 2 ** _attempt
                        log.warning("DistanceMatrix timeout, retrying in %ss (attempt %s/3)", wait, _attempt + 1)
                        await asyncio.sleep(wait)
                if _dm_response is None:
                    log.error("DistanceMatrix failed after 3 attempts for origin=%s chunk_size=%s", o_str, len(chunk))
                    continue
                mx = _dm_response
                _cache_set(ck, mx, ttl=3600)
            status = mx.get("status") if isinstance(mx, dict) else None
            if status != "OK":
                log.warning("DistanceMatrix status=%s for origin=%s chunk_size=%s", status, o_str, len(chunk))
                continue
            rows = mx.get("rows") if isinstance(mx, dict) else None
            if not rows:
                log.warning("DistanceMatrix rows missing for origin=%s chunk_size=%s", o_str, len(chunk))
                continue
            elems = (rows[0] or {}).get("elements", []) if rows else []
            for j, _ in enumerate(chunk):
                e = elems[j] if j < len(elems) else {}
                if not isinstance(e, dict):
                    continue
                if e.get("status") != "OK":
                    log.debug("DistanceMatrix element status=%s idx=%s origin=%s", e.get("status"), j, o_str)
                    continue
                dist_m = (e.get("distance") or {}).get("value")
                if dist_m is not None:
                    out[i + j] = round(float(dist_m) / 1000.0, 3)
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
    log.info("within-drive-km: request base=%s max_km_raw=%s", base_account_id, max_km)
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
    need_member = False
    need_batch_extras = False

    # Extras de Account (p.ej. Accredited__c, ShippingAddress, etc.)
    need_account_fields = False
    need_account_extras = False
    account_fields_needed: Set[str] = set()
    requested_account_cols: Set[str] = set()
    nd_counts_by_acc: Dict[str, Dict[str, Optional[int]]] = {}

    def _norm_label_to_key_safe(k: str) -> str:
        try:
            return _norm_label_to_key(k)
        except Exception:
            return k

    # Convert nested sub-groups to expression format before flattening (same as explorer_search)
    filters = _filter_group_to_expr(filters)
    # Clasificación de reglas (nested sub-groups are flattened first)
    for r in _flatten_filter_rules(filters):
        f = _norm_label_to_key_safe(r.get("field") or "")
        op = r.get("op") or r.get("operator") or "equals"
        val = r.get("value")

        if f in ("sf.Account.MemberName", "Account.MemberName"):
            member_rules.append(Rule(field="Account.MemberName", operator=op, value=val))
            need_member = True
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
            # Special: sf.Account.Id → WHERE on Opportunity.AccountId
            if nf == "Account.Id":
                sf_rules.append(Rule(field="AccountId", operator=op, value=val))
            elif nf.startswith("Account."):
                fld = nf.split(".", 1)[1]
                account_rules.append(Rule(field=fld, operator=op, value=val))
                need_account_fields = True
                account_fields_needed.add(fld)
            elif nf.startswith("Assignment."):
                # Assignment__c fields are not queryable in Opportunity SOQL.
                if nf == "Assignment.Name":
                    _aop = "contains" if op in ("equals", "=") else ("not_contains" if op in ("not_equals", "!=") else op)
                    extra_rules.append(Rule(field="extra.AssignmentsNames", operator=_aop, value=val))
                    need_batch_extras = True
            else:
                sf_rules.append(Rule(field=nf, operator=op, value=val))
        elif f.startswith("Account."):
            fld = f.split(".",1)[1]
            if fld == "Id":
                sf_rules.append(Rule(field="AccountId", operator=op, value=val))
            else:
                account_rules.append(Rule(field=fld, operator=op, value=val))
                need_account_fields = True
                need_account_extras = True
                account_fields_needed.add(fld)
        elif f.startswith("Assignment."):
            # Frontend strips "sf." prefix: "sf.Assignment.Name" → "Assignment.Name"
            if f == "Assignment.Name":
                _aop = "contains" if op in ("equals", "=") else ("not_contains" if op in ("not_equals", "!=") else op)
                extra_rules.append(Rule(field="extra.AssignmentsNames", operator=_aop, value=val))
                need_batch_extras = True
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
        acct_field = None
        if k.startswith("Account."):
            acct_field = _strip_account_prefix(k)
        elif k.startswith("sf.Account." ):
            acct_field = _strip_account_prefix(k)
        if acct_field:
            requested_account_cols.add(acct_field)
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
    log.info("within-drive-km: base=%s opportunity_accounts=%s", base_account_id, len(acc_ids_all))
    # Filtramos inactivas mediante _build_account_map
    acc_map_all = _build_account_map(sf, acc_ids_all)
    log.info("within-drive-km: active_accounts=%s", len(acc_map_all))

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

    # Candidate coords: use only what's already in SF / local DB — NO geocoding for candidates.
    # Exclude candidates without coords and log how many were dropped.
    _n_before_coord_filter = len(coords_by_acc)
    _excluded_no_coords = {aid for aid, info in coords_by_acc.items()
                          if info.get("lat") is None or info.get("lng") is None}
    for aid in _excluded_no_coords:
        del coords_by_acc[aid]
    if _excluded_no_coords:
        log.info("within-drive-km: excluded %s candidates without coords (no geocoding for candidates)",
                 len(_excluded_no_coords))

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
    log.info("within-drive-km: base coords lat=%s lng=%s city=%s country=%s", lat0, lng0, base_info.get("city"), base_info.get("country"))

    # -------- 1) Vecinos por distancia — haversine pre-filter --------
    # Collect all candidates with valid coords
    _all_candidates: List[Tuple[str, float, float]] = []
    for acc, info in coords_by_acc.items():
        if acc == str(base_account_id):
            continue
        lat_i, lng_i = info.get("lat"), info.get("lng")
        if lat_i is None or lng_i is None:
            continue
        _all_candidates.append((acc, float(lat_i), float(lng_i)))
    _n_total_candidates = len(_all_candidates)

    # Haversine pre-filter: compute straight-line distance, sort, keep top N
    _hav_scored = [
        (acc, lat_i, lng_i, _haversine_km(float(lat0), float(lng0), lat_i, lng_i))
        for acc, lat_i, lng_i in _all_candidates
    ]
    _hav_scored.sort(key=lambda x: x[3])
    _hav_scored = _hav_scored[:DM_CANDIDATES_PER_BASE]

    dests: List[Tuple[float, float]] = [(lat_i, lng_i) for _, lat_i, lng_i, _ in _hav_scored]
    dest_accs: List[str] = [acc for acc, _, _, _ in _hav_scored]

    log.info(
        "within-drive-km: total_candidates=%s after_haversine_prefilter=%s (top %s nearest)",
        _n_total_candidates, len(dest_accs), DM_CANDIDATES_PER_BASE,
    )

    # Guard rail: reject if billable elements exceed safety limit
    dm_elements = 1 * len(dests)  # 1 origin × N destinations
    if dm_elements > MAX_DISTANCE_MATRIX_ELEMENTS:
        log.warning(
            "within-drive-km: BLOCKED — %s billable elements (1 base × %s dests) exceeds limit %s",
            dm_elements, len(dests), MAX_DISTANCE_MATRIX_ELEMENTS,
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Search too broad: {dm_elements} Distance Matrix elements "
                f"(limit {MAX_DISTANCE_MATRIX_ELEMENTS}). "
                f"Reduce max_km or add filters to narrow candidates."
            ),
        )

    dists_km = await _drive_km_matrix((float(lat0), float(lng0)), dests)
    valid = [km for km in dists_km if km is not None]
    log.info(
        "within-drive-km: distance stats valid=%s none=%s min=%.2f max=%.2f",
        len(valid),
        len(dists_km) - len(valid),
        min(valid) if valid else float("nan"),
        max(valid) if valid else float("nan"),
    )
    neighbor_accs = [acc for acc, km in zip(dest_accs, dists_km) if km is not None and km <= float(max_km)]
    log.info(
        "within-drive-km: base=%s neighbor_candidates=%s within_max=%s max_km=%s",
        base_account_id,
        len(dest_accs),
        len(neighbor_accs),
        max_km,
    )

    # === Extras también para neighbors_all ===
    fields_to_fetch_neighbors: Set[str] = (account_fields_needed | requested_account_cols) - {"MemberName", "HasPI"}
    account_extras_for_neighbors: Dict[str, Dict[str, Any]] = {}
    if need_account_fields and neighbor_accs and fields_to_fetch_neighbors:
        account_extras_for_neighbors = _fetch_account_extras(sf, neighbor_accs, fields_to_fetch_neighbors)

    # Batch extras (extra.*) para vecinos
    extras_map_neighbors: Dict[str, Dict[str, Any]] = {}
    if neighbor_accs:
        if need_batch_extras:
            extras_map_neighbors = batch_fetch_account_extras(sf, neighbor_accs)
        else:
            extras_map_neighbors = batch_fetch_account_extras(sf, neighbor_accs)

    member_by_acc_neighbors: Dict[str, Optional[str]] = {}
    if need_member and neighbor_accs:
        vals = ",".join(f"'{a}'" for a in neighbor_accs)
        rows = _sf_query_all(sf, f"SELECT Id, C_Member__c, C_Member__r.Name FROM Account WHERE Id IN ({vals})")
        for a in rows:
            member_by_acc_neighbors[str(a.get("Id"))] = (a.get("C_Member__r") or {}).get("Name") or None

    # Sólo vecinos ACTIVOS (según _build_account_map)
    acc_map_neighbors = _build_account_map(sf, neighbor_accs) if neighbor_accs else {}
    if acc_map_neighbors:
        for aid, counts in nd_counts_by_acc.items():
            if aid in acc_map_neighbors:
                acc_map_neighbors[aid].setdefault("nd_counts", counts)
    active_neighbor_ids = list(acc_map_neighbors.keys())
    log.info("within-drive-km: active_neighbor_ids=%s (from %s)", len(active_neighbor_ids), len(neighbor_accs))

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
        if need_account_fields:
            extras.update(account_extras_for_neighbors.get(str(acc), {}))
        if need_batch_extras:
            # añade sólo las columnas pedidas de extra.* si hay
            if requested_extra_cols:
                xall = extras_map_neighbors.get(str(acc), {}) or {}
                extras.update({k: xall.get(k) for k in requested_extra_cols})

        assignments_list = _split_assignment_names(extras_map_neighbors.get(str(acc), {}).get("extra.AssignmentsNames"))
        assign_count = _safe_int(extras_map_neighbors.get(str(acc), {}).get("extra.AssignmentsCount"))
        nd_meta = accd.get("nd_counts") or {}
        cs_flags = accd.get("cs") or {}

        neighbors_all.append({
            "lat": lat, "lng": lng,
            "account_id": acc,
            "account_name": accd.get("name"),
            "city": accd.get("city") or (site_by_acc.get(acc) or {}).get("city"),
            "country": accd.get("country") or (site_by_acc.get(acc) or {}).get("country"),
            "extras": extras,
            "badges": {"profiling": False, "qualification": False},
            "cs": {
                "clinical": bool(cs_flags.get("clinical")),
                "referral": bool(cs_flags.get("referral")),
                "detect": bool(cs_flags.get("detect")),
            },
            "meta": {
                "pi_name": extras_map_neighbors.get(str(acc), {}).get("extra.PIName"),
                "pi_email": extras_map_neighbors.get(str(acc), {}).get("extra.PIEmail"),
                "pi_phone": extras_map_neighbors.get(str(acc), {}).get("extra.PIPhone"),
                "assignments": assignments_list,
                "assignments_count": assign_count,
                "nd_u18": nd_meta.get("u18"),
                "nd_o18": nd_meta.get("o18"),
            },
        })

    # Si no hay vecinos activos, devolvemos bootstrap mínimo
    if not active_neighbor_ids:
        log.info(
            "within-drive-km: no active neighbors (neighbor_accs=%s) for base=%s max_km=%s",
            len(neighbor_accs),
            base_account_id,
            max_km,
        )
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
        raw = k[3:]
        if raw.startswith("Account."):
            inner = raw.split(".", 1)[1]
            if _exists_on_account(inner):
                opp_fields.add(f"Account.{inner}")
        else:
            if _exists_on_opportunity(raw):
                opp_fields.add(raw)

    opp_fields_valid = {f for f in opp_fields if _exists_on_opportunity(f)}
    if (set(opp_fields) - opp_fields_valid):
        log.warning("Omitiendo campos inexistentes en Opportunity: %s", ", ".join(sorted(set(opp_fields) - opp_fields_valid)))

    select_fields = ", ".join(sorted(opp_fields_valid))
    opps = _sf_query_all(sf, f"SELECT {select_fields} FROM Opportunity {where_sql}")
    if not opps:
        log.info(
            "within-drive-km: no opportunities after filter (active_neighbors=%s base=%s max_km=%s where=%s)",
            len(active_neighbor_ids),
            base_account_id,
            max_km,
            where_sql,
        )
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
        sid: _expand_describe_keys_for_row(
            _expand_comments_keys_for_row(data or {}, groups_by_slug),
            groups_by_slug
        ) for sid, data in qual_rows
    }

    # Member/HasPI y extras sobre el subconjunto filtrado
    member_by_acc: Dict[str, Optional[str]] = {}
    if need_member and acc_ids:
        vals2 = ",".join(f"'{x}'" for x in acc_ids)
        rows = _sf_query_all(sf, f"SELECT Id, C_Member__c, C_Member__r.Name FROM Account WHERE Id IN ({vals2})")
        for a in rows:
            member_by_acc[str(a.get("Id"))] = (a.get("C_Member__r") or {}).get("Name") or None

    # PI/account derived data (HasPI) removed — not querying AccountContactRelation here

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
            # is empty / is not empty
            if op in ("is_null", "isnull", "is_empty"):
                return val == ""
            if op in ("is_not_null", "notnull", "is_not_empty"):
                return val != ""
            # Normalize country values to ISO for equality/set operators
            if rule.field == "site.country" and op in ("equals", "=", "not_equals", "!=", "in", "not_in"):
                val = (_country_norm(val) or val).upper()
                if op not in ("in", "not_in"):
                    s = (_country_norm(s) or s).upper()
            # in / not_in (comma-separated values)
            if op in ("in", "not_in"):
                raw_items = [x.strip() for x in str(rule.value or "").split(",") if x.strip()]
                if rule.field == "site.country":
                    items = [(_country_norm(x) or x).upper() for x in raw_items]
                    present = val.upper() in items
                else:
                    items = [x.lower() for x in raw_items]
                    present = val.lower() in items
                return present if op == "in" else not present
            # For site.country string ops also match against English display name
            if rule.field == "site.country" and op in (
                "equals","=","not_equals","!=","contains","not_contains","starts_with","ends_with"
            ):
                iso = (_country_norm(val) or val).upper()
                display = _ISO2_TO_DISPLAY.get(iso, iso)
                s_iso = (_country_norm(s) or "").upper()
                s_norm = s_iso if s_iso else s
                def _cmatch(needle: str) -> bool:
                    nl = needle.lower()
                    if   op in ("equals","="):       return iso.lower() == nl or display.lower() == nl
                    elif op in ("not_equals","!="):  return iso.lower() != nl and display.lower() != nl
                    elif op == "contains":           return nl in iso.lower() or nl in display.lower()
                    elif op == "not_contains":       return nl not in iso.lower() and nl not in display.lower()
                    elif op == "starts_with":        return iso.lower().startswith(nl) or display.lower().startswith(nl)
                    elif op == "ends_with":          return iso.lower().endswith(nl) or display.lower().endswith(nl)
                    return True
                return _cmatch(s_norm)
            if op in ("equals","="): return val.lower() == s.lower()
            if op in ("not_equals","!="): return val.lower() != s.lower()
            if op == "contains": return s.lower() in val.lower()
            if op == "not_contains": return s.lower() not in val.lower()
            if op == "starts_with": return val.lower().startswith(s.lower())
            if op == "ends_with": return val.lower().endswith(s.lower())
            return True
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = [match_one(r) for r in site_rules]
        # Multiple "site.country equals X" rules under AND logic is semantically nonsensical
        # (a site can't be in two countries). Treat country rules as OR, city rules as AND.
        if glue_and:
            country_idx = [i for i, r in enumerate(site_rules) if r.field == "site.country"]
            if len(country_idx) > 1:
                country_pass = any(res[i] for i in country_idx)
                other_pass = all(res[i] for i in range(len(res)) if i not in set(country_idx))
                return country_pass and other_pass
        return all(res) if glue_and else any(res)

    def pass_qual(qual_data: Dict[str, Any]) -> bool:
        if not qual_rules:
            return True
        res = [_eval_qual_rule(_qual_get(qual_data, qr.field), qr.operator, qr.value)
               for qr in qual_rules]
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

    # pass_haspi removed: HasPI filtering deprecated/removed

    def pass_account(aid: str) -> bool:
        if not account_rules:
            return True
        vals = dict(account_extras_by_acc.get(str(aid), {}))
        vals.setdefault("Id", str(aid))  # sf.Account.Id filter — always available
        if need_member: vals["MemberName"] = member_by_acc.get(str(aid))
        # HasPI support removed — don't attempt to compute or include it here.
        # (keep values derived from account_extras and member_by_acc only)
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

    # Helper: applies all Python-side checks respecting root AND/OR logic.
    _root_and = (filters.get("logic") or "AND") == "AND"
    def passes_row_checks(acc: Dict[str, Any], qual_data: Dict[str, Any], aid: str) -> bool:
        if _root_and:
            return (pass_site(acc) and pass_qual(qual_data) and
                    pass_member(aid) and pass_account(aid) and pass_extra(aid))
        checks = []
        if site_rules:    checks.append(pass_site(acc))
        if qual_rules:    checks.append(pass_qual(qual_data))
        if member_rules:  checks.append(pass_member(aid))
        if account_rules: checks.append(pass_account(aid))
        if extra_rules:   checks.append(pass_extra(aid))
        return any(checks) if checks else True

    # --- construir filas crudas ---
    rows: List[Dict[str, Any]] = []
    for o in opps:
        aid = o.get("AccountId")
        if not aid:
            continue
        if aid not in acc_map:      # evita inactivas
            continue
        acc = acc_map.get(aid) or {}
        sid = site_id_by_acc.get(str(aid))
        qual_data = qual_by_site.get(sid, {}) if sid else {}
        if not passes_row_checks(acc, qual_data, aid): continue

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
                elif sub == "HasPI":                data[k] = None
                else:
                    data[k] = (account_extras_by_acc.get(str(aid), {}) or {}).get(sub)
                continue
            if k.startswith("qual."):  data[k] = _qual_get(qual_data, k[5:]); continue
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

    # --- Prefetch maps for Account.* and extra.* so we can inject values even if not used in WHERE ---
    account_extras_map: Dict[str, Dict[str, Any]] = {}
    if need_account_extras and requested_account_cols:
        try:
            account_extras_map = _fetch_account_extras(sf, acc_ids, requested_account_cols)
        except Exception as e:
            log.warning("/explorer/search: _fetch_account_extras failed: %s", e)
            account_extras_map = {}

    extra_batch_map: Dict[str, Dict[str, Any]] = {}
    if need_batch_extras and acc_ids:
        try:
            extra_batch_map = batch_fetch_account_extras(sf, list(acc_ids)) or {}
        except Exception as e:
            log.warning("/explorer/search: batch_fetch_account_extras failed: %s", e)
            extra_batch_map = {}

    map_extra = extra_batch_map if extra_batch_map else (
        batch_fetch_account_extras(sf, list(acc_ids)) if acc_ids else {}
    )

    # Colapsa a una fila por cuenta
    rows = collapse_rows_by_account(rows)

    # --- Inject Account.* + extra.* + Member/HasPI into returned rows ---
    if rows:
        for r in rows:
            aid = str(r.get("account_id") or "")
            if not aid:
                continue
            r.setdefault("data", {})
            data = r["data"]

            # 1) Account.* columns explicitly requested
            if requested_account_cols:
                acc_vals = (account_extras_map.get(aid, {}) or {})
                for f in requested_account_cols:
                    # Keep the exact front-end key format: "Account.<Field>"
                    data[f"Account.{f}"] = acc_vals.get(f)

            # 2) extra.* columns explicitly requested
            if requested_extra_cols:
                ex_vals = (extra_batch_map.get(aid, {}) or {})
                for full_key in requested_extra_cols:
                    fetched = ex_vals.get(full_key)
                    if fetched is not None:
                        if full_key not in data or data.get(full_key) in (None, "", []):
                            data[full_key] = fetched
                    else:
                        if full_key not in data:
                            data[full_key] = None

            # 3) MemberName / HasPI if requested
            if need_member:
                m = (extra_batch_map.get(aid, {}) or {}).get("extra.MemberName")
                if m is None:
                    m = member_by_acc.get(aid)
                data["Account.MemberName"] = m
                # also expose as extra.MemberName if not present
                if "extra.MemberName" not in data:
                    data["extra.MemberName"] = m

            # HasPI deprecated — do not inject Account.HasPI

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
    for aid, counts in nd_counts_by_acc.items():
        if aid in acc_map_final:
            acc_map_final[aid].setdefault("nd_counts", counts)
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
        cs_flags = accd.get("cs") or {}
        extras_entry = (map_extra.get(aid) or {})
        assignments_list = _split_assignment_names(extras_entry.get("extra.AssignmentsNames"))
        assign_count = _safe_int(extras_entry.get("extra.AssignmentsCount"))
        nd_meta = accd.get("nd_counts") or {}

        points.append({
            "lat": lat, "lng": lng,
            "account_id": aid,
            "account_name": accd.get("name") or r.get("account_name"),
            "city": accd.get("city") or r.get("city"),
            "country": accd.get("country") or r.get("country"),
            "badges": badges.get(aid, {"profiling": False, "qualification": False}),
            "cs": {
                "clinical": bool(cs_flags.get("clinical")),
                "referral": bool(cs_flags.get("referral")),
                "detect": bool(cs_flags.get("detect")),
            },
            "meta": {
                "pi_name": extras_entry.get("extra.PIName"),
                "pi_email": extras_entry.get("extra.PIEmail"),
                "pi_phone": extras_entry.get("extra.PIPhone"),
                "assignments": assignments_list,
                "assignments_count": assign_count,
                "nd_u18": nd_meta.get("u18"),
                "nd_o18": nd_meta.get("o18"),
            },
        })

    log.info(
        "within-drive-km: neighbors_all=%s rows=%s points=%s (base=%s max_km=%s)",
        len(neighbors_all),
        len(rows),
        len(points),
        base_account_id,
        max_km,
    )

    return {
        "base": {"account_id": base_account_id, "lat": lat0, "lng": lng0},
        "neighbors_all": neighbors_all,   # ahora sólo activos
        "points": points,                  # vecinos que cumplen filtros
        "rows": rows,                      # colapsados por cuenta
        "meta": {"mode": "drive_km", "max_km": max_km}
    }


# ============================================================
# Endpoint: nearby-multi — same as within-drive-km but with
# multiple base account IDs; each candidate's distance is the
# minimum over all bases.
# ============================================================

# _haversine_km — moved to geo_cache.py

@explorer_router.post("/search/nearby-multi")
async def explorer_search_nearby_multi(
    payload: Dict = Body(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Find INNODIA sites within max_km driving distance of ANY of the given base_account_ids.
    Each candidate site's distance is the minimum driving distance to any base site.
    Response shape is compatible with within-drive-km (same NearbyDrawer on frontend).
    """
    sf = _get_sf(request)
    _ensure_describes(sf)

    base_account_ids: List[str] = payload.get("base_account_ids") or []
    max_km: Optional[float] = payload.get("max_km")
    if not base_account_ids:
        raise HTTPException(status_code=400, detail="base_account_ids is required")
    if not max_km or max_km <= 0:
        max_km = 120.0

    filters = payload.get("filters") or {"logic": "AND", "rules": []}
    columns = payload.get("columns") or []

    # -------- classify rules (same logic as within-drive-km) --------
    site_rules:    List[Rule] = []
    sf_rules:      List[Rule] = []
    qual_rules:    List[Rule] = []
    account_rules: List[Rule] = []
    extra_rules:   List[Rule] = []
    member_rules:  List[Rule] = []
    need_member = False
    need_batch_extras = False
    need_account_fields = False
    account_fields_needed: Set[str] = set()
    requested_account_cols: Set[str] = set()
    nd_counts_by_acc: Dict[str, Dict[str, Optional[int]]] = {}

    def _norm_safe(k: str) -> str:
        try: return _norm_label_to_key(k)
        except Exception: return k

    # Convert nested sub-groups to expression format before flattening (same as explorer_search)
    filters = _filter_group_to_expr(filters)
    for r in _flatten_filter_rules(filters):
        f  = _norm_safe(r.get("field") or "")
        op = r.get("op") or r.get("operator") or "equals"
        val = r.get("value")
        if f in ("sf.Account.MemberName", "Account.MemberName"):
            member_rules.append(Rule(field="Account.MemberName", operator=op, value=val)); need_member = True; continue
        if f.startswith("site."):
            site_rules.append(Rule(field=f, operator=op, value=val))
        elif f.startswith("qual."):
            qual_rules.append(Rule(field=f[5:], operator=op, value=val))
        elif f.startswith("extra."):
            extra_rules.append(Rule(field=f, operator=op, value=val)); need_batch_extras = True
        elif f.startswith("sf."):
            nf = f[3:]
            if nf == "Account.Id":
                sf_rules.append(Rule(field="AccountId", operator=op, value=val))
            elif nf.startswith("Account."):
                fld = nf.split(".", 1)[1]
                account_rules.append(Rule(field=fld, operator=op, value=val)); need_account_fields = True; account_fields_needed.add(fld)
            elif nf.startswith("Assignment."):
                if nf == "Assignment.Name":
                    _aop = "contains" if op in ("equals", "=") else ("not_contains" if op in ("not_equals", "!=") else op)
                    extra_rules.append(Rule(field="extra.AssignmentsNames", operator=_aop, value=val)); need_batch_extras = True
            else:
                sf_rules.append(Rule(field=nf, operator=op, value=val))
        elif f.startswith("Account."):
            fld = f.split(".", 1)[1]
            if fld == "Id":
                sf_rules.append(Rule(field="AccountId", operator=op, value=val))
            else:
                account_rules.append(Rule(field=fld, operator=op, value=val)); need_account_fields = True; account_fields_needed.add(fld)
        elif f.startswith("Assignment."):
            # Frontend strips "sf." prefix: "sf.Assignment.Name" → "Assignment.Name"
            if f == "Assignment.Name":
                _aop = "contains" if op in ("equals", "=") else ("not_contains" if op in ("not_equals", "!=") else op)
                extra_rules.append(Rule(field="extra.AssignmentsNames", operator=_aop, value=val)); need_batch_extras = True
        else:
            if f in ALLOWED_FIELDS and not f.startswith("Account."):
                sf_rules.append(Rule(field=f, operator=op, value=val))

    sf_filter = FilterQuery(logic=filters.get("logic", "AND"), rules=sf_rules)

    requested_cols: List[str] = []
    requested_extra_cols: Set[str] = set()
    for c in (columns or []):
        k = _norm_safe(c)
        requested_cols.append(k)
        if k in ("sf.Account.Member__c", "Account.Member__c", "sf.Account.MemberName", "Account.MemberName"):
            need_member = True
        acct_field = None
        if k.startswith("Account."): acct_field = _strip_account_prefix(k)
        elif k.startswith("sf.Account."): acct_field = _strip_account_prefix(k)
        if acct_field: requested_account_cols.add(acct_field); need_account_fields = True
        if k.startswith("extra."): requested_extra_cols.add(k); need_batch_extras = True

    # -------- load site coords --------
    site_rows = db.execute(
        select(Site.salesforce_account_id, Site.latitude, Site.longitude, Site.city, Site.country, Site.id)
        .where(Site.salesforce_account_id.isnot(None))
    ).all()
    site_by_acc: Dict[str, Dict[str, Any]] = {}
    site_id_by_acc: Dict[str, int] = {}
    for acc, lat, lng, city, country, sid in site_rows:
        site_by_acc[str(acc)] = {"lat": lat, "lng": lng, "city": city, "country": country}
        site_id_by_acc[str(acc)] = int(sid)

    # -------- all CTS accounts --------
    acc_ids_all = [r["AccountId"] for r in _sf_query_all(
        sf, f"SELECT AccountId FROM Opportunity WHERE Type IN ({TYPE_IN}) AND AccountId != null GROUP BY AccountId"
    )]
    acc_map_all = _build_account_map(sf, acc_ids_all)

    # merge coords for all active accounts
    coords_by_acc: Dict[str, Dict[str, Any]] = {}
    for aid in set(list(site_by_acc.keys()) + acc_ids_all):
        if aid not in acc_map_all:
            continue
        a = acc_map_all.get(aid, {})
        s = site_by_acc.get(aid, {})
        lat = a.get("lat") if a.get("lat") is not None else s.get("lat")
        lng = a.get("lng") if a.get("lng") is not None else s.get("lng")
        coords_by_acc[aid] = {"lat": lat, "lng": lng, "city": a.get("city") or s.get("city"), "country": a.get("country") or s.get("country")}

    # Candidate coords: use only what's already in SF / local DB — NO geocoding for candidates.
    _nm_excluded_no_coords = {aid for aid, info in coords_by_acc.items()
                              if info.get("lat") is None or info.get("lng") is None}
    for aid in _nm_excluded_no_coords:
        del coords_by_acc[aid]
    if _nm_excluded_no_coords:
        log.info("nearby-multi: excluded %s candidates without coords (no geocoding for candidates)",
                 len(_nm_excluded_no_coords))

    # -------- resolve base coords (geocoding allowed for bases only) --------
    base_coords: List[Tuple[float, float]] = []
    acc_map_bases = _build_account_map(sf, base_account_ids)
    for bid in base_account_ids:
        a = acc_map_bases.get(bid) or {}
        s = site_by_acc.get(str(bid)) or {}
        lat = a.get("lat") if a.get("lat") is not None else s.get("lat")
        lng = a.get("lng") if a.get("lng") is not None else s.get("lng")
        if lat is None or lng is None:
            glat, glng = await _geocode_city_country(a.get("city") or s.get("city"), a.get("country") or s.get("country"))
            lat, lng = glat, glng
        if lat is not None and lng is not None:
            base_coords.append((float(lat), float(lng)))

    if not base_coords:
        raise HTTPException(status_code=404, detail="None of the base accounts have geolocation")

    # -------- haversine pre-filter: top-N per base (independently) --------
    # For each base, compute haversine to all candidates, sort, keep top N.
    # Union the per-base top-N sets to get the final candidate list.
    _nm_total_with_coords = 0
    _nm_candidate_pool: List[Tuple[str, float, float]] = []  # all candidates with coords
    for acc, info in coords_by_acc.items():
        if acc in base_account_ids:
            continue
        lat_i, lng_i = info.get("lat"), info.get("lng")
        if lat_i is None or lng_i is None:
            continue
        _nm_total_with_coords += 1
        _nm_candidate_pool.append((acc, float(lat_i), float(lng_i)))

    # Per-base top-N: each base independently picks its N nearest candidates
    _nm_selected: set = set()
    for b_lat, b_lng in base_coords:
        _scored = [
            (acc, lat_i, lng_i, _haversine_km(b_lat, b_lng, lat_i, lng_i))
            for acc, lat_i, lng_i in _nm_candidate_pool
        ]
        _scored.sort(key=lambda x: x[3])
        for acc, _, _, hav_dist in _scored[:DM_CANDIDATES_PER_BASE]:
            _nm_selected.add(acc)

    # Build final dests from the union of per-base selections
    _nm_selected_map = {acc: (lat_i, lng_i) for acc, lat_i, lng_i in _nm_candidate_pool if acc in _nm_selected}
    dests: List[Tuple[float, float]] = list(_nm_selected_map.values())
    dest_accs: List[str] = list(_nm_selected_map.keys())

    log.info(
        "nearby-multi: bases=%s total_with_coords=%s selected_union=%s (top %s per base)",
        len(base_coords), _nm_total_with_coords, len(dest_accs), DM_CANDIDATES_PER_BASE,
    )

    # Guard rail: reject if billable elements exceed safety limit
    dm_elements = len(base_coords) * len(dests)
    if dm_elements > MAX_DISTANCE_MATRIX_ELEMENTS:
        log.warning(
            "nearby-multi: BLOCKED — %s billable elements (%s bases × %s dests) exceeds limit %s",
            dm_elements, len(base_coords), len(dests), MAX_DISTANCE_MATRIX_ELEMENTS,
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Search too broad: {dm_elements} Distance Matrix elements "
                f"({len(base_coords)} bases × {len(dests)} destinations, "
                f"limit {MAX_DISTANCE_MATRIX_ELEMENTS}). "
                f"Reduce max_km, use fewer base accounts, or add filters."
            ),
        )

    # -------- driving distance from each base (parallel) --------
    # Fire all base → candidates Distance Matrix calls concurrently, keep min per destination.
    dist_per_dest: List[Optional[float]] = [None] * len(dests)
    all_base_dists = await asyncio.gather(
        *[_drive_km_matrix(base_latng, dests) for base_latng in base_coords],
        return_exceptions=True,
    )
    for result in all_base_dists:
        if isinstance(result, Exception):
            log.warning("nearby-multi: _drive_km_matrix failed for one base: %s", result)
            continue
        for i, d in enumerate(result):
            if d is not None:
                if dist_per_dest[i] is None or d < dist_per_dest[i]:
                    dist_per_dest[i] = d

    # -------- keep candidates within max_km --------
    neighbor_accs = [acc for acc, d in zip(dest_accs, dist_per_dest) if d is not None and d <= float(max_km)]
    dist_by_acc: Dict[str, float] = {acc: d for acc, d in zip(dest_accs, dist_per_dest) if d is not None and d <= float(max_km)}
    log.info("nearby-multi: within_max=%s max_km=%s", len(neighbor_accs), max_km)

    # -------- fetch extras for neighbors_all --------
    fields_to_fetch_neighbors: Set[str] = (account_fields_needed | requested_account_cols) - {"MemberName", "HasPI"}
    account_extras_for_neighbors: Dict[str, Dict[str, Any]] = {}
    if need_account_fields and neighbor_accs and fields_to_fetch_neighbors:
        account_extras_for_neighbors = _fetch_account_extras(sf, neighbor_accs, fields_to_fetch_neighbors)

    extras_map_neighbors: Dict[str, Dict[str, Any]] = {}
    if neighbor_accs:
        extras_map_neighbors = batch_fetch_account_extras(sf, neighbor_accs)

    member_by_acc_neighbors: Dict[str, Optional[str]] = {}
    if need_member and neighbor_accs:
        vals = ",".join(f"'{a}'" for a in neighbor_accs)
        rows_m = _sf_query_all(sf, f"SELECT Id, C_Member__c, C_Member__r.Name FROM Account WHERE Id IN ({vals})")
        for a in rows_m:
            member_by_acc_neighbors[str(a.get("Id"))] = (a.get("C_Member__r") or {}).get("Name") or None

    acc_map_neighbors = _build_account_map(sf, neighbor_accs) if neighbor_accs else {}
    active_neighbor_ids = list(acc_map_neighbors.keys())
    log.info("nearby-multi: active_neighbor_ids=%s", len(active_neighbor_ids))

    # -------- build neighbors_all --------
    neighbors_all: List[Dict[str, Any]] = []
    for acc in active_neighbor_ids:
        accd = acc_map_neighbors.get(acc)
        lat, lng = accd.get("lat"), accd.get("lng")
        if lat is None or lng is None:
            glat, glng = await _geocode_city_country(accd.get("city"), accd.get("country"))
            if glat is not None: lat, lng = glat, glng
        if lat is None or lng is None:
            s_info = site_by_acc.get(acc) or {}
            lat = lat if lat is not None else s_info.get("lat")
            lng = lng if lng is not None else s_info.get("lng")
        if (lat is None or lng is None) and site_by_acc.get(acc):
            glat, glng = await _geocode_city_country((site_by_acc.get(acc) or {}).get("city"), (site_by_acc.get(acc) or {}).get("country"))
            if glat is not None: lat, lng = glat, glng
        if lat is None or lng is None:
            continue
        extras: Dict[str, Any] = {}
        if need_member:    extras["MemberName"] = member_by_acc_neighbors.get(str(acc))
        if need_account_fields: extras.update(account_extras_for_neighbors.get(str(acc), {}))
        assignments_list = _split_assignment_names(extras_map_neighbors.get(str(acc), {}).get("extra.AssignmentsNames"))
        assign_count = _safe_int(extras_map_neighbors.get(str(acc), {}).get("extra.AssignmentsCount"))
        nd_meta = accd.get("nd_counts") or {}
        cs_flags = accd.get("cs") or {}
        neighbors_all.append({
            "lat": lat, "lng": lng,
            "account_id": acc,
            "account_name": accd.get("name"),
            "city": accd.get("city") or (site_by_acc.get(acc) or {}).get("city"),
            "country": accd.get("country") or (site_by_acc.get(acc) or {}).get("country"),
            "distance_km": dist_by_acc.get(acc),
            "extras": extras,
            "badges": {"profiling": False, "qualification": False},
            "cs": {"clinical": bool(cs_flags.get("clinical")), "referral": bool(cs_flags.get("referral")), "detect": bool(cs_flags.get("detect"))},
            "meta": {
                "pi_name": extras_map_neighbors.get(str(acc), {}).get("extra.PIName"),
                "pi_email": extras_map_neighbors.get(str(acc), {}).get("extra.PIEmail"),
                "pi_phone": extras_map_neighbors.get(str(acc), {}).get("extra.PIPhone"),
                "assignments": assignments_list, "assignments_count": assign_count,
                "nd_u18": nd_meta.get("u18"), "nd_o18": nd_meta.get("o18"),
            },
        })

    if not active_neighbor_ids:
        return {
            "base": {"account_ids": base_account_ids, "count": len(base_account_ids)},
            "neighbors_all": neighbors_all, "points": [], "rows": [],
            "meta": {"mode": "drive_km_multi", "max_km": max_km, "bases_count": len(base_account_ids)},
        }

    # -------- SOQL filter on active neighbors --------
    vals_sql = ", ".join(f"'{a}'" for a in active_neighbor_ids)
    base_where = f"Type IN ({TYPE_IN}) AND AccountId != null AND AccountId IN ({vals_sql})"
    extra_where = _build_sf_where(sf_filter)
    where_sql = f"WHERE {base_where}" + (f" AND {extra_where}" if extra_where else "")

    opp_fields: Set[str] = {"Id", "Name", "Type", "StageName", "IsClosed", "CloseDate", "AccountId"}
    for r in sf_rules:
        if r.field.startswith("Account."): continue
        if _exists_on_opportunity(r.field): opp_fields.add(r.field)
    for k in requested_cols:
        if not k.startswith("sf."): continue
        raw = k[3:]
        if raw.startswith("Account."):
            inner = raw.split(".", 1)[1]
            if _exists_on_account(inner): opp_fields.add(f"Account.{inner}")
        else:
            if _exists_on_opportunity(raw): opp_fields.add(raw)

    opp_fields_valid = {f for f in opp_fields if _exists_on_opportunity(f)}
    select_fields = ", ".join(sorted(opp_fields_valid))
    opps = _sf_query_all(sf, f"SELECT {select_fields} FROM Opportunity {where_sql}")
    if not opps:
        return {
            "base": {"account_ids": base_account_ids, "count": len(base_account_ids)},
            "neighbors_all": neighbors_all, "points": [], "rows": [],
            "meta": {"mode": "drive_km_multi", "max_km": max_km, "bases_count": len(base_account_ids)},
        }

    acc_ids = sorted({o.get("AccountId") for o in opps if o.get("AccountId")})
    acc_map = _build_account_map(sf, acc_ids)

    qual_rows_db = db.execute(select(SiteQual.site_id, SiteQual.data)).all()
    groups_by_slug = _qual_groups_from_questions(db)
    qual_by_site: Dict[int, Dict[str, Any]] = {
        sid: _expand_describe_keys_for_row(_expand_comments_keys_for_row(data or {}, groups_by_slug), groups_by_slug)
        for sid, data in qual_rows_db
    }

    member_by_acc: Dict[str, Optional[str]] = {}
    if need_member and acc_ids:
        vals2 = ",".join(f"'{x}'" for x in acc_ids)
        rows_m2 = _sf_query_all(sf, f"SELECT Id, C_Member__c, C_Member__r.Name FROM Account WHERE Id IN ({vals2})")
        for a in rows_m2:
            member_by_acc[str(a.get("Id"))] = (a.get("C_Member__r") or {}).get("Name") or None

    fields_to_fetch = (account_fields_needed | requested_account_cols) - {"MemberName", "HasPI"}
    account_extras_by_acc: Dict[str, Dict[str, Any]] = {}
    if need_account_fields and acc_ids and fields_to_fetch:
        account_extras_by_acc = _fetch_account_extras(sf, acc_ids, fields_to_fetch)

    extras_map: Dict[str, Dict[str, Any]] = {}
    if need_batch_extras and acc_ids:
        extras_map = batch_fetch_account_extras(sf, acc_ids)

    # -------- pass_* helpers --------
    def pass_site_m(acc: Dict[str, Any]) -> bool:
        if not site_rules: return True
        def match_one(rule: Rule) -> bool:
            val = ""
            if rule.field == "site.city":    val = acc.get("city") or ""
            if rule.field == "site.country": val = acc.get("country") or ""
            op = _OP_SYNONYM.get(rule.operator, rule.operator); s = str(rule.value or "")
            if op in ("is_null","isnull","is_empty"): return val == ""
            if op in ("is_not_null","notnull","is_not_empty"): return val != ""
            if rule.field == "site.country" and op in ("equals","=","not_equals","!=","in","not_in"):
                val = (_country_norm(val) or val).upper()
                if op not in ("in","not_in"): s = (_country_norm(s) or s).upper()
            if op in ("in","not_in"):
                raw_items = [x.strip() for x in str(rule.value or "").split(",") if x.strip()]
                items = [(_country_norm(x) or x).upper() for x in raw_items] if rule.field == "site.country" else [x.lower() for x in raw_items]
                present = (val.upper() if rule.field == "site.country" else val.lower()) in items
                return present if op == "in" else not present
            if op in ("equals","="): return val.lower() == s.lower()
            if op in ("not_equals","!="): return val.lower() != s.lower()
            if op == "contains": return s.lower() in val.lower()
            if op == "not_contains": return s.lower() not in val.lower()
            if op == "starts_with": return val.lower().startswith(s.lower())
            if op == "ends_with": return val.lower().endswith(s.lower())
            return True
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = [match_one(r) for r in site_rules]
        return all(res) if glue_and else any(res)

    def pass_qual_m(qual_data: Dict[str, Any]) -> bool:
        if not qual_rules: return True
        res = [_eval_qual_rule(_qual_get(qual_data, qr.field), qr.operator, qr.value) for qr in qual_rules]
        return all(res) if (filters.get("logic") or "AND") == "AND" else any(res)

    def pass_member_m(aid: str) -> bool:
        if not member_rules: return True
        actual = member_by_acc.get(str(aid)) or ""
        def eval_one(rule: Rule) -> bool:
            op = _OP_SYNONYM.get(rule.operator, rule.operator); s = str(rule.value or "")
            if op in ("equals","="): return actual == s
            if op in ("not_equals","!="): return actual != s
            if op == "contains": return s.lower() in actual.lower()
            return True
        return all(eval_one(r) for r in member_rules) if (filters.get("logic") or "AND") == "AND" else any(eval_one(r) for r in member_rules)

    def pass_account_m(aid: str) -> bool:
        if not account_rules: return True
        vals_d = dict(account_extras_by_acc.get(str(aid), {})); vals_d.setdefault("Id", str(aid))
        if need_member: vals_d["MemberName"] = member_by_acc.get(str(aid))
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = [_eval_qual_rule(vals_d.get(ar.field.split(".",1)[1]), ar.operator, ar.value) for ar in account_rules]
        return all(res) if glue_and else any(res)

    def pass_extra_m(aid: str) -> bool:
        if not extra_rules: return True
        vals_d = extras_map.get(str(aid), {}) if extras_map else {}
        glue_and = (filters.get("logic") or "AND") == "AND"
        res = [_eval_qual_rule(vals_d.get(er.field), er.operator, er.value) for er in extra_rules]
        return all(res) if glue_and else any(res)

    _root_and = (filters.get("logic") or "AND") == "AND"
    def passes_row_checks_m(acc: Dict[str, Any], qual_data: Dict[str, Any], aid: str) -> bool:
        if _root_and:
            return pass_site_m(acc) and pass_qual_m(qual_data) and pass_member_m(aid) and pass_account_m(aid) and pass_extra_m(aid)
        checks = []
        if site_rules:    checks.append(pass_site_m(acc))
        if qual_rules:    checks.append(pass_qual_m(qual_data))
        if member_rules:  checks.append(pass_member_m(aid))
        if account_rules: checks.append(pass_account_m(aid))
        if extra_rules:   checks.append(pass_extra_m(aid))
        return any(checks) if checks else True

    # -------- build rows --------
    rows_out: List[Dict[str, Any]] = []
    for o in opps:
        aid = o.get("AccountId")
        if not aid or aid not in acc_map: continue
        acc = acc_map.get(aid) or {}
        sid = site_id_by_acc.get(str(aid))
        qual_data = qual_by_site.get(sid, {}) if sid else {}
        if not passes_row_checks_m(acc, qual_data, aid): continue
        data: Dict[str, Any] = {}
        for k in requested_cols:
            if k == "site.city":    data[k] = acc.get("city"); continue
            if k == "site.country": data[k] = acc.get("country"); continue
            if k.startswith("Account."):
                sub = k.split(".",1)[1]
                if sub == "Id":              data[k] = aid
                elif sub == "Name":          data[k] = acc.get("name")
                elif sub == "ShippingCity":  data[k] = acc.get("city")
                elif sub == "ShippingCountry": data[k] = acc.get("country")
                elif sub == "MemberName":    data[k] = member_by_acc.get(str(aid)) if need_member else None
                elif sub == "HasPI":         data[k] = None
                else: data[k] = (account_extras_by_acc.get(str(aid), {}) or {}).get(sub)
                continue
            if k.startswith("qual."):  data[k] = _qual_get(qual_data, k[5:]); continue
            if k.startswith("sf."):    data[k] = o.get(k[3:]); continue
            if k.startswith("extra."): data[k] = (extras_map.get(str(aid), {}) or {}).get(k); continue
            data[k] = o.get(k)
        _flatten_sf_inplace(data)
        rows_out.append({
            "account_id": aid,
            "account_name": acc.get("name"),
            "country": acc.get("country"),
            "city": acc.get("city"),
            "opportunity_type": _norm_type(o.get("Type")),
            "distance_km": dist_by_acc.get(aid),
            "data": data,
        })

    rows_out = collapse_rows_by_account(rows_out)

    # inject extra.* / Account.* into returned rows
    extra_batch_map: Dict[str, Dict[str, Any]] = {}
    if need_batch_extras and acc_ids:
        try: extra_batch_map = batch_fetch_account_extras(sf, list(acc_ids)) or {}
        except Exception as e: log.warning("nearby-multi: batch_fetch_account_extras failed: %s", e)
    map_extra = extra_batch_map if extra_batch_map else (batch_fetch_account_extras(sf, list(acc_ids)) if acc_ids else {})

    if rows_out:
        for r in rows_out:
            aid = str(r.get("account_id") or "")
            if not aid: continue
            r.setdefault("data", {})
            data = r["data"]
            if need_member:
                m = (extra_batch_map.get(aid, {}) or {}).get("extra.MemberName") or member_by_acc.get(aid)
                data["Account.MemberName"] = m
                if "extra.MemberName" not in data: data["extra.MemberName"] = m
            # inject distance_km as a data column
            data["distance_km"] = dist_by_acc.get(aid)

    # -------- build points --------
    badges: Dict[str, Dict[str, bool]] = {}
    for r in rows_out:
        aid = r.get("account_id")
        if not aid: continue
        b = badges.setdefault(aid, {"profiling": False, "qualification": False})
        t = (r.get("opportunity_type") or "").lower()
        if t in ("profiling","both"): b["profiling"] = True
        if t in ("qualification","both"): b["qualification"] = True

    acc_map_final = _build_account_map(sf, [r["account_id"] for r in rows_out if r.get("account_id")])
    points_out: List[Dict[str, Any]] = []
    for r in rows_out:
        aid = r.get("account_id")
        accd = acc_map_final.get(aid) or {}
        lat, lng = accd.get("lat"), accd.get("lng")
        if lat is None or lng is None:
            glat, glng = await _geocode_city_country(accd.get("city"), accd.get("country"))
            if glat is not None: lat, lng = glat, glng
        if lat is None or lng is None:
            s_info = site_by_acc.get(str(aid)) or {}
            lat = lat if lat is not None else s_info.get("lat")
            lng = lng if lng is not None else s_info.get("lng")
        if (lat is None or lng is None) and site_by_acc.get(str(aid)):
            glat, glng = await _geocode_city_country((site_by_acc.get(str(aid)) or {}).get("city"), (site_by_acc.get(str(aid)) or {}).get("country"))
            if glat is not None: lat, lng = glat, glng
        if lat is None or lng is None: continue
        cs_flags = accd.get("cs") or {}
        extras_entry = map_extra.get(aid) or {}
        assignments_list = _split_assignment_names(extras_entry.get("extra.AssignmentsNames"))
        assign_count = _safe_int(extras_entry.get("extra.AssignmentsCount"))
        nd_meta = accd.get("nd_counts") or {}
        points_out.append({
            "lat": lat, "lng": lng,
            "account_id": aid,
            "account_name": accd.get("name") or r.get("account_name"),
            "city": accd.get("city") or r.get("city"),
            "country": accd.get("country") or r.get("country"),
            "distance_km": dist_by_acc.get(aid),
            "badges": badges.get(aid, {"profiling": False, "qualification": False}),
            "cs": {"clinical": bool(cs_flags.get("clinical")), "referral": bool(cs_flags.get("referral")), "detect": bool(cs_flags.get("detect"))},
            "meta": {
                "pi_name": extras_entry.get("extra.PIName"),
                "pi_email": extras_entry.get("extra.PIEmail"),
                "pi_phone": extras_entry.get("extra.PIPhone"),
                "assignments": assignments_list, "assignments_count": assign_count,
                "nd_u18": nd_meta.get("u18"), "nd_o18": nd_meta.get("o18"),
            },
        })

    log.info("nearby-multi: neighbors_all=%s rows=%s points=%s bases=%s max_km=%s", len(neighbors_all), len(rows_out), len(points_out), len(base_account_ids), max_km)

    return {
        "base": {"account_ids": base_account_ids, "count": len(base_account_ids)},
        "neighbors_all": neighbors_all,
        "points": points_out,
        "rows": rows_out,
        "meta": {"mode": "drive_km_multi", "max_km": max_km, "bases_count": len(base_account_ids)},
    }
