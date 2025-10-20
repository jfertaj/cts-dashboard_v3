import logging, random
from typing import Optional, Tuple, Dict, Any
import httpx
from sqlalchemy.orm import Session
from app.settings import settings
from app.database import SessionLocal
from app.models import GeocodedAddress

logger = logging.getLogger(__name__)
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

def _jitter(base: float) -> float:
    return base * (1 + random.uniform(-0.2, 0.2))

def _cache_get(db: Session, address: str) -> Optional[Tuple[float, float]]:
    row = db.query(GeocodedAddress).filter(GeocodedAddress.address == address).first()
    if row:
        return (row.latitude, row.longitude)
    return None

def _cache_put(db: Session, address: str, lat: float, lng: float, meta: Dict[str, Any]):
    db.merge(GeocodedAddress(address=address, latitude=lat, longitude=lng, provider="google", meta=meta))
    db.commit()

async def _sleep(seconds: float):
    import anyio
    await anyio.sleep(max(0.05, seconds))

_ISO2 = {
    # 🇪🇸 Southwestern Europe
    "spain": "ES", "españa": "ES", "espana": "ES", "es": "ES",
    "portugal": "PT", "pt": "PT",
    "andorra": "AD", "ad": "AD",
    "gibraltar": "GI", "gi": "GI",

    # 🇫🇷 Western Europe
    "france": "FR", "fr": "FR", "francia": "FR",
    "monaco": "MC", "mc": "MC",

    # 🇩🇪 Central Europe
    "germany": "DE", "de": "DE", "deutschland": "DE",
    "austria": "AT", "at": "AT", "österreich": "AT", "osterreich": "AT",
    "switzerland": "CH", "ch": "CH", "suisse": "CH", "schweiz": "CH", "svizzera": "CH",
    "liechtenstein": "LI", "li": "LI",

    # 🇮🇹 Southern Europe
    "italy": "IT", "it": "IT", "italia": "IT", "san marino": "SM", "sm": "SM", "vatican": "VA", "holy see": "VA", "va": "VA",

    # 🇬🇧 Northern Atlantic
    "united kingdom": "GB", "uk": "GB", "gb": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "ireland": "IE", "ie": "IE", "eire": "IE",

    # 🇧🇪 Benelux
    "belgium": "BE", "belgië": "BE", "belgie": "BE", "belgique": "BE", "be": "BE",
    "netherlands": "NL", "nederland": "NL", "holland": "NL", "nl": "NL",
    "luxembourg": "LU", "luxemburg": "LU", "lu": "LU",

    # 🇩🇰🇳🇴🇸🇪🇫🇮🇮🇸 Nordics
    "denmark": "DK", "danmark": "DK", "dk": "DK",
    "norway": "NO", "norge": "NO", "no": "NO",
    "sweden": "SE", "sverige": "SE", "se": "SE",
    "finland": "FI", "suomi": "FI", "fi": "FI",
    "iceland": "IS", "island": "IS", "is": "IS",

    # 🇵🇱🇨🇿🇸🇰🇭🇺 Central-Eastern
    "poland": "PL", "polska": "PL", "pl": "PL",
    "czech republic": "CZ", "czechia": "CZ", "cz": "CZ", "cesko": "CZ",
    "slovakia": "SK", "slovensko": "SK", "sk": "SK",
    "hungary": "HU", "magyarország": "HU", "hu": "HU",

    # 🇷🇴🇧🇬🇬🇷 Balkan / SE Europe
    "romania": "RO", "ro": "RO", "românia": "RO",
    "bulgaria": "BG", "bg": "BG", "bălgarija": "BG",
    "greece": "GR", "gr": "GR", "ellada": "GR", "ελλάδα": "GR",
    "croatia": "HR", "hrvatska": "HR", "hr": "HR",
    "serbia": "RS", "rs": "RS", "srbija": "RS",
    "montenegro": "ME", "me": "ME", "crna gora": "ME",
    "bosnia": "BA", "bosnia and herzegovina": "BA", "ba": "BA",
    "north macedonia": "MK", "macedonia": "MK", "mk": "MK", "makedonija": "MK",
    "albania": "AL", "al": "AL",
    "kosovo": "XK", "xk": "XK",

    # 🇧🇾🇺🇦🇲🇩 Eastern Europe
    "belarus": "BY", "by": "BY", "belarussia": "BY", "belorussia": "BY",
    "ukraine": "UA", "ua": "UA", "ukraina": "UA",
    "moldova": "MD", "md": "MD", "moldova republic": "MD",

    # 🇪🇪🇱🇻🇱🇹 Baltics
    "estonia": "EE", "eesti": "EE", "ee": "EE",
    "latvia": "LV", "lv": "LV", "latvija": "LV",
    "lithuania": "LT", "lt": "LT", "lietuva": "LT",

    # 🇨🇾🇲🇹 Islands & Med
    "cyprus": "CY", "kypros": "CY", "cy": "CY",
    "malta": "MT", "mt": "MT",

    # Non-EU border frequent
    "turkey": "TR", "türkiye": "TR", "tr": "TR",
    "switzerland": "CH", "ch": "CH",
    "russia": "RU", "ru": "RU", "россия": "RU", "rossiya": "RU",
    "iceland": "IS", "is": "IS",
    "norway": "NO", "no": "NO",

    # Common mistakes
    "eu": "EU", "europe": "EU",
}

