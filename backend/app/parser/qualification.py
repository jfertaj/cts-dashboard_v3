# backend/app/parser/qualification.py
import pandas as pd, re, os, sys
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict

# ========= DEBUG =========
QUAL_DEBUG = os.getenv("QUAL_DEBUG", "0") not in ("0", "", "false", "False")
def _dbg(*args):
    if QUAL_DEBUG:
        print("[qualification.py]", *args, file=sys.stderr)

META_KEYS = {
    "site": ["site", "centre", "center"],
    "street": ["address", "street"],
    "city": ["city", "town"],
    "country": ["country"],
    "postal_code": ["postal", "postcode", "zip"],
}

def _clean(x) -> str:
    return str(x).strip() if pd.notna(x) and str(x).strip() not in ("nan", "NaN") else ""

# =================== Metadatos ligeros ===================
def _find_meta(df: pd.DataFrame, max_rows: int = 30) -> Dict[str, Optional[str]]:
    meta = {k: None for k in ["site", "street", "city", "country", "postal_code"]}
    R = min(max_rows, len(df.index))
    for r in range(R):
        row_vals = [_clean(df.iloc[r, c]) for c in range(min(6, df.shape[1]))]
        joined = " ".join([v for v in row_vals if v]).lower()
        for key, variants in META_KEYS.items():
            if meta[key]:
                continue
            for v in variants:
                m = re.search(rf"\b{re.escape(v)}\b\s*:?\s*(.+)", joined, re.IGNORECASE)
                if m:
                    val = re.split(r"\s{2,}|\|", m.group(1).strip())[0]
                    if val:
                        meta[key] = val
                        break
        if sum(1 for v in meta.values() if v) >= 3:
            break
    return meta

# =================== Heurísticas de cabeceras ===================
_NUM_TOKEN_EXTRACT = re.compile(r"^\s*([0-9]+(?:\s*[.\u00B7\u2024\u2027\u2219]\s*[0-9]+)*\.?)\s*$")
_PART_RE  = re.compile(r"^\s*PART\s+([IVXLCDM]+)\s*(?:[–—-]\s*(.+))?$", re.IGNORECASE)

def _normalize_number_token(raw: str) -> Optional[str]:
    """Extrae algo tipo '3.6.4' aunque venga con espacios o puntos raros; quita punto final."""
    if not raw:
        return None
    m = _NUM_TOKEN_EXTRACT.match(str(raw))
    if not m:
        return None
    txt = m.group(1)
    txt = re.sub(r"[\s\u00A0]+", "", txt)  # quita espacios normales y NBSP
    txt = re.sub(r"[.\u00B7\u2024\u2027\u2219]", ".", txt)
    txt = txt.rstrip(".")
    return txt if re.match(r"^\d+(?:\.\d+)*$", txt) else None

def _detect_part_header(vals: List[str]) -> Optional[Tuple[str, str, str]]:
    """Busca 'PART <ROMANO> – <Título>' en las 3 primeras celdas."""
    for v in vals[:3]:
        t = _clean(v)
        if not t:
            continue
        m = _PART_RE.match(t)
        if m:
            roman = (m.group(1) or "").upper()
            title = (m.group(2) or "").strip()
            return (t, roman, title)
    return None

def _looks_like_title(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?") or ":" in t:
        return False
    words = t.split()
    return 1 <= len(words) <= 12

_CODE_AT_START = re.compile(r"^\s*(\d+(?:\.\d+)*)(?:\s+(.+))?$")
def _split_code_title(cell_text: str) -> tuple[Optional[str], Optional[str]]:
    t = _clean(cell_text)
    m = _CODE_AT_START.match(t or "")
    if not m:
        return (None, None)
    return (m.group(1), (m.group(2) or "").strip() or None)

# --- Disclaimer a la derecha que rompía la detección ---
_DISCLAIMER_RX = re.compile(
    r"\bonly\s+need\s+to\s+fill\s+in\s+temp\s+log\s+and\s+backup\s+plan.*",
    re.IGNORECASE
)
def _is_disclaimer(s: str) -> bool:
    return bool(_DISCLAIMER_RX.search((s or "").strip()))

def _normalize_subsection_title(title: str) -> str:
    """Quita el añadido 'only need to fill in temp log...' y espacios raros."""
    t = (title or "").strip()
    t = _DISCLAIMER_RX.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" -—–")
    return t

