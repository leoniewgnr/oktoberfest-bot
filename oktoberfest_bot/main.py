#!/usr/bin/env python3
"""Main orchestrator for Oktoberfest tent reservation monitoring.

One task per target at that target's own cadence, plus two watchdog loops. Every
decision here serves one requirement: a Fri/Sat/Sun evening slot must never be
missed silently. So weekend slots are alerted individually and first, slot state
is committed only for the alerts Telegram actually accepted, and no alarm path
can latch to silence.
"""

import asyncio
import difflib
import html
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Set

from . import filters
from .config_loader import ConfigLoader
from .health import BlindnessMonitor, HealthReporter, escalating_interval
from .notifiers import TelegramNotifier
from .scrapers import AnnouncementScraper, ApiFzosScraper, FormSelectScraper
from .scrapers.api_fzos import prime_areas_cache
from .state_manager import StateManager

# Per-target loop tuning
_JITTER_FRACTION = 0.10  # ±10% jitter on every check interval
_MIN_SLEEP_SECONDS = 30  # safety floor so jitter can't drive cadence to zero
_BOOT_STAGGER_MAX_SECONDS = 30

_BLINDNESS_POLL_SECONDS = 60
# A target counts as blind once it has missed three of its own polls (the config
# floor still applies): a 30 min announcement page is not blind after 15 min.
_BLIND_POLLS_MISSED = 3

# Targets whose failure means we can no longer see booking supply. Announcement
# pages are third-party marketing sites; one of them being permanently down must
# not leave the external dead-man's switch stuck red and therefore useless.
_SLOT_BEARING_TYPES = ('api_fzos', 'form_select')

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
    if scraper_type == 'api_fzos':
        return ApiFzosScraper(tent_config)
    if scraper_type == 'announcement':
        return AnnouncementScraper(tent_config)
    raise ValueError(f"Unknown scraper type: {scraper_type}")


class Runtime(NamedTuple):
    """Collaborators every loop needs."""

    state: StateManager
    notifier: TelegramNotifier
    health: HealthReporter
    logger: logging.Logger
    config: Dict[str, Any]


class SlotEvent(NamedTuple):
    kind: str  # new | returned | state_changed | area_added
    slot: Dict[str, Any]
    # True unless the slot is provably Mon–Thu. Fri/Sat/Sun and every date we
    # could not parse get their own individual message: the unparseable class is
    # the safety net, so it must not be batched behind weekday noise.
    urgent: bool


def _slot_text(slot: Dict[str, Any]) -> str:
    date_text = (slot.get('date_text') or '').strip()
    time_text = (slot.get('time_text') or '').strip()
    return f"{date_text} – {time_text}" if time_text else date_text


def _slot_areas(slot: Dict[str, Any]) -> List[Dict[str, str]]:
    return [{'text': area} for area in (slot.get('areas') or []) if area]


def _interval_seconds(tent_config: Dict) -> int:
    return max(60, int(tent_config.get('check_interval', 180)))


def _blind_deadline(tent_config: Dict, blind_after: float) -> float:
    return max(blind_after, _interval_seconds(tent_config) * _BLIND_POLLS_MISSED)


def _alert_worthy(date_text: str, time_text: str) -> bool:
    """Whether a slot is worth a Telegram alert.

    Follows the shift filter, plus: when the shift is unknown (the browser tents
    can't read it), only Fri/Sat/Sun — or a date we can't parse — is worth
    sending. A weekday date with no readable shift is not the weekend evening she
    is watching for, and alerting on all of them is pure noise.
    """
    weekday = filters.parse_weekday(f"{date_text} {time_text}")
    if not filters.should_alert(date_text, time_text, weekday):
        return False
    if not (time_text or "").strip() and weekday is not None and weekday < 4:
        return False
    return True


def _slot_events(diffs: Dict[str, List[Dict[str, Any]]]) -> List[SlotEvent]:
    """One event per changed slot, urgent ones first.

    Precedence new > returned > state_changed > area_added so one slot never
    fires four messages in a cycle; the earlier kind is always the more urgent
    wording.
    """
    events: List[SlotEvent] = []
    seen = set()
    for kind in ('new', 'returned', 'state_changed', 'area_added'):
        for slot in diffs.get(kind) or []:
            key = slot.get('key')
            if key in seen:
                continue
            seen.add(key)
            date_text = slot.get('date_text') or ''
            time_text = slot.get('time_text') or ''
            if not _alert_worthy(date_text, time_text):
                continue
            weekday = filters.parse_weekday(f"{date_text} {time_text}")
            events.append(SlotEvent(kind, slot, weekday is None or weekday >= 4))
    events.sort(key=lambda event: not event.urgent)  # stable: urgent first
    return events


