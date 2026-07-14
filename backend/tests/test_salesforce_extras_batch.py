"""Tests de batch_fetch_account_extras() — el módulo que alimenta /explorer/search y /explorer/fill-columns.

Regla central: "no tiene ninguno" (0) NO es lo mismo que "no lo sabemos" (ausente).
Un COUNT() de registros relacionados es un valor CONOCIDO cuando vale cero.
"""
from typing import Any, Dict, List

import pytest

from app.routers.filter_engine import _eval_extra_rule
from app.routers.salesforce_extras_batch import batch_fetch_account_extras


class FakeSF:
    """simple-salesforce mínimo: responde por forma del SOQL, cuenta las queries."""

    def __init__(self, accounts: List[str], assignments: Dict[str, List[str]]):
        self.accounts = accounts
        self.assignments = assignments  # account_id -> [nombres de Assignment__c]
        self.queries: List[str] = []

    def _records(self, soql: str) -> List[Dict[str, Any]]:
        self.queries.append(soql)
        asked = [a for a in self.accounts if f"'{a}'" in soql]

        if "FROM Account " in soql:
            return [{"Id": a, "C_Member__r": None, "Clinical_Site_CS__c": False} for a in asked]
        if "FROM AccountContactRelation" in soql:
            return []
        if "COUNT(Id) cnt" in soql:
            return [
                {"C_Account__c": a, "cnt": len(self.assignments[a])}
                for a in asked
                if self.assignments.get(a)
            ]
        if "FROM Assignment__c" in soql:
            return [
                {"C_Account__c": a, "Name": n, "C_Opportunity_Name__r": None}
                for a in asked
                for n in self.assignments.get(a, [])
            ]
        return []

    def query(self, soql: str) -> Dict[str, Any]:
        return {"records": self._records(soql)}

    def query_all(self, soql: str) -> Dict[str, Any]:
        return {"records": self._records(soql)}


ACC_WITH = "001AAA"       # 2 assignments
ACC_WITHOUT = "001BBB"    # 0 assignments
ACC_UNQUERIED = "001ZZZ"  # nunca lo preguntamos


@pytest.fixture
def sf() -> FakeSF:
    return FakeSF(
        accounts=[ACC_WITH, ACC_WITHOUT, ACC_UNQUERIED],
        assignments={ACC_WITH: ["ASG-1", "ASG-2"], ACC_WITHOUT: [], ACC_UNQUERIED: []},
    )


def test_account_without_assignments_gets_zero_not_missing_key(sf: FakeSF):
    out = batch_fetch_account_extras(sf, [ACC_WITH, ACC_WITHOUT])

    assert "extra.AssignmentsCount" in out[ACC_WITHOUT], "la clave debe existir, no faltar"
    assert out[ACC_WITHOUT]["extra.AssignmentsCount"] == 0


def test_account_with_assignments_keeps_its_real_count(sf: FakeSF):
    out = batch_fetch_account_extras(sf, [ACC_WITH, ACC_WITHOUT])

    assert out[ACC_WITH]["extra.AssignmentsCount"] == 2


def test_only_accounts_we_asked_about_are_defaulted(sf: FakeSF):
    out = batch_fetch_account_extras(sf, [ACC_WITHOUT])

    assert ACC_UNQUERIED not in out, "una cuenta que no preguntamos no se puede rellenar con 0"


def test_no_accounts_means_no_queries(sf: FakeSF):
    assert batch_fetch_account_extras(sf, []) == {}
    assert sf.queries == []


# --- El 0 tiene que seguir significando "no está en ningún assignment" para los filtros ---
# Moby traduce "sites not in any assignment" a {field: extra.AssignmentsCount, operator: is_empty}
# (backend/app/moby/prompt.py). Si el backend pasa a mandar 0, is_empty debe seguir casando.

@pytest.mark.parametrize("actual", [None, 0])
def test_is_empty_on_assignments_count_matches_zero_and_absent(actual):
    assert _eval_extra_rule("extra.AssignmentsCount", actual, "is_empty", None) is True


@pytest.mark.parametrize("op", ["is_empty", "is_null"])
def test_is_empty_synonyms_on_assignments_count(op):
    assert _eval_extra_rule("extra.AssignmentsCount", 0, op, None) is True
    assert _eval_extra_rule("extra.AssignmentsCount", 3, op, None) is False


def test_is_not_empty_on_assignments_count_needs_a_real_assignment():
    assert _eval_extra_rule("extra.AssignmentsCount", 3, "is_not_empty", None) is True
    assert _eval_extra_rule("extra.AssignmentsCount", 0, "is_not_empty", None) is False
    assert _eval_extra_rule("extra.AssignmentsCount", None, "is_not_empty", None) is False


def test_zero_stays_a_value_for_every_other_extra_field():
    # Un 0 en un campo que NO es un conteo de registros relacionados sigue siendo un valor:
    # is_empty sólo debe casar con ausencia real.
    assert _eval_extra_rule("extra.SomeNumber", 0, "is_empty", None) is False
    assert _eval_extra_rule("extra.SomeNumber", None, "is_empty", None) is True


def test_non_emptiness_rules_still_delegate_to_the_generic_evaluator():
    assert _eval_extra_rule("extra.AssignmentsCount", 3, ">", 2) is True
    assert _eval_extra_rule("extra.AssignmentsCount", 0, ">", 2) is False
    assert _eval_extra_rule("extra.AssignmentsNames", "ASG-1; ASG-2", "contains", "ASG-2") is True
