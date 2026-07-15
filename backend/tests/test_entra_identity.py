from app.services import entra_oauth as e


def test_extract_prefers_email_then_preferred_username():
    ui = {"email": "a@innodia.org", "preferred_username": "b@innodia.org",
          "name": "A", "oid": "oid-1"}
    assert e.extract_identity(ui) == {"email": "a@innodia.org", "name": "A", "oid": "oid-1"}


def test_extract_falls_back_to_preferred_username():
    ui = {"preferred_username": "b@innodia.org", "name": "B", "oid": "oid-2"}
    out = e.extract_identity(ui)
    assert out["email"] == "b@innodia.org"


def test_is_innodia():
    assert e.is_innodia("x@innodia.org") is True
    assert e.is_innodia("x@gmail.com") is False
