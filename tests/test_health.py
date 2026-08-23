import logging

import pytest
import requests

from oktoberfest_bot.health import (
    MAX_REALERT_INTERVAL_S,
    BlindnessMonitor,
    HealthReporter,
    escalating_interval,
)

DAY = 86400
HALF_HOUR = 1800


@pytest.fixture
def monitor():
    return BlindnessMonitor(blind_after_seconds=900, realert_interval_seconds=HALF_HOUR)


class FakeGet:
    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc:
            raise self.exc
        return object()


@pytest.fixture
def fake_get(monkeypatch):
    fake = FakeGet()
    monkeypatch.setattr(requests, "get", fake)
    return fake


def test_healthy_tent_is_not_blind(monitor):
    assert monitor.evaluate({"hacker": 120.0}, now=0.0) == []
    assert monitor.should_alert(0.0) is False


def test_never_succeeded_counts_as_blind(monitor):
    assert monitor.evaluate({"hacker": None}, now=0.0) == ["hacker"]
    assert monitor.should_alert(0.0) is True


def test_blind_tents_reported_sorted(monitor):
    states = {"paulaner": 5000.0, "hacker": 5000.0, "kufflers": 60.0}
    assert monitor.evaluate(states, now=0.0) == ["hacker", "paulaner"]


def test_alerts_for_30_blind_days_never_stop_but_back_off(monitor):
    """The 30-day-silence bug: a tent that stays blind must keep alerting.

    The interval escalates to a 6 h floor so a chronically dead target cannot
    train her to mute the one channel that must never be ignored.
    """
    alerts = []
    for tick in range(0, 30 * DAY, 60):
        now = float(tick)
        # blind since the first tick, exactly the 2026-07-24 scenario
        monitor.evaluate({"hacker": now + 3600}, now=now)
        if monitor.should_alert(now):
            alerts.append(now)
            monitor.note_alerted(now)

    gaps = [b - a for a, b in zip(alerts, alerts[1:])]
    assert gaps[0] == HALF_HOUR  # a fresh outage pages at the configured interval
    assert max(gaps) == MAX_REALERT_INTERVAL_S  # and never goes quieter than that
    # still shouting on day 30 — no latching to silence, ever
    assert alerts[-1] > 29 * DAY
    assert len(alerts) == pytest.approx(30 * DAY / MAX_REALERT_INTERVAL_S, abs=8)


def test_a_newly_blind_tent_is_never_delayed_by_a_chronic_one(monitor):
    """A permanently dead target must not silence a tent that just went blind."""
    monitor.evaluate({"dead": None}, now=0.0)
    monitor.note_alerted(0.0)
    assert monitor.should_alert(60.0) is False

    monitor.evaluate({"dead": None, "hacker": None}, now=120.0)
    assert monitor.due(120.0) == ["hacker"]
    assert monitor.should_alert(120.0) is True


def test_no_realert_before_interval_elapses(monitor):
    monitor.evaluate({"hacker": None}, now=0.0)
    assert monitor.should_alert(0.0) is True
    monitor.note_alerted(0.0)

    assert monitor.should_alert(HALF_HOUR - 1) is False
    assert monitor.should_alert(HALF_HOUR) is True


def test_still_alerts_while_any_tent_stays_blind(monitor):
    monitor.evaluate({"hacker": None, "paulaner": None}, now=0.0)
    monitor.note_alerted(0.0)

    # hacker recovers, paulaner does not — silence would be wrong
    assert monitor.evaluate({"hacker": 10.0, "paulaner": None}, now=100.0) == ["paulaner"]
    assert monitor.should_alert(HALF_HOUR + 100) is True


def test_recovery_resets_so_next_outage_alerts_immediately(monitor):
    monitor.evaluate({"hacker": None}, now=0.0)
    monitor.note_alerted(0.0)
    assert monitor.should_alert(60.0) is False

    assert monitor.evaluate({"hacker": 30.0}, now=120.0) == []
    assert monitor.should_alert(120.0) is False

    assert monitor.evaluate({"hacker": 5000.0}, now=180.0) == ["hacker"]
    assert monitor.should_alert(180.0) is True


