"""Owner-only hard delete of a reel (services.videos.delete_own_video).

No database: the session and the Bunny client are stubbed, so this checks the
ordering and authorization rules only.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services import bunny_stream, videos


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    async def delete(self, obj):
        self.calls.append("db.delete")

    async def commit(self):
        self.calls.append("db.commit")


def _video(owner):
    return SimpleNamespace(user_id=owner, bunny_video_id="guid-1")


@pytest.mark.anyio
async def test_owner_delete_removes_bunny_video_before_the_row(monkeypatch):
    calls = []

    async def fake_bunny_delete(guid):
        calls.append(f"bunny.delete:{guid}")

    monkeypatch.setattr(bunny_stream, "delete_video", fake_bunny_delete)
    owner = uuid.uuid4()

    await videos.delete_own_video(FakeSession(calls), _video(owner), owner)

    assert calls == ["bunny.delete:guid-1", "db.delete", "db.commit"]


@pytest.mark.anyio
async def test_someone_else_cannot_delete_and_nothing_is_touched(monkeypatch):
    calls = []

    async def fake_bunny_delete(guid):
        calls.append("bunny.delete")

    monkeypatch.setattr(bunny_stream, "delete_video", fake_bunny_delete)

    with pytest.raises(HTTPException) as exc:
        await videos.delete_own_video(FakeSession(calls), _video(uuid.uuid4()), uuid.uuid4())

    assert exc.value.status_code == 403
    assert calls == []


@pytest.mark.anyio
async def test_bunny_failure_keeps_the_row_so_the_user_can_retry(monkeypatch):
    calls = []

    async def failing_bunny_delete(guid):
        raise HTTPException(status_code=502, detail="Bunny Stream API error")

    monkeypatch.setattr(bunny_stream, "delete_video", failing_bunny_delete)
    owner = uuid.uuid4()

    with pytest.raises(HTTPException) as exc:
        await videos.delete_own_video(FakeSession(calls), _video(owner), owner)

    assert exc.value.status_code == 502
    assert calls == []
