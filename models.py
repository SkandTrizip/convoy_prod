from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SendOTPRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"mobile": "9876543210"}})

    mobile: str = Field(..., description="10-digit mobile number", examples=["9876543210"])


class VerifyOTPRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"mobile": "9876543210", "otp": "1234"}}
    )

    mobile: str = Field(..., description="10-digit mobile number", examples=["9876543210"])
    otp: str = Field(..., description="4-digit OTP received via SMS", examples=["1234"])


class UserProfile(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"name": "Raj Kumar", "profilePhoto": "https://example.com/photo.jpg"}}
    )

    name: Optional[str] = Field(None, description="Display name")
    profilePhoto: Optional[str] = Field(None, description="URL to profile image")


class HashedContact(BaseModel):
    name: str = Field(..., description="Contact display name from device")
    hashedNumber: str = Field(
        ...,
        description="SHA256 hex hash of normalized phone digits (never send raw numbers)",
        min_length=64,
        max_length=64,
    )


class SyncContactsRequest(BaseModel):
    contacts: list[HashedContact] = Field(
        default_factory=list,
        description="Hashed device contacts to store for mutual matching",
    )


class MutualContactsRequest(BaseModel):
    otherUserId: str = Field(..., description="User ID to compare phone books against")


class PushTokenRequest(BaseModel):
    """Legacy single-token registration — kept for un-upgraded app versions.
    Internally routed into the same per-device table as DeviceRegisterRequest,
    under a synthesized device id. See routers/users.py's /push-token endpoint."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"pushToken": "f3Qz...:APA91bH...(FCM registration token)"}}
    )

    pushToken: str = Field(..., description="FCM registration token")


class DeviceRegisterRequest(BaseModel):
    """Idempotent device sync — safe to call on every token refresh/app
    launch-if-changed. Same (device_id, platform, fcmToken) upserts in place."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "deviceId": "b6e2f1a0-...-stable-client-generated-uuid",
                "platform": "android",
                "fcmToken": "f3Qz...:APA91bH...(FCM registration token)",
                "appVersion": "1.3.1",
            }
        }
    )

    deviceId: str = Field(..., description="Stable client-generated device identifier, persisted across app launches")
    platform: Literal["ios", "android", "web"] = Field(..., description="Device platform")
    fcmToken: str = Field(..., description="Current FCM registration token")
    appVersion: Optional[str] = Field(None, description="App version string, e.g. '1.3.1'")
    deviceName: Optional[str] = Field(None, description="Optional human-readable device name")


class DeviceLogoutRequest(BaseModel):
    deviceId: str = Field(..., description="Device to deregister for push on this logout")


class SendNotificationBatchRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "userIds": ["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
                "title": "New Feature",
                "body": "Check out what's new in Convoy.",
                "data": {"type": "manual"},
            }
        }
    )

    userIds: list[str] = Field(..., min_length=1, description="Target user IDs")
    title: str = Field(..., description="Push notification title")
    body: str = Field(..., description="Push notification body")
    data: Dict[str, Any] = Field(default_factory=dict, description="Extra payload for the app")


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"email": "admin@convoy.app", "password": "••••••••"}}
    )

    email: str = Field(..., description="Admin email")
    password: str = Field(..., description="Admin password")


class CreateAdminUserRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "new-admin@convoy.app",
                "password": "••••••••",
                "name": "New Admin",
            }
        }
    )

    email: str = Field(..., description="Email for the new admin account")
    password: str = Field(..., min_length=8, description="Password (min 8 characters)")
    name: Optional[str] = Field(None, description="Display name")


class Location(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Mumbai, Maharashtra",
                "lat": 19.076,
                "lng": 72.8777,
                "type": "local",
                "pincode": "400001",
                "city": "Mumbai",
                "state": "Maharashtra",
            }
        }
    )

    name: str = Field(..., description="Human-readable place name")
    lat: float = Field(..., description="Latitude")
    lng: float = Field(..., description="Longitude")
    type: str = Field("local", description="Location source type")
    pincode: Optional[str] = Field(None, description="Postal code")
    city: Optional[str] = Field(None, description="City name")
    state: Optional[str] = Field(None, description="State name")


