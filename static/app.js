// Call Desk front end: one EventSource, a transcript pane, and a flag feed.
// All server text goes through textContent (never innerHTML).

const $ = (sel) => document.querySelector(sel);
const ET = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const INTEGRATIONS = [["gemini", "Gemini"], ["nimble", "Nimble"], ["tiger", "Tiger"], ["snowflake", "Snowflake"], ["sentry", "Sentry"]];
const BADGE = { cut: "CUT", raise: "RAISE", reaffirm: "REAFFIRM", new: "NEW", "n/a": "NOTE" };
const EXEC = /(CEO|CFO|Chief|President|Chair|IR|Investor Relations)/i;

const state = {
  mode: "live",
  running: false,
  duration: 0,
  clipStart: null, // Date of clip start on the real call clock
  runStartedAt: 0,
  flags: new Map(),
  lines: [],
  hasAudio: false,
  lastOffset: 0,
};

const audio = $("#audio");

// ---------- helpers ----------
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const fmtET = (iso) => ET.format(new Date(iso));
const signed = (v, d = 2) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(d)}`);
const dirClass = (d) => (d === "n/a" ? "na" : d || "na");

// ---------- integration pills ----------
function renderPills(modes = {}) {
  const ul = $("#pills");
  ul.replaceChildren();
  for (const [key, label] of INTEGRATIONS) {
    const mode = modes[key] || "mock";
    const li = el("li", `pill ${mode}`);
    li.title = mode === "live" ? `${label}: live` : `${label}: mock data (key missing or MOCK_ flag set)`;
    li.append(el("span", null, label.toUpperCase()), el("span", "pill-mode", mode.toUpperCase()));
    ul.append(li);
  }
}

function setMode(mode) {
  state.mode = mode;
  for (const b of document.querySelectorAll(".mode button")) b.setAttribute("aria-checked", String(b.dataset.mode === mode));
}

// ---------- clock + progress ----------
function currentOffset() {
  if (state.hasAudio && !audio.paused) state.lastOffset = audio.currentTime;
  else if (state.running) state.lastOffset = ((performance.now() - state.runStartedAt) / 1000) * (state.speed || 1);
  return state.lastOffset;
}

function tick() {
  const t = Math.min(currentOffset(), state.duration || Infinity);
  if (state.clipStart) $("#clock").textContent = ET.format(new Date(state.clipStart.getTime() + t * 1000));
  $("#elapsed").textContent = fmtTime(t || 0);
  if (state.clipStart && state.duration) {
    const end = new Date(state.clipStart.getTime() + state.duration * 1000);
    $("#clip-range").textContent = `Clip of the live call · ${ET.format(state.clipStart)} → ${ET.format(end)} ET`;
  }
  if (state.duration) $("#progress-fill").style.width = `${Math.min(100, (t / state.duration) * 100)}%`;
  requestAnimationFrame(tick);
}

function addMarker(flag) {
  if (!state.duration || document.getElementById(`m-${flag.id}`)) return;
  const m = el("span", `marker ${dirClass(flag.direction)}`);
  m.id = `m-${flag.id}`;
  m.style.left = `${(flag.t_offset / state.duration) * 100}%`;
  m.title = flag.metric_label;
  $("#markers").append(m);
}

// ---------- transcript ----------
function addLine(seg) {
  $("#transcript-empty").hidden = true;
  const li = el("li", "line");
  li.append(el("span", "line-time", fmtET(seg.call_ts)));
  const body = el("div");
  body.append(el("span", `line-speaker${EXEC.test(seg.speaker) ? " exec" : ""}`, seg.speaker), el("span", "line-text", seg.text));
  li.append(body);
  const pane = $("#transcript");
  const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120;
  pane.append(li);
  state.lines.push({ li, text: seg.text });
  if (atBottom) pane.scrollTop = pane.scrollHeight;
}

function highlightQuote(quote, direction) {
  if (!quote) return;
  const needle = quote.toLowerCase().replace(/[.,]$/, "");
  for (let i = state.lines.length - 1; i >= Math.max(0, state.lines.length - 6); i--) {
    const { li, text } = state.lines[i];
    const idx = text.toLowerCase().indexOf(needle);
    if (idx < 0) continue;
    const span = li.querySelector(".line-text");
    span.replaceChildren(text.slice(0, idx), el("mark", dirClass(direction), text.slice(idx, idx + needle.length)), text.slice(idx + needle.length));
    return;
  }
}

// ---------- flags ----------
function sparkline(svg, pr) {
  svg.replaceChildren();
  const pts = pr.series || [];
  if (pts.length < 2) return;
  const W = 240, H = 56, P = 4;
  const times = pts.map((p) => new Date(p.ts).getTime());
  const vals = pts.flatMap((p) => [p.pct, p.bench_pct]).concat([0]);
  const t0 = times[0], t1 = times[times.length - 1];
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const x = (t) => P + ((t - t0) / Math.max(1, t1 - t0)) * (W - 2 * P);
  const y = (v) => H - P - ((v - lo) / Math.max(0.01, hi - lo)) * (H - 2 * P);
  const ns = "http://www.w3.org/2000/svg";
  const mk = (tag, attrs) => { const n = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); svg.append(n); return n; };
  if (pr.since_ts) {
    const xs = x(new Date(pr.since_ts).getTime());
    const xe = pr.until_ts ? x(new Date(pr.until_ts).getTime()) : xs + 1;
    mk("rect", { class: "window", x: xs, y: 0, width: Math.max(1, xe - xs), height: H });
    mk("line", { class: "quote-mark", x1: xs, x2: xs, y1: 0, y2: H });
  }
  mk("line", { class: "zero", x1: P, x2: W - P, y1: y(0), y2: y(0) });
  const path = (key) => pts.map((p, i) => `${i ? "L" : "M"}${x(times[i]).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  mk("path", { class: "bench", d: path("bench_pct") });
  mk("path", { class: "main", d: path("pct") });
  svg.setAttribute("aria-label", `${pr.ticker} versus ${pr.benchmark}, percent change from prior close`);
}

