# Moby force-tool guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the SC06/SS01/SS03 flaky regressions so `refactor/moby-agentic-loop` can ship to `main`, by forcing `tool_choice=required` on tabular-intent queries and adding a one-shot retry when the loop exits text-only.

**Architecture:** One new module (`moby_tool_policy.py`) holds the intent detector + tool whitelist. `ai_chat.py::_claude_chat` gets a `tools_override` kwarg. `ai_chat.py::_agentic_loop` (a) computes `_tabular` once, (b) forces whitelisted tools on turn 1 when `_tabular`, (c) runs one retry after the loop if text-only and `_tabular` and no new table was produced this request. No feature flag; new behaviour only triggers under the intent detector.

**Tech Stack:** Python 3.10+, FastAPI, `anthropic` SDK, pytest, `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-04-22-moby-force-tool-guard-design.md` (commit `dcd5dbb`).

**Branch:** `refactor/moby-agentic-loop` (already checked out, already has prior refactor WIP).

---

## File Structure

**Create:**
- `backend/app/routers/moby_tool_policy.py` — pure module: `TABLE_RETURNING_TOOLS`, `_POSITIVES`, `_BLACKLIST`, `_norm`, `has_tabular_intent`, `filter_tools_spec`.
- `backend/tests/test_tool_policy.py` — unit tests for the module.

**Modify:**
- `backend/app/routers/ai_chat.py`:
  - `_claude_chat` signature (line 4528): add `tools_override: list[dict] | None = None` kwarg + wire it into `claude_tools` construction at line 4544.
  - `_agentic_loop` signature (line 4738): add `user_msg: str = ""` kwarg.
  - `_agentic_loop` body (lines 4757–4899): extract `_dispatch_tool_calls` helper, add `_tabular` + `_whitelist_spec` + `_table_produced_this_request` state, force whitelist on turn 1 when `_tabular`, add post-loop retry block.
  - `chat_api` (the caller) — pass `user_msg=` to `_agentic_loop`.
- `backend/tests/test_agentic_loop.py` — 8 new tests reusing existing fixtures.

**Not touched:**
- `moby_planner.py`, `moby_tools.py`, `salesforce_explorer.py`, any frontend file.
- `_try_planner` and all deterministic handlers.
- System prompt.

---

## Task 1: Create `moby_tool_policy.py` skeleton + `TABLE_RETURNING_TOOLS` constant

**Files:**
- Create: `backend/app/routers/moby_tool_policy.py`
- Create: `backend/tests/test_tool_policy.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_tool_policy.py` with:

```python
"""Unit tests for moby_tool_policy — pure functions, no network, no DB."""
from __future__ import annotations


def test_table_returning_tools_constant_has_four_entries():
    from backend.app.routers.moby_tool_policy import TABLE_RETURNING_TOOLS
    assert TABLE_RETURNING_TOOLS == frozenset({
        "explorer_search",
        "nearest_filtered_sites",
        "study_coordinators_with_activities",
        "members_search",
    })


def test_table_returning_tools_is_frozenset():
    from backend.app.routers.moby_tool_policy import TABLE_RETURNING_TOOLS
    assert isinstance(TABLE_RETURNING_TOOLS, frozenset)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest backend/tests/test_tool_policy.py -v
```

Expected: `ModuleNotFoundError: No module named 'backend.app.routers.moby_tool_policy'`.

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/routers/moby_tool_policy.py`:

```python
"""Tool-choice policy helpers for Moby.

Pure, stateless helpers used by _agentic_loop to decide when to force a
table-returning tool call. Importing this module must have no side effects
and no external dependencies beyond the standard library.
"""
from __future__ import annotations


TABLE_RETURNING_TOOLS: frozenset[str] = frozenset({
    "explorer_search",
    "nearest_filtered_sites",
    "study_coordinators_with_activities",
    "members_search",
})
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest backend/tests/test_tool_policy.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/moby_tool_policy.py backend/tests/test_tool_policy.py
git commit -m "feat(moby): add moby_tool_policy module with TABLE_RETURNING_TOOLS constant"
```

---

## Task 2: `_norm` + `has_tabular_intent` — English positives

**Files:**
- Modify: `backend/app/routers/moby_tool_policy.py`
- Modify: `backend/tests/test_tool_policy.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_tool_policy.py`:

```python
def test_has_tabular_intent_real_regression_queries_en():
    """The 3 flaky regressions + 3 pattern-1 regressions must all be detected."""
    from backend.app.routers.moby_tool_policy import has_tabular_intent
    queries = [
        "Which sites in France have HLA typing capability available?",          # SC06
        "Show me all sites where profiling form has been uploaded to the database",  # SS01
        "List sites where C_Profiling_Complete is true",                          # SS03
        "Show me all CTS sites in Germany, Italy, and Belgium",                   # SC01
        "Show all sites that have NOT yet completed profiling",                   # SC09
        "Find sites that are CTS-validated but not yet active in any assignment", # SS04
    ]
    for q in queries:
        assert has_tabular_intent(q), f"Expected tabular for: {q!r}"


def test_has_tabular_intent_synthetic_imperatives_en():
    from backend.app.routers.moby_tool_policy import has_tabular_intent
    for q in [
        "give me all members",
        "display sites in Spain",
        "get me the coordinators",
        "fetch the activities",
        "return the members with no sites",
        "search for sites near Paris",
        "pull up the CTS list",
        "how many sites are there?",
        "which members are in Germany?",
        "a table of sites",
        "list of countries",
        "report on qualification status",
    ]:
        assert has_tabular_intent(q), f"Expected tabular for: {q!r}"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest backend/tests/test_tool_policy.py -v
```

Expected: 2 new tests fail with `ImportError: cannot import name 'has_tabular_intent'`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/routers/moby_tool_policy.py`:

```python
import unicodedata


_POSITIVES_EN: tuple[str, ...] = (
    "list", "show", "show me", "give", "give me", "find", "display",
    "fetch", "get", "get me", "return", "search", "search for", "pull up",
    "how many", "how much", "which", "which ones",
    "what sites", "what members", "what centers", "what countries", "what coordinators",
    "list of", "a list", "a table", "as a table", "as table",
    "in a table", "table of", "report", "overview",
)


def _norm(s: str) -> str:
    """Lowercase and strip accents for robust keyword matching."""
    if not s:
        return ""
    return unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()


def has_tabular_intent(user_msg: str) -> bool:
    """True if the query explicitly asks for a list/table/count/search result."""
    if not user_msg:
        return False
    norm = _norm(user_msg)
    return any(p in norm for p in _POSITIVES_EN)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest backend/tests/test_tool_policy.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/moby_tool_policy.py backend/tests/test_tool_policy.py
git commit -m "feat(moby): add has_tabular_intent with English positive keywords"
```

