# Assignment & Contact Report (referral + Role) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an Assignment/Contact-centric report tool that reproduces the "myINNODIA Referral Database" and adds the `AccountContactRelation.Role__c` column native Salesforce reports cannot — surfaced as a new dashboard tab and a Moby tool, backed by one deterministic service.

**Architecture:** A single pure-functions-plus-IO service (`assignment_report.py`) builds SOQL over `Assignment__c`, joins each assignment's contact to its `AccountContactRelation.Role__c`, and returns `{rows, columns}`. A thin router exposes it at `POST /api/assignments/report`; a Moby tool wraps the same service; a new React tab consumes the endpoint. Isolated from the account-centric Explorer.

**Tech Stack:** Python 3 / FastAPI / simple_salesforce (backend), pytest (tests), React + TypeScript + Vite + TanStack Table (frontend), Playwright (E2E).

**Spec:** `docs/superpowers/specs/2026-06-05-assignment-contact-report-design.md`

**Branch:** `feat/assignment-contact-report` (already created; spec committed).

**Test command:** `python -m pytest backend/tests/ -v` (single file: append the path).

---

## Confirmed facts (from prod, do not re-discover)

`Assignment__c` join keys (verified via FieldDefinition):
- `C_Opportunity_Name__c` — Master-Detail(Opportunity); study name at `C_Opportunity_Name__r.Name`
- `C_Contact_Name__c` — Lookup(Contact)
- `C_Account__c` — Lookup(Account); the center
- `C_Assignment_Stage__c` — Picklist (Activated/Selected/Closed)
- `Referral_Contact__c` — Checkbox

Role lives on `AccountContactRelation.Role__c` (custom field). In prod the **standard** `Roles`
field is empty; `Role__c` holds Investigator / Study Coordinator / Study Nurse / etc. A contact may
have several ACRs (one per account); the role is populated on the indirect (`IsDirect=false`,
`_CS-…`) relation. Resolution strategy (validated to 53/55 on ground truth): index every ACR by
contactId; prefer a non-empty role on the center-pair `(C_Account__c, contactId)`, else the most
recently modified non-empty role for that contact.

Ground-truth oracle: `backend/tests/fixtures/referral_groundtruth.json` — 71 assignment-contact
rows / 55 unique contacts from the live report (studies Baricade/Safeguard/Beta Preserve, stage
Activated, Referral Contact = true, excluding United Kingdom). Shape:
`{"rows":[{"pop","city","stage","first","last","email"}...], "unique_emails":[...]}`.

---

## File Structure

**Create:**
- `backend/app/services/assignment_report.py` — core: filters model, SOQL builder, ACR index, role resolver, row assembler, fetch orchestration.
- `backend/app/routers/assignments_report.py` — thin router: `POST /api/assignments/report`, `GET /api/assignments/report/options`.
- `backend/tests/test_assignment_report.py` — unit tests for the pure functions.
- `backend/tests/fixtures/referral_groundtruth.json` — already added.
- `scripts/test_assignment_report_integration.py` — live integration vs ground truth (needs SF cookie).
- `frontend/src/pages/AssignmentsView.tsx` — new tab.
- `frontend/tests/e2e/assignments.spec.ts` — E2E (mocked API).

**Modify:**
- `backend/app/main.py` — register the new router.
- `backend/app/routers/moby_tools.py` — register `assignment_contact_report` tool.
- `backend/app/moby/tools_spec.py` — add the tool's JSON schema.
- `frontend/src/types.ts` — extend `Tab`.
- `frontend/src/App.tsx` — route the new tab.
- `frontend/src/components/Header.tsx` — add the tab button.
- `frontend/src/lib/api.ts` — add `assignmentReport()` call.

---

## Task 1: Filters model + SOQL builder (pure)

