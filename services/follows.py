"""Follow graph: follow/unfollow, counts, and follower/following lists."""

import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import User, UserFollow


async def get_followable_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or user.account_status != "active":
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def follow(session: AsyncSession, follower_id: uuid.UUID, followee_id: uuid.UUID) -> None:
    """Idempotent: following someone already followed is a no-op."""
    if follower_id == followee_id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself")
    await get_followable_user(session, followee_id)
    await session.execute(
        insert(UserFollow).values(follower_id=follower_id, followee_id=followee_id).on_conflict_do_nothing()
    )
    await session.commit()


async def unfollow(session: AsyncSession, follower_id: uuid.UUID, followee_id: uuid.UUID) -> None:
    """Idempotent. Deliberately doesn't require the target to still be active,
    so a follower can always clean up a follow of a suspended account."""
    await session.execute(
        UserFollow.__table__.delete().where(
            UserFollow.follower_id == follower_id, UserFollow.followee_id == followee_id
        )
    )
    await session.commit()


async def counts(session: AsyncSession, user_id: uuid.UUID) -> tuple[int, int]:
    """(followers, following)"""
    followers = await session.scalar(select(func.count()).where(UserFollow.followee_id == user_id))
    following = await session.scalar(select(func.count()).where(UserFollow.follower_id == user_id))
    return followers or 0, following or 0


async def is_following(session: AsyncSession, follower_id: uuid.UUID, followee_id: uuid.UUID) -> bool:
    return bool(
        await session.scalar(
            select(func.count()).where(
                UserFollow.follower_id == follower_id, UserFollow.followee_id == followee_id
            )
        )
    )


async def list_users(
    session: AsyncSession,
    user_id: uuid.UUID,
    viewer_id: uuid.UUID,
    *,
    direction: str,
    limit: int,
    offset: int,
) -> tuple[list[tuple[User, bool]], int]:
    """Followers (`direction='followers'`) or followings of `user_id`, newest
    first, each with whether `viewer_id` follows that person. Suspended
    accounts are hidden."""
    if direction == "followers":
        match_col, join_col = UserFollow.followee_id, UserFollow.follower_id
    else:
        match_col, join_col = UserFollow.follower_id, UserFollow.followee_id

    base = (
        select(User, UserFollow.created_at)
        .join(UserFollow, join_col == User.id)
        .where(match_col == user_id, User.account_status == "active")
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(UserFollow.created_at.desc(), User.id).limit(limit).offset(offset)
        )
    ).all()
    users = [u for u, _ in rows]

    followed: set[uuid.UUID] = set()
    if users:
        result = await session.execute(
            select(UserFollow.followee_id).where(
                UserFollow.follower_id == viewer_id, UserFollow.followee_id.in_([u.id for u in users])
            )
        )
        followed = set(result.scalars().all())
    return [(u, u.id in followed) for u in users], total or 0


async def author_follow_context(
    session: AsyncSession, author_ids: set[uuid.UUID], viewer_id: uuid.UUID
) -> tuple[set[uuid.UUID], dict[uuid.UUID, int]]:
    """For reel cards: which of these authors the viewer follows, and each
    author's follower count — 2 queries per page regardless of size."""
    if not author_ids:
        return set(), {}
    followed = set(
        (
            await session.execute(
                select(UserFollow.followee_id).where(
                    UserFollow.follower_id == viewer_id, UserFollow.followee_id.in_(author_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    rows = await session.execute(
        select(UserFollow.followee_id, func.count())
        .where(UserFollow.followee_id.in_(author_ids))
        .group_by(UserFollow.followee_id)
    )
    return followed, {user_id: n for user_id, n in rows.all()}
