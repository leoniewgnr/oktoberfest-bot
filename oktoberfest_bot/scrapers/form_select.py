"""Scraper for tent reservation pages using form select dropdowns"""

import asyncio
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import async_playwright

from .base_scraper import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


# Phrases tents show when they haven't published reservations yet. If the page
# loads (no bot-check / no DOM error) but lacks a date <select>, AND contains
# any of these, we treat that as "tent is up, just no slots yet" — success with
# zero dates rather than an error.
_NO_SLOTS_PHRASES = (
    "kein termin verfügbar",
    "keine reservierung",
    "noch keine reservierung",
    "regelmäßigen abständen",          # "Wir stellen in regelmäßigen Abständen..."
    "regelmäßigen abständen neue",
    "demnächst",
    "no availability",
    "no reservations available",
)


def _looks_like_no_slots_message(body_text: str) -> bool:
    """Heuristic: does the page body look like a tent saying 'no slots yet'?"""
    if not body_text:
        return False
    t = body_text.lower()
    return any(phrase in t for phrase in _NO_SLOTS_PHRASES)


class FormSelectScraper(BaseScraper):
    """Scraper for tents using select dropdown detection"""

    def _start_xvfb(self) -> Tuple[Optional[subprocess.Popen], Optional[str]]:
        """Start a temporary Xvfb display for headed Chromium (helps with some bot protection).

        Returns (proc, display). If Xvfb cannot be started, returns (None, None).
        """
        if os.environ.get('DISPLAY'):
            return None, None

        display = ':99'
        try:
            proc = subprocess.Popen(
                ['Xvfb', display, '-screen', '0', '1365x768x24', '-nolisten', 'tcp'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return proc, display
        except Exception:
            return None, None

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

            def looks_like_time(meta: Dict[str, Any]) -> bool:
                blob = f"{meta['label']} {meta['id']} {meta['name']}"
                return any(tok in blob for tok in time_tokens)

            # Pass 1: explicit time-label match with at least one real option.
            for meta, handle in zip(info, handles):
                if meta['isDate'] or not meta['visible']:
                    continue
                if looks_like_time(meta) and meta['realOpts'] >= 1:
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

    async def check_availability(self) -> ScrapeResult:
        """Check for available dates (and optionally times) on the reservation page."""
        logger.info(f"Checking availability for {self.tent_name}...")

        async with async_playwright() as p:
            async def _launch(headless: bool):
                args = [
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-blink-features=AutomationControlled',
                ]
                try:
                    return await p.chromium.launch(headless=headless, channel='chrome', args=args)
                except Exception:
                    return await p.chromium.launch(headless=headless, args=args)

            async def _run_once(browser) -> ScrapeResult:
                page = await browser.new_page(
                    user_agent=(
                        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                        '(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36'
                    ),
                    viewport={'width': 1365, 'height': 768},
                    locale='de-DE',
                )
                page.set_default_timeout(30000)

                logger.info(f"Loading page: {self.url}")
                await page.goto(self.url, wait_until='domcontentloaded')
                try:
                    await page.wait_for_load_state('networkidle', timeout=15000)
                except Exception:
                    pass

                date_selector = self.config.get('selector', 'select.form-select')
                time_selector = self.config.get('time_selector')

                try:
                    await page.wait_for_selector(date_selector, timeout=60000)
                except Exception:
                    # Distinguish "no slots published yet" (legitimate empty state)
                    # from real scrape failure (bot-check / DOM change). Empty state
                    # should be reported as a success with zero dates, NOT as an
                    # error, so we don't spam error alerts or trigger the headed
                    # fallback unnecessarily.
                    body_head = ""
                    try:
                        body_head = (await page.inner_text('body'))[:600]
                    except Exception:
                        pass
                    if _looks_like_no_slots_message(body_head):
                        logger.info(
                            f"{self.tent_name}: no slots published yet "
                            "(empty-state page detected)"
                        )
                        return ScrapeResult(
                            success=True,
                            dates_available=False,
                            available_dates=[],
                            available_times={},
                        )
                    if body_head:
                        logger.warning(
                            f"Date select not found; body starts with: "
                            f"{body_head[:200].replace(chr(10), ' ')!r}"
                        )
                    return ScrapeResult(success=False, error='Select element not found')

                # Dates
                available_dates = await self._extract_select(page, date_selector)
                logger.info(f"Found {len(available_dates)} available date options")

                # Times (optional; auto-detect if not configured)
                available_times: Dict[str, Dict[str, Any]] = {}
                if available_dates:
                    date_select = await page.query_selector(date_selector)

                    guessed_time_select = None

                    def _looks_like_date(text: str) -> bool:
                        # e.g. "Freitag, 25.09.2026" or "25.09.2026"
                        import re
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
                        import re
                        if len(t) <= 12 and re.search(r"[a-zäöüß]", t):
                            return True
                        return False

                    for date in available_dates:
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

                            # Filter out bogus "times" that are actually dates or other long labels
                            times = [t for t in times if _looks_like_time(t.get('text', ''))]

                            if times:
                                available_times[date['value']] = {
                                    'date_text': date['text'],
                                    'times': times,
                                }
                        except Exception as e:
                            logger.info(f"{self.tent_name}: Failed to extract times for date {date.get('text')}: {e}")


                # If a time selector is configured for this tent, we consider it a scrape failure
                # when we cannot extract any time options for any date. This prevents sending
                # misleading date-only notifications and helps avoid missing Abend opportunities.
                if time_selector and available_dates and not available_times:
                    logger.warning(
                        f"{self.tent_name}: time_selector configured ({time_selector}) but extracted 0 time options"
                    )
                    return ScrapeResult(success=False, error='Time options not extracted')

                return ScrapeResult(
                    success=True,
                    dates_available=len(available_dates) > 0,
                    available_dates=available_dates,
                    available_times=available_times,
                )

            browser = None
            xvfb_proc = None
            old_display = os.environ.get('DISPLAY')
            try:
                # First try: headless (cheap)
                browser = await _launch(headless=True)
                result = await _run_once(browser)
                if result.success:
                    return result

                # Fallback: headed Chromium inside Xvfb (often passes bot-protection).
                # Only attempt this if Xvfb actually starts — otherwise we'd launch
                # a headed browser without a display and crash the call. Without
                # Xvfb, return the original headless failure instead.
                await browser.close()
                browser = None

                xvfb_proc, display = self._start_xvfb()
                if not display:
                    logger.info(
                        f"{self.tent_name}: headless failed, Xvfb unavailable — "
                        "skipping headed fallback"
                    )
                    return result

                os.environ['DISPLAY'] = display
                browser = await _launch(headless=False)
                result2 = await _run_once(browser)
                return result2

            except Exception as e:
                logger.error(f"Error checking page: {e}")
                return ScrapeResult(success=False, error=str(e))
            finally:
                try:
                    if browser:
                        await browser.close()
                except Exception:
                    pass
                if xvfb_proc:
                    try:
                        xvfb_proc.terminate()
                    except Exception:
                        pass
                # Restore DISPLAY
                if old_display is not None:
                    os.environ['DISPLAY'] = old_display
                elif 'DISPLAY' in os.environ:
                    del os.environ['DISPLAY']
