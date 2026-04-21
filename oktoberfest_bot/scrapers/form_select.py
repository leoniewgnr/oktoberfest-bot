"""Scraper for tent reservation pages using form select dropdowns"""

import asyncio
import logging
from typing import Any, Dict, List

from playwright.async_api import async_playwright

from .base_scraper import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


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
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox'],
            )

            try:
                page = await browser.new_page()
                page.set_default_timeout(30000)

                logger.info(f"Loading page: {self.url}")
                # Some of these reservation portals are SPAs that keep long-running requests open,
                # so 'networkidle' can be flaky. 'domcontentloaded' + explicit selector wait is
                # more robust.
                await page.goto(self.url, wait_until='domcontentloaded')

                date_selector = self.config.get('selector', 'select.form-select')
                time_selector = self.config.get('time_selector')

                # Give SPA widgets time to hydrate and then wait for the actual date selector.
                try:
                    await page.wait_for_selector(date_selector, timeout=45000)
                except Exception:
                    logger.warning(f"Date select element not found (timeout): {date_selector}")
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
                            logger.info(f"Failed to extract times for date {date.get('text')}: {e}")

                return ScrapeResult(
                    success=True,
                    dates_available=len(available_dates) > 0,
                    available_dates=available_dates,
                    available_times=available_times,
                )

            except Exception as e:
                logger.error(f"Error checking page: {e}")
                return ScrapeResult(success=False, error=str(e))
            finally:
                await browser.close()
