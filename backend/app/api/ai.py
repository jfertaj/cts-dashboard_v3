# app/api/ai.py
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Any, Dict, List, Literal, Optional
from sqlalchemy.orm import Session
from app.db import SessionLocal
from app.ai.planner import plan_from_llm
from app.ai.executor import execute_explorer_query
import os

router = APIRouter(prefix="/api/ai", tags=["ai"])

class Message(BaseModel):
  role: Literal["system","user","assistant"]
  content: str

class ChatRequest(BaseModel):
  messages: List[Message]

def get_db():
  db = SessionLocal()
  try:
    yield db
  finally:
    db.close()

@router.post("/chat")
def chat(body: ChatRequest, db: Session = Depends(get_db)) -> Dict[str,Any]:
  if not body.messages:
    raise HTTPException(status_code=400, detail="messages required")

  plan = plan_from_llm([m.model_dump() for m in body.messages])

  # 1) Ejecutable (explorer_query)
  if plan.get("action") == "explorer_query":
    result = execute_explorer_query(db, plan)
    # Texto auxiliar “friendly” (opcional)
    answer = "He preparado la tabla solicitada." if result.get("table",{}).get("rows") else "No encontré resultados."
    return {"ok": True, "answer": answer, **result}

  # 2) Chit-chat: si hay API key, pedimos respuesta libre; si no, responde corto
  if os.getenv("OPENAI_API_KEY"):
    try:
      from openai import OpenAI
      client = OpenAI()
      resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"system","content":"Eres conciso y útil."}]+[m.model_dump() for m in body.messages],
        temperature=0.3,
      )
      text = resp.choices[0].message.content
      return {"ok": True, "answer": text}
    except Exception as e:
      return {"ok": True, "answer": "No pude completar la respuesta generativa, pero el servicio está operativo."}

  # Sin API key: respuesta simple
  return {"ok": True, "answer": "Puedo chatear y además generar tablas/gráficos si me pides análisis de centros."}