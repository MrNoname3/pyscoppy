"use strict";
const VREF = 3.3, MIDV = VREF / 2;
const TIMEBASES = [100000000, 10000000, 1000000, 300000, 100000, 30000, 10000];
const VDIV_LADDER = [2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02];
const HDIV = 10, VDIVS = 8;
const TRIG_NAMES = ["NONE", "AUTO", "NORM"];
// scope sample-rate menu (Hz). 0 = Auto.
const SAMPLE_RATES = [0, 1, 2, 5, 10, 20, 50, 100, 200, 500,
  1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000, 1000000, 2000000];
// device max-rate ceiling (RP2040 max sample rate). code -> [label, maxHz]
const MAX_SR = [[0, "500 kS/s", 500000], [2, "1.3 MS/s", 1300000],
  [4, "2 MS/s", 2000000], [5, "2.5 MS/s", 2500000]];
const maxSrHz = () => (MAX_SR.find(m => m[0] === (state.max_sr || 0)) || MAX_SR[0])[2];
const MAX_SR_LABEL = () => (MAX_SR.find(m => m[0] === (state.max_sr || 0)) || MAX_SR[0])[1];

let state = {}, frame = null, lastBy = "—";
let view = "yt", selCh = 0, hpos = 0;
// roll mode: free-running right-to-left scroll, trigger ignored (for slow timebases)
let rollMode = false;
// measurement cursors (fractions of the screen) + which handle is being dragged
let cursorsOn = false, cur = { x1: 0.35, x2: 0.65, y1: 0.40, y2: 0.60 };
// FFT settings (like the app): window, vertical scale, span zoom (1x = full 0..fs/2)
let fftWindow = "Hann", fftScale = "Linear", fftZoom = 1;
const WIN_FNS = {
  Hann: (i, N) => 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (N - 1)),
  Hamming: (i, N) => 0.54 - 0.46 * Math.cos(2 * Math.PI * i / (N - 1)),
  Blackman: (i, N) => 0.42 - 0.5 * Math.cos(2 * Math.PI * i / (N - 1)) + 0.08 * Math.cos(4 * Math.PI * i / (N - 1)),
};
// math channel (drawn in orange on CH1's scale). 0 = off.
let mathMode = 0;
const MATH_NAMES = ["MATH", "CH1−CH2", "CH1+CH2", "CH2−CH1", "−CH1", "−CH2"];
function mathCompute(va, vb, mode) {
  switch (mode) { case 1: return va - vb; case 2: return va + vb; case 3: return vb - va; case 4: return -va; case 5: return -vb; default: return 0; }
}
const mathNeedsA = (mode) => mode !== 5;     // every mode except −CH2 uses CH1
const mathNeedsB = (mode) => mode === 1 || mode === 2 || mode === 3 || mode === 5;
// logic-analyzer channel colours (D0..D7 = GP6..GP13)
const LOGIC_COLORS = ["#7CFC4D", "#FFD54A", "#66d9ff", "#ff8c00", "#ff66cc", "#66ffcc", "#c08cff", "#ff6666"];
// all measurements (display order); which appear on screen is user-configurable
const MEAS_ORDER = ["Vpp", "Vmax", "Vmin", "Mean", "AC RMS", "DC RMS", "Freq", "Time", "Duty",
  "Min Pulse", "Bit Rate", "+Edges", "−Edges", "+Pulses", "−Pulses"];
const MEAS_DEFAULT = ["Vpp", "Vmax", "Vmin", "Mean", "Freq", "Time", "Duty"];
let measEnabled = new Set(MEAS_DEFAULT);
try { const s = JSON.parse(localStorage.getItem("measEnabled")); if (Array.isArray(s)) measEnabled = new Set(s); } catch (e) { }
const saveMeas = () => { try { localStorage.setItem("measEnabled", JSON.stringify([...measEnabled])); } catch (e) { } };
// vdivIdx -> VDIV_LADDER volts/division (display scale, like the app); pos = vertical
// position in DIVISIONS (+ = up); probe = attenuation factor.
const chcfg = {
  0: { color: "#7CFC4D", vdivIdx: 1, pos: -1.5, probe: 1 },
  1: { color: "#FFD54A", vdivIdx: 1, pos: -1.5, probe: 1 },
};
let lastTrigStart = null;   // last triggered start index (NORM hold)
let drawStart = 0, drawScreenN = 0;   // current on-screen slice, shared with drawMath
// single-shot acquisition (client-side, like the app's SINGLE/FORCE button):
// "run" = live; "armed" = waiting for one trigger (button shows FORCE); "stop" = frozen
let acq = "run", frozenFrame = null;

const chLabel = (ch) => "CH" + (Number(ch) + 1);   // firmware id 0/1 -> board/app CH1/CH2
const $ = (id) => document.getElementById(id);
// per-channel front-end calibration [min_v, max_v] for the active range (from frame.cal)
const chCal = (ch) => (frame && frame.cal && frame.cal[String(ch)]) || [0, VREF];
// probe attenuation scales the *interpreted* voltage (×10 probe => signal is 10× the
// measured front-end voltage). Affects readouts/measurements, not the trace position.
const probe = (ch) => chcfg[ch].probe || 1;
const chVolts = (ch, s) => { const c = chCal(ch); return (c[0] + s / 255 * (c[1] - c[0])) * probe(ch); };
const cv = $("scope"), ctx = cv.getContext("2d");
function resize() { const r = window.devicePixelRatio || 1; cv.width = cv.clientWidth * r; cv.height = cv.clientHeight * r; }
window.addEventListener("resize", resize); resize();

/* ---------- transport ---------- */
const es = new EventSource("/events");
es.onopen = () => setConn(true);
es.onerror = () => setConn(false);
es.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.type === "frame") {
    frame = m;
    // the live actual rate rides on frames; state.rate_hz is only refreshed on
    // state broadcasts (set commands), so keep it current here and refresh the readout
    if (m.rate && m.rate !== state.rate_hz) { state.rate_hz = m.rate; updateSrate(); }
  } else if (m.type === "state") { state = m.state; if (m.by) lastBy = m.by; applyState(); }
};
let sseOk = false;
function setConn(ok) { sseOk = ok; renderConn(); }
function renderConn() {
  const b = $("conn");
  if (!sseOk) { b.textContent = "○ no daemon"; b.className = "badge bad"; return; }
  if (state.synced) { b.textContent = "● connected"; b.className = "badge ok"; }
  else { b.textContent = "○ device offline"; b.className = "badge bad"; }
}
function cmd(o) { fetch("/cmd", { method: "POST", body: JSON.stringify(o) }); }
function setp(p) { cmd({ cmd: "set", params: p }); }

