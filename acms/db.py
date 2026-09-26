from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from .settings import get_settings


def async_database_url(url: str) -> str:
    """Ensure SQLAlchemy uses the asyncio driver for the target database."""
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


settings = get_settings()
database_url = async_database_url(settings.database_url)

# NullPool for SQLite keeps isolated-file test databases from pinning connections
# (and their event loops) across processes; PostgreSQL uses the default async pool.
engine = create_async_engine(
    database_url,
    future=True,
    poolclass=NullPool if database_url.startswith("sqlite+aiosqlite") else None,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
