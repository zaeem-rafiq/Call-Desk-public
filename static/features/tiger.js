// tiger feature module: owned by its workstream. Exports are called from app.js; keep their names.

function bytes(n) {
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} kB`;
}

const hasChunks = (s) => s && s.chunks !== null && s.chunks !== undefined;
const chunksText = (s) => (hasChunks(s) ? `${s.chunks} chunk${s.chunks === 1 ? "" : "s"}` : "");

// The storage fact for the card face: compression when Tiger reports it, else the hypertable size.
function storageText(s) {
  if (!hasChunks(s)) return "";
  if (s.ratio) return `columnstore ${s.ratio}× smaller`;
  return s.total_bytes ? `hypertable ${bytes(s.total_bytes)}` : "";
}

// One collapsed line under the market stats: query time and storage; opens to where the numbers came
// from and the SQL Tiger ran. Elements are reused across re-renders so an open <details> stays open.
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
    const source = document.createElement("p");
    source.className = "tiger-source";
    const pre = document.createElement("pre");
    pre.className = "sql";
    details.append(summary, source, pre);
    box.replaceChildren(details);
  }

  const parts = ["TIGER DATA"];
  if (pr.mock) parts.push("query shown, not run (offline)");
  else {
    if (pr.query_ms !== null && pr.query_ms !== undefined) parts.push(`query ${pr.query_ms} ms`);
    const storage = storageText(pr.storage);
    if (storage) parts.push(storage);
  }
  const text = parts.join(" · ");
  const line = details.querySelector(".tiger-line");
  line.textContent = text;
  line.title = text;
  details.querySelector(".tiger-source").textContent = ["bars_1m continuous aggregate", chunksText(pr.storage)].filter(Boolean).join(" · ");
  details.querySelector("pre").textContent = pr.peers_sql ? `${pr.sql}\n\n-- peers, same window\n${pr.peers_sql}` : pr.sql;
  renderPeers(box, pr);
}

// One line: how the company and its peers moved against the benchmark in the same window after the quote.
function renderPeers(box, pr) {
  let row = box.querySelector(".tiger-peers");
  const peers = pr.peers || [];
  if (!peers.length) { if (row) row.remove(); return; }
  if (!row) { row = document.createElement("p"); row.className = "tiger-peers"; box.append(row); }
  const label = document.createElement("span");
  label.className = "peers-label";
  label.textContent = `PEERS VS ${pr.benchmark}`;
  label.title = `Each stock's move minus ${pr.benchmark} over the same window${pr.window ? ` (${pr.window})` : ""}`;
  const items = [...peers].sort((a, b) => (b.subject - a.subject) || (a.vs_benchmark - b.vs_benchmark)).map((p) => {
    const s = document.createElement("span");
    s.className = `peer${p.subject ? " subject" : ""} ${p.vs_benchmark >= 0 ? "pos" : "neg"}`;
    s.textContent = `${p.ticker} ${p.vs_benchmark >= 0 ? "+" : "−"}${Math.abs(p.vs_benchmark).toFixed(2)}pp`;
    s.title = `${p.ticker} ${p.move >= 0 ? "+" : ""}${p.move.toFixed(2)}% vs ${pr.benchmark}`;
    return s;
  });
  row.replaceChildren(label, ...items);
}
