"""Telegram notification implementation"""

import logging
import time
from typing import Any, Optional

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

    def send_notification(self, message: str) -> Optional[int]:
        """Send a notification. Returns message_id on success, None on failure.

        Retries on 429 (honoring Telegram's retry_after), 5xx, and network errors.
        Does NOT retry on other 4xx (won't help).
        """
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }

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
                return msg_id

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

    def react_to_message(self, message_id: Any, emoji: str):
        """Best-effort: react to a Telegram message (requires Bot API support)."""
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/setMessageReaction"
            payload = {
                "chat_id": self.chat_id,
                "message_id": int(message_id),
                "reaction": [{"type": "emoji", "emoji": emoji}],
            }
            response = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
            if response.status_code != 200:
                logger.info("Could not add reaction: %s", response.text[:300])
        except Exception as e:
            logger.info("Could not add reaction: %s", e)


def _parse_retry_after(response) -> Optional[float]:
    try:
        data = response.json()
    except ValueError:
        return None
    params = data.get("parameters") or {}
    val = params.get("retry_after")
    return float(val) if val is not None else None
