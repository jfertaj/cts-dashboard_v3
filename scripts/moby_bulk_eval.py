#!/usr/bin/env python3
"""Bulk evaluation of Moby questions — captures raw results for classification.

Usage:
    SF_SESSION_COOKIE="..." python scripts/moby_bulk_eval.py
"""
import os, sys, json, re, time, urllib.request, urllib.error

API_BASE   = os.getenv("API_BASE", "http://localhost:8000")
COOKIE_VAL = os.getenv("SF_SESSION_COOKIE", "")
COOKIE_HDR = f"sf_session={COOKIE_VAL}" if COOKIE_VAL else ""
OUT_FILE   = os.path.join(os.path.dirname(__file__), "..", "docs", "moby-bulk-eval-raw.json")

QUESTIONS = [
    # -- Finding sites that meet study criteria --
    ("SC01", "Show me all CTS sites in Germany, Italy, and Belgium"),
    ("SC02", "Which sites have more than 50 Stage 1 individuals currently followed?"),
    ("SC03", "List all sites with Stage 2 patients AND overnight stay capacity"),
    ("SC04", "Find sites in Spain or Portugal that have pharmacy on-site"),
    ("SC05", "Show me sites that have both Stage 1 AND Stage 2 patients"),
    ("SC06", "Which sites in France have HLA typing capability available?"),
    ("SC07", "List sites with more than 20 newly diagnosed T1D patients under 18 per year"),
    ("SC08", "Find sites that have a pharmacy AND are CTS-validated"),
    ("SC09", "Show all sites that have NOT yet completed profiling"),
    ("SC10", "Which sites have autoantibody testing possible?"),
    # -- Country-level planning --
    ("CL01", "How many CTS sites are there in each country?"),
    ("CL02", "Which countries have the most Stage 1 patients followed?"),
    ("CL03", "Show me all Italian sites sorted by number of Stage 2 patients"),
    ("CL04", "What sites do we have in Eastern Europe?"),
    ("CL05", "Are there any CTS sites in Romania or Bulgaria?"),
    # -- Site status and pipeline --
    ("SS01", "Show me all sites where profiling form has been uploaded to the database"),
    ("SS02", "Which sites have had a first contact date but no meeting date yet?"),
    ("SS03", "List sites where C_Profiling_Complete is true"),
    ("SS04", "Find sites that are CTS-validated but not yet active in any assignment"),
    ("SS05", "Which sites have a Referral/Clinical Partner role?"),
    ("SS06", "What percentage of clinical sites are participating to at least 1 activity?"),
    ("SS07", "Show me all of the sites visited for qualification in the last year"),
    ("SS08", "Show me all of the sites with planned visits"),
    ("SS09", "Show me all of the sites in Italy that still need a qualification visit"),
    # -- Screening & diagnosis capabilities --
    ("QD01", "Which sites can perform early diagnosis screening?"),
    ("QD02", "Show me sites that have ZnT8 autoantibody testing available"),
    ("QD03", "Find sites with islet autoantibody testing AND more than 100 T1D patients per year"),
    ("QD04", "Which sites have insulin autoantibody testing?"),
    ("QD05", "Show me sites with OGTT (oral glucose tolerance test) capacity"),
    # -- Operational readiness --
    ("QO01", "Which sites can accommodate a single-day visit longer than 8 hours?"),
    ("QO02", "Find sites where documents are retained for more than 15 years"),
    ("QO03", "Which sites have a dedicated study coordinator on-site?"),
    ("QO04", "Show me sites that have experience running early diagnosis programs"),
    ("QO05", "Which sites confirmed they can perform emergency procedures within 30 minutes?"),
    # -- Infrastructure --
    ("QI01", "Which sites have a biobank or sample storage facility?"),
    ("QI02", "Show me sites that can run mechanistic studies (LAB-validated)"),
    ("QI03", "Which sites have CT or MRI capability?"),
    ("QI04", "Find sites with insulin pump therapy available"),
    # -- Site counts and rankings --
    ("MR01", "How many sites do we have per country?"),
    ("MR02", "Which country has the most newly diagnosed T1D patients under 18?"),
    ("MR03", "What are the top 10 sites by number of Stage 1 individuals followed?"),
    ("MR04", "Show me a breakdown of Stage 2 patients by country"),
    ("MR05", "Which sites have the highest number of T1D patients seen per year?"),
    ("MR06", "How many sites across all countries have both Stage 1 and Stage 2 patients?"),
    # -- Specific study planning --
    ("SP01", "Show me all sites involved in the Beta Preserve assignment"),
    ("SP02", "Which sites are participating in Barricade Delay?"),
    ("SP03", "Find all sites within 150 km of the sites assigned to Safeguard"),
    ("SP04", "Which sites are part of CT-3 Phase I trials?"),
    ("SP05", "Show me all sites that have ever had an assignment in Germany"),
    # -- Geographic/logistics --
    ("GL01", "What are the 5 closest CTS sites to Munich?"),
    ("GL02", "Show me all sites within 200 km of Paris"),
    ("GL03", "Which sites are nearest to Leuven, Belgium?"),
    ("GL04", "Find the closest site with overnight stay capacity to Warsaw"),
    ("GL05", "Show me all sites within driving distance of Madrid that have pharmacy on-site"),
    ("GL06", "What CTS sites are near the sites involved in the Safeguard activity?"),
    # -- People and contacts --
    ("PC01", "Who are the study coordinators at the Italian sites?"),
    ("PC02", "Show me the principal investigators for all German sites"),
    ("PC03", "Who is the PI at the site in Barcelona?"),
    ("PC04", "Which sites don't have a dedicated study coordinator listed?"),
    ("PC05", "Show me all contacts at sites in the Netherlands"),
    # -- Activity and assignment analysis --
    ("AA01", "Which sites are active in any current assignment?"),
    ("AA02", "How many sites are involved in activities sponsored by Sanofi?"),
    ("AA03", "Show me all activities and how many sites participate in each"),
    ("AA04", "Which assignments have the most sites?"),
    ("AA05", "List all sites that have no current assignment"),
    ("AA06", "Show me a breakdown of assignments by country"),
    # -- Profiling pipeline --
    ("PP01", "Which sites have completed profiling but aren't CTS yet?"),
    ("PP02", "How many sites are currently in the profiling stage?"),
    ("PP03", "Show me the profiling sites in Poland sorted by meeting date"),
    ("PP04", "Which profiling sites have submitted the questionnaire but not had a meeting?"),
    ("PP05", "Find sites where profiling form was sent but not yet received"),
    # -- Multi-condition complex queries --
    ("MC01", "Show me sites in Germany or Austria that have Stage 2 patients AND pharmacy on-site"),
    ("MC02", "Find sites with more than 30 Stage 1 patients that are NOT already in any assignment"),
    ("MC03", "Which Italian or Spanish sites have HLA typing AND overnight stay AND more than 100 T1D patients per year?"),
    ("MC04", "Show me all sites where profiling is complete AND they have ZnT8 testing AND they're in France or Belgium"),
    # -- Members: Institution discovery --
    ("MI01", "Show me all Member institutions in the UK"),
    ("MI02", "Which institutions have both CS and CTS validated roles?"),
    ("MI03", "Find institutions that have a DxLab (Diagnostic Lab) role"),
    ("MI04", "Which institutions are validated as Patient Organizations?"),
    ("MI05", "Show me all Research/Mechanistic Lab members in Germany"),
    # -- Members: Relationship mapping --
    ("MR06b", "How many clinical subaccounts does the Leuven institution have?"),
    ("MR07", "Which institution does the site in Brest belong to?"),
    ("MR08", "Show me all clinical units linked to the Paris member institution"),
    ("MR09", "What is the local language name for Oslo University"),
    # -- Org-level contacts --
    ("OC01", "Who are the key contacts at the UCL member institution?"),
    ("OC02", "Show me all contacts and roles at the Edinburgh institution"),
    ("OC03", "Which institutions have proposed Clinical Site role but not validated yet?"),
    # -- Maps: Visual planning --
    ("MV01", "Show me all CTS sites in Europe on a map"),
    ("MV02", "Which sites are geographically clustered together in Northern Italy?"),
    ("MV03", "Are there any CTS sites in Scandinavia? Where exactly?"),
    ("MV04", "Show me the distribution of Stage 1 sites across Europe"),
    ("MV05", "Which regions of Spain have CTS sites?"),
    # -- Maps: Logistics --
    ("ML01", "I need to plan a monitoring visit tour through central Europe - which sites are clustered together?"),
    ("ML02", "Are there CTS sites near the Frankfurt airport corridor?"),
    ("ML03", "Show me all sites within 300 km of Brussels for a potential sub-study"),
    # -- Qualification Upload / Data Quality: After uploading --
    ("QU01", "Did the qualification data for site X upload correctly?"),
    ("QU02", "Why is the pharmacy field showing empty for the Berlin site?"),
    ("QU03", "The ZnT8 field isn't showing up in the Explorer - is it in the qualification data?"),
    ("QU04", "Can I see a preview of the full qualification for the Warsaw site?"),
    # -- Data reconciliation --
    ("DR01", "The site in Lyon uploaded their form - does the data match what's in Salesforce?"),
    ("DR02", "Which sites have uploaded a qualification form but don't yet have geocoordinates?"),
    ("DR03", "Show me all sites where the qual upload date is older than 2 years"),
    # -- Reporting & Export: For presentations/governance --
    ("RE01", "Give me a table of all CTS sites with their Stage 1 and Stage 2 patient counts, by country"),
    ("RE02", "Export to CSV all sites with their PI names and study coordinator contact details"),
    ("RE03", "Show me all German sites with their profiling completion status"),
    ("RE04", "Give me a count of sites per country that have at least one validated role"),
    ("RE05", "I need a chart of Stage 2 patients per country for the steering committee"),
    # -- Tracking over time --
    ("RT01", "Which sites completed profiling in the last year?"),
    ("RT02", "Show me all sites where the first contact was in 2024"),
    ("RT03", "Which sites had a meeting date in Q3 2024?"),
    # -- Feasibility assessments --
    ("FA01", "We need 20 sites for a new study that requires overnight stay and pharmacy - which sites qualify?"),
    ("FA02", "For a Stage 1 prevention trial we need sites with >30 Stage 1 patients - how many sites do we have, and in which countries?"),
    ("FA03", "We want to expand into Eastern Europe - which countries already have profiling sites we could fast-track to CTS?"),
    ("FA04", "How many sites could potentially support early diagnosis work based on their profiling data?"),
    ("FA05", "If we need HLA typing at the site level, how many of our current sites can provide it?"),
    # -- Network planning --
    ("NP01", "Are there clusters of sites in any country where we could designate a regional hub?"),
    ("NP02", "Which member institutions have multiple clinical subaccounts that could serve as coordinating centers?"),
    ("NP03", "Show me which CTS sites are also validated Diagnostic Labs"),
    # -- Budget and resource allocation --
    ("BR01", "How many sites per country are currently active in assignments?"),
    ("BR02", "Which countries have the highest site density - are we over-represented anywhere?"),
    ("BR03", "Which sites haven't been involved in any assignment in the last 2 years? They might need re-engagement."),
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:500].decode(errors="replace")
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}


