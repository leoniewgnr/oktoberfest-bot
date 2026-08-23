"""Configuration loading and validation"""

import json
import os
import sys
from typing import Dict, List, Any


# Extra required fields per scraper_type, on top of the fields every tent needs.
SCRAPER_TYPE_REQUIRED_FIELDS = {
    'api_fzos': ['api_host', 'company_id'],
    'form_select': ['selector'],
    'announcement': [],
}


class ConfigLoader:
    """Loads and validates configuration files"""

    def __init__(self, config_path: str, tents_path: str):
        self.config_path = config_path
        self.tents_path = tents_path
        self.config = self._load_config()
        self.tents = self._load_tents()

    def _load_config(self) -> Dict[str, Any]:
        """Load main configuration file"""
        if not os.path.exists(self.config_path):
            print(f"Error: Config file not found: {self.config_path}")
            print("Please copy config.example.json to config.json and update with your settings")
            sys.exit(1)

        with open(self.config_path, 'r') as f:
            config = json.load(f)

        # Validate required fields
        required_fields = ['telegram_bot_token', 'telegram_chat_id', 'state_file', 'log_file']
        missing = [field for field in required_fields if field not in config]

        if missing:
            print(f"Error: Missing required config fields: {', '.join(missing)}")
            sys.exit(1)

        config.setdefault('heartbeat_interval_seconds', 3600)
        config.setdefault('blind_alert_after_seconds', 900)
        config.setdefault('blind_realert_interval_seconds', 1800)
        config.setdefault('max_slot_age_for_display_seconds', 3600)
        config.setdefault('healthcheck_url', '')
        return config

    def _load_tents(self) -> List[Dict[str, Any]]:
        """Load tent configurations"""
        if not os.path.exists(self.tents_path):
            print(f"Error: Tents config file not found: {self.tents_path}")
            sys.exit(1)

        with open(self.tents_path, 'r') as f:
            tents_data = json.load(f)

        tents = tents_data.get('tents', [])

        if not tents:
            print("Error: No tents configured in tents.json")
            sys.exit(1)

        # Filter only enabled tents
        enabled_tents = [tent for tent in tents if tent.get('enabled', True)]

        if not enabled_tents:
            print("Error: No enabled tents found")
            sys.exit(1)

        # Validate tent configurations
        for tent in enabled_tents:
            tent_id = tent.get('id', 'unknown')

            required = ['id', 'name', 'url', 'scraper_type']
            missing = [field for field in required if field not in tent]
            if missing:
                print(f"Error: Tent '{tent_id}' missing fields: {', '.join(missing)}")
                sys.exit(1)

            scraper_type = tent['scraper_type']
            if scraper_type not in SCRAPER_TYPE_REQUIRED_FIELDS:
                known = ', '.join(sorted(SCRAPER_TYPE_REQUIRED_FIELDS))
                print(f"Error: Tent '{tent_id}' has unknown scraper_type '{scraper_type}' (known: {known})")
                sys.exit(1)

            missing = [field for field in SCRAPER_TYPE_REQUIRED_FIELDS[scraper_type] if not tent.get(field)]
            if missing:
                print(f"Error: Tent '{tent_id}' (scraper_type '{scraper_type}') missing fields: {', '.join(missing)}")
                sys.exit(1)

        return enabled_tents

    def get_config(self) -> Dict[str, Any]:
        """Get main configuration"""
        return self.config

    def get_tents(self) -> List[Dict[str, Any]]:
        """Get list of enabled tents"""
        return self.tents
