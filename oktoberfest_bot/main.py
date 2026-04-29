#!/usr/bin/env python3
"""Main orchestrator for Oktoberfest tent reservation monitoring"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Dict, List

from .config_loader import ConfigLoader
from .state_manager import StateManager
from .notifiers import TelegramNotifier
from .scrapers import FormSelectScraper

# Default paths
BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "config.json"
TENTS_FILE = CONFIG_DIR / "tents.json"


def setup_logging(log_file: str):
    """Configure logging"""
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )


def create_scraper(tent_config: Dict):
    """Factory function to create appropriate scraper for tent"""
    scraper_type = tent_config.get('scraper_type', 'form_select')

    if scraper_type == 'form_select':
        return FormSelectScraper(tent_config)
    raise ValueError(f"Unknown scraper type: {scraper_type}")


def _values(items: List[Dict]) -> set:
    return {i.get('value') for i in items if i.get('value') is not None}


async def check_tent(
    tent_config: Dict,
    state_manager: StateManager,
    notifier: TelegramNotifier,
    logger: logging.Logger,
):
    """Check a single tent for availability"""
    tent_id = tent_config['id']
    tent_name = tent_config['name']

    try:
        scraper = create_scraper(tent_config)
        result = await scraper.check_availability()

        if result.success:
            was_available = state_manager.is_dates_available(tent_id)
            was_in_error_state = state_manager.is_error_notified(tent_id)

            prev_times = state_manager.get_available_times(tent_id)
            prev_dates = state_manager.get_available_dates(tent_id)
            prev_date_values = _values(prev_dates)

            if was_in_error_state:
                notifier.send_recovery_notification(tent_name)

            # Detect newly added date options (even if dates were already available)
            new_dates = [d for d in result.available_dates if d.get('value') not in prev_date_values]

            # Detect newly available times (best-effort; only if scraper provides them)
            newly_available_times = []
            if result.available_times:
                for date_value, info in result.available_times.items():
                    prev_for_date = prev_times.get(date_value, {})
                    prev_time_values = _values(prev_for_date.get('times', []))
                    current_times = info.get('times', [])
                    new_times = [t for t in current_times if t.get('value') not in prev_time_values]
                    if new_times:
                        newly_available_times.append((info.get('date_text') or date_value, new_times))

            # Update state
            state_manager.mark_check_success(
                tent_id,
                result.dates_available,
                result.available_dates,
                result.available_times,
            )

            # State change: dates
            if result.dates_available and not was_available:
                logger.info(f"{tent_name}: NEW DATES AVAILABLE!")
                notifier.send_dates_available(tent_name, tent_config['url'], result.available_dates)

                # If the page also exposes time slots, announce them too.
                for date_text, new_times in newly_available_times:
                    notifier.send_times_available(tent_name, tent_config['url'], date_text, new_times)

            elif not result.dates_available and was_available:
                logger.info(f"{tent_name}: Dates no longer available")
                notifier.send_dates_unavailable(tent_name)

            else:
                # No change in overall date availability
                if result.dates_available:
                    dates_str = ", ".join(d.get("text", "") for d in result.available_dates)
                    logger.info(f"{tent_name}: Dates still available ({len(result.available_dates)}): {dates_str}")

                    # If additional dates appeared, announce them.
                    if new_dates:
                        logger.info(f"{tent_name}: New dates added: {len(new_dates)}")
                        notifier.send_new_dates_added(tent_name, tent_config['url'], new_dates)

                    # New time slots can appear even if dates stay available.
                    for date_text, new_times in newly_available_times:
                        logger.info(f"{tent_name}: New time slots for {date_text}: {len(new_times)}")
                        notifier.send_times_available(tent_name, tent_config['url'], date_text, new_times)
                else:
                    logger.info(f"{tent_name}: No dates available yet")

        else:
            error_msg = result.error
            logger.error(f"{tent_name}: Check failed - {error_msg}")

            state_manager.mark_check_error(tent_id)

            if not state_manager.is_error_notified(tent_id):
                error_count = state_manager.get_consecutive_errors(tent_id)
                notifier.send_error_notification(tent_name, error_msg, error_count)
                state_manager.mark_error_notified(tent_id)

    except Exception as e:
        logger.error(f"{tent_name}: Unexpected error - {e}")
        state_manager.mark_check_error(tent_id)

        if not state_manager.is_error_notified(tent_id):
            error_count = state_manager.get_consecutive_errors(tent_id)
            notifier.send_error_notification(tent_name, str(e), error_count)
            state_manager.mark_error_notified(tent_id)


async def monitor_loop(
    config_loader: ConfigLoader,
    state_manager: StateManager,
    notifier: TelegramNotifier,
    logger: logging.Logger,
):
    """Main monitoring loop"""
    tents = config_loader.get_tents()

    logger.info("Starting Oktoberfest Monitor...")
    logger.info(f"Monitoring {len(tents)} tent(s)")

    tent_names = [tent['name'] for tent in tents]
    min_interval = min(tent.get('check_interval', 180) for tent in tents)
    notifier.send_startup_notification(tent_names, min_interval)

    while True:
        try:
            tasks = [check_tent(tent, state_manager, notifier, logger) for tent in tents]
            await asyncio.gather(*tasks)

            logger.info(f"Waiting {min_interval} seconds until next check...")
            await asyncio.sleep(min_interval)

        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}")
            await asyncio.sleep(60)


def main():
    """Main entry point"""
    try:
        config_loader = ConfigLoader(str(CONFIG_FILE), str(TENTS_FILE))
        config = config_loader.get_config()

        setup_logging(config['log_file'])
        logger = logging.getLogger(__name__)

        state_manager = StateManager(config['state_file'])

        notifier = TelegramNotifier(config['telegram_bot_token'], config['telegram_chat_id'])

        asyncio.run(monitor_loop(config_loader, state_manager, notifier, logger))

    except KeyboardInterrupt:
        logger = logging.getLogger(__name__)
        logger.info("Monitoring stopped by user")
        sys.exit(0)
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
