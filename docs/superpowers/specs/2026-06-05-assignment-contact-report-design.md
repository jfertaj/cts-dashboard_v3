# Assignment & Contact Report (referral contacts + Role) — Design

**Date:** 2026-06-05
**Status:** Approved (design), pending implementation plan
**Author:** Juan + Claude

## Problem

Hasna asked to add a **Role** column (Investigator / Study Coordinator / Study Nurse…)
to the Salesforce report *"myINNODIA Referral Database v3"* (`00OVg00000Ng4d4MAB`).

That report's type is `Opportunities → Assignments → Contacts`. The Role she wants is a
custom field **`AccountContactRelation.Role__c`** — it lives on the Account↔Contact junction,
which is **not part of that report type**. Native Salesforce reports cannot add a column from
an object outside their report type, so the column is impossible to add natively.

The CTS Dashboard, however, queries Salesforce via SOQL/REST (not report types), so it **can**
join `Assignment__c` → `Contact` → `AccountContactRelation.Role__c` in one place. A direct data
join over the 55 ground-truth contacts confirmed **53/55 have a Role populated** (the 2 blanks are
unpopulated SF data, not a system limit).

## Scope & guiding principle

This is **not** "Hasna's report hardcoded" and **not** "a second Explorer". It is the
**Assignment/Contact-centric complement** to the Explorer (which is Account/site-centric).

Its reason to exist is exactly what the Explorer cannot do: filter on `Assignment__c`
dimensions and drill to **contact grain** with the contact's **Role at the center**.
Hasna's report is simply *one instance* of this tool.

**Grain:** one row per assignment-contact.

**Parameterizable filter dimensions (the ones the Explorer does not cover):**
1. Study / Opportunity (`Assignment__c.C_Opportunity_Name__r.Name`) — multi-select
2. Assignment Stage (`C_Assignment_Stage__c`) — multi-select
3. Referral Contact (`Referral_Contact__c`) — yes/no
4. Role of the contact (`AccountContactRelation.Role__c`) — filter/display

Deliberately **out of scope**: re-implementing Account-level filtering that the Explorer already
does. A light country include/exclude (on the center's country) is included only to reproduce the
report's "exclude UK" criterion 1:1.

## Confirmed Salesforce join keys (`Assignment__c`)

| Field | Type | Role in the join |
|---|---|---|
| `C_Opportunity_Name__c` | Master-Detail(Opportunity) | the study (Baricade / Safeguard / Beta Preserve…) |
| `C_Contact_Name__c` | Lookup(Contact) | the contact |
| `C_Account__c` | Lookup(Account) | the center |
| `C_Assignment_Stage__c` | Picklist | Activated / Selected / Closed… |
| `Referral_Contact__c` | Checkbox | the report's defining flag |

**Role:** `AccountContactRelation` where `AccountId = Assignment.C_Account__c` and
`ContactId = Assignment.C_Contact_Name__c`, field `Role__c`.

## Architecture (Option A)

A single deterministic backend service feeds two surfaces:

```
                     ┌─ POST /api/assignments/report ──→ AssignmentsView.tsx (new tab)
build_assignment_report(filters) ─┤
   (pure functions + SF I/O)       └─ Moby tool "assignment_contact_report" (same service)
```

Isolated from the Explorer. Same logic, two surfaces — no duplicated business logic.

## Components & boundaries (built for testability)

- **`app/services/assignment_report.py`** — the core, with pure functions separated from I/O:
  - `build_assignment_soql(filters) -> str` — pure; unit-testable without network; escapes /
    allowlists all filter values (no SOQL injection).
  - `assemble_rows(assignment_records, acr_role_index) -> list[dict]` — pure; performs the
    contact+center → Role join and shapes the output rows/columns.
  - `fetch_report(sf, filters) -> {rows, columns}` — I/O orchestration: SOQL on `Assignment__c`
    → collect `(contactId, accountId)` pairs → batched SOQL on `AccountContactRelation` for
    `Role__c` → `assemble_rows`.
- **`app/routers/assignments_report.py`** — thin router:
  - `POST /api/assignments/report` — auth via SF session cookie; calls `fetch_report`.
  - `GET /api/assignments/report/options` — distinct studies + stages to populate the dropdowns.
- **`app/routers/moby_tools.py`** — register tool `assignment_contact_report` that calls the same
  `fetch_report`. Add an intent hint so the agentic loop invokes it for "referral contacts + role".
- **`frontend/src/pages/AssignmentsView.tsx`** — new tab: 4 filter controls (Study multi,
  Stage multi, Referral yes/no, Role multi) + results table (reuse existing table + CSV export
  component) + "Open in…" affordances consistent with the app. Register the tab in `App.tsx`
  (`Tab` union) and `Header.tsx`.

## Data flow & default columns

```
filters
  → SOQL Assignment__c (C_Opportunity_Name__r.Name,
       C_Contact_Name__r.{FirstName,LastName,Email,Account.Patient_Population__c, Account.ShippingCity, Account.ShippingCountry},
       C_Account__c, C_Assignment_Stage__c, Referral_Contact__c)
  → join Role__c via ACR(AccountId=C_Account__c, ContactId=C_Contact_Name__c)
  → rows
```

**Default columns (reproduce the report + Role):**
Study · Stage · Referral · Center · City · Patient Population · First Name · Last Name · Email · **Role**.

A light country include/exclude filter (on the center's country) reproduces the report's
"exclude United Kingdom" criterion exactly.

## Error handling & risks

- **Expired SF session →** 401, consistent with the rest of the app. Empty results → empty table,
  not an error.
- **SOQL injection:** all filter values escaped / allowlisted. The repo builds SOQL with f-strings;
  this module hardens that for its own inputs.
- **SOQL limits:** studies via `IN`; ACR fetched in chunked `IN` batches.
- **Join risk to confirm in planning:** `Role__c` is populated on the **indirect** ACR (center
  `_CS-…`, `IsDirect=false`). Planning must confirm `C_Account__c` points to that center account.
  If it points to the parent account instead, resolve by falling back to the contact's ACR keyed by
  `ContactId` (the approach that yielded 53/55 in the validation probe).

## Testing

- **Unit (no network):** `build_assignment_soql` (filter combinations, escaping) and
  `assemble_rows` (correct join, empty Role → blank cell, a contact with multiple roles
  disambiguated by center account).
- **Regression vs. ground truth:** the real report was already extracted —
  **71 rows / 55 unique contacts** (saved at `/tmp/moby_groundtruth.json`; to be promoted to a repo
  test fixture during implementation). Integration test asserts the tool reproduces those 71
  assignment-contacts and the correct Role (53/55 populated).
- **Frontend E2E:** mock `/api/assignments/report`, assert table render + CSV export.

## Out of scope (YAGNI)

- No Account-level filter machinery (that's the Explorer's job).
- No write-back to Salesforce; read-only.
- No new fields/automation in Salesforce — the Role already exists on `AccountContactRelation`.
