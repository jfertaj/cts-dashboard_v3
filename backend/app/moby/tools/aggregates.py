"""SQL + aggregate (group_count / group_count_agg / qual_search) tool implementations.

Pure move from `app.routers.ai_chat` (Phase 2 refactor). Behavior is
unchanged. Helpers (`_dbg`, `_ok_table`, `_resolve_metric`, `_pretty_label`)
are resolved lazily via `from app.routers import ai_chat as _ai` to avoid
an import cycle (ai_chat re-exports these tool functions back).

The functions are re-exported by `ai_chat.py`, so callers / tests that
import `app.routers.ai_chat.tool_sql_query` etc. keep working.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Literal, Optional
from app.moby.helpers.debug import _dbg
from app.moby.helpers.labels import _pretty_label
from app.moby.helpers.metrics import _resolve_metric
from app.moby.helpers.tables import _ok_table


from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session


def tool_sql_query(db: Session, sql: str, params: Optional[Dict[str, Any]] = None):

    _dbg("SQL >>> %s | params=%s", sql, params)
    if re.search(r"\b(insert|update|delete|alter|drop|create|grant|revoke|truncate)\b", sql, re.I):
        raise HTTPException(400, "Only read-only SELECT is allowed")

    suspects = re.findall(r"(?:from|join)\s+([A-Za-z0-9_\.]+)", sql, flags=re.I)
    for t in suspects:
        if not _ok_table(t):
            raise HTTPException(400, f"Table not allowed: {t}")

    # Geo guard: no Postgres geo/earthdistance functions (must use Explorer within-drive-km)
    geo_rx = re.compile(r"\bST_\w+\b|\bgeography\s*\(|\bpoint\s*\(|\bearthdistance\b|\bearth\s*\(|\bll_to_earth\b", re.I)
    if geo_rx.search(sql or ""):
        raise HTTPException(400, "Geospatial SQL is not allowed. Use explorer_within_drive_km for distance queries.")

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
        except Exception:
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


def tool_group_count(
    db: Session,
    by: List[Literal["country", "city"]],
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
            if op not in {">", "<", ">=", "<=", "=", "!="}:
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
        for i, b in enumerate(by):
            rr[b] = r.get(cols[i])
        result_rows.append(rr)
    return {"columns": [{"key": b, "label": b.title()} for b in by] + [{"key": "sites", "label": "Sites"}], "rows": result_rows}


def tool_group_count_agg(
    db: Session,
    by: List[Literal["country", "city"]],
    metric: Optional[str] = None,
    agg: Literal["avg", "sum", "ratio_exists"] = "avg",
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
            for i, b in enumerate(by):
                rr[b] = r.get(cols[i])
            res.append(rr)
        return {"columns": [{"key": b, "label": b.title()} for b in by] + [{"key": "ratio", "label": "Ratio"}], "rows": res}
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
        for i, b in enumerate(by):
            rr[b] = r.get(cols[i])
        res.append(rr)
    return {"columns": [{"key": b, "label": b.title()} for b in by] + [{"key": "value", "label": _pretty_label(f"qual.{k}") + f" ({agg})"}], "rows": res}


def tool_qual_search(
    db: Session,
    text: str,
    limit: int = 50,
):
    """Semantic search over qualification comments using GIN tsv index with ILIKE fallback."""
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
         OR public.qual_concat_comments(sq.data) ILIKE :pat
      ORDER BY rank DESC
      LIMIT :lim
    """
    out = tool_sql_query(db, sql, {"q": text, "pat": f"%{text}%", "lim": int(limit)})
    c = out.get("columns") or []
    rows = [{c[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
    return {
        "columns": [
            {"key": "account_id", "label": "Account Id"},
            {"key": "site", "label": "Account Name"},
            {"key": "country", "label": "Country"},
            {"key": "city", "label": "City"},
            {"key": "rank", "label": "Rank"},
            {"key": "snippet", "label": "Snippet"}
        ],
        "rows": rows,
    }
