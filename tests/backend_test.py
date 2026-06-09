"""
Convoy Backend API Tests
Tests the complete flow: OTP -> KYC -> Vehicle -> Post -> Search -> Smart Match
"""
import os
import pytest
import requests
import time
from datetime import datetime

# Use public URL for testing
BASE_URL = os.environ.get('EXPO_PUBLIC_BACKEND_URL', 'https://ride-match-trucks.preview.emergentagent.com').rstrip('/')
API = f"{BASE_URL}/api"

# Unique mobile for this test run to avoid collisions
TEST_MOBILE_A = f"+9198{int(time.time()) % 100000000:08d}"
TEST_MOBILE_B = f"+9197{int(time.time()) % 100000000:08d}"

# Shared state across tests
STATE = {}


@pytest.fixture(scope="session")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


# ==================== HEALTH ====================
def test_health_root(s):
    r = s.get(f"{API}/")
    assert r.status_code == 200
    assert "Convoy" in r.json().get("message", "")


def test_truck_types(s):
    r = s.get(f"{API}/truck-types")
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert isinstance(data["truckTypes"], list)
    assert "Container" in data["truckTypes"]


# ==================== AUTH ====================
def test_send_otp_dev_mode(s):
    r = s.post(f"{API}/auth/send-otp", json={"mobile": TEST_MOBILE_A})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["success"] is True
    assert data.get("otp") is not None, "Dev mode should return OTP"
    assert len(data["otp"]) == 4
    STATE["otp_a"] = data["otp"]


def test_verify_otp_invalid(s):
    s.post(f"{API}/auth/send-otp", json={"mobile": TEST_MOBILE_B})
    r = s.post(f"{API}/auth/verify-otp", json={"mobile": TEST_MOBILE_B, "otp": "000000"})
    assert r.status_code == 400


def test_verify_otp_success_create_user(s):
    # Re-send to ensure OTP available
    r1 = s.post(f"{API}/auth/send-otp", json={"mobile": TEST_MOBILE_A})
    otp = r1.json()["otp"]
    r = s.post(f"{API}/auth/verify-otp", json={"mobile": TEST_MOBILE_A, "otp": otp})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["success"] is True
    assert data["user"]["mobile"] == TEST_MOBILE_A
    assert data["user"]["kycStatus"] == "pending"
    assert data.get("accessToken"), "verify-otp should return JWT accessToken"
    STATE["user_id"] = data["user"]["_id"]
    STATE["access_token"] = data["accessToken"]
    s.headers.update({"Authorization": f"Bearer {STATE['access_token']}"})


# ==================== USER PROFILE ====================
def test_get_user_profile(s):
    uid = STATE["user_id"]
    r = s.get(f"{API}/user/profile/{uid}")
    assert r.status_code == 200
    assert r.json()["user"]["mobile"] == TEST_MOBILE_A


def test_update_user_profile(s):
    uid = STATE["user_id"]
    r = s.put(f"{API}/user/profile/{uid}", json={"name": "Test Driver"})
    assert r.status_code == 200
    # Verify persistence
    r2 = s.get(f"{API}/user/profile/{uid}")
    assert r2.json()["user"]["name"] == "Test Driver"


# ==================== LOCATIONS ====================
def test_locations_search(s):
    r = s.get(f"{API}/locations/search", params={"query": "delhi"})
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert len(data["locations"]) > 0
    names = [l["name"] for l in data["locations"]]
    assert any("Delhi" in n for n in names)


def test_locations_search_short_query(s):
    r = s.get(f"{API}/locations/search", params={"query": "de"})
    assert r.status_code == 200
    assert r.json()["locations"] == []


# ==================== KYC ====================
def test_kyc_digilocker_no_longer_auto_approves(s):
    uid = STATE["user_id"]
    payload = {"method": "digilocker", "data": {"aadhaar": "123412341234", "name": "Test Driver"}}
    r = s.post(f"{API}/kyc/submit/{uid}", json=payload)
    assert r.status_code == 400


