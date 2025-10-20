# app/salesforce.py
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
import os
import httpx
from urllib.parse import urlencode
from app.database import SessionLocal
from app.models import SalesforceToken, GeocodedAddress, OpportunityGeolocation
from pydantic import BaseModel
from typing import List, Literal, Union
from dotenv import load_dotenv
import asyncio
import pprint
from simple_salesforce import Salesforce
from pathlib import Path
import json
from app.models import SalesforceToken, AccountGeolocation
from app.settings import settings
from app.geo.google_geocoding import geocode_with_fallback


async def get_profiling_opportunities_from_salesforce(db: Session):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()

    if not token:
        raise HTTPException(status_code=403, detail="No Salesforce token found")

    base_filter = "Type = 'CTS Profiling' AND StageName != 'Closed Lost'"
    soql = f"""
        SELECT Id, Name, StageName, CloseDate, AccountId
        FROM Opportunity
        WHERE {base_filter}
    """

    url = f"{token.instance_url}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {token.access_token}"}

    print(f"📄 Ejecutando SOQL: {soql}")

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers, params={"q": soql})

    if res.status_code != 200:
        raise HTTPException(status_code=500, detail="Error fetching opportunities from Salesforce")

    records = res.json().get("records", [])

    print(f"📊 Oportunidades recibidas: {len(records)}")

    return [
        {
            "id": r["Id"],
            "name": r["Name"],
            "stage": r["StageName"],
            "close_date": r.get("CloseDate"),
            "account_id": r["AccountId"],
        }
        for r in records if r.get("AccountId")
    ]

def get_salesforce_session() -> Salesforce:
    db = SessionLocal()
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    db.close()

    if not token:
        raise Exception("❌ No Salesforce token found")

    return Salesforce(
        instance_url=token.instance_url,
        session_id=token.access_token
    )

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

load_dotenv()

router = APIRouter()
logger = logging.getLogger(__name__)


SF_CLIENT_ID = os.getenv("SF_CLIENT_ID")
SF_CLIENT_SECRET = os.getenv("SF_CLIENT_SECRET")
SF_REDIRECT_URI = os.getenv("SF_REDIRECT_URI")
SF_AUTH_URL = os.getenv("SF_AUTH_URL")
SF_TOKEN_URL = os.getenv("SF_TOKEN_URL")

@router.get("/sf/login")
def salesforce_login(db: Session = Depends(get_db)):
    from httpx import get

    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if token:
        # Verificar si el token sigue siendo válido
        headers = {"Authorization": f"Bearer {token.access_token}"}
        test_url = f"{token.instance_url}/services/oauth2/userinfo"

        try:
            response = get(test_url, headers=headers, timeout=5)
            if response.status_code in [401, 403]:
                print("⚠️ Salesforce token expired. Deleting it before redirecting to login...")
                db.delete(token)
                db.commit()
        except Exception as e:
            print(f"❌ Exception during token check: {e}")
            db.delete(token)
            db.commit()

    # 🚀 Redirigir al login OAuth2 de Salesforce
    params = {
        "response_type": "code",
        "client_id": SF_CLIENT_ID,
        "redirect_uri": SF_REDIRECT_URI,
    }
    redirect_url = f"{SF_AUTH_URL}?{urlencode(params)}"
    print(f"🔁 Redirecting to Salesforce login: {redirect_url}")
    return RedirectResponse(redirect_url)

@router.get("/sf/me")
async def get_salesforce_user_info(db: Session = Depends(get_db)):
    # Obtener el último token
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        raise HTTPException(status_code=403, detail="No Salesforce token found")

    headers = {
        "Authorization": f"Bearer {token.access_token}"
    }
    userinfo_url = f"{token.instance_url}/services/oauth2/userinfo"

    async with httpx.AsyncClient() as client:
        response = await client.get(userinfo_url, headers=headers)

    if response.status_code in [401, 403]:
        print(f"⚠️ Token might be expired (HTTP {response.status_code})")
        # Opcional: eliminar el token automáticamente si se desea
        db.delete(token)
        db.commit()
        raise HTTPException(
            status_code=403,
            detail="Your Salesforce session has expired. Please log in again to renew your access."
        )

    if response.status_code != 200:
        print(f"❌ Unexpected error from Salesforce (HTTP {response.status_code})")
        print(f"📄 Response: {response.text}")
        raise HTTPException(status_code=response.status_code, detail="Failed to fetch Salesforce user info")

    try:
        return response.json()
    except Exception as e:
        print(f"❌ Error parsing JSON from Salesforce: {e}")
        raise HTTPException(status_code=500, detail="Salesforce returned an invalid response.")


