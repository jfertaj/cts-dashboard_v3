# backend/app/api/health.py
from fastapi import APIRouter, Response, status
from sqlalchemy import text
from app.database import engine
import os

router = APIRouter()

def check_db() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

@router.get("/healthz")
def healthz(response: Response):
    ok_db = check_db()
    gmaps_ok = bool(os.getenv("GOOGLE_MAPS_API_KEY", ""))
    if not ok_db:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "ok": ok_db and gmaps_ok,
        "db": "up" if ok_db else "down",
        "google_maps_key": "set" if gmaps_ok else "missing",
    }