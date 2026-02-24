# app/routers/salesforce_extras.py
from typing import Optional, Dict, Any, List, Iterable, Tuple
from fastapi import APIRouter, HTTPException, Query, Request

#from app.services.salesforce_oauth import get_salesforce_from_session_id
#from app.routers.salesforce_auth import unsign_value, COOKIE_NAME

from app.routers.salesforce_explorer import _get_sf

router = APIRouter(prefix="/api/salesforce", tags=["salesforce-extras"])

# -------------------- Constantes comunes --------------------

TYPE_ALIASES = [
    "Qualification", "CTS/CTU Qualification",
    "Profiling", "CTS/CTU Profiling", "CTS Profiling",
]
TYPE_IN = ", ".join([f"'{t}'" for t in TYPE_ALIASES])

# En tu org estos son los API names reales:
API_INNODIA_CLINICAL_TRIAL = "INNODIA_Clinical_Trial_Site__c"
API_REFERRAL_OUTREACH      = "Referral_Outreach_Site_Non_CTS__c"
API_ELIGIBLE_DETECT        = "Elegible_for_DETECT_Site__c"

# RecordType developer names en tu org
RT_PROFILING = "RT_Profiling_Opportunities"
RT_ACCRED    = "RT_CTS_Accreditation"

# -------------------- Helpers SF --------------------

