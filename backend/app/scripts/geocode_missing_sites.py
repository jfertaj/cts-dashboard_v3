# app/scripts/geocode_missing_sites.py
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import Site, GeonamesCity
import pycountry

# ----------------------
# Manual country name fixes
# ----------------------
manual_fixes = {
    "UK": "GB",
    "U.K.": "GB",
    "SPAIN": "ES",
    "USA": "US",
    "U.S.A.": "US",
    "RUSSIA": "RU",
    "IRAN": "IR",
    "SOUTH KOREA": "KR",
    "NORTH KOREA": "KP",
    "VENEZUELA": "VE",
    "BOLIVIA": "BO",
    "SYRIA": "SY",
    "LAOS": "LA",
    "MOLDOVA": "MD",
    "VIETNAM": "VN",
    "CZECH REPUBLIC": "CZ",
    "CZECHIA": "CZ",
    "IVORY COAST": "CI",
    "TAIWAN": "TW",
    "PALESTINE": "PS",
    "TANZANIA": "TZ",
    "BRUNEI": "BN",
    "REPUBLIC OF THE CONGO": "CG",
    "DEMOCRATIC REPUBLIC OF THE CONGO": "CD",
    "CAPE VERDE": "CV",
    "ESWATINI": "SZ",
    "SWAZILAND": "SZ",
    "MICRONESIA": "FM",
    "NORTH MACEDONIA": "MK",
    "SAO TOME AND PRINCIPE": "ST",
    "EAST TIMOR": "TL",
    "MYANMAR": "MM",
}

# ----------------------
# Map country name → ISO2 code
# ----------------------
country_name_to_code = {}
for country in pycountry.countries:
    country_name_to_code[country.name.upper()] = country.alpha_2
    if hasattr(country, 'official_name'):
        country_name_to_code[country.official_name.upper()] = country.alpha_2

country_name_to_code.update({k.upper(): v for k, v in manual_fixes.items()})

# ----------------------
# Helpers
# ----------------------
_CITY_SUFFIX_CC_RE = re.compile(r"\b[A-Z]{2}$")

def _clean_city(raw: str) -> str:
    """
    - Coge el primer tramo si viene con coma: 'Seville, US' -> 'Seville'
    - Quita un posible sufijo tipo código país de 2 letras: 'Seville US' -> 'Seville'
    """
    if not raw:
        return raw
    city = raw.split(",", 1)[0].strip()
    # elimina sufijo " XX" si son 2 mayúsculas (p.ej. 'US', 'GB', 'ES')
    parts = city.split()
    if parts and _CITY_SUFFIX_CC_RE.fullmatch(parts[-1] or ""):
        parts = parts[:-1]
        city = " ".join(parts).strip()
    return city


# ----------------------
# Geocoding logic
# ----------------------
def update_missing_coordinates():
    db: Session = SessionLocal()
    try:
        sites = db.query(Site).filter(
            Site.latitude.is_(None),
            Site.longitude.is_(None),
            Site.city.isnot(None),
            Site.country.isnot(None)
        ).all()

        print(f"🌍 Found {len(sites)} sites to geocode...")

        for site in sites:
            city_raw = (site.city or "").strip()
            country_raw = site.country.strip().upper()

            # Extract just the first part of city if comma-separated
            city = _clean_city(city_raw)

            # Normalize country
            iso_code = country_name_to_code.get(country_raw)
            if not iso_code:
                print(f"❌ Could not map country '{country_raw}' to ISO2 code — skipping site: {site.name}")
                continue

            geo = db.query(GeonamesCity).filter(
                GeonamesCity.country_code == iso_code
            ).filter(
                (GeonamesCity.name.ilike(city)) |
                (GeonamesCity.asciiname.ilike(city)) |
                (GeonamesCity.alternatenames.ilike(f"%{city}%"))
            ).order_by(GeonamesCity.population.desc()).first()

            if geo:
                site.latitude = geo.latitude
                site.longitude = geo.longitude
                # coherente con /api/geo endpoints
                if hasattr(site, "geocode_source"):
                    site.geocode_source = "geonames"
                if hasattr(site, "geocode_precision"):
                    site.geocode_precision = "city"
                db.add(site)
                print(f"✅ {site.name} → {geo.latitude}, {geo.longitude} [Matched: {geo.name}]")
            else:
                print(f"⚠️ No match for city '{city}' in {country_raw} — site: {site.name}")

        db.commit()
        print("✅ Geocoding complete.")

    except Exception as e:
        db.rollback()
        print("❌ Error during geocoding:", str(e))
    finally:
        db.close()