# backend/app/routers/qualification.py
# backend/app/routers/qualification.py
from __future__ import annotations

import io
import hashlib
import re
from typing import List, Dict, Any, Optional, Tuple

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Request, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, text, select

from simple_salesforce.exceptions import SalesforceExpiredSession, SalesforceAuthenticationFailed

from app.database import get_db
from app.models import (
    Site,
    Questionnaire,
    Section,
    Question,
    Response,
    QuestionnaireType,
)
from app.models.site_qual import SiteQual  # 👈 JSONB con aplanado qual por site
from app.parser.qualification import parse_qualification_checklist
from app.services.geocoding import geocode_with_fallback

# === Helpers SF session (usamos la misma infraestructura del módulo OAuth) ===
from app.services.salesforce_oauth import (
    COOKIE_NAME,
    unsign_value,
    get_salesforce_from_session_id,
)

router = APIRouter(prefix="/api/qualification", tags=["qualification"])

# --------------------------------------------------------------------------------------
# Utils
# --------------------------------------------------------------------------------------

def _sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()

def _slugify_question(text: str) -> str:
    """
    Misma lógica que en salesforce_explorer.py para que las claves coincidan.
    """
    t = (text or "").strip().lower()
    t = re.sub(r"\([^)]*\)", "", t)           # quita paréntesis
    t = re.sub(r"[^a-z0-9]+", "_", t)         # no alfanumérico → _
    t = re.sub(r"_+", "_", t).strip("_")
    return t or "q"

def _clean(s: str | None) -> str:
    return (s or "").strip()

# --------------------------------------------------------------------------------------
# País/ciudad helpers (idénticos a tu archivo original con pequeños fixes)
# --------------------------------------------------------------------------------------

