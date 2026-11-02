// Approximate renders of the iPhone widget (iphone/CryptoTicker.js) for the docs.
//   node tools/widget_screenshots.mjs        (needs Playwright + Chromium)
//
// The real script runs against the Scriptable stand-in from the tests
// (tests/widget/scriptable_stub.mjs) with sample data; the recorded widget tree is
// turned into HTML/SVG and screenshotted. These are APPROXIMATIONS of what iOS draws:
// fonts are not SF Pro, spacing and text shrinking are simplified, coin icons are
// placeholders, and the lock-screen tint is simulated.
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { createRequire } from "node:module"
import { allNodes, makeFS, runWidget } from "../tests/widget/scriptable_stub.mjs"

const require = createRequire(import.meta.url)
const { chromium } = require("playwright")
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const OUT = path.join(ROOT, "docs", "screenshots")

// ---------- sample data ----------
const MARKETS = JSON.parse(fs.readFileSync(path.join(ROOT, "tests", "fixtures", "markets.json")))
const hoursAgo = h => new Date(Date.now() - h * 3600e3).toUTCString()
const RSS = `<?xml version="1.0"?><rss version="2.0"><channel><title>Coin Daily</title>
<item><title>Bitcoin holds above $109,000 as ETF inflows pick up for a third week</title>
<link>https://news.example.com/btc-etf</link><pubDate>${hoursAgo(2)}</pubDate></item>
</channel></rss>`
const ICON_COLORS = { bitcoin: "#F7931A", ethereum: "#627EEA", cardano: "#0033AD" }

function route(url) {
  if (url.includes("coin-images")) {
    const id = Object.keys(ICON_COLORS).find(k => url.includes(k.slice(0, 5))) || "bitcoin"
    return { image: { icon: true, color: ICON_COLORS[id] || "#888", letter: id[0].toUpperCase() } }
  }
  if (url.includes("api.coingecko.com")) {
    const u = new URL(url)
    const ids = (u.searchParams.get("ids") || "").split(","), syms = (u.searchParams.get("symbols") || "").split(",")
    return { json: MARKETS.filter(r => ids.includes(r.id) || syms.includes(r.symbol)) }
  }
  return { text: RSS }
}

// ---------- widget tree -> HTML ----------
const enc = t => encodeURIComponent(t).replace(/'/g, "%27")
const esc = t => String(t).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]))
const css = c => c ? `rgba(${parseInt(c.hex.slice(1, 3), 16)},${parseInt(c.hex.slice(3, 5), 16)},${parseInt(c.hex.slice(5, 7), 16)},${c.alpha ?? 1})` : null
const WEIGHT = { heavySystemFont: 850, boldSystemFont: 700, boldRoundedSystemFont: 700, semiboldSystemFont: 600,
                 mediumSystemFont: 500, systemFont: 400, semiboldRoundedSystemFont: 600 }
const fontCss = f => f ? `font-size:${f.size}px;font-weight:${WEIGHT[f.font] || 400};` +
  (f.font.includes("Rounded") ? "font-family:'SF Pro Rounded',ui-rounded,'Nunito','Inter',system-ui,sans-serif;" : "") : ""

