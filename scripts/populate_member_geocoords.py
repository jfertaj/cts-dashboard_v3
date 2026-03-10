#!/usr/bin/env python3
"""
populate_member_geocoords.py
────────────────────────────
Geocode INNODIA RT_Member accounts that are missing ShippingLatitude/Longitude.

Strategy (in order of preference):
  1. ShippingCity + ShippingCountry  (already on Shipping address)
  2. BillingCity  + BillingCountry   (many institutions fill Billing only)
  3. Institution name + country       (last resort — less accurate)

Modes:
  --dry-run (default) → writes proposed_geocoords_<timestamp>.csv, NO SF writes
  --apply             → reads the CSV and writes lat/lng back to Salesforce

Usage:
  # Step 1: generate the CSV for review
  SF_SESSION_COOKIE="<value>" python3 scripts/populate_member_geocoords.py --dry-run

  # Step 2: review proposed_geocoords_*.csv, delete rows you don't want, then:
  SF_SESSION_COOKIE="<value>" python3 scripts/populate_member_geocoords.py --apply

  # (optional) specify a specific CSV file
  SF_SESSION_COOKIE="<value>" python3 scripts/populate_member_geocoords.py --apply --file proposed_geocoords_20260310_123456.csv
"""
from __future__ import annotations
import argparse, csv, json, os, sys, time, pathlib, datetime
import requests

# ── Load backend/.env ─────────────────────────────────────────────────────────

_HERE = pathlib.Path(__file__).parent.parent
_ENV  = _HERE / "backend" / ".env"
if _ENV.exists():
    for line in _ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k not in os.environ:
                os.environ[k] = v

COOKIE      = os.environ.get("SF_SESSION_COOKIE", "")
GOOGLE_KEY  = os.environ.get("GOOGLE_API_KEY", "")
DB_URL      = os.environ.get("DATABASE_URL", "")
COOKIE_NAME = "sf_session"

if not GOOGLE_KEY:
    print("ERROR: GOOGLE_API_KEY not set (checked env + backend/.env)")
    sys.exit(1)

# ── SF authentication via PostgreSQL sf_sessions table ───────────────────────

def _get_sf_token() -> tuple[str, str]:
    """
    Resolve SF access_token + instance_url from:
      1. PostgreSQL sf_sessions table (using SESSION_COOKIE → session_id)
      2. direct simple_salesforce username/password (fallback, rarely works)
    Returns (access_token, instance_url).
    """
    if not COOKIE:
        print("ERROR: SF_SESSION_COOKIE not set")
        sys.exit(1)

    # ---- unsign the cookie (itsdangerous TimestampSigner) ----
    cookie_secret = os.environ.get("COOKIE_SECRET", "dev-secret-change-me")
    try:
        from itsdangerous import TimestampSigner, BadSignature
        signer = TimestampSigner(cookie_secret)
        session_id = signer.unsign(COOKIE).decode()
    except Exception as e:
        # Try treating it as unsigned (local dev)
        session_id = COOKIE.split(".")[0]

    # ---- read from PostgreSQL ----
    if DB_URL:
        try:
            import psycopg
            # psycopg3: strip the sqlalchemy prefix if present
            pg_url = DB_URL
            for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
                if pg_url.startswith(prefix):
                    pg_url = "postgresql://" + pg_url[len(prefix):]
                    break
            with psycopg.connect(pg_url) as conn:
                row = conn.execute(
                    "SELECT access_token, instance_url FROM sf_sessions WHERE session_id = %s",
                    (session_id,),
                ).fetchone()
            if row:
                print(f"✓  SF token from database (session {session_id[:12]}…)")
                return row[0], row[1]
            else:
                print(f"WARN: session {session_id[:12]}… not found in sf_sessions table")
        except Exception as e:
            print(f"WARN: DB read failed ({e}) — trying fallback")

    # ---- fallback: token IS the session_id for simple_salesforce ----
    # simple_salesforce uses access_token as session_id in its API calls
    instance = "https://innodiaivzw.my.salesforce.com"
    print(f"⚠  Using session_id as access_token directly (fallback)")
    return session_id, instance


def _sf_query(token: str, instance: str, soql: str) -> list[dict]:
    """Execute a SOQL query against SF REST API."""
    url = f"{instance}/services/data/v59.0/query"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, params={"q": soql}, headers=headers, verify=False, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"SF query failed {r.status_code}: {r.text[:300]}")
    data = r.json()
    records = data.get("records", [])
    # Handle paginated results
    next_url = data.get("nextRecordsUrl")
    while next_url:
        r2 = requests.get(f"{instance}{next_url}", headers=headers, verify=False, timeout=60)
        data2 = r2.json()
        records.extend(data2.get("records", []))
        next_url = data2.get("nextRecordsUrl")
    return records


