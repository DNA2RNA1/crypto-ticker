"""Videos screen: channel links, handle lookup + cache, feed parsing, Shorts, last-good
uploads, the screen in the cycle, settings and the phone page."""

import json
import os
from datetime import datetime, timezone

import pytest
import requests
from conftest import FIXTURES, FakeResponse

import videos as yt
from scroller import Scroller
from test_ticker import FakeDisplay
from ticker import Config, Ticker

with open(os.path.join(FIXTURES, "youtube_feed.xml"), "rb") as _fh:
    FEED = _fh.read()
NOW = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc).timestamp()
CID = "UCaaaaaaaaaaaaaaaaaaaaaa"
CID2 = "UCbbbbbbbbbbbbbbbbbbbbbb"
PAGE = f'<html><head><link rel="canonical" href="https://www.youtube.com/channel/{CID2}"></head></html>'


class Resp(FakeResponse):
    def __init__(self, status=200, body=b""):
        super().__init__(status, text=body.decode("utf-8", "replace"))
        self.content = body


# --- channel links ---------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    (CID, CID),
    (f"https://www.youtube.com/channel/{CID}", CID),
    (f"https://www.youtube.com/channel/{CID}/videos", CID),
    (f"youtube.com/channel/{CID}", CID),
    (f"https://www.youtube.com/feeds/videos.xml?channel_id={CID}", CID),
    ("https://www.youtube.com/@SomeChannel", "@somechannel"),
    ("https://m.youtube.com/@Some.Channel/videos", "@some.channel"),
    ("youtube.com/@some_channel?si=abc", "@some_channel"),
    ("@SomeChannel", "@somechannel"),
])
def test_parse_entry(text, want):
    assert yt.parse_entry(text) == want


@pytest.mark.parametrize("text", [
    "", "hello", "https://example.com/@someone", "https://www.youtube.com/watch?v=abc123",
    "https://www.youtube.com/channel/UCshort", "@x", "javascript:alert(1)",
    "https://www.youtube.com/feeds/videos.xml?channel_id=nope",
])
def test_parse_entry_rejects(text):
    with pytest.raises(ValueError):
        yt.parse_entry(text)


def test_parse_entries_dedupes_and_caps():
    assert yt.parse_entries(f"@a1b\n\n{CID}, https://www.youtube.com/@A1B") == ["@a1b", CID]
    assert yt.parse_entries("  ") == []
    with pytest.raises(ValueError):
        yt.parse_entries("\n".join(f"@chan{i:02d}" for i in range(11)))


@pytest.mark.parametrize("page", [
    PAGE,
    f'<script>var x = {{"externalId":"{CID2}","title":"x"}}</script>',
    f'<link rel="alternate" type="application/rss+xml" href="https://www.youtube.com/feeds/videos.xml?channel_id={CID2}">',
    f'<meta itemprop="identifier" content="{CID2}">',
])
def test_find_channel_id(page):
    assert yt.find_channel_id(page) == CID2


def test_find_channel_id_missing():
    assert yt.find_channel_id("<html>consent page</html>") is None


# --- feed parsing ------------------------------------------------------------------------
def test_parse_videos_skips_shorts_and_old_uploads():
    name, items = yt.parse_videos(FEED, "u", NOW, max_age_days=3, skip_shorts=True)
    assert name == "EXAMPLE MARK"
    assert [i["title"] for i in items] == ["Bitcoin: The Risk Nobody Is Talking About",
                                           "Altcoin Season & the Business Cycle"]
    assert items[0]["link"] == "https://www.youtube.com/watch?v=vid0000001"


def test_parse_videos_keeps_shorts_when_asked_and_longer_window():
    _, items = yt.parse_videos(FEED, "u", NOW, max_age_days=60, skip_shorts=False)
    assert len(items) == 4 and any(yt.is_short(i) for i in items)


def test_channel_with_no_uploads_is_not_an_error():
    empty = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>New Channel</title></feed>'
    assert yt.parse_videos(empty, "u", NOW) == ("NEW CHANNEL", [])


def test_malformed_feed_raises():
    with pytest.raises(ValueError):
        yt.parse_videos(b"<html>not a feed", "u", NOW)


# --- fetching ----------------------------------------------------------------------------
def make_videos(tmp_path, entries, routes, **kw):
    v = yt.Videos(entries, cache_path=str(tmp_path / "yt.json"), **kw)
    calls = []

    def get(url, timeout=None, headers=None):
        calls.append(url)
        r = routes.get(url)
        if r is None:
            raise AssertionError(f"unexpected request {url}")
        if isinstance(r, BaseException):
            raise r
        return r

    v.session.get = get
    v.calls = calls
    return v


def run(v, monkeypatch, now=NOW):
    monkeypatch.setattr(yt.time, "time", lambda: now)
    v._busy = True
    v._run()
    return v.data


def feed_url(cid):
    return yt.FEED_URL.format(cid)


