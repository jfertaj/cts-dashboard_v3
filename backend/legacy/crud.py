from sqlalchemy.orm import Session
from app import models
from app.models import QuestionnaireType
import re
from pathlib import Path
from typing import List, Dict, Any, Union
from app.scripts.geocode_missing_sites import update_missing_coordinates

def ingest_data(parsed_data: Dict[str, Any], questionnaire_type: str, file_name: str, db: Session, file_hash: str):
    responses = parsed_data["responses"]
    location = parsed_data.get("location", {})
    city = location.get("city")
    country = location.get("country")
    postcode = location.get("postcode")
    version = parsed_data.get("version", "Unknown")

    # Normalize known aliases
    if country:
        country = country.strip().upper()
        alias_map = {
            "UK": "United Kingdom",
            "U.K.": "United Kingdom",
            "USA": "United States",
            "U.S.A.": "United States",
        }
        country = alias_map.get(country, country)

    if city:
        city = city.strip().split(",")[0].strip()

    # Extract site name from responses or filename
    site_name = extract_site_name(responses, fallback_filename=file_name)

    # Get or create the site
    site = db.query(models.Site).filter(models.Site.name.ilike(site_name)).first()
    if not site:
        site = models.Site(name=site_name, city=city, country=country, postcode=postcode)
        db.add(site)
        db.commit()
        db.refresh(site)
    else:
        # Update city/country/postcode if missing and new info exists
        updated = False
        if city and not site.city:
            site.city = city
            updated = True
        if country and not site.country:
            site.country = country
            updated = True
        if postcode and not site.postcode:
            site.postcode = postcode
            updated = True
        if updated:
            db.commit()
            db.refresh(site)

    # Create questionnaire
    questionnaire = models.Questionnaire(
        site_id=site.id,
        file_name=file_name,
        version=version,
        type=QuestionnaireType(questionnaire_type),
        file_hash=file_hash
    )
    db.add(questionnaire)
    db.commit()
    db.refresh(questionnaire)

    # Initialize section tracking
    section_map = {}
    current_section = "Uncategorized"
    section_counter = 0

    # Create default section
    default_section = models.Section(
        name="Uncategorized",
        questionnaire_id=questionnaire.id,
        section_order=section_counter
    )
    db.add(default_section)
    db.commit()
    db.refresh(default_section)
    section_map[current_section] = default_section

    for row in responses:
        # Handle section changes
        if row.get("section"):
            new_section = row["section"].strip()
            if new_section != current_section:
                current_section = new_section
                section_counter += 1
                if current_section not in section_map:
                    section = models.Section(
                        name=current_section,
                        questionnaire_id=questionnaire.id,
                        section_order=section_counter
                    )
                    db.add(section)
                    db.commit()
                    db.refresh(section)
                    section_map[current_section] = section

        # Fallback to default section if not mapped
        if current_section not in section_map:
            current_section = "Uncategorized"

        # Create question
        question = models.Question(
            section_id=section_map[current_section].id,
            question_text=row.get("question", "No question text"),
            question_number=row.get("question_number", "N/A"),
            subsection=row.get("subsection") if row.get("subsection") else None,
            question_order=int(row.get("question_number", 0)) if str(row.get("question_number", "0")).isdigit() else 0
        )
        db.add(question)
        db.commit()
        db.refresh(question)

        # Create response
        db.add(models.Response(
            question_id=question.id,
            response_text=row.get("answer", "").strip() or "No response provided"
        ))

    db.commit()

    # Automatically geocode new sites
    update_missing_coordinates()


def extract_site_name(parsed_data: List[Dict[str, Any]], fallback_filename: str = None) -> str:
    target_questions = [
        "Name of hospital or center conducting clinical trials:",
        "Name of affiliated INNODIA member:",
        "Center and Membership Information"
    ]
    for row in parsed_data:
        if any(tq in row.get("question", "") for tq in target_questions):
            answer = row.get("answer", "").strip()
            if answer:
                clean_name = re.split(r'[-–(]', answer)[0].strip()
                return clean_name

    # Fallback to file name parsing
    if fallback_filename:
        stem = Path(fallback_filename).stem
        name_guess = re.sub(
            r'^(CTS_)?(Profiling_Questionnaire_)?(v\d+\.\d+)?_?\d{1,2}_\w+_\d{4}_?[-–]?\s*',
            '',
            stem,
            flags=re.IGNORECASE
        ).replace('_', ' ').strip()
        return name_guess or "Unknown Site"

    return "Unknown Site"