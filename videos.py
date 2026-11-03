"""New uploads from YouTube channels, for the Videos screen.

Every YouTube channel has a free Atom feed:
https://www.youtube.com/feeds/videos.xml?channel_id=UC...

People paste channel links, so each entry is boiled down to either a channel id (UC...) or an
@handle. A handle is looked up once on the channel's page and cached in
cache/youtube_channels.json. Feeds are fetched every 30 minutes in the background; each
channel keeps its last good uploads, so one broken channel (or no Wi-Fi) never empties the
screen. Shorts are skipped when the feed marks them (their link is /shorts/<id>).
"""

import json
import logging
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

from feeds import BROWSER_UA
from news import MAX_BYTES, News, clean_text, parse_feed

log = logging.getLogger("ticker.videos")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "cache", "youtube_channels.json")
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
CHANNEL_PAGE = "https://www.youtube.com/@{}"
TAG_COLOR = (230, 30, 30)        # YouTube red, for the source tag on the panel
MAX_CHANNELS = 10
YT_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com")
CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
HANDLE_RE = re.compile(r"[A-Za-z0-9._-]{3,30}")
# Where a channel page states its own id, most reliable first
PAGE_ID_PATTERNS = (
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{22})'),
    re.compile(r'<meta itemprop="identifier" content="(UC[A-Za-z0-9_-]{22})"'),
)


def parse_entry(text):
    """A pasted channel link, feed URL, @handle or channel id -> its canonical form:
    ``UC...`` (a channel id) or ``@handle``. Raises ValueError with a short message."""
    s = str(text or "").strip()
    if not s:
        raise ValueError("empty")
    if CHANNEL_ID_RE.fullmatch(s):
        return s
    if s.startswith("@"):
        if HANDLE_RE.fullmatch(s[1:]):
            return "@" + s[1:].lower()
        raise ValueError(f"not a YouTube handle: {s[:40]}")
    if not re.match(r"https?://", s, re.I):
        s = "https://" + s
    u = urlparse(s)
    if (u.hostname or "").lower() not in YT_HOSTS:
        raise ValueError(f"not a YouTube link: {text[:40]}")
    parts = [p for p in u.path.split("/") if p]
    if parts[:2] == ["feeds", "videos.xml"]:
        cid = (parse_qs(u.query).get("channel_id") or [""])[0]
        if CHANNEL_ID_RE.fullmatch(cid):
            return cid
    elif len(parts) >= 2 and parts[0] == "channel" and CHANNEL_ID_RE.fullmatch(parts[1]):
        return parts[1]
    elif parts and parts[0].startswith("@") and HANDLE_RE.fullmatch(parts[0][1:]):
        return "@" + parts[0][1:].lower()
    raise ValueError(f"use a channel link (youtube.com/@name): {text[:40]}")


def parse_entries(text):
    """Several entries (one per line, or comma separated) -> canonical list, no repeats."""
    out = []
    for raw in re.split(r"[\s,]+", str(text or "").strip()):
        if raw:
            entry = parse_entry(raw)
            if entry not in out:
                out.append(entry)
    if len(out) > MAX_CHANNELS:
        raise ValueError(f"{MAX_CHANNELS} channels max")
    return out


def find_channel_id(page):
    """A channel page's HTML -> its UC... id, or None."""
    for pattern in PAGE_ID_PATTERNS:
        m = pattern.search(page or "")
        if m:
            return m.group(1)
    return None


def is_short(item):
    return "/shorts/" in (item.get("link") or "")


def parse_videos(content, url, now_ts, max_age_days=3, skip_shorts=True):
    """YouTube Atom feed bytes -> (channel name for the tag, [videos newest first]).
    Old uploads and (optionally) Shorts are left out; an empty list is not an error."""
    try:
        _, items = parse_feed(content, url)
    except ValueError as exc:
        if "no headlines" not in str(exc):
            raise
        items = []                # a channel with no uploads yet
    return channel_name(content), keep_recent(items, now_ts, max_age_days, skip_shorts)


def keep_recent(items, now_ts, max_age_days, skip_shorts):
    cutoff = now_ts - max_age_days * 86400
    return [i for i in items
            if not (skip_shorts and is_short(i)) and (i.get("at") is None or i["at"] >= cutoff)]