@router.get("/sf/callback")
async def salesforce_callback(request: Request, db: Session = Depends(get_db)):
    code = request.query_params.get("code")
    if not code:
        print("❌ Missing 'code' in Salesforce callback.")
        return JSONResponse({"error": "Missing code from Salesforce"}, status_code=400)

    print(f"🔁 Received code from Salesforce: {code}")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": SF_CLIENT_ID,
        "client_secret": SF_CLIENT_SECRET,
        "redirect_uri": SF_REDIRECT_URI
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(SF_TOKEN_URL, data=data)
        token_data = response.json()

    if "access_token" not in token_data:
        print("❌ Failed to get access token. Response:")
        print(token_data)
        return JSONResponse({"error": "Failed to get token", "details": token_data}, status_code=400)

    print("✅ Access token received. Saving to DB...")
    token = SalesforceToken(
        access_token=token_data["access_token"],
        instance_url=token_data["instance_url"],
        user_id=token_data.get("id", "unknown")
    )
    db.add(token)
    db.commit()
    db.refresh(token)

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:4173")
    redirect_to = f"{frontend_url}/salesforce-explorer"
    print(f"🔁 Redirecting to: {redirect_to}")
    return RedirectResponse(url=redirect_to)

# ------------------------------
# 🧠 Query Opportunities endpoint
# ------------------------------

async def geocode_address(address: str):
    import urllib.parse
    url = f"https://nominatim.openstreetmap.org/search?format=json&q={urllib.parse.quote(address)}"
    headers = {"User-Agent": "cts-dashboard/1.0"}

    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, headers=headers, timeout=10)
            res.raise_for_status()
            data = res.json()
            if data:
                return float(data[0]['lat']), float(data[0]['lon'])
        except Exception as e:
            print(f"❌ Error geocoding {address}: {e}")
            return None


async def geocode_address_cached(address: str, db: Session):
    from app.models import GeocodedAddress
    import urllib.parse

    # Buscar en la caché
    cached = db.query(GeocodedAddress).filter_by(address=address).first()
    if cached:
        return cached.latitude, cached.longitude

    # Llamar a Nominatim si no está en la caché
    url = f"https://nominatim.openstreetmap.org/search?format=json&q={urllib.parse.quote(address)}"
    headers = {"User-Agent": "cts-dashboard/1.0"}

    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, headers=headers, timeout=10)
            res.raise_for_status()
            data = res.json()
            if data:
                lat = float(data[0]['lat'])
                lon = float(data[0]['lon'])
                # Guardar en caché
                db.add(GeocodedAddress(address=address, latitude=lat, longitude=lon))
                db.commit()
                return lat, lon
        except Exception as e:
            print(f"❌ Error geocoding {address}: {e}")
            return None


class Rule(BaseModel):
    field: str
    operator: str
    value: Union[str, int, List[Union[str, int]]]


class FilterQuery(BaseModel):
    logic: Literal["AND", "OR"]
    rules: List[Rule]
    columns: List[str]

