#!/usr/bin/env python3
"""
Build a semantic field index for Moby's NLP resolver.

Embeds every alias known to `_build_knowledge_index()` (SF curated + site_qual +
profiling_kv + manual aliases) using AWS Bedrock + Cohere Embed multilingual v3.

Output: backend/app/cache/field_index.json

Usage (do NOT auto-run; this costs real Bedrock $):
    AWS_PROFILE=juan python scripts/build_field_index_cohere.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import boto3
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
CACHE_DIR = BACKEND_DIR / "app" / "cache"
OUT_PATH = CACHE_DIR / "field_index.json"

# Bedrock config
EMBED_REGION = os.environ.get("BEDROCK_EMBED_REGION", "eu-west-1")
EMBED_MODEL_ID = "cohere.embed-multilingual-v3"
EMBED_DIM = 1024
BATCH_SIZE = 96  # Cohere Bedrock cap

# Make `app.*` importable
sys.path.insert(0, str(BACKEND_DIR))


def _load_index_aliases() -> List[Dict[str, str]]:
    """
    Reproduce `_build_knowledge_index()` output as a flat alias list.

    DB-dependent sources (site_qual, profiling_kv) are skipped here — those
    move into the index at runtime if needed. POC focuses on SF curated +
    qual aliases + manual SF/qual maps, which are static.
    """
    from app.routers.ai_chat import (
        _load_sf_fields,
        _load_qual_aliases,
        _normalize,
    )

    fused: Dict[str, Dict[str, str]] = {}

    # 1) SF curated
    fused.update(_load_sf_fields())

    # 2) Qual aliases
    for alias, key in _load_qual_aliases().items():
        fused[alias] = {"source": "site_qual", "key": key}

    # 3) Manual SF + qual aliases — re-import the dicts by importing the
    # module-level constants is not possible (they live inside the function),
    # so we duplicate the small set here. Kept small on purpose.
    manual = _manual_aliases(_normalize)
    for alias, meta in manual.items():
        fused[alias] = meta

    entries: List[Dict[str, str]] = []
    for alias, meta in fused.items():
        label = meta.get("label") or meta.get("field") or meta.get("key") or alias
        entries.append({
            "alias": alias,
            "label": str(label),
            "source": meta.get("source", ""),
            "key": str(meta.get("field") or meta.get("key") or ""),
        })
    return entries


def _manual_aliases(norm) -> Dict[str, Dict[str, str]]:
    """Subset of manual aliases declared inside `_build_knowledge_index`."""
    nd_o18 = {"source": "sf", "field": "C_Number_of_new_T1D_diagnosed_O_18__c",
              "label": "New T1D diagnosed >=18 (last year)"}
    nd_u18 = {"source": "sf", "field": "C_Number_of_new_T1D_diagnosed_U_18__c",
              "label": "New T1D diagnosed <18 (last year)"}
    cu18 = {"source": "sf", "field": "C_Number_of_T1D_Patients_currently_U_18__c",
            "label": "T1D patients currently <18"}
    co18 = {"source": "sf", "field": "C_Number_of_T1D_Patients_currently_O_18__c",
            "label": "T1D patients currently >=18"}
    s1 = {"source": "sf", "field": "C_Number_of_Stage1_Individuals_followed__c",
          "label": "Stage 1 individuals followed"}
    s2 = {"source": "sf", "field": "C_Number_of_Stage2_Individuals_followed__c",
          "label": "Stage 2 individuals followed"}
    return {
        norm("Stage 1"): s1, norm("Stage 2"): s2,
        norm("ND"): nd_o18, norm("ND <18"): nd_u18, norm("ND >=18"): nd_o18,
        norm("newly diagnosed"): nd_o18,
        norm("T1D patients under 18"): cu18,
        norm("T1D patients over 18"): co18,
    }


def _embed_batch(client, texts: List[str]) -> List[List[float]]:
    payload = {
        "texts": texts,
        "input_type": "search_document",
        "truncate": "END",
    }
    resp = client.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps(payload).encode("utf-8"),
        accept="application/json",
        contentType="application/json",
    )
    body = json.loads(resp["body"].read())
    return body["embeddings"]


def _normalize_rows(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def main() -> int:
    entries = _load_index_aliases()
    print(f"[build] {len(entries)} aliases collected")

    # Embed the human-readable label (richer signal than the normalized alias)
    texts = [e["label"] or e["alias"] for e in entries]

    session = boto3.Session(region_name=EMBED_REGION)
    client = session.client("bedrock-runtime")

    all_vecs: List[List[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        chunk = texts[i:i + BATCH_SIZE]
        try:
            vecs = _embed_batch(client, chunk)
        except Exception as exc:
            print(f"[build] batch {i} failed: {exc}", file=sys.stderr)
            return 1
        all_vecs.extend(vecs)
        print(f"[build] embedded {i + len(chunk)}/{len(texts)}")

    mat = np.asarray(all_vecs, dtype=np.float32)
    if mat.shape[1] != EMBED_DIM:
        print(f"[build] WARN: unexpected dim {mat.shape[1]}", file=sys.stderr)
    mat = _normalize_rows(mat)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "model": EMBED_MODEL_ID,
        "dim": int(mat.shape[1]),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "keys": [e["key"] for e in entries],
        "labels": [e["label"] for e in entries],
        "aliases": [e["alias"] for e in entries],
        "sources": [e["source"] for e in entries],
        "embeddings": mat.tolist(),
    }
    OUT_PATH.write_text(json.dumps(payload), encoding="utf-8")
    print(f"[build] wrote {OUT_PATH} ({mat.shape[0]} rows x {mat.shape[1]}d)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