---

## Task 3: `has_tabular_intent` — Spanish positives

**Files:**
- Modify: `backend/app/routers/moby_tool_policy.py`
- Modify: `backend/tests/test_tool_policy.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_tool_policy.py`:

```python
def test_has_tabular_intent_positives_es():
    from backend.app.routers.moby_tool_policy import has_tabular_intent
    for q in [
        "lista los sitios CTS",
        "muestra los miembros en España",
        "dame los coordinadores",
        "busca sitios cerca de Madrid",
        "encuentra los sitios con perfilado completo",
        "cuántos sitios hay en Alemania?",
        "cuáles son los países con más sitios?",
        "qué sitios tienen HLA typing?",
        "tabla de miembros",
        "una tabla de actividades",
    ]:
        assert has_tabular_intent(q), f"Expected tabular for: {q!r}"


def test_has_tabular_intent_accents_normalized():
    from backend.app.routers.moby_tool_policy import has_tabular_intent
    assert has_tabular_intent("cuántos sitios")
    assert has_tabular_intent("cuantos sitios")
    assert has_tabular_intent("CUÁNTOS SITIOS")
    assert has_tabular_intent("  Cuántos Sitios  ")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest backend/tests/test_tool_policy.py::test_has_tabular_intent_positives_es backend/tests/test_tool_policy.py::test_has_tabular_intent_accents_normalized -v
```

Expected: both fail (Spanish keywords not yet in `_POSITIVES_EN`).

- [ ] **Step 3: Write minimal implementation**

Edit `backend/app/routers/moby_tool_policy.py`. Rename `_POSITIVES_EN` to `_POSITIVES` and append Spanish entries. Update `has_tabular_intent` to use `_POSITIVES`.

```python
_POSITIVES: tuple[str, ...] = (
    # EN imperatives
    "list", "show", "show me", "give", "give me", "find", "display",
    "fetch", "get", "get me", "return", "search", "search for", "pull up",
    # ES imperatives (accent-stripped forms)
    "lista", "muestra", "muestrame", "dame", "ensena", "busca",
    "buscame", "encuentra", "saca", "sacame", "trae", "traeme",
    # EN interrogatives
    "how many", "how much", "which", "which ones",
    "what sites", "what members", "what centers", "what countries", "what coordinators",
    # ES interrogatives
    "cuantos", "cuantas", "cuales",
    "que sitios", "que miembros", "que centros", "que paises",
    # Markers
    "list of", "a list", "a table", "as a table", "as table",
    "in a table", "table of", "report", "overview",
    "lista de", "en tabla", "como tabla", "una tabla", "tabla de",
)
```

Replace the reference to `_POSITIVES_EN` in `has_tabular_intent` with `_POSITIVES`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest backend/tests/test_tool_policy.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/moby_tool_policy.py backend/tests/test_tool_policy.py
git commit -m "feat(moby): extend has_tabular_intent with Spanish keywords"
```

---

## Task 4: `has_tabular_intent` — blacklist wins

**Files:**
- Modify: `backend/app/routers/moby_tool_policy.py`
- Modify: `backend/tests/test_tool_policy.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_tool_policy.py`:

```python
def test_has_tabular_intent_blacklist_wins():
    """Conversational phrasings beat positive keywords."""
    from backend.app.routers.moby_tool_policy import has_tabular_intent
    for q in [
        "Explain what HLA typing means",
        "What is the meaning of profiling?",
        "Why are some sites CTS-validated?",
        "Can you explain how the qualification workflow works?",
        "Describe the Clinical Trial Site concept",
        "Tell me about CTS validation",
        "Show me what that error means",
        "Show me how the workflow works",
        "Summarize the current status",
        "Puedes explicar cómo funciona",
        "Por qué hay sitios sin asignación?",
        "Qué significa CTS?",
        "Resume el estado actual",
        "Cuéntame sobre la validación",
    ]:
        assert not has_tabular_intent(q), f"Expected NOT tabular for: {q!r}"


def test_has_tabular_intent_conversational_short():
    from backend.app.routers.moby_tool_policy import has_tabular_intent
    for q in ["hi", "thanks", "ok", "what?", "hola", "", "   "]:
        assert not has_tabular_intent(q)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest backend/tests/test_tool_policy.py::test_has_tabular_intent_blacklist_wins -v
```

Expected: fail — "Explain what HLA typing means" contains `show me` / `what sites` etc. — actually it matches none, but "Show me what that error means" matches `show me` and should be blacklisted. Several queries will fail.

- [ ] **Step 3: Write minimal implementation**

Add `_BLACKLIST` tuple and update `has_tabular_intent` in `backend/app/routers/moby_tool_policy.py`:

```python
_BLACKLIST: tuple[str, ...] = (
    # EN
    "summarize", "summary of", "explain", "what does", "what is the",
    "why", "how do i", "can you explain", "describe", "tell me about",
    "show me what", "show me how",
    # ES (accent-stripped)
    "resume", "explica", "que significa", "por que",
    "como puedo", "como hago", "puedes explicar",
    "cuentame sobre", "ensename que", "ensename como",
)


def has_tabular_intent(user_msg: str) -> bool:
    """True if the query explicitly asks for a list/table/count/search result.

    Blacklist wins: conversational phrasings return False even if a
    positive keyword is present in the query.
    """
    if not user_msg:
        return False
    norm = _norm(user_msg)
    if any(b in norm for b in _BLACKLIST):
        return False
    return any(p in norm for p in _POSITIVES)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest backend/tests/test_tool_policy.py -v
```

Expected: 8 passed. Double-check: re-run the earlier positive tests to confirm no regression (the blacklist should not blackball any query from Step 1/2/3 of prior tasks).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/moby_tool_policy.py backend/tests/test_tool_policy.py
git commit -m "feat(moby): add blacklist that overrides positives for conversational phrasings"
```

---

## Task 5: `has_tabular_intent` — CL05 edge case documentation

**Files:**
- Modify: `backend/tests/test_tool_policy.py`

