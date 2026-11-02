"""Pure rules for the paid 1:1 call amount (no DB). DB-backed money tests are in
test_call_requests.py."""

from decimal import Decimal

import pytest

from services.call_requests import compute_hold

D = Decimal


@pytest.mark.parametrize(
    "amount,hold",
    [
        ("50", "25.00"),  # 25% = 12.50, floor is Rs25
        ("100", "25.00"),  # 25% = 25.00
        ("101", "25.25"),
        ("150", "37.50"),
        ("333.33", "83.33"),  # 83.3325 rounds half up
        ("10000", "2500.00"),
    ],
)
def test_hold_is_a_quarter_with_a_25_rupee_floor(amount, hold):
    assert compute_hold(D(amount)) == D(hold)


def test_hold_never_exceeds_the_offer():
    assert compute_hold(D("20")) == D("20")
