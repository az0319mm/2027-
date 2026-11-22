// node run_js_engine.js <jobdir> → JS 엔진 결과를 JSON 으로 (Python process_job 과 비교용)
"use strict";
const fs = require("fs");
const path = require("path");
const P = require("../../uxp-plugin/engine/plan.js");
const { crc32 } = require("../../uxp-plugin/lib/layers.js");

const dir = process.argv[2];
const req = JSON.parse(fs.readFileSync(path.join(dir, "request.json"), "utf8"));
const layers = new Map();
for (const L of req.layers) if (L.file) layers.set(Number(L.order), new Uint8Array(fs.readFileSync(path.join(dir, L.file))));
const composite = req.composite && fs.existsSync(path.join(dir, req.composite.file)) ? new Uint8Array(fs.readFileSync(path.join(dir, req.composite.file))) : null;
const t0 = Date.now();
const r = P.runCleanup(req, { layers, composite }, req.kind || "cleanup");
console.log(JSON.stringify({
  ms: Date.now() - t0,
  summary: r.summary,
  skipped: r.skipped_layers.map((x) => x.path),
  patches: r.patches.map((p) => ({ order: p.order, path: p.path, left: p.left, top: p.top, width: p.width, height: p.height,
    components: p.components, changed_pixels: p.changed_pixels, expected_hash: p.expected_hash, expected_bounds: p.expected_bounds,
    data_crc: crc32(p.data) })),
}));
