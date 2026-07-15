#!/usr/bin/env python3
"""
Fetch secrets from AWS Secrets Manager and write backend/.env for local development.
Requires: aws cli configured with SSO profile 'juan'  (aws sso login --profile juan)

Usage:
    python3 scripts/gen_local_env.py
"""
import subprocess, json, os, sys

BACKEND_SECRET = "arn:aws:secretsmanager:eu-west-1:745854319016:secret:prod/cts-dashboard/backend-oshxLi"
SF_SECRET      = "arn:aws:secretsmanager:eu-west-1:745854319016:secret:prod/cts-dashboard/salesforce-kkVxPr"

def get_secret(arn: str) -> dict:
    r = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value",
         "--secret-id", arn, "--query", "SecretString", "--output", "text"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print("❌  AWS error:", r.stderr[:200])
        sys.exit(1)
    return json.loads(r.stdout.strip())

backend = get_secret(BACKEND_SECRET)
sf      = get_secret(SF_SECRET)

# Entra SSO gate bypass, local-dev default — bypasses the Entra login flow so
# local dev + tests don't need a real Entra session. Set AUTH_DISABLED in the
# secrets bundle (e.g. "0") to exercise the real Entra flow locally instead.
defaults = {"AUTH_DISABLED": "1"}

merged = {**defaults, **backend, **sf}

# Local overrides
merged.update({
    "AI_CHAT_DEBUG":        "1",
    "GOOGLE_REGION_BIAS":   "es",
    "SF_SCOPES":            "refresh_token api id web",
    "FRONTEND_ORIGINS":     "http://localhost:8080,http://localhost:3000",
    "FRONTEND_BASE":        "http://localhost:8080",
    "COOKIE_SECRET":        "dev-secret-change-me",  # matches production default in salesforce_oauth.py
    "ENABLE_SECURE_COOKIES": "false",
    # SF OAuth callback for local dev — must be registered in SF Connected App
    # (one-time setup: Setup → App Manager → CTS Dashboard → Edit → Callback URLs)
    "SF_REDIRECT_URI":      "http://localhost:8000/api/salesforce/oauth/callback",
})

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path  = os.path.join(repo_root, "backend", ".env")

with open(env_path, "w") as f:
    for k, v in sorted(merged.items()):
        # Single-quote all values so bash never interprets spaces as commands
        v_escaped = str(v).replace("'", "'\\''")
        f.write(f"{k}='{v_escaped}'\n")

print(f"✅  Written {env_path}")
print(f"    Keys: {', '.join(sorted(merged.keys()))}")
