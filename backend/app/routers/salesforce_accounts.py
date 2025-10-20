# app/routers/salesforce_accounts.py
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any

from app.database import get_db
from app.models import Site
from app.services.salesforce_client import get_salesforce_session

router = APIRouter(prefix="/api/salesforce", tags=["salesforce"])


@router.get("/accounts/search")
def search_accounts(
    request: Request,
    q: str = Query(..., description="Texto a buscar en Name o ShippingCity"),
    limit: int = Query(25, ge=1, le=200),
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Busca cuentas en Salesforce por nombre o ciudad de envío.
    Requiere sesión OAuth (cookie httpOnly). Si no hay sesión, devuelve 401.
    """
    # Obtén cliente SF desde cookie (o fallback a env si lo dejaste habilitado)
    sf = get_salesforce_session(request)

    # Escapa comillas simples para LIKE en SOQL (usemos '' -> comilla literal)
    q_escaped = (q or "").replace("'", "''")

    soql = f"""
        SELECT Id, Name, Type, ShippingCity, ShippingCountry, ShippingStreet, ShippingPostalCode
        FROM Account
        WHERE (Name LIKE '%{q_escaped}%'
               OR ShippingCity LIKE '%{q_escaped}%')
        ORDER BY Name
        LIMIT {int(limit)}
    """

    try:
        recs = sf.query_all(soql).get("records", [])
    except Exception as e:
        # Normaliza errores de autenticación/permiso
        raise HTTPException(status_code=400, detail=f"Salesforce query error: {e}")

    out = [
        {
            "id": r.get("Id"),
            "name": r.get("Name"),
            "type": r.get("Type"),
            "shipping_city": r.get("ShippingCity"),
            "shipping_country": r.get("ShippingCountry"),
            "shipping_street": r.get("ShippingStreet"),
            "shipping_postal_code": r.get("ShippingPostalCode"),
        }
        for r in recs
    ]
    return {"results": out}


@router.post("/accounts/link-site")
def link_site(
    site_id: int,
    account_id: str,
    db: Session = Depends(get_db),
):
    """
    Vincula un Site local con una Account de Salesforce (guarda el Id).
    """
    site = db.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site no encontrado")

    if not account_id:
        raise HTTPException(status_code=400, detail="account_id requerido")

    site.salesforce_account_id = account_id
    db.commit()
    return {"status": "ok", "site_id": site_id, "salesforce_account_id": account_id}


@router.post("/accounts/unlink-site")
def unlink_site(
    site_id: int,
    db: Session = Depends(get_db),
):
    """
    Desvincula un Site local de cualquier Account Salesforce.
    """
    site = db.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site no encontrado")

    site.salesforce_account_id = None
    db.commit()
    return {"status": "ok", "site_id": site_id}


@router.get("/sites/unlinked")
def list_unlinked_sites(
    country: Optional[str] = Query(None, description="Filtra por país exacto (opcional)"),
    city: Optional[str] = Query(None, description="Filtra por ciudad (icontains, opcional)"),
    limit: int = Query(1000, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    """
    Devuelve los Sites que NO tienen salesforce_account_id (no vinculados).
    Permite filtrar por country exacto y city (icontains).
    """
    q = db.query(Site).filter(Site.salesforce_account_id.is_(None))

    if country:
        q = q.filter(Site.country == country)

    if city:
        q = q.filter(Site.city.ilike(f"%{city}%"))

    rows = (
        q.order_by(Site.name.asc())
         .limit(limit)
         .all()
    )

    return {
        "rows": [
            {
                "id": s.id,
                "name": s.name,
                "city": s.city,
                "country": s.country,
            }
            for s in rows
        ]
    }