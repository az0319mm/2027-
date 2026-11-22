// 디스크를 실제로 쓰는 모의 Photoshop (플러그인 ↔ Python 엔진 통합 테스트용)
// Photoshop 처럼 getPixels 는 '픽셀이 있는 영역(알파>0)'으로 잘라서 돌려준다고 가정.
// 레이어 pixels 는 캔버스 크기 버퍼(W*H*components)로 보관.
"use strict";
const fsn = require("fs");
const pathm = require("path");

let NEXT_DOC = 500, NEXT_LAYER = 1000;
class Layer {
  constructor(o) {
    Object.assign(this, { visible: true, opacity: 100, fillOpacity: 100, blendMode: "normal", isClippingMask: false,
      isBackgroundLayer: false, allLocked: false, pixelsLocked: false, positionLocked: false,
      transparentPixelsLocked: false, layers: null, components: 4 }, o);
    if (this.id == null) this.id = NEXT_LAYER++;
  }
}
const clone = (ls) => (ls || []).map((l) => new Layer(Object.assign({}, l, {
  pixels: l.pixels ? new Uint8Array(l.pixels) : undefined, layers: l.layers ? clone(l.layers) : null })));
function find(ls, id) {
  for (const l of ls || []) { if (l.id === id) return l; const f = find(l.layers, id); if (f) return f; }
  return null;
}

function trimmed(l, W, H) {
  const c = l.components;
  let x0 = W, y0 = H, x1 = -1, y1 = -1;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const on = c === 4 ? l.pixels[(y * W + x) * 4 + 3] > 0 : true;
    if (on) { if (x < x0) x0 = x; if (y < y0) y0 = y; if (x > x1) x1 = x; if (y > y1) y1 = y; }
  }
  if (x1 < 0) return null;
  const w = x1 - x0 + 1, h = y1 - y0 + 1, out = new Uint8Array(w * h * c);
  for (let y = 0; y < h; y++) out.set(l.pixels.subarray(((y0 + y) * W + x0) * c, ((y0 + y) * W + x0 + w) * c), y * w * c);
  return { data: out, bounds: { left: x0, top: y0, right: x1 + 1, bottom: y1 + 1 }, w, h };
}

