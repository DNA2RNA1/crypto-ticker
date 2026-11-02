#!/usr/bin/env python3
"""Extra Wi-Fi profiles: a WPA2-Enterprise (PEAP + MSCHAPv2) work network, ordinary
networks such as a phone hotspot, and a list of what's saved.

Run through install.sh, which asks the questions:

    ./install.sh --enterprise-wifi          add (or replace) the work Wi-Fi profile
    ./install.sh --remove-enterprise-wifi   remove it again
    ./install.sh --add-wifi                 save an ordinary network (e.g. a phone hotspot)
    ./install.sh --list-wifi                list saved networks and the ones in range

It writes one NetworkManager keyfile, /etc/NetworkManager/system-connections/
ticker-work-wifi.nmconnection (root-only, mode 600), and loads it. Nothing else about
the network setup is touched; the home Wi-Fi profile stays exactly as it is and
NetworkManager joins whichever network is in range.

The password is stored in that root-only file in plain text. NetworkManager can't use
an NT hash for MSCHAPv2: it passes the password to wpa_supplicant over D-Bus as raw
bytes, and wpa_supplicant only understands "hash:..." in its own config files, so a
hash would be sent as the literal password and every login would fail.

Fields are read from stdin, NUL-separated, so the password never appears in a command
line or the environment:
    ssid, identity, password, anonymous identity, check (file|system|none), CA file, domain
"""

import base64
import os
import re
import subprocess
import sys
import uuid

PROFILE = "ticker-work-wifi"
NM_DIR = os.environ.get("CT_NM_DIR", "/etc/NetworkManager/system-connections")
CA_DIR = os.environ.get("CT_CA_DIR", "/etc/crypto-ticker")
CA_FILE = "work-wifi-ca.pem"
NMCLI = os.environ.get("CT_NMCLI", "nmcli")
CHECKS = ("file", "system", "none")
MAX_CA_BYTES = 64 * 1024


class WifiError(Exception):
    pass


# --- validation -------------------------------------------------------------------
def _no_control(name, value, max_len):
    if not value:
        raise WifiError(f"{name} is empty")
    if len(value) > max_len:
        raise WifiError(f"{name} is too long (max {max_len} characters)")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise WifiError(f"{name} contains a control character (newline, tab, ...)")
    return value


def validate(ssid, identity, password, anonymous="", check="none", ca_path="", domain=""):
    _no_control("Network name", ssid, 32)
    if len(ssid.encode("utf-8")) > 32:
        raise WifiError("Network name is longer than 32 bytes")
    _no_control("Username", identity, 253)
    _no_control("Password", password, 256)
    if anonymous:
        _no_control("Anonymous identity", anonymous, 253)
    if check not in CHECKS:
        raise WifiError(f"server check must be one of {', '.join(CHECKS)}")
    if domain and not re.fullmatch(r"(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)*[A-Za-z0-9-]{1,63}", domain):
        raise WifiError("server domain should look like radius.example.com")
    if check == "file":
        read_ca(ca_path)
    if check == "system" and not domain:
        raise WifiError("checking against the system certificates needs the server domain")
    return True


def read_ca(path):
    """The PEM certificate(s) in ``path``; raises WifiError if it isn't one."""
    if not path or not os.path.isfile(path):
        raise WifiError(f"certificate file not found: {path or '(none)'}")
    if os.path.getsize(path) > MAX_CA_BYTES:
        raise WifiError("certificate file is too large")
    with open(path, "rb") as fh:
        data = fh.read()
    blocks = re.findall(rb"-----BEGIN CERTIFICATE-----\s*(.+?)\s*-----END CERTIFICATE-----", data, re.S)
    if not blocks:
        raise WifiError("that file has no PEM certificate (-----BEGIN CERTIFICATE-----)")
    for b in blocks:
        try:
            der = base64.b64decode(re.sub(rb"\s+", b"", b), validate=True)
        except ValueError:
            raise WifiError("the certificate isn't valid base64") from None
        if not der.startswith(b"\x30"):
            raise WifiError("the certificate isn't a DER/X.509 certificate")
    return data


# --- keyfile ----------------------------------------------------------------------------
def _escape(value):
    """GKeyFile string escaping (control characters are rejected before this)."""
    value = value.replace("\\", "\\\\")
    if value.startswith(" "):
        value = "\\s" + value[1:]
    return value


