import os, requests
from typing import Optional, Tuple

API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

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
    if not country:
        return None
    return _ISO2.get(str(country).strip().lower())

def _geocode(q: str, *, country_hint: Optional[str] = None) -> Optional[dict]:
    try:
        params = {"address": q, "key": API_KEY}
        iso = _country_to_iso2(country_hint)
        if iso:
            # fuerza el país: evita que "Seville" caiga en EEUU
            params["components"] = f"country:{iso}"
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params=params,
            timeout=10,
        )
        r.raise_for_status(); data = r.json()
        if data.get("status") == "OK" and data["results"]:
            return data["results"][0]
    except Exception as e:
        print("❌ Geocode error:", e)
    return None

def geocode_with_fallback(street: Optional[str], city: Optional[str], country: Optional[str]):
    parts = [p for p in [street, city, country] if p]
    if parts:
        res = _geocode(", ".join(parts), country_hint=country)
        if res:
            loc = res["geometry"]["location"]
            return loc["lat"], loc["lng"], res["geometry"].get("location_type","UNKNOWN"), "full_address"
    parts2 = [p for p in [city, country] if p]
    if parts2:
        res = _geocode(", ".join(parts2), country_hint=country)
        if res:
            loc = res["geometry"]["location"]
            return loc["lat"], loc["lng"], res["geometry"].get("location_type","UNKNOWN"), "city_country"
    return None, None, "NONE", "none"