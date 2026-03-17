#!/usr/bin/env python3
"""
Test: Explorer nearby-multi vs Google Distance Matrix ground truth.

Flow:
  1. Use SF_SESSION_COOKIE → look up SF access token from DB
  2. Explorer search → sf.Assignment.Name contains "Beta Preserve" → base sites (via backend API)
  3. Get coordinates for base sites from SF directly
  4. Get all INNODIA candidate sites from SF directly
  5. Google Distance Matrix → ground truth driving distances
  6. /api/explorer/search/nearby-multi → compare results
  7. Compare Explorer nearby-multi vs Moby (ask Moby the same question)
"""
import os, sys, json, math, time
import httpx
from pathlib import Path

# ── Load .env ─────────────────────────────────────────────────────────────────
env_path = Path(__file__).parent.parent / "backend" / ".env"
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))

GOOGLE_API_KEY  = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ["GOOGLE_API_KEY"]
API_BASE        = os.environ.get("API_BASE", "http://localhost:8000")
SF_SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")

if not SF_SESSION_COOKIE:
    print("ERROR: set SF_SESSION_COOKIE env var")
    sys.exit(1)

MAX_KM = 120
DRIVE_BATCH = 25  # Google DM max destinations per request

# Cookie jar for backend calls
COOKIES = {"sf_session": SF_SESSION_COOKIE}

# ── Step 1: Get SF token from DB ───────────────────────────────────────────────
print("=" * 60)
print("STEP 1: Looking up SF access token from DB...")

import psycopg

# Strip psycopg dialect prefix for plain psycopg3
db_url = os.environ["DATABASE_URL"]
db_url_pg = db_url.replace("postgresql+psycopg://", "postgresql://")

sf_token = None
sf_instance = None

try:
    # Cookie is signed: "<session_id>.<signature>" — DB stores just the UUID part
    session_id = SF_SESSION_COOKIE.rsplit(".", 1)[0]
    with psycopg.connect(db_url_pg, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT access_token, instance_url FROM sf_sessions WHERE session_id = %s",
                (session_id,)
            )
            row = cur.fetchone()
            if row:
                sf_token, sf_instance = row
                print(f"  ✓ SF instance: {sf_instance} (session_id: {session_id})")
            else:
                print(f"  ✗ Session '{session_id}' not found in DB — will rely on backend proxy only")
except Exception as e:
    print(f"  ✗ DB lookup failed: {e} — will rely on backend proxy only")

SF_HEADERS = {"Authorization": f"Bearer {sf_token}", "Content-Type": "application/json"} if sf_token else {}

def sf_query(soql):
    if not sf_token:
        raise RuntimeError("No SF token available")
    url = f"{sf_instance}/services/data/v59.0/query"
    records, next_url = [], None
    while True:
        resp = httpx.get(next_url or url, params=None if next_url else {"q": soql},
                         headers=SF_HEADERS, timeout=60)
        resp.raise_for_status()
        j = resp.json()
        records.extend(j.get("records", []))
        next_url = j.get("nextRecordsUrl")
        if not next_url:
            break
        next_url = sf_instance + next_url
    return records

# ── Step 2: Explorer search for Beta Preserve base sites ─────────────────────
print("\nSTEP 2: Explorer search — Assignment.Name contains 'Beta Preserve'...")
resp = httpx.post(
    f"{API_BASE}/api/explorer/search",
    json={
        "filters": {
            "logic": "AND",
            "rules": [{"field": "sf.Assignment.Name", "operator": "contains", "value": "Beta Preserve"}]
        },
        "columns": ["extra.AssignmentsNames"],
    },
    cookies=COOKIES,
    timeout=120,
)

if resp.status_code != 200:
    print(f"  ✗ Explorer API returned {resp.status_code}: {resp.text[:200]}")
    sys.exit(1)

data = resp.json()
base_sites = []
for row in data.get("rows", []):
    base_sites.append({
        "account_id":   row["account_id"],
        "account_name": row["account_name"],
        "lat": None, "lng": None,
        "city": row.get("city", ""), "country": row.get("country", ""),
    })
print(f"  ✓ {len(base_sites)} Beta Preserve sites via Explorer API")

if not base_sites:
    print("  No base sites found — aborting")
    sys.exit(1)

# ── Resolve coordinates for base sites ───────────────────────────────────────
print("\nResolving base site coordinates from SF...")
acc_ids_need_coords = [s["account_id"] for s in base_sites]
if acc_ids_need_coords and sf_token:
    chunk_size = 100
    coord_map = {}
    for i in range(0, len(acc_ids_need_coords), chunk_size):
        chunk = acc_ids_need_coords[i:i+chunk_size]
        ids_str = "','".join(chunk)
        recs = sf_query(
            f"SELECT Id, ShippingLatitude, ShippingLongitude, ShippingCity, ShippingCountry "
            f"FROM Account WHERE Id IN ('{ids_str}')"
        )
        for rec in recs:
            coord_map[rec["Id"]] = rec
    for s in base_sites:
        if s["account_id"] in coord_map:
            rec = coord_map[s["account_id"]]
            s["lat"] = rec.get("ShippingLatitude")
            s["lng"] = rec.get("ShippingLongitude")
            s["city"] = s["city"] or rec.get("ShippingCity", "")
            s["country"] = s["country"] or rec.get("ShippingCountry", "")
