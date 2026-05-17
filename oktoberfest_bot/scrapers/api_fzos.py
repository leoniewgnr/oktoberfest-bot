"""Festzelt-OS REST API scraper.

For tents that ship the Nuxt SPA frontend backed by a tenant-specific
`*.festzelt-os.com` (or `api.festzelt-os.com`) JSON API, we can skip Chromium
entirely and read the same data Cloudflare-cached, with structured shift +
area metadata that the HTML scraper can't easily reach.

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
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Tuple

import requests

from .base_scraper import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


_REQUEST_TIMEOUT = 20
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) oktoberfest-bot/1.0"
)
# Space out per-guestlist definition fetches to stay under Cloudflare's
# per-IP rate limit (Error 1015 kicks in around ~10 req/s in our testing).
# 300 ms gives ~3 req/s; well under the threshold.
_INTER_CALL_DELAY_S = 0.30
_RATE_LIMIT_BACKOFF_S = 60.0  # Cloudflare Error 1015 can hold for ~60 s

# Cloudflare rate-limits per source IP across all *.festzelt-os.com hosts.
# Serialize API scrapes for every tent so we never burst from this box.
# (This is process-wide; each call holds the lock for the full scrape, which
# takes ~0.5 s for Schützen up to ~4 s for Schottenhamel — but only on the
# first scrape; subsequent scrapes hit the area cache and finish in ~0.1 s.)
_API_RATE_LOCK = threading.Lock()

# Process-wide cache of area definitions per (api_host, guestlist_uid).
# Area definitions are stable for a published guestlist — operators set them
# once when creating the slot. By caching, we avoid re-fetching N definitions
# per cycle and stay clear of Cloudflare's rate limit. main.py primes this
# cache from persisted state on startup so a restart doesn't trigger a burst.
_areas_cache: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
_areas_cache_lock = threading.Lock()


def prime_areas_cache(api_host: str, areas_by_uid: Dict[str, Dict[str, Any]]) -> int:
    """Pre-populate the area cache for one tent from persisted state.

    `areas_by_uid` is the StateManager.get_available_areas() shape:
        {uid: {"date_text": str, "areas": [{value, text}, ...]}}
    Returns number of entries primed.
    """
    primed = 0
    with _areas_cache_lock:
        for uid, info in (areas_by_uid or {}).items():
            areas = (info or {}).get("areas") or []
            if not areas:
                continue
            _areas_cache[(api_host, uid)] = areas
            primed += 1
    return primed


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
            return ScrapeResult(success=False, error=str(e))

    def _scrape_locked(self) -> ScrapeResult:
        with _API_RATE_LOCK:
            return self._scrape()

    def _headers(self) -> Dict[str, str]:
        return {
            "x-festzelt-os-company": self.company_id,
            "user-agent": _USER_AGENT,
            "accept": "application/json",
            "accept-language": "de",
        }

    def _scrape(self) -> ScrapeResult:
        # 1) list of guestlists (date + shift)
        url = f"https://{self.api_host}/lp/guestlists"
        try:
            resp = self._get_with_retry(url)
        except requests.RequestException as e:
            return ScrapeResult(success=False, error=f"guestlists network error: {e}")
        if resp.status_code != 200:
            return ScrapeResult(
                success=False,
                error=f"guestlists returned {resp.status_code}: {resp.text[:200]}",
            )
        try:
            guestlists = resp.json().get("data") or []
        except ValueError as e:
            return ScrapeResult(success=False, error=f"guestlists JSON decode: {e}")

        available_dates: List[Dict] = []
        available_times: Dict[str, Dict[str, Any]] = {}
        available_areas: Dict[str, Dict[str, Any]] = {}

        # Two passes so cached vs fetched UIDs are handled separately and
        # the inter-call delay only applies to actual network fetches.
        fetched_count = 0
        for g in guestlists:
            uid = g.get("uid")
            if not uid:
                continue
            name = (g.get("name") or "").strip()
            shift_label = ((g.get("shift") or {}).get("label") or "").strip()

            available_dates.append({"value": uid, "text": name})

            # Treat shift as the "time" entry so the existing diff logic flags
            # a new (date, shift) pair as a new-time-slot event.
            shift_value = shift_label or "?"
            available_times[uid] = {
                "date_text": name,
                "times": [{"value": shift_value, "text": shift_label}],
            }

            # Check cache first — area definitions are stable per UID.
            cache_key = (self.api_host, uid)
            with _areas_cache_lock:
                cached = _areas_cache.get(cache_key)

            if cached is not None:
                available_areas[uid] = {"date_text": name, "areas": cached}
                continue

            # Cache miss → fetch with paced inter-call delay.
            if fetched_count > 0:
                time.sleep(_INTER_CALL_DELAY_S)
            try:
                areas = self._fetch_areas(uid)
                fetched_count += 1
            except Exception as e:
                logger.warning(
                    f"{self.tent_name}: definitions for {uid} failed - {e}"
                )
                areas = []

            if areas:
                available_areas[uid] = {"date_text": name, "areas": areas}
                with _areas_cache_lock:
                    _areas_cache[cache_key] = areas

        if fetched_count:
            logger.info(
                f"{self.tent_name}: fetched {fetched_count} new area definition(s); "
                f"{len(guestlists) - fetched_count} served from cache"
            )

        return ScrapeResult(
            success=True,
            dates_available=len(available_dates) > 0,
            available_dates=available_dates,
            available_times=available_times,
            available_areas=available_areas,
        )

    def _get_with_retry(self, url: str) -> "requests.Response":
        """GET that retries once on 429 after a longer sleep."""
        resp = requests.get(url, headers=self._headers(), timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 429:
            logger.warning(
                f"{self.tent_name}: rate-limited at {url}; sleeping "
                f"{_RATE_LIMIT_BACKOFF_S:.0f}s before single retry"
            )
            time.sleep(_RATE_LIMIT_BACKOFF_S)
            resp = requests.get(url, headers=self._headers(), timeout=_REQUEST_TIMEOUT)
        return resp

    def _fetch_areas(self, uid: str) -> List[Dict[str, str]]:
        url = f"https://{self.api_host}/lp/guestlists/{uid}/definitions"
        resp = self._get_with_retry(url)
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
