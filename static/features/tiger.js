// tiger feature module: owned by its workstream. Exports are called from app.js; keep their names.

function bytes(n) {
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} kB`;
}

function storageText(s) {
  if (!s || s.chunks === null || s.chunks === undefined) return "";
  const chunks = `${s.chunks} chunk${s.chunks === 1 ? "" : "s"}`;
  if (s.ratio) return `columnstore ${s.ratio}× smaller (${chunks})`;
  return s.total_bytes ? `hypertable ${bytes(s.total_bytes)} · ${chunks}` : chunks;
}

// One collapsed line under the market stats: where the numbers came from, the query time, storage;
// opens to the SQL Tiger ran. Elements are reused across re-renders so an open <details> stays open.
export function renderTigerExtra(node, f) {
  const box = node.querySelector(".tiger-extra");
  if (!box) return;
  const pr = f.price_reaction;
  box.hidden = !pr?.sql;
  if (box.hidden) return;

  let details = box.querySelector("details.tiger-sql");
  if (!details) {
    details = document.createElement("details");
    details.className = "tiger-sql";
    const summary = document.createElement("summary");
    const line = document.createElement("span");
    line.className = "tiger-line";
    const toggle = document.createElement("span");
    toggle.className = "tiger-toggle";
    toggle.textContent = "SQL";
    summary.append(line, toggle);
    const pre = document.createElement("pre");
    pre.className = "sql";
    details.append(summary, pre);
    box.replaceChildren(details);
  }

  const parts = ["TIGER DATA"];
  if (pr.mock) parts.push("query shown, not run (offline)");
  else {
    parts.push("bars_1m continuous aggregate");
    if (pr.query_ms !== null && pr.query_ms !== undefined) parts.push(`query ${pr.query_ms} ms`);
    const storage = storageText(pr.storage);
    if (storage) parts.push(storage);
  }
  const text = parts.join(" · ");
  const line = details.querySelector(".tiger-line");
  line.textContent = text;
  line.title = text;
  details.querySelector("pre").textContent = pr.sql;
}