@router.post("/api/salesforce/cts-profiling-opportunities")
async def cts_profiling_opportunities(query: FilterQuery, db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    base_filters = [
        "StageName != 'Closed Lost'",
        "Type = 'CTS Profiling'"
    ]

    where_clauses = []

    for rule in query.rules:
        f, op, v = rule.field, rule.operator, rule.value

        if isinstance(v, list) and op == "between":
            where_clauses.append(f"{f} >= {v[0]} AND {f} <= {v[1]}")
        elif isinstance(v, str):
            if op == "contains":
                where_clauses.append(f"{f} LIKE '%{v}%'")
            elif op == "not_contains":
                where_clauses.append(f"NOT {f} LIKE '%{v}%'")
            elif op == "starts_with":
                where_clauses.append(f"{f} LIKE '{v}%'")
            elif op == "ends_with":
                where_clauses.append(f"{f} LIKE '%{v}'")
            elif op == "equals":
                where_clauses.append(f"{f} = '{v}'")
            elif op == "not_equals":
                where_clauses.append(f"{f} != '{v}'")
        elif isinstance(v, (int, float)):
            where_clauses.append(f"{f} {op} {v}")

    all_clauses = base_filters + ([f"({query.logic.join(where_clauses)})"] if where_clauses else [])
    soql_where = " AND ".join(all_clauses)

    selected_fields = list(set(query.columns + ["Id", "Name", "StageName", "Type", "Account.Name", 
                                                 "Account.ShippingStreet", "Account.ShippingCity", 
                                                 "Account.ShippingPostalCode", "Account.ShippingCountry"]))

    soql = f"""
        SELECT {', '.join(selected_fields)}
        FROM Opportunity
        WHERE {soql_where}
    """

    url = f"{token.instance_url}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {token.access_token}"}
    params = {"q": soql}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers, params=params)

    if res.status_code != 200:
        return JSONResponse({
            "error": "Salesforce query failed",
            "details": res.json()
        }, status_code=res.status_code)

    records = res.json().get("records", [])
    mapped = []

    for r in records:
        account = r.get("Account", {})
        address_parts = [account.get("ShippingStreet"), account.get("ShippingCity"),
                         account.get("ShippingPostalCode"), account.get("ShippingCountry")]
        address = ", ".join(p for p in address_parts if p)
        maps_url = f"https://www.google.com/maps?q={address.replace(' ', '+')}" if address else None

        mapped.append({
            "opportunity_id": r.get("Id"),
            "name": r.get("Name"),
            "stage": r.get("StageName"),
            "type": r.get("Type"),
            "account": account.get("Name"),
            "address": address,
            "google_maps_url": maps_url,
            **{f: r.get(f) for f in query.columns if f not in ["Id", "Name", "StageName", "Type"]}
        })

    return mapped

@router.post("/api/salesforce/filter-opportunities/")
async def filter_opportunities(query: FilterQuery, db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    logic = query.logic
    where_clauses = []

    for rule in query.rules:
        f, op, v = rule.field, rule.operator, rule.value
        if isinstance(v, list) and op == "between":
            where_clauses.append(f"{f} >= {v[0]} AND {f} <= {v[1]}")
        elif isinstance(v, str):
            if op == "contains":
                where_clauses.append(f"{f} LIKE '%{v}%'")
            elif op == "not_contains":
                where_clauses.append(f"NOT {f} LIKE '%{v}%'")
            elif op == "starts_with":
                where_clauses.append(f"{f} LIKE '{v}%'")
            elif op == "ends_with":
                where_clauses.append(f"{f} LIKE '%{v}'")
            elif op == "equals":
                where_clauses.append(f"{f} = '{v}'")
            elif op == "not_equals":
                where_clauses.append(f"{f} != '{v}'")
        elif isinstance(v, (int, float)):
            where_clauses.append(f"{f} {op} {v}")

    where_clause = f" {logic} ".join(where_clauses)
    fields = ", ".join(set(query.columns + [r.field for r in query.rules]))
    soql = f"SELECT {fields} FROM Opportunity"
    if where_clause:
        soql += f" WHERE {where_clause}"

    url = f"{token.instance_url}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {token.access_token}"}
    params = {"q": soql}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers, params=params)

    if res.status_code != 200:
        return JSONResponse({"error": "Salesforce query failed", "details": res.json()}, status_code=res.status_code)

    return res.json()["records"]

from fastapi import Body

