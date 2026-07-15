from unittest.mock import patch, MagicMock
from app.routers import salesforce_explorer as se


def test_get_sf_returns_service_client_ignoring_request():
    fake = MagicMock(name="ServiceSF")
    with patch.object(se, "get_service_sf", return_value=fake) as g:
        out = se._get_sf(request=None)
    g.assert_called_once()
    assert out is fake
