# app/api/upload.py
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_db
from app import models
from app.models import QuestionnaireType, SiteQual, Questionnaire
from app.parser.profiling import parse_profiling_questionnaire
from app.parser.qualification import parse_qualification_checklist
from app.crud import ingest_data

import io
import hashlib
import re
import logging
from typing import BinaryIO

router = APIRouter()
log = logging.getLogger("cts-backend")


def compute_file_hash(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def stream_sha256(f: BinaryIO, chunk_size: int = 1024 * 1024) -> str:
    """
    Calcula SHA-256 de un file-like sin cargarlo entero en memoria.
    Mantiene el puntero original (seek back).
    """
    pos = f.tell()
    try:
        f.seek(0)
    except Exception:
        pos = None  # si no soporta seek, no podremos restaurar
    h = hashlib.sha256()
    while True:
        chunk = f.read(chunk_size)
        if not chunk:
            break
        h.update(chunk)
    if pos is not None:
        try:
            f.seek(0)
        except Exception:
            pass
    return h.hexdigest()

# ---------------- helpers ----------------
_slug_re_1 = re.compile(r"\([^)]*\)")
_slug_re_2 = re.compile(r"[^a-z0-9]+")
_CODE_RE   = re.compile(r"^\s*(\d+(?:\.\d+)*)\b")

YES_NO = {"yes","no","sí","si","y","n","true","false"}

def _slugify(q: str) -> str:
    s = (q or "").strip().lower()
    s = _slug_re_1.sub("", s)
    s = _slug_re_2.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "q"

def _coerce_from_row(row: dict):
    a_type = (row.get("answer_type") or "").lower()
    if a_type == "boolean":
        return bool(row.get("answer_norm"))
    if a_type == "integer":
        try: return int(row.get("answer_norm"))
        except Exception: pass
    if a_type == "number":
        try: return float(row.get("answer_norm"))
        except Exception: pass
    ans = (row.get("answer") or "").strip()
    return ans if ans != "" else None

def _normalize_key(k: str) -> str:
    k = (k or "").lower().replace(".", "_")
    k = re.sub(r"[^a-z0-9_]+", "_", k)
    k = re.sub(r"_+", "_", k).strip("_")
    return k

def _guess_subcode(row: dict) -> str | None:
    sc = (row.get("subsection_code") or "").strip()
    if sc: return sc
    sub = (row.get("subsection") or "").strip()
    m = _CODE_RE.match(sub)
    if m: return m.group(1)
    qn = (row.get("question_number") or "").strip()
    m2 = _CODE_RE.match(qn)
    if m2: return m2.group(1)
    return None

# --------- NUEVO: sanear nombre de sitio si viene "YES/NO" o vacío ----------
_site_from_fname_re = re.compile(
    r"^\s*(.+?)\s*[_-]\s*Qualification\s+Visit\s+Audit\s+Checklist", re.I
)

def _guess_site_from_filename(filename: str) -> str | None:
    base = (filename or "").rsplit(".", 1)[0]
    m = _site_from_fname_re.search(base)
    if m:
        guess = m.group(1).strip(" _-")
        # Típicos patrones “City_Country …”
        return guess.replace("_", " ").replace("-", " ").strip()
    # fallback: primer token antes de espacio grande
    return base.split("Qualification",1)[0].replace("_"," ").replace("-"," ").strip() or None

def _sanitize_site_meta(parsed: dict, filename: str) -> None:
    meta = parsed.setdefault("meta", {})
    raw = (meta.get("site_name") or "").strip()
    raw_l = raw.lower()
    if (not raw) or (raw_l in YES_NO):
        guess = _guess_site_from_filename(filename)
        if guess:
            log.info("site_name fixed from filename: %r -> %r", raw, guess)
            meta["site_name"] = guess

@router.post("/")
async def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    # Validación rápida y hash streaming (sin duplicar memoria)
    # UploadFile.file es (normalmente) un SpooledTemporaryFile (seekable)
    base_fp = file.file  # BinaryIO
    try:
        # si el cliente subió 0 bytes
        base_fp.seek(0, 2)  # EOF
        if base_fp.tell() == 0:
            raise HTTPException(status_code=400, detail="Empty file.")
        base_fp.seek(0)
    except Exception:
        # fallback a leer un pequeño chunk para validar
        preview = await file.read(1)
        if not preview:
            raise HTTPException(status_code=400, detail="Empty file.")
        # reinyecta el byte leído
        base_fp.write(preview)
        base_fp.seek(0)

    # Hash sin cargar en memoria
    file_hash = stream_sha256(base_fp)

    # Duplicados por hash
    existing = db.query(models.Questionnaire).filter_by(file_hash=file_hash).first()
    if existing:
        raise HTTPException(status_code=400, detail="This file was already uploaded.")

    # Parser
    name_l = (file.filename or "").lower()
    # Intentamos dar al parser un file-like; si tus parsers ya aceptan file-like
    # esto evita una copia completa en memoria. Si no, caemos a BytesIO.
    parsed = None
    qtype = QuestionnaireType.qualification if "qualification" in name_l else QuestionnaireType.profiling
    try:
        base_fp.seek(0)
        if qtype == QuestionnaireType.qualification:
            parsed = await run_in_threadpool(parse_qualification_checklist, base_fp, file.filename)
        else:
            parsed = await run_in_threadpool(parse_profiling_questionnaire, base_fp, file.filename)
    except TypeError:
        # fallback: el parser requiere bytes -> una sola copia controlada
        base_fp.seek(0)
        contents = base_fp.read()
        bio = io.BytesIO(contents)
        if qtype == QuestionnaireType.qualification:
            parsed = await run_in_threadpool(parse_qualification_checklist, bio, file.filename)
        else:
            parsed = await run_in_threadpool(parse_profiling_questionnaire, bio, file.filename)
 

    if not parsed:
        raise HTTPException(status_code=400, detail="Parsing failed")

    # Asegura que el nombre de sitio sea razonable (evitar “YES”)
    _sanitize_site_meta(parsed, file.filename or "")

    # Log diagnóstico
    try:
        rows_parsed = parsed.get("rows") or []
        has_qk = sum(1 for r in rows_parsed if (r.get("question_key") or "").strip())
        has_subc = sum(1 for r in rows_parsed if (r.get("subsection_code") or "").strip())
        sig = (parsed.get("meta") or {}).get("parser_signature")
        log.info("ingest:%s rows=%s | with question_key=%s | with subsection_code=%s | signature=%s",
                 qtype.value, len(rows_parsed), has_qk, has_subc, sig)
    except Exception:
        pass

    # Inserta TODO en tablas normalizadas → el PREVIEW verá también PART I y V
    try:
        # Ingesta pesada en threadpool (no bloquea el event loop)
        await run_in_threadpool(
            ingest_data,
            parsed_data=parsed,
            questionnaire_type=qtype,
            file_name=file.filename,
            db=db,
            file_hash=file_hash,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database update failed: {str(e)}")

    # Qualification: aplanado para SiteQual (ocultando PART I & V)
    if qtype == QuestionnaireType.qualification:
        q: Questionnaire = (
            db.query(Questionnaire)
            .filter(Questionnaire.file_hash == file_hash)
            .first()
        )
        if not q:
            return {
                "status": "success",
                "filename": file.filename,
                "form_type": qtype.value,
                "warn": "Questionnaire not found after ingest; SiteQual not updated.",
            }

        site_id = q.site_id
        flat: dict = {}
        seen_keys = set()

        # for r in (parsed.get("rows") or []):
        #     section_name = (r.get("section") or "").strip().lower()
        #     # 👇 Aquí escondemos Part I y Part V del EXPLORER (columnas/filtros)
        #     if section_name.startswith("part i") or section_name.startswith("part v"):
        #         continue

        #     qkey = (r.get("question_key") or "").strip()
        #     if qkey:
        #         key_base = _normalize_key(qkey)
        #     else:
        #         subcode = _guess_subcode(r)
        #         qtxt = r.get("question_text") or ""
        #         if subcode:
        #             key_base = f"{subcode.replace('.', '_')}__{_slugify(qtxt)}"
        #         else:
        #             key_base = _slugify(qtxt)
        #         key_base = _normalize_key(key_base)

        #     if not key_base:
        #         continue

        #     key = key_base
        #     i = 2
        #     while key in seen_keys:
        #         key = f"{key_base}_{i}"
        #         i += 1
        #     seen_keys.add(key)

        #     flat[key] = _coerce_from_row(r)

        # sq: SiteQual = db.query(SiteQual).filter(SiteQual.site_id == site_id).first()
        # if sq is None:
        #     sq = SiteQual(site_id=site_id, data=flat)
        #     db.add(sq)
        # else:
        #     d = dict(sq.data or {})
        #     d.update(flat)
        #     sq.data = d
        #     db.add(sq)

        # db.commit()

        # precalcula flags para evitar .lower() por fila
        rows_parsed = parsed.get("rows") or []
        for r in rows_parsed:
            section = (r.get("section") or "").strip()
            section_l = section.lower()
            # 👇 ocultar Part I / V en EXPLORER
            if section_l.startswith("part i") or section_l.startswith("part v"):
                continue

            qkey = (r.get("question_key") or "").strip()
            if qkey:
                key_base = _normalize_key(qkey)
            else:
                subcode = _guess_subcode(r)
                qtxt = r.get("question_text") or ""
                if subcode:
                    key_base = f"{subcode.replace('.', '_')}__{_slugify(qtxt)}"
                else:
                    key_base = _slugify(qtxt)
                key_base = _normalize_key(key_base)

            if not key_base:
                continue

            key = key_base
            i = 2
            while key in seen_keys:
                key = f"{key_base}_{i}"
                i += 1
            seen_keys.add(key)

            flat[key] = _coerce_from_row(r)

        # Una sola transacción/commit para SiteQual
        with db.begin():
            sq: SiteQual = db.query(SiteQual).filter(SiteQual.site_id == site_id).first()
            if sq is None:
                sq = SiteQual(site_id=site_id, data=flat)
                db.add(sq)
            else:
                d = dict(sq.data or {})
                d.update(flat)
                sq.data = d
                db.add(sq)

    return {
        "status": "success",
        "filename": file.filename,
        "form_type": qtype.value,
    }