// Crypto Ticker widget for Scriptable (iPhone home screen, lock screen, StandBy)
// Companion to the Raspberry Pi LED ticker: same chart look, same price rules.
// It fetches everything itself, so it works away from home (the Pi isn't needed).
//
// SETUP
// 1. Install "Scriptable" from the App Store, tap +, paste this whole file, name it "Crypto Ticker".
// 2. Add a Scriptable widget, long-press it -> Edit Widget:
//      Script:    Crypto Ticker
//      Parameter: one of the forms below
//    Tapping a widget opens the coin on CoinGecko, or the article for a headline.
//
// PARAMETER
//   coins:<sym,sym,...>   prices for a coin list
//       coins:btc                     one coin
//       coins:btc,ada,snek            several (medium widget shows up to 3)
//       coins:night:midnight-3        "sym:coingecko-id" pins an exact coin (id is in its CoinGecko URL)
//   news:<RSS or Atom URL>  the latest headline from a feed
//       news:https://www.coindesk.com/arc/outboundfeeds/rss/
//       news:https://feeds.bbci.co.uk/news/technology/rss.xml
//   btc,ada               the old plain form still works (same as coins:btc,ada)
//   (empty)               DEFAULT_COINS below
//
// WHERE EACH WIDGET SIZE GOES
//   Home screen small / StandBy:  one coin, big price over a 7-day chart (first coin of the list)
//   Home screen medium:           up to 3 coins with mini charts
//   Lock screen inline (above the clock):
//       coins:btc,eth,sol    -> one coin per refresh, rotating through the list
//       news:<url>           -> the latest headline
//   Lock screen circular:
//       coins:btc            -> symbol, ▲/▼ and a ring gauge of the 24h % change
//                               (full ring = GAUGE_FULL_SCALE %). Uses the first coin.
//       news:<url>           -> source initials and the headline's age
//   Lock screen rectangular:
//       coins:btc            -> symbol, 24h change, price and a sparkline
//       coins:btc,eth        -> two coins, one per row
//       news:<url>           -> source and age, then a 2-3 line headline
//   StandBy: add small widgets to StandBy's stacks, one coin each, and swipe to switch.
//
// Lock-screen widgets are monochrome, so direction is shown with ▲/▼, never color alone.
// If a fetch fails (no signal, rate limit), the last good data is shown and marked:
// "offline" on the home screen, ⚠︎ on the lock screen. iOS decides when widgets refresh
// (usually every 15-30 minutes).

// ----- settings you can change -----
const DEFAULT_COINS = "btc"
const CURRENCY = "usd"
const API_KEY = ""               // optional free CoinGecko Demo key
const REFRESH_MINUTES = 15       // a request; iOS decides the real timing
const GAUGE_FULL_SCALE = 10      // circular widget: a 10% move fills the whole ring
const KNOWN = {                  // ticker -> exact CoinGecko id
  btc: "bitcoin", eth: "ethereum", ada: "cardano", link: "chainlink",
  hbar: "hedera-hashgraph", pepe: "pepe", doge: "dogecoin", snek: "snek",
  sol: "solana", night: "midnight-3", xrp: "ripple",
}
// -------------------------------------

const HEX = { up: "#1FD07A", down: "#FF4D3D", flat: "#9AA0A6", white: "#FFFFFF" }
const UP = new Color(HEX.up), DOWN = new Color(HEX.down), FLAT = new Color(HEX.flat)
const WHITE = Color.white(), BG = new Color("#0B0D10")
const SIGNS = { usd: "$", eur: "€", gbp: "£", jpy: "¥", cad: "$", aud: "$" }
const STALE = "⚠︎"
const fm = FileManager.local()
const dir = fm.joinPath(fm.cachesDirectory(), "crypto-ticker")
if (!fm.fileExists(dir)) fm.createDirectory(dir, true)

// ---------- parameter ----------
// "coins:btc,ada" | "news:https://..." | "btc,ada" (old form) | "" (DEFAULT_COINS)
function parseParam(param) {
  const p = (param || "").trim()
  const m = p.match(/^(coins|news)\s*:\s*(.*)$/i)
  if (m && m[1].toLowerCase() === "news") {
    const url = m[2].trim()
    if (!/^https?:\/\/\S+$/i.test(url)) throw new Error("news: needs a feed URL starting with https://")
    return { mode: "news", url }
  }
  return { mode: "coins", coins: parseCoins(m ? m[2] : p) }
}

