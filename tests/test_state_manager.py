import json
from datetime import datetime, timedelta

import pytest

from oktoberfest_bot.state_manager import StateManager


def slot(date_value, time_value=None, state="requested"):
    return {
        "key": f"{date_value}|{time_value}" if time_value else str(date_value),
        "date_text": f"Sa. {date_value}",
        "time_text": time_value or "",
        "state": state,
        "date_value": date_value,
        "time_value": time_value,
    }


@pytest.fixture
def sm(tmp_path):
    return StateManager(str(tmp_path / "state.json"))


def test_returned_after_disappearing(sm):
    evening = slot("2026-09-26", "18:00")

    assert sm.diff_slots("hacker", [evening])["new"] == [evening]
    sm.commit_slots("hacker", [evening])

    sm.commit_slots("hacker", [])

    diff = sm.diff_slots("hacker", [evening])
    assert diff["returned"] == [evening]
    assert diff["new"] == []


def test_unchanged_slot_is_not_alerted(sm):
    lunch = slot("2026-09-26", "12:00")
    sm.commit_slots("hacker", [lunch])

    diff = sm.diff_slots("hacker", [lunch])
    assert diff == {"new": [], "returned": [], "state_changed": [], "area_added": []}

    sm.commit_slots("hacker", [lunch])
    assert sm.diff_slots("hacker", [lunch]) == {
        "new": [], "returned": [], "state_changed": [], "area_added": []
    }


def test_state_change_is_detected(sm):
    sm.commit_slots("schottenhamel", [slot("2026-09-27", "17:00", state="requested")])

    changed = slot("2026-09-27", "17:00", state="confirmed")
    diff = sm.diff_slots("schottenhamel", [changed])
    assert diff["state_changed"] == [changed]
    assert diff["new"] == []
    assert diff["returned"] == []


def test_diff_does_not_mutate_commit_does(sm):
    evening = slot("2026-10-03", "17:00")

    sm.diff_slots("kufflers", [evening])
    assert sm.get_tent_state("kufflers")["seen_slots"] == {}

    sm.commit_slots("kufflers", [evening])
    seen = sm.get_tent_state("kufflers")["seen_slots"]
    assert set(seen) == {"2026-10-03|17:00"}
    assert seen["2026-10-03|17:00"]["state"] == "requested"
    assert seen["2026-10-03|17:00"]["gone_since"] is None


def test_commit_persists_across_instances(sm):
    sm.commit_slots("kufflers", [slot("2026-10-03", "17:00")])

    reloaded = StateManager(sm.state_file)
    assert reloaded.diff_slots("kufflers", [slot("2026-10-03", "17:00")])["new"] == []


def test_slot_without_time_uses_date_as_key(sm):
    day = slot("2026-09-20")
    sm.commit_slots("bratwurst", [day])
    assert set(sm.get_tent_state("bratwurst")["seen_slots"]) == {"2026-09-20"}


def test_seen_slots_pruned_after_60_days(sm):
    sm.commit_slots("hacker", [slot("2026-01-01", "12:00")])
    seen = sm.get_tent_state("hacker")["seen_slots"]
    seen["2026-01-01|12:00"]["last_seen"] = (
        datetime.now() - timedelta(days=61)
    ).isoformat()

    sm.commit_slots("hacker", [slot("2026-09-26", "18:00")])
    assert set(sm.get_tent_state("hacker")["seen_slots"]) == {"2026-09-26|18:00"}


def test_renotify_after_interval(sm):
    assert sm.should_renotify_error("hacker", 3600) is True

    sm.set_error_notified_at("hacker", datetime.now())
    assert sm.should_renotify_error("hacker", 3600) is False

    # the 30-day-silence bug: an error must alert again once the interval elapses
    assert sm.should_renotify_error("hacker", 3600, now=datetime.now() + timedelta(hours=2)) is True

    sm.set_error_notified_at("hacker", datetime.now() - timedelta(days=1))
    assert sm.should_renotify_error("hacker", 3600) is True


def test_success_clears_error_notification(sm):
    sm.mark_check_error("hacker", "403 Forbidden")

    sm.mark_check_success("hacker", True, [{"value": "2026-09-26", "text": "Sa. 26.09."}])
    assert sm.should_renotify_error("hacker", 3600) is True
    assert sm.get_consecutive_errors("hacker") == 0


def test_error_message_truncated(sm):
    sm.mark_check_error("hacker", "x" * 500)
    assert len(sm.get_tent_state("hacker")["last_error_message"]) == 300


def test_staleness(sm):
    assert sm.seconds_since_success("hacker") is None
    assert sm.is_stale("hacker", 3600) is True

    sm.mark_check_success("hacker", False)
    assert sm.seconds_since_success("hacker") < 5
    assert sm.is_stale("hacker", 3600) is False

    sm.update_tent_state(
        "hacker", last_success=(datetime.now() - timedelta(days=30)).isoformat()
    )
    assert sm.is_stale("hacker", 3600) is True

    pairs, age = sm.get_slot_pairs_with_age("hacker")
    assert pairs == []
    assert age > 29 * 86400


def test_old_state_file_upgrades(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "hacker": {
                    "last_check": "2026-07-24T21:34:00",
                    "dates_available": True,
                    "available_dates": [{"value": "2026-09-26", "text": "Sa. 26.09."}],
                    "available_times": {},
                    "available_areas": {},
                    "consecutive_errors": 12,
                    "error_notified": True,
                }
            }
        )
    )
    sm = StateManager(str(path))

    state = sm.get_tent_state("hacker")
    assert state["last_success"] is None
    assert state["seen_slots"] == {}
    assert state["last_error"] is None
    assert sm.get_consecutive_errors("hacker") == 12
    assert sm.get_slot_pairs("hacker") == ["Sa. 26.09."]

    # blindness must be visible, and the latched error must be re-notifiable
    assert sm.is_stale("hacker", 3600) is True
    assert state["last_error_notified_at"] == "2026-07-24T21:34:00"
    assert sm.should_renotify_error("hacker", 3600) is True


def test_new_bereich_inside_a_known_slot_is_flagged(sm):
    """A Storno freeing a table shows up only as a new Bereich — nothing else
    about the guestlist changes."""
    evening = slot("2026-09-26", "Abend")
    evening["areas"] = ["Box 1"]
    sm.commit_slots("hacker", [evening])

    freed = dict(evening, areas=["Box 1", "Box 9"])
    diff = sm.diff_slots("hacker", [freed])
    assert diff["new"] == []
    assert [s["new_areas"] for s in diff["area_added"]] == [["Box 9"]]

    sm.commit_slots("hacker", [freed])
    assert sm.diff_slots("hacker", [freed])["area_added"] == []


def test_areas_unknown_before_does_not_alert_on_first_record(sm):
    evening = slot("2026-09-26", "Abend")
    sm.commit_slots("hacker", [dict(evening)])  # committed with areas=[]
    diff = sm.diff_slots("hacker", [dict(evening, areas=["Box 1"])])
    assert [s["new_areas"] for s in diff["area_added"]] == [["Box 1"]]


def test_skipped_keys_stay_uncommitted_so_they_re_alert(sm):
    """A slot whose alert Telegram dropped must be re-classified next cycle."""
    evening = slot("2026-09-26", "Abend")
    sm.commit_slots("hacker", [evening], skip_keys={evening["key"]})

    assert sm.diff_slots("hacker", [evening])["new"] == [evening]


def test_non_dict_tent_entry_does_not_raise(sm):
    sm.state["hacker"] = None
    assert sm.get_tent_state("hacker")["seen_slots"] == {}
    assert sm.seconds_since_success("hacker") is None
