import os
import uuid
import asyncio
import logging
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)

from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware
import anyio

# --- startup / db ---
from app.startup import init_db

# Optional tasks if they exist
initial_geocoding: Optional[Callable[[], Awaitable[None]]] = None
sync_salesforce_subaccounts: Optional[Callable[[], None]] = None
try:
    from app.startup_geo import initial_geocoding as _initial_geocoding
    initial_geocoding = _initial_geocoding
except ImportError:
    logger.info("startup_geo module not found, skipping initial_geocoding")
    initial_geocoding = None
except Exception:
    logger.exception("Failed to import initial_geocoding")
    initial_geocoding = None

try:
    from app.startup import sync_salesforce_subaccounts as _sync_sf
    sync_salesforce_subaccounts = _sync_sf
except ImportError:
    logger.info("sync_salesforce_subaccounts not found in startup, skipping")
    sync_salesforce_subaccounts = None
except Exception:
    logger.exception("Failed to import sync_salesforce_subaccounts")
    sync_salesforce_subaccounts = None

# --- Classic Routers ---
from app.api.health import router as health_router
# NOTE: salesforce_auth_router (per-user SF login) is retired — no longer imported
# or mounted. The module app/routers/salesforce_auth.py stays dormant in the tree.
from app.routers.qualification import router as qualification_router
from app.routers.salesforce_accounts import router as salesforce_accounts_router
from app.routers.salesforce_sync import router as salesforce_sync_router
from app.routers.geo import router as geo_router
# --- New Explorer Routers (in salesforce_explorer.py) ---
from app.routers.salesforce_explorer import (
    salesforce_router,   # /api/salesforce/...
    explorer_router,     # /api/explorer/...
)

# --- Extras (Member + PI + debug) ---
from app.routers import salesforce_extras

# --- Members view
from app.routers import members_explorer

# --- Assignment → Contact report
from app.routers import assignments_report

# --- OpenAI
from app.routers import ai_chat, explorer_bridge

# --- Entra SSO auth (public: login/callback/me/logout)
from app.routers.entra_auth import router as entra_auth_router, _secure as _cookies_secure

# --- Auth guard for data routers (require_user); AUTH_DISABLED=1 bypasses in local dev
from app.deps.auth import require_user


# --- App ---
app = FastAPI(title="CTS Backend", version="1.0.0")

# Basic Logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cts-backend")

# Filter to suppress healthz logs
class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Suppress logs containing GET /healthz
        return "GET /healthz" not in record.getMessage()

# Apply filter to uvicorn.access logger
logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())


# --- Request ID middleware ---
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    
    # Skip logging for health checks to reduce noise
    if request.url.path == "/healthz":
        return await call_next(request)
    
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# --- CORS ---
FRONTEND_ORIGINS = os.getenv("FRONTEND_ORIGINS", "")
origins = [o.strip() for o in FRONTEND_ORIGINS.split(",") if o.strip()]
if not origins:
    logger.warning("CORS using '*' temporarily; define FRONTEND_ORIGINS in .env")
    origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# --- Session middleware (Authlib OAuth state/nonce during the Entra login flow).
# APP_SESSION_SECRET is required (prod gets it from Secrets Manager); fail fast if missing.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["APP_SESSION_SECRET"],
    same_site="lax",
    https_only=_cookies_secure(),  # Secure flag on the OAuth state/nonce cookie in prod (HTTPS)
)


# --- Startup ---
@app.on_event("startup")
async def on_startup():

    try:
        init_db()
        logger.info("init_db OK")
    except Exception as e:
        logger.critical("init_db failed — cannot start without database: %s", e, exc_info=True)
        raise RuntimeError(f"Database initialisation failed: {e}") from e

    async def _bg_tasks():
        # Background tasks are optional — failures are logged but do not affect app operation.
        if initial_geocoding:
            try:
                await initial_geocoding()
            except Exception:
                logger.exception("initial_geocoding failed (non-fatal — geocoding will run lazily per request)")

        if sync_salesforce_subaccounts:
            try:
                await anyio.to_thread.run_sync(sync_salesforce_subaccounts)
            except Exception:
                logger.exception("sync_salesforce_subaccounts failed (non-fatal)")

    try:
        asyncio.create_task(_bg_tasks())
    except Exception:
        logger.exception("Could not launch background tasks")

    # No todo lo que hay en app.routes es una ruta con path: en FastAPI moderno
    # un router incluido aparece como _IncludedRouter, sin `.path`. Darlo por
    # hecho reventaba el arranque con AttributeError y ECS hacía rollback del
    # deploy entero — mientras deploy.sh cantaba "✅ Deploy complete".
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None:
            continue
        logger.info("➡️ %s (%s)", path, getattr(route, "name", "?"))


# --- Static Files (optional if bundling front) ---
static_dir = os.path.join("app", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")


# --- API Routers (Include each ONCE) ---
# Data routers are gated by require_user (401 unless a valid session cookie or
# AUTH_DISABLED=1). health_router and entra_auth_router stay PUBLIC.
# The per-user Salesforce login router (salesforce_auth_router) is retired — no
# longer mounted; the module stays dormant in the tree.
_GUARD = [Depends(require_user)]

app.include_router(health_router)
app.include_router(qualification_router, tags=["qualification"], dependencies=_GUARD)
app.include_router(salesforce_accounts_router, tags=["salesforce"], dependencies=_GUARD)
app.include_router(salesforce_sync_router, tags=["salesforce-sync"], dependencies=_GUARD)
app.include_router(geo_router, tags=["geo"], dependencies=_GUARD)

# 👉 New "unique" routers (explorer)
app.include_router(salesforce_router, dependencies=_GUARD)   # /api/salesforce/... (explorer)
app.include_router(explorer_router, dependencies=_GUARD)     # /api/explorer/...  (explorer)

# 👉 extras: member + PI + debug (Do NOT include router_explorer if it doesn't exist)
app.include_router(salesforce_extras.router, dependencies=_GUARD)  # /api/salesforce/...

# Members view
app.include_router(members_explorer.router, dependencies=_GUARD)  # /api/members/...
app.include_router(assignments_report.router, dependencies=_GUARD)  # /api/assignments/...

# OpenAI
app.include_router(ai_chat.router, dependencies=_GUARD)
app.include_router(explorer_bridge.router, dependencies=_GUARD)

# Entra SSO auth (public — login/callback/me/logout)
app.include_router(entra_auth_router)  # /api/auth/... (public)

# --- Catch-all for SPA (if serving front from /static) ---
@app.get("/{full_path:path}")
async def serve_react_app(full_path: str):
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"error": "Frontend not found"}