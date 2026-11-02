"""Update / roll back with real git repositories (a bare "GitHub" repo, a developer
clone that pushes new versions, and a "Pi" clone). pip is faked by a stand-in venv
python that logs what it was asked to do."""

import os
import shutil
import stat
import subprocess
import sys

import pytest

import launcher as lx
import updater as up

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

TICKER_OK = 'import sys\nif "--check" in sys.argv:\n    print("ticker self-check ok")\n    sys.exit(0)\n'
TICKER_BROKEN = 'import sys\nif "--check" in sys.argv:\n    raise ImportError("no module named oops")\n'
FAKE_PY = """#!/bin/sh
D="$(cd "$(dirname "$0")/../.." && pwd)"
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  echo "$*" >> "$D/pip.log"
  case "$3" in
    freeze) cat "$D/freeze.txt" ;;
    install) if [ -f "$D/pip-fail" ] && echo "$*" | grep -q requirements.txt; then
               echo "ERROR: No matching distribution found for pillow==99"; exit 1; fi ;;
  esac
  exit 0
fi
exec "%s" "$@"
""" % sys.executable


def sh(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com"))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class World:
    def __init__(self, tmp):
        self.origin = str(tmp / "origin.git")
        self.dev = str(tmp / "dev")
        self.pi = str(tmp / "pi")
        sh(str(tmp), "init", "-q", "--bare", "-b", "main", self.origin)
        sh(str(tmp), "clone", "-q", self.origin, self.dev)
        self.write(**{"ticker.py": TICKER_OK, "requirements.txt": "requests>=2.31\n",
                      "launcher.py": open(os.path.join(ROOT, "launcher.py")).read(),
                      ".gitignore": "venv/\ncache/\n.launcher/\npip.log\nfreeze.txt\npip-fail\n"})
        self.commit("v1")
        sh(str(tmp), "clone", "-q", self.origin, self.pi)
        os.makedirs(os.path.join(self.pi, "venv", "bin"))
        py = os.path.join(self.pi, "venv", "bin", "python")
        with open(py, "w") as fh:
            fh.write(FAKE_PY)
        os.chmod(py, os.stat(py).st_mode | stat.S_IEXEC)
        with open(os.path.join(self.pi, "freeze.txt"), "w") as fh:
            fh.write("requests==2.31.0\nPillow==10.4.0\nrgbmatrix @ file:///home/pi/rpi-rgb-led-matrix\n")
        os.makedirs(os.path.join(self.pi, ".launcher"))
        shutil.copy(os.path.join(self.pi, "launcher.py"), os.path.join(self.pi, ".launcher", "launcher.py"))
        self.restarts = []
        self.updater = up.Updater(repo=self.pi, remote=self.origin, restart=lambda: self.restarts.append(1) or True)

    def write(self, **files):
        for name, text in files.items():
            with open(os.path.join(self.dev, name), "w") as fh:
                fh.write(text)

    def commit(self, msg, **files):
        self.write(**files)
        sh(self.dev, "add", "-A")
        sh(self.dev, "commit", "-q", "-m", msg)
        sh(self.dev, "push", "-q", "origin", "HEAD:main")
        return sh(self.dev, "rev-parse", "HEAD")

    def head(self):
        return sh(self.pi, "rev-parse", "HEAD")

    def state(self):
        return lx.load_state(self.pi)

    def update(self):
        self.updater._busy = True
        self.updater._run(self.updater.update)
        return self.state()

    def pip_log(self):
        try:
            return open(os.path.join(self.pi, "pip.log")).read()
        except OSError:
            return ""


@pytest.fixture(autouse=True)
def plain_git(monkeypatch):
    """Ignore the machine's git config (URL rewrites, credential helpers) like a fresh Pi."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in list(os.environ):
        if key.startswith("GIT_CONFIG_") and key not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
            monkeypatch.delenv(key)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_update_restart_and_confirm(world):
    v1 = world.head()
    v2 = world.commit("v2", **{"new.txt": "hi"})
    s = world.update()
    assert world.head() == v2 and s["state"] == "pending" and s["previous"] == v1
    assert world.restarts == [1]
    assert "pip.log" not in os.listdir(world.pi)  # requirements unchanged: no pip
    lx.guard(world.pi)                  # new version starts...
    assert lx.mark_healthy(world.pi)    # ...and runs for a minute
    assert world.state()["state"] == "ok"
    st = world.updater.status()
    assert st["can_rollback"] and st["version"]["subject"] == "v2"


def test_already_up_to_date(world):
    assert world.update()["message"] == "Already up to date."
    assert world.restarts == []


def test_manual_rollback(world):
    v1 = world.head()
    world.commit("v2", **{"new.txt": "hi"})
    world.update()
    lx.mark_healthy(world.pi)
    world.updater._busy = True
    world.updater._run(world.updater.rollback)
    assert world.head() == v1
    assert world.state()["state"] == "rolled_back" and not world.updater.status()["can_rollback"]
    assert len(world.restarts) == 2


def test_failed_self_check_restores_previous(world):
    v1 = world.head()
    world.commit("broken", **{"ticker.py": TICKER_BROKEN})
    s = world.update()
    assert world.head() == v1
    assert s["state"] == "failed" and "self-check" in s["message"] and "oops" in s["message"]
    assert world.restarts == []


def test_new_version_that_wont_start_is_rolled_back_by_launcher(world):
    v1 = world.head()
    world.commit("v2", **{"new.txt": "hi"})
    world.update()
    for _ in range(lx.MAX_ATTEMPTS):     # crashes before it can mark itself healthy
        assert lx.guard(world.pi)["state"] == "pending"
    s = lx.guard(world.pi)
    assert s["state"] == "rolled_back" and "didn't start" in s["message"]
    assert world.head() == v1


def test_dirty_tree_is_refused(world):
    world.commit("v2", **{"new.txt": "hi"})
    with open(os.path.join(world.pi, "ticker.py"), "a") as fh:
        fh.write("# local edit\n")
    s = world.update()
    assert s["state"] == "failed" and "edited by hand" in s["message"]


# --- dependencies -------------------------------------------------------------------
def test_requirements_change_installs_before_restart(world):
    v2 = world.commit("deps", **{"requirements.txt": "requests>=2.31\nnumpy>=1.26\n"})
    s = world.update()
    assert world.head() == v2 and s["state"] == "pending"
    log = world.pip_log()
    assert "pip freeze" in log and "pip install -r requirements.txt" in log
    assert s["deps_changed"] and "rgbmatrix @ file" not in "\n".join(s["freeze"])


def test_failed_package_install_rolls_back_code_and_packages(world):
    v1 = world.head()
    world.commit("deps", **{"requirements.txt": "pillow==99\n"})
    open(os.path.join(world.pi, "pip-fail"), "w").close()
    # pretend the failed install left a new package behind
    with open(os.path.join(world.pi, "freeze.txt"), "a") as fh:
        fh.write("numpy==2.0.0\n")
    world.updater.update_freeze_before = None
    real_freeze = lx.freeze
    calls = []

    def freeze(repo):  # first call (snapshot) excludes numpy; later calls see it
        out = real_freeze(repo)
        calls.append(1)
        return [ln for ln in out if not ln.startswith("numpy")] if len(calls) == 1 else out

    lx.freeze, saved = freeze, lx.freeze
    try:
        s = world.update()
    finally:
        lx.freeze = saved
    assert world.head() == v1
    assert s["state"] == "failed" and "Installing the new packages failed" in s["message"]
    assert "pillow==99" in s["message"]
    log = world.pip_log()
    assert "pip uninstall -y numpy" in log                     # removed what the update added
    assert "pip install --no-deps -r" in log                   # reinstalled the saved versions
    assert world.restarts == []


# --- launcher protection ------------------------------------------------------------
def test_good_new_launcher_is_installed(world):
    new = open(os.path.join(world.dev, "launcher.py")).read() + "\n# v2\n"
    world.commit("launcher v2", **{"launcher.py": new})
    s = world.update()
    assert s["state"] == "pending" and s["launcher_switched"]
    installed = open(os.path.join(world.pi, ".launcher", "launcher.py")).read()
    assert installed.endswith("# v2\n")
    assert os.path.exists(os.path.join(world.pi, ".launcher", "launcher.prev.py"))


def test_broken_new_launcher_is_not_installed(world):
    before = open(os.path.join(world.pi, ".launcher", "launcher.py")).read()
    world.commit("bad launcher", **{"launcher.py": "def main(:\n"})
    s = world.update()
    assert s["state"] == "pending"                 # the ticker update itself still goes ahead
    assert "old one is still in use" in s["message"] and not s["launcher_switched"]
    assert open(os.path.join(world.pi, ".launcher", "launcher.py")).read() == before


def test_rollback_restores_previous_launcher(world):
    new = open(os.path.join(world.dev, "launcher.py")).read() + "\n# v2\n"
    world.commit("launcher v2", **{"launcher.py": new})
    world.update()
    for _ in range(lx.MAX_ATTEMPTS + 1):
        lx.guard(world.pi)
    assert not open(os.path.join(world.pi, ".launcher", "launcher.py")).read().endswith("# v2\n")


def test_launcher_self_check_and_start(world, tmp_path):
    r = subprocess.run([sys.executable, os.path.join(world.pi, ".launcher", "launcher.py"), "--self-check"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout
    assert lx.repo_dir(os.path.join(world.pi, ".launcher", "launcher.py")) == world.pi


# --- private repo over SSH ----------------------------------------------------------
def test_auth_failure_says_deploy_key_needed(world, tmp_path):
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text("#!/bin/sh\necho 'git@github.com: Permission denied (publickey).' >&2\nexit 255\n")
    fake_ssh.chmod(0o755)
    world.updater.remote_setting = "git@github.com:owner/private-repo.git"
    world.updater._ssh_command = lambda: str(fake_ssh)
    s = world.update()
    assert s["state"] == "failed" and s["message"] == up.DEPLOY_KEY_MSG
    assert world.updater.status()["needs_key"]


def test_network_failure_message(world, tmp_path):
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text("#!/bin/sh\necho 'ssh: connect to host github.com port 22: Network is unreachable' >&2\nexit 255\n")
    fake_ssh.chmod(0o755)
    world.updater.remote_setting = "git@github.com:owner/repo.git"
    world.updater._ssh_command = lambda: str(fake_ssh)
    assert world.update()["message"] == up.OFFLINE_MSG


def test_missing_ssh_client_message():
    out = "ssh -i key: 1: ssh: not found\nfatal: Could not read from remote repository."
    assert up.classify_fetch_error(out) == up.NO_SSH_MSG


@pytest.mark.parametrize("out,msg", [
    ("ERROR: Repository not found.\nfatal: Could not read from remote repository.", up.DEPLOY_KEY_MSG),
    ("Warning: Identity file /home/pi/.ssh/crypto-ticker-deploy not accessible: No such file or directory.\n"
     "git@github.com: Permission denied (publickey).", up.DEPLOY_KEY_MSG),
    ("Host key verification failed.", up.DEPLOY_KEY_MSG),
    ("ssh: Could not resolve hostname github.com: Temporary failure in name resolution", up.OFFLINE_MSG),
    ("fatal: couldn't find remote ref main", "Couldn't fetch the update: fatal: couldn't find remote ref main"),
])
def test_classify_fetch_error(out, msg):
    assert up.classify_fetch_error(out) == msg


@pytest.mark.parametrize("url,want", [
    ("https://github.com/Owner/crypto-ticker.git", "git@github.com:Owner/crypto-ticker.git"),
    ("https://github.com/Owner/crypto-ticker", "git@github.com:Owner/crypto-ticker.git"),
    ("git@github.com:Owner/crypto-ticker.git", "git@github.com:Owner/crypto-ticker.git"),
    ("/srv/repo.git", "/srv/repo.git"),
])
def test_ssh_url(url, want):
    assert up.ssh_url(url) == want


def test_default_remote_uses_ssh(world):
    sh(world.pi, "remote", "set-url", "origin", "https://github.com/Owner/crypto-ticker.git")
    world.updater.remote_setting = ""
    assert world.updater.remote() == "git@github.com:Owner/crypto-ticker.git"
