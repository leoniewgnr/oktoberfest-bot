import json

from oktoberfest_bot import events
from oktoberfest_bot.state_manager import StateManager


def test_slot_transitions_are_appended_as_jsonl(tmp_path):
    path = events.configure(str(tmp_path / "state.json"))
    sm = StateManager(str(tmp_path / "state.json"))
    a = {"key": "2026-09-26|abend", "date_text": "Sa. 26.09.2026", "time_text": "Abend", "state": "open"}
    b = {"key": "2026-09-27", "date_text": "So. 27.09.2026", "time_text": "", "state": None}

    sm.commit_slots("hacker", [a, b])
    sm.commit_slots("hacker", [b])
    sm.commit_slots("hacker", [a, b])
    events.record("notified", text="hello")

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(r["kind"], r.get("date")) for r in rows] == [
        ("appeared", "Sa. 26.09.2026"),
        ("appeared", "So. 27.09.2026"),
        ("disappeared", "Sa. 26.09.2026"),
        ("returned", "Sa. 26.09.2026"),
        ("notified", None),
    ]
    assert rows[0]["tent"] == "hacker" and rows[0]["shift"] == "Abend"
    assert all("+" in r["ts"] or "-" in r["ts"][10:] for r in rows)  # tz offset present
