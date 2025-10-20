# app/startup_geo.py
import logging
from sqlalchemy.orm import Session
from app.utils_db import db_session
from app.models import AccountGeolocation, SalesforceAccount
from app.geo.google_geocoding import geocode_with_fallback

logger = logging.getLogger(__name__)

async def initial_geocoding():
    """
    Geocodifica Accounts que aún no tengan coordenadas.
    """
    with db_session() as db:
        accounts = db.query(SalesforceAccount).all()
        created = 0
        for acc in accounts:
            exists = db.query(AccountGeolocation).filter_by(account_id=acc.id).first()
            if exists:
                continue
            street = getattr(acc, "shipping_street", None)
            city = getattr(acc, "shipping_city", None)
            postal = getattr(acc, "shipping_postal_code", None)
            country = getattr(acc, "shipping_country", None)

            loc, mode, used_address = await geocode_with_fallback(street, city, postal, country)
            if loc:
                lat, lng = loc
                db.add(AccountGeolocation(
                    account_id=acc.id,
                    address=used_address,
                    latitude=lat,
                    longitude=lng
                ))
                created += 1
            else:
                logger.warning("No geocodificado en startup: %s (%s, %s)", acc.id, acc.name, city)
        logger.info("Startup geocoding: %s nuevas geolocalizaciones", created)