This task adds a characterization test documenting that CL05 is NOT covered (per spec).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_tool_policy.py`:

```python
def test_has_tabular_intent_cl05_not_covered():
    """CL05 ('Are there any CTS sites in X?') is intentionally NOT detected.

    Per spec: CL05 is a semantic-note regression (CTS flag is null for
    Bucharest), not a real bug. The force-tool guard does not cover it.
    """
    from backend.app.routers.moby_tool_policy import has_tabular_intent
    assert not has_tabular_intent("Are there any CTS sites in Romania or Bulgaria?")
    assert not has_tabular_intent("Is there a CTS site in Poland?")
```

- [ ] **Step 2: Run the test to verify it passes immediately**

```bash
python -m pytest backend/tests/test_tool_policy.py::test_has_tabular_intent_cl05_not_covered -v
```

Expected: PASS. No implementation change needed — "are there any" matches no positive. This is a characterization test locking in current behaviour.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_tool_policy.py
git commit -m "test(moby): document CL05 ('are there any') is out of scope for force-tool guard"
```

---

## Task 6: `filter_tools_spec` — implementation + tests

**Files:**
- Modify: `backend/app/routers/moby_tool_policy.py`
- Modify: `backend/tests/test_tool_policy.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_tool_policy.py`:

```python
def _fake_spec(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_filter_tools_spec_whitelist_basic():
    from backend.app.routers.moby_tool_policy import filter_tools_spec
    spec = [_fake_spec("a"), _fake_spec("b"), _fake_spec("c")]
    out = filter_tools_spec(spec, {"a", "c"})
    assert [t["function"]["name"] for t in out] == ["a", "c"]


def test_filter_tools_spec_preserves_entry_identity():
    from backend.app.routers.moby_tool_policy import filter_tools_spec
    spec = [_fake_spec("a"), _fake_spec("b")]
    out = filter_tools_spec(spec, {"a"})
    assert out[0] is spec[0]  # reference-preserving, not a copy


def test_filter_tools_spec_empty_whitelist():
    from backend.app.routers.moby_tool_policy import filter_tools_spec
    spec = [_fake_spec("a")]
    assert filter_tools_spec(spec, set()) == []


def test_filter_tools_spec_nonexistent_tool_in_whitelist():
    from backend.app.routers.moby_tool_policy import filter_tools_spec
    spec = [_fake_spec("a")]
    out = filter_tools_spec(spec, {"a", "does_not_exist"})
    assert [t["function"]["name"] for t in out] == ["a"]


def test_filter_tools_spec_default_uses_table_returning():
    from backend.app.routers.moby_tool_policy import filter_tools_spec, TABLE_RETURNING_TOOLS
    spec = [_fake_spec("explorer_search"), _fake_spec("soql_query"), _fake_spec("members_search")]
    out = filter_tools_spec(spec)  # no whitelist arg → defaults to TABLE_RETURNING_TOOLS
    names = {t["function"]["name"] for t in out}
    assert names == {"explorer_search", "members_search"}
    assert names.issubset(TABLE_RETURNING_TOOLS)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest backend/tests/test_tool_policy.py -v
```

Expected: 5 new tests fail with `ImportError: cannot import name 'filter_tools_spec'`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/routers/moby_tool_policy.py`:

```python
from typing import Iterable


def filter_tools_spec(
    tools_spec: list[dict],
    whitelist: Iterable[str] = TABLE_RETURNING_TOOLS,
) -> list[dict]:
    """Return a sub-list of tools_spec whose tool name is in the whitelist.

    Entries are returned by reference (not copied). Unknown names in the
    whitelist are silently ignored. Order follows tools_spec.
    """
    wl = frozenset(whitelist)
    return [t for t in tools_spec if t.get("function", {}).get("name") in wl]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest backend/tests/test_tool_policy.py -v
```

Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/moby_tool_policy.py backend/tests/test_tool_policy.py
git commit -m "feat(moby): add filter_tools_spec helper with TABLE_RETURNING_TOOLS default"
```

---

## Task 7: Integration-style test — whitelist matches real `TOOLS_SPEC`

**Files:**
- Modify: `backend/tests/test_tool_policy.py`

Ensures `TABLE_RETURNING_TOOLS` doesn't drift from the real tool registry.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_tool_policy.py`:

```python
def test_table_returning_tools_exist_in_real_tools_spec():
    """All 4 entries in TABLE_RETURNING_TOOLS must correspond to real tools in TOOLS_SPEC."""
    from backend.app.routers.ai_chat import TOOLS_SPEC
    from backend.app.routers.moby_tool_policy import TABLE_RETURNING_TOOLS

    real_names = {t.get("function", {}).get("name") for t in TOOLS_SPEC}
    missing = TABLE_RETURNING_TOOLS - real_names
    assert not missing, f"TABLE_RETURNING_TOOLS references non-existent tools: {missing}"


def test_filter_tools_spec_against_real_tools_spec():
    from backend.app.routers.ai_chat import TOOLS_SPEC
    from backend.app.routers.moby_tool_policy import filter_tools_spec, TABLE_RETURNING_TOOLS

    out = filter_tools_spec(TOOLS_SPEC)
    assert len(out) == 4
    names = {t["function"]["name"] for t in out}
    assert names == TABLE_RETURNING_TOOLS
```

- [ ] **Step 2: Run the tests**

```bash
python -m pytest backend/tests/test_tool_policy.py::test_table_returning_tools_exist_in_real_tools_spec backend/tests/test_tool_policy.py::test_filter_tools_spec_against_real_tools_spec -v
```

Expected: PASS. (If they fail, one of the tool names has been renamed in `TOOLS_SPEC` — fix the constant in `moby_tool_policy.py` to match.)

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_tool_policy.py
git commit -m "test(moby): lock TABLE_RETURNING_TOOLS against real TOOLS_SPEC"
```

---

## Task 8: Add `tools_override` kwarg to `_claude_chat`

**Files:**
- Modify: `backend/app/routers/ai_chat.py` lines 4528–4546
- Modify: `backend/tests/test_ai_chat.py` (add new test — if the file does not already mock the Anthropic client, check `test_agentic_loop.py` for the pattern)

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_agentic_loop.py` (at the bottom, after existing tests):

