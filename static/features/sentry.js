// Sentry browser monitoring. Exports are called from app.js; keep their names.
//
// initSentryBrowser(state): when /api/state carries sentry_browser (Sentry is live on the server), load Sentry's
// official CDN bundle and start browser tracing. Same-origin API calls then carry trace headers, so the page load
// and the server's request transactions share one trace.
// traceFlagRender(f): once per flag, when its verdict first arrives, add a "ui.render" span to that flag's own
// fact-check trace. The flag's trace then runs from the agent's work to the card on screen.

// The 10.x line on purpose: 11.x streams spans by default, while the server's sentry-sdk 2.x sends transactions.
const SDK_VERSION = "10.76.2";
const SDK_URL = `https://browser.sentry-cdn.com/${SDK_VERSION}/bundle.tracing.min.js`;
// Official SRI hash for this exact file, from Sentry's release registry (sdks/sentry.javascript.browser/10.76.2).
const SDK_INTEGRITY = "sha384-BqXQi3B9SFi6WiAcbDTLzqa9A2e7ZDXDn8xcqqAmznB00bkKr7PmxAR1S0/sM1pl";

let sdk = null; // Promise of the initialised Sentry global (null inside when the bundle failed); null = monitoring off
const traced = new Set(); // "<flag id>:<trace id>" already given a render span (flag ids repeat across runs)

function loadBundle() {
  if (window.Sentry) return Promise.resolve(window.Sentry);
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SDK_URL;
    script.integrity = SDK_INTEGRITY;
    script.crossOrigin = "anonymous";
    script.async = true;
    script.onload = () => (window.Sentry ? resolve(window.Sentry) : reject(new Error("bundle loaded without window.Sentry")));
    script.onerror = () => reject(new Error(`could not load ${SDK_URL}`));
    document.head.append(script);
  });
}

export function initSentryBrowser(state) {
  const cfg = state && state.sentry_browser;
  if (sdk || !cfg || !cfg.dsn) return;
  sdk = loadBundle()
    .then((Sentry) => {
      Sentry.init({
        dsn: cfg.dsn,
        environment: cfg.environment,
        release: cfg.release,
        // linkPreviousTrace off: otherwise each flag's render span links to the previous, unrelated flag's trace.
        integrations: [Sentry.browserTracingIntegration({ linkPreviousTrace: "off" })],
        tracesSampleRate: 1.0,
        tracePropagationTargets: [location.origin],
      });
      return Sentry;
    })
    .catch((err) => {
      console.warn("Sentry browser monitoring is off:", err.message);
      return null;
    });
}

const nowSeconds = () => (performance.timeOrigin + performance.now()) / 1000;
// A hidden tab never paints (requestAnimationFrame pauses), so the span ends at once there, and after 1 s at most.
const nextPaint = () => document.hidden ? Promise.resolve() : new Promise((resolve) => {
  requestAnimationFrame(() => setTimeout(resolve, 0));
  setTimeout(resolve, 1000);
});
const randomSpanId = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");

export function traceFlagRender(f) {
  if (!sdk || !f || f.status !== "checked" || !/^[0-9a-f]{32}$/.test(f.sentry_trace_id || "")) return;
  const key = `${f.id}:${f.sentry_trace_id}`;
  if (traced.has(key)) return;
  traced.add(key);
  const start = nowSeconds(); // app.js renders the card just before this call; the span runs until it is painted
  const visible = !document.hidden;
  nextPaint()
    .then(() => {
      const end = nowSeconds();
      return sdk.then((Sentry) => Sentry && recordRender(Sentry, f, start, end, visible));
    })
    .catch((err) => console.warn("Sentry render span skipped:", err.message));
}

function recordRender(Sentry, f, start, end, visible) {
  const attributes = {};
  const values = {
    "calldesk.flag_id": f.id, "calldesk.metric": f.metric, "calldesk.direction": f.direction,
    "calldesk.latency_s": f.latency_s, "calldesk.checked_latency_s": f.checked_latency_s, "calldesk.confidence": f.confidence,
    "calldesk.page_visible": visible,
  };
  for (const [k, v] of Object.entries(values)) if (v !== null && v !== undefined) attributes[k] = v;
  // A made-up parent span id: the browser cannot know the server span, but the trace id puts this span in the flag's trace.
  const sentryTrace = `${f.sentry_trace_id}-${randomSpanId()}-1`;
  Sentry.withActiveSpan(null, () =>
    Sentry.continueTrace({ sentryTrace, baggage: undefined }, () =>
      Sentry.startSpanManual({ name: `render ${f.metric_label}`, op: "ui.render", startTime: start, attributes }, (span) => {
        for (const [name, v] of [["quote_to_flag_s", f.latency_s], ["quote_to_verdict_s", f.checked_latency_s]]) {
          if (typeof v === "number") Sentry.setMeasurement(name, v, "second", span);
        }
        span.end(end);
      })));
}
