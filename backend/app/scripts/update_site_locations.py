from app.database import SessionLocal
from app.models import Site, Questionnaire, Section, Question, Response
from app.parser.profiling import extract_location_fields

def update_sites_with_location():
    db = SessionLocal()

    # Fetch all Sites that have profiling questionnaires
    sites = db.query(Site).join(Questionnaire).filter(Questionnaire.type == "profiling").all()

    for site in sites:
        responses = (
            db.query(Response.response_text, Question.question_text)
            .join(Question)
            .join(Section)
            .join(Questionnaire)
            .filter(Questionnaire.site_id == site.id, Questionnaire.type == "profiling")
            .all()
        )

        fake_data = [
            {
                "question": r.question_text,
                "answer": r.response_text,
            }
            for r in responses
        ]

        city, country, _ = extract_location_fields(fake_data)

        if city or country:
            site.city = city
            site.country = country
            print(f"✅ Updated site '{site.name}' with city: {city}, country: {country}")

    db.commit()
    db.close()

if __name__ == "__main__":
    update_sites_with_location()