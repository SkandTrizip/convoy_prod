import uuid
from datetime import date, timedelta

from decimal import Decimal

from config import CALL_RING_SECONDS

from db.base import (
    AdminUser,
    CallRequest,
    Booking,
    CallLog,
    KYCRecord,
    Location,
    Notification,
    ReelCategory,
    ScratchCard,
    SearchDemand,
    Truck,
    TruckRoute,
    TruckRouteDestination,
    User,
    Video,
    VideoComment,
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
        # Where callers pay this person after a call (null until they add it). Only ever sent to the
        # person themselves (login / profile) and to admins.
        "upiId": user.upi_id,
        "accountStatus": user.account_status,
        "createdDate": user.created_date,
        "verificationStatus": user.verification_status or {},
    }


def admin_to_dict(admin: AdminUser) -> dict:
    return {
        "id": str(admin.id),
        "email": admin.email,
        "name": admin.name,
        "isActive": admin.is_active,
        "createdAt": admin.created_at,
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
        "length": truck.length_ft,
        "height": truck.height_ft,
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
        "length": route.length_ft,
        "height": route.height_ft,
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


def scratch_card_to_dict(card: ScratchCard) -> dict:
    return {
        "id": str(card.id),
        "status": card.status,
        "earnedAt": card.earned_at.isoformat(),
        "expiresAt": card.expires_at.isoformat(),
        "scratchedAt": card.scratched_at.isoformat() if card.scratched_at else None,
        "rewardRupees": (
            str((Decimal(card.reward_amount_paise) / 100).quantize(Decimal("0.01")))
            if card.reward_amount_paise is not None
            else None
        ),
    }


def call_log_to_dict(log: CallLog) -> dict:
    return {
        "_id": str(log.id),
        "userId": str(log.user_id),
        "truckPostId": log.truck_post_id,
        "timestamp": log.timestamp,
    }


def video_to_dict(video: Video) -> dict:
    return {
        "id": str(video.id),
        "userId": str(video.user_id),
        "title": video.title,
        "description": video.description,
        "status": video.status,
        "durationSeconds": video.duration_seconds,
        "playbackUrl": video.playback_url,
        "thumbnailUrl": video.thumbnail_url,
        "rejectionReason": video.rejection_reason,
        "hashtags": list(video.hashtags or []),
        # City/state label only — the exact GPS point is never exposed.
        "location": (
            {"city": video.location_city, "state": video.location_state}
            if video.location_city or video.location_state
            else None
        ),
        "likeCount": video.like_count,
        "commentCount": video.comment_count,
        "shareCount": video.share_count,
        # Path clients should actually play, in place of playbackUrl — same
        # video, but segment 0 comes from the prefetch cache when it's warm.
        # Only meaningful once encoding has finished.
        "streamUrl": f"/api/videos/{video.id}/fast-start.m3u8" if video.status == "ready" else None,
        "createdAt": video.created_at.isoformat(),
        "updatedAt": video.updated_at.isoformat(),
    }


def reel_author_to_dict(author: User | None) -> dict | None:
    if author is None:
        return None
    return {
        "id": str(author.id),
        "name": author.name,
        "profilePhoto": profile_photo_url(author),
    }


def follow_user_to_dict(user: User, followed_by_me: bool, viewer_id: uuid.UUID) -> dict:
    return {
        **reel_author_to_dict(user),
        "followedByMe": followed_by_me,
        "isMe": user.id == viewer_id,
    }


def reel_category_to_dict(category: ReelCategory) -> dict:
    return {
        "id": str(category.id),
        "slug": category.slug,
        "name": category.name,
        "sortOrder": category.sort_order,
        "isActive": category.is_active,
    }


def reel_to_dict(
    video: Video,
    author: User | None,
    categories: list[ReelCategory] | None = None,
    liked_by_me: bool = False,
    followed_by_me: bool = False,
    author_follower_count: int = 0,
    viewer_id: uuid.UUID | None = None,
) -> dict:
    """Pass the pieces from services.reel_social.load_reel_context."""
    author_dict = reel_author_to_dict(author)
    if author_dict is not None:
        author_dict.update(
            followedByMe=followed_by_me,
            followerCount=author_follower_count,
            isMe=author is not None and author.id == viewer_id,
        )
    return {
        **video_to_dict(video),
        "author": author_dict,
        "categories": [
            {"id": str(c.id), "slug": c.slug, "name": c.name} for c in (categories or [])
        ],
        "likedByMe": liked_by_me,
        "postedAt": video.created_at.isoformat(),
    }


def creator_reel_to_dict(video: Video, include_views: bool) -> dict:
    """Compact grid item for a creator's profile. `viewCount` is owner-only."""
    item = {
        "id": str(video.id),
        "title": video.title,
        "thumbnailUrl": video.thumbnail_url,
        "durationSeconds": video.duration_seconds,
        "likeCount": video.like_count,
        "commentCount": video.comment_count,
        "postedAt": video.created_at.isoformat(),
    }
    if include_views:
        item["viewCount"] = video.view_count
    return item


def reel_comment_to_dict(comment: VideoComment, author: User | None, can_delete: bool) -> dict:
    return {
        "id": str(comment.id),
        "reelId": str(comment.video_id),
        "body": comment.body,
        "author": reel_author_to_dict(author),
        "canDelete": can_delete,
        "createdAt": comment.created_at.isoformat(),
    }



def call_request_party_to_dict(user: User | None) -> dict:
    return {
        "id": str(user.id) if user else None,
        "name": user.name if user else None,
        "photo": profile_photo_url(user) if user else None,
    }


def payment_details(request: CallRequest, provider: User | None, viewer_id: uuid.UUID) -> dict | None:
    """What the requester needs to pay (the driver's UPI ID is shown to them only, and only while a payment
    is due). None for everyone else / at any other time."""
    if request.status != "awaiting_payment" or viewer_id != request.requester_id:
        return None
    return {
        "upiId": request.payee_upi_id or (provider.upi_id if provider else None),
        "payeeName": provider.name if provider else None,
        "fullAmount": str(request.amount),
        "minimumAmount": str(request.hold_amount),
        "thresholdMet": bool(request.threshold_met),
        "deadline": request.feedback_deadline.isoformat() if request.feedback_deadline else None,
    }


def payment_ledger_entry_to_dict(
    request: CallRequest, requester: User | None, provider: User | None, viewer_id: uuid.UUID
) -> dict:
    """One line of the payments ledger. `counterparty` is the other person. Only the driver sees
    `providerConfirmed` (their own "did you receive it?" answer); the requester's line never changes
    because of it."""
    received = viewer_id == request.provider_id
    return {
        "id": str(request.id),
        "direction": "received" if received else "sent",
        "counterparty": call_request_party_to_dict(requester if received else provider),
        "amount": str(request.paid_amount),
        "offerAmount": str(request.amount),
        "amountType": "full" if request.paid_amount == request.amount else "minimum",
        "topic": request.topic,
        "durationMinutes": request.duration_minutes,
        "connectedSeconds": request.connected_seconds,
        "paidAt": request.payment_decided_at.isoformat() if request.payment_decided_at else None,
        "callEndedAt": request.call_ended_at.isoformat() if request.call_ended_at else None,
        "upiId": request.payee_upi_id,
        "upiReference": request.upi_reference,
        "providerConfirmed": request.provider_confirmed if received else None,
    }


def call_request_to_dict(
    request: CallRequest,
    requester: User | None,
    provider: User | None,
    viewer_id: uuid.UUID,
    on_another_device: bool = False,
) -> dict:
    """`role` tells the app which side the caller is. `amount` is the offer; `holdAmount` /
    `minimumAmount` is the least payable (nothing is held in the new flow). `onAnotherDevice`: the
    call is on the viewer's OTHER phone (they are signed in on several), so this phone must
    not offer to join, rejoin or decline it."""
    return {
        "id": str(request.id),
        "role": "requester" if viewer_id == request.requester_id else "provider",
        "status": request.status,
        "topic": request.topic,
        "durationMinutes": request.duration_minutes,
        "amount": str(request.amount),
        "holdAmount": str(request.hold_amount),
        "requester": call_request_party_to_dict(requester),
        "provider": call_request_party_to_dict(provider),
        "truckRouteId": str(request.truck_route_id) if request.truck_route_id else None,
        "expiresAt": request.expires_at.isoformat(),
        "createdAt": request.created_at.isoformat(),
        "respondedAt": request.responded_at.isoformat() if request.responded_at else None,
        "callStartedAt": request.call_started_at.isoformat() if request.call_started_at else None,
        # While the phone is ringing: when the ring stops (the requester's ring screen counts down to it).
        "ringEndsAt": (request.ring_started_at + timedelta(seconds=CALL_RING_SECONDS)).isoformat()
        if request.status == "ringing" and request.ring_started_at
        else None,
        "callEndedAt": request.call_ended_at.isoformat() if request.call_ended_at else None,
        "connectedSeconds": request.connected_seconds,
        "feedbackDeadline": request.feedback_deadline.isoformat() if request.feedback_deadline else None,
        # New flow: `holdAmount` is the minimum payable. Nothing is held; the requester pays UPI directly.
        "minimumAmount": str(request.hold_amount),
        "thresholdMet": request.threshold_met,
        "paymentOutcome": request.payment_outcome,
        "paidAmount": str(request.paid_amount) if request.paid_amount is not None else None,
        "paymentReason": request.payment_reason,
        "paymentDecidedAt": request.payment_decided_at.isoformat() if request.payment_decided_at else None,
        "providerConfirmed": request.provider_confirmed,
        "payment": payment_details(request, provider, viewer_id),
        "onAnotherDevice": on_another_device,
    }
