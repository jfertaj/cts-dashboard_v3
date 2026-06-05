"""Site-centric contact / role-presence report services.

Two Moby tools share the same join shape as ``assignment_report`` but key the
table off the *site's own account* (Opportunity RecordType SubAccount,
C_Type__c Clinical -> the linked Account, whose ShippingCountry / ShippingCity
hold the centre geography). Roles come from ``AccountContactRelation.Role__c``
on that account.

Pure helpers (SOQL building, account-set resolution, ACR indexing, row
assembly) are separated from Salesforce I/O so they unit-test without a
network. The thin ``fetch_*`` orchestrators run the queries (with 200-id
chunking) and call the pure assemblers.

Tools:
- ``site_contacts_report``  -> contact-grain table (one row per site x contact)
- ``site_role_presence``    -> site-grain table (one row per site, has_role flag)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.utils.soql_helpers import soql_escape_quote


# ---------------------------------------------------------------------------
# Shared SOQL fragments / constants
# ---------------------------------------------------------------------------

# A "site" is a SubAccount Clinical Account, excluding inactive ones. Mirrors
# the canonical clause used across salesforce_explorer.py and
# tools/salesforce.py::tool_group_count_sf.
_SITE_RT_CLAUSE = "RecordType.DeveloperName = 'SubAccount' AND C_Type__c = 'Clinical'"
_SITE_INACTIVE_CLAUSE = (
    "(Account_Inactive__c = false OR Account_Inactive__c = null) "
    "AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
)

_CHUNK = 200


def _soql_str_list(values: List[str]) -> str:
    """Quote+escape a list of strings for a SOQL IN(...) clause.

    Returns "" for an empty list — callers MUST guard against feeding an empty
    list into IN(...) (``IN ()`` is a SOQL syntax error).
    """
    return ",".join(f"'{soql_escape_quote(str(v))}'" for v in values)


def _chunked(seq: List[str], n: int = _CHUNK) -> List[List[str]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

@dataclass
class SiteContactFilters:
    """Filters for ``site_contacts_report`` (contact-grain)."""
    countries: List[str] = field(default_factory=list)   # Account.ShippingCountry IN (...)
    cities: List[str] = field(default_factory=list)       # Account.ShippingCity IN (...)
    studies: List[str] = field(default_factory=list)      # via Assignment__c.C_Opportunity_Name__r.Name
    roles: List[str] = field(default_factory=list)        # post-join filter on ACR Role__c
    limit: Optional[int] = None                           # cap on emitted rows


@dataclass
class SiteRolePresenceFilters:
    """Filters for ``site_role_presence`` (site-grain)."""
    role: str = ""                                        # REQUIRED target role (substring match)
    countries: List[str] = field(default_factory=list)
    cities: List[str] = field(default_factory=list)
    studies: List[str] = field(default_factory=list)
    mode: str = "all"                                     # all | present | absent


# ---------------------------------------------------------------------------
# Site-set resolution (account_id -> {name, country, city})
# ---------------------------------------------------------------------------

SiteIndex = Dict[str, Dict[str, Any]]


def build_site_soql(countries: List[str], cities: List[str]) -> str:
    """SOQL selecting the SubAccount Clinical accounts (sites) with geography."""
    where = [_SITE_RT_CLAUSE, _SITE_INACTIVE_CLAUSE]
    if countries:
        where.append(f"ShippingCountry IN ({_soql_str_list(countries)})")
    if cities:
        where.append(f"ShippingCity IN ({_soql_str_list(cities)})")
    clause = " AND ".join(where)
    return (
        "SELECT Id, Name, ShippingCountry, ShippingCity "
        f"FROM Account WHERE {clause} "
        "ORDER BY ShippingCountry, ShippingCity, Name"
    )


def build_study_accounts_soql(studies: List[str]) -> str:
    """SOQL selecting the centre AccountIds participating in the given studies.

    A site participates in a study if it has an Assignment__c whose
    C_Opportunity_Name__r.Name matches. Keys off C_Account__c (the centre
    account) — the same account used as the ACR pair key.
    """
    return (
        "SELECT C_Account__c FROM Assignment__c "
        f"WHERE C_Opportunity_Name__r.Name IN ({_soql_str_list(studies)}) "
        "AND C_Account__c != null"
    )


def build_site_index(site_records: List[Dict[str, Any]]) -> SiteIndex:
    """Map AccountId -> {name, country, city} preserving SOQL order via dict insertion."""
    idx: SiteIndex = {}
    for r in site_records or []:
        aid = r.get("Id")
        if aid and aid not in idx:
            idx[aid] = {
                "account_id": aid,
                "site": r.get("Name") or "",
                "country": r.get("ShippingCountry") or "",
                "city": r.get("ShippingCity") or "",
            }
    return idx


# ---------------------------------------------------------------------------
# ACR (AccountContactRelation) indexing — keyed by AccountId
# ---------------------------------------------------------------------------

# AccountId -> list of ACR records (each may carry a joined Contact.*).
AcrByAccount = Dict[str, List[Dict[str, Any]]]


def build_acr_by_account(acr_records: List[Dict[str, Any]]) -> AcrByAccount:
    idx: AcrByAccount = {}
    for r in acr_records or []:
        aid = r.get("AccountId")
        if aid:
            idx.setdefault(aid, []).append(r)
    return idx


def _contact_of(acr: Dict[str, Any]) -> Dict[str, Any]:
    return acr.get("Contact") or {}


# ---------------------------------------------------------------------------
# Tool 1 — site_contacts_report (contact-grain assembly)
# ---------------------------------------------------------------------------

CONTACT_COLUMNS = ["site", "country", "city", "first_name", "last_name",
                   "email", "title", "role"]

_CONTACT_LABELS = {
    "site": "Site", "country": "Country", "city": "City",
    "first_name": "First Name", "last_name": "Last Name", "email": "Email",
    "title": "Title", "role": "Role",
}


def _contact_table(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    columns = [{"key": k, "label": _CONTACT_LABELS[k]} for k in CONTACT_COLUMNS]
    return {"columns": columns, "rows": rows}


def assemble_contact_rows(site_index: SiteIndex, acr_by_account: AcrByAccount,
                          f: SiteContactFilters) -> Dict[str, Any]:
    """One row per (site, contact) with the contact's Role at that site's account."""
    role_filter = {_norm(r) for r in (f.roles or [])}
    rows: List[Dict[str, Any]] = []
    for aid, site in site_index.items():
        for acr in acr_by_account.get(aid, []):
            role = acr.get("Role__c") or ""
            if role_filter and _norm(role) not in role_filter:
                continue
            c = _contact_of(acr)
            rows.append({
                "site": site["site"],
                "country": site["country"],
                "city": site["city"],
                "first_name": c.get("FirstName") or "",
                "last_name": c.get("LastName") or "",
                "email": c.get("Email") or "",
                "title": c.get("Title") or "",
                "role": role,
            })
            if f.limit and len(rows) >= f.limit:
                return _contact_table(rows)
    return _contact_table(rows)


# ---------------------------------------------------------------------------
# Tool 2 — site_role_presence (site-grain, left-anti flag)
# ---------------------------------------------------------------------------

PRESENCE_COLUMNS = ["site", "country", "city", "has_role",
                    "first_name", "last_name", "email"]

_PRESENCE_LABELS = {
    "site": "Site", "country": "Country", "city": "City", "has_role": "Has Role",
    "first_name": "First Name", "last_name": "Last Name", "email": "Email",
}


def _matching_acr(acrs: List[Dict[str, Any]], role: str) -> Optional[Dict[str, Any]]:
    """First ACR whose Role__c contains the target role (case-insensitive)."""
    target = _norm(role)
    if not target:
        return None
    for acr in acrs:
        if target in _norm(acr.get("Role__c")):
            return acr
    return None


def assemble_presence_rows(site_index: SiteIndex, acr_by_account: AcrByAccount,
                           f: SiteRolePresenceFilters) -> Dict[str, Any]:
    """One row per site flagged by whether any ACR with the target role exists."""
    mode = (f.mode or "all").strip().lower()
    rows: List[Dict[str, Any]] = []
    for aid, site in site_index.items():
        match = _matching_acr(acr_by_account.get(aid, []), f.role)
        has_role = match is not None
        if mode == "present" and not has_role:
            continue
        if mode == "absent" and has_role:
            continue
        c = _contact_of(match) if match else {}
        rows.append({
            "site": site["site"],
            "country": site["country"],
            "city": site["city"],
            "has_role": has_role,
            "first_name": c.get("FirstName") or "",
            "last_name": c.get("LastName") or "",
            "email": c.get("Email") or "",
        })
    columns = [{"key": k, "label": _PRESENCE_LABELS[k]} for k in PRESENCE_COLUMNS]
    return {"columns": columns, "rows": rows}


# ---------------------------------------------------------------------------
# Shared I/O helpers (resolve site set, batch ACR fetch)
# ---------------------------------------------------------------------------

_ACR_FIELDS = (
    "AccountId, ContactId, Role__c, "
    "Contact.FirstName, Contact.LastName, Contact.Email, Contact.Title"
)


def _resolve_site_index(sf: Any, countries: List[str], cities: List[str],
                        studies: List[str]) -> SiteIndex:
    """Resolve the filtered site set to an ordered AccountId -> geo index.

    If ``studies`` is given, first restrict to centre accounts participating in
    those studies (via Assignment__c), then intersect with the geo-filtered
    SubAccount set.
    """
    site_idx = build_site_index(
        (sf.query_all(build_site_soql(countries, cities)) or {}).get("records", [])
    )
    if not studies:
        return site_idx
    study_recs = (sf.query_all(build_study_accounts_soql(studies)) or {}).get("records", [])
    study_ids = {r.get("C_Account__c") for r in study_recs if r.get("C_Account__c")}
    return {aid: v for aid, v in site_idx.items() if aid in study_ids}


def _fetch_acr_by_account(sf: Any, account_ids: List[str]) -> AcrByAccount:
    """Batch-query AccountContactRelation for the account ids, chunked at 200."""
    acr_records: List[Dict[str, Any]] = []
    for chunk in _chunked(account_ids):
        soql = (
            f"SELECT {_ACR_FIELDS} FROM AccountContactRelation "
            f"WHERE AccountId IN ({_soql_str_list(chunk)})"
        )
        acr_records.extend((sf.query_all(soql) or {}).get("records", []))
    return build_acr_by_account(acr_records)


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------

def fetch_site_contacts(sf: Any, f: SiteContactFilters) -> Dict[str, Any]:
    """site_contacts_report: resolve sites -> ACR rows -> contact-grain table."""
    site_idx = _resolve_site_index(sf, f.countries, f.cities, f.studies)
    if not site_idx:
        return _contact_table([])
    acr_by_account = _fetch_acr_by_account(sf, list(site_idx.keys()))
    return assemble_contact_rows(site_idx, acr_by_account, f)


def fetch_site_role_presence(sf: Any, f: SiteRolePresenceFilters) -> Dict[str, Any]:
    """site_role_presence: resolve sites -> ACR -> per-site has_role flag."""
    site_idx = _resolve_site_index(sf, f.countries, f.cities, f.studies)
    if not site_idx:
        return assemble_presence_rows(site_idx, {}, f)
    acr_by_account = _fetch_acr_by_account(sf, list(site_idx.keys()))
    return assemble_presence_rows(site_idx, acr_by_account, f)
