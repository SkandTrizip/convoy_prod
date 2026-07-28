from config import logger
from db import async_session
from notifications.campaigns.night_campaign import NightCampaign


async def run_night_campaign() -> None:
    try:
        async with async_session() as session:
            await NightCampaign().run(session)
    except Exception as e:
        logger.error(f"Night campaign failed: {e}", exc_info=True)