def _query_safe(sf, soql: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Igual que _query, pero nunca lanza: devuelve (resultado, error_str)
    """
    try:
        return sf.query(soql), None
    except Exception as e:
        return None, f"{e}"

def _query(sf, soql: str) -> Dict[str, Any]:
    try:
        return sf.query(soql)
    except Exception as e:
        # devolvemos un error 500 limpio en vez de stacktrace
        raise HTTPException(status_code=500, detail=f"Salesforce query failed: {e}")


# -------------------- Núcleo: extras por Account --------------------

def _account_extras_core(sf, account_id: str) -> Dict[str, Any]:
    errors: List[str] = []


    # ---- 1) Account: Member lookup + CS Contribution flags ----
    acc_soql = (
        "SELECT Id, Name, "
        "C_Member__c, C_Member__r.Id, C_Member__r.Name, "
        f"{API_INNODIA_CLINICAL_TRIAL}, {API_REFERRAL_OUTREACH}, {API_ELIGIBLE_DETECT} "
        f"FROM Account WHERE Id = '{account_id}' LIMIT 1"
    )
    acc, err = _query_safe(sf, acc_soql)
    if err: errors.append(f"account:{err}")

    member_name: Optional[str] = None
    member_id: Optional[str] = None
    cs_contrib = {
        # Use the real Salesforce API name as primary key exposed to clients
        "INNODIA_Clinical_Trial_Site__c": None,
        "Referral_Outreach_Site_Non_CTS__c": None,
        "Elegible_for_DETECT_Site__c": None,
    }

    if acc and acc.get("records"):
        rec = acc["records"][0]
        member_id = rec.get("C_Member__c")
        member_name = (rec.get("C_Member__r") or {}).get("Name")
        cs_contrib = {
            "INNODIA_Clinical_Trial_Site__c": rec.get(API_INNODIA_CLINICAL_TRIAL),
            "Referral_Outreach_Site_Non_CTS__c": rec.get(API_REFERRAL_OUTREACH),
            "Elegible_for_DETECT_Site__c": rec.get(API_ELIGIBLE_DETECT),
        }

    # ---- 2) PI via AccountContactRelation (Role__c = 'PI') ----
    soql_pi = (
        "SELECT Id, AccountId, ContactId, Contact.Name, Contact.Email, Contact.Phone, Role__c, LastModifiedDate "
        "FROM AccountContactRelation "
        f"WHERE AccountId = '{account_id}' AND Role__c = 'PI' "
        "ORDER BY LastModifiedDate DESC"
    )
    acr, err = _query_safe(sf, soql_pi)
    if err: errors.append(f"acr:{err}")
    pi: Dict[str, Any] = {}
    acr_preview: List[Dict[str, Any]] = []
    if acr and acr.get("records"):
        for r in acr["records"][:10]:
            c = (r.get("Contact") or {})
            acr_preview.append({
                "acrId": r.get("Id"),
                "contactId": r.get("ContactId"),
                "name": c.get("Name"),
                "email": c.get("Email"),
                "phone": c.get("Phone"),
                "Role__c": r.get("Role__c"),
            })
        # elegimos el más reciente con email si existe
        chosen = next((r for r in acr["records"] if (r.get("Contact") or {}).get("Email")), acr["records"][0])
        c = (chosen.get("Contact") or {})
        pi = {
            "contact_id": chosen.get("ContactId"),
            "name": c.get("Name"),
            "email": c.get("Email"),
            "phone": c.get("Phone"),
            "role__c": chosen.get("Role__c"),
        }

    # ---- 3) Oportunidad para “new diagnoses”: prioriza PROFILING sobre QUALIFICATION ----
    # ---- 3) Oportunidad (prefer Profiling) ----
    # Además traemos lookup PI de la Opportunity para usarlo como fallback:
    opp_fields = (
        "Id, Name, LastModifiedDate, "
        "C_Number_of_new_T1D_diagnosed_U_18__c, "
        "C_Number_of_new_T1D_diagnosed_O_18__c, "
        "C_Principal_Investigator__c, "
        "C_Principal_Investigator__r.Name, "
        "C_Principal_Investigator__r.Email, "
        "C_Principal_Investigator__r.Phone, "
        "RecordType.DeveloperName"
    )

    # 3a) intenta con Profiling primero (por RecordType)
    opp_prof_soql = (
        f"SELECT {opp_fields} FROM Opportunity "
        f"WHERE AccountId = '{account_id}' "
        f"AND RecordType.DeveloperName = '{RT_PROFILING}' "
        "ORDER BY LastModifiedDate DESC LIMIT 1"
    )
    opportunity: Dict[str, Any] = {}
    prof_rec, err = _query_safe(sf, opp_prof_soql)
    if err: errors.append(f"opp_prof:{err}")
    if prof_rec and prof_rec.get("records"):
        r = prof_rec["records"][0]
        opportunity = {
            "id": r.get("Id"),
            "name": r.get("Name"),
            "new_dx_u18": r.get("C_Number_of_new_T1D_diagnosed_U_18__c"),
            "new_dx_o18": r.get("C_Number_of_new_T1D_diagnosed_O_18__c"),
        }
        # Fallback de PI desde Opportunity si no lo teníamos por ACR
        if not pi and r.get("C_Principal_Investigator__c"):
            pi = {
                "contact_id": r.get("C_Principal_Investigator__c"),
                "name":  (r.get("C_Principal_Investigator__r") or {}).get("Name"),
                "email": (r.get("C_Principal_Investigator__r") or {}).get("Email"),
                "phone": (r.get("C_Principal_Investigator__r") or {}).get("Phone"),
                "role__c": "PI (Opportunity lookup)",
            }

    # 3b) fallback: Qualification por RecordType si no hubo Profiling
    if not opportunity:
        opp_qual_soql = (
            f"SELECT {opp_fields} FROM Opportunity "
            f"WHERE AccountId = '{account_id}' "
            f"AND RecordType.DeveloperName = '{RT_QUALIFICATION}' "
            "ORDER BY LastModifiedDate DESC LIMIT 1"
        )
        qual_rec, err = _query_safe(sf, opp_qual_soql)
        if err: errors.append(f"opp_qual:{err}")
        if qual_rec and qual_rec.get("records"):
            r = qual_rec["records"][0]
            opportunity = {
                "id": r.get("Id"),
                "name": r.get("Name"),
                "new_dx_u18": r.get("C_Number_of_new_T1D_diagnosed_U_18__c"),
                "new_dx_o18": r.get("C_Number_of_new_T1D_diagnosed_O_18__c"),
            }
            if not pi and r.get("C_Principal_Investigator__c"):
                pi = {
                    "contact_id": r.get("C_Principal_Investigator__c"),
                    "name":  (r.get("C_Principal_Investigator__r") or {}).get("Name"),
                    "email": (r.get("C_Principal_Investigator__r") or {}).get("Email"),
                    "phone": (r.get("C_Principal_Investigator__r") or {}).get("Phone"),
                    "role__c": "PI (Opportunity lookup)",
                }
            if not pi and r.get("C_Principal_Investigator__c"):
                pi = {
                    "contact_id": r.get("C_Principal_Investigator__c"),
                    "name":  (r.get("C_Principal_Investigator__r") or {}).get("Name"),
                    "email": (r.get("C_Principal_Investigator__r") or {}).get("Email"),
                    "phone": (r.get("C_Principal_Investigator__r") or {}).get("Phone"),
                    "role__c": "PI",
                }

    # ---- 4) Assignments del Account (tolerante si no existe el objeto) ----
    assign_soql = (
        "SELECT Id, Name, C_Account__c, "
        "C_Opportunity_Name__c, C_Opportunity_Name__r.Name, "
        "C_Assignment_Stage__c, Assignment_Type__c, CreatedDate "
        f"FROM Assignment__c WHERE C_Account__c = '{account_id}' "
        "ORDER BY CreatedDate DESC LIMIT 50"
    )
    assignments: List[Dict[str, Any]] = []
    try:
        ares = _query(sf, assign_soql)
        for a in ares.get("records", []):
            assignments.append({
                "id": a.get("Id"),
                "name": a.get("Name"),  # ej: A-00217
                "stage": a.get("C_Assignment_Stage__c"),
                "type": a.get("Assignment_Type__c"),
                "opportunity_id": a.get("C_Opportunity_Name__c"),
                "opportunity_name": (a.get("C_Opportunity_Name__r") or {}).get("Name"),
                "created": a.get("CreatedDate"),
            })
    except HTTPException as e:
        if e.status_code != 500:
            raise
    except Exception:
        errors.append("assignments:unknown")

    return {
        "account_id": account_id,
        "member": {"account_id": member_id, "name": member_name} if member_id else None,
        "pi": pi or None,
        "opportunity": opportunity or None,
        "assignments": assignments,
        "newDxUnder18": opportunity.get("new_dx_u18") if opportunity else None,
        "newDxOver18":  opportunity.get("new_dx_o18") if opportunity else None,
        # CS Contribution (booleans) -> claves “antiguas” pero valores correctos
            "csContribution": cs_contrib,
        # debug
        "_debug": {
            "acc_soql": acc_soql,
            "soql_pi": soql_pi,
            "acr_found": len(acr.get("records", [])) if isinstance(acr, dict) else 0,
            "acr_preview": acr_preview,
            "opp_prof_soql": opp_prof_soql,
            "opp_qual_soql": 'computed_if_needed',
            "assign_soql": assign_soql,
            "errors": errors,
        }
    }


# -------------------- Batch helpers --------------------

def _chunked(ids: Iterable[str], size: int = 150):
    buf: List[str] = []
    for x in ids:
        if not x:
            continue
        buf.append(str(x))
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf

def batch_fetch_account_extras(sf, account_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Devuelve un mapa:
      { <account_id>: {
          "extra.MemberName": <str|None>,
          "extra.PIName": <str|None>,
          "extra.PIEmail": <str|None>,
          "extra.PIPhone": <str|None>,
          "extra.CS_Clinical_Site_CS__c": <bool|None>,         # desde INNODIA_Clinical_Trial_Site__c
          "extra.CS_Referral_Outreach_Site__c": <bool|None>,   # Referral_Outreach_Site_Non_CTS__c
          "extra.CS_Eligible_DETECT__c": <bool|None>,          # Elegible_for_DETECT_Site__c
          "extra.AssignmentsCount": <int>,
        }, ... }

    Hace 3 consultas batched:
      - Account (member + flags CS)
      - AccountContactRelation (PI)
      - Assignment__c (COUNT() GROUP BY C_Account__c)
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not account_ids:
        return out

    acc_ids = sorted({str(a) for a in account_ids if a})

    # 1) Account: Member + flags CS
    for group in _chunked(acc_ids, 150):
        vals = ", ".join(f"'{x}'" for x in group)
        soql = (
            "SELECT Id, "
            "C_Member__c, C_Member__r.Name, "
            f"{API_INNODIA_CLINICAL_TRIAL}, {API_REFERRAL_OUTREACH}, {API_ELIGIBLE_DETECT} "
            f"FROM Account WHERE Id IN ({vals})"
        )
        recs = _query(sf, soql).get("records", [])
        for r in recs:
            aid = r.get("Id")
            if not aid:
                continue
            d = out.setdefault(str(aid), {})
            d["extra.MemberName"]                   = (r.get("C_Member__r") or {}).get("Name")
            d["extra.CS_Clinical_Site_CS__c"]      = r.get(API_INNODIA_CLINICAL_TRIAL)
            d["extra.CS_Referral_Outreach_Site__c"]= r.get(API_REFERRAL_OUTREACH)
            d["extra.CS_Eligible_DETECT__c"]       = r.get(API_ELIGIBLE_DETECT)

    # 2) PI via ACR (Role__c = 'PI') -> cogemos el más reciente con email si existe
    for group in _chunked(acc_ids, 150):
        vals = ", ".join(f"'{x}'" for x in group)
        soql = (
            "SELECT Id, AccountId, ContactId, Contact.Name, Contact.Email, Contact.Phone, Role__c, LastModifiedDate "
            "FROM AccountContactRelation "
            f"WHERE AccountId IN ({vals}) AND Role__c = 'PI' "
            "ORDER BY LastModifiedDate DESC"
        )
        recs = _query(sf, soql).get("records", [])
        best_by_acc: Dict[str, Dict[str, Any]] = {}
        for r in recs:
            aid = str(r.get("AccountId") or "")
            if not aid:
                continue
            cur = best_by_acc.get(aid)
            has_email = (r.get("Contact") or {}).get("Email")
            if cur is None:
                best_by_acc[aid] = r
            else:
                cur_has_email = (cur.get("Contact") or {}).get("Email")
                if has_email and not cur_has_email:
                    best_by_acc[aid] = r
                # si ambos tienen/no tienen email, ya están ordenados por fecha desc

        for aid, r in best_by_acc.items():
            d = out.setdefault(aid, {})
            c = (r.get("Contact") or {})
            d["extra.PIName"]  = c.get("Name")
            d["extra.PIEmail"] = c.get("Email")
            d["extra.PIPhone"] = c.get("Phone")

    # 3) Assignments count (tolerante si el objeto no existe)
    try:
        for group in _chunked(acc_ids, 150):
            vals = ", ".join(f"'{x}'" for x in group)
            soql = (
                "SELECT C_Account__c, COUNT() "
                "FROM Assignment__c "
                f"WHERE C_Account__c IN ({vals}) "
                "GROUP BY C_Account__c"
            )
            recs = _query(sf, soql).get("records", [])
            for r in recs:
                aid = str(r.get("C_Account__c") or "")
                if not aid:
                    continue
                d = out.setdefault(aid, {})
                cnt = r.get("expr0") or r.get("count") or r.get("COUNT()") or 0
                try:
                    cnt = int(cnt)
                except Exception:
                    cnt = 0
                d["extra.AssignmentsCount"] = cnt
    except HTTPException as he:
        if he.status_code != 500:
            raise
    except Exception:
        pass

    # Completar claves faltantes con None/0 para homogeneidad
    defaults = {
        "extra.MemberName": None,
        "extra.PIName": None,
        "extra.PIEmail": None,
        "extra.PIPhone": None,
        "extra.CS_Clinical_Site_CS__c": None,
        "extra.CS_Referral_Outreach_Site__c": None,
        "extra.CS_Eligible_DETECT__c": None,
        "extra.AssignmentsCount": 0,
    }
    for aid in acc_ids:
        d = out.setdefault(str(aid), {})
        for k, v in defaults.items():
            d.setdefault(k, v)

    return out


# -------------------- Endpoints públicos --------------------

@router.get("/account-extras")
def account_extras(account_id: str = Query(...), request: Request = None) -> Dict[str, Any]:
    sf = _get_sf(request)
    try:
        return _account_extras_core(sf, account_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Alias EXACTO que usa el front del Explorer/MapView
@router.get("/explorer/account-extras/{account_id}")
def account_extras_alias(account_id: str, request: Request = None) -> Dict[str, Any]:
    try:
        sf = _get_sf(request)
        out = _account_extras_core(sf, account_id)
        out.setdefault("_source", "salesforce_extras.py")
        return out
    except HTTPException as he:
        # Solo si es 401 devolvemos “vacío”; en 500 devolvemos un bloque mínimo con error explícito.
        if he.status_code == 401:
            return {
                "account_id": account_id,
                "member": None, "pi": None, "opportunity": None, "assignments": [],
                "newDxUnder18": None, "newDxOver18": None,
                "csContribution": {
                    "INNODIA_Clinical_Trial_Site__c": None,
                    "Clinical_Site_CS__c": None,
                    "Referral_Outreach_Site_Non_CTS__c": None,
                    "Elegible_for_DETECT_Site__c": None,
                },
                "error": f"salesforce_unauth: {he.detail}",
            }
        # En otros casos, devolvemos estructura mínima con error (no dejamos al frontend con “—” por todo)
        return {
            "account_id": account_id,
            "member": None, "pi": None, "opportunity": None, "assignments": [],
            "newDxUnder18": None, "newDxOver18": None,
            "csContribution": {
                "INNODIA_Clinical_Trial_Site__c": None,
                "Clinical_Site_CS__c": None,
                "Referral_Outreach_Site_Non_CTS__c": None,
                "Elegible_for_DETECT_Site__c": None,
            },
            "error": f"salesforce_error:{he.status_code}:{he.detail}",
        }
    except Exception as e:
        return {
            "account_id": account_id,
            "member": None, "pi": None, "opportunity": None, "assignments": [],
            "newDxUnder18": None, "newDxOver18": None,
            "csContribution": {
                "INNODIA_Clinical_Trial_Site__c": None,
                "Clinical_Site_CS__c": None,
                "Referral_Outreach_Site_Non_CTS__c": None,
                "Elegible_for_DETECT_Site__c": None,
            },
            "error": f"salesforce_unexpected:{e}",
        }


# -------------------- Endpoints de debug opcionales --------------------

@router.get("/debug/acr")
def debug_acr(account_id: str = Query(...), request: Request = None) -> Dict[str, Any]:
    sf = _get_sf(request)
    soql = (
        "SELECT Id, AccountId, ContactId, Contact.Name, Contact.Email, Contact.Phone, "
        "Role__c, IsActive, IsDirect, CreatedDate, LastModifiedDate "
        f"FROM AccountContactRelation WHERE AccountId = '{account_id}' "
        "ORDER BY LastModifiedDate DESC LIMIT 50"
    )
    rows = _query(sf, soql)
    return {"soql": soql, "count": len(rows.get("records", [])), "records": rows.get("records", [])}

@router.get("/debug/acr-roles")
def debug_acr_roles(account_id: str = Query(...), request: Request = None) -> Dict[str, Any]:
    sf = _get_sf(request)
    soql = f"SELECT Role__c FROM AccountContactRelation WHERE AccountId = '{account_id}'"
    rows = _query(sf, soql)
    vals = sorted({(r.get("Role__c") or "") for r in rows.get("records", []) if r})
    return {"distinct_roles": vals, "total_relations": len(rows.get("records", []))}