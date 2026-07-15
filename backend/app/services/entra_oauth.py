from __future__ import annotations

import os
from typing import Dict

from authlib.integrations.starlette_client import OAuth

REDIRECT_PATH = "/api/auth/callback"
FRONTEND_BASE = os.getenv("FRONTEND_BASE", "http://localhost:5173").rstrip("/")
_TENANT = os.getenv("ENTRA_TENANT_ID", "")

oauth = OAuth()
oauth.register(
    name="entra",
    server_metadata_url=(
        f"https://login.microsoftonline.com/{_TENANT}/v2.0/.well-known/openid-configuration"
    ),
    client_id=os.getenv("ENTRA_CLIENT_ID", ""),
    client_secret=os.getenv("ENTRA_CLIENT_SECRET", ""),
    client_kwargs={"scope": "openid profile email"},
)


def extract_identity(userinfo: Dict) -> Dict:
    email = userinfo.get("email") or userinfo.get("preferred_username") or ""
    return {"email": email, "name": userinfo.get("name"), "oid": userinfo.get("oid")}


def is_innodia(email: str) -> bool:
    return email.lower().endswith("@innodia.org")
