"""Base notifier interface for sending notifications

Every send_* returns True only when everything it had to say actually reached
Telegram (or there was nothing to say). The orchestrator commits slot state on
that answer, so a dropped message means "re-alert next cycle" instead of
"silently forget this slot".
"""

import html
import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from ..filters import is_weekend_evening, parse_weekday, should_alert, weekend_class

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_TELEGRAM_LIMIT = 4096
_MAX_SLOTS_PER_TENT = 20
_MAX_DIFF_LINES = 15


def _esc(value: Any) -> str:
    """Telegram-HTML-escape any scraped or remote text before it goes in a message."""
    return html.escape(str(value if value is not None else ""))


def _esc_cut(value: Any, limit: int) -> str:
    """Truncate first, escape second — slicing escaped text splits `&lt;` in half
    and Telegram answers 400, which used to drop the whole alarm."""
    return _esc(str(value if value is not None else "")[:limit])


def _cap(message: str) -> str:
    """Trim to Telegram's limit on a line boundary, so no HTML tag is cut in half.

    Only for messages whose length is bounded by construction — anything holding
    a list of slots is chunked instead, because dropping a line drops a slot.
    """
    if len(message) <= _TELEGRAM_LIMIT:
        return message
    head = message[: _TELEGRAM_LIMIT - 40]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]
    return head + "\n… (truncated)"


def _chunk_lines(lines: List[str], overhead: int) -> List[List[str]]:
    """Group lines so each group plus overhead fits one Telegram message."""
    budget = max(200, _TELEGRAM_LIMIT - overhead)
    groups: List[List[str]] = []
    current: List[str] = []
    size = 0
    for line in lines:
        cost = len(line) + 1
        if current and size + cost > budget:
            groups.append(current)
            current = []
            size = 0
        current.append(line)
        size += cost
    if current:
        groups.append(current)
    return groups or [[]]


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


def _fmt_age(seconds: Optional[float]) -> str:
    """Age as "34 s" / "4 min" / "3 h" / "29 days"; "never" for a missing timestamp."""
    if seconds is None:
        return "never"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)} s"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{int(round(minutes))} min"
    hours = minutes / 60.0
    if hours < 36:
        return f"{int(round(hours))} h"
    return f"{int(round(hours / 24.0))} days"


_is_weekend_evening = is_weekend_evening
_weekend_class = weekend_class

_CLASS_RANK = {"evening": 0, "unknown": 1, "daytime": 2}


def _strongest_class(pairs: "list[tuple]") -> "str | None":
    """Pick the most urgent weekend class across (date_text, time_text) pairs:
    evening > unknown > daytime; None if nothing is a weekend."""
    best = None
    for date_text, time_text in pairs:
        k = weekend_class(str(date_text or ""), str(time_text or ""))
        if k and (best is None or _CLASS_RANK[k] < _CLASS_RANK[best]):
            best = k
    return best


def _weekend_header(tent_name: str, calm: str, klass: "str | None") -> str:
    """Honest weekend header. Only a shift we actually read as Abend earns the
    EVENING banner; an unreadable shift says so instead of overclaiming; a
    weekend daytime slot is still flagged but not dressed up as an evening."""
    name = _esc(tent_name.upper())
    if klass == "evening":
        return f"🔴🍺 <b>WEEKEND EVENING - {name} - BOOK NOW</b> 🍺🔴"
    if klass == "unknown":
        return f"🔴❓ <b>WEEKEND SLOT (SHIFT UNKNOWN) - {name} - CHECK NOW</b> ❓🔴"
    if klass == "daytime":
        return f"🟠 <b>WEEKEND (not evening) - {name}</b>"
    return calm


def _booking_footer(tent_url: str, slot_key: Optional[str] = None) -> str:
    """Neither tent platform exposes a per-slot URL, so the booking page is the
    most specific link there is and the slot key goes in as matchable text."""
    footer = f"🔗 Book now: {_esc(tent_url)}"
    if slot_key:
        footer += f"\nSlot-ID: <code>{_esc(slot_key)}</code>"
    return footer


