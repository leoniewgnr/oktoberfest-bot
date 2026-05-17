"""Slot filters — decide which scraped reservation slots warrant a Telegram alert.

User rule: alert only on Abend (evening) slots and on slots whose shift is
unknown (defensive — don't risk missing a date that just didn't expose its
shift word). Suppress everything that's clearly Mittag, Vormittag, or daytime.

Concretely:
- Alert: "Abend", "Abendveranstaltung", "dinner", "evening", numeric times >= 18:00,
        and any text where we can't determine the shift (no Mittag/Vormittag/Abend
        word and no numeric time, e.g. date-only "Mittwoch, 23.09.2026").
- Skip:  "Mittag", "Mittagstisch", "lunch", "noon", "Vormittag", numeric times
        entirely within 10:00–17:59.
"""

from __future__ import annotations

import re

# "Mittag" or "Mittagstisch" but NOT "Vormittag" (= morning, not lunch).
_MITTAG_RE = re.compile(r"(?<!vor)\bmittag", re.IGNORECASE)
_VORMITTAG_RE = re.compile(r"\bvormittag", re.IGNORECASE)
_FRUEHSCHOPPEN_RE = re.compile(r"\bfr(ü|ue?)hschoppen", re.IGNORECASE)
_ABEND_RE = re.compile(r"\babend", re.IGNORECASE)  # Abend, Abendveranstaltung
_LUNCH_EN_RE = re.compile(r"\b(lunch|noon)\b", re.IGNORECASE)
_DINNER_EN_RE = re.compile(r"\b(dinner|evening)\b", re.IGNORECASE)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def is_abend(text: str) -> bool:
    """True if text clearly indicates an evening shift."""
    if not text:
        return False
    if _ABEND_RE.search(text) or _DINNER_EN_RE.search(text):
        return True
    # Any numeric time at or after 18:00 in the text → evening.
    for m in _TIME_RE.finditer(text):
        hour = int(m.group(1))
        if 18 <= hour <= 23:
            return True
    return False


def is_mittag(text: str) -> bool:
    """True if text clearly indicates a lunch / midday shift."""
    if not text:
        return False
    return bool(_MITTAG_RE.search(text) or _LUNCH_EN_RE.search(text))


def is_fruehschoppen(text: str) -> bool:
    """True if text clearly indicates a Frühschoppen (Sunday-morning beer) shift."""
    if not text:
        return False
    return bool(_FRUEHSCHOPPEN_RE.search(text))


def is_vormittag_or_daytime(text: str) -> bool:
    """True if text clearly indicates morning/daytime BUT not evening."""
    if not text:
        return False
    if _VORMITTAG_RE.search(text):
        return True
    # All numeric times are in the 10:00–17:59 daytime band AND there's at least one.
    hours = [int(m.group(1)) for m in _TIME_RE.finditer(text)]
    if hours and all(10 <= h <= 17 for h in hours):
        return True
    return False


def should_alert(date_text: str = "", time_text: str = "") -> bool:
    """Return True if a slot should fire a Telegram alert.

    Rule: alert on Abend (or anything clearly evening) and on slots whose
    shift cannot be determined. Suppress explicit Mittag/Vormittag/daytime.

    Either argument may be empty. They're inspected together so the weekday
    can come from date_text while the shift word comes from time_text.
    """
    combined = f"{date_text or ''} {time_text or ''}".strip()
    if not combined:
        return True  # truly nothing → alert defensively

    if is_abend(combined):
        return True
    if is_mittag(combined):
        return False
    if is_fruehschoppen(combined):
        return False
    if is_vormittag_or_daytime(combined):
        return False
    # No discriminating signal at all → alert (defensive).
    return True
