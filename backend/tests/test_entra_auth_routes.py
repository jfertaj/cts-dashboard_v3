"""Focused TestClient tests for the /api/auth (Entra SSO) router.

Importing app.main requires APP_SESSION_SECRET (fail-fast in prod via Secrets
Manager), so it is set at module scope before the import below.
"""
import os

os.environ.setdefault("APP_SESSION_SECRET", "test-secret")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.routers import entra_auth as ea  # noqa: E402
from app.routers.entra_auth import _safe_next  # noqa: E402

client = TestClient(app)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/", "/"),
        ("/explorer", "/explorer"),
        ("/a/b?x=1", "/a/b?x=1"),
        (None, "/"),          # missing
        ("", "/"),            # empty
        ("@evil.com", "/"),   # userinfo open-redirect trick
        ("//evil.com", "/"),  # protocol-relative
        ("/\\evil.com", "/"), # backslash host trick
        ("https://evil.com", "/"),
    ],
)
def test_safe_next_blocks_open_redirects(raw, expected):
    assert _safe_next(raw) == expected


def test_me_unauthenticated_returns_false(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_me_auth_disabled_returns_dev_user(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json() == {
        "authenticated": True,
        "email": "dev@innodia.org",
        "name": "Dev User",
    }


def test_logout_clears_cookie_and_returns_ok(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    resp = client.get("/api/auth/logout")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # delete_cookie emits a Set-Cookie that expires the session cookie.
    assert "cts_session" in resp.headers.get("set-cookie", "")


def test_callback_innodia_sets_session_cookie_and_redirects(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    token = {"userinfo": {"email": "user@innodia.org", "name": "User", "oid": "oid-1"}}
    with patch.object(ea.oauth.entra, "authorize_access_token", new=AsyncMock(return_value=token)), \
         patch.object(ea, "create_session", return_value="sid-123"):
        resp = client.get("/api/auth/callback?code=x&state=y", follow_redirects=False)
    assert resp.status_code == 302
    assert "auth_error" not in resp.headers["location"]
    assert "cts_session" in resp.headers.get("set-cookie", "")


def test_callback_non_innodia_redirects_with_error_and_no_cookie(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    token = {"userinfo": {"email": "user@gmail.com", "name": "U", "oid": "o"}}
    with patch.object(ea.oauth.entra, "authorize_access_token", new=AsyncMock(return_value=token)), \
         patch.object(ea, "create_session", return_value="sid-x") as mk_sess:
        resp = client.get("/api/auth/callback?code=x&state=y", follow_redirects=False)
    assert "auth_error=not_innodia" in resp.headers["location"]
    assert "cts_session" not in resp.headers.get("set-cookie", "")
    mk_sess.assert_not_called()


def test_callback_exchange_failure_redirects_login_failed(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    boom = AsyncMock(side_effect=Exception("bad state/nonce"))
    with patch.object(ea.oauth.entra, "authorize_access_token", new=boom):
        resp = client.get("/api/auth/callback?code=x&state=y", follow_redirects=False)
    assert "auth_error=login_failed" in resp.headers["location"]
    assert "cts_session" not in resp.headers.get("set-cookie", "")
