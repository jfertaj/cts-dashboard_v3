"""Canonical country name normalisation for CTS Dashboard.

Single source of truth for all country lookups. All routers should import
from here instead of maintaining local copies.

Maps:
  _ALIAS_TO_ISO2  — any representation → ISO-2
  ISO2_TO_DISPLAY — ISO-2 → English display name
  _ISO2_TO_SF     — ISO-2 → Salesforce ShippingCountry string
  SORTED_ALIASES  — pre-sorted keys for greedy text matching

Functions:
  norm_to_iso2(country)   → Optional[str]  ISO-2 or normalised string
  iso2_to_display(iso2)   → str            English name (falls back to code)
  iso2_to_sf_name(iso2)   → str            SF ShippingCountry string
  resolve_countries(text) → List[str]      all ISO-2 codes found in text
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# ISO-2 → English display name
# ---------------------------------------------------------------------------
ISO2_TO_DISPLAY: Dict[str, str] = {
    "AD": "Andorra",
    "AL": "Albania",
    "AT": "Austria",
    "AU": "Australia",
    "BA": "Bosnia and Herzegovina",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "CA": "Canada",
    "CH": "Switzerland",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "EE": "Estonia",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "GR": "Greece",
    "HR": "Croatia",
    "HU": "Hungary",
    "IE": "Ireland",
    "IL": "Israel",
    "IT": "Italy",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "ME": "Montenegro",
    "MK": "North Macedonia",
    "MT": "Malta",
    "NL": "The Netherlands",
    "NO": "Norway",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "RS": "Serbia",
    "SE": "Sweden",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "TR": "Turkey",
    "US": "United States",
}

# Salesforce ShippingCountry strings — usually same as display name,
# except Salesforce uses "Czech Republic" not "Czechia".
_ISO2_TO_SF: Dict[str, str] = {**ISO2_TO_DISPLAY, "CZ": "Czech Republic"}

# ---------------------------------------------------------------------------
# Alias → ISO-2  (covers English names, native names, Spanish, adjective forms)
# ---------------------------------------------------------------------------
_ALIAS_TO_ISO2: Dict[str, str] = {
    # ---- ISO-2 codes themselves ----------------------------------------
    "ad": "AD", "al": "AL", "at": "AT", "au": "AU",
    "ba": "BA", "be": "BE", "bg": "BG", "ca": "CA",
    "ch": "CH", "cy": "CY", "cz": "CZ",
    "de": "DE", "dk": "DK", "ee": "EE", "es": "ES",
    "fi": "FI", "fr": "FR", "gb": "GB", "gr": "GR",
    "hr": "HR", "hu": "HU", "ie": "IE", "il": "IL",
    "it": "IT", "lt": "LT", "lu": "LU", "lv": "LV",
    "me": "ME", "mk": "MK", "mt": "MT", "nl": "NL",
    "no": "NO", "pl": "PL", "pt": "PT", "ro": "RO",
    "rs": "RS", "se": "SE", "si": "SI", "sk": "SK",
    "tr": "TR", "us": "US",
    # ---- English full names --------------------------------------------
    "andorra": "AD",
    "albania": "AL",
    "austria": "AT",
    "australia": "AU",
    "bosnia and herzegovina": "BA", "bosnia": "BA",
    "belgium": "BE",
    "bulgaria": "BG",
    "canada": "CA",
    "switzerland": "CH",
    "cyprus": "CY",
    "czech republic": "CZ", "czechia": "CZ",
    "germany": "DE",
    "denmark": "DK",
    "estonia": "EE",
    "spain": "ES",
    "finland": "FI",
    "france": "FR",
    "united kingdom": "GB", "great britain": "GB", "britain": "GB", "uk": "GB",
    "greece": "GR",
    "croatia": "HR",
    "hungary": "HU",
    "ireland": "IE", "eire": "IE",
    "israel": "IL",
    "italy": "IT",
    "lithuania": "LT",
    "luxembourg": "LU", "luxemburg": "LU",
    "latvia": "LV",
    "montenegro": "ME",
    "north macedonia": "MK", "macedonia": "MK",
    "malta": "MT",
    "netherlands": "NL", "the netherlands": "NL", "holland": "NL",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "romania": "RO",
    "serbia": "RS",
    "sweden": "SE",
    "slovenia": "SI",
    "slovakia": "SK",
    "turkey": "TR",
    "united states": "US", "usa": "US",
    # ---- Native / other-language names --------------------------------
    # German
    "österreich": "AT", "oesterreich": "AT",
    "belgique": "BE", "belgië": "BE", "belgie": "BE",
    "schweiz": "CH",
    "cesko": "CZ",
    "deutschland": "DE",
    "frankreich": "FR",  # French name for France in German context
    "ellada": "GR", "ελλάδα": "GR",
    "hrvatska": "HR",
    "magyarország": "HU",
    "italia": "IT",
    "lietuva": "LT",
    "latvija": "LV",
    "crna gora": "ME",
    "nederland": "NL",
    "norge": "NO",
    "polska": "PL",
    "srbija": "RS",
    "sverige": "SE",
    "slovenija": "SI",
    "slovensko": "SK",
    "türkiye": "TR",
    "suomi": "FI",
    "eesti": "EE",
    "kypros": "CY",
    "suisse": "CH",       # French
    "svizzera": "CH",     # Italian
    "danmark": "DK",
    # ---- Spanish names ------------------------------------------------
    "españa": "ES", "espana": "ES",
    "bélgica": "BE",
    "suiza": "CH",
    "república checa": "CZ",
    "alemania": "DE",
    "dinamarca": "DK",
    "finlandia": "FI",
    "francia": "FR",
    "grecia": "GR",
    "croacia": "HR",
    "hungría": "HU",
    "irlanda": "IE",
    "italia": "IT",       # same as native
    "lituania": "LT",
    "letonia": "LV",
    "países bajos": "NL", "holanda": "NL",
    "noruega": "NO",
    "polonia": "PL",
    "rumania": "RO",
    "eslovaquia": "SK",
    "eslovenia": "SI",
    "suecia": "SE",
    "turquía": "TR",
    "reino unido": "GB",
    # ---- Adjective forms (English) ------------------------------------
    "austrian": "AT",
    "belgian": "BE",
    "swiss": "CH",
    "czech": "CZ",
    "german": "DE",
    "danish": "DK",
    "estonian": "EE",
    "spanish": "ES",
    "finnish": "FI",
    "french": "FR",
    "british": "GB",
    "greek": "GR",
    "croatian": "HR",
    "hungarian": "HU",
    "irish": "IE",
    "israeli": "IL",
    "italian": "IT",
    "lithuanian": "LT",
    "latvian": "LV",
    "maltese": "MT",
    "dutch": "NL",
    "norwegian": "NO",
    "polish": "PL",
    "portuguese": "PT",
    "romanian": "RO",
    "serbian": "RS",
    "swedish": "SE",
    "slovenian": "SI",
    "slovak": "SK",
    "turkish": "TR",
    "american": "US",
}

# Pre-sorted by length (longest first) for greedy text matching.
SORTED_ALIASES: List[str] = sorted(_ALIAS_TO_ISO2, key=len, reverse=True)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def norm_to_iso2(country: Optional[str]) -> Optional[str]:
    """Normalise any country representation to ISO-2 code.

    Accepts: English/native/Spanish full name, adjective form, or ISO-2 code.
    Returns: ISO-2 string (upper-case), or the input title-cased if not
             recognised (never raises, never returns None for non-empty input).
    Returns None only when *country* is None or empty.
    """
    if not country:
        return None
    s = str(country).strip().lower()
    iso2 = _ALIAS_TO_ISO2.get(s)
    if iso2:
        return iso2
    if len(s) == 2:
        return s.upper()
    # Fall back: return title-cased string (legible but unrecognised)
    return str(country).strip().title()


def iso2_to_display(iso2: Optional[str]) -> str:
    """Return English display name for an ISO-2 code.

    Falls back to the code itself if not in the map.
    """
    if not iso2:
        return ""
    return ISO2_TO_DISPLAY.get(iso2.upper(), iso2.upper())


def iso2_to_sf_name(iso2: Optional[str]) -> str:
    """Return the Salesforce ShippingCountry string for an ISO-2 code.

    Differs from ``iso2_to_display`` for CZ ("Czech Republic" in SF vs
    "Czechia" in the display map).
    """
    if not iso2:
        return ""
    return _ISO2_TO_SF.get(iso2.upper(), iso2.upper())


def resolve_countries(text: str) -> List[str]:
    """Extract all ISO-2 country codes mentioned in *text*.

    Uses longest-match-first strategy.  Returns a de-duplicated list in
    the order each country is first matched.

    For 2-letter ISO codes (e.g. "me", "no", "it"), requires the alias to
    appear in UPPERCASE in the original text to avoid matching common English
    words like "me" → Montenegro, "no" → Norway, "at" → Austria.
    """
    s = text.lower()
    found: List[str] = []
    for phrase in SORTED_ALIASES:
        # Short ISO codes: only match if typed in uppercase in original text
        if len(phrase) <= 2:
            if not re.search(r"(?<![A-Za-z])" + re.escape(phrase.upper()) + r"(?![A-Za-z])", text):
                continue
        if re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", s):
            iso2 = _ALIAS_TO_ISO2[phrase]
            if iso2 not in found:
                found.append(iso2)
    return found