**Files:**
- Create: `backend/app/services/assignment_report.py`
- Test: `backend/tests/test_assignment_report.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_assignment_report.py
"""Unit tests for assignment_report service — pure, no DB/network.
Run: python -m pytest backend/tests/test_assignment_report.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.assignment_report import AssignmentFilters, build_assignment_soql, _soql_str_list


class TestSoqlStrList:
    def test_escapes_single_quote(self):
        assert _soql_str_list(["O'Brien", "Safe"]) == "'O\\'Brien','Safe'"

    def test_empty(self):
        assert _soql_str_list([]) == ""


class TestBuildAssignmentSoql:
    def test_referral_and_studies_and_stages(self):
        f = AssignmentFilters(
            studies=["Baricade", "Safeguard"],
            stages=["Activated"],
            referral_only=True,
        )
        soql = build_assignment_soql(f)
        assert "FROM Assignment__c" in soql
        assert "C_Opportunity_Name__r.Name IN ('Baricade','Safeguard')" in soql
        assert "C_Assignment_Stage__c IN ('Activated')" in soql
        assert "Referral_Contact__c = true" in soql
        assert "C_Contact_Name__c != null" in soql

    def test_no_filters_still_guards_contact(self):
        soql = build_assignment_soql(AssignmentFilters())
        assert "C_Contact_Name__c != null" in soql
        assert "Referral_Contact__c = true" not in soql
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_assignment_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.assignment_report'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/assignment_report.py
"""Assignment/Contact-centric report service.

Pure helpers (SOQL building, ACR indexing, role resolution, row assembly) are
separated from Salesforce I/O so they unit-test without a network. See spec:
docs/superpowers/specs/2026-06-05-assignment-contact-report-design.md
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AssignmentFilters:
    studies: List[str] = field(default_factory=list)        # C_Opportunity_Name__r.Name IN (...)
    stages: List[str] = field(default_factory=list)         # C_Assignment_Stage__c IN (...)
    referral_only: bool = False                             # Referral_Contact__c = true
    roles: List[str] = field(default_factory=list)          # post-join filter on Role__c
    exclude_countries: List[str] = field(default_factory=list)  # post-fetch on center country
    include_countries: List[str] = field(default_factory=list)


def _soql_str_list(values: List[str]) -> str:
    """Quote+escape a list of strings for a SOQL IN(...) clause."""
    return ",".join("'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)


# Fields fetched per assignment. Display fields come from the contact's primary
# account (mirrors the report's "Contact Name: ..." columns); C_Account__c is the
# center used as the Role ACR pair key.
_SOQL_FIELDS = (
    "Id, C_Assignment_Stage__c, Referral_Contact__c, C_Account__c, "
    "C_Opportunity_Name__r.Name, "
    "C_Contact_Name__c, C_Contact_Name__r.FirstName, C_Contact_Name__r.LastName, "
    "C_Contact_Name__r.Email, "
    "C_Contact_Name__r.Account.Name, C_Contact_Name__r.Account.ShippingCity, "
    "C_Contact_Name__r.Account.ShippingCountry, "
    "C_Contact_Name__r.Account.Patient_Population__c"
)


def build_assignment_soql(f: AssignmentFilters) -> str:
    where: List[str] = []
    if f.studies:
        where.append(f"C_Opportunity_Name__r.Name IN ({_soql_str_list(f.studies)})")
    if f.stages:
        where.append(f"C_Assignment_Stage__c IN ({_soql_str_list(f.stages)})")
    if f.referral_only:
        where.append("Referral_Contact__c = true")
    where.append("C_Contact_Name__c != null")
    clause = " WHERE " + " AND ".join(where) if where else ""
    return (
        f"SELECT {_SOQL_FIELDS} FROM Assignment__c{clause} "
        "ORDER BY C_Contact_Name__r.Account.ShippingCountry, "
        "C_Opportunity_Name__r.Name, C_Contact_Name__r.LastName"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest backend/tests/test_assignment_report.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/assignment_report.py backend/tests/test_assignment_report.py
git commit -m "feat(assignments): filters model + SOQL builder (pure)"
```

---

## Task 2: ACR index + role resolver (pure)