def _sf_composite_update(token: str, instance: str, updates: list[dict]) -> tuple[int, int]:
    """
    Batch PATCH to SF using composite/sobjects endpoint.
    updates = [{"Id": "...", "ShippingLatitude": ..., "ShippingLongitude": ...}, ...]
    Returns (ok_count, err_count).
    """
    import warnings; warnings.filterwarnings("ignore")
    url = f"{instance}/services/data/v59.0/composite/sobjects"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    ok = err = 0
    BATCH = 200
    for i in range(0, len(updates), BATCH):
        batch = updates[i:i+BATCH]
        records = [{"attributes": {"type": "Account"}, **u} for u in batch]
        payload = {"allOrNone": False, "records": records}
        r = requests.patch(url, json=payload, headers=headers, verify=False, timeout=60)
        if r.status_code not in (200, 201):
            print(f"  ERROR batch {i//BATCH+1}: {r.status_code} {r.text[:200]}")
            err += len(batch)
            continue
        for res in r.json():
            if res.get("success"):
                ok += 1
            else:
                err += 1
                errs = res.get("errors", [])
                print(f"  ✗ {errs}")
        print(f"  Batch {i//BATCH+1}/{(len(updates)-1)//BATCH+1}: {len(batch)} records")
        time.sleep(0.2)
    return ok, err


# ── Google Geocoding ──────────────────────────────────────────────────────────

_GEO_CACHE: dict[str, dict | None] = {}

def geocode(query: str) -> dict | None:
    """Geocode a free-text query. Returns {lat, lng, formatted_address} or None."""
    if query in _GEO_CACHE:
        return _GEO_CACHE[query]
    r = requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={"address": query, "key": GOOGLE_KEY},
        timeout=10,
    )
    results = r.json().get("results", []) if r.status_code == 200 else []
    if not results:
        _GEO_CACHE[query] = None
        return None
    loc = results[0]["geometry"]["location"]
    result = {
        "lat": loc["lat"],
        "lng": loc["lng"],
        "formatted_address": results[0].get("formatted_address", ""),
    }
    _GEO_CACHE[query] = result
    time.sleep(0.05)  # gentle rate limit (~20 req/s)
    return result


def best_query(row: dict, billing: dict) -> tuple[str, str]:
    """Build geocode query; return (query, source_label)."""
    name         = row["account_name"]
    ship_city    = row.get("ShippingCity") or ""
    ship_country = row.get("ShippingCountry") or ""
    bill_city    = billing.get("BillingCity") or ""
    bill_country = billing.get("BillingCountry") or ""

    if ship_city and ship_country:
        return f"{name}, {ship_city}, {ship_country}", "shipping"
    if bill_city and bill_country:
        return f"{name}, {bill_city}, {bill_country}", "billing"
    if bill_city:
        return f"{name}, {bill_city}", "billing_city_only"
    if ship_country:
        return f"{name}, {ship_country}", "country_only"
    return name, "name_only"


# ── Dry run ───────────────────────────────────────────────────────────────────

