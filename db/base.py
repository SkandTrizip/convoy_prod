import uuid
from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class LoginOTP(Base):
    __tablename__ = "login_otps"
    __table_args__ = (Index("idx_login_otps_mobile_expires", "mobile", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    mobile: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    otp: Mapped[str] = mapped_column(String(4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    mobile: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    profile_photo: Mapped[str | None] = mapped_column(Text, nullable=True)
    kyc_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    kyc_step: Mapped[str] = mapped_column(String(32), default="aadhaar", nullable=False)
    account_status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_date: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    push_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    contacts_last_updated: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )


class UserDevice(Base):
    """One row per physical device a user has registered for push. Replaces
    User.push_token (kept temporarily for un-upgraded app versions — see
    routers/users.py's legacy /push-token endpoint). device_id and fcm_token
    are both globally unique: device_id lets a device row be reassigned to a
    different user_id on re-sync (shared device / login-as-someone-else)
    instead of accumulating orphan rows, and fcm_token unique reflects that an
    FCM token belongs to exactly one app instance at a time — a token showing
    up on a second device_id means the first row is stale and gets
    deactivated (see device_repository.upsert_device)."""

    __tablename__ = "user_devices"
    __table_args__ = (
        UniqueConstraint("device_id", name="uq_user_devices_device_id"),
        UniqueConstraint("fcm_token", name="uq_user_devices_fcm_token"),
        Index("idx_user_devices_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)  # ios | android | web | unknown
    fcm_token: Mapped[str] = mapped_column(Text, nullable=False)
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # last_token_sync moves only on an explicit register/sync call; last_app_seen
    # moves opportunistically on any authenticated request carrying X-Device-Id
    # (middleware/auth.py) — cleanup (services/device_cleanup.py) judges
    # staleness off last_app_seen, since that's the one that reflects real use.
    last_token_sync: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.utcnow, nullable=False
    )
    last_app_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AdminUser(Base):
    """Back-office login — separate from User (drivers). Its own JWT namespace,
    see middleware/admin_auth.py."""

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class UserContact(Base):
    __tablename__ = "user_contacts"
    __table_args__ = (
        UniqueConstraint("user_id", "hashed_number", name="uq_user_contacts_user_hash"),
        Index("idx_user_contacts_user_id", "user_id"),
        Index("idx_user_contacts_hashed_number", "hashed_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    hashed_number: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    pincode: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    state: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    google_place_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="google", nullable=False)


class KYCRecord(Base):
    __tablename__ = "kyc_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    submitted_date: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    reviewed_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    aadhaar_front_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    aadhaar_back_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Truck(Base):
    __tablename__ = "trucks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    truck_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    truck_type: Mapped[str] = mapped_column(String(64), nullable=False)
    capacity: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Not every truck type specifies these — e.g. a tanker may only ever set
    # capacity, never length/height. All three are independently nullable.
    length_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    height_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    vahan_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    added_date: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class TruckRoute(Base):
    __tablename__ = "truck_routes"
    __table_args__ = (
        Index("idx_truck_routes_origin_location", "origin_location", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    truck_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    truck_number: Mapped[str] = mapped_column(String(32), nullable=False)
    truck_type: Mapped[str] = mapped_column(String(64), nullable=False)
    capacity: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Copied from the vehicle at post-create/edit time, display-only in search
    # results — not used as a search filter and deliberately not read by
    # services/matching.py (smart-match stays capacity-only).
    length_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    height_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    origin_name: Mapped[str] = mapped_column(String(255), nullable=False)
    origin_location: Mapped[str] = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    origin: Mapped[dict] = mapped_column(JSONB, nullable=False)
    current_location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class TruckRouteDestination(Base):
    __tablename__ = "truck_route_destinations"
    __table_args__ = (
        Index("idx_truck_route_destinations_location", "destination_location", postgresql_using="gist"),
        Index("idx_truck_route_destinations_route", "truck_route_id"),
        UniqueConstraint("truck_route_id", "position", name="uq_truck_route_destinations_position"),
        CheckConstraint("position BETWEEN 1 AND 5", name="ck_truck_route_destinations_position_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    truck_route_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("truck_routes.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    destination_name: Mapped[str] = mapped_column(String(255), nullable=False)
    destination_location: Mapped[str] = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    destination: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    truck_route_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class SearchDemand(Base):
    __tablename__ = "search_demands"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    origin: Mapped[dict] = mapped_column(JSONB, nullable=False)
    destination: Mapped[dict] = mapped_column(JSONB, nullable=False)
    truck_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    radius_km: Mapped[float] = mapped_column(Float, default=150, nullable=False)
    capacity: Mapped[float | None] = mapped_column(Float, nullable=True)
    search_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    expiry_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    notification_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)


class Wallet(Base):
    """Cached balance summary — WalletTransaction is the source of truth; this
    table is derivable from it and exists only to make reads O(1)."""

    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("available_balance >= 0", name="ck_wallets_available_nonneg"),
        CheckConstraint("reserved_balance >= 0", name="ck_wallets_reserved_nonneg"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    available_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    reserved_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class WalletTransaction(Base):
    """Immutable, append-only ledger. Never updated or deleted — reversals are
    new rows. Every row's idempotency_key is unique so retried mutating calls
    (client timeout retries, double-taps) are safe no-ops."""

    __tablename__ = "wallet_transactions"
    __table_args__ = (
        Index("idx_wallet_transactions_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    available_after: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reserved_after: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reference_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class RedeemRequest(Base):
    __tablename__ = "redeem_requests"
    __table_args__ = (
        Index("idx_redeem_requests_status", "status"),
        Index("idx_redeem_requests_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    upi_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    processed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserRewardStats(Base):
    """Reward-domain satellite table — deliberately not columns on `users`,
    same rationale as Wallet: keeps write-hot reward counters off the identity
    table and out of its lock path."""

    __tablename__ = "user_reward_stats"
    __table_args__ = (
        CheckConstraint("total_scratches >= 0", name="ck_reward_stats_scratches_nonneg"),
        CheckConstraint("total_reward_received_paise >= 0", name="ck_reward_stats_reward_nonneg"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    total_scratches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_reward_received_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    first_scratch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class ScratchCard(Base):
    """Earned on a qualifying truck post (see services.scratch_service), one
    per user per UTC calendar day. Reward is computed at reveal time, never at
    creation time — see services.reward_engine."""

    __tablename__ = "scratch_cards"
    __table_args__ = (
        Index("idx_scratch_cards_user_earned", "user_id", "earned_at"),
        Index("idx_scratch_cards_status_expiry", "status", "expires_at"),
        CheckConstraint(
            "status IN ('unscratched', 'scratched', 'expired')", name="ck_scratch_cards_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Loose reference to the truck_routes row that earned this card — same
    # pattern as Notification.related_post_id / CallLog.truck_post_id, so
    # deleting the post later doesn't retroactively touch the card or its
    # already-computed reward.
    post_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="unscratched", nullable=False)
    reward_amount_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    earned_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    scratched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class PlatformStats(Base):
    """Singleton row (id always 1) — platform-wide reward totals, used for the
    outlier-correction average. Locked with FOR UPDATE on every reveal."""

    __tablename__ = "platform_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    total_scratch_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_reward_sum_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    related_post_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    read_status: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class CallLog(Base):
    __tablename__ = "call_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    truck_post_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class UserActivity(Base):
    """Recent-activity feed: last 10 searches + last 10 posts per user (trimmed on write)."""

    __tablename__ = "user_activity"
    __table_args__ = (
        Index("idx_user_activity_user_type_created", "user_id", "type", "created_at"),
        CheckConstraint("type IN ('search', 'post')", name="ck_user_activity_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Populated for type="post" — live reference, joined at read-time. ON DELETE CASCADE means
    # the activity entry disappears on its own once the underlying post is deleted.
    truck_route_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("truck_routes.id", ondelete="CASCADE"), nullable=True
    )
    # Populated for type="search" — the search criteria used (not the results, which change
    # over time as posts expire/get created).
    search_criteria: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class Campaign(Base):
    """A marketing notification campaign — audience + content pool + schedule +
    delivery rules, all data-driven so non-technical admins can manage it
    without touching code. See services/campaigns/ for the engine that runs these."""

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # manual (send now) | scheduled | triggered (future-ready, not yet executed by anything)
    campaign_type: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    # draft -> testing -> scheduled -> running -> paused -> completed -> archived
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    # AND/OR condition tree — see services/campaigns/audience_filters.py FILTER_REGISTRY
    audience_filter: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class CampaignContent(Base):
    """One variation in a campaign's content pool. Users cycle through every
    variation once (see services/campaigns/rotation.py) before any repeats."""

    __tablename__ = "campaign_contents"
    __table_args__ = (Index("idx_campaign_contents_campaign", "campaign_id", "sort_order"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    data_payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class CampaignSchedule(Base):
    """1:1 with Campaign. Scheduler tick (services/campaigns/scheduler_tick.py)
    polls this table every minute for rows due to run — no APScheduler job is
    registered per campaign, since that wouldn't survive a process restart."""

    __tablename__ = "campaign_schedules"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    # immediate | one_time | daily | weekly | monthly | cron (cron reserved, not executed yet)
    schedule_type: Mapped[str] = mapped_column(String(16), default="immediate", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    time_of_day: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "HH:MM", local to `timezone`
    day_of_week: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 0=Mon..6=Sun, weekly only
    day_of_month: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 1-28, monthly only
    cron_expression: Mapped[str | None] = mapped_column(String(64), nullable=True)  # reserved, unused in MVP
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class CampaignDeliveryRule(Base):
    """1:1 with Campaign. respect_preferences is currently a no-op — there is
    no per-user notification-preference model yet, so it's stored but not
    enforced until one exists."""

    __tablename__ = "campaign_delivery_rules"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    max_per_user_per_day: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    min_interval_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quiet_hours_start: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "HH:MM"
    quiet_hours_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    respect_preferences: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class CampaignExecution(Base):
    """One row per campaign run (scheduled tick, manual send-now, or test send)."""

    __tablename__ = "campaign_executions"
    __table_args__ = (Index("idx_campaign_executions_campaign", "campaign_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(16), default="schedule", nullable=False)  # schedule|manual|test
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    audience_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # sent_count/failed_count are user-level ("users reached/failed", deduped
    # across a user's devices). devices_targeted/devices_delivered are the
    # underlying device-message counts — a user with 2 devices that both
    # succeed counts once in sent_count but twice in devices_delivered.
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    no_token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    devices_targeted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    devices_delivered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class CampaignNotificationLog(Base):
    """The 'Notification History' entity — one row per (campaign, user, send
    attempt). Rotation (services/campaigns/rotation.py) derives 'which content
    has this user already seen for this campaign' from this table directly,
    rather than a separate cursor table."""

    __tablename__ = "campaign_notification_logs"
    __table_args__ = (
        Index("idx_campaign_notif_log_campaign_user", "campaign_id", "user_id", "sent_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_executions.id", ondelete="CASCADE"), nullable=True
    )
    content_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_contents.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # sent | failed | no_token | skipped
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
