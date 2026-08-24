"""End-to-end wiring: a scraped slot must actually produce a Telegram message.

Everything else in this suite tests a module in isolation. The miss paths that
mattered all lived in the seams — a slot that diffs correctly but whose alert is
never sent, or is sent and then forgotten. So these drive the real orchestrator
against a real StateManager with only the scraper and the transport stubbed.
"""

import asyncio
import logging

import pytest
import requests

from oktoberfest_bot import main
from oktoberfest_bot.notifiers.base_notifier import BaseNotifier
from oktoberfest_bot.scrapers.base_scraper import ScrapeResult
from oktoberfest_bot.state_manager import StateManager

SATURDAY_EVENING = {
    'key': '2026-09-26|abend',
    'date_value': '2026-09-26',
    'time_value': 'abend',
    'date_text': 'Samstag, 26.09.2026',
    'time_text': 'Abend',
    'state': 'requested',
    'areas': [],
}
MONDAY_LUNCH = {
    'key': '2026-09-21|mittag',
    'date_value': '2026-09-21',
    'time_value': 'mittag',
    'date_text': 'Montag, 21.09.2026',
    'time_text': 'Mittagstisch',
    'state': 'requested',
    'areas': [],
}

TENT = {
    'id': 'hacker-festzelt',
    'name': 'Hacker-Festzelt',
    'url': 'https://reservierung.derhimmelderbayern.de/reservierung',
    'scraper_type': 'form_select',
    'check_interval': 600,
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any HTTP call from a test is a bug in the test."""
    def boom(*args, **kwargs):
        raise AssertionError("test made a network request")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests, "post", boom)


class RecordingNotifier(BaseNotifier):
    """Records every rendered message; `deliver=False` simulates Telegram
    dropping it (a 5xx window, a revoked token, a malformed entity)."""

    def __init__(self, deliver: bool = True):
        self.deliver = deliver
        self.messages = []

    def send_notification(self, message):
        self.messages.append(message)
        return 1 if self.deliver else None

    def texts(self):
        return "\n---\n".join(self.messages)


class FakeScraper:
    def __init__(self, result):
        self.result = result

    async def check_availability(self):
        return self.result


def _result(slots):
    result = ScrapeResult(
        success=True,
        dates_available=bool(slots),
        available_dates=[{'value': s['date_value'], 'text': s['date_text']} for s in slots],
        slots=list(slots),
    )
    result.times_incomplete = False
    return result


@pytest.fixture
def rt(tmp_path):
    def build(notifier):
        return main.Runtime(
            state=StateManager(str(tmp_path / "state.json")),
            notifier=notifier,
            health=main.HealthReporter("", logging.getLogger("test")),
            logger=logging.getLogger("test"),
            config={
                'blind_alert_after_seconds': 900,
                'blind_realert_interval_seconds': 1800,
                'max_slot_age_for_display_seconds': 3600,
                'heartbeat_interval_seconds': 3600,
            },
        )

    return build


def run_cycle(monkeypatch, runtime, slots):
    monkeypatch.setattr(main, "create_scraper", lambda cfg: FakeScraper(_result(slots)))
    asyncio.run(main.check_tent(runtime, TENT))


def test_saturday_evening_slot_produces_an_alert(monkeypatch, rt):
    notifier = RecordingNotifier()
    runtime = rt(notifier)

    run_cycle(monkeypatch, runtime, [SATURDAY_EVENING])

    assert len(notifier.messages) == 1
    assert "WEEKEND EVENING" in notifier.messages[0]
    assert "26.09.2026" in notifier.messages[0]

    # ...and it is not repeated once it has been seen
    run_cycle(monkeypatch, runtime, [SATURDAY_EVENING])
    assert len(notifier.messages) == 1


def test_disappearing_then_reappearing_slot_alerts_as_returned(monkeypatch, rt):
    notifier = RecordingNotifier()
    runtime = rt(notifier)

    run_cycle(monkeypatch, runtime, [SATURDAY_EVENING])
    run_cycle(monkeypatch, runtime, [])          # slot booked / withdrawn
    notifier.messages.clear()
    run_cycle(monkeypatch, runtime, [SATURDAY_EVENING])  # Storno

    assert len(notifier.messages) == 1
    assert "FREED UP" in notifier.messages[0]
    assert "Storno" in notifier.messages[0]


def test_dropped_telegram_message_re_alerts_next_cycle(monkeypatch, rt):
    """The worst bug: a slot committed as seen although nobody was told."""
    dropped = RecordingNotifier(deliver=False)
    runtime = rt(dropped)

    run_cycle(monkeypatch, runtime, [SATURDAY_EVENING])
    assert len(dropped.messages) == 1

    runtime = runtime._replace(notifier=RecordingNotifier(deliver=True))
    run_cycle(monkeypatch, runtime, [SATURDAY_EVENING])
    assert "WEEKEND EVENING" in runtime.notifier.texts()


def test_new_bereich_in_a_known_saturday_slot_alerts(monkeypatch, rt):
    notifier = RecordingNotifier()
    runtime = rt(notifier)

    run_cycle(monkeypatch, runtime, [dict(SATURDAY_EVENING, areas=['Box 1'])])
    notifier.messages.clear()

    run_cycle(monkeypatch, runtime, [dict(SATURDAY_EVENING, areas=['Box 1', 'Box 9'])])
    assert len(notifier.messages) == 1
    assert "Box 9" in notifier.messages[0]
    assert "Box 1" not in notifier.messages[0]


def test_monday_lunch_is_filtered_out(monkeypatch, rt):
    notifier = RecordingNotifier()
    runtime = rt(notifier)

    run_cycle(monkeypatch, runtime, [MONDAY_LUNCH])
    assert notifier.messages == []


def test_saturday_evening_is_alerted_before_the_weekday_batch(monkeypatch, rt):
    notifier = RecordingNotifier()
    runtime = rt(notifier)

    run_cycle(
        monkeypatch,
        runtime,
        [MONDAY_LUNCH, dict(SATURDAY_EVENING, time_text='Abendreservierung')],
    )
    assert "WEEKEND EVENING" in notifier.messages[0]


def test_state_flip_on_a_saturday_slot_alerts(monkeypatch, rt):
    notifier = RecordingNotifier()
    runtime = rt(notifier)

    run_cycle(monkeypatch, runtime, [SATURDAY_EVENING])
    notifier.messages.clear()

    run_cycle(monkeypatch, runtime, [dict(SATURDAY_EVENING, state='confirmed')])
    assert len(notifier.messages) == 1
    assert "BOOKING STATE CHANGED" in notifier.messages[0]


def test_weekend_shift_unknown_is_not_labelled_evening():
    """Regression: a browser tent weekend slot with an unreadable shift used to
    get the WEEKEND EVENING banner and a repeated ISO-date artifact. It must now
    say the shift is unknown, and never falsely claim EVENING."""
    from oktoberfest_bot.notifiers.base_notifier import BaseNotifier

    class Rec(BaseNotifier):
        def __init__(self): self.msgs = []
        def send_notification(self, m): self.msgs.append(m); return 1

    n = Rec()
    n.send_new_dates_added(
        "Hacker-Festzelt", "https://reservierung.derhimmelderbayern.de/reservierung",
        [{"value": "2026-10-04", "text": "Sonntag, 04.10.2026"}],
    )
    body = n.msgs[0]
    assert "SHIFT UNKNOWN" in body
    assert "WEEKEND EVENING" not in body      # must not overclaim
    assert "2026-10-04" not in body           # ISO-date artifact gone
    assert "Sonntag, 04.10.2026" in body      # the date itself still shows

    # A real, read Abend still earns the evening banner and keeps a useful uid.
    n2 = Rec()
    n2.send_dates_available(
        "Schottenhamel", "https://x/",
        [{"value": "ASAGWCE", "text": "Samstag, 26.09.2026 – Abend"}],
    )
    assert "WEEKEND EVENING" in n2.msgs[0]
    assert "ASAGWCE" in n2.msgs[0]
