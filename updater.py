"""Update the ticker from GitHub, and roll back, from the phone settings page.

Update:
  1. refuse if tracked files were edited by hand (settings.env and icons are untracked)
  2. fetch the branch over SSH with the deploy key (works for a private repo)
  3. fast-forward only; remember the previous commit
  4. if requirements.txt changed: snapshot the packages, install the new ones;
     on failure restore code and packages
  5. self-check the new code (``ticker.py --check``); on failure restore everything
  6. if launcher.py changed: switch the installed launcher only if the new one passes
     ``launcher.py --self-check``; otherwise keep the old one
  7. restart the service. The launcher rolls back automatically if the new version
     fails to start 3 times; the ticker marks the update good after 60 s of running.

Roll back restores the previous commit (and packages/launcher if the update changed them).
"""

import logging
import os
import re
import shutil
import threading
import time

import launcher as lx

log = logging.getLogger("ticker.update")

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = "crypto-ticker"
DEFAULT_KEY = "~/.ssh/crypto-ticker-deploy"
DEPLOY_KEY_MSG = "Pi can't reach GitHub — deploy key needed"
OFFLINE_MSG = "Pi can't reach GitHub — check its internet connection"
NO_SSH_MSG = "The SSH client is missing — run: sudo apt-get install -y openssh-client"

NETWORK_ERRORS = ("could not resolve hostname", "network is unreachable", "connection timed out",
                  "operation timed out", "no route to host", "temporary failure in name resolution",
                  "connection refused", "timed out after")
AUTH_ERRORS = ("permission denied (publickey", "could not read from remote repository",
               "repository not found", "authentication failed", "host key verification failed",
               "terminal prompts disabled", "could not read username", "deploy key", "no such identity",
               "identity file", "invalid format", "load key")


def classify_fetch_error(output):
    low = output.lower()
    if "ssh: not found" in low or "ssh: command not found" in low or "cannot run ssh" in low:
        return NO_SSH_MSG
    if any(s in low for s in NETWORK_ERRORS):
        return OFFLINE_MSG
    if any(s in low for s in AUTH_ERRORS):
        return DEPLOY_KEY_MSG
    return "Couldn't fetch the update: " + (output.strip().splitlines() or ["unknown error"])[-1][:200]


def ssh_url(url):
    """https://github.com/owner/repo(.git) -> git@github.com:owner/repo.git; others unchanged."""
    m = re.fullmatch(r"https?://(?:[^@/]+@)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?", url or "")
    return f"git@github.com:{m[1]}/{m[2]}.git" if m else url


class UpdateError(Exception):
    pass


