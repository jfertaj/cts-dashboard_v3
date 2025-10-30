#backend/app/routers/explorer_bridge.py

from fastapi import APIRouter, Body

router = APIRouter(prefix="/api/explorer-bridge", tags=["explorer-bridge"])

@router.post("/highlight")
def highlight(accounts: dict = Body(...)):
    """
    Recibe: {"account_ids": ["001...", ...]}
    Responde siempre ok (el Explorer escucha un CustomEvent en el frontend).
    """
    return {"ok": True, "selected": accounts.get("account_ids", [])}


@router.post("/add-columns")
def add_columns(payload: dict = Body(...)):
    """
    Recibe: {"columns": ["Field__c", ...]}
    """
    return {"ok": True, "columns": payload.get("columns", [])}