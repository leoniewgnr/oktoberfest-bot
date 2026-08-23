"""Text-diff watcher for plain marketing / announcement pages.

Weekend-evening supply is often announced as prose weeks ahead instead of being
published on a booking route — e.g. festhalle-schottenhamel.de announcing an
in-person Münchner-Kontingent window offering Samstag-/Sonntagabend tables.
Those pages are plain WordPress and not Cloudflare-protected, so plain
`requests` reaches them from the server.

Tent config (in tents.json) for this scraper type:

    {
      "id": "schottenhamel_muenchner_spezial",
      "name": "Schottenhamel Münchner Spezial (Ankündigung)",
      "url": "https://festhalle-schottenhamel.de/reservierung/muenchner-spezial/",
      "scraper_type": "announcement",
      "check_interval": 3600,
      "enabled": true
    }

Reports body_hash / text / keywords_found; deciding whether a change is worth
notifying is the orchestrator's job.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from typing import List

import requests

from .base_scraper import BaseScraper, ScrapeResult, HONEST_USER_AGENT

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 20

_DROP_BLOCKS = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_COMMENTS = re.compile(r"<!--.*?-->", re.S)
_BLOCK_BREAKS = re.compile(r"(?i)</(?:p|div|li|tr|td|h[1-6]|section|article)\s*>|<br\s*/?>")
_TAGS = re.compile(r"<[^>]*>")

# Junk that changes per request and would otherwise read as real content change.
_NOISE = (
    re.compile(r"(?i)\b(?:nonce|_?wpnonce|csrf[-_]?token|token|ver)\s*[=:]\s*[\"']?[\w.\-]+"),
    re.compile(r"\?ver=[\w.\-]+"),
    re.compile(r"(?i)\bwp-emoji[\w\-./]*"),
    re.compile(r"\b[0-9a-f]{24,}\b"),
    # long mixed alphanumeric runs (hashes, base64) — the digit+letter lookaheads
    # keep long German compounds intact
    re.compile(r"\b(?=[\w+/=-]*\d)(?=[\w+/=-]*[A-Za-z])[\w+/=-]{24,}\b"),
)

_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "cf-challenge",
    "enable javascript and cookies",
    "checking your browser",
)

_KEYWORDS = (
    "münchner kontingent", "muenchner kontingent", "kontingent",
    "samstagabend", "sonntagabend", "freitagabend", "abend",
    "restplätze", "restplaetze", "spontanreservierung", "kurzfristig",
    "freie tische", "verfügbar", "verfuegbar",
    "reservierung möglich", "reservierung moeglich",
    "vergeben", "ausgebucht",
)
_DATE_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")


def html_to_text(raw: str) -> str:
    """Readable text from HTML, stdlib only (bs4 is not a dependency)."""
    text = _DROP_BLOCKS.sub(" ", raw)
    text = _COMMENTS.sub(" ", text)
    text = _BLOCK_BREAKS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    return html.unescape(text)


def normalise(text: str) -> str:
    for pattern in _NOISE:
        text = pattern.sub("", text)
    lines: List[str] = []
    seen = set()
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def find_keywords(text: str) -> List[str]:
    lowered = text.lower()
    found = [term for term in _KEYWORDS if term in lowered]
    found.extend(sorted(set(_DATE_RE.findall(text))))
    found.extend(sorted(set(_TIME_RE.findall(text))))
    return found


class AnnouncementScraper(BaseScraper):
    """Hashes the readable text of a plain announcement page."""

    async def check_availability(self) -> ScrapeResult:
        logger.info(f"Checking announcement page for {self.tent_name}...")
        try:
            return await asyncio.to_thread(self._scrape)
        except Exception as e:
            logger.error(f"{self.tent_name}: announcement scrape failed - {e}")
            return ScrapeResult(success=False, error=str(e))

    def _scrape(self) -> ScrapeResult:
        headers = {
            "user-agent": HONEST_USER_AGENT,
            "accept": "text/html",
            "accept-language": "de",
        }
        try:
            resp = requests.get(
                self.url, headers=headers, timeout=_REQUEST_TIMEOUT, allow_redirects=True
            )
        except requests.RequestException as e:
            return ScrapeResult(success=False, error=f"network error: {e}")

        body = resp.text or ""
        head = body[:4000].lower()
        if resp.status_code in (403, 429) or any(m in head for m in _CHALLENGE_MARKERS):
            return ScrapeResult(
                success=False,
                blocked=True,
                status_code=resp.status_code,
                error=f"refused/challenged ({resp.status_code})",
            )
        if resp.status_code != 200:
            return ScrapeResult(
                success=False,
                status_code=resp.status_code,
                error=f"returned {resp.status_code}",
            )

        text = normalise(html_to_text(body))
        result = ScrapeResult(
            success=True, status_code=resp.status_code, slots=[], empty_state=not text
        )
        # An announcement page has no structured slots; the diff runs on text.
        result.body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        result.text = text
        result.keywords_found = find_keywords(text)
        return result
