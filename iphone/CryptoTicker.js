// Crypto Ticker widget for Scriptable (iPhone home screen, lock screen, StandBy)
// Companion to the Raspberry Pi LED ticker: same chart look, same price rules.
//
// SETUP
// 1. Install "Scriptable" from the App Store, tap +, paste this whole file, name it "Crypto Ticker".
// 2. Add a Scriptable widget (small for StandBy), long-press it -> Edit Widget:
//      Script:    Crypto Ticker
//      Parameter: the coin, e.g.  btc   or  ada   or  night:midnight-3
//    A medium widget can take up to 3 coins:  btc,ada,snek
// 3. For StandBy: add small Scriptable widgets to StandBy's widget stacks, one coin each.
//    Stack several and swipe to switch coins.
//
// "sym:coingecko-id" pins an exact coin (the id is in the coin's CoinGecko URL).
// Plain tickers below are already pinned for convenience.

// ----- settings you can change -----
const DEFAULT_COINS = "btc"
const CURRENCY = "usd"
const API_KEY = ""               // optional free CoinGecko Demo key
const REFRESH_MINUTES = 15       // a request; iOS decides the real timing
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
const fm = FileManager.local()
const dir = fm.joinPath(fm.cachesDirectory(), "crypto-ticker")
if (!fm.fileExists(dir)) fm.createDirectory(dir, true)

// ---------- data ----------
function parseCoins(param) {
  return (param || DEFAULT_COINS).split(",").map(s => s.trim().toLowerCase()).filter(Boolean)
    .map(e => { const [sym, id] = e.split(":"); return { sym, id: id || KNOWN[sym] || null } })
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
    const r = rows.find(r => c.id ? r.id === c.id : r.symbol.toLowerCase() === c.sym)
    return r && {
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
    if (fm.fileExists(key)) {
      const c = JSON.parse(fm.readString(key))
      return { assets: c.assets, at: new Date(c.at), stale: true }
    }
    throw e
  }
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

function lockWidget(a, family) {  // lock screen
  const w = new ListWidget()
  if (family === "accessoryInline") {
    w.addText(`${a.sym} ${fmtPrice(a.price)} ${fmtChange(a.change)}`)
    return w
  }
  const top = w.addStack()
  const s = top.addText(a.sym); s.font = Font.heavySystemFont(14)
  top.addSpacer()
  const c = top.addText(fmtChange(a.change)); c.font = Font.boldSystemFont(12)
  const p = w.addText(fmtPrice(a.price)); p.font = Font.boldRoundedSystemFont(20); p.minimumScaleFactor = 0.6
  const img = w.addImage(chartImage(a.spark, 150, 18, 1, HEX.white, { lineWidth: 1.5 }))
  img.imageSize = new Size(150, 18)
  return w
}

function errorWidget(msg) {
  const w = new ListWidget(); w.backgroundColor = BG
  const t = w.addText("Crypto Ticker"); t.font = Font.heavySystemFont(14); t.textColor = WHITE
  w.addSpacer(4)
  const m = w.addText(msg); m.font = Font.systemFont(11); m.textColor = FLAT
  return w
}

// ---------- main ----------
const family = config.widgetFamily || "small"
const coins = parseCoins(args.widgetParameter)
let widget
try {
  const { assets, stale } = await getData(coins)
  if (family.startsWith("accessory")) widget = lockWidget(assets[0], family)
  else if (family !== "small" && assets.length > 1) widget = await listWidget(assets, stale)
  else widget = await bigWidget(assets[0], stale, family)
} catch (e) {
  widget = errorWidget("No price data. " + (e.message || e))
}
widget.refreshAfterDate = new Date(Date.now() + REFRESH_MINUTES * 60 * 1000)

if (config.runsInWidget) {
  Script.setWidget(widget)
} else {
  await widget.presentSmall()   // tap ▶ in Scriptable to preview
}
Script.complete()
