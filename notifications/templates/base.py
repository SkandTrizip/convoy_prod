import random
from typing import Sequence


def pick_variant(variants: Sequence[str], recent: Sequence[str]) -> str:
    """Pick a line, excluding ones the user was recently shown, so the same
    driver doesn't see the same copy twice in a row. Falls back to the full
    set if every variant has been recently shown."""
    recent_set = set(recent)
    candidates = [v for v in variants if v not in recent_set]
    if not candidates:
        candidates = list(variants)
    return random.choice(candidates)


def render(template: str, **variables) -> str:
    """Interpolate {driver_name}/{city}/etc placeholders; leaves the template
    untouched if a variable is missing rather than raising."""
    try:
        return template.format(**variables)
    except (KeyError, IndexError):
        return template
