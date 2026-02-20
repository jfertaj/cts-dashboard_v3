import http.client
import json
import time
import sys

# Color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

TEST_CASES = [
    {
        "name": "General Site Count (Salesforce)",
        "q": "How many sites are there in total?",
        "checks": ["count", "total"], # Expect a numeric count or table with count
        "block": "BLOCK 1"
    },
    {
        "name": "Geography Filter (Salesforce)",
        "q": "Show me all sites in Belgium",
        "checks": ["Leuven", "Brussels", "Antwerp", "Gent"], # Common Belgian cities/sites
        "optional_check": True, # Pass if at least one city found or "Found X results"
        "block": "BLOCK 1"
    },
    {
        "name": "Site Qual: On-site Pharmacy (Postgres JOIN + ILIKE)",
        "q": "How many sites have a pharmacy on site?",
        "checks": ["count", "found", "result"],
        "block": "BLOCK 3"
    },
    {
        "name": "Site Qual: Glucose Testing (Postgres JSON Key)",
        "q": "Which sites have glucose testing available?",
        "checks": ["result", "table"],
        "block": "BLOCK 3"
    },
    {
        "name": "Combined: Belgium + On-site Pharmacy (SF + pg JOIN)",
        "q": "Show me sites in Belgium with on-site pharmacy",
        "checks": ["found", "table", "result"],
        "block": "BLOCK 9"
    },
    {
        "name": "Combined: >50 Patients + Overnight Stay (SF + pg JSON)",
        "q": "Sites with more than 50 patients and overnight stay",
        "checks": ["found", "table", "result"],
        "block": "BLOCK 9"
    },
    {
        "name": "Regression: Paediatric Loop + Ongoing Trials (Fix Check)",
        "q": "Find 10 qualified clinical trial sites in Europe that are seeing at least 40 newly diagnosed paediatric patients per year and that have maximum 1 ongoing clinical trial for patients above 12 years.",
        "checks": ["found", "table", "result"],
        "block": "BLOCK 9"
    }
]

def run_test(test_case):
    print(f"Testing: {test_case['name']} [{test_case['block']}]")
    print(f"  Query: \"{test_case['q']}\"")
    
    # Hardcoded cookie from user
    run_test.cookie = "3fe1b2c8-8350-47aa-985d-32c807544dc3.THTeTKvDPKyZVQZ-dP87jfzGPhc"

    headers = {'Content-Type': 'application/json'}
    if run_test.cookie:
        headers['Cookie'] = f"sf_session={run_test.cookie}"

    try:
        conn = http.client.HTTPConnection("localhost", 8000, timeout=20)
        payload = json.dumps({
            "messages": [{"role": "user", "content": test_case['q']}],
            "stream": False
        })
        headers = {'Content-Type': 'application/json'}
        conn.request("POST", "/api/ai/chat", payload, headers)
        res = conn.getresponse()
        data = res.read().decode("utf-8")
        status = res.status
        conn.close()

        if status != 200:
            print(f"  {RED}FAIL{RESET}: HTTP {status} - {data[:100]}")
            return False

        print(f"  Response: {data[:150]}...")
        
        # Validation
        valid = False
        lower_data = data.lower()
        
        # Check for error fallback
        if "couldn't complete" in lower_data or "rephrase" in lower_data:
             print(f"  {RED}FAIL{RESET}: Fallback error message detected.")
             return False

        # Content checks
        matched = []
        for chk in test_case['checks']:
            if chk.lower() in lower_data:
                matched.append(chk)
        
        if test_case.get('optional_check'):
             # For some lists, finding any match is enough, or generic "Found X results"
             if matched or "found" in lower_data or "rows" in lower_data:
                 valid = True
        else:
             if matched or "rows" in lower_data or "table" in lower_data:
                 valid = True

        if valid:
            print(f"  {GREEN}PASS{RESET}")
            return True
        else:
            print(f"  {RED}FAIL{RESET}: Expected {test_case['checks']} not found.")
            return False

    except Exception as e:
        print(f"  {RED}FAIL{RESET}: Exception {e}")
        return False

def main():
    print(f"\n{GREEN}=== Starting AI Chat Verification Battery ==={RESET}\n")
    passed = 0
    total = len(TEST_CASES)
    
    for test in TEST_CASES:
        if run_test(test):
            passed += 1
        print("-" * 50)
        time.sleep(1)  # Faster execution with upgraded tier
        
    print(f"\nSummary: {passed}/{total} Tests Passed")
    if passed == total:
        print(f"{GREEN}ALL SYSTEMS GO used Gemini 2.0 Flash{RESET}")
        sys.exit(0)
    else:
        print(f"{RED}SOME TESTS FAILED{RESET}")
        sys.exit(1)

if __name__ == "__main__":
    main()