def _gone_for_seconds(state: StateManager, tent_id: str, slot_key: str) -> Optional[float]:
    """How long a returned slot had been missing — a Storno freed up 3 min ago
    reads very differently from one that was gone for a week."""
    entry = (state.get_tent_state(tent_id).get('seen_slots') or {}).get(slot_key) or {}
    gone_since = entry.get('gone_since')
    if not gone_since:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(gone_since)).total_seconds()
    except ValueError:
        return None


def _state_change_message(tent_config: Dict, slot: Dict[str, Any]) -> str:
    """No notifier template covers new_reservation_state flipping, and it is the
    only availability-ish field the API exposes, so it gets its own message."""
    name = html.escape(tent_config['name'].upper())
    weekend = filters.is_weekend_evening(
        slot.get('date_text') or '', slot.get('time_text') or ''
    )
    header = (
        f"🔴🔄 <b>WEEKEND EVENING - BOOKING STATE CHANGED - {name}</b> 🔄🔴"
        if weekend
        else f"🔄 <b>{name} - booking state changed</b>"
    )
    return (
        f"{header}\n\n"
        f"Slot: <b>{html.escape(_slot_text(slot))}</b>\n"
        f"New state: <code>{html.escape(str(slot.get('state')))}</code>\n\n"
        f"🔗 {html.escape(tent_config['url'])}\n"
        f"Slot-ID: <code>{html.escape(str(slot.get('key') or ''))}</code>"
    )


def _times_unknown_message(tent_config: Dict, slot_texts: List[str]) -> str:
    return (
        f"⚠️ <b>{html.escape(tent_config['name'].upper())} - DATES WITH UNKNOWN "
        "TIME</b>\n\n"
        "These reservation dates are published, but the bot cannot read their "
        "time slot(s) (e.g. Abend). Check them manually ASAP — this alert repeats "
        "until the times become readable:\n\n"
        + "\n".join(f"• {html.escape(text)}" for text in slot_texts)
        + f"\n\n🔗 {html.escape(tent_config['url'])}"
    )


def _startup_message(tents: List[Dict], watchdog_enabled: bool) -> str:
    """Cadences differ by an order of magnitude between API, browser and
    announcement targets, so every line carries its own."""
    lines = [
        "🚀 <b>Oktoberfest Monitor Started</b>",
        "",
        f"Watching {len(tents)} target(s):",
    ]
    for tent in tents:
        lines.append(
            f"• {html.escape(tent['name'])} — every {_interval_seconds(tent)}s "
            f"({html.escape(tent['scraper_type'])})"
        )
    if not watchdog_enabled:
        lines += [
            "",
            "⛔ <b>EXTERNAL WATCHDOG DISABLED</b> — healthcheck_url is empty, so "
            "Telegram is the only alarm path. That is the topology that hid the "
            "last 30-day outage.",
        ]
    return "\n".join(lines)


def _send_slot_event(rt: Runtime, tent_config: Dict, event: SlotEvent) -> bool:
    """Send one slot's alert. False means Telegram never took it."""
    tent_name = tent_config['name']
    url = tent_config['url']
    slot = event.slot
    key = slot.get('key') or ''
    date_text = slot.get('date_text') or ''
    time_text = (slot.get('time_text') or '').strip()

    if event.kind == 'returned':
        return rt.notifier.send_slot_returned(
            tent_name,
            url,
            date_text,
            time_text,
            key,
            _gone_for_seconds(rt.state, tent_config['id'], key),
        )
    if event.kind == 'state_changed':
        return rt.notifier.send_notification(
            _state_change_message(tent_config, slot)
        ) is not None
    if event.kind == 'area_added':
        return rt.notifier.send_areas_available(
            tent_name,
            url,
            date_text,
            time_text,
            [{'text': area} for area in slot.get('new_areas') or []],
            key,
        )
    if time_text:
        return rt.notifier.send_times_available(
            tent_name,
            url,
            date_text,
            [{'value': slot.get('time_value') or '', 'text': time_text}],
            _slot_areas(slot),
            key,
        )
    return rt.notifier.send_new_dates_added(
        tent_name, url, [{'value': key, 'text': date_text}], {key: _slot_areas(slot)}
    )


