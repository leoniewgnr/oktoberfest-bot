"""Base notifier interface for sending notifications"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


class BaseNotifier(ABC):
    """Abstract base class for notification services"""

    def _now_local(self) -> datetime:
        """Best-effort local time for notification policies."""
        if ZoneInfo is None:
            return datetime.utcnow()
        try:
            return datetime.now(ZoneInfo('Europe/Berlin'))
        except Exception:
            return datetime.utcnow()

    def _is_midday_slot(self, time_text: str) -> Optional[bool]:
        """Return True if the time_text clearly indicates a midday/lunch slot.

        If uncertain, return None.
        """
        t = (time_text or '').strip().lower()
        if not t:
            return None

        # Clear labels
        if 'mittag' in t or 'lunch' in t:
            return True
        if 'abend' in t or 'dinner' in t:
            return False

        # Try parsing leading HH:MM
        import re
        m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
        if not m:
            return None
        hour = int(m.group(1))

        # Treat early afternoon as 'midday' for suppression purposes.
        if 10 <= hour <= 15:
            return True
        if hour >= 18:
            return False

        return None

    def _should_suppress_midday(self, time_text: str) -> bool:
        """Mon–Thu: suppress midday slot notifications; if unsure, do not suppress."""
        now = self._now_local()
        weekday = now.weekday()  # Mon=0 .. Sun=6
        if weekday > 3:
            return False

        is_midday = self._is_midday_slot(time_text)
        return is_midday is True

    @abstractmethod
    def send_notification(self, message: str) -> Any:
        """Send a notification message."""
        raise NotImplementedError

    def _maybe_react(self, message_id: Any, emoji: str):
        """Best-effort reaction helper for notifiers that support it."""
        if message_id is None:
            return
        react_fn = getattr(self, 'react_to_message', None)
        if callable(react_fn):
            try:
                react_fn(message_id, emoji)
            except Exception:
                pass

    def send_startup_notification(self, tent_names: List[str], check_interval: int):
        """Send notification when monitoring starts"""
        tents_list = "\n".join([f"• {name}" for name in tent_names])
        message = (
            "🚀 <b>Oktoberfest Monitor Started</b>\n\n"
            f"Monitoring {len(tent_names)} tent(s):\n"
            f"{tents_list}\n\n"
            f"Check interval: {check_interval} seconds"
        )
        self.send_notification(message)

    def send_dates_available(self, tent_name: str, tent_url: str, available_dates: List[Dict]):
        """Send notification when dates become available"""
        dates_text = "\n".join([f"• {date['text']}" for date in available_dates])

        message = (
            f"🍺🎉 <b>{tent_name.upper()} - DATES AVAILABLE!</b> 🎉🍺\n\n"
            f"Found {len(available_dates)} available date(s):\n"
            f"{dates_text}\n\n"
            f"🔗 Book now: {tent_url}"
        )
        message_id = self.send_notification(message)
        self._maybe_react(message_id, "🍺")

    def send_new_dates_added(self, tent_name: str, tent_url: str, new_dates: List[Dict]):
        """Send notification when additional dates are added while dates were already available."""
        dates_text = "\n".join([f"• {date['text']}" for date in new_dates])

        message = (
            f"🆕📅 <b>{tent_name.upper()} - NEW DATES ADDED!</b> 📅🆕\n\n"
            f"Newly added date(s) ({len(new_dates)}):\n"
            f"{dates_text}\n\n"
            f"🔗 Book now: {tent_url}"
        )
        message_id = self.send_notification(message)
        self._maybe_react(message_id, "📅")

    def send_times_available(self, tent_name: str, tent_url: str, date_text: str, new_times: List[Dict]):
        """Send notification when new time slots become available for an already-available date.

        Policy: Mon–Thu, suppress clear midday/lunch-only slot notifications. If we're unsure,
        we send the notification ("better to notify than hide").
        """
        filtered = [t for t in new_times if not self._should_suppress_midday(t.get('text', ''))]
        if not filtered:
            return

        times_text = "\n".join([f"• {t['text']}" for t in filtered])

        message = (
            f"⏰🎉 <b>{tent_name.upper()} - NEW TIME SLOTS!</b> 🎉⏰\n\n"
            f"Date: <b>{date_text}</b>\n"
            f"New time option(s) found ({len(filtered)}):\n"
            f"{times_text}\n\n"
            f"🔗 Book now: {tent_url}"
        )
        message_id = self.send_notification(message)
        self._maybe_react(message_id, "⏰")

    def send_dates_unavailable(self, tent_name: str):
        """Send notification when dates become unavailable"""
        message = (
            f"❌ <b>{tent_name} - Dates No Longer Available</b>\n\n"
            "The previously available dates have been booked.\n"
            "Will continue monitoring..."
        )
        self.send_notification(message)

    def send_error_notification(self, tent_name: str, error_msg: str, error_count: int):
        """Send notification about monitoring errors"""
        import html

        escaped_error = html.escape(error_msg)

        message = (
            f"⚠️ <b>{tent_name} - Monitor Error</b>\n\n"
            f"Failed to check reservation page {error_count} time(s):\n"
            f"<code>{escaped_error[:500]}</code>\n\n"
            "Monitor will continue trying..."
        )
        self.send_notification(message)

    def send_recovery_notification(self, tent_name: str):
        """Send notification when monitoring recovers from errors"""
        message = (
            f"✅ <b>{tent_name} - Monitor Recovered</b>\n\n"
            "Successfully reconnected to reservation page.\n"
            "Monitoring continues normally."
        )
        self.send_notification(message)