def _looks_like_subsection_row(c0: str, c1: str, rest: List[str]) -> Tuple[bool, str, str]:
    """
    Cabecera de subsección si:
    - A tiene código (3 / 3.5 / 3.5.1) — tolerando formatos raros
    - Título está en B o, si B vacío por merges, en C
    - El resto de la fila sólo puede contener vacío o el disclaimer
    """
    code_norm = _normalize_number_token(c0)

    if not code_norm:
        # Soporta "3.6.4 <título>" si vino todo en B por merges
        code_b, raw_t1 = _split_code_title(c1)
        t1 = _normalize_subsection_title(raw_t1 or "")
        if code_b and _looks_like_title(t1) and not any(_clean(x) and not _is_disclaimer(x) for x in rest):
            full_title = f"{code_b} {t1}".strip()
            _dbg("SUBSECTION (B-merged):", full_title)
            return (True, full_title, code_b)
        return (False, "", "")

    c2 = rest[0] if len(rest) > 0 else ""
    tail = rest[1:] if len(rest) > 1 else []
    if any((_clean(x) and not _is_disclaimer(x)) for x in tail):
        return (False, "", "")

    cand_b = _normalize_subsection_title(c1)
    cand_c = _normalize_subsection_title(c2)

    if _looks_like_title(cand_b):
        title = cand_b
    elif not _clean(c1) and _looks_like_title(cand_c):
        title = cand_c
    else:
        return (False, "", "")

    full_title = f"{code_norm} {title}".strip()
    _dbg("SUBSECTION:", full_title)
    return (True, full_title, code_norm)

# =================== Tipado de respuestas ===================
BOOL_TRUE  = {"yes","y","true","si","sí","oui","ja","verdadero","afirmativo","ok"}
BOOL_FALSE = {"no","n","false","non","nein","falso","negativo"}

def _to_number(s: str) -> Optional[float]:
    if not s:
        return None
    t = s.strip()
    t = re.sub(r"[^\d,.\-%+]", "", t)
    pct = t.endswith("%")
    if pct:
        t = t[:-1]
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    else:
        if "," in t and "." not in t:
            t = t.replace(",", ".")
    try:
        val = float(t)
    except Exception:
        return None
    return val / 100.0 if pct else val

def _detect_and_coerce_answer(raw: str) -> Tuple[str, Optional[object], Optional[str]]:
    s = (raw or "").strip()
    if not s or s.lower() in {"na","n/a","--","-","none","null"}:
        return ("null", None, None)
    low = s.lower()
    if low in BOOL_TRUE:
        return ("boolean", True, None)
    if low in BOOL_FALSE:
        return ("boolean", False, None)
    unit = "%" if s.strip().endswith("%") else None
    num = _to_number(s)
    if num is not None:
        if unit == "%" or abs(num - round(num)) > 1e-9:
            return ("number", float(num), unit)
        return ("integer", int(round(num)), unit)
    return ("string", None, None)

# -------- slug util --------
_slug_bad = re.compile(r"[^a-z0-9]+")
def _slugify(s: str) -> str:
    s = (s or "").lower().strip()
    s = _slug_bad.sub("_", s).strip("_")
    return s[:140]

# =================== Dispositivos ===================
# Secciones que tratamos como "con dispositivos"
DEVICE_SECTIONS = {"3.5.4", "3.6.4", "3.7.1", "3.6.5", "3.7.2"}

# Overrides específicos por subcódigo y slug base
COMMENT_LABEL_OVERRIDES = {
    "2.1": {
        "comments": "Comments on SOPs or general",
    },
    "2.2": {
        "comments": "Comments on consent process or age of enrollment",
    },
    "2.4": {
        "comments": "Comments on Contracting Process",
    },
    "3.8": {
        "comments": [
            "Comments on the site lab resources or accreditation",
            "Comments on the lab capability",
        ],
    },
}

DESCRIBE_LABEL_OVERRIDES = {
    "3.4": {
        "describe": [
            "Describe the site's experience Managing serious AEs",
            "Describe the site's experience with Phase 1 or Islet Transplant",
        ],
    },
    "3.5.1": {
        "describe": "Description of how source data is captured",
    },
}

GENERIC_LABEL_OVERRIDES: Dict[str, Dict[str, List[str] | str]] = {
    "3.7.2": {
        "how_often": "How often are regular device checks conducted?",
        "by_whom": "Who conducts regular device checks?",
        "certificate_label_provided_by_vendor_to_site": "Are certificates provided after routine device checks?",
    },
    "3.8": {
        "availability": "Is the lab available centrally or locally?",
        "certificate_available": "Do you have a local or external lab certificate available?",
        "is_there_an_sop": "Is there an SOP for preparing blood samples?",
        "describe_process": "Describe process for preparing blood samples",
        "fbc": "Are FBC tests available?",
        "hba1c": "Is HbA1c testing available?",
        "c_peptide": "Is C-peptide testing available?",
        "glucose": "Is glucose testing available?",
        "insulin": "Are insulin tests available?",
        "insulin_2": "Are insulin autoantibody tests available?",
        "peripheral_blood_mononuclear_cells": "Are PBMC tests available?",
        "autoantibodies": "Are autoantibody tests available?",
        "gad65": "Are GAD65 tests available?",
        "znt8": "Are ZnT8 tests available?",
        "ia_2": "Are IA-2 tests available?",
    },
}

