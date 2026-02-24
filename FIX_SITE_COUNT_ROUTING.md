# CRITICAL FIX: Site Count Routing to Salesforce Account

## Problem Summary

**Issue**: Moby was incorrectly using the Postgres `sites` table for basic site count queries like "How many sites per country?", returning incomplete/incorrect results.

**Root Cause**: 
1. System prompt had conflicting guidance stating "Country/City for Explorer come from Postgres.sites"
2. Fast planner was using `tool_group_count()` which queries Postgres `sites` table
3. No explicit routing hints were provided for basic site count queries

**Impact**: Site counts by country/city were wrong because:
- Postgres `sites` table only contains sites that uploaded qualification files (incomplete dataset)
- The complete and authoritative site list is in Salesforce Account table

## Changes Made

### 1. Updated System Prompt Schema Hint (Line 1832-1839)

**Before**:
```
- Country/City for Explorer come from Postgres.sites (NOT Account.Shipping*).
```

**After**:
```
- Account: **PRIMARY SOURCE for site lists, counts, and geography** (ShippingCountry, ShippingCity)
- **CRITICAL**: Country/City data comes from Account.ShippingCountry and Account.ShippingCity (NOT from Postgres sites table).
```

### 2. Strengthened Critical Routing Rules (Line 1854-1863)

**Key additions**:
```python
**NEVER EVER use sql_query with sites/site_qual tables for:**
  * Counting total sites or sites per country/city
  * Listing sites by country/city
  * Any basic site queries
- Postgres sites/site_qual tables contain ONLY sites that uploaded qualification files (incomplete dataset)
- **ALWAYS filter inactive accounts**: (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)
```

### 3. Updated Default Columns (Line 1923-1932)

**Before**:
```
- Country (sites.country)
- City (sites.city)
```

**After**:
```
- Country (ALWAYS use Account.ShippingCountry from Salesforce)
- City (ALWAYS use Account.ShippingCity from Salesforce)
```

### 4. Enhanced BLOCK 1 Examples (Line 1936-1941)

Added explicit ORDER BY clauses and more variations:
```
• "How many sites per country?" / "Sites by country" → 
  SELECT ShippingCountry country, COUNT(Id) sites FROM Account 
  WHERE RecordType.DeveloperName = 'SubAccount' AND C_Type__c = 'Clinical' 
  AND ShippingCountry != null 
  AND (Account_Inactive__c = false OR Account_Inactive__c = null) 
  AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) 
  GROUP BY ShippingCountry 
  ORDER BY COUNT(Id) DESC

• "Sites per city" → similar query with ShippingCity
```

### 5. Disabled Fast Planner for Basic Site Counts (Line 2147-2158)

**Detection logic**:
```python
is_basic_site_count = (
    wants_count and 
    re.search(r"\bsites?\b", s) and
    not re.search(r"\b(with|have|has|having|where|que|con|que tienen)\b", s) and
    not re.search(r"\b(patient|stage|screening|hla|pharmacy|overnight|t1d|diagnosis|qualification)\b", s)
)

if is_basic_site_count:
    _dbg("Fast planner: basic site count detected, deferring to LLM for Salesforce Account query")
    return None  # Force LLM to use Salesforce
```

### 6. Restricted Fast Planner's group_count (Line 2173-2190)

**Change**: Only allow `group_count` (Postgres sites) when there's an explicit qualification filter:

```python
# Plan B: SOLO para métricas de qualification, NO para sitios básicos
if group_by and wants_count and not is_basic_site_count:
    # SOLO ejecutar si hay una condición de qualification específica
    if cond:  # cond only set when meta.source == "site_qual"
        try:
            table = tool_group_count(db, [group_by], cond)
            ...
```

### 7. Added Explicit Routing Hint (Line 2221-2241)

**New critical detection** before LLM call:
```python
is_basic_site_query = bool(
    re.search(r"\b(how\s+many|count|total|list|show|all)\b.*\bsites?\b", user_utterance) and
    re.search(r"\b(country|countries|city|cities|per|by)\b", user_utterance) and
    not re.search(r"\b(patient|stage|screening|hla|pharmacy|overnight|t1d|diagnosis|qualification|with\s+)\b", user_utterance)
)

if is_basic_site_query:
    hints.append(
        "**CRITICAL ROUTING**: This is a basic site count/list query. "
        "You MUST use salesforce_query with Account table (RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical'). "
        "Use Account.ShippingCountry for country, Account.ShippingCity for city. "
        "NEVER use sql_query with sites or site_qual tables for this type of query. "
        "Filter inactive: (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null). "
        "ORDER BY COUNT(Id) DESC for aggregations."
    )
```

## Testing

### Test Queries (Should Now Work Correctly)

1. **"How many sites per country?"**
   - ✅ Should use Salesforce Account
   - ✅ Should return complete site counts
   - ✅ Should order by count DESC
   - ✅ Should filter out inactive accounts

2. **"Show all sites in Spain"**
   - ✅ Should query Account with ShippingCountry = 'Spain'
   - ✅ Should show City and Country columns (not ShippingCity/ShippingCountry)
   - ✅ Should filter inactive accounts

3. **"Sites per city"**
   - ✅ Should query Account.ShippingCity
   - ✅ Should order by count DESC
   - ✅ Should show both city and country

4. **"Total number of sites"**
   - ✅ Should query Account with COUNT(Id)
   - ✅ Should mention "active sites" in response

### Test Cases That Should Still Use Postgres

1. **"Sites with overnight stay facilities"** → Should use site_qual (qualification data)
2. **"Sites with onsite pharmacy"** → Should use site_qual
3. **"Search comments for 'power outage'"** → Should use qual_search

## Verification Commands

After deploying, verify with:
```bash
# Check backend logs for routing decisions
docker logs cts-dashboard_v3-backend-1 -f

# Look for these debug messages:
# "[AI-CHAT] Fast planner: basic site count detected, deferring to LLM"
# "[AI-CHAT] TOOL CALL → salesforce_query args={'soql': 'SELECT ShippingCountry...'}"
```

## Prevention Measures

To prevent this issue from recurring:

1. **Never** modify system prompt to suggest using `sites` table for basic counts
2. **Always** verify that fast planner skips basic site count queries
3. **Test** "How many sites per country?" after any routing changes
4. **Document** that Postgres `sites` table is INCOMPLETE (only qualification uploads)
5. **Remember** that Salesforce Account is the PRIMARY SOURCE for:
   - Site lists
   - Site counts
   - Geography (ShippingCountry, ShippingCity)

## Key Principle

> **Salesforce Account is the SINGLE SOURCE OF TRUTH for site lists and geography.**  
> **Postgres `sites` table is ONLY for qualification data enrichment.**

## Deployment

Changes deployed:
- ✅ Backend rebuilt: `docker compose build backend`
- ✅ Backend restarted: `docker compose up -d backend`
- ✅ Changes are live and ready for testing

## Related Files

- `backend/app/routers/ai_chat.py` - All routing logic and system prompt
- `CHART_IMPROVEMENTS.md` - Chart enhancements from previous session
