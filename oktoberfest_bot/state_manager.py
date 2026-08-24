"""State management for tracking tent availability across monitoring sessions"""

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SEEN_SLOT_RETENTION = timedelta(days=60)
ERROR_MESSAGE_MAX_LEN = 300


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def slot_key(date_value: Any, time_value: Any = None) -> str:
    """Stable identity of a bookable slot. Time is optional: form tents without a
    time select expose the date only."""
    if time_value in (None, ""):
        return str(date_value)
    return f"{date_value}|{time_value}"


def _slot_key(slot: Dict[str, Any]) -> str:
    return slot.get('key') or slot_key(slot.get('date_value'), slot.get('time_value'))


def _slot_areas(slot: Dict[str, Any]) -> List[str]:
    return sorted({str(a).strip() for a in (slot.get('areas') or []) if str(a).strip()})


class StateManager:
    """Manages persistent state for all monitored tents"""

    def __init__(self, state_file: str):
        self.state_file = state_file
        # Handlers run in worker threads (so a Telegram backoff can't stall the
        # other tents), and they all share this one dict.
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> Dict[str, Any]:
        """Load state from file or return empty state"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save(self):
        """Save current state to file atomically.

        Writes to a temp file in the same directory, then os.replace() — so a
        crash or SIGKILL mid-write can never leave a zero-byte state.json.
        """
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".state_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get_tent_state(self, tent_id: str) -> Dict[str, Any]:
        """Get state for a specific tent"""
        with self._lock:
            # A hand-edited or half-migrated state.json can hold a non-dict here;
            # a TypeError from this method would take out both watchdog loops.
            if not isinstance(self.state.get(tent_id), dict):
                self.state[tent_id] = {}
            tent_state = self.state[tent_id]
            return self._with_defaults(tent_state)

    @staticmethod
    def _with_defaults(tent_state: Dict[str, Any]) -> Dict[str, Any]:
        defaults = {
            "last_check": None,
            "last_success": None,
            "last_error": None,
            "last_error_message": None,
            # ISO ts of the last error alert actually sent. Never latches to a
            # bool — that is what silenced the bot for 30 days.
            "last_error_notified_at": None,
            # How many error alerts have gone out for the current failing spell;
            # drives the escalating re-alert interval.
            "error_notify_count": 0,
            # Standing alarm for dates whose time slot we cannot read.
            "times_unknown_notified_at": None,
            "dates_available": False,
            "available_dates": [],
            # Optional: mapping keyed by date value -> {date_text, times:[{value,text}, ...]}
            "available_times": {},
            # Optional: mapping keyed by date value -> {date_text, areas:[{value,text}, ...]}
            "available_areas": {},
            # slot_key -> {first_seen, last_seen, state, gone_since}
            # slot_key -> {first_seen, last_seen, state, gone_since, areas}
            "seen_slots": {},
            "consecutive_errors": 0,
        }
        for key, value in defaults.items():
            if key not in tent_state:
                tent_state[key] = value
        if not isinstance(tent_state.get('seen_slots'), dict):
            tent_state['seen_slots'] = {}
        # Old state files carried a one-shot error_notified bool.
        if tent_state.pop('error_notified', False) and not tent_state['last_error_notified_at']:
            tent_state['last_error_notified_at'] = tent_state['last_check']
        return tent_state

    def update_tent_state(self, tent_id: str, **kwargs):
        """Update state for a specific tent"""
        with self._lock:
            tent_state = self.get_tent_state(tent_id)
            tent_state.update(kwargs)
            self._save()

    def mark_check_success(
        self,
        tent_id: str,
        dates_available: bool,
        available_dates: List[Dict] = None,
        available_times: Dict[str, Dict[str, Any]] = None,
        available_areas: Dict[str, Dict[str, Any]] = None,
    ):
        """Mark a successful check for a tent"""
        now = datetime.now().isoformat()
        self.update_tent_state(
            tent_id,
            last_check=now,
            last_success=now,
            dates_available=dates_available,
            available_dates=available_dates or [],
            available_times=available_times or {},
            available_areas=available_areas or {},
            consecutive_errors=0,
            last_error=None,
            last_error_message=None,
            last_error_notified_at=None,
            error_notify_count=0,
        )

    def mark_check_error(self, tent_id: str, message: str = None):
        """Increment error counter for a tent and record what went wrong"""
        with self._lock:
            tent_state = self.get_tent_state(tent_id)
            self.update_tent_state(
                tent_id,
                last_error=datetime.now().isoformat(),
                last_error_message=(message or '')[:ERROR_MESSAGE_MAX_LEN] or None,
                consecutive_errors=tent_state.get('consecutive_errors', 0) + 1,
            )

    def get_consecutive_errors(self, tent_id: str) -> int:
        """Get number of consecutive errors for a tent"""
        return self.get_tent_state(tent_id).get('consecutive_errors', 0)

    def seconds_since_success(self, tent_id: str) -> Optional[float]:
        """Age of the last successful scrape, or None if this tent never succeeded."""
        last_success = _parse_ts(self.get_tent_state(tent_id).get('last_success'))
        if last_success is None:
            return None
        return (datetime.now() - last_success).total_seconds()

    def is_stale(self, tent_id: str, max_age_seconds: float) -> bool:
        """True when the tent's data is older than max_age_seconds. Never having
        succeeded counts as stale."""
        age = self.seconds_since_success(tent_id)
        return age is None or age > max_age_seconds

    def is_dates_available(self, tent_id: str) -> bool:
        """Check if dates are currently available for a tent"""
        return self.get_tent_state(tent_id).get('dates_available', False)

    def get_available_areas(self, tent_id: str) -> Dict[str, Dict[str, Any]]:
        """Get mapping of available areas per date (only populated by API scrapers)."""
        return self.get_tent_state(tent_id).get('available_areas', {})

    def get_slot_pairs(self, tent_id: str) -> List[str]:
        """Return human-readable "date [– time] [— N Bereich(e)]" strings for every
        slot tracked. Used by the heartbeat digest. Area counts only — full lists
        live in the new-area alert messages. Keeps the daily digest under
        Telegram's 4096-char limit even with 60+ tracked pairs.
        """
        state = self.get_tent_state(tent_id)
        dates = state.get('available_dates') or []
        times_by_date = state.get('available_times') or {}
        areas_by_date = state.get('available_areas') or {}

        pairs: List[str] = []
        for date in dates:
            date_value = date.get('value')
            date_text = (date.get('text') or '').strip()
            times_info = times_by_date.get(date_value) if date_value is not None else None
            times = (times_info or {}).get('times') if times_info else None
            areas_info = areas_by_date.get(date_value) if date_value is not None else None
            areas = (areas_info or {}).get('areas') if areas_info else None
            area_suffix = ""
            if areas:
                n = sum(1 for a in areas if (a.get('text') or '').strip())
                if n:
                    area_suffix = f"  —  {n} Bereich" + ("" if n == 1 else "e")
            if times:
                for t in times:
                    t_text = (t.get('text') or '').strip()
                    base = f"{date_text} – {t_text}" if t_text else date_text
                    pairs.append(base + area_suffix)
            else:
                pairs.append(date_text + area_suffix)
        return pairs

    def get_slot_pairs_with_age(self, tent_id: str) -> Tuple[List[str], Optional[float]]:
        """Slot pairs plus the age of the data, so the heartbeat can refuse to
        present a stale list as current."""
        return self.get_slot_pairs(tent_id), self.seconds_since_success(tent_id)

    def set_error_notified_at(self, tent_id: str, ts: datetime = None):
        """Record when an error alert was sent"""
        self.update_tent_state(
            tent_id, last_error_notified_at=(ts or datetime.now()).isoformat()
        )

    def should_renotify_error(
        self, tent_id: str, interval_seconds: float, now: datetime = None
    ) -> bool:
        """True when no error alert has gone out yet, or the interval has elapsed
        since the last one. Silence must never be permanent."""
        return self.should_renotify(
            tent_id, 'last_error_notified_at', interval_seconds, now
        )

    def should_renotify(
        self, tent_id: str, field: str, interval_seconds: float, now: datetime = None
    ) -> bool:
        """Generic "has the re-alert interval elapsed" gate for a timestamp field."""
        last = _parse_ts(self.get_tent_state(tent_id).get(field))
        if last is None:
            return True
        return ((now or datetime.now()) - last).total_seconds() >= interval_seconds

    def mark_notified(self, tent_id: str, field: str, ts: datetime = None):
        self.update_tent_state(tent_id, **{field: (ts or datetime.now()).isoformat()})

    def diff_slots(
        self, tent_id: str, current_slots: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Classify the scraped slots against what we have seen before. Read-only:
        commit_slots() persists, so a notifier crash cannot swallow the alert.

        "returned" is the cancellation signal — a slot that was gone for at least
        one scrape and is bookable again.
        """
        seen = self.get_tent_state(tent_id).get('seen_slots') or {}
        result: Dict[str, List[Dict[str, Any]]] = {
            "new": [],
            "returned": [],
            "state_changed": [],
            "area_added": [],
        }
        for slot in current_slots:
            entry = seen.get(_slot_key(slot))
            if not isinstance(entry, dict):
                result["new"].append(slot)
                continue
            if entry.get('gone_since'):
                result["returned"].append(slot)
            if slot.get('state') != entry.get('state'):
                result["state_changed"].append(slot)
            # A Bereich appearing inside an already-published slot IS the Storno
            # signal on the API tents — nothing else about the slot changes.
            # 'areas' missing means we never recorded any, so we cannot tell what
            # is new; the commit below records it for next cycle.
            if 'areas' in entry:
                added = [a for a in _slot_areas(slot) if a not in (entry.get('areas') or [])]
                if added:
                    enriched = dict(slot)
                    enriched['new_areas'] = added
                    result["area_added"].append(enriched)
        return result

    def commit_slots(
        self,
        tent_id: str,
        current_slots: List[Dict[str, Any]],
        skip_keys: Optional[Iterable[str]] = None,
    ):
        """Persist the scraped slots: refresh what is present, flag what vanished.

        skip_keys are left exactly as they were, so a slot whose alert was never
        actually delivered gets re-classified — and re-alerted — next cycle.
        """
        with self._lock:
            tent_state = self.get_tent_state(tent_id)
            seen = tent_state['seen_slots']
            now = datetime.now()
            now_iso = now.isoformat()
            skipped = {str(k) for k in (skip_keys or [])}

            current_keys = set()
            for slot in current_slots:
                key = _slot_key(slot)
                current_keys.add(key)
                if key in skipped:
                    continue
                entry = seen.get(key)
                if not isinstance(entry, dict):
                    seen[key] = {
                        "first_seen": now_iso,
                        "last_seen": now_iso,
                        "date_text": slot.get('date_text') or '',
                        "time_text": slot.get('time_text') or '',
                        "state": slot.get('state'),
                        "gone_since": None,
                        "areas": _slot_areas(slot),
                    }
                else:
                    entry['last_seen'] = now_iso
                    entry['date_text'] = slot.get('date_text') or entry.get('date_text') or ''
                    entry['time_text'] = slot.get('time_text') or entry.get('time_text') or ''
                    entry['state'] = slot.get('state')
                    entry['gone_since'] = None
                    entry['areas'] = _slot_areas(slot)

            for key, entry in seen.items():
                if not isinstance(entry, dict):
                    continue
                if key not in current_keys and not entry.get('gone_since'):
                    entry['gone_since'] = now_iso

            cutoff = now - SEEN_SLOT_RETENTION
            for key in list(seen):
                entry = seen[key]
                last_seen = _parse_ts(entry.get('last_seen')) if isinstance(entry, dict) else None
                if last_seen is not None and last_seen < cutoff:
                    del seen[key]

            self._save()
