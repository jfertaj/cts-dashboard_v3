# backend/app/routers/members_explorer.py
"""
Members Explorer — INNODIA Member institutions view.

Endpoints:
  GET  /api/members/bootstrap         → All Member accounts + counts (cached 5 min)
  POST /api/members/search            → Filtered subset (Python-side on cached data)
  GET  /api/members/{account_id}/detail → Contacts + SubAccounts for one Member
"""
from __future__ import annotations
import os, time, logging, threading, re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from simple_salesforce.exceptions import SalesforceExpiredSession, SalesforceAuthenticationFailed, SalesforceMalformedRequest

from app.services.salesforce_oauth import (
    COOKIE_NAME, unsign_value, get_salesforce_from_session_id,
)
from app.utils.country_norms import norm_to_iso2, iso2_to_display

log = logging.getLogger("cts-backend")
router = APIRouter(prefix="/api/members", tags=["members"])

# ---------------------------------------------------------------------------
# SF session helpers (same pattern as salesforce_explorer.py)
# ---------------------------------------------------------------------------

def _get_sf(request: Request = None):
    from app.services.salesforce_service import get_service_sf
    sf = get_service_sf()
    if not sf:
        raise HTTPException(403, "No autenticado en Salesforce")
    return sf


def _sf_query(sf, soql: str) -> List[Dict]:
    try:
        res = sf.query_all(soql)
        return res.get("records", [])
    except (SalesforceExpiredSession, SalesforceAuthenticationFailed):
        # Service-account token went stale before its local TTL: re-mint + retry
        # once so a backend-token blip does not 401 (and log out) the user.
        from app.services.salesforce_service import get_service_sf, reset_service_sf_cache
        reset_service_sf_cache()
        try:
            return get_service_sf().query_all(soql).get("records", [])
        except (SalesforceExpiredSession, SalesforceAuthenticationFailed):
            raise HTTPException(status_code=503, detail="Salesforce authentication temporarily unavailable. Please retry.")
    except SalesforceMalformedRequest as e:
        raise HTTPException(status_code=400, detail=f"Invalid query: {e}")
    except Exception as e:
        log.exception("SF query failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Salesforce query failed: {e}")


# ---------------------------------------------------------------------------
# Country normalisation — imported from app.utils.country_norms
# ---------------------------------------------------------------------------

def _country_norm(country: Optional[str]) -> Optional[str]:
    return norm_to_iso2(country)


# ---------------------------------------------------------------------------
# Bootstrap cache (5-minute TTL per SF session token)
# ---------------------------------------------------------------------------

_CACHE: Dict[str, Any] = {"rows": [], "ts": 0.0}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 300  # 5 minutes

# SF fields for Member accounts
_MEMBER_FIELDS = [
    "Id", "Name",
    "ShippingCountry", "ShippingCity",
    "ShippingLatitude", "ShippingLongitude",
    "C_Level_of_Membership__c", "C_Membership__c", "Account_Status__c",
    "Account_Inactive__c", "Phone", "Website",
    "C_Member_Representative__c",
    "C_Member_Representative__r.Name",
    "C_Member_Representative__r.Email",
    # Proposed roles
    "Clinical_Site_CS__c",
    "C_Deliver_Clinical_Grade_Services__c",
    "C_Perform_Cutting_Edge__c",
    "C_Contribute_as_a_Patient_Organization__c",
    # Validated roles
    "Clinical_Site_CS_validated__c",
    "Clinical_Trial_Site_CTS_validated__c",
    "Diagnostic_Lab_DxLab_validated__c",
    "Research_Mechanistic_Lab_LAB_validated__c",
    "Patient_Organization_validated__c",
]

_MEMBER_SOQL = (
    "SELECT " + ", ".join(_MEMBER_FIELDS) + " "
    "FROM Account "
    "WHERE RecordType.DeveloperName = 'RT_Member' "
    "  AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
    "ORDER BY Name "
    "LIMIT 2000"
)