def _format_slot_line(
    date: Dict,
    areas_by_date_value: Optional[Dict[str, List[Dict]]] = None,
    extra_text: str = "",
) -> str:
    """One slot line: weekend evenings marked, Bereiche inlined when known, and
    the slot key appended so she can match it in the booking picker."""
    text = date.get("text", "")
    klass = weekend_class(str(text), extra_text)
    marker = {"evening": "🔴 ", "unknown": "🔴 ", "daytime": "🟠 "}.get(klass, "")
    line = f"• {marker}{_esc(text)}"

    value = date.get("value")
    areas = (areas_by_date_value or {}).get(value) if value is not None else None
    labels = [
        (a.get("text") or "").strip()
        for a in (areas or [])
        if (a.get("text") or "").strip()
    ]
    if labels:
        line += "  —  " + _esc(", ".join(labels))
    # The key is only worth showing when it is an opaque booking id (API uid);
    # for the browser tents it is just the ISO date already shown in the text.
    if value and _is_useful_key(str(value), str(text)):
        line += f"  <code>{_esc(value)}</code>"
    return line


def _is_useful_key(value: str, text: str) -> bool:
    if not value or value in text:
        return False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(\|.*)?", value):  # ISO date, ± a shift suffix
        return False
    return True


def _urgency_rank(text: str, extra_text: str = "") -> int:
    """0 = Fri/Sat/Sun evening, 1 = weekday we could not parse, 2 = the rest.

    The unparseable class is the deliberate safety net, so it must never sort
    below known weekday noise.
    """
    combined = " ".join(t for t in (text, extra_text) if t).strip()
    if _is_weekend_evening(combined):
        return 0
    return 1 if parse_weekday(combined) is None else 2


def _weekend_first(items: Iterable[Dict], extra_text: str = "") -> List[Dict]:
    """Stable sort putting Fri/Sat/Sun evening slots first, then the slots whose
    weekday we could not determine."""
    return sorted(items, key=lambda d: _urgency_rank(str(d.get("text", "")), extra_text))


