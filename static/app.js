// Call Desk front end: one EventSource, a transcript pane, and a flag feed.
// All server text goes through textContent (never innerHTML).
// Feature modules (static/features/*.js) each own one sponsor feature; app.js only calls their exports.

import { renderTigerExtra } from "./features/tiger.js";
import { onFlagVoice } from "./features/voice.js";
import { renderBrief, resetBrief } from "./features/brief.js";
import { initSentryBrowser, traceFlagRender } from "./features/sentry.js";

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
  else if (state.running) state.lastOffset = (state.startS || 0) + ((performance.now() - state.runStartedAt) / 1000) * (state.speed || 1);
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
  state.lines.push({ li, text: seg.text, marks: [] });
  if (atBottom) pane.scrollTop = pane.scrollHeight;
}

function highlightQuote(quote, direction) {
  if (!quote) return;
  const needle = quote.toLowerCase().replace(/[.,]$/, "");
  for (let i = state.lines.length - 1; i >= Math.max(0, state.lines.length - 8); i--) {
    const line = state.lines[i];
    const start = line.text.toLowerCase().indexOf(needle);
    if (start < 0) continue;
    const end = start + needle.length;
    const existing = line.marks.find((m) => m.start === start && m.end === end);
    if (existing) existing.dir = direction || existing.dir;
    else if (!line.marks.some((m) => start < m.end && end > m.start)) line.marks.push({ start, end, dir: direction });
    renderMarks(line);
    return;
  }
}

function renderMarks(line) {
  const span = line.li.querySelector(".line-text");
  const parts = [];
  let pos = 0;
  for (const m of [...line.marks].sort((a, b) => a.start - b.start)) {
    parts.push(line.text.slice(pos, m.start), el("mark", dirClass(m.dir), line.text.slice(m.start, m.end)));
    pos = m.end;
  }
  parts.push(line.text.slice(pos));
  span.replaceChildren(...parts);
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
  const timeEl = node.querySelector(".flag-time");
  timeEl.replaceChildren(`${fmtET(f.call_ts)} ET`);
  if (f.latency_s !== null && f.latency_s !== undefined) {
    const lat = el("span", "latency", ` · flagged +${f.latency_s.toFixed(1)}s`);
    if (f.checked_latency_s !== null && f.checked_latency_s !== undefined) lat.textContent += ` · checked +${f.checked_latency_s.toFixed(1)}s`;
    lat.title = "Seconds after the quote was spoken";
    timeEl.append(lat);
  }
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
  renderTrajectory(node, f);
  node.querySelector(".degraded-tag").hidden = !f.degraded;
  node.querySelector(".speaker").textContent = f.speaker;
  node.querySelector(".quote").textContent = `“${f.quote}”`;
  node.querySelector(".what").textContent = f.what_changed || "";
  node.querySelector(".why").textContent = f.why_it_matters || "";

  const checking = f.status !== "checked";
  node.querySelector(".checking").hidden = !checking;
  if (checking) {
    const calls = Object.fromEntries((f.tool_calls || []).map((t) => [t.backend, t]));
    node.querySelector(".checking-label").textContent = f.tool_calls?.length ? "Writing verdict" : "Fact-checking";
    for (const chip of node.querySelectorAll(".tool-chip")) {
      const t = calls[chip.dataset.backend];
      const name = chip.dataset.backend[0].toUpperCase() + chip.dataset.backend.slice(1);
      chip.className = `tool-chip${t ? (t.ok ? " done" : " failed") : ""}`;
      chip.textContent = t ? `${name} ${t.ok ? "✓" : "✗"} ${t.ms}ms` : name;
    }
  }

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
    renderTigerExtra(node, f);
  }

  const conf = node.querySelector(".conf");
  conf.hidden = checking || f.confidence === null || f.confidence === undefined;
  if (!conf.hidden) {
    node.querySelector(".conf-fill").style.width = `${Math.round(f.confidence * 100)}%`;
    node.querySelector(".conf-value").textContent = f.confidence.toFixed(2);
  }
  renderAgents(node, f);
  renderDetail(node, f);
  renderDeskDetail(node, f);
  renderSnowflakeDetail(node, f);
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

