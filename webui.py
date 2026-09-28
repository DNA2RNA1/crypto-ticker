"""Phone-friendly settings page served by the ticker on your home network.

Open http://<pi-name>.local:8080 in Safari, then Share -> Add to Home Screen.
Protected by WEB_PIN from settings.env. Uses only the Python standard library.
"""

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

log = logging.getLogger("ticker.web")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
ICON_DIR = os.path.join(HERE, "icons")
MAX_UPLOAD = 8 * 1024 * 1024
COIN_META = os.path.join(CACHE, "coins.json")

EDITABLE = {  # key -> validator returning the string to store
    "SYMBOLS": lambda v: _symbols(v),
    "LAYOUT": lambda v: _choice(v, ("classic", "chart", "mix")),
    "TRANSITION": lambda v: _choice(v, ("slide", "none")),
    "BRIGHTNESS": lambda v: _int(v, 5, 100),
    "SLEEP": lambda v: _int(v, 2, 60),
    "DIM_HOURS": lambda v: _dim(v),
    "DIM_BRIGHTNESS": lambda v: _int(v, 1, 100),
    "CURRENCY": lambda v: _choice(v.lower(), ("usd", "eur", "gbp", "cad", "aud", "jpy")),
    "REFRESH_RATE": lambda v: "auto" if str(v).lower() in ("auto", "0", "") else _int(v, 60, 3600),
    "FEATURED": lambda v: _featured(v),
}

MAX_FAILS = 5
LOCKOUT_SECONDS = 300


# --- validation --------------------------------------------------------------
def _int(v, lo, hi):
    n = int(v)
    if not lo <= n <= hi:
        raise ValueError(f"must be {lo}-{hi}")
    return str(n)


def _choice(v, options):
    if v not in options:
        raise ValueError(f"must be one of {options}")
    return v


def _dim(v):
    if v in ("", None):
        return ""
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", v)
    if not m or int(m[1]) > 23 or int(m[2]) > 23:
        raise ValueError("use start-end hours, e.g. 22-7")
    return v


def _featured(v):
    syms = [e.strip().lower() for e in v.split(",") if e.strip()]
    for e in syms:
        if not re.fullmatch(r"[a-z0-9.\-]+", e):
            raise ValueError(f"bad featured coin: {e}")
    return ",".join(syms)


def _symbols(v):
    entries = [e.strip().lower() for e in v.split(",") if e.strip()]
    if not entries:
        raise ValueError("add at least one coin")
    if len(entries) > 30:
        raise ValueError("30 coins max")
    for e in entries:
        if not re.fullmatch(r"[a-z0-9.\-]+(:[a-z0-9.\-]+)?", e):
            raise ValueError(f"bad coin entry: {e}")
    return ",".join(entries)


