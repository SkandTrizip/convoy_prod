import uuid
from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
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
    truck_type: Mapped[str] = mapped_column(String(64), nullable=False)
    search_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    expiry_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    notification_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)


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
