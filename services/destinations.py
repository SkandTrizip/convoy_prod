import uuid
from typing import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import TruckRouteDestination
from models import Location
from services.spatial import make_geography_point


def destination_to_dict(destination: TruckRouteDestination) -> dict:
    return {"_id": str(destination.id), "position": destination.position, **destination.destination}


async def create_destinations(
    session: AsyncSession,
    truck_route_id: uuid.UUID,
    destinations: list[Location],
) -> list[TruckRouteDestination]:
    """Insert destination rows for a route, ordered 1..N. Caller commits."""
    rows = [
        TruckRouteDestination(
            truck_route_id=truck_route_id,
            position=index + 1,
            destination_name=destination.name,
            destination_location=make_geography_point(destination.lng, destination.lat),
            destination=destination.model_dump(),
        )
        for index, destination in enumerate(destinations)
    ]
    session.add_all(rows)
    return rows


async def replace_destinations(
    session: AsyncSession,
    truck_route_id: uuid.UUID,
    destinations: list[Location],
) -> list[TruckRouteDestination]:
    """Full replace of a route's destinations. Caller commits."""
    await session.execute(
        delete(TruckRouteDestination).where(TruckRouteDestination.truck_route_id == truck_route_id)
    )
    return await create_destinations(session, truck_route_id, destinations)


async def get_destinations_for_route(
    session: AsyncSession, truck_route_id: uuid.UUID
) -> list[TruckRouteDestination]:
    result = await session.execute(
        select(TruckRouteDestination)
        .where(TruckRouteDestination.truck_route_id == truck_route_id)
        .order_by(TruckRouteDestination.position)
    )
    return list(result.scalars().all())


async def get_destinations_for_routes(
    session: AsyncSession, truck_route_ids: Sequence[uuid.UUID]
) -> dict[str, list[TruckRouteDestination]]:
    """Batch fetch, grouped by route id (as str), ordered by position within each group."""
    if not truck_route_ids:
        return {}

    result = await session.execute(
        select(TruckRouteDestination)
        .where(TruckRouteDestination.truck_route_id.in_(truck_route_ids))
        .order_by(TruckRouteDestination.truck_route_id, TruckRouteDestination.position)
    )

    grouped: dict[str, list[TruckRouteDestination]] = {}
    for row in result.scalars().all():
        grouped.setdefault(str(row.truck_route_id), []).append(row)
    return grouped
