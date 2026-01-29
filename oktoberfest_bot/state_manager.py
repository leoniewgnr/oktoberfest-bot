"""State management for tracking tent availability across monitoring sessions"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional


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
        """Save current state to file"""
        # Ensure directory exists
        Path(self.state_file).parent.mkdir(parents=True, exist_ok=True)

        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)

    def get_tent_state(self, tent_id: str) -> Dict[str, Any]:
        """Get state for a specific tent"""
        if tent_id not in self.state:
            self.state[tent_id] = {
                "last_check": None,
                "dates_available": False,
                "available_dates": [],
                "consecutive_errors": 0,
                "error_notified": False
            }
        return self.state[tent_id]

    def update_tent_state(self, tent_id: str, **kwargs):
        """Update state for a specific tent"""
        tent_state = self.get_tent_state(tent_id)
        tent_state.update(kwargs)
        self._save()

    def mark_check_success(self, tent_id: str, dates_available: bool, available_dates: List[Dict] = None):
        """Mark a successful check for a tent"""
        self.update_tent_state(
            tent_id,
            last_check=datetime.now().isoformat(),
            dates_available=dates_available,
            available_dates=available_dates or [],
            consecutive_errors=0,
            error_notified=False
        )

    def mark_check_error(self, tent_id: str):
        """Increment error counter for a tent"""
        tent_state = self.get_tent_state(tent_id)
        self.update_tent_state(
            tent_id,
            consecutive_errors=tent_state.get('consecutive_errors', 0) + 1
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

    def is_error_notified(self, tent_id: str) -> bool:
        """Check if error notification has been sent for current error state"""
        return self.get_tent_state(tent_id).get('error_notified', False)

    def mark_error_notified(self, tent_id: str):
        """Mark that error notification has been sent"""
        self.update_tent_state(tent_id, error_notified=True)
