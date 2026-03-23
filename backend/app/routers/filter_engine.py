"""Pure filter evaluation functions — no Salesforce, DB, or network dependencies."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, Field


# ======================= DATE / SCALAR HELPERS =======================

_DATE_FMTS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _parse_date_any(x: Any) -> Optional[datetime]:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x
    s = str(x).strip()
    if not s:
        return None
    # Primero intenta ISO completo (no recortar la cadena)
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass  # <- NECESARIO: sin esto, el 'except' queda vacío y da IndentationError

    # Luego intenta los formatos conocidos (sin slicing)
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _coerce_scalar(x: Any) -> Tuple[Any, str]:
    """ Devuelve (valor, tipo) con tipo in {'none','bool','num','date','str'} """
    if x is None:
        return None, "none"
    if isinstance(x, bool):
        return x, "bool"
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return x, "num"
    s = str(x).strip()
    if s.lower() in ("true", "false"):
        return (s.lower() == "true"), "bool"
    if _NUM_RE.match(s or ""):
        try:
            return (float(s) if "." in s else int(s)), "num"
        except Exception:
            pass
    d = _parse_date_any(s)
    if d is not None:
        return d, "date"
    return s, "str"


def _cmp(a: Any, b: Any) -> int:
    if type(a) is type(b):
        return 0 if a == b else (-1 if a < b else 1)
    sa, sb = str(a), str(b)
    return 0 if sa == sb else (-1 if sa < sb else 1)


def _normalize_list(v: Any) -> Iterable[Any]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return v
    s = str(v)
    return [p.strip() for p in s.split(",")] if "," in s else [v]


def _eval_qual_rule(value: Any, op: str, raw: Any) -> bool:
    op = (op or "").lower()
    v, tv = _coerce_scalar(value)

    # Null checks
    if op in ("is_null","isnull"):
        return v is None or (tv == "str" and str(v).strip() == "")
    if op in ("is_not_null","notnull"):
        return not (v is None or (tv == "str" and str(v).strip() == ""))

    # BETWEEN
    if op == "between":
        lo, hi = None, None
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            lo, hi = raw
        else:
            s = str(raw or "")
            lo, hi = (s.split("..",1) + [None])[:2] if ".." in s else (s.split(",",1) + [None])[:2]
        lo_v, _ = _coerce_scalar(lo); hi_v, _ = _coerce_scalar(hi)
        if v is None or lo_v is None or hi_v is None:
            return False
        return _cmp(lo_v, v) <= 0 and _cmp(v, hi_v) <= 0

    # IN / NOT IN
    if op in ("in","not_in"):
        items = [_coerce_scalar(x)[0] for x in _normalize_list(raw)]
        present = any(_cmp(v, it) == 0 for it in items)
        return present if op == "in" else (not present)

    # Binarios escalares
    tgt, _ = _coerce_scalar(raw)
    if op in ("equals","=","eq"):
        return _cmp(v, tgt) == 0
    if op in ("not_equals","!=","ne"):
        return _cmp(v, tgt) != 0
    # Comparison operators: None values never satisfy gt/gte/lt/lte
    # (guards against str("None") > str("0") lexicographic false positives)
    if op in ("gt",">"):
        return v is not None and _cmp(v, tgt) > 0
    if op in ("gte",">="):
        return v is not None and _cmp(v, tgt) >= 0
    if op in ("lt","<"):
        return v is not None and _cmp(v, tgt) < 0
    if op in ("lte","<="):
        return v is not None and _cmp(v, tgt) <= 0

    # Empty / not-empty
    if op in ("is_empty",):
        return v is None or (isinstance(v, str) and v.strip() == "")
    if op in ("is_not_empty",):
        return not (v is None or (isinstance(v, str) and v.strip() == ""))

    # Strings
    vs = "" if v is None else str(v)
    ts = "" if tgt is None else str(tgt)
    if op == "contains":       return ts.lower() in vs.lower()
    if op == "not_contains":   return ts.lower() not in vs.lower()
    if op == "starts_with":    return vs.lower().startswith(ts.lower())
    if op == "ends_with":      return vs.lower().endswith(ts.lower())

    return True


def _qual_get(qual_data: Dict[str, Any], key: str) -> Any:
    """
    Lookup de una clave qual en el JSONB con 5 fallbacks, para manejar
    los distintos formatos históricos de almacenamiento:
      1. Clave exacta:          "3_5_4__fridge__device_available"
      2. Primer _ → punto:      "3.5_4__fridge__device_available"  (compat. legacy)
      3. Todos los _ del prefijo de sección → puntos:
                                "3.5.4__fridge__device_available"
         Cubre uploads recientes donde el subsection code usa puntos (3.5.4) en lugar de
         guiones bajos (3_5_4). Crítico para secciones 3.5.x y 3.7.x.
      4. Clave base sin prefijo: "fridge__device_available"
      5. Base sin prefijo y sin último sufijo _word:
         "3_8__autoantibodies_aab" -> "autoantibodies"
         "3_3__estimate_arrival_time_of_emergency_personnel_min" -> "estimate_arrival_time_of_emergency_personnel"
    """
    v = qual_data.get(key)
    if v is None and "_" in key:
        # Fallback 2: replace only first underscore with dot
        v = qual_data.get(key.replace("_", ".", 1))
    if v is None and "__" in key:
        # Fallback 3: replace ALL underscores in the section prefix with dots
        section_part, _, rest = key.partition("__")
        section_dotted = section_part.replace("_", ".")
        if section_dotted != section_part:
            v = qual_data.get(f"{section_dotted}__{rest}")
        # Fallback 4: strip section prefix entirely → base slug
        if v is None:
            v = qual_data.get(rest)
        # Fallback 5: strip last underscore-word suffix from base slug
        if v is None:
            last_sep = rest.rfind("_")
            if last_sep > 0:
                v = qual_data.get(rest[:last_sep])
    return v


# ======================= MODELOS Pydantic (request) =======================

class Rule(BaseModel):
    field: str
    operator: str
    value: Any

class FilterQuery(BaseModel):
    logic: str = "AND"  # "AND", "OR", or expression like "1 AND (2 OR 3)"
    rules: List[Rule] = Field(default_factory=list)
    columns: List[str] = Field(default_factory=list)


# ======================= WHERE BUILDER (SOQL) =======================

_OP_SYNONYM = {
    "eq": "equals",
    "neq": "not_equals",
    "icontains": "contains",
    "≥": ">=",
    "≤": "<=",
    "gte": ">=",
    "lte": "<=",
    "gt": ">",
    "lt": "<",
}

_OP_MAP = {
    "equals": "=", "not_equals": "!=",
    ">": ">", ">=": ">=", "<": "<", "<=": "<=",
}


def _flatten_filter_rules(group: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Recursively flatten nested FilterGroup sub-groups into a single list of leaf rules.

    FilterBuilder can produce nested groups like:
        {logic:AND, rules:[{logic:OR, rules:[{site.country=Spain}, {site.country=PT}]}, ...]}
    The classification loops only handle flat rules. This helper extracts all leaf rules
    so nested groups are not silently ignored during classification.
    Note: nested group logic is not preserved — the parent's logic applies after flattening.
    For site.country rules this is fine because the multi-country AND→OR fix handles them.
    """
    result: List[Dict[str, Any]] = []
    for item in (group.get("rules") or []):
        if "rules" in item and not item.get("field"):  # sub-group
            result.extend(_flatten_filter_rules(item))
        else:
            result.append(item)
    return result