function svg(img, mono) {
  if (img.icon) return `data:image/svg+xml;utf8,${enc(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><circle cx="20" cy="20" r="20" fill="${img.color}"/>` +
    `<text x="20" y="27" font-family="Inter,system-ui,sans-serif" font-weight="800" font-size="20" fill="#fff" text-anchor="middle">${img.letter}</text></svg>`)}`
  const { width: W, height: H } = img.size
  const parts = []
  const pts = p => p.map(q => `${q.x.toFixed(2)},${q.y.toFixed(2)}`).join(" ")
  const col = c => mono && c ? `rgba(255,255,255,${c.alpha ?? 1})` : css(c)
  for (const o of img.ops) {
    if (o.op === "rect") parts.push(`<rect x="${o.rect.x}" y="${o.rect.y}" width="${o.rect.w}" height="${o.rect.h}" fill="${col(o.color)}"/>`)
    if (o.op === "ellipse") parts.push(`<ellipse cx="${o.rect.x + o.rect.w / 2}" cy="${o.rect.y + o.rect.h / 2}" rx="${o.rect.w / 2}" ry="${o.rect.h / 2}" fill="${col(o.color)}"/>`)
    if (o.op === "fill") parts.push(`<polygon points="${pts(o.points)}" fill="${col(o.color)}"/>`)
    if (o.op === "stroke") parts.push(`<polyline points="${pts(o.points)}" fill="none" stroke="${col(o.color)}" stroke-width="${o.width}" stroke-linecap="round" stroke-linejoin="round"/>`)
    if (o.op === "text") {
      const anchor = { center: "middle", right: "end" }[o.align] || "start"
      const x = o.align === "center" ? o.rect.x + o.rect.w / 2 : o.align === "right" ? o.rect.x + o.rect.w : o.rect.x
      parts.push(`<text x="${x}" y="${o.rect.y + o.font.size * 0.95}" font-family="Inter,system-ui,sans-serif" font-size="${o.font.size}" font-weight="${WEIGHT[o.font.font] || 400}" fill="${col(o.color) || "#fff"}" text-anchor="${anchor}">${esc(o.text)}</text>`)
    }
  }
  return `data:image/svg+xml;utf8,${enc(`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${parts.join("")}</svg>`)}`
}

function node(el, axis, mono) {
  if (el.kind === "spacer") return el.n == null ? `<div style="flex:1 1 0"></div>`
    : `<div style="flex:0 0 ${el.n}px;${axis === "row" ? "width" : "height"}:${el.n}px"></div>`
  if (el.kind === "text") {
    const clamp = el.lineLimit === 1 ? "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
      : el.lineLimit ? `display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:${el.lineLimit};overflow:hidden;` : ""
    const color = mono ? "rgba(255,255,255,.92)" : css(el.textColor) || "#fff"
    const shadow = el.shadowRadius ? "text-shadow:0 1px 3px #000;" : ""
    return `<div style="${fontCss(el.font)}color:${color};${clamp}${shadow}${el.align === "center" ? "text-align:center;align-self:stretch;" : ""}min-width:0;line-height:1.18">${esc(el.text)}</div>`
  }
  if (el.kind === "image") {
    const sz = el.imageSize || (el.img.size ? { width: el.img.size.width, height: el.img.size.height } : { width: 24, height: 24 })
    return `<img src="${svg(el.img, mono)}" style="width:${sz.width}px;height:${sz.height}px;flex:none;border-radius:${el.cornerRadius || 0}px;${el.align === "center" ? "align-self:center;" : ""}">`
  }
  // stack
  const dir = el.vertical ? "column" : "row"
  const size = el.size ? `${el.size.width ? `width:${el.size.width}px;flex:none;` : ""}${el.size.height ? `height:${el.size.height}px;` : ""}` : ""
  return `<div style="display:flex;flex-direction:${dir};align-items:${el.centered ? "center" : "flex-start"};${dir === "row" ? "align-self:stretch;" : ""}${size}min-width:0">${el.children.map(c => node(c, dir, mono)).join("")}</div>`
}

function widgetHTML(w, { width, height, radius = 22, mono = false, accessoryBg = false, defaultPad = 16 }) {
  const pad = w.padding || [defaultPad, defaultPad, defaultPad, defaultPad]
  const bg = w.backgroundImage ? `background:url('${svg(w.backgroundImage, false)}') center/cover;`
    : w.backgroundColor ? `background:${css(w.backgroundColor)};` : ""
  const acc = accessoryBg && w.addAccessoryWidgetBackground ? "background:rgba(255,255,255,.16);" : ""
  return `<div style="width:${width}px;height:${height}px;border-radius:${radius}px;${bg}${acc}box-sizing:border-box;padding:${pad.map(p => p + "px").join(" ")};display:flex;flex-direction:column;justify-content:center;align-items:stretch;overflow:hidden;font-family:Inter,system-ui,sans-serif;color:#fff">${w.children.map(c => node(c, "column", mono)).join("")}</div>`
}

