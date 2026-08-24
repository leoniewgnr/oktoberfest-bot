import pytest

from oktoberfest_bot.filters import (
    is_abend,
    is_fruehschoppen,
    is_mittag,
    is_vormittag_or_daytime,
    parse_weekday,
    should_alert,
)

WEEKEND = [4, 5, 6]
WEEK = [0, 1, 2, 3]

# Labels that Mon–Thu would suppress, plus junk and a red-herring daytime time.
LABELS = [
    "Mittag",
    "Mittagstisch 11:00 - 16:00",
    "Vormittag",
    "Frühschoppen",
    "Fruehschoppen",
    "Abend",
    "",
    "Wiesn-Nacht",
    "Einlass 12:00",
    "lunch",
    "???",
]


EVENING = ["Abend", "Abendveranstaltung", "dinner", "Einlass 18:30",
           "Samstagabend", "15:30 - 22:30"]
DAYTIME = ["Mittag", "Mittagstisch 11:00 - 16:00", "Vormittag", "Nachmittag",
           "Frühschoppen", "Fruehschoppen", "lunch", "Einlass 12:00", "10:00 - 15:00"]


@pytest.mark.parametrize("weekday", WEEKEND + WEEK)
@pytest.mark.parametrize("label", EVENING)
def test_evening_always_alerts_any_day(weekday, label):
    # Abend is what she wants — on a weekend or a weekday.
    assert should_alert("25.09.2026", label, weekday=weekday) is True


@pytest.mark.parametrize("weekday", WEEKEND + WEEK)
@pytest.mark.parametrize("label", DAYTIME)
def test_identified_non_evening_never_alerts(weekday, label):
    # A shift we can read as non-evening is suppressed even on Fri/Sat/Sun —
    # this is the change: no more weekend Mittag/Frühschoppen/Nachmittag noise.
    assert should_alert("25.09.2026", label, weekday=weekday) is False


@pytest.mark.parametrize("weekday", WEEKEND)
def test_weekend_unknown_shift_still_alerts(weekday):
    # Shift unreadable on a weekend -> alert defensively (might be the evening).
    assert should_alert("25.09.2026", "", weekday=weekday) is True
    assert should_alert("Wiesn-Nacht", "", weekday=weekday) is True
    assert should_alert("???", "", weekday=weekday) is True


@pytest.mark.parametrize("weekday", WEEK)
def test_weekday_unknown_shift_stays_quiet(weekday):
    # Mon-Thu with no readable shift is not her priority.
    assert should_alert("", "", weekday=weekday) is False
    assert should_alert("Wiesn-Nacht", "", weekday=weekday) is False


def test_unparseable_date_alerts_unless_identified_daytime():
    assert should_alert("", "", weekday=None) is True          # nothing known -> defensive
    assert should_alert("Wiesn-Nacht", "") is True             # unparseable, unknown shift
    assert should_alert("", "Mittag", weekday=None) is False   # identified Mittag -> suppress
    assert should_alert("", "Abend", weekday=None) is True     # identified Abend -> alert
    assert should_alert() is True


def test_weekday_derived_from_text_when_not_passed():
    # 25.09.2026 is a Friday, but Mittag is now suppressed everywhere.
    assert should_alert("Freitag, 25.09.2026", "Mittag") is False
    assert should_alert("Freitag, 25.09.2026", "Abend") is True
    # 23.09.2026 is a Wednesday → Mittag suppressed, Abend alerts.
    assert should_alert("Mittwoch, 23.09.2026", "Mittag") is False
    assert should_alert("Mittwoch, 23.09.2026", "Abend") is True


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1. Sonntag, 20.09.2026 - Mittag", 6),
        ("Freitag, 25.09.2026", 4),
        ("25.09.2026", 4),
        ("Mittwoch, 23.09.2026", 2),
        ("19.09.2026", 5),
        ("Sonnabend", 5),
        ("Donnerstag", 3),
        ("Abend", None),
        ("", None),
        ("31.02.2026", None),
        ("2. Samstag", 5),
    ],
)
def test_parse_weekday(text, expected):
    assert parse_weekday(text) == expected


def test_date_beats_weekday_word():
    # Word and date disagree; the date (a Friday) must win.
    assert parse_weekday("Montag, 25.09.2026") == 4


def test_shift_helpers_still_exported():
    assert is_abend("Abendveranstaltung") is True
    assert is_abend("Einlass 18:30") is True
    assert is_mittag("Mittagstisch") is True
    assert is_mittag("Vormittag") is False
    assert is_fruehschoppen("Frühschoppen") is True
    assert is_vormittag_or_daytime("Einlass 12:00") is True
    assert is_vormittag_or_daytime("Einlass 18:30") is False


def test_weekend_class_honest_labels():
    from oktoberfest_bot.filters import weekend_class
    # the jackpot only when we actually read Abend
    assert weekend_class("Samstag, 26.09.2026", "Abend") == "evening"
    assert weekend_class("Samstag, 26.09.2026 – Abend") == "evening"
    # a read non-evening weekend shift is flagged but not "evening"
    assert weekend_class("Sonntag, 04.10.2026", "Frühschoppen") == "daytime"
    assert weekend_class("Samstag, 26.09.2026", "Mittag") == "daytime"
    # unreadable shift on a weekend must be honest, not overclaimed as evening
    assert weekend_class("Sonntag, 04.10.2026", "") == "unknown"
    assert weekend_class("Samstag, 26.09.2026") == "unknown"
    # weekdays are never a weekend class
    assert weekend_class("Montag, 21.09.2026", "Abend") is None
    assert weekend_class("Freitag, 25.09.2026", "Abend") == "evening"