else:
    print("  Skipping coordinate lookup (no SF token)")

base_with_coords = [s for s in base_sites if s["lat"] and s["lng"]]
print(f"  ✓ {len(base_with_coords)}/{len(base_sites)} bases have coordinates")

# ── Step 3: Get all INNODIA candidate sites ──────────────────────────────────
print("\nSTEP 3: Getting candidate INNODIA sites from SF...")
base_acc_ids = {s["account_id"] for s in base_sites}

if not sf_token:
    print("  ✗ No SF token — cannot get candidates. Skipping ground truth comparison.")
    print("  Proceeding to call nearby-multi API only.")
    candidates = []
    base_with_coords = []
else:
    innodia_recs = sf_query(
        "SELECT Id, Name, ShippingLatitude, ShippingLongitude, ShippingCity, ShippingCountry "
        "FROM Account WHERE INNODIA_Clinical_Trial_Site__c = true "
        "AND ShippingLatitude != null AND ShippingLongitude != null"
    )
    candidates = []
    for rec in innodia_recs:
        aid = rec["Id"]
        if aid in base_acc_ids:
            continue  # skip — this is a base site
        candidates.append({
            "account_id":   aid,
            "account_name": rec["Name"],
            "lat":          rec["ShippingLatitude"],
            "lng":          rec["ShippingLongitude"],
            "city":         rec.get("ShippingCity", ""),
            "country":      rec.get("ShippingCountry", ""),
        })
    print(f"  ✓ {len(candidates)} INNODIA candidate sites (excluding base sites)")

# ── Step 4: Haversine pre-filter ──────────────────────────────────────────────
def haversine(lat1, lng1, lat2, lng2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))

ground_truth = []
if candidates and base_with_coords:
    base_coords = [(s["lat"], s["lng"]) for s in base_with_coords]

    print(f"\nSTEP 4: Haversine pre-filter (≤ {MAX_KM*2} km from any base)...")
    pre_filtered = []
    for c in candidates:
        min_hav = min(haversine(b[0], b[1], c["lat"], c["lng"]) for b in base_coords)
        if min_hav <= MAX_KM * 2:
            c["min_hav_km"] = round(min_hav, 1)
            pre_filtered.append(c)
    print(f"  ✓ {len(pre_filtered)} candidates within {MAX_KM*2} km haversine")

    # ── Step 5: Google Distance Matrix — ground truth ──────────────────────────
    print(f"\nSTEP 5: Google Distance Matrix — driving distances (batched {DRIVE_BATCH} dests)...")

    def google_dm_batch(origin_lat, origin_lng, dest_list):
        """Returns list of driving km (or None) for each dest in dest_list."""
        o_str = f"{origin_lat},{origin_lng}"
        result = [None] * len(dest_list)
        for i in range(0, len(dest_list), DRIVE_BATCH):
            chunk = dest_list[i:i+DRIVE_BATCH]
            d_str = "|".join(f"{d[0]},{d[1]}" for d in chunk)
            resp = httpx.get(
                "https://maps.googleapis.com/maps/api/distancematrix/json",
                params={"origins": o_str, "destinations": d_str, "mode": "driving", "key": GOOGLE_API_KEY},
                timeout=30,
            )
            resp.raise_for_status()
            j = resp.json()
            if j.get("status") != "OK":
                print(f"    WARN: DM status={j.get('status')} for origin {o_str}")
                continue
            elems = (j.get("rows") or [{}])[0].get("elements", [])
            for k, e in enumerate(elems):
                if isinstance(e, dict) and e.get("status") == "OK":
                    result[i + k] = e["distance"]["value"] / 1000  # metres → km
        return result

    dest_coords = [(c["lat"], c["lng"]) for c in pre_filtered]
    min_drive = [None] * len(pre_filtered)
    total_calls = 0

    for b_idx, base in enumerate(base_with_coords):
        dists = google_dm_batch(base["lat"], base["lng"], dest_coords)
        total_calls += math.ceil(len(dest_coords) / DRIVE_BATCH)
        for i, d in enumerate(dists):
            if d is not None:
                if min_drive[i] is None or d < min_drive[i]:
                    min_drive[i] = d
        print(f"  Base {b_idx+1}/{len(base_with_coords)}: {base['account_name'][:40]} — done", end="\r")

    print(f"\n  ✓ {total_calls} Google DM requests total")

    for c, d in zip(pre_filtered, min_drive):
        if d is not None and d <= MAX_KM:
            ground_truth.append({**c, "drive_km": round(d, 1)})

    ground_truth.sort(key=lambda x: x["drive_km"])

    print(f"\n  ✓ GROUND TRUTH: {len(ground_truth)} INNODIA sites within {MAX_KM} km driving of any Beta Preserve site")
    print()
    for g in ground_truth:
        print(f"    {g['account_name'][:45]:<45}  {g['drive_km']:>6.1f} km  [{g['city']}, {g['country']}]")