def _handle_times_incomplete(rt: Runtime, tent_config: Dict, slots: List[Dict[str, Any]]):
    """Standing alarm for dates whose time we cannot read.

    Reporting it only on the cycle the date first appeared turns "I can see a
    date but not whether it is Abend" into permanent silence.
    """
    tent_id = tent_config['id']
    unknown = [
        _slot_text(s)
        for s in slots
        if not (s.get('time_text') or '').strip()
        and _alert_worthy(s.get('date_text') or '', '')
    ]
    if not unknown:
        rt.state.update_tent_state(tent_id, times_unknown_notified_at=None)
        return
    interval = float(rt.config['blind_realert_interval_seconds'])
    if not rt.state.should_renotify(tent_id, 'times_unknown_notified_at', interval):
        return
    rt.logger.warning(f"{tent_config['name']}: time unreadable for {len(unknown)} date(s)")
    if rt.notifier.send_notification(_times_unknown_message(tent_config, unknown)) is not None:
        rt.state.mark_notified(tent_id, 'times_unknown_notified_at')


def _handle_slots(rt: Runtime, tent_config: Dict, result):
    tent_id = tent_config['id']
    tent_name = tent_config['name']
    logger = rt.logger
    state = rt.state

    was_available = state.is_dates_available(tent_id)
    was_failing = state.get_consecutive_errors(tent_id) > 0

    current_slots = result.slots or result.build_slots()
    events = _slot_events(state.diff_slots(tent_id, current_slots))
    logger.info(
        f"{tent_name}: {len(current_slots)} slot(s) visible, "
        f"{len(events)} alert-worthy change(s)"
    )

    # A slot lands in here when its alert was never delivered. commit_slots then
    # leaves it untouched, so the next cycle classifies and alerts it again.
    undelivered: Set[str] = set()

    # Weekend evenings — and everything we could not date — go out one message
    # each, before anything else in this cycle. Never batched behind noise.
    for event in [e for e in events if e.urgent]:
        logger.warning(f"{tent_name}: URGENT SLOT {event.kind} — {_slot_text(event.slot)}")
        if not _send_slot_event(rt, tent_config, event):
            undelivered.add(str(event.slot.get('key')))

    weekday_events = [e for e in events if not e.urgent]
    fresh = [e.slot for e in weekday_events if e.kind == 'new']
    if fresh:
        items = [{'value': s.get('key'), 'text': _slot_text(s)} for s in fresh]
        areas = {s.get('key'): _slot_areas(s) for s in fresh}
        send = (
            rt.notifier.send_new_dates_added
            if was_available
            else rt.notifier.send_dates_available
        )
        if not send(tent_name, tent_config['url'], items, areas):
            undelivered.update(str(s.get('key')) for s in fresh)
    for event in weekday_events:
        if event.kind != 'new' and not _send_slot_event(rt, tent_config, event):
            undelivered.add(str(event.slot.get('key')))

    if getattr(result, 'times_incomplete', False):
        _handle_times_incomplete(rt, tent_config, current_slots)

    if was_available and not result.dates_available:
        logger.info(f"{tent_name}: nothing published any more")
        rt.notifier.send_dates_unavailable(tent_name)

    if undelivered:
        logger.error(
            f"{tent_name}: {len(undelivered)} alert(s) NOT delivered — those slots "
            f"stay uncommitted and will re-alert next cycle"
        )
        rt.health.ping_failure(
            f"{tent_name}: Telegram dropped {len(undelivered)} slot alert(s)"
        )

    # Commit only what actually reached her: a dropped message must re-alert next
    # cycle instead of silently swallowing the slot.
    state.commit_slots(tent_id, current_slots, skip_keys=undelivered)
    state.mark_check_success(
        tent_id,
        result.dates_available,
        result.available_dates,
        result.available_times,
        result.available_areas,
    )
    if was_failing:
        rt.notifier.send_recovery_notification(tent_name)


