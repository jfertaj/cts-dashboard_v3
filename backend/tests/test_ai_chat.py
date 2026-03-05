"""
Unit tests for ai_chat.py pure functions.
No DB, no network, no FastAPI — just Python logic.
Run: pytest backend/tests/test_ai_chat.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.routers.ai_chat import _is_complex_query, _truncate_history


# ──────────────────────────────────────────────────────────────────────────────
# _is_complex_query
# ──────────────────────────────────────────────────────────────────────────────

class TestIsComplexQuery:
    # ── Nearest + filter combos ──────────────────────────────────────────────
    def test_nearest_with_stage(self):
        assert _is_complex_query("nearest sites with stage 2") is True

    def test_closest_with_pharmacy(self):
        assert _is_complex_query("find the closest centers that have a pharmacy") is True

    def test_nearest_with_overnight(self):
        assert _is_complex_query("nearest 5 sites with overnight stay") is True

    def test_nearest_with_nd(self):
        assert _is_complex_query("nearest sites with ND > 10") is True

    def test_nearest_without_filter(self):
        # "nearest" alone — no qualifier → not complex (deterministic planner handles it)
        assert _is_complex_query("nearest sites to Madrid") is False

    # ── Two clinical concepts ────────────────────────────────────────────────
    def test_stage1_and_pharmacy(self):
        assert _is_complex_query("show sites with stage 1 that also have a pharmacy") is True

    def test_stage2_and_overnight(self):
        assert _is_complex_query("sites with stage 2 and overnight stay") is True

    def test_nd_and_hla(self):
        assert _is_complex_query("sites with ND and HLA typing") is True

    def test_pi_and_stage1(self):
        assert _is_complex_query("stage 1 sites with their PI contacts") is True

    def test_pharmacy_and_hla(self):
        assert _is_complex_query("sites with on-site pharmacy and HLA") is True

    def test_phase_and_stage(self):
        assert _is_complex_query("Phase I sites that also track stage 2") is True

    def test_assignment_and_stage(self):
        assert _is_complex_query("stage 1 sites with active MCA assignments") is True

    # ── Single concept — NOT complex ─────────────────────────────────────────
    def test_single_stage2(self):
        assert _is_complex_query("show sites with stage 2 > 0") is False

    def test_single_pharmacy(self):
        assert _is_complex_query("which sites have a pharmacy?") is False

    def test_single_nd(self):
        assert _is_complex_query("sites with newly diagnosed > 5") is False

    def test_single_country(self):
        assert _is_complex_query("sites in Italy") is False

    def test_simple_count(self):
        assert _is_complex_query("how many INNODIA sites are there?") is False

    # ── Multi-condition connectors ───────────────────────────────────────────
    # Note: connector regex matches \bsite\b (singular) or \bcenter\b or \baccount\b
    def test_and_also_with_site_singular(self):
        assert _is_complex_query("show site in Spain and also with coordinator contacts") is True

    def test_and_with_center(self):
        assert _is_complex_query("show German center and with their accounts") is True

    def test_connector_without_site_noun_not_complex(self):
        # connector present but no site/center/account word → not complex
        assert _is_complex_query("I need data and also want charts") is False

    # ── Spanish-language queries ─────────────────────────────────────────────
    def test_spanish_nearest(self):
        assert _is_complex_query("centros más próximos a Barcelona que tengan stage 2") is True

    def test_spanish_cerca_de(self):
        assert _is_complex_query("cerca de Madrid con pharmacy") is True

    # ── Edge cases ───────────────────────────────────────────────────────────
    def test_empty_string(self):
        assert _is_complex_query("") is False

    def test_none_value(self):
        assert _is_complex_query(None) is False  # type: ignore

    def test_case_insensitive(self):
        assert _is_complex_query("Sites with STAGE 1 and HLA Typing") is True

    def test_newly_diagnosed_full(self):
        # "newly diagnosed" written out (not abbreviation)
        assert _is_complex_query("sites with newly diagnosed patients and HLA") is True


# ──────────────────────────────────────────────────────────────────────────────
# _truncate_history
# ──────────────────────────────────────────────────────────────────────────────

class TestTruncateHistory:
    def _msgs(self, pairs):
        """Build list of dict messages from (role, content) tuples."""
        return [{"role": r, "content": c} for r, c in pairs]

    # ── No truncation needed ─────────────────────────────────────────────────
    def test_short_history_unchanged(self):
        msgs = self._msgs([("user", "q1"), ("assistant", "a1"), ("user", "q2")])
        result = _truncate_history(msgs, max_turns=3)
        assert result == msgs

    def test_empty_history(self):
        assert _truncate_history([], max_turns=3) == []

    def test_single_user_message(self):
        msgs = self._msgs([("user", "hello")])
        assert _truncate_history(msgs, max_turns=3) == msgs

    def test_exact_turns_unchanged(self):
        msgs = self._msgs([
            ("user", "q1"), ("assistant", "a1"),
            ("user", "q2"), ("assistant", "a2"),
            ("user", "q3"), ("assistant", "a3"),
        ])
        result = _truncate_history(msgs, max_turns=3)
        assert result == msgs

    # ── Truncation ───────────────────────────────────────────────────────────
    def test_truncates_oldest_turns(self):
        msgs = self._msgs([
            ("user", "q1"), ("assistant", "a1"),
            ("user", "q2"), ("assistant", "a2"),
            ("user", "q3"), ("assistant", "a3"),
            ("user", "q4"), ("assistant", "a4"),
        ])
        result = _truncate_history(msgs, max_turns=2)
        # Should keep last 2 user turns: q3,a3,q4,a4
        assert len([m for m in result if m["role"] == "user"]) == 2
        assert result[0]["content"] == "q3"
        assert result[-1]["content"] == "a4"

    def test_truncates_to_1_turn(self):
        msgs = self._msgs([
            ("user", "q1"), ("assistant", "a1"),
            ("user", "q2"), ("assistant", "a2"),
            ("user", "q3"),
        ])
        result = _truncate_history(msgs, max_turns=1)
        assert len([m for m in result if m["role"] == "user"]) == 1
        assert result[0]["content"] == "q3"

    def test_keeps_trailing_assistant_message(self):
        msgs = self._msgs([
            ("user", "q1"), ("assistant", "a1"),
            ("user", "q2"), ("assistant", "a2"),
            ("user", "q3"), ("assistant", "a3"),
        ])
        result = _truncate_history(msgs, max_turns=1)
        # Last user + assistant
        assert result[0]["content"] == "q3"
        assert result[1]["content"] == "a3"

    # ── Object-style messages (with .role attribute) ─────────────────────────
    def test_object_messages_with_role_attr(self):
        class Msg:
            def __init__(self, role, content):
                self.role = role
                self.content = content

        msgs = [
            Msg("user", "q1"), Msg("assistant", "a1"),
            Msg("user", "q2"), Msg("assistant", "a2"),
            Msg("user", "q3"), Msg("assistant", "a3"),
        ]
        result = _truncate_history(msgs, max_turns=2)
        assert len(result) == 4
        assert result[0].content == "q2"

    # ── Mixed content (assistant-only messages don't count as turns) ─────────
    def test_only_user_turns_counted(self):
        # If there are many assistant messages, they shouldn't inflate turn count
        msgs = self._msgs([
            ("user", "q1"),
            ("assistant", "a1"), ("assistant", "a1b"),  # two assistant msgs
            ("user", "q2"), ("assistant", "a2"),
        ])
        result = _truncate_history(msgs, max_turns=2)
        # 2 user turns — no truncation needed
        assert result == msgs

    def test_default_max_turns_applied(self):
        # Default is MAX_HISTORY_TURNS (12); with 5 user turns, no truncation
        msgs = self._msgs(
            [(r, f"m{i}") for i, r in enumerate(
                ["user", "assistant"] * 5
            )]
        )
        result = _truncate_history(msgs)  # uses default max_turns
        assert result == msgs
