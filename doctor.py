#!/usr/bin/env python3
"""Read-only health check for the ticker. Run ./doctor.sh and paste the output when asking for help.

It only reads files, runs read-only commands (nmcli, systemctl, journalctl, ssh -T) and
makes GET requests. It never changes settings, files, services or Wi-Fi, never prints
passwords, the PIN or API keys, and doesn't even add GitHub to known_hosts.
Standard library only, so it works before the venv exists.
"""

import os
import platform
import pwd
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = "crypto-ticker"
TIMEOUT = float(os.environ.get("CT_DOCTOR_TIMEOUT", "8"))
DEFAULT_FEEDS = ["https://www.coindesk.com/arc/outboundfeeds/rss/", "https://cointelegraph.com/rss",
                 "https://feeds.bbci.co.uk/news/business/rss.xml"]
SERVICES = [  # name, URL (a cheap GET each)
    ("CoinGecko (prices)", "https://api.coingecko.com/api/v3/ping"),
    ("Open-Meteo (weather)", "https://api.open-meteo.com/v1/forecast?latitude=0&longitude=0&current=temperature_2m"),
    ("ESPN (sports)", "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?limit=1"),
    ("GitHub (updates)", "https://github.com/"),
]
GPIO_MAPPINGS = ("adafruit-hat", "adafruit-hat-pwm", "regular", "regular-pi1", "classic", "classic-pi1",
                 "compute-module")
SECRET_KEYS = re.compile(r"PIN|KEY|PASSWORD|SECRET|TOKEN", re.I)


@dataclass
class Result:
    status: str      # OK, WARN, FAIL, INFO
    name: str
    detail: str
    hint: str = ""


def run(cmd, timeout=10):
    """(exit code, output) of a read-only command; (127, msg) if it isn't installed."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, LC_ALL="C"))
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out"


def fetch(url, timeout=None):
    """(status code or None, error text) for a GET request; the body is mostly ignored."""
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-ticker-doctor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT, context=ssl.create_default_context()) as r:
            r.read(2048)
            return r.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, socket.timeout, OSError, ssl.SSLError) as exc:
        reason = getattr(exc, "reason", exc)
        return None, str(reason)[:120]


def read_settings(path):
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return None
    return out


def owner_home(repo):
    """Home of the folder's owner (the deploy key lives there even under sudo)."""
    try:
        return pwd.getpwuid(os.stat(repo).st_uid).pw_dir
    except (KeyError, OSError):
        return os.path.expanduser("~")


# --- checks: each returns a list of Results -------------------------------------------
def check_system(root="/"):
    out = []
    osr = {}
    try:
        with open(os.path.join(root, "etc/os-release")) as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                osr[k] = v.strip('"')
    except OSError:
        pass
    pretty = osr.get("PRETTY_NAME", platform.platform())
    codename = osr.get("VERSION_CODENAME", "")
    py = platform.python_version()
    detail = f"{pretty}, {platform.machine()}, Python {py}"
    if codename in ("buster", "bullseye", "stretch"):
        out.append(Result("WARN", "OS", detail, "This needs Raspberry Pi OS Bookworm or newer; reflash with Raspberry Pi Imager."))
    elif sys.version_info < (3, 9):
        out.append(Result("FAIL", "OS", detail, "Python 3.9+ is needed; use Raspberry Pi OS Bookworm."))
    else:
        out.append(Result("OK", "OS", detail))
    try:
        with open(os.path.join(root, "proc/device-tree/model"), "rb") as fh:
            model = fh.read().decode(errors="replace").strip("\x00\n ")
        out.append(Result("OK", "Pi model", model))
    except OSError:
        out.append(Result("WARN", "Pi model", "not a Raspberry Pi (no /proc/device-tree/model)",
                          "The LED panel only works on a Raspberry Pi."))
    return out


