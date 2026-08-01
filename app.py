import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from config import PORT, logger
from database import close_db, init_db
from middleware import RequestLoggingMiddleware
from notifications.scheduler import start_notification_scheduler, stop_notification_scheduler
from openapi_config import API_METADATA, OPENAPI_TAGS, get_servers
from routers import api_router
from services.device_cleanup import run_device_cleanup_loop
from services.post_expiry import run_post_expiry_loop
from services.scratch_service import run_scratch_card_expiry_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create PostGIS extension and tables on startup; close DB on shutdown."""
    logger.info("Starting Convoy API")
    expiry_task: asyncio.Task | None = None
    device_cleanup_task: asyncio.Task | None = None
    scratch_card_expiry_task: asyncio.Task | None = None
    try:
        await init_db()
        logger.info("Database initialized (PostGIS + tables)")
        expiry_task = asyncio.create_task(run_post_expiry_loop())
        device_cleanup_task = asyncio.create_task(run_device_cleanup_loop())
        scratch_card_expiry_task = asyncio.create_task(run_scratch_card_expiry_loop())
        start_notification_scheduler()
    except Exception as e:
        logger.error("Error during startup: %s", e, exc_info=True)
        raise
    yield
    stop_notification_scheduler()
    for task in (expiry_task, device_cleanup_task, scratch_card_expiry_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    logger.info("Shutting down Convoy API")
    await close_db()


app = FastAPI(
    lifespan=lifespan,
    **API_METADATA,
    openapi_tags=OPENAPI_TAGS,
    servers=get_servers(),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


app.include_router(api_router)


def _configure_openapi_security() -> None:
    schema = app.openapi()
    security_schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    security_schemes["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "JWT from POST /api/auth/verify-otp (`accessToken`)",
    }
    security_schemes["AdminBearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "JWT from POST /api/admin-auth/login (`accessToken`) — separate namespace from driver auth",
    }
    for path, methods in schema.get("paths", {}).items():
        if path.startswith("/api/auth/") or path in ("/api/", "/api/truck-types"):
            continue
        if path == "/api/admin-auth/login":
            continue
        security_scheme = (
            "AdminBearerAuth" if path.startswith(("/api/admin/", "/api/admin-auth/")) else "BearerAuth"
        )
        for operation in methods.values():
            if isinstance(operation, dict):
                operation["security"] = [{security_scheme: []}]
    app.openapi_schema = schema


_configure_openapi_security()

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=True)