COUNTRY_ALIASES = {
    # --- Western Europe ---
    "belgium": "Belgium",
    "belgie": "Belgium",
    "belgië": "Belgium",
    "belgique": "Belgium",
    "belgica": "Belgium",
    "bélgica": "Belgium",

    "netherlands": "Netherlands",
    "nederland": "Netherlands",
    "holland": "Netherlands",
    "países bajos": "Netherlands",
    "paises bajos": "Netherlands",
    "pays-bas": "Netherlands",
    "paesi bassi": "Netherlands",

    "luxembourg": "Luxembourg",
    "luxemburgo": "Luxembourg",
    "luxembourg (grand duchy)": "Luxembourg",
    "lux": "Luxembourg",

    "france": "France",
    "francia": "France",
    "frankrijk": "France",
    "fr": "France",
    "frankreich": "France",

    "germany": "Germany",
    "deutschland": "Germany",
    "alemania": "Germany",
    "allemagne": "Germany",
    "germania": "Germany",

    "switzerland": "Switzerland",
    "suisse": "Switzerland",
    "schweiz": "Switzerland",
    "suiza": "Switzerland",
    "svizzera": "Switzerland",

    "austria": "Austria",
    "österreich": "Austria",
    "austria (at)": "Austria",

    "liechtenstein": "Liechtenstein",

    # --- Southern Europe ---
    "italy": "Italy",
    "italia": "Italy",
    "ita": "Italy",

    "spain": "Spain",
    "españa": "Spain",
    "espana": "Spain",
    "espagne": "Spain",
    "spanje": "Spain",
    "spagna": "Spain",

    "portugal": "Portugal",
    "portogallo": "Portugal",
    "portugal (pt)": "Portugal",

    "andorra": "Andorra",
    "monaco": "Monaco",
    "san marino": "San Marino",
    "vatican": "Vatican City",
    "holy see": "Vatican City",
    "vaticano": "Vatican City",

    "malta": "Malta",

    # --- Northern Europe ---
    "denmark": "Denmark",
    "danmark": "Denmark",
    "dinamarca": "Denmark",

    "sweden": "Sweden",
    "sverige": "Sweden",
    "suecia": "Sweden",
    "suède": "Sweden",

    "norway": "Norway",
    "norge": "Norway",
    "noruega": "Norway",
    "norvège": "Norway",

    "finland": "Finland",
    "suomi": "Finland",
    "finlandia": "Finland",
    "finlande": "Finland",

    "iceland": "Iceland",
    "ísland": "Iceland",
    "islandia": "Iceland",

    "ireland": "Ireland",
    "éire": "Ireland",
    "irlanda": "Ireland",
    "irlande": "Ireland",

    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "great britain": "United Kingdom",
    "reino unido": "United Kingdom",
    "royaume-uni": "United Kingdom",
    "regno unito": "United Kingdom",

    # --- Eastern Europe ---
    "poland": "Poland",
    "polska": "Poland",
    "polonia": "Poland",

    "czech republic": "Czechia",
    "czechia": "Czechia",
    "česko": "Czechia",
    "república checa": "Czechia",

    "slovakia": "Slovakia",
    "slovensko": "Slovakia",
    "eslovaquia": "Slovakia",

    "hungary": "Hungary",
    "magyarország": "Hungary",
    "hungría": "Hungary",
    "hongrie": "Hungary",

    "slovenia": "Slovenia",
    "slovenija": "Slovenia",
    "eslovenia": "Slovenia",

    "croatia": "Croatia",
    "hrvatska": "Croatia",
    "croacia": "Croatia",

    "serbia": "Serbia",
    "srbija": "Serbia",
    "serbie": "Serbia",

    "bosnia and herzegovina": "Bosnia and Herzegovina",
    "bosna i hercegovina": "Bosnia and Herzegovina",
    "bosnia": "Bosnia and Herzegovina",
    "bosnia-herzegovina": "Bosnia and Herzegovina",

    "montenegro": "Montenegro",
    "crna gora": "Montenegro",

    "north macedonia": "North Macedonia",
    "macedonia": "North Macedonia",
    "makedonija": "North Macedonia",

    "albania": "Albania",
    "shqipëria": "Albania",
    "albanie": "Albania",

    "greece": "Greece",
    "ελλάδα": "Greece",
    "grecia": "Greece",
    "grèce": "Greece",
    "grecja": "Greece",

    "bulgaria": "Bulgaria",
    "българия": "Bulgaria",
    "bulgarie": "Bulgaria",
    "bulgaria (bg)": "Bulgaria",

    "romania": "Romania",
    "românia": "Romania",
    "rumania": "Romania",
    "roumanie": "Romania",

    # --- Baltics ---
    "estonia": "Estonia",
    "eesti": "Estonia",
    "letonia": "Estonia",

    "latvia": "Latvia",
    "latvija": "Latvia",
    "letonia": "Latvia",

    "lithuania": "Lithuania",
    "lietuvos": "Lithuania",
    "lituania": "Lithuania",

    # --- Easternmost Europe / Caucasus ---
    "russia": "Russia",
    "россия": "Russia",
    "rusia": "Russia",

    "belarus": "Belarus",
    "беларусь": "Belarus",
    "bielorrusia": "Belarus",

    "ukraine": "Ukraine",
    "україна": "Ukraine",
    "ucrania": "Ukraine",
    "ukraine (ua)": "Ukraine",

    "moldova": "Moldova",
    "moldova (republic of)": "Moldova",
    "moldavia": "Moldova",
}

def _norm_country_name(s: str | None) -> str | None:
    if not s:
        return None
    k = s.strip().lower()
    return COUNTRY_ALIASES.get(k, s.strip().title())