class LocationRequest(BaseModel):
    logic: Literal["AND", "OR"]
    rules: List[Rule]

@router.post("/api/salesforce/opportunity-sites/")
async def opportunity_sites(payload: LocationRequest, db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    logic = payload.logic
    where_clauses = []

    for rule in payload.rules:
        f, op, v = rule.field, rule.operator, rule.value
        if isinstance(v, list) and op == "between":
            where_clauses.append(f"{f} >= {v[0]} AND {f} <= {v[1]}")
        elif isinstance(v, str):
            if op == "contains":
                where_clauses.append(f"{f} LIKE '%{v}%'")
            elif op == "not_contains":
                where_clauses.append(f"NOT {f} LIKE '%{v}%'")
            elif op == "starts_with":
                where_clauses.append(f"{f} LIKE '{v}%'")
            elif op == "ends_with":
                where_clauses.append(f"{f} LIKE '%{v}'")
            elif op == "equals":
                where_clauses.append(f"{f} = '{v}'")
            elif op == "not_equals":
                where_clauses.append(f"{f} != '{v}'")
        elif isinstance(v, (int, float)):
            where_clauses.append(f"{f} {op} {v}")

    where_clause = f" {logic} ".join(where_clauses)
    soql = """
        SELECT Account.Name, Account.BillingCity, Account.BillingCountry, Account.BillingLatitude, Account.BillingLongitude, Type
        FROM Opportunity
    """
    if where_clause:
        soql += f" WHERE {where_clause}"

    url = f"{token.instance_url}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {token.access_token}"}
    params = {"q": soql}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers, params=params)

    if res.status_code != 200:
        return JSONResponse({"error": "Salesforce query failed", "details": res.json()}, status_code=res.status_code)

    records = res.json()["records"]
    filtered = [
        {
            "site": r["Account"]["Name"],
            "latitude": r["Account"].get("BillingLatitude"),
            "longitude": r["Account"].get("BillingLongitude"),
            "city": r["Account"].get("BillingCity"),
            "country": r["Account"].get("BillingCountry"),
            "type": r.get("Type")
        }
        for r in records if r.get("Account", {}).get("BillingLatitude") and r.get("Account", {}).get("BillingLongitude")
    ]

    return filtered


@router.get("/api/geo/city")
def get_city_by_name(name: str, db: Session = Depends(get_db)):
    match = db.query(GeonamesCity).filter(GeonamesCity.name.ilike(f"%{name}%")).order_by(GeonamesCity.population.desc()).first()
    if not match:
        return JSONResponse({"error": f"No match found for city: {name}"}, status_code=404)
    return {
        "name": match.name,
        "latitude": match.latitude,
        "longitude": match.longitude,
        "country": match.country_code,
        "population": match.population
    }