// ---------- frames ----------
const PAGE = body => `<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Nunito:wght@700;800&display=block" rel="stylesheet">
<style>body{margin:0;background:#000;font-family:Inter,system-ui,sans-serif}
.caption{position:absolute;left:0;right:0;bottom:10px;text-align:center;color:rgba(255,255,255,.55);font-size:12px}
.frame{position:relative;overflow:hidden}</style></head><body>${body}</body></html>`
const WALL = "background:radial-gradient(120% 80% at 20% 10%,#3b4a8f 0%,#1d2347 45%,#0b0d1c 100%)"
const HOME_WALL = "background:linear-gradient(160deg,#2c3e66 0%,#5b3f7a 55%,#1d1b2e 100%)"
const NOTE = '<div class="caption">Approximate render (Scriptable stand-in, sample data)</div>'

const frames = {
  home: (small, medium, label) => `<div class="frame" style="width:393px;height:460px;${HOME_WALL};padding:28px 24px;box-sizing:border-box">
    <div style="display:flex;gap:22px;align-items:flex-start">${small}<div style="color:#fff;font-size:13px;opacity:.8;padding-top:6px">Small<br><span style="opacity:.7">${label}</span></div></div>
    <div style="height:22px"></div>${medium}<div style="color:#fff;font-size:13px;opacity:.8;margin-top:8px">Medium</div>${NOTE}</div>`,
  standby: (left, right) => `<div class="frame" style="width:852px;height:393px;background:#000;display:flex;align-items:center;justify-content:center;gap:28px">
    <div style="transform:scale(2.15);transform-origin:center;margin:0 85px">${left}</div>
    <div style="transform:scale(2.15);transform-origin:center;margin:0 85px">${right}</div>${NOTE}</div>`,
  lock: (inner, which) => `<div class="frame" style="width:393px;height:430px;${WALL};color:#fff">
    ${which === "inline" ? inner : `<div style="text-align:center;padding-top:62px;font-size:17px;font-weight:600;opacity:.92">Mon 2 Nov</div>`}
    <div style="text-align:center;font-size:96px;font-weight:700;letter-spacing:-2px;line-height:1;margin-top:${which === "inline" ? 4 : 2}px;opacity:.95">9:41</div>
    <div style="display:flex;justify-content:center;gap:14px;margin-top:18px">${which === "inline" ? "" : inner}</div>${NOTE}</div>`,
}

// ---------- run the widget for each variant ----------
async function build(family, param, fsys = makeFS()) {
  return runWidget({ family, param, route, fsys })
}

