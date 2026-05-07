"""SOQL validation + sanitization helpers extracted from ai_chat.py.

Phase 3 refactor — pure moves, no behavior changes. Lazy imports for the
SF allowlists / describe helper avoid circular deps with ai_chat.
"""
from __future__ import annotations

import re

from fastapi import HTTPException


def _validate_soql(soql: str, sf=None):
    from app.routers.ai_chat import (
        SF_ALLOWED_DYNAMIC,
        SF_ALLOWED_FIELDS,
        _describe_fields,
    )

    if not re.match(r"^\s*select\s", soql, re.I):
        raise HTTPException(400, "SOQL must start with SELECT")

    # Permitir queries a Opportunity, Account, Contact, AccountContactRelation
    allowed_objects = ["Opportunity", "Account", "Contact", "AccountContactRelation"]
    table_name = None
    for obj in allowed_objects:
        if re.search(rf"\bfrom\s+{obj}\b", soql, re.I):
            table_name = obj
            break

    if not table_name:
        raise HTTPException(400, f"SOQL must query FROM one of: {', '.join(allowed_objects)}")

    m = re.search(rf"select\s+(.*?)\s+from\s+{table_name}", soql, re.I | re.S)
    if not m:
        return
    raw_fields = m.group(1)
    fields = [f.strip() for f in re.split(r",(?![^()]*\))", raw_fields) if f.strip()]

    def norm(f: str) -> str:
        f = re.sub(r"\s+ASC|\s+DESC", "", f, flags=re.I)
        f = re.sub(r"\s+NULLS\s+(FIRST|LAST)", "", f, flags=re.I)
        f = f.split(" ")[0]
        return f

    # Per-object allowlists
    allowed_account = {
        "Id","Name","ShippingCountry","ShippingCity","ShippingLatitude","ShippingLongitude",
        "ParentId","C_Member__c","C_Member__r.Name","C_Type__c","Account_Inactive__c","Subaccount_Inactive__c",
    }
    allowed_contact = {
        "Id","Name","Email","Phone","Title","Department","AccountId","Account.Name",
    }
    # Opportunity fields are in SF_ALLOWED_FIELDS minus explicit Account.* (those must be prefixed)
    allowed_opp = {f for f in (SF_ALLOWED_FIELDS | SF_ALLOWED_DYNAMIC) if not f.startswith("Account.")}

    bad = []
    for f in fields:
        base = norm(f)
        if re.match(r"^(count|sum|min|max|avg)\s*\(", base, re.I):
            continue
        if table_name == "Opportunity":
            # Allow Account.* when querying Opportunity
            if base.startswith("Account."):
                # dynamic describe for Account; fall back to static allowlist when describe fails
                static_acc = {f for f in SF_ALLOWED_FIELDS if f.startswith('Account.')}
                if sf is not None:
                    acc_fields = _describe_fields(sf, 'Account')
                    dynamic_acc = {f'Account.{x}' for x in acc_fields} if acc_fields else set()
                    if base not in (dynamic_acc | static_acc | SF_ALLOWED_DYNAMIC):
                        bad.append(base)
                else:
                    if base not in (SF_ALLOWED_FIELDS | SF_ALLOWED_DYNAMIC):
                        bad.append(base)
                continue
            if base not in allowed_opp:
                bad.append(base)
        elif table_name == "Account":
            # Only Account fields allowed
            if base not in allowed_account:
                bad.append(base)
        elif table_name == "Contact":
            if base not in allowed_contact:
                bad.append(base)
        elif table_name == "AccountContactRelation":
            # Conservative: allow only fields we reference in tools
            allowed_acr = {
                "Id","AccountId","ContactId","Role__c","Contact.Name","Contact.Email","Contact.Phone","Contact.Title","Contact.Department","Account.Name"
            }
            if base not in allowed_acr:
                bad.append(base)
    if bad:
        raise HTTPException(400, f"SOQL field(s) not allowed for {table_name}: {', '.join(bad)}")


def _ensure_soql_has_account_id(soql: str) -> str:
    """
    Si el SELECT contiene algún campo Account.* pero NO incluye Account.Id, lo inyectamos.
    Conserva el resto del SOQL (WHERE/ORDER BY/LIMIT).
    """
    try:
        m = re.search(r"^\s*select\s+(?P<select>.+?)\s+from\s+Opportunity(?P<tail>.*)$", soql, flags=re.I | re.S)
        if not m:
            return soql
        sel = m.group("select")
        tail = m.group("tail") or ""
        # detectar si hay Account.* en el select
        has_account_fields = re.search(r"\bAccount\.[A-Za-z0-9_]+\b", sel) is not None
        has_account_id = re.search(r"\bAccount\.Id\b", sel) is not None
        if has_account_fields and not has_account_id:
            # insertamos al principio del SELECT para no romper alias ni ORDER BY
            new_sel = "Account.Id, " + sel.strip()
            fixed = f"SELECT {new_sel} FROM Opportunity{tail}"
            return fixed
        return soql
    except Exception:
        return soql


def _sanitize_soql_basic(soql: str) -> str:
    """
    Corrige errores comunes que el modelo puede introducir:
    - elimina prefijo ficticio 'sf.' delante de campos
    - reemplaza Account.Country/City por Account.ShippingCountry/Account.ShippingCity
    - normaliza NULL/Null → null para comparaciones
    - fuerza alias COUNT(Id) a 'count' (evita 'sites')
    """
    s = soql or ""
    # quitar 'sf.' sólo delante de identificadores (no tocar strings)
    s = re.sub(r"\bsf\.", "", s)
    # geografía correcta según whitelist
    s = re.sub(r"\bAccount\.Country\b", "Account.ShippingCountry", s)
    s = re.sub(r"\bAccount\.City\b", "Account.ShippingCity", s)
    # normalizar NULL literales
    s = re.sub(r"\bNULL\b", "null", s)
    # alias de COUNT(Id) → 'count'
    s = re.sub(r"(?i)(count\s*\(\s*id\s*\)\s+)sites\b", r"\1count", s)
    s = re.sub(r"(?i)(count\s*\(\s*id\s*\)\s+)(\w+)\b", lambda m: m.group(1) + ("count" if m.group(2).lower()=="sites" else m.group(2)), s)
    return s