@router.post("/api/salesforce/profiling-opportunities-map")
async def profiling_opportunities_map(query: FilterQuery, db: Session = Depends(get_db)):
    import pprint

    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    print("🟨 Filtros recibidos:")
    pprint.pprint(query.rules)
    print("🟩 Columnas seleccionadas:")
    pprint.pprint(query.columns)

    # 📂 Leer field types del JSON
    FIELD_TYPE_MAP = {}
    try:
        fields_file = Path("app/static/fields_label_name_opportunity.json")
        with fields_file.open("r") as f:
            field_defs = json.load(f)
            if "fields" in field_defs:
                field_defs = field_defs["fields"]
            FIELD_TYPE_MAP = {f["name"]: f.get("type", "string") for f in field_defs}
    except Exception as e:
        print(f"❌ Error reading field types: {e}")

    # ✅ Validar y convertir reglas
    validated_rules = []
    for rule in query.rules:
        f = rule.field
        op = rule.operator
        v = rule.value
        original_value = v

        field_type = FIELD_TYPE_MAP.get(f, "string")

        try:
            if field_type == "boolean":
                if isinstance(v, str):
                    v = v.strip().lower()
                    if v in ["true", "1", "yes"]:
                        v = True
                    elif v in ["false", "0", "no"]:
                        v = False
                    else:
                        raise ValueError(f"Invalid boolean string: {original_value}")
                elif isinstance(v, (int, float)):
                    v = bool(v)

            elif field_type in ["int", "ID"]:
                v = int(v)

            elif field_type == "double":
                v = float(v)

            elif field_type in ["date", "dateTime", "string"]:
                v = str(v)

            validated_rules.append(Rule(field=f, operator=op, value=v))

        except Exception as e:
            print(f"❌ Error parsing value '{original_value}' for field '{f}' ({field_type}): {e}")
            raise HTTPException(status_code=422, detail=f"Invalid value '{original_value}' for field '{f}' (type {field_type})")

    # 🔁 Construcción del WHERE SOQL
    base_filters = [
        "StageName != 'Closed Lost'",
        "Type = 'CTS Profiling'"
    ]
    where_clauses = []

    for rule in validated_rules:
        f, op, v = rule.field, rule.operator, rule.value

        if isinstance(v, list) and op == "between" and len(v) == 2:
            where_clauses.append(f"{f} >= {v[0]} AND {f} <= {v[1]}")
        elif isinstance(v, str):
            if op == "contains":
                where_clauses.append(f"{f} LIKE '%{v}%'")
            elif op == "not_contains":
                where_clauses.append(f"NOT {f} LIKE '%{v}%'")
            elif op == "starts_with":
                where_clauses.append(f"{f} LIKE '{v}%'")
            elif op == "ends_with":
                where_clauses.append(f"{f} LIKE '%{v}'")
            elif op in ("equals", "="):
                where_clauses.append(f"{f} = '{v}'")
            elif op in ("not_equals", "!="):
                where_clauses.append(f"{f} != '{v}'")
        elif isinstance(v, bool):
            soql_bool = 'TRUE' if v else 'FALSE'
            where_clauses.append(f"{f} = {soql_bool}")
        elif isinstance(v, (int, float)):
            if op in [">", "<", ">=", "<=", "=", "!="]:
                where_clauses.append(f"{f} {op} {v}")

    logic = query.logic.upper()  # ✅ Normaliza el combinador lógico
    combined_filters = f"({f' {query.logic} '.join(where_clauses)})" if where_clauses else None
    all_clauses = base_filters + ([combined_filters] if combined_filters else [])
    soql_where = " AND ".join(all_clauses)

    selected_fields = list(set(query.columns + [
        "Id", "Name", "StageName", "Type", "Account.Name",
        "Account.ShippingStreet", "Account.ShippingCity",
        "Account.ShippingPostalCode", "Account.ShippingCountry"
    ]))

    soql = f"""
        SELECT {', '.join(selected_fields)}
        FROM Opportunity
        WHERE {soql_where}
    """
    print("📄 SOQL generado:")
    print(soql)

    # 🚀 Ejecutar consulta
    url = f"{token.instance_url}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {token.access_token}"}
    params = {"q": soql}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers, params=params)

    if res.status_code != 200:
        return JSONResponse({
            "error": "Salesforce query failed",
            "details": res.json()
        }, status_code=res.status_code)

    records = res.json().get("records", [])
    print(f"🟡 Total oportunidades devueltas desde Salesforce: {len(records)}")

    result = []

    for r in records:
        account = r.get("Account", {})
        parts = [
            account.get("ShippingStreet"),
            account.get("ShippingCity"),
            account.get("ShippingPostalCode"),
            account.get("ShippingCountry")
        ]
        address = ", ".join([p for p in parts if p])
        maps_url = f"https://www.google.com/maps?q={address.replace(' ', '+')}" if address else None

        latitude, longitude = None, None
        if address:
            geo = db.query(OpportunityGeolocation).filter_by(opportunity_id=r["Id"]).first()
            if geo:
                latitude, longitude = geo.latitude, geo.longitude

        extra_data = {}
        for f in query.columns:
            if f.startswith("Account."):
                account_field = f.split("Account.")[1]
                extra_data[f] = account.get(account_field)
            else:
                extra_data[f] = r.get(f)

        result.append({
            "opportunity_id": r.get("Id"),
            "name": r.get("Name"),
            "stage": r.get("StageName"),
            "type": r.get("Type"),
            "account": account.get("Name"),
            "address": address,
            "google_maps_url": maps_url,
            "latitude": latitude,
            "longitude": longitude,
            **extra_data
        })

    print(f"✅ Oportunidades tras filtrar y procesar: {len(result)}")
    return result