class SearchTrucksRequest(BaseModel):
    origin: Optional[Location] = Field(
        None, description="Required if destination is omitted — at least one of the two is needed"
    )
    destination: Optional[Location] = Field(
        None, description="Required if origin is omitted — at least one of the two is needed"
    )
    truckType: Optional[str] = Field(
        None,
        description="Truck type filter. If omitted, posts of any truck type are returned.",
        examples=["Open Body"],
    )
    radius_km: float = Field(150, description="Search radius in kilometres", ge=1, le=500)
    page: int = Field(1, ge=1, description="Page number (1-indexed), 10 results per page")
    capacity: Optional[float] = Field(
        None,
        ge=0,
        description="Minimum required capacity in tonnes. If omitted, posts of any capacity are "
        "returned; if set, only posts with capacity >= this value are returned.",
    )
    sortBy: Optional[Literal["nearest_origin", "nearest_destination"]] = Field(
        None,
        description="Sort results by distance to origin or destination. If omitted, defaults to "
        "combined distance (both given), or whichever single one was given. If the requested side "
        "wasn't provided in the search (e.g. nearest_origin with no origin), falls back to the "
        "default ranking instead of failing.",
    )

    @model_validator(mode="after")
    def _require_origin_or_destination(self) -> "SearchTrucksRequest":
        if self.origin is None and self.destination is None:
            raise ValueError("At least one of origin or destination is required")
        return self


class CreateBookingRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "truck_route_id": "550e8400-e29b-41d4-a716-446655440000",
                "price": 15000,
            }
        }
    )

    truck_route_id: str = Field(..., description="UUID of the truck route to book")
    price: Optional[float] = Field(None, description="Override price in INR")


class AadhaarSendOtpRequest(BaseModel):
    aadhaarNumber: str = Field(..., description="12-digit Aadhaar number", examples=["123456789012"])


class AadhaarVerifyOtpRequest(BaseModel):
    refId: str = Field(..., description="Reference ID from send-otp response")
    otp: str = Field(..., description="OTP sent to Aadhaar-linked mobile")
    aadhaarNumber: str = Field(..., description="12-digit Aadhaar number")


class AadhaarOcrRequest(BaseModel):
    aadhaarFrontImage: str = Field(..., description="Base64 or HTTPS URL of Aadhaar front")
    aadhaarBackImage: str = Field(..., description="Base64 or HTTPS URL of Aadhaar back")
    aadhaarNumber: Optional[str] = Field(
        None, description="Optional 12-digit Aadhaar to cross-check against OCR"
    )


class KYCSubmission(BaseModel):
    method: str = Field(..., description="KYC method", examples=["aadhaar"])
    data: Dict[str, Any] = Field(default_factory=dict, description="Method-specific payload")
    aadhaarFrontImage: Optional[str] = Field(None, description="Base64 or URL of Aadhaar front")
    aadhaarBackImage: Optional[str] = Field(None, description="Base64 or URL of Aadhaar back")


class VerifyVehicleRequest(BaseModel):
    vehicleNumber: str = Field(..., description="Vehicle registration number", examples=["MH12AB1234"])


class VerifyDLRequest(BaseModel):
    dlnumber: str = Field(..., description="Driving licence number")
    dob: str = Field(..., description="Date of birth (DD-MM-YYYY)", examples=["01-01-1990"])


class BatchVerifyDocument(BaseModel):
    type: str = Field(..., description="Document type", examples=["vehicle", "dl"])
    data: Dict[str, Any] = Field(default_factory=dict)


class BatchVerifyRequest(BaseModel):
    documents: list[BatchVerifyDocument]


class AddVehicleRequest(BaseModel):
    vehicleNumber: str = Field(..., examples=["MH12AB1234"])
    truckType: str = Field(..., examples=["Open Body"])
    capacity: Optional[float] = Field(None, description="Capacity in tonnes")


