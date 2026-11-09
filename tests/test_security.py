"""Settings page hardening: host names, other websites, headers, PIN guessing."""

import json
import os

import pytest
from conftest import login, request

import webui


@pytest.mark.parametrize("header,name", [
    ("ticker.local:8080", "ticker.local"), ("192.168.1.20:8080", "192.168.1.20"),
    ("[fe80::1]:8080", "fe80::1"), ("Ticker.Local", "ticker.local"), ("", ""), ("[bad", ""),
])
def test_host_name(header, name):
    assert webui.host_name(header) == name


def test_host_allowed():
    me = os.uname().nodename.lower()
    for ok in ("", "localhost", me, me + ".local", me + ".local.", me + ".lan", me + ".home.arpa",
               "192.168.1.20", "fe80::1", "fe80::1%wlan0"):
        assert webui.host_allowed(ok), ok
    for bad in ("evil.example", "ticker.local.evil.example", "192.168.1.20.nip.io", me + ".evil.example",
                "other.lan"):
        assert not webui.host_allowed(bad), bad
    assert webui.host_allowed("ticker.tailnet.ts.net", {"ticker.tailnet.ts.net"})


def test_rebinding_host_is_refused(server):
    status, body, _ = request(server, "GET", "/api/settings", headers={"Host": "attacker.example"})
    assert status == 421 and "WEB_ALLOWED_HOSTS" in body["error"]
    assert request(server, "POST", "/api/login", json.dumps({"pin": "2468"}).encode(),
                   {"Host": "attacker.example"})[0] == 421


def test_allowed_hosts_setting(app, server):
    with open(app.env_path, "a") as fh:
        fh.write("WEB_ALLOWED_HOSTS=ticker.home.arpa, Other.Name\n")
    assert app.allowed_hosts() == {"ticker.home.arpa", "other.name"}
    assert request(server, "GET", "/api/state", headers={"Host": "ticker.home.arpa:8080"})[0] == 200


def test_post_from_another_website_is_refused(server):
    pin = json.dumps({"pin": "2468"}).encode()
    port = server.server_address[1]
    assert request(server, "POST", "/api/login", pin, {"Origin": "https://evil.example"})[0] == 403
    assert request(server, "POST", "/api/login", pin, {"Origin": "null"})[0] == 403
    ok = request(server, "POST", "/api/login", pin,
                 {"Origin": f"http://127.0.0.1:{port}", "Host": f"127.0.0.1:{port}"})
    assert ok[0] == 200


def test_json_endpoints_need_json(server):
    auth = login(server)
    # a plain HTML form can only send these types; refusing them blocks form-based attacks
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        assert request(server, "POST", "/api/settings", b'{"brightness": 50}',
                       dict(auth, **{"Content-Type": ctype}))[0] == 415


def test_without_pin_other_websites_still_cant_change_settings(app, server):
    with open(app.env_path, "w") as fh:
        fh.write("SYMBOLS=btc\n")  # no WEB_PIN: open to the home network...
    assert request(server, "POST", "/api/settings", b'{"brightness": 50}')[0] == 200
    # ...but not to a web page some phone on that network is showing
    assert request(server, "POST", "/api/settings", b'{"brightness": 60}',
                   {"Origin": "http://evil.example"})[0] == 403
    assert request(server, "POST", "/api/settings", b'{"brightness": 60}', {"Host": "evil.example"})[0] == 421


def test_security_headers_on_every_response(server):
    import http.client
    for path in ("/", "/api/state", "/nope"):
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        r.read()
        assert r.getheader("X-Frame-Options") == "DENY"
        assert r.getheader("X-Content-Type-Options") == "nosniff"
        assert "frame-ancestors 'none'" in r.getheader("Content-Security-Policy")


def test_guessing_from_many_addresses_pauses_sign_in(app, monkeypatch):
    for i in range(webui.GLOBAL_MAX_FAILS - 1):
        assert app.login(f"10.0.{i}.1", "0000")[0] is False  # each address under its own limit
    ok, msg = app.login("10.0.99.1", "0000")
    assert not ok
    ok, msg = app.login("10.0.100.1", "2468")  # even the right PIN waits
    assert not ok and "paused" in msg
    monkeypatch.setattr(webui.time, "time", lambda: app.global_until + 1)
    assert app.login("10.0.100.1", "2468")[0] is True


def test_per_device_lockout_still_works(app):
    for _ in range(webui.MAX_FAILS):
        app.login("10.0.0.5", "0000")
    ok, msg = app.login("10.0.0.5", "2468")
    assert not ok and "Wait" in msg


def test_failed_logins_table_stays_small(app, monkeypatch):
    app.fails = {f"10.{i // 256}.{i % 256}.1": (1, 0) for i in range(1001)}
    app.login("10.9.9.9", "0000")
    assert len(app.fails) <= 2


def test_non_root_service_restarts_by_exiting(monkeypatch):
    import updater
    killed = []
    monkeypatch.setenv("INVOCATION_ID", "x")
    monkeypatch.setattr(updater.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(updater.threading, "Timer",
                        lambda delay, fn, args: type("T", (), {"start": lambda self: killed.append(args)})())
    u = updater.Updater.__new__(updater.Updater)
    assert u._systemd_restart() is True and killed and killed[0][0] == os.getpid()
