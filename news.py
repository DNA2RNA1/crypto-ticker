"""Headlines from RSS / Atom feeds for the news screen.

Each feed is fetched every 15 minutes in the background. Every feed keeps its own last
good headlines, so one broken feed (or no Wi-Fi) never empties the screen. Titles are
stripped of HTML and odd characters the LED fonts can't draw.
"""

import email.utils
import hashlib
import html
import logging
import re
import threading
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

from feeds import Feed

log = logging.getLogger("ticker.news")

# Crypto headlines (NEWS_FEEDS)
DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
]
# General news: top stories, politics, business, world (NEWS_GENERAL_FEEDS; off by default).
# AP and CNN don't offer working public RSS feeds, so these are NPR and the BBC.
DEFAULT_GENERAL_FEEDS = [
    "https://feeds.npr.org/1001/rss.xml",      # NPR top stories
    "https://feeds.npr.org/1014/rss.xml",      # NPR politics
    "https://feeds.npr.org/1006/rss.xml",      # NPR business
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]
# Politics (NEWS_POLITICS_FEEDS; off by default), from across the spectrum. AP and Reuters
# no longer publish RSS: AP's politics picks come via its Flipboard magazine, and Reuters'
# US stories via a Google News search limited to reuters.com.
DEFAULT_POLITICS_FEEDS = [
    "https://flipboard.com/@associatedpress/politics-h8vn14lnz.rss",
    "https://news.google.com/rss/search?q=when:24h+site:reuters.com/world/us&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.npr.org/1014/rss.xml",
    "https://rss.politico.com/politics-news.xml",
    "https://www.ms.now/category/politics/feed",
    "https://moxie.foxnews.com/google-publisher/politics.xml",
]
# Tag names for feeds whose own titles don't say who they are
KNOWN_SOURCES = (
    ("flipboard.com/@associatedpress", "AP"),
    ("site:reuters.com", "REUTERS"),
    ("site:apnews.com", "AP"),
    ("ms.now/", "MS NOW"),
    ("foxnews.com/", "FOX NEWS"),
    ("politico.com/", "POLITICO"),
    ("npr.org/", "NPR"),
)
MAX_BYTES = 2_000_000       # a feed bigger than this is ignored (the Pi Zero has 512 MB)
MAX_PER_FEED = 15           # newest headlines kept per feed
MAX_TITLE = 160

# typographic characters -> what the BDF fonts can draw
ASCII_MAP = str.maketrans({"‘": "'", "’": "'", "‚": ",", "‛": "'", "“": '"',
                           "”": '"', "„": '"', "–": "-", "—": "-", "―": "-",
                           "…": "...", " ": " ", "′": "'", "″": '"', "×": "x",
                           "•": "-", "·": "-"})