else:
    print("\nSTEP 4-5: Skipped (no SF token or no candidates)")

# ── Step 6: Call nearby-multi API ─────────────────────────────────────────────
print(f"\nSTEP 6: Calling /api/explorer/search/nearby-multi with {len(base_acc_ids)} base IDs, {MAX_KM} km...")
api_resp = httpx.post(
    f"{API_BASE}/api/explorer/search/nearby-multi",
    json={
        "base_account_ids": list(base_acc_ids),
        "max_km": MAX_KM,
        "filters": {"logic": "AND", "rules": []},  # NO extra filters — just distance
        "columns": [],
    },
    cookies=COOKIES,
    timeout=300,
)

if api_resp.status_code == 401:
    print(f"  ✗ 401 Unauthorized — session cookie may be expired")
    sys.exit(1)
elif api_resp.status_code != 200:
    print(f"  ✗ API returned {api_resp.status_code}: {api_resp.text[:400]}")
    sys.exit(1)

api_data = api_resp.json()
api_rows = api_data.get("rows", [])
api_names = {r["account_name"]: r for r in api_rows}

print(f"  ✓ API returned {len(api_rows)} rows")
print()
for r in sorted(api_rows, key=lambda x: x.get("data", {}).get("distance_km", 999)):
    dist = r.get("data", {}).get("distance_km", "?")
    print(f"    {r['account_name'][:45]:<45}  {str(dist):>6} km  [{r.get('city','')}, {r.get('country','')}]")

# ── Step 7: Compare ────────────────────────────────────────────────────────────
if ground_truth:
    print(f"\nSTEP 7: Comparison")
    gt_names      = {g["account_name"] for g in ground_truth}
    api_names_set = {r["account_name"] for r in api_rows}

    in_gt_not_api = gt_names - api_names_set
    in_api_not_gt = api_names_set - gt_names

    print(f"\n  Ground truth (Google DM):  {len(ground_truth)} sites")
    print(f"  API result:               {len(api_rows)} sites")

    if not in_gt_not_api and not in_api_not_gt:
        print("\n  ✅ PERFECT MATCH — API and ground truth are identical")
    else:
        if in_gt_not_api:
            print(f"\n  ❌ In ground truth but NOT in API result ({len(in_gt_not_api)}):")
            for n in sorted(in_gt_not_api):
                g = next(x for x in ground_truth if x["account_name"] == n)
                print(f"     - {n}  [{g['drive_km']} km]")
        if in_api_not_gt:
            print(f"\n  ⚠️  In API result but NOT in ground truth ({len(in_api_not_gt)}):")
            for n in sorted(in_api_not_gt):
                print(f"     - {n}")

# ── Step 8: Ask Moby the same question ────────────────────────────────────────
print(f"\nSTEP 8: Asking Moby — 'Which INNODIA sites are within {MAX_KM} km of any Beta Preserve site?'...")
moby_resp = httpx.post(
    f"{API_BASE}/api/ai/chat",
    json={
        "messages": [
            {"role": "user", "content": f"Which INNODIA CTS sites are within {MAX_KM} km driving distance of any site involved in the Beta Preserve study?"}
        ],
        "last_filters": None,
        "last_table": None,
    },
    cookies=COOKIES,
    timeout=300,
)

if moby_resp.status_code != 200:
    print(f"  ✗ Moby returned {moby_resp.status_code}: {moby_resp.text[:200]}")
else:
    moby_data = moby_resp.json()
    moby_table = moby_data.get("last_table") or {}
    moby_rows  = moby_table.get("rows", [])
    moby_cols  = moby_table.get("columns", [])
    moby_text  = moby_data.get("answer", "")

    print(f"  ✓ Moby answer: {moby_text[:300]}")
    print(f"  ✓ Moby table: {len(moby_rows)} rows")

    if moby_rows:
        # Find name column
        name_col = next((c for c in moby_cols if "name" in c.lower()), moby_cols[0] if moby_cols else None)
        moby_names = set()
        for mr in moby_rows:
            if isinstance(mr, dict):
                v = mr.get(name_col, "") if name_col else ""
            elif isinstance(mr, list):
                v = mr[0] if mr else ""
            else:
                v = str(mr)
            if v:
                moby_names.add(str(v))

        api_names_set = {r["account_name"] for r in api_rows}

        in_moby_not_api = moby_names - api_names_set
        in_api_not_moby = api_names_set - moby_names

        print(f"\n  ── Moby vs Explorer nearby-multi ────────────────────────")
        print(f"  Explorer: {len(api_rows)} sites")
        print(f"  Moby:     {len(moby_rows)} sites")

        if not in_moby_not_api and not in_api_not_moby:
            print("  ✅ PERFECT MATCH between Explorer nearby-multi and Moby")
        else:
            if in_moby_not_api:
                print(f"  ⚠️  In Moby but NOT Explorer ({len(in_moby_not_api)}): {sorted(in_moby_not_api)[:5]}")
            if in_api_not_moby:
                print(f"  ⚠️  In Explorer but NOT Moby ({len(in_api_not_moby)}): {sorted(in_api_not_moby)[:5]}")

print("\nDone.")
