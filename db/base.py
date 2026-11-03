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
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    # Where wallet withdrawals are paid out (a UPI VPA such as name@bank).
    upi_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verification_status: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    contacts_last_updated: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )


class UserFollow(Base):
    """`follower_id` follows `followee_id`. Counts are computed from this table
    (both directions are indexed), not denormalized onto users, so they can't
    drift — account deletion needs no counter fix-ups, the FKs cascade."""

    __tablename__ = "user_follows"
    __table_args__ = (
        Index("idx_user_follows_followee_created", "followee_id", "created_at"),
        Index("idx_user_follows_follower_created", "follower_id", "created_at"),
        CheckConstraint("follower_id <> followee_id", name="ck_user_follows_not_self"),
    )

    follower_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    followee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


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
    # iPhone only: the PushKit token used for the system incoming-call screen (see services/apns_client.py).
    voip_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    voip_sandbox: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    __table_args__ = (
        # Partial index: only an *approved* record's hash counts as "taken" —
        # pending/rejected attempts (including retries, including a user's own
        # earlier failed attempts) never block anything. One row per user (see
        # routers/kyc.py, always scalar_one_or_none()) means a user re-verifying
        # their own Aadhaar just updates their own row, so this never
        # self-conflicts — only a genuinely different user_id can collide.
        Index(
            "ux_kyc_records_aadhaar_hash_approved",
            "aadhaar_hash",
            unique=True,
            postgresql_where=(text("status = 'approved'")),
        ),
    )

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
    # HMAC of the normalized Aadhaar number — never the raw number itself.
    # Only ever set by the OTP verification flow (see services/kyc.py).
    aadhaar_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


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


class WalletRecharge(Base):
    """One attempt to add money to a wallet through Razorpay. The amounts are fixed by the
    server when the order is made (never taken from the app afterwards), and the row is what a
    payment is checked against before any money is credited. `review` means the person paid but
    the credit could not be applied (e.g. the wallet was frozen): a human must sort it out."""

    __tablename__ = "wallet_recharges"
    __table_args__ = (
        UniqueConstraint("razorpay_order_id", name="uq_wallet_recharges_order"),
        UniqueConstraint("razorpay_payment_id", name="uq_wallet_recharges_payment"),
        CheckConstraint(
            "credit_amount > 0 AND fee_amount >= 0 AND total_amount = credit_amount + fee_amount",
            name="ck_wallet_recharges_amounts",
        ),
        Index("idx_wallet_recharges_user", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    razorpay_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credit_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="created", nullable=False)
    wallet_transaction_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


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


class Video(Base):
    """A short vertical video ('reel'). Storage/transcoding/delivery is handled
    entirely by Bunny Stream — `bunny_video_id` is the only link to it, and
    `playback_url`/`thumbnail_url` are Bunny's own public CDN URLs, populated
    only once encoding finishes and the video is confirmed within the duration
    cap (see services/videos.py)."""

    __tablename__ = "videos"
    __table_args__ = (
        Index("idx_videos_user_created", "user_id", "created_at"),
        Index("idx_videos_status_created", "status", "created_at"),
        Index("idx_videos_user_status_created", "user_id", "status", "created_at"),
        Index("idx_videos_hashtags", "hashtags", postgresql_using="gin"),
        Index("idx_videos_location", "location", postgresql_using="gist"),
        CheckConstraint(
            "status IN ('pending_upload', 'processing', 'ready', 'rejected', 'failed')",
            name="ck_videos_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    bunny_video_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending_upload", nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    playback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form discovery tags, normalized lowercase without '#' (see
    # services/reel_social.normalize_hashtags). Curated categories live in
    # video_categories instead.
    hashtags: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)), default=list, server_default=text("'{}'"), nullable=False
    )
    # Uploader's GPS at posting time. Exact point is never returned to clients —
    # only the reverse-geocoded city/state labels below are.
    location: Mapped[str | None] = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=True)
    location_city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    location_state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Denormalized counters, kept in step with video_likes/video_comments/
    # video_shares by atomic UPDATE ... SET n = n + 1 in services/reel_social.py.
    like_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    comment_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    share_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    # Updated in batches by services/view_counter.py, so it lags real views by
    # up to REEL_VIEW_FLUSH_INTERVAL_SECONDS. Only exposed to the reel's owner.
    view_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ReelCategory(Base):
    """Admin-curated reel category (fixed list, unlike free-form hashtags).
    Deactivated rather than deleted, so existing reels keep their tags."""

    __tablename__ = "reel_categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    # server_default so raw-SQL inserts (the migration's seed rows) work too.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.utcnow, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.utcnow, onupdate=datetime.utcnow, server_default=text("NOW()")
    )


class VideoCategory(Base):
    __tablename__ = "video_categories"
    __table_args__ = (Index("idx_video_categories_category", "category_id", "video_id"),)

    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reel_categories.id", ondelete="CASCADE"), primary_key=True
    )


