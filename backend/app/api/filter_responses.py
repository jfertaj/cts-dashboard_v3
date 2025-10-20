# app/api/filter_responses.py
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func, cast, Float
from app.database import get_db
from app import models

router = APIRouter()

def build_filter_clause(rule, model=models):
    if "logic" in rule and "rules" in rule:
        logic_fn = and_ if rule["logic"] == "AND" else or_
        return logic_fn(*[build_filter_clause(r, model) for r in rule["rules"]])

    filters = []

    if rule.get("section"):
        filters.append(model.Section.name == rule["section"])
    if rule.get("subsection"):
        filters.append(model.Question.subsection == rule["subsection"])
    if rule.get("question"):
        filters.append(model.Question.question_text == rule["question"])

    if "operator" in rule and "value" in rule:
        column = model.Response.response_text
        op = rule["operator"]
        val = rule["value"]

        try:
            if op == "equals":
                filters.append(column == val)
            elif op == "contains":
                filters.append(column.ilike(f"%{val}%"))
            elif op == "not_contains":
                filters.append(~column.ilike(f"%{val}%"))
            elif op == "gt":
                filters.append(cast(column, Float) > float(val))
            elif op == "lt":
                filters.append(cast(column, Float) < float(val))
            elif op == "=":
                filters.append(cast(column, Float) == float(val))
            else:
                raise ValueError(f"Unsupported operator: {op}")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid numeric value for operator '{op}': {val}")

    return and_(*filters)

@router.post("/api/filter-responses/")
def filter_responses(body: dict, db: Session = Depends(get_db)):
    try:
        clause = build_filter_clause(body)

        query = (
            db.query(
                models.Site.name.label("site"),
                models.Questionnaire.version.label("version"),
                models.Questionnaire.type.label("type"),
                models.Section.name.label("section"),
                models.Question.question_text.label("question"),
                models.Response.response_text.label("answer"),
                models.Question.subsection.label("subsection"),
                models.Site.latitude,
                models.Site.longitude,
                models.Site.city,
                models.Site.country,
                models.Site.postcode
            )
            .join(models.Questionnaire, models.Site.id == models.Questionnaire.site_id)
            .join(models.Section, models.Questionnaire.id == models.Section.questionnaire_id)
            .join(models.Question, models.Section.id == models.Question.section_id)
            .join(models.Response, models.Question.id == models.Response.question_id)
            .filter(clause)
        )

        # Optional: city and country filters
        if body.get("city"):
            query = query.filter(models.Site.city.ilike(f"%{body['city']}%"))
        if body.get("country"):
            query = query.filter(models.Site.country.ilike(f"%{body['country']}%"))

        results = query.all()
        return [dict(row._mapping) for row in results]

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid filter request: {str(e)}")