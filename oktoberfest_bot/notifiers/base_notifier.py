"""Base notifier interface for sending notifications"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..filters import should_alert

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _fmt_iso_local(iso: Optional[str]) -> str:
    """Render an ISO timestamp in Europe/Berlin. Used only for status reports."""
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if ZoneInfo and dt.tzinfo:
        try:
            dt = dt.astimezone(ZoneInfo("Europe/Berlin"))
        except Exception:
            pass
    return dt.strftime("%Y-%m-%d %H:%M %Z").strip()


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

    def _should_suppress_midday(self, time_text: str, date_text: str = "") -> bool:
        """Return True if this slot should be suppressed from notifications.

        Policy (Leonie): suppress ONLY Mon–Fri Mittag. Alert on all Abend (any
        day), Sa/So Mittag, and anything unparseable. Uses the shared filter.

        Backwards-compatible signature: existing callers that pass only the
        time text still work; for time-slot tents, callers should also pass
        date_text so the weekday rule applies.
        """
        return not should_alert(date_text or "", time_text or "")

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
        """Send notification when dates become available.

        Note: This should communicate the *reservation slot* (as shown on the website),
        i.e., date+time if the site exposes it in the option text.
        Weekday-Mittag slots are suppressed (only when both can be inferred from the
        combined option text). Date-only options with no shift indicator pass through.
        """
        filtered = [d for d in available_dates if should_alert(d.get('text', ''))]
        if not filtered:
            return

        import html

        dates_text = "\n".join([f"• {html.escape(str(date.get('text', '')))}" for date in filtered])

        message = (
            f"🍺🎉 <b>{tent_name.upper()} - TABLES AVAILABLE!</b> 🎉🍺\n\n"
            f"Found {len(filtered)} available option(s):\n"
            f"{dates_text}\n\n"
            f"🔗 Book now: {tent_url}"
        )
        message_id = self.send_notification(message)
        self._maybe_react(message_id, "🍺")

    def send_new_dates_added(self, tent_name: str, tent_url: str, new_dates: List[Dict]):
        """Send notification when additional options are added while availability already existed."""
        filtered = [d for d in new_dates if should_alert(d.get('text', ''))]
        if not filtered:
            return

        import html

        dates_text = "\n".join([f"• {html.escape(str(date.get('text', '')))}" for date in filtered])

        message = (
            f"🆕📅 <b>{tent_name.upper()} - NEW OPTIONS ADDED!</b> 📅🆕\n\n"
            f"Newly added option(s) ({len(filtered)}):\n"
            f"{dates_text}\n\n"
            f"🔗 Book now: {tent_url}"
        )
        message_id = self.send_notification(message)
        self._maybe_react(message_id, "📅")

    def send_times_available(self, tent_name: str, tent_url: str, date_text: str, new_times: List[Dict]):
        """Send notification when new time slots become available for an already-available date."""
        # Weekday-aware filter: pass both date and time so Mon–Fri Mittag is
        # suppressed but Sa/So Mittag and any Abend are not.
        filtered = [
            t for t in new_times
            if not self._should_suppress_midday(t.get('text', ''), date_text)
        ]
        if not filtered:
            return

        import html

        safe_date_text = html.escape(str(date_text or ""))
        times_text = "\n".join([f"• {html.escape(str(t.get('text', '')))}" for t in filtered])

        message = (
            f"⏰🎉 <b>{tent_name.upper()} - NEW TIME SLOTS!</b> 🎉⏰\n\n"
            f"Day: <b>{safe_date_text}</b>\n"
            f"New time option(s) found ({len(filtered)}):\n"
            f"{times_text}\n\n"
            f"🔗 Book now: {tent_url}"
        )
        message_id = self.send_notification(message)
        self._maybe_react(message_id, "⏰")

    def send_dates_unavailable(self, tent_name: str):
        """Send notification when dates become unavailable"""
        message = (
            f"❌ <b>{tent_name} - No Longer Available</b>\n\n"
            "The previously available options have been booked.\n"
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

    def send_heartbeat(self, tent_reports: List[Dict[str, Any]]):
        """Periodic 'still alive' digest covering every monitored tent.

        Each report dict: {name, last_check_iso, available_count,
        consecutive_errors, slot_pairs}. Goal: if any tent silently stops
        being polled, it's visible in the digest. Slot pairs are listed so
        the user can see at a glance which date/time combinations the bot is
        currently tracking (incl. ones suppressed by the alert filter).
        """
        import html

        _MAX_SLOTS_PER_TENT = 20  # keep message under Telegram's 4096-char limit

        sections: List[str] = []
        any_problem = False
        for r in tent_reports:
            name = html.escape(str(r.get("name", "?")))
            last = _fmt_iso_local(r.get("last_check_iso"))
            count = int(r.get("available_count", 0))
            errs = int(r.get("consecutive_errors", 0))
            slot_pairs: List[str] = list(r.get("slot_pairs") or [])

            if errs >= 5:
                icon = "⚠️"
                status = f"⚠ {errs} consecutive error(s)"
                any_problem = True
            elif r.get("last_check_iso") is None:
                icon = "❓"
                status = "no successful check yet"
                any_problem = True
            elif count == 0:
                icon = "✅"
                status = "no dates published yet"
            else:
                icon = "✅"
                status = f"{count} date(s) tracked"

            section = (
                f"{icon} <b>{name}</b>\n"
                f"   Last check: {html.escape(last)}\n"
                f"   {status}"
            )

            if slot_pairs:
                shown = slot_pairs[:_MAX_SLOTS_PER_TENT]
                hidden = max(0, len(slot_pairs) - len(shown))
                bullets = "\n".join(f"   • {html.escape(s)}" for s in shown)
                section += "\n" + bullets
                if hidden:
                    section += f"\n   …and {hidden} more"

            sections.append(section)

        header = (
            "📊 <b>Daily Status</b>"
            if not any_problem
            else "📊 <b>Daily Status — issues detected</b>"
        )
        message = header + "\n\n" + "\n\n".join(sections)
        self.send_notification(message)
