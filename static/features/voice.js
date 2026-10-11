// voice feature module: owned by its workstream. Exports are called from app.js; keep their names.
// Gemini TTS alerts: shows the spoken line on cut / raise cards and plays each alert once per page session.

const STORE_KEY = "calldesk.voiceAlerts";
const played = new Set(); // flag ids whose audio already played (or was skipped) in this page session
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

function paintToggle() {
  toggle.textContent = muted ? "🔇 Alerts off" : "🔊 Alerts on";
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
      label.textContent = "🔊 SPOKEN ALERT";
      const text = document.createElement("span");
      text.className = "voice-text";
      line.append(label, " · ", text);
      const quote = card.querySelector(".quote");
      if (quote) quote.after(line); else card.append(line);
    }
    line.querySelector(".voice-text").textContent = f.alert_text;
  }
  if (!isAlertUrl(f.alert_audio_url) || played.has(f.id)) return;
  played.add(f.id);
  if (muted) return;
  queue = queue.then(() => playOnce(f.alert_audio_url, document.getElementById("flag-" + f.id)));
}
