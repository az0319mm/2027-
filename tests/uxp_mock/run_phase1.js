// node run_phase1.js <scenario> <tmpdir>  → 결과 JSON 출력
"use strict";
const path = require("path");
const fsn = require("fs");
const { makeEnv, solid } = require("./mock_ps.js");
const { runPhase1 } = require("../../uxp-plugin/lib/phase1.js");

(async () => {
  const [scenario, tmp] = process.argv.slice(2);
  const opts = {};
  if (scenario === "corrupt") opts.corruptSave = (s) => s.replace('"opacity":60', '"opacity":59');
  const env = makeEnv(opts);
  const { Layer } = env;
  const origPath = path.join(tmp, "작품.psd");
  fsn.writeFileSync(origPath, "ORIGINAL-PSD-BYTES");
  const locked = scenario === "locked";
  const doc = new env.Document({ name: "작품.psd", path: origPath, layers: [
    new Layer({ name: "모티브 그룹", kind: "group", bounds: { left: 0, top: 0, right: 64, bottom: 48 }, blendMode: "passThrough", layers: [
      new Layer({ name: "꽃", kind: "pixel", bounds: { left: 10, top: 5, right: 30, bottom: 25 }, opacity: 60, blendMode: "multiply",
                  pixels: solid(20, 20, [200, 50, 80, 255]), allLocked: locked }),
      new Layer({ name: "잎", kind: "pixel", bounds: { left: 30, top: 20, right: 50, bottom: 40 }, visible: false,
                  pixels: solid(20, 20, [40, 160, 60, 255]), allLocked: locked }),
    ] }),
    new Layer({ name: "글자", kind: "text", bounds: { left: 2, top: 2, right: 20, bottom: 8 } }),
    new Layer({ name: "배경", kind: "pixel", isBackgroundLayer: true, components: 3, bounds: { left: 0, top: 0, right: 64, bottom: 48 },
                pixels: new Uint8Array(64 * 48 * 3).fill(250), pixelsLocked: locked }),
  ] });
  env.app.activeDocument = doc;
  const logs = [];
  const rep = await runPhase1(env, (m) => logs.push(m));
  const origAfter = fsn.readFileSync(origPath, "utf8");
  console.log(JSON.stringify({ passed: rep.passed, checks: rep.checks, test: rep.test, savedPath: rep.savedPath,
    originalUntouched: origAfter === "ORIGINAL-PSD-BYTES", openDocs: env.docs.map((d) => d.name),
    origLayerPixelsUnchanged: Buffer.from(doc.layers[0].layers[0].pixels).equals(Buffer.from(solid(20, 20, [200, 50, 80, 255]))) }));
})().catch((e) => { console.error(e); process.exit(1); });
