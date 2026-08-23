"""Telegram notification implementation"""

import logging
import time
from typing import Optional

import requests

from .base_notifier import BaseNotifier

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 2.0  # seconds; ~2, 4, 8 between attempts
_REQUEST_TIMEOUT = 20  # seconds


class TelegramNotifier(BaseNotifier):
    """Send notifications via Telegram Bot API with bounded retries."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._last_status: Optional[int] = None

    def send_notification(self, message: str) -> Optional[int]:
        """Send a notification. Returns message_id on success, None on failure.

        Retries on 429 (honoring Telegram's retry_after), 5xx, and network errors.
        A 400 is retried exactly once as plain text: the usual cause is malformed
        HTML in scraped text, and losing an alert to a stray '<' is unacceptable.
        """
        message_id = self._post(message, parse_mode="HTML")
        if message_id is None and self._last_status == 400:
            logger.warning("Telegram rejected the HTML (400) — retrying as plain text")
            message_id = self._post(message, parse_mode=None)
        return message_id

    def _post(self, message: str, parse_mode: Optional[str]) -> Optional[int]:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        self._last_status = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
            except requests.RequestException as e:
                wait = _BACKOFF_BASE ** attempt
                logger.warning(
                    "Telegram send failed (network, attempt %d/%d): %s — retrying in %.0fs",
                    attempt, _MAX_ATTEMPTS, e, wait,
                )
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(wait)
                continue

            if response.status_code == 200:
                try:
                    data = response.json()
                    msg_id = data.get("result", {}).get("message_id")
                except ValueError:
                    msg_id = None
                logger.info("Telegram notification sent")
                # 0 keeps the "delivered" answer truthy-by-not-being-None even if
                # the body was unparseable; callers test `is not None`.
                return msg_id if msg_id is not None else 0

            if response.status_code == 429:
                retry_after = _parse_retry_after(response) or _BACKOFF_BASE ** attempt
                logger.warning(
                    "Telegram rate-limited (attempt %d/%d) — sleeping %.0fs",
                    attempt, _MAX_ATTEMPTS, retry_after,
                )
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(retry_after)
                continue

            if 400 <= response.status_code < 500:
                self._last_status = response.status_code
                logger.error(
                    "Telegram returned %d (not retried): %s",
                    response.status_code, response.text[:300],
                )
                return None

            # 5xx → transient, back off and retry.
            wait = _BACKOFF_BASE ** attempt
            logger.warning(
                "Telegram returned %d (attempt %d/%d) — retrying in %.0fs",
                response.status_code, attempt, _MAX_ATTEMPTS, wait,
            )
            if attempt < _MAX_ATTEMPTS:
                time.sleep(wait)

        logger.error("Telegram notification dropped after %d attempts", _MAX_ATTEMPTS)
        return None


def _parse_retry_after(response) -> Optional[float]:
    try:
        data = response.json()
    except ValueError:
        return None
    params = data.get("parameters") or {}
    val = params.get("retry_after")
    return float(val) if val is not None else None