class CreateTruckPostRequest(BaseModel):
    vehicleId: str = Field(..., description="UUID of a verified vehicle")
    origin: Location
    destinations: list[Location] = Field(
        ..., min_length=1, max_length=5, description="1-5 destinations for this route"
    )
    currentLocation: Location
    contactName: Optional[str] = Field(
        None, description="Contact name for this post. Defaults to the user's own name if omitted."
    )
    contactNumber: Optional[str] = Field(
        None, description="Contact number for this post. Defaults to the user's own mobile if omitted."
    )


class EditTruckPostRequest(BaseModel):
    vehicleId: Optional[str] = Field(None, description="UUID of a verified vehicle to update the post to")
    destinations: Optional[list[Location]] = Field(
        None, min_length=1, max_length=5, description="Full replacement list of 1-5 destinations"
    )
    currentLocation: Optional[Location] = None
    contactName: Optional[str] = Field(None, description="Override this post's contact name")
    contactNumber: Optional[str] = Field(None, description="Override this post's contact number")


class AdminUpdateUserRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"name": "Raj Kumar"}})

    name: str = Field(..., description="Updated display name")


class InitiateRedeemRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"upiId": "9876543210@ybl", "idempotencyKey": "a1b2c3d4-..."}
        }
    )

    upiId: str = Field(..., description="UPI VPA to pay out to, e.g. 9876543210@ybl")
    idempotencyKey: str = Field(
        ...,
        description="Client-generated key, unique per redeem attempt. Retrying the "
        "same request with the same key returns the original result instead of "
        "reserving the balance twice.",
    )


class AdminRedeemAction(BaseModel):
    reason: Optional[str] = Field(None, description="Required when rejecting")


class CampaignContentInput(BaseModel):
    title: str = Field(..., description="Push notification title")
    body: str = Field(..., description="Push notification body")
    dataPayload: Dict[str, Any] = Field(default_factory=dict, description="Optional extra FCM data payload")


class CampaignScheduleInput(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "scheduleType": "daily",
                "timezone": "Asia/Kolkata",
                "startDate": "2026-08-01T09:00:00",
                "endDate": None,
                "timeOfDay": "09:00",
                "enabled": True,
            }
        }
    )

    scheduleType: str = Field(..., description="immediate | one_time | daily | weekly | monthly")
    timezone: str = Field("Asia/Kolkata", description="IANA timezone name")
    startDate: str = Field(..., description="Wall-clock date/time in `timezone`, ISO 8601, no offset needed")
    endDate: Optional[str] = Field(None, description="Optional wall-clock end bound, same format as startDate")
    timeOfDay: Optional[str] = Field(None, description="'HH:MM', required for daily/weekly/monthly")
    dayOfWeek: Optional[int] = Field(None, ge=0, le=6, description="0=Monday..6=Sunday, weekly only")
    dayOfMonth: Optional[int] = Field(None, ge=1, le=28, description="1-28, monthly only")
    enabled: bool = True


class CampaignDeliveryRulesInput(BaseModel):
    maxPerUserPerDay: int = Field(1, ge=1, le=50)
    minIntervalMinutes: int = Field(0, ge=0)
    quietHoursStart: Optional[str] = Field(None, description="'HH:MM'")
    quietHoursEnd: Optional[str] = Field(None, description="'HH:MM'")
    respectPreferences: bool = Field(
        True, description="Accepted but not enforced — no per-user preference model exists yet"
    )


class CampaignRequest(BaseModel):
    """Shared shape for create and full-replace update."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    campaignType: str = Field("manual", description="manual | scheduled | triggered")
    audienceFilter: Dict[str, Any] = Field(
        default_factory=dict, description="AND/OR condition tree — see GET .../filter-fields"
    )
    contents: list[CampaignContentInput] = Field(default_factory=list, min_length=0)
    schedule: Optional[CampaignScheduleInput] = None
    deliveryRules: Optional[CampaignDeliveryRulesInput] = None


class CampaignTestSendRequest(BaseModel):
    userIds: list[str] = Field(..., min_length=1, description="Send this campaign's next content to these users right now")


class AudiencePreviewRequest(BaseModel):
    audienceFilter: Dict[str, Any] = Field(default_factory=dict)
    sampleSize: int = Field(10, ge=1, le=50)
