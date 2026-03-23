#!/usr/bin/env python3
"""Test all demo questions against local Moby backend and summarise results.

Usage:
    SF_SESSION_COOKIE="..." python scripts/test_demo_questions.py [--section SECTION]
"""
import os, sys, json, re, time, argparse
import urllib.request, urllib.error

API_BASE   = os.getenv("API_BASE", "http://localhost:8000")
COOKIE_VAL = os.getenv("SF_SESSION_COOKIE", "")
COOKIE_HDR = f"sf_session={COOKIE_VAL}" if COOKIE_VAL else ""

QUESTIONS = [
    # ── Explorer / Site Discovery ──────────────────────────────────────────────
    ("E01", "Show me all CTS sites in Germany, Italy, and Belgium"),
    ("E02", "Which sites have more than 50 Stage 1 individuals currently followed?"),
    ("E03", "List all sites with Stage 2 patients AND overnight stay capacity"),
    ("E04", "Find sites in Spain or Portugal that have pharmacy on-site"),
    ("E05", "Show me sites that have both Stage 1 AND Stage 2 patients"),
    ("E06", "Which sites in France have HLA typing capability available?"),
    ("E07", "List sites with more than 20 newly diagnosed T1D patients under 18 per year"),
    ("E08", "Find sites that have a pharmacy AND are CTS-validated"),
    ("E09", "Show all sites that have NOT yet completed profiling"),
    ("E10", "Which sites have autoantibody testing possible?"),
    # ── Country-level planning ─────────────────────────────────────────────────
    ("C01", "How many CTS sites are there in each country?"),
    ("C02", "Which countries have the most Stage 1 patients followed?"),
    ("C03", "Show me all Italian sites sorted by number of Stage 2 patients"),
    ("C04", "What sites do we have in Eastern Europe?"),
    ("C05", "Are there any CTS sites in Romania or Bulgaria?"),
    # ── Site status / pipeline ─────────────────────────────────────────────────
    ("S01", "Show me all sites where profiling form has been uploaded to the database"),
    ("S02", "Which sites have had a first contact date but no meeting date yet?"),
    ("S03", "List sites where C_Profiling_Complete is true"),
    ("S04", "Find sites that are CTS-validated but not yet active in any assignment"),
    ("S05", "Which sites have a Referral/Clinical Partner role?"),
    # ── Qualification — screening ──────────────────────────────────────────────
    ("Q01", "Which sites can perform early diagnosis screening?"),
    ("Q02", "Show me sites that have ZnT8 autoantibody testing available"),
    ("Q03", "Find sites with islet autoantibody testing AND more than 100 T1D patients per year"),
    ("Q04", "Which sites have insulin autoantibody testing?"),
    ("Q05", "Show me sites with OGTT (oral glucose tolerance test) capacity"),
    # ── Qualification — operational ────────────────────────────────────────────
    ("Q06", "Which sites can accommodate a single-day visit longer than 8 hours?"),
    ("Q07", "Find sites where documents are retained for more than 15 years"),
    ("Q08", "Which sites have a dedicated study coordinator on-site?"),
    ("Q09", "Show me sites that have experience running early diagnosis programs"),
    ("Q10", "Which sites confirmed they can perform emergency procedures within 30 minutes?"),
    # ── Qualification — infrastructure ─────────────────────────────────────────
    ("Q11", "Which sites have a biobank or sample storage facility?"),
    ("Q12", "Show me sites that can run mechanistic studies (LAB-validated)"),
    ("Q13", "Which sites have CT or MRI capability?"),
    ("Q14", "Find sites with insulin pump therapy available"),
    # ── Moby — site counts / rankings ─────────────────────────────────────────
    ("M01", "How many sites do we have per country?"),
    ("M02", "Which country has the most newly diagnosed T1D patients under 18?"),
    ("M03", "What are the top 10 sites by number of Stage 1 individuals followed?"),
    ("M04", "Show me a breakdown of Stage 2 patients by country"),
    ("M05", "Which sites have the highest number of T1D patients seen per year?"),
    ("M06", "How many sites across all countries have both Stage 1 and Stage 2 patients?"),
    # ── Moby — specific study planning ────────────────────────────────────────
    ("M07", "Show me all sites involved in the Beta Preserve assignment"),
    ("M08", "Which sites are participating in Barricade Delay?"),
    ("M09", "Find all sites within 150 km of the sites assigned to Safeguard"),
    ("M10", "Which sites are part of CT-3 Phase I trials?"),
    ("M11", "Show me all sites that have ever had an assignment in Germany"),
    # ── Moby — geographic / logistics ─────────────────────────────────────────
    ("G01", "What are the 5 closest CTS sites to Munich?"),
    ("G02", "Show me all sites within 200 km of Paris"),
    ("G03", "Which sites are nearest to Leuven, Belgium?"),
    ("G04", "Find the closest site with overnight stay capacity to Warsaw"),
    ("G05", "Show me all sites within driving distance of Madrid that have pharmacy on-site"),
    ("G06", "What CTS sites are near the sites involved in the Safeguard activity?"),
    # ── Moby — people / contacts ──────────────────────────────────────────────
    ("P01", "Who are the study coordinators at the Italian sites?"),
    ("P02", "Show me the principal investigators for all German sites"),
    ("P03", "Who is the PI at the site in Barcelona?"),
    ("P04", "Which sites don't have a dedicated study coordinator listed?"),
    ("P05", "Show me all contacts at sites in the Netherlands"),
    # ── Moby — activity / assignment analysis ─────────────────────────────────
    ("A01", "Which sites are active in any current assignment?"),
    ("A02", "How many sites are involved in activities sponsored by Sanofi?"),
    ("A03", "Show me all activities and how many sites participate in each"),
    ("A04", "Which assignments have the most sites?"),
    ("A05", "List all sites that have no current assignment"),
    ("A06", "Show me a breakdown of assignments by country"),
    # ── Moby — profiling pipeline ─────────────────────────────────────────────
    ("F01", "Which sites have completed profiling but aren't CTS yet?"),
    ("F02", "How many sites are currently in the profiling stage?"),
    ("F03", "Show me the profiling sites in Poland sorted by meeting date"),
    ("F04", "Which profiling sites have submitted the questionnaire but not had a meeting?"),
    ("F05", "Find sites where profiling form was sent but not yet received"),
    # ── Moby — multi-condition complex ────────────────────────────────────────
    ("X01", "Show me sites in Germany or Austria that have Stage 2 patients AND pharmacy on-site"),
    ("X02", "Find sites with more than 30 Stage 1 patients that are NOT already in any assignment"),
    ("X03", "Which Italian or Spanish sites have HLA typing AND overnight stay AND more than 100 T1D patients per year?"),
    ("X04", "Show me all sites where profiling is complete AND they have ZnT8 testing AND they're in France or Belgium"),
    # ── Members ───────────────────────────────────────────────────────────────
    ("MB1", "Show me all Member institutions in the UK"),
    ("MB2", "Which institutions have both CS and CTS validated roles?"),
    ("MB3", "Find institutions that have a DxLab (Diagnostic Lab) role"),
    ("MB4", "Which institutions are validated as Patient Organizations?"),
    ("MB5", "Show me all Research/Mechanistic Lab members in Germany"),
    ("MB6", "How many clinical subaccounts does the Leuven institution have?"),
    ("MB7", "Which institution does the site in Brest belong to?"),
    ("MB8", "Show me all clinical units linked to the Paris member institution"),
    # ── Members — org-level contacts ──────────────────────────────────────────
    ("OC1", "Who are the key contacts at the UCL member institution?"),
    ("OC2", "Show me all contacts and roles at the Edinburgh institution"),
    ("OC3", "Which institutions have proposed Clinical Site role but not validated yet?"),
    # ── Maps ──────────────────────────────────────────────────────────────────
    ("MAP1", "Show me all CTS sites in Europe on a map"),
    ("MAP2", "Which sites are geographically clustered together in Northern Italy?"),
    ("MAP3", "Are there any CTS sites in Scandinavia? Where exactly?"),
    ("MAP4", "Show me the distribution of Stage 1 sites across Europe"),
    ("MAP5", "Which regions of Spain have CTS sites?"),
    ("MAP6", "I need to plan a monitoring visit tour through central Europe — which sites are clustered together?"),
    ("MAP7", "Are there CTS sites near the Frankfurt airport corridor?"),
    ("MAP8", "Show me all sites within 300 km of Brussels for a potential sub-study"),
    # ── Qualification upload / data quality ───────────────────────────────────
    ("QU1", "Did the qualification data for site X upload correctly?"),
    ("QU2", "Why is the pharmacy field showing empty for the Berlin site?"),
    ("QU3", "The ZnT8 field isn't showing up in the Explorer — is it in the qualification data?"),
    ("QU4", "Can I see a preview of the full qualification for the Warsaw site?"),
    ("QU5", "The site in Lyon uploaded their form — does the data match what's in Salesforce?"),
    ("QU6", "Which sites have uploaded a qualification form but don't yet have geocoordinates?"),
    ("QU7", "Show me all sites where the qual upload date is older than 2 years"),
    # ── Reporting & export ────────────────────────────────────────────────────
    ("RE1", "Give me a table of all CTS sites with their Stage 1 and Stage 2 patient counts, by country"),
    ("RE2", "Export to CSV all sites with their PI names and study coordinator contact details"),
    ("RE3", "Show me all German sites with their profiling completion status"),
    ("RE4", "Give me a count of sites per country that have at least one validated role"),
    ("RE5", "I need a chart of Stage 2 patients per country for the steering committee"),
    ("RE6", "Which sites completed profiling in the last year?"),
    ("RE7", "Show me all sites where the first contact was in 2024"),
    ("RE8", "Which sites had a meeting date in Q3 2024?"),
    # ── Cross-functional / strategic — feasibility ────────────────────────────
    ("ST1", "We need 20 sites for a new study that requires overnight stay and pharmacy — which sites qualify?"),
    ("ST2", "For a Stage 1 prevention trial we need sites with more than 30 Stage 1 patients — how many sites do we have, and in which countries?"),
    ("ST3", "We want to expand into Eastern Europe — which countries already have profiling sites we could fast-track to CTS?"),
    ("ST4", "How many sites could potentially support early diagnosis work based on their profiling data?"),
    ("ST5", "If we need HLA typing at the site level, how many of our current sites can provide it?"),
    # ── Cross-functional / strategic — network planning ───────────────────────
    ("ST6", "Are there clusters of sites in any country where we could designate a regional hub?"),
    ("ST7", "Which member institutions have multiple clinical subaccounts that could serve as coordinating centers?"),
    ("ST8", "Show me which CTS sites are also validated Diagnostic Labs"),
    # ── Cross-functional / strategic — budget & resource allocation ───────────
    ("ST9", "How many sites per country are currently active in assignments?"),
    ("ST10", "Which countries have the highest site density — are we over-represented anywhere?"),
    ("ST11", "Which sites haven't been involved in any assignment in the last 2 years? They might need re-engagement."),
    # ── Golden Questions (Step 2 benchmark) ───────────────────────────────────
    ("GQ01", "How many CTS sites do we have in total?"),
    ("GQ02", "Which countries have more than 5 CTS sites?"),
    ("GQ03", "List all CTS sites in Italy"),
    ("GQ04", "How many newly diagnosed T1D patients under 18 are tracked per country?"),
    ("GQ05", "Which sites in Germany have Stage 2 patients followed?"),
    ("GQ06", "Show me sites with both Stage 1 AND Stage 2 patients"),
    ("GQ07", "Which sites have an on-site pharmacy?"),
    ("GQ08", "Which sites are assigned to the DETECT activity?"),
    ("GQ09", "Who are the study coordinators at sites in France?"),
    ("GQ10", "What is the total number of Stage 1 patients across all sites?"),
    ("GQ11", "Show me all activities and how many sites are assigned to each"),
    ("GQ12", "Which sites are within 100 km of Munich?"),
    ("GQ13", "Show me sites that have completed profiling AND have Stage 2 patients AND are in Germany or France"),
    ("GQ14", "Rank the top 10 sites by combined Stage 1 + Stage 2 patient count"),
    ("GQ15", "Which sites are in both the DETECT activity and Fabulinus?"),           # known gap: AND across two activities
    ("GQ16", "How many days on average does profiling take per country (from first contact to meeting)?"),  # known gap: date-diff
    ("GQ17", "Which sites have both HLA typing AND overnight stay AND more than 50 T1D patients per year?"),
    ("GQ18", "Which activities have participating sites in Germany, France, AND Italy?"),  # known gap: multi-country AND
    ("GQ19", "Show me all sites that are NOT in any current assignment"),
    ("GQ20", "Which member institutions have 3 or more clinical subaccounts?"),
]


