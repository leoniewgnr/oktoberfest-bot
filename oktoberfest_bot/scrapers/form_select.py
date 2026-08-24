"""Scraper for tent reservation pages using form select dropdowns"""

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .base_scraper import BROWSER_USER_AGENT, BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


# A page that rendered less text than this did not really load; calling that an
# empty state would report blindness as health.
_MIN_RENDERED_BODY_CHARS = 200

# The date select's id is pinned in tents.json, so all four browser tents break
# together if the platform renames it. One Chromium at a time across all of them
# — a burst of four is the most detectable and most expensive thing this bot does.
_launch_lock: Optional[asyncio.Lock] = None


def _get_launch_lock() -> "asyncio.Lock":
    global _launch_lock
    if _launch_lock is None:
        _launch_lock = asyncio.Lock()
    return _launch_lock

# Cloudflare challenge / WAF block pages. Seeing one means we were refused,
# which is a different failure from a parse error and must not be retried.
_REFUSAL_MARKERS = ("just a moment", "cf-mitigated", "you have been blocked")

# A browser launch is the most expensive and most detectable thing this bot
# does, so one refusal parks the whole host instead of us knocking again.
_BREAKER_COOLDOWN_S = 30 * 60
_breaker_open_until: Dict[str, float] = {}
_breaker_lock = threading.Lock()


def _refusal_reason(
    status: Optional[int],
    headers: Dict[str, str],
    body_text: str,
) -> Optional[str]:
    """Return a human-readable reason if this page is a challenge/WAF refusal
    rather than the tent's own page."""
    body = (body_text or "")[:4096].lower()
    marker = next((m for m in _REFUSAL_MARKERS if m in body), None)
    mitigated = (headers or {}).get("cf-mitigated")
    if status != 403 and marker is None and not mitigated:
        return None
    parts = [f"HTTP {status}"]
    if marker:
        parts.append(f"body marker '{marker}'")
    if mitigated:
        parts.append(f"cf-mitigated: {mitigated}")
    return "refused by edge (" + ", ".join(parts) + ")"



def _breaker_remaining(host: str) -> float:
    with _breaker_lock:
        until = _breaker_open_until.get(host)
        if until is None:
            return 0.0
        remaining = until - time.monotonic()
        if remaining > 0:
            return remaining
        del _breaker_open_until[host]
    logger.info(f"Circuit breaker closed for {host}; browser launches resumed")
    return 0.0


def _breaker_trip(host: str, reason: str) -> None:
    with _breaker_lock:
        _breaker_open_until[host] = time.monotonic() + _BREAKER_COOLDOWN_S
    logger.warning(
        f"Circuit breaker OPEN for {host} for "
        f"{_BREAKER_COOLDOWN_S / 60:.0f} min - {reason}"
    )


