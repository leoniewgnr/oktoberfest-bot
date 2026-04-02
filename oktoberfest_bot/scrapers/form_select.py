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
                await page.goto(self.url, wait_until='networkidle')

                # Give SPA widgets time to hydrate.
                await asyncio.sleep(5)

                date_selector = self.config.get('selector', 'select.form-select')
                time_selector = self.config.get('time_selector')

                # Ensure date select exists
                if not await page.query_selector(date_selector):
                    logger.warning(f"Date select element not found: {date_selector}")
                    return ScrapeResult(success=False, error='Select element not found')

                # Dates
                available_dates = await self._extract_select(page, date_selector)
                logger.info(f"Found {len(available_dates)} available date options")

                # Times (optional; auto-detect if not configured)
                available_times: Dict[str, Dict[str, Any]] = {}
                if available_dates:
                    date_select = await page.query_selector(date_selector)

                    guessed_time_select = None
                    if not time_selector:
                        # Try to guess a secondary time dropdown.
                        guessed_time_select = await self._guess_time_select(page, date_selector)

                    for date in available_dates:
                        try:
                            await date_select.select_option(value=date['value'])
                            await asyncio.sleep(2)

                            if time_selector:
                                times = await self._extract_select(page, time_selector)
                            elif guessed_time_select:
                                times = await self._extract_select_handle(guessed_time_select)
                            else:
                                times = []

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
