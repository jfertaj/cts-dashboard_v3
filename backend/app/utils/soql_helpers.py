#backend/app/utils/soql_helpers.py

from typing import Iterable


def soql_in_ids(field: str, ids: Iterable[str]) -> str:
    """
    Construye un IN (...) seguro. Si la lista viene vacía, nunca rompe.
    """
    ids = [i for i in ids if i and isinstance(i, str)]
    if not ids:
        return f"{field} IN ('__EMPTY__')"
    safe = ",".join([f"'{i}'" for i in ids])
    return f"{field} IN ({safe})"


def build_followup_accounts_query(account_ids: list[str]) -> str:
    """
    Devuelve el SOQL para traer pacientes actuales (U18/O18) de Account,
    con filtros correctamente parentetizados.
    """
    where_ids = soql_in_ids("Id", account_ids)
    return (
        "SELECT Id, Name, BillingCountry, BillingCity, "
        "C_Number_of_T1D_Patients_currently_U_18__c, "
        "C_Number_of_T1D_Patients_currently_O_18__c "
        "FROM Account "
        f"WHERE {where_ids} "
        "AND (C_Number_of_T1D_Patients_currently_U_18__c != NULL "
        "OR C_Number_of_T1D_Patients_currently_O_18__c != NULL) "
        "ORDER BY Name"
    )