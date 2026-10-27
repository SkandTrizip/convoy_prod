"""Thin wrapper around Bunny Stream's REST API for video reels.

Storage/transcoding/delivery is handled entirely by Bunny — this module only
creates video placeholders, mints direct-to-Bunny TUS upload credentials, and
reads back encoding status/URLs. The backend never receives raw video bytes.
"""

import hashlib
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException

from config import BUNNY_CDN_HOSTNAME, BUNNY_STREAM_API_KEY, BUNNY_STREAM_LIBRARY_ID, logger

BUNNY_API_BASE = "https://video.bunnycdn.com"
TUS_UPLOAD_URL = "https://video.bunnycdn.com/tusupload"
TUS_SIGNATURE_TTL_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 30

# Bunny Stream encoding status codes, per https://docs.bunny.net/docs/stream-webhook
# Not independently verified against a live Bunny account — if status
# transitions look wrong in practice, check the real payload against Bunny's
# current docs and adjust.
STATUS_FINISHED = 4
STATUS_FAILED_CODES = {5, 6}


@dataclass
class BunnyVideoInfo:
    guid: str
    status: int
    length_seconds: int
    thumbnail_file_name: str | None


@dataclass
class TusUploadCredentials:
    upload_url: str
    headers: dict[str, str]
    expires_at: float


def _configured() -> bool:
    return bool(BUNNY_STREAM_API_KEY and BUNNY_STREAM_LIBRARY_ID and BUNNY_CDN_HOSTNAME)


def _require_configured() -> None:
    if not _configured():
        raise HTTPException(status_code=503, detail="Video uploads are not configured")


def _headers() -> dict[str, str]:
    return {"AccessKey": BUNNY_STREAM_API_KEY, "accept": "application/json"}


async def create_video(title: str) -> str:
    """Creates a video placeholder on Bunny, returns its GUID."""
    _require_configured()
    url = f"{BUNNY_API_BASE}/library/{BUNNY_STREAM_LIBRARY_ID}/videos"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.post(url, headers=_headers(), json={"title": title})
    if response.status_code >= 400:
        logger.error("Bunny create_video failed (%s): %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="Bunny Stream API error")
    return response.json()["guid"]


def build_tus_upload_credentials(video_guid: str) -> TusUploadCredentials:
    expires_at = time.time() + TUS_SIGNATURE_TTL_SECONDS
    expire_ts = int(expires_at)
    signature_input = f"{BUNNY_STREAM_LIBRARY_ID}{BUNNY_STREAM_API_KEY}{expire_ts}{video_guid}"
    signature = hashlib.sha256(signature_input.encode("utf-8")).hexdigest()

    return TusUploadCredentials(
        upload_url=TUS_UPLOAD_URL,
        headers={
            "AuthorizationSignature": signature,
            "AuthorizationExpire": str(expire_ts),
            "VideoId": video_guid,
            "LibraryId": BUNNY_STREAM_LIBRARY_ID,
        },
        expires_at=expires_at,
    )


async def get_video(video_guid: str) -> BunnyVideoInfo:
    _require_configured()
    url = f"{BUNNY_API_BASE}/library/{BUNNY_STREAM_LIBRARY_ID}/videos/{video_guid}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers=_headers())
    if response.status_code >= 400:
        logger.error("Bunny get_video failed (%s): %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="Bunny Stream API error")

    data = response.json()
    return BunnyVideoInfo(
        guid=data["guid"],
        status=data["status"],
        length_seconds=data.get("length", 0),
        thumbnail_file_name=data.get("thumbnailFileName"),
    )


async def delete_video(video_guid: str) -> None:
    _require_configured()
    url = f"{BUNNY_API_BASE}/library/{BUNNY_STREAM_LIBRARY_ID}/videos/{video_guid}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.delete(url, headers=_headers())
    if response.status_code >= 400 and response.status_code != 404:
        logger.error("Bunny delete_video failed (%s): %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="Bunny Stream API error")


def build_playback_url(video_guid: str) -> str:
    return f"https://{BUNNY_CDN_HOSTNAME}/{video_guid}/playlist.m3u8"


def build_thumbnail_url(video_guid: str, thumbnail_file_name: str | None) -> str | None:
    if not thumbnail_file_name:
        return None
    return f"https://{BUNNY_CDN_HOSTNAME}/{video_guid}/{thumbnail_file_name}"


async def fetch_variant_playlist(video_guid: str) -> tuple[str, str]:
    """Returns (playlist_text, playlist_url) for the video's first (only) rendition.

    NOTE: HLS layout under Bunny's pull zone is not verified against a live
    Bunny account (same caveat as STATUS_FINISHED/STATUS_FAILED_CODES above) —
    this walks the master playlist -> first variant playlist as plain text,
    which is how HLS playlists work in general, but adjust if real Bunny
    output doesn't match.
    """
    _require_configured()
    master_url = build_playback_url(video_guid)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        master_response = await client.get(master_url)
        if master_response.status_code >= 400:
            raise HTTPException(status_code=502, detail="Failed to load video master playlist")

        variant_uri = next(
            line for line in master_response.text.splitlines() if line and not line.startswith("#")
        )
        variant_url = urljoin(master_url, variant_uri)

        variant_response = await client.get(variant_url)
        if variant_response.status_code >= 400:
            raise HTTPException(status_code=502, detail="Failed to load video variant playlist")

        return variant_response.text, variant_url


async def fetch_segment_bytes(video_guid: str, segment_index: int) -> bytes:
    variant_text, variant_url = await fetch_variant_playlist(video_guid)
    segment_uris = [line for line in variant_text.splitlines() if line and not line.startswith("#")]
    if segment_index >= len(segment_uris):
        raise HTTPException(
            status_code=502, detail=f"Video has no segment at index {segment_index}"
        )
    segment_url = urljoin(variant_url, segment_uris[segment_index])

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        segment_response = await client.get(segment_url)
        if segment_response.status_code >= 400:
            raise HTTPException(status_code=502, detail="Failed to load video segment")
        return segment_response.content
