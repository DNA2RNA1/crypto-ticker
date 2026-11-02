"""CoinGecko client: normal data, timeouts, 429s, malformed JSON, Wi-Fi loss, cache."""

import pytest
from conftest import FakeResponse, malformed, rate_limited, timeout, wifi_down

from cache import RequestCache
from prices import CoinGecko, DataError, NetworkError, PriceError, RateLimitError


def make(http, symbols=("btc", "eth", "ada:cardano"), cache=None, **kw):
    api = CoinGecko(list(symbols), cache=cache or RequestCache(), sleep=lambda s: None, **kw)
    api.session.get = http.get
    return api


def test_normal_data_keeps_user_order(http, ok):
    http.add(ok)
    api = make(http, symbols=("eth", "btc", "ada:cardano"))
    assets = api.fetch()
    assert [a["symbol"] for a in assets] == ["ETH", "BTC", "ADA"]
    btc = assets[1]
    assert btc["price"] == 109234.56
    assert btc["change_24h"] == 1.84
    assert len(btc["sparkline"]) == 168
    assert btc["image"].startswith("https://")
    # plain tickers and pinned ids go in separate requests
    assert len(http.calls) == 2
    assert http.calls[0][1]["symbols"] == "eth,btc"
    assert http.calls[1][1]["ids"] == "cardano"


def test_timeout_is_retried_then_succeeds(http, ok):
    http.add(timeout(), ok)
    assets = make(http, symbols=("btc",)).fetch()
    assert assets[0]["symbol"] == "BTC"
    assert len(http.calls) == 2


def test_timeout_every_time_raises_network_error(http):
    http.add(timeout())
    api = make(http, symbols=("btc",))
    with pytest.raises(NetworkError, match="timed out"):
        api.fetch()
    assert len(http.calls) == 3  # three attempts, then give up


def test_wifi_loss_raises_network_error(http):
    http.add(wifi_down())
    with pytest.raises(NetworkError, match="network error"):
        make(http, symbols=("btc",)).fetch()


def test_server_error_is_retried(http, ok):
    http.add(FakeResponse(503, text="<html>Service Unavailable</html>"), ok)
    assert make(http, symbols=("btc",)).fetch()[0]["price"] == 109234.56


def test_rate_limit_is_not_retried_and_reports_retry_after(http):
    http.add(rate_limited("90"))
    with pytest.raises(RateLimitError) as info:
        make(http, symbols=("btc",)).fetch()
    assert info.value.retry_after == 90
    assert "429" in str(info.value)
    assert len(http.calls) == 1  # hammering a 429 only makes it worse


def test_rate_limit_without_header(http):
    http.add(rate_limited(None))
    with pytest.raises(RateLimitError) as info:
        make(http, symbols=("btc",)).fetch()
    assert info.value.retry_after is None


def test_rate_limit_reported_in_200_body(http):
    http.add(FakeResponse(200, {"status": {"error_code": 429, "error_message": "slow down"}}))
    with pytest.raises(RateLimitError):
        make(http, symbols=("btc",)).fetch()


def test_malformed_json(http):
    http.add(malformed())
    with pytest.raises(DataError, match="bad JSON"):
        make(http, symbols=("btc",)).fetch()


@pytest.mark.parametrize("body", [{"coins": []}, "hello", 42, None])
def test_wrong_json_shape(http, body):
    http.add(FakeResponse(200, text=__import__("json").dumps(body)))
    with pytest.raises(PriceError):
        make(http, symbols=("btc",)).fetch()


def test_bad_rows_are_skipped_not_fatal(http, markets):
    rows = [
        "not a dict",
        {"symbol": "btc"},                                          # no id
        {"id": "ethereum", "symbol": "eth", "current_price": "N/A"},  # bad price
        {"id": "cardano", "symbol": "ada", "current_price": float("nan")},
        {"id": "x", "symbol": None, "current_price": 1},
    ] + [markets[0] | {"sparkline_in_7d": {"price": [1, None, "x", 2]},
                       "price_change_percentage_24h": None, "name": 5, "image": []}]
    http.add(FakeResponse(200, rows))
    assets = make(http, symbols=("btc", "eth", "ada")).fetch()
    assert [a["symbol"] for a in assets] == ["BTC"]
    assert assets[0]["sparkline"] == [1.0, 2.0]
    assert assets[0]["change_24h"] is None
    assert assets[0]["name"] == "BTC" and assets[0]["image"] is None


def test_no_usable_rows_raises(http):
    http.add(FakeResponse(200, []))
    with pytest.raises(DataError):
        make(http, symbols=("btc",)).fetch()


def test_other_http_error(http):
    http.add(FakeResponse(401, {"error": "invalid key"}))
    with pytest.raises(PriceError, match="401"):
        make(http, symbols=("btc",)).fetch()
    assert len(http.calls) == 1


def test_every_attempt_counts_against_the_budget(http, ok):
    calls = []
    http.add(timeout(), ok)
    make(http, symbols=("btc",), on_call=lambda: calls.append(1)).fetch()
    assert len(calls) == 2


def test_cache_serves_repeat_requests_without_network(http, ok):
    http.add(ok)
    api = make(http, symbols=("btc",))
    api.fetch()
    api.fetch()
    assert len(http.calls) == 1


def test_last_good_survives_restart_offline(http, ok, tmp_path):
    path = str(tmp_path / "requests.json")
    http.add(ok)
    make(http, symbols=("btc",), cache=RequestCache(path)).fetch()

    # "reboot" with Wi-Fi down: fresh client, cache reloaded from disk
    offline = type(http)().add(wifi_down())
    api = make(offline, symbols=("btc",), cache=RequestCache(path))
    api.cache._mem = {k: (0.0, v) for k, (_, v) in api.cache._mem.items()}  # make it old
    with pytest.raises(NetworkError):
        api.fetch()
    assets, fetched_at = api.last_good()
    assert assets[0]["price"] == 109234.56
    assert fetched_at == 0.0


def test_last_good_empty_when_nothing_cached(http):
    assert make(http, symbols=("btc",)).last_good() == (None, None)


def test_search_sorts_by_rank_and_tolerates_junk(http):
    http.add(FakeResponse(200, {"coins": [
        {"id": "fake-btc", "name": "Fake", "symbol": "btc", "market_cap_rank": None},
        {"id": "bitcoin", "name": "Bitcoin", "symbol": "btc", "market_cap_rank": 1, "large": "https://x/b.png"},
        "junk", {"name": "no id"}]}))
    out = make(http).search("btc")
    assert [c["id"] for c in out] == ["bitcoin", "fake-btc"]
    make(http).search("btc")


def test_search_malformed(http):
    http.add(malformed())
    with pytest.raises(DataError):
        make(http).search("btc")
