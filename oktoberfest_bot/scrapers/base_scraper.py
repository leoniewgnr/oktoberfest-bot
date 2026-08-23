"""Base scraper interface for checking tent reservations"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional
from datetime import datetime

# Contactable UA for the plain-HTTP JSON API calls.
HONEST_USER_AGENT = (
    "oktoberfest-watcher/2.0 (personal table watcher; +mailto:leonie@lumeraenergy.de)"
)
# A real headless Chromium sends a real Chrome UA — no fingerprint impersonation.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)


class ScrapeResult:
    """Result from a scraping operation"""

    def __init__(
        self,
        success: bool,
        dates_available: bool = False,
        available_dates: Optional[List[Dict]] = None,
        available_times: Optional[Dict[str, Dict[str, Any]]] = None,
        available_areas: Optional[Dict[str, Dict[str, Any]]] = None,
        error: str = None,
        slots: Optional[List[Dict[str, Any]]] = None,
        blocked: bool = False,
        status_code: Optional[int] = None,
        empty_state: bool = False,
    ):
        self.success = success
        self.dates_available = dates_available
        self.available_dates = available_dates or []
        # available_times is an optional mapping keyed by date "value".
        # Each entry: {"date_text": str, "times": [{"value": str, "text": str}, ...]}
        self.available_times = available_times or {}
        # available_areas (optional, populated only by scrapers that have area data,
        # e.g. the FZOS REST API). Keyed by date "value" (same key space as
        # available_times). Each entry: {"date_text": str, "areas": [{"value", "text"}, ...]}
        self.available_areas = available_areas or {}
        self.error = error
        # Flat per-slot view used for re-release detection (a slot whose area
        # set or state changes without any new date/time appearing).
        self.slots = slots or []
        # blocked = "we were refused" (403 / Cloudflare challenge / WAF page),
        # which is different from success=False for a parse error.
        self.blocked = blocked
        self.status_code = status_code
        # empty_state = page loaded fine and genuinely published nothing, as
        # opposed to a selector that silently matched nothing.
        self.empty_state = empty_state
        self.timestamp = datetime.now().isoformat()

    def build_slots(self) -> List[Dict[str, Any]]:
        """Flatten available_dates/times/areas into slots unless the scraper
        already supplied them. Caches the result on the instance."""
        if self.slots:
            return self.slots

        slots: List[Dict[str, Any]] = []
        for date in self.available_dates:
            date_value = str(date.get('value', ''))
            date_text = date.get('text') or date_value
            state = date.get('state') or ''
            areas = [
                a.get('text') or a.get('value')
                for a in (self.available_areas.get(date_value) or {}).get('areas') or []
            ]
            times = (self.available_times.get(date_value) or {}).get('times') or []
            if times:
                for t in times:
                    time_value = str(t.get('value', ''))
                    slots.append({
                        'key': f"{date_value}|{time_value}",
                        'date_value': date_value,
                        'time_value': time_value,
                        'date_text': date_text,
                        'time_text': t.get('text') or time_value,
                        'state': state,
                        'areas': areas,
                    })
            else:
                slots.append({
                    'key': date_value,
                    'date_value': date_value,
                    'time_value': '',
                    'date_text': date_text,
                    'time_text': '',
                    'state': state,
                    'areas': areas,
                })

        self.slots = slots
        return slots

class BaseScraper(ABC):
    """Abstract base class for tent reservation scrapers"""

    def __init__(self, tent_config: Dict[str, Any]):
        self.tent_id = tent_config['id']
        self.tent_name = tent_config['name']
        self.url = tent_config['url']
        self.config = tent_config

    @abstractmethod
    async def check_availability(self) -> ScrapeResult:
        """Check for available reservation dates"""
        pass