# --- settings.env read/write ---------------------------------------------------
def read_settings(path):
    out = {}
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_settings(path, updates):
    """Update KEY=value lines in place (keeping comments); append new keys."""
    lines = []
    if os.path.exists(path):
        with open(path) as fh:
            lines = fh.read().splitlines()
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in updates:
            lines[i] = f"{key}={updates[key]}"
            done.add(key)
    for key, value in updates.items():
        if key not in done:
            lines.append(f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    if os.path.exists(path):  # keep the owner so you can still edit it by hand
        st = os.stat(path)
        os.chmod(tmp, st.st_mode & 0o777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except (PermissionError, AttributeError):
            pass
    os.replace(tmp, path)


# --- app state ---------------------------------------------------------------
class WebApp:
    def __init__(self, env_path, ticker=None, search=None):
        self.env_path = env_path
        self.ticker = ticker
        self.search_fn = search  # callable(query) -> list of coins
        self.fails = {}  # ip -> (count, locked_until)
        os.makedirs(CACHE, exist_ok=True)
        secret_path = os.path.join(CACHE, "web_secret")
        if not os.path.exists(secret_path):
            with open(secret_path, "w") as fh:
                fh.write(secrets.token_hex(32))
            os.chmod(secret_path, 0o600)
        with open(secret_path) as fh:
            self.secret = fh.read().strip().encode()

    # auth: the cookie is an HMAC of the PIN, so changing WEB_PIN logs everyone out
    def pin(self):
        return read_settings(self.env_path).get("WEB_PIN", "")

    def token(self):
        return hmac.new(self.secret, self.pin().encode(), hashlib.sha256).hexdigest()

    def authed(self, cookie_header):
        if not self.pin():
            return True
        c = SimpleCookie(cookie_header or "")
        return "tk" in c and hmac.compare_digest(c["tk"].value, self.token())

    def login(self, ip, pin):
        count, until = self.fails.get(ip, (0, 0))
        if time.time() < until:
            return False, f"Too many tries. Wait {int(until - time.time())}s."
        if hmac.compare_digest(str(pin), self.pin()):
            self.fails.pop(ip, None)
            return True, ""
        count += 1
        until = time.time() + LOCKOUT_SECONDS if count >= MAX_FAILS else 0
        self.fails[ip] = (0 if until else count, until)
        return False, "Wrong PIN." if not until else f"Locked for {LOCKOUT_SECONDS // 60} min."

    # coin names/icons for the list
    def _meta(self):
        try:
            with open(COIN_META) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def remember_coins(self, coins):
        meta = self._meta()
        for c in coins:
            if c.get("id"):
                meta[c["id"]] = {"name": c.get("name"), "symbol": c.get("symbol"),
                                 "image": c.get("thumb") or c.get("image")}
        with open(COIN_META, "w") as fh:
            json.dump(meta, fh)

    def settings(self):
        s = read_settings(self.env_path)
        meta = self._meta()
        live = {a["symbol"].lower(): a for a in (self.ticker.assets if self.ticker else [])}
        coins = []
        for entry in (s.get("SYMBOLS") or "btc,eth").split(","):
            entry = entry.strip().lower()
            if not entry:
                continue
            sym, _, cid = entry.partition(":")
            a = live.get(sym, {})
            m = meta.get(cid or a.get("id", ""), {})
            custom = os.path.exists(os.path.join(ICON_DIR, f"{sym}.png"))
            coins.append({"entry": entry, "symbol": sym.upper(),
                          "name": m.get("name") or a.get("name") or "",
                          "image": (f"/icons/{sym}.png?v={int(os.path.getmtime(os.path.join(ICON_DIR, sym + '.png')))}"
                                    if custom else m.get("image") or a.get("image") or ""),
                          "custom_icon": custom,
                          "price": a.get("price")})
        return {
            "coins": coins,
            "layout": s.get("LAYOUT", "classic"),
            "transition": s.get("TRANSITION", "slide"),
            "brightness": int(s.get("BRIGHTNESS") or 70),
            "sleep": int(float(s.get("SLEEP") or 5)),
            "dim_hours": s.get("DIM_HOURS", ""),
            "dim_brightness": int(s.get("DIM_BRIGHTNESS") or 15),
            "currency": s.get("CURRENCY", "usd"),
            "refresh_rate": (s.get("REFRESH_RATE") or "auto").lower(),
            "usage": (self.ticker.usage.summary(self.ticker.interval()) if self.ticker else None),
            "pin_set": bool(s.get("WEB_PIN")),
            "featured": [x for x in (s.get("FEATURED") or "").split(",") if x],
        }

    def save_icon(self, symbol, data):
        """Store an uploaded picture as icons/<symbol>.png (it overrides the default)."""
        if not re.fullmatch(r"[a-z0-9.\-]+", symbol):
            raise ValueError("bad coin")
        if not data or len(data) > MAX_UPLOAD:
            raise ValueError("picture missing or too large")
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:
            raise ValueError("that file isn't a picture I can read")
        img = img.convert("RGBA")
        img.thumbnail((256, 256))
        os.makedirs(ICON_DIR, exist_ok=True)
        img.save(os.path.join(ICON_DIR, f"{symbol}.png"))
        if self.ticker:
            self.ticker.request_reload()

    def reset_icon(self, symbol):
        path = os.path.join(ICON_DIR, f"{symbol}.png")
        if re.fullmatch(r"[a-z0-9.\-]+", symbol) and os.path.exists(path):
            os.remove(path)
        if self.ticker:
            self.ticker.request_reload()

    def save(self, data):
        updates = {}
        for key, check in EDITABLE.items():
            if key.lower() in data:
                updates[key] = check(str(data[key.lower()]))
        if "coins_meta" in data:
            self.remember_coins(data["coins_meta"])
        write_settings(self.env_path, updates)
        if self.ticker:
            self.ticker.request_reload()
        return updates

    def icon_png(self):
        """Home-screen icon: the current ticker screen, LED-style."""
        t = self.ticker
        img = None
        if t and t.assets:
            img = t.renderer._chart_screen(t.assets[0])
        if img is None:
            from render import Renderer
            img = Renderer(64, 32).message("$")
        big = img.resize((img.width * 3, img.height * 3), Image.NEAREST)
        out = Image.new("RGB", (192, 192), (0, 0, 0))
        out.paste(big, (0, (192 - big.height) // 2))
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()


# --- HTTP --------------------------------------------------------------------
def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "crypto-ticker"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.client_address[0], *args)

        def _send(self, code, body, ctype="application/json", headers=None):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def _authed(self):
            return app.authed(self.headers.get("Cookie"))

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if url.path in ("/icon.png", "/apple-touch-icon.png"):
                return self._send(200, app.icon_png(), "image/png")
            m = re.fullmatch(r"/icons/([a-z0-9.\-]+)\.png", url.path)
            if m and os.path.exists(os.path.join(ICON_DIR, m[1] + ".png")):
                with open(os.path.join(ICON_DIR, m[1] + ".png"), "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            if url.path == "/api/state":
                return self._send(200, {"authed": self._authed(), "pin_set": bool(app.pin())})
            if not self._authed():
                return self._send(401, {"error": "login required"})
            if url.path == "/api/settings":
                return self._send(200, app.settings())
            if url.path == "/api/search":
                q = parse_qs(url.query).get("q", [""])[0].strip()
                if len(q) < 2:
                    return self._send(200, {"coins": []})
                try:
                    return self._send(200, {"coins": app.search_fn(q) if app.search_fn else []})
                except Exception as exc:  # network trouble shouldn't kill the server
                    log.warning("search failed: %s", exc)
                    return self._send(502, {"error": "Search failed. Check the Pi's internet."})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            if url.path == "/api/login":
                ok, msg = app.login(self.client_address[0], self._body().get("pin", ""))
                if not ok:
                    return self._send(403, {"error": msg})
                cookie = f"tk={app.token()}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"
                return self._send(200, {"ok": True}, headers={"Set-Cookie": cookie})
            if not self._authed():
                return self._send(401, {"error": "login required"})
            if url.path in ("/api/icon", "/api/icon/reset"):
                sym = parse_qs(url.query).get("sym", [""])[0].lower()
                try:
                    if url.path == "/api/icon":
                        n = int(self.headers.get("Content-Length") or 0)
                        if n > MAX_UPLOAD:
                            raise ValueError("picture too large")
                        app.save_icon(sym, self.rfile.read(n))
                    else:
                        app.reset_icon(sym)
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                return self._send(200, {"ok": True})
            if url.path == "/api/settings":
                try:
                    saved = app.save(self._body())
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                return self._send(200, {"ok": True, "saved": saved})
            self._send(404, {"error": "not found"})

    return Handler


def start(app, port=8080):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="webui").start()
    log.info("settings page on http://%s.local:%d", os.uname().nodename, port)
    return server


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Ticker">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>Ticker</title>
<style>
:root{--bg:#0b0d10;--card:#16191e;--line:#262a31;--text:#eef1f5;--muted:#8a93a0;--accent:#f5b400;--up:#1fd07a;--down:#ff4d3d}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.4 -apple-system,system-ui,sans-serif;
 padding:env(safe-area-inset-top) 16px calc(env(safe-area-inset-bottom) + 24px)}
h1{font-size:28px;margin:18px 0 4px}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:26px 4px 8px}
.card{background:var(--card);border-radius:14px;overflow:hidden}
.row{display:flex;align-items:center;gap:12px;padding:12px 14px;border-top:1px solid var(--line);min-height:56px}
.row:first-child{border-top:0}
.row img,.ph{width:30px;height:30px;border-radius:50%;background:#222;flex:none}
.grow{flex:1;min-width:0}.sym{font-weight:700}.name{color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btn{border:0;background:#232830;color:var(--text);width:38px;height:38px;border-radius:10px;font-size:17px}
.btn:disabled{opacity:.25}.del{color:var(--down)}.star{color:#5a616b;font-size:20px}.star.on{color:var(--accent)}
.pic{position:relative;flex:none;border:0;background:none;padding:0}.pic:after{content:"✎";position:absolute;right:-4px;bottom:-4px;font-size:11px;
 background:#2b313a;color:var(--text);border-radius:50%;width:16px;height:16px;line-height:16px;text-align:center}
.link{color:var(--accent);text-decoration:none}
input[type=search],input[type=password]{width:100%;padding:13px 14px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--text);font-size:17px}
.seg{display:flex;width:100%;background:#0f1216;border-radius:10px;padding:3px;gap:3px}
.seg button{flex:1;border:0;background:none;color:var(--muted);padding:9px 0;border-radius:8px;font-size:15px}
.seg button.on{background:#2b313a;color:var(--text);font-weight:600}
input[type=range]{width:100%;accent-color:var(--accent)}
select{background:#232830;color:var(--text);border:0;border-radius:8px;padding:8px;font-size:16px}
.val{color:var(--muted);font-variant-numeric:tabular-nums;min-width:48px;text-align:right}
.rank{color:var(--muted);font-size:13px}.add{color:var(--accent);font-size:26px;font-weight:300}
#toast{position:fixed;left:50%;bottom:calc(env(safe-area-inset-bottom) + 20px);transform:translateX(-50%);background:#2b313a;
 padding:10px 18px;border-radius:20px;font-size:15px;opacity:0;transition:opacity .25s;pointer-events:none}
#toast.show{opacity:1}.err{color:var(--down)}
.hint{color:var(--muted);font-size:13px;margin:8px 4px}
.login{max-width:320px;margin:22vh auto 0;text-align:center}
.login button{margin-top:12px;width:100%;padding:13px;border:0;border-radius:12px;background:var(--accent);color:#000;font-size:17px;font-weight:600}
.switch{position:relative;width:51px;height:31px;flex:none}.switch input{display:none}
.switch span{position:absolute;inset:0;background:#39404a;border-radius:31px;transition:.2s}
.switch span:before{content:"";position:absolute;width:27px;height:27px;left:2px;top:2px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked+span{background:var(--up)}.switch input:checked+span:before{transform:translateX(20px)}
</style></head><body>
<div id="app"></div><div id="toast"></div>
<script>
const $=s=>document.querySelector(s), app=$('#app');
let S=null, saveTimer=null, searchTimer=null, pendingMeta=[];
const esc=t=>String(t??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(msg,bad){const t=$('#toast');t.textContent=msg;t.className='show'+(bad?' err':'');clearTimeout(t._h);t._h=setTimeout(()=>t.className='',1600)}
async function api(path,body){
  const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  const j=await r.json().catch(()=>({}));
  if(r.status==401){showLogin();throw new Error('login')}
  if(!r.ok)throw new Error(j.error||('HTTP '+r.status));
  return j;
}
function showLogin(msg){
  app.innerHTML=`<div class="login"><h1>Ticker</h1><p class="hint">Enter PIN</p>
  <input id="pin" type="password" inputmode="numeric" autocomplete="current-password" autofocus>
  <button id="go">Unlock</button><p class="err" id="lerr">${esc(msg||'')}</p></div>`;
  const go=async()=>{try{await api('/api/login',{pin:$('#pin').value});load()}catch(e){if(e.message!='login')$('#lerr').textContent=e.message}};
  $('#go').onclick=go;$('#pin').onkeydown=e=>{if(e.key=='Enter')go()};
}
async function load(){
  try{S=await api('/api/settings')}catch(e){return}
  render();
}
function coinRow(c,i,n){
  const img=c.image?`<img src="${esc(c.image)}" alt="">`:'<div class="ph"></div>';
  const sym=c.entry.split(':')[0], fav=(S.featured||[]).includes(sym);
  const sub=c.custom_icon?`<a href="#" class="link" data-reset="${i}">Reset picture</a>`:esc(c.name||c.entry);
  return `<div class="row"><button class="pic" data-pic="${i}" aria-label="Change icon">${img}</button>
  <div class="grow"><div class="sym">${esc(c.symbol)}</div><div class="name">${sub}</div></div>
  <button class="btn star ${fav?'on':''}" data-star="${i}" aria-label="Feature">★</button>
  <button class="btn" data-up="${i}" ${i==0?'disabled':''} aria-label="Move up">↑</button>
  <button class="btn" data-down="${i}" ${i==n-1?'disabled':''} aria-label="Move down">↓</button>
  <button class="btn del" data-del="${i}" aria-label="Remove">✕</button></div>`;
}
function seg(key,opts){return `<div class="seg" data-seg="${key}">${opts.map(([v,l])=>`<button data-v="${v}" class="${S[key]==v?'on':''}">${l}</button>`).join('')}</div>`}
function hourSel(id,val){return `<select id="${id}">${[...Array(24).keys()].map(h=>`<option value="${h}" ${h==val?'selected':''}>${(h%12||12)+(h<12?' am':' pm')}</option>`).join('')}</select>`}
function render(){
  const dim=S.dim_hours?S.dim_hours.split('-').map(Number):null;
  app.innerHTML=`<h1>Ticker</h1>
  <h2>Coins</h2><div class="card" id="coins">${S.coins.map((c,i)=>coinRow(c,i,S.coins.length)).join('')}</div>
  <p class="hint">★ = featured: gets a big-picture screen before its price. Tap a coin's picture to use your own.</p>
  <input type="file" id="file" accept="image/*" hidden>
  <h2>Add a coin</h2><input id="q" type="search" placeholder="Search name or ticker, e.g. cardano" autocomplete="off" autocorrect="off" autocapitalize="off">
  <div class="card" id="results" style="margin-top:8px"></div>
  <h2>Display</h2><div class="card">
   <div class="row"><div class="grow">Layout</div></div>
   <div class="row" style="border-top:0;padding-top:0">${seg('layout',[['classic','Icons'],['chart','Chart'],['mix','Mix']])}</div>
   <div class="row"><div class="grow">Brightness</div><div class="val" id="bv">${S.brightness}%</div></div>
   <div class="row" style="border-top:0;padding-top:0"><input type="range" id="bright" min="5" max="100" value="${S.brightness}"></div>
   <div class="row"><div class="grow">Seconds per coin</div><div class="val" id="sv">${S.sleep}s</div></div>
   <div class="row" style="border-top:0;padding-top:0"><input type="range" id="sleep" min="2" max="30" value="${S.sleep}"></div>
   <div class="row"><div class="grow">Slide between coins</div><label class="switch"><input type="checkbox" id="slide" ${S.transition=='slide'?'checked':''}><span></span></label></div>
  </div>
  <h2>Night dimming</h2><div class="card">
   <div class="row"><div class="grow">Dim at night</div><label class="switch"><input type="checkbox" id="dim" ${dim?'checked':''}><span></span></label></div>
   ${dim?`<div class="row"><div class="grow">From</div>${hourSel('dfrom',dim[0])}<div>to</div>${hourSel('dto',dim[1])}</div>
   <div class="row"><div class="grow">Night brightness</div><div class="val" id="dbv">${S.dim_brightness}%</div></div>
   <div class="row" style="border-top:0;padding-top:0"><input type="range" id="dbright" min="1" max="60" value="${S.dim_brightness}"></div>`:''}
  </div>
  <h2>Prices</h2><div class="card">
   <div class="row"><div class="grow">Currency</div><select id="cur">${['usd','eur','gbp','cad','aud','jpy'].map(c=>`<option ${S.currency==c?'selected':''}>${c}</option>`).join('')}</select></div>
   <div class="row"><div class="grow">Refresh prices every</div><select id="rr">${[['auto','Auto'],['60','1 min'],['120','2 min'],['180','3 min'],['300','5 min'],['600','10 min'],['900','15 min'],['1800','30 min']].map(([v,l])=>`<option value="${v}" ${String(S.refresh_rate)==v?'selected':''}>${l}</option>`).join('')}</select></div>
   ${usageRow()}
  </div>
  <p class="hint">Changes show on the ticker within a few seconds.${S.pin_set?'':' <span class="err">No PIN set: anyone on your Wi-Fi can change this. Add WEB_PIN to settings.env.</span>'}</p>`;
  bind();
}
function save(extra){
  clearTimeout(saveTimer);
  saveTimer=setTimeout(async()=>{
    const body={symbols:S.coins.map(c=>c.entry).join(','),featured:(S.featured||[]).join(','),layout:S.layout,transition:S.transition,brightness:S.brightness,
      sleep:S.sleep,dim_hours:S.dim_hours,dim_brightness:S.dim_brightness,currency:S.currency,refresh_rate:S.refresh_rate,coins_meta:pendingMeta};
    try{await api('/api/settings',body);pendingMeta=[];toast('Saved ✓')}catch(e){if(e.message!='login')toast(e.message,true)}
  },extra===0?0:450);
}
function bind(){
  $('#coins').onclick=e=>{
    const r=e.target.closest('[data-reset]');
    if(r){e.preventDefault();resetIcon(S.coins[+r.dataset.reset]);return}
    const b=e.target.closest('button');if(!b)return;
    const L=S.coins;
    if(b.dataset.pic!==undefined){pickIcon(L[+b.dataset.pic]);return}
    if(b.dataset.star!==undefined){const sym=L[+b.dataset.star].entry.split(':')[0];S.featured=S.featured||[];
      S.featured=S.featured.includes(sym)?S.featured.filter(x=>x!=sym):[...S.featured,sym];
      b.classList.toggle('on');save(0);return}
    if(b.dataset.del!==undefined){if(L.length<2)return toast('Keep at least one coin',true);L.splice(+b.dataset.del,1)}
    else if(b.dataset.up!==undefined){const i=+b.dataset.up;[L[i-1],L[i]]=[L[i],L[i-1]]}
    else if(b.dataset.down!==undefined){const i=+b.dataset.down;[L[i+1],L[i]]=[L[i],L[i+1]]}
    $('#coins').innerHTML=L.map((c,i)=>coinRow(c,i,L.length)).join('');save(0);
  };
  $('#q').oninput=e=>{clearTimeout(searchTimer);const q=e.target.value.trim();
    if(q.length<2){$('#results').innerHTML='';return}
    searchTimer=setTimeout(()=>search(q),350)};
  document.querySelectorAll('[data-seg]').forEach(s=>s.onclick=e=>{const b=e.target.closest('button');if(!b)return;
    S[s.dataset.seg]=b.dataset.v;s.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x==b));save()});
  $('#bright').oninput=e=>{S.brightness=+e.target.value;$('#bv').textContent=S.brightness+'%';save()};
  $('#sleep').oninput=e=>{S.sleep=+e.target.value;$('#sv').textContent=S.sleep+'s';save()};
  $('#slide').onchange=e=>{S.transition=e.target.checked?'slide':'none';save()};
  $('#dim').onchange=e=>{S.dim_hours=e.target.checked?'22-7':'';render();save()};
  if($('#dfrom')){const f=()=>{S.dim_hours=$('#dfrom').value+'-'+$('#dto').value;save()};$('#dfrom').onchange=f;$('#dto').onchange=f;
    $('#dbright').oninput=e=>{S.dim_brightness=+e.target.value;$('#dbv').textContent=S.dim_brightness+'%';save()}}
  $('#cur').onchange=e=>{S.currency=e.target.value;save()};
  $('#rr').onchange=e=>{S.refresh_rate=e.target.value;save();setTimeout(load,1200)};
}
function usageRow(){
  const u=S.usage;if(!u)return '';
  const pct=Math.min(100,Math.round(u.calls/u.budget*100));
  const every=u.interval<90?'every minute':`every ${(u.interval/60).toFixed(u.interval%60?1:0)} min`;
  const perMonth=Math.round(30*24*3600/u.interval);
  const note=S.refresh_rate=='auto'
    ? `Auto: refreshing ${every}. Speeds up or slows down to stay under the free limit.`
    : (perMonth>u.budget?`<span class="err">If left on 24/7 this rate would use ~${perMonth.toLocaleString()} calls a month.</span> Fine if the ticker is off part of the day; Auto handles this for you.`:`Refreshing ${every}.`);
  return `<div class="row" style="display:block"><div style="display:flex;justify-content:space-between"><span>Calls this month</span>
    <span class="val">${u.calls.toLocaleString()} / ${u.budget.toLocaleString()}</span></div>
    <div style="height:6px;background:#0f1216;border-radius:3px;margin:8px 0 6px"><div style="height:6px;width:${pct}%;border-radius:3px;background:${pct>85?'var(--down)':'var(--up)'}"></div></div>
    <div class="name" style="white-space:normal">${note}</div></div>`;
}
function pickIcon(c){
  const f=$('#file');f.value='';
  f.onchange=async()=>{
    const file=f.files[0];if(!file)return;
    try{
      // shrink on the phone first: small upload, and HEIC photos become PNG
      const bmp=await createImageBitmap(file);const k=Math.min(1,256/Math.max(bmp.width,bmp.height));
      const cv=document.createElement('canvas');cv.width=Math.round(bmp.width*k);cv.height=Math.round(bmp.height*k);
      cv.getContext('2d').drawImage(bmp,0,0,cv.width,cv.height);
      const blob=await new Promise(r=>cv.toBlob(r,'image/png'));
      const sym=c.entry.split(':')[0];
      const r=await fetch('/api/icon?sym='+encodeURIComponent(sym),{method:'POST',headers:{'Content-Type':'image/png'},body:blob});
      if(r.status==401)return showLogin();
      if(!r.ok)throw new Error((await r.json()).error);
      toast('New picture for '+c.symbol);load();
    }catch(e){toast(e.message||'Upload failed',true)}
  };
  f.click();
}
async function resetIcon(c){
  const sym=c.entry.split(':')[0];
  try{await api('/api/icon/reset?sym='+encodeURIComponent(sym),{});toast(c.symbol+' back to default');load()}
  catch(e){if(e.message!='login')toast(e.message,true)}
}
async function search(q){
  const box=$('#results');box.innerHTML='<div class="row"><div class="grow name">Searching…</div></div>';
  try{
    const {coins}=await api('/api/search?q='+encodeURIComponent(q));
    if(!coins.length){box.innerHTML='<div class="row"><div class="grow name">No matches</div></div>';return}
    box.innerHTML=coins.map((c,i)=>`<div class="row" data-i="${i}">${c.thumb?`<img src="${esc(c.thumb)}" alt="">`:'<div class="ph"></div>'}
      <div class="grow"><div class="sym">${esc(c.symbol)}</div><div class="name">${esc(c.name)}</div></div>
      <div class="rank">${c.market_cap_rank?'#'+c.market_cap_rank:''}</div><div class="add">＋</div></div>`).join('');
    box.onclick=e=>{const r=e.target.closest('[data-i]');if(!r)return;const c=coins[+r.dataset.i];
      const entry=c.symbol.toLowerCase()+':'+c.id;
      if(S.coins.some(x=>x.entry==entry||x.entry.split(':')[1]==c.id))return toast('Already on the list');
      S.coins.push({entry,symbol:c.symbol,name:c.name,image:c.thumb});pendingMeta.push(c);
      $('#coins').innerHTML=S.coins.map((x,i)=>coinRow(x,i,S.coins.length)).join('');
      box.innerHTML='';$('#q').value='';toast('Added '+c.symbol);save(0)};
  }catch(e){if(e.message!='login')box.innerHTML=`<div class="row"><div class="grow err">${esc(e.message)}</div></div>`}
}
api('/api/state').then(s=>s.authed?load():showLogin());
</script></body></html>
"""