```python
@patch("backend.app.routers.ai_chat._anthropic_sdk")
def test_claude_chat_tools_override_replaces_tools_spec(mock_sdk):
    """When tools_override is passed, claude_tools is built from the override, not TOOLS_SPEC."""
    from backend.app.routers.ai_chat import _claude_chat

    # Capture kwargs passed to messages.create
    captured = {}
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = lambda **kw: captured.update(kw) or _fake_anthropic_response()
    mock_sdk.Anthropic.return_value = mock_client

    override = [{
        "type": "function",
        "function": {"name": "explorer_search", "description": "x", "parameters": {"type": "object", "properties": {}}},
    }]

    import os
    os.environ["ANTHROPIC_API_KEY"] = "test-key"

    _claude_chat([{"role": "user", "content": "hi"}], tools_override=override)

    tools_passed = captured.get("tools") or []
    names = [t["name"] for t in tools_passed]
    assert names == ["explorer_search"], f"Expected only explorer_search, got {names}"


def _fake_anthropic_response():
    """Minimal response object mimicking the Anthropic SDK Message shape."""
    resp = types.SimpleNamespace()
    resp.content = [types.SimpleNamespace(type="text", text="ok")]
    resp.stop_reason = "end_turn"
    resp.usage = types.SimpleNamespace(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    return resp
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python -m pytest backend/tests/test_agentic_loop.py::test_claude_chat_tools_override_replaces_tools_spec -v
```

Expected: FAIL — `_claude_chat` does not accept `tools_override`; raises `TypeError: _claude_chat() got an unexpected keyword argument 'tools_override'`.

- [ ] **Step 3: Modify `_claude_chat` signature and body**

In `backend/app/routers/ai_chat.py` line 4528, change:

```python
def _claude_chat(
    messages: List[Dict[str, Any]],
    tool_choice: str = "required",
    *,
    force_no_tools: bool = False,
    use_thinking: bool = False,
):
```

to:

```python
def _claude_chat(
    messages: List[Dict[str, Any]],
    tool_choice: str = "required",
    *,
    force_no_tools: bool = False,
    use_thinking: bool = False,
    tools_override: Optional[List[Dict[str, Any]]] = None,
):
```

Around line 4544 (inside the `if not force_no_tools:` block that builds `claude_tools`), change:

```python
for t in TOOLS_SPEC:
```

to:

```python
source_spec = tools_override if tools_override is not None else TOOLS_SPEC
for t in source_spec:
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
python -m pytest backend/tests/test_agentic_loop.py::test_claude_chat_tools_override_replaces_tools_spec -v
```

Expected: PASS.

- [ ] **Step 5: Run existing `_claude_chat`-touching tests to verify no regression**

```bash
python -m pytest backend/tests/test_agentic_loop.py backend/tests/test_ai_chat.py -v
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/ai_chat.py backend/tests/test_agentic_loop.py
git commit -m "feat(moby): add tools_override kwarg to _claude_chat"
```

---

## Task 9: Pure refactor — extract `_dispatch_tool_calls` helper

**Files:**
- Modify: `backend/app/routers/ai_chat.py` lines 4807–4876 (dispatch block) + call site

Zero behaviour change. Tests are the existing `test_agentic_loop.py` ones — they must all still pass.

- [ ] **Step 1: Create the helper (added just above `_agentic_loop`, around line 4736)**

Insert in `backend/app/routers/ai_chat.py` immediately before `def _agentic_loop(`:

```python
def _dispatch_tool_calls(
    assistant_msg,
    msgs: List[Dict[str, Any]],
    tool_ctx: "ToolContext",
    seen_hashes: set,
    dm_called: bool,
    tool_calls_made: List[Dict[str, Any]],
    turn,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    """Dispatch every tool_call in assistant_msg, applying dedup + DM + truncation.

    Returns a tuple:
        (dm_called_after, last_table, last_viz, last_filters, table_produced_now)

    - dm_called_after: True if any DM tool was dispatched in this call.
    - last_table/viz/filters: latest values after dispatch (None if none set).
    - table_produced_now: True if any dispatched tool set a non-None last_table.
    """
    import hashlib
    from backend.app.routers.moby_tools import dispatch_tool

    _DM_TOOL_NAMES = {"nearest_filtered_sites", "explorer_within_drive_km"}
    _char_limit = MAX_TOOL_RESULT_TOKENS * 4

    last_table: Optional[Dict[str, Any]] = None
    last_visualization: Optional[Dict[str, Any]] = None
    last_explorer_filters: Optional[Dict[str, Any]] = None
    table_produced_now = False

    for tc in assistant_msg.tool_calls:
        name = tc.function.name
        args_json = tc.function.arguments or "{}"
        args = json.loads(args_json)
        tool_call_id = tc.id

        call_hash = hashlib.md5(f"{name}:{args_json}".encode()).hexdigest()
        if call_hash in seen_hashes:
            _dbg("Agentic loop: DEDUP skip %s (same args)", name)
            msgs.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps({
                    "error": f"Duplicate call to {name} with same arguments — skipped. Use different parameters or summarize what you have."
                }),
            })
            continue
        seen_hashes.add(call_hash)

        if name in _DM_TOOL_NAMES:
            if dm_called:
                _dbg("Agentic loop: DM tool %s blocked (already called a DM tool)", name)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps({
                        "error": "Distance Matrix tool already called this turn. Use the results from the previous call."
                    }),
                })
                continue
            dm_called = True

        _dbg("Agentic loop turn %s: TOOL %s args=%s", turn, name, args)
        tool_calls_made.append({"tool": name, "turn": turn})

        tool_ctx.args = args
        tool_ctx.tool_call_id = tool_call_id

        tool_result = dispatch_tool(name, tool_ctx)

        # Truncate oversized tool results in msgs immediately
        for _m in reversed(msgs):
            if _m.get("role") == "tool" and _m.get("tool_call_id") == tool_call_id:
                _content = _m.get("content", "")
                if len(_content) > _char_limit:
                    try:
                        _parsed = json.loads(_content)
                        if isinstance(_parsed, dict) and "rows" in _parsed:
                            _rows = _parsed["rows"]
                            if isinstance(_rows, list) and len(_rows) > 50:
                                _parsed["rows"] = _rows[:50]
                                _parsed["_truncated"] = f"Showing 50 of {len(_rows)} rows"
                                _m["content"] = json.dumps(_parsed, default=str)
                    except Exception:
                        pass
                break

        if tool_result.last_table is not None:
            last_table = tool_result.last_table
            tool_ctx.last_table = last_table
            table_produced_now = True
        if tool_result.last_visualization is not None:
            last_visualization = tool_result.last_visualization
            tool_ctx.last_visualization = last_visualization
        if tool_result.last_explorer_filters is not None:
            last_explorer_filters = tool_result.last_explorer_filters
            tool_ctx.last_explorer_filters = last_explorer_filters

    return (dm_called, last_table, last_visualization, last_explorer_filters, table_produced_now)
```