def _build_member_rows(sf) -> List[Dict]:
    """Fetch Members + batch counts, return normalised row list."""
    records = _sf_query(sf, _MEMBER_SOQL)
    if not records:
        return []

    member_ids = [r["Id"] for r in records]

    # Batch: SubAccount counts per member
    sub_counts: Dict[str, int] = {}
    if member_ids:
        id_list = "', '".join(member_ids)
        soql_sub = (
            f"SELECT C_Member__c, COUNT(Id) cnt "
            f"FROM Account "
            f"WHERE C_Member__c IN ('{id_list}') "
            f"  AND RecordType.DeveloperName = 'SubAccount' "
            f"  AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) "
            f"GROUP BY C_Member__c"
        )
        try:
            for rec in _sf_query(sf, soql_sub):
                mid = rec.get("C_Member__c")
                if mid:
                    sub_counts[mid] = rec.get("cnt", 0)
        except HTTPException:
            pass  # counts non-critical

    # Batch: Contact counts per member
    contact_counts: Dict[str, int] = {}
    if member_ids:
        id_list = "', '".join(member_ids)
        soql_c = (
            f"SELECT AccountId, COUNT(Id) cnt "
            f"FROM Contact "
            f"WHERE AccountId IN ('{id_list}') "
            f"  AND (Contact_Inactive__c = false OR Contact_Inactive__c = null) "
            f"GROUP BY AccountId"
        )
        try:
            for rec in _sf_query(sf, soql_c):
                aid = rec.get("AccountId")
                if aid:
                    contact_counts[aid] = rec.get("cnt", 0)
        except HTTPException:
            pass

    rows = []
    for r in records:
        mid = r["Id"]
        country_raw = r.get("ShippingCountry") or ""
        country_iso = _country_norm(country_raw) or country_raw

        # Representative info (nested relationship object)
        rep = r.get("C_Member_Representative__r") or {}
        rep_name = rep.get("Name") if isinstance(rep, dict) else None
        rep_email = rep.get("Email") if isinstance(rep, dict) else None

        rows.append({
            "account_id": mid,
            "account_name": r.get("Name") or "",
            "country": country_iso,
            "city": r.get("ShippingCity") or "",
            "lat": r.get("ShippingLatitude"),
            "lng": r.get("ShippingLongitude"),
            "data": {
                "sf.C_Level_of_Membership__c": r.get("C_Level_of_Membership__c"),
                "sf.C_Membership__c": r.get("C_Membership__c"),
                "sf.Account_Status__c": r.get("Account_Status__c"),
                "sf.Phone": r.get("Phone"),
                "sf.Website": r.get("Website"),
                "sf.C_Member_Representative__r.Name": rep_name,
                "sf.C_Member_Representative__r.Email": rep_email,
                # Proposed roles
                "sf.Clinical_Site_CS__c": bool(r.get("Clinical_Site_CS__c")),
                "sf.C_Deliver_Clinical_Grade_Services__c": bool(r.get("C_Deliver_Clinical_Grade_Services__c")),
                "sf.C_Perform_Cutting_Edge__c": bool(r.get("C_Perform_Cutting_Edge__c")),
                "sf.C_Contribute_as_a_Patient_Organization__c": bool(r.get("C_Contribute_as_a_Patient_Organization__c")),
                # Validated roles
                "sf.Clinical_Site_CS_validated__c": bool(r.get("Clinical_Site_CS_validated__c")),
                "sf.Clinical_Trial_Site_CTS_validated__c": bool(r.get("Clinical_Trial_Site_CTS_validated__c")),
                "sf.Diagnostic_Lab_DxLab_validated__c": bool(r.get("Diagnostic_Lab_DxLab_validated__c")),
                "sf.Research_Mechanistic_Lab_LAB_validated__c": bool(r.get("Research_Mechanistic_Lab_LAB_validated__c")),
                "sf.Patient_Organization_validated__c": bool(r.get("Patient_Organization_validated__c")),
                # Derived counts
                "extra.SubAccountsCount": sub_counts.get(mid, 0),
                "extra.ContactsCount": contact_counts.get(mid, 0),
            },
        })
    return rows


def _get_cached_rows(sf) -> List[Dict]:
    global _CACHE
    now = time.time()
    with _CACHE_LOCK:
        if not _CACHE["rows"] or (now - _CACHE["ts"]) > _CACHE_TTL:
            _CACHE["rows"] = _build_member_rows(sf)
            _CACHE["ts"] = now
        return list(_CACHE["rows"])


# ---------------------------------------------------------------------------
# Filter evaluation (simple, Python-side)
# ---------------------------------------------------------------------------