def test_fetch_by_channel_id(tmp_path, monkeypatch):
    v = make_videos(tmp_path, [CID], {feed_url(CID): Resp(body=FEED)})
    data = run(v, monkeypatch)
    assert [d["source"] for d in data] == ["EXAMPLE MARK"] * 2
    assert v.status_for([CID]) == {CID: {"ok": True, "name": "EXAMPLE MARK"}}


def test_handle_is_looked_up_once_and_cached(tmp_path, monkeypatch):
    routes = {"https://www.youtube.com/@somechannel": Resp(body=PAGE.encode()),
              feed_url(CID2): Resp(body=FEED)}
    v = make_videos(tmp_path, ["@somechannel"], routes)
    assert run(v, monkeypatch)
    assert json.loads((tmp_path / "yt.json").read_text()) == {"@somechannel": CID2}
    v.calls.clear()
    run(v, monkeypatch)
    assert v.calls == [feed_url(CID2)]  # no second page lookup
    # a fresh Videos (e.g. after a restart) reads the cache too
    v2 = make_videos(tmp_path, ["@somechannel"], {feed_url(CID2): Resp(body=FEED)})
    assert run(v2, monkeypatch) and v2.calls == [feed_url(CID2)]


def test_unknown_handle_shows_an_error_and_others_still_work(tmp_path, monkeypatch):
    routes = {"https://www.youtube.com/@nobodyhere": Resp(404, b"not found"),
              feed_url(CID): Resp(body=FEED)}
    v = make_videos(tmp_path, ["@nobodyhere", CID], routes)
    data = run(v, monkeypatch)
    assert data and {d["source"] for d in data} == {"EXAMPLE MARK"}
    st = v.status_for(["@nobodyhere", CID])
    assert st["@nobodyhere"] == {"ok": False, "error": "no channel with that handle"}
    assert st[CID]["ok"] is True


def test_page_without_an_id_shows_an_error(tmp_path, monkeypatch):
    v = make_videos(tmp_path, ["@consent"], {"https://www.youtube.com/@consent": Resp(body=b"<html>consent</html>")})
    assert not run(v, monkeypatch)
    assert "channel id" in v.status_for(["@consent"])["@consent"]["error"]


def test_malformed_feed_error_is_per_channel(tmp_path, monkeypatch):
    v = make_videos(tmp_path, [CID, CID2], {feed_url(CID): Resp(body=b"<html>oops"), feed_url(CID2): Resp(body=FEED)})
    assert run(v, monkeypatch)
    assert v.status_for([CID])[CID] == {"ok": False, "error": "the feed couldn't be read"}


@pytest.mark.parametrize("failure,msg", [
    (requests.exceptions.ConnectionError("Failed to resolve host"), "couldn't reach YouTube"),
    (requests.exceptions.ReadTimeout("timed out"), "couldn't reach YouTube"),
])
def test_offline_keeps_last_good(tmp_path, monkeypatch, failure, msg):
    v = make_videos(tmp_path, [CID], {feed_url(CID): Resp(body=FEED)})
    good = run(v, monkeypatch)
    v.session.get = lambda url, timeout=None, headers=None: (_ for _ in ()).throw(failure)
    assert run(v, monkeypatch) == good
    assert v.status_for([CID])[CID]["error"] == msg


def test_offline_from_the_start_means_no_screen(tmp_path, monkeypatch):
    v = make_videos(tmp_path, [CID], {feed_url(CID): requests.exceptions.ConnectionError("offline")})
    assert not run(v, monkeypatch)


def test_last_good_uploads_age_out_while_offline(tmp_path, monkeypatch):
    v = make_videos(tmp_path, [CID], {feed_url(CID): Resp(body=FEED)})
    assert run(v, monkeypatch)
    v.session.get = lambda url, timeout=None, headers=None: (_ for _ in ()).throw(
        requests.exceptions.ConnectionError("offline"))
    later = NOW + 2.5 * 86400          # only the newest upload is still inside the 3-day window
    assert [d["title"] for d in run(v, monkeypatch, now=later)] == [
        "Bitcoin: The Risk Nobody Is Talking About"]
    week_later = NOW + 7 * 86400
    assert run(v, monkeypatch, now=week_later) == []  # nothing recent left: screen skipped


def test_no_channels_means_no_fetch(tmp_path):
    v = yt.Videos([], cache_path=str(tmp_path / "yt.json"))
    v.session.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not fetch"))
    assert v.update() is None and not v._busy


# --- in the cycle ------------------------------------------------------------------------
def make_ticker(screens=("videos",)):
    cfg = Config()
    cfg.screens = list(screens)
    cfg.videos_channels = [CID]
    t = Ticker(cfg, FakeDisplay())
    t.display.realtime = False
    return t


