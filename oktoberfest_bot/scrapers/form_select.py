"""Scraper for tent reservation pages using form select dropdowns"""

import asyncio
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import async_playwright

from .base_scraper import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


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
        """Heuristic: pick a secondary <select> that likely represents a time slot dropdown.

        Returns an ElementHandle or None.
        """
        try:
            date_el = await page.query_selector(date_selector)
            selects = await page.query_selector_all('select')
            candidates = [s for s in selects if s != date_el]

            # 1) First pass: look for selects with recognizable id/name.
            preferred: List[Any] = []
            other: List[Any] = []
            for cand in candidates:
                _id = (await cand.get_attribute('id')) or ''
                _name = (await cand.get_attribute('name')) or ''
                blob = f"{_id} {_name}".lower()
                if any(token in blob for token in ['time', 'uhr', 'booking_list', 'slot', 'termin', 'session']):
                    preferred.append(cand)
                else:
                    other.append(cand)

            for group in (preferred, other):
                for cand in group:
                    opts = await self._extract_select_handle(cand)
                    if len(opts) >= 1:
                        return cand

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
                    # Capture a tiny hint for debugging (often a bot-check page).
                    try:
                        body_head = (await page.inner_text('body'))[:200].replace('\n', ' ')
                        logger.warning(f"Date select not found; body starts with: {body_head!r}")
                    except Exception:
                        pass
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
                        # Common patterns/labels
                        if ':' in t or 'uhr' in t:
                            return True
                        if any(word in t for word in ['mittag', 'vormittag', 'nachmittag', 'abend', 'nachts']):
                            return True
                        # Short labels like "Lunch"/"Dinner" etc.
                        if len(t) <= 12:
                            return True
                        return False

                    for date in available_dates:
                        try:
                            await date_select.select_option(value=date['value'])
                            await asyncio.sleep(2)

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

                # Fallback: headed Chromium inside Xvfb (often passes bot-protection)
                await browser.close()
                browser = None

                xvfb_proc, display = self._start_xvfb()
                if display:
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
