"""Focused TestClient tests for the /api/auth (Entra SSO) router.

Importing app.main requires APP_SESSION_SECRET (fail-fast in prod via Secrets
Manager), so it is set at module scope before the import below.
"""
import os

os.environ.setdefault("APP_SESSION_SECRET", "test-secret")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
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
