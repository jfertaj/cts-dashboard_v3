# app/api/responses.py

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_
from app.database import get_db
from app.models import Site, Questionnaire, Section, Question, Response

router = APIRouter()

def apply_operator(column, operator: str, value: str):
    if operator == "equals":
        return column == value
    elif operator == "contains":
        return column.ilike(f"%{value}%")
    elif operator == "not contains":
        return ~column.ilike(f"%{value}%")
    elif operator == ">":
        return func.nullif(column, 'No response provided').cast(float) > float(value)
    elif operator == "<":
        return func.nullif(column, 'No response provided').cast(float) < float(value)
    elif operator == "=":
        return func.nullif(column, 'No response provided').cast(float) == float(value)
    else:
        raise ValueError(f"Unsupported operator: {operator}")

def build_filter_condition(rule):
    filters = []
    if rule.get("section"):
        filters.append(Section.name == rule["section"])
    if rule.get("subsection"):
        filters.append(Question.subsection == rule["subsection"])
    if rule.get("question"):
        filters.append(Question.question_text == rule["question"])
    if rule.get("operator") and rule.get("value") is not None:
        filters.append(apply_operator(Response.response_text, rule["operator"], rule["value"]))
    return and_(*filters)

def combine_conditions(rules, logic):
    conditions = [build_filter_condition(rule) for rule in rules]
    return and_(*conditions) if logic == "AND" else or_(*conditions)


@router.get("/")
def get_all_responses(db: Session = Depends(get_db)):
    query = db.query(
        Site.name.label("site"),
        Questionnaire.version.label("version"),
        Questionnaire.type.label("form_type"),
        Section.name.label("section"),
        Question.question_text.label("question"),
        Response.response_text.label("answer"),
        Question.subsection.label("subsection"),
        Site.latitude,
        Site.longitude,
        Site.city,
        Site.country,
        Site.postcode
    ).join(Questionnaire, Site.id == Questionnaire.site_id
    ).join(Section, Questionnaire.id == Section.questionnaire_id
    ).join(Question, Section.id == Question.section_id
    ).join(Response, Question.id == Response.question_id)

    results = query.all()
    return [dict(row._mapping) for row in results]


@router.post("/filter-responses/")
async def filter_responses(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    rules = payload.get("rules", [])
    logic = payload.get("logic", "AND")
    city = payload.get("city")
    country = payload.get("country")

    query = db.query(
        Site.name.label("site"),
        Questionnaire.version.label("version"),
        Questionnaire.type.label("form_type"),
        Section.name.label("section"),
        Question.question_text.label("question"),
        Response.response_text.label("answer"),
        Question.subsection.label("subsection"),
        Site.latitude,
        Site.longitude,
        Site.city,
        Site.country,
        Site.postcode
    ).join(Questionnaire, Site.id == Questionnaire.site_id
    ).join(Section, Questionnaire.id == Section.questionnaire_id
    ).join(Question, Section.id == Question.section_id
    ).join(Response, Question.id == Response.question_id)

    if rules:
        try:
            condition = combine_conditions(rules, logic)
            query = query.filter(condition)
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))

    if city:
        query = query.filter(Site.city.ilike(f"%{city}%"))
    if country:
        query = query.filter(Site.country.ilike(f"%{country}%"))

    results = query.all()
    return [dict(row._mapping) for row in results]