function renderFlag(f) {
  let node = document.getElementById(`flag-${f.id}`);
  const isNew = !node;
  if (isNew) {
    node = $("#flag-template").content.firstElementChild.cloneNode(true);
    node.id = `flag-${f.id}`;
    node.addEventListener("click", (e) => {
      if (e.target.closest("a, .detail")) return;
      toggleDetail(node);
    });
    $("#feed").prepend(node);
    $("#feed-empty").hidden = true;
    node.classList.add("fresh");
    setTimeout(() => node.classList.remove("fresh"), 2500);
  }
  node.className = `flag ${dirClass(f.direction)}${node.classList.contains("fresh") ? " fresh" : ""}`;
  node.querySelector(".badge").textContent = BADGE[f.direction] || f.direction.toUpperCase();
  node.querySelector(".metric").textContent = f.metric_label;
  node.querySelector(".flag-time").textContent = `${fmtET(f.call_ts)} ET`;
  const prior = node.querySelector(".prior");
  prior.textContent = f.prior_value || "no prior";
  prior.classList.toggle("none", !f.prior_value);
  const newEl = node.querySelector(".new");
  newEl.textContent = f.new_value || (f.status === "checked" ? "not stated" : "finding the number…");
  newEl.classList.toggle("pending", !f.new_value);
  const vs = node.querySelector(".value-source");
  vs.hidden = !f.new_value || !f.new_value_source || f.new_value_source === "spoken";
  vs.textContent = vs.hidden ? "" : `not spoken · ${f.new_value_source}`;
  renderRelated(node, f);
  node.querySelector(".speaker").textContent = f.speaker;
  node.querySelector(".quote").textContent = `“${f.quote}”`;
  node.querySelector(".what").textContent = f.what_changed || "";
  node.querySelector(".why").textContent = f.why_it_matters || "";

  const checking = f.status !== "checked";
  node.querySelector(".checking").hidden = !checking;

  const pr = f.price_reaction;
  const market = node.querySelector(".market");
  market.hidden = !pr;
  if (pr) {
    sparkline(node.querySelector(".spark"), pr);
    const stats = node.querySelector(".market-stats");
    stats.replaceChildren();
    const ab = el("div", `big ${pr.vs_benchmark >= 0 ? "pos" : "neg"}`, `${signed(pr.vs_benchmark)}pp`);
    stats.append(
      el("div", "lbl", `${pr.ticker} VS ${pr.benchmark} · ${pr.window} AFTER QUOTE`),
      ab,
      el("div", null, `${pr.ticker} ${signed(pr.pct_move)}%  ${pr.benchmark} ${signed(pr.benchmark_pct_move)}%`),
    );
    if (pr.close_pct_move !== null && pr.close_pct_move !== undefined) {
      const close = el("div", "close-line");
      close.append(el("span", "lbl", "BY CLOSE "), el("span", pr.close_pct_move >= 0 ? "pos" : "neg", `${pr.ticker} ${signed(pr.close_pct_move)}%`),
        el("span", "lbl", ` · `), el("span", pr.close_vs_benchmark >= 0 ? "pos" : "neg", `${signed(pr.close_vs_benchmark)}pp`), el("span", "lbl", ` vs ${pr.benchmark}`));
      stats.append(close);
    }
    if (pr.mock) stats.firstChild.append(el("span", "mock-tag", "MOCK"));
  }

  const conf = node.querySelector(".conf");
  conf.hidden = checking || f.confidence === null || f.confidence === undefined;
  if (!conf.hidden) {
    node.querySelector(".conf-fill").style.width = `${Math.round(f.confidence * 100)}%`;
    node.querySelector(".conf-value").textContent = f.confidence.toFixed(2);
  }
  renderDetail(node, f);
  if (f.status === "checked") addMarker(f);
  updateTally();
}

