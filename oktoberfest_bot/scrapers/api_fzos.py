"""Festzelt-OS REST API scraper.

For tents that ship the Nuxt SPA frontend backed by a tenant-specific
`*.festzelt-os.com` (or `api.festzelt-os.com`) JSON API, we can skip Chromium
entirely and read the same data, with structured shift + area metadata that the
HTML scraper can't easily reach.

Tent config (in tents.json) for this scraper type:

    {
      "id": "schuetzenfestzelt",
      "name": "Schützenfestzelt",
      "url": "https://reservierung.schuetzenfestzelt.com/reservation",
      "scraper_type": "api_fzos",
      "api_host": "schuetzen-api.festzelt-os.com",
      "company_id": "M5RN1H1",
      "check_interval": 120,
      "enabled": true
    }

Endpoints used (both public, anonymous):
- GET /lp/guestlists                         → list of {uid, name, shift, date, ...}
- GET /lp/guestlists/{uid}/definitions       → {areas: [{id, label, start, end}], ...}

Maps onto the existing ScrapeResult shape so downstream diff/notify logic
doesn't need to special-case API tents:
- one guestlist  → one entry in `available_dates` (value=uid, text=name)
- guestlist.shift → one entry in `available_times[uid].times`
- guestlist.areas → entries in `available_areas[uid].areas`
- one guestlist  → one entry in `slots` (a guestlist IS one date+shift here)

The host sends neither ETag nor Last-Modified and marks everything no-cache, so
`result.body_hash` (sha256 of the raw guestlists body) is the only way for the
orchestrator to tell "genuinely unchanged" from "we saw nothing new".
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from .. import filters
from .base_scraper import BaseScraper, HONEST_USER_AGENT, ScrapeResult

logger = logging.getLogger(__name__)


_REQUEST_TIMEOUT = 20
# Space out per-guestlist definition fetches to stay well under Cloudflare's
# per-IP rate limit (Error 1015 kicks in around ~10 req/s in our testing).
# 500 ms gives ~2 req/s; politeness beats speed, the area data is secondary.
_INTER_CALL_DELAY_S = 0.50
_RATE_LIMIT_BACKOFF_S = 60.0  # Cloudflare Error 1015 can hold for ~60 s

# Cloudflare rate-limits per source IP across all *.festzelt-os.com hosts.
# Serialize API scrapes for every tent so we never burst from this box.
_API_RATE_LOCK = threading.Lock()

# Substrings that mean "the edge refused us", not "the API answered".
_REFUSAL_MARKERS = ("just a moment", "cf-mitigated", "you have been blocked")

# Once refused, stop touching that host for a while — repeatedly hammering a
# challenged host is what turned a transient block into a 30-day outage.
_BREAKER_COOLDOWN_S = 30 * 60
_breaker_open_until: Dict[str, float] = {}
_breaker_lock = threading.Lock()


def _definitions_key(api_host: str) -> str:
    """Definitions is the endpoint Cloudflare rate-limits first. Its breaker is
    kept separate so losing the secondary area feed never blinds the guestlists."""
    return api_host + "#definitions"

# Process-wide cache of area definitions per (api_host, guestlist_uid).
# Area definitions are stable for a published guestlist — operators set them
# once when creating the slot. By caching, we avoid re-fetching N definitions
# per cycle and stay clear of Cloudflare's rate limit. main.py primes this
# cache from persisted state on startup so a restart doesn't trigger a burst.
# Value: (areas, fetched_at_monotonic).
_areas_cache: Dict[Tuple[str, str], Tuple[List[Dict[str, str]], float]] = {}
_areas_cache_lock = threading.Lock()

# Areas are re-read only for Fri/Sat/Sun evening guestlists: a Bereich freeing up
# inside one of those IS the Storno we exist to catch. Everything else keeps the
# cached set forever, which is what keeps the request rate low.
_AREAS_REFRESH_TTL_S = 15 * 60


class _StopDefinitionsError(Exception):
    """Sentinel: the host rate-limited or refused a definitions call.
    Caller stops fetching further definitions for this cycle."""
    pass


def prime_areas_cache(api_host: str, areas_by_uid: Dict[str, Dict[str, Any]]) -> int:
    """Pre-populate the area cache for one tent from persisted state.

    `areas_by_uid` is the StateManager.get_available_areas() shape:
        {uid: {"date_text": str, "areas": [{value, text}, ...]}}
    Returns number of entries primed.
    """
    primed = 0
    now = time.monotonic()
    with _areas_cache_lock:
        for uid, info in (areas_by_uid or {}).items():
            areas = (info or {}).get("areas") or []
            if not areas:
                continue
            _areas_cache[(api_host, uid)] = (areas, now)
            primed += 1
    return primed


def _breaker_remaining(api_host: str) -> float:
    with _breaker_lock:
        until = _breaker_open_until.get(api_host)
        if until is None:
            return 0.0
        remaining = until - time.monotonic()
        if remaining > 0:
            return remaining
        del _breaker_open_until[api_host]
    logger.info(f"Circuit breaker closed for {api_host}; resuming requests")
    return 0.0


def _breaker_trip(api_host: str, reason: str, seconds: float = _BREAKER_COOLDOWN_S) -> None:
    with _breaker_lock:
        _breaker_open_until[api_host] = time.monotonic() + seconds
    logger.warning(
        f"Circuit breaker OPEN for {api_host} for {seconds / 60:.0f} min - {reason}"
    )


_WEEKDAY_ABBR = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def _label_with_date(raw_date: Any, name: str) -> str:
    """Put the guestlist's authoritative `date` in front of its free-text name.

    The name is operator-typed and has been seen to carry a *different* date than
    the record ("Mittagstisch ab 21.09.2026" on a Saturday list). Every weekday
    decision downstream reads the first dd.mm.yyyy it finds, so the real date has
    to come first or a Mon-Thu name can suppress a Saturday evening.
    """
    text = str(raw_date or "").strip()
    parsed = None
    for candidate in (text[:10], text[:19]):
        try:
            parsed = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if parsed is None:
        return name
    stamp = f"{_WEEKDAY_ABBR[parsed.weekday()]} {parsed:%d.%m.%Y}"
    return f"{stamp} – {name}" if name else stamp


def _refusal_reason(resp: "requests.Response") -> Optional[str]:
    """Return a human-readable reason if this response is a refusal (403 or a
    challenge/WAF body) rather than an API answer."""
    # Challenge pages are always HTML; only scan those, so a guestlist whose
    # name happens to contain a marker word can never trip the breaker.
    is_json = "json" in (resp.headers.get("content-type") or "").lower()
    body = "" if is_json else (resp.text or "")[:4096].lower()
    marker = next((m for m in _REFUSAL_MARKERS if m in body), None)
    if resp.status_code != 403 and marker is None:
        return None
    parts = [f"HTTP {resp.status_code}"]
    if marker:
        parts.append(f"body marker '{marker}'")
    mitigated = resp.headers.get("cf-mitigated")
    if mitigated:
        parts.append(f"cf-mitigated: {mitigated}")
    return "refused by edge (" + ", ".join(parts) + ")"


class ApiFzosScraper(BaseScraper):
    """Scrape the Festzelt-OS landing-page REST API directly."""

    def __init__(self, tent_config: Dict[str, Any]):
        super().__init__(tent_config)
        try:
            self.api_host = tent_config["api_host"]
            self.company_id = tent_config["company_id"]
        except KeyError as e:
            raise ValueError(
                f"Tent '{tent_config.get('id')}' missing required FZOS API "
                f"field: {e}. Need 'api_host' and 'company_id'."
            )

    async def check_availability(self) -> ScrapeResult:
        """Run the sync requests work in a thread so we don't block the event loop.
        All API scrapes are serialized via a process-wide lock to stay under
        Cloudflare's per-IP rate limit."""
        logger.info(f"Checking availability for {self.tent_name} (API)...")
        try:
            return await asyncio.to_thread(self._scrape_locked)
        except Exception as e:
            logger.error(f"{self.tent_name}: API scrape failed - {e}")
            return self._fail(str(e))

    def _fail(
        self,
        error: str,
        blocked: bool = False,
        status_code: Optional[int] = None,
    ) -> ScrapeResult:
        result = ScrapeResult(
            success=False, error=error, blocked=blocked, status_code=status_code
        )
        # No trustworthy body → the orchestrator must never read this as "unchanged".
        result.body_hash = None
        return result

    def _scrape_locked(self) -> ScrapeResult:
        cooldown = _breaker_remaining(self.api_host)
        if cooldown:
            return self._fail(
                f"circuit breaker open for {self.api_host} after a refusal; "
                f"not retrying for another {cooldown / 60:.1f} min",
                blocked=True,
            )
        with _API_RATE_LOCK:
            return self._scrape()

    def _headers(self) -> Dict[str, str]:
        return {
            "x-festzelt-os-company": self.company_id,
            "user-agent": HONEST_USER_AGENT,
            "accept": "application/json",
            "accept-language": "de",
        }

    def _scrape(self) -> ScrapeResult:
        # 1) list of guestlists (date + shift)
        url = f"https://{self.api_host}/lp/guestlists"
        try:
            resp = self._get_with_retry(url)
        except requests.RequestException as e:
            return self._fail(f"guestlists network error: {e}")

        refusal = _refusal_reason(resp)
        if refusal:
            _breaker_trip(self.api_host, f"guestlists {refusal}")
            return self._fail(
                f"guestlists {refusal}",
                blocked=True,
                status_code=resp.status_code,
            )
        if resp.status_code != 200:
            return self._fail(
                f"guestlists returned {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )

        body_hash = hashlib.sha256(resp.content).hexdigest()
        try:
            payload = resp.json()
        except ValueError as e:
            return self._fail(f"guestlists JSON decode: {e}")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            # An unexpected shape must never read as "the tent published nothing":
            # that is indistinguishable from health and is how blindness hides.
            return self._fail(
                "guestlists payload has no 'data' list — API shape changed?"
            )
        guestlists = payload["data"]

        # pagination.total is known-unreliable on this API (it has reported 6
        # while returning 8, and 0 while returning 2). len(data) is the truth.
        total = (payload.get("pagination") or {}).get("total")
        if isinstance(total, int) and total > 0 and total != len(guestlists):
            logger.info(
                f"{self.tent_name}: pagination.total={total} but data has "
                f"{len(guestlists)} guestlists; using len(data)"
            )

        available_dates: List[Dict] = []
        available_times: Dict[str, Dict[str, Any]] = {}
        available_areas: Dict[str, Dict[str, Any]] = {}
        slots: List[Dict[str, Any]] = []

        # Areas are a nice-to-have: if definitions are rate-limited or refused we
        # stop fetching for this cycle and still return the slots. Missing areas
        # must never fail a scrape. The cache fills in over subsequent cycles.
        fetched_count = 0
        skipped_count = 0
        definitions_stopped = False

        for g in guestlists:
            uid = g.get("uid")
            if not uid:
                continue
            name = _label_with_date(g.get("date"), (g.get("name") or "").strip())
            shift_label = ((g.get("shift") or {}).get("label") or "").strip()

            available_dates.append({"value": uid, "text": name})

            # Treat shift as the "time" entry so the existing diff logic flags
            # a new (date, shift) pair as a new-time-slot event.
            shift_value = shift_label or "?"
            available_times[uid] = {
                "date_text": name,
                "times": [{"value": shift_value, "text": shift_label}],
            }

            # Check cache first. Area definitions are stable per UID, except on
            # the weekend evenings we exist for — those get re-read on a TTL.
            cache_key = (self.api_host, uid)
            ttl = (
                _AREAS_REFRESH_TTL_S
                if filters.is_weekend_evening(name, shift_label)
                else None
            )
            with _areas_cache_lock:
                cached = _areas_cache.get(cache_key)
            areas = None
            if cached is not None:
                cached_areas, fetched_at = cached
                if ttl is None or time.monotonic() - fetched_at < ttl:
                    areas = cached_areas

            if areas is None:
                if not definitions_stopped and _breaker_remaining(
                    _definitions_key(self.api_host)
                ):
                    definitions_stopped = True
                if definitions_stopped:
                    skipped_count += 1
                    # Keep the stale set rather than reporting zero Bereiche.
                    areas = cached[0] if cached else []
                else:
                    if fetched_count > 0:
                        time.sleep(_INTER_CALL_DELAY_S)
                    try:
                        areas = self._fetch_areas(uid)
                        fetched_count += 1
                    except _StopDefinitionsError as e:
                        logger.warning(
                            f"{self.tent_name}: stopping area fetches this cycle - {e}"
                        )
                        definitions_stopped = True
                        skipped_count += 1
                        areas = []
                    except Exception as e:
                        logger.warning(
                            f"{self.tent_name}: definitions for {uid} failed - {e}"
                        )
                        areas = []
                    if areas:
                        with _areas_cache_lock:
                            _areas_cache[cache_key] = (areas, time.monotonic())
                    elif cached is not None:
                        # Refresh failed — keep what we had rather than reporting
                        # zero Bereiche, which would read as "everything gone".
                        areas = cached[0]

            if areas:
                available_areas[uid] = {"date_text": name, "areas": areas}

            slots.append({
                "key": uid,
                "date_value": uid,
                "time_value": shift_label or None,
                "date_text": name,
                "time_text": shift_label,
                "state": g.get("new_reservation_state"),
                "areas": [a.get("text") or a.get("value") for a in areas],
            })

        if fetched_count or skipped_count:
            cached_count = len(guestlists) - fetched_count - skipped_count
            logger.info(
                f"{self.tent_name}: areas → {fetched_count} fetched, "
                f"{cached_count} cached, {skipped_count} skipped"
            )

        result = ScrapeResult(
            success=True,
            dates_available=len(available_dates) > 0,
            available_dates=available_dates,
            available_times=available_times,
            available_areas=available_areas,
            slots=slots,
            status_code=resp.status_code,
            empty_state=not slots,
        )
        result.body_hash = body_hash
        return result

    def _get_with_retry(self, url: str) -> "requests.Response":
        """GET the guestlists list. A 429 is honoured by parking the host for the
        Retry-After window rather than sleeping here: this runs while holding the
        process-wide API lock, so waiting would stall the other API tents too."""
        resp = requests.get(url, headers=self._headers(), timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 429:
            delay = _retry_after_seconds(resp) or _RATE_LIMIT_BACKOFF_S
            _breaker_trip(
                self.api_host,
                "guestlists rate-limited (429), honouring Retry-After",
                seconds=delay,
            )
        return resp

    def _fetch_areas(self, uid: str) -> List[Dict[str, str]]:
        url = f"https://{self.api_host}/lp/guestlists/{uid}/definitions"
        resp = requests.get(url, headers=self._headers(), timeout=_REQUEST_TIMEOUT)
        refusal = _refusal_reason(resp)
        if refusal:
            _breaker_trip(_definitions_key(self.api_host), f"definitions {refusal}")
            raise _StopDefinitionsError(f"definitions for {uid} {refusal}")
        if resp.status_code == 429:
            _breaker_trip(
                _definitions_key(self.api_host),
                "definitions rate-limited (429)",
                seconds=_retry_after_seconds(resp) or _RATE_LIMIT_BACKOFF_S,
            )
            raise _StopDefinitionsError(
                f"definitions for {uid} returned 429 (rate-limited)"
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"definitions returned {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json().get("data") or {}
        areas_raw = data.get("areas") or []
        out: List[Dict[str, str]] = []
        for a in areas_raw:
            area_id = a.get("id")
            label = (a.get("label") or "").strip()
            if area_id is None or not label:
                continue
            out.append({"value": str(area_id), "text": label})
        return out


def _retry_after_seconds(resp: "requests.Response") -> Optional[float]:
    """Retry-After in seconds, if the host sent a sane numeric one."""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return min(max(seconds, 0.0), 300.0) or None
