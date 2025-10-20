import os
import uuid
import asyncio
import logging
from typing import Optional, Callable, Awaitable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import anyio

# --- startup / db ---
from app.startup import init_db

# Tareas opcionales si existen
initial_geocoding: Optional[Callable[[], Awaitable[None]]] = None
sync_salesforce_subaccounts: Optional[Callable[[], None]] = None
try:
    from app.startup_geo import initial_geocoding as _initial_geocoding
    initial_geocoding = _initial_geocoding
except Exception:
    initial_geocoding = None

try:
    from app.startup import sync_salesforce_subaccounts as _sync_sf
    sync_salesforce_subaccounts = _sync_sf
except Exception:
    sync_salesforce_subaccounts = None

# --- Routers “clásicos” ---
from app.api.health import router as health_router
from app.routers.salesforce_auth import router as salesforce_auth_router
from app.routers.qualification import router as qualification_router
from app.routers.salesforce_accounts import router as salesforce_accounts_router
from app.routers.salesforce_sync import router as salesforce_sync_router
from app.routers.geo import router as geo_router
# from app.routers.explorer_combined import router as explorer_combined_router

# --- Routers nuevos del Explorer (en salesforce_explorer.py) ---
from app.routers.salesforce_explorer import (
    salesforce_router,   # /api/salesforce/...
    explorer_router,     # /api/explorer/...
)

# --- Extras (Member + PI + debug) ---
from app.routers import salesforce_extras

# --- OpenAI
from app.routers import ai_chat
#from app.api.ai import router as ai_router


# --- App ---
app = FastAPI(title="CTS Backend", version="1.0.0")

# --- Logging básico ---
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cts-backend")


# --- Request ID middleware ---
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# --- CORS ---
FRONTEND_ORIGINS = os.getenv("FRONTEND_ORIGINS", "")
origins = [o.strip() for o in FRONTEND_ORIGINS.split(",") if o.strip()]
if not origins:
    logger.warning("CORS usando '*' temporalmente; define FRONTEND_ORIGINS en .env")
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
    except Exception:
        logger.exception("init_db falló (continuamos)")

    async def _bg_tasks():
        if initial_geocoding:
            try:
                await initial_geocoding()
            except Exception:
                logger.exception("Error en initial_geocoding")

        if sync_salesforce_subaccounts:
            try:
                await anyio.to_thread.run_sync(sync_salesforce_subaccounts)
            except Exception:
                logger.exception("Error en sync_salesforce_subaccounts")

    try:
        asyncio.create_task(_bg_tasks())
    except Exception:
        logger.exception("No se pudieron lanzar tareas de background")

    for route in app.routes:
        logger.info("➡️ %s (%s)", route.path, route.name)


# --- Estáticos (opcional si empaquetas el front) ---
static_dir = os.path.join("app", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static")


# --- API Routers (UNA SOLA VEZ cada uno) ---
app.include_router(health_router)
app.include_router(salesforce_auth_router, tags=["salesforce-auth"])
app.include_router(qualification_router, tags=["qualification"])
app.include_router(salesforce_accounts_router, tags=["salesforce"])
app.include_router(salesforce_sync_router, tags=["salesforce-sync"])
app.include_router(geo_router, tags=["geo"])

# 👉 nuevos routers “únicos” (explorer)
app.include_router(salesforce_router)   # /api/salesforce/... (explorer)
app.include_router(explorer_router)     # /api/explorer/...  (explorer)
# app.include_router(explorer_combined_router)

# 👉 extras: member + PI + debug (NO intentamos incluir router_explorer si no existe)
app.include_router(salesforce_extras.router)  # /api/salesforce/...

# OpenAI
app.include_router(ai_chat.router)
#app.include_router(ai_router)

# --- Health sencillo ---
@app.get("/healthz")
async def healthz():
    return {"ok": True}


# --- Catch-all para SPA (si sirves el front desde /static) ---
@app.get("/{full_path:path}")
async def serve_react_app(full_path: str):
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"error": "Frontend not found"}