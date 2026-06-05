"""Knowledge index loaders and builder for Moby.

Extracted from `app.routers.ai_chat` (Phase 5b refactor).

The shared `_INDEX_CACHE` dict lives here. `ai_chat.py` exposes it via a
shim re-export so external callers (`moby_tools.py`, `helpers/labels.py`)
keep accessing the same object.
"""
from __future__ import annotations

import json
import re
from time import time
from typing import Any, Dict

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.moby.config import (
    FIELDS_SF_JSON_PATH,
    INDEX_REFRESH_SEC,
    QUAL_ALIAS_JSON_PATH,
)
from app.moby.helpers.debug import _dbg
from app.moby.knowledge.text import _normalize


_INDEX_CACHE: Dict[str, Any] = {"ts": 0, "index": {}, "sf_fields": {}}


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
        out.setdefault(alias, {"source": "sf", "field": field, "label": label})

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
            out.setdefault(base, {"source": "site_qual", "key": key})
            # variante sin prefijos largos si los hubiera
            short = re.sub(r"^C[_\-]+", "", key)
            out.setdefault(_normalize(short), {"source": "site_qual", "key": key})
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
            out.setdefault(base, {"source": "profiling_kv", "key": key})
            short = re.sub(r"^C[_\-]+", "", key)
            out.setdefault(_normalize(short), {"source": "profiling_kv", "key": key})
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
        fused[alias] = {"source": "site_qual", "key": key}
    # Luego SF pisa (prioridad)
    for k, v in sf_map.items():
        fused[k] = v

    # Manual fallbacks (robust aliases) — NLP glossary: acronyms, abbreviations, domain terms
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
