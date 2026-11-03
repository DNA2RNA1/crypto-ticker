"""install.sh's LED-library build step: fetching Pillow's C headers (sparse first, full
clone as fallback), cleaning up old header folders, and recording the Pillow version;
plus doctor.sh's check of that record. git and pip are stubs; nothing is downloaded."""

import os
import shutil
import subprocess
import sys

import pytest

import doctor as dr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# git stub: logs every call; makes the folders a real clone would. GIT_MODE=nosparse makes
# the sparse clone fail (old git / server without filter support).
GIT = r"""#!/bin/sh
echo "git $*" >> "$CT_LOG"
case "$*" in
  *"--sparse"*)
    [ "$GIT_MODE" = nosparse ] && { echo "fatal: filtering not recognized by server" >&2; exit 128; }
    for a; do dest="$a"; done; mkdir -p "$dest/.git"; exit 0 ;;
  "-C "*" sparse-checkout set --no-cone /src/libImaging/")
    mkdir -p "$2/src/libImaging"; echo "/* Imaging.h */" > "$2/src/libImaging/Imaging.h"; exit 0 ;;
  "clone -q --depth 1 --branch "*)
    for a; do dest="$a"; done; mkdir -p "$dest/src/libImaging" "$dest/Tests/images"
    echo "/* Imaging.h */" > "$dest/src/libImaging/Imaging.h"; exit 0 ;;
  "clone --depth 1 https://github.com/hzeller/"*)
    for a; do dest="$a"; done; mkdir -p "$dest"; exit 0 ;;
  *) exit 0 ;;
esac
"""
SUDO = '#!/bin/sh\necho "sudo $*" >> "$CT_LOG"\ncat >/dev/null 2>&1\nexit 0\n'
SSH_KEYGEN = '#!/bin/sh\nwhile [ $# -gt 1 ]; do [ "$1" = -f ] && f="$2"; shift; done\necho k > "$f"; echo "ssh-ed25519 AAAA t" > "$f.pub"\n'
# python3 stub: "-m venv DIR" makes a venv whose python reports Pillow 13.0.0 and whose pip
# logs its arguments and the CPATH it was given.
PY = r"""#!/bin/sh
if [ "$1" = -m ] && [ "$2" = venv ]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/pip" <<'EOF'
#!/bin/sh
echo "pip $* CPATH=${CPATH:-}" >> "$CT_LOG"
EOF
  cat > "$3/bin/python" <<EOF
#!/bin/sh
case "\$*" in
  *"import PIL"*) echo 13.0.0 ;;
  *"import rgbmatrix"*) exit 0 ;;
  *) exec {real} "\$@" ;;
esac
EOF
  chmod +x "$3/bin/pip" "$3/bin/python"; exit 0
fi
exec {real} "$@"
""".replace("{real}", sys.executable)


def run_build(tmp, git_mode="", pre=()):
    stubs = tmp / "stubs"
    stubs.mkdir()
    for name, body in {"git": GIT, "sudo": SUDO, "ssh-keygen": SSH_KEYGEN, "ssh-keyscan": "#!/bin/sh\nexit 0\n",
                       "python3": PY}.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    repo = tmp / "crypto-ticker"
    shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", "venv", "cache", "logs", "settings.env",
                                                             ".launcher", "__pycache__", "docs", "fonts", "tests"))
    for d in pre:
        (tmp / d).mkdir(parents=True, exist_ok=True)
    (tmp / "home").mkdir()
    log = tmp / "calls.log"
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp / "home"), "CT_LOG": str(log), "GIT_MODE": git_mode}
    p = subprocess.run(["bash", str(repo / "install.sh"), "--no-service", "--pin", "4826"], capture_output=True,
                       text=True, env=env, timeout=120)
    return p, log.read_text(), repo


def test_sparse_fetch_of_just_the_headers(tmp_path):
    p, log, repo = run_build(tmp_path, pre=["pillow-src-12.1.0/src", "pillow-src-11.0.0", "other-project"])
    assert p.returncode == 0, p.stdout[-1500:] + p.stderr[-1500:]
    src = tmp_path / "pillow-src-13.0.0"
    assert "--filter=blob:none --sparse --branch 13.0.0 https://github.com/python-pillow/Pillow.git" in log
    assert f"-C {src} sparse-checkout set --no-cone /src/libImaging/" in log
    assert "clone -q --depth 1 --branch 13.0.0" not in log                 # no full clone needed
    assert (src / "src/libImaging/Imaging.h").exists()
    pip_line = next(line for line in log.splitlines() if line.startswith("pip install") and "rpi-rgb-led-matrix" in line)
    assert f"CPATH={src}/src/libImaging" in pip_line                         # compiler finds Imaging.h
    assert not (tmp_path / "pillow-src-12.1.0").exists() and not (tmp_path / "pillow-src-11.0.0").exists()
    assert (tmp_path / "other-project").exists()                             # only old header folders go
    assert (repo / "venv/.led-built-with-pillow").read_text().strip() == "13.0.0"


def test_falls_back_to_a_full_clone(tmp_path):
    p, log, repo = run_build(tmp_path, git_mode="nosparse")
    assert p.returncode == 0, p.stdout[-1500:] + p.stderr[-1500:]
    assert "--sparse" in log and "clone -q --depth 1 --branch 13.0.0 https://github.com/python-pillow" in log
    assert (tmp_path / "pillow-src-13.0.0/src/libImaging/Imaging.h").exists()


def test_existing_headers_are_reused(tmp_path):
    hdr = tmp_path / "pillow-src-13.0.0/src/libImaging"
    hdr.mkdir(parents=True)
    (hdr / "Imaging.h").write_text("/* already here */")
    p, log, repo = run_build(tmp_path)
    assert p.returncode == 0
    assert "python-pillow" not in log                                        # nothing fetched again
    assert (hdr / "Imaging.h").read_text() == "/* already here */"


# --- doctor.sh: was the LED library built against the Pillow that's installed now? ----------
def fake_venv(tmp_path, built=None, installed=None):
    venv = tmp_path / "venv"
    (venv / "lib/python3.11/site-packages/PIL").mkdir(parents=True)
    if built:
        (venv / ".led-built-with-pillow").write_text(built + "\n")
    if installed:
        (venv / "lib/python3.11/site-packages/PIL/_version.py").write_text(
            f'# Master version for Pillow\nfrom __future__ import annotations\n\n__version__ = "{installed}"\n')
    return tmp_path


@pytest.mark.parametrize("built,installed,want", [
    ("13.0.0", "13.0.0", "OK"), ("12.1.0", "13.0.0", "WARN"), ("13.0.0", None, "WARN"), (None, "13.0.0", None),
])
def test_doctor_led_library_check(tmp_path, built, installed, want):
    r = dr.check_led_library(str(fake_venv(tmp_path, built, installed)))
    assert (r[0].status if r else None) == want
    if want == "WARN" and installed:
        assert "12.1.0" in r[0].detail and "13.0.0" in r[0].detail and "./install.sh" in r[0].hint
