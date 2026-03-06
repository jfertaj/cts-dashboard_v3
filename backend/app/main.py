import os
import uuid
import asyncio
import logging
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
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
from app.routers.salesforce_auth import router as salesforce_auth_router
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

# --- OpenAI
from app.routers import ai_chat, explorer_bridge


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


# --- Startup ---
@app.on_event("startup")
async def on_startup():

    try:
        init_db()
        logger.info("init_db OK")
    except Exception as e:
        logger.critical("init_db failed: %s", e)
        # We might want to raise here depending on strictness, but for now we log critical.
        # raise e 

    async def _bg_tasks():
        if initial_geocoding:
            try:
                await initial_geocoding()
            except Exception:
                logger.exception("Error in initial_geocoding")

        if sync_salesforce_subaccounts:
            try:
                await anyio.to_thread.run_sync(sync_salesforce_subaccounts)
            except Exception:
                logger.exception("Error in sync_salesforce_subaccounts")

    try:
        asyncio.create_task(_bg_tasks())
    except Exception:
        logger.exception("Could not launch background tasks")

    for route in app.routes:
        logger.info("➡️ %s (%s)", route.path, route.name)


# --- Static Files (optional if bundling front) ---
static_dir = os.path.join("app", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")


# --- API Routers (Include each ONCE) ---
app.include_router(health_router)
app.include_router(salesforce_auth_router, tags=["salesforce-auth"])
app.include_router(qualification_router, tags=["qualification"])
app.include_router(salesforce_accounts_router, tags=["salesforce"])
app.include_router(salesforce_sync_router, tags=["salesforce-sync"])
app.include_router(geo_router, tags=["geo"])

# 👉 New "unique" routers (explorer)
app.include_router(salesforce_router)   # /api/salesforce/... (explorer)
app.include_router(explorer_router)     # /api/explorer/...  (explorer)

# 👉 extras: member + PI + debug (Do NOT include router_explorer if it doesn't exist)
app.include_router(salesforce_extras.router)  # /api/salesforce/...

# Members view
app.include_router(members_explorer.router)  # /api/members/...

# OpenAI
app.include_router(ai_chat.router)
app.include_router(explorer_bridge.router)

# --- Simple Health ---
@app.get("/healthz")
async def healthz():
    return {"ok": True}


# --- Catch-all for SPA (if serving front from /static) ---
@app.get("/{full_path:path}")
async def serve_react_app(full_path: str):
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"error": "Frontend not found"}