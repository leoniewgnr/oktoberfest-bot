"""Dead-man's switch: an external watchdog plus an in-process blindness alarm.

The last outage lasted 30 days because the only alarm path was the same Telegram
call that could itself be broken, and because the per-tent error flag latched
True after one message and never cleared. So there are two independent alarms
here: HealthReporter pings an outside service (healthchecks.io) that shouts when
*we* go quiet, and BlindnessMonitor keeps re-alerting for as long as any tent is
blind — it can never latch to silence.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

from .scrapers.base_scraper import HONEST_USER_AGENT

logger = logging.getLogger(__name__)

_PING_TIMEOUT = 10
# healthchecks.io truncates the ping body anyway; keep the request small.
_MAX_BODY_CHARS = 2000

# The blindness loop evaluates every 60 s, but the external switch only needs a
# steady heartbeat — a status *change* is always sent immediately, so throttling
# the unchanged repeats costs no detection latency and drops 1440 pings/day to
# roughly 288. Keep the check's period comfortably above this.
_PING_MIN_INTERVAL_S = 300.0

# A target that has been dead for days must still be reported, but not at the
# fresh-outage cadence: a channel she has learned to mute is a missed slot.
MAX_REALERT_INTERVAL_S = 6 * 3600.0


def escalating_interval(
    base_seconds: float, alerts_sent: int, max_seconds: float = MAX_REALERT_INTERVAL_S
) -> float:
    """Re-alert interval that doubles per alert already sent, capped.

    A new outage pages immediately and keeps paging every base interval; a
    chronic one settles at the cap so it never goes silent and never floods.
    """
    if alerts_sent <= 0:
        return 0.0
    exponent = min(max(alerts_sent - 1, 0), 12)
    return min(base_seconds * (2 ** exponent), max_seconds)


class HealthReporter:
    """Pings an external dead-man's switch (healthchecks.io-style URL)."""

    def __init__(self, healthcheck_url: Optional[str], logger: logging.Logger = logger):
        self.url = (healthcheck_url or "").strip().rstrip("/") or None
        self.logger = logger
        self._last_path: Optional[str] = None
        self._last_sent_at = 0.0
        if self.url is None:
            self.logger.warning(
                "EXTERNAL DEAD-MAN'S SWITCH DISABLED (healthcheck_url is empty). "
                "Nothing outside this process will notice if the bot dies or gets "
                "blocked — that is exactly how the last outage stayed unnoticed for "
                "30 days. Create a check on healthchecks.io and set healthcheck_url."
            )

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def ping_success(self, extra: str = "") -> None:
        self._ping("", extra)

    def ping_failure(self, reason: str) -> None:
        """Trips the external alarm while the process is still alive — "running but
        every request refused" must page us too, not just "process dead"."""
        self._ping("/fail", reason)

    def ping_log(self, msg: str) -> None:
        self._ping("/log", msg)

    def _ping(self, path: str, body: str) -> None:
        if self.url is None:
            return
        # /log is a note, not a status, so it never affects the throttle.
        if path != "/log":
            now = time.monotonic()
            changed = path != self._last_path
            if not changed and now - self._last_sent_at < _PING_MIN_INTERVAL_S:
                return
            self._last_path = path
            self._last_sent_at = now
        url = self.url + path
        try:
            requests.get(
                url,
                # healthchecks.io stores the request body as the ping's log line.
                data=(body or "")[:_MAX_BODY_CHARS].encode("utf-8"),
                headers={"User-Agent": HONEST_USER_AGENT},
                timeout=_PING_TIMEOUT,
            )
        except Exception as e:
            self.logger.warning("Healthcheck ping to %s failed: %s", url, e)


class BlindnessMonitor:
    """Tracks which tents we have stopped getting successful reads from, and
    decides when to (re-)alert. Never goes quiet while something is blind."""

    def __init__(
        self,
        blind_after_seconds: float,
        realert_interval_seconds: float,
        max_realert_interval_seconds: float = MAX_REALERT_INTERVAL_S,
    ):
        self.blind_after_seconds = blind_after_seconds
        self.realert_interval_seconds = realert_interval_seconds
        self.max_realert_interval_seconds = max_realert_interval_seconds
        self._blind: set = set()
        # Per tent, so a chronically dead target can never delay or mask the
        # alert for a tent that just went blind.
        self._last_alert: Dict[str, float] = {}
        self._alert_count: Dict[str, int] = {}

    def evaluate(self, tent_states: Dict[str, Optional[float]], now: float) -> List[str]:
        """tent_states maps tent id -> seconds since its last success (None = never
        succeeded, which counts as blind: an unproven tent is not a healthy one)."""
        self._blind = {
            tent_id
            for tent_id, age in tent_states.items()
            if age is None or age >= self.blind_after_seconds
        }
        for tent_id in list(self._last_alert):
            if tent_id not in self._blind:
                self.clear(tent_id)
        return sorted(self._blind)

    def due(self, now: float) -> List[str]:
        """Blind tents whose own re-alert interval has elapsed."""
        due = []
        for tent_id in sorted(self._blind):
            last = self._last_alert.get(tent_id)
            if last is None:
                due.append(tent_id)
                continue
            interval = escalating_interval(
                self.realert_interval_seconds,
                self._alert_count.get(tent_id, 0),
                self.max_realert_interval_seconds,
            )
            if now - last >= interval:
                due.append(tent_id)
        return due

    def should_alert(self, now: float) -> bool:
        return bool(self.due(now))

    def note_alerted(self, now: float) -> None:
        """Every blind tent is named in the alert, so every blind tent is marked."""
        for tent_id in self._blind:
            self._last_alert[tent_id] = now
            self._alert_count[tent_id] = self._alert_count.get(tent_id, 0) + 1

    def clear(self, tent_id: str) -> None:
        self._blind.discard(tent_id)
        self._last_alert.pop(tent_id, None)
        self._alert_count.pop(tent_id, None)
