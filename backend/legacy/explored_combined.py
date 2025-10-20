from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Any, Literal, Dict, Set
import os, json, logging, httpx

from app.database import SessionLocal
from app.models import (
    QualificationRecord,
    StructuredQuestion,
    SalesforceToken,
    SalesforceAccount,
    AccountGeolocation,
)
from app.settings import settings

router = APIRouter()
logger = logging.getLogger(__name__)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class ExplorerFilter(BaseModel):
    field: str
    operator: str
    value: Any

class ExplorerQuery(BaseModel):
    logic: Literal["AND", "OR"] = "AND"
    rules: List[ExplorerFilter] = []
    columns: List[str] = []

def load_profiling_fields() -> List[Dict[str, Any]]:
    try:
        base_dir = os.path.dirname(__file__)
        path = os.path.join(base_dir, "static", "fields_label_name_opportunity.json")
        with open(path, "r") as f:
            raw = json.load(f)
            return raw["fields"] if "fields" in raw else raw
    except Exception as e:
        logger.exception("Error al leer campos profiling: %s", e)
        return []

def _escape_soql_value(val: Any) -> str:
    # Manejo simple: si es numérico, devuélvelo tal cual; si no, comillas con escape
    if isinstance(val, (int, float)) or (isinstance(val, str) and val.replace(".", "", 1).isdigit()):
        return str(val)
    if isinstance(val, str):
        return "'" + val.replace("'", "\\'") + "'"
    # Fechas ISO → comillas
    return "'" + str(val).replace("'", "\\'") + "'"

def _build_soql_filters(filters: List[ExplorerFilter], allowed_fields: Set[str]) -> List[str]:
    clauses = ["Type = 'CTS Profiling'", "StageName != 'Closed Lost'"]
    allowed_ops = {"equals", "not_equals", "contains", "starts_with", "ends_with", ">", "<", ">=", "<=", "between"}

    for rule in filters:
        if rule.field.startswith("question::"):
            continue  # se manejan en qualification
        if rule.operator not in allowed_ops:
            continue
        if rule.field not in allowed_fields:
            continue

        clause = None
        if rule.operator == "equals":
            clause = f"{rule.field} = {_escape_soql_value(rule.value)}"
        elif rule.operator == "not_equals":
            clause = f"{rule.field} != {_escape_soql_value(rule.value)}"
        elif rule.operator == "contains":
            clause = f"{rule.field} LIKE '%' || { _escape_soql_value(rule.value).strip(\"'\") } || '%'"
            # NOTA: Salesforce NO soporta ||. Alternativa portable:
            # clause = f"{rule.field} LIKE '%{str(rule.value).replace(\"'\", \"\\'\\")}%'"  # simple
            clause = f"{rule.field} LIKE '%{str(rule.value).replace(\"'\",\"\\'\\")}%'"  # usar LIKE directo
        elif rule.operator == "starts_with":
            clause = f"{rule.field} LIKE '{str(rule.value).replace(\"'\",\"\\'\\")}%'" 
        elif rule.operator == "ends_with":
            clause = f"{rule.field} LIKE '%{str(rule.value).replace(\"'\",\"\\'\\")}'"
        elif rule.operator in {">", "<", ">=", "<="}:
            clause = f"{rule.field} {rule.operator} {_escape_soql_value(rule.value)}"
        elif rule.operator == "between" and isinstance(rule.value, list) and len(rule.value) == 2:
            lo = _escape_soql_value(rule.value[0])
            hi = _escape_soql_value(rule.value[1])
            clause = f"{rule.field} >= {lo} AND {rule.field} <= {hi}"

        if clause:
            clauses.append(clause)
    return clauses

