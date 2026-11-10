/* Race Blueprint frontend: runs the Python engine in the browser via Pyodide. */
"use strict";

const STORE_KEY = "race-blueprint:v1";
const MAX_STORED_GPX = 1_500_000;
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = { gpxText: null, gpxName: null, result: null };

// ----------------------------------------------------------------- formatting
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function fmtDuration(sec) {
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}
const fmtPace = (secPerKm) => fmtDuration(secPerKm);
const fmtNum = (n, d = 0) => Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

function parseMinSec(text) {
  const m = /^\s*(\d{1,2}):(\d{2})\s*$/.exec(text || "");
  return m && Number(m[2]) < 60 ? Number(m[1]) * 60 + Number(m[2]) : null;
}
function parseHms(text) {
  if (!text || !text.trim()) return undefined;
  const parts = text.trim().split(":").map(Number);
  if (parts.some((p) => !Number.isFinite(p) || p < 0) || parts.length < 2 || parts.length > 3) return null;
  const [h, m, s] = parts.length === 3 ? parts : [0, ...parts];
  return m < 60 && s < 60 ? h * 3600 + m * 60 + s : null;
}
function defaultStart() {
  const d = new Date();
  d.setDate(d.getDate() + ((7 - d.getDay()) % 7 || 7)); // next Sunday
  d.setHours(9, 0, 0, 0);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ----------------------------------------------------------------- engine (Pyodide)
function setEngine(stateName, text) {
  const el = $("#engine");
  el.dataset.state = stateName;
  $(".engine__text", el).textContent = text;
  $("#go").disabled = stateName !== "ready";
}

const enginePromise = (async () => {
  try {
    if (typeof loadPyodide !== "function") throw new Error("Pyodide failed to load (offline?)");
    setEngine("loading", "Loading engine…");
    const py = await loadPyodide();
    setEngine("loading", "Loading maths libraries…");
    await py.loadPackage(["numpy", "pandas", "pydantic"]);
    const files = await (await fetch("py/files.json", { cache: "no-cache" })).json();
    py.FS.mkdirTree("/home/pyodide/race_strategy");
    await Promise.all(
      files.map(async (f) => {
        const resp = await fetch(`py/race_strategy/${f}`, { cache: "no-cache" });
        if (!resp.ok) throw new Error(`Missing engine file ${f}`);
        py.FS.writeFile(`/home/pyodide/race_strategy/${f}`, await resp.text());
      }),
    );
    py.runPython("import sys\nif '/home/pyodide' not in sys.path: sys.path.insert(0, '/home/pyodide')");
    const web = py.pyimport("race_strategy.web");
    setEngine("ready", "Engine ready");
    return web;
  } catch (err) {
    console.error(err);
    setEngine("error", "Engine failed — reload");
    throw err;
  }
})();
enginePromise.catch(() => {}); // surfaced via the status pill and on submit

// ----------------------------------------------------------------- persistence
function saveInputs() {
  try {
    const data = {};
    for (const el of $("#form").elements) {
      if (!el.name) continue;
      if (el.type === "radio") { if (el.checked) data[el.name] = el.value; } else data[el.name] = el.value;
    }
    const stored = { fields: data, gpxName: state.gpxName };
    if (state.gpxText && state.gpxText.length < MAX_STORED_GPX) stored.gpxText = state.gpxText;
    localStorage.setItem(STORE_KEY, JSON.stringify(stored));
  } catch { /* storage unavailable or full: non-critical */ }
}
function restoreInputs() {
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch { stored = null; }
  const form = $("#form");
  if (stored && stored.fields) {
    for (const [name, value] of Object.entries(stored.fields)) {
      const els = form.elements[name];
      if (!els) continue;
      if (els instanceof RadioNodeList) { for (const r of els) r.checked = r.value === value; } else els.value = value;
    }
  }
  if (!form.elements.start_time.value) form.elements.start_time.value = defaultStart();
  if (stored && stored.gpxText) setGpx(stored.gpxText, stored.gpxName || "Saved course");
  applySport();
}

// ----------------------------------------------------------------- course input
function setGpx(text, name) {
  state.gpxText = text;
  state.gpxName = name;
  $("#gpx-name").textContent = name;
  const pts = (text.match(/<(trkpt|rtept)\b/g) || []).length;
  $("#gpx-hint").textContent = pts ? `${fmtNum(pts)} points · tap to change` : "No track points found";
  $("#drop").classList.toggle("has-file", pts > 0);
}
async function readFile(file) {
  if (!file) return;
  if (file.size > 25 * 1024 * 1024) return showErrors([{ field: "gpx", message: "GPX file is larger than 25 MB" }]);
  setGpx(await file.text(), file.name);
  clearErrors();
  saveInputs();
}

function applySport() {
  const sport = $("#form").elements.sport.value;
  for (const el of $$("[data-sport]", $("#form"))) {
    if (el.classList.contains("chip")) continue;
    el.hidden = el.dataset.sport !== sport;
  }
}

// ----------------------------------------------------------------- payload
function buildPayload() {
  const f = $("#form").elements;
  const errors = [];
  const num = (name, { optional = false } = {}) => {
    const raw = f[name].value.trim();
    if (raw === "") {
      if (!optional) errors.push({ field: name, message: "is required" });
      return undefined;
    }
    const v = Number(raw.replace(",", "."));
    if (!Number.isFinite(v)) errors.push({ field: name, message: "must be a number" });
    return v;
  };
  const sport = f.sport.value;
  const athlete = {
    sport,
    weight_kg: num("weight_kg"),
    max_hr: num("max_hr"),
    threshold_hr: num("threshold_hr", { optional: true }),
    target_carbs_per_hour: num("target_carbs_per_hour"),
    sweat_rate_l_hr: num("sweat_rate_l_hr"),
  };
  if (sport === "run") {
    const pace = parseMinSec(f.threshold_pace.value);
    if (pace === null) errors.push({ field: "threshold_pace", message: "use min:ss, e.g. 4:30" });
    athlete.threshold_pace_sec_km = pace ?? undefined;
  } else {
    athlete.threshold_power_watts = num("threshold_power_watts");
  }
  const windDir = f.wind_direction_deg.value;
  const weather = {
    start_time: f.start_time.value || undefined,
    temperature_c: num("temperature_c"),
    temperature_end_c: num("temperature_end_c", { optional: true }),
    humidity_pct: num("humidity_pct"),
    wind_speed_kmh: num("wind_speed_kmh", { optional: true }) ?? 0,
    wind_direction_deg: windDir === "" ? undefined : Number(windDir),
  };
  if (!weather.start_time) errors.push({ field: "start_time", message: "is required" });

  const target = parseHms(f.target_time.value);
  if (target === null) errors.push({ field: "target_time", message: "use h:mm:ss, e.g. 3:45:00" });
  const aid = f.aid_stations.value.trim();
  const aidKm = aid ? aid.split(/[,;\s]+/).filter(Boolean).map((x) => Number(x.replace(",", "."))) : [];
  if (aidKm.some((x) => !Number.isFinite(x) || x <= 0)) errors.push({ field: "aid_stations", message: "list distances like 5, 10, 15" });

  const strategy = {
    target_finish_time_s: target || undefined,
    pacing: f.pacing.value,
    aid_stations_km: aidKm,
    fueling: {
      gel_name: f.gel_name.value.trim() || "Gel",
      gel_carbs_g: num("gel_carbs_g"),
      drink_name: f.drink_name.value.trim() || "Drink",
      drink_carbs_g_per_l: num("drink_carbs_g_per_l", { optional: true }) ?? 0,
    },
  };
  if (!state.gpxText) errors.push({ field: "gpx", message: "choose a course GPX first" });
  return { payload: JSON.parse(JSON.stringify({ athlete, weather, strategy })), errors };
}

// ----------------------------------------------------------------- errors
const FIELD_LABELS = {
  gpx: "Course", threshold_pace: "Threshold pace", threshold_pace_sec_km: "Threshold pace",
  threshold_power_watts: "FTP", weight_kg: "Weight", max_hr: "Max HR", threshold_hr: "Threshold HR",
  target_carbs_per_hour: "Carbs target", sweat_rate_l_hr: "Sweat rate", start_time: "Start",
  temperature_c: "Temp at start", temperature_end_c: "Temp at finish", humidity_pct: "Humidity",
  wind_speed_kmh: "Wind", target_time: "Goal time", target_finish_time_s: "Goal time",
  aid_stations: "Aid stations", aid_stations_km: "Aid stations", gel_carbs_g: "Carbs per gel",
  drink_carbs_g_per_l: "Drink carbs",
};
const FORM_NAME = { threshold_pace_sec_km: "threshold_pace", target_finish_time_s: "target_time", aid_stations_km: "aid_stations" };

function clearErrors() {
  $("#errors").hidden = true;
  for (const el of $$(".field.is-invalid")) el.classList.remove("is-invalid");
}
function showErrors(errors) {
  clearErrors();
  const items = errors.map((e) => {
    const key = String(e.field).split(".").filter((p) => isNaN(p)).pop();
    const formName = FORM_NAME[key] || key;
    const input = $("#form").elements[formName];
    if (input && input.closest) input.closest(".field")?.classList.add("is-invalid");
    const label = FIELD_LABELS[key] || key;
    const msg = String(e.message).replace(/^Value error,\s*/, "");
    return `<li><b>${esc(label)}</b>: ${esc(msg)}</li>`;
  });
  const box = $("#errors");
  box.innerHTML = `<strong>Check these</strong><ul>${items.join("")}</ul>`;
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ----------------------------------------------------------------- render
function render(result) {
  const bp = result.blueprint;
  const isRun = bp.athlete.sport === "run";
  const n = bp.nutrition;
  const c = bp.course;
  const hasTarget = bp.strategy.target_finish_time_s != null;

  $("#r-kicker").textContent = hasTarget ? "Goal finish" : "Predicted finish";
  $("#r-time").textContent = bp.predicted_finish_label;
  const sustain = hasTarget ? ` · model estimate ${fmtDuration(bp.sustainable_finish_s)}` : "";
  $("#r-sub").textContent = `${fmtNum(c.total_distance_km, 2)} km · +${fmtNum(c.elevation_gain_m)} m / −${fmtNum(c.elevation_loss_m)} m${sustain}`;

  const avg = isRun
    ? [`Avg pace`, `${fmtPace(bp.predicted_finish_s / c.total_distance_km)}<small>/km</small>`]
    : [`Avg speed`, `${fmtNum(c.total_distance_km / (bp.predicted_finish_s / 3600), 1)}<small>km/h</small>`];
  const stats = [
    avg,
    ["Feels like", `${fmtNum(bp.environment.apparent_temp_start_c)}–${fmtNum(bp.environment.apparent_temp_end_c)}<small>°C</small>`],
    ["Gels", `${n.gels_total}`],
    ["Carbs", `${fmtNum(n.carbs_total_g)}<small>g</small>`],
    ["Fluid", `${fmtNum(n.fluid_total_ml / 1000, 2)}<small>L</small>`],
    ["Body-mass loss", `${fmtNum(n.est_body_mass_loss_pct, 1)}<small>%</small>`],
  ];
  $("#r-stats").innerHTML = stats.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");

  $("#r-profile").innerHTML = profileSvg(result.profile, bp);

  const w = $("#r-warnings");
  w.hidden = !bp.warnings.length;
  w.innerHTML = bp.warnings.length ? `<strong>Heads up</strong><ul>${bp.warnings.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";

  $("#r-pacing").innerHTML = bp.segments.map((s) => segCard(s, isRun)).join("");
  $("#r-fueling").innerHTML = fuelList(bp);
  $("#r-splits").innerHTML = splitsTable(bp, isRun);

  $("#results").hidden = false;
  selectTab("pacing");
  $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
}

const TERRAIN_VAR = { Flat: "var(--flat)", Ascent: "var(--ascent)", Descent: "var(--descent)" };
const TERRAIN_NAME = { Flat: "Flat", Ascent: "Climb", Descent: "Descent" };

function segCard(s, isRun) {
  const target = isRun
    ? `${esc(fmtPace(s.target_pace_sec_km))}<small>per km</small>`
    : `${s.target_power_watts} W<small>${fmtNum(s.target_speed_kmh, 1)} km/h</small>`;
  const note = s.guidance.split(": ").slice(1).join(": ") || s.guidance;
  return `<article class="seg-card">
    <span class="seg-card__bar" style="background:${TERRAIN_VAR[s.terrain]}"></span>
    <div class="seg-card__meta"><b>${TERRAIN_NAME[s.terrain]}</b> · km ${fmtNum(s.start_km, 1)}–${fmtNum(s.end_km, 1)} · ${s.avg_grade_pct > 0 ? "+" : ""}${fmtNum(s.avg_grade_pct, 1)}% · from ${fmtDuration(s.est_start_s)}</div>
    <div class="seg-card__target">${target}</div>
    <div class="seg-card__hr">HR ${s.hr_target_low}–${s.hr_target_high} · <span class="cap">cap ${s.hr_cap}</span></div>
    <p class="seg-card__note">${esc(note)}</p>
  </article>`;
}

function fuelList(bp) {
  const ev = bp.fueling_events;
  const n = bp.nutrition;
  if (!ev.length) return `<p class="empty">No fueling needed for an effort this short. Start well hydrated.</p>`;
  const fp = bp.strategy.fueling;
  const items = ev.map((e, i) => {
    const clock = new Date(e.clock_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const pills = [];
    if (e.gels) pills.push(`${e.gels} × ${esc(fp.gel_name)}`);
    if (e.fluid_ml) pills.push(`${e.fluid_ml} ml ${esc(fp.drink_name)}`);
    pills.push(`${fmtNum(e.carbs_g)} g carbs`);
    if (e.sodium_mg) pills.push(`${e.sodium_mg} mg sodium`);
    const note = (/\(([^)]+)\)\s*$/.exec(e.message) || [])[1];
    return `<li><span class="fuel__n">${i + 1}</span><div>
      <div class="fuel__when">${esc(e.elapsed_label)}<span>km ${fmtNum(e.distance_km, 1)} · ${esc(clock)}</span></div>
      <div class="fuel__what">${pills.map((p) => `<span class="pill">${p}</span>`).join("")}</div>
      ${note ? `<div class="fuel__note">${esc(note)}</div>` : ""}
    </div></li>`;
  });
  return `<ol class="fuel">${items.join("")}</ol>
    <div class="fuel-sum">
      <div><b>${fmtNum(n.carbs_per_hour_target)} g/h</b>carb target</div>
      <div><b>${fmtNum(n.fluid_avg_l_hr, 2)} L/h</b>avg drinking</div>
      <div><b>${fmtNum(n.sweat_rate_avg_l_hr, 2)} L/h</b>est. sweat rate</div>
      <div><b>${fmtNum(n.sodium_total_mg)} mg</b>sodium total</div>
    </div>`;
}

function splitsTable(bp, isRun) {
  const sp = bp.splits;
  const full = sp.filter((s, i) => i < sp.length - 1 || sp.length === 1);
  const sorted = full.map((s) => s.split_s).sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)] || 0;
  const rows = sp.map((s, i) => {
    const len = i === 0 ? s.end_km : s.end_km - sp[i - 1].end_km;
    const perKm = s.split_s / len;
    const cls = i < sp.length - 1 && median ? (s.split_s > median * 1.04 ? "slow" : s.split_s < median * 0.96 ? "fast" : "") : "";
    const rate = isRun ? `${fmtPace(perKm)}/km` : `${fmtNum(len / (s.split_s / 3600), 1)} km/h`;
    return `<tr><td>${fmtNum(s.end_km, s.end_km % 1 ? 2 : 0)}</td><td class="${cls}">${esc(s.split_label)}</td><td>${rate}</td><td>${esc(s.cumulative_label)}</td></tr>`;
  });
  return `<table class="splits"><thead><tr><th>km</th><th>Split</th><th>${isRun ? "Pace" : "Speed"}</th><th>Total</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

// Elevation profile: terrain-coloured areas + numbered fuel pins.
function profileSvg(profile, bp) {
  const W = 1000, H = 300, L = 44, R = 14, T = 44, B = 28;
  const kms = profile.map((p) => p[0]);
  const eles = profile.map((p) => p[1]);
  const maxKm = kms[kms.length - 1];
  let lo = Math.min(...eles), hi = Math.max(...eles);
  if (hi - lo < 30) { const mid = (hi + lo) / 2; lo = mid - 15; hi = mid + 15; }
  const padE = (hi - lo) * 0.08; lo -= padE; hi += padE;
  const x = (km) => L + (km / maxKm) * (W - L - R);
  const y = (e) => T + (1 - (e - lo) / (hi - lo)) * (H - T - B);
  const eleAt = (km) => {
    let i = kms.findIndex((k) => k >= km);
    if (i <= 0) return eles[Math.max(0, i)];
    const f = (km - kms[i - 1]) / (kms[i] - kms[i - 1] || 1);
    return eles[i - 1] + f * (eles[i] - eles[i - 1]);
  };

  const parts = [];
  // grid
  for (let k = 0; k <= 3; k++) {
    const e = lo + ((hi - lo) * k) / 3;
    parts.push(`<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(e)}" y2="${y(e)}"/>`);
    parts.push(`<text x="${L - 6}" y="${y(e) + 4}" text-anchor="end">${Math.round(e)}</text>`);
  }
  const steps = [1, 2, 5, 10, 20, 25, 50, 100];
  const step = steps.find((s) => maxKm / s <= 8) || 100;
  for (let k = 0; k <= maxKm + 1e-9; k += step) parts.push(`<text x="${x(k)}" y="${H - 8}" text-anchor="middle">${k}</text>`);
  parts.push(`<text x="${W - R}" y="${H - 8}" text-anchor="end">km</text>`);

  // terrain areas
  for (const s of bp.segments) {
    const pts = [[s.start_km, eleAt(s.start_km)], ...profile.filter((p) => p[0] > s.start_km && p[0] < s.end_km), [s.end_km, eleAt(s.end_km)]];
    const top = pts.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" L");
    const base = H - B;
    parts.push(`<path d="M${x(s.start_km).toFixed(1)},${base} L${top} L${x(s.end_km).toFixed(1)},${base} Z" fill="${TERRAIN_VAR[s.terrain]}" opacity=".9"/>`);
  }
  // outline
  parts.push(`<path d="M${profile.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" L")}" fill="none" stroke="var(--ink)" stroke-width="1.5"/>`);

  // fuel pins (stagger rows when they would overlap)
  let lastX = -Infinity, row = 0;
  bp.fueling_events.forEach((e, i) => {
    const px = x(e.distance_km);
    row = px - lastX < 26 ? (row + 1) % 2 : 0;
    lastX = px;
    const cy = 16 + row * 22;
    parts.push(`<g class="fuel-pin"><line x1="${px}" x2="${px}" y1="${cy}" y2="${y(eleAt(e.distance_km))}" stroke="var(--fuel)" stroke-width="1.2" stroke-dasharray="2 3"/><circle cx="${px}" cy="${cy}" r="10"/><text x="${px}" y="${cy + 4}" text-anchor="middle">${i + 1}</text></g>`);
  });

  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Elevation profile with ${bp.fueling_events.length} fueling points">${parts.join("")}</svg>`;
}

function selectTab(name) {
  for (const b of $$(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const p of $$(".panel")) p.hidden = p.dataset.panel !== name;
}

// ----------------------------------------------------------------- wiring
function init() {
  restoreInputs();
  const form = $("#form");

  $("#gpx").addEventListener("change", (e) => readFile(e.target.files[0]));
  const drop = $("#drop");
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("is-over")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); readFile(e.dataTransfer.files[0]); });

  for (const chip of $$(".chip[data-sample]")) {
    chip.addEventListener("click", async () => {
      try {
        const resp = await fetch(`samples/${chip.dataset.sample}`);
        if (!resp.ok) throw new Error(resp.statusText);
        setGpx(await resp.text(), chip.textContent.trim());
        form.elements.sport.value = chip.dataset.sport;
        applySport();
        clearErrors();
        saveInputs();
      } catch (err) {
        showErrors([{ field: "gpx", message: `could not load sample (${err.message})` }]);
      }
    });
  }

  form.addEventListener("change", (e) => { if (e.target.name === "sport") applySport(); saveInputs(); });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const { payload, errors } = buildPayload();
    if (errors.length) return showErrors(errors);
    clearErrors();
    const btn = $("#go");
    const label = $(".btn__label", btn);
    btn.disabled = true;
    label.textContent = "Calculating…";
    try {
      const web = await enginePromise;
      await new Promise((r) => setTimeout(r, 30)); // let the button repaint
      const result = JSON.parse(web.run(state.gpxText, JSON.stringify(payload)));
      if (!result.ok) return showErrors(result.errors);
      state.result = result;
      render(result);
    } catch (err) {
      console.error(err);
      showErrors([{ field: "engine", message: err.message || String(err) }]);
    } finally {
      btn.disabled = $("#engine").dataset.state !== "ready";
      label.textContent = "Build my race plan";
    }
  });

  for (const b of $$(".tabs button")) b.addEventListener("click", () => selectTab(b.dataset.tab));
  $("#edit").addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
  $("#download").addEventListener("click", () => {
    if (!state.result) return;
    const blob = new Blob([JSON.stringify(state.result.blueprint, null, 2)], { type: "application/json" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "race-blueprint.json" });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
}

init();
