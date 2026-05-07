"""Inline-blob parser for Moby chat responses.

Extracted from `app.routers.ai_chat` (Phase 5b refactor). Parses any
'table' or 'visualization' JSON the model pasted into raw text and
returns a normalized dict {answer, table, visualization}.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from app.moby.helpers.debug import _dbg


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
            pass  # No es JSON valido, continuar con el parsing normal

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
    # lineas basura como  ,,,,"rows":
    content = re.sub(r'(?m)^\s*[,"]*\s*rows\s*:\s*[,]?\s*$', "", content or "")
    # Propaga la version limpiada al texto de salida
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
    viz_obj = _json_or_none(viz)

    if table_obj:
        # normalizamos a forma esperada por el front
        cols = table_obj.get("columns") or table_obj.get("Cols") or table_obj.get("COLUMNS")
        rows = table_obj.get("rows") or table_obj.get("Rows") or table_obj.get("ROWS")
        if isinstance(cols, list) and isinstance(rows, list):
            out["table"] = {
                "columns": [{"key": c.get("key", c.get("name", str(i))), "label": c.get("label", c.get("name", c.get("key", str(i))))} if isinstance(c, dict) else {"key": str(c), "label": str(c)} for i, c in enumerate(cols)],
                "rows": rows,
            }
        # quitamos la seccion del texto
        out["answer"] = re.sub(r"\*\*table\*\*.*?\}", "", out["answer"], flags=re.I | re.S)
        out["answer"] = re.sub(r"\btable\s*:\s*\{.*?\}", "", out["answer"], flags=re.I | re.S)

    if viz_obj:
        if isinstance(viz_obj, dict):
            # si vino envuelto { type, xKey, yKeys, data, meta }
            out["visualization"] = viz_obj
        # quitamos la seccion del texto
        out["answer"] = re.sub(r"\*\*visualization\*\*.*?\}", "", out["answer"], flags=re.I | re.S)
        out["answer"] = re.sub(r"\bvisualization\s*:\s*\{.*?\}", "", out["answer"], flags=re.I | re.S)

    # Limpieza final de saltos/espacios sobrantes
    txt = out["answer"]

    # Elimina etiquetas sueltas <table> que quedaron antes del escape
    txt = re.sub(r"(?im)^\s*<table>\s*$", "", txt)
    txt = txt.replace("<table>", "")

    # --- Limpieza avanzada para quitar JSON crudo y artefactos ---
    # 1. Elimina bloques ```json ... ``` o ```...```
    txt = re.sub(r"```(?:json)?[\\s\\S]*?```", "", txt)
    # 2. SKIP: No eliminar todo el JSON, solo limpiamos residuos especificos abajo
    # 3. Elimina llaves o corchetes sueltos (pero no en lineas con contenido)
    txt = re.sub(r"(?m)^\\s*[{}\\[\\]]+\\s*$", "", txt)
    # NUEVO: elimina lineas basura tipo comas sueltas o claves estilo JSON
    #   - lineas que sean solo comas/quotes/espacios
    txt = re.sub(r"(?m)^\s*[,\"']+\s*$", "", txt)
    #   - lineas que empiecen como una clave JSON:  "algo":
    txt = re.sub(r'(?m)^\s*"[A-Za-z0-9_. -]+"\s*:\s*$', "", txt)
    #   - comas que queden solas entre saltos de linea
    txt = re.sub(r"(?m)^\s*,+\s*$", "", txt)
    #   - secuencias largas de comas: ,,,,,,,
    txt = re.sub(r",{3,}", "", txt)
    #   - palabras clave JSON sueltas: "rows":, "visualization":, etc.
    txt = re.sub(r'\b(?:rows|columns|table|visualization|data|meta)\s*:\s*,*', "", txt)
    # 4. Artefactos HTML/XML como <table>...</table>, <columns>, <rows>
    txt = re.sub(r"(?is)<table[\s\S]*?</table>", "", txt)
    txt = re.sub(r"(?is)<columns?[\s\S]*?</columns?>", "", txt)
    txt = re.sub(r"(?is)</?rows?[\s\S]*?>", "", txt)  # captura apertura y cierre
    txt = re.sub(r"(?is)<column[^>]*/?>", "", txt)
    txt = re.sub(r"(?is)<row[^>]*/?>", "", txt)
    # Eliminar tags sueltos como </rows> o <rows> que quedaron
    txt = re.sub(r"</?rows?>", "", txt)
    txt = re.sub(r"</?columns?>", "", txt)
    # 5. Restos tipo: ,,,, "rows":   o  "rows":  o  key="..." label="..."
    txt = re.sub(r'(?m)^\s*,*\s*"rows"\s*:\s*$', "", txt)
    txt = re.sub(r'\b(?:key|label)="[^"]*"', "", txt)
    # 6. Compacta multiples saltos de linea
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()

    # --- Normalizacion de estilo textual ---
    txt = re.sub(r"(?m)^\s*[-•]\s*", "• ", txt)
    txt = re.sub(r"(?m)^\s*\d+\.\s*", lambda m: f"{m.group(0).strip()} ", txt)
    txt = txt.strip()

    # --- Conversion a HTML con listas ordenadas y no ordenadas ---
    import html

    def _lines(s: str) -> list[str]:
        return [ln.strip() for ln in (s or "").split("\n")]

    lines = [ln for ln in _lines(txt) if ln]

    # Detect ordered list: lines like "1. ...", "2. ..."
    is_ordered = len(lines) >= 2 and all(re.match(r"^\d+\.\s+", ln) for ln in lines)
    # Detect unordered list: leading bullet or dash
    is_unordered = (not is_ordered) and len(lines) >= 2 and all(re.match(r"^(?:[-••])\s+", ln) for ln in lines)

    if is_ordered:
        items = [re.sub(r"^\d+\.\s+", "", ln) for ln in lines]
        safe_items = [html.escape(it) for it in items]
        safe = "<ol>" + "".join(f"<li>{it}</li>" for it in safe_items) + "</ol>"
    elif is_unordered:
        items = [re.sub(r"^(?:[-••])\s+", "", ln) for ln in lines]
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
