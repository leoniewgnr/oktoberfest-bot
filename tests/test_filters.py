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


@pytest.mark.parametrize("weekday", WEEKEND)
@pytest.mark.parametrize("label", LABELS)
def test_weekend_always_alerts(weekday, label):
    assert should_alert("25.09.2026", label, weekday=weekday) is True
    assert should_alert(label, "", weekday=weekday) is True


@pytest.mark.parametrize("weekday", WEEKEND)
def test_weekend_alerts_even_with_suppressing_date_text(weekday):
    assert should_alert("1. Sonntag, 20.09.2026 - Mittag", "", weekday=weekday) is True


@pytest.mark.parametrize("weekday", WEEK)
@pytest.mark.parametrize("label", ["Mittag", "Mittagstisch 11:00 - 16:00", "Vormittag",
                                  "Frühschoppen", "lunch", "Einlass 12:00"])
def test_weekday_suppresses_daytime(weekday, label):
    assert should_alert("", label, weekday=weekday) is False


@pytest.mark.parametrize("weekday", WEEK)
@pytest.mark.parametrize("label", ["Abend", "Abendveranstaltung", "dinner", "Einlass 18:30",
                                  "", "Wiesn-Nacht", "???"])
def test_weekday_alerts_on_abend_and_undetermined(weekday, label):
    assert should_alert("", label, weekday=weekday) is True


def test_unknown_weekday_always_alerts():
    assert should_alert("", "Mittag", weekday=None) is True
    assert should_alert("Wiesn-Nacht", "Frühschoppen") is True
    assert should_alert() is True


def test_weekday_derived_from_text_when_not_passed():
    # 25.09.2026 is a Friday → weekend guarantee applies even to a Mittag label.
    assert should_alert("Freitag, 25.09.2026", "Mittag") is True
    # 23.09.2026 is a Wednesday → Mittag is suppressed.
    assert should_alert("Mittwoch, 23.09.2026", "Mittag") is False


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
