"""Festzelt-OS Livewire scraper — reads date AND shift without a browser.

The "new-gen" tents (Hacker, Paulaner, Ochsenbraterei, zur-Bratwurst) render a
Filament/Livewire form. The date lives in a native <select>, but the shift
(Schicht: Mittag/Abend/Frühschoppen) only appears after the date is chosen — the
page fires a POST /livewire/update and re-renders the shift <select> from the
response. There is no JSON API for these tents, but that Livewire call is exactly
what a browser makes when you pick a date, so we replay it with plain requests:

    GET  /<reservation path>      → csrf token + wire:snapshot + date <options>
    POST /livewire/update         → {snapshot, updates:{...date: <date>}}
                                  ← effects.html containing the shift <select>

This reads the same data the reservation page shows a human — it does NOT start a
booking (no seat selection, no "Weiter"). To stay light we only resolve the shift
for the dates that matter (Fri/Sat/Sun); weekday dates are emitted date-only.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import threading
import time
from datetime import date as _date
from typing import Any, Dict, List, Optional

import requests

from .base_scraper import HONEST_USER_AGENT, BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 25
_INTER_CALL_DELAY_S = 0.5  # pace the per-date shift lookups; politeness over speed

_DATE_FIELD = "data.createBookingStepOneForm.date"
_SHIFT_FIELD = "data.createBookingStepOneForm.booking_list_id"

_REFUSAL_MARKERS = ("just a moment", "cf-mitigated", "you have been blocked")

_BREAKER_COOLDOWN_S = 30 * 60
_breaker_open_until: Dict[str, float] = {}
_breaker_lock = threading.Lock()

# One Livewire host at a time across all tents, mirroring the browser launch lock:
# a burst of concurrent form sessions from one IP is the most abuse-like thing here.
_HOST_LOCK = threading.Lock()

_CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
_SNAPSHOT_RE = re.compile(r'wire:snapshot="([^"]+)"')
_OPTION_RE = re.compile(r'<option\s+value="([^"]*)"[^>]*>(.*?)</option>', re.S)


def _breaker_remaining(host: str) -> float:
    with _breaker_lock:
        until = _breaker_open_until.get(host)
        if until is None:
            return 0.0
        remaining = until - time.monotonic()
        if remaining > 0:
            return remaining
        del _breaker_open_until[host]
    logger.info(f"Circuit breaker closed for {host}; resuming Livewire requests")
    return 0.0


def _breaker_trip(host: str, reason: str) -> None:
    with _breaker_lock:
        _breaker_open_until[host] = time.monotonic() + _BREAKER_COOLDOWN_S
    logger.warning(
        f"Circuit breaker OPEN for {host} for {_BREAKER_COOLDOWN_S / 60:.0f} min - {reason}"
    )


def _is_refused(resp: "requests.Response") -> Optional[str]:
    if resp.status_code == 403:
        return f"HTTP 403 (cf-mitigated: {resp.headers.get('cf-mitigated', '?')})"
    head = (resp.text or "")[:2000].lower()
    marker = next((m for m in _REFUSAL_MARKERS if m in head), None)
    return f"body marker {marker!r}" if marker else None


def _select_block(text: str, field_id: str) -> Optional[str]:
    """Return the <select ... id=field_id ...>…</select> block, or None."""
    m = re.search(r'id="' + re.escape(field_id) + r'".*?</select>', text, re.S)
    return m.group(0) if m else None


def _clean_label(raw: str) -> str:
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    raw = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(raw).strip()


def _options(block: str) -> List[Dict[str, str]]:
    """Real <option>s (value + label) from a <select> block, placeholders dropped."""
    out: List[Dict[str, str]] = []
    for value, raw in _OPTION_RE.findall(block):
        if not value:
            continue
        label = _clean_label(raw)
        if label:
            out.append({"value": value, "text": label})
    return out


def _is_weekend(date_value: str) -> bool:
    """date_value is YYYY-MM-DD. Fri/Sat/Sun (or unparseable → treat as worth checking)."""
    try:
        return _date.fromisoformat(date_value).weekday() >= 4
    except ValueError:
        return True


class LivewireFzosScraper(BaseScraper):
    """Replay the Festzelt-OS Livewire form to read dates and their shifts."""

    def __init__(self, tent_config: Dict[str, Any]):
        super().__init__(tent_config)
        self.host = requests.utils.urlparse(self.url).netloc

    async def check_availability(self) -> ScrapeResult:
        logger.info(f"Checking availability for {self.tent_name} (Livewire)...")
        try:
            return await asyncio.to_thread(self._scrape_locked)
        except Exception as e:
            logger.error(f"{self.tent_name}: Livewire scrape failed - {e}")
            return ScrapeResult(success=False, error=str(e))

    def _scrape_locked(self) -> ScrapeResult:
        remaining = _breaker_remaining(self.host)
        if remaining > 0:
            return ScrapeResult(
                success=False, blocked=True,
                error=f"circuit breaker open for {self.host} ({remaining:.0f}s left)",
            )
        with _HOST_LOCK:
            return self._scrape()

    def _fail(self, error, blocked=False, status_code=None) -> ScrapeResult:
        return ScrapeResult(success=False, error=error, blocked=blocked, status_code=status_code)

    def _scrape(self) -> ScrapeResult:
        session = requests.Session()
        session.headers["user-agent"] = HONEST_USER_AGENT

        try:
            r = session.get(self.url, timeout=_REQUEST_TIMEOUT)
        except requests.RequestException as e:
            return self._fail(f"page GET network error: {e}")
        refused = _is_refused(r)
        if refused:
            _breaker_trip(self.host, refused)
            return self._fail(f"refused by edge ({refused})", blocked=True, status_code=r.status_code)
        if r.status_code != 200:
            return self._fail(f"page GET returned {r.status_code}", status_code=r.status_code)

        page = r.text
        token_m = _CSRF_RE.search(page)
        snapshot = next(
            (html.unescape(s) for s in _SNAPSHOT_RE.findall(page)
             if "createBookingStepOneForm" in html.unescape(s)),
            None,
        )
        date_block = _select_block(page, _DATE_FIELD)

        if not date_block:
            # No date select. Distinguish a real empty state from a broken parse.
            if token_m and snapshot:
                logger.info(f"{self.tent_name}: no dates published yet")
                return ScrapeResult(success=True, dates_available=False,
                                    available_dates=[], slots=[], empty_state=True)
            return self._fail("no date <select> and no Livewire snapshot — page structure changed")
        if not (token_m and snapshot):
            return self._fail("date select present but csrf token / snapshot missing")

        token = token_m.group(1)
        dates = _options(date_block)
        logger.info(f"Found {len(dates)} available date options")

        slots: List[Dict[str, Any]] = []
        shift_lookups = 0
        for d in dates:
            date_value = d["value"]
            date_text = d["text"]
            # Only resolve the shift for the days she cares about (Fri/Sat/Sun).
            # Weekday dates ride along date-only; the orchestrator won't alert them.
            if not _is_weekend(date_value):
                slots.append(self._slot(date_value, date_text, None, ""))
                continue

            if shift_lookups:
                time.sleep(_INTER_CALL_DELAY_S)
            shift_lookups += 1
            try:
                shifts = self._fetch_shifts(session, token, snapshot, date_value)
            except _Refused as e:
                _breaker_trip(self.host, str(e))
                return self._fail(f"refused mid-scrape ({e})", blocked=True)
            except Exception as e:
                logger.warning(f"{self.tent_name}: shift lookup for {date_value} failed - {e}")
                # Fall back to date-only so a weekend date is never dropped.
                slots.append(self._slot(date_value, date_text, None, ""))
                continue

            if not shifts:
                # Weekend date with no bookable shift → not a real opening; skip it
                # rather than firing a phantom alert.
                continue
            for sh in shifts:
                slots.append(self._slot(date_value, date_text, sh["value"], sh["text"]))

        if shift_lookups:
            logger.info(f"{self.tent_name}: resolved shifts for {shift_lookups} weekend date(s)")

        return ScrapeResult(
            success=True,
            dates_available=len(slots) > 0,
            available_dates=dates,
            slots=slots,
        )

    def _slot(self, date_value, date_text, time_value, time_text) -> Dict[str, Any]:
        key = f"{date_value}|{time_value}" if time_value else date_value
        return {
            "key": key,
            "date_value": date_value,
            "time_value": time_value,
            "date_text": date_text,
            "time_text": time_text,
            "state": None,
            "areas": [],
        }

    def _fetch_shifts(self, session, token, snapshot, date_value) -> List[Dict[str, str]]:
        payload = {
            "_token": token,
            "components": [{
                "snapshot": snapshot,
                "updates": {_DATE_FIELD: date_value},
                "calls": [],
            }],
        }
        resp = session.post(
            f"https://{self.host}/livewire/update",
            json=payload,
            timeout=_REQUEST_TIMEOUT,
            headers={
                "x-csrf-token": token,
                "referer": self.url,
                "origin": f"https://{self.host}",
                "content-type": "application/json",
            },
        )
        refused = _is_refused(resp)
        if refused:
            raise _Refused(refused)
        if resp.status_code != 200:
            raise RuntimeError(f"/livewire/update returned {resp.status_code}")
        frag = resp.json()["components"][0]["effects"].get("html", "")
        block = _select_block(frag, _SHIFT_FIELD)
        return _options(block) if block else []


class _Refused(Exception):
    pass