def _eval_filter(rule: Dict, row: Dict) -> bool:
    """Evaluate a single filter rule against a member row."""
    field: str = rule.get("field", "")
    op: str = rule.get("operator", rule.get("op", "equals"))
    value = rule.get("value")

    # Resolve field value from row
    if field == "site.country":
        cell = row.get("country", "")
    elif field == "site.city":
        cell = row.get("city", "")
    elif field in ("sf.Name", "account_name"):
        cell = row.get("account_name", "")
    else:
        cell = row.get("data", {}).get(field)

    # Normalise
    if cell is None:
        cell_s = ""
    else:
        cell_s = str(cell).strip().lower()

    val_s = str(value).strip().lower() if value is not None else ""

    # Country: match ISO2 or display name (supports equals and in operators)
    if field == "site.country" and value is not None:
        norm_cell = _country_norm(str(cell)) or str(cell).upper()
        # 'in' operator with list of values (multi-select)
        if op in ("in",) and isinstance(value, list):
            for v in value:
                norm_v = _country_norm(str(v)) or str(v).upper()
                display_v = iso2_to_display(str(v)) or str(v)
                if (norm_cell == norm_v or
                        str(cell).lower() == display_v.lower() or
                        str(cell).upper() == str(v).upper()):
                    return True
            return False
        # 'equals' operator (single value)
        norm_filter = _country_norm(str(value)) or str(value).upper()
        display_filter = iso2_to_display(str(value)) or str(value)
        return (norm_cell == norm_filter or
                str(cell).lower() == display_filter.lower() or
                str(cell).upper() == str(value).upper())

    if op in ("equals", "eq", "="):
        if isinstance(cell, bool):
            return cell == bool(value)
        return cell_s == val_s

    if op in ("not_equals", "neq", "!="):
        if isinstance(cell, bool):
            return cell != bool(value)
        return cell_s != val_s

    if op in ("contains", "icontains"):
        return val_s in cell_s

    if op == "not_contains":
        return val_s not in cell_s

    if op in ("in",):
        if isinstance(value, list):
            return cell_s in [str(v).strip().lower() for v in value]
        return cell_s == val_s

    if op == "not_in":
        if isinstance(value, list):
            return cell_s not in [str(v).strip().lower() for v in value]
        return True

    if op in (">", "gt"):
        try:
            return float(cell) > float(value)
        except (TypeError, ValueError):
            return False

    if op in (">=", "gte"):
        try:
            return float(cell) >= float(value)
        except (TypeError, ValueError):
            return False

    if op in ("<", "lt"):
        try:
            return float(cell) < float(value)
        except (TypeError, ValueError):
            return False

    if op in ("<=", "lte"):
        try:
            return float(cell) <= float(value)
        except (TypeError, ValueError):
            return False

    if op == "is_empty":
        return cell is None or cell_s == ""

    if op == "is_not_empty":
        return cell is not None and cell_s != ""

    return True  # unknown op → permissive


def _apply_filter_group(group: Dict, row: Dict) -> bool:
    logic = group.get("logic", "AND").upper()
    rules = group.get("rules", [])
    if not rules:
        return True
    results = []
    for rule in rules:
        if "rules" in rule:
            results.append(_apply_filter_group(rule, row))
        else:
            results.append(_eval_filter(rule, row))
    if logic == "OR":
        return any(results)
    return all(results)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class FilterRule(BaseModel):
    field: str
    operator: Optional[str] = "equals"
    op: Optional[str] = None
    value: Any = None


class FilterGroup(BaseModel):
    logic: str = "AND"
    rules: List[Any] = []


class SearchRequest(BaseModel):
    filters: Optional[FilterGroup] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/bootstrap")
def members_bootstrap(request: Request):
    """Return all active Member accounts with role flags and counts."""
    sf = _get_sf(request)
    rows = _get_cached_rows(sf)
    return {"rows": rows, "total": len(rows)}


@router.post("/search")
def members_search(request: Request, body: SearchRequest):
    """Filter Member accounts by country, level, roles, counts, etc."""
    sf = _get_sf(request)
    all_rows = _get_cached_rows(sf)

    if body.filters and body.filters.rules:
        filtered = [r for r in all_rows if _apply_filter_group(body.filters.dict(), r)]
    else:
        filtered = all_rows

    return {"rows": filtered, "total": len(filtered)}


