"""Unit tests for the SemanticFieldResolver POC.

Bedrock calls are fully mocked; no network or AWS credentials are needed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

# Make `app.*` importable when run from repo root or backend/.
_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def fake_index(tmp_path, monkeypatch):
    """Write a tiny field index to disk and point the resolver at it."""
    # Hand-crafted vectors so cosine sim against [1,0,...] yields a stable
    # shortlist order: row 0 best, row 1 second, row 2 last.
    vecs = np.array([
        [0.9, 0.1, 0, 0, 0, 0, 0, 0],
        [0.7, 0.3, 0, 0, 0, 0, 0, 0],
        [0.1, 0.9, 0, 0, 0, 0, 0, 0],
    ], dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    index = {
        "model": "cohere.embed-multilingual-v3",
        "dim": 8,
        "built_at": "2026-05-04T00:00:00Z",
        "keys": [
            "C_Number_of_Stage1_Individuals_followed__c",
            "C_Number_of_Stage2_Individuals_followed__c",
            "C_Is_HLA_typing_performed__c",
        ],
        "labels": [
            "Stage 1 individuals followed",
            "Stage 2 individuals followed",
            "HLA typing performed",
        ],
        "aliases": ["stage 1", "stage 2", "hla typing"],
        "sources": ["sf", "sf", "sf"],
        "embeddings": vecs.tolist(),
    }
    path = tmp_path / "field_index.json"
    path.write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setenv("MOBY_FIELD_INDEX_PATH", str(path))

    from app import moby_semantic_resolver as mod
    # INDEX_PATH is frozen at module import → patch it explicitly so the
    # fixture path wins even when other tests imported the module first.
    monkeypatch.setattr(mod, "INDEX_PATH", path)
    mod.reset_caches_for_tests()
    return mod


def _fake_embed(_q: str) -> np.ndarray:
    v = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    return v / np.linalg.norm(v)


def test_resolve_orders_by_rerank_score(fake_index):
    mod = fake_index
    rerank_payload = [
        {"index": 1, "relevance_score": 0.91},
        {"index": 0, "relevance_score": 0.42},
    ]
    with mock.patch.object(mod, "_embed_query", side_effect=_fake_embed), \
         mock.patch.object(mod, "_rerank", return_value=rerank_payload):
        out = mod.SemanticFieldResolver().resolve("stage two patients", top_n=2)
    assert [r["key"] for r in out] == [
        "C_Number_of_Stage2_Individuals_followed__c",
        "C_Number_of_Stage1_Individuals_followed__c",
    ]
    assert out[0]["score"] > out[1]["score"]


def test_resolve_dedupes_by_canonical_key(fake_index):
    """Multiple rerank hits pointing at the same key (with/without 'sf.' prefix)
    must collapse: keep the max score, preserve descending order."""
    mod = fake_index
    mod._load_index()  # force lazy-load so we can mutate the cached dict.
    # Inject an extra row with the same canonical key as row 0 but prefixed
    # with "sf." (mimics the real index where curated + alias entries coexist).
    mod._INDEX["keys"].append("sf.C_Number_of_Stage1_Individuals_followed__c")
    mod._INDEX["labels"].append("Stage 1 individuals followed (alias)")
    mod._INDEX["sources"].append("sf")
    extra = np.array([[0.85, 0.15, 0, 0, 0, 0, 0, 0]], dtype=np.float32)
    extra /= np.linalg.norm(extra, axis=1, keepdims=True)
    mod._INDEX["embeddings"] = np.vstack([mod._INDEX["embeddings"], extra])

    # Rerank returns 4 hits, two of which collapse onto Stage1 (idx 0 and 3).
    # The lower-scoring duplicate must be dropped, the higher one kept.
    # shortlist order (sims desc against [1,0,...]): row0, row3, row1, row2
    # → local indices: 0=Stage1, 1=Stage1-dup(sf.), 2=Stage2, 3=HLA
    rerank_payload = [
        {"index": 0, "relevance_score": 0.50},  # Stage1 (low)
        {"index": 2, "relevance_score": 0.80},  # Stage2
        {"index": 1, "relevance_score": 0.92},  # Stage1 sf.-prefixed (high)
        {"index": 3, "relevance_score": 0.20},  # HLA
    ]
    with mock.patch.object(mod, "_embed_query", side_effect=_fake_embed), \
         mock.patch.object(mod, "_rerank", return_value=rerank_payload):
        out = mod.SemanticFieldResolver().resolve("stage one", top_n=3)

    # Three unique canonical keys, ordered by score desc.
    assert len(out) == 3
    canon = lambda k: k[3:] if k.startswith("sf.") else k
    canons = [canon(r["key"]) for r in out]
    assert canons == [
        "C_Number_of_Stage1_Individuals_followed__c",
        "C_Number_of_Stage2_Individuals_followed__c",
        "C_Is_HLA_typing_performed__c",
    ]
    # Surviving Stage1 hit is the high-scoring one (0.92), not 0.50.
    assert out[0]["score"] == pytest.approx(0.92)
    # Strictly decreasing scores.
    assert out[0]["score"] > out[1]["score"] > out[2]["score"]


def test_resolver_raises_when_bedrock_fails(fake_index):
    mod = fake_index
    with mock.patch.object(mod, "_embed_query",
                           side_effect=mod.BedrockUnavailable("boom")):
        with pytest.raises(mod.BedrockUnavailable):
            mod.SemanticFieldResolver().resolve("anything")


def test_resolve_caches_query_embedding(fake_index):
    """Two consecutive resolve() calls with the same query must hit the
    embedding cache: _embed_query is invoked once, last_cache_status flips
    miss → hit. Rerank is still called every time (not cached)."""
    mod = fake_index
    rerank_payload = [{"index": 0, "relevance_score": 0.8}]
    embed_mock = mock.MagicMock(side_effect=_fake_embed)
    with mock.patch.object(mod, "_embed_query", embed_mock), \
         mock.patch.object(mod, "_rerank", return_value=rerank_payload):
        r1 = mod.SemanticFieldResolver()
        r1.resolve("stage 1 followups", top_n=1)
        assert r1.last_cache_status == "miss"
        r2 = mod.SemanticFieldResolver()
        r2.resolve("stage 1 followups", top_n=1)
        assert r2.last_cache_status == "hit"
    assert embed_mock.call_count == 1


def test_top_matches_flag_off_uses_lexical(monkeypatch):
    """Flag off: pure lexical matcher (semantic branch deprecated in Option A)."""
    monkeypatch.delenv("MOBY_SEMANTIC_FIELD_RESOLVER", raising=False)
    from app.routers import ai_chat
    out = ai_chat._top_matches("stage 1", ["stage 1", "hla typing"], k=2)
    assert "stage 1" in out


def test_top_matches_flag_on_still_uses_lexical(monkeypatch):
    """Option A: _top_matches no longer calls the semantic resolver — semantic
    guidance now lives in the system prompt via _semantic_field_hints."""
    monkeypatch.setenv("MOBY_SEMANTIC_FIELD_RESOLVER", "1")
    from app.routers import ai_chat

    sentinel = mock.MagicMock(side_effect=AssertionError("must not be called"))
    with mock.patch.object(ai_chat, "_semantic_top_matches", side_effect=sentinel):
        out = ai_chat._top_matches("stage 1", ["stage 1", "hla typing"], k=2)
    assert "stage 1" in out
