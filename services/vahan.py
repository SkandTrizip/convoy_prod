from services.ulip import verify_vehicle_registration


async def verify_vehicle_with_vahan(vehicle_number: str):
    """Verify vehicle using ULIP VAHAN API."""
    result = verify_vehicle_registration(vehicle_number)
    if not result.get("verified"):
        return None
    return {
        "verified": True,
        "vehicleNumber": result.get("vehicleNumber") or vehicle_number,
        "message": result.get("message"),
        **(result.get("data") or {}),
        "mock": result.get("mock", False),
    }
