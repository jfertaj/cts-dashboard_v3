"""
Regression tests for QI02 LAB/mechanistic-studies routing fix.

Verifies that queries about "mechanistic studies" / "LAB-validated" with
site-oriented wording route to the dedicated site-parent-institution LAB
handler, NOT to the member search handler.

No DB, no network, no FastAPI — pure regex routing logic.
Run: pytest backend/tests/test_lab_site_routing.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import re
import pytest


# ── Routing flag helpers (mirror the exact logic from ai_chat.py) ──────────

def _qi02_lab_sites(s: str) -> bool:
    """True when the query should route to the LAB-sites handler (sites whose
    parent institution has the Research/Mechanistic Lab role)."""
    return bool(
        re.search(r"\bsite[s]?\b|\bcenter[s]?\b", s, re.I)
        and re.search(r"\bmechanistic\b|\blab.?validat\b|\bresearch.*lab\b", s, re.I)
        and not re.search(r"\binstitution[s]?\b|\bmember[s]?\b", s, re.I)
    )


def _mem_role_ctx(s: str) -> bool:
    """True when the query activates member-role context (would route to
    members handler if combined with institution/member keywords)."""
    return bool(re.search(
        r"\bdxlab[s]?\b|\bdiagnostic\s+lab[s]?\b|\bpatient\s+org(?:anization)?s?\b"
        r"|\bresearch.*lab[s]?\b|\bmechanistic.*lab[s]?\b|\blab.?validat\b"
        r"|\bcs\b.*\bvalidat|\bvalidat.*\bcs\b|\bcts\b.*\bvalidat|\bvalidat.*\bcts\b"
        r"|\brole[s]?\b", s, re.I))


def _mem_inst_intent(s: str) -> bool:
    """True when the query should route to the member/institution handler."""
    _mem_inst_kw = bool(re.search(
        r"\bmember\s+institution[s]?\b|\bmember\s+universit|\binstitution[s]?\s+(?:in|from|of)\b"
        r"|\bnetwork\s+member[s]?\b|\binnodia\s+member[s]?\b",
        s, re.I
    ))
    _role_ctx = _mem_role_ctx(s)
    return _mem_inst_kw or bool(
        _role_ctx and re.search(r"\binstitution[s]?\b|\bmember[s]?\b", s, re.I)
        and not re.search(r"\bcts\s+site[s]?\b", s, re.I)
        and not (re.search(r"\bclinical\s+site[s]?\b", s, re.I)
                 and not re.search(r"\bclinical\s+site\s+role\b|\bcs\s+role\b", s, re.I))
    )


def _st8_cts_dxlab(s: str) -> bool:
    """True when the query should route to the ST8 CTS+DxLab handler."""
    return bool(
        re.search(r"\bcts\s+site[s]?\b|\bclinical\s+trial\s+site[s]?\b", s, re.I)
        and re.search(r"\bdxlab[s]?\b|\bdiagnostic\s+lab[s]?\b|\blab.validat", s, re.I)
    )


# ══════════════════════════════════════════════════════════════════════════════
# A. Site-oriented LAB queries → must route to _qi02_lab_sites
# ══════════════════════════════════════════════════════════════════════════════

class TestLabSiteRouting:
    """Queries about mechanistic studies / LAB with site wording must route
    to the dedicated LAB-sites handler, NOT to member search."""

    def test_mechanistic_lab_validated_sites(self):
        s = "show me sites that can run mechanistic studies (lab-validated)"
        assert _qi02_lab_sites(s) is True
        assert _mem_inst_intent(s) is False

    def test_mechanistic_sites_short(self):
        """Without explicit 'lab-validated', 'mechanistic' alone triggers."""
        s = "which sites can do mechanistic studies?"
        assert _qi02_lab_sites(s) is True
        assert _mem_inst_intent(s) is False

    def test_research_lab_sites(self):
        s = "sites with research lab capability"
        assert _qi02_lab_sites(s) is True
        assert _mem_inst_intent(s) is False

    def test_mechanistic_centers(self):
        s = "which centers can run mechanistic studies?"
        assert _qi02_lab_sites(s) is True
        assert _mem_inst_intent(s) is False

    def test_lab_validated_sites_explicit(self):
        s = "show me lab validated sites for mechanistic studies"
        assert _qi02_lab_sites(s) is True
        assert _mem_inst_intent(s) is False


# ══════════════════════════════════════════════════════════════════════════════
# B. Site + LAB + country → must still use LAB-sites, not member fallback
# ══════════════════════════════════════════════════════════════════════════════

class TestLabSiteWithCountry:
    def test_lab_sites_in_germany(self):
        s = "mechanistic studies lab-validated sites in germany"
        assert _qi02_lab_sites(s) is True
        assert _mem_inst_intent(s) is False

    def test_lab_centers_france(self):
        s = "which centers in france can do mechanistic lab studies?"
        assert _qi02_lab_sites(s) is True


# ══════════════════════════════════════════════════════════════════════════════
# C. Member-oriented LAB queries → must use member path, NOT LAB-sites
# ══════════════════════════════════════════════════════════════════════════════

class TestMemberLabRouting:
    """When the query explicitly says 'member' or 'institution', the member
    handler should take priority, not the LAB-sites handler."""

    def test_members_with_mechanistic(self):
        s = "show me all research/mechanistic lab members in germany"
        assert _qi02_lab_sites(s) is False  # 'members' blocks LAB-sites
        assert _mem_inst_intent(s) is True

    def test_institutions_with_lab_role(self):
        s = "which institutions have a mechanistic lab role?"
        assert _qi02_lab_sites(s) is False
        assert _mem_inst_intent(s) is True

    def test_member_institutions_lab(self):
        s = "member institutions with research lab capability"
        assert _qi02_lab_sites(s) is False
        assert _mem_inst_intent(s) is True


# ══════════════════════════════════════════════════════════════════════════════
# D. ST8 CTS+DxLab → must keep working (regression guard)
# ══════════════════════════════════════════════════════════════════════════════

class TestST8DxLabRegression:
    """ST8 handler must fire for CTS + DxLab queries and NOT be intercepted
    by the new LAB-sites handler."""

    def test_cts_dxlab(self):
        s = "show me which cts sites are also validated diagnostic labs"
        assert _st8_cts_dxlab(s) is True
        # LAB-sites should also match (both have lab keyword), but ST8
        # fires first in the actual handler chain — this is order-dependent
        # in ai_chat.py, not regex-level. Just verify ST8 activates.

    def test_cts_sites_dxlab(self):
        s = "cts sites that are diagnostic labs"
        assert _st8_cts_dxlab(s) is True

    def test_dxlab_not_mechanistic(self):
        """A pure DxLab query without 'mechanistic' should NOT fire LAB-sites."""
        s = "find institutions that have a dxlab role"
        assert _qi02_lab_sites(s) is False
        assert _mem_inst_intent(s) is True


# ══════════════════════════════════════════════════════════════════════════════
# E. Negative cases — should NOT fire LAB-sites
# ══════════════════════════════════════════════════════════════════════════════

class TestLabSiteNegative:
    """Queries that mention labs but are NOT site-capability queries."""

    def test_plain_sites_no_lab(self):
        s = "show me all sites in italy"
        assert _qi02_lab_sites(s) is False

    def test_pharmacy_sites(self):
        s = "which sites have a pharmacy?"
        assert _qi02_lab_sites(s) is False

    def test_hla_sites(self):
        s = "sites with hla typing in france"
        assert _qi02_lab_sites(s) is False

    def test_lab_without_sites(self):
        """'lab-validated' alone without 'site'/'center' should not fire."""
        s = "is lab-validated available?"
        assert _qi02_lab_sites(s) is False
