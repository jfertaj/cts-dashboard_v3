"""Data-manipulation tool implementations.

Pure move from `app.routers.ai_chat` (Phase 4 refactor). Behavior is
unchanged. Re-exported by `ai_chat.py` so existing call sites and test
mocks (`@patch("app.routers.ai_chat.tool_manipulate_data")`) keep working.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import Request

from app.moby.helpers.debug import _dbg


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