SPECIFIC_LABEL_OVERRIDES: Dict[str, str] = {
    "3_7_1__fridge__available": "Fridge — available (2-8ºC)",
    "3_7_1__fridge__backup_plan_for_fridge_failure": "Fridge — Backup plan for fridge failure (2-8ºC)",
}

IMP_SECTION_PREFIXES = {
    "3.5.4": "IMP ",
}

DEVICE_TEMPERATURE_SUFFIX = {
    "3.5.4": {
        "room_temperature": " (RT)",
        "fridge": " (2-8ºC)",
        "standard_freezer": " (-20 - -10ºC)",
        "medication_freezer": " (-69ºC)",
    },
    "3.7.1": {
        "fridge": " (2-8ºC)",
        "standard_freezer": " (-20 - -10ºC)",
        "specimen_freezer": " (-69ºC)",
    },
}

# Patrones de detección de cabecera de dispositivo en la columna B
# (para 3.6.5 / 3.7.2 añadimos dispositivos de monitorización/equipamiento general)
DEVICE_PATTERNS = [
    (re.compile(r"\broom\s*temperature\b", re.I), "room_temperature"),
    (re.compile(r"\bfridge\b|\brefrigerator\b", re.I), "fridge"),
    (re.compile(r"\bstandard\s*freezer\b", re.I), "standard_freezer"),
    (re.compile(r"\bmedication\s*freezer\b|\b<\s*-?\s*69", re.I), "medication_freezer"),
    (re.compile(r"\bspecimen\s*freezer\b", re.I), "specimen_freezer"),
    (re.compile(r"\bultra[-\s]*low\b|\b-80\s*°?c\b", re.I), "ultra_low_freezer"),
    # Equipos adicionales
    (re.compile(r"\becg\b|\belectrocardiogram", re.I), "ecg"),
    (re.compile(r"\bcentrifuge\b", re.I), "centrifuge"),
    (re.compile(r"\b(blood\s*pressure|sphygmo)", re.I), "blood_pressure_monitor"),
    (re.compile(r"\b(oxygen\s*saturation|pulse\s*ox(imeter)?)\b", re.I), "oxygen_saturation_monitor"),
    (re.compile(r"\b(respiratory\s*rate|respiration\s*monitor)\b", re.I), "respiratory_rate_monitor"),
]

DEVICE_PRETTY = {
    "room_temperature": "Room Temperature",
    "fridge": "Fridge",
    "standard_freezer": "Standard Freezer",
    "medication_freezer": "Medication Freezer",
    "specimen_freezer": "Specimen Freezer",
    "ultra_low_freezer": "Ultra Low Freezer",
    "ecg": "ECG device",
    "centrifuge": "Centrifuge",
    "blood_pressure_monitor": "Blood pressure monitor",
    "oxygen_saturation_monitor": "Monitor for oxygen saturation",
    "respiratory_rate_monitor": "Monitor for respiratory rate",
}

def _normalize_device_header(text: str) -> Optional[str]:
    # quita "(2-8ºC)" etc. antes de buscar patrón
    t = re.sub(r"\([^)]*\)", "", _clean(text))
    for rx, name in DEVICE_PATTERNS:
        if rx.search(t):
            return name
    return None

# =================== Canonicalización de preguntas ===================
_CANON_RULES = [
    # temperatura
    (re.compile(r"^\s*temp(?:erature)?\s*log\s*:?\s*$", re.I), "Temperature log"),
    # lockable
    (re.compile(r"^\s*lockable(?:\s*\(.*\))?\s*\??\s*$", re.I), "Lockable?"),
    # alarmas
    (re.compile(r"^\s*automated\s*alarm\s*system.*", re.I), "Automated alarm system?"),
    # backup plan
    (re.compile(r"^\s*backup\s*plan.*(freezer|monitor|device).*$", re.I), "Backup plan for device failure"),
    # certificado/etiqueta
    (re.compile(r"^\s*certificate\s*/?\s*label\s*available\??\s*$", re.I), "Certificate/label available?"),
    # "available" lo tratamos aparte, ver más abajo
]