def infer_site_name_from_rows(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    CANDS = ("institute", "institution", "center name", "centre name", "site name")
    for r in rows:
        q = (r.get("question") or r.get("question_text") or "").strip().lower()
        if any(k in q for k in CANDS):
            ans = _clean(r.get("answer"))
            if ans:
                return ans
    return None

def infer_city_country_from_rows(rows: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    if not rows:
        return (None, None)
    CANDS = ("city and country", "city, country", "city & country", "city / country")
    for r in rows:
        q = (r.get("question") or r.get("question_text") or "").strip().lower()
        if any(k in q for k in CANDS):
            raw = _clean(r.get("answer"))
            if not raw:
                continue
            for sep in [",", ";", "/", "|", " - ", "-", "—"]:
                if sep in raw:
                    left, right = raw.split(sep, 1)
                    city = _clean(left)
                    country = _norm_country_name(_clean(right))
                    if city and country:
                        return (city, country)
            return (raw, None)
    return (None, None)


# Mapeos ISO2 <-> nombre país (Europa)
ISO2_TO_NAME = {
    "AD":"Andorra","AT":"Austria","BE":"Belgium","BG":"Bulgaria","CH":"Switzerland","CY":"Cyprus",
    "CZ":"Czechia","DE":"Germany","DK":"Denmark","EE":"Estonia","ES":"Spain","FI":"Finland",
    "FR":"France","GB":"United Kingdom","GR":"Greece","HR":"Croatia","HU":"Hungary","IE":"Ireland",
    "IS":"Iceland","IT":"Italy","LI":"Liechtenstein","LT":"Lithuania","LU":"Luxembourg",
    "LV":"Latvia","MC":"Monaco","MD":"Moldova","ME":"Montenegro","MK":"North Macedonia",
    "MT":"Malta","NL":"Netherlands","NO":"Norway","PL":"Poland","PT":"Portugal","RO":"Romania",
    "RS":"Serbia","RU":"Russia","SE":"Sweden","SI":"Slovenia","SK":"Slovakia","SM":"San Marino",
    "UA":"Ukraine","VA":"Vatican City","AL":"Albania","BA":"Bosnia and Herzegovina","BY":"Belarus"
}

NAME_TO_ISO2 = {v: k for k, v in ISO2_TO_NAME.items()}


def normalize_city_country_with_geonames(
    db: Session, city: str | None, country: str | None
) -> tuple[str | None, str | None, float | None, float | None]:
    if not city:
        return (None, _norm_country_name(country), None, None)

    ctry_name = _norm_country_name(country)
    city_l = city.strip().lower()
    iso2 = NAME_TO_ISO2.get(ctry_name) if ctry_name else None

    if iso2:
        row = db.execute(
            text("""
                SELECT name, country_code, latitude, longitude
                FROM geonames_cities
                WHERE lower(name) = :n AND lower(country_code) = :c
                ORDER BY population DESC NULLS LAST
                LIMIT 1
            """),
            {"n": city_l, "c": iso2.lower()},
        ).first()
        if row:
            return (row[0], ISO2_TO_NAME.get(row[1], row[1]), row[2], row[3])

    row = db.execute(
        text("""
            SELECT name, country_code, latitude, longitude
            FROM geonames_cities
            WHERE lower(name) = :n
            ORDER BY population DESC NULLS LAST
            LIMIT 1
        """),
        {"n": city_l},
    ).first()
    if row:
        return (row[0], ISO2_TO_NAME.get(row[1], row[1]), row[2], row[3])

    return (city.strip().title(), ctry_name, None, None)

def _get_sf(request: Request = None):
    from app.services.salesforce_service import get_service_sf
    sf = get_service_sf()
    if not sf:
        raise HTTPException(403, "No autenticado en Salesforce")
    return sf

# --------------------------------------------------------------------------------------
# Subida: guarda Questionnaire/Section/Question/Response y APLANA en site_qual
# --------------------------------------------------------------------------------------

@router.post("/upload")
async def upload_qualification(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Sube un Excel .xlsx/.xls")

    content = await file.read()
    if not content:
        raise HTTPException(400, "Archivo vacío")

    # Parsea
    try:
        result = parse_qualification_checklist(io.BytesIO(content), file.filename)
    except Exception as e:
        raise HTTPException(400, f"No se pudo procesar el Excel: {e}")

    if not result or not result.get("rows"):
        raise HTTPException(400, "No se pudieron extraer preguntas/respuestas del Excel")

    meta, rows = result["meta"], result["rows"]

    # Completa metadatos desde el contenido
    inferred_name = infer_site_name_from_rows(rows)
    if inferred_name:
        meta["site_name"] = inferred_name

    ic, ictry = infer_city_country_from_rows(rows)
    if ic or ictry:
        norm_city, norm_country, nlat, nlng = normalize_city_country_with_geonames(
            db, ic or meta.get("city"), ictry or meta.get("country")
        )
        if norm_city:
            meta["city"] = norm_city
        if norm_country:
            meta["country"] = norm_country
        if nlat is not None and nlng is not None:
            meta["_lat"] = nlat
            meta["_lng"] = nlng

    # Upsert Site
    site_name = (meta.get("site_name") or "").strip() or "Unknown Site"
    site = (
        db.query(Site)
        .filter(func.lower(Site.name) == site_name.lower())
        .first()
        or Site(name=site_name)
    )
    site.street   = meta.get("street")      or site.street
    site.city     = meta.get("city")        or site.city
    site.country  = meta.get("country")     or site.country
    site.postcode = meta.get("postal_code") or site.postcode

    pre_lat, pre_lng = meta.get("_lat"), meta.get("_lng")
    if site.latitude is None or site.longitude is None:
        if pre_lat is not None and pre_lng is not None:
            site.latitude, site.longitude = pre_lat, pre_lng
            site.geocode_precision, site.geocode_source = "city", "geonames"
        else:
            lat, lng, prec, src = geocode_with_fallback(site.street, site.city, site.country)
            site.latitude, site.longitude = lat, lng
            site.geocode_precision, site.geocode_source = prec, src

    db.add(site)
    db.flush()  # para tener site.id

    # Crea Questionnaire
    q = Questionnaire(
        site_id=site.id,
        file_name=file.filename,
        version=meta.get("version"),
        type=QuestionnaireType.qualification,
        file_hash=_sha1(content),
    )
    db.add(q)
    db.flush()

    # Inserta Section/Question/Response manteniendo orden
    by_section: Dict[str, list[dict]] = {}
    for r in rows:
        by_section.setdefault(r.get("section") or "General", []).append(r)

    order_section = 0
    for section_name, items in by_section.items():
        order_section += 1
        sec = Section(name=section_name, questionnaire_id=q.id, section_order=order_section)
        db.add(sec)
        db.flush()

        order_q = 0
        for r in items:
            order_q += 1
            que = Question(
                section_id=sec.id,
                question_text=r.get("question") or r.get("question_text"),
                question_number=r.get("question_number"),
                subsection=r.get("subsection"),
                question_order=order_q,
            )
            db.add(que)
            db.flush()
            db.add(Response(question_id=que.id, response_text=r.get("answer")))

    # 👇 APLANADO: clave (question_key con subcódigo) -> respuesta
    flat: Dict[str, Any] = {}
    for r in rows:
        ans = r.get("answer")
        # IMPORTANTE: Siempre usar question_key que viene con el formato section__slug del parser
        key = _clean(r.get("question_key"))
        if not key:
            # Si por alguna razón no hay question_key, construirlo con el patrón correcto
            qtext = r.get("question") or r.get("question_text") or ""
            sub_code = r.get("subsection_code") or "x"
            slug = _slugify_question(qtext)
            key = f"{sub_code}__{slug}" if slug else None
        if key and ans not in (None, ""):
            flat[key] = str(ans)

    # UPSERT en SiteQual para este site
    sq = db.execute(select(SiteQual).where(SiteQual.site_id == site.id)).scalar_one_or_none()
    if sq:
        sq.data = flat
    else:
        sq = SiteQual(site_id=site.id, data=flat)
        db.add(sq)

    db.commit()
    return {
        "status": "ok",
        "site_id": site.id,
        "site_name": site.name,
        "rows": len(rows),
        "geocoded": site.latitude is not None and site.longitude is not None,
    }

# --------------------------------------------------------------------------------------
# Listado
# --------------------------------------------------------------------------------------

@router.get("/list")
def list_qualifications(db: Session = Depends(get_db)):
    q = (
        db.query(
            Questionnaire.id.label("id"),
            Questionnaire.file_name.label("file_name"),
            Questionnaire.version.label("version"),
            Questionnaire.upload_date.label("uploaded_at"),
            Site.id.label("site_id"),
            Site.name.label("site_name"),
            Site.city.label("city"),
            Site.country.label("country"),
            Site.salesforce_account_id.label("salesforce_account_id"),
        )
        .join(Site, Site.id == Questionnaire.site_id)
        .filter(Questionnaire.type == QuestionnaireType.qualification)
        .order_by(
            Questionnaire.upload_date.desc().nullslast(),
            Questionnaire.id.desc(),
        )
    )

    rows = []
    for r in q.all():
        rows.append(
            {
                "id": r.id,
                "file_name": r.file_name,
                "version": r.version,
                "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
                "site_id": r.site_id,
                "site_name": r.site_name,
                "city": r.city,
                "country": r.country,
                "salesforce_account_id": r.salesforce_account_id,
                "salesforce_account_name": None,
            }
        )
    return {"rows": rows}

# --------------------------------------------------------------------------------------
# Link / Unlink (sin cambios funcionales)
# --------------------------------------------------------------------------------------

class LinkBody(BaseModel):
    site_id: int
    account_id: str

class UnlinkBody(BaseModel):
    site_id: int

def _require_sf_ok(request: Request, acc_id: str):
    sf = _get_sf(request)
    try:
        res = sf.query_all(f"SELECT Id FROM Account WHERE Id = '{acc_id}' LIMIT 1")
        exists = bool(res.get("records"))
    except (SalesforceExpiredSession, SalesforceAuthenticationFailed):
        # The service-account client already re-mints once on an auth failure
        # (salesforce_service._ServiceSF); a persistent failure is a transient
        # upstream issue → 503, never 401 (do not log the user out).
        raise HTTPException(status_code=503, detail="Salesforce authentication temporarily unavailable. Please retry.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error consultando Salesforce: {e}")
    if not exists:
        raise HTTPException(status_code=404, detail="Account de Salesforce no encontrado")

@router.post("/link")
def link_site(body: LinkBody, request: Request, db: Session = Depends(get_db)):
    acc_id = (body.account_id or "").strip()
    if not acc_id or not (len(acc_id) in (15, 18)) or not acc_id.isalnum():
        raise HTTPException(400, detail="account_id inválido")

    site = db.query(Site).get(body.site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site no encontrado")

    _require_sf_ok(request, acc_id)

    site.salesforce_account_id = acc_id
    db.add(site)
    db.commit()

    return {"status": "ok", "site_id": site.id, "account_id": site.salesforce_account_id}

@router.post("/unlink")
def unlink_site(body: UnlinkBody, db: Session = Depends(get_db)):
    site = db.query(Site).get(body.site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site no encontrado")
    site.salesforce_account_id = None
    db.add(site)
    db.commit()
    return {"status": "ok", "site_id": site.id}

# --------------------------------------------------------------------------------------
# Preview del parseo (sin cambios de lógica, solo limpieza menor)
# --------------------------------------------------------------------------------------

@router.get("/preview/by-site/{site_id}")
def preview_latest_by_site(
    site_id: int,
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    sub = (
        db.query(Questionnaire.id)
        .filter(
            Questionnaire.site_id == site_id,
            Questionnaire.type == QuestionnaireType.qualification,
        )
        .order_by(
            Questionnaire.upload_date.desc().nullslast(),
            Questionnaire.id.desc(),
        )
        .limit(1)
        .subquery()
    )
    qid = db.query(sub.c.id).scalar()
    if not qid:
        return {"site_id": site_id, "questionnaire_id": None, "rows": []}

    rows = (
        db.query(
            Section.name.label("section"),
            Question.subsection.label("subsection"),
            Question.question_number.label("question_number"),
            Question.question_text.label("question_text"),
            Response.response_text.label("answer"),
        )
        .select_from(Section)
        .join(Question, Question.section_id == Section.id)
        .outerjoin(Response, Response.question_id == Question.id)
        .filter(Section.questionnaire_id == qid)
        .order_by(Section.section_order.asc(), Question.question_order.asc())
        .limit(limit)
        .all()
    )

    return {
        "site_id": site_id,
        "questionnaire_id": qid,
        "rows": [
            {
                "section": r.section,
                "subsection": r.subsection,
                "question_number": r.question_number,
                "question_text": r.question_text,
                "answer": r.answer,
            }
            for r in rows
        ],
    }

# --------------------------------------------------------------------------------------
# Delete por Questionnaire.id  ➜ borra Q&A y RECOMPONE site_qual del último Excel
# --------------------------------------------------------------------------------------

def _recompute_site_qual(db: Session, site_id: int) -> None:
    """
    Toma el último questionnaire 'qualification' del site y vuelve a generar
    el JSON aplanado en SiteQual. Si no hay, borra la fila de SiteQual.
    """
    # último questionnaire
    q = (
        db.query(Questionnaire.id)
        .filter(
            Questionnaire.site_id == site_id,
            Questionnaire.type == QuestionnaireType.qualification,
        )
        .order_by(Questionnaire.upload_date.desc().nullslast(), Questionnaire.id.desc())
        .first()
    )
    if not q:
        # no queda cuestionario => elimina SiteQual
        db.query(SiteQual).filter(SiteQual.site_id == site_id).delete(synchronize_session=False)
        return

    qid = q[0]
    # leemos preguntas/respuestas de ese questionnaire y aplanamos
    # IMPORTANTE: Incluir subsection para generar claves con prefijo de sección
    rows = (
        db.query(Question.question_text, Question.subsection, Response.response_text)
        .join(Section, Section.id == Question.section_id)
        .outerjoin(Response, Response.question_id == Question.id)
        .filter(Section.questionnaire_id == qid)
        .all()
    )
    flat: Dict[str, Any] = {}
    for qtext, subsection, ans in rows:
        # Extraer el código de subsección del formato "2.1 Title" -> "2_1"
        sub_code = "x"
        if subsection:
            match = re.match(r"^(\d+(?:\.\d+)*)\s+", subsection)
            if match:
                sub_code = match.group(1).replace(".", "_")
        
        slug = _slugify_question(qtext)
        key = f"{sub_code}__{slug}" if slug else None
        if key and ans not in (None, ""):
            flat[key] = str(ans)

    sq = db.execute(select(SiteQual).where(SiteQual.site_id == site_id)).scalar_one_or_none()
    if sq:
        sq.data = flat
    else:
        db.add(SiteQual(site_id=site_id, data=flat))

@router.delete("/delete/{questionnaire_id}")
def delete_qualification(questionnaire_id: int, db: Session = Depends(get_db)):
    q = (
        db.query(Questionnaire)
        .filter(Questionnaire.id == questionnaire_id, Questionnaire.type == QuestionnaireType.qualification)
        .first()
    )
    if not q:
        raise HTTPException(status_code=404, detail="Questionnaire not found")

    site_id = q.site_id

    # IDs para borrado jerárquico
    section_ids = [sid for (sid,) in db.query(Section.id).filter(Section.questionnaire_id == q.id).all()]
    if section_ids:
        question_ids = [qid for (qid,) in db.query(Question.id).filter(Question.section_id.in_(section_ids)).all()]
        if question_ids:
            db.query(Response).filter(Response.question_id.in_(question_ids)).delete(synchronize_session=False)
            db.query(Question).filter(Question.id.in_(question_ids)).delete(synchronize_session=False)
        db.query(Section).filter(Section.id.in_(section_ids)).delete(synchronize_session=False)

    db.query(Questionnaire).filter(Questionnaire.id == q.id).delete(synchronize_session=False)

    # 🔁 Recalcula/limpia el aplanado del site
    _recompute_site_qual(db, site_id)

    db.commit()
    return {"status": "deleted", "questionnaire_id": questionnaire_id}
