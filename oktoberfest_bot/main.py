#!/usr/bin/env python3
"""Main orchestrator for Oktoberfest tent reservation monitoring"""

import asyncio
import logging
import random
import sys
from pathlib import Path
from typing import Any, Dict, List

from .config_loader import ConfigLoader
from .state_manager import StateManager
from .notifiers import TelegramNotifier
from .scrapers import FormSelectScraper

# Per-tent loop tuning
_JITTER_FRACTION = 0.10  # ±10% jitter on every check interval
_MIN_SLEEP_SECONDS = 30  # safety floor so jitter can't drive cadence to zero

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


def _combine_date_times(
    dates: List[Dict],
    times_by_date: Dict[str, Dict],
) -> List[Dict]:
    """Return a flat list of combined slot options.

    If we have times for a date, we emit "<date> – <time>" items.
    If we don't, we fall back to the date text only.

    (We *do not* apply suppression here; suppression is handled in time-slot-only alerts.)
    """
    combined: List[Dict] = []
    for d in (dates or []):
        date_val = d.get('value')
        date_text = (d.get('text') or '').strip()
        info = (times_by_date or {}).get(date_val) if date_val is not None else None
        ts = (info or {}).get('times') or []

        if ts:
            for t in ts:
                t_val = t.get('value')
                t_text = (t.get('text') or '').strip()
                combined.append(
                    {
                        'value': f"{date_val}|{t_val}" if t_val is not None else str(date_val),
                        'text': f"{date_text} – {t_text}" if t_text else date_text,
                    }
                )
        else:
            combined.append({'value': date_val, 'text': date_text})

    return combined


