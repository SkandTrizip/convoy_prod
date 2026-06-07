from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from config import logger
from database import close_db, init_db
from middleware import RequestLoggingMiddleware
from openapi_config import API_METADATA, OPENAPI_TAGS, get_servers
from routers import api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create PostGIS extension and tables on startup; close DB on shutdown."""
    logger.info("Starting Convoy API")
    try:
        await init_db()
        logger.info("Database initialized (PostGIS + tables)")
    except Exception as e:
        logger.error("Error during startup: %s", e, exc_info=True)
        raise
    yield
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

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