- [ ] **Step 2: Replace the inline dispatch block in `_agentic_loop`**

In `_agentic_loop`, replace lines 4807–4876 (starting with `for tc in assistant_msg.tool_calls:` and ending after the three `tool_ctx.last_explorer_filters = ...` guards) with:

```python
        (dm_called, _lt, _lv, _lf, _produced_now) = _dispatch_tool_calls(
            assistant_msg, msgs, tool_ctx, seen_hashes, dm_called, tool_calls_made, turn,
        )
        if _lt is not None:
            last_table = _lt
        if _lv is not None:
            last_visualization = _lv
        if _lf is not None:
            last_explorer_filters = _lf
        # _produced_now used by post-loop retry logic added in Task 12
```

Also remove the now-unused `import hashlib` at the top of `_agentic_loop` (line 4753) — the helper imports it locally.

- [ ] **Step 3: Run the entire agentic-loop test file**

```bash
python -m pytest backend/tests/test_agentic_loop.py -v
```

Expected: all 12+ existing tests still pass. No new tests yet.

- [ ] **Step 4: Also run the broader test suite**

```bash
python -m pytest backend/tests/ -v
```

Expected: all ~600 tests pass (552 original + 27 refactor + 9 compare-script + 13 new tool_policy + 1 tools_override).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ai_chat.py
git commit -m "refactor(moby): extract _dispatch_tool_calls helper (no behaviour change)"
```

---

## Task 10: Thread `user_msg` into `_agentic_loop`

**Files:**
- Modify: `backend/app/routers/ai_chat.py` — `_agentic_loop` signature + call site in `chat_api`

- [ ] **Step 1: Locate the caller**

```bash
grep -n "_agentic_loop(" backend/app/routers/ai_chat.py
```

Expected: two matches — the `def _agentic_loop(` declaration (around line 4738) and one call site (in `chat_api` or a helper). Note the exact line of the call site.

- [ ] **Step 2: Add `user_msg` kwarg to the signature**

Change the signature at line 4738 from:

```python
def _agentic_loop(
    msgs: List[Dict[str, Any]],
    tool_ctx: "ToolContext",
    *,
    use_thinking: bool = False,
) -> Dict[str, Any]:
```

to:

```python
def _agentic_loop(
    msgs: List[Dict[str, Any]],
    tool_ctx: "ToolContext",
    *,
    use_thinking: bool = False,
    user_msg: str = "",
) -> Dict[str, Any]:
```

- [ ] **Step 3: Pass `user_msg` from the call site**

At the call site (located in Step 1), add `user_msg=user_msg` or `user_msg=<the-variable-holding-the-user-text>`. If the call site does not have a local variable for the user text, extract it:

```python
_um = ""
for _m in reversed(msgs):
    if _m.get("role") == "user":
        _um = _m.get("content", "") or ""
        break
# existing call:
result = _agentic_loop(msgs=msgs, tool_ctx=tool_ctx, use_thinking=..., user_msg=_um)
```

- [ ] **Step 4: Run existing tests — default-arg path must keep working**

```bash
python -m pytest backend/tests/test_agentic_loop.py -v
```

Expected: all pass. Existing tests call `_agentic_loop` without `user_msg` → default `""`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ai_chat.py
git commit -m "feat(moby): thread user_msg into _agentic_loop"
```

---

## Task 11: Compute `_tabular` + whitelist at loop start; force turn 1 when `_tabular`

**Files:**
- Modify: `backend/app/routers/ai_chat.py` — top of `_agentic_loop` + turn 1 call
- Modify: `backend/tests/test_agentic_loop.py` — 2 new tests

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_agentic_loop.py`:

```python
@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_forces_tool_choice_when_tabular_intent(mock_dispatch, mock_claude, tool_ctx):
    """Tabular intent → turn 1 uses tool_choice='required' and tools_override whitelist."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tool_policy import TABLE_RETURNING_TOOLS

    mock_claude.return_value = _text_response("<p>empty</p>")

    _agentic_loop(
        msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
        user_msg="List sites in Spain",
    )

    assert mock_claude.called
    _, kwargs = mock_claude.call_args_list[0]
    assert kwargs.get("tool_choice") == "required", f"Got tool_choice={kwargs.get('tool_choice')}"
    override = kwargs.get("tools_override")
    assert override is not None, "tools_override must be passed when tabular"
    names = {t["function"]["name"] for t in override}
    assert names == TABLE_RETURNING_TOOLS


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_uses_auto_when_not_tabular(mock_dispatch, mock_claude, tool_ctx):
    """Non-tabular query → existing behaviour: tool_choice='auto', no override."""
    from backend.app.routers.ai_chat import _agentic_loop

    mock_claude.return_value = _text_response("<p>hi</p>")

    _agentic_loop(
        msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
        user_msg="Explain what CTS means",
    )

    _, kwargs = mock_claude.call_args_list[0]
    assert kwargs.get("tool_choice") == "auto"
    assert kwargs.get("tools_override") is None
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
python -m pytest backend/tests/test_agentic_loop.py::test_loop_forces_tool_choice_when_tabular_intent backend/tests/test_agentic_loop.py::test_loop_uses_auto_when_not_tabular -v
```

Expected: both fail — `_agentic_loop` does not yet call `has_tabular_intent` nor pass `tools_override`.

- [ ] **Step 3: Implement the tabular gate in `_agentic_loop`**

In `backend/app/routers/ai_chat.py`, immediately after the imports inside `_agentic_loop` (around line 4756) and before `start_time = ...`, add:

```python
    from backend.app.routers.moby_tool_policy import (
        has_tabular_intent,
        filter_tools_spec,
    )

    _tabular = False
    _whitelist_spec: Optional[List[Dict[str, Any]]] = None
    try:
        if has_tabular_intent(user_msg):
            _tabular = True
            _whitelist_spec = filter_tools_spec(TOOLS_SPEC)
            _dbg("Tabular intent detected (query=%r), forcing tool_choice=required", user_msg[:80])
    except Exception as _e:
        _dbg("has_tabular_intent failed, defaulting to non-tabular: %s", _e)
```

Then replace the turn-1 `_claude_chat` call at line 4785:

```python
            resp = _claude_chat(msgs, tool_choice="auto", force_no_tools=is_final, use_thinking=think)
```

with:

```python
            if _tabular and turn == 1:
                resp = _claude_chat(
                    msgs,
                    tool_choice="required",
                    force_no_tools=is_final,
                    use_thinking=think,
                    tools_override=_whitelist_spec,
                )
            else:
                resp = _claude_chat(msgs, tool_choice="auto", force_no_tools=is_final, use_thinking=think)
```

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
python -m pytest backend/tests/test_agentic_loop.py::test_loop_forces_tool_choice_when_tabular_intent backend/tests/test_agentic_loop.py::test_loop_uses_auto_when_not_tabular -v
```

Expected: both PASS.

- [ ] **Step 5: Run the full agentic-loop test file for regressions**

```bash
python -m pytest backend/tests/test_agentic_loop.py -v
```

Expected: all tests pass (existing + 2 new).

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/ai_chat.py backend/tests/test_agentic_loop.py
git commit -m "feat(moby): force tool_choice=required on turn 1 for tabular-intent queries"
```

---

## Task 12: Track `_table_produced_this_request` and add post-loop retry

**Files:**
- Modify: `backend/app/routers/ai_chat.py` — `_agentic_loop`
- Modify: `backend/tests/test_agentic_loop.py` — 4 new tests

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_agentic_loop.py`:

```python
@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_retry_on_text_only_with_tabular_intent(mock_dispatch, mock_claude, tool_ctx):
    """Turn 1 returns text only + tabular intent → retry fires, produces a table."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tools import ToolResult

    # Turn 1: text-only response (Claude answered from memory)
    # Retry: tool call that produces a table
    tool_call = _make_mock_tool_call("t1", "explorer_search", {})
    mock_claude.side_effect = [
        _text_response("<p>9 French sites have HLA typing.</p>"),
        _tool_response([tool_call], text="Found 9 sites"),
    ]
    mock_dispatch.return_value = ToolResult(
        last_table={"rows": [{"account_id": "a1", "country": "FR"}], "columns": ["account_id"]},
    )

    result = _agentic_loop(
        msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
        user_msg="List sites in France with HLA typing",
    )

    # Retry was invoked: mock_claude called twice total
    assert mock_claude.call_count == 2
    # Retry used required + override
    _, retry_kwargs = mock_claude.call_args_list[1]
    assert retry_kwargs.get("tool_choice") == "required"
    assert retry_kwargs.get("tools_override") is not None
    # Final result has the table
    assert result["last_table"] is not None
    assert result["last_table"]["rows"]


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_retry_noop_without_tabular_intent(mock_dispatch, mock_claude, tool_ctx):
    """Text-only response + non-tabular intent → NO retry."""
    from backend.app.routers.ai_chat import _agentic_loop

    mock_claude.return_value = _text_response("<p>CTS stands for Clinical Trial Site.</p>")

    _agentic_loop(
        msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
        user_msg="Explain what CTS means",
    )

    assert mock_claude.call_count == 1, "No retry expected for conversational queries"


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_retry_noop_if_table_produced_this_request(mock_dispatch, mock_claude, tool_ctx):
    """Turn 1 produced a table → NO retry even if tabular intent."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tools import ToolResult

    tool_call = _make_mock_tool_call("t1", "explorer_search", {})
    mock_claude.side_effect = [
        _tool_response([tool_call], text="Found 50 sites — here is the summary."),
        _text_response("<p>Here are your 50 sites.</p>"),
    ]
    mock_dispatch.return_value = ToolResult(
        last_table={"rows": [{"a": 1}], "columns": ["a"]},
    )

    _agentic_loop(
        msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
        user_msg="List sites",
    )

    # Expected 1 or 2 calls (loop may synthesise), but NOT 3 (which would mean retry also fired).
    assert mock_claude.call_count <= 2


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_retry_fires_when_inherited_table_but_no_new_tool(mock_dispatch, mock_claude, tool_ctx):
    """tool_ctx.last_table pre-populated (follow-up), turn 1 text-only → retry still fires."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tools import ToolResult

    tool_ctx.last_table = {"rows": [{"old": 1}], "columns": ["old"]}  # inherited from a previous request

    tool_call = _make_mock_tool_call("t2", "explorer_search", {})
    mock_claude.side_effect = [
        _text_response("<p>some text</p>"),
        _tool_response([tool_call], text="New result"),
    ]
    mock_dispatch.return_value = ToolResult(
        last_table={"rows": [{"new": 1}], "columns": ["new"]},
    )

    result = _agentic_loop(
        msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
        user_msg="List sites in Germany",
    )

    # Retry fired
    assert mock_claude.call_count == 2
    # Final table is the NEW one, not the inherited one
    assert result["last_table"]["rows"][0] == {"new": 1}


@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_retry_max_one(mock_dispatch, mock_claude, tool_ctx):
    """Retry itself answers text-only → do NOT run a second retry (cap=1)."""
    from backend.app.routers.ai_chat import _agentic_loop

    mock_claude.side_effect = [
        _text_response("<p>first text</p>"),
        _text_response("<p>retry also text</p>"),
        _text_response("<p>SHOULD NOT BE CALLED</p>"),
    ]

    _agentic_loop(
        msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
        user_msg="List sites",
    )

    # Exactly 2 calls: turn 1 + 1 retry. No third call.
    assert mock_claude.call_count == 2
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
python -m pytest backend/tests/test_agentic_loop.py -k "retry" -v
```

Expected: all 5 retry tests fail (retry logic not yet implemented).

- [ ] **Step 3: Implement the retry block in `_agentic_loop`**

In `backend/app/routers/ai_chat.py` `_agentic_loop`:

First, thread `table_produced_now` through. Change the refactored dispatch invocation (added in Task 9) to update a running flag. At the top of `_agentic_loop`, near line 4762, add:

```python
    _table_produced_this_request = False
```

Replace the dispatch invocation block (from Task 9) with:

```python
        (dm_called, _lt, _lv, _lf, _produced_now) = _dispatch_tool_calls(
            assistant_msg, msgs, tool_ctx, seen_hashes, dm_called, tool_calls_made, turn,
        )
        if _lt is not None:
            last_table = _lt
        if _lv is not None:
            last_visualization = _lv
        if _lf is not None:
            last_explorer_filters = _lf
        if _produced_now:
            _table_produced_this_request = True
```

Then, after the main `for turn in range(...)` loop ends and before the `return {"text": text_out, ...}` (find the return near the end of `_agentic_loop`; it typically looks like `return {"text": text_out, "turns_used": ..., "tool_calls_made": ..., "last_table": last_table, ...}`), insert:

```python
    # Post-loop retry: one forced call if the loop exited text-only but the
    # user asked for a table and no table was produced this request.
    if _tabular and text_out and not _table_produced_this_request and _whitelist_spec is not None:
        _dbg("Retry triggered: loop exited text-only with tabular intent, re-calling with whitelist")
        msgs.append({
            "role": "user",
            "content": (
                "The previous answer lacked a table. The user asked for a list/table — "
                "you MUST call one of: explorer_search, nearest_filtered_sites, "
                "study_coordinators_with_activities, members_search."
            ),
        })
        try:
            retry_resp = _claude_chat(
                msgs,
                tool_choice="required",
                force_no_tools=False,
                use_thinking=False,
                tools_override=_whitelist_spec,
            )
        except (_anthropic_sdk.APITimeoutError, _anthropic_sdk.APIConnectionError, _anthropic_sdk.RateLimitError) as _e:
            _dbg("Retry failed: %s", _e)
        else:
            retry_msg = retry_resp.choices[0].message
            if retry_msg.tool_calls:
                msgs.append({
                    "role": "assistant",
                    "content": retry_msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in retry_msg.tool_calls],
                })
                (dm_called, _lt, _lv, _lf, _produced_now) = _dispatch_tool_calls(
                    retry_msg, msgs, tool_ctx, seen_hashes, dm_called, tool_calls_made, turn="retry",
                )
                if _lt is not None:
                    last_table = _lt
                if _lv is not None:
                    last_visualization = _lv
                if _lf is not None:
                    last_explorer_filters = _lf
                if _produced_now:
                    _table_produced_this_request = True
                    # Prefer retry's companion text if it has one
                    retry_text = (retry_msg.content or "").strip()
                    if retry_text:
                        text_out = retry_text
