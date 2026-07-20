import uuid
from functools import lru_cache

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContentSettings

from config import AZURE_CONN_STR, AZURE_CONTAINER_NAME, logger

PROFILE_PHOTO_PREFIX = "convoy/profile-images"
ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@lru_cache(maxsize=1)
def _container_client():
    client = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    return client.get_container_client(AZURE_CONTAINER_NAME)


def upload_profile_photo(user_id: str, content: bytes, content_type: str) -> str:
    """Upload a profile photo to the private container, returns the blob name."""
    ext = ALLOWED_CONTENT_TYPES[content_type]
    blob_name = f"{PROFILE_PHOTO_PREFIX}/{user_id}/{uuid.uuid4().hex}{ext}"

    _container_client().upload_blob(
        blob_name,
        content,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )
    return blob_name


def download_profile_photo(blob_name: str) -> tuple[bytes, str] | None:
    """Download a profile photo's bytes and content type. None if it no longer exists."""
    blob_client = _container_client().get_blob_client(blob_name)
    try:
        downloader = blob_client.download_blob()
    except ResourceNotFoundError:
        return None
    content_type = downloader.properties.content_settings.content_type or "application/octet-stream"
    return downloader.readall(), content_type


def delete_profile_photo(blob_name: str) -> None:
    blob_client = _container_client().get_blob_client(blob_name)
    try:
        blob_client.delete_blob()
    except ResourceNotFoundError:
        pass
    except Exception as e:
        logger.error(f"Error deleting old profile photo blob {blob_name}: {str(e)}")