class BaseNotifier(ABC):
    """Abstract base class for notification services"""

    def _should_suppress_midday(self, time_text: str, date_text: str = "") -> bool:
        """True only for Mon–Thu daytime slots (Mittag, Vormittag, Frühschoppen).

        The weekday is parsed here and handed to the filter explicitly, so
        Fri/Sat/Sun — and any date we cannot parse — can never be suppressed.
        """
        combined = f"{date_text or ''} {time_text or ''}".strip()
        return not should_alert(date_text or "", time_text or "", parse_weekday(combined))

    @abstractmethod
    def send_notification(self, message: str) -> Any:
        """Send a notification. Non-None means Telegram accepted it."""
        raise NotImplementedError

    def _send(self, message: str) -> bool:
        """Send one bounded message. False means it never arrived."""
        return self.send_notification(_cap(message)) is not None

    def _send_listing(
        self, header: str, lines: List[str], footer: str
    ) -> bool:
        """Send a slot listing across as many messages as it takes.

        Truncating a listing deletes slot lines, and the truncated tail is exactly
        where the least-classifiable slots sit — so it is chunked, never cut.
        """
        groups = _chunk_lines(lines, len(header) + len(footer) + 80)
        delivered = True
        for index, group in enumerate(groups, start=1):
            label = f"  (part {index}/{len(groups)})" if len(groups) > 1 else ""
            body = "\n".join(group)
            if not self._send(f"{header}{label}\n\n{body}\n\n{footer}"):
                delivered = False
        return delivered

    def send_dates_available(
        self,
        tent_name: str,
        tent_url: str,
        available_dates: List[Dict],
        areas_by_date_value: Dict[str, List[Dict]] = None,
    ) -> bool:
        """Alert on the first availability seen for a tent. The slot text is what
        the site shows, i.e. date + time when the site exposes both."""
        filtered = _weekend_first(
            d for d in available_dates
            if not self._should_suppress_midday("", d.get('text', ''))
        )
        if not filtered:
            return True

        klass = _strongest_class([(d.get('text', ''), '') for d in filtered])
        header = _weekend_header(
            tent_name,
            f"🍺🎉 <b>{_esc(tent_name.upper())} - TABLES AVAILABLE!</b> 🎉🍺",
            klass,
        )
        header += f"\n\nFound {len(filtered)} available option(s):"
        lines = [_format_slot_line(d, areas_by_date_value) for d in filtered]
        return self._send_listing(header, lines, _booking_footer(tent_url))

    def send_new_dates_added(
        self,
        tent_name: str,
        tent_url: str,
        new_dates: List[Dict],
        areas_by_date_value: Dict[str, List[Dict]] = None,
    ) -> bool:
        """Send notification when additional options are added while availability already existed."""
        filtered = _weekend_first(
            d for d in new_dates
            if not self._should_suppress_midday("", d.get('text', ''))
        )
        if not filtered:
            return True

        klass = _strongest_class([(d.get('text', ''), '') for d in filtered])
        header = _weekend_header(
            tent_name,
            f"🆕📅 <b>{_esc(tent_name.upper())} - NEW OPTIONS ADDED!</b> 📅🆕",
            klass,
        )
        header += f"\n\nNewly added option(s) ({len(filtered)}):"
        lines = [_format_slot_line(d, areas_by_date_value) for d in filtered]
        return self._send_listing(header, lines, _booking_footer(tent_url))

    def send_times_available(
        self,
        tent_name: str,
        tent_url: str,
        date_text: str,
        new_times: List[Dict],
        current_areas: List[Dict] = None,
        slot_key: Optional[str] = None,
    ) -> bool:
        """Send notification when new time slots become available for an
        already-available date. Includes the currently-tracked Bereich list
        for that date if the scraper provides it (API tents)."""
        filtered = _weekend_first(
            (t for t in new_times
             if not self._should_suppress_midday(t.get('text', ''), date_text)),
            date_text,
        )
        if not filtered:
            return True

        klass = _strongest_class([(date_text, t.get('text', '')) for t in filtered])
        _mk = {"evening": "🔴 ", "unknown": "🔴 ", "daytime": "🟠 "}
        times_text = "\n".join(
            "• "
            + _mk.get(weekend_class(date_text, str(t.get('text', ''))), "")
            + _esc(t.get('text', ''))
            for t in filtered
        )

        area_line = ""
        if current_areas:
            area_labels = [
                (a.get('text') or '').strip()
                for a in current_areas
                if (a.get('text') or '').strip()
            ]
            if area_labels:
                area_line = f"Bereiche: <b>{_esc(', '.join(area_labels))}</b>\n"

        header = _weekend_header(
            tent_name,
            f"⏰🎉 <b>{_esc(tent_name.upper())} - NEW TIME SLOTS!</b> 🎉⏰",
            klass,
        )
        message = (
            f"{header}\n\n"
            f"Day: <b>{_esc(date_text)}</b>\n"
            f"{area_line}"
            f"New time option(s) found ({len(filtered)}):\n"
            f"{times_text}\n\n"
            f"{_booking_footer(tent_url, slot_key)}"
        )
        return self._send(message)

    def send_areas_available(
        self,
        tent_name: str,
        tent_url: str,
        date_text: str,
        time_text: str,
        new_areas: List[Dict],
        slot_key: Optional[str] = None,
    ) -> bool:
        """A Bereich appeared inside an already-published slot. On the API tents
        that is the only visible trace of a Storno freeing a table."""
        if not new_areas:
            return True
        if self._should_suppress_midday(time_text or "", date_text):
            return True

        klass = weekend_class(date_text, time_text)
        areas_text = "\n".join(
            f"• {_esc(a.get('text', ''))}" for a in new_areas
        )
        slot_line = (
            f"Day: <b>{_esc(date_text)}</b> – <b>{_esc(time_text)}</b>"
            if time_text
            else f"Day: <b>{_esc(date_text)}</b>"
        )
        header = _weekend_header(
            tent_name,
            f"📍🆕 <b>{_esc(tent_name.upper())} - NEW BEREICH(E)!</b> 🆕📍",
            klass,
        )

        message = (
            f"{header}\n\n"
            f"{slot_line}\n"
            f"New area(s) ({len(new_areas)}):\n"
            f"{areas_text}\n\n"
            f"{_booking_footer(tent_url, slot_key)}"
        )
        return self._send(message)

    def send_slot_returned(
        self,
        tent_name: str,
        tent_url: str,
        date_text: str,
        time_text: str = "",
        slot_key: Optional[str] = None,
        gone_for_seconds: Optional[float] = None,
    ) -> bool:
        """A slot that had vanished is back: a Storno, not a new publication.
        These get taken within minutes, so it is worded as urgently as a new one."""
        if self._should_suppress_midday(time_text or "", date_text):
            return True

        klass = weekend_class(date_text, time_text)
        name = _esc(tent_name.upper())
        if klass == "evening":
            header = f"🔴♻️ <b>WEEKEND EVENING FREED UP - {name} - BOOK NOW</b> ♻️🔴"
        elif klass == "unknown":
            header = f"🔴♻️ <b>WEEKEND TABLE FREED UP (SHIFT UNKNOWN) - {name} - CHECK NOW</b> ♻️🔴"
        elif klass == "daytime":
            header = f"🟠♻️ <b>WEEKEND (not evening) TABLE FREED UP - {name}</b> ♻️🟠"
        else:
            header = f"♻️🍺 <b>{name} - TABLE FREED UP</b> 🍺♻️"
        slot_line = (
            f"Slot: <b>{_esc(date_text)}</b> – <b>{_esc(time_text)}</b>"
            if time_text
            else f"Slot: <b>{_esc(date_text)}</b>"
        )
        gone_line = (
            f"Was gone for {_fmt_age(gone_for_seconds)} before reappearing.\n"
            if gone_for_seconds is not None
            else ""
        )

        message = (
            f"{header}\n\n"
            "This slot had disappeared and is bookable again — somebody cancelled "
            "(Storno). It is not a new publication.\n\n"
            f"{slot_line}\n"
            f"{gone_line}\n"
            f"{_booking_footer(tent_url, slot_key)}"
        )
        return self._send(message)

    def send_announcement_changed(
        self,
        name: str,
        url: str,
        keywords_found: List[str],
        diff_lines: List[str],
        baseline: bool = False,
    ) -> bool:
        """A watched marketing page changed — or was read for the first time.

        Offline Münchner-Kontingent windows are announced here in prose only,
        often weeks before any booking route, so the very first read is reported
        too: silently baselining it would hide a window that is already open.
        """
        keywords = [str(k).strip() for k in (keywords_found or []) if str(k).strip()]
        lines: List[str] = []
        if baseline:
            lines.append(f"📣 <b>{_esc(name.upper())} - NOW WATCHING THIS PAGE</b>")
            lines.append("")
            lines.append(
                "First read of this announcement page. Check it once by hand — a "
                "window may already be open; from here on you only get changes."
            )
            lines.append("")
        if keywords:
            if not baseline:
                lines.append(
                    f"🔴📣 <b>{_esc(name.upper())} - ANNOUNCEMENT: "
                    f"{_esc(', '.join(keywords).upper())}</b>"
                )
                lines.append("")
            lines.append(f"High-signal keywords: <b>{_esc(', '.join(keywords))}</b>")
        elif not baseline:
            lines.append(f"📣 <b>{_esc(name.upper())} - page text changed</b>")
            lines.append("")

        all_diff = [str(d) for d in (diff_lines or []) if str(d).strip()]
        shown = all_diff[:_MAX_DIFF_LINES]
        if shown:
            lines.append("Changed lines:")
            lines += [f"<code>{_esc_cut(d, 300)}</code>" for d in shown]
            hidden = len(all_diff) - len(shown)
            if hidden > 0:
                lines.append(f"…and {hidden} more changed line(s)")

        lines += ["", f"🔗 {_esc(url)}"]
        return self._send("\n".join(lines))

    def send_dates_unavailable(self, tent_name: str) -> bool:
        """Send notification when dates become unavailable"""
        message = (
            f"❌ <b>{_esc(tent_name)} - No Longer Available</b>\n\n"
            "The previously available options have been booked.\n"
            "Will continue monitoring..."
        )
        return self._send(message)

    def send_error_notification(
        self, tent_name: str, error_msg: str, error_count: int
    ) -> bool:
        """Send notification about monitoring errors"""
        message = (
            f"⚠️ <b>{_esc(tent_name)} - Monitor Error</b>\n\n"
            f"Failed to check reservation page {error_count} time(s):\n"
            f"<code>{_esc_cut(error_msg, 500)}</code>\n\n"
            "Monitor will continue trying..."
        )
        return self._send(message)

    def send_recovery_notification(self, tent_name: str) -> bool:
        """Send notification when monitoring recovers from errors"""
        message = (
            f"✅ <b>{_esc(tent_name)} - Monitor Recovered</b>\n\n"
            "Successfully reconnected to reservation page.\n"
            "Monitoring continues normally."
        )
        return self._send(message)

    def send_blind_alert(
        self,
        tent_reports: List[Dict[str, Any]],
        minutes_blind: float,
        watchdog_enabled: bool = True,
    ) -> bool:
        """The bot cannot see. Meant to be re-sent while the condition lasts —
        a one-shot latched error notification is what hid 30 days of blindness.

        Each report dict: {name, seconds_since_success, last_error}.
        """
        lines = [
            "🚨🚨 <b>I CANNOT SEE - MONITOR IS BLIND</b> 🚨🚨",
            "",
            f"No successful check for <b>{_fmt_age(float(minutes_blind) * 60)}</b>. "
            "Weekend evening slots could be published and gone again right now "
            "without any alert.",
            "",
        ]
        for r in tent_reports:
            lines.append(
                f"• <b>{_esc(r.get('name', '?'))}</b> — last success "
                f"{_fmt_age(r.get('seconds_since_success'))} ago"
            )
            last_error = r.get('last_error')
            if last_error:
                lines.append(f"  <code>{_esc_cut(last_error, 200)}</code>")

        if not watchdog_enabled:
            lines += ["", "⛔ EXTERNAL WATCHDOG DISABLED (healthcheck_url is empty)."]
        lines += [
            "",
            "Check the box: <code>systemctl status oktoberfest-bot</code>",
        ]
        return self._send("\n".join(lines))

    def send_heartbeat(
        self,
        tent_reports: List[Dict[str, Any]],
        max_slot_age_for_display_seconds: float = 3600.0,
        watchdog_enabled: bool = True,
    ) -> bool:
        """Periodic digest covering every monitored tent.

        Each report dict: {name, last_check_iso, seconds_since_success,
        available_count, consecutive_errors, slot_pairs, interval_seconds,
        blind_after_seconds, last_error}.

        Freshness is judged per tent against the same deadline the blindness loop
        uses, and slot data older than max_slot_age_for_display_seconds is
        withheld instead of printed: a stale slot list reads exactly like a
        healthy one, which is what made 30 days of blindness look alive.
        """
        sections: List[str] = []
        blind: List[str] = []

        for r in tent_reports:
            name = str(r.get("name", "?"))
            age = r.get("seconds_since_success")
            errs = int(r.get("consecutive_errors", 0))
            count = int(r.get("available_count", 0))
            slot_pairs: List[str] = list(r.get("slot_pairs") or [])

            deadline = float(
                r.get("blind_after_seconds")
                or (float(r.get("interval_seconds") or 0) * 3)
                or max_slot_age_for_display_seconds
            )
            if age is None or age > deadline:
                blind.append(name)
                icon, status = "🚨", "NOT SEEING THIS TENT"
            elif errs >= 5:
                icon, status = "⚠️", f"{errs} consecutive error(s)"
            elif count == 0:
                icon, status = "✅", "no slots published yet"
            else:
                icon, status = "✅", f"{count} slot(s) tracked"

            lines = [
                f"{icon} <b>{_esc(name)}</b>",
                f"   Last success: {_fmt_age(age)}" + (" ago" if age is not None else ""),
            ]
            if r.get("last_check_iso"):
                lines.append(f"   Last check: {_esc(_fmt_iso_local(r['last_check_iso']))}")
            lines.append(f"   {status}")
            if r.get("last_error"):
                lines.append(f"   <code>{_esc_cut(r['last_error'], 120)}</code>")

            if slot_pairs and (age is None or age > max_slot_age_for_display_seconds):
                stale = "never verified" if age is None else f"{_fmt_age(age)} old"
                lines.append(f"   ⛔ slot data is {stale} - NOT current")
            elif slot_pairs:
                shown = slot_pairs[:_MAX_SLOTS_PER_TENT]
                lines += [f"   • {_esc(s)}" for s in shown]
                hidden = len(slot_pairs) - len(shown)
                if hidden:
                    lines.append(f"   …and {hidden} more")

            sections.append("\n".join(lines))

        if blind:
            header = (
                f"🚨 <b>ALARM: {len(blind)} of {len(tent_reports)} tent(s) BLIND</b>\n"
                f"No fresh data from: {_esc(', '.join(blind))}"
            )
        else:
            header = f"✅ <b>Daily Status - all {len(tent_reports)} tent(s) fresh</b>"
        if not watchdog_enabled:
            header += "\n⛔ EXTERNAL WATCHDOG DISABLED (healthcheck_url is empty)"

        return self._send_listing(header, sections, "")
