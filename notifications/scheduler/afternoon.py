from config import logger
from db import async_session
from notifications.campaigns.afternoon_campaign import AfternoonCampaign


async def run_afternoon_campaign() -> None:
    try:
        async with async_session() as session:
            await AfternoonCampaign().run(session)
    except Exception as e:
        logger.error(f"Afternoon campaign failed: {e}", exc_info=True)