function renderRelated(node, f) {
  const table = node.querySelector(".related");
  const rows = f.related_changes || [];
  table.hidden = !rows.length;
  const body = table.querySelector("tbody");
  body.replaceChildren();
  for (const r of rows) {
    const tr = el("tr");
    tr.append(
      el("td", null, r.metric_label),
      el("td", "r-prior", r.prior_value || "—"),
      el("td", null, `→ ${r.new_value}`),
      el("td", `r-dir ${dirClass(r.direction)}`, BADGE[r.direction] || r.direction),
    );
    body.append(tr);
  }
}

function toggleDetail(node, open) {
  const detail = node.querySelector(".detail");
  const btn = node.querySelector(".expand");
  const show = open ?? detail.hidden;
  detail.hidden = !show;
  btn.setAttribute("aria-expanded", String(show));
}

function renderDetail(node, f) {
  const ev = node.querySelector(".evidence");
  ev.replaceChildren();
  for (const e of f.evidence || []) {
    const li = el("li", "ev");
    const head = el("div", "ev-head");
    head.append(el("span", `ev-stance ${e.stance}`, e.stance), el("span", null, e.source));
    if (e.mock) head.append(el("span", "mock-tag", "MOCK"));
    li.append(head, el("p", "ev-snippet", e.snippet));
    if (/^https?:\/\//.test(e.ref)) {
      const a = el("a", null, e.ref);
      a.href = e.ref; a.target = "_blank"; a.rel = "noopener noreferrer";
      li.append(a);
    } else if (e.ref) li.append(el("div", "trace", e.ref));
    ev.append(li);
  }
  if (!ev.children.length) ev.append(el("li", "muted", f.status === "checked" ? "No evidence returned." : "Gathering evidence…"));

  const comps = node.querySelector(".components");
  comps.replaceChildren();
  const c = f.confidence_components;
  if (c) {
    for (const [k, w] of [["prior", 0.45], ["web", 0.35], ["price", 0.2]]) {
      const li = el("li", "comp");
      const bar = el("span", "comp-bar");
      const fill = el("span", "comp-fill");
      fill.style.width = `${Math.round(c[k] * 100)}%`;
      bar.append(fill);
      li.append(el("span", null, `${k} ×${w}`), bar, el("span", null, c[k].toFixed(1)));
      comps.append(li);
    }
  }
  node.querySelector(".rationale").textContent = f.confidence_rationale || "";

  const tcs = node.querySelector(".toolcalls");
  tcs.replaceChildren();
  for (const t of f.tool_calls || []) {
    const li = el("li", "tc");
    li.append(el("span", t.ok ? "ok" : "fail", t.ok ? "✓" : "✗"), el("span", null, `${t.tool} → ${t.backend}`), el("span", null, `${t.ms}ms`));
    if (t.mock) li.append(el("span", "mock-tag", "MOCK"));
    if (t.error) li.title = t.error;
    tcs.append(li);
  }
  if (!tcs.children.length) tcs.append(el("li", "muted", "Running…"));

  const trace = node.querySelector(".trace");
  trace.replaceChildren();
  if (f.sentry_trace_url) {
    const a = el("a", null, `Open trace ${f.sentry_trace_id.slice(0, 12)}… in Sentry`);
    a.href = f.sentry_trace_url; a.target = "_blank"; a.rel = "noopener noreferrer";
    trace.append(a);
  } else if (f.sentry_trace_id) {
    trace.append(`trace_id ${f.sentry_trace_id}`, el("span", "mock-tag", "LOCAL"));
  }
}