def run_dry_run(token: str, instance: str) -> str:
    # 1. Fetch all RT_Member accounts with address fields
    print("\n── Fetching member accounts from Salesforce ─────────────────────────")
    soql = (
        "SELECT Id, Name, "
        "ShippingCity, ShippingCountry, ShippingPostalCode, "
        "ShippingLatitude, ShippingLongitude, "
        "BillingCity, BillingCountry, BillingPostalCode "
        "FROM Account "
        "WHERE RecordType.DeveloperName = 'RT_Member' "
        "  AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
        "ORDER BY Name"
    )
    records = _sf_query(token, instance, soql)
    print(f"  {len(records)} Member accounts fetched")

    # 2. Split: missing lat/lng vs already have
    missing = [r for r in records if not r.get("ShippingLatitude") or not r.get("ShippingLongitude")]
    already = len(records) - len(missing)
    print(f"  {already} already have lat/lng — will skip")
    print(f"  {len(missing)} need geocoding")

    if not missing:
        print("  ✅ Nothing to do — all members already have coordinates!")
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(_HERE / f"proposed_geocoords_{ts}.csv")

    # 3. Geocode
    print(f"\n── Geocoding {len(missing)} members ─────────────────────────────────")
    proposals  = []
    failed     = []

    for i, row in enumerate(missing):
        query, source = best_query(row, row)  # billing fields are in same row
        result = geocode(query)

        confidence = {"shipping": "high", "billing": "high",
                      "billing_city_only": "medium", "country_only": "low",
                      "name_only": "low"}.get(source, "low")

        marker = "✓" if "high" in confidence else ("~" if "medium" in confidence else "?")

        if result:
            proposals.append({
                "account_id":        row["Id"],
                "account_name":      row["Name"],
                "shipping_city":     row.get("ShippingCity") or "",
                "shipping_country":  row.get("ShippingCountry") or "",
                "billing_city":      row.get("BillingCity") or "",
                "billing_country":   row.get("BillingCountry") or "",
                "query_used":        query,
                "source":            source,
                "confidence":        confidence,
                "lat":               result["lat"],
                "lng":               result["lng"],
                "formatted_address": result["formatted_address"],
            })
            print(f"  [{i+1:3}/{len(missing)}] {marker} {row['Name'][:52]:52s}  "
                  f"{result['lat']:8.4f},{result['lng']:8.4f}  [{source}]")
        else:
            failed.append(row)
            print(f"  [{i+1:3}/{len(missing)}] ✗ {row['Name'][:52]:52s}  NO RESULT")

    # 4. Write CSV
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    outfile = str(_HERE / f"proposed_geocoords_{ts}.csv")
    fields = ["account_id", "account_name", "shipping_city", "shipping_country",
              "billing_city", "billing_country", "query_used", "source",
              "confidence", "lat", "lng", "formatted_address"]
    with open(outfile, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(proposals)

    print(f"\n  Proposed: {len(proposals)}  |  Failed (no geocode result): {len(failed)}")
    if failed:
        print("  ⚠️  Could not geocode:")
        for r in failed:
            print(f"     - {r['Name']}")
    print(f"\n  ✅ CSV written: {outfile}")
    print(f"     Review it, delete rows you don't want, then run:")
    print(f"     SF_SESSION_COOKIE=\"...\" python3 scripts/populate_member_geocoords.py --apply --file {pathlib.Path(outfile).name}")
    return outfile


# ── Apply ─────────────────────────────────────────────────────────────────────

def run_apply(token: str, instance: str, csvfile: str) -> None:
    with open(csvfile, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    updates = []
    for r in rows:
        if r.get("lat") and r.get("lng"):
            try:
                updates.append({
                    "Id":                r["account_id"],
                    "ShippingLatitude":  float(r["lat"]),
                    "ShippingLongitude": float(r["lng"]),
                })
            except ValueError:
                pass

    print(f"\n── Writing {len(updates)} records to Salesforce ─────────────────────")
    print(f"   Source CSV: {csvfile}")
    print(f"   Fields:     ShippingLatitude, ShippingLongitude")
    confirm = input(f"\n   Proceed? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print("   Aborted.")
        return

    ok, err = _sf_composite_update(token, instance, updates)
    print(f"\n  ✅ Done — Updated: {ok}  |  Errors: {err}")
    if ok > 0:
        print("  The members_bootstrap cache will refresh automatically (5-min TTL).")
        print("  Or restart the backend to force immediate refresh.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import warnings; warnings.filterwarnings("ignore")

    parser = argparse.ArgumentParser(description="Geocode INNODIA Member lat/lng in Salesforce")
    parser.add_argument("--apply",   action="store_true", help="Apply CSV to Salesforce (writes data!)")
    parser.add_argument("--dry-run", action="store_true", help="Geocode only, write CSV (default)")
    parser.add_argument("--file",    default=None,        help="CSV file to use with --apply")
    args = parser.parse_args()

    token, instance = _get_sf_token()
    print(f"  SF instance: {instance}")

    if args.apply:
        # Find CSV
        if args.file:
            csvfile = args.file
        else:
            csvs = sorted(_HERE.glob("proposed_geocoords_*.csv"), reverse=True)
            if not csvs:
                print("ERROR: no proposed_geocoords_*.csv found. Run --dry-run first.")
                sys.exit(1)
            csvfile = str(csvs[0])
            print(f"  Using most recent CSV: {pathlib.Path(csvfile).name}")
        run_apply(token, instance, csvfile)
    else:
        run_dry_run(token, instance)


if __name__ == "__main__":
    main()
