# app/schemas.py

from pydantic import BaseModel
from typing import List, Optional, Any, Literal
from datetime import datetime

# 🔐 Salesforce token
class SalesforceTokenSchema(BaseModel):
    id: int
    access_token: str
    refresh_token: Optional[str]
    instance_url: str
    issued_at: datetime
    expires_in: Optional[int]

    class Config:
        from_attributes = True

# 📊 Explorer request para /api/explorer/combined
class ExplorerFilter(BaseModel):
    field: str
    operator: str
    value: Any

class QueryRequest(BaseModel):
    logic: Literal["AND", "OR"] = "AND"
    rules: List[ExplorerFilter] = []
    columns: List[str] = []

class ExplorerQuery(BaseModel):
    logic: Literal["AND", "OR"] = "AND"
    rules: List[ExplorerFilter] = []
    columns: List[str] = []

# 📄 Structured Qualification
class StructuredQuestionOut(BaseModel):
    id: int
    question_text: str
    answer: Optional[str]

    class Config:
        from_attributes = True

class StructuredSubsectionOut(BaseModel):
    id: int
    subsection_title: str
    questions: List[StructuredQuestionOut]

    class Config:
        from_attributes = True

class StructuredSectionOut(BaseModel):
    id: int
    section_code: str
    section_title: str
    subsections: List[StructuredSubsectionOut]
    questions: List[StructuredQuestionOut]

    class Config:
        from_attributes = True

class StructuredPartOut(BaseModel):
    id: int
    part_title: str
    sections: List[StructuredSectionOut]
    questions: List[StructuredQuestionOut]

    class Config:
        from_attributes = True

class StructuredRecordSummary(BaseModel):
    id: int
    site_name: str
    version: str
    filename: str
    structured_parts: List[StructuredPartOut]

    class Config:
        from_attributes = True