"""Assignment/Contact-centric report service.

Pure helpers (SOQL building, ACR indexing, role resolution, row assembly) are
separated from Salesforce I/O so they unit-test without a network. See spec:
docs/superpowers/specs/2026-06-05-assignment-contact-report-design.md
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.utils.soql_helpers import soql_escape_quote


@dataclass
class AssignmentFilters:
    studies: List[str] = field(default_factory=list)        # C_Opportunity_Name__r.Name IN (...)
    stages: List[str] = field(default_factory=list)         # C_Assignment_Stage__c IN (...)
    referral_only: bool = False                             # Referral_Contact__c = true
    roles: List[str] = field(default_factory=list)          # post-join filter on Role__c
    exclude_countries: List[str] = field(default_factory=list)  # post-fetch on center country
    include_countries: List[str] = field(default_factory=list)


def _soql_str_list(values: List[str]) -> str:
    """Quote+escape a list of strings for a SOQL IN(...) clause.

    NOTE: returns "" for an empty list — callers MUST guard against passing
    an empty list into an IN(...) clause (IN () is a SOQL syntax error).
    """
    return ",".join(f"'{soql_escape_quote(str(v))}'" for v in values)


# Fields fetched per assignment. Center/city/country/patient_population come from
# the assignment's center account (C_Account__r) — the same account used as the
# Role ACR pair key (C_Account__c). Using the contact's primary account here would
# be wrong for indirect referrals, where the contact's primary account differs from
# the assignment center (so the "exclude UK" country filter must key off the center).
# Name/email are contact-level (C_Contact_Name__r).
_SOQL_FIELDS = (
    "Id, C_Assignment_Stage__c, Referral_Contact__c, C_Account__c, "
    "C_Opportunity_Name__r.Name, "
    "C_Account__r.Name, C_Account__r.ShippingCity, "
    "C_Account__r.ShippingCountry, C_Account__r.Patient_Population__c, "
    "C_Contact_Name__c, C_Contact_Name__r.FirstName, C_Contact_Name__r.LastName, "
    "C_Contact_Name__r.Email"
)


def build_assignment_soql(filters: AssignmentFilters) -> str:
    where: List[str] = []
    if filters.studies:
        where.append(f"C_Opportunity_Name__r.Name IN ({_soql_str_list(filters.studies)})")
    if filters.stages:
        where.append(f"C_Assignment_Stage__c IN ({_soql_str_list(filters.stages)})")
    if filters.referral_only:
        where.append("Referral_Contact__c = true")
    where.append("C_Contact_Name__c != null")
    clause = " WHERE " + " AND ".join(where) if where else ""
    return (
        f"SELECT {_SOQL_FIELDS} FROM Assignment__c{clause} "
        "ORDER BY C_Account__r.ShippingCountry, "
        "C_Opportunity_Name__r.Name, C_Contact_Name__r.LastName"
    )


# ACR index: maps each contactId -> list of its AccountContactRelation records.
AcrIndex = Dict[str, List[Dict[str, Any]]]


def build_acr_index(acr_records: List[Dict[str, Any]]) -> AcrIndex:
    idx: AcrIndex = {}
    for r in acr_records or []:
        cid = r.get("ContactId")
        if cid:
            idx.setdefault(cid, []).append(r)
    return idx


def resolve_role(idx: AcrIndex, center_account_id: Optional[str], contact_id: str) -> str:
    """Prefer a non-empty role on the center pair, else the most recently
    modified non-empty role for the contact; '' if none."""
    recs = idx.get(contact_id) or []
    # 1) center-pair, non-empty
    for r in recs:
        if center_account_id and r.get("AccountId") == center_account_id and (r.get("Role__c") or "").strip():
            return r["Role__c"]
    # 2) latest non-empty for the contact
    # An empty role on the center pair intentionally yields to a non-center non-empty role.
    nonempty = [r for r in recs if (r.get("Role__c") or "").strip()]
    if not nonempty:
        return ""
    # Lexical sort of LastModifiedDate works because Salesforce returns fixed-width
    # ISO-8601 'YYYY-MM-DDThh:mm:ss.sssZ'. Secondary key (AccountId) breaks
    # same-timestamp ties deterministically instead of relying on SOQL order.
    nonempty.sort(key=lambda r: (r.get("LastModifiedDate") or "", r.get("AccountId") or ""), reverse=True)
    return nonempty[0]["Role__c"]


REPORT_COLUMNS = ["study", "stage", "referral", "center", "city",
                  "patient_population", "first_name", "last_name", "email", "role"]

_COLUMN_LABELS = {
    "study": "Study", "stage": "Stage", "referral": "Referral", "center": "Center",
    "city": "City", "patient_population": "Patient Population", "first_name": "First Name",
    "last_name": "Last Name", "email": "Email", "role": "Role",
}


def _nested(rec: Dict[str, Any], path: str) -> Any:
    cur: Any = rec
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def assemble_rows(assignments: List[Dict[str, Any]], acr_index: AcrIndex,
                  f: AssignmentFilters) -> Dict[str, Any]:
    excl = {c.strip().lower() for c in (f.exclude_countries or [])}
    incl = {c.strip().lower() for c in (f.include_countries or [])}
    role_filter = {r.strip().lower() for r in (f.roles or [])}
    rows: List[Dict[str, Any]] = []
    for a in assignments or []:
        # Country/center fields key off the assignment center (C_Account__r), not
        # the contact's primary account — see _SOQL_FIELDS note.
        country = _nested(a, "C_Account__r.ShippingCountry") or ""
        cl = country.strip().lower()
        if excl and cl in excl:
            continue
        if incl and cl not in incl:
            continue
        role = resolve_role(acr_index, a.get("C_Account__c"), a.get("C_Contact_Name__c") or "")
        if role_filter and role.strip().lower() not in role_filter:
            continue
        rows.append({
            "study": _nested(a, "C_Opportunity_Name__r.Name") or "",
            "stage": a.get("C_Assignment_Stage__c") or "",
            "referral": bool(a.get("Referral_Contact__c")),
            "center": _nested(a, "C_Account__r.Name") or "",
            "city": _nested(a, "C_Account__r.ShippingCity") or "",
            "patient_population": _nested(a, "C_Account__r.Patient_Population__c") or "",
            "first_name": _nested(a, "C_Contact_Name__r.FirstName") or "",
            "last_name": _nested(a, "C_Contact_Name__r.LastName") or "",
            "email": _nested(a, "C_Contact_Name__r.Email") or "",
            "role": role,
        })
    columns = [{"key": k, "label": _COLUMN_LABELS[k]} for k in REPORT_COLUMNS]
    return {"columns": columns, "rows": rows}


_ACR_CHUNK = 200


def _chunked(seq: List[str], n: int) -> List[List[str]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def fetch_report(sf: Any, f: AssignmentFilters) -> Dict[str, Any]:
    """Run the two-step query (assignments -> ACR roles) and assemble rows."""
    arows = (sf.query_all(build_assignment_soql(f)) or {}).get("records", [])
    contact_ids = sorted({a.get("C_Contact_Name__c") for a in arows if a.get("C_Contact_Name__c")})
    acr_records: List[Dict[str, Any]] = []
    for chunk in _chunked(contact_ids, _ACR_CHUNK):
        soql = (
            "SELECT AccountId, ContactId, Role__c, IsDirect, LastModifiedDate "
            f"FROM AccountContactRelation WHERE ContactId IN ({_soql_str_list(chunk)})"
        )
        acr_records.extend((sf.query_all(soql) or {}).get("records", []))
    return assemble_rows(arows, build_acr_index(acr_records), f)