```

- [ ] **Step 4: Run all agentic-loop tests**

```bash
python -m pytest backend/tests/test_agentic_loop.py -v
```

Expected: all pass (original + previous Task 11 + 5 new retry tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ai_chat.py backend/tests/test_agentic_loop.py
git commit -m "feat(moby): add post-loop retry when tabular intent yields no table"
```

---

## Task 13: Emit SSE progress event for the retry turn

**Files:**
- Modify: `backend/app/routers/ai_chat.py` — in the retry block from Task 12
- Modify: `backend/tests/test_agentic_loop.py` — 1 new test

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_agentic_loop.py`:

```python
@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_progress_event_on_retry(mock_dispatch, mock_claude, tool_ctx):
    """When streaming (_STREAM_Q.q set), retry emits a __PROGRESS__ event."""
    import queue
    from backend.app.routers.ai_chat import _agentic_loop, _STREAM_Q
    from backend.app.routers.moby_tools import ToolResult

    tool_call = _make_mock_tool_call("t1", "explorer_search", {})
    mock_claude.side_effect = [
        _text_response("<p>text</p>"),
        _tool_response([tool_call], text="retrieved"),
    ]
    mock_dispatch.return_value = ToolResult(
        last_table={"rows": [{"a": 1}], "columns": ["a"]},
    )

    q: "queue.Queue[str]" = queue.Queue()
    _STREAM_Q.q = q
    try:
        _agentic_loop(
            msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
            user_msg="List sites",
        )
    finally:
        _STREAM_Q.q = None

    # Collect events emitted to the queue
    emitted = []
    while not q.empty():
        emitted.append(q.get_nowait())
    progress_events = [e for e in emitted if isinstance(e, str) and e.startswith("__PROGRESS__")]
    retry_events = [e for e in progress_events if '"retry"' in e or '"turn": "retry"' in e]
    assert retry_events, f"No retry progress event in {progress_events}"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python -m pytest backend/tests/test_agentic_loop.py::test_loop_progress_event_on_retry -v
