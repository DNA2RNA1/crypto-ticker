"""News screen: feed parsing, HTML stripping, last-good headlines, scrolling, timing."""

import os
import time

import pytest
import requests
from conftest import FIXTURES, FakeResponse

import news as nw
from render import Renderer
from scroller import SPEEDS, Lane, Scroller
from test_ticker import FakeDisplay
from ticker import Config, Ticker


def fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


RSS, ATOM, RDF = (fixture_bytes(n) for n in ("news_rss.xml", "news_atom.xml", "news_rdf.xml"))
URLS = ["https://feeds.example.com/rss", "https://wire.example.com/atom"]


class Resp(FakeResponse):
    def __init__(self, status=200, body=b""):
        super().__init__(status, text=body.decode("utf-8", "replace"))
        self.content = body


# --- parsing -----------------------------------------------------------------------
def test_rss():
    src, items = nw.parse_feed(RSS, URLS[0])
    assert src == "COINDESK"
    assert [i["title"] for i in items] == [
        "Bitcoin Climbs Past $109K as ETF Inflows Hit 3-Week High",
        'Ether & Solana Lead Altcoin Rebound - "Risk-On" Returns',     # double-escaped, typographic
        "Stablecoin Supply Reaches a New Record",                       # whitespace collapsed, bad date last
    ]
    assert items[0]["link"] == "https://news.example.com/markets/btc-109k" and items[2]["at"] is None


def test_atom():
    src, items = nw.parse_feed(ATOM, URLS[1])
    assert src == "MARKETS WIRE"
    assert items[0] == {"title": "Oil Slips on Supply Outlook", "link": "https://wire.example.com/a/2",
                        "at": pytest.approx(1793631600)}
    assert items[1]["title"] == "Stocks Edge Higher as Tech Rallies"   # HTML in the title stripped


def test_rss_1_0():
    src, items = nw.parse_feed(RDF, "https://old.example.com/index.rdf")
    assert src == "OLD STYLE FE" and items[0]["title"] == "RSS 1.0 Headline"


@pytest.mark.parametrize("body", [b"", b"<html><body>Not a feed</body></html>", b"{\"json\": true}",
                                  b"<rss><channel><title>x</title></channel></rss>", b"<rss><unclosed>"])
def test_malformed_or_empty(body):
    with pytest.raises(ValueError):
        nw.parse_feed(body, URLS[0])


def test_clean_text_drops_what_the_font_cannot_draw():
    drawable = lambda ch: ch.isascii()  # noqa: E731
    assert nw.clean_text("Café ‘bulls’ — \U0001F680 moon…", drawable) == "Cafe 'bulls' - moon..."
    assert nw.clean_text("<script>x</script>A&nbsp;B\x07") == "x A B"


