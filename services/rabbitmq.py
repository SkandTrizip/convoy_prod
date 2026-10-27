"""Publishes video-segment prefetch jobs for worker.py to consume.

Best-effort like services/redis_cache.py — a missing RABBITMQ_URL or a broker
that's down just means no prefetching happens this time, never a broken
request for the caller.

The connection/channel are opened once and reused (not re-opened per publish)
— this is called from services/video_prefetch.py::report_watching inside a
loop over several neighbor videos on every POST /videos/{id}/watching, a
client-facing hot path hit on each feed scroll; a fresh TCP+AMQP handshake
per call there would multiply connection-setup latency under real traffic.
`connect_robust` already auto-reconnects on a dropped network connection, so
caching it is safe across transient broker blips.
"""

import asyncio
import json

import aio_pika

from config import RABBITMQ_URL, logger

PREFETCH_QUEUE_NAME = "video_prefetch_jobs"

_channel = None
_channel_lock = asyncio.Lock()


async def _get_channel():
    global _channel
    if _channel is not None and not _channel.is_closed:
        return _channel

    async with _channel_lock:
        if _channel is not None and not _channel.is_closed:
            return _channel
        connection = await aio_pika.connect_robust(RABBITMQ_URL)
        channel = await connection.channel()
        await channel.declare_queue(PREFETCH_QUEUE_NAME, durable=True)
        _channel = channel
        return _channel


async def publish_prefetch_job(payload: dict) -> bool:
    if not RABBITMQ_URL:
        return False
    try:
        channel = await _get_channel()
        await channel.default_exchange.publish(
            aio_pika.Message(body=json.dumps(payload).encode("utf-8")),
            routing_key=PREFETCH_QUEUE_NAME,
        )
        return True
    except Exception as e:
        logger.warning("Failed to publish prefetch job %s: %s", payload, e)
        global _channel
        _channel = None  # force a fresh connection next time, in case this one is bad
        return False