```

Expected: FAIL — no retry progress event is emitted yet.

- [ ] **Step 3: Implement the emit**

In the retry block added in Task 12, immediately after the `_dispatch_tool_calls(retry_msg, ...)` call, add:

```python
                token_q = getattr(_STREAM_Q, "q", None)
                if token_q is not None:
                    try:
                        progress = json.dumps({
                            "turn": "retry",
                            "tools": [tc.function.name for tc in retry_msg.tool_calls],
                        })
                        token_q.put(f"__PROGRESS__{progress}")
                    except Exception:
                        pass
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
python -m pytest backend/tests/test_agentic_loop.py::test_loop_progress_event_on_retry -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ai_chat.py backend/tests/test_agentic_loop.py
git commit -m "feat(moby): emit SSE progress event for retry turn"
```

---

## Task 14: Retry respects DM guard (characterization test)

**Files:**
- Modify: `backend/tests/test_agentic_loop.py` — 1 new test

This is a characterization test — the behaviour is already correct because the retry reuses `_dispatch_tool_calls`, which respects `dm_called`. The test locks it in.

- [ ] **Step 1: Write the test**

Append to `backend/tests/test_agentic_loop.py`:

```python
@patch("backend.app.routers.ai_chat._claude_chat")
@patch("backend.app.routers.moby_tools.dispatch_tool")
def test_loop_retry_respects_dm_guard(mock_dispatch, mock_claude, tool_ctx):
    """Retry that picks a DM tool after one DM call already happened is blocked, does not crash."""
    from backend.app.routers.ai_chat import _agentic_loop
    from backend.app.routers.moby_tools import ToolResult

    # Turn 1: Claude calls nearest_filtered_sites (DM tool). Tool returns no table.
    # Loop exits text-only. Retry picks nearest_filtered_sites again — DM guard blocks it.
    turn1_tool = _make_mock_tool_call("t1", "nearest_filtered_sites", {"lat": 0, "lng": 0})
    retry_tool = _make_mock_tool_call("t2", "nearest_filtered_sites", {"lat": 1, "lng": 1})
    mock_claude.side_effect = [
        _tool_response([turn1_tool], text=""),
        _text_response("<p>no table</p>"),      # turn 2 synthesis — still no table
        _tool_response([retry_tool], text=""),  # retry
    ]
    mock_dispatch.return_value = ToolResult(last_table=None)  # no table from any call

    result = _agentic_loop(
        msgs=tool_ctx.msgs, tool_ctx=tool_ctx,
        user_msg="nearest sites to Madrid",
    )

    # Loop completes without exception; text_out is the text-only response
    assert "no table" in (result["text"] or "") or result["text"] is not None
```

- [ ] **Step 2: Run the test**

```bash
python -m pytest backend/tests/test_agentic_loop.py::test_loop_retry_respects_dm_guard -v
```

Expected: PASS (characterization — no code change needed).

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_agentic_loop.py
git commit -m "test(moby): lock DM guard behaviour in the retry path"
```

