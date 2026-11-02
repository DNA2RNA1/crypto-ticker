"""Work Wi-Fi (WPA2-Enterprise, PEAP + MSCHAPv2) profile: keyfile, validation, install.sh.

No real NetworkManager is touched: the profile goes to a temp folder (CT_NM_DIR) and
`nmcli` is a stub that records its arguments. No real credentials are used anywhere.
"""

import base64
import configparser
import os
import shutil
import stat
import subprocess
import sys

import pytest

import wifi_enterprise as we

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = "Fake-Passw0rd;with#odd\\chars"   # made up for the tests
DER = bytes([0x30, 0x82, 0x01, 0x0a]) + b"\x00" * 16
PEM = (b"-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(DER) + b"-----END CERTIFICATE-----\n")


# --- a stub nmcli that remembers loaded profiles --------------------------------------
NMCLI_STUB = r"""#!/bin/sh
D="$(dirname "$0")"
echo "$*" >> "$D/nmcli.log"
case "$*" in
  "-t -f RUNNING general") echo running ;;
  "-t -f NAME connection show") echo "preconfigured"; [ -f "$D/loaded" ] && cat "$D/loaded" ;;
  "connection load "*) echo ticker-work-wifi > "$D/loaded" ;;
  "connection delete id ticker-work-wifi") rm -f "$D/loaded" ;;
  *) echo "unexpected nmcli call: $*" >&2; exit 9 ;;
esac
"""


@pytest.fixture
def nm(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "nmcli"
    stub.write_text(NMCLI_STUB)
    stub.chmod(0o755)
    nm_dir, ca_dir = tmp_path / "system-connections", tmp_path / "ca"
    monkeypatch.setattr(we, "NM_DIR", str(nm_dir))
    monkeypatch.setattr(we, "CA_DIR", str(ca_dir))
    monkeypatch.setattr(we, "NMCLI", str(stub))
    return {"bin": bin_dir, "nm_dir": nm_dir, "ca_dir": ca_dir, "log": bin_dir / "nmcli.log", "tmp": tmp_path}


def parse(text):
    cp = configparser.ConfigParser(interpolation=None, comment_prefixes=("#",))
    cp.optionxform = str
    cp.read_string(text)
    return cp


# --- the keyfile ----------------------------------------------------------------------------
def test_keyfile_peap_mschapv2_with_ca_and_domain():
    cp = parse(we.keyfile("Corp-WiFi", "jdoe@example.com", PASSWORD, "anonymous@example.com", "file",
                          "/etc/crypto-ticker/work-wifi-ca.pem", "radius.example.com", uid="u-1"))
    assert cp["connection"]["id"] == "ticker-work-wifi" and cp["connection"]["type"] == "wifi"
    assert cp["connection"]["autoconnect"] == "true" and cp["connection"]["autoconnect-priority"] == "-1"
    assert cp["wifi"]["ssid"] == "Corp-WiFi" and cp["wifi-security"]["key-mgmt"] == "wpa-eap"
    x = cp["802-1x"]
    assert (x["eap"], x["phase2-auth"]) == ("peap;", "mschapv2")
    assert x["identity"] == "jdoe@example.com" and x["anonymous-identity"] == "anonymous@example.com"
    assert x["password-flags"] == "0" and x["password"] == PASSWORD.replace("\\", "\\\\")
    assert x["ca-cert"] == "/etc/crypto-ticker/work-wifi-ca.pem" and x["domain-suffix-match"] == "radius.example.com"
    assert "system-ca-certs" not in x


def test_keyfile_system_cas_and_skipped_check():
    sys_ = parse(we.keyfile("W", "u", "p", check="system", domain="radius.example.com"))["802-1x"]
    assert sys_["system-ca-certs"] == "true" and sys_["domain-suffix-match"] == "radius.example.com"
    assert "ca-cert" not in sys_
    none = parse(we.keyfile("W", "u", "p", check="none", domain="radius.example.com"))["802-1x"]
    assert not {"ca-cert", "system-ca-certs", "domain-suffix-match"} & set(none)
    assert "anonymous-identity" not in none


@pytest.mark.parametrize("ssid,want", [
    ("Corp WiFi", "Corp WiFi"),
    ("Café;Guest", "".join(f"{b};" for b in "Café;Guest".encode())),   # unusual: byte list
    (" lead", "".join(f"{b};" for b in b" lead")),
])
def test_ssid_encoding(ssid, want):
    assert we._ssid(ssid) == want


def test_escaping_leading_space_and_backslash():
    assert we._escape(" a\\b") == "\\sa\\\\b"


# --- validation -----------------------------------------------------------------------------
@pytest.mark.parametrize("args,msg", [
    (("", "u", "p"), "Network name is empty"),
    (("x" * 33, "u", "p"), "too long"),
    (("é" * 17, "u", "p"), "32 bytes"),
    (("W", "", "p"), "Username is empty"),
    (("W", "u\nx", "p"), "control character"),
    (("W", "u", ""), "Password is empty"),
    (("W", "u", "pa\tss"), "control character"),
    (("W", "u", "p", "", "maybe"), "server check"),
    (("W", "u", "p", "", "system"), "needs the server domain"),
    (("W", "u", "p", "", "system", "", "not a domain!"), "radius.example.com"),
    (("W", "u", "p", "", "file", "/nonexistent.pem"), "not found"),
])
def test_validation(args, msg):
    with pytest.raises(we.WifiError, match=msg):
        we.validate(*args)


def test_ca_must_be_a_certificate(tmp_path):
    bad = tmp_path / "bad.pem"
    bad.write_bytes(b"hello")
    with pytest.raises(we.WifiError, match="no PEM certificate"):
        we.read_ca(str(bad))
    junk = tmp_path / "junk.pem"
    junk.write_bytes(b"-----BEGIN CERTIFICATE-----\n@@@@\n-----END CERTIFICATE-----\n")
    with pytest.raises(we.WifiError, match="base64"):
        we.read_ca(str(junk))
    good = tmp_path / "ca.pem"
    good.write_bytes(PEM)
    assert we.read_ca(str(good)) == PEM


# --- add / remove ------------------------------------------------------------------------------
def test_add_writes_root_only_profile_and_loads_it(nm):
    ca = nm["tmp"] / "ca.pem"
    ca.write_bytes(PEM)
    path = we.add("Corp-WiFi", "jdoe", PASSWORD, "", "file", str(ca), "radius.example.com")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    installed = nm["ca_dir"] / "work-wifi-ca.pem"
    assert installed.read_bytes() == PEM and stat.S_IMODE(installed.stat().st_mode) == 0o644
    assert parse(open(path).read())["802-1x"]["ca-cert"] == str(installed)
    log = nm["log"].read_text()
    assert f"connection load {path}" in log
    assert PASSWORD not in log and "jdoe" not in log            # secrets never on a command line
    assert os.listdir(nm["nm_dir"]) == ["ticker-work-wifi.nmconnection"]   # nothing else touched


def test_add_replaces_existing_profile(nm):
    we.add("Old", "u", "p")
    we.add("New", "u", "p2")
    log = nm["log"].read_text().splitlines()
    assert "connection delete id ticker-work-wifi" in log
    assert parse(open(we.profile_path()).read())["wifi"]["ssid"] == "New"


def test_invalid_input_writes_nothing(nm):
    with pytest.raises(we.WifiError):
        we.add("W", "u", "bad\npassword")
    assert not nm["nm_dir"].exists() and not nm["log"].exists()


def test_remove(nm):
    ca = nm["tmp"] / "ca.pem"
    ca.write_bytes(PEM)
    we.add("Corp", "u", "p", "", "file", str(ca))
    assert we.remove() is True
    assert not os.path.exists(we.profile_path()) and not (nm["ca_dir"] / "work-wifi-ca.pem").exists()
    assert "connection delete id ticker-work-wifi" in nm["log"].read_text()
    assert we.remove() is False


def test_main_reads_nul_separated_stdin(nm, monkeypatch, capsys):
    import io
    data = "\0".join(["Corp", "jdoe", PASSWORD, "", "none", "", ""]).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
    monkeypatch.setenv("CT_NM_DIR", str(nm["nm_dir"]))
    assert we.main(["add"]) == 0
    assert PASSWORD not in capsys.readouterr().out
    assert parse(open(we.profile_path()).read())["802-1x"]["password"] == PASSWORD.replace("\\", "\\\\")


# --- install.sh, end to end with stubs -----------------------------------------------------------
def make_stubs(tmp, log):
    """A PATH with stand-ins for sudo (runs the command, logs it), nmcli, ssh-keygen and a
    python3 whose 'venv' needs no network."""
    b = tmp / "stubs"
    b.mkdir(exist_ok=True)
    stubs = {
        "sudo": f'#!/bin/sh\necho "sudo $*" >> {log}\ncase "$1" in\n  apt-get|tee|systemctl|mkdir|systemd-analyze) cat >/dev/null 2>&1; exit 0 ;;\nesac\nexec "$@"\n',
        "ssh-keygen": '#!/bin/sh\nwhile [ $# -gt 1 ]; do [ "$1" = -f ] && f="$2"; shift; done\n'
                      'echo key > "$f"; echo "ssh-ed25519 AAAATEST test" > "$f.pub"\n',
        "ssh-keyscan": "#!/bin/sh\nexit 0\n",
        "python3": f'#!/bin/sh\nif [ "$1" = -m ] && [ "$2" = venv ]; then mkdir -p "$3/bin"\n'
                   f'  printf \'#!/bin/sh\\nexit 0\\n\' > "$3/bin/pip"\n'
                   f'  printf \'#!/bin/sh\\nexec {sys.executable} "$@"\\n\' > "$3/bin/python"\n'
                   f'  chmod +x "$3/bin/pip" "$3/bin/python"; exit 0; fi\nexec {sys.executable} "$@"\n',
    }
    for name, body in stubs.items():
        (b / name).write_text(body)
        (b / name).chmod(0o755)
    (b / "nmcli").write_text(NMCLI_STUB)
    (b / "nmcli").chmod(0o755)
    return b


def copy_repo(tmp):
    dest = tmp / "crypto-ticker"
    shutil.copytree(ROOT, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "venv", "cache", "logs", "settings.env",
                                                             ".launcher", "__pycache__", "docs", "fonts", "tests"))
    return dest


def run_install(tmp, args, stdin=""):
    log = tmp / "sudo.log"
    stubs = make_stubs(tmp, log)
    repo = copy_repo(tmp)
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(home),
           "CT_NM_DIR": str(tmp / "nm"), "CT_CA_DIR": str(tmp / "ca"), "CT_NMCLI": str(stubs / "nmcli")}
    p = subprocess.run(["bash", str(repo / "install.sh"), *args], input=stdin, capture_output=True, text=True,
                       env=env, cwd=repo, timeout=120)
    return p, log.read_text() if log.exists() else "", (stubs / "nmcli.log"), repo


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_normal_install_leaves_wifi_alone(tmp_path):
    p, sudo_log, nmcli_log, repo = run_install(tmp_path, ["--skip-build", "--no-service", "--pin", "4826"])
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    assert not nmcli_log.exists()                                   # nmcli never called
    assert "wifi_enterprise" not in sudo_log and "NetworkManager" not in sudo_log
    assert not (tmp_path / "nm").exists() and not (tmp_path / "ca").exists()
    assert "Done." in p.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("answers,check", [
    (["Corp-WiFi", "jdoe", PASSWORD, PASSWORD, "", "3", "skip"], "none"),
    (["Corp-WiFi", "jdoe", PASSWORD, PASSWORD, "anon@example.com", "2", "radius.example.com"], "system"),
    (["Corp-WiFi", "jdoe", PASSWORD, "typo", PASSWORD, PASSWORD, "", "4", "3", "no", "1", "{ca}",
      "radius.example.com"], "file"),
])
def test_enterprise_wifi_flag(tmp_path, answers, check):
    ca = tmp_path / "ca-from-it.pem"
    ca.write_bytes(PEM)
    stdin = "\n".join(a.replace("{ca}", str(ca)) for a in answers) + "\n"
    p, sudo_log, nmcli_log, repo = run_install(tmp_path, ["--enterprise-wifi"], stdin)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    prof = tmp_path / "nm" / "ticker-work-wifi.nmconnection"
    assert stat.S_IMODE(prof.stat().st_mode) == 0o600
    x = parse(prof.read_text())["802-1x"]
    assert x["identity"] == "jdoe" and x["password"] == PASSWORD.replace("\\", "\\\\")
    assert ("ca-cert" in x) == (check == "file") and ("system-ca-certs" in x) == (check == "system")
    if check == "none":
        assert "can be cracked offline" in p.stderr
    assert PASSWORD not in sudo_log and PASSWORD not in nmcli_log.read_text() and PASSWORD not in p.stdout
    assert "apt-get" not in sudo_log and "systemctl" not in sudo_log      # nothing else installed
    assert not (repo / "venv").exists() and not (repo / "settings.env").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_remove_enterprise_wifi_flag(tmp_path):
    answers = "\n".join(["Corp", "u", "p", "p", "", "3", "skip"]) + "\n"
    run_install(tmp_path, ["--enterprise-wifi"], answers)
    assert (tmp_path / "nm" / "ticker-work-wifi.nmconnection").exists()
    p, _, nmcli_log, _ = run_install(tmp_path, ["--remove-enterprise-wifi"])
    assert p.returncode == 0 and "Removed the work Wi-Fi profile" in p.stdout
    assert not (tmp_path / "nm" / "ticker-work-wifi.nmconnection").exists()


def test_no_credentials_in_repo():
    """Nothing that looks like a saved Wi-Fi profile or secret is tracked."""
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert not [f for f in tracked if f.endswith((".nmconnection", ".pem", ".crt", ".key"))]
    gitignore = open(os.path.join(ROOT, ".gitignore")).read()
    assert "*.nmconnection" in gitignore