@pytest.mark.parametrize("title,url,want", [
    ("BBC News - Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "BBC"),
    ("Cointelegraph.com News", "https://cointelegraph.com/rss", "COINTELEGRAPH"),
    ("", "https://www.example-news.com/feed", "EXAMPLE-NEWS"),
    ("A Really Very Long Publication Name Here", "https://pub.example.org/rss", "PUB"),
])
def test_source_name(title, url, want):
    assert nw.source_name(title, url) == want[:12]


# --- the feed: background fetch, last good, merging ---------------------------------
def make_feed(routes):
    feed = nw.News(URLS)

    def get(url, timeout=None):
        r = routes[url]
        if isinstance(r, BaseException):
            raise r
        return r

    feed.session.get = get
    return feed


def run(feed):
    feed._busy = True
    feed._run()
    return feed.data


def test_merge_alternates_sources_newest_first():
    data = run(make_feed({URLS[0]: Resp(body=RSS), URLS[1]: Resp(body=ATOM)}))
    assert [d["source"] for d in data[:4]] == ["COINDESK", "MARKETS WIRE", "COINDESK", "MARKETS WIRE"]
    assert len(data) == 5


def test_one_feed_down_keeps_the_other_and_its_own_last_good():
    feed = make_feed({URLS[0]: Resp(body=RSS), URLS[1]: Resp(body=ATOM)})
    run(feed)
    feed.session.get = lambda url, timeout=None: (Resp(body=RSS) if url == URLS[0]
                                                  else Resp(500, b"error"))
    data = run(feed)
    assert {d["source"] for d in data} == {"COINDESK", "MARKETS WIRE"}  # atom's last good kept


@pytest.mark.parametrize("failure", [
    requests.exceptions.ConnectionError("Failed to resolve host"),     # offline
    requests.exceptions.ReadTimeout("timed out"),
])
def test_offline_keeps_last_good(failure):
    feed = make_feed({URLS[0]: Resp(body=RSS), URLS[1]: Resp(body=ATOM)})
    good = run(feed)
    feed.session.get = lambda url, timeout=None: (_ for _ in ()).throw(failure)
    assert run(feed) == good


def test_offline_from_the_start_means_no_screen():
    feed = make_feed({u: requests.exceptions.ConnectionError("offline") for u in URLS})
    assert run(feed) is None


def test_huge_feed_is_ignored():
    feed = make_feed({URLS[0]: Resp(body=b"<rss>" + b" " * (nw.MAX_BYTES + 1)), URLS[1]: Resp(body=ATOM)})
    assert {d["source"] for d in run(feed)} == {"MARKETS WIRE"}


def test_next_batch_rotates():
    feed = make_feed({URLS[0]: Resp(body=RSS), URLS[1]: Resp(body=ATOM)})
    run(feed)
    a, b = feed.next_batch(3), feed.next_batch(3)
    assert len(a) == len(b) == 3 and a[0] != b[0]
    assert feed.next_batch(10) and len(feed.next_batch(10)) == 5  # never more than there are


# --- scrolling -------------------------------------------------------------------------
def scroller(title="Short", speed=30):
    return Renderer().news({"title": title, "source": "COINDESK", "at": time.time() - 7200}, speed=speed)


def test_short_headline_does_not_scroll():
    a = scroller("Oil Slips")
    assert not a.scrolls and a.duration == 3.0


def test_long_headline_scrolls_to_its_end():
    a = scroller("Bitcoin Climbs Past $109K as ETF Inflows Hit 3-Week High")
    assert a.scrolls and a.travel > 300
    lane = a.lanes[0]
    first, last = a.first_frame(), a.last_frame()
    x0, y0, x1, y1 = lane.box
    assert first.crop((x0, y0, x1 + 1, y1 + 1)).tobytes() == lane.strip.crop((0, 0, 64, lane.strip.height)).tobytes()
    end = lane.strip.crop((lane.strip.width - 64, 0, lane.strip.width, lane.strip.height))
    assert last.crop((0, lane.box[1], 64, lane.box[3] + 1)).tobytes() == end.tobytes()
    assert a.duration == pytest.approx(1.0 + a.travel / 30 + 1.2)


def test_scroll_moves_one_pixel_per_frame_right_to_left():
    from PIL import Image
    strip = Image.new("RGB", (200, 5))
    strip.putpixel((100, 2), (255, 255, 255))
    a = Scroller(Image.new("RGB", (64, 32)), [Lane(strip, (0, 10, 63, 14))], speed=30)
    xs = []
    t = a.scroll_start
    while t < a.scroll_end:
        f = a.frame_at(t)
        xs += [x for x in range(64) if f.getpixel((x, 12)) == (255, 255, 255)]
        t += a.frame_interval
    assert xs == list(range(63, 63 - len(xs), -1))  # one pixel left per frame, no jumps


def make_ticker(transition="scroll", screens=("news",)):
    cfg = Config()
    cfg.screens = list(screens)
    cfg.transition = transition
    t = Ticker(cfg, FakeDisplay())
    t.display.realtime = False
    return t


@pytest.mark.parametrize("transition,frames", [("scroll", 13), ("slide", 13), ("none", 0)])
def test_follows_transition_in_and_out(transition, frames):
    from PIL import Image
    t = make_ticker(transition)
    before = Image.new("RGB", (64, 32), (255, 0, 0))
    t.show(before, 1)
    a = scroller("Bitcoin Climbs Past $109K as ETF Inflows Hit 3-Week High")
    shown = []
    t.display.show = lambda img, hold=0: shown.append((img, hold))
    t.play(a)
    holds = [h for _, h in shown]
    assert len(shown) == frames + 1 + (a.travel - 1) + 1  # in, first frame, 1 px per frame, last
    assert holds[frames] == a.scroll_start                  # reading pause before it moves
    assert t.current.tobytes() == a.last_frame().tobytes()  # the next transition starts here
    after = Image.new("RGB", (64, 32), (0, 0, 255))
    shown.clear()
    t.show(after, 1)
    assert len(shown) == frames + 1


def test_realtime_play_keeps_frame_time_in_budget(monkeypatch):
    """Run the real-time loop at the fastest speed with the clock sped up and check the
    work per frame against the budget (one pixel per 1/45 s)."""
    t = make_ticker("none")
    t.display.realtime = True
    sleeps = []
    monkeypatch.setattr("ticker.time.sleep", lambda s: sleeps.append(s))
    a = scroller("Bitcoin Climbs Past $109K as ETF Inflows Hit 3-Week High", speed=SPEEDS["fast"])
    a.scroll_start = 0.0
    real = time.monotonic
    fake = [real()]

    def clock():
        fake[0] += 0.001  # each call advances 1 ms, so the loop finishes quickly
        return fake[0]

    monkeypatch.setattr("ticker.time.monotonic", clock)
    t.play(a)
    assert len(t.frame_times) > 20
    budget = a.frame_interval
    p95 = sorted(t.frame_times)[int(len(t.frame_times) * 0.95)]
    assert p95 < budget  # work per frame (measured with the sped-up clock) fits the budget
    monkeypatch.setattr("ticker.time.monotonic", real)


def test_frame_build_time_measured_well_under_budget():
    """Pure frame build time on this machine; leaves room for a Pi Zero that is 20-40x slower."""
    a = scroller("Bitcoin Climbs Past $109K as ETF Inflows Hit 3-Week High and Traders Eye Minutes",
                 speed=SPEEDS["fast"])
    times = []
    for px in range(int(a.travel)):
        t0 = time.perf_counter()
        a.frame_at(a.scroll_start + px / a.speed)
        times.append(time.perf_counter() - t0)
    p95 = sorted(times)[int(len(times) * 0.95)]
    assert p95 * 40 < a.frame_interval, f"p95 {p95 * 1e3:.3f} ms vs budget {a.frame_interval * 1e3:.1f} ms"


# --- in the cycle ------------------------------------------------------------------------
def test_screen_shows_n_headlines_per_cycle():
    t = make_ticker()
    t.news.data = [{"title": f"Headline {i}", "source": "X", "at": None} for i in range(7)]
    t.news.next_fetch = float("inf")
    t.cfg.news_per_cycle = 3
    assert len(t.screens()) == 3
    assert all(isinstance(s(), Scroller) for s in t.screens())


def test_screen_skipped_without_headlines():
    t = make_ticker()
    t.news.data, t.news.next_fetch = None, float("inf")
    assert t.screens() == []


def test_settings(monkeypatch):
    c = Config.from_env()
    assert c.news_feeds == nw.DEFAULT_FEEDS and c.news_per_cycle == 3 and c.news_speed == "normal"
    monkeypatch.setenv("NEWS_FEEDS", "https://a.example.com/rss, ftp://bad, https://b.example.com/feed")
    monkeypatch.setenv("NEWS_PER_CYCLE", "25")
    monkeypatch.setenv("NEWS_SPEED", "warp")
    c = Config.from_env()
    assert c.news_feeds == ["https://a.example.com/rss", "https://b.example.com/feed"]
    assert c.news_per_cycle == 10 and c.news_speed == "normal"


def test_reload_recreates_feed_when_urls_change(tmp_path):
    t = make_ticker()
    env = tmp_path / "settings.env"
    env.write_text("SCREENS=news\nNEWS_FEEDS=https://a.example.com/rss\n")
    t.env_path = str(env)
    old = t.news
    t.reload()
    assert t.news is not old and t.news.urls == ["https://a.example.com/rss"]


def test_news_groups_switch_independently(monkeypatch):
    from news import DEFAULT_FEEDS, DEFAULT_GENERAL_FEEDS
    from ticker import Config, news_urls
    for k in ("NEWS_FEEDS", "NEWS_GENERAL_FEEDS", "NEWS_CRYPTO", "NEWS_GENERAL"):
        monkeypatch.delenv(k, raising=False)  # earlier tests may have loaded a settings file
    c = Config.from_env()
    assert news_urls(c) == DEFAULT_FEEDS                     # crypto on, general off by default
    monkeypatch.setenv("NEWS_GENERAL", "on")
    c = Config.from_env()
    assert news_urls(c) == DEFAULT_FEEDS + DEFAULT_GENERAL_FEEDS
    monkeypatch.setenv("NEWS_CRYPTO", "off")
    assert news_urls(Config.from_env()) == DEFAULT_GENERAL_FEEDS
    monkeypatch.setenv("NEWS_GENERAL", "off")
    assert news_urls(Config.from_env()) == []


def test_page_saves_news_groups(tmp_path):
    from webui import WebApp
    env = tmp_path / "settings.env"
    env.write_text("WEB_PIN=1234\n")
    app = WebApp(str(env))
    saved = app.save({"news_crypto": "off", "news_general": "on",
                      "news_general_feeds": "https://feeds.npr.org/1001/rss.xml\nhttps://example.com/feed"})
    assert saved["NEWS_CRYPTO"] == "off" and saved["NEWS_GENERAL"] == "on"
    assert saved["NEWS_GENERAL_FEEDS"] == "https://feeds.npr.org/1001/rss.xml,https://example.com/feed"
    s = app.settings()
    assert s["news_crypto"] == "off" and s["news_general"] == "on"
    assert app.save({"news_general_feeds": ""})["NEWS_GENERAL_FEEDS"] == ""


def test_politics_group_and_source_tags(monkeypatch):
    from news import DEFAULT_POLITICS_FEEDS, parse_feed, source_name
    from ticker import Config, news_urls
    for k in ("NEWS_FEEDS", "NEWS_GENERAL_FEEDS", "NEWS_POLITICS_FEEDS", "NEWS_CRYPTO",
              "NEWS_GENERAL", "NEWS_POLITICS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEWS_CRYPTO", "off")
    monkeypatch.setenv("NEWS_POLITICS", "on")
    assert news_urls(Config.from_env()) == DEFAULT_POLITICS_FEEDS
    tags = [source_name("whatever", u) for u in DEFAULT_POLITICS_FEEDS]
    assert tags == ["AP", "REUTERS", "NPR", "POLITICO", "MS NOW", "FOX NEWS"]
    gn = b"""<rss><channel><title>"when:24h site:reuters.com" - Google News</title>
      <item><title>Senate passes funding bill - Reuters</title><link>https://news.google.com/x</link>
      <pubDate>Mon, 02 Nov 2026 20:00:00 GMT</pubDate></item></channel></rss>"""
    src, items = parse_feed(gn, DEFAULT_POLITICS_FEEDS[1])
    assert src == "REUTERS" and items[0]["title"] == "Senate passes funding bill"


def test_page_saves_politics_group(tmp_path):
    from webui import WebApp
    env = tmp_path / "settings.env"
    env.write_text("WEB_PIN=1234\n")
    app = WebApp(str(env))
    from news import DEFAULT_POLITICS_FEEDS
    saved = app.save({"news_politics": "on", "news_politics_feeds": "\n".join(DEFAULT_POLITICS_FEEDS)})
    assert saved["NEWS_POLITICS"] == "on"
    assert saved["NEWS_POLITICS_FEEDS"].split(",") == DEFAULT_POLITICS_FEEDS
    assert app.settings()["news_politics_feeds"] == DEFAULT_POLITICS_FEEDS


def test_no_feeds_switched_on_is_not_an_error():
    from news import News
    assert News([]).fetch() == []