def test_screen_shows_n_videos_per_cycle_with_a_red_tag():
    t = make_ticker()
    t.videos.data = [{"title": f"Video {i}", "source": "CHAN", "at": None, "link": None} for i in range(7)]
    t.videos.next_fetch = float("inf")
    t.cfg.videos_per_cycle = 2
    shots = t.screens()
    assert len(shots) == 2
    s = shots[0]()
    assert isinstance(s, Scroller)
    assert yt.TAG_COLOR in {c for _, c in s.first_frame().getcolors(64 * 32)}


def test_screen_skipped_without_videos():
    t = make_ticker()
    t.videos.data, t.videos.next_fetch = None, float("inf")
    assert t.screens() == []


def test_settings(monkeypatch):
    c = Config.from_env()
    assert (c.videos_channels, c.videos_per_cycle, c.videos_max_age_days, c.videos_skip_shorts) == ([], 3, 3, True)
    monkeypatch.setenv("VIDEOS_CHANNELS", f"@somechannel,not a link,{CID},@SomeChannel")
    monkeypatch.setenv("VIDEOS_PER_CYCLE", "25")
    monkeypatch.setenv("VIDEOS_MAX_AGE_DAYS", "0")
    monkeypatch.setenv("VIDEOS_SKIP_SHORTS", "off")
    c = Config.from_env()
    assert c.videos_channels == ["@somechannel", CID]
    assert (c.videos_per_cycle, c.videos_max_age_days, c.videos_skip_shorts) == (10, 1, False)


def test_reload_recreates_videos_when_channels_change(tmp_path):
    t = make_ticker()
    env = tmp_path / "settings.env"
    env.write_text("SCREENS=clock\nVIDEOS_CHANNELS=@otherchannel\n")
    t.env_path = str(env)
    old = t.videos
    t.reload()
    assert t.videos is not old and t.videos.entries == ["@otherchannel"]


def test_videos_off_by_default():
    assert "videos" not in Config().screens


# --- phone page --------------------------------------------------------------------------
def test_page_settings(app):
    s = app.settings()
    assert s["videos_channels"] == [] and s["videos_per_cycle"] == 3
    assert s["videos_max_age_days"] == 3 and s["videos_skip_shorts"] == "on" and s["videos_status"] == {}
    assert "videos" in s["screen_names"]
    saved = app.save({"videos_channels": f"https://www.youtube.com/@SomeChannel\n{CID}\nyoutube.com/@somechannel",
                      "videos_per_cycle": 5, "videos_max_age_days": 7, "videos_skip_shorts": "off",
                      "screens": "videos,clock"})
    assert saved["VIDEOS_CHANNELS"] == f"@somechannel,{CID}" and saved["SCREENS"] == "videos,clock,coins"
    s = app.settings()
    assert s["videos_channels"] == ["@somechannel", CID]
    assert s["videos_status"] == {"@somechannel": None, CID: None}  # not checked yet
    assert app.save({"videos_channels": ""})["VIDEOS_CHANNELS"] == ""


def test_page_shows_channel_status_from_the_ticker(app):
    class T:
        videos = yt.Videos(["@somechannel"], cache_path=os.devnull)
    T.videos.status["@somechannel"] = {"ok": False, "error": "no channel with that handle"}
    app.save({"videos_channels": "@somechannel"})
    app.ticker = T()
    app.ticker.assets = []
    assert app._videos_status(lambda k, d: "@somechannel") == {
        "@somechannel": {"ok": False, "error": "no channel with that handle"}}


@pytest.mark.parametrize("field,value", [
    ("videos_channels", "https://example.com/@someone"), ("videos_channels", "javascript:alert(1)"),
    ("videos_channels", "\n".join(f"@chan{i:02d}" for i in range(11))),
    ("videos_per_cycle", 0), ("videos_per_cycle", 11), ("videos_max_age_days", 31),
    ("videos_skip_shorts", "maybe"),
])
def test_page_validation(app, field, value):
    before = open(app.env_path).read()
    with pytest.raises(ValueError):
        app.save({field: value})
    assert open(app.env_path).read() == before


# --- doctor.sh ---------------------------------------------------------------------------
def test_doctor_checks_youtube_only_when_channels_are_set():
    import doctor as dr
    seen = []

    def fetch(url):
        seen.append(url)
        return 200, ""

    names = [r.name for r in dr.check_sources({"NEWS_FEEDS": "https://a.example.com/rss"}, fetch)]
    assert "YouTube" not in names
    seen.clear()
    r = dr.check_sources({"NEWS_FEEDS": "https://a.example.com/rss", "VIDEOS_CHANNELS": f"@somechannel,{CID}"}, fetch)
    assert r[-1].name == "YouTube" and r[-1].status == "OK"
    assert seen[-1] == yt.FEED_URL.format(CID)
    r = dr.check_sources({"VIDEOS_CHANNELS": "@somechannel"}, fetch)
    assert seen[-1] == "https://www.youtube.com/"