def _handle_announcement(rt: Runtime, tent_config: Dict, result):
    """Text diff on plain marketing pages — this is what catches an offline
    Münchner-Kontingent window announced in prose, weeks before any booking route."""
    tent_id = tent_config['id']
    tent_name = tent_config['name']
    state = rt.state
    was_failing = state.get_consecutive_errors(tent_id) > 0

    text = getattr(result, 'text', '') or ''
    body_hash = getattr(result, 'body_hash', '') or ''
    stored = state.get_tent_state(tent_id)
    previous_hash = stored.get('announcement_hash')

    delivered = True
    if not previous_hash:
        # The very first read is reported: one of these pages may already be
        # announcing an open Münchner-Kontingent window right now, and silently
        # baselining it would mean only ever hearing about the *next* one.
        rt.logger.info(f"{tent_name}: announcement baseline")
        delivered = rt.notifier.send_announcement_changed(
            tent_name,
            tent_config['url'],
            getattr(result, 'keywords_found', []) or [],
            [],
            baseline=True,
        )
    elif body_hash != previous_hash:
        previous_text = stored.get('announcement_text') or ''
        diff_lines = [
            line
            for line in difflib.unified_diff(
                previous_text.splitlines(), text.splitlines(), lineterm='', n=0
            )
            if line[:1] in ('+', '-') and not line.startswith(('---', '+++'))
        ]
        rt.logger.warning(f"{tent_name}: page text changed, {len(diff_lines)} line(s)")
        delivered = rt.notifier.send_announcement_changed(
            tent_name,
            tent_config['url'],
            getattr(result, 'keywords_found', []) or [],
            diff_lines,
        )

    # Baselining a change we never managed to report would bury it forever.
    if delivered:
        state.update_tent_state(
            tent_id, announcement_hash=body_hash, announcement_text=text
        )
    else:
        rt.logger.error(f"{tent_name}: announcement alert NOT delivered — not baselining")
        rt.health.ping_failure(f"{tent_name}: Telegram dropped the announcement alert")
    state.mark_check_success(tent_id, False)
    if was_failing:
        rt.notifier.send_recovery_notification(tent_name)


def _handle_failure(rt: Runtime, tent_config: Dict, message: str):
    """Re-alert for as long as a target keeps failing. The old one-shot flag is
    what kept 30 days of blindness quiet; the escalation is what keeps a target
    that has been dead for a week from training her to mute the channel."""
    tent_id = tent_config['id']
    rt.state.mark_check_error(tent_id, message)
    sent = int(rt.state.get_tent_state(tent_id).get('error_notify_count') or 0)
    interval = escalating_interval(
        float(rt.config['blind_realert_interval_seconds']), sent
    )
    if rt.state.should_renotify_error(tent_id, interval):
        rt.notifier.send_error_notification(
            tent_config['name'], message, rt.state.get_consecutive_errors(tent_id)
        )
        rt.state.set_error_notified_at(tent_id)
        rt.state.update_tent_state(tent_id, error_notify_count=sent + 1)


async def check_tent(rt: Runtime, tent_config: Dict):
    """Run one check for one target and notify on whatever changed."""
    tent_name = tent_config['name']

    try:
        result = await create_scraper(tent_config).check_availability()
    except Exception as e:
        rt.logger.error(f"{tent_name}: scraper raised - {e}")
        await asyncio.to_thread(_handle_failure, rt, tent_config, str(e))
        return

    if result.blocked:
        # Being refused is not a scrape error: no per-target alert and no retry
        # here. The scraper's circuit breaker does the backing off, and the
        # blindness loop is what shouts if the refusal persists.
        reason = result.error or f"refused (status {result.status_code})"
        rt.logger.error(f"{tent_name}: REFUSED by the site - {reason}")
        await asyncio.to_thread(rt.state.mark_check_error, tent_config['id'], reason)
        return

    if not result.success:
        rt.logger.error(f"{tent_name}: check failed - {result.error}")
        await asyncio.to_thread(
            _handle_failure, rt, tent_config, result.error or 'unknown error'
        )
        return

    # Notifying is blocking requests with retries and backoff; off the event loop
    # so one slow Telegram call can never stall the other tents or the watchdogs.
    handler = (
        _handle_announcement
        if tent_config['scraper_type'] == 'announcement'
        else _handle_slots
    )
    await asyncio.to_thread(handler, rt, tent_config, result)