def channel_name(content):
    """The feed's own <title> is the channel name: shortened to fit the panel's tag."""
    m = re.search(rb"<title>(.*?)</title>", content or b"", re.S)
    name = clean_text(m.group(1).decode("utf-8", "replace")) if m else ""
    return (name[:12].strip() or "YOUTUBE").upper()


class Videos(News):
    """Every 30 minutes: resolve each channel, fetch its feed, keep its last good uploads."""
    refresh_every = 1800

    def __init__(self, entries=None, max_age_days=3, skip_shorts=True, cache_path=CACHE_PATH):
        super().__init__(urls=[])
        self.entries = list(entries or [])
        self.max_age_days = max_age_days
        self.skip_shorts = skip_shorts
        self.cache_path = cache_path
        self.status = {}          # entry -> {"ok": bool, "name": str} or {"ok": False, "error": str}
        self.by_entry = {}        # entry -> (name, all recent videos), last good per channel
        self._ids = self._load_cache()
        self._status_lock = threading.Lock()

    def update(self, now=None):
        if not self.entries:      # nothing to follow: no fetches, no warnings in the log
            return None
        return super().update(now)

    # --- handle -> channel id ------------------------------------------------------------
    def _load_cache(self):
        try:
            with open(self.cache_path) as fh:
                data = json.load(fh)
            return {k: v for k, v in data.items() if isinstance(v, str) and CHANNEL_ID_RE.fullmatch(v)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self._ids, fh)
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            log.warning("could not save the YouTube channel cache: %s", exc)

    def channel_id(self, entry):
        if CHANNEL_ID_RE.fullmatch(entry):
            return entry
        if entry in self._ids:
            return self._ids[entry]
        r = self.session.get(CHANNEL_PAGE.format(entry[1:]), timeout=15,
                             headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"})
        if r.status_code == 404:
            raise LookupError("no channel with that handle")
        r.raise_for_status()
        cid = find_channel_id(r.text)
        if not cid:
            raise LookupError("couldn't find the channel id on its page")
        self._ids[entry] = cid
        self._save_cache()
        return cid

    # --- fetch -------------------------------------------------------------------------------
    def fetch(self):
        now = time.time()
        errors = []
        for entry in self.entries:
            try:
                url = FEED_URL.format(self.channel_id(entry))
                r = self.session.get(url, timeout=15)
                if r.status_code == 404:
                    raise LookupError("YouTube has no feed for this channel")
                r.raise_for_status()
                if len(r.content) > MAX_BYTES:
                    raise ValueError("feed too large")
                name, items = parse_videos(r.content, url, now, self.max_age_days, self.skip_shorts)
                self.by_entry[entry] = (name, items)
                self._set_status(entry, {"ok": True, "name": name})
            except Exception as exc:  # one bad channel must not affect the others
                msg = friendly_error(exc)
                errors.append(f"{entry}: {msg}")
                log.warning("videos: %s failed: %s", entry, exc)
                self._set_status(entry, {"ok": False, "error": msg})
        with self._lock:
            self.by_entry = {e: v for e, v in self.by_entry.items() if e in self.entries}
            # last good lists age too: an old upload drops off even while a feed is down
            self.by_url = {e: (name, keep_recent(items, now, self.max_age_days, self.skip_shorts))
                           for e, (name, items) in self.by_entry.items()}
            merged = self._merge()
        if not merged and errors:
            log.warning("videos: nothing recent to show (%s)", "; ".join(errors[:3]))
        # an empty list (not an error) replaces old data, so uploads that aged out while
        # YouTube was unreachable stop showing; an empty list skips the screen
        return merged

    def _set_status(self, entry, value):
        with self._status_lock:
            self.status[entry] = value

    def status_for(self, entries):
        """Status per entry for the settings page; None = not checked yet."""
        with self._status_lock:
            return {e: self.status.get(e) for e in entries}


def friendly_error(exc):
    import requests
    if isinstance(exc, LookupError):
        return str(exc)
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return "couldn't reach YouTube"
    if isinstance(exc, requests.exceptions.HTTPError):
        code = getattr(exc.response, "status_code", None)
        return f"YouTube answered with an error ({code})" if code else "YouTube answered with an error"
    if isinstance(exc, ValueError):
        return "the feed couldn't be read"
    return "failed"
