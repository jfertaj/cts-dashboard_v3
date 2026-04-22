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
