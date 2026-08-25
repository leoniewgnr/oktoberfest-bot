"""Unit tests for the Livewire scraper's pure parsing helpers. The live replay
(GET snapshot -> POST date -> read shift) needs the network and is verified on
the Pi; here we lock down the HTML parsing that turns a Festzelt-OS render into
slots, using fragments shaped like the real responses."""

from oktoberfest_bot.scrapers import livewire_fzos as lw


DATE_SELECT = '''
<select id="data.createBookingStepOneForm.date" wire:model.live="data.createBookingStepOneForm.date">
  <option value="">Wählen Sie eine Option</option>
  <option value="2026-09-25">Freitag, 25.09.2026</option>
  <option value="2026-09-26">Samstag, 26.09.2026</option>
  <option value="2026-09-28">Montag, 28.09.2026</option>
</select>'''

SHIFT_SELECT = '''
<select id="data.createBookingStepOneForm.booking_list_id">
  <option value="">Wählen Sie eine Option</option>
  <option value="2111"><!--[if BLOCK]><![endif]-->  Frühschoppen  <!--[if ENDBLOCK]><![endif]--></option>
  <option value="1677">Abend</option>
</select>'''


def test_select_block_and_options():
    block = lw._select_block(DATE_SELECT, lw._DATE_FIELD)
    assert block is not None
    opts = lw._options(block)
    # placeholder (empty value) dropped, three real dates kept
    assert [o["value"] for o in opts] == ["2026-09-25", "2026-09-26", "2026-09-28"]
    assert opts[0]["text"] == "Freitag, 25.09.2026"


def test_shift_options_strip_blade_comments_and_umlauts():
    block = lw._select_block(SHIFT_SELECT, lw._SHIFT_FIELD)
    opts = lw._options(block)
    assert opts == [
        {"value": "2111", "text": "Frühschoppen"},
        {"value": "1677", "text": "Abend"},
    ]




def test_slot_key_shape():
    s = lw.LivewireFzosScraper(
        {"id": "t", "name": "T", "url": "https://x/r", "scraper_type": "livewire"}
    )
    with_shift = s._slot("2026-09-26", "Samstag, 26.09.2026", "1677", "Abend")
    assert with_shift["key"] == "2026-09-26|1677"
    assert with_shift["time_text"] == "Abend"
    date_only = s._slot("2026-09-28", "Montag, 28.09.2026", None, "")
    assert date_only["key"] == "2026-09-28"
    assert date_only["time_value"] is None
