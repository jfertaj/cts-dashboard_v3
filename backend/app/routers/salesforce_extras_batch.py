# app/routers/salesforce_extras_batch.py
from typing import Dict, List, Any, Iterable

EXTRA_BOOL_DEFAULT = None  # usa False si prefieres

def _chunked(seq: Iterable[str], size: int = 150):
    buf: List[str] = []
    for x in seq:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf

def batch_fetch_account_extras(sf, account_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Devuelve por cada AccountId un dict con claves extra.* para inyectar en row.data.
    Requiere un cliente simple-salesforce en `sf`.
    """
    account_ids = [i for i in (account_ids or []) if i]
    out: Dict[str, Dict[str, Any]] = {aid: {} for aid in account_ids}
    if not account_ids:
        return out

    # --- 1) Account-level: Member + CS Contribution flags
    for chunk in _chunked(account_ids, 150):
        ids = ",".join([f"'{i}'" for i in chunk])
        soql = (
            "SELECT Id, "
            "C_Member__c, C_Member__r.Name, "
            "Clinical_Site_CS__c, INNODIA_Clinical_Trial_Site__c, Referral_Outreach_Site_Non_CTS__c, Elegible_for_DETECT_Site__c "
            f"FROM Account WHERE Id IN ({ids})"
        )
        res = sf.query(soql)
        for r in res.get("records", []):
            aid = r.get("Id")
            if not aid:
                continue
            o = out.setdefault(aid, {})
            o["extra.MemberName"] = (r.get("C_Member__r") or {}).get("Name")
            o["extra.Clinical_Site_CS__c"] = r.get("Clinical_Site_CS__c", EXTRA_BOOL_DEFAULT)
            o["extra.INNODIA_Clinical_Trial_Site__c"] = r.get("INNODIA_Clinical_Trial_Site__c", EXTRA_BOOL_DEFAULT)
            o["extra.Referral_Outreach_Site_Non_CTS__c"] = r.get("Referral_Outreach_Site_Non_CTS__c", EXTRA_BOOL_DEFAULT)
            o["extra.Elegible_for_DETECT_Site__c"] = r.get("Elegible_for_DETECT_Site__c", EXTRA_BOOL_DEFAULT)

    # --- 2) PI (vía AccountContactRelation con Role__c='PI')
    for chunk in _chunked(account_ids, 150):
        ids = ",".join([f"'{i}'" for i in chunk])
        soql = (
            "SELECT Id, AccountId, ContactId, Role__c, LastModifiedDate, "
            "Contact.Name, Contact.Email, Contact.Phone "
            f"FROM AccountContactRelation WHERE AccountId IN ({ids}) AND Role__c = 'PI' "
            "ORDER BY LastModifiedDate DESC"
        )
        res = sf.query(soql)
        seen = set()  # una PI por cuenta (la más reciente)
        for r in res.get("records", []):
            aid = r.get("AccountId")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            c = (r.get("Contact") or {})
            o = out.setdefault(aid, {})
            o["extra.PIName"]  = c.get("Name")
            o["extra.PIEmail"] = c.get("Email")
            o["extra.PIPhone"] = c.get("Phone")

    # --- 3) (Opcional) Conteo de Assignments por Account
    counts_by_acc: Dict[str, int] = {}
    for chunk in _chunked(account_ids, 150):
        ids = ",".join([f"'{i}'" for i in chunk])
        soql = (
            "SELECT C_Account__c, COUNT(Id) cnt "
            "FROM Assignment__c "
            f"WHERE C_Account__c IN ({ids}) "
            "GROUP BY C_Account__c"
        )
        res = sf.query(soql)
        for r in res.get("records", []):
            aid = r.get("C_Account__c")
            if not aid:
                continue
            # algunas versiones devuelven el alias como expr0
            cnt = r.get("cnt", r.get("expr0"))
            counts_by_acc[str(aid)] = cnt

    names_by_acc: Dict[str, List[str]] = {}
    for chunk in _chunked(account_ids, 150):
        ids = ",".join([f"'{i}'" for i in chunk])
        soql = (
            "SELECT C_Account__c, Name, C_Opportunity_Name__r.Name "
            "FROM Assignment__c "
            f"WHERE C_Account__c IN ({ids}) "
            "ORDER BY CreatedDate DESC"
        )
        res = sf.query_all(soql)
        for r in res.get("records", []):
            aid = r.get("C_Account__c")
            if not aid:
                continue
            name = (r.get("Name") or "").strip()
            if not name:
                continue
            opp = ((r.get("C_Opportunity_Name__r") or {}).get("Name") or "").strip()
            label = f"{name} ({opp})" if opp else name
            bucket = names_by_acc.setdefault(str(aid), [])
            if label in bucket:
                continue
            if len(bucket) >= 15:
                continue
            bucket.append(label)

    for aid, cnt in counts_by_acc.items():
        out.setdefault(aid, {})["extra.AssignmentsCount"] = cnt
    for aid, names in names_by_acc.items():
        if not names:
            continue
        entry = out.setdefault(aid, {})
        entry["extra.AssignmentsNames"] = "; ".join(names)
        entry.setdefault("extra.AssignmentsCount", len(names))

    return out