function makeEnv(opts) {
  opts = opts || {};
  const docs = [];
  const app = {
    activeDocument: null, get documents() { return docs; },
    async open(entry) { const d = load(entry.nativePath); app.activeDocument = d; return d; },
  };
  class Document {
    constructor(o) {
      Object.assign(this, { resolution: 300, mode: "RGBColorMode", bitsPerChannel: "bitDepth8", colorProfileType: "custom",
        colorProfileName: "sRGB IEC61966-2.1", saved: true }, o);
      this.id = NEXT_DOC++;
      const self = this;
      this.saveAs = {
        psd: async (entry) => {
          const ser = (ls) => ls.map((l) => Object.assign({}, l, {
            pixels: l.pixels ? Buffer.from(l.pixels).toString("base64") : undefined, layers: l.layers ? ser(l.layers) : null }));
          let s = JSON.stringify({ name: pathm.basename(entry.nativePath), width: self.width, height: self.height,
            resolution: self.resolution, mode: self.mode, bitsPerChannel: self.bitsPerChannel,
            colorProfileName: self.colorProfileName, layers: ser(self.layers) });
          if (opts.corruptSave) s = opts.corruptSave(s);
          fsn.writeFileSync(entry.nativePath, s);
          self.path = entry.nativePath;
        },
      };
      docs.push(this);
    }
    async duplicate(name) {
      const d = new Document({ name, path: "", width: this.width, height: this.height, layers: clone(this.layers), saved: false });
      app.activeDocument = d;
      return d;
    }
    async createLayer(kind, o) {
      if (kind !== "pixel") throw new Error("kind");
      const l = new Layer(Object.assign({ kind: "pixel", bounds: { left: 0, top: 0, right: 0, bottom: 0 },
                                          pixels: new Uint8Array(this.width * this.height * 4) }, o || {}));
      this.layers.unshift(l);
      return l;
    }
    closeWithoutSaving() {
      const i = docs.indexOf(this);
      if (i >= 0) docs.splice(i, 1);
      app.activeDocument = docs[docs.length - 1] || null;
    }
  }
  docs.add = async (o) => {
    need();
    const d = new Document({ name: o.name, path: "", width: o.width, height: o.height, resolution: o.resolution, layers: [], saved: false });
    app.activeDocument = d;
    return d;
  };
  function load(p) {
    const j = JSON.parse(fsn.readFileSync(p, "utf8"));
    const des = (ls) => ls.map((l) => new Layer(Object.assign({}, l, {
      pixels: l.pixels ? new Uint8Array(Buffer.from(l.pixels, "base64")) : undefined, layers: l.layers ? des(l.layers) : null })));
    return new Document(Object.assign({}, j, { path: p, layers: des(j.layers) }));
  }
  const core = {
    modal: false,
    async executeAsModal(fn, o) {
      if (!o || !o.commandName) throw new Error("commandName");
      core.modal = true;
      try { return await fn({}); } finally { core.modal = false; }
    },
  };
  const need = () => { if (!core.modal) throw new Error("executeAsModal 밖"); };
  const imgData = (w, h, c, d) => ({ width: w, height: h, components: c, colorSpace: "RGB",
    colorProfile: "sRGB IEC61966-2.1", hasAlpha: c === 4,
    async getData() { return new Uint8Array(d); }, async dispose() {} });
  const imaging = {
    async getPixels(o) {
      need();
      const doc = docs.find((d) => d.id === o.documentID);
      if (o.layerID == null) {
        const out = new Uint8Array(doc.width * doc.height * 3).fill(255);
        return { imageData: imgData(doc.width, doc.height, 3, out),
                 sourceBounds: { left: 0, top: 0, right: doc.width, bottom: doc.height }, level: 0 };
      }
      const l = find(doc.layers, o.layerID);
      const t = trimmed(l, doc.width, doc.height);
      if (!t) throw new Error("no pixels");
      return { imageData: imgData(t.w, t.h, l.components, t.data), sourceBounds: t.bounds, level: 0 };
    },
    async putPixels(o) {
      need();
      const doc = docs.find((d) => d.id === o.documentID);
      const l = find(doc.layers, o.layerID);
      if (l.allLocked || l.pixelsLocked) throw new Error("locked");
      const W = doc.width, c = l.components, src = await o.imageData.getData();
      if (o.imageData.components !== c) throw new Error("component mismatch");
      for (let y = 0; y < o.imageData.height; y++)
        for (let x = 0; x < o.imageData.width; x++)
          for (let k = 0; k < c; k++)
            l.pixels[((o.targetBounds.top + y) * W + o.targetBounds.left + x) * c + k] = src[(y * o.imageData.width + x) * c + k];
    },
    async getLayerMask() { throw new Error("no mask"); },
    async createImageDataFromBuffer(buf, o) { return imgData(o.width, o.height, o.components, buf); },
  };
  const formats = { binary: "binary", utf8: "utf8" };
  const types = { folder: "folder", file: "file" };
  const entry = (p) => ({
    nativePath: p, name: pathm.basename(p),
    async read(o) {
      const b = fsn.readFileSync(p);
      return o && o.format === "utf8" ? b.toString("utf8") : b.buffer.slice(b.byteOffset, b.byteOffset + b.length);
    },
    async write(data) { fsn.writeFileSync(p, typeof data === "string" ? data : Buffer.from(data)); },
    async getEntries() { return fsn.readdirSync(p).map((n) => entry(pathm.join(p, n))); },
    async delete() { fsn.rmSync(p, { recursive: true, force: true }); },
    async moveTo(folder, o) { fsn.renameSync(p, pathm.join(folder.nativePath, o.newName)); },
  });
  const fs = {
    async getEntryWithUrl(url) {
      const p = url.replace(/^file:/, "");
      if (!fsn.existsSync(p)) throw new Error("not found " + p);
      return entry(p);
    },
    async createEntryWithUrl(url, o) {
      const p = url.replace(/^file:/, "");
      if (o && o.type === "folder") { fsn.mkdirSync(p, { recursive: true }); return entry(p); }
      if (fsn.existsSync(p) && !(o && o.overwrite)) throw new Error("exists");
      return entry(p);
    },
  };
  return { app, core, imaging, fs, formats, types, Document, Layer, docs };
}

module.exports = { makeEnv, Layer };
