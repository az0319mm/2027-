// Photoshop UXP API 의 일부를 흉내 내는 모의 객체 (Node 테스트 전용).
// 실제 Photoshop 동작을 보장하지 않으며, 플러그인 로직(순서·좌표·비교)을 검증하기 위한 것.
"use strict";
const fsn = require("fs");

let NEXT_DOC = 100, NEXT_LAYER = 1;

class Layer {
  constructor(o) {
    Object.assign(this, { visible: true, opacity: 100, fillOpacity: 100, blendMode: "normal", isClippingMask: false,
      isBackgroundLayer: false, allLocked: false, pixelsLocked: false, positionLocked: false,
      transparentPixelsLocked: false, layers: null }, o);
    if (this.id == null) this.id = NEXT_LAYER++;
  }
}

function cloneLayers(ls) {
  return (ls || []).map((l) => new Layer(Object.assign({}, l, {
    pixels: l.pixels ? new Uint8Array(l.pixels) : undefined,
    layers: l.layers ? cloneLayers(l.layers) : null,
  })));
}

function findLayer(layers, id) {
  for (const l of layers || []) {
    if (l.id === id) return l;
    const f = findLayer(l.layers, id);
    if (f) return f;
  }
  return null;
}

function makeEnv(opts) {
  opts = opts || {};
  const docs = [];
  const files = {};                       // 모의 파일시스템 경로 → 내용(Buffer)
  class Document {
    constructor(o) {
      Object.assign(this, { width: 64, height: 48, resolution: 300, mode: "RGBColorMode", bitsPerChannel: "bitDepth8",
        colorProfileType: "custom", colorProfileName: "sRGB IEC61966-2.1", saved: true }, o);
      this.id = NEXT_DOC++;
      const self = this;
      this.saveAs = {
        psd: async (entry, options, asCopy) => {
          const snap = JSON.stringify(self.serialize());
          let out = snap;
          if (opts.corruptSave) out = opts.corruptSave(snap);
          files[entry.nativePath] = Buffer.from(out);
          self.path = entry.nativePath;
        },
      };
      docs.push(this);
    }
    serialize() {
      const ser = (ls) => ls.map((l) => Object.assign({}, l, {
        pixels: l.pixels ? Buffer.from(l.pixels).toString("base64") : undefined,
        layers: l.layers ? ser(l.layers) : null,
      }));
      return { width: this.width, height: this.height, resolution: this.resolution, mode: this.mode,
        bitsPerChannel: this.bitsPerChannel, colorProfileType: this.colorProfileType,
        colorProfileName: this.colorProfileName, name: this.name, layers: ser(this.layers) };
    }
    async duplicate(name) {
      const d = new Document({ name: name || this.name + " copy", path: "", width: this.width, height: this.height,
        resolution: this.resolution, mode: this.mode, bitsPerChannel: this.bitsPerChannel,
        colorProfileType: this.colorProfileType, colorProfileName: this.colorProfileName, saved: false,
        layers: cloneLayers(this.layers) });
      app.activeDocument = d;
      return d;
    }
    closeWithoutSaving() {
      const i = docs.indexOf(this);
      if (i >= 0) docs.splice(i, 1);
      app.activeDocument = docs[docs.length - 1] || null;
    }
  }
  function loadDoc(path) {
    const j = JSON.parse(files[path].toString());
    const des = (ls) => ls.map((l) => new Layer(Object.assign({}, l, {
      pixels: l.pixels ? new Uint8Array(Buffer.from(l.pixels, "base64")) : undefined,
      layers: l.layers ? des(l.layers) : null,
    })));
    return new Document(Object.assign({}, j, { path, layers: des(j.layers) }));
  }
  const app = {
    activeDocument: null,
    get documents() { return docs; },
    async open(entry) { const d = loadDoc(entry.nativePath); app.activeDocument = d; return d; },
  };
  const core = {
    modal: false,
    async executeAsModal(fn, o) {
      if (!o || !o.commandName) throw new Error("commandName required");
      core.modal = true;
      try { return await fn({}); } finally { core.modal = false; }
    },
  };
  const needModal = () => { if (!core.modal) throw new Error("executeAsModal 밖에서 호출됨"); };
  const imageData = (w, h, c, data) => ({
    width: w, height: h, components: c, colorSpace: "RGB", colorProfile: "sRGB IEC61966-2.1",
    hasAlpha: c === 4, componentSize: 8, chunky: true,
    async getData() { return new Uint8Array(data); },
    async dispose() {},
  });
  const imaging = {
    async getPixels(o) {
      needModal();
      const doc = docs.find((d) => d.id === o.documentID);
      const l = findLayer(doc.layers, o.layerID);
      if (!l || !l.pixels) throw new Error("no pixels");
      const b = l.bounds, c = l.components || 4;
      return { imageData: imageData(b.right - b.left, b.bottom - b.top, c, l.pixels), sourceBounds: Object.assign({}, b), level: 0 };
    },
    async putPixels(o) {
      needModal();
      const doc = docs.find((d) => d.id === o.documentID);
      const l = findLayer(doc.layers, o.layerID);
      if (l.allLocked || l.pixelsLocked) throw new Error("layer locked");
      const b = l.bounds, c = l.components || 4, w = b.right - b.left;
      const src = await o.imageData.getData();
      for (let y = 0; y < o.imageData.height; y++)
        for (let x = 0; x < o.imageData.width; x++)
          for (let k = 0; k < c; k++)
            l.pixels[((o.targetBounds.top - b.top + y) * w + (o.targetBounds.left - b.left + x)) * c + k] =
              src[(y * o.imageData.width + x) * c + k];
    },
    async createImageDataFromBuffer(buf, o) { return imageData(o.width, o.height, o.components, buf); },
  };
  const formats = { binary: "binary", utf8: "utf8" };
  const fs = {
    async getEntryWithUrl(url) {
      const p = url.replace(/^file:/, "");
      return { nativePath: p, async read() {
        if (files[p]) return files[p].buffer.slice(files[p].byteOffset, files[p].byteOffset + files[p].length);
        const b = fsn.readFileSync(p); return b.buffer.slice(b.byteOffset, b.byteOffset + b.length);
      } };
    },
    async createEntryWithUrl(url, o) { return { nativePath: url.replace(/^file:/, "") }; },
  };
  return { app, core, imaging, fs, formats, Document, Layer, files, docs };
}

function solid(w, h, rgba) {
  const a = new Uint8Array(w * h * 4);
  for (let i = 0; i < w * h; i++) a.set(rgba, i * 4);
  return a;
}

module.exports = { makeEnv, solid, Layer };
