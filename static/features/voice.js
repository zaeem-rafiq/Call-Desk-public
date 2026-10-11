// voice feature module: owned by its workstream. Exports are called from app.js; keep their names.
// Gemini TTS alerts: shows the spoken line on cut / raise cards and plays each alert once per page session.

const STORE_KEY = "calldesk.voiceAlerts";
const played = new Set(); // "<run id>:<flag id>" whose audio already played (or was skipped) in this page session
let runId = ""; // the run whose events are streaming; a new run's flags reuse ids like f01-core_eps_growth
let muted = readMuted();
let toggle = null;
let queue = Promise.resolve(); // alerts play one after another, never on top of each other

function readMuted() {
  try { return localStorage.getItem(STORE_KEY) === "off"; } catch { return false; }
}

function saveMuted() {
  try { localStorage.setItem(STORE_KEY, muted ? "off" : "on"); } catch { /* storage blocked: keep the choice in memory */ }
}

function ensureToggle() {
  if (toggle) return;
  const bar = document.querySelector(".topbar");
  if (!bar) return;
  toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "voice-toggle";
  toggle.title = "Gemini spoken alerts for guidance cuts and raises";
  toggle.addEventListener("click", () => {
    muted = !muted;
    saveMuted();
    paintToggle();
  });
  paintToggle();
  const controls = bar.querySelector(".controls");
  if (controls) controls.prepend(toggle); else bar.append(toggle);
}

const SVG_NS = "http://www.w3.org/2000/svg";

// A speaker with sound waves (on) or a cross (off), drawn in the button's text colour.
function speakerIcon(on) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", "vt-icon");
  svg.setAttribute("aria-hidden", "true");
  const paths = [["M2 6h2.5L8 3v10L4.5 10H2z", "fill"],
    ...(on ? [["M10.5 5.5a3.5 3.5 0 0 1 0 5", "stroke"], ["M12.5 3.5a6.4 6.4 0 0 1 0 9", "stroke"]]
      : [["M10.5 6l4 4M14.5 6l-4 4", "stroke"]])];
  for (const [d, kind] of paths) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    if (kind === "fill") path.setAttribute("fill", "currentColor");
    else { path.setAttribute("fill", "none"); path.setAttribute("stroke", "currentColor"); path.setAttribute("stroke-width", "1.5"); path.setAttribute("stroke-linecap", "round"); }
    svg.append(path);
  }
  return svg;
}

function paintToggle() {
  const label = document.createElement("span");
  label.className = "vt-label";
  label.textContent = muted ? "Alerts off" : "Alerts on";
  toggle.replaceChildren(speakerIcon(!muted), label);
  toggle.setAttribute("aria-pressed", String(!muted));
  toggle.classList.toggle("off", muted);
}

function isAlertUrl(url) {
  return typeof url === "string" && url.startsWith("/static/alerts/") && url.endsWith(".wav");
}

function playOnce(url, card) {
  return new Promise((resolve) => {
    const call = document.getElementById("audio"); // duck the call audio while the alert speaks
    const ducked = call && !call.paused ? call.volume : null;
    const done = () => {
      if (ducked !== null) call.volume = ducked;
      card?.classList.remove("voice-speaking");
      resolve();
    };
    const audio = new Audio(url);
    audio.addEventListener("ended", done, { once: true });
    audio.addEventListener("error", done, { once: true });
    audio.play().then(() => {
      if (ducked !== null) call.volume = Math.min(ducked, 0.3);
      card?.classList.add("voice-speaking");
    }).catch(done); // autoplay blocked or file missing: stay silent
  });
}

ensureToggle(); // module scripts run after the document is parsed, so the top bar exists

// Called on each run's reset event (an SSE reconnect replays the same run id, so its alerts do not repeat).
export function setVoiceRun(id) {
  runId = id;
}

export function onFlagVoice(f) {
  ensureToggle();
  if (!f || !f.alert_text) return;
  const card = document.getElementById("flag-" + f.id);
  if (card) {
    let line = card.querySelector(".voice-line");
    if (!line) {
      line = document.createElement("p");
      line.className = "voice-line";
      const label = document.createElement("span");
      label.className = "voice-label";
      label.append(speakerIcon(true), "Spoken alert");
      const text = document.createElement("span");
      text.className = "voice-text";
      line.append(label, " · ", text);
      const quote = card.querySelector(".quote");
      if (quote) quote.after(line); else card.append(line);
    }
    line.querySelector(".voice-text").textContent = f.alert_text;
  }
  const key = `${runId}:${f.id}`;
  if (!isAlertUrl(f.alert_audio_url) || played.has(key)) return;
  played.add(key);
  if (muted) return;
  queue = queue.then(() => playOnce(f.alert_audio_url, document.getElementById("flag-" + f.id)));
}
