"""
Semantic field resolver for Moby — POC.

Wraps Bedrock Cohere Embed multilingual v3 (eu-west-1) + Rerank 3.5
(eu-central-1) over the precomputed field index built by
`scripts/build_field_index_cohere.py`.

Design notes
------------
- Index loaded lazily, cached in module scope.
- boto3 sessions are lazy too; in prod the ECS task role is used, in local
  dev the default credential chain picks up `AWS_PROFILE=juan`.
- Cosine similarity computed in numpy — corpus is small (~thousands).
- If Bedrock fails for any reason, callers receive `BedrockUnavailable` and
  must fall back to the legacy lexical matcher.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# --- Config -----------------------------------------------------------------

EMBED_REGION = os.environ.get("BEDROCK_EMBED_REGION", "eu-west-1")
RERANK_REGION = os.environ.get("BEDROCK_RERANK_REGION", "eu-central-1")
EMBED_MODEL_ID = os.environ.get("BEDROCK_EMBED_MODEL", "cohere.embed-multilingual-v3")
RERANK_MODEL_ID = os.environ.get("BEDROCK_RERANK_MODEL", "cohere.rerank-v3-5:0")

_DEFAULT_INDEX_PATH = (
    Path(__file__).resolve().parent / "cache" / "field_index.json"
)
INDEX_PATH = Path(os.environ.get("MOBY_FIELD_INDEX_PATH", str(_DEFAULT_INDEX_PATH)))


# --- Errors -----------------------------------------------------------------

class BedrockUnavailable(RuntimeError):
    """Raised when Bedrock embed/rerank cannot complete a request."""


class IndexUnavailable(RuntimeError):
    """Raised when the on-disk field index is missing or corrupt."""


# --- Lazy singletons --------------------------------------------------------

_INDEX_LOCK = threading.Lock()
_INDEX: Optional[Dict[str, Any]] = None
_EMBED_CLIENT = None  # boto3 client for eu-west-1
_RERANK_CLIENT = None  # boto3 client for eu-central-1


def _load_index() -> Dict[str, Any]:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _INDEX_LOCK:
        if _INDEX is not None:
            return _INDEX
        if not INDEX_PATH.exists():
            raise IndexUnavailable(f"field index not found at {INDEX_PATH}")
        try:
            raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            embeddings = np.asarray(raw["embeddings"], dtype=np.float32)
        except Exception as exc:
            raise IndexUnavailable(f"failed to load index: {exc}") from exc
        _INDEX = {
            "keys": raw.get("keys", []),
            "labels": raw.get("labels", []),
            "aliases": raw.get("aliases", []),
            "sources": raw.get("sources", []),
            "embeddings": embeddings,
            "dim": int(raw.get("dim", embeddings.shape[1])),
            "model": raw.get("model", EMBED_MODEL_ID),
            "built_at": raw.get("built_at"),
        }
        logger.info(
            "moby-semantic: loaded index with %d entries (%dd)",
            embeddings.shape[0], _INDEX["dim"],
        )
        return _INDEX


def _get_embed_client():
    global _EMBED_CLIENT
    if _EMBED_CLIENT is None:
        import boto3
        _EMBED_CLIENT = boto3.Session(region_name=EMBED_REGION).client("bedrock-runtime")
    return _EMBED_CLIENT


def _get_rerank_client():
    global _RERANK_CLIENT
    if _RERANK_CLIENT is None:
        import boto3
        _RERANK_CLIENT = boto3.Session(region_name=RERANK_REGION).client("bedrock-runtime")
    return _RERANK_CLIENT


# --- Bedrock calls ----------------------------------------------------------

def _embed_query(query: str) -> np.ndarray:
    payload = {"texts": [query], "input_type": "search_query", "truncate": "END"}
    try:
        resp = _get_embed_client().invoke_model(
            modelId=EMBED_MODEL_ID,
            body=json.dumps(payload).encode("utf-8"),
            accept="application/json",
            contentType="application/json",
        )
        body = json.loads(resp["body"].read())
        vec = np.asarray(body["embeddings"][0], dtype=np.float32)
    except Exception as exc:
        raise BedrockUnavailable(f"embed failed: {exc}") from exc
    norm = float(np.linalg.norm(vec)) or 1.0
    return vec / norm


def _rerank(query: str, docs: List[str], top_n: int) -> List[Dict[str, Any]]:
    payload = {
        "query": query,
        "documents": docs,
        "top_n": min(top_n, len(docs)),
        "api_version": 2,
    }
    try:
        resp = _get_rerank_client().invoke_model(
            modelId=RERANK_MODEL_ID,
            body=json.dumps(payload).encode("utf-8"),
            accept="application/json",
            contentType="application/json",
        )
        body = json.loads(resp["body"].read())
    except Exception as exc:
        raise BedrockUnavailable(f"rerank failed: {exc}") from exc
    return body.get("results", [])


def _canon_key(key: str) -> str:
    return key[3:] if key.startswith("sf.") else key


def _dedupe_by_key(ranked: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
    """Collapse hits sharing the same canonical key, keep the best score, sort desc."""
    best: Dict[str, Dict[str, Any]] = {}
    for hit in ranked:
        canon = _canon_key(hit["key"])
        prev = best.get(canon)
        if prev is None or hit["score"] > prev["score"]:
            best[canon] = hit
    out = sorted(best.values(), key=lambda h: h["score"], reverse=True)
    return out[:top_n]


# --- Public API -------------------------------------------------------------

class SemanticFieldResolver:
    """Resolve a free-text field reference to ranked SF/qual field keys."""

    def __init__(self) -> None:
        self._index = _load_index()

    def _shortlist(self, qvec: np.ndarray, candidates: int) -> List[int]:
        sims = self._index["embeddings"] @ qvec  # already row-normalized
        if candidates >= sims.shape[0]:
            order = np.argsort(-sims)
        else:
            part = np.argpartition(-sims, candidates)[:candidates]
            order = part[np.argsort(-sims[part])]
        return [int(i) for i in order]

    def resolve(
        self,
        query: str,
        top_n: int = 5,
        candidates: int = 50,
    ) -> List[Dict[str, Any]]:
        if not query or not query.strip():
            return []
        qvec = _embed_query(query)
        shortlist_idx = self._shortlist(qvec, candidates)
        labels = self._index["labels"]
        keys = self._index["keys"]
        sources = self._index["sources"]
        docs = [labels[i] or keys[i] for i in shortlist_idx]
        # Over-fetch from rerank so post-dedupe we still have top_n unique keys.
        reranked = _rerank(query, docs, top_n=min(len(docs), max(top_n * 4, top_n)))

        # Dedupe by canonical key (strip "sf." prefix so e.g. "X" and "sf.X"
        # collapse — the caller normalizes both forms identically).
        ranked: List[Dict[str, Any]] = []
        for r in reranked:
            local_idx = int(r.get("index", -1))
            if local_idx < 0 or local_idx >= len(shortlist_idx):
                continue
            global_idx = shortlist_idx[local_idx]
            ranked.append({
                "key": keys[global_idx],
                "label": labels[global_idx],
                "source": sources[global_idx] if global_idx < len(sources) else "",
                "score": float(r.get("relevance_score", 0.0)),
            })
        return _dedupe_by_key(ranked, top_n)


def reset_caches_for_tests() -> None:
    """Clear lazy singletons — only for unit tests."""
    global _INDEX, _EMBED_CLIENT, _RERANK_CLIENT
    _INDEX = None
    _EMBED_CLIENT = None
    _RERANK_CLIENT = None