def _filter_group_to_expr(fg: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a nested FilterGroup (sub-groups inside rules) to expression format.

    Nested sub-groups lose their inner logic when flattened.  This function
    converts the tree into a numbered expression string so the existing
    ``_eval_logic_expr_be`` evaluator can honour every AND/OR correctly.

    Example::

        {logic:"AND", rules:[{logic:"OR", rules:[ruleA, ruleB]}, ruleC]}
        → {logic:"(1 OR 2) AND 3", rules:[ruleA, ruleB, ruleC]}

    If the group has no nested sub-groups, or is already in expression format
    (logic contains a digit), it is returned unchanged.
    """
    rules = fg.get("rules") or []
    logic = fg.get("logic") or "AND"

    # Already an expression string — leave as-is
    if _is_logic_expr(logic):
        return fg

    # No nested sub-groups — nothing to convert
    has_sub_groups = any("rules" in r and not r.get("field") for r in rules)
    if not has_sub_groups:
        return fg

    flat_rules: List[Dict[str, Any]] = []

    def _build_expr(node: Dict[str, Any]) -> str:
        node_logic = node.get("logic") or "AND"
        node_rules = node.get("rules") or []
        parts = []
        for rule in node_rules:
            if "rules" in rule and not rule.get("field"):  # sub-group
                sub_expr = _build_expr(rule)
                parts.append(f"({sub_expr})")
            else:
                flat_rules.append(rule)
                parts.append(str(len(flat_rules)))
        if not parts:
            return "1"  # degenerate empty group
        return f" {node_logic} ".join(parts)

    expr = _build_expr(fg)
    return {"logic": expr, "rules": flat_rules}


def _is_logic_expr(logic: str) -> bool:
    """Returns True if logic is a Salesforce-style expression like '1 AND (2 OR 3)'
    rather than a simple 'AND' or 'OR'."""
    return bool(re.search(r"\d", logic or ""))


def _eval_logic_expr_be(expr: str, results: Dict[int, bool]) -> bool:
    """Evaluate a Salesforce-style logic expression like '1 AND (2 OR 3)'.

    results: dict mapping 1-indexed rule numbers to bool.
             Unknown indices (rules in other categories or SF-filtered rules) default to True.

    Operator precedence: AND binds tighter than OR (standard logic).
    """
    tokens: List[Any] = []
    i = 0
    s = expr.strip()
    while i < len(s):
        c = s[i]
        if c in " \t\n\r":
            i += 1; continue
        if c == "(":
            tokens.append("LPAREN"); i += 1; continue
        if c == ")":
            tokens.append("RPAREN"); i += 1; continue
        if c.isdigit():
            n = ""
            while i < len(s) and s[i].isdigit():
                n += s[i]; i += 1
            tokens.append(("NUM", int(n))); continue
        if s[i:i+3].upper() == "AND" and (i+3 >= len(s) or not s[i+3].isalnum()):
            tokens.append("AND"); i += 3; continue
        if s[i:i+2].upper() == "OR" and (i+2 >= len(s) or not s[i+2].isalnum()):
            tokens.append("OR"); i += 2; continue
        i += 1  # skip unexpected chars gracefully

    tokens.append("EOF")
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else "EOF"

    def consume():
        t = tokens[pos[0]]
        pos[0] += 1
        return t

    def parse_expr():
        left = parse_term()
        while peek() == "OR":
            consume()
            right = parse_term()
            left = left or right
        return left

    def parse_term():
        left = parse_factor()
        while peek() == "AND":
            consume()
            right = parse_factor()
            left = left and right
        return left

    def parse_factor():
        t = peek()
        if t == "LPAREN":
            consume()
            v = parse_expr()
            if peek() == "RPAREN":
                consume()
            return v
        if isinstance(t, tuple) and t[0] == "NUM":
            consume()
            return results.get(t[1], True)  # unknown rule → True (don't exclude)
        if t == "EOF":
            return True
        consume()
        return True

    try:
        return bool(parse_expr())
    except Exception:
        return True  # fail open: don't exclude rows if expression fails


def _norm_label_to_key(x: Any) -> str:
    if isinstance(x, dict):
        x = x.get("key") or x.get("value") or x.get("id") or ""
    s = str(x or "").strip()
    if s.startswith("[sf] "):   s = s[5:]
    if s.startswith("[site] "): s = s[8:]
    if s.startswith("[qual] "): s = s[7:]
    return s
