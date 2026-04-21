# Moby Agentic Loop — Design Spec

**Date:** 2026-04-09
**Status:** Approved
**Goal:** Make Moby a better agent by adding multi-turn tool execution, consolidating tools, and shrinking the deterministic layer.

---

## Problem Statement

Moby's two biggest failure modes:

1. **Claude picks the wrong tool or wrong parameters** — 25 tools with overlapping purposes (especially `explorer_search` vs `salesforce_query` vs `sql_query`), uneven descriptions, and no negative guidance.
2. **No multi-step reasoning** — Claude gets one shot to call tools. Can't chain results (e.g., filter → rank → answer), can't self-correct on bad tool picks, can't inspect results and refine.

Secondary: the deterministic `_try_planner()` layer (~2000 lines, 15+ regex handlers) misroutes queries by matching too aggressively.

---

## Design

### 1. Agentic Loop

Replace the current single-turn tool execution with a bounded loop.

```
User message
  -> _try_planner() (thin: math, charts only)
  -> if no match: Claude Turn 1 (tools available, tool_choice: "auto")
      -> tool_use? Execute tools, send results back to Claude
      -> Claude Turn 2 (tools available, tool_choice: "auto")
      -> tool_use? Execute tools, send results back to Claude
      -> Claude Turn 3 (tool_choice: "none" — must respond with text)
  -> Response to user
```

**Rules:**

- `MOBY_MAX_AGENT_TURNS = 3` (env-configurable). Turns 1-2 allow tools; Turn 3 forces text.
- Each turn can call **multiple tools in parallel** (Claude returns multiple `tool_use` blocks).
- If Claude responds with text (no tool_use) on Turn 1 or 2, the loop exits early — no need to reach Turn 3.
- Streaming applies to the final text turn only.

**What this enables:**

- Multi-step: "Find German sites with overnight stays not in any assignment, ranked by ND" -> Turn 1: explorer_search with FilterGroup -> Turn 2: inspect + refine or rank -> Turn 3: synthesize answer.
- Self-correction: Turn 1 returns 0 results -> Claude tries different tool/params in Turn 2.
- Tool chaining: Turn 1 filters, Turn 2 aggregates, Turn 3 explains.

### 2. Tool Consolidation & Descriptions

**Goal:** 25 tools -> ~18. Eliminate overlap, improve descriptions.

#### Tools to remove or merge

| Current Tool | Action | Rationale |
|---|---|---|
| `sql_query` | **Remove** | `explorer_search` handles `qual.*` queries. Edge-case raw SQL aggregations move to `soql_query`. |
| `salesforce_query` | **Keep, rename to `soql_query`** | Restricted description: "Use ONLY for queries that explorer_search cannot handle — COUNT/GROUP BY aggregations, traversal relationships, fields not in Explorer catalog. Do NOT use for site filtering." |
| `explorer_search` | **Keep as primary** | Promoted description: "DEFAULT tool for any question about sites." |
| `rank_sites` + `rank_sites_by_group` | **Merge** into `rank_sites` | Add optional `group_by` parameter. |
| `group_count` + `group_count_agg` + `group_count_sf` | **Merge** into `group_count` | Add optional `aggregation` and `source` parameters. |
| `group_agg_sf` + `time_series_sf` | **Merge** into `sf_aggregate` | Add `mode` parameter (aggregate vs time_series). |

#### Description format for every tool

```
1. **When to use** — positive instruction (1-2 sentences)
2. **When NOT to use** — explicit anti-patterns pointing to the correct tool
3. **Example** — one concrete input/output pair
```

#### System prompt decision tree

Replace the current 9-block routing prose with:

```
Question about sites? -> explorer_search
  Need aggregation/COUNT? -> soql_query
  Need proximity/distance? -> nearest_filtered_sites or explorer_within_drive_km
  Need contacts/people? -> salesforce_account_contacts
Question about members/institutions? -> members_search
Need a chart? -> render_chart
Need math on last table? -> (deterministic, no Claude)
```

### 3. Shrinking the Deterministic Layer

**Keep** only safe, unambiguous handlers in `_try_planner()`:

| Handler | Status | Why |
|---|---|---|
| Math (sum/avg/min/max on last table) | **Keep** | Pure Python, zero ambiguity |
| Chart from last table | **Keep** | Operates on cached data |
| Conversational no-tools ("thanks", "ok") | **Keep** | No need to invoke Claude |
| Table context injection | **Keep** | System prompt enrichment, not a handler |
| ND, T1D, Stage, country, nearest, activities, HLA, pharmacy, members, pipeline, profiling, sponsors (~12 handlers) | **Remove** | These are misroute sources. Claude with the agentic loop handles them. |