def test_kyc_aadhaar_otp_flow(s):
    uid = STATE["user_id"]
    # Cashfree sandbox test Aadhaar; OTP is always 111000 in test env
    aadhaar = "655675523712"
    r1 = s.post(f"{API}/kyc/aadhaar/send-otp/{uid}", json={"aadhaarNumber": aadhaar})
    assert r1.status_code == 200, r1.text
    ref_id = r1.json()["refId"]
    r2 = s.post(
        f"{API}/kyc/aadhaar/verify/{uid}",
        json={"refId": ref_id, "otp": "111000", "aadhaarNumber": aadhaar},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "approved"


def test_kyc_status_approved(s):
    uid = STATE["user_id"]
    r = s.get(f"{API}/kyc/status/{uid}")
    assert r.status_code == 200
    data = r.json()
    assert data["kyc"]["status"] == "approved"


# ==================== VEHICLE ====================
def test_add_vehicle(s):
    uid = STATE["user_id"]
    payload = {"vehicleNumber": "MH12AB1234", "truckType": "Container"}
    r = s.post(f"{API}/vehicle/add/{uid}", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["success"] is True
    assert data["vehicle"]["verificationStatus"] == "verified"
    assert data["vehicle"]["vehicleNumber"] == "MH12AB1234"
    STATE["vehicle_id"] = data["vehicle"]["_id"]


def test_add_vehicle_duplicate(s):
    uid = STATE["user_id"]
    payload = {"vehicleNumber": "MH12AB1234", "truckType": "Container"}
    r = s.post(f"{API}/vehicle/add/{uid}", json=payload)
    assert r.status_code == 400


def test_list_vehicles(s):
    uid = STATE["user_id"]
    r = s.get(f"{API}/vehicle/list/{uid}")
    assert r.status_code == 200
    vehicles = r.json()["vehicles"]
    assert len(vehicles) >= 1
    assert any(v["vehicleNumber"] == "MH12AB1234" for v in vehicles)


def test_add_vehicle_without_kyc(s):
    # Create unverified user
    mob = f"+9196{int(time.time()) % 100000000:08d}"
    r1 = s.post(f"{API}/auth/send-otp", json={"mobile": mob})
    otp = r1.json()["otp"]
    r2 = s.post(f"{API}/auth/verify-otp", json={"mobile": mob, "otp": otp})
    new_uid = r2.json()["user"]["_id"]
    new_token = r2.json()["accessToken"]
    r = s.post(
        f"{API}/vehicle/add/{new_uid}",
        json={"vehicleNumber": "DL01AB9999", "truckType": "Pickup"},
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert r.status_code == 403


# ==================== SEARCH (no posts yet) -> TRACK DEMAND ====================
DELHI = {"name": "Delhi", "lat": 28.7041, "lng": 77.1025, "type": "local"}
MUMBAI = {"name": "Mumbai", "lat": 19.0760, "lng": 72.8777, "type": "local"}
BANGALORE = {"name": "Bangalore", "lat": 12.9716, "lng": 77.5946, "type": "local"}


def test_search_trucks_no_results(s):
    # Create a 2nd user (searcher) so smart match can trigger
    mob = f"+9195{int(time.time()) % 100000000:08d}"
    r1 = s.post(f"{API}/auth/send-otp", json={"mobile": mob})
    otp = r1.json()["otp"]
    r2 = s.post(f"{API}/auth/verify-otp", json={"mobile": mob, "otp": otp})
    STATE["searcher_id"] = r2.json()["user"]["_id"]
    STATE["searcher_token"] = r2.json()["accessToken"]

    # Search for unique route - should return empty initially
    payload = {"origin": BANGALORE, "destination": MUMBAI, "truckType": "Trailer"}
    r = s.post(f"{API}/search/trucks", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert isinstance(data["posts"], list)


def test_track_search_demand(s):
    uid = STATE["searcher_id"]
    payload = {"origin": DELHI, "destination": MUMBAI, "truckType": "Container"}
    r = s.post(
        f"{API}/search/track-demand/{uid}",
        json=payload,
        headers={"Authorization": f"Bearer {STATE['searcher_token']}"},
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


# ==================== TRUCK POST CREATION & SMART MATCH ====================
def test_create_truck_post(s):
    uid = STATE["user_id"]
    payload = {
        "vehicleId": STATE["vehicle_id"],
        "origin": DELHI,
        "destination": MUMBAI,
        "currentLocation": DELHI,
    }
    r = s.post(f"{API}/posts/create/{uid}", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["post"]["status"] == "active"
    assert data["post"]["truckType"] == "Container"
    STATE["post_id"] = data["post"]["_id"]


def test_smart_match_notification_sent(s):
    """After truck post is created matching the tracked demand, searcher should get notification"""
    uid = STATE["searcher_id"]
    time.sleep(1)  # allow processing
    r = s.get(
        f"{API}/notifications/{uid}",
        headers={"Authorization": f"Bearer {STATE['searcher_token']}"},
    )
    assert r.status_code == 200
    notifs = r.json()["notifications"]
    smart_matches = [n for n in notifs if n.get("type") == "smart_match"]
    assert len(smart_matches) >= 1, f"Expected smart_match notification, got: {notifs}"


def test_get_my_posts_active(s):
    uid = STATE["user_id"]
    r = s.get(f"{API}/posts/my-posts/{uid}", params={"status": "active"})
    assert r.status_code == 200
    posts = r.json()["posts"]
    assert len(posts) >= 1
    assert any(p["_id"] == STATE["post_id"] for p in posts)


def test_search_trucks_finds_post(s):
    payload = {"origin": DELHI, "destination": MUMBAI, "truckType": "Container"}
    r = s.post(f"{API}/search/trucks", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    found = [p for p in data["posts"] if p["_id"] == STATE["post_id"]]
    assert len(found) == 1
    assert found[0]["userMobile"] == TEST_MOBILE_A


def test_search_trucks_radius_boundary(s):
    """Origin within 100km - Gurgaon is ~30km from Delhi"""
    gurgaon = {"name": "Gurgaon", "lat": 28.4595, "lng": 77.0266, "type": "local"}
    payload = {"origin": gurgaon, "destination": MUMBAI, "truckType": "Container"}
    r = s.post(f"{API}/search/trucks", json=payload)
    assert r.status_code == 200
    assert r.json()["count"] >= 1


def test_search_trucks_outside_radius(s):
    """Bangalore is too far from Delhi origin"""
    payload = {"origin": BANGALORE, "destination": MUMBAI, "truckType": "Container"}
    r = s.post(f"{API}/search/trucks", json=payload)
    assert r.status_code == 200
    # Should not include our Delhi->Mumbai post
    found = [p for p in r.json()["posts"] if p["_id"] == STATE["post_id"]]
    assert len(found) == 0


# ==================== CALL LOG ====================
def test_log_call(s):
    uid = STATE["searcher_id"]
    r = s.post(
        f"{API}/search/log-call/{uid}",
        json={"postId": STATE["post_id"]},
        headers={"Authorization": f"Bearer {STATE['searcher_token']}"},
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


# ==================== NOTIFICATIONS ====================
def test_get_notifications(s):
    uid = STATE["searcher_id"]
    r = s.get(
        f"{API}/notifications/{uid}",
        headers={"Authorization": f"Bearer {STATE['searcher_token']}"},
    )
    assert r.status_code == 200
    assert isinstance(r.json()["notifications"], list)


# ==================== ANALYTICS ====================
def test_admin_analytics(s):
    r = s.get(f"{API}/admin/analytics")
    assert r.status_code == 200, r.text
    a = r.json()["analytics"]
    for key in ["totalUsers", "approvedKYC", "totalVehicles", "verifiedVehicles",
                "activePosts", "totalSearches", "totalCalls", "topRoutes"]:
        assert key in a
    assert a["totalUsers"] >= 1
    assert a["approvedKYC"] >= 1
    assert a["verifiedVehicles"] >= 1
    assert a["activePosts"] >= 1


# ==================== POST LIFECYCLE ====================
def test_reactivate_post_requires_expired(s):
    """Active posts cannot be reactivated — only expired ones."""
    r = s.post(f"{API}/posts/reactivate/{STATE['post_id']}")
    assert r.status_code == 400


def test_delete_post(s):
    r = s.delete(f"{API}/posts/delete/{STATE['post_id']}")
    assert r.status_code == 200
    # verify gone
    r2 = s.delete(f"{API}/posts/delete/{STATE['post_id']}")
    assert r2.status_code == 404