async def _tent_loop(rt: Runtime, tent_config: Dict):
    """One independent loop per target — own interval, own jitter."""
    tent_name = tent_config['name']
    interval = _interval_seconds(tent_config)

    # Randomized stagger so all targets don't fire on the same wall-clock tick
    # right after boot.
    await asyncio.sleep(random.uniform(0, min(_BOOT_STAGGER_MAX_SECONDS, interval)))

    while True:
        try:
            await check_tent(rt, tent_config)
        except Exception as e:
            rt.logger.error(f"{tent_name}: tent loop unexpected error - {e}")

        jitter = random.uniform(-_JITTER_FRACTION, _JITTER_FRACTION) * interval
        sleep_for = max(_MIN_SLEEP_SECONDS, interval + jitter)
        rt.logger.debug(f"{tent_name}: sleeping {sleep_for:.1f}s")
        await asyncio.sleep(sleep_for)


async def _blindness_loop(rt: Runtime, tents: List[Dict]):
    """Independent alarm: shout while any target has stopped reading, and drive
    the external dead-man's switch. It can never latch to silence."""
    blind_after = float(rt.config['blind_alert_after_seconds'])
    monitor = BlindnessMonitor(
        blind_after, float(rt.config['blind_realert_interval_seconds'])
    )
    by_id = {tent['id']: tent for tent in tents}
    deadlines = {
        tent['id']: _blind_deadline(tent, blind_after) for tent in tents
    }
    critical = {
        tent['id'] for tent in tents if tent['scraper_type'] in _SLOT_BEARING_TYPES
    }
    started = time.monotonic()

    while True:
        await asyncio.sleep(_BLINDNESS_POLL_SECONDS)
        try:
            ages = {}
            for tent_id in by_id:
                try:
                    ages[tent_id] = rt.state.seconds_since_success(tent_id)
                except Exception as e:
                    rt.logger.error(f"{tent_id}: unreadable state, treating as blind - {e}")
                    ages[tent_id] = None
            now = time.monotonic()
            elapsed = now - started
            # The monitor holds a single threshold, so each age arrives scaled to
            # that target's own deadline — blind means "missed three of its polls".
            # "Never succeeded" only counts as blind once that target has had a
            # full deadline to finish its first check; otherwise every restart
            # would fire a false alarm naming every tent.
            scaled = {}
            for tent_id, age in ages.items():
                deadline = deadlines[tent_id]
                effective = age if age is not None else (
                    elapsed if elapsed < deadline else None
                )
                scaled[tent_id] = (
                    None if effective is None else effective * blind_after / deadline
                )
            blind_ids = monitor.evaluate(scaled, now)

            # The external switch tracks the booking supply only. A permanently
            # dead third-party marketing page would otherwise pin it red and we
            # would lose the one alarm that does not depend on Telegram.
            blind_critical = [t for t in blind_ids if t in critical]
            detail = ", ".join(by_id[t]['name'] for t in blind_critical)
            if blind_critical:
                await asyncio.to_thread(
                    rt.health.ping_failure, f"blind: {detail}"
                )
            else:
                await asyncio.to_thread(
                    rt.health.ping_success,
                    f"{len(by_id) - len(blind_ids)}/{len(by_id)} target(s) reading",
                )

            if blind_ids and monitor.should_alert(now):
                reports = [
                    {
                        'name': by_id[tent_id]['name'],
                        'seconds_since_success': ages[tent_id],
                        'last_error': rt.state.get_tent_state(tent_id).get(
                            'last_error_message'
                        ),
                    }
                    for tent_id in blind_ids
                ]
                known_ages = [ages[t] for t in blind_ids if ages[t] is not None]
                worst = max(known_ages) if known_ages else now - started
                rt.logger.error(f"BLIND: {', '.join(blind_ids)}")
                delivered = await asyncio.to_thread(
                    rt.notifier.send_blind_alert,
                    reports,
                    worst / 60.0,
                    rt.health.enabled,
                )
                if not delivered:
                    # We are blind AND cannot say so. Only the outside switch is left.
                    rt.logger.error("blind alert NOT delivered")
                    await asyncio.to_thread(
                        rt.health.ping_failure, "blind AND Telegram is not delivering"
                    )
                monitor.note_alerted(now)
        except Exception as e:
            rt.logger.error(f"blindness loop error: {e}")