def check_wifi(run=run, wifi=None):
    code, out = run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "no"])
    if code == 127:
        return [Result("WARN", "Wi-Fi", "nmcli not found", "Raspberry Pi OS Bookworm uses NetworkManager; is this Bookworm?")]
    active = None
    for line in out.splitlines():
        parts = re.split(r"(?<!\\):", line)
        if len(parts) >= 3 and parts[0] == "yes":
            active = (parts[1].replace("\\:", ":"), parts[2], parts[3] if len(parts) > 3 else "")
    _, wired = run(["nmcli", "-t", "-f", "TYPE,STATE", "device"])
    if not active:
        if re.search(r"^ethernet:connected", wired, re.M):
            return [Result("OK", "Network", "wired Ethernet connected (no Wi-Fi in use)")]
        return [Result("FAIL", "Wi-Fi", "not connected to any Wi-Fi network",
                       "Check the network name/password (./install.sh --list-wifi); a Pi Zero W needs 2.4 GHz Wi-Fi.")]
    ssid, signal, security = active
    dbm = (wifi or {}).get("dbm")
    strength = f"signal {signal}%" + (f" ({dbm:.0f} dBm)" if isinstance(dbm, (int, float)) else "")
    detail = f'"{ssid}", {strength}' + (f", {security}" if security else "")
    try:
        pct = int(signal)
    except ValueError:
        pct = None
    if pct is not None and pct < 40:
        return [Result("WARN", "Wi-Fi", detail, "Weak signal: move the Pi closer to the router or use a 2.4 GHz extender.")]
    return [Result("OK", "Wi-Fi", detail)]


def check_internet(resolve=socket.getaddrinfo, fetch=fetch):
    try:
        resolve("github.com", 443)
    except OSError as exc:
        return [Result("FAIL", "Internet", f"can't look up names (DNS): {exc}",
                       "The Wi-Fi is connected but has no internet, or DNS is blocked; try another network.")]
    code, err = fetch("https://www.google.com/generate_204")
    if code is None:
        return [Result("FAIL", "Internet", f"no connection: {err}",
                       "Check the router's internet; on a work or hotel network you may need to sign in first.")]
    if code != 204:
        return [Result("WARN", "Internet", f"a test page returned HTTP {code} instead of 204",
                       "A sign-in page (captive portal) may be in the way; open any website on another device on this network.")]
    return [Result("OK", "Internet", "connected")]


