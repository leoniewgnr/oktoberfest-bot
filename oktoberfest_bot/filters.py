"""Slot filters — decide which scraped reservation slots warrant a Telegram alert.

The goal is zero missed Fri/Sat/Sun evening slots, so the weekday decides
whether any filtering happens at all:

- Fri, Sat, Sun (weekday 4, 5, 6): ALWAYS alert. No shift filtering, whatever
  the label says. A noisy weekend alert costs nothing, a missed one is fatal.
- Weekday unknown (unparseable date): ALWAYS alert.
- Mon–Thu (0–3): suppress slots that are clearly Mittag, Vormittag,
  Frühschoppen or daytime-only (all times within 10:00–17:59). Abend and
  anything undetermined still alerts.
"""

from __future__ import annotations

import re
from datetime import date

# "Mittag" or "Mittagstisch" but NOT "Vormittag" (= morning, not lunch).
_MITTAG_RE = re.compile(r"(?<!vor)\bmittag", re.IGNORECASE)
_VORMITTAG_RE = re.compile(r"\bvormittag", re.IGNORECASE)
_FRUEHSCHOPPEN_RE = re.compile(r"\bfr(ü|ue?)hschoppen", re.IGNORECASE)
# No leading \b: German compounds ("Samstagabend", "Freitagabend") must match.
_ABEND_RE = re.compile(r"abend", re.IGNORECASE)
_LUNCH_EN_RE = re.compile(r"\b(lunch|noon)\b", re.IGNORECASE)
_DINNER_EN_RE = re.compile(r"\b(dinner|evening)\b", re.IGNORECASE)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")

_DATE_RE = re.compile(r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\b")
_WEEKDAY_RE = re.compile(
    r"\b(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonnabend|sonntag)\b",
    re.IGNORECASE,
)
_WEEKDAY_WORDS = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonnabend": 5,
    "sonntag": 6,
}


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


def parse_weekday(text: str) -> int | None:
    """Weekday (0=Mon..6=Sun) of a German slot text, or None if undeterminable.

    An explicit dd.mm.yyyy date wins over the weekday word, which is free-text
    and may be part of an ordinal like "1. Sonntag".
    """
    if not text:
        return None

    for m in _DATE_RE.finditer(text):
        day, month, year = (int(g) for g in m.groups())
        try:
            return date(year, month, day).weekday()
        except ValueError:
            continue  # e.g. 31.02. — try the next date-looking match

    m = _WEEKDAY_RE.search(text)
    if m:
        return _WEEKDAY_WORDS[m.group(1).lower()]
    return None


def should_alert(date_text: str = "", time_text: str = "", weekday: int | None = None) -> bool:
    """Return True if a slot should fire a Telegram alert.

    Fri/Sat/Sun and unknown weekdays always alert; only Mon–Thu is filtered
    down to Abend plus anything whose shift we couldn't determine.
    """
    combined = f"{date_text or ''} {time_text or ''}".strip()
    if weekday is None:
        weekday = parse_weekday(combined)

    if weekday is None or weekday >= 4:
        return True

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


def is_weekend_evening(*texts: str) -> bool:
    """Fri/Sat/Sun with an Abend or undeterminable shift — the slots this bot
    exists for. Explicit Mittag/Vormittag/Frühschoppen on those days is not it."""
    combined = " ".join(t for t in texts if t).strip()
    weekday = parse_weekday(combined)
    if weekday is None or weekday < 4:
        return False
    if is_abend(combined):
        return True
    return not (
        is_mittag(combined)
        or is_fruehschoppen(combined)
        or is_vormittag_or_daytime(combined)
    )
