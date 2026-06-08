from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import logger
from database import get_session
from db.base import User
from db.serializers import parse_uuid, user_to_dict
from middleware.auth import require_path_user
from models import BatchVerifyRequest, VerifyDLRequest, VerifyVehicleRequest
from services.ulip import (
    to_api_response,
    validate_dl_dob,
    verify_driving_license,
    verify_vehicle_registration,
)

router = APIRouter(prefix="/verification", tags=["verification"])


async def _get_user_or_404(session: AsyncSession, user_id: str) -> User:
    result = await session.execute(select(User).where(User.id == parse_uuid(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _save_dl_verification(user: User, dl_number: str, result: dict) -> None:
    status = user.verification_status or {}
    status["dl"] = {
        "status": "verified" if result.get("verified") else "failed",
        "verified": bool(result.get("verified")),
        "verifiedAt": datetime.utcnow().isoformat(),
        "dlNumber": dl_number,
        "data": result.get("data"),
        "message": result.get("message"),
    }
    user.verification_status = status


@router.post("/vehicle/{user_id}")
async def verify_vehicle(
    user_id: str,
    request: VerifyVehicleRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Verify vehicle registration via ULIP VAHAN proxy."""
    try:
        await _get_user_or_404(session, user_id)
        result = verify_vehicle_registration(request.vehicleNumber)

        if not result.get("verified"):
            raise HTTPException(
                status_code=400,
                detail=result.get("message") or "Vehicle verification failed",
            )

        return to_api_response(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in verify_vehicle: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/dl/{user_id}")
async def verify_dl(
    user_id: str,
    request: VerifyDLRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Verify driving license via ULIP SARATHI proxy."""
    try:
        user = await _get_user_or_404(session, user_id)
        validate_dl_dob(request.dob)
        result = verify_driving_license(request.dlnumber, request.dob)

        _save_dl_verification(user, request.dlnumber, result)
        await session.commit()
        await session.refresh(user)

        if not result.get("verified"):
            raise HTTPException(
                status_code=400,
                detail=result.get("message") or "Driving license verification failed",
            )

        response = to_api_response(result)
        response["user"] = user_to_dict(user)
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in verify_dl: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/batch/{user_id}")
async def batch_verify(
    user_id: str,
    request: BatchVerifyRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_path_user),
):
    """Verify multiple documents in one request."""
    try:
        user = await _get_user_or_404(session, user_id)
        results = []

        for doc in request.documents:
            doc_type = doc.type.lower()
            if doc_type == "dl":
                dob = doc.data.get("dob", "")
                dl_number = doc.data.get("dlNumber") or doc.data.get("dlnumber", "")
                validate_dl_dob(dob)
                result = verify_driving_license(dl_number, dob)
                _save_dl_verification(user, dl_number, result)
                results.append({"type": "dl", **to_api_response(result)})
            elif doc_type == "vehicle":
                vehicle_number = doc.data.get("vehicleNumber", "")
                result = verify_vehicle_registration(vehicle_number)
                results.append({"type": "vehicle", **to_api_response(result)})
            else:
                results.append({
                    "type": doc_type,
                    "success": False,
                    "verified": False,
                    "message": f"Unsupported document type: {doc.type}",
                })

        await session.commit()
        await session.refresh(user)

        return {
            "success": True,
            "results": results,
            "user": user_to_dict(user),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in batch_verify: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
