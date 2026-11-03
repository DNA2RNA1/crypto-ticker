// iPhone-size screenshots of the phone settings page, served by tools/demo_server.py.
//   node tools/settings_screenshots.mjs        (needs Playwright + Chromium)
// Writes docs/screenshots/settings-page-*.png
import { spawn } from "node:child_process"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { createRequire } from "node:module"

const require = createRequire(import.meta.url)
const { chromium } = require("playwright")
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const OUT = path.join(ROOT, "docs", "screenshots")
const PY = process.env.PYTHON || "python3"
const IPHONE = { viewport: { width: 393, height: 852 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true }

async function serve(state, port) {
  const p = spawn(PY, [path.join(ROOT, "tools", "demo_server.py"), "--port", String(port), "--state", state],
                  { stdio: ["ignore", "pipe", "inherit"] })
  await new Promise((ok, bad) => { p.stdout.once("data", ok); p.once("exit", bad) })
  return p
}

async function page(browser, port) {
  const ctx = await browser.newContext(IPHONE)
  const pg = await ctx.newPage()
  const errors = []
  pg.on("pageerror", e => errors.push(e.message))
  await pg.goto(`http://127.0.0.1:${port}/`)
  await pg.fill("#pin", "2468")
  await pg.keyboard.press("Enter")
  await pg.waitForSelector("#software .row")
  await pg.waitForTimeout(400)
  return { pg, errors }
}

const browser = await chromium.launch()
const shots = []
try {
  let srv = await serve("ok", 8095)
  try {
    const { pg, errors } = await page(browser, 8095)
    await pg.screenshot({ path: path.join(OUT, "settings-page-iphone-full.png"), fullPage: true })
    await pg.screenshot({ path: path.join(OUT, "settings-page-iphone-top.png") })
    await pg.evaluate(() => [...document.querySelectorAll("h2")].find(h => h.textContent == "Display").scrollIntoView())
    await pg.screenshot({ path: path.join(OUT, "settings-page-iphone-list-view.png") })
    const toHeading = t => pg.evaluate(t => [...document.querySelectorAll("h2")].find(h => h.textContent == t).scrollIntoView(), t)
    await toHeading("Screens")
    await pg.screenshot({ path: path.join(OUT, "settings-page-iphone-screens.png") })
    // the indices options: source, layout (and the $ / point switch in the compact layout)
    await pg.evaluate(() => document.querySelector("#isrc").closest(".row").scrollIntoView())
    await pg.screenshot({ path: path.join(OUT, "settings-page-iphone-indices.png") })
    await toHeading("News headlines")
    await pg.screenshot({ path: path.join(OUT, "settings-page-iphone-news.png") })
    await toHeading("YouTube videos")
    await pg.screenshot({ path: path.join(OUT, "settings-page-iphone-videos.png") })
    await toHeading("Sports")
    await pg.fill('[data-teamq="nfl"]', "chiefs")
    await pg.waitForSelector("#teams-nfl [data-ti]")
    await pg.screenshot({ path: path.join(OUT, "settings-page-iphone-sports.png") })
    await pg.fill('[data-teamq="nfl"]', "")
    await pg.evaluate(() => { document.querySelector("#teams-nfl").innerHTML = "" })
    await pg.evaluate(() => document.querySelector("#status").previousElementSibling.scrollIntoView())
    await pg.screenshot({ path: path.join(OUT, "settings-page-iphone-status-update.png") })
    shots.push("settings-page-iphone-full.png", "settings-page-iphone-top.png",
               "settings-page-iphone-list-view.png", "settings-page-iphone-screens.png",
               "settings-page-iphone-indices.png", "settings-page-iphone-news.png", "settings-page-iphone-videos.png", "settings-page-iphone-sports.png",
               "settings-page-iphone-status-update.png")
    if (errors.length) throw new Error("page errors: " + errors.join("; "))
  } finally { srv.kill() }
  srv = await serve("needs_key", 8094)
  try {
    const { pg, errors } = await page(browser, 8094)
    await pg.evaluate(() => document.querySelector("#status").previousElementSibling.scrollIntoView())
    await pg.screenshot({ path: path.join(OUT, "settings-page-iphone-deploy-key.png") })
    shots.push("settings-page-iphone-deploy-key.png")
    if (errors.length) throw new Error("page errors: " + errors.join("; "))
  } finally { srv.kill() }
} finally { await browser.close() }
for (const s of shots) console.log("wrote docs/screenshots/" + s)
