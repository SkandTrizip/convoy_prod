"""Standalone process that consumes video-segment prefetch jobs from RabbitMQ
and warms the Redis cache (see services/video_prefetch.py/rabbitmq.py).

Run separately from the API process — see deploy/convoy-video-worker.service.
Not started by app.py's lifespan: it has its own long-lived AMQP connection
and shouldn't share a process/restart cycle with the HTTP server.

    python worker.py
"""

import asyncio
import json

import aio_pika

from config import PREFETCH_CACHE_TTL_SECONDS, RABBITMQ_URL, logger
from services import bunny_stream, redis_cache
from services.rabbitmq import PREFETCH_QUEUE_NAME
from services.video_prefetch import content_key, lock_key


async def handle_job(job: dict) -> None:
    bunny_video_id = job["bunny_video_id"]
    segment_index = job["segment_index"]

    segment_bytes = await bunny_stream.fetch_segment_bytes(bunny_video_id, segment_index)
    await redis_cache.set(content_key(bunny_video_id, segment_index), segment_bytes, PREFETCH_CACHE_TTL_SECONDS)
    await redis_cache.delete(lock_key(bunny_video_id, segment_index))


async def main() -> None:
    if not RABBITMQ_URL:
        raise RuntimeError("RABBITMQ_URL is not configured — nothing for this worker to consume")

    logger.info("Connecting to RabbitMQ...")
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(PREFETCH_QUEUE_NAME, durable=True)
        logger.info("Connected. Waiting for prefetch jobs on '%s'...", PREFETCH_QUEUE_NAME)

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process():
                    job = json.loads(message.body.decode("utf-8"))
                    logger.info("Processing job: %s", job)
                    try:
                        await handle_job(job)
                        logger.info("Cached %s segment %s", job["bunny_video_id"], job["segment_index"])
                    except Exception:
                        logger.exception("Failed to prefetch job %s", job)


if __name__ == "__main__":
    asyncio.run(main())
