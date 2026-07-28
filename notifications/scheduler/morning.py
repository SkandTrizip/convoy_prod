from config import logger
from db import async_session
from notifications.campaigns.morning_campaign import MorningCampaign


async def run_morning_campaign() -> None:
    try:
        async with async_session() as session:
            await MorningCampaign().run(session)
    except Exception as e:
        logger.error(f"Morning campaign failed: {e}", exc_info=True)
