// A minimal stand-in for the Scriptable app API, enough to run iphone/CryptoTicker.js in Node.
// It records the widget tree so tests can check what would be on screen. It is NOT Scriptable:
// layout, fonts, and iOS refresh timing are not simulated.
import fs from "node:fs"

export class Color {
  constructor(hex, alpha = 1) { this.hex = hex; this.alpha = alpha }
  static white() { return new Color("#FFFFFF") }
  static black() { return new Color("#000000") }
}
export const Font = new Proxy({}, { get: (_, name) => (size) => ({ font: name, size }) })
export class Size { constructor(w, h) { this.width = w; this.height = h } }
export class Point { constructor(x, y) { this.x = x; this.y = y } }
export class Rect { constructor(x, y, w, h) { Object.assign(this, { x, y, w, h }) } }
export class Path {
  constructor() { this.points = [] }
  move(p) { this.points.push(p) } addLine(p) { this.points.push(p) } closeSubpath() {}
}
export class DrawContext {
  // Records every drawing call with its style, so tests can inspect it and
  // tools/widget_screenshots.mjs can turn it into an SVG.
  constructor() { this.ops = []; this.lw = 1; this.align = "left" }
  addPath(p) { this.path = p; this.ops.push({ op: "path", n: p.points.length, points: p.points }) }
  strokePath() { this.ops.push({ op: "stroke", color: this.stroke, width: this.lw, points: this.path?.points || [] }) }
  fillPath() { this.ops.push({ op: "fill", color: this.fill, points: this.path?.points || [] }) }
  setStrokeColor(c) { this.stroke = c } setFillColor(c) { this.fill = c } setLineWidth(w) { this.lw = w }
  setFont(f) { this.font = f } setTextColor(c) { this.textColor = c }
  setTextAlignedCenter() { this.align = "center" } setTextAlignedRight() { this.align = "right" }
  setTextAlignedLeft() { this.align = "left" }
  fillRect(r) { this.ops.push({ op: "rect", rect: r, color: this.fill }) }
  fillEllipse(r) { this.ops.push({ op: "ellipse", rect: r, color: this.fill }) }
  drawTextInRect(t, r) {
    this.ops.push({ op: "text", text: t, rect: r, font: this.font, color: this.textColor, align: this.align })
  }
  getImage() { return { image: true, ops: this.ops, size: this.size } }
}
class Element {
  constructor(kind, extra = {}) { this.kind = kind; Object.assign(this, extra); this.children = [] }
  addText(t) { const e = new Element("text", { text: String(t) }); this.children.push(e); return e }
  addImage(img) { const e = new Element("image", { img }); this.children.push(e); return e }
  addStack() { const e = new Element("stack"); this.children.push(e); return e }
  addSpacer(n) { this.children.push(new Element("spacer", { n })) }
  layoutVertically() { this.vertical = true } layoutHorizontally() { this.vertical = false }
  centerAlignContent() { this.centered = true } centerAlignImage() { this.align = "center" }
  centerAlignText() { this.align = "center" }
  setPadding(t, l, b, r) { this.padding = [t, l, b, r] }
}
export class ListWidget extends Element {
  constructor() { super("widget") }
  async presentSmall() {} async presentMedium() {}
}
export function allTexts(el) { return [el.text, ...el.children.flatMap(allTexts)].filter(t => t != null) }
export function allNodes(el) { return [el, ...el.children.flatMap(allNodes)] }

export function makeFS() {
  const files = new Map()
  return {
    files,
    local: () => ({
      cachesDirectory: () => "/cache", joinPath: (a, b) => `${a}/${b}`,
      fileExists: p => files.has(p) || [...files.keys()].some(k => k.startsWith(p + "/")),
      createDirectory: () => {}, readString: p => files.get(p), writeString: (p, s) => files.set(p, s),
      readImage: p => files.get(p), writeImage: (p, i) => files.set(p, i),
    }),
  }
}

// XMLParser: callback-style like Scriptable's (didStartElement, foundCharacters, didEndElement)
export class XMLParser {
  constructor(xml) { this.xml = xml }
  parse() {
    const decode = s => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
      .replace(/&#39;|&apos;/g, "'").replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(+n)).replace(/&amp;/g, "&")
    const re = /<(\/?)([A-Za-z_][\w:.-]*)([^>]*?)(\/?)>|([^<]+)|<!--[\s\S]*?-->|<\?[\s\S]*?\?>|<!DOCTYPE[^>]*>/g
    let m, stack = []
    while ((m = re.exec(this.xml))) {
      if (m[5] !== undefined) { if (stack.length) this.foundCharacters?.(decode(m[5])); continue }
      if (!m[2]) continue
      if (m[1]) {
        if (stack.pop() !== m[2]) { this.parseErrorOccurred?.("mismatched tag"); return false }
        this.didEndElement?.(m[2]); continue
      }
      const attrs = {}
      for (const a of m[3].matchAll(/([\w:-]+)\s*=\s*"([^"]*)"/g)) attrs[a[1]] = decode(a[2])
      this.didStartElement?.(m[2], attrs)
      if (m[4]) this.didEndElement?.(m[2]); else stack.push(m[2])
    }
    return stack.length === 0
  }
}

const SCRIPT = fs.readFileSync(new URL("../../iphone/CryptoTicker.js", import.meta.url), "utf8")

// Run the widget once. route(url) returns {json} | {text} | throws.
export async function runWidget({ family, param, route, fsys }) {
  const out = {}
  class Request {
    constructor(url) { this.url = url; this.headers = {} }
    async loadJSON() { return (await route(this.url)).json }
    async loadString() { return (await route(this.url)).text }
    async loadImage() {
      const r = await route(this.url)
      if (!r || !r.image) throw new Error("no image")
      return r.image
    }
  }
  const globals = {
    Color, Font, Size, Point, Rect, Path, DrawContext, ListWidget, XMLParser, Request,
    FileManager: fsys, config: { widgetFamily: family, runsInWidget: true },
    args: { widgetParameter: param }, Script: { setWidget: w => { out.widget = w }, complete: () => {} },
  }
  const AsyncFunction = (async () => {}).constructor
  await new AsyncFunction(...Object.keys(globals), SCRIPT)(...Object.values(globals))
  return out.widget
}