/* ---------- state -> UI ---------- */
function chEnabled(ch) { return (state.channels || [0]).includes(ch); }
const arOn = (ch) => !!(state.auto_vrange && state.auto_vrange[ch]);
// hardware front-end ranges available for a channel, from the calibration table
// (state.voltage_ranges keyed "ch,rid" -> [min_v, max_v]). Like the app's VOLTS/DIV,
// these switch the actual front-end gain; measurements stay correct (cal rides on frames).
function rangesFor(ch) {
  const vr = state.voltage_ranges || {}, out = [];
  for (let r = 0; r < 8; r++) { const mm = vr[ch + "," + r]; if (mm) out.push([r, mm]); }
  return out;
}
function renderRanges() {
  const box = $("vrange"), rs = rangesFor(selCh);
  if (!rs.length) { box.innerHTML = ""; return; }
  const cur = Number((state.vrange && state.vrange[String(selCh)]) ?? 0);
  // full-scale span as the label (e.g. 12V, 2V) — the hardware input range, distinct
  // from the display VOLTS/DIV zoom above. Probe attenuation scales it too.
  box.innerHTML = rs.map(([r, mm]) => {
    const fs = (mm[1] - mm[0]) * probe(selCh);
    return `<button class="seg-btn rng ${r === cur ? "active" : ""}" data-r="${r}">${fmtV(fs)}</button>`;
  }).join("");
  box.querySelectorAll(".rng").forEach(b => b.onclick = () => setp({ vrange: { [selCh]: Number(b.dataset.r) } }));
}
function applyState() {
  const run = state.run_mode === 0;
  $("run").textContent = run ? "STOP" : "RUN"; $("run").classList.toggle("on", run);
  updateSingleBtn();
  seg("trig", "trig", state.trig_mode);
  seg("trigedge", "edge", state.trig_type);
  seg("trigsrc", "src", state.trig_channel);
  $("ch-on").classList.toggle("on", chEnabled(selCh));
  $("ch-on").textContent = chEnabled(selCh) ? "ON" : "OFF";
  const pv = probe(selCh), preset = [1, 10, 100].includes(pv);
  document.querySelectorAll("#probe .seg-btn[data-p]").forEach(b => b.classList.toggle("active", Number(b.dataset.p) === pv));
  $("probe-custom").classList.toggle("active", !preset);
  $("probe-custom").textContent = preset ? "✎" : "×" + pv;
  $("ar-btn").classList.toggle("active", arOn(selCh));
  $("ar-btn").textContent = arOn(selCh) ? "AUTO RANGE ●" : "AUTO RANGE";
  renderRanges();
  updateSrate();
  renderConn();
  $("lastby").textContent = "last: " + lastBy;
  $("trigstat").textContent = "trig: " + TRIG_NAMES[state.trig_mode || 0];
  $("lvl-val").textContent = fmtV(chVolts(state.trig_channel || 0, state.trig_level || 0));
  $("pre-val").textContent = (state.pre_trigger ?? 50) + "%";
  seg("pretrig", "pre", state.pre_trigger ?? 50);
  updateReadouts();
}
function fmtRate(hz) {
  if (!hz) return "0 S/s";
  if (hz >= 1e6) return (hz / 1e6) + " MS/s";
  if (hz >= 1e3) return (hz / 1e3) + " kS/s";
  return hz + " S/s";
}
function updateSrate() {
  const sel = state.sample_rate || 0, act = state.rate_hz || 0;
  // show the selection; on Auto (or while the device settles to a capped value) show actual too
  let txt;
  if (!sel) txt = "Auto · " + fmtRate(act);
  else if (Math.abs(act - sel) <= sel * 0.02) txt = fmtRate(sel);   // settled at request
  else txt = fmtRate(sel) + " → " + fmtRate(act);                   // capped / still settling
  $("srate").textContent = txt;
}
function seg(id, attr, val) { document.querySelectorAll(`#${id} .seg-btn`).forEach(b => b.classList.toggle("active", Number(b.dataset[attr]) === Number(val))); }
function fmtTime(s) { if (s >= 1) return s.toFixed(2) + " s"; if (s >= 1e-3) return (s * 1e3).toFixed(2) + " ms"; if (s >= 1e-6) return (s * 1e6).toFixed(1) + " µs"; return (s * 1e9).toFixed(0) + " ns"; }
function fmtHz(f) { return f >= 1e6 ? (f / 1e6).toFixed(2) + " MHz" : f >= 1e3 ? (f / 1e3).toFixed(2) + " kHz" : f.toFixed(0) + " Hz"; }
// real time span of the shown window (daemon-reported); falls back to an estimate
function frameWindowS() { return (frame && frame.win_s) ? frame.win_s : 3200 / (state.rate_hz || 1); }
function fmtV(v) { const a = Math.abs(v); return a < 1 ? (v * 1000).toFixed(0) + " mV" : v.toFixed(2) + " V"; }
function updateReadouts() {
  $("tdiv-val").textContent = fmtTime(frameWindowS() / HDIV) + "/div";
  $("vdiv-val").textContent = fmtV(VDIV_LADDER[chcfg[selCh].vdivIdx]) + "/div";
  // app parity: horizontal position in divisions, vertical position in Volts
  $("hpos-val").textContent = (hpos * HDIV).toFixed(1) + " div";
  $("vpos-val").textContent = fmtV(chcfg[selCh].pos * VDIV_LADDER[chcfg[selCh].vdivIdx]);
}