**Where the domain knowledge goes:**

1. **System prompt hints** — e.g., "When user asks about 'newly diagnosed', the relevant fields are `sf.C_Number_of_new_T1D_diagnosed_O_18__c` and `_U_18__c`."
2. **Tool descriptions** — `explorer_search` description includes common field mappings.
3. **`fields_opportunity_curated.json` knowledge index** — already loaded at startup. Becomes the primary way Claude discovers field names.

**Phase 1/2 planning (`moby_planner.py`) becomes hint injection:**

```
[SYSTEM HINT] Detected countries: Germany (DE).
Detected filters: overnight stay (qual.3_5__overnight_accommodation).
Use these as starting points but verify against the user's actual question.
```

Claude can use or ignore these hints. Domain knowledge preserved without hard routing.

### 4. Streaming & UX Integration

#### Progress events during tool execution

New SSE event type `progress`:

```
event: progress
data: {"turn": 1, "tool": "explorer_search", "status": "calling", "summary": "Searching for German sites with overnight stays..."}

event: progress
data: {"turn": 1, "tool": "explorer_search", "status": "done", "rows": 23}

event: progress
data: {"turn": 2, "tool": "soql_query", "status": "calling", "summary": "Checking assignment status..."}

event: text
data: {"delta": "I found 14 sites in Germany..."}
```

#### Frontend changes (ChatView.tsx)

- Compact status line during tool execution: `"Searching sites... -> Checking assignments... -> Composing answer..."`
- Live cursor (`cursor`) appears only during final text stream
- Tool steps are NOT shown as separate chat bubbles — just the status line
- ActionableTable / Explorer integration unchanged (`last_table` -> highlight/filter/add-columns)

### 5. Error Handling & Safety

#### Loop termination guarantees

- **Hard cap:** `MOBY_MAX_AGENT_TURNS = 3`
- **Token budget:** Tool results truncated to `MAX_TOOL_RESULT_TOKENS = 4000` per tool call
- **Timeout:** `MOBY_AGENT_TIMEOUT_S = 30` wall-clock cap. If exceeded, return whatever Claude has + "I ran out of time" note.
- **Tool errors:** Send exception message back to Claude as `tool_result`. Claude decides whether to retry differently or explain the failure. (Today errors are silently swallowed.)

#### Self-correction guardrails

- **Empty result detection:** Claude sees 0-row results and can retry with relaxed filters.
- **No duplicate calls:** Same tool_name + params hash cannot be called twice in the same user message.
- **Read-only:** All tools remain read-only. No writes to SF or DB.

#### Distance Matrix cost protection

- Inherits existing `MAX_DISTANCE_MATRIX_ELEMENTS = 2000` guard rail
- `explorer_within_drive_km` and `nearest_filtered_sites` keep haversine pre-filter + top-N cap
- DM tools can be called at most once per user message (enforced in loop)

#### Fallback

If all turns produce no usable result: "I wasn't able to answer that. Could you rephrase or break it into smaller questions?"

---

## Files Affected

| File | Changes |
|---|---|
| `backend/app/routers/ai_chat.py` | Agentic loop in `chat_api()`, tool dispatch refactor, `_try_planner()` shrink, progress SSE events |
| `backend/app/routers/moby_handlers.py` | Remove handlers that move to Claude; keep only if reused by agentic loop |
| `backend/app/routers/moby_planner.py` | Convert from execution to hint injection |
| `frontend/src/components/ChatView.tsx` | Handle `progress` SSE events, show status line |
| `backend/app/routers/ai_chat.py` (TOOLS_SPEC) | Merge tools, rewrite descriptions, add decision tree to system prompt |

## Configuration

| Env Var | Default | Purpose |
|---|---|---|
| `MOBY_MAX_AGENT_TURNS` | `3` | Max Claude turns per user message |
| `MAX_TOOL_RESULT_TOKENS` | `4000` | Truncation limit per tool result |
| `MOBY_AGENT_TIMEOUT_S` | `30` | Wall-clock timeout for full agentic loop |

## Expected Outcomes

- Multi-step questions work (2-3 tool chains)
- Claude self-corrects on wrong tool picks
- Fewer misroutes from aggressive regex handlers
- ~3-5s slower on complex queries (extra turns), but faster on previously-misrouted queries
- Deterministic handlers still fast for math/charts
- Tool count reduced from 25 to ~18