// ---------- Snowflake guidance history ----------
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function shortDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d} ’${String(y).slice(2)}`;
}
const compact = (v) => (v || "").replace(/^approximately\s+/i, "~").replace(/\s*\(.*\)$/, "");

function renderTrajectory(node, f) {
  const box = node.querySelector(".trajectory");
  const pts = f.guidance_history || [];
  box.hidden = !pts.length;
  if (!pts.length) return;
  const by = box.querySelector(".by");
  by.replaceChildren("Snowflake · SQL on GUIDANCE_LINES");
  if ((f.mocked || []).includes("snowflake")) by.append(el("span", "mock-tag", "MOCK"));
  const ol = box.querySelector(".traj");
  ol.replaceChildren();
  pts.forEach((p, i) => {
    const li = el("li", "tp");
    li.title = `${p.event} · ${p.status}${p.qualifier ? ` · ${p.qualifier}` : ""}`;
    li.append(el("span", "tp-date", shortDate(p.as_of_date)), el("span", "tp-val", compact(p.value_text)));
    if (p.qualifier && /low end/i.test(p.qualifier)) li.append(el("span", "tp-note", "low end"));
    else if (i > 0 && p.value_text === pts[i - 1].value_text) li.classList.add("same");
    ol.append(li);
  });
  const now = el("li", `tp now ${dirClass(f.direction)}`);
  now.append(el("span", "tp-date", "Oct 8 · this call"), el("span", "tp-val", f.new_value ? compact(f.new_value) : "…"));
  ol.append(now);
}

// ---------- agents strip (Nimble web desk + Snowflake Cortex Agent) ----------
const STEP_SHORT = { primary: "IR", broad: "WEB", serp: "GOOGLE", sitemap: "MAP", verify: "AGENT", wall: "WALL", social: "REDDIT", newswire: "NEWS" };
const VERDICT_CLASS = { CONSISTENT: "ok", NEW: "new", CONTRADICTS: "bad", UNVERIFIABLE: "na" };
// Plain labels: a bare "NEW" next to a red CUT badge read like a contradiction.
const VERDICT_LABEL = { CONSISTENT: "MATCHES PRIOR GUIDANCE", NEW: "CHANGES PRIOR GUIDANCE", CONTRADICTS: "CONFLICTS WITH PRIOR", UNVERIFIABLE: "NO PRIOR GUIDANCE" };

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function stepTitle(s) {
  return `${s.label} · ${s.endpoint}${s.ms !== null && s.ms !== undefined ? ` · ${s.ms} ms` : ""}${s.summary ? `\n${s.summary}` : ""}`;
}

// One line for the card face: what Nimble's verification agent found, with its first cited excerpt.
function verifyLine(v) {
  const a = v.answer || {};
  const parts = [];
  if (a.now) parts.push(`Now ${a.now}`);
  if (a.before) parts.push(`before ${a.before}`);
  if (a.change) parts.push(a.change);
  const cite = (v.claims || []).flatMap((c) => c.citations || []).find((c) => c.snippet);
  let out = parts.join(" · ");
  if (cite) out += `${out ? " — " : ""}“${cite.snippet.slice(0, 140)}${cite.snippet.length > 140 ? "…" : ""}” (${cite.source || "source"})`;
  return out || a.answer || "";
}

function renderAgents(node, f) {
  const desk = f.web_desk, sa = f.snowflake_agent;
  node.querySelector(".agents").hidden = !desk && !sa;
  const nrow = node.querySelector(".nimble-row");
  nrow.hidden = !desk;
  if (desk) {
    const dots = nrow.querySelector(".desk-dots");
    dots.replaceChildren();
    for (const s of desk.steps) {
      if (s.status === "skipped") continue;  // skipped specialists stay in the detail panel, off the card face
      const d = el("span", `dot ${s.status} ${s.phase}`, STEP_SHORT[s.id] || s.id.toUpperCase());
      d.title = stepTitle(s);
      dots.append(d);
    }
    const v = nrow.querySelector(".agent-verdict");
    v.replaceChildren();
    const vs = desk.steps.find((s) => s.id === "verify");
    if (desk.verify) {
      v.append(el("span", `vbadge c-${desk.verify.confidence || "na"}`, `VERIFIED · ${(desk.verify.confidence || "?").toUpperCase()}`),
        el("span", "vmeta", `${plural(desk.verify.official_sources, "official source")} · ${desk.verify.cached_at ? `cached ${desk.verify.cached_at}` : `${desk.verify.run_s ?? "?"}s`}`));
    } else if (vs && vs.status === "running") v.append(el("span", "vrun", "verification agent running…"));
    else if (vs && vs.status !== "ok") v.append(el("span", "vmeta", vs.summary.startsWith("unavailable") ? "verification agent unavailable" : `verification agent ${vs.status}`));
    if (desk.mock) v.append(el("span", "mock-tag", "MOCK"));
    let line = nrow.querySelector(".agent-answer");
    if (!line) { line = el("p", "agent-answer"); nrow.append(line); }
    line.textContent = desk.verify ? verifyLine(desk.verify) : "";
  }
  const srow = node.querySelector(".sf-row");
  srow.hidden = !sa;
  if (sa) {
    const v = srow.querySelector(".agent-verdict");
    v.replaceChildren();
    if (sa.status === "running") v.append(el("span", "vrun", "Cortex Search + Cortex Analyst running…"));
    else if (sa.status === "failed") v.append(el("span", "vbadge", "NO ANSWER"));
    else v.append(el("span", `vbadge v-${VERDICT_CLASS[sa.verdict] || "na"}`, VERDICT_LABEL[sa.verdict] || sa.verdict || "ANSWERED"),
      el("span", "vmeta", `${plural((sa.tools || []).length, "tool")} · ${plural((sa.rows || []).length, "row")} · ${sa.ms !== null && sa.ms !== undefined ? (sa.ms / 1000).toFixed(0) : "?"}s`));
    if (sa.mock) v.append(el("span", "mock-tag", "MOCK"));
    const answer = sa.status === "failed" ? `No answer: ${sa.error || "the agent did not finish"}.`
      : (sa.answer || "").replace(/^(CONSISTENT|CONTRADICTS|NEW|UNVERIFIABLE)[.:—–-]?\s*/i, "");
    srow.querySelector(".agent-answer").textContent = answer;
  }
}

function link(url, text) {
  if (!/^https?:\/\//i.test(url || "")) return el("span", null, text || url || "");  // never a javascript: or data: href
  const a = el("a", null, text || url);
  a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
  return a;
}

function renderDeskDetail(node, f) {
  const list = node.querySelector(".desk-steps");
  const extra = node.querySelector(".desk-extra");
  list.replaceChildren();
  extra.replaceChildren();
  const desk = f.web_desk;
  if (!desk) { list.append(el("li", "muted", "Runs with the fact-check.")); return; }
  for (const s of desk.steps) {
    const li = el("li", `step ${s.status}`);
    const head = el("div", "step-head");
    head.append(el("span", `step-dot ${s.status}`, s.status === "ok" ? "✓" : s.status === "failed" ? "✗" : s.status === "skipped" ? "–" : "…"),
      el("span", "step-label", s.label), el("span", "step-phase", s.phase === "verdict" ? "verdict" : "after verdict"),
      el("span", "step-ms", s.ms !== null && s.ms !== undefined ? `${s.ms} ms` : ""));
    li.append(head, el("div", "step-endpoint", s.endpoint + (s.feeds.length ? `  ← ${s.feeds.join(", ")}` : "")));
    if (s.summary) li.append(el("div", "step-summary", s.summary));
    if (s.mock) head.append(el("span", "mock-tag", "MOCK"));
    list.append(li);
  }
  const v = desk.verify;
  if (v) {
    const box = el("div", "desk-block");
    box.append(el("div", "sect-label", `Verification agent · trust ${v.confidence || "?"}`));
    const t = el("table", "kv");
    for (const [k, val] of Object.entries(v.answer || {})) {
      const tr = el("tr");
      const td = el("td");
      if (/^https?:\/\//.test(String(val))) td.append(link(String(val))); else td.textContent = String(val);
      tr.append(el("th", null, k), td);
      t.append(tr);
    }
    box.append(t);
    for (const c of (v.claims || []).slice(0, 4)) {
      const p = el("div", "claim");
      p.append(el("span", `vbadge c-${c.confidence || "na"}`, (c.confidence || "?").toUpperCase()), el("span", "claim-name", c.claim));
      for (const ci of c.citations.slice(0, 2)) if (ci.url) p.append(link(ci.url, ci.source || ci.url));
      box.append(p);
    }
    if (v.reasoning) box.append(el("p", "muted small", v.reasoning));
    extra.append(box);
  }
  if (desk.wall && desk.wall.domain) {
    const w = desk.wall;
    const box = el("div", "desk-block");
    box.append(el("div", "sect-label", "Wall-breaker"));
    const scan = el("div", "scan");
    for (const sc of w.scanned || []) scan.append(el("span", `scan-site${sc.antibots.length ? " protected" : ""}`, `${sc.domain}${sc.antibots.length ? ` · ${[...new Set(sc.antibots)].join(", ")}` : " · open"}`));
    box.append(scan, el("p", "small", `Read ${w.domain} with driver ${w.driver || "auto"}${w.antibots?.length ? ` past ${[...new Set(w.antibots)].join(", ")}` : ""}.`));
    if (w.snippet) box.append(el("p", "ev-snippet", w.snippet));
    if (w.url) box.append(link(w.url, w.title || w.url));
    extra.append(box);
  }
  for (const [items, label] of [[desk.street, "Newswire & street reaction"], [desk.social, "Reddit pulse"]]) {
    if (!items || !items.length) continue;
    const box = el("div", "desk-block");
    box.append(el("div", "sect-label", label));
    const ul = el("ul", "items");
    for (const it of items) {
      const li = el("li");
      if (it.url) li.append(link(it.url, it.title || it.url)); else li.append(el("span", null, it.title));
      if (it.snippet) li.append(el("div", "small muted", it.snippet));
      ul.append(li);
    }
    box.append(ul);
    extra.append(box);
  }
}

function renderSnowflakeDetail(node, f) {
  const box = node.querySelector(".sf-detail");
  const sa = f.snowflake_agent;
  box.replaceChildren();
  if (!sa) { box.append(el("p", "muted", "Runs after the verdict.")); return; }
  if (sa.status === "running") { box.append(el("p", "muted", "Agent running: Cortex Analyst over the semantic view GUIDANCE_SV, Cortex Search over prior guidance documents.")); return; }
  if (sa.status === "failed") { box.append(el("p", "neg small", sa.error || "failed")); return; }
  const tools = el("div", "scan");
  for (const t of sa.tools || []) tools.append(el("span", "scan-site", t));
  box.append(tools);
  if (sa.answer) box.append(el("p", "ev-snippet", sa.answer));
  if (sa.sql) {
    box.append(el("div", "sect-label", "SQL the agent wrote"));
    box.append(el("pre", "sql", sa.sql));
  }
  if (sa.rows && sa.rows.length) {
    const cols = Object.keys(sa.rows[0]);
    const t = el("table", "rows");
    const head = el("tr");
    for (const c of cols) head.append(el("th", null, c.toLowerCase()));
    t.append(head);
    for (const r of sa.rows) {
      const tr = el("tr");
      for (const c of cols) tr.append(el("td", null, r[c] === null || r[c] === undefined ? "" : String(r[c])));
      t.append(tr);
    }
    box.append(t);
  }
  if (sa.citations && sa.citations.length) box.append(el("p", "small muted", `Cited: ${sa.citations.join(" · ")}`));
  if (sa.mock) box.append(el("span", "mock-tag", "MOCK"));
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

  const trace = node.querySelector("p.trace");  // evidence refs also use .trace (div), so select the Sentry paragraph explicitly
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
  resetBrief();
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
  const startParam = Number(new URLSearchParams(location.search).get("start") || 0);
  const res = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: state.mode, start_s: startParam }) });
  if (!res.ok) {
    $("#audio-state").textContent = (await res.json()).detail || "ERROR";
    return;
  }
  state.runStartedAt = performance.now();
  state.joinedMidRun = false;
  setRunning(true);
  if (state.hasAudio) {
    // The server snaps the start to a chunk boundary; the status event re-syncs if it differs.
    audio.currentTime = Math.floor(startParam / (state.chunkS || 12)) * (state.chunkS || 12);
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
    if (s.chunk_s) state.chunkS = s.chunk_s;
    if (s.state === "running" && s.start_s !== undefined && !state.joinedMidRun) {
      state.startS = s.start_s;
      if (state.hasAudio && Math.abs(audio.currentTime - s.start_s) > 1.5 && !audio.paused) audio.currentTime = s.start_s;
    }
    if (s.duration) { state.duration = s.duration; $("#duration").textContent = fmtTime(s.duration); }
    if (s.call?.clip_start) state.clipStart = new Date(s.call.clip_start);
    if (s.transcriber) $("#transcriber").textContent = s.transcriber === "gemini" ? `Gemini · ${state.chunkS || 12}s chunks` : "MOCK transcript (no Gemini key)";
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
    onFlagVoice(f);
    traceFlagRender(f);
  });
  es.addEventListener("brief", (e) => renderBrief(JSON.parse(e.data)));
  es.addEventListener("error", (e) => {
    if (e.data) console.warn("pipeline error", e.data);
  });
}

async function init() {
  const s = await (await fetch("/api/state")).json();
  initSentryBrowser(s);
  renderPills(s.integrations);
  setMode(s.default_mode);
  state.clipStart = new Date(s.call.clip_start);
  state.hasAudio = s.audio;
  $("#ticker").textContent = s.call.ticker;
  $("#call-title").textContent = s.call.title;
  document.querySelector('.mode button[data-mode="replay"]').disabled = !s.replay_available;
  if (!s.audio) $("#audio-state").textContent = "NO AUDIO FILE — MOCK CLOCK";
  if (s.running && s.position_s !== null) {
    // Joined mid-run (page reload): pick the clock up where the server is.
    state.startS = 0;
    state.speed = s.speed || 1;
    state.joinedMidRun = true;
    state.runStartedAt = performance.now() - (s.position_s / state.speed) * 1000;
    state.mode = s.mode;
    setRunning(true);
  }
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