def test_clear_resets_when_nothing_blind_remains(monitor):
    monitor.evaluate({"hacker": None, "paulaner": None}, now=0.0)
    monitor.note_alerted(0.0)

    monitor.clear("hacker")
    assert monitor.should_alert(60.0) is False  # paulaner still blind, interval not up

    monitor.clear("paulaner")
    assert monitor.should_alert(60.0) is False  # nothing blind at all
    monitor.evaluate({"paulaner": None}, now=61.0)
    assert monitor.should_alert(61.0) is True


def test_reporter_pings_endpoints(fake_get):
    reporter = HealthReporter("https://hc-ping.com/abc/")

    reporter.ping_success("7 tents ok")
    reporter.ping_failure("403 from schottenhamel-api")
    reporter.ping_log("heartbeat sent")

    urls = [url for url, _ in fake_get.calls]
    assert urls == [
        "https://hc-ping.com/abc",
        "https://hc-ping.com/abc/fail",
        "https://hc-ping.com/abc/log",
    ]
    assert fake_get.calls[1][1]["data"] == b"403 from schottenhamel-api"
    assert fake_get.calls[0][1]["timeout"] == 10


def test_reporter_never_raises(monkeypatch, caplog):
    monkeypatch.setattr(requests, "get", FakeGet(exc=requests.ConnectionError("boom")))
    reporter = HealthReporter("https://hc-ping.com/abc")

    with caplog.at_level(logging.WARNING):
        reporter.ping_success()
        reporter.ping_failure("blocked")
        reporter.ping_log("x")
    assert len(caplog.records) == 3


def test_disabled_reporter_is_noop_but_warns_loudly(fake_get, caplog):
    with caplog.at_level(logging.WARNING):
        reporter = HealthReporter("")
    assert len(caplog.records) == 1
    assert "healthchecks.io" in caplog.records[0].message

    reporter.ping_success("x")
    reporter.ping_failure("x")
    reporter.ping_log("x")
    assert fake_get.calls == []

    with caplog.at_level(logging.WARNING):
        HealthReporter(None)
    assert len(caplog.records) == 2


def test_escalating_interval_doubles_then_caps():
    assert escalating_interval(HALF_HOUR, 0) == 0.0  # never alerted -> alert now
    assert escalating_interval(HALF_HOUR, 1) == HALF_HOUR
    assert escalating_interval(HALF_HOUR, 2) == 2 * HALF_HOUR
    assert escalating_interval(HALF_HOUR, 99) == MAX_REALERT_INTERVAL_S


def test_ping_throttle_never_delays_a_status_change(monkeypatch):
    """Steady repeats are throttled, but success<->failure must go out at once:
    a delayed transition is a delayed alarm."""
    from oktoberfest_bot import health

    sent = []
    monkeypatch.setattr(
        health.requests, "get", lambda url, **kw: sent.append(url) or None
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr(health.time, "monotonic", lambda: clock["t"])

    reporter = health.HealthReporter("https://hc-ping.com/uuid")

    reporter.ping_success()
    assert len(sent) == 1

    # Unchanged status inside the window is dropped.
    clock["t"] += 60
    reporter.ping_success()
    assert len(sent) == 1

    # A change is sent immediately, however recent the last ping.
    reporter.ping_failure("blind")
    assert len(sent) == 2 and sent[-1].endswith("/fail")

    # And back again, still inside the window.
    reporter.ping_success()
    assert len(sent) == 3 and not sent[-1].endswith("/fail")

    # Steady state resumes after the interval elapses.
    clock["t"] += health._PING_MIN_INTERVAL_S + 1
    reporter.ping_success()
    assert len(sent) == 4


def test_ping_disabled_without_url(monkeypatch):
    from oktoberfest_bot import health

    sent = []
    monkeypatch.setattr(health.requests, "get", lambda url, **kw: sent.append(url))
    health.HealthReporter("").ping_failure("blind")
    assert sent == []