**Files:**
- Modify: `backend/app/services/assignment_report.py`
- Test: `backend/tests/test_assignment_report.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_assignment_report.py
from app.services.assignment_report import build_acr_index, resolve_role


def _acr(acc, con, role, direct=False, lm="2026-01-01T00:00:00Z"):
    return {"AccountId": acc, "ContactId": con, "Role__c": role,
            "IsDirect": direct, "LastModifiedDate": lm}


class TestRoleResolution:
    def test_prefers_center_pair_nonempty(self):
        idx = build_acr_index([
            _acr("ACENTER", "C1", "Investigator"),
            _acr("AOTHER", "C1", "Study Nurse"),
        ])
        assert resolve_role(idx, center_account_id="ACENTER", contact_id="C1") == "Investigator"

    def test_falls_back_to_latest_nonempty_for_contact(self):
        idx = build_acr_index([
            _acr("ACENTER", "C1", "", lm="2026-01-01T00:00:00Z"),
            _acr("AOTHER", "C1", "Study Coordinator", lm="2026-05-01T00:00:00Z"),
        ])
        assert resolve_role(idx, center_account_id="ACENTER", contact_id="C1") == "Study Coordinator"

    def test_blank_when_no_roles(self):
        idx = build_acr_index([_acr("ACENTER", "C1", "")])
        assert resolve_role(idx, center_account_id="ACENTER", contact_id="C1") == ""

    def test_blank_when_contact_absent(self):
        idx = build_acr_index([])
        assert resolve_role(idx, center_account_id="ACENTER", contact_id="CX") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_assignment_report.py::TestRoleResolution -v`
Expected: FAIL with `ImportError: cannot import name 'build_acr_index'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to backend/app/services/assignment_report.py

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
    nonempty = [r for r in recs if (r.get("Role__c") or "").strip()]
    if not nonempty:
        return ""
    nonempty.sort(key=lambda r: r.get("LastModifiedDate") or "", reverse=True)
    return nonempty[0]["Role__c"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest backend/tests/test_assignment_report.py::TestRoleResolution -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/assignment_report.py backend/tests/test_assignment_report.py
git commit -m "feat(assignments): ACR index + two-tier role resolver (pure)"
```

---

## Task 3: Row assembler (pure)

**Files:**
- Modify: `backend/app/services/assignment_report.py`
- Test: `backend/tests/test_assignment_report.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_assignment_report.py
from app.services.assignment_report import assemble_rows, REPORT_COLUMNS


def _assignment(con, acc, study="Baricade", stage="Activated", referral=True,
                first="Jane", last="Doe", email="j@x.org",
                city="Liege", country="Belgium", pop="Both"):
    return {
        "C_Assignment_Stage__c": stage, "Referral_Contact__c": referral,
        "C_Account__c": acc, "C_Opportunity_Name__r": {"Name": study},
        "C_Contact_Name__c": con,
        "C_Contact_Name__r": {
            "FirstName": first, "LastName": last, "Email": email,
            "Account": {"Name": "Center X", "ShippingCity": city,
                        "ShippingCountry": country, "Patient_Population__c": pop},
        },
    }


class TestAssembleRows:
    def test_basic_row_shape_and_role(self):
        assignments = [_assignment("C1", "ACENTER")]
        idx = build_acr_index([_acr("ACENTER", "C1", "Investigator")])
        out = assemble_rows(assignments, idx, AssignmentFilters())
        assert [c["key"] for c in out["columns"]] == REPORT_COLUMNS
        row = out["rows"][0]
        assert row["role"] == "Investigator"
        assert row["email"] == "j@x.org"
        assert row["study"] == "Baricade"
        assert row["city"] == "Liege"

    def test_exclude_country_drops_row(self):
        assignments = [_assignment("C1", "ACENTER", country="United Kingdom")]
        out = assemble_rows(assignments, build_acr_index([]), AssignmentFilters(exclude_countries=["United Kingdom"]))
        assert out["rows"] == []

    def test_role_filter_keeps_only_matching(self):
        assignments = [_assignment("C1", "AC1"), _assignment("C2", "AC2", email="b@x.org")]
        idx = build_acr_index([_acr("AC1", "C1", "Investigator"), _acr("AC2", "C2", "Study Nurse")])
        out = assemble_rows(assignments, idx, AssignmentFilters(roles=["Investigator"]))
        assert len(out["rows"]) == 1
        assert out["rows"][0]["role"] == "Investigator"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_assignment_report.py::TestAssembleRows -v`
