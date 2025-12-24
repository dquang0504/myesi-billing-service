import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

TESTING = os.getenv("TESTING", "").lower() in {"1", "true", "yes"}

engine = None
AsyncSessionLocal = None

if not TESTING:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    AsyncSessionLocal = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )


async def get_db():
    if TESTING:
        raise RuntimeError("get_db should be overridden in tests")
    async with AsyncSessionLocal() as session:
        yield session
