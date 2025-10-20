from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.database import get_db
from app.models import Site
from app.services.geocoding import geocode_with_fallback

router = APIRouter(prefix="/api/geo", tags=["geo"])

@router.post("/regeocode-missing")
def regeocode_missing(force: bool = Query(False), limit: int = Query(500), db: Session = Depends(get_db)):
    q = db.query(Site)
    if not force:
        q = q.filter(or_(Site.latitude.is_(None), Site.longitude.is_(None)))
    sites = q.order_by(Site.id).limit(limit).all()
    updated, skipped = 0, 0
    for s in sites:
        lat,lng,prec,src = geocode_with_fallback(s.street, s.city, s.country)
        if lat is None or lng is None: skipped += 1; continue
        s.latitude,s.longitude = lat,lng
        s.geocode_precision,s.geocode_source = prec,src
        updated += 1
    db.commit()
    return {"status":"ok","count":len(sites),"updated":updated,"skipped":skipped,"forced":force}

@router.post("/regeocode-site/{site_id}")
def regeocode_site(site_id: int, db: Session = Depends(get_db)):
    s = db.get(Site, site_id)
    if not s: return {"status":"not_found","site_id":site_id}
    lat,lng,prec,src = geocode_with_fallback(s.street, s.city, s.country)
    if lat is None or lng is None: return {"status":"no_coords","site_id":site_id}
    s.latitude,s.longitude = lat,lng
    s.geocode_precision,s.geocode_source = prec,src
    db.commit()
    return {"status":"ok","site_id":site_id,"lat":lat,"lng":lng,"precision":prec,"source":src}