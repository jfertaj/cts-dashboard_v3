"""Pydantic request/response schemas for Moby chat endpoints.

Pure move from `app.routers.ai_chat` (Phase 1 refactor). `ai_chat.py`
re-exports these via shim imports so existing references keep working.
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    role: Role
    content: str
    tool_name: Optional[str] = None  # solo para compat interna; OpenAI ignora esto


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    stream: bool = False
    last_table: Optional[Dict[str, Any]] = None  # Tabla de la respuesta anterior para follow-ups eficientes
    last_filters: Optional[Dict[str, Any]] = None  # FilterGroup que produjo la tabla anterior
