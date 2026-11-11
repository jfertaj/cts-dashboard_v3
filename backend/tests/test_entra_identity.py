import pytest

from app.services import entra_oauth as e


def test_member_is_identified_by_sign_in_name():
    ui = {"preferred_username": "Ana.Perez@INNODIA.org", "email": "other@example.com",
          "name": "Ana", "oid": "oid-1"}
    assert e.member_identity(ui) == {"email": "ana.perez@innodia.org", "name": "Ana", "oid": "oid-1"}


def test_guest_with_innodia_email_claim_is_refused():
    ui = {"preferred_username": "someone_gmail.com#EXT#@innodiavzw.onmicrosoft.com",
          "email": "someone@innodia.org", "name": "G", "oid": "o"}
    assert e.member_identity(ui) is None
    assert e.member_identity({"preferred_username": "x_evil.com#EXT#@innodia.org"}) is None


def test_email_claim_alone_never_grants_access():
    assert e.member_identity({"email": "boss@innodia.org", "name": "B", "oid": "o"}) is None
    assert e.member_identity({}) is None


@pytest.mark.parametrize("name", [
    "a@innodia.eu", "a@innodia.org.evil.com", "a@evil.com@innodia.org", "@innodia.org",
    "a b@innodia.org", "x@gmail.com", "", 42, None,
])
def test_other_domains_and_malformed_names_are_refused(name):
    assert e.member_identity({"preferred_username": name}) is None
