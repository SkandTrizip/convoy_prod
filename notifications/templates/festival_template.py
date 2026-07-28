"""Copy library from the marketing push-notification spec, trigger #18 (Festivals).

FESTIVAL_CALENDAR is keyed by (year, month, day) rather than just (month, day)
because most of these are lunar-calendar festivals that shift every Gregorian
year — a fixed (month, day) mapping would silently misfire in later years.

Only Independence Day (a fixed Gregorian date) is filled in below. The lunar
festivals (Raksha Bandhan, Janmashtami, Ganesh Chaturthi, Navratri) are left
unset — add each year's verified date before the campaign should fire for it.
"""
from datetime import date
from typing import Dict, List, Optional, Tuple

FESTIVAL_CALENDAR: Dict[Tuple[int, int, int], str] = {
    (2026, 8, 15): "Independence Day",
    # (2026, month, day): "Raksha Bandhan",   # TODO: verify and fill in
    # (2026, month, day): "Janmashtami",      # TODO: verify and fill in
    # (2026, month, day): "Ganesh Chaturthi",  # TODO: verify and fill in
    # (2026, month, day): "Navratri",         # TODO: verify and fill in
}

VARIANTS_BY_FESTIVAL: Dict[str, List[str]] = {
    "Raksha Bandhan": [
        "Jaldi load pakdo... Rakhi time pe ghar pahunchna hai.",
        "Behen intezaar kar rahi hogi. Safe drive karo.",
    ],
    "Janmashtami": [
        "Kanha ka aashirwad aur achha freight, dono mile.",
    ],
    "Independence Day": [
        "Bharat chal raha hai kyunki aap chal rahe ho.",
        "Har safar desh ki tarakki ka hissa hai.",
    ],
    "Ganesh Chaturthi": [
        "Ganpati Bappa aaj naye mauke laayein.",
    ],
    "Navratri": [
        "Shubh shuruaat ek naye load se.",
    ],
}

TITLE = "Festival Greetings"


def get_active_festival(today: date) -> Optional[str]:
    return FESTIVAL_CALENDAR.get((today.year, today.month, today.day))