class FormSelectScraper(BaseScraper):
    """Scraper for tents using select dropdown detection"""

    async def _extract_select_handle(self, select_element: Any) -> List[Dict[str, str]]:
        """Extract available options from a <select> ElementHandle."""
        if not select_element:
            return []

        options = await select_element.query_selector_all('option')

        available_options: List[Dict[str, str]] = []
        for option in options:
            is_disabled = await option.get_attribute('disabled')
            value = await option.get_attribute('value')
            text = await option.inner_text()

            # Skip placeholders
            if not is_disabled and value and value != "":
                available_options.append({'value': value, 'text': (text or '').strip()})

        return available_options

    async def _extract_select(self, page: Any, selector: str) -> List[Dict[str, str]]:
        """Extract available options from a <select> via CSS selector."""
        select_element = await page.query_selector(selector)
        return await self._extract_select_handle(select_element)

    async def check_availability(self) -> ScrapeResult:
        """Check for available dates (and optionally times) on the reservation page."""
        host = urlsplit(self.url).netloc.lower()
        cooldown = _breaker_remaining(host)
        if cooldown:
            return self._fail(
                f"circuit breaker open for {host}: refused recently, no browser "
                f"launch for another {cooldown / 60:.1f} min",
                blocked=True,
            )

        logger.info(f"Checking availability for {self.tent_name}...")

        async with _get_launch_lock():
            async with async_playwright() as p:
                browser = None
                try:
                    browser = await self._launch(p)
                    result = await self._scrape(browser)
                except Exception as e:
                    logger.error(f"Error checking page: {e}")
                    return self._fail(str(e))
                finally:
                    try:
                        if browser:
                            await browser.close()
                    except Exception:
                        pass

        if result.blocked:
            _breaker_trip(host, f"{self.tent_name}: {result.error}")
        return result

    async def _launch(self, p: Any) -> Any:
        args = [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-blink-features=AutomationControlled',
        ]
        try:
            return await p.chromium.launch(headless=True, channel='chrome', args=args)
        except Exception:
            return await p.chromium.launch(headless=True, args=args)

    async def _scrape(self, browser: Any) -> ScrapeResult:
        page = await browser.new_page(
            user_agent=BROWSER_USER_AGENT,
            viewport={'width': 1365, 'height': 768},
            locale='de-DE',
        )
        page.set_default_timeout(30000)

        logger.info(f"Loading page: {self.url}")
        response = await page.goto(self.url, wait_until='domcontentloaded')
        status = response.status if response else None
        headers = response.headers if response else {}
        try:
            await page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            pass

        refused = _refusal_reason(status, headers, await self._body_text(page))
        if refused:
            return self._fail(refused, blocked=True, status_code=status)

        date_selector = self.config.get('selector', 'select.form-select')

        try:
            await page.wait_for_selector(date_selector, timeout=20000)
        except Exception:
            # "Nothing published yet" and "they renamed the select" look identical
            # from here, and calling the second one a success is how the bot goes
            # green while blind. The discriminator is other <select> elements: the
            # booking form renders them, an unpublished tent renders none.
            body = await self._body_text(page)
            refused = _refusal_reason(status, headers, body)
            if refused:
                return self._fail(refused, blocked=True, status_code=status)

            select_count = await self._count_selects(page)
            if select_count is None or select_count > 0:
                return self._fail(
                    f"date select {date_selector!r} not found but the page has "
                    f"{select_count} <select> element(s) — markup changed, "
                    f"the configured selector is stale",
                    status_code=status,
                )
            if len(body.strip()) < _MIN_RENDERED_BODY_CHARS:
                return self._fail(
                    f"page rendered only {len(body.strip())} chars of text and no "
                    f"<select> at all — not a trustworthy empty state",
                    status_code=status,
                )

            logger.info(f"{self.tent_name}: no booking form published yet")
            result = ScrapeResult(
                success=True,
                dates_available=False,
                available_dates=[],
                available_times={},
                slots=[],
                status_code=status,
                empty_state=True,
            )
            return result

        # Dates
        available_dates = await self._extract_select(page, date_selector)
        logger.info(f"Found {len(available_dates)} available date options")

        # The shift/Schicht is only revealed inside the booking wizard (after a
        # seat is picked on the seatplan), so it is never on the page we read.
        # The date list is all these tents expose; every date comes back
        # time-unknown and the orchestrator alerts weekend dates for a manual
        # check. We used to select each date hunting for a time dropdown, but on
        # the current Livewire sites that timed out ~30s per date and never found
        # one — pure waste that made the browser scrapes slow and heavy.
        slots = self._build_slots(available_dates, {})
        return ScrapeResult(
            success=True,
            dates_available=len(available_dates) > 0,
            available_dates=available_dates,
            available_times={},
            slots=slots,
            status_code=status,
        )

    def _build_slots(
        self,
        available_dates: List[Dict[str, str]],
        available_times: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Date x time cross product. state stays None: this page generation
        exposes no reservation-state field."""
        slots: List[Dict[str, Any]] = []
        for date in available_dates:
            date_value = str(date.get('value', ''))
            date_text = date.get('text') or date_value
            times = (available_times.get(date_value) or {}).get('times') or []
            if times:
                for t in times:
                    time_value = str(t.get('value', ''))
                    slots.append({
                        'key': f"{date_value}|{time_value}",
                        'date_value': date_value,
                        'time_value': time_value,
                        'date_text': date_text,
                        'time_text': t.get('text') or time_value,
                        'state': None,
                        'areas': [],
                    })
            else:
                slots.append({
                    'key': date_value,
                    'date_value': date_value,
                    'time_value': None,
                    'date_text': date_text,
                    'time_text': '',
                    'state': None,
                    'areas': [],
                })
        return slots
