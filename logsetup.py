"""Logging for the ticker: the console (journald when run by systemd) plus a size-capped,
rotating log file so a small SD card never fills up.

Settings (settings.env or environment):
  LOG_LEVEL     DEBUG, INFO (default), WARNING, ERROR
  LOG_FILE      path of the log file (default logs/ticker.log next to ticker.py; blank = no file)
  LOG_MAX_KB    size of one log file before it rotates (default 512)
  LOG_BACKUPS   how many rotated files to keep (default 3), so the total is at most
                LOG_MAX_KB * (LOG_BACKUPS + 1)  ->  2 MB with the defaults
"""

import logging
import logging.handlers
import os

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(HERE, "logs", "ticker.log")


def _int(raw, default, lo, hi):
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


def setup_logging(level=None, log_file=None, max_kb=None, backups=None, console=True):
    """Configure the root logger. Arguments default to the LOG_* environment variables."""
    env = os.environ
    level_name = (level or env.get("LOG_LEVEL") or "INFO").upper()
    lvl = getattr(logging, level_name, None)
    if not isinstance(lvl, int):
        lvl = logging.INFO
    if log_file is None:
        log_file = env.get("LOG_FILE", DEFAULT_FILE)
    if log_file and not os.path.isabs(log_file):
        log_file = os.path.join(HERE, log_file)
    max_kb = _int(max_kb if max_kb is not None else env.get("LOG_MAX_KB"), 512, 16, 102400)
    backups = _int(backups if backups is not None else env.get("LOG_BACKUPS"), 3, 0, 20)

    root = logging.getLogger()
    for h in list(root.handlers):  # make this safe to call twice
        root.removeHandler(h)
        h.close()
    root.setLevel(lvl)
    fmt = logging.Formatter(FORMAT)

    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    if log_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_kb * 1024, backupCount=backups, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:  # read-only or full card: keep running, console only
            logging.getLogger("ticker").warning("log file %s unavailable: %s", log_file, exc)

    # requests/urllib3 are chatty at DEBUG; keep them at WARNING unless asked
    if lvl > logging.DEBUG:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root