Expected: FAIL with `ImportError: cannot import name 'assemble_rows'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to backend/app/services/assignment_report.py

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
        country = _nested(a, "C_Contact_Name__r.Account.ShippingCountry") or ""
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
            "center": _nested(a, "C_Contact_Name__r.Account.Name") or "",
            "city": _nested(a, "C_Contact_Name__r.Account.ShippingCity") or "",
            "patient_population": _nested(a, "C_Contact_Name__r.Account.Patient_Population__c") or "",
            "first_name": _nested(a, "C_Contact_Name__r.FirstName") or "",
            "last_name": _nested(a, "C_Contact_Name__r.LastName") or "",
            "email": _nested(a, "C_Contact_Name__r.Email") or "",
            "role": role,
        })
    columns = [{"key": k, "label": _COLUMN_LABELS[k]} for k in REPORT_COLUMNS]
    return {"columns": columns, "rows": rows}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest backend/tests/test_assignment_report.py -v`
Expected: PASS (all tasks 1-3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/assignment_report.py backend/tests/test_assignment_report.py
git commit -m "feat(assignments): row assembler with country + role filtering (pure)"
```

---

## Task 4: fetch_report orchestration (I/O) with a fake SF

**Files:**
- Modify: `backend/app/services/assignment_report.py`
- Test: `backend/tests/test_assignment_report.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_assignment_report.py
from app.services.assignment_report import fetch_report


class _FakeSF:
    """Records SOQL and returns canned {'records': [...]} per call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.queries = []
    def query_all(self, soql):
        self.queries.append(soql)
        return self._responses.pop(0)


class TestFetchReport:
    def test_two_queries_and_join(self):
        assignment_resp = {"records": [_assignment("C1", "ACENTER")]}
        acr_resp = {"records": [_acr("ACENTER", "C1", "Investigator")]}
        sf = _FakeSF([assignment_resp, acr_resp])
        out = fetch_report(sf, AssignmentFilters(studies=["Baricade"], referral_only=True))
        assert len(sf.queries) == 2
        assert "FROM Assignment__c" in sf.queries[0]
        assert "FROM AccountContactRelation" in sf.queries[1]
        assert "ContactId IN ('C1')" in sf.queries[1]
        assert out["rows"][0]["role"] == "Investigator"

    def test_no_assignments_skips_acr_query(self):
        sf = _FakeSF([{"records": []}])
        out = fetch_report(sf, AssignmentFilters())
        assert len(sf.queries) == 1
        assert out["rows"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_assignment_report.py::TestFetchReport -v`
Expected: FAIL with `ImportError: cannot import name 'fetch_report'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to backend/app/services/assignment_report.py

_ACR_CHUNK = 200


def _chunked(seq: List[str], n: int) -> List[List[str]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def fetch_report(sf: Any, f: AssignmentFilters) -> Dict[str, Any]:
    """Run the two-step query (assignments → ACR roles) and assemble rows."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest backend/tests/test_assignment_report.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/assignment_report.py backend/tests/test_assignment_report.py
git commit -m "feat(assignments): fetch_report two-step orchestration"
```

---

## Task 5: Router endpoint + options + register in main

**Files:**
- Create: `backend/app/routers/assignments_report.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write the router**

```python
# backend/app/routers/assignments_report.py
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.routers.salesforce_explorer import _get_sf
from app.services.assignment_report import AssignmentFilters, fetch_report

router = APIRouter(prefix="/api/assignments", tags=["assignments-report"])


class ReportRequest(BaseModel):
    studies: List[str] = []
    stages: List[str] = []
    referral_only: bool = False
    roles: List[str] = []
    exclude_countries: List[str] = []
    include_countries: List[str] = []


@router.post("/report")
def assignments_report(body: ReportRequest, request: Request) -> Dict[str, Any]:
    sf = _get_sf(request)
    f = AssignmentFilters(
        studies=body.studies, stages=body.stages, referral_only=body.referral_only,
        roles=body.roles, exclude_countries=body.exclude_countries,
        include_countries=body.include_countries,
    )
    try:
        return fetch_report(sf, f)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"assignment report failed: {e}")


@router.get("/report/options")
def assignments_report_options(request: Request) -> Dict[str, Any]:
    sf = _get_sf(request)
    studies = sf.query_all(
        "SELECT Name FROM Opportunity WHERE Id IN "
        "(SELECT C_Opportunity_Name__c FROM Assignment__c WHERE C_Contact_Name__c != null) "
        "ORDER BY Name"
    ).get("records", [])
    stages = sf.query_all(
        "SELECT C_Assignment_Stage__c FROM Assignment__c "
        "WHERE C_Assignment_Stage__c != null GROUP BY C_Assignment_Stage__c"
    ).get("records", [])
    return {
        "studies": sorted({r.get("Name") for r in studies if r.get("Name")}),
        "stages": sorted({r.get("C_Assignment_Stage__c") for r in stages if r.get("C_Assignment_Stage__c")}),
    }
```

- [ ] **Step 2: Register the router in main.py**

In `backend/app/main.py`, after the `from app.routers import members_explorer` import line, add:

```python
from app.routers import assignments_report
```

After the `app.include_router(members_explorer.router)` line, add:

```python
app.include_router(assignments_report.router)  # /api/assignments/...
```

- [ ] **Step 3: Verify the app imports cleanly**

Run: `cd backend && python -c "import app.main; print('ok')"`
Expected: prints `ok` (no import errors)

- [ ] **Step 4: Run the full backend suite (no regressions)**

Run: `python -m pytest backend/tests/ -q`
Expected: all green (existing + new assignment tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/assignments_report.py backend/app/main.py
git commit -m "feat(assignments): POST /api/assignments/report endpoint + options"
```

---

## Task 6: Moby tool wrapper + schema

**Files:**
- Modify: `backend/app/routers/moby_tools.py`
- Modify: `backend/app/moby/tools_spec.py`

- [ ] **Step 1: Register the tool handler**

Append to `backend/app/routers/moby_tools.py` (uses the same `@register_tool` pattern as `study_coordinators_with_activities`):

```python
@register_tool("assignment_contact_report")
def handle_assignment_contact_report(ctx: ToolContext) -> ToolResult:
    from app.services.assignment_report import AssignmentFilters, fetch_report
    result = ToolResult()
    a = ctx.tool_args or ctx.args or {}
    f = AssignmentFilters(
        studies=a.get("studies") or [],
        stages=a.get("stages") or [],
        referral_only=bool(a.get("referral_only") or False),
        roles=a.get("roles") or [],
        exclude_countries=a.get("exclude_countries") or [],
        include_countries=a.get("include_countries") or [],
    )
    try:
        result.last_table = fetch_report(ctx.sf, f)
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True, "rows": len(result.last_table["rows"])})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result
```

- [ ] **Step 2: Add the tool JSON schema**

In `backend/app/moby/tools_spec.py`, add this object to the tools list (same shape as the `salesforce_account_extras` entry):

```python
{
    "type": "function",
    "function": {
        "name": "assignment_contact_report",
        "description": "Assignment/contact-level report: referral contacts for given studies and assignment stages, with each contact's Role at the center (AccountContactRelation.Role__c). Use for queries about referral contacts, study coordinators/investigators per study, or 'who is the X at the sites in study Y', especially when a contact-grain table with Role is requested. NOT for account/site-level filtering (use explorer_search for that).",
        "parameters": {
            "type": "object",
            "properties": {
                "studies": {"type": "array", "items": {"type": "string"}, "description": "Opportunity/study names, e.g. Baricade, Safeguard, Beta Preserve"},
                "stages": {"type": "array", "items": {"type": "string"}, "description": "Assignment stages, e.g. Activated"},
                "referral_only": {"type": "boolean", "description": "Only assignments flagged Referral Contact"},
                "roles": {"type": "array", "items": {"type": "string"}, "description": "Filter to these roles, e.g. Investigator, Study Coordinator"},
                "exclude_countries": {"type": "array", "items": {"type": "string"}, "description": "Center countries to exclude, e.g. United Kingdom"},
                "include_countries": {"type": "array", "items": {"type": "string"}}
            },
            "required": []
        }
    }
}
```

- [ ] **Step 3: Verify imports + dispatch registration**

Run:
```bash
cd backend && python -c "from app.routers import moby_tools; print('assignment_contact_report' in moby_tools.TOOL_DISPATCH)"
```
Expected: prints `True`

- [ ] **Step 4: Run backend suite**

Run: `python -m pytest backend/tests/ -q`
Expected: all green

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/moby_tools.py backend/app/moby/tools_spec.py
git commit -m "feat(assignments): Moby tool assignment_contact_report + schema"
```