def _ssid(value):
    """NetworkManager keyfile ssid: plain text when unambiguous, else a byte list."""
    if re.fullmatch(r"[A-Za-z0-9_.@:+/()!?#$%&*,<>=\[\]{}~^|'\"-]+( [A-Za-z0-9_.@:+/()!?#$%&*,<>=\[\]{}~^|'\"-]+)*", value):
        return value
    return "".join(f"{b};" for b in value.encode("utf-8"))


def keyfile(ssid, identity, password, anonymous="", check="none", ca_installed="", domain="", uid=None):
    """The NetworkManager keyfile text for the profile."""
    lines = [
        "[connection]",
        f"id={PROFILE}",
        f"uuid={uid or uuid.uuid4()}",
        "type=wifi",
        "autoconnect=true",
        "# the home Wi-Fi (priority 0) wins if both networks are in range",
        "autoconnect-priority=-1",
        "",
        "[wifi]",
        "mode=infrastructure",
        f"ssid={_ssid(ssid)}",
        "",
        "[wifi-security]",
        "key-mgmt=wpa-eap",
        "",
        "[802-1x]",
        "eap=peap;",
        "phase2-auth=mschapv2",
        f"identity={_escape(identity)}",
    ]
    if anonymous:
        lines.append(f"anonymous-identity={_escape(anonymous)}")
    lines += ["password-flags=0", f"password={_escape(password)}"]
    if check == "file":
        lines.append(f"ca-cert={ca_installed}")
    elif check == "system":
        lines.append("system-ca-certs=true")
    if domain and check != "none":
        lines.append(f"domain-suffix-match={_escape(domain)}")
    lines += ["", "[ipv4]", "method=auto", "", "[ipv6]", "addr-gen-mode=default", "method=auto", ""]
    return "\n".join(lines)


def _write_private(path, data, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data.encode("utf-8") if isinstance(data, str) else data)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def profile_path():
    return os.path.join(NM_DIR, f"{PROFILE}.nmconnection")


def nmcli(*args, check=True):
    p = subprocess.run([NMCLI, *args], capture_output=True, text=True)
    if check and p.returncode:
        raise WifiError(f"nmcli {' '.join(args[:2])} failed: {(p.stderr or p.stdout).strip()[:300]}")
    return p


def exists():
    p = nmcli("-t", "-f", "NAME", "connection", "show", check=False)
    return p.returncode == 0 and PROFILE in p.stdout.splitlines()


def add(ssid, identity, password, anonymous="", check="none", ca_path="", domain=""):
    validate(ssid, identity, password, anonymous, check, ca_path, domain)
    ca_installed = ""
    if check == "file":
        data = read_ca(ca_path)
        os.makedirs(CA_DIR, exist_ok=True)
        ca_installed = os.path.join(CA_DIR, CA_FILE)
        _write_private(ca_installed, data, 0o644)   # a CA certificate is public
    if exists():
        nmcli("connection", "delete", "id", PROFILE)
    os.makedirs(NM_DIR, exist_ok=True)
    path = profile_path()
    _write_private(path, keyfile(ssid, identity, password, anonymous, check, ca_installed, domain))
    nmcli("connection", "load", path)
    return path


def remove():
    removed = False
    if exists():
        nmcli("connection", "delete", "id", PROFILE)
        removed = True
    for path in (profile_path(), os.path.join(CA_DIR, CA_FILE)):
        if os.path.exists(path):
            os.remove(path)
            removed = True
    return removed


# --- ordinary (WPA2/WPA3-Personal or open) networks, e.g. a phone hotspot ----------------
EXTRA_PREFIX = "ticker-wifi-"


