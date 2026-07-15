import pytest
from app.services import app_session as s


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", "unit-test-secret")
    s._reload_signer()
    yield


def test_sign_unsign_roundtrip():
    signed = s.sign_value("abc-123")
    assert signed != "abc-123"
    assert s.unsign_value(signed) == "abc-123"


def test_tampered_value_rejected():
    signed = s.sign_value("abc-123")
    assert s.unsign_value(signed + "x") is None


def test_cookie_name_is_cts_session():
    assert s.COOKIE_NAME == "cts_session"