async def _heartbeat_loop(rt: Runtime, tents: List[Dict]):
    """Periodic digest covering every target — so none silently drops out."""
    interval = max(60, int(rt.config['heartbeat_interval_seconds']))
    max_slot_age = float(rt.config['max_slot_age_for_display_seconds'])
    blind_after = float(rt.config['blind_alert_after_seconds'])

    while True:
        await asyncio.sleep(interval)
        try:
            reports = []
            for tent in tents:
                tent_id = tent['id']
                try:
                    tent_state = rt.state.get_tent_state(tent_id)
                    slot_pairs, age = rt.state.get_slot_pairs_with_age(tent_id)
                except Exception as e:
                    rt.logger.error(f"{tent_id}: unreadable state in heartbeat - {e}")
                    tent_state, slot_pairs, age = {}, [], None
                reports.append({
                    'name': tent['name'],
                    'last_check_iso': tent_state.get('last_check'),
                    'seconds_since_success': age,
                    'available_count': len(tent_state.get('available_dates') or []),
                    'consecutive_errors': tent_state.get('consecutive_errors', 0),
                    'slot_pairs': slot_pairs,
                    'interval_seconds': _interval_seconds(tent),
                    'blind_after_seconds': _blind_deadline(tent, blind_after),
                    'last_error': tent_state.get('last_error_message'),
                })
            delivered = await asyncio.to_thread(
                rt.notifier.send_heartbeat, reports, max_slot_age, rt.health.enabled
            )
            if delivered:
                rt.logger.info(f"Heartbeat sent — {len(reports)} target(s)")
            else:
                rt.logger.error("heartbeat NOT delivered")
                await asyncio.to_thread(
                    rt.health.ping_failure, "Telegram is not delivering the heartbeat"
                )
        except Exception as e:
            rt.logger.error(f"heartbeat loop error: {e}")


async def monitor_loop(
    config_loader: ConfigLoader,
    state_manager: StateManager,
    notifier: TelegramNotifier,
    logger: logging.Logger,
    config: Dict[str, Any],
):
    """Main monitoring loop — one task per target plus the two watchdogs."""
    tents = config_loader.get_tents()
    rt = Runtime(
        state=state_manager,
        notifier=notifier,
        health=HealthReporter(config['healthcheck_url'], logger),
        logger=logger,
        config=config,
    )

    # Fail loudly at startup on a malformed number rather than inside a watchdog
    # loop, where the crash would be invisible.
    for key in (
        'blind_alert_after_seconds',
        'blind_realert_interval_seconds',
        'max_slot_age_for_display_seconds',
        'heartbeat_interval_seconds',
    ):
        float(config[key])

    # Prime the API scraper's area-definitions cache from persisted state so we
    # don't burst Cloudflare with N fetches on every restart.
    for tent in tents:
        if tent['scraper_type'] == 'api_fzos':
            primed = prime_areas_cache(
                tent['api_host'], state_manager.get_available_areas(tent['id'])
            )
            if primed:
                logger.info(f"{tent['name']}: primed {primed} area cache entry(ies) from state")

    logger.info(
        f"Starting Oktoberfest Monitor — {len(tents)} target(s), heartbeat every "
        f"{config['heartbeat_interval_seconds']}s"
    )
    notifier.send_notification(_startup_message(tents, rt.health.enabled))

    tasks = [
        asyncio.create_task(_tent_loop(rt, tent), name=f"tent:{tent['id']}")
        for tent in tents
    ]
    tasks.append(asyncio.create_task(_blindness_loop(rt, tents), name="blindness"))
    tasks.append(asyncio.create_task(_heartbeat_loop(rt, tents), name="heartbeat"))

    # Every task loops forever, so the first one to finish has died. Waiting for
    # all of them (asyncio.gather) would mean never noticing.
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        exc = task.exception() if not task.cancelled() else None
        logger.error(f"Task {task.get_name()} exited: {exc!r}")
    for task in pending:
        task.cancel()
    names = ", ".join(t.get_name() for t in done)
    notifier.send_notification(
        f"🚨 <b>MONITOR TASK DIED — RESTARTING</b> 🚨\n\n"
        f"Task(s): <code>{html.escape(names)}</code>\n"
        "The process is exiting so systemd restarts it. If this repeats, the bot "
        "is not watching anything."
    )
    raise RuntimeError(f"monitor task exited: {names}")


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
