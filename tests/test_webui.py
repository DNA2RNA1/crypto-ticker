"""Phone settings page: every input is validated before settings.env is touched."""

import json

import pytest

import webui
from conftest import login, request


GOOD = {"symbols": "btc:bitcoin,ada:cardano", "layout": "chart", "transition": "none", "brightness": 55,
        "sleep": 8, "dim_hours": "22-7", "dim_brightness": 10, "currency": "eur", "refresh_rate": "auto",
        "featured": "ada", "screens": "clock,coins", "weather_lat": "40.7128", "weather_lon": "-74.006",
        "weather_place": "Somewhere, XX", "temp_unit": "C", "timezone": "Europe/London",
        "indices_source": "yahoo"}


def test_valid_save_writes_and_keeps_comments(app):
    saved = app.save(GOOD)
    text = open(app.env_path).read()
    assert text.startswith("# my settings\n")
    assert "SYMBOLS=btc:bitcoin,ada:cardano" in text and "WEATHER_LON=-74.0060" in text
    assert saved["BRIGHTNESS"] == "55" and "WEB_PIN=2468" in text


@pytest.mark.parametrize("field,value", [
    ("brightness", "abc"), ("brightness", 0), ("brightness", 101), ("brightness", "1e2"), ("brightness", True),
    ("sleep", 1), ("sleep", "5.5"), ("dim_brightness", -1),
    ("dim_hours", "25-7"), ("dim_hours", "night"), ("dim_hours", "7-7"),
    ("layout", "fancy"), ("transition", "fade"), ("currency", "btc"), ("temp_unit", "K"),
    ("refresh_rate", 30), ("refresh_rate", "hourly"), ("refresh_rate", 99999),
    ("symbols", ""), ("symbols", "btc,<script>"), ("symbols", "btc\nWEB_PIN="), ("symbols", ",".join(f"c{i}" for i in range(31))),
    ("symbols", "a" * 21), ("featured", "bad coin"),
    ("screens", "clock,radar"),
    ("weather_lat", "91"), ("weather_lat", "nan"), ("weather_lon", "-181"), ("weather_lon", "1;2"),
    ("timezone", "Mars/Olympus"), ("timezone", "../../etc/passwd"), ("timezone", "UTC\nWEB_PIN=1"),
    ("indices_source", "bloomberg"),
    ("brightness", [70]), ("symbols", {"a": 1}), ("layout", None),
])
def test_invalid_values_are_rejected_and_nothing_written(app, field, value):
    before = open(app.env_path).read()
    with pytest.raises(ValueError) as info:
        app.save(dict(GOOD, **{field: value}))
    assert ":" in str(info.value)  # message names the field, e.g. "Brightness: must be 5-100"
    assert open(app.env_path).read() == before


def test_text_is_sanitized(app):
    saved = app.save({"weather_place": "Town\n<b>WEB_PIN=1</b>  , XX"})
    assert saved["WEATHER_PLACE"] == "Town bWEB_PIN1b , XX"
    assert "\n" not in saved["WEATHER_PLACE"]


def test_normalization(app):
    saved = app.save({"symbols": " BTC , btc, Eth:Ethereum ", "screens": "clock,clock",
                      "dim_hours": "07-22", "layout": " Chart "})
    assert saved["SYMBOLS"] == "btc,eth:ethereum"
    assert saved["SCREENS"] == "clock"  # crypto prices can be off now
    assert saved["DIM_HOURS"] == "7-22" and saved["LAYOUT"] == "chart"


def test_featured_coins_must_be_on_the_list(app):
    saved = app.save({"symbols": "btc,eth", "featured": "eth,doge"})
    assert saved["FEATURED"] == "eth"


def test_non_object_body_rejected(app):
    with pytest.raises(ValueError):
        app.save(["symbols", "btc"])


def test_coins_meta_is_sanitized(app):
    app.save({"symbols": "btc", "coins_meta": [
        {"id": "bitcoin", "name": "Bitcoin<script>", "symbol": "btc", "thumb": "javascript:alert(1)"},
        {"id": "../evil"}, "junk"]})
    meta = json.load(open(webui.COIN_META))
    assert list(meta) == ["bitcoin"]
    assert meta["bitcoin"]["name"] == "Bitcoinscript" and not meta["bitcoin"]["image"]
    with pytest.raises(ValueError):
        app.save({"coins_meta": "nope"})


