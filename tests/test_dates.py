import pytest

from santa.bot import display_date, local_date


def test_date_uses_server_local_time():
    assert display_date(local_date("31.12.2027")) == "31.12.2027, 18:00"


def test_single_date_format():
    with pytest.raises(ValueError):
        local_date("01.12.2027 20.12.2027 25.12.2027")
