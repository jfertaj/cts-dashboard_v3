import pytest
from types import SimpleNamespace
from fastapi import HTTPException
from unittest.mock import patch
from app.deps import auth as a


def _req(cookies=None):
    return SimpleNamespace(cookies=cookies or {})


def test_bypass_when_auth_disabled(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    assert a.require_user(_req())["email"] == "dev@innodia.org"


def test_missing_cookie_401(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    with pytest.raises(HTTPException) as ei:
        a.require_user(_req())
    assert ei.value.status_code == 401


def test_valid_session(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    with patch.object(a, "unsign_value", return_value="sid-1"), \
         patch.object(a, "get_session", return_value={"email": "u@innodia.org", "name": "U", "oid": "o"}):
        out = a.require_user(_req({a.COOKIE_NAME: "signed"}))
    assert out["email"] == "u@innodia.org"


def test_expired_session_401(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    with patch.object(a, "unsign_value", return_value="sid-1"), \
         patch.object(a, "get_session", return_value=None):
        with pytest.raises(HTTPException) as ei:
            a.require_user(_req({a.COOKIE_NAME: "signed"}))
    assert ei.value.status_code == 401
