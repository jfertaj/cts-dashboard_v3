from sqlalchemy.orm import Session
from models import GeonamesCity

def find_city_coordinates(db: Session, city: str, country: str):
    result = (
        db.query(GeonamesCity)
        .filter(GeonamesCity.name.ilike(city))
        .filter(GeonamesCity.country_code.ilike(country))
        .order_by(GeonamesCity.population.desc())  # Prefer large cities
        .first()
    )
    return result.latitude, result.longitude if result else (None, None)