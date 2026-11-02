// Runs iphone/CryptoTicker.js against the Scriptable stand-in:  node --test tests/widget/
import assert from "node:assert/strict"
import fs from "node:fs"
import { test } from "node:test"
import { allNodes, allTexts, makeFS, runWidget } from "./scriptable_stub.mjs"

const markets = JSON.parse(fs.readFileSync(new URL("../fixtures/markets.json", import.meta.url)))
const extra = [{ id: "solana", symbol: "sol", name: "Solana", current_price: 212.44, price_change_percentage_24h: 25,
                 sparkline_in_7d: { price: [200, 212.44] } }]
const ROWS = [...markets, ...extra]

const coingecko = url => {
  const u = new URL(url)
  const ids = (u.searchParams.get("ids") || "").split(",")
  const syms = (u.searchParams.get("symbols") || "").split(",")
  return { json: ROWS.filter(r => ids.includes(r.id) || syms.includes(r.symbol)) }
}
const offline = () => { throw new Error("The Internet connection appears to be offline.") }
const hoursAgo = h => new Date(Date.now() - h * 3600e3).toUTCString()

const RSS = `<?xml version="1.0"?><rss version="2.0"><channel><title>Coin Daily</title>
<item><title><![CDATA[Older story & stuff]]></title><link>https://news.example/old</link><pubDate>${hoursAgo(5)}</pubDate></item>
<item><title>Bitcoin &amp; Ether rally <b>again</b></title><link>https://news.example/new</link><pubDate>${hoursAgo(2)}</pubDate></item>
</channel></rss>`
const ATOM = `<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Wire</title>
<entry><title>Atom headline</title><link rel="alternate" href="https://atom.example/a1"/><updated>${new Date(Date.now() - 30 * 60e3).toISOString()}</updated></entry></feed>`

const run = (family, param, route = coingecko, fsys = makeFS()) => runWidget({ family, param, route, fsys })
const texts = w => allTexts(w).join(" | ")
const drawn = w => allNodes(w).filter(n => n.kind === "image" && n.img?.ops).flatMap(n => n.img.ops)

test("legacy plain parameter still works (medium list)", async () => {
  const w = await run("medium", "btc,ada")
  assert.match(texts(w), /BTC .*ADA/)
  assert.match(texts(w), /\$109,235/)
})

test("empty parameter uses DEFAULT_COINS", async () => {
  assert.match(texts(await run("small", "")), /BTC/)
})

test("coins: prefix, small home widget unchanged layout", async () => {
  const w = await run("small", "coins:eth")
  assert.match(texts(w), /ETH \| ▼2\.9% \| \$4,012$/)
  assert.equal(w.url, "https://www.coingecko.com/en/coins/ethereum")
})

test("inline rotates one coin per refresh", async () => {
  const fsys = makeFS(), seen = []
  for (let i = 0; i < 4; i++) seen.push(texts(await run("accessoryInline", "coins:btc,eth,sol", coingecko, fsys)).split(" ")[0])
  assert.deepEqual(seen, ["BTC", "ETH", "SOL", "BTC"])
})

test("circular: symbol, arrow and ring gauge", async () => {
  const w = await run("accessoryCircular", "coins:sol")
  const ops = drawn(w)
  assert.ok(ops.some(o => o.op === "text" && o.text === "SOL"))
  assert.ok(ops.some(o => o.op === "text" && o.text === "▲25%"))
  const strokes = ops.filter(o => o.op === "stroke")
  assert.equal(strokes.length, 2)  // track + value arc (25% > full scale, so a full ring)
  assert.equal(w.addAccessoryWidgetBackground, true)
})

test("circular: down move draws counter-clockwise arc", async () => {
  const ops = drawn(await run("accessoryCircular", "coins:eth"))
  assert.ok(ops.some(o => o.op === "text" && o.text === "▼2.9%"))
  const arc = ops.filter(o => o.op === "path")[1].points
  assert.ok(arc.at(-1).x < arc[0].x)  // went left from 12 o'clock
})