async function variants() {
  const out = {}
  const home = async (p, label) => frames.home(
    widgetHTML(await build("small", p), { width: 158, height: 158 }),
    widgetHTML(await build("medium", p === "coins:btc" ? "coins:btc,eth,ada" : p), { width: 338, height: 158 }), label)
  const inline = async p => `<div style="text-align:center;padding-top:62px;font-size:16px;font-weight:600;opacity:.92;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-left:40px;padding-right:40px">${esc(allNodes(await build("accessoryInline", p)).filter(n => n.kind === "text").map(n => n.text).join(" "))}</div>`
  const circ = async p => widgetHTML(await build("accessoryCircular", p), { width: 72, height: 72, radius: 36, mono: true, accessoryBg: true, defaultPad: 0 })
  const rect = async p => widgetHTML(await build("accessoryRectangular", p), { width: 160, height: 72, radius: 12, mono: true, defaultPad: 0 })
  const NEWS = "news:https://news.example.com/feed.xml"

  out["widget-coins-home"] = await home("coins:btc", "coins:btc")
  out["widget-coins-standby"] = frames.standby(widgetHTML(await build("small", "coins:btc"), { width: 158, height: 158 }),
                                               widgetHTML(await build("small", "coins:eth"), { width: 158, height: 158 }))
  out["widget-coins-lock-inline"] = frames.lock(await inline("coins:btc,eth,sol"), "inline")
  out["widget-coins-lock-circular"] = frames.lock(await circ("coins:btc"), "circular")
  out["widget-coins-lock-rectangular"] = frames.lock((await rect("coins:btc")) + (await rect("coins:btc,eth")), "rect")
  // stale: fetch once, then offline
  const fsys = makeFS()
  await runWidget({ family: "accessoryRectangular", param: "coins:btc,eth", route, fsys })
  const offline = () => { throw new Error("offline") }
  const staleRect = await runWidget({ family: "accessoryRectangular", param: "coins:btc,eth", route: offline, fsys })
  const staleCirc = await runWidget({ family: "accessoryCircular", param: "coins:btc,eth", route: offline, fsys })
  out["widget-coins-lock-stale"] = frames.lock(
    widgetHTML(staleCirc, { width: 72, height: 72, radius: 36, mono: true, accessoryBg: true, defaultPad: 0 }) +
    widgetHTML(staleRect, { width: 160, height: 72, radius: 12, mono: true, defaultPad: 0 }), "rect")

  out["widget-news-home"] = await home(NEWS, "news:&lt;feed URL&gt;")
  out["widget-news-standby"] = frames.standby(widgetHTML(await build("small", NEWS), { width: 158, height: 158 }),
                                              widgetHTML(await build("small", "coins:btc"), { width: 158, height: 158 }))
  out["widget-news-lock-inline"] = frames.lock(await inline(NEWS), "inline")
  out["widget-news-lock-circular"] = frames.lock(await circ(NEWS), "circular")
  out["widget-news-lock-rectangular"] = frames.lock(await rect(NEWS), "rect")
  return out
}

const browser = await chromium.launch()
try {
  const page = await browser.newPage({ deviceScaleFactor: 2 })
  const shots = await variants()
  for (const [name, html] of Object.entries(shots)) {
    await page.setContent(PAGE(html), { waitUntil: "networkidle" }).catch(() => page.setContent(PAGE(html)))
    await page.waitForTimeout(150)
    await page.locator(".frame").screenshot({ path: path.join(OUT, `${name}.png`) })
    console.log(`wrote docs/screenshots/${name}.png`)
  }
  // one sheet with every variant, labeled
  const label = n => n.replace("widget-", "").replace(/-/g, " ").replace("lock", "lock screen:")
  const cell = n => `<div style="display:flex;flex-direction:column;gap:6px;align-items:flex-start">
    <div style="color:#cfd3da;font:600 15px Inter,system-ui">${label(n)}</div>
    <img src="${n}.png" style="height:${n.includes("standby") ? 230 : 330}px;border-radius:10px"></div>`
  const order = Object.keys(shots)
  const row = mode => `<div style="display:flex;flex-wrap:wrap;gap:20px;align-items:flex-end">${order.filter(n => n.includes(mode)).map(cell).join("")}</div>`
  const sheet = `<!doctype html><html><head><meta charset="utf-8"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=block" rel="stylesheet"></head>
    <body style="margin:0;background:#18191d"><div class="frame" style="padding:24px;display:flex;flex-direction:column;gap:26px;width:2300px">
    <div style="color:#eef1f5;font:700 26px Inter,system-ui">iPhone widget (Scriptable): every variant — approximate renders, sample data</div>
    <div style="color:#9aa0a6;font:600 18px Inter,system-ui">coins:</div>${row("coins")}
    <div style="color:#9aa0a6;font:600 18px Inter,system-ui">news:</div>${row("news")}</div></body></html>`
  const sheetPath = path.join(OUT, "widget-sheet.html")
  fs.writeFileSync(sheetPath, sheet)
  try {
    await page.goto("file://" + sheetPath, { waitUntil: "networkidle" })
    await page.locator(".frame").screenshot({ path: path.join(OUT, "widget-contact-sheet.png") })
  } finally { fs.unlinkSync(sheetPath) }
  console.log("wrote docs/screenshots/widget-contact-sheet.png")
} finally { await browser.close() }
