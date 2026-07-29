"""Audience filter registry — compiles a JSON AND/OR condition tree (built by
the admin panel's visual filter builder) into a SQLAlchemy boolean expression
over `users` rows.

Every field resolver returns a standalone column expression (using correlated
scalar subqueries for aggregate fields), so nesting AND/OR groups is just
and_()/or_() composition — no joins or GROUP BY to reconcile between sibling
conditions.

`app_version` is deliberately absent: no such column exists on User yet and
the mobile app doesn't report one, so there's nothing to filter on. Add it
here once that data exists — the registry is the only place that needs to
change (wizard UI, query builder, etc. all read from FILTER_REGISTRY).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import ColumnElement, Select, and_, func, or_, select

from db.base import SearchDemand, Truck, TruckRoute, User, UserActivity, Wallet


class FilterError(ValueError):
    """Raised for a malformed or unsupported condition tree — always a 400 to the client."""


def _days_ago(days: float) -> datetime:
    return datetime.utcnow() - timedelta(days=days)


def _compare(column: ColumnElement, operator: str, value: Any) -> ColumnElement:
    if operator == "eq":
        return column == value
    if operator == "neq":
        return column != value
    if operator == "gt":
        return column > value
    if operator == "gte":
        return column >= value
    if operator == "lt":
        return column < value
    if operator == "lte":
        return column <= value
    if operator == "in":
        return column.in_(value if isinstance(value, list) else [value])
    if operator == "not_in":
        return column.notin_(value if isinstance(value, list) else [value])
    if operator == "between":
        lo, hi = value
        return column.between(lo, hi)
    raise FilterError(f"Unsupported operator '{operator}'")


def _date_compare(column: ColumnElement, operator: str, value: Any) -> ColumnElement:
    if operator == "before":
        return column < datetime.fromisoformat(value)
    if operator == "after":
        return column > datetime.fromisoformat(value)
    if operator == "within_last_days":
        return column >= _days_ago(float(value))
    if operator == "not_within_last_days":
        return or_(column.is_(None), column < _days_ago(float(value)))
    raise FilterError(f"Unsupported operator '{operator}' for a date field")


def _truck_count_subquery():
    return (
        select(func.count(Truck.id)).where(Truck.user_id == User.id).correlate(User).scalar_subquery()
    )


def _post_count_subquery():
    return (
        select(func.count(TruckRoute.id))
        .where(TruckRoute.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )


def _search_count_subquery():
    return (
        select(func.count(SearchDemand.id))
        .where(SearchDemand.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )


def _wallet_balance_subquery():
    return (
        select(func.coalesce(Wallet.available_balance, 0))
        .where(Wallet.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )


def _last_active_subquery():
    """Mirrors the (now-retired) InactiveUsersAudience: max activity timestamp
    across both searches and posts. UserActivity only keeps the last 10 rows
    per (user, type), but that trim removes the oldest rows, so max() is unaffected."""
    return (
        select(func.max(UserActivity.created_at))
        .where(UserActivity.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )


def _last_searched_subquery():
    return (
        select(func.max(SearchDemand.search_timestamp))
        .where(SearchDemand.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )


def _last_posted_subquery():
    return (
        select(func.max(TruckRoute.created_at))
        .where(TruckRoute.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )


def _most_frequent_name_subquery(json_column):
    name_expr = json_column["name"].as_string()
    return (
        select(name_expr)
        .where(SearchDemand.user_id == User.id)
        .group_by(name_expr)
        .order_by(func.count().desc())
        .limit(1)
        .correlate(User)
        .scalar_subquery()
    )


def _vehicle_type_condition(operator: str, value: Any) -> ColumnElement:
    values = value if isinstance(value, list) else [value]
    exists_clause = (
        select(Truck.id)
        .where(Truck.user_id == User.id, Truck.truck_type.in_(values))
        .correlate(User)
        .exists()
    )
    if operator == "in":
        return exists_clause
    if operator == "not_in":
        return ~exists_clause
    raise FilterError(f"Unsupported operator '{operator}' for vehicle_type")


# field key -> { label, value_type (for the frontend's input control), operators, resolve(operator, value) }
FILTER_REGISTRY: dict[str, dict[str, Any]] = {
    "kyc_status": {
        "label": "KYC Status",
        "value_type": "string",
        "operators": ["eq", "neq", "in", "not_in"],
        "resolve": lambda op, val: _compare(User.kyc_status, op, val),
    },
    "registration_date": {
        "label": "Registration Date",
        "value_type": "date",
        "operators": ["before", "after", "within_last_days", "not_within_last_days"],
        "resolve": lambda op, val: _date_compare(User.created_date, op, val),
    },
    "total_vehicles": {
        "label": "Total Vehicles",
        "value_type": "number",
        "operators": ["eq", "gt", "gte", "lt", "lte", "between"],
        "resolve": lambda op, val: _compare(_truck_count_subquery(), op, val),
    },
    "vehicle_type": {
        "label": "Vehicle Type (any of user's vehicles)",
        "value_type": "string_list",
        "operators": ["in", "not_in"],
        "resolve": _vehicle_type_condition,
    },
    "truck_posted_recently": {
        "label": "Truck Posted Recently",
        "value_type": "date",
        "operators": ["within_last_days", "not_within_last_days"],
        "resolve": lambda op, val: _date_compare(_last_posted_subquery(), op, val),
    },
    "wallet_balance": {
        "label": "Wallet Balance",
        "value_type": "number",
        "operators": ["eq", "gt", "gte", "lt", "lte", "between"],
        "resolve": lambda op, val: _compare(_wallet_balance_subquery(), op, val),
    },
    "number_of_posts": {
        "label": "Number of Posts",
        "value_type": "number",
        "operators": ["eq", "gt", "gte", "lt", "lte", "between"],
        "resolve": lambda op, val: _compare(_post_count_subquery(), op, val),
    },
    "number_of_searches": {
        "label": "Number of Searches",
        "value_type": "number",
        "operators": ["eq", "gt", "gte", "lt", "lte", "between"],
        "resolve": lambda op, val: _compare(_search_count_subquery(), op, val),
    },
    "most_searched_origin": {
        "label": "Most Searched Origin",
        "value_type": "string",
        "operators": ["eq", "neq"],
        "resolve": lambda op, val: _compare(_most_frequent_name_subquery(SearchDemand.origin), op, val),
    },
    "most_searched_destination": {
        "label": "Most Searched Destination",
        "value_type": "string",
        "operators": ["eq", "neq"],
        "resolve": lambda op, val: _compare(
            _most_frequent_name_subquery(SearchDemand.destination), op, val
        ),
    },
    "last_searched_time": {
        "label": "Last Searched Time",
        "value_type": "date",
        "operators": ["before", "after", "within_last_days", "not_within_last_days"],
        "resolve": lambda op, val: _date_compare(_last_searched_subquery(), op, val),
    },
    "last_active_date": {
        "label": "Last Active Date",
        "value_type": "date",
        "operators": ["before", "after", "within_last_days", "not_within_last_days"],
        "resolve": lambda op, val: _date_compare(_last_active_subquery(), op, val),
    },
}


def compile_condition(node: dict) -> ColumnElement:
    if "combinator" in node:
        combinator = str(node.get("combinator", "AND")).upper()
        children = [compile_condition(child) for child in node.get("rules", [])]
        if not children:
            raise FilterError("A rule group must contain at least one condition")
        return and_(*children) if combinator == "AND" else or_(*children)

    field = node.get("field")
    operator = node.get("operator")
    value = node.get("value")

    definition = FILTER_REGISTRY.get(field)
    if not definition:
        raise FilterError(f"Unknown filter field '{field}'")
    if operator not in definition["operators"]:
        raise FilterError(f"Operator '{operator}' is not supported for field '{field}'")

    resolve: Callable[[str, Any], ColumnElement] = definition["resolve"]
    return resolve(operator, value)


def build_audience_query(filter_tree: dict | None) -> Select:
    """SELECT of matching User rows. Always restricted to active accounts —
    suspended users are never a valid marketing audience, matching the
    behaviour of every audience builder that came before this system."""
    stmt = select(User).where(User.account_status == "active")
    if filter_tree and filter_tree.get("rules"):
        stmt = stmt.where(compile_condition(filter_tree))
    return stmt


def build_audience_count_query(filter_tree: dict | None) -> Select:
    stmt = select(func.count(User.id)).where(User.account_status == "active")
    if filter_tree and filter_tree.get("rules"):
        stmt = stmt.where(compile_condition(filter_tree))
    return stmt