class Updater:
    def __init__(self, repo=HERE, remote=None, key=None, restart=None, check_timeout=180):
        self.repo = repo
        self.remote_setting = remote or os.environ.get("UPDATE_REMOTE", "")
        self.key_setting = key or os.environ.get("UPDATE_SSH_KEY", "") or DEFAULT_KEY
        self.restart = restart if restart is not None else self._systemd_restart
        self.check_timeout = check_timeout
        self._lock = threading.Lock()
        self._busy = False

    # --- helpers --------------------------------------------------------------------
    def _git(self, *args, timeout=120, ssh=False):
        extra = {"GIT_SSH_COMMAND": self._ssh_command()} if ssh else None
        return lx.git(self.repo, *args, timeout=timeout, env_extra=extra)

    def key_path(self):
        _, _, env = lx.owner_env(self.repo)
        p = self.key_setting
        return os.path.join(env.get("HOME", "~"), p[2:]) if p.startswith("~/") else p

    def _ssh_command(self):
        return (f"ssh -i '{self.key_path()}' -o IdentitiesOnly=yes -o BatchMode=yes "
                "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15")

    def remote(self):
        if self.remote_setting:
            return self.remote_setting
        code, out = self._git("remote", "get-url", "origin")
        if code:
            raise UpdateError("This folder has no 'origin' remote to update from.")
        return ssh_url(out.strip())

    def public_key(self):
        try:
            with open(self.key_path() + ".pub") as fh:
                return fh.read().strip()
        except OSError:
            return None

    def _systemd_restart(self):
        if os.environ.get("INVOCATION_ID") and shutil.which("systemctl"):
            os.spawnlp(os.P_NOWAIT, "systemctl", "systemctl", "--no-block", "restart", f"{SERVICE}.service")
            return True
        return False  # not running as the service (e.g. the emulator): restart by hand

    def _set(self, **fields):
        state = lx.load_state(self.repo)
        state.update(fields, at=time.time())
        lx.save_state(self.repo, state)
        return state

    def version(self):
        code, out = self._git("log", "-1", "--format=%h%x09%cs%x09%s")
        if code:
            return {"commit": "unknown", "date": "", "subject": ""}
        h, d, s = (out.split("\t") + ["", ""])[:3]
        return {"commit": h, "date": d, "subject": s[:80]}

    # --- status for the phone page ----------------------------------------------------
    def status(self):
        state = lx.load_state(self.repo)
        st = state.get("state") or "idle"
        return {
            "version": self.version(),
            "state": "running" if self._busy else st,
            "message": state.get("message", ""),
            "at": state.get("at"),
            "can_rollback": bool(state.get("previous")) and st in ("ok", "pending", "restart_needed")
                            and not self._busy,
            "previous": (state.get("previous") or "")[:7],
            "deploy_key": self.public_key(),
            "needs_key": state.get("message") == DEPLOY_KEY_MSG,
        }

    # --- update -----------------------------------------------------------------------
    def start_update(self):
        return self._start(self.update)

    def start_rollback(self):
        return self._start(self.rollback)

    def _start(self, fn):
        with self._lock:
            if self._busy:
                raise UpdateError("An update is already running.")
            self._busy = True
        threading.Thread(target=self._run, args=(fn,), daemon=True, name="updater").start()

    def _run(self, fn):
        try:
            fn()
        except UpdateError as exc:
            log.warning("update: %s", exc)
            self._set(state="failed", message=str(exc))
        except Exception as exc:
            log.exception("update crashed")
            self._set(state="failed", message=f"Update failed: {exc}")
        finally:
            self._busy = False

    def update(self):
        repo = self.repo
        self._set(state="running", message="Checking for updates…")
        code, dirty = self._git("status", "--porcelain", "--untracked-files=no")
        if code:
            raise UpdateError("This folder isn't a git checkout; update by hand.")
        if dirty.strip():
            raise UpdateError("Files in the ticker folder were edited by hand; update over SSH instead.")
        code, branch = self._git("symbolic-ref", "--short", "HEAD")
        if code:
            raise UpdateError("Not on a branch; update over SSH instead.")
        branch = branch.strip()
        code, out = self._git("fetch", "--no-tags", self.remote(), branch, timeout=180, ssh=True)
        if code:
            raise UpdateError(classify_fetch_error(out))
        prev = self._git("rev-parse", "HEAD")[1].strip()
        new = self._git("rev-parse", "FETCH_HEAD")[1].strip()
        if prev == new:
            self._set(state="idle", message="Already up to date.")
            return
        if self._git("merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD")[0]:
            raise UpdateError("The update can't be applied automatically (the code here has local "
                              "commits); update over SSH instead.")
        deps_changed = self._git("diff", "--quiet", prev, new, "--", "requirements.txt")[0] != 0
        saved = lx.freeze(repo) if deps_changed else None
        if deps_changed and saved is None:
            raise UpdateError("Couldn't read the installed packages; update over SSH instead.")
        state = self._set(state="running", message="Applying the update…", previous=prev, target=new,
                          deps_changed=deps_changed, freeze=saved, launcher_switched=False, attempts=0)

        code, out = self._git("merge", "--ff-only", "FETCH_HEAD")
        if code:
            raise UpdateError(f"Couldn't apply the update: {out[-200:]}")

        def fail(reason):
            lx.rollback(repo, state, reason)
            raise_state = lx.load_state(repo)
            log.error("update to %s rolled back: %s", new[:7], reason)
            raise UpdateError(raise_state.get("message", reason))

        if deps_changed:
            self._set(message="Installing new Python packages (this can take a while on a Pi Zero)…")
            code, out = lx.run(repo, [lx.venv_python(repo), "-m", "pip", "install", "-r", "requirements.txt"],
                               timeout=1800)
            if code:
                fail("Installing the new packages failed, so the previous version was restored. "
                     f"pip said: {out.strip().splitlines()[-1][:160] if out.strip() else 'no output'}")

        self._set(message="Checking the new version…")
        code, out = lx.run(repo, [lx.venv_python(repo), "ticker.py", "--check"], timeout=self.check_timeout)
        if code:
            fail("The new version failed its self-check, so the previous version was restored. "
                 f"({out.strip().splitlines()[-1][:160] if out.strip() else 'no output'})")

        note = self._update_launcher()
        ok_message = f"Updated to {new[:7]}." + (f" {note}" if note else "")
        if self.restart():
            self._set(state="pending", attempts=0, ok_message=ok_message,
                      message=f"Restarting with {new[:7]}…" + (f" {note}" if note else ""))
        else:
            self._set(state="restart_needed", ok_message=ok_message,
                      message=ok_message + " Restart the ticker to use it.")
        log.info("updated %s -> %s", prev[:7], new[:7])

    def _update_launcher(self):
        """Install the repo's launcher.py as the service's launcher only if it passes its
        self-check. Returns a note for the user, or ''."""
        installed_dir = os.path.join(self.repo, ".launcher")
        installed = os.path.join(installed_dir, "launcher.py")
        source = os.path.join(self.repo, "launcher.py")
        try:
            with open(source, "rb") as a, open(installed, "rb") as b:
                if a.read() == b.read():
                    return ""
        except FileNotFoundError:
            if not os.path.isdir(installed_dir):
                return ""  # not installed as a service (dev / emulator)
        code, out = lx.run(self.repo, [lx.venv_python(self.repo), source, "--self-check"], timeout=60)
        if code:
            log.error("new launcher failed its self-check; keeping the old one: %s", out[-300:])
            return "The new launcher failed its self-check, so the old one is still in use."
        if os.path.exists(installed):
            shutil.copy2(installed, os.path.join(installed_dir, "launcher.prev.py"))
        tmp = installed + ".tmp"
        shutil.copy2(source, tmp)
        os.replace(tmp, installed)
        self._set(launcher_switched=True)
        return ""

    # --- rollback ---------------------------------------------------------------------
    def rollback(self):
        state = lx.load_state(self.repo)
        if not state.get("previous"):
            raise UpdateError("There's no previous version to go back to.")
        self._set(state="running", message="Restoring the previous version…")
        state = lx.rollback(self.repo, state, f"Rolled back to {state['previous'][:7]}.")
        if state["state"] != "rolled_back":
            raise UpdateError(state["message"])
        if not self.restart():
            self._set(message=state["message"] + " Restart the ticker to use it.")
        log.info("rolled back to %s", state.get("current", "")[:7])
