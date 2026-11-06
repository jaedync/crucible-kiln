"""The zone name the dashboard shows comes from TZ first, then /etc/localtime; the demo always uses UTC."""
import os
import time

import pytest

from kiln import config
from kiln.demo import run as demo_run
from kiln.store import queries


@pytest.fixture
def zone(monkeypatch):
    def set_zone(tz, link="/usr/share/zoneinfo/America/New_York"):
        if tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", tz)
        monkeypatch.setattr(config.os, "readlink", lambda p: link)
    yield set_zone
    time.tzset()


@pytest.mark.parametrize("tz, name", [("UTC", "UTC"), (":UTC", "UTC"), ("Europe/Berlin", "Europe/Berlin"),
                                      (":/usr/share/zoneinfo/Asia/Tokyo", "Asia/Tokyo")])
def test_tz_wins_over_the_localtime_link(zone, tz, name):
    zone(tz)
    assert config.system_tz() == name and queries.local_tz() == name


def test_without_tz_the_localtime_link_names_the_zone(zone):
    zone(None)
    assert config.system_tz() == "America/New_York" and queries.local_tz() == "America/New_York"


def test_the_demo_runs_in_utc_whatever_the_machine_zone(zone, monkeypatch):
    zone(None)
    seen = {}

    async def fake_run(*a, **k):
        seen["tz"], seen["label"] = time.strftime("%Z"), queries.local_tz()
        return 0
    monkeypatch.setattr(demo_run, "run", fake_run)
    assert demo_run.main(17999, 1.0, 0, True) == 0
    assert seen == {"tz": "UTC", "label": "UTC"}