function updateTally() {
  const counts = {};
  for (const n of document.querySelectorAll(".feed .flag")) {
    const d = [...n.classList].find((c) => ["cut", "raise", "reaffirm", "new", "na"].includes(c)) || "na";
    counts[d] = (counts[d] || 0) + 1;
  }
  const t = $("#tally");
  t.replaceChildren();
  for (const [d, label] of [["cut", "CUT"], ["raise", "RAISE"], ["reaffirm", "REAFFIRM"], ["new", "NEW"], ["na", "NOTE"]]) {
    if (counts[d]) t.append(el("span", `t-${d}`, `${counts[d]} ${label}`));
  }
}

// ---------- run control ----------
function resetUI() {
  $("#transcript").replaceChildren();
  $("#feed").replaceChildren();
  $("#markers").replaceChildren();
  $("#transcript-empty").hidden = false;
  $("#feed-empty").hidden = false;
  state.flags.clear();
  state.lines = [];
  state.lastOffset = 0;
  updateTally();
}

function setRunning(on, label) {
  state.running = on;
  $("#play").classList.toggle("running", on);
  $("#play-label").textContent = on ? "Stop" : "Play call";
  $("#live-dot").classList.toggle("on", on);
  $("#audio-state").textContent = label || (on ? (state.mode === "replay" ? "REPLAY" : "LIVE") : "READY");
}

async function play() {
  if (state.running) {
    await fetch("/api/stop", { method: "POST" });
    audio.pause();
    setRunning(false, "STOPPED");
    return;
  }
  const res = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: state.mode }) });
  if (!res.ok) {
    $("#audio-state").textContent = (await res.json()).detail || "ERROR";
    return;
  }
  state.runStartedAt = performance.now();
  setRunning(true);
  if (state.hasAudio) {
    audio.currentTime = 0;
    audio.play().catch(() => { $("#audio-state").textContent = "AUDIO BLOCKED — CLICK PLAY AGAIN"; });
  }
}

// ---------- SSE ----------
function connect() {
  const es = new EventSource("/api/events");
  es.addEventListener("reset", () => resetUI());
  es.addEventListener("status", (e) => {
    const s = JSON.parse(e.data);
    if (s.integrations) renderPills(s.integrations);
    if (s.speed) state.speed = s.speed;
    if (s.duration) { state.duration = s.duration; $("#duration").textContent = fmtTime(s.duration); }
    if (s.call?.clip_start) state.clipStart = new Date(s.call.clip_start);
    if (s.transcriber) $("#transcriber").textContent = s.transcriber === "gemini" ? `Gemini · ${s.chunk_s}s chunks` : "MOCK transcript (no Gemini key)";
    if (s.state === "running") { if (!state.running) state.runStartedAt = performance.now() - s.t * 1000; setRunning(true); }
    if (s.state === "running" && s.speed && s.speed !== 1 && state.hasAudio) audio.playbackRate = Math.min(4, s.speed);
    if (s.state === "done") setRunning(false, s.mode === "replay" ? "REPLAY DONE" : "CALL ENDED");
    if (s.state === "stopped" || s.state === "error") setRunning(false, s.state.toUpperCase());
  });
  es.addEventListener("transcript", (e) => addLine(JSON.parse(e.data)));
  es.addEventListener("claim", (e) => {
    const c = JSON.parse(e.data);
    if (c.accepted) highlightQuote(c.quote, null);
  });
  es.addEventListener("flag", (e) => {
    const f = JSON.parse(e.data);
    state.flags.set(f.id, f);
    renderFlag(f);
    highlightQuote(f.quote, f.direction);
  });
  es.addEventListener("error", (e) => {
    if (e.data) console.warn("pipeline error", e.data);
  });
}

async function init() {
  const s = await (await fetch("/api/state")).json();
  renderPills(s.integrations);
  setMode(s.default_mode);
  state.clipStart = new Date(s.call.clip_start);
  state.hasAudio = s.audio;
  $("#ticker").textContent = s.call.ticker;
  $("#call-title").textContent = s.call.title;
  document.querySelector('.mode button[data-mode="replay"]').disabled = !s.replay_available;
  if (!s.audio) $("#audio-state").textContent = "NO AUDIO FILE — MOCK CLOCK";
  audio.addEventListener("loadedmetadata", () => {
    if (!state.duration) { state.duration = audio.duration; $("#duration").textContent = fmtTime(audio.duration); }
  });
  for (const b of document.querySelectorAll(".mode button")) b.addEventListener("click", () => !b.disabled && setMode(b.dataset.mode));
  $("#play").addEventListener("click", play);
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".expand");
    if (btn) { e.stopPropagation(); toggleDetail(btn.closest(".flag")); }
  }, true);
  connect();
  requestAnimationFrame(tick);
}

init();