/* ---------- controls ---------- */
// RUN resumes live acquisition (clears any single-shot freeze) and toggles the device
$("run").onclick = () => { acq = "run"; frozenFrame = null; updateSingleBtn(); setp({ run_mode: state.run_mode === 0 ? 1 : 0 }); };
// SINGLE arms a one-shot; while armed the button reads FORCE and a second press
// captures the current frame immediately (force-trigger)
$("single").onclick = () => {
  if (acq === "armed") { frozenFrame = frame; acq = "stop"; }
  else { acq = "armed"; frozenFrame = null; }
  updateSingleBtn();
};
function updateSingleBtn() {
  const b = $("single");
  b.textContent = acq === "armed" ? "FORCE" : "SINGLE";
  b.classList.toggle("on", acq !== "run");
}
// has the current frame met the trigger condition? (NONE => always)
function trigSatisfied(f) {
  if ((state.trig_mode || 0) === 0) return true;
  const src = f && f.channels && f.channels[String(state.trig_channel || 0)];
  return !!src && triggerOffset(src, 1, src.length - 1) !== null;
}
document.querySelectorAll("#viewtabs .tab").forEach(t => t.onclick = () => { view = t.dataset.view; document.querySelectorAll("#viewtabs .tab").forEach(x => x.classList.toggle("active", x === t)); });
document.querySelectorAll("#chsel .seg-btn").forEach(b => b.onclick = () => { selCh = Number(b.dataset.ch); document.querySelectorAll("#chsel .seg-btn").forEach(x => x.classList.toggle("active", x === b)); applyState(); });
document.querySelectorAll("#probe .seg-btn[data-p]").forEach(b => b.onclick = () => { chcfg[selCh].probe = Number(b.dataset.p); applyState(); });
$("probe-custom").onclick = () => openDialog("probe");
$("ar-btn").onclick = () => { const a = Object.assign({}, state.auto_vrange); a[selCh] = !arOn(selCh); setp({ auto_vrange: a }); };
$("ch-on").onclick = () => { const s = new Set(state.channels || [0]); s.has(selCh) ? s.delete(selCh) : s.add(selCh); setp({ channels: [...s].sort() }); };
document.querySelectorAll("#trig .seg-btn").forEach(b => b.onclick = () => setp({ trig_mode: Number(b.dataset.trig) }));
document.querySelectorAll("#trigedge .seg-btn").forEach(b => b.onclick = () => setp({ trig_type: Number(b.dataset.edge) }));
document.querySelectorAll("#trigsrc .seg-btn").forEach(b => b.onclick = () => setp({ trig_channel: Number(b.dataset.src) }));
document.querySelectorAll("#pretrig .seg-btn").forEach(b => b.onclick = () => setp({ pre_trigger: Number(b.dataset.pre) }));
$("srate").onclick = () => openDialog("samplerate");
$("reconnect").onclick = () => { cmd({ cmd: "reconnect" }); $("conn").textContent = "○ reconnecting…"; };
$("cur-btn").onclick = () => { cursorsOn = !cursorsOn; $("cur-btn").classList.toggle("active", cursorsOn); };
$("roll-btn").onclick = () => { rollMode = !rollMode; $("roll-btn").classList.toggle("active", rollMode); };
$("math-btn").onclick = () => { mathMode = (mathMode + 1) % MATH_NAMES.length; $("math-btn").textContent = MATH_NAMES[mathMode]; $("math-btn").classList.toggle("active", mathMode !== 0); };
$("meas-btn").onclick = () => openDialog("measurements");
$("fft-btn").onclick = () => openDialog("fftset");
$("export-btn").onclick = () => openDialog("export");
function downloadCSV(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function csvFrom(cols, n, dt) {  // cols: [{label, get(i)->string}]
  let csv = "time_s," + cols.map(c => c.label).join(",") + "\n";
  for (let i = 0; i < n; i++) csv += (i * dt).toExponential(6) + "," + cols.map(c => c.get(i)).join(",") + "\n";
  return csv;
}
// the displayed points (downsampled "framepoints"), volts incl. calibration + probe
function exportVisible() {
  if (!frame || !frame.channels) return;
  const chs = (state.channels || [0]).filter(c => frame.channels[String(c)]);
  if (!chs.length) return;
  const n = Math.max(...chs.map(c => frame.channels[String(c)].length));
  const dt = (frame.win_s && frame.screen_pts) ? frame.win_s / frame.screen_pts : (state.rate_hz ? 1 / state.rate_hz : 0);
  const cols = chs.map(c => ({ label: chLabel(c) + "_V", get: i => { const d = frame.channels[String(c)]; return i < d.length ? chVolts(c, d[i]).toFixed(5) : ""; } }));
  downloadCSV("scoppy_visible.csv", csvFrom(cols, n, dt));
  closeDlg();
}
// the full-resolution capture window (one record / ring window) via the daemon's grab
async function exportFull() {
  const chs = state.channels || [0]; const got = {}; let rate = state.rate_hz || 0;
  for (const c of chs) {
    const r = await fetch("/cmd", { method: "POST", body: JSON.stringify({ cmd: "grab", channel: c, n: 200000 }) }).then(r => r.json()).catch(() => null);
    if (r) { got[c] = r.data || []; rate = r.rate || rate; }
  }
  const present = chs.filter(c => (got[c] || []).length);
  if (!present.length) return;
  const n = Math.max(...present.map(c => got[c].length)), dt = rate ? 1 / rate : 0;
  const cols = present.map(c => ({ label: chLabel(c) + "_V", get: i => { const d = got[c]; return i < d.length ? chVolts(c, d[i]).toFixed(5) : ""; } }));
  downloadCSV("scoppy_full.csv", csvFrom(cols, n, dt));
  closeDlg();
}
// on-screen draggable handles (YT only): cursor handles (when cursors on), the
// trigger-level marker on the right edge, and per-channel ground (0 V) markers on the left.
let dragT = null, trigPreview = null;
const clamp01 = (f) => Math.min(1, Math.max(0, f));
// invert vy: screen Y -> raw 0..255 sample on channel ch (for the trigger marker)
function yToRaw(y, h, ch) {
  const c = chCal(ch), v = yToVolts(y, h, ch) / probe(ch);
  return Math.round((v - c[0]) / (c[1] - c[0]) * 255);
}
// map a mouse event into the YT pane (device px -> pane fractions), null if outside
function panePos(e, clamp) {
  if (!ytRect) return null;
  const r = cv.getBoundingClientRect();
  let fx = ((e.clientX - r.left) * cv.width / r.width - ytRect.x) / ytRect.w;
  let fy = ((e.clientY - r.top) * cv.height / r.height - ytRect.y) / ytRect.h;
  if (!clamp && (fx < -0.02 || fx > 1.02 || fy < -0.02 || fy > 1.02)) return null;
  return { fx: clamp ? clamp01(fx) : fx, fy: clamp ? clamp01(fy) : fy, h: ytRect.h };
}
cv.addEventListener("mousedown", (e) => {
  const p = panePos(e, false); if (!p) return;
  const { fx, fy, h } = p, y = fy * h;
  if (cursorsOn) {   // cursor handles take priority when visible
    const cand = [["x1", Math.abs(fx - cur.x1)], ["x2", Math.abs(fx - cur.x2)], ["y1", Math.abs(fy - cur.y1)], ["y2", Math.abs(fy - cur.y2)]];
    cand.sort((a, b) => a[1] - b[1]);
    if (cand[0][1] < 0.05) { dragT = { kind: "cur", key: cand[0][0] }; return; }
  }
  const tch = state.trig_channel || 0;
  if ((state.trig_mode || 0) !== 0 && chEnabled(tch) && fx > 0.92 && Math.abs(y - vy(state.trig_level ?? 128, h, tch)) < h * 0.05) {
    dragT = { kind: "trig" }; return;
  }
  for (const ch of (state.channels || [0])) {
    if (fx < 0.08 && Math.abs(y - vyVolts(0, h, ch)) < h * 0.05) { dragT = { kind: "gnd", ch }; return; }
  }
});
window.addEventListener("mousemove", (e) => {
  if (!dragT) return;
  const p = panePos(e, true); if (!p) return;
  const { fx, fy, h } = p;
  if (dragT.kind === "cur") cur[dragT.key] = dragT.key[0] === "x" ? fx : fy;
  else if (dragT.kind === "gnd") { chcfg[dragT.ch].pos = (h / 2 - fy * h) / (h / VDIVS); updateReadouts(); }
  else if (dragT.kind === "trig") trigPreview = Math.max(0, Math.min(255, yToRaw(fy * h, h, state.trig_channel || 0)));
});
window.addEventListener("mouseup", () => {
  if (dragT && dragT.kind === "trig" && trigPreview != null) setp({ trig_level: trigPreview });
  dragT = null; trigPreview = null;
});
function tbIdx() { let bi = 0, bd = 1e18; TIMEBASES.forEach((t, i) => { const d = Math.abs(t - (state.timebase_centi_us || TIMEBASES[0])); if (d < bd) { bd = d; bi = i; } }); return bi; }
const ACT = {
  "time+": () => setp({ timebase_centi_us: TIMEBASES[Math.min(tbIdx() + 1, TIMEBASES.length - 1)] }),
  "time-": () => setp({ timebase_centi_us: TIMEBASES[Math.max(tbIdx() - 1, 0)] }),
  "hpos+": () => { hpos = Math.min(hpos + 0.05, 0.5); }, "hpos-": () => { hpos = Math.max(hpos - 0.05, -0.5); },
  // VOLTS/DIV: smaller volts/div = zoom in vertically (more sensitive)
  "volts+": () => { chcfg[selCh].vdivIdx = Math.min(chcfg[selCh].vdivIdx + 1, VDIV_LADDER.length - 1); },
  "volts-": () => { chcfg[selCh].vdivIdx = Math.max(chcfg[selCh].vdivIdx - 1, 0); },
  "pos+": () => { chcfg[selCh].pos += 0.5; },   // move trace up
  "pos-": () => { chcfg[selCh].pos -= 0.5; },
  "lvl+": () => setp({ trig_level: Math.min((state.trig_level || 0) + 6, 255) }),
  "lvl-": () => setp({ trig_level: Math.max((state.trig_level || 0) - 6, 0) }),
  // centre buttons: like the app, open a direct picker / centre the position
  "timeMenu": () => openDialog("timebase"),
  "voltsMenu": () => openDialog("voltsdiv"),
  "lvlMenu": () => openDialog("triglevel"),
  "hposCenter": () => { hpos = 0; },
  "vposCenter": () => { chcfg[selCh].pos = 0; },
};
document.querySelectorAll("[data-act]").forEach(b => b.onclick = () => { if (ACT[b.dataset.act]) { ACT[b.dataset.act](); updateReadouts(); } });

/* ---------- menu + dialogs ---------- */
$("menubtn").onclick = () => $("menu").classList.remove("hidden");
$("menuclose").onclick = () => $("menu").classList.add("hidden");
document.querySelectorAll(".menuitem").forEach(b => b.onclick = () => { $("menu").classList.add("hidden"); openDialog(b.dataset.dlg); });
$("dlg").onclick = (e) => { if (e.target === $("dlg")) $("dlg").classList.add("hidden"); };
function openDialog(name) {
  const b = $("dlgbody"); b.innerHTML = DIALOGS[name] ? DIALOGS[name]() : `<h3>${name}</h3><p>Coming soon.</p>`;
  $("dlg").classList.remove("hidden");
  if (WIRE[name]) WIRE[name]();
}
function closeDlg() { $("dlg").classList.add("hidden"); }
const DIALOGS = {
  samplerate: () => {
    const sel = state.sample_rate || 0, cap = maxSrHz();
    const btns = SAMPLE_RATES.filter(hz => hz === 0 || hz <= cap).map(hz =>
      `<button class="seg-btn ratebtn ${hz === sel ? "active" : ""}" data-hz="${hz}">${hz ? fmtRate(hz) : "Auto"}</button>`).join("");
    return `<h3>Sample rate</h3>
      <p class="dim"><b>Auto</b> picks the rate from Time/Div; a <b>fixed</b> rate forces
      it (capped by the device max, now ${MAX_SR_LABEL()}).</p>
      <div class="readline"><span>Selected</span><span class="val">${sel ? fmtRate(sel) : "Auto"}</span></div>
      <div class="readline"><span>Actual now</span><span class="val">${fmtRate(state.rate_hz || 0)}</span></div>
      <div class="rategrid">${btns}</div>
      <p class="dim">Change the ceiling in Menu › Max Sample Rate.</p>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`;
  },
  maxsr: () => {
    const cur = state.max_sr || 0;
    const maxbtns = MAX_SR.map(([code, lbl]) =>
      `<button class="seg-btn maxbtn ${code === cur ? "active" : ""}" data-code="${code}">${lbl}</button>`).join("");
    return `<h3>Max Sample Rate (RP2040)</h3>
      <p class="dim">The device-wide ceiling — like the app's RP2040 setting. Set it
      once; the everyday rate lives in the top-left value. Higher codes overclock the
      ADC: our FScope reaches ~1.0 MS/s at "2 MS/s", ~1.25 MS/s at "2.5 MS/s" (≈half
      the label per channel). Front-end bandwidth is ~150 kHz, so very high rates
      mainly help anti-aliasing / FFT, not measuring faster signals.</p>
      <div class="row seg" id="maxsr">${maxbtns}</div>
      <div class="readline"><span>Actual now</span><span class="val">${fmtRate(state.rate_hz || 0)}</span></div>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`;
  },
  timebase: () => {
    const cur = state.timebase_centi_us || TIMEBASES[0];
    const btns = TIMEBASES.map(tb =>
      `<button class="seg-btn tbbtn ${tb === cur ? "active" : ""}" data-tb="${tb}">${fmtTime(tb * 1e-7 / HDIV)}/div</button>`).join("");
    return `<h3>Time / Div</h3>
      <p class="dim">Horizontal timebase — the on-screen window spans 10 divisions.</p>
      <div class="rategrid">${btns}</div>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`;
  },
  voltsdiv: () => {
    const cur = chcfg[selCh].vdivIdx;
    const btns = VDIV_LADDER.map((v, i) =>
      `<button class="seg-btn vibtn ${i === cur ? "active" : ""}" data-vi="${i}">${fmtV(v)}/div</button>`).join("");
    return `<h3>Volts / Div — ${chLabel(selCh)}</h3>
      <p class="dim">Display vertical zoom. The hardware input range is separate — set it
      in the Vertical panel (it changes ADC sensitivity, this only scales the drawing).</p>
      <div class="rategrid">${btns}</div>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`;
  },
  triglevel: () => {
    const lvl = state.trig_level ?? 128, src = state.trig_channel || 0;
    return `<h3>Trigger Level</h3>
      <div class="field"><label>Level: <span id="tl-val" class="val">${fmtV(chVolts(src, lvl))}</span></label>
        <input id="tl-range" type="range" min="0" max="255" step="1" value="${lvl}"></div>
      <p class="dim">Source: ${chLabel(src)}. The raw threshold is mapped to Volts via
      that channel's calibration (and probe).</p>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`;
  },
  siggen: () => {
    const sg = state.siggen || { func: 1, freq: 1000, duty: 50 };
    return `<h3>Signal Generator</h3>
      <div class="row seg" id="sg-func">
        <button class="seg-btn ${sg.func === 0 ? "active" : ""}" data-f="0">OFF</button>
        <button class="seg-btn ${sg.func === 1 ? "active" : ""}" data-f="1">SQUARE</button>
        <button class="seg-btn ${sg.func === 2 ? "active" : ""}" data-f="2">SINE</button>
      </div>
      <div class="field"><label>Frequency: <span id="sg-fval" class="val">${sg.freq} Hz</span></label>
        <input id="sg-freq" type="range" min="100" max="100000" step="100" value="${sg.freq}"></div>
      <div class="field"><label>Duty: <span id="sg-dval" class="val">${sg.duty}%</span></label>
        <input id="sg-duty" type="range" min="5" max="95" step="5" value="${sg.duty}"></div>
      <p class="dim">Output on GP22. Jumper GP22→GP26 to read on CH0.</p>
      <div class="dlg-actions"><button id="sg-apply" class="tgl on">Apply</button><button onclick="closeDlg()" class="ghost">Close</button></div>`;
  },
  channels: () => `<h3>Channels</h3>
      <label class="row" style="align-items:center;gap:8px"><input type="checkbox" id="cb0" ${chEnabled(0) ? "checked" : ""}> <span style="color:var(--ch0)">CH1 — GP26 (ADC0)</span></label>
      <label class="row" style="align-items:center;gap:8px"><input type="checkbox" id="cb1" ${chEnabled(1) ? "checked" : ""}> <span style="color:var(--ch1)">CH2 — GP27 (ADC1)</span></label>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`,
  measurements: () => `<h3>On-screen measurements</h3>
      <p class="dim">Pick which measurements show in the bar at the bottom of the screen.</p>
      <div class="rategrid" style="grid-template-columns:repeat(2,1fr)">
        ${MEAS_ORDER.map(k => `<label class="row" style="gap:6px;align-items:center;font-size:13px">
          <input type="checkbox" class="measchk" data-k="${k}" ${measEnabled.has(k) ? "checked" : ""}> ${k}</label>`).join("")}
      </div>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`,
  probe: () => `<h3>Custom probe — ${chLabel(selCh)}</h3>
      <p class="dim">Attenuation factor: the signal is this many times the voltage measured
      at the input. Scales readouts and measurements, not the trace position.</p>
      <div class="field"><label>Factor: <span id="pf-val" class="val">×${probe(selCh)}</span></label>
        <input id="pf-range" type="range" min="1" max="100" step="1" value="${probe(selCh)}"></div>
      <div class="dlg-actions"><button id="pf-apply" class="tgl on">Apply</button><button onclick="closeDlg()" class="ghost">Close</button></div>`,
  export: () => `<h3>Export CSV</h3>
      <p class="dim">Time + per-channel Volts (calibration + probe applied), for the enabled
      channels.</p>
      <div class="row" style="gap:8px"><button id="exp-vis" class="tgl on">Visible (framepoints)</button>
        <button id="exp-full" class="ghost">Full-res window</button></div>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`,
  fftset: () => `<h3>FFT settings</h3>
      <div class="label" style="margin-top:4px">Window</div>
      <div class="row seg">${["Hann", "Hamming", "Blackman"].map(x => `<button class="seg-btn fset ${x === fftWindow ? "active" : ""}" data-g="win" data-v="${x}">${x}</button>`).join("")}</div>
      <div class="label" style="margin-top:4px">Vertical scale</div>
      <div class="row seg">${["Linear", "dBV"].map(x => `<button class="seg-btn fset ${x === fftScale ? "active" : ""}" data-g="scale" data-v="${x}">${x}</button>`).join("")}</div>
      <div class="label" style="margin-top:4px">Span (zoom into low frequencies)</div>
      <div class="row seg">${[1, 2, 4, 8].map(z => `<button class="seg-btn fset ${z === fftZoom ? "active" : ""}" data-g="zoom" data-v="${z}">${z === 1 ? "Full" : "÷" + z}</button>`).join("")}</div>
      <p class="dim">Spectrum of the selected channel, in Volts. dBV is relative to 1 V RMS.
      RBW (resolution bandwidth) is shown on the FFT screen.</p>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`,
  math: () => `<h3>Math channel</h3>
      <div class="row seg" id="mathsel">
        ${MATH_NAMES.map((n, i) => `<button class="seg-btn ${i === mathMode ? "active" : ""}" data-m="${i}">${i === 0 ? "OFF" : n}</button>`).join("")}
      </div>
      <p class="dim">Drawn in orange on CH1's vertical scale; needs both channels enabled.
      You can also toggle it from the Tools panel.</p>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`,
  mode: () => `<h3>Mode</h3>
      <div class="row seg" id="modesel">
        <button class="seg-btn ${state.logic_mode ? "" : "active"}" data-m="0">Oscilloscope</button>
        <button class="seg-btn ${state.logic_mode ? "active" : ""}" data-m="1">Logic Analyzer</button>
      </div>
      <p class="dim">Logic analyzer: 8 digital channels D0–D7 on GP6–GP13.</p>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`,
  display: () => `<h3>Display</h3>
      <label class="row" style="gap:8px;align-items:center"><input type="checkbox" id="d-grid" ${dgrid ? "checked" : ""}> Show graticule</label>
      <div class="field"><label>Trace width: <span id="d-tw-val" class="val">${traceWidth.toFixed(1)} px</span></label>
        <input id="d-tw" type="range" min="1" max="4" step="0.2" value="${traceWidth}"></div>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`,
  help: () => `<h3>Help</h3><p>Shared Scoppy oscilloscope. The daemon owns the USB
      connection; this GUI, the CLI and the agent all share it live.</p>
      <p class="dim">Wiki: oscilloscope.fhdm.xyz</p>
      <div class="dlg-actions"><button onclick="closeDlg()" class="ghost">Close</button></div>`,
};
let dgrid = true, traceWidth = 1.6;
const WIRE = {
  samplerate: () => document.querySelectorAll(".ratebtn").forEach(b => b.onclick = () => {
    setp({ sample_rate: Number(b.dataset.hz) });
    document.querySelectorAll(".ratebtn").forEach(x => x.classList.toggle("active", x === b));
  }),
  maxsr: () => document.querySelectorAll("#maxsr .maxbtn").forEach(b => b.onclick = () => {
    setp({ max_sr: Number(b.dataset.code) });
    document.querySelectorAll("#maxsr .maxbtn").forEach(x => x.classList.toggle("active", x === b));
  }),
  timebase: () => document.querySelectorAll(".tbbtn").forEach(b => b.onclick = () => {
    setp({ timebase_centi_us: Number(b.dataset.tb) }); closeDlg();
  }),
  voltsdiv: () => document.querySelectorAll(".vibtn").forEach(b => b.onclick = () => {
    chcfg[selCh].vdivIdx = Number(b.dataset.vi); updateReadouts(); closeDlg();
  }),
  triglevel: () => {
    const r = $("tl-range");
    r.oninput = () => $("tl-val").textContent = fmtV(chVolts(state.trig_channel || 0, Number(r.value)));
    r.onchange = () => setp({ trig_level: Number(r.value) });
  },
  siggen: () => {
    let f = (state.siggen || {}).func ?? 1;
    document.querySelectorAll("#sg-func .seg-btn").forEach(b => b.onclick = () => { f = Number(b.dataset.f); document.querySelectorAll("#sg-func .seg-btn").forEach(x => x.classList.toggle("active", x === b)); });
    $("sg-freq").oninput = () => $("sg-fval").textContent = $("sg-freq").value + " Hz";
    $("sg-duty").oninput = () => $("sg-dval").textContent = $("sg-duty").value + "%";
    $("sg-apply").onclick = () => cmd({ cmd: "siggen", func: f, freq: Number($("sg-freq").value), duty: Number($("sg-duty").value), gpio: 255 });
  },
  channels: () => { $("cb0").onchange = $("cb1").onchange = () => { const s = new Set(); if ($("cb0").checked) s.add(0); if ($("cb1").checked) s.add(1); setp({ channels: [...s].sort().length ? [...s].sort() : [0] }); }; },
  measurements: () => document.querySelectorAll(".measchk").forEach(b => b.onchange = () => {
    b.checked ? measEnabled.add(b.dataset.k) : measEnabled.delete(b.dataset.k);
    saveMeas();
  }),
  export: () => { $("exp-vis").onclick = exportVisible; $("exp-full").onclick = exportFull; },
  probe: () => {
    const r = $("pf-range");
    r.oninput = () => $("pf-val").textContent = "×" + r.value;
    $("pf-apply").onclick = () => { chcfg[selCh].probe = Number(r.value); applyState(); closeDlg(); };
  },
  fftset: () => document.querySelectorAll(".fset").forEach(b => b.onclick = () => {
    const g = b.dataset.g, v = b.dataset.v;
    if (g === "win") fftWindow = v; else if (g === "scale") fftScale = v; else fftZoom = Number(v);
    b.parentElement.querySelectorAll(".seg-btn").forEach(x => x.classList.toggle("active", x === b));
  }),
  math: () => document.querySelectorAll("#mathsel .seg-btn").forEach(b => b.onclick = () => {
    mathMode = Number(b.dataset.m);
    document.querySelectorAll("#mathsel .seg-btn").forEach(x => x.classList.toggle("active", x === b));
    $("math-btn").textContent = MATH_NAMES[mathMode]; $("math-btn").classList.toggle("active", mathMode !== 0);
  }),
  mode: () => document.querySelectorAll("#modesel .seg-btn").forEach(b => b.onclick = () => { document.querySelectorAll("#modesel .seg-btn").forEach(x => x.classList.toggle("active", x === b)); setp({ logic_mode: b.dataset.m === "1" }); }),
  display: () => { $("d-grid").onchange = () => dgrid = $("d-grid").checked; $("d-tw").oninput = () => { traceWidth = Number($("d-tw").value); $("d-tw-val").textContent = traceWidth.toFixed(1) + " px"; }; },
};
window.closeDlg = closeDlg;

/* ---------- FFT ---------- */
function fft(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) { let bit = n >> 1; for (; j & bit; bit >>= 1) j ^= bit; j ^= bit; if (i < j) { [re[i], re[j]] = [re[j], re[i]]; [im[i], im[j]] = [im[j], im[i]]; } }
  for (let len = 2; len <= n; len <<= 1) { const ang = -2 * Math.PI / len; for (let i = 0; i < n; i += len) for (let k = 0; k < len / 2; k++) { const wr = Math.cos(ang * k), wi = Math.sin(ang * k), a = i + k, b = a + len / 2; const tr = re[b] * wr - im[b] * wi, ti = re[b] * wi + im[b] * wr; re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti; } }
}

