#!/usr/bin/env python3
"""Service entry point and update safety net. Standard library only, on purpose.

systemd runs an installed copy of this file (.launcher/launcher.py), not the one git
updates. On every start it checks whether the last update is still unproven; if the
new version has failed to start MAX_ATTEMPTS times in a row, it restores the previous
commit (and the previous Python packages, if the update changed them) before starting
the ticker. The updater only replaces the installed copy after the new launcher passes
``--self-check``, so a broken launcher can't disable this.

    python launcher.py               start the ticker (what the service does)
    python launcher.py --self-check  verify this launcher can run; exit 0 if so

updater.py imports the git/pip helpers below, so there's one copy of the rollback logic.
"""

import json
import os
import pwd
import shutil
import subprocess
import sys
import time

MAX_ATTEMPTS = 3          # starts allowed before an unproven update is rolled back
STATE_NAME = os.path.join("cache", "update.json")
FREEZE_NAME = os.path.join("cache", "update-freeze.txt")


def repo_dir(start=None):
    """The crypto-ticker folder: this file's folder, or its parent for .launcher/."""
    here = os.path.dirname(os.path.abspath(start or __file__))
    return os.path.dirname(here) if os.path.basename(here) == ".launcher" else here


def log(msg):
    print(f"launcher: {msg}", file=sys.stderr, flush=True)


# --- state file -------------------------------------------------------------------
def load_state(repo):
    try:
        with open(os.path.join(repo, STATE_NAME)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(repo, state):
    path = os.path.join(repo, STATE_NAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, path)


# --- running git and pip as the folder's owner --------------------------------------
def owner_env(repo):
    """(uid, gid, env) of the folder's owner. The service runs as root, but git and pip
    must run as the owner so files stay theirs and git doesn't refuse the folder."""
    st = os.stat(repo)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        pw = pwd.getpwuid(st.st_uid)
        env.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)
    except KeyError:
        pass
    return st.st_uid, st.st_gid, env


def run(repo, cmd, timeout=300, env_extra=None):
    """Run cmd in repo as the owner. Returns (returncode, combined output)."""
    uid, gid, env = owner_env(repo)
    env.update(env_extra or {})
    kw = {}
    if os.geteuid() == 0 and uid != 0:
        kw = dict(user=uid, group=gid, extra_groups=[])
    try:
        p = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def git(repo, *args, timeout=120, env_extra=None):
    return run(repo, ["git", *args], timeout=timeout, env_extra=env_extra)


def venv_python(repo):
    py = os.path.join(repo, "venv", "bin", "python")
    return py if os.path.exists(py) else sys.executable


def freeze(repo):
    """Current packages, minus ones installed from a local folder (the LED library),
    which requirements.txt never touches and which take ages to rebuild."""
    code, out = run(repo, [venv_python(repo), "-m", "pip", "freeze"], timeout=120)
    if code:
        return None
    return [ln for ln in out.splitlines() if ln and not ln.startswith("#") and " @ file:" not in ln]


def restore_packages(repo, saved):
    """Put the venv back to the saved package list: remove packages the update added,
    reinstall the saved versions. Returns (ok, output)."""
    now = freeze(repo) or []
    name = lambda line: line.split("==")[0].split(" @ ")[0].strip().lower()  # noqa: E731
    keep = {name(ln) for ln in saved}
    extra = sorted({name(ln) for ln in now} - keep)
    out = ""
    if extra:
        _, out = run(repo, [venv_python(repo), "-m", "pip", "uninstall", "-y", *extra], timeout=600)
    path = os.path.join(repo, FREEZE_NAME)
    with open(path, "w") as fh:
        fh.write("\n".join(saved) + "\n")
    code, out2 = run(repo, [venv_python(repo), "-m", "pip", "install", "--no-deps", "-r", path], timeout=1800)
    return code == 0, (out + "\n" + out2).strip()


def rollback(repo, state, reason):
    """Restore state['previous'] (code, packages, launcher). Returns the new state."""
    prev = state.get("previous")
    msgs = []
    ok = True
    if prev:
        code, out = git(repo, "reset", "--hard", prev)
        if code:
            ok = False
            msgs.append(f"git reset failed: {out[-300:]}")
    if state.get("deps_changed") and state.get("freeze"):
        good, out = restore_packages(repo, state["freeze"])
        if not good:
            ok = False
            msgs.append(f"package restore failed: {out[-300:]}")
    backup = os.path.join(repo, ".launcher", "launcher.prev.py")
    if state.get("launcher_switched") and os.path.exists(backup):
        shutil.copy2(backup, os.path.join(repo, ".launcher", "launcher.py"))
    new = {"state": "rolled_back" if ok else "rollback_failed", "at": time.time(),
           "message": reason + ("" if ok else " Rollback had problems: " + "; ".join(msgs)),
           "rolled_back_from": state.get("target"), "current": prev}
    save_state(repo, new)
    return new


def mark_healthy(repo):
    """Called by the ticker once it has run for a while: the update is proven."""
    state = load_state(repo)
    if state.get("state") == "pending":
        state.update(state="ok", at=time.time(), attempts=0,
                     message=state.get("ok_message") or "Updated.")
        save_state(repo, state)
        return True
    return False


# --- entry points -----------------------------------------------------------------
def guard(repo):
    """Count starts of an unproven update; roll it back after MAX_ATTEMPTS failures."""
    state = load_state(repo)
    if state.get("state") != "pending":
        return state
    state["attempts"] = int(state.get("attempts") or 0) + 1
    if state["attempts"] > MAX_ATTEMPTS:
        log(f"update {state.get('target', '?')[:7]} failed to start {MAX_ATTEMPTS} times; rolling back")
        return rollback(repo, state, f"The update didn't start ({MAX_ATTEMPTS} tries), so the "
                                     "previous version was restored automatically.")
    save_state(repo, state)
    log(f"unproven update, start {state['attempts']} of {MAX_ATTEMPTS}")
    return state


def self_check(repo):
    """Exit status 0 if this launcher can do its job with this folder."""
    problems = []
    if not os.path.exists(os.path.join(repo, "ticker.py")):
        problems.append("ticker.py not found")
    if shutil.which("git") is None:
        problems.append("git not installed")
    load_state(repo)  # must not raise
    owner_env(repo)
    for name in ("guard", "rollback", "mark_healthy", "freeze", "restore_packages", "main"):
        if not callable(globals().get(name)):
            problems.append(f"{name} missing")
    if problems:
        log("self-check failed: " + ", ".join(problems))
        return 1
    print("launcher self-check ok")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    repo = repo_dir()
    if argv[:1] == ["--self-check"]:
        return self_check(repo)
    try:
        guard(repo)
    except Exception as exc:  # the safety net must never stop the ticker from starting
        log(f"update check failed: {exc}")
    ticker = os.path.join(repo, "ticker.py")
    py = venv_python(repo)
    os.chdir(repo)
    os.execv(py, [py, ticker, *argv])


if __name__ == "__main__":
    sys.exit(main())
