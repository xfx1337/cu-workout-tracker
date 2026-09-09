from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

IS_POSTGRES = settings.database_url.startswith("postgresql")

# pool_pre_ping: после перезапуска контейнера с базой соединения в пуле мертвы,
# без проверки первый же запрос падал бы.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=IS_POSTGRES,
)
SessionMaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