@router.get("/{account_id}/detail")
def member_detail(account_id: str, request: Request):
    """Return full detail for one Member: contacts + SubAccounts + their contacts."""
    # Validate SF ID format (15 or 18 alphanumeric chars)
    if not re.fullmatch(r"[A-Za-z0-9]{15,18}", account_id):
        raise HTTPException(status_code=404, detail="Member not found")

    sf = _get_sf(request)

    # --- Member account basic info (from cache) ---
    all_rows = _get_cached_rows(sf)
    member_row = next((r for r in all_rows if r["account_id"] == account_id), None)

    # --- Member contacts ---
    soql_mc = (
        f"SELECT Id, Name, Email, Phone, MobilePhone, Title, Department, "
        f"C_Board_Member__c, C_Country_Lead__c, "
        f"C_Voting_Rights__c, C_Content_Communication_Person__c "
        f"FROM Contact "
        f"WHERE AccountId = '{account_id}' "
        f"  AND (Contact_Inactive__c = false OR Contact_Inactive__c = null) "
        f"ORDER BY Name"
    )
    mc_records = _sf_query(sf, soql_mc)
    member_contacts = [
        {
            "id": c["Id"],
            "name": c.get("Name") or "",
            "email": c.get("Email"),
            "phone": c.get("Phone") or c.get("MobilePhone"),
            "title": c.get("Title"),
            "department": c.get("Department"),
            "is_board_member": bool(c.get("C_Board_Member__c")),
            "is_country_lead": bool(c.get("C_Country_Lead__c")),
            "voting_rights": bool(c.get("C_Voting_Rights__c")),
            "is_content_person": bool(c.get("C_Content_Communication_Person__c")),
        }
        for c in mc_records
    ]

    # --- SubAccounts ---
    soql_sub = (
        f"SELECT Id, Name, ShippingCountry, ShippingCity, "
        f"INNODIA_Clinical_Trial_Site__c, Clinical_Site_CS__c, "
        f"C_Deliver_Clinical_Grade_Services__c, C_Referral_Clinical_Partner__c, "
        f"C_Accredited_Clinical_Trial_Site__c, Account_Status__c, "
        f"Subaccount_Inactive__c "
        f"FROM Account "
        f"WHERE C_Member__c = '{account_id}' "
        f"  AND RecordType.DeveloperName = 'SubAccount' "
        f"  AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) "
        f"ORDER BY Name"
    )
    sub_records = _sf_query(sf, soql_sub)

    subaccount_ids = [s["Id"] for s in sub_records]

    # --- SubAccount contacts (batch) ---
    sub_contacts: Dict[str, List[Dict]] = {sid: [] for sid in subaccount_ids}
    if subaccount_ids:
        id_list = "', '".join(subaccount_ids)
        soql_sc = (
            f"SELECT Id, Name, Email, Phone, Title, Department, AccountId "
            f"FROM Contact "
            f"WHERE AccountId IN ('{id_list}') "
            f"  AND (Contact_Inactive__c = false OR Contact_Inactive__c = null) "
            f"ORDER BY AccountId, Name"
        )
        for c in _sf_query(sf, soql_sc):
            aid = c.get("AccountId")
            if aid in sub_contacts:
                sub_contacts[aid].append({
                    "id": c["Id"],
                    "name": c.get("Name") or "",
                    "email": c.get("Email"),
                    "phone": c.get("Phone"),
                    "title": c.get("Title"),
                    "department": c.get("Department"),
                })

    subaccounts = []
    for s in sub_records:
        sid = s["Id"]
        country_raw = s.get("ShippingCountry") or ""
        subaccounts.append({
            "id": sid,
            "name": s.get("Name") or "",
            "country": _country_norm(country_raw) or country_raw,
            "city": s.get("ShippingCity") or "",
            "is_cts": bool(s.get("INNODIA_Clinical_Trial_Site__c")),
            "is_cs": bool(s.get("Clinical_Site_CS__c")),
            "is_dxlab": bool(s.get("C_Deliver_Clinical_Grade_Services__c")),
            "is_rp": bool(s.get("C_Referral_Clinical_Partner__c")),
            "is_accredited": bool(s.get("C_Accredited_Clinical_Trial_Site__c")),
            "status": s.get("Account_Status__c"),
            "contacts": sub_contacts.get(sid, []),
        })

    # Build proposed/validated role summary from cache
    data = (member_row or {}).get("data", {})
    proposed_roles = {
        "cs":         data.get("sf.Clinical_Site_CS__c", False),
        "dxlab":      data.get("sf.C_Deliver_Clinical_Grade_Services__c", False),
        "lab":        data.get("sf.C_Perform_Cutting_Edge__c", False),
        "patient_org":data.get("sf.C_Contribute_as_a_Patient_Organization__c", False),
    }
    validated_roles = {
        "cs":         data.get("sf.Clinical_Site_CS_validated__c", False),
        "cts":        data.get("sf.Clinical_Trial_Site_CTS_validated__c", False),
        "dxlab":      data.get("sf.Diagnostic_Lab_DxLab_validated__c", False),
        "lab":        data.get("sf.Research_Mechanistic_Lab_LAB_validated__c", False),
        "patient_org":data.get("sf.Patient_Organization_validated__c", False),
    }

    return {
        "account_id": account_id,
        "account_name": (member_row or {}).get("account_name", ""),
        "country": (member_row or {}).get("country", ""),
        "city": (member_row or {}).get("city", ""),
        "level": data.get("sf.C_Level_of_Membership__c"),
        "status": data.get("sf.Account_Status__c"),
        "phone": data.get("sf.Phone"),
        "website": data.get("sf.Website"),
        "representative_name": data.get("sf.C_Member_Representative__r.Name"),
        "representative_email": data.get("sf.C_Member_Representative__r.Email"),
        "proposed_roles": proposed_roles,
        "validated_roles": validated_roles,
        "member_contacts": member_contacts,
        "subaccounts": subaccounts,
    }