/* ---------- trigger (client-side display stabilization) ---------- */
// first level-crossing edge with index in [lo, hi] (raw samples; trig_level is raw 0..255)
function triggerOffset(data, lo, hi) {
  if ((state.trig_mode || 0) === 0 || !data) return null;
  const lv = state.trig_level ?? 128, rising = (state.trig_type || 0) === 0;
  lo = Math.max(1, lo | 0); hi = Math.min(hi | 0, data.length - 1);
  for (let i = lo; i <= hi; i++) {
    const a = data[i - 1], b = data[i];
    if (rising ? (a < lv && b >= lv) : (a > lv && b <= lv)) return i;
  }
  return null;   // no trigger in range
}

/* ---------- drawing ---------- */
function drawGraticule(w, h) {
  ctx.fillStyle = "#1a1a1a"; ctx.fillRect(0, 0, w, h);
  if (!dgrid) return;
  ctx.lineWidth = 1; ctx.strokeStyle = "#3a3a3a";
  for (let i = 0; i <= HDIV; i++) { const x = w * i / HDIV; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
  for (let i = 0; i <= VDIVS; i++) { const y = h * i / VDIVS; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  ctx.strokeStyle = "#5a5a5a"; ctx.beginPath(); ctx.moveTo(w / 2, 0); ctx.lineTo(w / 2, h); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
}
// map a voltage to a screen Y using channel ch's scale + position
function vyVolts(v, h, ch) {
  const ppd = h / VDIVS;                          // pixels per division
  const vpd = VDIV_LADDER[chcfg[ch].vdivIdx];     // volts per division
  const groundY = h / 2 - chcfg[ch].pos * ppd;    // 0 V line, raised by `pos` divisions
  return groundY - (v / vpd) * ppd;
}
// inverse: screen Y -> volts in channel ch's scale (for cursors)
function yToVolts(y, h, ch) {
  const ppd = h / VDIVS, vpd = VDIV_LADDER[chcfg[ch].vdivIdx], groundY = h / 2 - chcfg[ch].pos * ppd;
  return (groundY - y) / ppd * vpd;
}
function vy(sample, h, ch) {
  // like the app: vertical scale is VOLTS/DIV, vertical position moves the 0 V line.
  return vyVolts(chVolts(ch, sample), h, ch);     // actual volts (calibration + probe)
}
function drawTraceYT(w, h) {
  if (!frame || !frame.channels) return;
  const chans = state.channels || [0];
  const ref = frame.channels[String(chans[0])]; if (!ref || !ref.length) return;
  const n = ref.length;
  // screen_pts = how many of the (2x) buffer points fill one screen; align within the rest
  const screenN = Math.max(2, Math.min(n, frame.screen_pts || Math.floor(n / 2)));
  const mode = state.trig_mode || 0;
  let start = n - screenN;                       // free-run / AUTO-no-trigger: newest screen
  const srcData = frame.channels[String(state.trig_channel || 0)];
  if (!rollMode && mode !== 0 && srcData) {      // roll mode: ignore the trigger, just scroll
    const pre = Math.round((state.pre_trigger ?? 50) / 100 * screenN);
    // search for the trigger edge where there's room for pre-samples before and the
    // rest of the screen after — so the chosen edge lands at the pre-trigger position
    const t = triggerOffset(srcData, pre, n - (screenN - pre));
    if (t !== null) { start = t - pre; lastTrigStart = start; }
    else if (mode === 2 && lastTrigStart != null) start = lastTrigStart;  // NORM: hold last
  }
  start += Math.round(hpos * screenN);           // horizontal pan
  start = Math.max(0, Math.min(start, n - screenN));
  drawStart = start; drawScreenN = screenN;      // shared with drawMath
  for (const ch of chans) {
    const data = frame.channels[String(ch)];
    if (data && data.length >= start + screenN) drawSlice(data, ch, w, h, start, screenN);
  }
  if (rollMode) paneLabel(w, h, "● ROLL MODE");
}
// draw a contiguous slice data[start..start+screenN) across the full width (no wrap)
function drawSlice(data, ch, w, h, start, screenN) {
  ctx.lineWidth = traceWidth; ctx.strokeStyle = chcfg[ch].color; ctx.lineJoin = "round"; ctx.beginPath();
  for (let i = 0; i < screenN; i++) {
    const x = w * i / (screenN - 1), y = vy(data[start + i], h, ch);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
}
// math channel: combine CH1 & CH2 in volts, drawn on CH1's scale (orange)
function drawMath(w, h) {
  if (mathMode === 0 || !frame || !frame.channels) return;
  const a = frame.channels["0"], b = frame.channels["1"];
  const s = drawStart, N = drawScreenN;             // same window/alignment as the traces
  if (!N) return;
  if (mathNeedsA(mathMode) && (!a || a.length < s + N)) return;
  if (mathNeedsB(mathMode) && (!b || b.length < s + N)) return;
  ctx.strokeStyle = "#ff8c00"; ctx.lineWidth = 1.4; ctx.lineJoin = "round"; ctx.beginPath();
  for (let i = 0; i < N; i++) {
    const va = a ? chVolts(0, a[s + i]) : 0, vb = b ? chVolts(1, b[s + i]) : 0;
    const x = w * i / (N - 1), y = vyVolts(mathCompute(va, vb, mathMode), h, 0);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
}
// draggable measurement cursors: two time (vertical) + two voltage (horizontal)
function drawCursors(w, h) {
  if (!cursorsOn) return;   // only called from the YT pane
  const x1 = cur.x1 * w, x2 = cur.x2 * w, y1 = cur.y1 * h, y2 = cur.y2 * h;
  ctx.save(); ctx.setLineDash([6, 4]); ctx.lineWidth = 1;
  ctx.strokeStyle = "#ff66cc";
  [x1, x2].forEach(x => { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); });
  ctx.strokeStyle = "#66e0ff";
  [y1, y2].forEach(y => { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); });
  ctx.restore();
  const winS = frameWindowS(), dt = Math.abs(cur.x2 - cur.x1) * winS;
  const dv = Math.abs(yToVolts(y1, h, selCh) - yToVolts(y2, h, selCh));
  const lines = ["Δt " + fmtTime(dt), "1/Δt " + (dt ? fmtHz(1 / dt) : "—"), "ΔV(" + chLabel(selCh) + ") " + fmtV(dv)];
  const r = window.devicePixelRatio || 1;
  ctx.font = (12 * r) + "px monospace"; ctx.textAlign = "left";
  const bw = 160 * r, bh = (lines.length * 16 + 8) * r, bx = w - bw - 8 * r, by = 8 * r;
  ctx.fillStyle = "rgba(0,0,0,.6)"; ctx.fillRect(bx, by, bw, bh);
  ctx.fillStyle = "#fff"; lines.forEach((l, i) => ctx.fillText(l, bx + 8 * r, by + (18 + i * 16) * r));
}
function vx(sample, w, ch) {
  // XY: horizontal axis uses the channel's VOLTS/DIV, like the vertical axis
  const v = chVolts(ch, sample);
  const ppd = w / HDIV, vpd = VDIV_LADDER[chcfg[ch].vdivIdx];
  return w / 2 + (v / vpd) * ppd;
}
function paneLabel(w, h, text) {     // small on-canvas label (top-left of the pane)
  const r = window.devicePixelRatio || 1;
  ctx.fillStyle = "#b3b3b3"; ctx.font = (11 * r) + "px monospace"; ctx.textAlign = "left";
  ctx.fillText(text, 4 * r, 12 * r);
}
function drawXY(w, h) {
  if (!frame || !frame.channels) return;
  const xs = frame.channels["0"], ys = frame.channels["1"];
  if (!xs || !ys || !xs.length || !ys.length) { paneLabel(w, h, "XY needs CH1 + CH2 on"); return; }
  paneLabel(w, h, "XY   X: CH1   Y: CH2");
  const n = Math.min(xs.length, ys.length);
  ctx.strokeStyle = "#66d9ff"; ctx.lineWidth = 1.2; ctx.lineJoin = "round"; ctx.beginPath();
  for (let i = 0; i < n; i++) { const x = vx(xs[i], w, 0), y = vy(ys[i], h, 1); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
  ctx.stroke();
}
function fftAxis(w, h, fmax, rbw, topLbl) {
  const r = window.devicePixelRatio || 1;
  ctx.fillStyle = "#b3b3b3"; ctx.font = (11 * r) + "px monospace";
  ctx.textAlign = "left"; ctx.fillText("0 Hz", 4 * r, h - 4 * r);
  ctx.fillText(topLbl, 4 * r, 14 * r); ctx.fillText("RBW " + fmtHz(rbw), 4 * r, 28 * r);
  ctx.textAlign = "right"; ctx.fillText(fmtHz(fmax), w - 4 * r, h - 4 * r);
  ctx.textAlign = "left";
}
function drawFFT(w, h) {
  if (!frame || !frame.channels) return;
  const data = frame.channels[String(selCh)] || frame.channels["0"]; if (!data) return;
  let N = 1; while (N * 2 <= data.length) N *= 2; N = Math.min(N, 1024); if (N < 4) return;
  const winFn = WIN_FNS[fftWindow] || WIN_FNS.Hann;
  const re = new Array(N), im = new Array(N).fill(0), win = new Array(N);
  // FFT of the signal in VOLTS (so dBV is meaningful), windowed, DC removed
  let mean = 0; for (let i = 0; i < N; i++) mean += chVolts(selCh, data[i]); mean /= N;
  let cg = 0; for (let i = 0; i < N; i++) { win[i] = winFn(i, N); cg += win[i]; } cg /= N;   // coherent gain
  for (let i = 0; i < N; i++) re[i] = (chVolts(selCh, data[i]) - mean) * win[i];
  fft(re, im);
  const half = N / 2, shown = Math.max(2, Math.floor(half / fftZoom));
  const amp = new Array(half);                       // single-sided amplitude in volts
  for (let i = 0; i < half; i++) amp[i] = 2 * Math.hypot(re[i], im[i]) / N / cg;
  const fs = (frame.screen_pts || data.length) / frameWindowS(), rbw = fs / N;
  ctx.strokeStyle = chcfg[selCh].color; ctx.lineWidth = 1.4; ctx.beginPath();
  if (fftScale === "dBV") {
    let mxd = -999; for (let i = 0; i < shown; i++) { const d = 20 * Math.log10(amp[i] + 1e-12); if (d > mxd) mxd = d; }
    const top = Math.ceil((mxd + 1) / 10) * 10, bot = top - 80;        // 80 dB display span
    for (let i = 0; i < shown; i++) { const d = Math.max(bot, 20 * Math.log10(amp[i] + 1e-12)); const x = w * i / (shown - 1), y = h - (d - bot) / (top - bot) * h * 0.92; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
    ctx.stroke(); fftAxis(w, h, fs / 2 / fftZoom, rbw, top + " dBV");
  } else {
    let mx = 1e-12; for (let i = 0; i < shown; i++) if (amp[i] > mx) mx = amp[i];
    for (let i = 0; i < shown; i++) { const x = w * i / (shown - 1), y = h - (amp[i] / mx) * h * 0.92; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
    ctx.stroke(); fftAxis(w, h, fs / 2 / fftZoom, rbw, fmtV(mx) + " pk");
  }
}
/* measurements (computed daemon-side on full-res samples; see frame.meas) */
// fixed-width field so the readouts don't shuffle as digit counts change
const mf = (label, val) => `<span class="mf"><span class="ml">${label}</span><span class="mv">${val}</span></span>`;
// all 15 measurements for one channel, as formatted strings (daemon sends raw-count
// stats; we convert to volts/time with the channel's calibration + probe and the rate)
function measValues(ch, m) {
  const c = chCal(ch), p = probe(ch), a0 = c[0] * p, a1 = (c[1] - c[0]) / 255 * p;
  const vmax = chVolts(ch, m.max), vmin = chVolts(ch, m.min), vmean = chVolts(ch, m.mean);
  const meanRaw = m.mean, msq = m.msq ?? meanRaw * meanRaw;
  const dcrms = Math.sqrt(Math.max(0, a0 * a0 + 2 * a0 * a1 * meanRaw + a1 * a1 * msq));
  const acrms = a1 * Math.sqrt(Math.max(0, msq - meanRaw * meanRaw));
  const rate = state.rate_hz || 0, minP = (m.min_pulse && rate) ? m.min_pulse / rate : 0;
  return {
    Vpp: fmtV(Math.abs(vmax - vmin)), Vmax: fmtV(vmax), Vmin: fmtV(vmin), Mean: fmtV(vmean),
    "AC RMS": fmtV(acrms), "DC RMS": fmtV(dcrms),
    Freq: m.freq ? fmtHz(m.freq) : "—", Time: m.freq ? fmtTime(1 / m.freq) : "—",
    Duty: m.duty.toFixed(0) + "%",
    "Min Pulse": minP ? fmtTime(minP) : "—", "Bit Rate": minP ? fmtHz(1 / minP).replace(/Hz$/, "bps") : "—",
    "+Edges": String(m.pos_edges ?? 0), "−Edges": String(m.neg_edges ?? 0),
    "+Pulses": String(m.pos_pulses ?? 0), "−Pulses": String(m.neg_pulses ?? 0),
  };
}
function updateMeasBar() {
  const bar = $("measbar"); if (!frame || !frame.meas) { bar.textContent = ""; return; }
  let html = "";
  // one row per channel (CH1, then CH2 below); only the user-enabled measurements show
  for (const ch of (state.channels || [0])) {
    const m = frame.meas[String(ch)]; if (!m) continue;
    const v = measValues(ch, m);
    let row = `<div class="mrow"><span class="mch" style="color:${chcfg[ch].color}">${chLabel(ch)}</span>`;
    for (const k of MEAS_ORDER) if (measEnabled.has(k)) row += mf(k, v[k]);
    html += row + `</div>`;
  }
  bar.innerHTML = html;
}
// logic-analyzer view: 8 stacked digital traces (step waveforms)
function drawLogic(w, h) {
  const d = frame && frame.logic, r = window.devicePixelRatio || 1;
  if (!d || !d.length) { $("overlay").textContent = "Logic Analyzer — waiting for data…"; return; }
  $("overlay").textContent = "Logic Analyzer   D0–D7 = GP6–GP13";
  const n = d.length, rows = 8, rowH = h / rows;
  ctx.font = (11 * r) + "px monospace"; ctx.textAlign = "left";
  for (let ch = 0; ch < rows; ch++) {
    const top = ch * rowH, hi = top + rowH * 0.25, lo = top + rowH * 0.78;
    ctx.strokeStyle = LOGIC_COLORS[ch]; ctx.lineWidth = 1.6; ctx.beginPath();
    let py = ((d[0] >> ch) & 1) ? hi : lo; ctx.moveTo(0, py);
    for (let i = 0; i < n; i++) { const bit = (d[i] >> ch) & 1, y = bit ? hi : lo, x = w * i / (n - 1); ctx.lineTo(x, py); ctx.lineTo(x, y); py = y; }
    ctx.stroke();
    ctx.fillStyle = LOGIC_COLORS[ch]; ctx.fillText("D" + ch + " · GP" + (6 + ch), 4 * r, top + rowH * 0.55);
  }
}
// edge markers: ground (0 V) on the left per channel, trigger level on the right
function edgeMarker(side, y, color, w) {
  const r = window.devicePixelRatio || 1, s = 8 * r;
  ctx.fillStyle = color; ctx.beginPath();
  if (side === "L") { ctx.moveTo(0, y - s); ctx.lineTo(0, y + s); ctx.lineTo(s * 1.4, y); }
  else { ctx.moveTo(w, y - s); ctx.lineTo(w, y + s); ctx.lineTo(w - s * 1.4, y); }
  ctx.closePath(); ctx.fill();
}
function drawHandles(w, h) {
  for (const ch of (state.channels || [0])) edgeMarker("L", vyVolts(0, h, ch), chcfg[ch].color, w);
  const tch = state.trig_channel || 0;
  if ((state.trig_mode || 0) !== 0 && chEnabled(tch)) {
    const lvl = trigPreview != null ? trigPreview : (state.trig_level ?? 128);
    edgeMarker("R", vy(lvl, h, tch), chcfg[tch].color, w);
  }
}
// pane layout for each display mode (like the app's display menu): list of [kind,x,y,w,h]
function panesFor(v, W, H) {
  switch (v) {
    case "xy": return [["xy", 0, 0, W, H]];
    case "fft": return [["fft", 0, 0, W, H]];
    case "yt_fft": return [["yt", 0, 0, W, H / 2], ["fft", 0, H / 2, W, H / 2]];
    case "yt_xy": return [["yt", 0, 0, W / 2, H], ["xy", W / 2, 0, W / 2, H]];
    case "yt_fft_xy": return [["yt", 0, 0, W, H / 2], ["fft", 0, H / 2, W / 2, H / 2], ["xy", W / 2, H / 2, W / 2, H / 2]];
    default: return [["yt", 0, 0, W, H]];
  }
}
let ytRect = null;   // YT pane rect (device px) — mouse handles map into it
function renderPane(kind, x, y, w, h) {
  ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip(); ctx.translate(x, y);
  drawGraticule(w, h);
  if (kind === "yt") { ytRect = { x, y, w, h }; drawTraceYT(w, h); drawMath(w, h); drawHandles(w, h); drawCursors(w, h); }
  else if (kind === "fft") { drawFFT(w, h); }
  else if (kind === "xy") { drawXY(w, h); }
  ctx.restore();
}
function draw() {
  const W = cv.width, H = cv.height;
  if (state.logic_mode) { drawGraticule(W, H); drawLogic(W, H); $("measbar").innerHTML = ""; $("overlay").textContent = ""; requestAnimationFrame(draw); return; }
  // single-shot: once armed and the trigger fires, latch this frame and stop
  if (acq === "armed" && frame && trigSatisfied(frame)) { frozenFrame = frame; acq = "stop"; updateSingleBtn(); }
  const live = frame, showFrozen = acq === "stop" && frozenFrame;
  if (showFrozen) frame = frozenFrame;
  ctx.clearRect(0, 0, W, H); ytRect = null;
  for (const [kind, x, y, w, h] of panesFor(view, W, H)) renderPane(kind, x, y, w, h);
  $("overlay").textContent = showFrozen ? "◼ STOPPED (single capture)" : (acq === "armed" ? "● armed — waiting for trigger" : "");
  updateMeasBar();
  frame = live;
  requestAnimationFrame(draw);
}
draw();
fetch("/state").then(r => r.json()).then(j => { if (j.state) { state = j.state; applyState(); } }).catch(() => {});
