from typing import Any, Dict, List
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