def ask(question: str) -> dict:
    payload = json.dumps({"messages": [{"role": "user", "content": question}]}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/api/ai/chat",
        data=payload,
        headers={"Content-Type": "application/json", "Cookie": COOKIE_HDR},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read()[:200].decode()}"}
    except Exception as e:
        return {"error": str(e)}


def summarise(result: dict) -> str:
    if "error" in result:
        return f"ERROR: {result['error'][:100]}"
    ans = result.get("answer", "")
    # strip HTML tags for display
    ans_text = re.sub(r"<[^>]+>", "", ans).strip()[:120]
    tbl  = result.get("table") or {}
    rows = tbl.get("rows") or []
    cols = [c.get("key", c.get("label","")) for c in (tbl.get("columns") or [])]
    n = len(rows)
    return f"[{n} rows | cols: {cols[:5]}] {ans_text}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--section", default="", help="Filter by prefix e.g. E,C,Q,M,G,P,A,F,X,MB")
    parser.add_argument("--ids", default="", help="Comma-separated IDs e.g. E03,E05,X01")
    args = parser.parse_args()

    filter_ids  = {x.strip().upper() for x in args.ids.split(",") if x.strip()}
    filter_pfx  = {p.strip().upper() for p in args.section.split(",") if p.strip()}

    results = []
    for qid, question in QUESTIONS:
        pfx = re.match(r"[A-Za-z]+", qid).group(0).upper()
        if filter_ids and qid.upper() not in filter_ids:
            continue
        if filter_pfx and pfx not in filter_pfx:
            continue

        print(f"▶ {qid}: {question[:70]}...", flush=True)
        t0 = time.time()
        res = ask(question)
        dt = time.time() - t0
        summary = summarise(res)
        print(f"   ✓ {dt:.1f}s — {summary}\n", flush=True)
        results.append((qid, question, summary, res))

    print(f"\n{'─'*70}")
    print(f"Done: {len(results)} questions tested")
    errors = [(qid, s) for qid, _, s, _ in results if s.startswith("ERROR")]
    if errors:
        print(f"\n⚠ Errors ({len(errors)}):")
        for qid, s in errors:
            print(f"  {qid}: {s}")

if __name__ == "__main__":
    main()
