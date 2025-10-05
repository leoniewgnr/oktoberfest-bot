"""Scraper for tent reservation pages using form select dropdowns"""

import asyncio
import logging
from playwright.async_api import async_playwright
from .base_scraper import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


class FormSelectScraper(BaseScraper):
    """Scraper for tents using select dropdown detection"""

    async def check_availability(self) -> ScrapeResult:
        """Check for available dates on the reservation page"""
        logger.info(f"Checking availability for {self.tent_name}...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )

            try:
                page = await browser.new_page()
                page.set_default_timeout(30000)

                logger.info(f"Loading page: {self.url}")
                await page.goto(self.url, wait_until='networkidle')

                # Wait for the page to load completely
                await asyncio.sleep(5)

                # Get selector from config or use default
                selector = self.config.get('selector', 'select.form-select')

                # Try to find the select element
                select_element = await page.query_selector(selector)

                if select_element:
                    # Get all option elements
                    options = await select_element.query_selector_all('option')

                    # Filter out disabled options
                    available_options = []
                    for option in options:
                        is_disabled = await option.get_attribute('disabled')
                        value = await option.get_attribute('value')
                        text = await option.inner_text()

                        # Skip the placeholder option
                        if not is_disabled and value and value != "":
                            available_options.append({
                                'value': value,
                                'text': text.strip()
                            })

                    logger.info(f"Found {len(available_options)} available date options")

                    return ScrapeResult(
                        success=True,
                        dates_available=len(available_options) > 0,
                        available_dates=available_options
                    )
                else:
                    logger.warning("Select element not found on page")
                    return ScrapeResult(
                        success=False,
                        error='Select element not found'
                    )

            except Exception as e:
                logger.error(f"Error checking page: {e}")
                return ScrapeResult(
                    success=False,
                    error=str(e)
                )
            finally:
                await browser.close()
