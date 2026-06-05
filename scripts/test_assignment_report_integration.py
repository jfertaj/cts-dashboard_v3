"""Live check: the assignment report reproduces the referral ground truth.
Run: SF_SESSION_COOKIE="<sf_session>" API_BASE="https://cts-innodia-dashboard.org" \
     python scripts/test_assignment_report_integration.py
"""
import json, os, sys, urllib.request

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
COOKIE = os.getenv("SF_SESSION_COOKIE", "")
GT = json.load(open(os.path.join(os.path.dirname(__file__), "..",
               "backend", "tests", "fixtures", "referral_groundtruth.json")))

# Real Salesforce study names. The original report matched Opportunity name
# CONTAINS "Baricade" (two records) plus exact "Safeguard" / "Beta Preserve".
STUDIES = ["Baricade Delay (JAJJ)", "Baricade Preserve (JAJK)", "Safeguard", "Beta Preserve"]


def post(path, body):
    req = urllib.request.Request(API_BASE + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Cookie": f"sf_session={COOKIE}"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    if not COOKIE:
        print("ERROR: set SF_SESSION_COOKIE"); sys.exit(1)
    out = post("/api/assignments/report", {
        "studies": STUDIES,
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