class VideoLike(Base):
    __tablename__ = "video_likes"
    __table_args__ = (Index("idx_video_likes_user_created", "user_id", "created_at"),)

    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class VideoComment(Base):
    """Flat comments (no replies). Deletable by the commenter or the reel's owner."""

    __tablename__ = "video_comments"
    __table_args__ = (
        Index("idx_video_comments_video_created", "video_id", "created_at"),
        Index("idx_video_comments_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class VideoShare(Base):
    """One row per share-sheet use (repeat shares by the same user all count),
    kept as an event log for recommendations, not just the counter."""

    __tablename__ = "video_shares"
    __table_args__ = (
        Index("idx_video_shares_video_created", "video_id", "created_at"),
        Index("idx_video_shares_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class OneToOneCall(Base):
    """LEGACY open-room 1:1 calls (one host, one guest, anyone could join). The /calls
    API that used this table was retired when paid call requests (CallRequest) replaced
    it. The model and table are kept only so no existing rows are dropped."""

    __tablename__ = "one_to_one_calls"
    __table_args__ = (
        Index("idx_one_to_one_calls_status_started", "status", "started_at"),
        CheckConstraint("status IN ('live', 'ended')", name="ck_one_to_one_calls_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    host_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    guest_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    host_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="live", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.utcnow, onupdate=datetime.utcnow
    )


# --- Paid 1:1 calls ---------------------------------------------------------
# NEW FLOW (this branch): no wallet. pending -> accepted -> ringing -> live
#   -> awaiting_payment -> completed (`payment_outcome` says paid / refused /
#   declined / expired). Side exits: rejected (provider), cancelled (requester),
#   expired (30 min window), failed (the call never connected at all).
#   `awaiting_feedback` is the classic (wallet) flow's status: never written here.
CALL_STATUSES = (
    "pending",
    "accepted",
    "ringing",
    "live",
    "awaiting_feedback",
    "awaiting_payment",
    "completed",
    "rejected",
    "cancelled",
    "expired",
    "failed",
)
# At most one of these per requester/provider pair (partial unique index below).
CALL_OPEN_STATUSES = ("pending", "accepted", "ringing", "live")


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class CallPaymentStanding(Base):
    """An admin forgave this user's unpaid-call strikes at `reset_at`: only strikes after that time
    count towards the block (see services/call_payments.py)."""

    __tablename__ = "call_payment_standing"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    reset_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class UserLocationTracking(Base):
    """The 48-hour window in which a user's phone reports its location now and then, after they
    post a truck (see services/location_tracking.py). One row per user; a new post or a
    reactivation restarts it. The last known position is kept only while the window is on:
    stopping (logout, post deleted) or running out clears it."""

    __tablename__ = "user_location_tracking"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # The post that started this window (no foreign key: the post may be deleted, which stops it).
    post_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    report_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CallRequest(Base):
    """One paid 1:1 call request, from the request through the Agora call to
    settlement. The wallet side of it lives in WalletTransaction rows whose
    reference_type is 'call_request' and reference_id is this row's id."""

    __tablename__ = "call_requests"
    __table_args__ = (
        Index("idx_call_requests_provider_status", "provider_id", "status", "created_at"),
        Index("idx_call_requests_requester_status", "requester_id", "status", "created_at"),
        Index("idx_call_requests_status_expires", "status", "expires_at"),
        Index(
            "uq_call_requests_open_pair",
            "requester_id",
            "provider_id",
            unique=True,
            postgresql_where=text(f"status IN ({_sql_in(CALL_OPEN_STATUSES)})"),
        ),
        CheckConstraint(f"status IN ({_sql_in(CALL_STATUSES)})", name="ck_call_requests_status"),
        CheckConstraint("amount > 0", name="ck_call_requests_amount_positive"),
        CheckConstraint("hold_amount > 0 AND hold_amount <= amount", name="ck_call_requests_hold_range"),
        CheckConstraint("duration_minutes IN (10, 15, 30)", name="ck_call_requests_duration"),
        CheckConstraint("requester_id <> provider_id", name="ck_call_requests_not_self"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    requester_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # The live truck post the request was made from (no FK: posts expire and get
    # deleted, the request must outlive them).
    truck_route_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # What the requester offered, and the part of it frozen up front (the "call
    # amount": what the provider is owed once the call connects).
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    hold_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    # Call fields — filled in by the call stage (services/call_requests.py).
    channel_name: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    ring_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    ring_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    call_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    call_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    connected_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Live-call presence (see CALL_HEARTBEAT_GRACE_SECONDS in config.py). Each app
    # reports "I'm in the call" and "I can see the other person"; connected time only
    # accumulates while both say yes, so neither side can run up the 30 s threshold alone.
    requester_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    provider_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    requester_peer_present: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    provider_peer_present: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    connected_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # Which phone holds the call when a person is signed in on several (the app's X-Device-Id):
    # the one that answered the ring (requester) / started it (provider). Null = not known yet.
    requester_device_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_device_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Direct payment (new flow): the requester pays the provider's UPI ID after the call.
    # `hold_amount` is the MINIMUM payable (no money is frozen anywhere any more).
    payment_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    paid_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    payment_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    payment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payee_upi_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    upi_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provider_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    threshold_met: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provider_media_on: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    counts_as_strike: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    # One count per heartbeat while both were in the call; the fractions say how long a mic / camera was on.
    provider_beats: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    provider_mic_beats: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    provider_camera_beats: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    requester_beats: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    requester_mic_beats: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    requester_camera_beats: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    requester_saw_video_beats: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)

    # Settlement (classic flow; unused by the new one except `feedback_deadline`, the payment deadline).
    feedback_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    satisfied: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    settled_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class CallBehaviourEvent(Base):
    """Append-only record of what each side did around a call request: server
    lifecycle events (requested, accepted, rejected, ...) and client-reported
    ones (mic_off, camera_off, chat_opened, ...). Raw material for the future
    reputation system; chat text is never stored, only that chat was used."""

    __tablename__ = "call_behaviour_events"
    __table_args__ = (
        Index("idx_call_behaviour_events_request", "call_request_id", "created_at"),
        Index("idx_call_behaviour_events_user_type", "user_id", "event_type", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    call_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_requests.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # requester | provider
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="server", nullable=False)  # server | client
    # When it happened on the device (client events) or on the server.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