@router.post("/explorer/combined")
async def get_combined_explorer_data(
    query: ExplorerQuery,
    db: Session = Depends(get_db),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    logger.info("Explorador combinado → logic=%s, rules=%s, columns=%s, limit=%s, offset=%s",
                query.logic, len(query.rules), query.columns, limit, offset)

    logic = query.logic.upper()
    filters = query.rules or []
    columns = set(query.columns or [])

    profiling_fields = load_profiling_fields()
    profiling_field_names = {f["name"] for f in profiling_fields if "name" in f}

    # Campos mínimos que siempre pedimos a SF
    base_soql_fields = {"Id", "Name", "StageName", "CloseDate", "AccountId"}
    requested_profiling_fields = profiling_field_names.intersection(columns)
    soql_fields = base_soql_fields | set(requested_profiling_fields)

    # Whitelist de campos filtrables: (base + profiling fields del JSON)
    allowed_filter_fields = base_soql_fields | profiling_field_names

    # SOQL
    soql_clauses = _build_soql_filters(filters, allowed_filter_fields)
    soql = f"SELECT {', '.join(sorted(soql_fields))} FROM Opportunity WHERE {' AND '.join(soql_clauses)} LIMIT {limit} OFFSET {offset}"
    logger.debug("SOQL: %s", soql)

    # Token
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        raise HTTPException(status_code=403, detail="No Salesforce token found")

    # Consulta a Salesforce
    profiling_data: List[Dict[str, Any]] = []
    total_profiling = 0
    try:
        url = f"{token.instance_url}/services/data/{settings.SALESFORCE_API_VERSION}/query"
        headers = {"Authorization": f"Bearer {token.access_token}"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.get(url, headers=headers, params={"q": soql})
        res.raise_for_status()
        body = res.json()
        profiling_data = body.get("records", []) or []
        # NOTA: query endpoint no devuelve total sin hacer queryMore/COUNT. Lo omitimos o hace falta otra llamada.
        total_profiling = len(profiling_data)
        logger.info("Oportunidades profiling recuperadas: %s", len(profiling_data))
    except Exception as e:
        logger.exception("Error consultando Salesforce: %s", e)

    profiling_by_account = {opp.get("AccountId"): opp for opp in profiling_data if opp.get("AccountId")}
    profiling_ids = {k for k in profiling_by_account.keys() if k}

    # Qualification
    qualification_records = db.query(QualificationRecord).all()
    qualification_by_account = {}
    for record in qualification_records:
        if record.salesforce_account_id:
            qualification_by_account.setdefault(record.salesforce_account_id, []).append(record)

    question_filters = [f for f in filters if f.field.startswith("question::")]
    matching_qualification_ids = set()

    if question_filters:
        logger.info("Aplicando filtros sobre preguntas qualification…")
        # optimizar: traer solo preguntas de records implicados
        all_questions = db.query(StructuredQuestion).all()
        for q in all_questions:
            for rule in question_filters:
                key = rule.field.replace("question::", "")
                if q.question_text == key:
                    ans = q.answer or ""
                    val = str(rule.value or "")
                    match = (
                        (rule.operator == "equals" and ans == val) or
                        (rule.operator == "not_equals" and ans != val) or
                        (rule.operator == "contains" and val in ans)
                    )
                    if match:
                        matching_qualification_ids.add(q.record_id)

    qualification_ids = {
        r.salesforce_account_id for r in qualification_records
        if (not question_filters) or (r.id in matching_qualification_ids)
    }

    # Lógica combinada
    if not filters:
        relevant_account_ids = {
            acc.id for acc in db.query(SalesforceAccount)
            .filter(
                SalesforceAccount.record_type == "SubAccount",
                SalesforceAccount.c_type == "Clinical"
            )
        }
        logger.info("Sin filtros: %s subaccounts clínicas", len(relevant_account_ids))
    else:
        relevant_account_ids = (qualification_ids & profiling_ids) if logic == "AND" else (qualification_ids | profiling_ids)
        logger.info("Con filtros (%s): %s subaccounts matching", logic, len(relevant_account_ids))

    # Filas
    rows = []
    # Opcional: columnas fijas siempre visibles
    FORCE_FIXED_COLS = False
    for acc_id in relevant_account_ids:
        account = db.query(SalesforceAccount).filter_by(id=acc_id).first()
        if not account:
            continue

        row = {"account_id": acc_id}

        def want(col: str) -> bool:
            return True if FORCE_FIXED_COLS and col in {"account_name", "country", "location"} else (col in columns)

        if want("account_name"):
            row["account_name"] = account.name
        if want("country"):
            row["country"] = account.shipping_country
        if want("location"):
            row["location"] = (
                f"{account.shipping_city}, {account.shipping_country}"
                if account.shipping_city and account.shipping_country
                else account.shipping_country or (account.shipping_city or "")
            )

        if acc_id in qualification_by_account:
            if want("qualification_opportunity_name"):
                row["qualification_opportunity_name"] = qualification_by_account[acc_id][0].site_name
            if want("qualification_stage"):
                row["qualification_stage"] = "Uploaded"
            if any(c.startswith("question::") for c in columns):
                record_ids = [r.id for r in qualification_by_account[acc_id]]
                questions = db.query(StructuredQuestion).filter(StructuredQuestion.record_id.in_(record_ids)).all()
                for q in questions:
                    key = f"question::{q.question_text}"
                    if key in columns:
                        row[key] = q.answer

        if acc_id in profiling_by_account:
            opp = profiling_by_account[acc_id]
            if want("profiling_opportunity_name"):
                row["profiling_opportunity_name"] = opp.get("Name")
            if want("profiling_stage"):
                row["profiling_stage"] = opp.get("StageName")
            if want("close_date"):
                row["close_date"] = opp.get("CloseDate")
            for pf in requested_profiling_fields:
                row[pf] = opp.get(pf)

        rows.append(row)

    # Mapa
    map_locations = []
    for acc_id in relevant_account_ids:
        geo = db.query(AccountGeolocation).filter_by(account_id=acc_id).first()
        account = db.query(SalesforceAccount).filter_by(id=acc_id).first()
        if not geo or not account:
            continue
        has_qualification = acc_id in qualification_by_account
        has_profiling = acc_id in profiling_by_account
        marker_type = "both" if has_qualification and has_profiling else ("qualification_only" if has_qualification else "profiling_only")
        map_locations.append({
            "account_id": acc_id,
            "lat": geo.latitude,
            "lng": geo.longitude,
            "label": account.name or "Unknown",
            "type": marker_type,
        })

    logger.info("Filas=%s | Marcadores=%s", len(rows), len(map_locations))
    return {
        "rows": rows,
        "map_locations": map_locations,
        "meta": {
            "limit": limit,
            "offset": offset,
            "profiling_count": total_profiling,  # aproximación
            "returned_rows": len(rows),
            "returned_markers": len(map_locations),
        }
    }