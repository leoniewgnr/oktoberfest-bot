"""Scraper for tent reservation pages using form select dropdowns"""

import asyncio
import logging
import random
import re
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


def _looks_like_date(text: str) -> bool:
    # e.g. "Freitag, 25.09.2026" or "25.09.2026"
    return bool(re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", text))


def _looks_like_time(text: str) -> bool:
    t = (text or '').strip().lower()
    if not t:
        return False
    if _looks_like_date(t):
        return False
    # Reject pure numbers (e.g. Tischgröße "8", "10") — those are
    # never time labels even though they're short.
    if t.isdigit():
        return False
    # Strong positive signals
    if ':' in t or 'uhr' in t:
        return True
    if any(word in t for word in [
        'mittag', 'vormittag', 'nachmittag', 'abend', 'nachts',
        'morgens', 'lunch', 'dinner', 'evening', 'breakfast',
    ]):
        return True
    # Short labels (e.g. "Lunch"/"Dinner") — but only if they
    # contain at least one letter (rejects "8", "10", "1.5").
    if len(t) <= 12 and re.search(r"[a-zäöüß]", t):
        return True
    return False


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

    async def _guess_time_select(self, page: Any, date_selector: str) -> Any:
        """Heuristic: pick a secondary <select> that likely represents a time-slot dropdown.

        Uses the page's JS context to gather metadata about every <select>
        (label text, id, name, option count, whether it's the date select)
        in a single round-trip, then walks the corresponding Playwright handle
        list. Prefers selects whose label/id/name explicitly indicates a
        time/shift dropdown. Falls back to any non-date select that has
        at least one real option.

        Returns an ElementHandle or None.
        """
        try:
            info = await page.evaluate(
                """(dateSelector) => {
                    const dateEl = document.querySelector(dateSelector);
                    const all = Array.from(document.querySelectorAll('select'));
                    return all.map((sel) => {
                        let lab = '';
                        if (sel.labels && sel.labels.length) lab = sel.labels[0].innerText.trim();
                        if (!lab) {
                            const p = sel.closest('[class*=form], [class*=field], div');
                            const l = p && p.querySelector('label');
                            if (l) lab = l.innerText.trim();
                        }
                        const realOpts = Array.from(sel.options)
                            .filter(o => o.value && !o.disabled).length;
                        return {
                            isDate: sel === dateEl,
                            label: lab.toLowerCase(),
                            id: (sel.id || '').toLowerCase(),
                            name: (sel.name || '').toLowerCase(),
                            realOpts,
                            visible: sel.offsetParent !== null,
                        };
                    });
                }""",
                date_selector,
            )
            handles = await page.query_selector_all('select')
            if not info or len(info) != len(handles):
                return None

            time_tokens = (
                'uhrzeit', 'uhr', 'zeit', 'time', 'shift', 'schicht',
                'slot', 'session', 'termin', 'booking_list',
            )

            def looks_like_time_select(meta: Dict[str, Any]) -> bool:
                blob = f"{meta['label']} {meta['id']} {meta['name']}"
                return any(tok in blob for tok in time_tokens)

            # Pass 1: explicit time-label match with at least one real option.
            for meta, handle in zip(info, handles):
                if meta['isDate'] or not meta['visible']:
                    continue
                if looks_like_time_select(meta) and meta['realOpts'] >= 1:
                    return handle

            # Pass 2: any visible non-date select with real options.
            for meta, handle in zip(info, handles):
                if meta['isDate'] or not meta['visible']:
                    continue
                if meta['realOpts'] >= 1:
                    return handle
        except Exception:
            return None

        return None

    async def _body_text(self, page: Any) -> str:
        try:
            return await page.inner_text('body')
        except Exception:
            return ""

    async def _count_selects(self, page: Any) -> Optional[int]:
        """How many <select> elements the page rendered. None if we couldn't ask."""
        try:
            return len(await page.query_selector_all('select'))
        except Exception:
            return None

    def _fail(
        self,
        error: str,
        blocked: bool = False,
        status_code: Optional[int] = None,
    ) -> ScrapeResult:
        result = ScrapeResult(
            success=False, error=error, blocked=blocked, status_code=status_code
        )
        # Every result from this scraper carries the marker so callers can read
        # it without guarding.
        result.times_incomplete = False
        return result

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
        time_selector = self.config.get('time_selector')

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
            result.times_incomplete = False
            return result

        # Dates
        available_dates = await self._extract_select(page, date_selector)
        logger.info(f"Found {len(available_dates)} available date options")

        # Times (optional; auto-detect if not configured)
        available_times: Dict[str, Dict[str, Any]] = {}
        if available_dates:
            date_select = await page.query_selector(date_selector)
            guessed_time_select = None

            for i, date in enumerate(available_dates):
                if i:
                    # Jitter between date switches; each one costs the tent a request.
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                try:
                    await date_select.select_option(value=date['value'])
                    # Ensure JS listeners fire on frameworks that don't react to Playwright's
                    # select_option alone. Playwright's page.evaluate takes a single arg —
                    # pack our two values into a list and destructure inside JS.
                    try:
                        await page.evaluate(
                            "([sel, val]) => { const el = document.querySelector(sel); if (!el) return; el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                            [date_selector, date["value"]],
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(2)

                    # Some sites populate the time dropdown asynchronously after selecting a date.
                    # Wait until at least one non-placeholder option is present so we reliably
                    # include time labels (e.g., Mittag/Abend) in notifications.
                    if time_selector:
                        try:
                            await page.wait_for_selector(time_selector, timeout=10000)
                            await page.wait_for_function(
                                "(sel) => { const el = document.querySelector(sel); if (!el) return false; return Array.from(el.options).some(o => o.value && !o.disabled); }",
                                time_selector,
                                timeout=10000,
                            )
                        except Exception:
                            pass

                    # For auto-detect, re-guess after selecting a date (some pages create the time dropdown dynamically)
                    if not time_selector:
                        guessed_time_select = await self._guess_time_select(page, date_selector)

                    if time_selector:
                        times = await self._extract_select(page, time_selector)
                    elif guessed_time_select:
                        times = await self._extract_select_handle(guessed_time_select)
                    else:
                        times = []

                    # Every option of the configured booking-list select IS a
                    # booking list, whatever it is called ("Spontanreservierung",
                    # "26.09.2026 17:00 - 23:00"). Only the auto-detected select
                    # needs a sanity filter, because it may be Tischgröße.
                    if not time_selector:
                        times = [t for t in times if _looks_like_time(t.get('text', ''))]

                    if times:
                        available_times[date['value']] = {
                            'date_text': date['text'],
                            'times': times,
                        }
                except Exception as e:
                    logger.info(f"{self.tent_name}: Failed to extract times for date {date.get('text')}: {e}")

        slots = self._build_slots(available_dates, available_times)
        # A date whose time we couldn't read is still a date we must alert on —
        # reporting it as "time unknown" beats losing it to a hard failure.
        dates_without_times = [s for s in slots if s['time_value'] is None]
        if time_selector and dates_without_times:
            logger.warning(
                f"{self.tent_name}: no time options for {len(dates_without_times)}/"
                f"{len(available_dates)} dates via {time_selector}"
            )

        result = ScrapeResult(
            success=True,
            dates_available=len(available_dates) > 0,
            available_dates=available_dates,
            available_times=available_times,
            slots=slots,
            status_code=status,
        )
        result.times_incomplete = bool(time_selector and dates_without_times)
        return result

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
