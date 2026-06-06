import os
from collections.abc import AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from config import logger
from db.base import Base

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not DATABASE_URL:
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db_name = os.environ.get("POSTGRES_DB", os.environ.get("DB_NAME", "convoy_database"))
    DATABASE_URL = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


POSTGIS_INSTALL_HINT = (
    "PostGIS is not installed for this PostgreSQL server. "
    "On Windows: open Stack Builder (installed with PostgreSQL 14) → "
    "Spatial Extensions → PostGIS, or download the PostGIS bundle for PG14 from "
    "https://postgis.net/windows_downloads/ then run: CREATE EXTENSION postgis;"
)


def _existing_index_names(connection) -> set[str]:
    rows = connection.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    )
    return {row[0] for row in rows}


def _create_missing_tables(connection) -> None:
    """Create only tables that do not exist yet.

    Skips indexes that migrations already created so startup is idempotent.
    """
    existing_tables = set(inspect(connection).get_table_names())
    existing_indexes = _existing_index_names(connection)

    for table in Base.metadata.sorted_tables:
        if table.name in existing_tables:
            continue

        logger.info("Creating table: %s", table.name)
        connection.execute(CreateTable(table))
        existing_tables.add(table.name)

        for index in table.indexes:
            if index.name and index.name not in existing_indexes:
                logger.info("Creating index: %s on %s", index.name, table.name)
                index.create(connection)
                existing_indexes.add(index.name)


async def init_db() -> None:
    async with engine.begin() as conn:
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        except Exception as e:
            if "postgis.control" in str(e) or "UndefinedFileError" in str(e):
                raise RuntimeError(POSTGIS_INSTALL_HINT) from e
            raise
        await conn.run_sync(_create_missing_tables)


async def close_db() -> None:
    await engine.dispose()
