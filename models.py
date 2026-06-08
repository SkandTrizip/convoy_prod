from datetime import date
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


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
    origin: Location
    destination: Location
    truckType: str = Field(..., description="Truck type filter", examples=["Open Body"])
    radius_km: float = Field(150, description="Search radius in kilometres", ge=1, le=500)
    available_date: Optional[date] = Field(None, description="Required availability date")


class TruckSearchRequest(BaseModel):
    origin: str
    destination: str
    origin_lat: float
    origin_lng: float
    destination_lat: float
    destination_lng: float
    radius_km: float = Field(150, ge=1, le=500)
    available_date: date
    truck_type: Optional[str] = None


class CreateTruckRequest(BaseModel):
    truck_number: str = Field(..., examples=["MH12AB1234"])
    truck_type: str = Field(..., examples=["Open Body"])
    capacity: Optional[float] = Field(None, description="Capacity in tonnes")
    origin: Location
    destination: Location
    current_location: Optional[Location] = None
    available_date: date
    price: Optional[float] = Field(None, description="Quoted price in INR")


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
    destination: Location
    currentLocation: Location
    available_date: Optional[date] = None
    price: Optional[float] = Field(None, description="Quoted price in INR")


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