@router.get("/api/salesforce/opportunities-with-address")
async def opportunities_with_address(db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    # SOQL con filtros por StageName y Type
    soql = """
        SELECT Id, Name, StageName, Type, Account.Name,
               Account.RecordType.DeveloperName,
               Account.ShippingStreet,
               Account.ShippingCity,
               Account.ShippingState,
               Account.ShippingPostalCode,
               Account.ShippingCountry
        FROM Opportunity
        WHERE Account.RecordType.DeveloperName = 'SubAccount'
        AND Account.ShippingCity != NULL
        AND Account.ShippingCountry != NULL
        AND StageName != 'Closed Lost'
        AND Type = 'CTS Profiling'
    """

    url = f"{token.instance_url}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {token.access_token}"}
    params = {"q": soql}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers, params=params)

    if res.status_code != 200:
        return JSONResponse({
            "error": "Salesforce query failed",
            "details": res.json()
        }, status_code=res.status_code)

    records = res.json().get("records", [])
    result = []

    for r in records:
        account = r.get("Account", {})
        parts = [
            account.get("ShippingStreet"),
            account.get("ShippingCity"),
            account.get("ShippingPostalCode"),
            account.get("ShippingCountry")
        ]
        address = ", ".join([p for p in parts if p])
        google_maps_url = f"https://www.google.com/maps?q={address.replace(' ', '+')}" if address else None

        result.append({
            "opportunity_id": r["Id"],
            "name": r["Name"],
            "stage": r["StageName"],
            "type": r["Type"],
            "account": account.get("Name"),
            "address": address,
            "google_maps_url": google_maps_url
        })

    return result

@router.get("/api/salesforce/account-recordtypes")
async def get_accounts_by_record_type(record_type: str = "SubAccount", db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    soql = f"""
        SELECT Id, Name, RecordType.Name, Main_Address__c
        FROM Account
        WHERE RecordType.Name = '{record_type}'
    """

    url = f"{token.instance_url}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {token.access_token}"}
    params = {"q": soql}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers, params=params)

    if res.status_code != 200:
        return JSONResponse({"error": "Salesforce query failed", "details": res.json()}, status_code=res.status_code)

    return res.json()["records"]

@router.get("/api/salesforce/account-subaccounts")
async def list_subaccounts(db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    soql = """
        SELECT Id, Name, RecordType.Name
        FROM Account
        WHERE RecordType.Name = 'SubAccount'
    """

    url = f"{token.instance_url}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {token.access_token}"}
    params = {"q": soql}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers, params=params)

    if res.status_code != 200:
        return JSONResponse({"error": "Salesforce query failed", "details": res.json()}, status_code=res.status_code)

    return res.json()["records"]

@router.get("/api/salesforce/debug-fields")
async def debug_account_fields(db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    url = f"{token.instance_url}/services/data/v59.0/sobjects/Account/describe"
    headers = {"Authorization": f"Bearer {token.access_token}"}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)

    if res.status_code != 200:
        return JSONResponse({
            "error": "Failed to get field metadata",
            "details": res.json()
        }, status_code=res.status_code)

    return res.json()["fields"]

@router.get("/api/salesforce/describe/account")
async def describe_account_fields(db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    url = f"{token.instance_url}/services/data/v59.0/sobjects/Account/describe"
    headers = {"Authorization": f"Bearer {token.access_token}"}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)

    if res.status_code != 200:
        return JSONResponse({"error": "Failed to describe object", "details": res.json()}, status_code=res.status_code)

    return res.json()

