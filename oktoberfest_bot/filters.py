"""Slot filters — decide which scraped reservation slots warrant a Telegram alert.

User rule: skip Mon–Fri Mittag (incl. "Mittagstisch"). Alert on everything else:
all Abend any day, Sa/So Mittag, any non-Mittag shift on Mon–Fri.

Defensive: when the date text can't be parsed for a weekday, alert anyway —
false alert > missed reservation.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

# Full names first so "Mittwoch" matches before the short "Mi".
_WEEKDAY_RE = re.compile(
    r"\b(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag"
    r"|mo|di|mi|do|fr|sa|so)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
# Match "Mittag" or "Mittagstisch" but NOT "Vormittag" (= morning, NOT lunch).
_MITTAG_RE = re.compile(r"(?<!vor)\bmittag", re.IGNORECASE)

_WEEKDAY_TO_NUM = {
    "montag": 0, "mo": 0,
    "dienstag": 1, "di": 1,
    "mittwoch": 2, "mi": 2,
    "donnerstag": 3, "do": 3,
    "freitag": 4, "fr": 4,
    "samstag": 5, "sa": 5,
    "sonntag": 6, "so": 6,
}


def extract_weekday(text: str) -> Optional[int]:
    """Return 0..6 (Mon=0..Sun=6) parsed from text, or None if undetermined."""
    if not text:
        return None
    m = _WEEKDAY_RE.search(text)
    if m:
        return _WEEKDAY_TO_NUM[m.group(1).lower()]
    m = _DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).weekday()
        except ValueError:
            return None
    return None


def is_mittag(text: str) -> bool:
    return bool(_MITTAG_RE.search(text or ""))


def should_alert(date_text: str = "", time_text: str = "") -> bool:
    """Return True if a slot should fire a Telegram alert.

    Rule: skip ONLY Mon–Fri Mittag (including "Mittagstisch"). Alert on
    everything else, including any slot we can't parse confidently.

    Either argument may be empty. They're inspected together so the weekday
    can come from `date_text` while the shift word ("Mittag"/"Abend") comes
    from `time_text` — the common case for time-slot tents.
    """
    combined = f"{date_text or ''} {time_text or ''}".strip()
    wd = extract_weekday(combined)
    if wd is None:
        return True  # unknown weekday → don't risk missing
    if wd >= 5:  # Sa or So → always alert
        return True
    return not is_mittag(combined)
