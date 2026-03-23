"""Scraper for tent reservation pages using form select dropdowns"""

import asyncio
import logging
from typing import Any, Dict, List

from playwright.async_api import async_playwright

from .base_scraper import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


class FormSelectScraper(BaseScraper):
    """Scraper for tents using select dropdown detection"""

    async def _extract_select(self, page: Any, selector: str) -> List[Dict[str, str]]:
        """Extract available options from a <select>."""
        select_element = await page.query_selector(selector)
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

                # Times (optional)
                available_times: Dict[str, Dict[str, Any]] = {}
                if time_selector and available_dates:
                    date_select = await page.query_selector(date_selector)

                    for date in available_dates:
                        try:
                            await date_select.select_option(value=date['value'])
                            # Wait a bit for dependent selects to update.
                            await asyncio.sleep(2)

                            times = await self._extract_select(page, time_selector)
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
