"""Claude tool specifications (OpenAI-style JSON schema list).

Extracted from app/routers/ai_chat.py in Phase 2 of the Moby refactor.
This module owns the canonical TOOLS_SPEC; ai_chat.py re-exports it for
backward compatibility with tests and any external imports.
"""

from __future__ import annotations

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "soql_query",
            "description": (
                "Run a SOQL SELECT on Opportunity (and Account.*). "
                "When to use: COUNT/GROUP BY aggregations, traversal relationships (e.g. C_Member__r.*), "
                "or fields not in the Explorer catalog. "
                "When NOT to use: Do NOT use for site filtering — use explorer_search instead. "
                "Do NOT use for qualification data — use explorer_search with qual.* filters."
            ),
            "parameters": {
                "type":"object",
                "properties":{
                    "soql":{"type":"string","description":"SOQL SELECT ... FROM Opportunity ..."}
                },
                "required":["soql"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "salesforce_account_extras",
            "description": "Fetch extra info for a Salesforce Account: PI (name/email/phone), CS Contribution flags, latest new diagnoses and assignments count.",
            "parameters": {
                "type":"object",
                "properties":{
                    "account_id":{"type":"string"}
                },
                "required":["account_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "salesforce_account_contacts",
            "description": "List contacts for an Account (optionally including child Accounts). Useful for PI/SC/Study Nurse lookups.",
            "parameters": {
                "type":"object",
                "properties":{
                    "account_id":{"type":"string"},
                    "include_subaccounts":{"type":"boolean","default":False},
                    "role_contains":{"type":"string","description":"Filter by Title/Department contains (fallback when roles not provided)"},
                    "roles":{"type":"array","items":{"type":"string"},"description":"Filter by AccountContactRelation.Role__c; defaults to env SF_CONTACT_ROLES if set"},
                    "title_contains":{"type":"string","description":"Filter by Contact.Title (e.g., 'Study Coordinator', 'Nurse')"}
                },
                "required":["account_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "salesforce_assignments",
            "description": "List assignments per Account using Explorer extras (stage, type, opportunity, created).",
            "parameters": {
                "type":"object",
                "properties":{
                    "account_ids":{"type":"array","items":{"type":"string"}},
                    "active_only":{"type":"boolean","default":False},
                    "last_n_months":{"type":"integer","description":"Limit to recent assignments"}
                },
                "required":["account_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "explorer_set_filters",
            "description": "Tell the UI to update Explorer filters/columns per the given payload.",
            "parameters": {
                "type":"object",
                "properties":{
                    "filters":{"type":"object"},
                    "columns":{"type":"array","items":{"type":"string"}}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manipulate_data",
            "description": "Transform table data before charting: group small values, filter top N, or group by EU regions (North/South/East). Returns modified data ready for render_chart.",
            "parameters": {
                "type":"object",
                "properties":{
                    "operation":{"type":"string","enum":["group_others","top_n","filter","group_by_region"],"description":"Type of transformation"},
                    "threshold":{"type":"number","description":"For group_others: values below this are grouped. For top_n: number of items to keep."},
                    "value_column":{"type":"string","description":"Column name with numeric values (e.g., 'sites', 'count')"},
                    "label_column":{"type":"string","description":"Column name with labels (e.g., 'country', 'city')"},
                    "scheme":{"type":"string","enum":["eu_nse","custom"],"default":"eu_nse"},
                    "regions":{"type":"object","description":"Custom regions mapping: { North:[...], South:[...], East:[...] } (country names or ISO2)"}
                },
                "required":["operation"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "render_chart",
            "description": "Return a chart spec that the frontend will render directly. When NOT to use: for data retrieval — get data first with explorer_search, then visualize with this tool.",
            "parameters": {
                "type":"object",
                "properties":{
                    "kind":{"type":"string","enum":["bar","line","scatter","pie"]},
                    "data":{"type":"array","items":{"type":"object"}},
                    "xKey":{"type":"string"},
                    "yKeys":{"type":"array","items":{"type":"string"}},
                    "meta":{"type":"object"}
                },
                "required":["kind","data","xKey","yKeys"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "explorer_search",
            "description": (
                "DEFAULT tool for any question about clinical trial sites. "
                "Searches sites using FilterGroup with sf.* (Salesforce), qual.* (qualification), "
                "site.* (geography), and extra.* (assignments, activities) fields. "
                "When to use: ANY question about sites — filtering, listing, searching. This should be your FIRST choice. "
                "When NOT to use: Only skip this for SOQL aggregations (use soql_query) or member institutions (use members_search). "
                "Supports nested AND/OR logic. "
                "CRITICAL: ALWAYS express every filter as a rule — NEVER pass an empty filters object. "
                "Country filter: {field:'site.country', operator:'equals', value:'ES'} (ISO-2 codes). "
                "City filter: {field:'site.city', operator:'equals', value:'Barcelona'}. "
                "NESTED GROUPS: rules[] can contain sub-groups {logic, rules} for complex AND/OR. "
                "Examples: "
                "'sites in Spain' → filters={logic:'AND',rules:[{field:'site.country',operator:'equals',value:'ES'}]}. "
                "'Spain OR Italy with overnight' → filters={logic:'AND',rules:[{logic:'OR',rules:[{field:'site.country',op:'equals',value:'ES'},{field:'site.country',op:'equals',value:'IT'}]},{field:'qual.3_5_2__overnight_stay',operator:'equals',value:'Yes'}]}. "
                "'(Germany AND overnight) OR (France AND pediatric)' → filters={logic:'OR',rules:[{logic:'AND',rules:[{field:'site.country',operator:'equals',value:'DE'},{field:'qual.3_5_2__overnight_stay',operator:'equals',value:'Yes'}]},{logic:'AND',rules:[{field:'site.country',operator:'equals',value:'FR'},{field:'qual.3_5_2__overnight_stay',operator:'is_not_empty'}]}]}. "
                "'sites with pharmacy AND ND>50' → filters={logic:'AND',rules:[{field:'qual.3_6__is_your_pharmacy_on_site_or_off_campus',operator:'equals',value:'On-site'},{field:'sf.C_Number_of_new_T1D_diagnosed_O_18__c',operator:'>',value:50}]}. "
                "Operators: equals, not_equals, >, >=, <, <=, contains, in, not_in, is_empty, is_not_empty, between. "
                "ALWAYS use ISO-2 for country values: ES, IT, FR, DE, GB, BE, NL, PT, PL, CZ, SE, DK, NO, FI, AT, CH, IE. "
                "qual.* keys: 'qual.<section_key>' (e.g. 'qual.3_6__is_your_pharmacy_on_site_or_off_campus'). "
                "sf.* keys: 'sf.<ApiName>' (e.g. 'sf.C_Number_of_new_T1D_diagnosed_O_18__c'). "
                "site.* keys: 'site.country' (ISO-2), 'site.city'. "
                "After results you can call render_chart to visualize them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "description": (
                            "FilterGroup: {logic: 'AND'|'OR'|'<expr>', rules: [rule_or_group, ...]}. "
                            "Each element in rules[] is either a leaf rule {field, operator, value} "
                            "or a nested sub-group {logic:'AND'|'OR', rules:[...]}. "
                            "Country values MUST be ISO-2 (e.g. 'ES', 'IT', 'DE', 'FR', 'GB'). "
                            "Simple: {logic:'AND', rules:[{field:'site.country', operator:'equals', value:'ES'}]}. "
                            "Multi-country OR: {logic:'AND', rules:[{logic:'OR', rules:[{field:'site.country',operator:'equals',value:'ES'},{field:'site.country',operator:'equals',value:'IT'}]}, {field:'qual.3_5_2__overnight_stay',operator:'equals',value:'Yes'}]}. "
                            "Grouped: {logic:'OR', rules:[{logic:'AND', rules:[...]}, {logic:'AND', rules:[...]}]}."
                        )
                    },
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of column keys to include (sf.*, qual.*, site.*). Defaults to site name, country, city and patient counts."
                    }
                },
                "required": ["filters"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "explorer_within_drive_km",
            "description": "Find neighboring sites within a driving distance (km) from a base Salesforce Account, using the Explorer service. Use this for 'within X km', 'nearby', 'distance' queries. When NOT to use: for straight-line distance — use nearest_filtered_sites. For general search — use explorer_search.",
            "parameters": {
                "type":"object",
                "properties":{
                    "base_account_id":{"type":"string","description":"Salesforce Account Id for origin. If omitted, use the first account from the last result set."},
                    "max_km":{"type":"number","description":"Max driving distance in km"},
                    "filters":{"type":"object"},
                    "columns":{"type":"array","items":{"type":"string"}}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "nearest_filtered_sites",
            "description": (
                "Find nearest clinical sites to a city/address, optionally filtered by qual.*, sf.*, and extra.* fields. "
                "Returns sites sorted by straight-line distance (km). "
                "Supports ALL filter types in ONE call: qual.* (qualifications), sf.* (Salesforce fields), site.* (country/city), "
                "and extra.* (assignments — use extra.AssignmentsNames not_contains 'X' to exclude sites in a study, "
                "or extra.AssignmentsCount is_empty for sites not in any assignment). "
                "Use when user asks for 'nearest sites to X', 'closest to X', 'sites near X not in Y study'. "
                "IMPORTANT: combine proximity + filters in ONE call — do NOT make separate calls. "
                "When NOT to use: for general site search without proximity — use explorer_search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Reference city/address/landmark, e.g. 'Barcelona', 'Berlin, Germany', 'San Raffaele Hospital Milan'"
                    },
                    "filters": {
                        "type": "object",
                        "description": "Optional FilterGroup {logic:'AND'|'OR', rules:[...]} — same format as explorer_search. Use for qual.* and sf.* field filters."
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "How many nearest sites to return (default 10, max 50)",
                        "default": 10
                    },
                    "max_km": {
                        "type": "number",
                        "description": "Maximum straight-line radius in km (default 1000)",
                        "default": 1000
                    }
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rank_sites",
            "description": (
                "Top-N ranking of sites by a metric. ALWAYS use this for ranking queries. "
                "Returns account_id, site name, country, city, and the metric value. "
                "Works with both SF fields and site_qual keys/aliases (e.g., 'patients under 18', 'newly diagnosed', 'Stage 2'). "
                "When NOT to use: for filtering sites without ranking — use explorer_search."
            ),
            "parameters": {
                "type":"object",
                "properties":{
                    "metric":{"type":"string","description":"Alias or raw key (e.g., 'patients under 18', 'newly diagnosed under 18', 'new T1D <18', 'C_Number_of_T1D_Patients_currently_U_18__c')"},
                    "top_n":{"type":"integer","default":5},
                    "order":{"type":"string","enum":["asc","desc"],"default":"desc"},
                    "group_by":{"type":"string","enum":["country","city"],"description":"If provided, ranks top-N PER GROUP (e.g., top 3 per country). Omit for global ranking."}
                },
                "required":["metric"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"group_count",
            "description": (
                "Count or aggregate sites grouped by country/city. "
                "When to use: site counts per country/city, averages/sums of metrics per group, ratio of sites with a field. "
                "When NOT to use: for listing individual sites — use explorer_search."
            ),
            "parameters":{
                "type":"object",
                "properties":{
                    "by":{"type":"array","items":{"type":"string","enum":["country","city"]}},
                    "where":{"type":"object","description":"Optional site_qual filter for counting."},
                    "aggregation":{"type":"string","enum":["count","sum","avg","ratio"],"default":"count","description":"Type of aggregation. 'count' counts sites, 'sum'/'avg' aggregate a metric, 'ratio' computes ratio_exists."},
                    "metric":{"type":"string","description":"Required when aggregation is sum/avg/ratio. The metric key to aggregate."},
                    "source":{"type":"string","enum":["explorer","salesforce"],"default":"explorer","description":"Data source. 'salesforce' counts SF Accounts directly (SubAccount Clinical, active)."}
                },
                "required":["by"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_activities",
            "description": "List all Activities (Opportunities with RecordType RT_Activity). Returns Activity Name, Sponsor/Account, Activity Id. Use this when user asks for 'list of activities', 'what activities exist', 'show all activities', or 'activities where [company] is the sponsor/account'. Can filter by sponsor/account name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_name_like": {
                        "type": "string",
                        "description": "Filter activities by sponsor or account name (case-insensitive LIKE match). E.g. 'Sanofi' to find activities where the linked account/sponsor contains 'Sanofi'."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activities_with_countries",
            "description": "List Activities with the countries participating in each (via Assignments). Returns Opportunity Name, Account Name, Countries (comma-separated), Site Count. Use this when user asks 'which countries participate in each activity' or 'activities by country' or 'countries per activity'.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activity_counts_by_country",
            "description": "Aggregate number of activities per country (each activity counted once per country). Returns country and activities count.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activity_country_matrix",
            "description": "Per-activity country counts based on Assignment__c. If stacked=true, returns one row per (activity,country) with 'sites' (assignments) for stacked bars; else totals per activity with countries list.",
            "parameters": {
                "type": "object",
                "properties": {
"stacked": {"type": "boolean", "default": False}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activities_sites",
            "description": "List all clinical subaccounts (sites) that participate in any Activity via Assignments. Returns one row per (Site × Activity) with country and city. Use this when user asks for 'sites with activities' or 'activities by country'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "countries": {"type": "array", "items": {"type": "string"}}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activities_by_name",
            "description": "List clinical subaccounts that participate in a specific Activity (partial match by Name).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "countries": {"type": "array", "items": {"type": "string"}},
"exact": {"type": "boolean", "default": False}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sf_aggregate",
            "description": (
                "Aggregate Salesforce Opportunity fields — either grouped by geography or as a time series. "
                "When to use: sum/avg/max of SF fields by country/city, or trends over time (month/quarter/year). "
                "When NOT to use: for site-level data — use explorer_search. For qualification data — use explorer_search with qual.* filters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["aggregate", "time_series"], "default": "aggregate",
                             "description": "'aggregate' groups by country/city; 'time_series' groups by time period."},
                    "field": {"type": "string", "description": "SF Opportunity field API name to aggregate."},
                    "agg": {"type": "string", "enum": ["sum","max","avg"], "default": "avg"},
                    "by": {"type": "array", "items": {"type": "string", "enum": ["country","city"]},
                           "description": "Required for mode=aggregate. Group by country/city."},
                    "date_field": {"type": "string", "default": "CloseDate",
                                   "description": "For mode=time_series: the date field to group by."},
                    "period": {"type": "string", "enum": ["month","quarter","year"], "default": "month",
                               "description": "For mode=time_series: time granularity."},
                    "last_n": {"type": "integer",
                               "description": "For mode=time_series: limit to last N periods."}
                },
                "required": ["field"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activities_with_assignments_counts",
            "description": "List activities with total assignments and participating countries. Supports date filters (last_n_days/last_n_months or since/until). Returns activity_name, assignments, countries, activity_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "last_n_days": {"type": "integer"},
                    "last_n_months": {"type": "integer"},
                    "since": {"type": "string"},
                    "until": {"type": "string"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "activity_assignments_detailed",
            "description": "Detailed assignment rows for activities with optional filters: countries, activity_contains (substring), and date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "countries": {"type": "array", "items": {"type": "string"}},
                    "activity_contains": {"type": "string"},
                    "last_n_days": {"type": "integer"},
                    "last_n_months": {"type": "integer"},
                    "since": {"type": "string"},
                    "until": {"type": "string"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "study_coordinators_with_activities",
            "description": "List Study Coordinators per Account (via AccountContactRelation + Contact.Title) and include activities (Opportunity RT=Activity) per account.",
            "parameters": {
                "type":"object",
                "properties":{
                    "account_ids":{"type":"array","items":{"type":"string"}},
                    "countries":{"type":"array","items":{"type":"string"}},
                    "title_contains":{"type":"string"},
                    "roles":{"type":"array","items":{"type":"string"}},
"include_subaccounts":{"type":"boolean","default":False}
                }
            }
        }
    },

    {
        "type":"function",
        "function":{
            "name":"sql_query_fill_sf",
            "description":"Run SQL (must return account_id) and fill Account.* fields in batch from Salesforce.",
            "parameters":{
                "type":"object",
                "properties":{
                    "sql":{"type":"string"},
                    "account_fields":{"type":"array","items":{"type":"string"}},
                    "params":{"type":"object"}
                },
                "required":["sql","account_fields"]
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"contacts_by_group",
            "description":"Top-N contacts per country/city filtered by roles/title (AccountContactRelation).",
            "parameters":{
                "type":"object",
                "properties":{
                    "roles":{"type":"array","items":{"type":"string"}},
                    "title_contains":{"type":"string"},
                    "group_by":{"type":"string","enum":["country","city"],"default":"country"},
                    "top_n":{"type":"integer","default":1}
                }
            }
        }
    },
    {
        "type":"function",
        "function":{
            "name":"qual_search",
            "description":"Semantic search over qualification comments (GIN tsv).",
            "parameters":{
                "type":"object",
                "properties":{
                    "text":{"type":"string"},
                    "limit":{"type":"integer","default":50}
                },
                "required":["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "members_search",
            "description": (
                "Search INNODIA Member institutions (RecordType.DeveloperName = 'RT_Member') and their network roles, contacts, "
                "and linked SubAccount clinical sites. Use for ANY question about: "
                "member institutions, membership levels, proposed/validated network roles "
                "(CS/DxLab/LAB/CTS/Patient Organization), country leads, board members, "
                "institutional contacts, or sub-accounts linked to a member. "
                "When NOT to use: for clinical trial sites — use explorer_search. "
                "Examples: "
                "'how many member institutions?' → filters={logic:'AND',rules:[]}. "
                "'members in Italy' → filters={logic:'AND',rules:[{field:'site.country',operator:'equals',value:'Italy'}]}. "
                "'members with validated CTS role' → filters={logic:'AND',rules:[{field:'sf.Clinical_Trial_Site_CTS_validated__c',operator:'equals',value:true}]}. "
                "'members with proposed CS' → filters={logic:'AND',rules:[{field:'sf.Clinical_Site_CS__c',operator:'equals',value:true}]}. "
                "Filterable fields: site.country, site.city, sf.C_Level_of_Membership__c, sf.Account_Status__c, "
                "sf.Clinical_Site_CS__c, sf.C_Deliver_Clinical_Grade_Services__c, sf.C_Perform_Cutting_Edge__c, "
                "sf.C_Contribute_as_a_Patient_Organization__c, sf.Clinical_Site_CS_validated__c, "
                "sf.Clinical_Trial_Site_CTS_validated__c, sf.Diagnostic_Lab_DxLab_validated__c, "
                "sf.Research_Mechanistic_Lab_LAB_validated__c, sf.Patient_Organization_validated__c, "
                "extra.SubAccountsCount, extra.ContactsCount."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "description": (
                            "FilterGroup: {logic:'AND'|'OR', rules:[{field, operator, value},...]}. "
                            "Pass {logic:'AND',rules:[]} to return all members."
                        )
                    },
                    "include_detail": {
                        "type": "boolean",
                        "description": "If true, also fetches contacts and SubAccounts for each matched member (slower)."
                    }
                },
                "required": ["filters"]
            }
        }
    },
]
