"""Pure reward-computation functions — no DB access on purpose. Fully unit
testable with plain Python values, and callable from scripts/simulate_rewards.py.
"""

import random
from datetime import datetime

from services import reward_config as config


def outlier_correction_factor(user_avg_paise: float, global_avg_paise: float) -> float:
    """Throttles a user's ceiling if their average payout is running too far
    ahead of the platform average. Self-healing: once throttled, their own
    future rewards are smaller, which pulls their average back down over
    time — no separate "release" job needed."""
    if global_avg_paise <= 0:
        return 1.0
    threshold = global_avg_paise * config.OUTLIER_THRESHOLD
    if user_avg_paise <= threshold:
        return 1.0
    overshoot_ratio = user_avg_paise / threshold
    return max(1 / overshoot_ratio, config.MIN_CORRECTION_FACTOR)


def monthly_ceiling_paise(first_scratch_at: datetime, now: datetime) -> int:
    """Time-based ceiling, keyed off 30-day windows since the user's first-ever reveal."""
    days_elapsed = max((now - first_scratch_at).days, 0)
    month_index = days_elapsed // 30
    return config.MONTHLY_MAX_CEILING_PAISE.get(month_index, config.FLOOR_MAX_CEILING_PAISE)


def weighted_random_reward_paise(min_paise: int, max_paise: int) -> int:
    """Weighted random within [min_paise, max_paise], skewed so small wins are
    common and the top end is rare. Tune config.SKEW via simulate_rewards.py
    before changing in production."""
    if max_paise <= min_paise:
        return int(min_paise)

    bucket_size = (max_paise - min_paise) / config.NUM_BUCKETS
    weights = [config.SKEW ** (config.NUM_BUCKETS - i) for i in range(config.NUM_BUCKETS)]
    bucket_idx = random.choices(range(config.NUM_BUCKETS), weights=weights, k=1)[0]
    bucket_min = min_paise + bucket_idx * bucket_size
    bucket_max = bucket_min + bucket_size
    return round(random.uniform(bucket_min, bucket_max))


def compute_reward_paise(
    total_scratches: int,
    total_reward_received_paise: int,
    first_scratch_at: datetime | None,
    platform_total_scratch_count: int,
    platform_total_reward_sum_paise: int,
    now: datetime,
) -> int:
    """Single entry point used by the reveal endpoint. Takes plain values (not
    ORM objects) so it stays independently testable/simulatable."""
    # --- Case 1: user's first-ever reveal — fixed bonus, no randomness ---
    if total_scratches == 0:
        return config.FIRST_SCRATCH_FIXED_REWARD_PAISE

    # --- Case 2: normal scratch ---
    phase = config.REWARD_PHASES[config.ACTIVE_PHASE_INDEX]

    assert first_scratch_at is not None, "first_scratch_at must be set after the first reveal"
    ceiling = monthly_ceiling_paise(first_scratch_at, now)
    effective_max = min(ceiling, phase["max_reward_paise"])

    user_avg = total_reward_received_paise / max(total_scratches, 1)
    global_avg = platform_total_reward_sum_paise / max(platform_total_scratch_count, 1)
    correction = outlier_correction_factor(user_avg, global_avg)

    final_max = max(round(effective_max * correction), config.MIN_REWARD_PAISE)
    final_min = max(min(phase["min_reward_paise"], final_max // 2), config.MIN_REWARD_PAISE)

    return weighted_random_reward_paise(final_min, final_max)