def clean_text(text, drawable=None):
    """Plain one-line text: no HTML tags or entities, no control characters, typographic
    quotes/dashes made ASCII; characters ``drawable`` rejects are dropped."""
    text = html.unescape(html.unescape(str(text or "")))       # some feeds double-escape
    text = re.sub(r"<[^>]*>", " ", text)                        # strip tags
    text = text.translate(ASCII_MAP)
    text = "".join(ch for ch in unicodedata.normalize("NFKC", text)
                   if unicodedata.category(ch)[0] != "C" or ch == " ")
    if drawable is not None:
        text = "".join(ch if drawable(ch) else _fallback(ch, drawable) for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def _fallback(ch, drawable):
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    return base if base and all(drawable(c) for c in base) else ""


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child_text(el, *names):
    for child in el:
        if _local(child.tag) in names and (child.text or "").strip():
            return child.text
    return None


def _date(text):
    if not text:
        return None
    text = text.strip()
    try:
        d = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def source_name(title, url):
    """Short label for the colored tag: "CoinDesk: Bitcoin, Ethereum, ..." -> "COINDESK"."""
    low = (url or "").lower()
    for marker, known in KNOWN_SOURCES:
        if marker in low:
            return known
    m = re.search(r"site:(?:www\.)?([a-z0-9-]+)\.", low)  # other Google News site searches
    if m and "news.google." in low:
        return m.group(1)[:12].upper()
    name = clean_text(title or "")
    name = re.split(r"\s*(?:[:|]|\s-\s|\s–\s)\s*", name)[0] if name else ""
    if name.upper().startswith("BBC NEWS"):
        name = "BBC"
    if not name or len(name) > 14:
        host = urlparse(url).hostname or ""
        host = re.sub(r"^(www|feeds?|rss|news)\.", "", host)
        name = host.split(".")[0] if host else (name or "NEWS")
    return name[:12].upper()


def parse_feed(content, url):
    """RSS 2.0 / RSS 1.0 / Atom bytes -> (source, [ {title, link, at}, ... ] newest first).
    Raises ValueError if it isn't a feed with headlines."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"not XML: {exc}") from None
    channel = next((el for el in root.iter() if _local(el.tag) == "channel"), root)
    feed_title = _child_text(channel, "title") or _child_text(root, "title")
    google = "news.google." in (url or "").lower()
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = clean_text(_child_text(el, "title") or "")
        if google:  # "Headline - Reuters": the source is already on the tag
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
        if not title:
            continue
        link = None
        for child in el:
            if _local(child.tag) == "link":
                href = child.get("href")
                if href and child.get("rel", "alternate") == "alternate":
                    link = href
                    break
                if (child.text or "").strip():
                    link = child.text.strip()
                    break
        at = _date(_child_text(el, "pubdate", "published", "updated", "date"))
        items.append({"title": title[:MAX_TITLE], "link": link, "at": at})
    if not items:
        raise ValueError("no headlines in the feed")
    items.sort(key=lambda i: i["at"] or 0, reverse=True)
    return source_name(feed_title, url), items[:MAX_PER_FEED]


def tag_color(source):
    """A steady, bright color per source for its tag."""
    palette = [(0, 150, 255), (255, 140, 0), (200, 60, 255), (0, 190, 120), (230, 40, 90),
               (255, 200, 0), (0, 200, 210)]
    return palette[int(hashlib.md5(source.encode()).hexdigest()[:4], 16) % len(palette)]


class News(Feed):
    """Every 15 minutes: fetch each feed; keep its last good headlines."""
    refresh_every = 900

    def __init__(self, urls=None):
        super().__init__()
        self.session.headers["User-Agent"] = "crypto-ticker/2.0 (+RSS reader)"
        self.urls = list(urls if urls is not None else DEFAULT_FEEDS)
        self.by_url = {}          # url -> (source, items), last good per feed
        self._cursor = 0
        self._lock = threading.Lock()

    def fetch(self):
        if not self.urls:  # every news group switched off: nothing to fetch, nothing to report
            return []
        errors = []
        for url in self.urls:
            try:
                r = self.session.get(url, timeout=15)
                r.raise_for_status()
                content = r.content
                if len(content) > MAX_BYTES:
                    raise ValueError(f"feed too large ({len(content)} bytes)")
                self.by_url[url] = parse_feed(content, url)
            except Exception as exc:  # one bad feed must not affect the others
                errors.append(f"{urlparse(url).hostname}: {exc}")
                log.warning("news feed %s failed: %s", url, exc)
        with self._lock:
            self.by_url = {u: v for u, v in self.by_url.items() if u in self.urls}
            merged = self._merge()
        if not merged:
            raise RuntimeError("no headlines: " + "; ".join(errors[:3]))
        return merged

    def _merge(self):
        """Newest first, alternating between sources so one busy feed doesn't take over."""
        queues = [[dict(i, source=src) for i in items] for src, items in self.by_url.values()]
        out, seen = [], set()
        while any(queues):
            for q in queues:
                if q:
                    item = q.pop(0)
                    key = item["title"].lower()
                    if key not in seen:
                        seen.add(key)
                        out.append(item)
        return out

    def next_batch(self, n):
        """The next ``n`` headlines, continuing where the last cycle stopped."""
        items = self.data or []
        if not items:
            return []
        with self._lock:
            start = self._cursor % len(items)
            batch = [items[(start + i) % len(items)] for i in range(min(n, len(items)))]
            self._cursor = start + len(batch)
        return batch
