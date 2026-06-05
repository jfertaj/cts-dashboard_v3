"""Assignment/Contact-centric report service.

Pure helpers (SOQL building, ACR indexing, role resolution, row assembly) are
separated from Salesforce I/O so they unit-test without a network. See spec:
docs/superpowers/specs/2026-06-05-assignment-contact-report-design.md
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List

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
        "ORDER BY C_Contact_Name__r.Account.ShippingCountry, "
        "C_Opportunity_Name__r.Name, C_Contact_Name__r.LastName"
    )