---

## Task 7: Frontend API call

**Files:**
- Modify: `frontend/src/lib/api.ts`

- [ ] **Step 1: Add the call function**

Append to `frontend/src/lib/api.ts` (mirrors the `explorerSearch` pattern):

```typescript
export type AssignmentReportFilters = {
  studies?: string[];
  stages?: string[];
  referral_only?: boolean;
  roles?: string[];
  exclude_countries?: string[];
  include_countries?: string[];
};

export type ReportTable = {
  columns: { key: string; label?: string }[];
  rows: Array<Record<string, any>>;
};

export async function assignmentReport(filters: AssignmentReportFilters): Promise<ReportTable> {
  return api<ReportTable>("/api/assignments/report", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(filters),
    timeoutMs: 90_000,
  });
}

export async function assignmentReportOptions(): Promise<{ studies: string[]; stages: string[] }> {
  return api<{ studies: string[]; stages: string[] }>("/api/assignments/report/options", { retries: 1 });
}
```

- [ ] **Step 2: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors referencing api.ts

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(assignments): frontend api client for report endpoint"
```

---

## Task 8: New tab — AssignmentsView + wiring

**Files:**
- Create: `frontend/src/pages/AssignmentsView.tsx`
- Modify: `frontend/src/types.ts`, `frontend/src/App.tsx`, `frontend/src/components/Header.tsx`

- [ ] **Step 1: Extend the Tab union**

In `frontend/src/types.ts`, change:

```typescript
export type Tab = "upload" | "explorer" | "members" | "chat";
```
to:
```typescript
export type Tab = "upload" | "explorer" | "members" | "chat" | "assignments";
```

- [ ] **Step 2: Create the view**

```tsx
// frontend/src/pages/AssignmentsView.tsx
import React, { useEffect, useState } from "react";
import { assignmentReport, assignmentReportOptions, ReportTable } from "../lib/api";

