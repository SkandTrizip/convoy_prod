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
    model_config = ConfigDict(
        json_schema_extra={"example": {"pushToken": "ExponentPushToken[xxxxxxxxxxxxxx]"}}
    )

    pushToken: str = Field(..., description="Expo push notification token")


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


class AdminKYCAction(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "userId": "550e8400-e29b-41d4-a716-446655440000",
                "action": "approve",
                "reason": None,
            }
        }
    )

    userId: str = Field(..., description="User UUID")
    action: str = Field(..., description="`approve` or `reject`", examples=["approve"])
    reason: Optional[str] = Field(None, description="Required when action is reject")
