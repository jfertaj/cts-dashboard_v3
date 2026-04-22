"""Tool-choice policy helpers for Moby.

Pure, stateless helpers used by _agentic_loop to decide when to force a
table-returning tool call. Importing this module must have no side effects
and no external dependencies beyond the standard library.
"""
from __future__ import annotations

import unicodedata


TABLE_RETURNING_TOOLS: frozenset[str] = frozenset({
    "explorer_search",
    "nearest_filtered_sites",
    "study_coordinators_with_activities",
    "members_search",
})


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
    return any(p in norm for p in _POSITIVES)
