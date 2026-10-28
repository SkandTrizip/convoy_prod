"""How reels are ordered. The ONE place that defines "latest" and "popular".

This is the hook for the upcoming reels algorithm: swap the body of
`popularity_score` / `order_by` (or add a new `ReelSort` value) and every feed that
sorts through here — the main feed and the heatmap's area feed — follows, with no
change to routers or the app. Keep the SQL expression and the plain-Python mirror
below in step; `tests/test_reel_ranking.py` checks they agree.

"popular" = trending: engagement divided by an age factor, so a good new reel can
beat an old big one.

    score = (1 + likes*W_LIKE + comments*W_COMMENT + shares*W_SHARE + views*W_VIEW)
            / (age_hours + AGE_OFFSET_HOURS) ** GRAVITY

The `1 +` means a reel nobody has touched yet still ranks by recency instead of all
tying at zero. `view_count` is buffered and flushed in batches (services/view_counter),
so it lags real views by a few seconds — fine for ranking.
"""

from datetime import datetime
from typing import Literal

from sqlalchemy import literal
from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

from db.base import Video

ReelSort = Literal["latest", "popular"]

WEIGHT_LIKE = 1.0
WEIGHT_COMMENT = 2.0
WEIGHT_SHARE = 3.0
WEIGHT_VIEW = 0.2

# Hours added to a reel's age so a brand-new reel doesn't divide by ~0, and the
# exponent that sets how fast popularity fades with age (higher = fades faster).
AGE_OFFSET_HOURS = 2.0
GRAVITY = 1.2


def popularity_score_py(
    likes: int, comments: int, shares: int, views: int, age_hours: float
) -> float:
    """Plain-Python mirror of `popularity_score`, for tests and for reasoning."""
    engagement = (
        1
        + likes * WEIGHT_LIKE
        + comments * WEIGHT_COMMENT
        + shares * WEIGHT_SHARE
        + views * WEIGHT_VIEW
    )
    return engagement / (max(age_hours, 0.0) + AGE_OFFSET_HOURS) ** GRAVITY


def popularity_score(now: datetime) -> ColumnElement:
    """SQL expression of the trending score as of `now` (naive UTC, like created_at)."""
    age_hours = func.greatest(
        func.extract("epoch", literal(now) - Video.created_at) / 3600.0, 0.0
    )
    engagement = (
        1.0
        + Video.like_count * WEIGHT_LIKE
        + Video.comment_count * WEIGHT_COMMENT
        + Video.share_count * WEIGHT_SHARE
        + Video.view_count * WEIGHT_VIEW
    )
    return engagement / func.power(age_hours + AGE_OFFSET_HOURS, GRAVITY)


def order_by(sort: ReelSort, now: datetime) -> list[ColumnElement]:
    """ORDER BY clauses for `sort`. Always ends in (created_at DESC, id) so paging by
    offset is deterministic even when scores tie."""
    tie_break = [Video.created_at.desc(), Video.id]
    if sort == "popular":
        return [popularity_score(now).desc(), *tie_break]
    return tie_break
