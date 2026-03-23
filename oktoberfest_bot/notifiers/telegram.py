"""Telegram notification implementation"""

import logging
from typing import Any, Optional

import requests

from .base_notifier import BaseNotifier

logger = logging.getLogger(__name__)


class TelegramNotifier(BaseNotifier):
    """Send notifications via Telegram Bot API"""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send_notification(self, message: str) -> Optional[int]:
        """Send notification via Telegram.

        Returns message_id on success (used for optional reactions).
        """
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML',
            }

            response = requests.post(url, json=payload, timeout=20)
            if response.status_code == 200:
                data = response.json()
                msg_id = data.get('result', {}).get('message_id')
                logger.info("Telegram notification sent successfully")
                return msg_id

            logger.error(f"Failed to send Telegram notification: {response.text}")
            return None

        except Exception as e:
            logger.error(f"Error sending Telegram notification: {e}")
            return None

    def react_to_message(self, message_id: Any, emoji: str):
        """Best-effort: react to a Telegram message (requires Bot API support/permissions)."""
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/setMessageReaction"
            payload = {
                'chat_id': self.chat_id,
                'message_id': int(message_id),
                'reaction': [{'type': 'emoji', 'emoji': emoji}],
            }
            response = requests.post(url, json=payload, timeout=20)
            if response.status_code != 200:
                logger.info(f"Could not add reaction: {response.text}")
        except Exception as e:
            logger.info(f"Could not add reaction: {e}")
