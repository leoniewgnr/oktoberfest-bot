"""Append-only JSONL history of slot transitions and sent notifications.

One line per event next to state.json (events.jsonl). Nothing reads it at
runtime; it exists so publishing patterns can be analysed after the fact.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

_log = logging.getLogger("oktoberfest_bot.events")
_log.propagate = False


def configure(state_file: str) -> Path:
    path = Path(state_file).with_name("events.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    for handler in list(_log.handlers):
        _log.removeHandler(handler)
    _log.addHandler(logging.FileHandler(path, encoding="utf-8"))
    _log.setLevel(logging.INFO)
    return path


def record(kind: str, **fields) -> None:
    # Local time with offset: analysis spans the DST change in late October.
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    _log.info(json.dumps({"ts": ts, "kind": kind, **fields}, ensure_ascii=False))
