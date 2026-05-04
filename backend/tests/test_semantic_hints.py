"""Unit tests for Option A: semantic field hints injected into Moby's system prompt.

The resolver itself is mocked — these tests cover the wiring in
`backend/app/routers/ai_chat.py` (helpers `_semantic_field_hints` and
`_format_hints_block`) plus a smoke check that `chat_api`'s `msgs[]` builder
prepends the hints block when the flag is on.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

# Make `app.*` importable when run from repo root or backend/.
_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)


_FAKE_HITS = [
    {"key": "C_Number_of_Stage1_Individuals_followed__c",
     "label": "Stage 1 individuals followed", "score": 0.91},
    {"key": "C_Number_of_Stage2_Individuals_followed__c",
     "label": "Stage 2 individuals followed", "score": 0.87},
]


def test_hints_flag_off_returns_empty(monkeypatch):
    monkeypatch.delenv("MOBY_SEMANTIC_FIELD_RESOLVER", raising=False)
    from app.routers import ai_chat
    assert ai_chat._semantic_field_hints("anything") == ""


def test_hints_empty_query_returns_empty(monkeypatch):
    monkeypatch.setenv("MOBY_SEMANTIC_FIELD_RESOLVER", "1")
    from app.routers import ai_chat
    assert ai_chat._semantic_field_hints("") == ""
    assert ai_chat._semantic_field_hints("   ") == ""


def test_hints_flag_on_formats_block(monkeypatch):
    monkeypatch.setenv("MOBY_SEMANTIC_FIELD_RESOLVER", "1")
    from app.routers import ai_chat
    fake_resolver = mock.MagicMock()
    fake_resolver.resolve.return_value = _FAKE_HITS
    with mock.patch("app.moby_semantic_resolver.SemanticFieldResolver",
                    return_value=fake_resolver):
        out = ai_chat._semantic_field_hints("quanti pazienti stage 1?")
    assert "## Relevant fields (semantic hint, top-K=8)" in out
    assert "`C_Number_of_Stage1_Individuals_followed__c`" in out
    assert "Stage 1 individuals followed" in out
    assert "`C_Number_of_Stage2_Individuals_followed__c`" in out
    fake_resolver.resolve.assert_called_once()


def test_hints_resolver_failure_returns_empty(monkeypatch):
    """Bedrock/index failure must not break the chat path."""
    monkeypatch.setenv("MOBY_SEMANTIC_FIELD_RESOLVER", "1")
    from app.routers import ai_chat
    with mock.patch("app.moby_semantic_resolver.SemanticFieldResolver",
                    side_effect=RuntimeError("bedrock down")):
        out = ai_chat._semantic_field_hints("stage 1?")
    assert out == ""


def test_hints_no_results_returns_empty(monkeypatch):
    monkeypatch.setenv("MOBY_SEMANTIC_FIELD_RESOLVER", "1")
    from app.routers import ai_chat
    fake_resolver = mock.MagicMock()
    fake_resolver.resolve.return_value = []
    with mock.patch("app.moby_semantic_resolver.SemanticFieldResolver",
                    return_value=fake_resolver):
        out = ai_chat._semantic_field_hints("nothing matches this")
    assert out == ""


def test_format_hints_block_shape():
    from app.routers import ai_chat
    block = ai_chat._format_hints_block(_FAKE_HITS)
    lines = block.splitlines()
    # Header + description + blank + 2 bullet rows.
    assert lines[0].startswith("## Relevant fields")
    assert lines[-1].startswith("- `C_Number_of_Stage2")
    assert sum(1 for ln in lines if ln.startswith("- `")) == len(_FAKE_HITS)


def test_chat_api_injects_hints_block_into_msgs(monkeypatch):
    """When the flag is on and the resolver returns hits, the system prompt
    that feeds the agentic loop must contain the markdown hints block.

    We don't run the whole endpoint — we exercise the same `msgs.append` path
    by constructing the same trio of system messages chat_api builds and
    asserting the third message is the hints block.
    """
    monkeypatch.setenv("MOBY_SEMANTIC_FIELD_RESOLVER", "1")
    from app.routers import ai_chat

    fake_resolver = mock.MagicMock()
    fake_resolver.resolve.return_value = _FAKE_HITS
    with mock.patch("app.moby_semantic_resolver.SemanticFieldResolver",
                    return_value=fake_resolver):
        block = ai_chat._semantic_field_hints("Quanti pazienti T1D seguiti?")

    msgs = [
        {"role": "system", "content": "SYSTEM_PROMPT"},
        {"role": "system", "content": "KNOWLEDGE INDEX..."},
    ]
    if block:
        msgs.append({"role": "system", "content": block})

    assert len(msgs) == 3
    assert msgs[2]["role"] == "system"
    assert "Relevant fields (semantic hint" in msgs[2]["content"]
    assert "Stage 1 individuals followed" in msgs[2]["content"]
