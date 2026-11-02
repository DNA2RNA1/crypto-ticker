"""Status section data: Wi-Fi signal, uptime, last fetch and error, update endpoints."""

import json

import pytest

import sysinfo
from conftest import login, request

WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
 wlan0: 0000   54.  -56.  -256        0      0      0      0     11        0
"""


@pytest.mark.parametrize("text,want", [
    (WIRELESS, {"interface": "wlan0", "dbm": -56.0, "quality": 77, "label": "good"}),
    (WIRELESS.replace("-56.", "-80."), {"interface": "wlan0", "dbm": -80.0, "quality": 77, "label": "weak"}),
    (WIRELESS.replace("-56.", "200."), {"interface": "wlan0", "dbm": -56.0, "quality": 77, "label": "good"}),
    (WIRELESS.splitlines(True)[0] + WIRELESS.splitlines(True)[1], None),  # no interface (wired)
])
def test_wifi(tmp_path, text, want):
    p = tmp_path / "wireless"
    p.write_text(text)
    assert sysinfo.wifi(str(p)) == want


def test_wifi_missing_file():
    assert sysinfo.wifi("/nonexistent") is None
    assert sysinfo.system_uptime("/nonexistent") is None


def test_uptime(tmp_path):
    p = tmp_path / "uptime"
    p.write_text("12345.67 4321.00\n")
    assert sysinfo.system_uptime(str(p)) == 12345.67


class FakeTicker:
    assets = []

    def status(self):
        return {"last_success": 100.0, "stale": True, "failures": 2, "last_error": "network error: down",
                "last_error_at": 150.0, "started_at": 50.0, "coins": 3}


class FakeUpdater:
    def __init__(self):
        self.started = []

    def status(self):
        return {"state": "idle", "version": {"commit": "abc1234", "date": "2026-11-02", "subject": "x"}}

    def start_update(self):
        self.started.append("update")

    def start_rollback(self):
        from updater import UpdateError
        raise UpdateError("There's no previous version to go back to.")


def test_status_payload(app):
    app.ticker, app.updater = FakeTicker(), FakeUpdater()
    st = app.status()
    assert st["ticker"]["last_error"] == "network error: down" and st["ticker"]["stale"]
    assert st["update"]["version"]["commit"] == "abc1234"
    assert {"now", "wifi", "system_uptime"} <= set(st)


def test_update_endpoints(app, server):
    app.updater = FakeUpdater()
    assert request(server, "POST", "/api/update")[0] == 401         # PIN required
    auth = login(server)
    assert request(server, "GET", "/api/status", headers=auth)[0] == 200
    assert request(server, "POST", "/api/update", b"{}", auth)[0] == 202
    assert app.updater.started == ["update"]
    status, body, _ = request(server, "POST", "/api/rollback", b"{}", auth)
    assert status == 409 and "no previous version" in body["error"]


def test_update_unavailable_without_updater(app, server):
    auth = login(server)
    assert request(server, "POST", "/api/update", b"{}", auth)[0] == 404
    assert json.loads(json.dumps(app.status()))["update"] is None
