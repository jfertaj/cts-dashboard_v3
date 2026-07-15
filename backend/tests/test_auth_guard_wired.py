"""App-level tests that the require_user guard is actually wired into the app.

test_require_user.py tests the dependency in isolation; this file asserts the
guard is attached to the real routers via TestClient, so that dropping
``dependencies=_GUARD`` from an include_router call fails CI instead of silently
shipping an auth bypass.

Env is set BEFORE importing the app: app.main reads APP_SESSION_SECRET at module
scope, and require_user reads AUTH_DISABLED at request time. A 401 short-circuits
before any DB/Salesforce call, so these tests are deterministic and offline.
"""
import os

os.environ.setdefault("APP_SESSION_SECRET", "test-secret")
os.environ.pop("AUTH_DISABLED", None)  # guard must be active for the 401 assertion

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

_GUARDED_ROUTE = "/api/explorer/search"
_GUARDED_BODY = {"logic": "AND", "rules": []}


def test_guarded_route_401_without_session(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    resp = client.post(_GUARDED_ROUTE, json=_GUARDED_BODY)
    assert resp.status_code == 401


def test_public_route_reachable():
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200


def test_guarded_route_not_401_when_auth_disabled(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    # raise_server_exceptions=False: past the guard the handler hits DB/SF and
    # errors out; we only care the guard let it through (not 401), not the
    # downstream outcome. This keeps the test offline and deterministic.
    bypass_client = TestClient(app, raise_server_exceptions=False)
    resp = bypass_client.post(_GUARDED_ROUTE, json=_GUARDED_BODY)
    assert resp.status_code != 401