@router.get("/api/salesforce/describe/opportunity")
async def describe_account_fields(db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    url = f"{token.instance_url}/services/data/v59.0/sobjects/Opportunity/describe"
    headers = {"Authorization": f"Bearer {token.access_token}"}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)

    if res.status_code != 200:
        return JSONResponse({"error": "Failed to describe object", "details": res.json()}, status_code=res.status_code)

    return res.json()


router.get("/api/salesforce/describe/account")
async def describe_account_fields(db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    url = f"{token.instance_url}/services/data/v59.0/sobjects/Account/describe"
    headers = {"Authorization": f"Bearer {token.access_token}"}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)

    if res.status_code != 200:
        return JSONResponse({"error": "Failed to describe object", "details": res.json()}, status_code=res.status_code)

    return res.json()


@router.delete("/sf/logout")
async def logout_salesforce_token(db: Session = Depends(get_db)):
    tokens = db.query(SalesforceToken).all()
    if not tokens:
        return JSONResponse({"message": "Already logged out"}, status_code=200)

    for t in tokens:
        db.delete(t)
    db.commit()
    return {"message": f"Deleted {len(tokens)} Salesforce token(s)"}

@router.get("/api/geo/cache-status")
def check_geo_cache_status(db: Session = Depends(get_db)):
    count = db.query(OpportunityGeolocation).count()
    return {"geocoded_count": count}

@router.post("/api/geo/geocode-all")
async def geocode_all_subaccounts(db: Session = Depends(get_db)):
    from app.models import SalesforceToken, AccountGeolocation
    import httpx

    token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
    if not token:
        return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

    soql = """
        SELECT Id, Name, ShippingStreet, ShippingCity, ShippingPostalCode, ShippingCountry
        FROM Account
        WHERE RecordType.Name = 'SubAccount' AND C_Type__c = 'Clinical'
    """

    url = f"{token.instance_url}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {token.access_token}"}
    params = {"q": soql}

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers, params=params)

    if res.status_code != 200:
        return JSONResponse({"error": "Salesforce query failed", "details": res.json()}, status_code=res.status_code)

    records = res.json().get("records", [])

    new_entries = 0
    already_geocoded = 0
    skipped_incomplete = 0
    geocode_failed = 0

    for acc in records:
        acc_id = acc["Id"]
        name = acc.get("Name")
        city = acc.get("ShippingCity")
        country = acc.get("ShippingCountry")
        if not city or not country:
            print(f"⚠️ SubAccount {acc_id} '{name}' sin ciudad o país — ignorado")
            skipped_incomplete += 1
            continue

        street = acc.get("ShippingStreet")
        postal = acc.get("ShippingPostalCode")

        full_address_parts = [street, city, postal, country]
        full_address = ", ".join(p for p in full_address_parts if p)
        fallback_address = f"{city}, {country}"

        existing = db.query(AccountGeolocation).filter_by(account_id=acc_id).first()
        if existing:
            already_geocoded += 1
            continue

        coords = await geocode_address(full_address)
        if not coords:
            print(f"❌ Dirección completa fallida → {full_address}")
            coords = await geocode_address(fallback_address)

        if coords:
            lat, lon = coords
            db.add(AccountGeolocation(account_id=acc_id, address=full_address, latitude=lat, longitude=lon))
            new_entries += 1
        else:
            geocode_failed += 1
            print(f"❌ Fallback también fallido → {fallback_address}")

    db.commit()

    summary = {
        "total_processed": len(records),
        "already_geocoded": already_geocoded,
        "new_entries": new_entries,
        "skipped_incomplete": skipped_incomplete,
        "geocode_failed": geocode_failed
    }

    print(f"📦 Resumen geocodificación de subcuentas:", summary)
    return summary

# @router.post("/api/geo/geocode-all")
# async def geocode_all_opportunities(db: Session = Depends(get_db)):
#     from app.models import OpportunityGeolocation
#     import urllib.parse

#     token = db.query(SalesforceToken).order_by(SalesforceToken.issued_at.desc()).first()
#     if not token:
#         return JSONResponse({"error": "Not logged into Salesforce"}, status_code=403)

