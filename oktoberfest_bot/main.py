#!/usr/bin/env python3
"""Main orchestrator for Oktoberfest tent reservation monitoring"""

import asyncio
import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List

from config_loader import ConfigLoader
from state_manager import StateManager
from notifiers import TelegramNotifier
from scrapers import FormSelectScraper

# Default paths
BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "config.json"
TENTS_FILE = CONFIG_DIR / "tents.json"


def setup_logging(log_file: str):
    """Configure logging"""
    # Ensure log directory exists
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )


def create_scraper(tent_config: Dict):
    """Factory function to create appropriate scraper for tent"""
    scraper_type = tent_config.get('scraper_type', 'form_select')

    if scraper_type == 'form_select':
        return FormSelectScraper(tent_config)
    else:
        raise ValueError(f"Unknown scraper type: {scraper_type}")


async def check_tent(tent_config: Dict, state_manager: StateManager,
                     notifier: TelegramNotifier, logger: logging.Logger):
    """Check a single tent for availability"""
    tent_id = tent_config['id']
    tent_name = tent_config['name']

    try:
        # Create scraper for this tent
        scraper = create_scraper(tent_config)

        # Check availability
        result = await scraper.check_availability()

        if result.success:
            # Get previous state
            was_available = state_manager.is_dates_available(tent_id)
            was_in_error_state = state_manager.is_error_notified(tent_id)

            # Check if we recovered from errors
            if was_in_error_state:
                notifier.send_recovery_notification(tent_name)

            # Update state
            state_manager.mark_check_success(
                tent_id,
                result.dates_available,
                result.available_dates
            )

            # Check for state changes
            if result.dates_available and not was_available:
                # New dates became available!
                logger.info(f"{tent_name}: NEW DATES AVAILABLE!")
                notifier.send_dates_available(
                    tent_name,
                    tent_config['url'],
                    result.available_dates
                )

            elif not result.dates_available and was_available:
                # Dates no longer available
                logger.info(f"{tent_name}: Dates no longer available")
                notifier.send_dates_unavailable(tent_name)

            else:
                # No change in availability
                if result.dates_available:
                    logger.info(f"{tent_name}: Dates still available ({len(result.available_dates)} options)")
                else:
                    logger.info(f"{tent_name}: No dates available yet")

        else:
            # Check failed
            error_msg = result.error
            logger.error(f"{tent_name}: Check failed - {error_msg}")

            state_manager.mark_check_error(tent_id)

            # Send error notification only once when entering error state
            if not state_manager.is_error_notified(tent_id):
                error_count = state_manager.get_consecutive_errors(tent_id)
                notifier.send_error_notification(tent_name, error_msg, error_count)
                state_manager.mark_error_notified(tent_id)

    except Exception as e:
        logger.error(f"{tent_name}: Unexpected error - {e}")
        state_manager.mark_check_error(tent_id)

        # Send error notification only once when entering error state
        if not state_manager.is_error_notified(tent_id):
            error_count = state_manager.get_consecutive_errors(tent_id)
            notifier.send_error_notification(tent_name, str(e), error_count)
            state_manager.mark_error_notified(tent_id)


async def monitor_loop(config_loader: ConfigLoader, state_manager: StateManager,
                       notifier: TelegramNotifier, logger: logging.Logger):
    """Main monitoring loop"""
    tents = config_loader.get_tents()

    logger.info("Starting Oktoberfest Monitor...")
    logger.info(f"Monitoring {len(tents)} tent(s)")

    # Send startup notification
    tent_names = [tent['name'] for tent in tents]
    # Use the minimum check interval across all tents
    min_interval = min(tent.get('check_interval', 180) for tent in tents)
    notifier.send_startup_notification(tent_names, min_interval)

    while True:
        try:
            # Check all tents concurrently
            tasks = []
            for tent in tents:
                task = check_tent(tent, state_manager, notifier, logger)
                tasks.append(task)

            await asyncio.gather(*tasks)

            # Wait before next check (use minimum interval)
            logger.info(f"Waiting {min_interval} seconds until next check...")
            await asyncio.sleep(min_interval)

        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}")
            await asyncio.sleep(60)  # Wait a minute before retrying


def main():
    """Main entry point"""
    try:
        # Load configuration
        config_loader = ConfigLoader(str(CONFIG_FILE), str(TENTS_FILE))
        config = config_loader.get_config()

        # Setup logging
        setup_logging(config['log_file'])
        logger = logging.getLogger(__name__)

        # Initialize state manager
        state_manager = StateManager(config['state_file'])

        # Initialize notifier
        notifier = TelegramNotifier(
            config['telegram_bot_token'],
            config['telegram_chat_id']
        )

        # Run monitoring loop
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