def _country_to_iso2(country: Optional[str]) -> Optional[str]:
    if not country: return None
    return _ISO2.get(str(country).strip().lower())

async def geocode_google(address: str, *, country_hint: Optional[str] = None) -> Optional[Tuple[float, float]]:
    if not address or not settings.GOOGLE_MAPS_API_KEY:
        return None

    db = SessionLocal()
    try:
        cached = _cache_get(db, address)
        if cached:
            return cached
    finally:
        db.close()

    params = {"address": address, "key": settings.GOOGLE_MAPS_API_KEY}
    if settings.GOOGLE_REGION_BIAS:
        params["region"] = settings.GOOGLE_REGION_BIAS
    iso = _country_to_iso2(country_hint)
    if iso:
        params["components"] = f"country:{iso}"

    backoff = 0.5
    for attempt in range(settings.GEO_MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.get(GEOCODE_URL, params=params)
            if res.status_code != 200:
                logger.warning("Geocode HTTP %s for '%s'", res.status_code, address)
                await _sleep(backoff); backoff = min(backoff * 2, 16)
                continue

            data = res.json()
            status = data.get("status")
            if status == "OK":
                result = data["results"][0]
                loc = result["geometry"]["location"]
                lat, lng = float(loc["lat"]), float(loc["lng"])
                db = SessionLocal()
                try:
                    _cache_put(db, address, lat, lng, data)
                finally:
                    db.close()
                return (lat, lng)

            if status in {"ZERO_RESULTS"}:
                return None

            if status in {"OVER_QUERY_LIMIT", "RESOURCE_EXHAUSTED"}:
                logger.warning("Geocode rate/quota limited (attempt %d) for '%s'", attempt + 1, address)
                await _sleep(_jitter(backoff)); backoff = min(backoff * 2, 16); continue

            if status in {"REQUEST_DENIED", "INVALID_REQUEST"}:
                logger.error("Geocode denied/invalid for '%s': %s", address, data.get("error_message"))
                return None

            logger.warning("Geocode unknown status '%s' for '%s'", status, address)
            await _sleep(_jitter(backoff)); backoff = min(backoff * 2, 16)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            logger.warning("Geocode network/timeout: %s", e)
            await _sleep(_jitter(backoff)); backoff = min(backoff * 2, 16)
        except Exception as e:
            logger.exception("Geocode unexpected error: %s", e)
            await _sleep(_jitter(backoff)); backoff = min(backoff * 2, 16)
    return None

async def geocode_with_fallback(street: Optional[str], city: Optional[str], postal: Optional[str], country: Optional[str]):
    parts = [p for p in [street, city, postal, country] if p]
    full_address = ", ".join(parts)
    if full_address:
        loc = await geocode_google(full_address, country_hint=country)
        if loc:
            return loc, "full", full_address

    city_country = ", ".join([p for p in [city, country] if p])
    if city_country:
        loc = await geocode_google(city_country, country_hint=country)
        if loc:
            return loc, "city-country", city_country

    if country:
        loc = await geocode_google(country, country_hint=country)
        if loc:
            return loc, "country", country

    return None, "none", full_address or city_country or (country or "")