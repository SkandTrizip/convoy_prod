import uuid
from datetime import date

from db.base import (
    Booking,
    CallLog,
    KYCRecord,
    Location,
    Notification,
    SearchDemand,
    Truck,
    TruckRoute,
    TruckRouteDestination,
    User,
)


def parse_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


def profile_photo_url(user: User) -> str | None:
    """Photos uploaded via blob storage are proxied through our API; legacy
    rows may still hold a plain external URL, which is returned as-is."""
    if not user.profile_photo:
        return None
    if user.profile_photo.startswith("http://") or user.profile_photo.startswith("https://"):
        return user.profile_photo
    return f"/api/user/profile-photo/{user.id}"


def user_to_dict(user: User) -> dict:
    return {
        "_id": str(user.id),
        "mobile": user.mobile,
        "name": user.name,
        "profilePhoto": profile_photo_url(user),
        "kycStatus": user.kyc_status,
        "kycStep": user.kyc_step,
        "accountStatus": user.account_status,
        "createdDate": user.created_date,
        "pushToken": user.push_token,
        "verificationStatus": user.verification_status or {},
    }


def location_to_dict(location: Location) -> dict:
    return {
        "_id": str(location.id),
        "name": location.name,
        "lat": location.lat,
        "lng": location.lng,
        "type": location.source,
        "pincode": location.pincode,
        "city": location.city,
        "state": location.state,
    }


def kyc_to_dict(record: KYCRecord) -> dict:
    return {
        "_id": str(record.id),
        "userId": str(record.user_id),
        "method": record.method,
        "data": record.data,
        "submittedDate": record.submitted_date,
        "reviewedDate": record.reviewed_date,
        "reviewedBy": record.reviewed_by,
        "status": record.status,
        "aadhaarFrontImage": record.aadhaar_front_image,
        "aadhaarBackImage": record.aadhaar_back_image,
        "rejectionReason": record.rejection_reason,
    }


def truck_to_dict(truck: Truck) -> dict:
    return {
        "_id": str(truck.id),
        "userId": str(truck.user_id),
        "vehicleNumber": truck.truck_number,
        "truckNumber": truck.truck_number,
        "truckType": truck.truck_type,
        "capacity": truck.capacity,
        "verificationStatus": truck.verification_status,
        "vahanData": truck.vahan_data,
        "addedDate": truck.added_date,
    }


vehicle_to_dict = truck_to_dict


def truck_route_to_dict(route: TruckRoute, destinations: list[TruckRouteDestination]) -> dict:
    from datetime import datetime

    from services.destinations import destination_to_dict
    from services.post_expiry import is_post_expired

    now = datetime.utcnow()
    expired = is_post_expired(route, now)
    return {
        "_id": str(route.id),
        "userId": str(route.user_id),
        "vehicleId": str(route.truck_id),
        "truckId": str(route.truck_id),
        "vehicleNumber": route.truck_number,
        "truckNumber": route.truck_number,
        "truckType": route.truck_type,
        "capacity": route.capacity,
        "contactName": route.contact_name,
        "contactNumber": route.contact_number,
        "origin": route.origin,
        "destinations": [destination_to_dict(d) for d in destinations],
        "currentLocation": route.current_location,
        "originName": route.origin_name,
        "status": "expired" if expired else route.status,
        "isExpired": expired,
        "createdAt": route.created_at,
        "expiresAt": route.expires_at,
    }


truck_post_to_dict = truck_route_to_dict


def booking_to_dict(booking: Booking) -> dict:
    return {
        "_id": str(booking.id),
        "truckRouteId": str(booking.truck_route_id),
        "userId": str(booking.user_id),
        "status": booking.status,
        "price": float(booking.price) if booking.price is not None else None,
        "createdAt": booking.created_at,
    }


def search_demand_to_dict(demand: SearchDemand) -> dict:
    return {
        "_id": str(demand.id),
        "userId": str(demand.user_id),
        "origin": demand.origin,
        "destination": demand.destination,
        "truckType": demand.truck_type,
        "searchTimestamp": demand.search_timestamp,
        "expiryTimestamp": demand.expiry_timestamp,
        "notificationStatus": demand.notification_status,
    }


def notification_to_dict(notification: Notification) -> dict:
    return {
        "_id": str(notification.id),
        "userId": str(notification.user_id),
        "type": notification.type,
        "title": notification.title,
        "description": notification.description,
        "relatedPostId": notification.related_post_id,
        "createdAt": notification.created_at,
        "readStatus": notification.read_status,
    }


def call_log_to_dict(log: CallLog) -> dict:
    return {
        "_id": str(log.id),
        "userId": str(log.user_id),
        "truckPostId": log.truck_post_id,
        "timestamp": log.timestamp,
    }