#     soql = """
#         SELECT Id, Name, StageName, Type, Account.Name,
#                Account.ShippingStreet, Account.ShippingCity,
#                Account.ShippingPostalCode, Account.ShippingCountry
#         FROM Opportunity
#         WHERE StageName != 'Closed Lost' AND Type = 'CTS Profiling'
#     """

#     url = f"{token.instance_url}/services/data/v59.0/query"
#     headers = {"Authorization": f"Bearer {token.access_token}"}
#     params = {"q": soql}

#     async with httpx.AsyncClient() as client:
#         res = await client.get(url, headers=headers, params=params)

#     if res.status_code != 200:
#         return JSONResponse({"error": "Salesforce query failed", "details": res.json()}, status_code=res.status_code)

#     records = res.json().get("records", [])

#     new_entries = 0
#     already_geocoded = 0
#     skipped_incomplete = 0
#     skipped_empty = 0
#     geocode_failed = 0

#     for r in records:
#         opp_id = r["Id"]
#         name = r["Name"]
#         acc = r.get("Account", {})

#         city = acc.get("ShippingCity")
#         country = acc.get("ShippingCountry")
#         if not city or not country:
#             print(f"⚠️ {opp_id} '{name}' sin ciudad o país — ignorado")
#             skipped_incomplete += 1
#             continue

#         street = acc.get("ShippingStreet")
#         postal = acc.get("ShippingPostalCode")

#         full_address_parts = [street, city, postal, country]
#         full_address = ", ".join(p for p in full_address_parts if p)
#         fallback_address = f"{city}, {country}"

#         existing = db.query(OpportunityGeolocation).filter_by(opportunity_id=opp_id).first()
#         if existing:
#             already_geocoded += 1
#             continue

#         coords = await geocode_address(full_address)
#         if coords:
#             lat, lon = coords
#             db.add(OpportunityGeolocation(opportunity_id=opp_id, address=full_address, latitude=lat, longitude=lon))
#             new_entries += 1
#             continue

#         print(f"❌ Geocodificación fallida con dirección completa: {opp_id} '{name}' → {full_address}")

#         # Fallback con ciudad + país
#         coords = await geocode_address(fallback_address)
#         if coords:
#             lat, lon = coords
#             print(f"🟡 Fallback con ciudad/país OK: {opp_id} '{name}' → {fallback_address}")
#             db.add(OpportunityGeolocation(opportunity_id=opp_id, address=fallback_address, latitude=lat, longitude=lon))
#             new_entries += 1
#             continue

#         print(f"❌ Fallback también fallido: {opp_id} '{name}' → {fallback_address}")
#         geocode_failed += 1

#     db.commit()

#     summary = {
#         "total_processed": len(records),
#         "already_geocoded": already_geocoded,
#         "new_entries": new_entries,
#         "skipped_incomplete": skipped_incomplete,
#         "skipped_empty": skipped_empty,
#         "geocode_failed": geocode_failed
#     }

#     print(f"📦 Resumen geocodificación: {summary}")
#     return summary

@router.get("/api/salesforce/sync-subaccounts")
def sync_subaccounts(db: Session = Depends(get_db)):
    token = db.query(SalesforceToken).first()
    if not token:
        raise HTTPException(status_code=403, detail="No Salesforce token available")

    headers = {
        "Authorization": f"Bearer {token.access_token}"
    }

    query = """
    SELECT Id, Name, Type, RecordType.Name
    FROM Account
    WHERE RecordType.Name = 'SubAccount'
    """

    url = f"{token.instance_url}/services/data/v59.0/query"
    response = httpx.get(url, headers=headers, params={"q": query})

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Salesforce query failed")

    data = response.json()["records"]

    for acc in data:
        obj = SalesforceAccount(
            id=acc["Id"],
            name=acc["Name"],
            type=acc["Type"],
            record_type="SubAccount"
        )
        db.merge(obj)

    db.commit()
    return {"inserted": len(data)}

