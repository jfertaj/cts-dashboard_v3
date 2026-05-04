"""Unit tests for moby_tool_policy — pure functions, no network, no DB."""
from __future__ import annotations


def test_table_returning_tools_constant_has_four_entries():
    from app.routers.moby_tool_policy import TABLE_RETURNING_TOOLS
    assert TABLE_RETURNING_TOOLS == frozenset({
        "explorer_search",
        "nearest_filtered_sites",
        "study_coordinators_with_activities",
        "members_search",
    })


def test_table_returning_tools_is_frozenset():
    from app.routers.moby_tool_policy import TABLE_RETURNING_TOOLS
    assert isinstance(TABLE_RETURNING_TOOLS, frozenset)


def test_has_tabular_intent_real_regression_queries_en():
    """The 3 flaky regressions + 3 pattern-1 regressions must all be detected."""
    from app.routers.moby_tool_policy import has_tabular_intent
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
    from app.routers.moby_tool_policy import has_tabular_intent
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


def test_has_tabular_intent_positives_es():
    from app.routers.moby_tool_policy import has_tabular_intent
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
    from app.routers.moby_tool_policy import has_tabular_intent
    assert has_tabular_intent("cuántos sitios")
    assert has_tabular_intent("cuantos sitios")
    assert has_tabular_intent("CUÁNTOS SITIOS")
    assert has_tabular_intent("  Cuántos Sitios  ")


def test_has_tabular_intent_blacklist_wins():
    """Conversational phrasings beat positive keywords."""
    from app.routers.moby_tool_policy import has_tabular_intent
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
    from app.routers.moby_tool_policy import has_tabular_intent
    for q in ["hi", "thanks", "ok", "what?", "hola", "", "   "]:
        assert not has_tabular_intent(q)


def test_has_tabular_intent_cl05_not_covered():
    """CL05 ('Are there any CTS sites in X?') is intentionally NOT detected.

    Per spec: CL05 is a semantic-note regression (CTS flag is null for
    Bucharest), not a real bug. The force-tool guard does not cover it.
    """
    from app.routers.moby_tool_policy import has_tabular_intent
    assert not has_tabular_intent("Are there any CTS sites in Romania or Bulgaria?")
    assert not has_tabular_intent("Is there a CTS site in Poland?")


def _fake_spec(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_filter_tools_spec_whitelist_basic():
    from app.routers.moby_tool_policy import filter_tools_spec
    spec = [_fake_spec("a"), _fake_spec("b"), _fake_spec("c")]
    out = filter_tools_spec(spec, {"a", "c"})
    assert [t["function"]["name"] for t in out] == ["a", "c"]


def test_filter_tools_spec_preserves_entry_identity():
    from app.routers.moby_tool_policy import filter_tools_spec
    spec = [_fake_spec("a"), _fake_spec("b")]
    out = filter_tools_spec(spec, {"a"})
    assert out[0] is spec[0]  # reference-preserving, not a copy


def test_filter_tools_spec_empty_whitelist():
    from app.routers.moby_tool_policy import filter_tools_spec
    spec = [_fake_spec("a")]
    assert filter_tools_spec(spec, set()) == []


def test_filter_tools_spec_nonexistent_tool_in_whitelist():
    from app.routers.moby_tool_policy import filter_tools_spec
    spec = [_fake_spec("a")]
    out = filter_tools_spec(spec, {"a", "does_not_exist"})
    assert [t["function"]["name"] for t in out] == ["a"]


def test_filter_tools_spec_default_uses_table_returning():
    from app.routers.moby_tool_policy import filter_tools_spec, TABLE_RETURNING_TOOLS
    spec = [_fake_spec("explorer_search"), _fake_spec("soql_query"), _fake_spec("members_search")]
    out = filter_tools_spec(spec)  # no whitelist arg → defaults to TABLE_RETURNING_TOOLS
    names = {t["function"]["name"] for t in out}
    assert names == {"explorer_search", "members_search"}
    assert names.issubset(TABLE_RETURNING_TOOLS)


def test_table_returning_tools_exist_in_real_tools_spec():
    """All 4 entries in TABLE_RETURNING_TOOLS must correspond to real tools in TOOLS_SPEC."""
    from app.routers.ai_chat import TOOLS_SPEC
    from app.routers.moby_tool_policy import TABLE_RETURNING_TOOLS

    real_names = {t.get("function", {}).get("name") for t in TOOLS_SPEC}
    missing = TABLE_RETURNING_TOOLS - real_names
    assert not missing, f"TABLE_RETURNING_TOOLS references non-existent tools: {missing}"


def test_filter_tools_spec_against_real_tools_spec():
    from app.routers.ai_chat import TOOLS_SPEC
    from app.routers.moby_tool_policy import filter_tools_spec, TABLE_RETURNING_TOOLS

    out = filter_tools_spec(TOOLS_SPEC)
    assert len(out) == 4
    names = {t["function"]["name"] for t in out}
    assert names == TABLE_RETURNING_TOOLS
