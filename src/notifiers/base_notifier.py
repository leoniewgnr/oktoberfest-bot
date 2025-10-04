"""Base notifier interface for sending notifications"""

from abc import ABC, abstractmethod
from typing import Dict, List


class BaseNotifier(ABC):
    """Abstract base class for notification services"""

    @abstractmethod
    def send_notification(self, message: str):
        """Send a notification message"""
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
        dates_text = "\n".join([
            f"• {date['text']}"
            for date in available_dates
        ])

        message = (
            f"🍺🎉 <b>{tent_name.upper()} - DATES AVAILABLE!</b> 🎉🍺\n\n"
            f"Found {len(available_dates)} available date(s):\n"
            f"{dates_text}\n\n"
            f"🔗 Book now: {tent_url}"
        )
        self.send_notification(message)

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
