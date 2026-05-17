"""State management for tracking tent availability across monitoring sessions"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any


class StateManager:
    """Manages persistent state for all monitored tents"""

    def __init__(self, state_file: str):
        self.state_file = state_file
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
        if tent_id not in self.state:
            self.state[tent_id] = {
                "last_check": None,
                "dates_available": False,
                "available_dates": [],
                # Optional: mapping keyed by date value -> {date_text, times:[{value,text}, ...]}
                "available_times": {},
                # Optional: mapping keyed by date value -> {date_text, areas:[{value,text}, ...]}
                "available_areas": {},
                "consecutive_errors": 0,
                "error_notified": False,
            }
        # Backwards compat for old state files
        if 'available_times' not in self.state[tent_id]:
            self.state[tent_id]['available_times'] = {}
        if 'available_areas' not in self.state[tent_id]:
            self.state[tent_id]['available_areas'] = {}
        return self.state[tent_id]

    def update_tent_state(self, tent_id: str, **kwargs):
        """Update state for a specific tent"""
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
        self.update_tent_state(
            tent_id,
            last_check=datetime.now().isoformat(),
            dates_available=dates_available,
            available_dates=available_dates or [],
            available_times=available_times or {},
            available_areas=available_areas or {},
            consecutive_errors=0,
            error_notified=False,
        )

    def mark_check_error(self, tent_id: str):
        """Increment error counter for a tent"""
        tent_state = self.get_tent_state(tent_id)
        self.update_tent_state(
            tent_id,
            consecutive_errors=tent_state.get('consecutive_errors', 0) + 1,
        )

    def get_consecutive_errors(self, tent_id: str) -> int:
        """Get number of consecutive errors for a tent"""
        return self.get_tent_state(tent_id).get('consecutive_errors', 0)

    def is_dates_available(self, tent_id: str) -> bool:
        """Check if dates are currently available for a tent"""
        return self.get_tent_state(tent_id).get('dates_available', False)

    def get_available_dates(self, tent_id: str) -> List[Dict]:
        """Get list of available dates for a tent"""
        return self.get_tent_state(tent_id).get('available_dates', [])

    def get_available_times(self, tent_id: str) -> Dict[str, Dict[str, Any]]:
        """Get mapping of available times per date (if configured)."""
        return self.get_tent_state(tent_id).get('available_times', {})

    def get_available_areas(self, tent_id: str) -> Dict[str, Dict[str, Any]]:
        """Get mapping of available areas per date (only populated by API scrapers)."""
        return self.get_tent_state(tent_id).get('available_areas', {})

    def get_slot_pairs(self, tent_id: str) -> List[str]:
        """Return human-readable "date [– time] [— areas: ...]" strings for every
        slot tracked. Used by the heartbeat digest. Includes all dates the
        scraper saw regardless of the alert filter.
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
                labels = [
                    (a.get('text') or '').strip() for a in areas
                    if (a.get('text') or '').strip()
                ]
                if labels:
                    area_suffix = "  —  " + ", ".join(labels)
            if times:
                for t in times:
                    t_text = (t.get('text') or '').strip()
                    base = f"{date_text} – {t_text}" if t_text else date_text
                    pairs.append(base + area_suffix)
            else:
                pairs.append(date_text + area_suffix)
        return pairs

    def is_error_notified(self, tent_id: str) -> bool:
        """Check if error notification has been sent for current error state"""
        return self.get_tent_state(tent_id).get('error_notified', False)

    def mark_error_notified(self, tent_id: str):
        """Mark that error notification has been sent"""
        self.update_tent_state(tent_id, error_notified=True)