def extract_info(result) -> dict:
    if result is None:
        return {"status": "ERROR", "error": "response body was null", "rows": 0, "columns": [], "answer_preview": "", "has_table": False, "has_map": False}
    if not isinstance(result, dict):
        return {"status": "ERROR", "error": f"unexpected response type: {type(result).__name__}", "rows": 0, "columns": [], "answer_preview": "", "has_table": False, "has_map": False}
    if "error" in result:
        return {"status": "ERROR", "error": result["error"][:200], "rows": 0, "columns": [], "answer_preview": "", "has_table": False, "has_map": False}
    ans = result.get("answer", "")
    ans_text = re.sub(r"<[^>]+>", "", ans).strip()[:200]
    tbl = result.get("table") or {}
    rows = tbl.get("rows") or []
    cols = [c.get("key", c.get("label", "")) for c in (tbl.get("columns") or [])]
    return {
        "status": "OK",
        "rows": len(rows),
        "columns": cols,
        "answer_preview": ans_text,
        "has_table": bool(rows),
        "has_map": "map" in ans.lower() or bool(result.get("map")),
    }


def main():
    if not COOKIE_VAL:
        print("ERROR: Set SF_SESSION_COOKIE env var", file=sys.stderr)
        sys.exit(1)

    results = []
    total = len(QUESTIONS)
    for i, (qid, question) in enumerate(QUESTIONS, 1):
        print(f"[{i}/{total}] {qid}: {question[:65]}...", flush=True)
        t0 = time.time()
        raw = ask(question)
        dt = time.time() - t0
        info = extract_info(raw)
        entry = {
            "id": qid,
            "question": question,
            "elapsed_s": round(dt, 1),
            **info,
        }
        status_icon = "OK" if info["status"] == "OK" else "ERR"
        print(f"  [{status_icon}] {dt:.1f}s | {info['rows']} rows | {info['answer_preview'][:80]}", flush=True)
        results.append(entry)
        # Progressive save so a mid-run crash still leaves a usable raw.json
        with open(os.path.abspath(OUT_FILE), "w") as _f:
            json.dump(results, _f, indent=2, ensure_ascii=False)

    out_path = os.path.abspath(OUT_FILE)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nDone: {total} questions. Results saved to {out_path}")

    errors = [r for r in results if r["status"] == "ERROR"]
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for r in errors:
            print(f"  {r['id']}: {r.get('error', '')[:100]}")


if __name__ == "__main__":
    main()