def _has_time_label(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if "–" in t:
        return True
    if any(w in t for w in ["mittag", "abend", "vormittag", "nachmittag", "nachts", "uhr"]):
        return True
    import re
    return bool(re.search(r"\d{1,2}:\d{2}", t))


def _missing_time_dates(dates: list, times_by_date: dict) -> list:
    missing = []
    for d in (dates or []):
        dv = d.get("value")
        info = (times_by_date or {}).get(dv) if dv is not None else None
        ts = (info or {}).get("times") or []
        if not ts:
            missing.append((d.get("text") or str(dv) or "").strip())
    return missing


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

        async def _run_check_with_retries(max_attempts: int = 3, retry_delay_s: float = 2.5):
            last = None
            for attempt in range(1, max_attempts + 1):
                last = await scraper.check_availability()
                if last and last.success:
                    if tent_config.get('time_selector') and last.dates_available and not (last.available_times or {}):
                        if attempt < max_attempts:
                            logger.warning(
                                f"{tent_name}: times missing (attempt {attempt}/{max_attempts}); retrying in {retry_delay_s}s"
                            )
                            await asyncio.sleep(retry_delay_s)
                            continue
                break
            return last

        result = await _run_check_with_retries()

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


            if tent_config.get('time_selector') and new_dates:
                missing_for_new = _missing_time_dates(new_dates, result.available_times)
                if missing_for_new:
                    msg = (
                        f"⚠️ <b>{tent_name.upper()} - NEW DATE DETECTED, TIME UNKNOWN</b>\n\n"
                        "A new reservation date appeared, but the bot could not reliably extract the time slot(s) (e.g. Abend) right now. Please check manually ASAP:\n\n"
                        + "\n".join([f"• {d}" for d in missing_for_new])
                        + f"\n\n🔗 {tent_config['url']}"
                    )
                    notifier.send_notification(msg)


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

                # Prefer combined date+time slot text when we can extract times.
                combined_slots = _combine_date_times(result.available_dates, result.available_times)
                notifier.send_dates_available(tent_name, tent_config['url'], combined_slots)

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
                        logger.info(f"{tent_name}: New options added: {len(new_dates)}")
                        try:
                            logger.info(
                                f"{tent_name}: New option texts: "
                                + ", ".join([str(d.get('text', '')).strip() for d in new_dates])
                            )
                        except Exception:
                            pass

                        combined_new = _combine_date_times(new_dates, result.available_times)
                        if tent_config.get('time_selector'):
                            combined_new = [o for o in combined_new if _has_time_label(o.get('text', ''))]
                        if combined_new:
                            notifier.send_new_dates_added(tent_name, tent_config['url'], combined_new)

                    # New time slots can appear even if dates stay available.
                    for date_text, new_times in newly_available_times:
                        logger.info(f"{tent_name}: New time slots for {date_text}: {len(new_times)}")
                        try:
                            logger.info(
                                f"{tent_name}: New time texts for {date_text}: "
                                + ", ".join([str(t.get('text', '')).strip() for t in new_times])
                            )
                        except Exception:
                            pass
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


async def _tent_loop(
    tent_config: Dict,
    state_manager: StateManager,
    notifier: TelegramNotifier,
    logger: logging.Logger,
):
    """One independent loop per tent — own interval, own jitter."""
    tent_name = tent_config['name']
    interval = max(60, int(tent_config.get('check_interval', 180)))

    # Small randomized stagger so all tents don't fire on the same wall-clock
    # tick right after boot.
    await asyncio.sleep(random.uniform(0, min(30, interval)))

    while True:
        try:
            await check_tent(tent_config, state_manager, notifier, logger)
        except Exception as e:
            logger.error(f"{tent_name}: tent_loop unexpected error - {e}")

        jitter = random.uniform(-_JITTER_FRACTION, _JITTER_FRACTION) * interval
        sleep_for = max(_MIN_SLEEP_SECONDS, interval + jitter)
        logger.debug(f"{tent_name}: sleeping {sleep_for:.1f}s")
        await asyncio.sleep(sleep_for)


async def _heartbeat_loop(
    tents: List[Dict],
    state_manager: StateManager,
    notifier: TelegramNotifier,
    logger: logging.Logger,
    interval_seconds: int,
):
    """Periodic digest covering every monitored tent — so no tent silently drops."""
    interval = max(60, int(interval_seconds))
    while True:
        await asyncio.sleep(interval)
        try:
            reports = []
            for t in tents:
                tent_id = t['id']
                state = state_manager.get_tent_state(tent_id)
                reports.append({
                    'name': t['name'],
                    'last_check_iso': state.get('last_check'),
                    'available_count': len(state.get('available_dates') or []),
                    'consecutive_errors': state.get('consecutive_errors', 0),
                    'slot_pairs': state_manager.get_slot_pairs(tent_id),
                })
            notifier.send_heartbeat(reports)
            logger.info(f"Heartbeat sent — {len(reports)} tent(s)")
        except Exception as e:
            logger.error(f"heartbeat_loop error: {e}")


async def monitor_loop(
    config_loader: ConfigLoader,
    state_manager: StateManager,
    notifier: TelegramNotifier,
    logger: logging.Logger,
    config: Dict[str, Any],
):
    """Main monitoring loop — one task per tent + one heartbeat task."""
    tents = config_loader.get_tents()
    heartbeat_interval = int(config.get('heartbeat_interval_seconds', 86400))

    logger.info(
        f"Starting Oktoberfest Monitor — {len(tents)} tent(s), "
        f"heartbeat every {heartbeat_interval}s"
    )
    tent_names = [tent['name'] for tent in tents]
    min_interval = min(tent.get('check_interval', 180) for tent in tents)
    notifier.send_startup_notification(tent_names, min_interval)

    tasks = [
        asyncio.create_task(
            _tent_loop(t, state_manager, notifier, logger),
            name=f"tent:{t['id']}",
        )
        for t in tents
    ]
    tasks.append(
        asyncio.create_task(
            _heartbeat_loop(tents, state_manager, notifier, logger, heartbeat_interval),
            name="heartbeat",
        )
    )

    # Tasks loop forever; if one ever exits/crashes, log it and let the rest
    # keep running. (asyncio.gather with return_exceptions surfaces crashes
    # without killing siblings.)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results):
        if isinstance(result, BaseException):
            logger.error(f"Task {task.get_name()} crashed: {result}")


def main():
    """Main entry point"""
    try:
        config_loader = ConfigLoader(str(CONFIG_FILE), str(TENTS_FILE))
        config = config_loader.get_config()

        setup_logging(config['log_file'])
        logger = logging.getLogger(__name__)

        state_manager = StateManager(config['state_file'])

        notifier = TelegramNotifier(config['telegram_bot_token'], config['telegram_chat_id'])

        asyncio.run(monitor_loop(config_loader, state_manager, notifier, logger, config))

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
