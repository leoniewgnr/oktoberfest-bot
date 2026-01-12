"""Base scraper interface for checking tent reservations"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any
from datetime import datetime


class ScrapeResult:
    """Result from a scraping operation"""

    def __init__(self, success: bool, dates_available: bool = False,
                 available_dates: List[Dict] = None, error: str = None):
        self.success = success
        self.dates_available = dates_available
        self.available_dates = available_dates or []
        self.error = error
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        result = {
            'success': self.success,
            'timestamp': self.timestamp
        }
        if self.success:
            result['dates_available'] = self.dates_available
            result['available_dates'] = self.available_dates
        else:
            result['error'] = self.error
        return result


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

    def get_tent_info(self) -> Dict[str, str]:
        """Get basic tent information"""
        return {
            'id': self.tent_id,
            'name': self.tent_name,
            'url': self.url
        }
