from __future__ import annotations

import os
import re
from typing import Dict, Optional

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


ALLOWED_DOMAIN = "@innodia.org"  # never innodia.eu
_SIGN_IN_NAME = re.compile(r"^[^@\s]+@[^@\s]+$")


def member_identity(userinfo: Dict) -> Optional[Dict]:
    """The signed-in INNODIA member, or None when the account is not one.

    Only the sign-in name (``preferred_username``, the UPN) is trusted: its domain
    has to be one the tenant has verified, and a guest's contains ``#EXT#``. The
    ``email`` claim is ignored on purpose. Microsoft documents it as mutable and
    unverified ("Never use it for authorization"), and the tenant holds guest
    accounts whose mail attribute is free text.
    https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
    """
    raw = userinfo.get("preferred_username")
    sign_in_name = raw.strip().lower() if isinstance(raw, str) else ""
    is_member = (
        _SIGN_IN_NAME.match(sign_in_name) is not None
        and "#ext#" not in sign_in_name
        and sign_in_name.endswith(ALLOWED_DOMAIN)
    )
    if not is_member:
        return None
    return {"email": sign_in_name, "name": userinfo.get("name"), "oid": userinfo.get("oid")}
