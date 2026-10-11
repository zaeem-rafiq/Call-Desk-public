// brief feature module: the end-of-call IR brief written inside Snowflake (CALL_FLAGS + AI_COMPLETE).
// Exports are called from app.js; keep their names. All server text goes in through textContent.

const root = () => document.getElementById("call-brief");

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

function failedText(data) {
  const err = String(data.error || "");
  if (err.startsWith("no checked flags")) return "No checked flags this run, so no brief.";
  if (err.startsWith("timed out")) return "Snowflake did not return the brief in time.";
  return "The brief could not be written in Snowflake this run.";
}

export function renderBrief(data) {
  const box = root();
  if (!box || !data) return;
  const head = el("div", "brief-head");
  head.append(el("span", "brief-label", "SNOWFLAKE CORTEX · END-OF-CALL BRIEF"));
  if (data.mock) head.append(el("span", "mock-tag", "MOCK"));
  const parts = [head];

  if (data.status === "running") {
    const line = el("div", "brief-running");
    line.append(el("span", "spinner"), el("span", null, "Writing the brief in Snowflake…"));
    parts.push(line);
  } else if (data.status === "done") {
    parts.push(el("p", "brief-headline", data.headline));
    const list = el("ul", "brief-bullets");
    for (const b of data.bullets || []) list.append(el("li", null, b));
    parts.push(list);
    const n = data.rows_written || 0;
    const secs = typeof data.ms === "number" ? ` · ${(data.ms / 1000).toFixed(1)} s` : "";
    const meta = el("span", "brief-meta", `${n} flag${n === 1 ? "" : "s"} written to CALL_FLAGS · ${data.model || "AI_COMPLETE"}${secs}`);
    if (data.sql) {
      // The meta line is the SQL toggle: one compact footer row, the query unfolds below it.
      const det = el("details", "brief-sql");
      const sum = el("summary");
      sum.append(meta, el("span", "brief-sql-label", "SQL"));
      det.append(sum, el("pre", "sql", data.sql));
      parts.push(det);
    } else {
      parts.push(meta);
    }
  } else {
    parts.push(el("p", "brief-failed", failedText(data)));
  }

  box.className = `call-brief ${data.status || ""}`.trim();
  box.replaceChildren(...parts);
  box.hidden = false;
}

export function resetBrief() {
  const box = root();
  if (!box) return;
  box.hidden = true;
  box.className = "call-brief";
  box.replaceChildren();
}
