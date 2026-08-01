"""Scratch-card reward constants. Money here is integer PAISE, not the wallet's
Decimal rupees — conversion happens once, at the services.wallet.credit_wallet()
call boundary in scratch_service.py. Keeping the reward math in integers avoids
float rounding drift across the weighted-random/outlier-correction pipeline.

These are hardcoded, not env vars: bump ACTIVE_PHASE_INDEX (or tune the other
constants) and redeploy to move phases — this is reward-shape/business logic,
not per-environment config. Run scripts/simulate_rewards.py after any change
to sanity-check projected payout before it touches production money.
"""

from datetime import timedelta

# ---------------------------------------------------------------------------
# Card issuance
# ---------------------------------------------------------------------------
CARD_EXPIRY_DAYS = 29
CARD_EXPIRY = timedelta(days=CARD_EXPIRY_DAYS)

# ---------------------------------------------------------------------------
# Reward amounts
# ---------------------------------------------------------------------------
# Paid once, on the user's very first-ever REVEALED scratch card. Fixed, not random.
FIRST_SCRATCH_FIXED_REWARD_PAISE = 5000  # Rs.50.00

# Per-user monthly ceiling, keyed by "number of 30-day windows since the
# user's first reveal" (month 0 = days 0-29 after their first reveal, etc).
MONTHLY_MAX_CEILING_PAISE = {
    0: 10000,  # Rs.100 max
    1: 6000,  # Rs.60 max
    2: 4000,  # Rs.40 max
    3: 2500,  # Rs.25 max
}
# Beyond the last configured month, this floor applies forever.
FLOOR_MAX_CEILING_PAISE = 1000  # Rs.10 max

MIN_REWARD_PAISE = 100  # absolute floor on any non-fixed scratch: Rs.1.00

# ---------------------------------------------------------------------------
# Platform-wide phase — hardcoded, bump ACTIVE_PHASE_INDEX + redeploy to move
# phases. This is a SEPARATE cap from the monthly ceiling above; the stricter
# of the two always wins (see reward_engine.compute_reward_paise).
# ---------------------------------------------------------------------------
REWARD_PHASES = [
    {"name": "launch", "min_reward_paise": 500, "max_reward_paise": 10000},
    {"name": "growth", "min_reward_paise": 300, "max_reward_paise": 5000},
    {"name": "scale", "min_reward_paise": 100, "max_reward_paise": 2000},
]
ACTIVE_PHASE_INDEX = 0

# ---------------------------------------------------------------------------
# Outlier correction — throttles users whose average payout runs too hot vs
# the platform average. Self-healing: no separate "release" job needed.
# ---------------------------------------------------------------------------
OUTLIER_THRESHOLD = 1.5
MIN_CORRECTION_FACTOR = 0.2

# ---------------------------------------------------------------------------
# Weighted random distribution shape
# ---------------------------------------------------------------------------
NUM_BUCKETS = 10
SKEW = 2.2  # higher = more weight on the smallest bucket (frequent small wins)
