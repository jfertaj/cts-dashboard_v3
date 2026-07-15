import time
import pytest
from unittest.mock import patch, MagicMock
from app.services import salesforce_service as svc


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SF_MY_DOMAIN", "https://innodiaivzw.my.salesforce.com")
    monkeypatch.setenv("SF_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("SF_TOKEN_TTL_SECONDS", "3600")
    svc.reset_service_sf_cache()
    yield
    svc.reset_service_sf_cache()


def _fake_token_response():
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {
        "access_token": "TOK1",
        "instance_url": "https://innodiaivzw.my.salesforce.com",
        "issued_at": "0",
    }
    r.raise_for_status.return_value = None
    return r


def test_mint_posts_client_credentials_in_body():
    with patch.object(svc.httpx, "post", return_value=_fake_token_response()) as p, \
         patch.object(svc, "Salesforce", return_value=MagicMock()):
        svc.get_service_sf()
    url = p.call_args.args[0]
    data = p.call_args.kwargs["data"]
    assert url == "https://innodiaivzw.my.salesforce.com/services/oauth2/token"
    assert data["grant_type"] == "client_credentials"
    assert data["client_id"] == "cid"
    assert data["client_secret"] == "csecret"


def test_token_is_cached_across_calls():
    with patch.object(svc.httpx, "post", return_value=_fake_token_response()) as p, \
         patch.object(svc, "Salesforce", return_value=MagicMock()):
        svc.get_service_sf()
        svc.get_service_sf()
    assert p.call_count == 1


def test_token_reminted_after_ttl(monkeypatch):
    monkeypatch.setenv("SF_TOKEN_TTL_SECONDS", "1")
    svc.reset_service_sf_cache()
    with patch.object(svc.httpx, "post", return_value=_fake_token_response()) as p, \
         patch.object(svc, "Salesforce", return_value=MagicMock()):
        svc.get_service_sf()
        svc._CACHE["expires_at"] = time.time() - 1  # force expiry
        svc.get_service_sf()
    assert p.call_count == 2


def test_secret_never_in_repr():
    with patch.object(svc.httpx, "post", return_value=_fake_token_response()), \
         patch.object(svc, "Salesforce", return_value=MagicMock()):
        sf = svc.get_service_sf()
    assert "csecret" not in repr(sf)