test("rectangular: two coins, one per row", async () => {
  const t = texts(await run("accessoryRectangular", "coins:btc,eth,ada"))
  assert.match(t, /BTC \| \$109,235 \| ▲1\.8% \| ETH \| \$4,012 \| ▼2\.9%/)
  assert.doesNotMatch(t, /ADA/)
})

test("rectangular: one coin with sparkline", async () => {
  const w = await run("accessoryRectangular", "coins:btc")
  assert.match(texts(w), /BTC \| ▲1\.8% \| \$109,235/)
})

test("lock-screen widgets are monochrome (no text colors)", async () => {
  for (const fam of ["accessoryInline", "accessoryCircular", "accessoryRectangular"])
    for (const p of ["coins:btc,eth", "news:https://news.example/rss"]) {
      const w = await run(fam, p, u => u.includes("coingecko") ? coingecko(u) : { text: RSS })
      assert.ok(allNodes(w).every(n => n.textColor === undefined), `${fam} ${p}`)
    }
})

test("failed fetch shows cached prices marked stale", async () => {
  const fsys = makeFS()
  await run("accessoryRectangular", "coins:btc,eth", coingecko, fsys)
  const t = texts(await run("accessoryRectangular", "coins:btc,eth", offline, fsys))
  assert.match(t, /BTC .*\$109,235/)
  assert.match(t, /⚠︎ offline · now/)
  assert.match(texts(await run("accessoryInline", "coins:btc,eth", offline, fsys)), /^⚠︎ /)
  assert.ok(drawn(await run("accessoryCircular", "coins:btc,eth", offline, fsys)).some(o => o.text === "⚠︎"))
  assert.match(texts(await run("small", "coins:btc,eth", offline, fsys)), /offline/)
})

test("rate limit with no cache shows an error, not a crash", async () => {
  const w = await run("small", "coins:btc", () => ({ json: { status: { error_code: 429, error_message: "Rate limit" } } }))
  assert.match(texts(w), /No data\. Rate limit/)
})

test("news: newest RSS headline, source and age; tap opens article", async () => {
  const w = await run("accessoryRectangular", "news:https://news.example/rss", () => ({ text: RSS }))
  assert.equal(texts(w), "Coin Daily · 2h | Bitcoin & Ether rally again")
  assert.equal(w.url, "https://news.example/new")
})

test("news: Atom feed, inline and circular", async () => {
  const route = () => ({ text: ATOM })
  assert.equal(texts(await run("accessoryInline", "news:https://atom.example/feed", route)), "Atom headline")
  const c = await run("accessoryCircular", "news:https://atom.example/feed", route)
  assert.equal(texts(c), "AW | 30m")
  assert.equal(c.url, "https://atom.example/a1")
})

test("news: CDATA titles and home-screen card", async () => {
  const one = RSS.replace(/<item><title>Bitcoin[\s\S]*?<\/item>/, "")
  const w = await run("medium", "news:https://news.example/rss", () => ({ text: one }))
  assert.match(texts(w), /COIN DAILY · 5H \| Older story & stuff/)
})

test("news: offline shows cached headline marked stale", async () => {
  const fsys = makeFS()
  await run("accessoryRectangular", "news:https://news.example/rss", () => ({ text: RSS }), fsys)
  const w = await run("accessoryRectangular", "news:https://news.example/rss", offline, fsys)
  assert.match(texts(w), /^⚠︎ · Coin Daily · 2h \| Bitcoin/)
})

test("news: bad URL or broken feed gives a clear error", async () => {
  assert.match(texts(await run("accessoryRectangular", "news:ftp://x")), /feed URL/)
  assert.match(texts(await run("small", "news:https://x.example/rss", () => ({ text: "<html><body>" }))), /feed/)
  assert.match(texts(await run("small", "news:https://x.example/rss", () => ({ text: "<rss><channel></channel></rss>" }))), /no headlines/)
})