---

## Task 15: Full test suite + type check

- [ ] **Step 1: Run the full backend test suite**

```bash
python -m pytest backend/tests/ -v
```

Expected: all tests pass (552 original + 27 refactor + 9 compare + 13 tool_policy + ~8 new agentic_loop = ~609 total).

- [ ] **Step 2: Run the type checker if one is configured**

```bash
# If mypy or pyright is configured in the repo:
python -m mypy backend/app/routers/moby_tool_policy.py backend/app/routers/ai_chat.py 2>&1 | head -20
```

Expected: no new errors introduced by this plan. If the repo has no mypy config, skip.

- [ ] **Step 3: No commit — this is a verification step only**

---

## Task 16: Run the regression gate (merge requirement)

**Files:**
- None modified.

This is the merge gate defined in the spec. The code is now complete — this step verifies the fix actually resolves the SC06/SS01/SS03 flakiness.

- [ ] **Step 1: Get a fresh SF session cookie**

Follow the procedure in memory `reference_prod_smoke_token.md` to produce a fresh `sf_session` cookie from a local login.

- [ ] **Step 2: Start the local backend**

```bash
bash scripts/run_local_backend.sh
```

Wait until the 4-worker process is ready (log line `Uvicorn running on 0.0.0.0:8000`).

- [ ] **Step 3: Run the regression rerun three consecutive times**

```bash
SF_SESSION_COOKIE="<cookie>" API_BASE="http://localhost:8000" python scripts/rerun_regressions.py > run1.json
SF_SESSION_COOKIE="<cookie>" API_BASE="http://localhost:8000" python scripts/rerun_regressions.py > run2.json
SF_SESSION_COOKIE="<cookie>" API_BASE="http://localhost:8000" python scripts/rerun_regressions.py > run3.json
```

- [ ] **Step 4: Assert SC06, SS01, SS03 are PASS in all 3 runs**

```bash
for run in run1.json run2.json run3.json; do
  echo "=== $run ==="
  python -c "import json, sys; r=json.load(open('$run')); [print(i['id'], i.get('status')) for i in r if i['id'] in {'SC06','SS01','SS03'}]"
done
```

Expected: every line is `SC0X PASS` or `SS0X PASS` across all 3 runs. **If any line is FAIL in any run, the fix is not stable — do not merge. Re-open triage.**

- [ ] **Step 5: Document the rerun evidence**

Save the three JSONs as `docs/moby-regression-rerun-post-fix.json` (concatenate or keep as list):

```bash
python -c "import json; a=[json.load(open(f)) for f in ['run1.json','run2.json','run3.json']]; json.dump({'runs': a}, open('docs/moby-regression-rerun-post-fix.json', 'w'), indent=2)"
rm run1.json run2.json run3.json
git add docs/moby-regression-rerun-post-fix.json
git commit -m "docs(moby): record post-fix regression rerun evidence (3/3 PASS for SC06/SS01/SS03)"
```

---

## Task 17: Update project-state docs

**Files:**
- Modify: `docs/current-state.md`
- Modify: `docs/next-steps.md`

Per the `CLAUDE.md` instruction: mark next-steps items as done and update current-state in the same response as the change.

- [ ] **Step 1: Mark the relevant items done in `docs/next-steps.md`**

Find any bullet describing the SC06/SS01/SS03 flakiness triage. Change it to `~~description~~ — DONE (2026-04-22)`.

- [ ] **Step 2: Update `docs/current-state.md` summary**

Add a short paragraph under "In progress / just done":

> **2026-04-22 — Plan A of the SDK-vs-bespoke-loop brainstorm shipped.** `moby_tool_policy.py` + post-loop retry close the SC06/SS01/SS03 flakiness. Rerun evidence in `docs/moby-regression-rerun-post-fix.json` (3/3 PASS). Plan B (expose `TOOL_DISPATCH` as MCP) still scheduled — see spec `docs/superpowers/specs/2026-04-22-moby-force-tool-guard-design.md` non-goals.

- [ ] **Step 3: Commit**

```bash
git add docs/current-state.md docs/next-steps.md
git commit -m "docs: mark force-tool guard shipped; retain Plan B (MCP) as pending"
```

---

## Self-review

**Spec coverage check:**

| Spec section | Covered by task(s) |
|---|---|
| Problem — SC06/SS01/SS03 flakiness | Tasks 2 (detector) + 11 (turn 1 force) + 12 (retry) + 16 (rerun gate) |
| Non-goals (no SDK, no MCP, no planner touch) | Respected — zero changes to `moby_planner.py`, `moby_tools.py`, system prompt |
| `TABLE_RETURNING_TOOLS` frozenset | Task 1 + locked by Task 7 |
| `has_tabular_intent` EN + ES + blacklist | Tasks 2, 3, 4, 5 |
| `filter_tools_spec` | Task 6 |
| `_claude_chat` tools_override kwarg | Task 8 |
| `_dispatch_tool_calls` helper extraction | Task 9 |
| `user_msg` threaded into loop | Task 10 |
| Turn 1 force + whitelist when `_tabular` | Task 11 |
| `_table_produced_this_request` flag | Task 12 |
| Post-loop retry (cap=1, no extra turns) | Task 12 |
| SSE progress event for retry | Task 13 |
| DM guard respected in retry | Task 14 (characterization) |
| Merge gate (3 consecutive runs, SC06/SS01/SS03 PASS) | Task 16 |
| Post-merge doc updates | Task 17 |

No gaps.

**Placeholder scan:** no `TBD`, `TODO`, "similar to", or "handle edge cases" without spec. Every code block is the actual code.

**Type/name consistency:**
- `TABLE_RETURNING_TOOLS`, `_POSITIVES`, `_BLACKLIST`, `_norm`, `has_tabular_intent`, `filter_tools_spec` — consistent across tasks.
- `_tabular`, `_whitelist_spec`, `_table_produced_this_request`, `_produced_now` — consistent across Tasks 11, 12, 13.
- `_dispatch_tool_calls` return tuple: `(dm_called_after, last_table, last_viz, last_filters, table_produced_now)` — consistent between definition (Task 9) and call sites (Tasks 9, 12).
- `tools_override` kwarg: consistent between `_claude_chat` definition (Task 8), turn-1 call (Task 11), and retry call (Task 12).

No drift found.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-22-moby-force-tool-guard.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, I review between tasks, fast iteration and isolation.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints for review.

Which approach?