def _canonicalize_question(q: str) -> str:
    qq = (q or "").strip()
    # si viene como "X — available" no lo tocamos (es canónico ya)
    if "— available" in qq:
        return qq
    for rx, canon in _CANON_RULES:
        if rx.match(qq):
            return canon
    # pequeñas limpiezas
    qq = re.sub(r"\s*:\s*$", "", qq)  # quita ":" al final
    qq = re.sub(r"\s+", " ", qq).strip()
    return qq

# =================== Parser principal ===================
def parse_qualification_checklist(flike, filename: str = "") -> Dict[str, Any]:
    """
    Devuelve:
      {
        "meta": {...},
        "rows": [{
          section, subsection, subsection_code, section_roman,
          question_number, question_text, question_key,
          parent_question_key, answer, answer_type, answer_norm, unit
        }, ...]
      }

    Reglas:
      - Ignora PART IV y PART V.
      - Preguntas SIEMPRE en columna B; respuestas en C.
      - En 3.5.4/3.6.4/3.7.1 y también 3.6.5/3.7.2 detecta devices en B y crea contexto de device.
      - Sub-preguntas “If no/If yes/If … explain” quedan con parent_question_key
        apuntando a la última pregunta principal previa.
    """
    df = pd.read_excel(flike, sheet_name=0, header=None, engine="openpyxl").fillna("")

    # metadatos + versión
    meta = _find_meta(df)
    mver = re.search(r"\bV(?:er\.?|ersion)?\s*([0-9]+(?:\.[0-9]+)*)", filename, re.IGNORECASE)
    if mver:
        meta.setdefault("version", mver.group(1))

    rows: List[Dict[str, Any]] = []
    current_section: Optional[str] = None
    current_section_roman: Optional[str] = None
    current_subsection: Optional[str] = None
    current_sub_code: Optional[str] = None
    seen_any_part = False
    skip_due_to_part = False

    # Contexto especial para devices y para sub-preguntas condicionales
    current_device: Optional[str] = None
    last_main_qkey: Optional[str] = None  # para "If no, explain" etc.
    label_counters = defaultdict(int)

    def _is_conditional_follow_up(qtext: str) -> bool:
        t = (qtext or "").strip().lower()
        return (
            t.startswith("if no") or t.startswith("if yes")
            or t.startswith("if so") or t.startswith("if not")
            or t.startswith("if ") or "explain" in t
        )

    def _mk_key(sub_code: str, qtext: str, device: Optional[str] = None) -> str:
        base = _slugify(qtext)
        if device:
            return f"{sub_code}__{device}__{base}"
        return f"{sub_code}__{base}"

    for r in range(df.shape[0]):
        # Solo miramos las primeras columnas (A..E)
        c0 = _clean(df.iat[r, 0]) if 0 < df.shape[1] else ""
        c1 = _clean(df.iat[r, 1]) if 1 < df.shape[1] else ""
        c2 = _clean(df.iat[r, 2]) if 2 < df.shape[1] else ""
        c3 = _clean(df.iat[r, 3]) if 3 < df.shape[1] else ""
        c4 = _clean(df.iat[r, 4]) if 4 < df.shape[1] else ""
        row_vals = [c0, c1, c2, c3, c4]

        # 1) Section (PART …)
        part_info = _detect_part_header(row_vals)
        if part_info:
            part_header, roman, _title = part_info
            current_section = part_header
            current_section_roman = roman
            current_subsection = None
            current_sub_code = None
            current_device = None
            last_main_qkey = None
            seen_any_part = True
            skip_due_to_part = roman in {"IV", "V"}
            _dbg("PART:", part_header)
            continue

        if not seen_any_part or skip_due_to_part:
            continue

        # 2) Sub(sección)
        is_sub, sub_title, sub_code = _looks_like_subsection_row(c0, c1, [c2, c3, c4])
        if is_sub:
            current_subsection = sub_title
            current_sub_code = sub_code
            current_device = None
            last_main_qkey = None
            continue

        # 3) Si no hay subsección aún…
        if not current_sub_code:
            # …en PART I aceptamos preguntas B->C con un código sintético
            if (current_section_roman or "").upper() == "I":
                current_subsection = current_subsection or (current_section or "PART I")
                current_sub_code = "I"
            else:
                continue

        # 4) Detección de device (en B) para secciones con devices
        if current_sub_code in DEVICE_SECTIONS:
            maybe_dev = _normalize_device_header(c1)
            if maybe_dev:
                current_device = maybe_dev
                last_main_qkey = None
                pretty = DEVICE_PRETTY.get(current_device, c1)
                _dbg(f"DEVICE header in {current_sub_code}:", c1, "->", current_device)
                # si además hay respuesta en C, guarda 'device_available'
                if c2:
                    a_type, a_norm, unit = _detect_and_coerce_answer(c2)
                    q_text = f"{pretty} — available"
                    qkey = _mk_key(current_sub_code, "device_available", current_device)
                    rows.append({
                        "section": current_section,
                        "section_roman": current_section_roman or "",
                        "subsection": current_subsection or "",
                        "subsection_code": current_sub_code or "",
                        "question_number": None,
                        "question_text": q_text,  # canónico: "<Device> — available"
                        "question_key": qkey,
                        "parent_question_key": None,
                        "answer": c2,
                        "answer_type": a_type,
                        "answer_norm": a_norm,
                        "unit": unit,
                    })
                    last_main_qkey = qkey
                # esta fila es cabecera de device
                continue

        # 5) Pregunta/Respuesta (SIEMPRE B->C)
        question_text = c1
        answer = c2

        # sin pregunta, nada que guardar
        if not question_text:
            continue

        # Evita colar la propia cabecera de subsección si venía “reimpresa” en B
        if _split_code_title(question_text)[0]:
            continue

        # Canonicaliza el texto de la pregunta
        qtext_canon = _canonicalize_question(question_text)

        # Tipado y clave
        a_type, a_norm, unit = _detect_and_coerce_answer(answer)
        sub_code_safe = current_sub_code or "x"
        base_slug = _slugify(qtext_canon)
        occ_index = label_counters[(sub_code_safe, base_slug)]
        label_counters[(sub_code_safe, base_slug)] = occ_index + 1

        # follow-up condicional vinculado
        is_follow_up = _is_conditional_follow_up(qtext_canon)

        # Si hay device en contexto, prefijamos el texto con "<Device> — "
        display_text = qtext_canon
        if current_sub_code in DEVICE_SECTIONS and current_device:
            pretty = DEVICE_PRETTY.get(current_device, current_device)
            display_text = f"{pretty} — {qtext_canon}"

        qkey = _mk_key(sub_code_safe, qtext_canon, current_device)

        # Overrides para Comments / Describe / genéricas
        if base_slug == "comments":
            overrides = COMMENT_LABEL_OVERRIDES.get(sub_code_safe)
            if overrides:
                mapped = overrides.get("comments")
                if isinstance(mapped, list):
                    if occ_index < len(mapped):
                        display_text = mapped[occ_index]
                elif isinstance(mapped, str):
                    display_text = mapped
        elif base_slug == "describe":
            overrides = DESCRIBE_LABEL_OVERRIDES.get(sub_code_safe)
            if overrides:
                entry = overrides.get("describe") if isinstance(overrides, dict) else overrides
                if isinstance(entry, list):
                    if occ_index < len(entry):
                        display_text = entry[occ_index]
                elif isinstance(entry, str):
                    display_text = entry

        generic_map = GENERIC_LABEL_OVERRIDES.get(sub_code_safe)
        if generic_map and base_slug in generic_map:
            mapped = generic_map[base_slug]
            if isinstance(mapped, list):
                if occ_index < len(mapped):
                    display_text = mapped[occ_index]
            else:
                display_text = mapped

        # Prefijo IMP en 3.5.4
        if sub_code_safe in IMP_SECTION_PREFIXES and not display_text.lower().startswith("imp "):
            display_text = f"{IMP_SECTION_PREFIXES[sub_code_safe]}{display_text}"

        # Sufijos de temperatura en subsecciones con devices
        if current_sub_code in DEVICE_SECTIONS and current_device:
            temp_suffix_map = DEVICE_TEMPERATURE_SUFFIX.get(current_sub_code, {})
            suffix = temp_suffix_map.get(current_device)
            if suffix and not display_text.endswith(suffix):
                display_text = f"{display_text}{suffix}"

        specific_label = SPECIFIC_LABEL_OVERRIDES.get(qkey)
        if specific_label:
            display_text = specific_label

        parent_key = None
        if is_follow_up and last_main_qkey:
            parent_key = last_main_qkey

        rows.append({
            "section": current_section,
            "section_roman": current_section_roman or "",
            "subsection": current_subsection or "",
            "subsection_code": sub_code_safe,
            "question_number": None,
            "question_text": display_text,
            "question_key": qkey,
            "parent_question_key": parent_key,
            "answer": answer,
            "answer_type": a_type,
            "answer_norm": a_norm,
            "unit": unit,
        })

        if not is_follow_up:
            last_main_qkey = qkey

    return {"meta": meta, "rows": rows}
