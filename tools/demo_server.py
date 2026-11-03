#!/usr/bin/env python3
"""Serve the real phone settings page with sample data, for screenshots.

    python tools/demo_server.py --port 8095 [--state ok|needs_key]

Nothing here touches the network, the panel or this folder's settings.env: settings go
to a temporary file, and the ticker, updater, Wi-Fi and uptime are stand-ins.
"""

import argparse
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import sysinfo  # noqa: E402
import webui  # noqa: E402
from render import Renderer  # noqa: E402
from sample import sample_assets  # noqa: E402

SYMBOLS = "btc:bitcoin,eth:ethereum,ada:cardano,sol:solana,pepe:pepe,snek:snek"


class DemoUsage:
    def summary(self, interval):
        return {"calls": 4120, "budget": 10000, "month": "2026-11", "interval": interval}


class DemoSports:
    """Team search from the sample team table (no network)."""

    def search_teams(self, key, query):
        from sample import TEAMS
        q = query.lower()
        return [{"id": t[0], "abbr": t[1], "name": t[2], "color": t[3]} for t in TEAMS.get(key, [])
                if q in t[2].lower() or q == t[1].lower()]


class DemoVideos:
    """Channel check results as the phone page shows them (no network)."""

    def status_for(self, entries):
        sample = {"@examplemarkets": {"ok": True, "name": "MARKET CYCLE"},
                  "@examplecharts": {"ok": True, "name": "CHART TALK"},
                  "@nosuchchannel": {"ok": False, "error": "no channel with that handle"}}
        return {e: sample.get(e) for e in entries}


class DemoTicker:
    def __init__(self):
        self.sports = DemoSports()
        self.videos = DemoVideos()
        now = time.time()
        self.assets = sample_assets(SYMBOLS.split(","))
        self.usage = DemoUsage()
        self.renderer = Renderer()
        self._status = {"last_success": now - 95, "stale": False, "failures": 0,
                        "last_error": "network error: HTTPSConnectionPool(host='api.coingecko.com'): "
                                      "Read timed out. (read timeout=10)",
                        "last_error_at": now - 3 * 3600 - 600, "started_at": now - 2 * 86400 - 5 * 3600,
                        "coins": len(self.assets)}

    def interval(self):
        return 120

    def status(self):
        return dict(self._status)

    def request_reload(self):
        pass


class DemoUpdater:
    def __init__(self, state):
        self.state = state

    def status(self):
        base = {"version": {"commit": "cd4ec01", "date": "2026-11-02",
                            "subject": "List view: baseline punctuation in 5x8"},
                "at": time.time() - 3600, "previous": "ba8e3a2", "deploy_key": None}
        if self.state == "needs_key":
            return base | {"state": "failed", "message": "Pi can't reach GitHub — deploy key needed",
                           "can_rollback": False, "needs_key": True,
                           "deploy_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyNotARealKey0123456789abcd "
                                         "crypto-ticker@raspberrypi"}
        return base | {"state": "ok", "message": "Updated to cd4ec01.", "can_rollback": True, "needs_key": False}

    def start_update(self):
        pass

    def start_rollback(self):
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8095)
    p.add_argument("--state", default="ok", choices=("ok", "needs_key"))
    args = p.parse_args()
    tmp = tempfile.mkdtemp(prefix="ticker-demo-")
    webui.CACHE = os.path.join(tmp, "cache")
    webui.COIN_META = os.path.join(webui.CACHE, "coins.json")
    env = os.path.join(tmp, "settings.env")
    with open(env, "w") as fh:
        fh.write(f"SYMBOLS={SYMBOLS}\nFEATURED=btc,snek\nFOCUS=on\nLIST_VIEW=4\nLIST_PAGE_SECONDS=6\n"
                 "LAYOUT=classic\nBRIGHTNESS=70\nSLEEP=5\nDIM_HOURS=22-7\nDIM_BRIGHTNESS=15\n"
                 "SCREENS=clock,weather,news,videos,sports,feargreed,indices,coins\n"
                 "VIDEOS_CHANNELS=@examplemarkets,@examplecharts,@nosuchchannel\nWEATHER_PLACE=Home\nWEATHER_LAT=40.0000\n"
                 "SPORTS_NFL_FAVORITES=2:BUF\nSPORTS_NCAAF_FAVORITES=333:ALA,57:FLA\nSPORTS_NBA_TOP=8\n"
                 "WEATHER_LON=-75.0000\nTEMP_UNIT=F\nREFRESH_RATE=auto\nWEB_PIN=2468\n")
    sysinfo.wifi = lambda path=None: {"interface": "wlan0", "dbm": -58.0, "quality": 74, "label": "good"}
    sysinfo.system_uptime = lambda path=None: 6 * 86400 + 4 * 3600
    webui.start(webui.WebApp(env, DemoTicker(), search=lambda q: [], updater=DemoUpdater(args.state)),
                port=args.port)
    print(f"demo settings page on http://127.0.0.1:{args.port}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