// ---------- data ----------
function parseCoins(list) {
  const coins = (list || DEFAULT_COINS).split(",").map(s => s.trim().toLowerCase()).filter(Boolean)
    .map(e => { const [sym, id] = e.split(":"); return { sym, id: id || KNOWN[sym] || null } })
    .filter(c => /^[a-z0-9.\-]{1,20}$/.test(c.sym))
  if (!coins.length) throw new Error("no coins in the parameter")
  return coins
}

function readCache(key) {
  try { return fm.fileExists(key) ? JSON.parse(fm.readString(key)) : null } catch (e) { return null }
}

async function fetchMarkets(coins) {
  const ids = coins.filter(c => c.id).map(c => c.id)
  const syms = coins.filter(c => !c.id).map(c => c.sym)
  const base = "https://api.coingecko.com/api/v3/coins/markets?sparkline=true&price_change_percentage=24h&vs_currency=" + CURRENCY
  const rows = []
  for (const url of [ids.length && `${base}&ids=${ids.join(",")}`,
                     syms.length && `${base}&symbols=${syms.join(",")}&include_tokens=top`].filter(Boolean)) {
    const req = new Request(url)
    req.timeoutInterval = 15
    if (API_KEY) req.headers = { "x-cg-demo-api-key": API_KEY }
    const data = await req.loadJSON()
    if (!Array.isArray(data)) throw new Error(data?.status?.error_message || "bad response")
    rows.push(...data)
  }
  return coins.map(c => {
    const r = rows.find(r => r && (c.id ? r.id === c.id : String(r.symbol).toLowerCase() === c.sym))
    return r && typeof r.current_price === "number" && {
      sym: c.sym.toUpperCase(), id: r.id, name: r.name, image: r.image,
      price: r.current_price, change: r.price_change_percentage_24h,
      spark: (r.sparkline_in_7d?.price || []).filter(p => p != null),
    }
  }).filter(Boolean)
}

async function getData(coins) {
  const key = fm.joinPath(dir, "data-" + coins.map(c => c.id || c.sym).join("_") + ".json")
  try {
    const assets = await fetchMarkets(coins)
    if (!assets.length) throw new Error("no data")
    fm.writeString(key, JSON.stringify({ at: Date.now(), assets }))
    return { assets, at: new Date(), stale: false }
  } catch (e) {
    const c = readCache(key)
    if (c && c.assets?.length) return { assets: c.assets, at: new Date(c.at), stale: true }
    throw e
  }
}

// ---------- news (RSS 2.0 and Atom) ----------
function hashKey(s) { let h = 0; for (const ch of s) h = (h * 31 + ch.codePointAt(0)) | 0; return (h >>> 0).toString(16) }
const escXml = t => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
const cleanText = t => String(t || "").replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim()

