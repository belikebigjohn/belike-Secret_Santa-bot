from datetime import datetime

import pytest

from santa.bot import display_date, kb, local_date, page_buttons, page_slice
from santa.formatting import limited_html, profile_link


def test_date_uses_moscow_time_regardless_of_server_timezone():
    assert display_date(local_date("31.12.2027")) == "31.12.2027, 18:00"
    assert local_date("31.12.2027") == datetime(2027, 12, 31, 15)


def test_single_date_format():
    with pytest.raises(ValueError):
        local_date("01.12.2027 20.12.2027 25.12.2027")


def test_profile_link_escapes_name():
    assert profile_link(123, "Иван <Santa>") == (
        '<a href="tg://user?id=123">Иван &lt;Santa&gt;</a>')


def test_keyboard_pairs_actions_but_keeps_back_on_own_row():
    keyboard = kb(("Анкета", "profile:1"), ("Миссия", "mission:1"),
                  ("⬅️ К игре", "game:1"))
    assert [len(row) for row in keyboard.inline_keyboard] == [2, 1]


def test_eighty_members_paginate_to_ten_pages():
    people = list(range(80))
    assert [len(page_slice(people, page)) for page in range(10)] == [8] * 10
    assert page_buttons("members:1", 0, 80) == [("Далее ▶️", "members:1:1")]
    assert page_buttons("members:1", 9, 80) == [("◀️ Назад", "members:1:8")]


def test_html_snippet_keeps_entities_intact():
    result = limited_html("<" * 2000, 1000)
    assert len(result) <= 1000
    assert result.endswith("…")
    assert result.count("&lt;") == 249
