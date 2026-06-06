from typing import Any, Dict, Optional
from datetime import date

from pydantic import BaseModel


class SendOTPRequest(BaseModel):
    mobile: str


class VerifyOTPRequest(BaseModel):
    mobile: str
    otp: str


class UserProfile(BaseModel):
    name: Optional[str] = None
    profilePhoto: Optional[str] = None


class Location(BaseModel):
    name: str
    lat: float
    lng: float
    type: str = "local"
    pincode: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None


class SearchTrucksRequest(BaseModel):
    origin: Location
    destination: Location
    truckType: str
    radius_km: float = 150
    available_date: Optional[date] = None


class TruckSearchRequest(BaseModel):
    origin: str
    destination: str
    origin_lat: float
    origin_lng: float
    destination_lat: float
    destination_lng: float
    radius_km: float = 150
    available_date: date
    truck_type: Optional[str] = None


class CreateTruckRequest(BaseModel):
    truck_number: str
    truck_type: str
    capacity: Optional[float] = None
    origin: Location
    destination: Location
    current_location: Optional[Location] = None
    available_date: date
    price: Optional[float] = None


class CreateBookingRequest(BaseModel):
    truck_route_id: str
    price: Optional[float] = None


class AadhaarSendOtpRequest(BaseModel):
    aadhaarNumber: str


class AadhaarVerifyOtpRequest(BaseModel):
    refId: str
    otp: str
    aadhaarNumber: str


class KYCSubmission(BaseModel):
    method: str
    data: Dict[str, Any]
    aadhaarFrontImage: Optional[str] = None
    aadhaarBackImage: Optional[str] = None


class VerifyVehicleRequest(BaseModel):
    vehicleNumber: str


class VerifyDLRequest(BaseModel):
    dlnumber: str
    dob: str


class BatchVerifyDocument(BaseModel):
    type: str
    data: Dict[str, Any]


class BatchVerifyRequest(BaseModel):
    documents: list[BatchVerifyDocument]


class AddVehicleRequest(BaseModel):
    vehicleNumber: str
    truckType: str
    capacity: Optional[float] = None


class CreateTruckPostRequest(BaseModel):
    vehicleId: str
    origin: Location
    destination: Location
    currentLocation: Location
    available_date: Optional[date] = None
    price: Optional[float] = None


class AdminKYCAction(BaseModel):
    userId: str
    action: str
    reason: Optional[str] = None