function parseFeed(xml, url) {
  // CDATA sections become plain escaped text so every parser sees them as characters
  xml = xml.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, (_, t) => escXml(t))
  const items = []
  let feedTitle = "", item = null
  const texts = []  // one text buffer per open element; nested tags (<b> in a title) flow up
  const parser = new XMLParser(xml)
  parser.didStartElement = (name, attrs) => {
    const n = name.toLowerCase().replace(/^.*:/, "")
    texts.push("")
    if (n === "item" || n === "entry") item = { title: "", link: "", date: "" }
    if (item && n === "link" && attrs && attrs.href && (!attrs.rel || attrs.rel === "alternate")) item.link = attrs.href
  }
  parser.foundCharacters = s => { if (texts.length) texts[texts.length - 1] += s }
  parser.didEndElement = name => {
    const n = name.toLowerCase().replace(/^.*:/, "")
    const raw = texts.pop() || ""
    if (texts.length && !["item", "entry", "channel", "feed"].includes(n)) texts[texts.length - 1] += raw
    const t = cleanText(raw)
    if (item) {
      if (n === "title") item.title = t
      else if (n === "link" && t) item.link = t
      else if (["pubdate", "published", "updated", "date"].includes(n) && !item.date) item.date = t
      else if (n === "item" || n === "entry") { if (item.title) items.push(item); item = null }
    } else if (n === "title" && !feedTitle) feedTitle = t
  }
  if (!parser.parse()) throw new Error("couldn't read the feed")
  if (!items.length) throw new Error("the feed has no headlines")
  for (const it of items) { const d = new Date(it.date); it.at = isNaN(d) ? null : d.getTime() }
  const dated = items.filter(i => i.at)
  const top = dated.length ? dated.sort((a, b) => b.at - a.at)[0] : items[0]
  let host = ""
  try { host = url.match(/^https?:\/\/([^/:?#]+)/i)[1].replace(/^(www|feeds?|rss)\./, "") } catch (e) {}
  return { title: top.title, link: /^https?:\/\//i.test(top.link) ? top.link : url,
           at: top.at, source: (feedTitle || host).slice(0, 40) }
}

async function getNews(url) {
  const key = fm.joinPath(dir, "news-" + hashKey(url) + ".json")
  try {
    const req = new Request(url)
    req.timeoutInterval = 15
    const story = parseFeed(await req.loadString(), url)
    fm.writeString(key, JSON.stringify({ fetched: Date.now(), story }))
    return { story, stale: false }
  } catch (e) {
    const c = readCache(key)
    if (c && c.story?.title) return { story: c.story, stale: true }
    throw e
  }
}

function ago(ms) {
  if (!ms) return ""
  const m = Math.max(0, Math.round((Date.now() - ms) / 60000))
  return m < 1 ? "now" : m < 60 ? `${m}m` : m < 48 * 60 ? `${Math.round(m / 60)}h` : `${Math.round(m / 1440)}d`
}

async function icon(asset) {
  const path = fm.joinPath(dir, "icon-" + asset.id + ".png")
  if (fm.fileExists(path)) return fm.readImage(path)
  try {
    const img = await new Request(asset.image).loadImage()
    fm.writeImage(path, img)
    return img
  } catch (e) { return null }
}

// ---------- formatting (same rules as the LED panel) ----------
const SUB = "₀₁₂₃₄₅₆₇₈₉"
function fmtPrice(p) {
  const s = SIGNS[CURRENCY] ?? ""
  if (p == null) return "--"
  if (Math.round(p * 100) / 100 >= 1000) return s + Math.round(p).toLocaleString("en-US")
  if (p >= 1) return s + p.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  if (p <= 0) return s + "0.00"
  const decimals = Math.max(2, -Math.floor(Math.log10(p)) - 1 + 4)   // 4 significant digits
  const frac = p.toFixed(decimals).split(".")[1]
  const zeros = frac.length - frac.replace(/^0+/, "").length
  if (zeros >= 4) {  // 0.00001123 -> 0.0₄1123
    const sub = String(zeros).split("").map(d => SUB[d]).join("")
    return `${s}0.0${sub}${frac.replace(/^0+/, "").slice(0, 4)}`
  }
  return `${s}0.${frac}`
}
const trendHex = x => x == null || Math.abs(x) < 0.05 ? HEX.flat : x > 0 ? HEX.up : HEX.down
const trend = x => new Color(trendHex(x))
const fmtChange = x => x == null ? "--" : `${x >= 0 ? "▲" : "▼"}${Math.abs(x).toFixed(Math.abs(x) < 100 ? 1 : 0)}%`
const weekChange = a => a.spark.length > 1 ? (a.spark.at(-1) - a.spark[0]) / a.spark[0] * 100 : a.change

// ---------- drawing ----------
// Area chart: bright line with a soft fill, colored by the 7-day direction.
function chartImage(points, w, h, top, hex, opts = {}) {
  const color = new Color(hex)
  const ctx = new DrawContext()
  ctx.size = new Size(w, h)
  ctx.opaque = !!opts.background
  ctx.respectScreenScale = true
  if (opts.background) { ctx.setFillColor(BG); ctx.fillRect(new Rect(0, 0, w, h)) }
  if (points.length > 1) {
    const lo = Math.min(...points), hi = Math.max(...points), span = hi - lo || 1
    const right = w - 5  // keep the "now" dot fully inside
    const xy = points.map((v, i) => new Point(i / (points.length - 1) * right, h - 1 - (v - lo) / span * (h - 1 - top)))
    // fill in bands so it fades toward the bottom
    for (const [alpha, cut] of [[0.10, 0], [0.12, 0.35], [0.14, 0.65]]) {
      const area = new Path()
      area.move(new Point(0, h))
      xy.forEach(p => area.addLine(new Point(p.x, p.y + (h - p.y) * cut)))
      area.addLine(new Point(w, h)); area.closeSubpath()
      ctx.addPath(area); ctx.setFillColor(new Color(hex, alpha)); ctx.fillPath()
    }
    const line = new Path()
    line.move(xy[0]); xy.slice(1).forEach(p => line.addLine(p))
    ctx.addPath(line); ctx.setStrokeColor(color); ctx.setLineWidth(opts.lineWidth || 2); ctx.strokePath()
    const last = xy.at(-1)
    ctx.setFillColor(WHITE); ctx.fillEllipse(new Rect(last.x - 3, last.y - 3, 6, 6))
  }
  if (opts.tag) {
    ctx.setFont(Font.semiboldSystemFont(10)); ctx.setTextColor(new Color("#FFFFFF", 0.45))
    ctx.setTextAlignedRight(); ctx.drawTextInRect("7D", new Rect(w - 36, top - 10, 24, 14))
  }
  return ctx.getImage()
}

function shadowed(t) { t.shadowColor = Color.black(); t.shadowRadius = 3; t.shadowOffset = new Point(0, 1); return t }

async function header(stack, a, size) {
  const row = stack.addStack(); row.centerAlignContent()
  const img = await icon(a)
  if (img) { const i = row.addImage(img); i.imageSize = new Size(size, size); i.cornerRadius = size / 2; row.addSpacer(6) }
  const sym = shadowed(row.addText(a.sym)); sym.font = Font.heavySystemFont(size * 0.8); sym.textColor = WHITE; sym.lineLimit = 1
  row.addSpacer()
  const ch = shadowed(row.addText(fmtChange(a.change))); ch.font = Font.boldSystemFont(size * 0.6)
  ch.textColor = trend(a.change); ch.lineLimit = 1
}

async function bigWidget(a, stale, family) {
  const w = new ListWidget()
  const W = family === "medium" ? 338 : 158, H = 158
  w.backgroundImage = chartImage(a.spark, W, H, 44, trendHex(weekChange(a)), { background: true, tag: true, lineWidth: 2.5 })
  w.setPadding(12, 12, 10, 12)
  await header(w, a, 22)
  w.addSpacer()
  const p = shadowed(w.addText(fmtPrice(a.price)))
  p.font = Font.boldRoundedSystemFont(family === "medium" ? 34 : 26)
  p.textColor = WHITE; p.minimumScaleFactor = 0.5; p.lineLimit = 1
  if (stale) { const s = w.addText("offline · last update"); s.font = Font.systemFont(9); s.textColor = FLAT }
  w.url = "https://www.coingecko.com/en/coins/" + a.id
  return w
}

async function listWidget(assets, stale) {  // medium widget with 2-3 coins
  const w = new ListWidget()
  w.backgroundColor = BG
  w.setPadding(10, 14, 10, 14)
  for (const [i, a] of assets.slice(0, 3).entries()) {
    if (i) w.addSpacer()
    const row = w.addStack(); row.centerAlignContent()
    const img = await icon(a)
    if (img) { const im = row.addImage(img); im.imageSize = new Size(24, 24); im.cornerRadius = 12 }
    row.addSpacer(8)
    const col = row.addStack(); col.layoutVertically(); col.size = new Size(64, 0)
    const s = col.addText(a.sym); s.font = Font.heavySystemFont(15); s.textColor = WHITE; s.lineLimit = 1
    const c = col.addText(fmtChange(a.change)); c.font = Font.boldSystemFont(11); c.textColor = trend(a.change)
    row.addSpacer(6)
    const spark = row.addImage(chartImage(a.spark, 90, 30, 2, trendHex(weekChange(a)), { lineWidth: 1.5 }))
    spark.imageSize = new Size(90, 30)
    row.addSpacer()
    const p = row.addText(fmtPrice(a.price)); p.font = Font.boldRoundedSystemFont(18); p.textColor = WHITE
    p.minimumScaleFactor = 0.6; p.lineLimit = 1
  }
  if (stale) { w.addSpacer(2); const t = w.addText("offline · showing last prices"); t.font = Font.systemFont(9); t.textColor = FLAT }
  return w
}

// ---------- lock screen (monochrome: ▲/▼ carry the direction) ----------
const arrow = x => x == null ? "" : x >= 0 ? "▲" : "▼"

function nextIndex(name, n) {  // inline widget: a different coin each refresh
  const path = fm.joinPath(dir, "rotate-" + hashKey(name) + ".txt")
  let i = 0
  try { if (fm.fileExists(path)) i = (parseInt(fm.readString(path), 10) + 1) || 0 } catch (e) {}
  i = i % n
  fm.writeString(path, String(i))
  return i
}

function coinUrl(a) { return "https://www.coingecko.com/en/coins/" + a.id }

function inlineCoin(assets, stale, key) {
  const a = assets[nextIndex(key, assets.length)]
  const w = new ListWidget()
  w.addText(`${stale ? STALE + " " : ""}${a.sym} ${fmtPrice(a.price)} ${fmtChange(a.change)}`)
  w.url = coinUrl(a)
  return w
}

// Ring gauge drawn as an image: faint full track, bright arc for |24h change|,
// clockwise from 12 o'clock when up, counter-clockwise when down.
function gaugeImage(a, stale) {
  const S = 120, c = S / 2, r = 52, lw = 9
  const ctx = new DrawContext()
  ctx.size = new Size(S, S); ctx.opaque = false; ctx.respectScreenScale = true
  const arc = (from, to, color) => {
    const steps = Math.max(2, Math.ceil(Math.abs(to - from) / 0.05))
    const p = new Path()
    for (let i = 0; i <= steps; i++) {
      const t = from + (to - from) * i / steps
      const pt = new Point(c + r * Math.sin(t), c - r * Math.cos(t))
      i ? p.addLine(pt) : p.move(pt)
    }
    ctx.addPath(p); ctx.setStrokeColor(color); ctx.setLineWidth(lw); ctx.strokePath()
  }
  arc(0, 2 * Math.PI, new Color("#FFFFFF", 0.25))
  if (a.change != null) {
    const frac = Math.min(1, Math.abs(a.change) / GAUGE_FULL_SCALE)
    if (frac > 0.005) arc(0, (a.change >= 0 ? 1 : -1) * frac * 2 * Math.PI, WHITE)
  }
  ctx.setTextColor(WHITE); ctx.setTextAlignedCenter()
  const sym = a.sym.slice(0, 5)
  ctx.setFont(Font.heavySystemFont(sym.length > 3 ? 20 : 26))
  ctx.drawTextInRect(sym, new Rect(14, 34, S - 28, 32))
  ctx.setFont(Font.boldSystemFont(17))
  const ch = a.change == null ? "--" : `${arrow(a.change)}${Math.abs(a.change).toFixed(Math.abs(a.change) < 10 ? 1 : 0)}%`
  ctx.drawTextInRect(ch, new Rect(14, 66, S - 28, 22))
  if (stale) { ctx.setFont(Font.boldSystemFont(15)); ctx.drawTextInRect(STALE, new Rect(c - 12, 16, 24, 18)) }
  return ctx.getImage()
}

function circularCoin(a, stale) {
  const w = new ListWidget()
  w.addAccessoryWidgetBackground = true
  w.setPadding(0, 0, 0, 0)
  const img = w.addImage(gaugeImage(a, stale)); img.centerAlignImage()
  w.url = coinUrl(a)
  return w
}

function rectangularCoins(assets, stale, at) {
  const w = new ListWidget()
  if (assets.length === 1) {  // one coin: symbol, change, price, sparkline
    const a = assets[0]
    const top = w.addStack()
    const s = top.addText((stale ? STALE + " " : "") + a.sym); s.font = Font.heavySystemFont(14)
    top.addSpacer()
    const c = top.addText(fmtChange(a.change)); c.font = Font.boldSystemFont(12)
    const p = w.addText(fmtPrice(a.price)); p.font = Font.boldRoundedSystemFont(20); p.minimumScaleFactor = 0.6
    const img = w.addImage(chartImage(a.spark, 150, 18, 1, HEX.white, { lineWidth: 1.5 }))
    img.imageSize = new Size(150, 18)
    w.url = coinUrl(a)
    return w
  }
  for (const a of assets.slice(0, 2)) {  // two coins, one per row
    const row = w.addStack(); row.centerAlignContent()
    const s = row.addText(a.sym); s.font = Font.heavySystemFont(14); s.lineLimit = 1
    row.addSpacer(4)
    const p = row.addText(fmtPrice(a.price)); p.font = Font.boldRoundedSystemFont(14)
    p.minimumScaleFactor = 0.6; p.lineLimit = 1
    row.addSpacer()
    const c = row.addText(fmtChange(a.change)); c.font = Font.boldSystemFont(12); c.lineLimit = 1
  }
  if (stale) {
    const t = w.addText(`${STALE} offline · ${ago(at && at.getTime()) || "earlier"}`)
    t.font = Font.systemFont(10)
  }
  w.url = coinUrl(assets[0])
  return w
}

// ---------- news widgets ----------
function newsCaption(story, stale) {
  return [stale ? STALE : "", story.source, ago(story.at)].filter(Boolean).join(" · ")
}

function newsWidget(story, stale, family) {
  const w = new ListWidget()
  w.url = story.link
  if (family === "accessoryInline") {
    w.addText(`${stale ? STALE + " " : ""}${story.title}`)
    return w
  }
  if (family === "accessoryCircular") {
    w.addAccessoryWidgetBackground = true
    const initials = story.source.split(/[\s.\-]+/).filter(Boolean).map(x => x[0]).join("").slice(0, 3).toUpperCase()
    const a = w.addText(initials || "NEWS"); a.font = Font.heavySystemFont(16); a.centerAlignText()
    const b = w.addText((stale ? STALE + " " : "") + (ago(story.at) || "—")); b.font = Font.boldSystemFont(12); b.centerAlignText()
    return w
  }
  if (family === "accessoryRectangular") {
    const cap = w.addText(newsCaption(story, stale)); cap.font = Font.semiboldSystemFont(11); cap.lineLimit = 1
    const t = w.addText(story.title); t.font = Font.boldSystemFont(13); t.lineLimit = 2; t.minimumScaleFactor = 0.8
    return w
  }
  // home screen: dark card in the same style as the price widgets
  w.backgroundColor = BG
  w.setPadding(12, 14, 12, 14)
  const cap = w.addText(newsCaption(story, stale).toUpperCase()); cap.font = Font.semiboldSystemFont(10)
  cap.textColor = stale ? FLAT : new Color(HEX.up); cap.lineLimit = 1
  w.addSpacer(6)
  const t = w.addText(story.title); t.font = Font.boldSystemFont(family === "small" ? 15 : 17)
  t.textColor = WHITE; t.lineLimit = family === "small" ? 5 : 4; t.minimumScaleFactor = 0.75
  w.addSpacer()
  return w
}

function errorWidget(msg, family) {
  const w = new ListWidget()
  if (family && family.startsWith("accessory")) {
    w.addText(family === "accessoryCircular" ? STALE : `${STALE} ${msg}`)
    return w
  }
  w.backgroundColor = BG
  const t = w.addText("Crypto Ticker"); t.font = Font.heavySystemFont(14); t.textColor = WHITE
  w.addSpacer(4)
  const m = w.addText(msg); m.font = Font.systemFont(11); m.textColor = FLAT
  return w
}

// ---------- main ----------
const family = config.widgetFamily || "small"
let widget
try {
  const spec = parseParam(args.widgetParameter)
  if (spec.mode === "news") {
    const { story, stale } = await getNews(spec.url)
    widget = newsWidget(story, stale, family)
  } else {
    const { assets, stale, at } = await getData(spec.coins)
    if (family === "accessoryInline") widget = inlineCoin(assets, stale, args.widgetParameter || DEFAULT_COINS)
    else if (family === "accessoryCircular") widget = circularCoin(assets[0], stale)
    else if (family === "accessoryRectangular") widget = rectangularCoins(assets, stale, at)
    else if (family !== "small" && assets.length > 1) widget = await listWidget(assets, stale)
    else widget = await bigWidget(assets[0], stale, family)
  }
} catch (e) {
  const msg = String(e.message || e)
  widget = errorWidget(/feed|news|coins in/.test(msg) ? msg : "No data. " + msg, family)
}
widget.refreshAfterDate = new Date(Date.now() + REFRESH_MINUTES * 60 * 1000)

if (config.runsInWidget) {
  Script.setWidget(widget)
} else {  // tap ▶ in Scriptable to preview
  const fam = { accessoryCircular: "presentAccessoryCircular", accessoryRectangular: "presentAccessoryRectangular",
                accessoryInline: "presentAccessoryInline", medium: "presentMedium", large: "presentLarge" }[family]
  await (fam && widget[fam] ? widget[fam]() : widget.presentSmall())
}
Script.complete()
