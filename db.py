import asyncpg
import os

DB_POOL: asyncpg.Pool | None = None


async def init_db_pool():
    global DB_POOL

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("❌ DATABASE_URL не задан")

    DB_POOL = await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=10
    )

