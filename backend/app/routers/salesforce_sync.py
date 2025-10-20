# app/routers/salesforce_sync.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.database import get_db
from app.models import Site
from app.services.salesforce_client import get_salesforce_session

router = APIRouter(prefix="/api/salesforce/sync", tags=["salesforce-sync"])

@router.post("/profiling")
def sync_profiling(db: Session = Depends(get_db)):
    """
    Importa Accounts que tengan Opportunity Type='Profiling' abierta,
    y marca el Site correspondiente con has_profiling=True.
    Crea el Site si no existe aún.
    """
    sf = get_salesforce_session()
    if not sf:
        raise HTTPException(401, "Salesforce no autenticado")

    # Oportunidades de tipo Profiling abiertas (ajusta a tu lógica)
    soql = """
      SELECT Id, Name, StageName, IsClosed, Type,
             Account.Id, Account.Name,
             Account.ShippingCity, Account.ShippingCountry,
             Account.ShippingLatitude, Account.ShippingLongitude
      FROM Opportunity
      WHERE Type = 'Profiling' AND IsClosed = false
    """
    recs = sf.query_all(soql)["records"]

    imported = 0
    for r in recs:
        acc = r.get("Account") or {}
        account_id = acc.get("Id")
        if not account_id:
            continue

        # upsert Site por salesforce_account_id
        site = db.query(Site).filter(Site.salesforce_account_id == account_id).one_or_none()
        if not site:
            site = Site(
                name=acc.get("Name") or "(sin nombre)",
                salesforce_account_id=account_id,
                city=acc.get("ShippingCity"),
                country=acc.get("ShippingCountry"),
            )
            # si tu modelo tiene lat/lng, rellénalos (no falles si no existen)
            if hasattr(site, "lat") and hasattr(site, "lng"):
                site.lat = acc.get("ShippingLatitude")
                site.lng = acc.get("ShippingLongitude")
            db.add(site)
            imported += 1
        else:
            # refresca datos básicos
            site.name = acc.get("Name") or site.name
            site.city = acc.get("ShippingCity") or site.city
            site.country = acc.get("ShippingCountry") or site.country
            if hasattr(site, "lat") and hasattr(site, "lng"):
                if acc.get("ShippingLatitude") and acc.get("ShippingLongitude"):
                    site.lat = acc.get("ShippingLatitude")
                    site.lng = acc.get("ShippingLongitude")

        # marca que este Site tiene oportunidad de profiling
        if hasattr(site, "has_profiling"):
            site.has_profiling = True

    db.commit()
    return {"status": "ok", "imported": imported, "total": len(recs)}