def test_write_settings_refuses_newlines(tmp_path):
    with pytest.raises(ValueError):
        webui.write_settings(str(tmp_path / "s.env"), {"SYMBOLS": "btc\nWEB_PIN="})


def test_bad_hand_edits_dont_break_the_page(app):
    with open(app.env_path, "a") as fh:
        fh.write("REFRESH_RATE=30\nBRIGHTNESS=70%\nTIMEZONE=Nowhere/Land\nWEATHER_LAT=lots\nSYMBOLS=btc,BAD COIN\n")
    s = app.settings()
    assert s["refresh_rate"] == "auto" and s["brightness"] == 70 and s["timezone"] == ""
    assert [c["entry"] for c in s["coins"]] == ["btc"]
    # what the page sends back is accepted
    app.save({"refresh_rate": s["refresh_rate"], "brightness": s["brightness"], "timezone": s["timezone"],
              "weather_lat": s["weather_lat"], "symbols": ",".join(c["entry"] for c in s["coins"])})


def test_login_lockout_and_odd_pins(app):
    ok, _ = app.login("1.2.3.4", "2468")
    assert ok
    for pin in ("ünïcode", {"x": 1}, None, "9" * 1000):
        ok, _ = app.login("5.6.7.8", pin)  # must not raise
        assert not ok
    ok, msg = app.login("5.6.7.8", "0000")
    assert not ok and "Locked" in msg
    ok, msg = app.login("5.6.7.8", "2468")
    assert not ok and "Too many" in msg


def test_icon_upload_validation(app):
    with pytest.raises(ValueError):
        app.save_icon("../x", b"data")
    with pytest.raises(ValueError):
        app.save_icon("btc", b"not an image")
    with pytest.raises(ValueError):
        app.save_icon("btc", b"")


def test_http_requires_login(server):
    status, body, _ = request(server, "POST", "/api/settings", b'{"brightness": 50}')
    assert status == 401


def test_http_rejects_bad_requests(server):
    auth = login(server)
    assert request(server, "POST", "/api/settings", b"{not json", auth)[0] == 400
    assert request(server, "POST", "/api/settings", b"[1,2]", auth)[0] == 400
    assert request(server, "POST", "/api/settings", b"\xff\xfe", auth)[0] == 400
    status, body, _ = request(server, "POST", "/api/settings", b'{"brightness": "loud"}', auth)
    assert status == 400 and body["error"].startswith("Brightness")
    big = json.dumps({"weather_place": "x" * 70000}).encode()
    assert request(server, "POST", "/api/settings", big, auth)[0] == 413
    assert request(server, "POST", "/api/settings", b"{}", dict(auth, **{"Content-Length": "-5"}))[0] == 400
    assert request(server, "POST", "/api/icon?sym=btc", b"xx", auth)[0] == 400
    assert request(server, "POST", "/api/settings", b'{"brightness": 50}', auth)[0] == 200


def test_http_login_with_garbage_body(server):
    assert request(server, "POST", "/api/login", b"null")[0] == 400
    assert request(server, "POST", "/api/login", b'{"pin": ["1"]}')[0] == 403


def test_focus_and_list_view_settings(app):
    saved = app.save({"focus": "on", "list_view": "4", "list_page_seconds": 8})
    assert saved == {"FOCUS": "on", "LIST_VIEW": "4", "LIST_PAGE_SECONDS": "8"}
    s = app.settings()
    assert (s["focus"], s["list_view"], s["list_page_seconds"]) == ("on", "4", 8)
    for bad in ({"focus": "yes"}, {"list_view": "3"}, {"list_page_seconds": 1}, {"list_page_seconds": "6s"}):
        with pytest.raises(ValueError):
            app.save(bad)


def test_focus_defaults_off(app):
    s = app.settings()
    assert (s["focus"], s["list_view"], s["list_page_seconds"]) == ("off", "off", 6)


def test_indices_view_setting_on_page(app):
    assert app.settings()["indices_view"] == "percent"
    assert app.save({"indices_view": "both"}) == {"INDICES_VIEW": "both"}
    assert app.settings()["indices_view"] == "both"
    with pytest.raises(ValueError):
        app.save({"indices_view": "points"})