const STUDIES_DEFAULT = ["Baricade", "Safeguard", "Beta Preserve"];

function toCsv(t: ReportTable): string {
  const head = t.columns.map((c) => c.label || c.key).join(",");
  const esc = (v: any) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const body = t.rows.map((r) => t.columns.map((c) => esc(r[c.key])).join(",")).join("\n");
  return head + "\n" + body;
}

export default function AssignmentsView() {
  const [studies, setStudies] = useState<string[]>([]);
  const [stages, setStages] = useState<string[]>([]);
  const [selStudies, setSelStudies] = useState<string[]>(STUDIES_DEFAULT);
  const [selStages, setSelStages] = useState<string[]>(["Activated"]);
  const [referralOnly, setReferralOnly] = useState(true);
  const [excludeUK, setExcludeUK] = useState(true);
  const [table, setTable] = useState<ReportTable | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    assignmentReportOptions()
      .then((o) => { setStudies(o.studies); setStages(o.stages); })
      .catch(() => { /* options are best-effort; defaults still work */ });
  }, []);

  const run = async () => {
    setLoading(true); setError(null);
    try {
      const t = await assignmentReport({
        studies: selStudies,
        stages: selStages,
        referral_only: referralOnly,
        exclude_countries: excludeUK ? ["United Kingdom"] : [],
      });
      setTable(t);
    } catch (e: any) {
      setError(e?.message || "Failed to load report");
    } finally {
      setLoading(false);
    }
  };

  const exportCsv = () => {
    if (!table) return;
    const blob = new Blob([toCsv(table)], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "referral_contacts_with_role.csv"; a.click();
    URL.revokeObjectURL(url);
  };

  const toggle = (arr: string[], v: string, set: (x: string[]) => void) =>
    set(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);

  return (
    <div className="p-4" data-testid="assignments-view">
      <h2 className="text-lg font-semibold mb-3">Referral / Assignment Contacts (with Role)</h2>

      <div className="flex flex-wrap gap-4 items-start mb-4">
        <fieldset className="border rounded p-2">
          <legend className="text-xs px-1">Studies</legend>
          {(studies.length ? studies : STUDIES_DEFAULT).map((s) => (
            <label key={s} className="block text-sm">
              <input type="checkbox" checked={selStudies.includes(s)}
                     onChange={() => toggle(selStudies, s, setSelStudies)} /> {s}
            </label>
          ))}
        </fieldset>

        <fieldset className="border rounded p-2">
          <legend className="text-xs px-1">Stages</legend>
          {(stages.length ? stages : ["Activated"]).map((s) => (
            <label key={s} className="block text-sm">
              <input type="checkbox" checked={selStages.includes(s)}
                     onChange={() => toggle(selStages, s, setSelStages)} /> {s}
            </label>
          ))}
        </fieldset>

        <div className="flex flex-col gap-1 text-sm">
          <label><input type="checkbox" checked={referralOnly}
                        onChange={(e) => setReferralOnly(e.target.checked)} /> Referral contacts only</label>
          <label><input type="checkbox" checked={excludeUK}
                        onChange={(e) => setExcludeUK(e.target.checked)} /> Exclude United Kingdom</label>
        </div>

        <div className="flex flex-col gap-2">
          <button data-testid="assignments-run" onClick={run} disabled={loading}
                  className="px-3 py-1.5 rounded bg-[#003f7d] text-white text-sm disabled:opacity-50">
            {loading ? "Loading…" : "Run report"}
          </button>
          <button data-testid="assignments-export" onClick={exportCsv} disabled={!table}
                  className="px-3 py-1.5 rounded border text-sm disabled:opacity-50">Export CSV</button>
        </div>
      </div>

      {error && <div className="text-red-600 text-sm mb-2">{error}</div>}

      {table && (
        <div className="overflow-auto border rounded">
          <table className="min-w-full text-sm" data-testid="assignments-table">
            <thead className="bg-gray-100">
              <tr>{table.columns.map((c) => <th key={c.key} className="text-left px-2 py-1">{c.label || c.key}</th>)}</tr>
            </thead>
            <tbody>
              {table.rows.map((r, i) => (
                <tr key={i} className="border-t">
                  {table.columns.map((c) => (
                    <td key={c.key} className="px-2 py-1">
                      {typeof r[c.key] === "boolean" ? (r[c.key] ? "✓" : "") : String(r[c.key] ?? "")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="text-xs text-gray-500 px-2 py-1">{table.rows.length} rows</div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Route the tab in App.tsx**

In `frontend/src/App.tsx`, add the import near the other page imports:

```typescript
import AssignmentsView from "./pages/AssignmentsView";
```

Add this line in the tab-render block (after the `chat` line):

```typescript
{tab === "assignments" && <AssignmentsView />}
```

- [ ] **Step 4: Add the tab button in Header.tsx**

In `frontend/src/components/Header.tsx`, add this button inside the `<nav>` after the chat button:

```tsx
<button
  data-testid="tab-assignments"
  className={`px-3 py-1.5 rounded-full text-sm transition ${
    active === "assignments" ? "bg-white text-[#003f7d] shadow" : "hover:bg-white/20"
  }`}
  onClick={() => onTab("assignments")}
>
  Referral DB
</button>
```

- [ ] **Step 5: Type-check + build**

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: type-check clean, build succeeds

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/AssignmentsView.tsx frontend/src/types.ts frontend/src/App.tsx frontend/src/components/Header.tsx
git commit -m "feat(assignments): Referral DB tab (filters + table + CSV export)"
```

---

## Task 9: Frontend E2E (mocked)

**Files:**
- Create: `frontend/tests/e2e/assignments.spec.ts`

- [ ] **Step 1: Write the E2E test**

```typescript
// frontend/tests/e2e/assignments.spec.ts
import { test, expect } from "@playwright/test";

test("Referral DB tab runs report and renders table", async ({ page }) => {
  await page.route("**/api/salesforce/me", (r) =>
    r.fulfill({ json: { authenticated: true, instance_url: "x", issued_at: 1, has_refresh: true } }));
  await page.route("**/api/assignments/report/options", (r) =>
    r.fulfill({ json: { studies: ["Baricade", "Safeguard", "Beta Preserve"], stages: ["Activated"] } }));
  await page.route("**/api/assignments/report", (r) =>
    r.fulfill({ json: {
      columns: [{ key: "first_name", label: "First Name" }, { key: "email", label: "Email" }, { key: "role", label: "Role" }],
      rows: [{ first_name: "Bart", email: "bart@uzbrussel.be", role: "Investigator" }],
    }}));

  await page.goto("/");
  await page.getByTestId("tab-assignments").click();
  await expect(page.getByTestId("assignments-view")).toBeVisible();
  await page.getByTestId("assignments-run").click();
  await expect(page.getByTestId("assignments-table")).toContainText("Investigator");
  await expect(page.getByTestId("assignments-table")).toContainText("bart@uzbrussel.be");
});
```

- [ ] **Step 2: Run the E2E test**

Run: `cd frontend && npm run test:e2e -- assignments.spec.ts`
Expected: 1 passed

- [ ] **Step 3: Commit**

```bash
git add frontend/tests/e2e/assignments.spec.ts
git commit -m "test(assignments): E2E for Referral DB tab"
```

---

## Task 10: Live integration vs ground truth (needs SF cookie)

**Files:**
- Create: `scripts/test_assignment_report_integration.py`

- [ ] **Step 1: Write the integration script**

```python
# scripts/test_assignment_report_integration.py
"""Live check: the assignment report reproduces the referral ground truth.
Run: SF_SESSION_COOKIE="<sf_session>" API_BASE="https://cts-innodia-dashboard.org" \
     python scripts/test_assignment_report_integration.py
"""
import json, os, sys, urllib.request

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
COOKIE = os.getenv("SF_SESSION_COOKIE", "")
GT = json.load(open(os.path.join(os.path.dirname(__file__), "..",
               "backend", "tests", "fixtures", "referral_groundtruth.json")))


def post(path, body):
    req = urllib.request.Request(API_BASE + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Cookie": f"sf_session={COOKIE}"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    if not COOKIE:
        print("ERROR: set SF_SESSION_COOKIE"); sys.exit(1)
    out = post("/api/assignments/report", {
        "studies": ["Baricade", "Safeguard", "Beta Preserve"],
        "stages": ["Activated"], "referral_only": True,
        "exclude_countries": ["United Kingdom"],
    })
    got_emails = {(r.get("email") or "").lower() for r in out["rows"]}
    want_emails = {e.lower() for e in GT["unique_emails"]}
    missing = want_emails - got_emails
    extra = got_emails - want_emails
    with_role = sum(1 for r in out["rows"] if (r.get("role") or "").strip())
    print(f"rows={len(out['rows'])} unique_emails={len(got_emails)} "
          f"with_role={with_role} missing={len(missing)} extra={len(extra)}")
    if missing:
        print("MISSING:", sorted(missing)[:10])
    assert not missing, f"{len(missing)} ground-truth contacts missing"
    print("OK: all 55 ground-truth contacts present")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it (manual, requires a fresh SF cookie + running backend)**

Run (local backend or prod):
```bash
SF_SESSION_COOKIE="<sf_session>" API_BASE="https://cts-innodia-dashboard.org" \
  python scripts/test_assignment_report_integration.py
```
Expected: `OK: all 55 ground-truth contacts present`, `with_role` ≈ 53.

If `missing` is non-empty, the Role/center join or a filter mapping needs adjusting — inspect the
missing contacts' assignments and ACRs before changing the SOQL (this is the join-risk flagged in the spec).

- [ ] **Step 3: Commit**

```bash
git add scripts/test_assignment_report_integration.py
git commit -m "test(assignments): live integration check vs referral ground truth"
```

---

## Scope notes (intentional)

- **Role as a filter** is supported by the backend (`AssignmentFilters.roles`) and the Moby tool,
  but the **UI exposes Role as a display column only** in this iteration (Hasna's case needs the
  column, not a role picker). Adding a Role filter control is a small follow-up: extend
  `GET /report/options` to return distinct roles and add a checkbox group in `AssignmentsView`.
- `fetch_report` uses `sf.query_all` (paginated) intentionally — ACR result sets can exceed the
  2000-row single-page cap of `sf.query`.

## Final verification

- [ ] `python -m pytest backend/tests/ -q` — all green.
- [ ] `cd frontend && npx tsc --noEmit && npm run build` — clean.
- [ ] `cd frontend && npm run test:e2e -- assignments.spec.ts` — passes.
- [ ] Live integration script reproduces the 55 ground-truth contacts.
- [ ] Do NOT deploy to ECS — deployment is manual and only on Juan's explicit authorization.
