from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_throwaway_database_is_reachable(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert (await conn.execute(text("select 1"))).scalar_one() == 1
        name = (await conn.execute(text("select current_database()"))).scalar_one()
    assert name.startswith("setu_test_")