def extra_id(ssid):
    """Profile name for an extra network: ticker-wifi-<ssid with only safe characters>."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", ssid).strip("-")[:40] or "network"
    return EXTRA_PREFIX + slug


def validate_psk(ssid, password):
    _no_control("Network name", ssid, 32)
    if len(ssid.encode("utf-8")) > 32:
        raise WifiError("Network name is longer than 32 bytes")
    if password:
        if re.fullmatch(r"[0-9A-Fa-f]{64}", password):
            return True
        if not 8 <= len(password) <= 63 or not all(32 <= ord(c) <= 126 for c in password):
            raise WifiError("A Wi-Fi password is 8 to 63 ordinary characters (letters, digits, symbols)")
    return True


def psk_keyfile(ssid, password, hidden=False, uid=None):
    lines = ["[connection]", f"id={extra_id(ssid)}", f"uuid={uid or uuid.uuid4()}", "type=wifi",
             "autoconnect=true", "# the home Wi-Fi (priority 0) wins if both networks are in range",
             "autoconnect-priority=-1", "", "[wifi]", "mode=infrastructure", f"ssid={_ssid(ssid)}"]
    if hidden:
        lines.append("hidden=true")
    if password:
        lines += ["", "[wifi-security]", "key-mgmt=wpa-psk", "psk-flags=0", f"psk={_escape(password)}"]
    lines += ["", "[ipv4]", "method=auto", "", "[ipv6]", "addr-gen-mode=default", "method=auto", ""]
    return "\n".join(lines)


def add_psk(ssid, password, hidden=""):
    """Save an ordinary Wi-Fi network (root-only file, loaded with nmcli). Doesn't connect now."""
    validate_psk(ssid, password)
    name = extra_id(ssid)
    p = nmcli("-t", "-f", "NAME", "connection", "show", check=False)
    if p.returncode == 0 and name in p.stdout.splitlines():
        nmcli("connection", "delete", "id", name)
    os.makedirs(NM_DIR, exist_ok=True)
    path = os.path.join(NM_DIR, f"{name}.nmconnection")
    _write_private(path, psk_keyfile(ssid, password, hidden.lower() in ("y", "yes", "1", "true")))
    nmcli("connection", "load", path)
    return name


def _split(line):
    return [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", line)]


def list_networks():
    """Saved Wi-Fi profiles (no passwords) and the networks in range right now."""
    p = nmcli("-t", "-f", "NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,ACTIVE", "connection", "show", check=False)
    if p.returncode:
        raise WifiError(f"nmcli couldn't list connections: {(p.stderr or p.stdout).strip()[:200]}")
    saved = []
    for line in p.stdout.splitlines():
        f = _split(line)
        if len(f) >= 5 and f[1] in ("802-11-wireless", "wifi"):
            ssid = nmcli("-g", "802-11-wireless.ssid", "connection", "show", "id", f[0], check=False).stdout.strip()
            saved.append({"name": f[0], "ssid": ssid or "?", "auto": f[2] == "yes", "priority": f[3],
                          "active": f[4] == "yes"})
    scan = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "no", check=False)
    in_range = {}
    for line in scan.stdout.splitlines():
        f = _split(line)
        if len(f) >= 2 and f[0]:
            try:
                in_range[f[0]] = max(in_range.get(f[0], 0), int(f[1]))
            except ValueError:
                pass
    return saved, in_range


def format_networks(saved, in_range):
    out = ["Saved Wi-Fi networks (the Pi joins any of these that's in range; passwords not shown):"]
    if not saved:
        out.append("  (none)")
    for n in sorted(saved, key=lambda n: (-int(n["priority"] or 0), n["name"])):
        state = "connected" if n["active"] else (f"in range, {in_range[n['ssid']]}%" if n["ssid"] in in_range
                                                 else "not in range")
        auto = "" if n["auto"] else ", auto-join off"
        out.append(f"  {n['ssid']:<24} {state:<20} priority {n['priority']}{auto}   ({n['name']})")
    others = sorted(s for s in in_range if s not in {n["ssid"] for n in saved})
    if others:
        out.append("Other networks in range: " + ", ".join(f"{s} ({in_range[s]}%)" for s in others[:12]))
    return "\n".join(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if os.geteuid() != 0 and "CT_NM_DIR" not in os.environ:
        print("ERROR: run this as root (install.sh --enterprise-wifi uses sudo)", file=sys.stderr)
        return 2
    try:
        if argv[0] == "add":
            fields = sys.stdin.buffer.read().decode("utf-8").split("\0")
            fields = (fields + [""] * 7)[:7]
            path = add(*fields)
            print(f"Saved the work Wi-Fi profile ({path}, readable by root only).")
        elif argv[0] == "add-psk":
            fields = sys.stdin.buffer.read().decode("utf-8").split("\0")
            fields = (fields + [""] * 3)[:3]
            name = add_psk(*fields)
            print(f"Saved \"{fields[0]}\" as {name} (readable by root only). The Pi joins it when it's in range.")
        elif argv[0] == "list":
            print(format_networks(*list_networks()))
        elif argv[0] == "remove":
            print("Removed the work Wi-Fi profile." if remove() else "There was no work Wi-Fi profile.")
        elif argv[0] == "exists":
            return 0 if exists() else 1
        else:
            print(f"unknown command {argv[0]!r}", file=sys.stderr)
            return 2
    except (WifiError, OSError, UnicodeDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
