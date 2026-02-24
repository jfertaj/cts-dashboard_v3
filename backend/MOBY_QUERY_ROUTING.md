# Moby Query Routing Matrix

This document specifies the correct data source (Salesforce vs site_qual) for each type of query.

## Bloque 1: Búsquedas básicas y agregaciones

| Query | Data Source | Tool/Method | Notes |
|-------|-------------|-------------|-------|
| "How many sites do we have in total?" | **Salesforce** | `salesforce_query` | COUNT(DISTINCT Account.Id) FROM Opportunity |
| "Show me all sites in Spain" | **Salesforce** | `salesforce_query` | WHERE Account.ShippingCountry = 'Spain' |
| "How many sites per country?" | **Salesforce** | `salesforce_query` | GROUP BY Account.ShippingCountry |
| "What is the average number of T1D patients under 18 per site?" | **Salesforce** | `salesforce_query` | AVG(C_Number_of_T1D_Patients_currently_U_18__c) |

## Bloque 2: Rankings y Top N

| Query | Data Source | Tool/Method | Notes |
|-------|-------------|-------------|-------|
| "Show me the top 5 sites with most T1D patients under 18" | **Salesforce** | `rank_sites` | metric='T1D patients under 18', top_n=5 |
| "Top 3 sites per country by Stage 2" | **Salesforce** | `rank_sites_by_group` | metric='Stage 2', group_by='country', top_n=3 |
| "Which site has the lowest number of T1D patients over 18?" | **Salesforce** | `rank_sites` | metric='T1D patients over 18', top_n=1, order='asc' |

## Bloque 3: Búsqueda textual (qualification comments)

| Query | Data Source | Tool/Method | Notes |
|-------|-------------|-------------|-------|
| "Search qualification comments for: power outage plan" | **site_qual** | `qual_search` | text='power outage plan' |
| "Find sites that mention pharmacy in their comments" | **site_qual** | `qual_search` | text='pharmacy' |
| "Which sites have overnight stay facilities?" | **site_qual** | `sql_query` | data->>'3.5.2__overnight_stay' FROM site_qual |

## Bloque 4: Filtros condicionales y porcentajes

| Query | Data Source | Tool/Method | Notes |
|-------|-------------|-------------|-------|
| "% of sites per country with HLA typing" | **Salesforce** | `salesforce_query` | C_Is_HLA_typing_performed__c, GROUP BY country |
| "Show me sites in Germany with more than 10 new T1D diagnoses per year" | **Salesforce** | `salesforce_query` | C_Number_of_new_T1D_diagnosed_U_18__c + O_18__c > 10 |
| "How many sites have on-site pharmacy?" | **site_qual** | `sql_query` | WHERE data->>'onsite_pharmacy' = 'yes' |

## Bloque 5: Series temporales (Salesforce Opportunities)

| Query | Data Source | Tool/Method | Notes |
|-------|-------------|-------------|-------|
| "Time series (quarter) of Opportunity Amount last 8 quarters" | **Salesforce** | `time_series_sf` | field='Amount', period='quarter', last_n=8 |
| "Show me the trend of opportunity amounts by month for the last 6 months" | **Salesforce** | `time_series_sf` | field='Amount', period='month', last_n=6 |

## Bloque 6: Contactos y roles (Salesforce)

| Query | Data Source | Tool/Method | Notes |
|-------|-------------|-------------|-------|
| "Who is the PI for [site name]?" | **Salesforce** | `salesforce_account_extras` | First resolve site name to Account.Id |
| "Find all Study Coordinators in Belgium" | **Salesforce** | `salesforce_account_contacts` | title_contains='Study Coordinator', filter by country |
| "List contacts with PI role for sites in France" | **Salesforce** | `salesforce_account_contacts` | roles=['PI'], filter by country |

## Bloque 7: Follow-ups y contexto

| Query | Data Source | Tool/Method | Notes |
|-------|-------------|-------------|-------|
| "Show me the top 5 sites with most patients" | **Salesforce** | `rank_sites` | Sum of current U18 + O18 |
| → "Of those, which ones have HLA typing?" | **Salesforce** | `salesforce_query` | Filter previous results by C_Is_HLA_typing_performed__c |
| "Sites in Italy" | **Salesforce** | `salesforce_query` | WHERE Account.ShippingCountry = 'Italy' |
| → "Among those, show me only the ones with more than 5 new diagnoses per year" | **Salesforce** | `salesforce_query` | Filter by new_diagnosed_U_18 + O_18 > 5 |

## Bloque 8: Visualizaciones y gráficos

| Query | Data Source | Tool/Method | Notes |
|-------|-------------|-------------|-------|
| "Show me the top 5 sites with most patients per year in a table and bar chart" | **Salesforce** | `rank_sites` + `render_chart` | Auto-generate bar chart from table |
| "Create a bar chart of sites per country" | **Salesforce** | `salesforce_query` + `render_chart` | GROUP BY country, COUNT(*) |
| "Compare T1D patients under 18 vs over 18 for the top 5 sites" | **Salesforce** | `rank_sites` + `render_chart` | Include both U18 and O18 columns |

## Bloque 9: Queries complejas combinadas

| Query | Data Source | Tool/Method | Notes |
|-------|-------------|-------------|-------|
| "Find sites in Spain or France with HLA typing and more than 10 patients under 18" | **Salesforce** | `salesforce_query` | WHERE country IN ('Spain','France') AND HLA='Yes' AND U18>10 |
| "Which countries have the highest average Stage 2 values?" | **Salesforce** | `salesforce_query` | AVG(C_Number_of_Stage2_Individuals_followed__c) GROUP BY country |
| "Show me clinical trial sites with on-site pharmacy grouped by country" | **Both** | `sql_query` JOIN | Join Salesforce Account data with site_qual pharmacy data |

## Key Rules

### ALWAYS use Salesforce for:
- Site counts and lists
- Patient metrics (current, newly diagnosed, all ages)
- Stage 1/2 individuals followed
- HLA typing
- Screening program participation
- Time series on Opportunities
- Contact information (PI, SC, Study Nurse)
- Country/City geographic queries

### ONLY use site_qual for:
- Qualification checklist questions (overnight stay, pharmacy, examination rooms, etc.)
- Questions with keys like "3.x__..." format
- Qualification comments search
- Detailed facility information not in Salesforce

### When to use both:
- Complex queries that need both Salesforce account data AND qualification details
- Use JOIN between sites.salesforce_account_id and Salesforce Account.Id
