// ISO 3166-1 alpha-2 → English country name (European subset used in INNODIA)
export const ISO2_COUNTRY: Record<string, string> = {
  AD: "Andorra",
  AL: "Albania",
  AT: "Austria",
  BA: "Bosnia and Herzegovina",
  BE: "Belgium",
  BG: "Bulgaria",
  CH: "Switzerland",
  CY: "Cyprus",
  CZ: "Czechia",
  DE: "Germany",
  DK: "Denmark",
  EE: "Estonia",
  ES: "Spain",
  FI: "Finland",
  FR: "France",
  GB: "United Kingdom",
  GR: "Greece",
  HR: "Croatia",
  HU: "Hungary",
  IE: "Ireland",
  IT: "Italy",
  LT: "Lithuania",
  LU: "Luxembourg",
  LV: "Latvia",
  ME: "Montenegro",
  MK: "North Macedonia",
  MT: "Malta",
  NL: "Netherlands",
  NO: "Norway",
  PL: "Poland",
  PT: "Portugal",
  RO: "Romania",
  RS: "Serbia",
  SE: "Sweden",
  SI: "Slovenia",
  SK: "Slovakia",
  TR: "Turkey",
};

/**
 * Returns the full English country name for a known ISO2 code.
 * Falls back to the original value if not recognised.
 */
export function displayCountry(code: string | null | undefined): string {
  if (!code) return "";
  return ISO2_COUNTRY[code.trim().toUpperCase()] ?? code;
}