def check_sources(settings, fetch=fetch):
    out = []
    feeds = [u for u in (settings or {}).get("NEWS_FEEDS", ",".join(DEFAULT_FEEDS)).split(",") if u.strip()]
    targets = SERVICES + [(f"News feed {u.split('/')[2]}", u.strip()) for u in feeds]
    channels = [c.strip() for c in (settings or {}).get("VIDEOS_CHANNELS", "").split(",") if c.strip()]
    if channels:  # Videos screen: one request is enough to know YouTube is reachable
        ids = [c for c in channels if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", c)]
        targets.append(("YouTube", "https://www.youtube.com/feeds/videos.xml?channel_id=" + ids[0]
                        if ids else "https://www.youtube.com/"))
    for name, url in targets:
        t0 = time.monotonic()
        code, err = fetch(url)
        ms = int((time.monotonic() - t0) * 1000)
        if code is None:
            out.append(Result("FAIL", name, f"unreachable: {err}",
                              "Blocked by this network's firewall, or the site is down; other screens keep working."))
        elif code == 429:
            out.append(Result("WARN", name, "rate limited (HTTP 429)",
                              "Too many requests; use REFRESH_RATE=auto and a free CoinGecko Demo key."))
        elif 200 <= code < 400:
            out.append(Result("OK", name, f"HTTP {code}, {ms} ms"))
        else:
            out.append(Result("WARN", name, f"HTTP {code}",
                              "The site answered with an error; if it's a news feed, check its URL on the phone page."))
    return out


def check_service(run=run, lines=30):
    out = []
    _, active = run(["systemctl", "is-active", SERVICE])
    _, enabled = run(["systemctl", "is-enabled", SERVICE])
    active, enabled = active.splitlines()[-1] if active else "unknown", enabled.splitlines()[-1] if enabled else "unknown"
    if active == "active":
        out.append(Result("OK", "Service", f"running ({enabled} at boot)"))
    elif "not-found" in enabled or "No such file" in enabled or enabled == "unknown":
        out.append(Result("FAIL", "Service", "not installed", "Run ./install.sh."))
    else:
        out.append(Result("FAIL", "Service", f"{active} ({enabled} at boot)",
                          "Start it: sudo systemctl restart crypto-ticker; the log lines below say why it stopped."))
    if enabled not in ("enabled", "unknown") and active == "active":
        out[-1] = Result("WARN", "Service", f"running but {enabled} at boot",
                         "Turn on auto-start: sudo systemctl enable crypto-ticker")
    code, log = run(["journalctl", "-u", SERVICE, "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=15)
    if code != 0 or not log or log.startswith("-- No entries --"):
        path = os.path.join(HERE, "logs", "ticker.log")
        try:
            with open(path, errors="replace") as fh:
                log = "".join(fh.readlines()[-lines:]).strip()
        except OSError:
            log = ""
    if not log:
        out.append(Result("WARN", "Log", "no log lines available",
                          "Run with sudo to read the system journal: sudo ./doctor.sh"))
        return out, []
    rows = log.splitlines()[-lines:]
    errors = [r for r in rows if is_error(r)]
    out.append(Result("WARN" if errors else "OK", "Log",
                      f"last {len(rows)} lines, {len(errors)} with errors" if errors else f"last {len(rows)} lines, no errors",
                      "See the !! lines below." if errors else ""))
    return out, rows


def is_error(line):
    return bool(re.search(r"\b(ERROR|CRITICAL|Traceback|Exception|failed)\b", line, re.I)) and "WARNING" not in line


def check_resources(path=HERE, meminfo="/proc/meminfo"):
    out = []
    du = shutil.disk_usage(path)
    free_mb = du.free // 2**20
    detail = f"{free_mb:,} MB free of {du.total // 2**20:,} MB"
    if free_mb < 200:
        out.append(Result("FAIL", "Disk", detail, "Almost full: delete old files, or lower LOG_MAX_KB / LOG_BACKUPS."))
    elif free_mb < 1024:
        out.append(Result("WARN", "Disk", detail, "Getting full; logs are capped, but check other large files (du -sh ~/*)."))
    else:
        out.append(Result("OK", "Disk", detail))
    try:
        info = {}
        with open(meminfo) as fh:
            for line in fh:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) // 1024
        avail, total = info.get("MemAvailable", 0), info.get("MemTotal", 0)
        detail = f"{avail} MB available of {total} MB"
        out.append(Result("WARN", "Memory", detail, "Low memory: turn off screens you don't need (news, sports).")
                   if avail < 60 else Result("OK", "Memory", detail))
    except (OSError, ValueError):
        out.append(Result("INFO", "Memory", "unknown (no /proc/meminfo)"))
    return out


def check_panel(settings, model="", modprobe_dir="/etc/modprobe.d", lsmod="/proc/modules"):
    if settings is None:
        return [Result("FAIL", "settings.env", "missing", "Run ./install.sh (it creates settings.env from the example).")]
    s = settings
    rows, cols = s.get("LED_ROWS", "32"), s.get("LED_COLS", "64")
    chain, mapping = s.get("LED_CHAIN", "1"), s.get("LED_GPIO_MAPPING", "adafruit-hat")
    slowdown, seq = s.get("LED_SLOWDOWN_GPIO", "1"), s.get("LED_RGB_SEQUENCE", "RGB")
    detail = (f"LED_ROWS={rows} LED_COLS={cols} LED_CHAIN={chain} LED_GPIO_MAPPING={mapping} "
              f"LED_SLOWDOWN_GPIO={slowdown} LED_RGB_SEQUENCE={seq} BRIGHTNESS={s.get('BRIGHTNESS', '70')}")
    out = [Result("INFO", "Panel settings", detail)]
    problems = []
    if rows not in ("16", "32", "64") or cols not in ("32", "64", "128"):
        problems.append(("WARN", f"unusual size {cols}x{rows}", "A 64x32 panel needs LED_ROWS=32 and LED_COLS=64."))
    if mapping not in GPIO_MAPPINGS:
        problems.append(("FAIL", f"unknown LED_GPIO_MAPPING '{mapping}'",
                         "Use adafruit-hat (or adafruit-hat-pwm if the PWM jumper is soldered)."))
    if sorted(seq.upper()) != ["B", "G", "R"]:
        problems.append(("FAIL", f"LED_RGB_SEQUENCE '{seq}' isn't an order of R, G and B", "Use RGB, RBG, GRB, ..."))
    try:
        sd = int(slowdown)
    except ValueError:
        sd = None
        problems.append(("FAIL", f"LED_SLOWDOWN_GPIO '{slowdown}' isn't a number", "Pi Zero: 0-1, Pi 3: 2, Pi 4/5: 4."))
    if sd is not None:
        if ("Pi 4" in model or "Pi 5" in model) and sd < 3:
            problems.append(("WARN", f"LED_SLOWDOWN_GPIO={sd} is low for a {model.split(' Rev')[0]}",
                             "Pi 4/5 usually need 4; raise it if the panel flickers or shows garbage."))
        elif "Zero" in model and "Zero 2" not in model and sd > 2:
            problems.append(("WARN", f"LED_SLOWDOWN_GPIO={sd} is high for a Pi Zero", "0-1 is usually right; higher is dimmer."))
    for status, detail_, hint in problems:
        out.append(Result(status, "Panel settings", detail_, hint))
    if len(out) == 1:
        out.append(Result("OK", "Panel settings", "values look right for the hardware"))
    # onboard audio shares hardware with the panel timing
    blacklisted = os.path.exists(os.path.join(modprobe_dir, "blacklist-rgb-matrix.conf"))
    try:
        with open(lsmod) as fh:
            loaded = any(line.startswith("snd_bcm2835 ") for line in fh)
    except OSError:
        loaded = False
    if loaded:
        out.append(Result("WARN", "Onboard audio", "still loaded (it disturbs the panel timing)",
                          "Reboot after install; install.sh turns it off" + ("." if blacklisted else " (run ./install.sh).")))
    return out


def check_deploy_key(home, run=run):
    key = os.path.join(home, ".ssh", "crypto-ticker-deploy")
    if not os.path.exists(key):
        return [Result("WARN", "Deploy key", "not created (only needed for the Update button)",
                       "Run ./install.sh --deploy-key and add the printed key on GitHub.")]
    known = os.path.join(home, ".ssh", "known_hosts")
    code, out = run(["ssh", "-T", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                     "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}",
                     "-o", "UpdateHostKeys=no", "-o", "ConnectTimeout=10", "git@github.com"], timeout=20)
    if "successfully authenticated" in out:
        who = re.search(r"Hi ([^!]+)!", out)
        return [Result("OK", "Deploy key", f"works{(' for ' + who.group(1)) if who else ''}")]
    if code == 127:
        return [Result("FAIL", "Deploy key", "the SSH client is missing", "sudo apt-get install -y openssh-client")]
    if "Host key verification failed" in out or "No ED25519 host key" in out:
        return [Result("WARN", "Deploy key", "GitHub's host key isn't in known_hosts yet",
                       "Run ./install.sh --deploy-key once (it adds GitHub's key).")]
    if "Permission denied" in out:
        return [Result("FAIL", "Deploy key", "GitHub refused the key",
                       "Add the key on GitHub: repo -> Settings -> Deploy keys (show it with ./install.sh --deploy-key).")]
    return [Result("FAIL", "Deploy key", f"couldn't reach GitHub over SSH: {out.splitlines()[-1][:100] if out else code}",
                   "Outgoing SSH (port 22) may be blocked on this network.")]


# --- report ----------------------------------------------------------------------------
def report(results, log_rows):
    lines = [f"Crypto Ticker doctor, {time.strftime('%Y-%m-%d %H:%M %Z')} (read-only: nothing was changed)", ""]
    for r in results:
        lines.append(f"[{r.status}]".ljust(7) + f"{r.name}: {r.detail}")
        if r.hint and r.status in ("WARN", "FAIL"):
            lines.append(" " * 7 + f"Fix: {r.hint}")
    if log_rows:
        lines += ["", f"--- last {len(log_rows)} log lines (!! = error) ---"]
        lines += [("!! " if is_error(row) else "   ") + redact(row) for row in log_rows]
    counts = {s: sum(r.status == s for r in results) for s in ("OK", "WARN", "FAIL")}
    lines += ["", f"Summary: {counts['OK']} OK, {counts['WARN']} WARN, {counts['FAIL']} FAIL"]
    return "\n".join(lines)


def redact(text):
    """Hide anything that looks like a secret in a log line."""
    text = re.sub(r"(?i)\b([\w.-]*(?:pin|password|passwd|psk|api[_-]?key|token|secret))(\s*[=:]\s*)\S+",
                  r"\1\2***", text)
    return re.sub(r"x-cg-(demo|pro)-api-key['\"]?\s*[:=]\s*['\"]?[\w-]+", r"x-cg-\1-api-key=***", text)


def main(argv=None, repo=HERE, run=run, fetch=fetch, wifi=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    settings = read_settings(os.path.join(repo, "settings.env"))
    results = check_system()
    model = next((r.detail for r in results if r.name == "Pi model" and r.status == "OK"), "")
    if wifi is None:
        try:
            sys.path.insert(0, repo)
            import sysinfo
            wifi = sysinfo.wifi()
        except Exception:
            wifi = None
    results += check_wifi(run, wifi)
    results += check_internet(fetch=fetch)
    results += check_sources(settings, fetch)
    svc, log_rows = check_service(run)
    results += svc
    results += check_resources(repo)
    results += check_panel(settings, model)
    results += check_deploy_key(owner_home(repo), run)
    print(report(results, log_rows))
    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
