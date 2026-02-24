# backend/app/services/sf_labels.py

from functools import lru_cache


@lru_cache(maxsize=64)
def describe_fields(sf, sobject: str) -> dict:
    """
    Devuelve { apiName: label } cacheado para un SObject.
    """
    desc = sf.rest.get(f"/services/data/v{sf.api_version}/sobjects/{sobject}/describe")
    return {f["name"]: f["label"] for f in desc["fields"]}


def humanize_headers(sf, sobject: str, keys: list[str]) -> list[dict]:
    """
    A partir de una lista de claves (posiblemente con puntos), construye columnas con:
      - deduplicado por el "base" (después del último '.')
      - label legible según describe() o fallback Title Case
    """
    labels = describe_fields(sf, sobject)
    out = []
    seen = set()
    for k in keys:
        base = k.split(".")[-1]
        if base in seen:
            continue
        seen.add(base)
        label = labels.get(base, base.replace("__c", "").replace("_", " ").title())
        out.append({"key": k, "label": label})
    return out