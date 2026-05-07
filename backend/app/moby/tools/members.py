"""Members + contact-grouping tool implementations for Moby.

Pure move from `app.routers.ai_chat` (Phase 2 refactor). Behavior is
unchanged. Helpers (`tool_salesforce_query`) are resolved lazily via
`from app.routers import ai_chat as _ai` to avoid an import cycle at
module-load time (ai_chat re-exports these tool functions back).

The functions are re-exported by `ai_chat.py`, so existing tests / callers
that import `app.routers.ai_chat.tool_members_search` etc. keep working.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import HTTPException, Request


def tool_members_search(
    request: Request,
    filters: Dict[str, Any],
    include_detail: bool = False,
) -> Dict[str, Any]:
    """Proxy interno al endpoint /api/members/search."""
    url = "http://127.0.0.1:8000/api/members/search"
    payload: Dict[str, Any] = {
        "filters": filters or {"logic": "AND", "rules": []},
    }
    headers: Dict[str, str] = {}
    if request:
        ck = request.headers.get("cookie")
        if ck:
            headers["cookie"] = ck
    with httpx.Client(timeout=60.0) as cli:
        resp = cli.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"members_search failed: {resp.text[:300]}")
    data = resp.json()
    rows = data.get("rows") or []
    # Build flat rows for table display
    flat_rows = []
    for r in rows:
        flat: Dict[str, Any] = {
            "account_id": r.get("account_id", ""),
            "account_name": r.get("account_name", ""),
            "country": r.get("country", ""),
            "city": r.get("city", ""),
        }
        d = r.get("data") or {}
        flat["Level of Membership"] = d.get("sf.C_Level_of_Membership__c") or ""
        flat["Status"] = d.get("sf.Account_Status__c") or ""
        flat["Representative"] = d.get("sf.C_Member_Representative__r.Name") or ""
        flat["# Sites"] = d.get("extra.SubAccountsCount", 0)
        flat["# Contacts"] = d.get("extra.ContactsCount", 0)
        # Proposed roles as compact string
        proposed = []
        if d.get("sf.Clinical_Site_CS__c"):            proposed.append("CS")
        if d.get("sf.C_Deliver_Clinical_Grade_Services__c"): proposed.append("DxLab")
        if d.get("sf.C_Perform_Cutting_Edge__c"):      proposed.append("LAB")
        if d.get("sf.C_Contribute_as_a_Patient_Organization__c"): proposed.append("PatOrg")
        flat["Proposed Roles"] = ", ".join(proposed) if proposed else "—"
        # Validated roles as compact string
        validated = []
        if d.get("sf.Clinical_Site_CS_validated__c"):            validated.append("CS")
        if d.get("sf.Clinical_Trial_Site_CTS_validated__c"):     validated.append("CTS")
        if d.get("sf.Diagnostic_Lab_DxLab_validated__c"):        validated.append("DxLab")
        if d.get("sf.Research_Mechanistic_Lab_LAB_validated__c"):validated.append("LAB")
        if d.get("sf.Patient_Organization_validated__c"):        validated.append("PatOrg")
        flat["Validated Roles"] = ", ".join(validated) if validated else "—"
        flat_rows.append(flat)

    # Column definitions
    cols = [
        {"key": "account_name",        "label": "Institution"},
        {"key": "country",             "label": "Country"},
        {"key": "city",                "label": "City"},
        {"key": "Level of Membership", "label": "Level of Membership"},
        {"key": "Status",              "label": "Status"},
        {"key": "Representative",      "label": "Representative"},
        {"key": "# Sites",             "label": "# Sites"},
        {"key": "# Contacts",          "label": "# Contacts"},
        {"key": "Proposed Roles",      "label": "Proposed Roles"},
        {"key": "Validated Roles",     "label": "Validated Roles"},
    ]
    return {
        "columns": cols,
        "rows": flat_rows[:500],
        "meta": {"total": len(rows)},
    }


def tool_contacts_by_group(
    sf,
    roles: Optional[List[str]] = None,
    title_contains: Optional[str] = None,
    group_by: Literal["country", "city"] = "country",
    top_n: int = 1,
):
    from app.routers import ai_chat as _ai

    if not sf:
        raise HTTPException(400, "No SF session")
    group_field = "Account.ShippingCountry" if group_by == "country" else "Account.ShippingCity"
    role_filter = ""
    if roles:
        role_vals = ", ".join([f"'{r}'" for r in roles])
        role_filter = f" AND Role__c IN ({role_vals})"
    title_filter = ""
    if title_contains:
        title_escaped = title_contains.replace("'", "\\'")
        title_filter = f" AND Contact.Title LIKE '%{title_escaped}%'"
    soql = f"""
        SELECT {group_field} grp, Contact.Name, Contact.Email, Contact.Phone, Role__c, Contact.Title, LastModifiedDate
        FROM AccountContactRelation
        WHERE {group_field} != null {role_filter} {title_filter}
        ORDER BY {group_field}, LastModifiedDate DESC
    """
    raw = _ai.tool_salesforce_query(sf, soql)
    recs = raw.get("records", []) if isinstance(raw, dict) else []
    out: Dict[Any, List[Dict[str, Any]]] = {}
    for r in recs:
        g = r.get("Account", {}).get("ShippingCountry" if group_by == "country" else "ShippingCity")
        out.setdefault(g, [])
        if len(out[g]) < top_n:
            out[g].append({
                "group": g,
                "contact_name": (r.get("Contact") or {}).get("Name"),
                "email": (r.get("Contact") or {}).get("Email"),
                "phone": (r.get("Contact") or {}).get("Phone"),
                "role": r.get("Role__c"),
                "title": (r.get("Contact") or {}).get("Title"),
            })
    rows = [item for sub in out.values() for item in sub]
    return {"columns": [
        {"key": "group", "label": group_by.title()},
        {"key": "contact_name", "label": "Contact"}, {"key": "email", "label": "Email"},
        {"key": "phone", "label": "Phone"}, {"key": "role", "label": "Role"},
        {"key": "title", "label": "Title"}
    ], "rows": rows}
