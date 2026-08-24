"""Guards for the browser scraper's structure. It drives a real Chromium, so the
happy path isn't unit-testable here — but the helper methods its own error paths
call must exist (a refactor once deleted _fail, and only the live Pi caught it)."""

from oktoberfest_bot.scrapers.form_select import FormSelectScraper


def _scraper():
    return FormSelectScraper({
        "id": "t", "name": "T", "url": "https://x/",
        "scraper_type": "form_select", "selector": "select",
    })


def test_error_path_helpers_exist():
    s = _scraper()
    assert callable(getattr(s, "_fail", None))
    assert callable(getattr(s, "_build_slots", None))
    assert callable(getattr(s, "_extract_select", None))


def test_fail_produces_a_failed_result():
    r = _scraper()._fail("boom", blocked=True, status_code=403)
    assert r.success is False and r.blocked is True and r.status_code == 403
    assert r.error == "boom"


def test_build_slots_is_date_only_with_no_times():
    slots = _scraper()._build_slots(
        [{"value": "2026-09-26", "text": "Samstag, 26.09.2026"}], {}
    )
    assert len(slots) == 1
    s = slots[0]
    assert s["date_text"] == "Samstag, 26.09.2026"
    assert s["time_value"] is None and s["key"] == "2026-09-26"