def test_transition_choices(app):
    assert app.settings()["transition"] == "scroll"
    for v in ("scroll", "slide", "none", "Scroll"):
        assert app.save({"transition": v})["TRANSITION"] == v.lower()
    with pytest.raises(ValueError):
        app.save({"transition": "fade"})


# --- weather / news / sports settings ------------------------------------------------
def test_new_screens_save_in_order(app):
    saved = app.save({"screens": "sports,clock,news,weather"})
    assert saved["SCREENS"] == "sports,clock,news,weather"
    s = app.settings()
    assert s["screens"] == ["sports", "clock", "news", "weather"]
    assert s["screen_names"][-1] == "coins" and {"weather", "news", "sports"} <= set(s["screen_names"])


def test_news_settings(app):
    s = app.settings()
    assert len(s["news_feeds"]) == 3 and s["news_per_cycle"] == 3 and s["news_speed"] == "normal"
    saved = app.save({"news_feeds": "https://a.example.com/rss\n\nhttps://b.example.com/feed.xml\nhttps://a.example.com/rss",
                      "news_per_cycle": 5, "news_speed": "fast"})
    assert saved["NEWS_FEEDS"] == "https://a.example.com/rss,https://b.example.com/feed.xml"
    assert app.settings()["news_feeds"] == ["https://a.example.com/rss", "https://b.example.com/feed.xml"]


@pytest.mark.parametrize("field,value", [
    ("news_feeds", ""), ("news_feeds", "ftp://x.example.com/rss"), ("news_feeds", "javascript:alert(1)"),
    ("news_feeds", "https://x.example.com/<script>"), ("news_feeds", "\n".join(f"https://f{i}.example.com" for i in range(9))),
    ("news_per_cycle", 0), ("news_per_cycle", 11), ("news_speed", "warp"),
    ("sports_nfl", "maybe"), ("sports_nfl_top", "5"), ("sports_ncaaf_top", "8"),
    ("sports_nba_favorites", "BOS"), ("sports_nba_favorites", "2:BOSTONCELTICS"), ("sports_nba_favorites", "x2:BOS"),
    ("sports_nba_favorites", ",".join(f"{i}:T{i}" for i in range(21))),
])
def test_new_settings_validation(app, field, value):
    before = open(app.env_path).read()
    with pytest.raises(ValueError):
        app.save({field: value})
    assert open(app.env_path).read() == before


def test_sports_settings(app):
    sports = {lg["key"]: lg for lg in app.settings()["sports"]}
    assert list(sports) == ["nfl", "ncaaf", "nba", "ncaam", "mlb", "nhl"]
    assert sports["nfl"]["on"] and not sports["nhl"]["on"] and sports["ncaaf"]["top"] == 25
    assert sports["ncaaf"]["top_choices"] == [0, 10, 25] and sports["nba"]["top_choices"] == [0, 4, 8, 12, 16]
    saved = app.save({"sports_nhl": "on", "sports_nba_top": "12", "sports_nba_favorites": "2:bos, 7:DEN,2:BOS"})
    assert saved == {"SPORTS_NHL": "on", "SPORTS_NBA_TOP": "12", "SPORTS_NBA_FAVORITES": "2:BOS,7:DEN"}
    sports = {lg["key"]: lg for lg in app.settings()["sports"]}
    assert sports["nhl"]["on"] and sports["nba"]["favorites"] == [{"id": "2", "abbr": "BOS"}, {"id": "7", "abbr": "DEN"}]


def test_team_search_endpoint(app, server):
    calls = []

    def search(league, q):
        calls.append((league, q))
        if league == "bad":
            raise ValueError("unknown league")
        if q == "down":
            raise ConnectionError("offline")
        return [{"id": "2", "abbr": "BUF", "name": "Buffalo Bills", "color": "00338d"}]

    app.team_search = search
    auth = login(server)
    status, body, _ = request(server, "GET", "/api/teams?league=nfl&q=bil", headers=auth)
    assert status == 200 and body["teams"][0]["abbr"] == "BUF" and calls == [("nfl", "bil")]
    assert request(server, "GET", "/api/teams?league=bad&q=x", headers=auth)[0] == 400
    assert request(server, "GET", "/api/teams?league=nfl&q=down", headers=auth)[0] == 502
    assert request(server, "GET", "/api/teams?league=nfl&q=bil")[0] == 401
