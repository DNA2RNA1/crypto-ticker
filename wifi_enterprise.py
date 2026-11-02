#!/usr/bin/env python3
"""Add or remove a second Wi-Fi profile for a WPA2-Enterprise (PEAP + MSCHAPv2) network.

Run through install.sh, which asks the questions:

    ./install.sh --enterprise-wifi          add (or replace) the work Wi-Fi profile
    ./install.sh --remove-enterprise-wifi   remove it again

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
