"""doctor.sh / doctor.py: each check, the report, and that it never changes anything."""

import hashlib
import os
import shutil
import subprocess
import sys

import pytest

import doctor as dr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fake_run(table):
    """run() stand-in: first matching prefix in ``table`` -> (code, output)."""
    def run(cmd, timeout=10):
        line = " ".join(cmd)
        for prefix, result in table.items():
            if line.startswith(prefix):
                return result
        return 127, f"{cmd[0]}: not installed"
    return run


def by_name(results):
    return {r.name: r for r in results}


# --- individual checks ------------------------------------------------------------------------
def test_system_on_a_pi(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text('PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_CODENAME=bookworm\n')
    (tmp_path / "proc/device-tree").mkdir(parents=True)
    (tmp_path / "proc/device-tree/model").write_bytes(b"Raspberry Pi Zero W Rev 1.1\x00")
    r = by_name(dr.check_system(str(tmp_path)))
    assert r["OS"].status == "OK" and "bookworm" in r["OS"].detail
    assert r["Pi model"].status == "OK" and r["Pi model"].detail == "Raspberry Pi Zero W Rev 1.1"


def test_system_old_os(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text('PRETTY_NAME="Raspbian 10"\nVERSION_CODENAME=buster\n')
    r = by_name(dr.check_system(str(tmp_path)))
    assert r["OS"].status == "WARN" and "Bookworm" in r["OS"].hint
    assert r["Pi model"].status == "WARN"


@pytest.mark.parametrize("scan,wired,want,text", [
    ("no:Other:40:WPA2\nyes:HomeNet:78:WPA2", "wifi:connected", "OK", '"HomeNet", signal 78% (-58 dBm)'),
    ("yes:Far\\:Away:25:WPA2", "wifi:connected", "WARN", '"Far:Away"'),
    ("no:HomeNet:70:WPA2", "wifi:disconnected\nethernet:disconnected", "FAIL", "not connected"),
    ("", "ethernet:connected", "OK", "wired Ethernet"),
])
def test_wifi(scan, wired, want, text):
    run = fake_run({"nmcli -t -f ACTIVE": (0, scan), "nmcli -t -f TYPE": (0, wired)})
    r = dr.check_wifi(run, {"dbm": -58.0})[0]
    assert r.status == want and text in r.detail


def test_wifi_without_networkmanager():
    assert dr.check_wifi(fake_run({}), None)[0].status == "WARN"


def test_internet():
    ok = dr.check_internet(resolve=lambda h, p: [], fetch=lambda u: (204, ""))[0]
    portal = dr.check_internet(resolve=lambda h, p: [], fetch=lambda u: (200, ""))[0]
    down = dr.check_internet(resolve=lambda h, p: [], fetch=lambda u: (None, "timed out"))[0]

    def no_dns(h, p):
        raise OSError("Name or service not known")

    dns = dr.check_internet(resolve=no_dns, fetch=lambda u: (204, ""))[0]
    assert (ok.status, portal.status, down.status, dns.status) == ("OK", "WARN", "FAIL", "FAIL")
    assert "sign-in page" in portal.hint and "DNS" in dns.detail


def test_sources_cover_every_feed():
    seen = []

    def fetch(url):
        seen.append(url)
        return {"coingecko": (429, ""), "espn": (None, "Connection refused")}.get(
            next((k for k in ("coingecko", "espn") if k in url), ""), (200, ""))

    settings = {"NEWS_FEEDS": "https://a.example.com/rss,https://b.example.com/feed"}
    r = dr.check_sources(settings, fetch)
    names = [x.name for x in r]
    assert names[:4] == ["CoinGecko (prices)", "Open-Meteo (weather)", "ESPN (sports)", "GitHub (updates)"]
    assert names[4:] == ["News feed a.example.com", "News feed b.example.com"]
    st = {x.name: x.status for x in r}
    assert st["CoinGecko (prices)"] == "WARN" and st["ESPN (sports)"] == "FAIL" and st["GitHub (updates)"] == "OK"
    assert len(dr.check_sources(None, lambda u: (200, ""))) == 4 + len(dr.DEFAULT_FEEDS)   # defaults when unset


def test_service_and_log_highlighting():
    log = "\n".join([f"2026-11-02T10:00:{i:02d} pi python[1]: INFO ticker: prices: BTC 1" for i in range(28)]
                    + ["2026-11-02T10:01:00 pi python[1]: ERROR ticker: price fetch failed (3 in a row)",
                       "2026-11-02T10:01:01 pi python[1]: WARNING ticker.news: news feed failed: x"])
    run = fake_run({"systemctl is-active": (0, "active"), "systemctl is-enabled": (0, "enabled"),
                    "journalctl": (0, log)})
    results, rows = dr.check_service(run)
    r = by_name(results)
    assert r["Service"].status == "OK" and r["Log"].status == "WARN" and "1 with errors" in r["Log"].detail
    assert len(rows) == 30
    text = dr.report(results, rows)
    assert "!! 2026-11-02T10:01:00" in text and "   2026-11-02T10:01:01" in text   # warnings aren't errors


def test_service_stopped_and_missing():
    stopped = fake_run({"systemctl is-active": (3, "failed"), "systemctl is-enabled": (0, "enabled"),
                        "journalctl": (0, "-- No entries --")})
    r = by_name(dr.check_service(stopped)[0])
    assert r["Service"].status == "FAIL" and "restart" in r["Service"].hint
    missing = fake_run({"systemctl is-active": (3, "inactive"),
                        "systemctl is-enabled": (1, "Failed to get unit file state: No such file or directory")})
    assert by_name(dr.check_service(missing)[0])["Service"].detail == "not installed"


def test_resources(tmp_path):
    (tmp_path / "meminfo").write_text("MemTotal:  443000 kB\nMemAvailable:  40000 kB\n")
    r = by_name(dr.check_resources(str(tmp_path), str(tmp_path / "meminfo")))
    assert r["Memory"].status == "WARN" and "39 MB available of 432 MB" in r["Memory"].detail
    assert r["Disk"].status in ("OK", "WARN", "FAIL")


@pytest.mark.parametrize("settings,model,want", [
    ({"LED_ROWS": "32", "LED_COLS": "64", "LED_GPIO_MAPPING": "adafruit-hat", "LED_SLOWDOWN_GPIO": "1"},
     "Raspberry Pi Zero W Rev 1.1", "OK"),
    ({"LED_GPIO_MAPPING": "adafruit-bonnet"}, "", "FAIL"),
    ({"LED_RGB_SEQUENCE": "RGG"}, "", "FAIL"),
    ({"LED_SLOWDOWN_GPIO": "fast"}, "", "FAIL"),
    ({"LED_SLOWDOWN_GPIO": "1"}, "Raspberry Pi 4 Model B Rev 1.4", "WARN"),
    ({"LED_SLOWDOWN_GPIO": "4"}, "Raspberry Pi Zero W Rev 1.1", "WARN"),
    ({"LED_ROWS": "48"}, "", "WARN"),
])
def test_panel_settings(settings, model, want, tmp_path):
    r = dr.check_panel(settings, model, str(tmp_path), str(tmp_path / "modules"))
    assert r[0].status == "INFO" and "LED_ROWS=" in r[0].detail
    assert {x.status for x in r[1:]} == {want}


def test_panel_settings_missing_file_and_audio(tmp_path):
    assert dr.check_panel(None)[0].status == "FAIL"
    (tmp_path / "modules").write_text("snd_bcm2835 24576 0 - Live 0x0\n")
    r = dr.check_panel({}, "", str(tmp_path), str(tmp_path / "modules"))
    assert any(x.name == "Onboard audio" and x.status == "WARN" for x in r)


@pytest.mark.parametrize("out,code,want", [
    ("Hi DNA2RNA1/crypto-ticker! You've successfully authenticated, but GitHub does not provide shell access.", 1, "OK"),
    ("git@github.com: Permission denied (publickey).", 255, "FAIL"),
    ("Host key verification failed.", 255, "WARN"),
    ("ssh: connect to host github.com port 22: Connection timed out", 255, "FAIL"),
])
def test_deploy_key(tmp_path, out, code, want):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh/crypto-ticker-deploy").write_text("not a real key")
    calls = []

    def run(cmd, timeout=10):
        calls.append(cmd)
        return code, out

    r = dr.check_deploy_key(str(tmp_path), run)[0]
    assert r.status == want
    joined = " ".join(calls[0])
    assert "StrictHostKeyChecking=yes" in joined and "UpdateHostKeys=no" in joined   # never edits known_hosts


def test_deploy_key_missing(tmp_path):
    assert dr.check_deploy_key(str(tmp_path), fake_run({}))[0].status == "WARN"


def test_report_redacts_secrets_and_counts():
    rows = ["... settings: WEB_PIN=2468 COINGECKO_API_KEY=CG-abc password: hunter22",
            "... headers {'x-cg-demo-api-key': 'CG-xyz'}"]
    results = [dr.Result("OK", "A", "fine"), dr.Result("WARN", "B", "meh", "do x"), dr.Result("FAIL", "C", "bad", "do y")]
    text = dr.report(results, rows)
    for secret in ("2468", "CG-abc", "hunter22", "CG-xyz"):
        assert secret not in text
    assert "Summary: 1 OK, 1 WARN, 1 FAIL" in text and "       Fix: do y" in text
    assert "[OK]   A: fine" in text and "Fix: " not in text.split("[OK]   A: fine")[1].split("\n")[1]


def test_settings_secrets_never_printed(tmp_path, capsys):
    (tmp_path / "settings.env").write_text("WEB_PIN=8642\nCOINGECKO_API_KEY=CG-secret\nLED_ROWS=32\n")
    run = fake_run({"systemctl": (0, "active"), "journalctl": (0, "")})
    dr.main([], repo=str(tmp_path), run=run, fetch=lambda u: (200, ""), wifi={})
    out = capsys.readouterr().out
    assert "8642" not in out and "CG-secret" not in out and "LED_ROWS=32" in out


# --- read-only, end to end -----------------------------------------------------------------------
def tree_hash(*roots):
    h = hashlib.sha256()
    for root in roots:
        for dirpath, dirnames, files in sorted(os.walk(root)):
            dirnames.sort()
            for f in sorted(files):
                p = os.path.join(dirpath, f)
                st = os.lstat(p)
                h.update(p.encode() + str((st.st_size, st.st_mtime_ns, st.st_mode)).encode())
                with open(p, "rb") as fh:
                    h.update(fh.read())
            h.update(dirpath.encode() + str(sorted(dirnames)).encode())
    return h.hexdigest()


STUB = "#!/bin/sh\necho \"$(basename \"$0\") $*\" >> \"$CT_STUB_LOG\"\n{body}\n"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_doctor_sh_changes_nothing(tmp_path):
    repo = tmp_path / "crypto-ticker"
    shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", "venv", "cache", "logs", "__pycache__",
                                                             "docs", "fonts", "tests", "tools", "iphone"))
    (repo / "settings.env").write_text("LED_ROWS=32\nLED_COLS=64\nNEWS_FEEDS=https://news.invalid/rss\n")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh/crypto-ticker-deploy").write_text("fake")
    (home / ".ssh/known_hosts").write_text("")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    bodies = {"nmcli": "echo 'yes:HomeNet:70:WPA2'", "systemctl": "echo active",
              "journalctl": "echo 'pi python[1]: ERROR ticker: test'", "ssh": "echo 'Permission denied (publickey).'; exit 255"}
    for name, body in bodies.items():
        (stubs / name).write_text(STUB.format(body=body))
        (stubs / name).chmod(0o755)
    before = tree_hash(repo, home)
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(home), "CT_STUB_LOG": str(tmp_path / "calls.log"),
           "CT_DOCTOR_TIMEOUT": "3", "HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9"}
    p = subprocess.run(["bash", str(repo / "doctor.sh")], capture_output=True, text=True, env=env, timeout=180)
    assert "Summary:" in p.stdout, p.stderr[-2000:]
    assert tree_hash(repo, home) == before                        # not a byte changed (no .pyc either)
    calls = (tmp_path / "calls.log").read_text().splitlines()
    for line in calls:                                              # only read-only commands were run
        assert line.split()[0] in bodies
        assert not any(w in line.split() for w in ("restart", "start", "stop", "enable", "disable", "delete",
                                                   "modify", "add", "up", "down", "load", "reload"))
    assert "!! pi python[1]: ERROR ticker: test" in p.stdout
    assert p.returncode == 1                                       # some FAILs (no network here)


def test_help():
    p = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "doctor.py"), "--help"], capture_output=True, text=True)
    assert p.returncode == 0 and "never changes" in p.stdout
