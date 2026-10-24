/* MongoPilot – web interface (vanilla JS, no build step) */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const main = $("#main");
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const state = { catalog: null, overview: null, range: 3600, selectedProc: {}, refreshTimer: null, route: null,
  chartGroup: "Key metrics", qRange: 86400, qTab: "slow", qNs: "", openShapes: new Set(), openColls: new Set() };
const COLORS = ["var(--action)", "var(--ok)", "var(--warn)", "#8A4FA8", "var(--crit)", "var(--muted)"];
const SEVERITY = { critical: ["crit", "Critical"], warning: ["warn", "Warning"], info: ["info", "Info"] };
const JOB_STATUS = { queued: ["info", "Queued"], running: ["info", "Running"], success: ["ok", "Succeeded"], failed: ["crit", "Failed"] };
const LOCALE = "en-GB";

/* ---------------- utilities ---------------- */
async function api(path, opts = {}) {
  const init = { headers: {}, ...opts };
  if (opts.body !== undefined && typeof opts.body !== "string") {
    init.body = JSON.stringify(opts.body);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch("/api" + path, init);
  let data = null;
  try { data = await res.json(); } catch { /* empty response */ }
  if (!res.ok) throw new Error((data && data.error) || `HTTP error ${res.status}`);
  return data;
}
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = ""), err ? 7000 : 3000);
}
const fmtNum = (v, d = 1) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (a >= 1e4) return (v / 1e3).toFixed(1) + "k";
  if (a >= 100) return Math.round(v).toLocaleString(LOCALE);
  return Number(v.toFixed(d)).toLocaleString(LOCALE);
};
const fmtBytes = (v) => {
  if (v === null || v === undefined) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (Math.abs(v) >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
};
function fmtValue(v, unit) {
  if (v === null || v === undefined) return "–";
  if (unit === "B/s") return fmtBytes(v) + "/s";
  if (unit === "s" && v >= 120) return fmtDur(v);
  if (unit === "ms" && v >= 10000) return fmtDur(v / 1000);
  return fmtNum(v, unit === "GB" || unit === "GB/h" ? 2 : 1) + (unit ? (unit === "%" ? "%" : " " + unit) : "");
}
function fmtDur(s) {
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) { const m = Math.floor((s % 3600) / 60); return `${Math.floor(s / 3600)} h${m ? ` ${m} min` : ""}`; }
  const h = Math.floor((s % 86400) / 3600);
  return `${Math.floor(s / 86400)} d${h ? ` ${h} h` : ""}`;
}
const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)} s` : `${Math.round(ms)} ms`);
const ago = (ts) => (ts ? (Date.now() / 1000 - ts < 5 ? "just now" : fmtDur(Date.now() / 1000 - ts) + " ago") : "never");
const dateTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "medium" }) : "–");
const pill = (cls, text) => `<span class="pill ${cls}">${esc(text)}</span>`;
const metricMeta = (name) => (state.catalog?.metrics || []).find((m) => m.name === name) || { label: name, unit: "" };
const randomPassword = () => {
  const a = new Uint8Array(18); crypto.getRandomValues(a);
  return btoa(String.fromCharCode(...a)).replace(/[+/=]/g, "").slice(0, 22);
};
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast("Copied to clipboard");
}
const cmdBlock = (cmd) => `<div class="cmd"><div class="code">${esc(cmd)}</div><button type="button" class="small" data-copy="${esc(cmd)}">Copy</button></div>`;
function bindCopy(root = document) {
  $$("[data-copy]", root).forEach((b) => (b.onclick = (ev) => { ev.stopPropagation(); copyText(b.dataset.copy); }));
}
const RANGES = [[3600, "1 h"], [21600, "6 h"], [86400, "24 h"], [604800, "7 d"]];
const rangeSeg = (current, attr = "data-range") => `<div class="seg" role="group" aria-label="Time range">${RANGES.map(([r, l]) =>
  `<button type="button" ${attr}="${r}" class="${current === r ? "on" : ""}">${l}</button>`).join("")}</div>`;

/* ---------------- dialog ---------------- */
function openModal({ title, body, submit = "Save", danger = false, wide = false, onSubmit, onOpen }) {
  const dlg = $("#modal");
  const form = $("#modal-form");
  dlg.classList.toggle("wide", wide);
  form.innerHTML = `<h2>${esc(title)}</h2><div class="modal-body">${body}</div>
    <div class="actions"><button type="button" value="cancel" data-close>${submit ? "Cancel" : "Close"}</button>
    ${submit ? `<button type="submit" class="${danger ? "danger" : "primary"}">${esc(submit)}</button>` : ""}</div>`;
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    if (btn) btn.disabled = true;
    try {
      const keep = await onSubmit(new FormData(form), form);
      if (keep !== false) dlg.close();
    } catch (e) {
      toast(e.message, true);
    } finally { if (btn) btn.disabled = false; }
  };
  $("[data-close]", form).onclick = () => dlg.close();
  dlg.showModal();
  if (onOpen) onOpen(form);
  const first = form.querySelector("input:not([type=file]):not([type=hidden]), select, textarea");
  if (first) first.focus();
}
const confirmAction = (title, text, submit, fn, danger = false) =>
  openModal({ title, body: `<p>${text}</p>`, submit, danger, onSubmit: fn });

/* ---------------- router ---------------- */
const routes = [
  [/^#?\/?$/, pageHome, "home"],
  [/^#\/deployments$/, pageDeployments, "deployments"],
  [/^#\/deployments\/(\d+)$/, pageDeployment, "deployments"],
  [/^#\/deployments\/(\d+)\/(parameters|config|tls)$/, pageDeployment, "deployments"],
  [/^#\/deployments\/(\d+)\/tools\/(mongosh|dump|restore|export|import|schema)\/(\d+)$/, pageTool, "deployments"],
  [/^#\/new-replicaset$/, pageNewReplicaSet, "deployments"],
  [/^#\/new-sharded$/, pageNewSharded, "deployments"],
  [/^#\/queries$/, pageQueries, "queries"],
  [/^#\/queries\/(\d+)$/, pageQueries, "queries"],
  [/^#\/servers$/, pageServers, "servers"],
  [/^#\/servers\/(\d+)$/, pageServer, "servers"],
  [/^#\/alerts$/, pageAlerts, "alerts"],
  [/^#\/rules$/, pageRules, "rules"],
  [/^#\/channels$/, pageChannels, "channels"],
  [/^#\/jobs$/, pageJobs, "jobs"],
  [/^#\/jobs\/(\d+)$/, pageJob, "jobs"],
  [/^#\/audit$/, pageAudit, "audit"],
];
async function route() {
  clearInterval(state.refreshTimer);
  clearTimeout(state.exportTimer);
  if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } state.cleanup = null; }
  const hash = location.hash || "#/";
  for (const [re, fn, nav] of routes) {
    const m = hash.match(re);
    if (m) {
      $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
      state.route = hash;
      try { await fn(...m.slice(1)); } catch (e) { main.innerHTML = `<div class="notice crit">${esc(e.message)}</div>`; }
      return;
    }
  }
  main.innerHTML = `<div class="empty"><h3>Page not found</h3><a href="#/">Back to the overview</a></div>`;
}
function autoRefresh(fn, ms) {
  clearInterval(state.refreshTimer);
  const hash = location.hash;
  state.refreshTimer = setInterval(() => {
    if (location.hash !== hash || $("#modal").open || document.hidden) return;
    fn().catch(() => {});
  }, ms);
}
async function refreshNav() {
  try {
    const o = await api("/overview");
    state.overview = o;
    const open = (o.alerts_open.critical || 0) + (o.alerts_open.warning || 0) + (o.alerts_open.info || 0);
    $("#nav-alerts").textContent = open || "";
    $("#nav-jobs").textContent = o.jobs_running || "";
    $("#side-foot").textContent = `v${o.version} · polling ${o.poll_interval}s${o.demo ? " · demo" : ""}`;
  } catch { /* ignore */ }
}

/* ---------------- topology and node icons ---------------- */
const ICON_PATHS = {
  primary: '<path d="M2 12.5h12l.8-7.5-3.8 3L8 3 5 8 1.2 5z" fill="currentColor" stroke="none"/>',
  secondary: '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M3 10.5V4a1.5 1.5 0 0 1 1.5-1.5H10"/>',
  hidden: '<path d="M1.8 8S4 4 8 4s6.2 4 6.2 4S12 12 8 12 1.8 8 1.8 8z"/><circle cx="8" cy="8" r="1.8"/><path d="M2.5 13.5 13.5 2.5"/>',
  delayed: '<circle cx="8" cy="8" r="5.8"/><path d="M8 4.6V8l2.4 1.6"/>',
  passive: '<circle cx="8" cy="8" r="5.8"/><path d="M6.4 5.6v4.8M9.6 5.6v4.8"/>',
  arbiter: '<path d="M8 2.5v11M5 13.5h6M3 5h10M3 5 1.3 9.5h3.4zM13 5l-1.7 4.5h3.4z"/>',
  mongos: '<path d="M1.5 8h5.2M6.7 8l3-3.5h4.8M6.7 8l3 3.5h4.8M12.6 2.6l1.9 1.9-1.9 1.9M12.6 9.6l1.9 1.9-1.9 1.9"/>',
  config: '<circle cx="8" cy="8" r="2.3"/><path d="M8 1.6v2.2M8 12.2v2.2M1.6 8h2.2M12.2 8h2.2M3.5 3.5l1.6 1.6M10.9 10.9l1.6 1.6M3.5 12.5l1.6-1.6M10.9 5.1l1.6-1.6"/>',
  standalone: '<ellipse cx="8" cy="3.8" rx="5" ry="1.9"/><path d="M3 3.8v8.4c0 1 2.2 1.9 5 1.9s5-.9 5-1.9V3.8M3 8c0 1 2.2 1.9 5 1.9S13 9 13 8"/>',
  starting: '<path d="M13.2 8a5.2 5.2 0 1 1-1.6-3.8M13.4 2.6v3.2h-3.2"/>',
  down: '<path d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6"/>',
};
const KIND_LABEL = { primary: "Primary", secondary: "Secondary", hidden: "Hidden secondary", delayed: "Delayed secondary",
  passive: "Passive secondary (priority 0)", arbiter: "Arbiter", mongos: "mongos router", config: "Config server",
  standalone: "Standalone", starting: "Starting or recovering", down: "Unreachable" };
const icon = (kind, size = 15) => `<svg viewBox="0 0 16 16" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[kind] || ""}</svg>`;
const badge = (kind, size = 26) => `<span class="badge k-${kind}" style="width:${size}px;height:${size}px" title="${esc(KIND_LABEL[kind] || kind)}">${icon(kind, Math.round(size * 0.58))}</span>`;

function nodeKind(p) {
  if (!p.up && p.role !== "ARBITER") return "down";
  const r = (p.role || "").toUpperCase();
  const m = p.info?.member || p.member || {};
  if (r === "PRIMARY") return "primary";
  if (r === "STANDALONE") return "standalone";
  if (r === "MONGOS") return "mongos";
  if (r === "ARBITER") return "arbiter";
  if (r === "SECONDARY") {
    if (m.delay > 0) return "delayed";
    if (m.hidden) return "hidden";
    if (m.priority === 0) return "passive";
    return "secondary";
  }
  return "starting";
}
function nodeDetail(p) {
  const m = p.info?.member || p.member || {};
  const kind = nodeKind(p);
  const bits = [KIND_LABEL[kind].toLowerCase()];
  if (kind === "delayed") bits[0] = `delayed ${fmtDur(m.delay)}`;
  if (kind === "starting" && p.role) bits[0] = (ROLE_EN[p.role] || p.role).toLowerCase();
  if (m.votes === 0) bits.push("non-voting");
  if (p.shard === "config") bits.push("config");
  return bits.join(" · ");
}
function nodeClass(p) {   // used for table pills
  const k = nodeKind(p);
  return k === "down" ? "down" : k === "starting" ? "trans" : k;
}
const ROLE_EN = { PRIMARY: "primary", SECONDARY: "secondary", ARBITER: "arbiter", MONGOS: "mongos", STANDALONE: "standalone",
  STARTUP: "starting", STARTUP2: "starting", RECOVERING: "recovering", ROLLBACK: "rollback", "(not reachable/healthy)": "unreachable" };
const roleLabel = (p) => nodeDetail(p);
function roleCell(p) {
  return `<span class="role-cell">${badge(nodeKind(p), 20)}<span>${esc(nodeDetail(p))}</span></span>`;
}

function groupProcesses(procs) {
  const groups = new Map();
  for (const p of procs) {
    const key = p.role === "MONGOS" ? "mongos" : p.rs_name || p.shard || "standalone";
    if (!groups.has(key)) groups.set(key, { key, shard: p.shard, rs: p.rs_name, procs: [] });
    groups.get(key).procs.push(p);
  }
  const order = (g) => (g.key === "mongos" ? 0 : g.shard === "config" ? 2 : 1);
  return [...groups.values()].sort((a, b) => order(a) - order(b) || a.key.localeCompare(b.key));
}
function topologyHTML(procs, selectedId, legend = true) {
  if (!procs.length) return `<div class="empty">No processes discovered yet.</div>`;
  const kinds = new Set();
  const html = `<div class="topo">${groupProcesses(procs).map((g) => {
    const rank = { primary: 0, standalone: 0, secondary: 1, passive: 2, hidden: 3, delayed: 4, arbiter: 5, starting: 6, down: 7, mongos: 0 };
    const sorted = [...g.procs].sort((a, b) => rank[nodeKind(a)] - rank[nodeKind(b)] || a.host.localeCompare(b.host));
    const isCfg = g.shard === "config";
    const name = g.key === "mongos" ? `${icon("mongos", 13)} mongos routers` : isCfg ? `${icon("config", 13)} Config servers`
      : g.shard ? `Shard ${esc(g.shard)}` : "Replica set";
    if (isCfg) kinds.add("config");
    const track = sorted.map((p, i) => {
      const kind = nodeKind(p);
      kinds.add(kind);
      const lag = p.metrics?.repl_lag_s;
      const link = i === 0 ? "" : g.key === "mongos" ? `<span class="link" style="width:18px;background:transparent"></span>`
        : `<span class="link ${lag >= 10 && kind !== "delayed" ? "lag-warn" : ""}">${lag !== undefined ? `<span class="lag">${fmtDur(lag)} lag</span>` : ""}</span>`;
      return `${link}<button type="button" class="node n-${kind} ${p.id === selectedId ? "sel" : ""}" data-proc="${p.id}"
        title="${esc(p.last_error || KIND_LABEL[kind])}">${badge(kind)}<span><span class="nh">${esc(p.host)}</span><br>
        <span class="ns">${esc(nodeDetail(p))}${p.version ? " · " + esc(p.version) : ""}</span></span></button>`;
    }).join("");
    return `<div class="topo-set"><div class="set-name">${name}${g.rs ? `<b>${esc(g.rs)}</b>` : ""}</div><div class="topo-track">${track}</div></div>`;
  }).join("")}</div>`;
  if (!legend) return html;
  const order = ["primary", "secondary", "passive", "hidden", "delayed", "arbiter", "mongos", "config", "standalone", "starting", "down"];
  return html + `<div class="legend-row" aria-label="Legend">${order.filter((k) => kinds.has(k)).map((k) =>
    `<span>${badge(k, 18)}${esc(KIND_LABEL[k])}</span>`).join("")}</div>`;
}
function miniTopo(d) {
  const st = d.status || {};
  const sets = Object.values(st.replica_sets || {});
  if (!d.processes_total) return `<span class="muted small">–</span>`;
  const down = d.processes_total - d.processes_up;
  let html = "";
  for (let i = 0; i < d.processes_up; i++) html += `<i class="${i === 0 && sets.length ? "p" : ""}"></i>`;
  for (let i = 0; i < down; i++) html += `<i class="d"></i>`;
  return `<span class="mini-topo" title="${d.processes_up}/${d.processes_total} processes reachable">${html}</span>`;
}
function deploymentHealth(d) {
  if (d.paused && d.last_error === "Being created") return pill("info", "being created");
  if (d.paused) return pill("off", "paused");
  if (d.last_error) return pill("crit", "unreachable");
  const st = d.status || {};
  if (Object.values(st.replica_sets || {}).some((r) => !r.has_primary)) return pill("crit", "no primary");
  if (d.processes_up < d.processes_total) return pill("warn", `${d.processes_total - d.processes_up} processes down`);
  if (d.alerts_open) return pill("warn", `${d.alerts_open} alert${d.alerts_open > 1 ? "s" : ""}`);
  return pill("ok", "healthy");
}
const TOPO_EN = { replicaset: "Replica set", sharded: "Sharded cluster", standalone: "Standalone" };

/* ---------------- charts ---------------- */
// src: "proc" (default) or "srv" (host metrics of the server running the process)
const CHARTS = [
  { g: "Throughput", key: true, title: "Operations per second", keys: ["ops_query", "ops_insert", "ops_update", "ops_delete", "ops_command", "ops_getmore"], unit: "op/s", sum: true, short: { ops_query: "query", ops_insert: "insert", ops_update: "update", ops_delete: "delete", ops_command: "command", ops_getmore: "getmore" } },
  { g: "Throughput", title: "Documents per second", keys: ["docs_returned_s", "docs_inserted_s", "docs_updated_s", "docs_deleted_s"], unit: "doc/s", short: { docs_returned_s: "returned", docs_inserted_s: "inserted", docs_updated_s: "updated", docs_deleted_s: "deleted" } },
  { g: "Throughput", title: "Network requests", keys: ["net_requests_s"], unit: "req/s" },
  { g: "Latency", key: true, title: "Operation execution time", keys: ["lat_reads_ms", "lat_writes_ms", "lat_commands_ms"], unit: "ms", short: { lat_reads_ms: "reads", lat_writes_ms: "writes", lat_commands_ms: "commands" } },
  { g: "Latency", title: "Transaction latency", keys: ["lat_txn_ms"], unit: "ms" },
  { g: "Query efficiency", key: true, title: "Query targeting (scanned ÷ returned)", keys: ["query_targeting", "query_targeting_keys"], unit: "", short: { query_targeting: "documents", query_targeting_keys: "index keys" } },
  { g: "Query efficiency", key: true, title: "Scan and order (in-memory sorts)", keys: ["scan_and_order_s"], unit: "/s" },
  { g: "Query efficiency", title: "Write conflicts", keys: ["write_conflicts_s"], unit: "/s" },
  { g: "Query efficiency", title: "Longest running operation", keys: ["longest_op_s"], unit: "s" },
  { g: "Connections & cursors", key: true, title: "Connections", keys: ["connections_current", "connections_active"], unit: "", short: { connections_current: "current", connections_active: "active" } },
  { g: "Connections & cursors", title: "Connections used", keys: ["connections_used_pct"], unit: "%" },
  { g: "Connections & cursors", title: "New connections", keys: ["connections_created_s"], unit: "/s" },
  { g: "Connections & cursors", title: "Open cursors", keys: ["cursors_open", "cursors_notimeout"], unit: "", short: { cursors_open: "open", cursors_notimeout: "no timeout" } },
  { g: "Connections & cursors", title: "Cursors timed out", keys: ["cursors_timedout_s"], unit: "/s" },
  { g: "Concurrency", key: true, title: "Queued operations", keys: ["queue_readers", "queue_writers"], unit: "", short: { queue_readers: "readers", queue_writers: "writers" } },
  { g: "Concurrency", title: "Active clients", keys: ["active_readers", "active_writers"], unit: "", short: { active_readers: "readers", active_writers: "writers" } },
  { g: "Concurrency", title: "Tickets available", keys: ["tickets_read_avail", "tickets_write_avail"], unit: "", short: { tickets_read_avail: "read", tickets_write_avail: "write" } },
  { g: "WiredTiger cache", key: true, title: "Cache usage", keys: ["cache_used_pct", "cache_dirty_pct"], unit: "%", short: { cache_used_pct: "used", cache_dirty_pct: "dirty" } },
  { g: "WiredTiger cache", title: "Cache I/O", keys: ["cache_read_bps", "cache_written_bps"], unit: "B/s", short: { cache_read_bps: "read into", cache_written_bps: "written from" } },
  { g: "WiredTiger cache", title: "Page evictions", keys: ["cache_evicted_s", "cache_app_evictions_s"], unit: "/s", short: { cache_evicted_s: "all", cache_app_evictions_s: "by app threads" } },
  { g: "WiredTiger cache", title: "Last checkpoint duration", keys: ["checkpoint_ms"], unit: "ms" },
  { g: "Resources", key: true, hideKeyIfNoSrv: true, title: "Normalized process CPU", keys: ["cpu_process_pct"], unit: "%" },
  { g: "Resources", title: "Memory", keys: ["mem_resident_mb", "mem_virtual_mb"], unit: "MB", short: { mem_resident_mb: "resident", mem_virtual_mb: "virtual" } },
  { g: "Resources", title: "Resident memory of host RAM", keys: ["mem_resident_pct"], unit: "%" },
  { g: "Resources", title: "Page faults", keys: ["page_faults_s"], unit: "/s" },
  { g: "Resources", title: "Network", keys: ["net_in_bps", "net_out_bps"], unit: "B/s", short: { net_in_bps: "in", net_out_bps: "out" } },
  { g: "Resources", title: "Data volume used", keys: ["disk_used_pct"], unit: "%" },
  { g: "Storage", title: "Database sizes", keys: ["data_size_gb", "storage_size_gb", "index_size_gb"], unit: "GB", short: { data_size_gb: "data", storage_size_gb: "on disk", index_size_gb: "indexes" } },
  { g: "Storage", title: "Collections and indexes", keys: ["collections", "indexes"], unit: "", short: { collections: "collections", indexes: "indexes" } },
  { g: "Replication", key: true, title: "Replication lag", keys: ["repl_lag_s"], unit: "s" },
  { g: "Replication", title: "Oplog window and headroom", keys: ["oplog_window_h", "repl_headroom_h"], unit: "h", short: { oplog_window_h: "oplog window", repl_headroom_h: "headroom" } },
  { g: "Replication", title: "Oplog GB per hour", keys: ["oplog_gb_per_hour"], unit: "GB/h" },
  { g: "Transactions & other", title: "Transactions", keys: ["txn_open", "txn_active"], unit: "", short: { txn_open: "open", txn_active: "active" } },
  { g: "Transactions & other", title: "Transaction outcomes", keys: ["txn_committed_s", "txn_aborted_s"], unit: "/s", short: { txn_committed_s: "committed", txn_aborted_s: "aborted" } },
  { g: "Transactions & other", title: "TTL deletions", keys: ["ttl_deleted_s"], unit: "doc/s" },
  { g: "Transactions & other", title: "Asserts", keys: ["asserts_regular_s", "asserts_warning_s", "asserts_user_s"], unit: "/s", short: { asserts_regular_s: "regular", asserts_warning_s: "warning", asserts_user_s: "user" } },
  // Without SSH access to the host (deployments not created by MongoPilot): CPU and memory reported by MongoDB
  { g: "Host", noSrv: true, key: true, title: "CPU · serverStatus.extra_info", keys: ["cpu_user_pct", "cpu_sys_pct"], unit: "%", sum: true, short: { cpu_user_pct: "user", cpu_sys_pct: "system" } },
  { g: "Host", noSrv: true, key: true, title: "Memory · serverStatus.mem", keys: ["mem_resident_mb", "mem_virtual_mb"], unit: "MB", short: { mem_resident_mb: "resident", mem_virtual_mb: "virtual" } },
  { g: "Host", noSrv: true, title: "Resident memory of host RAM", keys: ["mem_resident_pct"], unit: "%" },
  { g: "Host", noSrv: true, title: "CPU cores used", keys: ["cpu_cores_used"], unit: "cores" },
  { g: "Host", src: "srv", key: true, title: "Normalized system CPU", keys: ["sys_cpu_pct", "sys_iowait_pct"], unit: "%", short: { sys_cpu_pct: "busy", sys_iowait_pct: "I/O wait" } },
  { g: "Host", src: "srv", key: true, title: "Disk latency", keys: ["disk_read_latency_ms", "disk_write_latency_ms"], unit: "ms", short: { disk_read_latency_ms: "read", disk_write_latency_ms: "write" } },
  { g: "Host", src: "srv", title: "Disk IOPS", keys: ["disk_read_iops", "disk_write_iops"], unit: "IOPS", short: { disk_read_iops: "read", disk_write_iops: "write" } },
  { g: "Host", src: "srv", title: "Disk utilization", keys: ["disk_util_pct"], unit: "%" },
  { g: "Host", src: "srv", title: "Disk space free", keys: ["disk_free_pct"], unit: "%" },
  { g: "Host", src: "srv", title: "System memory used", keys: ["sys_mem_used_pct", "swap_used_pct"], unit: "%", short: { sys_mem_used_pct: "memory", swap_used_pct: "swap" } },
  { g: "Host", src: "srv", title: "Load average per core", keys: ["load_per_core"], unit: "" },
];
const CHART_GROUPS = ["Key metrics", ...new Set(CHARTS.map((c) => c.g))];

function drawChart(el, ts, seriesList, unit) {
  const W = Math.max(260, el.clientWidth), H = 120, padL = 46, padR = 6, padT = 6, padB = 18;
  const all = seriesList.flatMap((s) => s.values).filter((v) => v !== null && v !== undefined);
  if (!all.length || ts.length < 2) { el.innerHTML = `<div class="empty small" style="padding:40px 0">No data in this period</div>`; return; }
  let max = Math.max(...all), min = Math.min(0, ...all);
  if (unit === "%") max = Math.max(max, 10);
  if (max === min) max = min + 1;
  max *= 1.08;
  const t0 = ts[0], t1 = ts[ts.length - 1];
  const x = (t) => padL + ((t - t0) / Math.max(1, t1 - t0)) * (W - padL - padR);
  const y = (v) => padT + (1 - (v - min) / (max - min)) * (H - padT - padB);
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img">`;
  for (let i = 0; i <= 2; i++) {
    const v = min + ((max - min) * i) / 2;
    svg += `<line class="grid" x1="${padL}" x2="${W - padR}" y1="${y(v)}" y2="${y(v)}"/>
      <text class="axis" x="${padL - 6}" y="${y(v) + 3}" text-anchor="end">${esc(unit === "B/s" ? fmtBytes(v).replace(" ", "") : fmtNum(v, max < 5 ? 1 : 0))}</text>`;
  }
  const tf = (t) => new Date(t * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  svg += `<text class="axis" x="${padL}" y="${H - 3}">${tf(t0)}</text><text class="axis" x="${W - padR}" y="${H - 3}" text-anchor="end">${tf(t1)}</text>`;
  seriesList.forEach((s, si) => {
    let d = "", pen = false;
    s.values.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(ts[i]).toFixed(1)},${y(v).toFixed(1)}`; pen = true;
    });
    svg += `<path d="${d}" fill="none" stroke="${COLORS[si % COLORS.length]}" stroke-width="1.6" stroke-linejoin="round"/>`;
  });
  svg += `<line class="hover-line" x1="0" x2="0" y1="${padT}" y2="${H - padB}" visibility="hidden"/><rect x="${padL}" y="0" width="${W - padL - padR}" height="${H}" fill="transparent"/></svg>`;
  el.innerHTML = svg;
  const svgEl = el.querySelector("svg"), hl = el.querySelector(".hover-line");
  let tip = $(".chart-tip");
  if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip"; document.body.appendChild(tip); }
  svgEl.addEventListener("mousemove", (ev) => {
    const r = svgEl.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    const t = t0 + ((px - padL) / (W - padL - padR)) * (t1 - t0);
    let idx = 0, best = Infinity;
    ts.forEach((tt, i) => { const dd = Math.abs(tt - t); if (dd < best) { best = dd; idx = i; } });
    hl.setAttribute("x1", x(ts[idx])); hl.setAttribute("x2", x(ts[idx])); hl.setAttribute("visibility", "visible");
    tip.innerHTML = `${dateTime(ts[idx])}<br>` + seriesList.map((s, si) =>
      `<span style="color:${COLORS[si % COLORS.length]}">■</span> ${esc(s.label)}: ${esc(fmtValue(s.values[idx], unit))}`).join("<br>");
    tip.style.display = "block";
    tip.style.left = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = ev.clientY + 14 + "px";
  });
  svgEl.addEventListener("mouseleave", () => { tip.style.display = "none"; hl.setAttribute("visibility", "hidden"); });
}

/* subjects: { proc: "proc:12", srv: "srv:3" }; group: a chart group or "Key metrics" */
async function renderCharts(container, subjects, group) {
  const wanted = CHARTS.filter((c) => (group === "Key metrics" ? c.key && !(c.hideKeyIfNoSrv && !subjects.srv) : c.g === group)
    && subjects[c.src || "proc"] && !(c.noSrv && subjects.srv));
  const sources = [...new Set(wanted.map((c) => c.src || "proc"))];
  const data = {};
  await Promise.all(sources.map(async (src) => {
    data[src] = await api(`/metrics?subject=${encodeURIComponent(subjects[src])}&range=${state.range}`);
  }));
  const available = wanted.filter((c) => c.keys.some((k) => (data[c.src || "proc"].series[k] || []).some((v) => v !== null)));
  if (!available.length) {
    container.innerHTML = `<div class="panel empty">${group === "Host" && !subjects.srv ? "Host metrics need this host to be registered on the Servers page." : "No metrics for this group in the selected period."}</div>`;
    return;
  }
  container.innerHTML = `<div class="charts">${available.map((c, i) => {
    const d = data[c.src || "proc"];
    return `<div class="panel chart" data-i="${i}"><div class="chart-head"><h3>${esc(c.title)}</h3><span class="now"></span></div><div class="plot"></div>
      ${c.keys.length > 1 ? `<div class="legend">${c.keys.filter((k) => d.series[k]).map((k, ki) => `<span><i style="background:${COLORS[ki % COLORS.length]}"></i>${esc(c.short?.[k] || k)}</span>`).join("")}</div>` : ""}</div>`;
  }).join("")}</div>`;
  const paint = () => available.forEach((c, i) => {
    const box = container.querySelector(`.chart[data-i="${i}"]`);
    if (!box) return;
    const d = data[c.src || "proc"];
    const series = c.keys.filter((k) => d.series[k]).map((k) => ({ label: c.short?.[k] || metricMeta(k).label, values: d.series[k] }));
    drawChart(box.querySelector(".plot"), d.ts, series, c.unit);
    const last = series.map((s) => [...s.values].reverse().find((v) => v !== null && v !== undefined));
    box.querySelector(".now").textContent = c.sum ? fmtValue(last.reduce((a, b) => a + (b || 0), 0), c.unit)
      : series.length === 1 ? fmtValue(last[0], c.unit) : "";
  });
  paint();
  container._paint = paint;
}
window.addEventListener("resize", () => {
  clearTimeout(window._rz);
  window._rz = setTimeout(() => $$("[data-charts]").forEach((c) => c._paint && c._paint()), 150);
});

/* ---------------- tags ---------------- */
const TAG_LABEL = { organization: "Organization", project: "Project", cluster_type: "Cluster type", license_key: "License key", license_type: "License type" };
const ctChip = (t) => (t ? `<span class="chip ct-${esc(t)}">${esc(t)}</span>` : "");
function maskKey(k) { return !k ? "" : k.length <= 6 ? "•••" : `${k.slice(0, 4)}…${k.slice(-3)}`; }
function tagChips(tags) {
  const t = tags || {};
  return [ctChip(t.cluster_type),
    t.organization ? `<span class="chip" title="Organization">${esc(t.organization)}</span>` : "",
    t.project ? `<span class="chip" title="Project">${esc(t.project)}</span>` : "",
    t.license_type ? `<span class="chip" title="License type">License ${esc(t.license_type.toLowerCase())}</span>` : "",
    t.license_key ? `<span class="chip mono" title="License key">${esc(maskKey(t.license_key))}</span>` : ""].join("");
}
function tagFields(tags = {}, deps = []) {
  const distinct = (k) => [...new Set(deps.map((d) => d.tags?.[k]).filter(Boolean))].sort();
  return `<div class="grid2"><div class="field"><label for="t-org">Organization</label><input id="t-org" name="tag_organization" list="dl-org" value="${esc(tags.organization || "")}">
      <datalist id="dl-org">${distinct("organization").map((v) => `<option value="${esc(v)}">`).join("")}</datalist></div>
    <div class="field"><label for="t-prj">Project</label><input id="t-prj" name="tag_project" list="dl-prj" value="${esc(tags.project || "")}">
      <datalist id="dl-prj">${distinct("project").map((v) => `<option value="${esc(v)}">`).join("")}</datalist></div></div>
    <div class="grid3"><div class="field"><label for="t-ct">Cluster type</label><select id="t-ct" name="tag_cluster_type"><option value="">Not set</option>
      ${state.catalog.cluster_types.map((v) => `<option ${tags.cluster_type === v ? "selected" : ""}>${v}</option>`).join("")}</select></div>
    <div class="field"><label for="t-lk">License key</label><input id="t-lk" name="tag_license_key" class="mono" value="${esc(tags.license_key || "")}"></div>
    <div class="field"><label for="t-lt">License type</label><select id="t-lt" name="tag_license_type"><option value="">Not set</option>
      ${state.catalog.license_types.map((v) => `<option ${tags.license_type === v ? "selected" : ""}>${v}</option>`).join("")}</select></div></div>`;
}
const readTags = (fd) => Object.fromEntries(Object.keys(TAG_LABEL).map((k) => [k, (fd.get(`tag_${k}`) || "").trim()]));

/* ---------------- overview / deployments ---------------- */
function createButtons() {
  return `<button id="btn-register">Monitor an existing deployment</button><a class="btn" href="#/new-sharded">Create sharded cluster</a><a class="btn primary" href="#/new-replicaset">Create replica set</a>`;
}
async function pageHome() {
  const [o, deps, alerts] = await Promise.all([api("/overview"), api("/deployments"), api("/alerts?status=active")]);
  state.overview = o;
  const crit = o.alerts_open.critical || 0, warn = o.alerts_open.warning || 0;
  main.innerHTML = `
    ${o.demo ? `<div class="demo-banner">Demo mode: servers and clusters are simulated. Every feature works without touching real systems.</div>` : ""}
    <div class="page-head"><div><h1>Overview</h1><div class="sub">Refreshed every ${o.poll_interval} seconds</div></div>
      <div class="row">${createButtons()}</div></div>
    <div class="panel health-line">
      <div class="item"><b>${o.deployments}</b><span>deployment${o.deployments === 1 ? "" : "s"}${o.deployments_error ? ` · ${o.deployments_error} unreachable` : ""}</span></div>
      <div class="item ${o.processes_up < o.processes ? "bad" : ""}"><b>${o.processes_up}/${o.processes}</b><span>processes reachable</span></div>
      <div class="item ${crit ? "bad" : ""}"><b>${crit}</b><span>critical alerts open</span></div>
      <div class="item"><b>${warn}</b><span>warnings open</span></div>
      <div class="item"><b>${fmtNum(o.slow_queries_24h, 0)}</b><span><a href="#/queries">slow queries in 24 h</a></span></div>
      <div class="item"><b>${o.jobs_running}</b><span>jobs running</span></div>
    </div>
    <div class="section"><div class="row"><h2 class="grow">Deployments</h2><a href="#/deployments" class="small">Filter deployments</a></div>
      <div class="panel table-wrap">${deploymentsTable(deps)}</div></div>
    <div class="section"><div class="row"><h2 class="grow">Active alerts</h2><a href="#/alerts" class="small">All alerts</a></div>
      <div class="panel table-wrap">${alertsTable(alerts.slice(0, 8))}</div></div>`;
  bindDeploymentRows();
  bindAlertActions();
  $("#btn-register").onclick = () => registerDeployment(deps);
  autoRefresh(async () => { if (state.route === location.hash) await pageHome(); }, Math.max(5000, o.poll_interval * 1000));
}
function deploymentsTable(deps, emptyText) {
  if (!deps.length) return `<div class="empty"><h3>${emptyText ? "No deployment matches the filters" : "No deployments yet"}</h3><p class="small" style="margin:0 auto">${emptyText || "Connect an existing cluster with its connection string, or create a new replica set or sharded cluster on your servers."}</p></div>`;
  return `<table><thead><tr><th>Name</th><th>Status</th><th>Cluster type</th><th>Topology</th><th>Organization · project</th><th>License</th><th>Processes</th><th>Version</th><th>Management</th></tr></thead><tbody>
  ${deps.map((d) => `<tr class="clickable" data-dep="${d.id}">
    <td><a href="#/deployments/${d.id}">${esc(d.name)}</a></td><td>${deploymentHealth(d)}</td>
    <td>${ctChip(d.tags?.cluster_type) || `<span class="muted small">–</span>`}</td>
    <td>${esc(TOPO_EN[d.topology] || "–")}${d.tls_mode && d.tls_mode !== "disabled" ? ` <span class="tag" title="TLS mode">TLS</span>` : ""}</td>
    <td class="small">${esc(d.tags?.organization || "–")}${d.tags?.project ? `<div class="muted">${esc(d.tags.project)}</div>` : ""}</td>
    <td class="small">${esc(d.tags?.license_type || "–")}${d.tags?.license_key ? `<div class="muted mono">${esc(maskKey(d.tags.license_key))}</div>` : ""}</td>
    <td>${miniTopo(d)} <span class="muted small">${d.processes_up}/${d.processes_total}</span></td>
    <td class="mono">${esc((d.status?.versions || []).join(", ") || "–")}</td>
    <td>${d.managed ? `<span class="tag">automation</span>` : `<span class="tag">monitoring only</span>`}</td></tr>`).join("")}</tbody></table>`;
}
function bindDeploymentRows() {
  $$("tr[data-dep]").forEach((tr) => tr.addEventListener("click", (ev) => {
    if (ev.target.closest("a, button")) return;
    location.hash = `#/deployments/${tr.dataset.dep}`;
  }));
}

const FILTER_KEYS = ["q", "topology", "cluster_type", "organization", "project", "license_type", "license_key"];
function loadFilters() {
  try { return JSON.parse(localStorage.getItem("mp.filters") || "{}"); } catch { return {}; }
}
function saveFilters(f) {
  try { localStorage.setItem("mp.filters", JSON.stringify(f)); } catch { /* storage unavailable */ }
}
function matches(d, f) {
  const t = d.tags || {};
  if (f.q && !d.name.toLowerCase().includes(f.q.toLowerCase())) return false;
  if (f.topology && d.topology !== f.topology) return false;
  for (const k of ["cluster_type", "organization", "project", "license_type"]) {
    if (f[k] && (f[k] === "__none" ? t[k] : t[k] !== f[k])) return false;
  }
  if (f.license_key && !(t.license_key || "").toLowerCase().includes(f.license_key.toLowerCase())) return false;
  return true;
}
async function pageDeployments() {
  const deps = await api("/deployments");
  const f = state.filters || (state.filters = loadFilters());
  const distinct = (k) => [...new Set(deps.map((d) => d.tags?.[k]).filter(Boolean))].sort();
  const sel = (key, label, values, display = (v) => v) => `<div><label for="f-${key}">${label}</label><select id="f-${key}" data-f="${key}">
      <option value="">All</option>${values.map((v) => `<option value="${esc(v)}" ${f[key] === v ? "selected" : ""}>${esc(display(v))}</option>`).join("")}
      <option value="__none" ${f[key] === "__none" ? "selected" : ""}>Not set</option></select></div>`;
  main.innerHTML = `<div class="page-head"><div><h1>Deployments</h1><div class="sub">Filter by name, topology and tags</div></div>
      <div class="row">${createButtons()}</div></div>
    <div class="panel filters">
      <div><label for="f-q">Cluster name</label><input id="f-q" data-f="q" type="search" value="${esc(f.q || "")}" placeholder="Search by name"></div>
      <div><label for="f-topology">Topology</label><select id="f-topology" data-f="topology"><option value="">All</option>
        ${Object.entries(TOPO_EN).map(([k, v]) => `<option value="${k}" ${f.topology === k ? "selected" : ""}>${v}</option>`).join("")}</select></div>
      ${sel("cluster_type", "Cluster type", state.catalog.cluster_types)}
      ${sel("organization", "Organization", distinct("organization"))}
      ${sel("project", "Project", distinct("project"))}
      ${sel("license_type", "License type", state.catalog.license_types)}
      <div><label for="f-license_key">License key</label><input id="f-license_key" data-f="license_key" value="${esc(f.license_key || "")}" placeholder="Contains"></div>
    </div>
    <div class="row" style="margin:12px 0"><span class="grow small muted" id="f-count"></span><button class="small" id="f-clear">Clear filters</button></div>
    <div class="panel table-wrap" id="dep-table"></div>`;
  const render = () => {
    const shown = deps.filter((d) => matches(d, f));
    $("#f-count").textContent = `${shown.length} of ${deps.length} deployment${deps.length === 1 ? "" : "s"}`;
    $("#dep-table").innerHTML = deploymentsTable(shown, deps.length ? "Change or clear the filters to see more deployments." : "");
    bindDeploymentRows();
  };
  $$("[data-f]").forEach((el) => el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => {
    f[el.dataset.f] = el.value; saveFilters(f); render();
  }));
  $("#f-clear").onclick = () => { FILTER_KEYS.forEach((k) => delete f[k]); saveFilters(f); pageDeployments(); };
  $("#btn-register").onclick = () => registerDeployment(deps);
  render();
}
function registerDeployment(deps = []) {
  openModal({
    title: "Monitor an existing deployment", submit: "Add and check", wide: true,
    body: `<div class="field"><label for="f-name">Name</label><input id="f-name" name="name" required placeholder="e.g. billing-prod"></div>
      <div class="field"><label for="f-uri">Connection string</label><input id="f-uri" name="uri" required class="mono" placeholder="mongodb://mongopilot-monitor:password@host1:27017,host2:27017/?replicaSet=rs0&authSource=admin">
      <div class="hint">For a sharded cluster, list the mongos routers. The user needs the <code>clusterMonitor</code> role, <code>read</code> on <code>local</code> (oplog window) and the index inspection privileges described in the README for Query performance. MongoPilot must reach every member.</div></div>
      <h3>Tags</h3>${tagFields({}, deps)}`,
    onSubmit: async (fd) => {
      const r = await api("/deployments", { method: "POST", body: { name: fd.get("name"), uri: fd.get("uri"), tags: readTags(fd) } });
      if (r.error) toast(`Added, but the first connection failed: ${r.error}`, true);
      else toast("Deployment added");
      location.hash = `#/deployments/${r.id}`;
    },
  });
}

function deploymentHeader(d, tab) {
  const running = d.jobs.find((j) => j.status === "running" || j.status === "queued");
  const tabs = [["", "Overview"], ["parameters", "Parameters"], ...(d.managed ? [["config", "Configuration files"], ["tls", "TLS"]] : [])];
  return `<div class="page-head"><div><div class="small"><a href="#/deployments">Deployments</a></div>
      <h1>${esc(d.name)}</h1><div class="sub row">${deploymentHealth(d)}
      <span>${esc(TOPO_EN[d.topology] || "")}</span>${d.status?.versions?.length ? `<span class="mono">${esc(d.status.versions.join(", "))}</span>` : ""}
      ${d.status?.fcv ? `<span>FCV <span class="mono">${esc(d.status.fcv)}</span></span>` : ""}
      ${d.tls_mode && d.tls_mode !== "disabled" ? `<span class="tag">TLS ${esc(d.tls_mode)}</span>` : ""}<span>checked ${esc(ago(d.last_poll))}</span></div>
      <div class="tag-row">${tagChips(d.tags)}</div></div>
      <div class="row">
        <a class="btn" href="#/queries/${d.id}">Query performance</a>
        ${d.managed && !d.paused && !tab ? `<button data-act="rolling_restart" ${running ? "disabled" : ""}>Rolling restart</button>
          <button data-act="rolling_upgrade" ${running ? "disabled" : ""}>Upgrade version</button>
          <button data-act="set_fcv" ${running ? "disabled" : ""}>Set FCV</button>` : ""}
        ${!tab ? `<button id="btn-poll">Refresh now</button>` : ""}
        <button id="btn-more">Settings and tags</button>
      </div></div>
    ${running ? `<div class="notice info">Job in progress: <a href="#/jobs/${running.id}">${esc(running.title)}</a></div>` : ""}
    <nav class="subnav">${tabs.map(([k, l]) => `<a href="#/deployments/${d.id}${k ? "/" + k : ""}" class="${(tab || "") === k ? "on" : ""}">${l}</a>`).join("")}</nav>`;
}
function bindHeader(d) {
  const bp = $("#btn-poll");
  if (bp) bp.onclick = async () => { await api(`/deployments/${d.id}/poll`, { method: "POST" }); toast("Status refreshed"); route(); };
  $("#btn-more").onclick = () => deploymentMenu(d);
  $$("[data-act]").forEach((b) => (b.onclick = () => deploymentAction(d, b.dataset.act)));
}

async function pageDeployment(id, tab) {
  id = Number(id);
  const d = await api(`/deployments/${id}`);
  if (tab === "parameters") return pageParameters(d);
  if (tab === "config") return pageConfigFiles(d);
  if (tab === "tls") return pageTls(d);
  const procs = d.processes;
  let sel = state.selectedProc[id];
  if (!procs.find((p) => p.id === sel)) sel = (procs.find((p) => p.role === "PRIMARY" || p.role === "MONGOS") || procs[0] || {}).id;
  state.selectedProc[id] = sel;
  const selProc = procs.find((p) => p.id === sel);
  const mi = d.managed_info;
  const running = d.jobs.find((j) => j.status === "running" || j.status === "queued");
  const dm = d.metrics || {};
  const hasShard = procs.some((p) => p.shard);
  main.innerHTML = `${deploymentHeader(d, "")}
    ${d.last_error && !d.paused ? `<div class="notice crit"><b>Connection failed.</b> ${esc(d.last_error)}<br><span class="small">Check that MongoPilot can reach the hosts and that the user has the clusterMonitor role.</span></div>` : ""}
    ${d.paused && d.last_error !== "Being created" ? `<div class="notice">Monitoring is paused: no samples are collected and alerts are suspended.</div>` : ""}
    <div class="panel panel-pad"><div class="row" style="margin-bottom:12px"><h2 class="grow" style="margin:0">Topology</h2>
      ${dm.members_unhealthy ? pill("warn", `${dm.members_unhealthy} unhealthy members`) : ""}
      ${dm.balancer_enabled === 0 ? pill("warn", "balancer disabled") : dm.balancer_enabled ? pill("ok", "balancer enabled") : ""}
      ${dm.jumbo_chunks ? pill("warn", `${dm.jumbo_chunks} jumbo chunks`) : ""}</div>
      ${topologyHTML(procs, sel)}<p class="small muted" style="margin:10px 0 0">Select a process to see its metrics.</p></div>

    <div class="section"><div class="row" style="margin-bottom:12px"><h2 class="grow" style="margin:0">Metrics · <span class="mono" style="font-weight:500">${esc(selProc?.host || "–")}</span></h2>
      ${rangeSeg(state.range)}</div>
      <div class="group-bar" role="group" aria-label="Metric group">${CHART_GROUPS.map((g) => `<button type="button" data-group="${esc(g)}" class="${state.chartGroup === g ? "on" : ""}">${esc(g)}</button>`).join("")}</div>
      ${selProc && !selProc.server_id ? `<p class="small muted" style="margin:-4px 0 12px">No SSH access to this host: CPU and memory come from MongoDB (<code>serverStatus().extra_info</code> user and system time, <code>serverStatus().mem</code>) and cover the ${selProc.role === "MONGOS" ? "mongos" : "mongod"} process, not the whole machine. Register the host on the <a href="#/servers">Servers</a> page for system CPU, disk and memory.</p>` : ""}
      <div id="charts" data-charts></div></div>

    ${selProc?.info?.slow_ops_sample?.length ? `<div class="section"><h2>Slow operations running on ${esc(selProc.host)}</h2><div class="panel table-wrap"><table>
      <thead><tr><th>opid</th><th>Namespace</th><th>Type</th><th class="num">Running for</th><th>Client</th></tr></thead><tbody>
      ${selProc.info.slow_ops_sample.map((o) => `<tr><td class="mono">${esc(o.opid)}</td><td class="mono">${esc(o.ns)}</td><td>${esc(o.op)}</td><td class="num">${esc(fmtDur(o.secs || 0))}</td><td class="mono">${esc(o.client || "")}</td></tr>`).join("")}
      </tbody></table></div></div>` : ""}

    <div class="section"><h2>Processes</h2><div class="panel table-wrap"><table><thead><tr><th>Host</th><th>Tools</th><th>Role</th>${hasShard ? "<th>Shard</th>" : ""}<th>Version</th>
      <th class="num">Connections</th><th class="num">Op/s</th><th class="num">Read latency</th><th class="num">Targeting</th><th class="num">Lag</th><th class="num">CPU · RAM</th><th class="num">Uptime</th>${d.managed ? "<th></th>" : ""}</tr></thead><tbody>
      ${procs.map((p) => { const m = p.metrics || {}; return `<tr>
        <td class="mono">${esc(p.host)}</td><td>${toolSelect(d.id, p)}</td><td>${roleCell(p)}</td>
        ${hasShard ? `<td>${esc(p.shard || "–")}</td>` : ""}<td class="mono">${esc(p.version || "–")}</td>
        <td class="num">${fmtNum(m.connections_current, 0)}</td><td class="num">${fmtNum(m.ops_total)}</td>
        <td class="num">${m.lat_reads_ms !== undefined ? fmtNum(m.lat_reads_ms) + " ms" : "–"}</td>
        <td class="num">${targetingCell(m.query_targeting)}</td>
        <td class="num">${m.repl_lag_s !== undefined ? esc(fmtDur(m.repl_lag_s)) : "–"}</td>
        <td class="num" style="white-space:nowrap" title="CPU from serverStatus().extra_info, resident memory from serverStatus().mem">${m.cpu_process_pct !== undefined ? fmtNum(m.cpu_process_pct) + "%" : "–"}
          ${m.mem_resident_mb !== undefined ? `<div class="muted small">${fmtBytes(m.mem_resident_mb * 1048576)}${m.mem_resident_pct !== undefined ? ` · ${fmtNum(m.mem_resident_pct, 0)}%` : ""}</div>` : ""}</td>
        <td class="num">${p.info?.uptime ? esc(fmtDur(p.info.uptime)) : "–"}</td>
        ${d.managed ? `<td class="num"><button class="small" data-proc-act="${esc(p.host)}" ${running ? "disabled" : ""}>Actions</button></td>` : ""}</tr>`; }).join("")}
      </tbody></table></div></div>

    ${mi ? `<div class="section"><h2>Managed configuration</h2><div class="panel panel-pad"><dl class="kv">
      ${mi.kind === "sharded" ? `<dt>Config server replica set</dt><dd class="mono">${esc(mi.config_rs)}</dd>` : `<dt>Replica set</dt><dd class="mono">${esc(mi.rs_name)}</dd>`}
      <dt>Binary version</dt><dd class="mono">${esc(mi.version)}</dd>
      <dt>Admin user</dt><dd class="mono">${esc(mi.admin_user)}</dd><dt>Bind IP</dt><dd class="mono">${esc(mi.bind_ip)}</dd>
      <dt>Data directory</dt><dd class="mono">${esc(mi.data_root)}</dd></dl>
      <div class="table-wrap" style="margin-top:14px"><table><thead><tr><th>Process</th><th>Server</th><th>Role</th>${mi.kind === "sharded" ? "<th>Replica set</th>" : ""}<th class="num">Priority</th><th class="num">Votes</th><th>Settings</th><th>systemd unit</th></tr></thead><tbody>
      ${mi.members.map((m) => `<tr><td class="mono">${esc(m.host)}</td><td>${esc(m.server)}</td>
        <td>${{ replica: "mongod", configsvr: "config server", shardsvr: "shard member", mongos: "mongos" }[m.role] || m.role}</td>
        ${mi.kind === "sharded" ? `<td class="mono small">${esc(m.rs_name || "–")}</td>` : ""}
        <td class="num">${m.role === "mongos" ? "–" : m.priority}</td><td class="num">${m.role === "mongos" ? "–" : m.votes}</td>
        <td class="small">${[m.arbiter ? "arbiter" : "", m.hidden ? "hidden" : "", m.delay ? `delay ${fmtDur(m.delay)}` : "", m.customized ? "edited config" : ""].filter(Boolean).join(", ") || "–"}</td>
        <td class="mono small">${esc(m.unit)}</td></tr>`).join("")}</tbody></table></div></div></div>` : ""}

    <div class="section"><h2>Recent jobs</h2><div class="panel table-wrap">${jobsTable(d.jobs, false)}</div></div>`;

  bindHeader(d);
  $$("[data-proc]").forEach((b) => (b.onclick = () => { state.selectedProc[id] = Number(b.dataset.proc); pageDeployment(id); }));
  $$("[data-range]").forEach((b) => (b.onclick = () => { state.range = Number(b.dataset.range); pageDeployment(id); }));
  $$("[data-group]").forEach((b) => (b.onclick = () => { state.chartGroup = b.dataset.group; pageDeployment(id); }));
  $$("[data-proc-act]").forEach((b) => (b.onclick = () => processAction(d, b.dataset.procAct)));
  bindToolSelects();
  bindJobRows();
  if (sel) await renderCharts($("#charts"), { proc: `proc:${sel}`, srv: selProc.server_id ? `srv:${selProc.server_id}` : null }, state.chartGroup);
  autoRefresh(async () => { if (state.route === location.hash) await pageDeployment(id); }, Math.max(15000, (state.overview?.poll_interval || 30) * 1000));
}
function targetingCell(v) {
  if (v === undefined || v === null) return "–";
  const cls = v >= 1000 ? "bad" : v >= 100 ? "warn" : "";
  return `<span class="targeting ${cls}" title="documents examined per document returned">${fmtNum(v)}</span>`;
}
async function deploymentMenu(d) {
  const deps = await api("/deployments").catch(() => []);
  openModal({
    title: `Settings for ${d.name}`, submit: "Save", wide: true,
    body: `<div class="field"><label for="f-n">Name</label><input id="f-n" name="name" value="${esc(d.name)}"></div>
      ${d.managed ? "" : `<div class="field"><label for="f-u">New connection string</label><input id="f-u" name="uri" class="mono" placeholder="leave empty to keep the current one"></div>`}
      <h3>Tags</h3>${tagFields(d.tags, deps)}
      <label class="check"><input type="checkbox" name="paused" ${d.paused ? "checked" : ""}>Pause monitoring and alerts</label>
      <div class="section" style="margin-top:22px"><h3>Remove from MongoPilot</h3>
      <p class="small muted">Deletes history, alerts and configuration from MongoPilot. MongoDB processes and data on the servers are not touched.</p>
      <button type="button" class="danger" id="btn-del">Remove deployment</button></div>`,
    onOpen: (form) => {
      $("#btn-del", form).onclick = () => {
        $("#modal").close();
        confirmAction("Remove this deployment?", `History and alerts of <b>${esc(d.name)}</b> will be deleted. Servers are not modified.`, "Remove",
          async () => { await api(`/deployments/${d.id}`, { method: "DELETE" }); toast("Deployment removed"); location.hash = "#/deployments"; }, true);
      };
    },
    onSubmit: async (fd) => {
      const body = { name: fd.get("name"), paused: fd.get("paused") === "on", tags: readTags(fd) };
      if (fd.get("uri")) body.uri = fd.get("uri");
      await api(`/deployments/${d.id}`, { method: "PATCH", body });
      toast("Settings saved");
      route();
    },
  });
}
async function submitAction(d, body) {
  const r = await api(`/deployments/${d.id}/actions`, { method: "POST", body });
  toast("Job started");
  location.hash = `#/jobs/${r.job_id}`;
}
async function deploymentAction(d, act) {
  const mi = d.managed_info;
  if (act === "rolling_restart") {
    const text = mi.kind === "sharded"
      ? "Pauses the balancer, then restarts one process at a time: config servers, each shard (secondaries first, primary after a step-down) and finally the mongos routers."
      : "Restarts one member at a time: secondaries first, then the primary after a step-down. The replica set stays available, but connections to the primary are closed during the switch.";
    return confirmAction("Rolling restart", text, "Start rolling restart", () => submitAction(d, { type: "rolling_restart" }));
  }
  if (act === "set_fcv") {
    return openModal({
      title: "Set featureCompatibilityVersion", submit: "Set FCV",
      body: `<div class="notice">After raising FCV, going back to the previous binaries requires a downgrade procedure.</div>
        <div class="field"><label for="f-fcv">FCV</label><select id="f-fcv" name="fcv">${mi.fcv_options.map((v) => `<option ${v === d.status?.fcv ? "selected" : ""}>${v}</option>`).join("")}</select>
        <div class="hint">Current: ${esc(d.status?.fcv || "not detected")} · binaries ${esc(mi.version)}${mi.kind === "sharded" ? " · set through the mongos routers" : ""}</div></div>`,
      onSubmit: (fd) => submitAction(d, { type: "set_fcv", fcv: fd.get("fcv") }),
    });
  }
  if (act === "rolling_upgrade") {
    const versions = await api("/mongodb-versions").catch(() => ({ versions: [] }));
    const cur = mi.version.split(".").map(Number);
    const curMajor = mi.version.split(".").slice(0, 2).join(".");
    const allowed = versions.versions.filter((v) => {
      const mj = v.split(".").slice(0, 2).join(".");
      const n = v.split(".").map(Number);
      return (mj === curMajor && n[2] > cur[2]) || mi.upgrade_to.includes(mj);
    });
    return openModal({
      title: "Upgrade MongoDB version", submit: "Start upgrade",
      body: `<p>Current version <span class="mono">${esc(mi.version)}</span>, FCV <span class="mono">${esc(d.status?.fcv || "?")}</span>.
        You can move to a newer patch of ${esc(curMajor)}${mi.upgrade_to.length ? ` or to ${mi.upgrade_to.map((v) => `<span class="mono">${v}</span>`).join(" or ")}` : ""}.</p>
        <div class="field"><label for="f-ver">New version</label><input id="f-ver" name="version" class="mono" list="dl-ver" required placeholder="e.g. ${mi.upgrade_to[0] || curMajor}.x">
        <datalist id="dl-ver">${allowed.map((v) => `<option value="${v}">`).join("")}</datalist>
        <div class="hint">Binaries are installed on every server before any restart; then one process at a time${mi.kind === "sharded" ? " (balancer paused; config servers, shards, then mongos)" : ", primary last"}.</div></div>
        <div class="field"><label for="f-url">Tarball URL (optional)</label><input id="f-url" name="tarball_url" class="mono" placeholder="only for networks without access to mongodb.org"></div>`,
      onSubmit: (fd) => submitAction(d, { type: "rolling_upgrade", version: fd.get("version").trim(), tarball_url: fd.get("tarball_url") || undefined }),
    });
  }
}
function processAction(d, host) {
  const p = d.processes.find((x) => x.host === host);
  openModal({
    title: `Actions on ${host}`, submit: "Run",
    body: `${p?.role === "PRIMARY" ? `<div class="notice">This is the primary: stopping or restarting it triggers an election. For a clean restart use the rolling restart.</div>` : ""}
      <div class="field"><label for="f-a">Action</label><select id="f-a" name="action"><option value="restart">Restart the process</option>
      <option value="stop">Stop the process</option><option value="start">Start the process</option></select></div>`,
    onSubmit: (fd) => submitAction(d, { type: "process_action", host, action: fd.get("action") }),
  });
}

/* ---------------- parameters ---------------- */
function paramEditor(row) {
  const v = row.value, dis = row.read_only ? "disabled" : "";
  if (typeof v === "boolean") return `<select class="param-val" data-pv="${esc(row.name)}" ${dis}><option ${v ? "selected" : ""}>true</option><option ${!v ? "selected" : ""}>false</option></select>`;
  if (typeof v === "number") return `<input class="param-val" type="number" step="any" data-pv="${esc(row.name)}" value="${v}" ${dis}>`;
  if (v !== null && typeof v === "object") return `<textarea class="param-val" data-pv="${esc(row.name)}" ${dis}>${esc(JSON.stringify(v))}</textarea>`;
  return `<input class="param-val" data-pv="${esc(row.name)}" value="${esc(v ?? "")}" ${dis}>`;
}
async function pageParameters(d) {
  const host = state.paramHost?.[d.id];
  const r = await api(`/deployments/${d.id}/parameters${host ? `?host=${encodeURIComponent(host)}` : ""}`);
  state.paramHost = { ...(state.paramHost || {}), [d.id]: r.host };
  const f = state.paramFilter || (state.paramFilter = { q: "", runtime: false, persisted: false });
  main.innerHTML = `${deploymentHeader(d, "parameters")}
    ${r.managed ? "" : `<div class="notice info">This deployment is monitored only: parameters are read-only here. Deployments created by MongoPilot can be changed.</div>`}
    <div class="panel panel-pad row" style="gap:16px;margin-bottom:14px">
      <div><label for="p-host">Process</label><select id="p-host" style="width:auto">${r.processes.filter((p) => p.role !== "ARBITER").map((p) =>
        `<option ${p.host === r.host ? "selected" : ""} ${p.up ? "" : "disabled"}>${esc(p.host)}</option>`).join("")}</select></div>
      <div class="grow"><label for="p-q">Search</label><input id="p-q" type="search" value="${esc(f.q)}" placeholder="Parameter name"></div>
      <label class="check small" style="margin-top:18px"><input type="checkbox" id="p-rt" ${f.runtime ? "checked" : ""}>Changeable at runtime</label>
      <label class="check small" style="margin-top:18px"><input type="checkbox" id="p-ps" ${f.persisted ? "checked" : ""}>Saved in the configuration file</label>
    </div>
    <div class="row small muted" style="margin-bottom:8px"><span class="grow" id="p-count"></span><span>Values read live with <code>getParameter</code> from ${esc(r.host)}</span></div>
    <div class="panel table-wrap" id="p-table"></div>`;
  const render = () => {
    const rows = r.parameters.filter((p) => (!f.q || p.name.toLowerCase().includes(f.q.toLowerCase()))
      && (!f.runtime || p.runtime) && (!f.persisted || p.persisted));
    $("#p-count").textContent = `${rows.length} of ${r.parameters.length} parameters`;
    $("#p-table").innerHTML = `<table><thead><tr><th>Parameter</th><th style="width:38%">Value</th><th>Can be set</th><th>Configuration file</th>${r.managed ? "<th></th>" : ""}</tr></thead><tbody>
      ${rows.slice(0, 400).map((p) => `<tr><td class="mono small">${esc(p.name)}</td><td>${paramEditor(p)}</td>
        <td class="small">${p.read_only ? `<span class="muted">managed by MongoPilot</span>` : p.runtime === null ? `<span class="muted">unknown (MongoDB &lt; 5.0)</span>`
          : [p.runtime ? `<span class="chip">runtime</span>` : "", p.startup ? `<span class="chip">startup</span>` : ""].join(" ") || "–"}</td>
        <td class="small">${p.persisted ? `<span class="chip">saved: ${esc(JSON.stringify(p.persisted_value))}</span>` : "–"}</td>
        ${r.managed ? `<td class="num">${p.read_only ? "" : `<button class="small" data-pset="${esc(p.name)}">Apply</button>`}</td>` : ""}</tr>`).join("")}
      </tbody></table>${rows.length > 400 ? `<p class="small muted panel-pad">Showing the first 400: refine the search.</p>` : ""}`;
    $$("[data-pset]").forEach((b) => (b.onclick = () => setParameterDialog(d, r, b.dataset.pset)));
  };
  $("#p-host").onchange = (e) => { state.paramHost[d.id] = e.target.value; pageParameters(d); };
  $("#p-q").oninput = (e) => { f.q = e.target.value; render(); };
  $("#p-rt").onchange = (e) => { f.runtime = e.target.checked; render(); };
  $("#p-ps").onchange = (e) => { f.persisted = e.target.checked; render(); };
  bindHeader(d);
  render();
}
function setParameterDialog(d, r, name) {
  const row = r.parameters.find((p) => p.name === name);
  const el = $(`[data-pv="${CSS.escape(name)}"]`);
  const value = el.value;
  const mi = d.managed_info;
  const me = mi.members.find((m) => m.host === r.host) || {};
  const sameSet = mi.members.filter((m) => m.rs_name && m.rs_name === me.rs_name).map((m) => m.host);
  const sameBin = mi.members.filter((m) => (m.role === "mongos") === (me.role === "mongos")).map((m) => m.host);
  const startupOnly = row.runtime === false;
  openModal({
    title: `Set ${name}`, submit: "Apply",
    body: `<p>New value <span class="code inline">${esc(value)}</span> (current <span class="code inline">${esc(JSON.stringify(row.value))}</span>).</p>
      ${startupOnly ? `<div class="notice">This parameter can only be set at startup: it is saved in the configuration file and takes effect after a restart.</div>` : ""}
      <div class="field"><label>Apply to</label><div class="radio-row">
        <label><input type="radio" name="scope" value="one" checked>${esc(r.host)}</label>
        ${sameSet.length > 1 ? `<label><input type="radio" name="scope" value="set">Replica set ${esc(me.rs_name)} (${sameSet.length})</label>` : ""}
        ${sameBin.length > 1 ? `<label><input type="radio" name="scope" value="all">All ${me.role === "mongos" ? "mongos routers" : "mongod processes"} (${sameBin.length})</label>` : ""}</div></div>
      <label class="check"><input type="checkbox" name="persist" ${startupOnly ? "checked disabled" : "checked"}>Also save it in the configuration files, so it survives restarts</label>
      <label class="check"><input type="checkbox" name="restart" ${startupOnly ? "" : "disabled"}>Restart the processes now (rolling, primary last)</label>`,
    onSubmit: async (fd) => {
      const scope = fd.get("scope");
      const hosts = scope === "set" ? sameSet : scope === "all" ? sameBin : [r.host];
      const res = await api(`/deployments/${d.id}/parameters`, { method: "POST", body: { name, value, hosts, persist: startupOnly || fd.get("persist") === "on", restart: fd.get("restart") === "on" } });
      const failed = res.results.filter((x) => !x.ok);
      if (failed.length) toast(`Not applied on ${failed.map((x) => x.host).join(", ")}: ${failed[0].error}`, true);
      else toast(res.results.length ? `${name} applied on ${res.results.length} process${res.results.length > 1 ? "es" : ""}` : `${name} saved`);
      if (res.job_id) location.hash = `#/jobs/${res.job_id}`; else pageParameters(d);
    },
  });
}

/* ---------------- configuration files ---------------- */
async function pageConfigFiles(d) {
  const r = await api(`/deployments/${d.id}/config`);
  let sel = state.cfgHost?.[d.id];
  if (!r.members.find((m) => m.host === sel)) sel = r.members[0]?.host;
  state.cfgHost = { ...(state.cfgHost || {}), [d.id]: sel };
  const m = r.members.find((x) => x.host === sel);
  const roleName = { replica: "mongod", configsvr: "config server", shardsvr: "shard member", mongos: "mongos" };
  main.innerHTML = `${deploymentHeader(d, "config")}
    <div class="cfg-layout">
      <div class="panel cfg-list">${r.members.map((x) => `<button type="button" data-cfg="${esc(x.host)}" class="${x.host === sel ? "on" : ""}">
        <span><span class="mono small">${esc(x.host)}</span><div class="muted small">${roleName[x.role] || x.role}${x.shard && x.shard !== "config" ? ` · ${esc(x.shard)}` : ""}</div></span>
        ${x.customized ? `<span class="chip" title="Differs from MongoPilot's default">edited</span>` : ""}</button>`).join("")}</div>
      <div class="panel panel-pad">
        <div class="row" style="margin-bottom:8px"><h2 class="grow" style="margin:0"><span class="mono" style="font-weight:500;font-size:.95rem">${esc(m.path)}</span></h2>
          <button class="small" id="cfg-remote">Compare with the file on the server</button></div>
        <textarea class="yaml" id="cfg-yaml" spellcheck="false" aria-label="Configuration file">${esc(m.yaml)}</textarea>
        <p class="small muted">Managed by MongoPilot and not editable here: ${r.protected.map((k) => `<code>${esc(k)}</code>`).join(", ")}. TLS is set in the TLS tab.
          Before anything is written, MongoDB validates the file with <code>${m.binary} --outputConfig</code>.</p>
        <div class="field"><label>Apply the changes to</label><div class="radio-row">
          <label><input type="radio" name="cfg-scope" value="member" checked>Only this process</label>
          <label><input type="radio" name="cfg-scope" value="type">Every ${m.binary === "mongos" ? "mongos router" : "mongod process"} (same changes, own ports and paths kept)</label></div></div>
        <label class="check"><input type="checkbox" id="cfg-restart" checked>Restart the affected processes now (rolling, one at a time, primary last)</label>
        <div class="row" style="margin-top:14px"><span class="grow"></span><button id="cfg-reset">Discard edits</button><button class="primary" id="cfg-save">Validate and apply</button></div>
      </div></div>`;
  bindHeader(d);
  $$("[data-cfg]").forEach((b) => (b.onclick = () => { state.cfgHost[d.id] = b.dataset.cfg; pageConfigFiles(d); }));
  $("#cfg-reset").onclick = () => ($("#cfg-yaml").value = m.yaml);
  $("#cfg-remote").onclick = async () => {
    try {
      const rr = await api(`/deployments/${d.id}/config/remote?host=${encodeURIComponent(sel)}`);
      openModal({ title: rr.matches ? "The file on the server matches" : "The file on the server is different", submit: null, wide: true,
        body: `${rr.matches ? `<p>${esc(m.path)} on the server is identical to MongoPilot's version.</p>` : `<div class="notice">Someone changed the file on the server. Saving here overwrites it with MongoPilot's version.</div>`}
          <div class="code" style="max-height:55vh;overflow:auto">${esc(rr.content)}</div>`, onSubmit: () => false });
    } catch (e) { toast(e.message, true); }
  };
  $("#cfg-save").onclick = async (ev) => {
    ev.target.disabled = true;
    try {
      const scope = $("input[name=cfg-scope]:checked").value;
      const res = await api(`/deployments/${d.id}/config`, { method: "PUT", body: { host: sel, yaml: $("#cfg-yaml").value, scope, restart: $("#cfg-restart").checked } });
      toast(`Applying ${Object.keys(res.changes.set).length + res.changes.deleted.length} change(s) on ${res.hosts.length} process(es)`);
      location.hash = `#/jobs/${res.job_id}`;
    } catch (e) { toast(e.message, true); ev.target.disabled = false; }
  };
}

/* ---------------- TLS ---------------- */
function pemPicker(name, hint) {
  return `<label class="dropzone" data-pem="${name}"><input type="file" accept=".pem,.crt,.cer,.key,text/plain"><span data-pemlabel>${esc(hint)}</span></label><input type="hidden" name="${name}">`;
}
function bindPemPickers(root) {
  $$("[data-pem]", root).forEach((zone) => {
    const input = $("input[type=file]", zone), label = $("[data-pemlabel]", zone), hidden = zone.nextElementSibling;
    const load = async (file) => {
      if (!file) return;
      const text = await file.text();
      const ok = text.includes("-----BEGIN CERTIFICATE-----");
      hidden.value = ok ? text : "";
      zone.className = "dropzone " + (ok ? "okk" : "bad");
      label.textContent = ok ? `${file.name} loaded` : `${file.name}: no PEM certificate found`;
    };
    input.onchange = () => load(input.files[0]);
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
    zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("drag"); load(e.dataTransfer.files[0]); });
  });
}
async function pageTls(d) {
  const t = await api(`/deployments/${d.id}/tls`);
  const MODE_HELP = { disabled: "No TLS: plain connections only.", allowTLS: "Accepts TLS and plain connections; members talk to each other without TLS.",
    preferTLS: "Accepts both; members talk to each other over TLS.", requireTLS: "TLS only: every client must connect with TLS." };
  main.innerHTML = `${deploymentHeader(d, "tls")}
    <div class="grid2" style="gap:16px;align-items:start">
    <div class="panel panel-pad"><h2>Current state</h2><dl class="kv">
      <dt>TLS mode</dt><dd>${t.mode === "disabled" ? pill("off", "disabled") : pill(t.mode === "requireTLS" ? "ok" : "warn", t.mode)}</dd>
      <dt>MongoPilot connects with TLS</dt><dd>${t.client ? "yes" : "no"}</dd>
      <dt>Certificate authority</dt><dd>${t.ca ? `${esc(t.ca.subject)}<div class="muted small">${t.generated ? "generated by MongoPilot · " : ""}valid until ${esc(t.ca.not_after.slice(0, 10))}</div>` : "–"}</dd>
      <dt>Host name check</dt><dd>${t.allow_invalid_hostnames ? "disabled (allowInvalidHostnames)" : "enabled"}</dd></dl>
      ${t.ca_pem ? `<p style="margin-top:12px"><a class="btn small" download="${esc(d.name)}-ca.pem" href="data:application/x-pem-file;charset=utf-8,${encodeURIComponent(t.ca_pem)}">Download the CA certificate</a></p>
        <p class="small muted">Applications need this CA to connect with <code>tls=true&amp;tlsCAFile=...</code>.</p>` : ""}
      ${t.certificates.some((c) => c.subject) ? `<div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>Server</th><th>Names in the certificate</th><th>Expires</th></tr></thead><tbody>
        ${t.certificates.map((c) => `<tr><td>${esc(c.server)}<div class="muted small mono">${esc(c.host)}</div></td><td class="small mono">${esc((c.names || []).join(", ") || c.error || "–")}
          ${c.missing_names?.length ? `<div>${pill("warn", "does not cover " + c.missing_names.join(", "))}</div>` : ""}</td><td class="small">${esc((c.not_after || "").slice(0, 10))}</td></tr>`).join("")}</tbody></table></div>` : ""}
    </div>
    <form class="panel panel-pad" id="tls-form"><h2>Change TLS</h2>
      <div class="field"><label for="tls-mode">Target mode</label><select id="tls-mode" name="mode">${t.modes.map((m) => `<option value="${m}" ${m === (t.mode === "disabled" ? "requireTLS" : t.mode) ? "selected" : ""}>${m}</option>`).join("")}</select>
        <div class="hint" id="tls-help"></div></div>
      <div class="field" id="tls-src"><label>Certificates</label><div class="radio-row" style="flex-direction:column;align-items:flex-start">
        <label><input type="radio" name="source" value="generate" ${t.ca ? "" : "checked"}>Generate a private CA and a certificate for every server</label>
        <label><input type="radio" name="source" value="upload">Upload my CA and server certificates</label>
        ${t.ca ? `<label><input type="radio" name="source" value="keep" checked>Keep the current certificates</label>` : ""}</div></div>
      <div id="tls-upload" hidden>
        <div class="field"><label>CA certificate (PEM)</label>${pemPicker("ca_pem", "Drop the CA certificate")}</div>
        ${t.certificates.map((c) => `<div class="field"><label>${esc(c.server)} <span class="muted mono small">${esc(c.host)}</span></label>${pemPicker(`pem_${c.server_id}`, "Drop the certificate + private key (one PEM file)")}</div>`).join("")}
        <p class="small muted">Each server certificate must include its MongoDB host name in the Subject Alternative Names and be usable for both server and client authentication (members authenticate to each other with it).</p>
      </div>
      <label class="check"><input type="checkbox" name="allow_invalid_hostnames" ${t.allow_invalid_hostnames ? "checked" : ""}>Allow invalid host names (only for labs)</label>
      <div class="notice info small" style="margin-top:14px">MongoPilot follows MongoDB's staged procedure: a rolling restart in <b>allowTLS</b> with the certificates, then <b>preferTLS</b> and <b>requireTLS</b> are set at runtime without further restarts. Before moving to requireTLS, update your applications' connection strings.</div>
      <div class="row" style="margin-top:12px"><span class="grow"></span><button class="primary" type="submit">Apply</button></div>
    </form></div>`;
  bindHeader(d);
  const form = $("#tls-form");
  bindPemPickers(form);
  const sync = () => {
    const mode = $("#tls-mode").value, src = form.querySelector("input[name=source]:checked")?.value;
    $("#tls-help").textContent = MODE_HELP[mode];
    $("#tls-src").hidden = mode === "disabled";
    $("#tls-upload").hidden = mode === "disabled" || src !== "upload";
  };
  form.addEventListener("change", sync);
  sync();
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const body = { mode: fd.get("mode"), source: fd.get("source") || "keep", allow_invalid_hostnames: fd.get("allow_invalid_hostnames") === "on" };
    if (body.source === "upload") {
      body.ca_pem = fd.get("ca_pem");
      body.pems = Object.fromEntries(t.certificates.map((c) => [String(c.server_id), fd.get(`pem_${c.server_id}`)]));
    }
    try { const r = await api(`/deployments/${d.id}/tls`, { method: "POST", body }); toast("TLS change started"); location.hash = `#/jobs/${r.job_id}`; }
    catch (e) { toast(e.message, true); }
  };
}

/* ---------------- tools: mongosh, mongodump, mongoexport, schema analyzer ---------------- */
const TOOLS = [
  ["mongosh", "mongosh"], ["dump", "mongodump"], ["restore", "mongorestore"], ["export", "mongoexport"], ["import", "mongoimport"],
  ["schema", "Schema Analyzer"],
];
function toolSelect(depId, p) {
  const disabled = p.role === "ARBITER" || !p.up;
  return `<select class="tool-select" data-tool-proc="${p.id}" data-dep="${depId}" aria-label="Open a tool on ${esc(p.host)}" ${disabled ? "disabled" : ""}
    title="${p.role === "ARBITER" ? "Arbiters hold no data" : !p.up ? "Not reachable" : "mongosh, mongodump, mongorestore, mongoexport, mongoimport, Schema Analyzer"}">
    <option value="">Open…</option>${TOOLS.map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select>`;
}
function bindToolSelects() {
  $$("[data-tool-proc]").forEach((sel) => (sel.onchange = () => {
    if (sel.value) location.hash = `#/deployments/${sel.dataset.dep}/tools/${sel.value}/${sel.dataset.toolProc}`;
  }));
}

let xtermLoading = null;
function loadXterm() {
  if (window.Terminal && window.FitAddon) return Promise.resolve();
  if (xtermLoading) return xtermLoading;
  const add = (tag, attrs) => new Promise((res, rej) => {
    const el = document.createElement(tag);
    Object.assign(el, attrs);
    el.onload = res; el.onerror = () => rej(new Error(`Cannot load ${attrs.src || attrs.href}`));
    document.head.appendChild(el);
  });
  xtermLoading = Promise.all([add("link", { rel: "stylesheet", href: "/static/vendor/xterm.css" }),
    add("script", { src: "/static/vendor/xterm.js" })]).then(() => add("script", { src: "/static/vendor/addon-fit.js" }));
  return xtermLoading;
}

/* credentials used by the tools of a deployment: kept in memory only, never stored in the browser */
function toolAuth(depId, info) {
  state.toolAuth = state.toolAuth || {};
  if (!state.toolAuth[depId]) state.toolAuth[depId] = { mode: info.default_auth, username: "", password: "", auth_source: "admin" };
  return state.toolAuth[depId];
}
function authPanel(depId, info) {
  const a = toolAuth(depId, info);
  return `<div class="auth-row" id="auth-row">
    <div><label for="ta-mode">Connect as</label><select id="ta-mode">
      ${info.managed ? `<option value="admin" ${a.mode === "admin" ? "selected" : ""}>MongoPilot admin user</option>` : ""}
      <option value="monitor" ${a.mode === "monitor" ? "selected" : ""}>Monitoring user (metadata only)</option>
      <option value="custom" ${a.mode === "custom" ? "selected" : ""}>Another user…</option></select></div>
    <div class="ta-custom" ${a.mode === "custom" ? "" : "hidden"}><label for="ta-user">User</label><input id="ta-user" autocomplete="off" value="${esc(a.username)}"></div>
    <div class="ta-custom" ${a.mode === "custom" ? "" : "hidden"}><label for="ta-pw">Password</label><input id="ta-pw" type="password" autocomplete="new-password" value="${esc(a.password)}"></div>
    <div class="ta-custom" ${a.mode === "custom" ? "" : "hidden"}><label for="ta-src">Auth database</label><input id="ta-src" value="${esc(a.auth_source)}" style="width:110px"></div>
    <div class="grow small muted ta-hint">${a.mode === "monitor" ? "The monitoring user can list databases and collections but cannot read documents." : a.mode === "custom" ? "Kept in this page only, never saved." : ""}</div>
  </div>`;
}
function bindAuthPanel(depId, info, onChange) {
  const a = toolAuth(depId, info);
  const sync = (reload) => {
    a.mode = $("#ta-mode").value;
    a.username = $("#ta-user").value.trim(); a.password = $("#ta-pw").value; a.auth_source = $("#ta-src").value.trim() || "admin";
    $$(".ta-custom").forEach((el) => (el.hidden = a.mode !== "custom"));
    $(".ta-hint").textContent = a.mode === "monitor" ? "The monitoring user can list databases and collections but cannot read documents."
      : a.mode === "custom" ? "Kept in this page only, never saved." : "";
    if (reload) onChange();
  };
  $("#ta-mode").onchange = () => sync(a.mode !== "custom" || $("#ta-user").value);
  ["#ta-user", "#ta-src"].forEach((id) => ($(id).onchange = () => sync(true)));
  $("#ta-pw").onchange = () => sync(true);
}
const authBody = (depId, info) => { const a = toolAuth(depId, info); return a.mode === "custom" ? { ...a } : { mode: a.mode }; };

async function pageTool(depId, tool, procId) {
  depId = Number(depId); procId = Number(procId);
  const [d, info] = await Promise.all([api(`/deployments/${depId}`), api(`/deployments/${depId}/tools`)]);
  const procs = info.processes.filter((p) => p.role !== "ARBITER");
  const p = procs.find((x) => x.id === procId) || procs.find((x) => x.up) || procs[0];
  if (!p) { main.innerHTML = `<div class="notice">This deployment has no data-bearing node.</div>`; return; }
  const ctx = { d, info, p, depId };
  main.innerHTML = `<div class="page-head"><div><div class="small"><a href="#/deployments">Deployments</a> › <a href="#/deployments/${depId}">${esc(d.name)}</a></div>
      <h1>${esc(TOOLS.find((t) => t[0] === tool)[1])}</h1>
      <div class="sub row">${badge(nodeKind({ ...p, info: d.processes.find((x) => x.id === p.id)?.info || {} }))}<span class="mono">${esc(p.host)}</span>
        <span>${esc(ROLE_EN[p.role] || p.role)}${p.shard ? ` · shard ${esc(p.shard)}` : ""}</span><span class="muted">direct connection to this node</span></div></div>
      <div class="row"><label for="tool-node" class="small muted" style="margin:0">Node</label>
        <select id="tool-node" style="width:auto">${procs.map((x) => `<option value="${x.id}" ${x.id === p.id ? "selected" : ""} ${x.up ? "" : "disabled"}>${esc(x.host)} · ${esc((ROLE_EN[x.role] || x.role).toLowerCase())}</option>`).join("")}</select></div></div>
    <nav class="subnav">${TOOLS.map(([k, l]) => `<a href="#/deployments/${depId}/tools/${k}/${p.id}" class="${k === tool ? "on" : ""}">${l}</a>`).join("")}</nav>
    ${info.demo ? `<div class="notice info small">Demo mode: the tools run against simulated data.</div>` : ""}
    <div class="panel panel-pad" style="margin-bottom:14px">${authPanel(depId, info)}</div>
    <div id="tool-body"></div>`;
  $("#tool-node").onchange = (e) => (location.hash = `#/deployments/${depId}/tools/${tool}/${e.target.value}`);
  const render = { mongosh: toolShell, dump: toolDump, restore: toolRestore, export: toolExport, import: toolImport, schema: toolSchema }[tool];
  bindAuthPanel(depId, info, () => render(ctx));
  await render(ctx);
}

/* ---- mongosh ---- */
async function toolShell(ctx) {
  const { depId, info, p } = ctx;
  const body = $("#tool-body");
  if (!info.available.mongosh) {
    body.innerHTML = `<div class="notice crit">mongosh is not installed in the MongoPilot container. Rebuild the image with the provided Dockerfile, or set <code>MP_MONGOSH</code> to its path.</div>`;
    return;
  }
  if (state.cleanup) { state.cleanup(); state.cleanup = null; }
  body.innerHTML = `<div class="term-bar row"><span class="grow small muted" id="term-status">Not connected</span>
      <button class="small" id="term-clear">Clear</button><button class="small" id="term-close" disabled>Disconnect</button><button class="small primary" id="term-open">Connect</button></div>
    <div class="term-wrap"><div id="term"></div></div>
    <p class="small muted">A real <code>mongosh</code> session runs inside the MongoPilot container with a direct connection to ${esc(p.host)}. Tab completion, history (↑ ↓) and Ctrl+C work as in a terminal. Idle sessions close after 30 minutes.</p>`;
  try { await loadXterm(); } catch (e) { body.insertAdjacentHTML("afterbegin", `<div class="notice crit">${esc(e.message)}</div>`); return; }
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const term = new Terminal({ fontFamily: "'IBM Plex Mono', ui-monospace, monospace", fontSize: 13, cursorBlink: true, convertEol: false, scrollback: 5000,
    theme: dark ? { background: "#0d1117", foreground: "#d6dde6" } : { background: "#101820", foreground: "#e6edf3", cursor: "#7fd4c1" } });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("#term"));
  fit.fit();
  let sid = null, alive = false, cursor = 0, queue = Promise.resolve(), pending = "", timer = null;
  const status = (t) => ($("#term-status").textContent = t);
  const send = (data) => {
    pending += data;
    clearTimeout(timer);
    timer = setTimeout(() => {
      const chunk = pending; pending = "";
      const id = sid;
      if (!id || !chunk) return;
      queue = queue.then(() => api(`/shell/${id}/input`, { method: "POST", body: { data: chunk } })).catch(() => {});
    }, 8);
  };
  term.onData((data) => { if (alive) send(data); });
  const pump = async (id) => {
    while (alive && sid === id) {
      try {
        const r = await api(`/shell/${id}/output?cursor=${cursor}`);
        if (sid !== id) return;
        if (r.data) term.write(r.data);
        cursor = r.cursor;
        if (r.closed) { alive = false; status("Session ended"); toggle(false); return; }
      } catch (e) {
        if (sid !== id) return;
        alive = false; term.write(`\r\n\x1b[31m${e.message}\x1b[0m\r\n`); status("Disconnected"); toggle(false); return;
      }
    }
  };
  const toggle = (on) => { $("#term-open").disabled = on; $("#term-close").disabled = !on; };
  const close = () => {
    const id = sid;
    alive = false; sid = null;
    if (id) fetch(`/api/shell/${id}`, { method: "DELETE", keepalive: true }).catch(() => {});
  };
  const open = async () => {
    close();
    term.reset(); cursor = 0;
    status(`Connecting to ${p.host}…`);
    try {
      const r = await api(`/deployments/${depId}/shell`, { method: "POST", body: { host: p.host, auth: authBody(depId, info), rows: term.rows, cols: term.cols } });
      sid = r.id; alive = true; toggle(true);
      status(`Connected to ${p.host}`);
      term.focus();
      pump(r.id);
    } catch (e) { status("Not connected"); term.write(`\x1b[31m${e.message}\x1b[0m\r\n`); toggle(false); }
  };
  const onResize = () => {
    fit.fit();
    if (sid) api(`/shell/${sid}/resize`, { method: "POST", body: { rows: term.rows, cols: term.cols } }).catch(() => {});
  };
  window.addEventListener("resize", onResize);
  $("#term-open").onclick = open;
  $("#term-close").onclick = () => { close(); status("Disconnected"); toggle(false); term.write("\r\n\x1b[90m[disconnected]\x1b[0m\r\n"); };
  $("#term-clear").onclick = () => term.clear();
  state.cleanup = () => { close(); window.removeEventListener("resize", onResize); term.dispose(); };
  open();
}

/* ---- namespaces (database / collection pickers) ---- */
async function loadNamespaces(ctx) {
  const key = JSON.stringify([ctx.depId, ctx.p.host, authBody(ctx.depId, ctx.info)]);
  state.nsCache = state.nsCache || {};
  if (!state.nsCache[key]) {
    state.nsCache[key] = api(`/deployments/${ctx.depId}/namespaces`, { method: "POST", body: { host: ctx.p.host, auth: authBody(ctx.depId, ctx.info) } })
      .then((r) => r.databases).catch((e) => { delete state.nsCache[key]; throw e; });
  }
  return state.nsCache[key];
}
function nsFields(prefix, required, allLabel) {
  return `<div class="grid2"><div class="field"><label for="${prefix}-db">Database${required ? "" : " (optional)"}</label>
      <input id="${prefix}-db" name="db" list="${prefix}-dbs" class="mono" ${required ? "required" : ""} placeholder="${allLabel ? "all databases" : ""}" autocomplete="off"><datalist id="${prefix}-dbs"></datalist></div>
    <div class="field"><label for="${prefix}-coll">Collection${required ? "" : " (optional)"}</label>
      <input id="${prefix}-coll" name="collection" list="${prefix}-colls" class="mono" ${required ? "required" : ""} placeholder="${allLabel ? "all collections" : ""}" autocomplete="off"><datalist id="${prefix}-colls"></datalist></div></div>
    <div class="small muted" id="${prefix}-nsmsg" style="margin:-6px 0 10px"></div>`;
}
async function bindNsFields(ctx, prefix, preset = {}) {
  const dbIn = $(`#${prefix}-db`), collIn = $(`#${prefix}-coll`), msg = $(`#${prefix}-nsmsg`);
  if (preset.db) dbIn.value = preset.db;
  if (preset.collection) collIn.value = preset.collection;
  let dbs = [];
  const fillColls = () => {
    const db = dbs.find((x) => x.db === dbIn.value.trim());
    $(`#${prefix}-colls`).innerHTML = (db?.collections || []).map((c) => `<option value="${esc(c.name)}">${c.type !== "collection" ? esc(c.type) : ""}</option>`).join("");
  };
  dbIn.addEventListener("input", fillColls);
  try {
    msg.textContent = "Loading databases…";
    dbs = await loadNamespaces(ctx);
    $(`#${prefix}-dbs`).innerHTML = dbs.map((x) => `<option value="${esc(x.db)}">${x.collections.length} collections</option>`).join("");
    fillColls();
    msg.textContent = `${dbs.length} databases on ${ctx.p.host}`;
  } catch (e) { msg.innerHTML = `<span class="bad-text">Cannot list databases: ${esc(e.message)}</span> You can still type the names.`; }
  return () => dbs;
}

/* ---- dumps and exports list ---- */
const EXPORT_STATUS = { running: ["info", "Running"], success: ["ok", "Ready"], failed: ["crit", "Failed"], cancelled: ["off", "Cancelled"] };
const RUN_TITLES = { dump: ["Available dumps", "dumps"], export: ["Available exports", "exports"], restore: ["Restore history", "restores"],
  import: ["Import history", "imports"], upload: ["Uploaded files", "uploaded files"] };
async function renderExports(ctx, kind, box, onChange) {
  const r = await api(`/deployments/${ctx.depId}/exports?kind=${kind}`);
  const rows = r.exports;
  const writeKind = kind === "restore" || kind === "import";
  const what = (e) => {
    const pr = e.params || {};
    if (kind === "upload") return `<span class="mono">${esc(pr.original_name || e.file_name)}</span>`;
    if (writeKind) {
      const target = kind === "import" ? `${pr.db}.${pr.collection}` : [pr.ns_include, pr.ns_from && `${pr.ns_from} → ${pr.ns_to}`].filter(Boolean).join(" · ") || "whole archive";
      const extra = kind === "restore" ? [pr.drop ? "drop" : "", pr.dry_run ? "dry run" : "", pr.oplog_replay ? "oplog replay" : "", pr.no_index_restore ? "no indexes" : ""]
        : [(pr.format || "").toUpperCase(), pr.mode && pr.mode !== "insert" ? pr.mode : "", pr.drop ? "drop" : ""];
      return `<span class="mono">${esc(target)}</span><div class="muted small mono" title="source file">${esc(pr.source_file || "")}</div>
        ${extra.filter(Boolean).length ? `<div class="muted small">${esc(extra.filter(Boolean).join(" · "))}</div>` : ""}`;
    }
    const ns = pr.db ? pr.db + (pr.collection ? "." + pr.collection : "") : "all databases";
    const extra = kind === "dump" ? [pr.oplog ? "oplog" : "", pr.users_and_roles ? "users and roles" : "", pr.query ? "query" : ""]
      : [(pr.format || "json").toUpperCase(), pr.query ? "query" : "", pr.limit ? `limit ${pr.limit}` : "", pr.gzip ? "gzip" : ""];
    return `<span class="mono">${esc(ns)}</span>${extra.filter(Boolean).length ? `<div class="muted small">${esc(extra.filter(Boolean).join(" · "))}</div>` : ""}`;
  };
  const [title, noun] = RUN_TITLES[kind];
  box.innerHTML = `<div class="row" style="margin-bottom:10px"><h2 class="grow" style="margin:0">${title}</h2>
      <span class="small muted" title="${esc(r.folder)}">${fmtBytes(r.storage_bytes)} used by MongoPilot tool files</span></div>
    <div class="panel table-wrap">${rows.length ? `<table><thead><tr><th>Date</th><th>Description</th>${kind === "upload" ? "" : "<th>Node</th>"}<th>${writeKind ? "Target · source" : kind === "upload" ? "File" : "Content"}</th>${writeKind ? "" : `<th class="num">Size</th>`}<th>Status</th><th></th></tr></thead><tbody>
    ${rows.map((e) => `<tr><td class="small" style="white-space:nowrap">${esc(dateTime(e.created_at))}${e.finished_at && kind !== "upload" ? `<div class="muted">${esc(fmtDur(e.finished_at - e.created_at))}</div>` : ""}</td>
      <td>${esc(e.description || "–")}<div class="muted small">by ${esc(e.created_by || "?")}</div></td>
      ${kind === "upload" ? "" : `<td class="mono small">${esc(e.host)}</td>`}<td class="small">${what(e)}</td>
      ${writeKind ? "" : `<td class="num small">${e.size !== null && e.size !== undefined ? fmtBytes(e.size) : "–"}</td>`}
      <td>${pill(...(writeKind && e.status === "success" ? ["ok", "Completed"] : EXPORT_STATUS[e.status] || ["off", e.status]))}${e.error ? `<div class="small bad-text" style="max-width:260px">${esc(e.error)}</div>` : ""}</td>
      <td class="num" style="white-space:nowrap">${e.status === "success" && e.file_name ? `<a class="btn small primary" href="/api/exports/${e.id}/download" download="${esc(e.file_name)}">Download</a>` : ""}
        ${kind === "upload" ? "" : `<button class="small" data-exp-log="${e.id}">Details</button>`}
        <button class="small" data-exp-del="${e.id}">${e.status === "running" ? "Cancel" : "Delete"}</button></td></tr>`).join("")}
    </tbody></table>` : `<div class="empty small">No ${noun} yet for this deployment.</div>`}</div>`;
  $$("[data-exp-log]", box).forEach((b) => (b.onclick = () => {
    const e = rows.find((x) => x.id === Number(b.dataset.expLog));
    openModal({ title: e.file_name || "Details", submit: null, wide: true,
      body: `<dl class="kv"><dt>Status</dt><dd>${pill(...EXPORT_STATUS[e.status] || ["off", e.status])}</dd><dt>Node</dt><dd class="mono">${esc(e.host)}</dd>
        <dt>Started</dt><dd>${esc(dateTime(e.created_at))}</dd><dt>File</dt><dd class="mono small">${esc(e.file_name || "–")}</dd></dl>
        <h3>Command</h3><div class="code small">${esc(e.command)}</div>
        ${kind === "dump" ? `<h3>Restore</h3><div class="code small">mongorestore --uri="mongodb://USER@HOST:PORT/?authSource=admin" --archive=${esc(e.file_name)} --gzip${e.params?.oplog ? " --oplogReplay" : ""}</div>` : ""}
        <h3>Output</h3><div class="code small" style="max-height:40vh;overflow:auto;white-space:pre-wrap">${esc(e.log || "(no output)")}</div>`, onSubmit: () => false });
  }));
  $$("[data-exp-del]", box).forEach((b) => (b.onclick = () => {
    const e = rows.find((x) => x.id === Number(b.dataset.expDel));
    confirmAction(e.status === "running" ? "Cancel this run?" : "Delete this file?", `${esc(e.file_name || e.description || "This run")} will be ${e.status === "running" ? "stopped" : e.file_name ? "deleted from the MongoPilot data volume" : "removed from the history"}.`,
      e.status === "running" ? "Cancel run" : "Delete", async () => { await api(`/exports/${e.id}`, { method: "DELETE" }); renderExports(ctx, kind, box, onChange); if (onChange) onChange(); }, true);
  }));
  clearTimeout(box._timer);
  if (rows.some((e) => e.status === "running")) {
    const hash = location.hash;
    box._timer = setTimeout(() => { if (location.hash === hash && document.body.contains(box)) renderExports(ctx, kind, box, onChange).catch(() => {}); }, 1500);
  }
}

/* ---- mongodump ---- */
async function toolDump(ctx) {
  const { depId, info, p } = ctx;
  const body = $("#tool-body");
  const rsMember = p.role === "PRIMARY" || p.role === "SECONDARY";
  body.innerHTML = `${info.available.mongodump ? "" : `<div class="notice crit">mongodump is not installed in the MongoPilot container (MongoDB Database Tools).</div>`}
    <form class="panel panel-pad" id="dump-form"><h2>New dump</h2>
      <div class="field"><label for="dp-desc">Description</label><input id="dp-desc" name="description" placeholder="e.g. before the 9.0 upgrade" maxlength="500"></div>
      ${nsFields("dp", false, true)}
      <div class="grid2">
        <div class="field"><label for="dp-q">Query (only with one collection)</label><textarea id="dp-q" name="query" class="mono" rows="2" placeholder='{"status": "paid"}'></textarea>
          <div class="hint">Extended JSON v2, e.g. <code>{"createdAt": {"$gte": {"$date": "2026-01-01T00:00:00Z"}}}</code></div></div>
        <div class="field"><label for="dp-ex">Exclude collections (with a database)</label><input id="dp-ex" name="exclude_collections" class="mono" placeholder="sessions, logs"></div>
      </div>
      <div class="opt-grid">
        <label class="check"><input type="checkbox" name="oplog" ${rsMember ? "" : "disabled"}>Include the oplog (<code>--oplog</code>) for a consistent point-in-time dump${rsMember ? "" : " · replica set members only"}</label>
        <label class="check"><input type="checkbox" name="users_and_roles">Users and roles of the database (<code>--dumpDbUsersAndRoles</code>)</label>
        <label class="check"><input type="checkbox" name="force_table_scan">Force a table scan (<code>--forceTableScan</code>)</label>
        <div class="field" style="margin:0"><label for="dp-par">Collections dumped in parallel</label><input id="dp-par" name="parallel" type="number" min="1" max="16" value="4" style="width:90px"></div>
      </div>
      <p class="small muted">The dump is a single gzipped archive (<code>--archive --gzip</code>) stored in the MongoPilot data volume. ${p.role === "PRIMARY" ? "This node is the primary: dumping a secondary avoids adding load to it." : ""}</p>
      <div class="row"><span class="grow"></span><button class="primary" type="submit" ${info.available.mongodump ? "" : "disabled"}>Start mongodump</button></div>
    </form>
    <div class="section" id="dump-list"></div>`;
  const form = $("#dump-form");
  await bindNsFields(ctx, "dp");
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const params = { db: fd.get("db").trim(), collection: fd.get("collection").trim(), query: fd.get("query").trim(),
      exclude_collections: fd.get("exclude_collections").trim(), oplog: fd.get("oplog") === "on", users_and_roles: fd.get("users_and_roles") === "on",
      force_table_scan: fd.get("force_table_scan") === "on", parallel: fd.get("parallel") };
    try {
      await api(`/deployments/${depId}/exports`, { method: "POST", body: { kind: "dump", host: p.host, auth: authBody(depId, info), description: fd.get("description"), params } });
      toast("mongodump started");
      renderExports(ctx, "dump", $("#dump-list"));
    } catch (e) { toast(e.message, true); }
  };
  await renderExports(ctx, "dump", $("#dump-list"));
}

/* ---- mongoexport ---- */
async function toolExport(ctx) {
  const { depId, info, p } = ctx;
  const body = $("#tool-body");
  body.innerHTML = `${info.available.mongoexport ? "" : `<div class="notice crit">mongoexport is not installed in the MongoPilot container (MongoDB Database Tools).</div>`}
    <form class="panel panel-pad" id="exp-form"><h2>New export</h2>
      <div class="field"><label for="ex-desc">Description</label><input id="ex-desc" name="description" placeholder="e.g. paid orders for accounting" maxlength="500"></div>
      ${nsFields("ex", true, false)}
      <div class="field"><label>Format</label><div class="radio-row"><label><input type="radio" name="format" value="json" checked>JSON</label><label><input type="radio" name="format" value="csv">CSV</label></div></div>
      <div class="field"><label for="ex-fields">Fields <span class="muted" id="ex-fields-req">(optional for JSON)</span></label><input id="ex-fields" name="fields" class="mono" placeholder="_id, total, status, shipping.city"></div>
      <div class="grid2">
        <div class="field"><label for="ex-q">Query</label><textarea id="ex-q" name="query" class="mono" rows="2" placeholder='{"status": "paid"}'></textarea></div>
        <div class="field"><label for="ex-s">Sort</label><textarea id="ex-s" name="sort" class="mono" rows="2" placeholder='{"createdAt": -1}'></textarea></div>
      </div>
      <div class="grid3"><div class="field"><label for="ex-l">Limit</label><input id="ex-l" name="limit" type="number" min="0" placeholder="all"></div>
        <div class="field"><label for="ex-k">Skip</label><input id="ex-k" name="skip" type="number" min="0" placeholder="0"></div>
        <div class="field" data-json><label for="ex-jf">JSON format</label><select id="ex-jf" name="json_format"><option value="relaxed">Relaxed Extended JSON</option><option value="canonical">Canonical Extended JSON</option></select></div></div>
      <div class="opt-grid">
        <label class="check" data-json><input type="checkbox" name="json_array">One JSON array (<code>--jsonArray</code>)</label>
        <label class="check" data-json><input type="checkbox" name="pretty">Pretty printed (<code>--pretty</code>)</label>
        <label class="check" data-csv hidden><input type="checkbox" name="no_header">No header line (<code>--noHeaderLine</code>)</label>
        <label class="check"><input type="checkbox" name="gzip">Compress the file (gzip)</label>
      </div>
      <div class="row"><span class="grow"></span><button class="primary" type="submit" ${info.available.mongoexport ? "" : "disabled"}>Start mongoexport</button></div>
    </form>
    <div class="section" id="exp-list"></div>`;
  const form = $("#exp-form");
  const syncFmt = () => {
    const csv = form.querySelector("input[name=format]:checked").value === "csv";
    $$("[data-json]", form).forEach((el) => (el.hidden = csv));
    $$("[data-csv]", form).forEach((el) => (el.hidden = !csv));
    $("#ex-fields").required = csv;
    $("#ex-fields-req").textContent = csv ? "(required for CSV)" : "(optional for JSON)";
  };
  form.addEventListener("change", syncFmt);
  syncFmt();
  await bindNsFields(ctx, "ex", state.exportPreset || {});
  state.exportPreset = null;
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const params = { db: fd.get("db").trim(), collection: fd.get("collection").trim(), format: fd.get("format"), fields: fd.get("fields"),
      query: fd.get("query").trim(), sort: fd.get("sort").trim(), limit: fd.get("limit"), skip: fd.get("skip"),
      json_format: fd.get("json_format") || "relaxed", json_array: fd.get("json_array") === "on", pretty: fd.get("pretty") === "on",
      no_header: fd.get("no_header") === "on", gzip: fd.get("gzip") === "on" };
    try {
      await api(`/deployments/${depId}/exports`, { method: "POST", body: { kind: "export", host: p.host, auth: authBody(depId, info), description: fd.get("description"), params } });
      toast("mongoexport started");
      renderExports(ctx, "export", $("#exp-list"));
    } catch (e) { toast(e.message, true); }
  };
  await renderExports(ctx, "export", $("#exp-list"));
}

/* ---- mongorestore / mongoimport: shared pieces ---- */
const WRITE_ROLES = ["PRIMARY", "MONGOS", "STANDALONE"];
function writeTargetNotice(ctx, tool) {
  const { p, info, depId } = ctx;
  if (WRITE_ROLES.includes(p.role)) return "";
  const better = info.processes.find((x) => x.up && (x.role === "MONGOS" || (x.role === "PRIMARY" && x.rs_name === p.rs_name)));
  return `<div class="notice">${tool} writes data, so it must connect to the primary${p.shard ? " or, better, to a mongos router" : ""}: ${esc(p.host)} is a ${esc((ROLE_EN[p.role] || p.role).toLowerCase())}.
    ${better ? `<a href="#/deployments/${depId}/tools/${tool === "mongorestore" ? "restore" : "import"}/${better.id}">Switch to ${esc(better.host)}</a>` : ""}</div>`;
}
function sourcePicker(prefix, accept, hint) {
  return `<div class="field"><label for="${prefix}-src">File</label>
      <div class="row" style="gap:8px;flex-wrap:nowrap"><select id="${prefix}-src" name="source_id" required class="grow"><option value="">Loading…</option></select>
        <label class="btn small" style="margin:0;white-space:nowrap">Upload a file<input type="file" id="${prefix}-up" accept="${accept}" hidden></label></div>
      <div class="hint">${hint}</div>
      <div class="upload-progress" id="${prefix}-prog" hidden><i></i><span></span></div></div>`;
}
async function fillSources(prefix, kind, ctx, selectId) {
  const sel = $(`#${prefix}-src`);
  const r = await api(`/tool-sources?kind=${kind}`);
  const label = (f) => `${f.kind === "upload" ? "Uploaded" : f.kind === "dump" ? "Dump" : "Export"} · ${f.description || f.params?.original_name || f.file_name} · ${dateTime(f.created_at)} · ${fmtBytes(f.size || 0)}`;
  const mine = r.files.filter((f) => f.deployment_id === ctx.depId), other = r.files.filter((f) => f.deployment_id !== ctx.depId);
  sel.innerHTML = r.files.length ? `<option value="">Choose a file…</option>
    ${mine.length ? `<optgroup label="${esc(ctx.d.name)}">${mine.map((f) => `<option value="${f.id}" data-format="${esc(f.format || "")}" data-name="${esc(f.file_name)}">${esc(label(f))}</option>`).join("")}</optgroup>` : ""}
    ${other.length ? `<optgroup label="Other deployments">${other.map((f) => `<option value="${f.id}" data-format="${esc(f.format || "")}" data-name="${esc(f.file_name)}">${esc(f.deployment)} · ${esc(label(f))}</option>`).join("")}</optgroup>` : ""}`
    : `<option value="">No file available: upload one${kind === "restore" ? " or run mongodump first" : " or run mongoexport first"}</option>`;
  if (selectId) sel.value = String(selectId);
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  return r.files;
}
function bindUpload(prefix, kind, ctx, onDone) {
  const input = $(`#${prefix}-up`), prog = $(`#${prefix}-prog`);
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    if (file.size > ctx.info.max_upload_mb * 1048576) { toast(`The file is larger than ${ctx.info.max_upload_mb} MB`, true); return; }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/deployments/${ctx.depId}/uploads?name=${encodeURIComponent(file.name)}&description=${encodeURIComponent("Uploaded " + file.name)}`);
    prog.hidden = false;
    xhr.upload.onprogress = (e) => {
      const pct = e.lengthComputable ? Math.round((100 * e.loaded) / e.total) : 0;
      prog.querySelector("i").style.width = pct + "%";
      prog.querySelector("span").textContent = `Uploading ${file.name}: ${pct}% of ${fmtBytes(file.size)}`;
    };
    xhr.onload = async () => {
      prog.hidden = true; input.value = "";
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 300) { toast(data.error || `Upload failed (HTTP ${xhr.status})`, true); return; }
      toast(`${file.name} uploaded`);
      await fillSources(prefix, kind, ctx, data.id);
      if (onDone) onDone();
    };
    xhr.onerror = () => { prog.hidden = true; toast("Upload failed: network error", true); };
    xhr.send(file);
  };
}
function confirmWrite(ctx, tool, summary, dangerous, run) {
  openModal({
    title: `Run ${tool} on ${ctx.p.host}?`, submit: `Run ${tool}`, danger: dangerous,
    body: `<p>${summary}</p>
      <dl class="kv"><dt>Deployment</dt><dd>${esc(ctx.d.name)} ${tagChips({ cluster_type: ctx.d.tags?.cluster_type })}</dd><dt>Node</dt><dd class="mono">${esc(ctx.p.host)}</dd></dl>
      ${dangerous ? `<div class="notice crit" style="margin-top:12px"><b>--drop</b> deletes the target collections before writing. Type the deployment name to confirm.</div>
        <div class="field"><label for="cw-name">Deployment name</label><input id="cw-name" name="confirm_name" autocomplete="off" required></div>` : ""}`,
    onSubmit: async (fd) => {
      if (dangerous && fd.get("confirm_name") !== ctx.d.name) { toast("The name does not match", true); return false; }
      await run();
    },
  });
}

/* ---- mongorestore ---- */
async function toolRestore(ctx) {
  const { depId, info, p } = ctx;
  const body = $("#tool-body");
  const canWrite = WRITE_ROLES.includes(p.role);
  body.innerHTML = `${info.available.mongorestore ? "" : `<div class="notice crit">mongorestore is not installed in the MongoPilot container (MongoDB Database Tools).</div>`}
    ${writeTargetNotice(ctx, "mongorestore")}
    <form class="panel panel-pad" id="rs2-form"><h2>New restore</h2>
      ${sourcePicker("rr", ".archive,.gz,.agz", "A mongodump archive: dumps made from any deployment in MongoPilot, or a file written with <code>mongodump --archive</code> (optionally <code>--gzip</code>).")}
      <div class="field"><label for="rr-desc">Description</label><input id="rr-desc" name="description" placeholder="e.g. restore crm into staging" maxlength="500"></div>
      <div class="grid2">
        <div class="field"><label for="rr-inc">Include namespaces</label><input id="rr-inc" name="ns_include" class="mono" placeholder="crm.*, shop.orders"><div class="hint"><code>--nsInclude</code>, comma separated, <code>*</code> as wildcard. Empty: everything in the archive.</div></div>
        <div class="field"><label for="rr-exc">Exclude namespaces</label><input id="rr-exc" name="ns_exclude" class="mono" placeholder="crm.sessions"><div class="hint"><code>--nsExclude</code></div></div>
      </div>
      <div class="grid2">
        <div class="field"><label for="rr-from">Rename from</label><input id="rr-from" name="ns_from" class="mono" placeholder="crm.*"><div class="hint"><code>--nsFrom</code>, e.g. restore <code>crm.*</code> as <code>crm_copy.*</code></div></div>
        <div class="field"><label for="rr-to">Rename to</label><input id="rr-to" name="ns_to" class="mono" placeholder="crm_copy.*"><div class="hint"><code>--nsTo</code>, one target for each source</div></div>
      </div>
      <div class="opt-grid">
        <label class="check"><input type="checkbox" name="drop">Drop each collection before restoring it (<code>--drop</code>)</label>
        <label class="check"><input type="checkbox" name="dry_run">Dry run: check the archive without writing (<code>--dryRun</code>)</label>
        <label class="check"><input type="checkbox" name="oplog_replay">Replay the oplog captured by <code>--oplog</code> (<code>--oplogReplay</code>)</label>
        <label class="check"><input type="checkbox" name="no_index_restore">Do not restore indexes (<code>--noIndexRestore</code>)</label>
        <label class="check"><input type="checkbox" name="maintain_insertion_order">Keep the insertion order (<code>--maintainInsertionOrder</code>)</label>
        <label class="check"><input type="checkbox" name="stop_on_error">Stop at the first error (<code>--stopOnError</code>)</label>
        <label class="check"><input type="checkbox" name="bypass_validation">Skip schema validation (<code>--bypassDocumentValidation</code>)</label>
        <label class="check"><input type="checkbox" name="preserve_uuid">Keep the collection UUIDs (<code>--preserveUUID</code>, needs drop)</label>
        <label class="check"><input type="checkbox" name="users_and_roles">Restore users and roles (<code>--restoreDbUsersAndRoles</code>)</label>
      </div>
      <div class="grid3"><div class="field"><label for="rr-par">Collections in parallel</label><input id="rr-par" name="parallel" type="number" min="1" max="16" value="4"></div>
        <div class="field"><label for="rr-w">Insertion workers per collection</label><input id="rr-w" name="workers" type="number" min="1" max="16" value="1"></div></div>
      <div class="row"><span class="grow"></span><button class="primary" type="submit" ${info.available.mongorestore && canWrite ? "" : "disabled"}>Start mongorestore</button></div>
    </form>
    <div class="section" id="rr-list"></div><div class="section" id="rr-uploads"></div>`;
  const form = $("#rs2-form");
  const refreshUploads = () => renderExports(ctx, "upload", $("#rr-uploads"), () => fillSources("rr", "restore", ctx));
  bindUpload("rr", "restore", ctx, refreshUploads);
  await fillSources("rr", "restore", ctx);
  form.onsubmit = (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const params = { source_id: fd.get("source_id") };
    ["ns_include", "ns_exclude", "ns_from", "ns_to", "parallel", "workers"].forEach((k) => (params[k] = (fd.get(k) || "").trim()));
    ["drop", "dry_run", "oplog_replay", "no_index_restore", "maintain_insertion_order", "stop_on_error", "bypass_validation", "preserve_uuid", "users_and_roles"]
      .forEach((k) => (params[k] = fd.get(k) === "on"));
    const src = $("#rr-src").selectedOptions[0];
    const what = params.ns_include || "everything in the archive";
    confirmWrite(ctx, "mongorestore", `Restore <b>${esc(what)}</b> from <span class="mono">${esc(src?.dataset.name || "")}</span>${params.ns_from ? ` renaming <span class="mono">${esc(params.ns_from)}</span> to <span class="mono">${esc(params.ns_to)}</span>` : ""}${params.dry_run ? " (dry run: nothing is written)" : ""}.`,
      params.drop && !params.dry_run, async () => {
        await api(`/deployments/${depId}/exports`, { method: "POST", body: { kind: "restore", host: p.host, auth: authBody(depId, info), description: fd.get("description"), params } });
        toast("mongorestore started");
        renderExports(ctx, "restore", $("#rr-list"));
      });
  };
  await Promise.all([renderExports(ctx, "restore", $("#rr-list")), refreshUploads()]);
}

/* ---- mongoimport ---- */
async function toolImport(ctx) {
  const { depId, info, p } = ctx;
  const body = $("#tool-body");
  const canWrite = WRITE_ROLES.includes(p.role);
  body.innerHTML = `${info.available.mongoimport ? "" : `<div class="notice crit">mongoimport is not installed in the MongoPilot container (MongoDB Database Tools).</div>`}
    ${writeTargetNotice(ctx, "mongoimport")}
    <form class="panel panel-pad" id="im-form"><h2>New import</h2>
      ${sourcePicker("im", ".json,.csv,.tsv,.gz", "A JSON (one document per line, or an array), CSV or TSV file, optionally gzipped: exports made in MongoPilot or a file you upload.")}
      <div class="field"><label for="im-desc">Description</label><input id="im-desc" name="description" placeholder="e.g. load the new price list" maxlength="500"></div>
      ${nsFields("im", true, false)}
      <div class="field"><label>Format</label><div class="radio-row"><label><input type="radio" name="format" value="json" checked>JSON</label><label><input type="radio" name="format" value="csv">CSV</label><label><input type="radio" name="format" value="tsv">TSV</label></div></div>
      <div class="opt-grid" data-json><label class="check"><input type="checkbox" name="json_array">The file is one JSON array (<code>--jsonArray</code>)</label></div>
      <div data-csv hidden>
        <div class="opt-grid"><label class="check"><input type="checkbox" name="headerline" checked>The first line holds the field names (<code>--headerline</code>)</label>
          <label class="check"><input type="checkbox" name="columns_have_types">Field names declare types, e.g. <code>price.double()</code> (<code>--columnsHaveTypes</code>)</label>
          <label class="check"><input type="checkbox" name="ignore_blanks">Skip empty values (<code>--ignoreBlanks</code>)</label></div>
        <div class="field" id="im-fields-box" hidden><label for="im-fields">Fields</label><input id="im-fields" name="fields" class="mono" placeholder="sku, name, price"></div>
      </div>
      <div class="grid2"><div class="field"><label for="im-mode">Mode</label><select id="im-mode" name="mode">
          <option value="insert">insert: add the documents</option><option value="upsert">upsert: replace documents that match</option>
          <option value="merge">merge: update the fields of documents that match</option><option value="delete">delete: remove documents that match</option></select></div>
        <div class="field"><label for="im-up">Match on fields</label><input id="im-up" name="upsert_fields" class="mono" placeholder="_id (default)" disabled><div class="hint"><code>--upsertFields</code> for upsert, merge and delete</div></div></div>
      <div class="opt-grid">
        <label class="check"><input type="checkbox" name="drop">Drop the collection before importing (<code>--drop</code>)</label>
        <label class="check"><input type="checkbox" name="stop_on_error">Stop at the first error (<code>--stopOnError</code>)</label>
        <label class="check"><input type="checkbox" name="maintain_insertion_order">Keep the insertion order (<code>--maintainInsertionOrder</code>)</label>
        <label class="check"><input type="checkbox" name="bypass_validation">Skip schema validation (<code>--bypassDocumentValidation</code>)</label>
        <div class="field" style="margin:0"><label for="im-w">Insertion workers</label><input id="im-w" name="workers" type="number" min="1" max="16" value="1" style="width:90px"></div>
      </div>
      <div class="row"><span class="grow"></span><button class="primary" type="submit" ${info.available.mongoimport && canWrite ? "" : "disabled"}>Start mongoimport</button></div>
    </form>
    <div class="section" id="im-list"></div><div class="section" id="im-uploads"></div>`;
  const form = $("#im-form");
  const sync = () => {
    const fmt = form.querySelector("input[name=format]:checked").value;
    $$("[data-json]", form).forEach((el) => (el.hidden = fmt !== "json"));
    $$("[data-csv]", form).forEach((el) => (el.hidden = fmt === "json"));
    const header = form.querySelector("input[name=headerline]").checked;
    $("#im-fields-box").hidden = header;
    $("#im-fields").required = fmt !== "json" && !header;
    const mode = $("#im-mode").value;
    $("#im-up").disabled = mode === "insert";
    form.querySelector("input[name=drop]").disabled = mode !== "insert";
  };
  form.addEventListener("change", sync);
  $("#im-src").addEventListener("change", () => {
    const fmt = $("#im-src").selectedOptions[0]?.dataset.format;
    if (fmt) { form.querySelector(`input[name=format][value=${fmt}]`).checked = true; sync(); }
  });
  sync();
  const refreshUploads = () => renderExports(ctx, "upload", $("#im-uploads"), () => fillSources("im", "import", ctx));
  bindUpload("im", "import", ctx, refreshUploads);
  await Promise.all([fillSources("im", "import", ctx), bindNsFields(ctx, "im")]);
  form.onsubmit = (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const params = { source_id: fd.get("source_id"), db: fd.get("db").trim(), collection: fd.get("collection").trim(), format: fd.get("format"),
      fields: fd.get("fields") || "", mode: fd.get("mode"), upsert_fields: fd.get("upsert_fields") || "", workers: fd.get("workers") };
    ["json_array", "headerline", "columns_have_types", "ignore_blanks", "drop", "stop_on_error", "maintain_insertion_order", "bypass_validation"]
      .forEach((k) => (params[k] = fd.get(k) === "on"));
    if (params.format === "json") { params.headerline = false; params.columns_have_types = false; params.ignore_blanks = false; }
    const src = $("#im-src").selectedOptions[0];
    confirmWrite(ctx, "mongoimport", `Import <span class="mono">${esc(src?.dataset.name || "")}</span> into <b class="mono">${esc(params.db)}.${esc(params.collection)}</b> (${esc(params.format.toUpperCase())}, ${esc(params.mode)} mode).`,
      params.drop, async () => {
        await api(`/deployments/${depId}/exports`, { method: "POST", body: { kind: "import", host: p.host, auth: authBody(depId, info), description: fd.get("description"), params } });
        toast("mongoimport started");
        renderExports(ctx, "import", $("#im-list"));
      });
  };
  await Promise.all([renderExports(ctx, "import", $("#im-list")), refreshUploads()]);
}

/* ---- schema analyzer ---- */
const TYPE_COLORS = { String: "#2B7A5B", Int32: "#2B4C7E", Int64: "#3A64A8", Double: "#5B83C4", Decimal128: "#7B9BD1", Date: "#B5651D",
  ObjectId: "#8A4FA8", Boolean: "#C2983A", Array: "#0F7B6C", Document: "#4B5D6E", Null: "#9AA5B1", Binary: "#A0522D", Undefined: "#D5DBE1" };
const typeColor = (t) => TYPE_COLORS[t] || "#7A8A99";
const pctTxt = (x) => (x >= 0.995 ? "100%" : x < 0.01 && x > 0 ? "<1%" : Math.round(x * 100) + "%");

function typeBar(field) {
  const parts = field.types.map((t) => [t.name, t.count]);
  if (field.missing) parts.push(["Undefined", field.missing]);
  const total = parts.reduce((a, [, c]) => a + c, 0) || 1;
  return `<div class="type-bar" role="img" aria-label="type distribution">${parts.map(([n, c]) =>
    `<span style="width:${(100 * c) / total}%;background:${typeColor(n)}" title="${esc(n)}: ${c} (${pctTxt(c / total)})"></span>`).join("")}</div>`;
}
function hBars(rows, total, path, clickable) {
  const max = Math.max(...rows.map((r) => r.count), 1);
  return `<div class="hbars">${rows.map((r) => {
    const label = r.value === null ? "null" : typeof r.value === "string" ? r.value : JSON.stringify(r.value);
    return `<div class="hbar ${clickable ? "clickable" : ""}" ${clickable ? `data-filter-path="${esc(path)}" data-filter-value="${esc(JSON.stringify(r.value))}" title="Add to the filter"` : ""}>
      <span class="hb-label mono">${esc(label === "" ? '""' : label)}</span><span class="hb-track"><i style="width:${(100 * r.count) / max}%"></i></span><span class="hb-n">${r.count} · ${pctTxt(r.count / total)}</span></div>`;
  }).join("")}</div>`;
}
function histogram(h, kind) {
  const max = Math.max(...h.bins, 1);
  const fmt = (v) => (h.time ? new Date(v * 1000).toLocaleDateString(LOCALE, { year: "numeric", month: "short", day: "numeric" }) : fmtNum(v, Math.abs(h.hi - h.lo) < 10 ? 2 : 0));
  return `<div class="histo" title="${esc(kind)} distribution">${h.bins.map((b, i) => `<span style="height:${Math.max(2, (100 * b) / max)}%" title="${b}"></span>`).join("")}</div>
    <div class="histo-axis small muted"><span>${esc(fmt(h.lo))}</span><span>${esc(fmt(h.hi))}</span></div>`;
}
function miniBars(values, labels) {
  const max = Math.max(...values, 1);
  return `<div class="mini-bars">${values.map((v, i) => `<span title="${esc(labels[i])}: ${v}"><i style="height:${Math.max(2, (100 * v) / max)}%"></i><b>${esc(labels[i])}</b></span>`).join("")}</div>`;
}
function typeDetail(t, path) {
  const n = t.count;
  const stats = (pairs) => `<dl class="kv tiny">${pairs.filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => `<dt>${esc(k)}</dt><dd class="mono">${esc(v)}</dd>`).join("")}</dl>`;
  const distinct = t.distinct !== undefined ? `${t.distinct}${t.distinct_capped ? "+" : ""} distinct${t.unique ? " · all values unique" : ""}` : "";
  switch (t.name) {
    case "String":
      return `${hBars(t.top_values, n, path, true)}<div class="small muted">${distinct} · length ${t.min_length}–${t.max_length} (avg ${t.avg_length})</div>`;
    case "Int32": case "Int64": case "Double": case "Decimal128":
      return `${t.histogram ? histogram(t.histogram, t.name) : ""}${stats([["min", fmtNum(t.min, 2)], ["max", fmtNum(t.max, 2)], ["mean", fmtNum(t.mean, 2)], ["median", fmtNum(t.median, 2)], ["distinct", distinct]])}
        ${t.top_values.length && t.distinct <= 12 ? hBars(t.top_values, n, path, true) : ""}`;
    case "Date":
      return `${histogram(t.histogram, "date")}<div class="grid2" style="gap:12px;margin-top:8px"><div><div class="small muted">Day of week</div>${miniBars(t.weekdays, ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"])}</div>
        <div><div class="small muted">Hour (UTC)</div>${miniBars(t.hours, t.hours.map((_, i) => String(i)))}</div></div>
        <div class="small muted">${esc(t.min)} → ${esc(t.max)}</div>`;
    case "ObjectId":
      return `${t.histogram ? histogram(t.histogram, "creation time") : ""}<div class="small muted">Created ${esc(t.min || "?")} → ${esc(t.max || "?")} · ${distinct}</div>`;
    case "Boolean":
      return hBars([{ value: true, count: t.true }, { value: false, count: t.false }], n, path, true);
    case "Null":
      return `<div class="small muted">null in ${n} sampled documents</div>`;
    case "Array": {
      const el = t.element;
      return `<div class="small muted">Length ${t.min_length}–${t.max_length} (avg ${t.avg_length})${el ? ` · ${el.total} elements` : ""}</div>
        ${el ? `<div class="small" style="margin:6px 0">Element types ${el.types.map((et) => `<span class="type-chip" style="--c:${typeColor(et.name)}">${esc(et.name)} ${pctTxt(et.count / el.total)}</span>`).join(" ")}</div>
          ${el.types[0] && !["Document", "Array"].includes(el.types[0].name) ? typeDetail(el.types[0], path) : ""}` : ""}`;
    }
    case "Document":
      return `<div class="small muted">${t.fields.length} nested field${t.fields.length === 1 ? "" : "s"}: expand the field to see them.</div>`;
    default:
      return `${t.top_values?.length ? hBars(t.top_values, n, path, false) : ""}<div class="small muted">${distinct}</div>`;
  }
}
function nestedFieldsOf(field) {
  const out = [];
  field.types.forEach((t) => {
    if (t.name === "Document") out.push(...t.fields);
    if (t.name === "Array" && t.element) t.element.types.forEach((et) => { if (et.name === "Document") out.push(...et.fields); });
  });
  return out;
}
function fieldRows(fields, depth = 0) {
  return fields.map((f) => {
    const nested = nestedFieldsOf(f);
    const sel = state.schemaType?.[f.path] || f.types[0]?.name;
    const t = f.types.find((x) => x.name === sel) || f.types[0];
    const open = state.schemaOpen?.has(f.path);
    return `<div class="sf" style="--depth:${depth}">
      <div class="sf-left">
        <div class="sf-name">${nested.length ? `<button type="button" class="link sf-toggle" data-sf-toggle="${esc(f.path)}" aria-expanded="${open}">${open ? "▾" : "▸"}</button>` : `<span class="sf-toggle"></span>`}
          <span class="mono">${esc(f.name)}</span>${depth ? `<span class="muted small mono"> ${esc(f.path)}</span>` : ""}</div>
        <div class="small muted">${pctTxt(f.probability)} of documents${f.missing ? ` · missing in ${f.missing}` : ""}</div>
        ${typeBar(f)}
        <div class="sf-types">${f.types.map((x) => `<button type="button" class="type-chip ${x.name === t?.name ? "on" : ""}" style="--c:${typeColor(x.name)}" data-sf-type="${esc(f.path)}" data-type="${esc(x.name)}">${esc(x.name)} ${pctTxt(x.probability)}</button>`).join("")}
          ${f.missing ? `<span class="type-chip ghost" style="--c:${typeColor("Undefined")}">undefined ${pctTxt(f.missing / (f.count + f.missing))}</span>` : ""}</div>
      </div>
      <div class="sf-right">${t ? typeDetail(t, f.path) : ""}</div>
    </div>${nested.length && open ? fieldRows(nested, depth + 1) : ""}`;
  }).join("");
}
async function toolSchema(ctx) {
  const { depId, info, p } = ctx;
  const body = $("#tool-body");
  const last = state.schemaForm?.[depId] || {};
  body.innerHTML = `<form class="panel panel-pad" id="sc-form">
      ${nsFields("sc", true, false)}
      <div class="grid-sc"><div class="field"><label for="sc-f">Filter</label><input id="sc-f" name="filter" class="mono" placeholder='{"status": "paid"}' value="${esc(last.filter || "")}"></div>
        <div class="field"><label for="sc-n">Sample size</label><input id="sc-n" name="size" type="number" min="1" max="${info.max_sample}" value="${esc(last.size || 1000)}"></div>
        <div class="field" style="align-self:end"><button class="primary" type="submit">Analyze</button></div></div>
      <p class="small muted" style="margin:0">Documents are sampled with <code>$sample</code> after the filter, directly on ${esc(p.host)}, and analysed in MongoPilot. Click a value to add it to the filter.</p>
    </form>
    <div id="sc-result" class="section"></div>`;
  await bindNsFields(ctx, "sc", last);
  const form = $("#sc-form");
  const show = (r) => {
    state.schemaResult = r;
    const res = $("#sc-result");
    res.innerHTML = `<div class="row schema-head"><div class="grow"><h2 style="margin:0" class="mono">${esc(r.namespace)}</h2>
        <div class="small muted">${r.sampled} documents sampled${r.total !== null && r.total !== undefined ? ` of about ${fmtNum(r.total, 0)}` : ""} · ${r.field_count} fields · ${r.took_ms} ms · ${esc(r.host)}</div></div>
        <button class="small" id="sc-expand">Expand all</button><button class="small" id="sc-json">Download analysis (JSON)</button>
        <button class="small" id="sc-export">Export with mongoexport</button></div>
      ${r.sampled ? `<div class="panel schema-list">${fieldRows(r.fields)}</div>` : `<div class="panel empty">No document matches the filter.</div>`}`;
    $$("[data-sf-toggle]", res).forEach((b) => (b.onclick = () => {
      state.schemaOpen = state.schemaOpen || new Set();
      const k = b.dataset.sfToggle;
      state.schemaOpen.has(k) ? state.schemaOpen.delete(k) : state.schemaOpen.add(k);
      show(r);
    }));
    $$("[data-sf-type]", res).forEach((b) => (b.onclick = () => {
      state.schemaType = state.schemaType || {};
      state.schemaType[b.dataset.sfType] = b.dataset.type;
      show(r);
    }));
    $$("[data-filter-path]", res).forEach((el) => (el.onclick = () => {
      let f = {};
      try { f = JSON.parse($("#sc-f").value || "{}"); } catch { f = {}; }
      f[el.dataset.filterPath] = JSON.parse(el.dataset.filterValue);
      $("#sc-f").value = JSON.stringify(f);
      $("#sc-f").focus();
      toast("Added to the filter: press Analyze");
    }));
    $("#sc-expand").onclick = () => {
      state.schemaOpen = new Set();
      const walk = (fs) => fs.forEach((f) => { const n = nestedFieldsOf(f); if (n.length) { state.schemaOpen.add(f.path); walk(n); } });
      walk(r.fields);
      show(r);
    };
    $("#sc-json").onclick = () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }));
      a.download = `${r.namespace}-schema.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    };
    $("#sc-export").onclick = () => {
      const [db, ...rest] = r.namespace.split(".");
      state.exportPreset = { db, collection: rest.join(".") };
      location.hash = `#/deployments/${depId}/tools/export/${p.id}`;
    };
  };
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const req = { host: p.host, auth: authBody(depId, info), db: fd.get("db").trim(), collection: fd.get("collection").trim(), filter: fd.get("filter").trim(), size: Number(fd.get("size")) };
    state.schemaForm = { ...(state.schemaForm || {}), [depId]: { db: req.db, collection: req.collection, filter: req.filter, size: req.size } };
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true; btn.textContent = "Analyzing…";
    $("#sc-result").innerHTML = `<div class="panel empty small">Sampling ${req.size} documents of ${esc(req.db)}.${esc(req.collection)}…</div>`;
    try {
      state.schemaOpen = new Set(); state.schemaType = {};
      show(await api(`/deployments/${depId}/schema`, { method: "POST", body: req }));
    } catch (e) { $("#sc-result").innerHTML = `<div class="notice crit">${esc(e.message)}</div>`; }
    finally { btn.disabled = false; btn.textContent = "Analyze"; }
  };
  if (state.schemaResult && state.schemaResult.host === p.host && last.db && state.schemaResult.namespace === `${last.db}.${last.collection}`) show(state.schemaResult);
}

/* ---------------- query performance ---------------- */
async function pageQueries(depId) {
  const deps = await api("/deployments");
  if (!deps.length) {
    main.innerHTML = `<div class="page-head"><h1>Query performance</h1></div><div class="panel empty"><h3>No deployments yet</h3><a href="#/">Add one from the overview</a></div>`;
    return;
  }
  depId = Number(depId) || state.qDep || deps[0].id;
  if (!deps.find((d) => d.id === depId)) depId = deps[0].id;
  state.qDep = depId;
  if (location.hash !== `#/queries/${depId}`) { history.replaceState(null, "", `#/queries/${depId}`); state.route = location.hash; }
  main.innerHTML = `<div class="page-head"><div><h1>Query performance</h1>
      <div class="sub">Slow queries captured from the MongoDB log, grouped by query shape, and index recommendations</div></div>
      <div class="row"><select id="q-dep" aria-label="Deployment" style="width:auto">${deps.map((d) => `<option value="${d.id}" ${d.id === depId ? "selected" : ""}>${esc(d.name)}</option>`).join("")}</select>
      ${rangeSeg(state.qRange, "data-qrange")}</div></div>
    <div class="tabs" role="tablist"><button type="button" data-tab="slow" class="${state.qTab === "slow" ? "on" : ""}">Slow queries</button>
      <button type="button" data-tab="advisor" class="${state.qTab === "advisor" ? "on" : ""}">Index advisor</button></div>
    <div id="q-body"><div class="empty">Loading…</div></div>`;
  $("#q-dep").onchange = (e) => { state.qNs = ""; location.hash = `#/queries/${e.target.value}`; };
  $$("[data-qrange]").forEach((b) => (b.onclick = () => { state.qRange = Number(b.dataset.qrange); pageQueries(depId); }));
  $$("[data-tab]").forEach((b) => (b.onclick = () => { state.qTab = b.dataset.tab; pageQueries(depId); }));
  if (state.qTab === "slow") await renderSlow(depId); else await renderAdvisor(depId);
}

function shapeText(shape) {
  const f = JSON.stringify(shape.filter || {});
  const s = shape.sort && Object.keys(shape.sort).length ? ` sort ${JSON.stringify(shape.sort)}` : "";
  return f + s;
}
function planPill(plans) {
  if (!plans) return "–";
  const first = plans.split(", ")[0];
  const cls = first.includes("COLLSCAN") ? "crit" : first.includes("IXSCAN") || first.includes("COUNT_SCAN") || first.includes("IDHACK") ? "ok" : "info";
  return `<span class="pill ${cls}" title="${esc(plans)}">${esc(first.length > 26 ? first.slice(0, 25) + "…" : first)}</span>`;
}

async function renderSlow(depId) {
  const qs = `range=${state.qRange}${state.qNs ? `&ns=${encodeURIComponent(state.qNs)}` : ""}`;
  const r = await api(`/insights/${depId}/slow?${qs}`);
  const t = r.totals;
  const box = $("#q-body");
  if (!r.groups.length && !state.qNs) {
    box.innerHTML = `${r.log_errors.length ? `<div class="notice crit"><b>Slow-query log not readable</b> on ${r.log_errors.length} member(s): ${esc(r.log_errors[0])}<br><span class="small">The monitoring user needs the clusterMonitor role (it includes getLog).</span></div>` : ""}
      <div class="panel empty"><h3>No slow queries in this period</h3>
      <p class="small" style="margin:0 auto;max-width:60ch">MongoPilot reads operations slower than MongoDB's <code>slowms</code> threshold (100 ms by default) from each data-bearing member's log, every polling cycle. To capture more, lower the threshold on the members, for example <code>db.setProfilingLevel(0, { slowms: 50 })</code>.</p></div>`;
    return;
  }
  box.innerHTML = `
    ${r.log_errors.length ? `<div class="notice"><b>Some members' logs could not be read:</b> ${esc(r.log_errors.join("; "))}</div>` : ""}
    <div class="panel health-line">
      <div class="item"><b>${fmtNum(t.operations, 0)}</b><span>slow operations</span></div>
      <div class="item"><b>${t.shapes}</b><span>query shapes</span></div>
      <div class="item ${t.collscans ? "bad" : ""}"><b>${fmtNum(t.collscans, 0)}</b><span>collection scans</span></div>
      <div class="item ${t.needing_index ? "bad" : ""}"><b>${t.needing_index}</b><span>shapes that need a better index</span></div>
    </div>
    <div class="row section" style="margin-top:18px;margin-bottom:10px"><h2 class="grow" style="margin:0">Query shapes by total time</h2>
      <select id="q-ns" aria-label="Collection" style="width:auto"><option value="">All collections</option>${t.namespaces.map((n) => `<option ${n === state.qNs ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
    ${t.checked_against_indexes ? "" : `<p class="small muted" style="margin-top:-4px">Index suggestions are not yet checked against existing indexes: run the <a href="#" id="go-adv">Index advisor</a> once.</p>`}
    <div class="panel table-wrap"><table><thead><tr><th>Collection</th><th>Query shape</th><th class="num">Count</th><th class="num">Avg</th><th class="num">P95</th>
      <th class="num">Examined ÷ returned</th><th>Diagnosis</th></tr></thead><tbody>
    ${r.groups.map((g) => {
      const open = state.openShapes.has(g.shape_key);
      const tcls = g.avg_targeting >= 1000 ? "bad" : g.avg_targeting >= 10 ? "warn" : "";
      const newIdx = g.suggestions.some((s) => !s.existing);
      return `<tr class="clickable ${open ? "open" : ""}" data-shape="${g.shape_key}">
        <td><span class="mono small">${esc(g.ns)}</span><div class="muted small">${esc(g.op)}</div></td>
        <td><div class="shape" title="${esc(shapeText(g.shape))}">${esc(shapeText(g.shape))}</div></td>
        <td class="num">${fmtNum(g.count, 0)}</td><td class="num">${fmtMs(g.avg_ms)}</td><td class="num">${fmtMs(g.p95_ms)}</td>
        <td class="num"><span class="targeting ${tcls}">${g.avg_docs_examined ? fmtNum(g.avg_targeting) : "–"}</span><div class="muted small">${fmtNum(g.avg_docs_examined, 0)} ÷ ${fmtNum(g.avg_returned, 0)}</div></td>
        <td><div class="diag">${planPill(g.plans)}${g.sort_ratio >= 0.5 ? `<span class="pill warn">in-memory sort</span>` : ""}
          ${g.reasons.length ? pill(newIdx ? "crit" : "warn", newIdx ? "index suggested" : "review") : pill("ok", "efficient")}</div></td></tr>
      ${open ? `<tr class="expand"><td colspan="7">${shapeDetail(g)}</td></tr>` : ""}`;
    }).join("")}
    </tbody></table></div>
    <p class="small muted">Literal values are never stored: shapes keep field names and operators only. Examined ÷ returned is the query targeting ratio; values close to 1 are ideal.</p>`;
  $("#q-ns").onchange = (e) => { state.qNs = e.target.value; renderSlow(depId); };
  const go = $("#go-adv"); if (go) go.onclick = (e) => { e.preventDefault(); state.qTab = "advisor"; pageQueries(depId); };
  $$("tr[data-shape]").forEach((tr) => (tr.onclick = (ev) => {
    if (ev.target.closest("button, a, select")) return;
    const k = tr.dataset.shape;
    state.openShapes.has(k) ? state.openShapes.delete(k) : state.openShapes.add(k);
    renderSlow(depId);
  }));
  bindCopy(box);
}
function shapeDetail(g) {
  const sugg = g.suggestions.map((s) => s.existing
    ? `<div class="notice info">An existing index already matches the suggested key ${esc(s.key_js)}: <b>${esc(s.existing)}</b>. If the plan still shows a scan, check the plan cache or the index selectivity.</div>`
    : `<div><div class="small" style="margin-bottom:4px">Create this index (Equality → Sort → Range order):</div>${cmdBlock(s.command)}</div>`).join("");
  return `<div class="stack">
    <div class="grid2" style="gap:16px"><div><h3>Query shape</h3><div class="code">${esc(JSON.stringify(g.shape, null, 2))}</div></div>
      <div><h3>Why it is slow</h3>${g.reasons.length ? `<div class="reasons">${g.reasons.map((x) => pill("warn", x)).join("")}</div>` : `<p class="small">The query uses an index efficiently (it examines about as many documents as it returns). The slowness likely comes from load, document size or lock contention rather than indexing.</p>`}
      ${g.note ? `<p class="small muted" style="margin-top:8px">${esc(g.note)}</p>` : ""}
      ${sugg ? `<div style="margin-top:10px" class="stack">${sugg}</div>` : ""}
      <dl class="kv small" style="margin-top:12px"><dt>Max duration</dt><dd>${fmtMs(g.max_ms)}</dd><dt>Plans</dt><dd class="mono">${esc(g.plans)}</dd><dt>Avg keys examined</dt><dd>${fmtNum(g.avg_keys_examined, 0)}</dd>
      <dt>Applications</dt><dd>${esc(g.apps.join(", ") || "–")}</dd><dt>Members</dt><dd class="mono">${esc(g.hosts.join(", "))}</dd><dt>Last seen</dt><dd>${esc(ago(g.last_seen))}</dd></dl></div></div>
    <div><h3>Slowest executions</h3><div class="table-wrap"><table><thead><tr><th>When</th><th>Member</th><th class="num">Duration</th><th class="num">Keys examined</th><th class="num">Docs examined</th><th class="num">Returned</th><th>Plan</th></tr></thead><tbody>
    ${g.samples.map((s) => `<tr><td class="small">${esc(dateTime(s.ts))}</td><td class="mono small">${esc(s.host)}</td><td class="num">${fmtMs(s.duration_ms)}</td>
      <td class="num">${fmtNum(s.keys_examined, 0)}</td><td class="num">${fmtNum(s.docs_examined, 0)}</td><td class="num">${fmtNum(s.nreturned, 0)}</td><td class="mono small">${esc(s.plan)}${s.has_sort ? " + SORT" : ""}</td></tr>`).join("")}
    </tbody></table></div></div></div>`;
}

async function renderAdvisor(depId) {
  const { report } = await api(`/insights/${depId}/report`);
  const box = $("#q-body");
  const running = report?.status === "running";
  const head = `<div class="panel panel-pad row" style="margin-bottom:18px">
      <div class="grow"><b>Index advisor</b><div class="small muted">Combines slow queries from the selected period with each collection's indexes, sizes and <code>$indexStats</code> usage counters.
      ${report ? ` Last analysis: ${esc(dateTime(report.created_at))}, over ${esc(fmtDur(report.range_s || 0))} of slow queries.` : ""}</div></div>
      <button class="primary" id="btn-analyze" ${running ? "disabled" : ""}>${running ? "Analyzing…" : report ? "Run a new analysis" : "Run analysis"}</button></div>`;
  if (!report || running || report.status === "failed" || !report.data) {
    box.innerHTML = head + (report?.status === "failed" ? `<div class="notice crit"><b>The analysis failed.</b> ${esc(report.error)}</div>`
      : running ? `<div class="panel empty">Reading collections, indexes and usage statistics…</div>`
      : `<div class="panel empty"><h3>No analysis yet</h3><p class="small" style="margin:0 auto;max-width:60ch">The analysis is read-only: it lists collections and indexes, reads <code>$indexStats</code> and collection statistics, and never reads documents.</p></div>`);
    bindAnalyze(depId);
    if (running) setTimeout(() => { if (location.hash === `#/queries/${depId}` && state.qTab === "advisor") renderAdvisor(depId); }, 1500);
    return;
  }
  const d = report.data, s = d.summary;
  box.innerHTML = head + `
    ${d.warnings.length ? `<div class="notice"><b>Partial results.</b> ${esc(d.warnings.slice(0, 3).join("; "))}${d.warnings.length > 3 ? ` (+${d.warnings.length - 3} more)` : ""}</div>` : ""}
    <div class="panel health-line">
      <div class="item"><b>${s.collections}</b><span>collections</span></div>
      <div class="item"><b>${s.indexes}</b><span>indexes</span></div>
      <div class="item ${s.suggestions ? "bad" : ""}"><b>${s.suggestions}</b><span>indexes to create</span></div>
      <div class="item"><b>${s.unused}</b><span>unused indexes</span></div>
      <div class="item"><b>${s.redundant}</b><span>redundant indexes</span></div>
    </div>

    <div class="section"><h2>Suggested indexes</h2>
    ${d.suggestions.length ? `<div class="panel table-wrap"><table><thead><tr><th>Impact</th><th>Collection</th><th>Index key</th><th>Why</th><th class="num">Executions</th><th class="num">Avg time</th><th class="num">Share of slow time</th></tr></thead><tbody>
      ${d.suggestions.map((x) => `<tr><td>${pill(x.impact === "High" ? "crit" : x.impact === "Medium" ? "warn" : "info", x.impact)}</td>
        <td class="mono small">${esc(x.ns)}</td><td><span class="code inline">${esc(x.key_js)}</span></td>
        <td class="small">${esc(x.reasons.join(", "))}${x.note ? `<div class="muted">${esc(x.note)}</div>` : ""}</td>
        <td class="num">${fmtNum(x.count, 0)}</td><td class="num">${fmtMs(x.avg_ms)}</td><td class="num">${fmtNum(x.time_share)}%</td></tr>
        <tr><td></td><td colspan="6" style="padding-top:0">${cmdBlock(x.command)}</td></tr>`).join("")}
      </tbody></table></div>
      <p class="small muted">Keys follow the Equality → Sort → Range rule. Every index adds write overhead and storage: verify each suggestion, and on large collections build it during a quiet period.</p>`
      : `<div class="panel empty">No index to create: the slow queries in this period either use an efficient index or cannot be helped by one.</div>`}</div>

    <div class="section"><h2>Unused indexes</h2>
    ${d.unused.length ? `<div class="panel table-wrap"><table><thead><tr><th>Collection</th><th>Index</th><th class="num">Size</th><th>Observed for</th><th>Recommendation</th></tr></thead><tbody>
      ${d.unused.map((u) => `<tr><td class="mono small">${esc(u.ns)}</td><td><b class="mono small">${esc(u.name)}</b><div><span class="code inline">${esc(u.key_js)}</span></div></td>
        <td class="num">${fmtBytes(u.size)}</td>
        <td class="small">${u.window_days !== null ? `${fmtNum(u.window_days)} days` : "–"}${u.low_confidence ? `<div>${pill("warn", "short window")}</div>` : ""}</td>
        <td class="small">${u.keep_reason ? `${pill("ok", "keep")} ${esc(u.keep_reason)}` : `No operation used it since the counters started. Hide it first, watch for regressions, then drop it.<div class="stack" style="margin-top:6px">${cmdBlock(u.hide)}${cmdBlock(u.drop)}</div>`}</td></tr>`).join("")}
      </tbody></table></div>
      <p class="small muted">Usage counters (<code>$indexStats</code>) are summed across all data-bearing members and reset when a member restarts; the observation window is the shortest one. Indexes used only by rare jobs (monthly reports) can look unused.</p>`
      : `<div class="panel empty">Every index has been used since the counters started.</div>`}</div>

    <div class="section"><h2>Redundant indexes</h2>
    ${d.redundant.length ? `<div class="panel table-wrap"><table><thead><tr><th>Collection</th><th>Index</th><th>Covered by</th><th class="num">Size</th><th>Commands</th></tr></thead><tbody>
      ${d.redundant.map((x) => `<tr><td class="mono small">${esc(x.ns)}</td><td><b class="mono small">${esc(x.name)}</b><div><span class="code inline">${esc(x.key_js)}</span></div></td>
        <td><b class="mono small">${esc(x.covered_by)}</b><div><span class="code inline">${esc(x.covered_by_key)}</span></div></td><td class="num">${fmtBytes(x.size)}</td>
        <td><div class="stack">${cmdBlock(x.hide)}${cmdBlock(x.drop)}</div></td></tr>`).join("")}
      </tbody></table></div>
      <p class="small muted">An index whose key is a prefix of another index can be served by the longer one. Unique, partial, sparse, TTL and collation indexes are never reported as redundant.</p>`
      : `<div class="panel empty">No index is a prefix of another one.</div>`}</div>

    <div class="section"><h2>Collections</h2><div class="panel table-wrap"><table><thead><tr><th>Collection</th><th class="num">Documents</th><th class="num">Data size</th><th class="num">Indexes</th><th class="num">Index size</th></tr></thead><tbody>
      ${d.collections.map((c) => { const open = state.openColls.has(c.ns); return `<tr class="clickable ${open ? "open" : ""}" data-coll="${esc(c.ns)}"><td class="mono small">${esc(c.ns)}</td><td class="num">${fmtNum(c.count, 0)}</td><td class="num">${fmtBytes(c.size)}</td>
        <td class="num">${c.indexes.length}</td><td class="num">${fmtBytes(c.index_size)}</td></tr>
        ${open ? `<tr class="expand"><td colspan="5"><table><thead><tr><th>Index</th><th>Key</th><th class="num">Accesses</th><th class="num">Size</th><th>Properties</th></tr></thead><tbody>
          ${c.indexes.map((ix) => `<tr><td class="mono small">${esc(ix.name)}</td><td><span class="code inline">${esc(ix.key_js)}</span></td><td class="num">${ix.ops === null ? "–" : fmtNum(ix.ops, 0)}</td>
            <td class="num">${fmtBytes(ix.size)}</td><td>${ix.flags.map((f) => `<span class="tag">${esc(f)}</span>`).join(" ")}</td></tr>`).join("")}</tbody></table></td></tr>` : ""}`; }).join("")}
    </tbody></table></div></div>`;
  bindAnalyze(depId);
  bindCopy(box);
  $$("tr[data-coll]").forEach((tr) => (tr.onclick = () => {
    const k = tr.dataset.coll;
    state.openColls.has(k) ? state.openColls.delete(k) : state.openColls.add(k);
    renderAdvisor(depId);
  }));
}
function bindAnalyze(depId) {
  const b = $("#btn-analyze");
  if (b) b.onclick = async () => {
    b.disabled = true;
    try { await api(`/insights/${depId}/analyze`, { method: "POST", body: { range: Math.max(state.qRange, 86400) } }); renderAdvisor(depId); }
    catch (e) { toast(e.message, true); b.disabled = false; }
  };
}

/* ---------------- create replica set / sharded cluster ---------------- */
function noServers(title) {
  main.innerHTML = `<div class="page-head"><h1>${title}</h1></div><div class="panel empty"><h3>Add your servers first</h3>
    <p class="small" style="margin:0 auto 14px">MongoPilot installs MongoDB over SSH on the VMs you register on the Servers page, including Vagrant VMs.</p><a class="btn primary" href="#/servers">Go to servers</a></div>`;
}
function defaultVersion(versions) {
  for (const s of ["9.0", "8.0"]) { const v = versions.find((x) => x.startsWith(s + ".")); if (v) return v; }
  return "";
}
function commonFields(versions, pw, prefix) {
  return `<div class="field"><label for="${prefix}-ver">MongoDB version</label><input id="${prefix}-ver" name="version" class="mono" list="${prefix}-vers" required value="${esc(defaultVersion(versions))}" placeholder="e.g. 9.0.1">
      <datalist id="${prefix}-vers">${versions.map((v) => `<option value="${v}">`).join("")}</datalist>
      <div class="hint">Community Edition, series ${state.catalog.versions_supported[0]} to ${state.catalog.versions_supported.at(-1)}. MongoDB 9.0 does not support Ubuntu 20.04. ${versions.length ? "" : "The release list is not reachable: type the version."}</div></div>
    <div class="grid2"><div class="field"><label for="${prefix}-au">Admin user</label><input id="${prefix}-au" name="admin_user" required value="admin"></div>
    <div class="field"><label for="${prefix}-ap">Admin password</label><input id="${prefix}-ap" name="admin_password" required minlength="12" class="mono" value="${esc(pw)}">
      <div class="hint">Save it now: MongoPilot stores it encrypted and uses it for automation.</div></div></div>
    <details><summary class="small" style="cursor:pointer;margin-bottom:12px">Advanced options</summary>
      <div class="grid2"><div class="field"><label for="${prefix}-dr">Data directory</label><input id="${prefix}-dr" name="data_root" class="mono" value="/var/lib/mongopilot"></div>
      <div class="field"><label for="${prefix}-bi">Bind IP</label><input id="${prefix}-bi" name="bind_ip" class="mono" value="0.0.0.0"><div class="hint">Restrict access with a firewall.</div></div></div>
      <div class="grid2"><div class="field"><label for="${prefix}-cg">WiredTiger cache (GB)</label><input id="${prefix}-cg" name="cache_gb" type="number" step="0.25" min="0.25" placeholder="automatic"></div>
      <div class="field"><label for="${prefix}-tu">Tarball URL</label><input id="${prefix}-tu" name="tarball_url" class="mono" placeholder="internal mirror, optional"></div></div>
    </details>`;
}
const MEMBER_HEAD = `<div class="member-row small muted"><span>Server</span><span>Port</span><span>Priority</span><span>Votes</span><span>Hidden</span><span>Arbiter</span><span title="secondaryDelaySecs">Delay (s)</span><span></span></div>`;
const SIMPLE_HEAD = `<div class="member-row small muted"><span>Server</span><span>Port</span><span></span></div>`;
/* first port >= base not already used by a deployment on that server (steps of 100 keep the families apart) */
function freePort(servers, sid, base) {
  const s = servers.find((x) => x.id === Number(sid));
  const used = new Set((s?.processes || []).map((p) => Number(p.port)));
  let port = base;
  while (used.has(port)) port += 100;
  return port;
}
function memberRow(opts, port, sid, simple, noArbiter) {
  const div = document.createElement("div");
  div.className = "member-row";
  div.innerHTML = simple
    ? `<select name="m_server" aria-label="Server">${opts}</select><input name="m_port" type="number" value="${port}" aria-label="Port"><button type="button" class="link small">Remove</button>`
    : `<select name="m_server" aria-label="Server">${opts}</select><input name="m_port" type="number" value="${port}" aria-label="Port">
      <input name="m_priority" type="number" value="1" step="0.5" min="0" aria-label="Priority"><input name="m_votes" type="number" value="1" min="0" max="1" aria-label="Votes">
      <label class="check"><input type="checkbox" name="m_hidden" aria-label="Hidden"></label>
      <label class="check"><input type="checkbox" name="m_arbiter" aria-label="Arbiter" ${noArbiter ? "disabled" : ""}></label>
      <input name="m_delay" type="number" value="0" min="0" step="60" aria-label="Delay in seconds">
      <button type="button" class="link small">Remove</button>`;
  if (sid) div.querySelector("select").value = sid;
  return div;
}
function readRows(container, simple) {
  return $$(".member-row", container).filter((r) => r.querySelector("[name=m_server]")).map((r) => {
    const m = { server_id: Number(r.querySelector("[name=m_server]").value), port: Number(r.querySelector("[name=m_port]").value) };
    if (simple) return m;
    const delay = Number(r.querySelector("[name=m_delay]").value) || 0;
    return { ...m, priority: delay ? 0 : Number(r.querySelector("[name=m_priority]").value), votes: Number(r.querySelector("[name=m_votes]").value),
      hidden: delay > 0 || r.querySelector("[name=m_hidden]").checked, arbiter: r.querySelector("[name=m_arbiter]").checked, delay };
  });
}
function setWarnings(ms, label, servers) {
  const warn = [];
  const voting = ms.filter((m) => m.votes > 0).length;
  if (ms.length && voting % 2 === 0) warn.push(`${label}: ${voting} voting members; with an even number an election can stall.`);
  if (ms.length && ms.length < 3) warn.push(`${label}: with fewer than 3 members there is no automatic failover.`);
  if (ms[0]?.arbiter) warn.push(`${label}: the first member must be a data-bearing node.`);
  if (ms.some((m) => m.delay && m.delay < 0)) warn.push(`${label}: delay cannot be negative.`);
  return warn;
}
function forwardedWarnings(all, servers) {
  const out = new Set();
  all.forEach((m) => {
    const s = servers.find((x) => x.id === m.server_id);
    if (s && !s.mongo_host && ["host.docker.internal", "127.0.0.1", "localhost"].includes(s.hostname))
      out.add(`${s.name} is reached through a forwarded port: set its "MongoDB host name" to the VM's private network IP.`);
  });
  return [...out];
}
function fakeProcs(ms, servers, rs, shard, startId = 0) {
  return ms.map((m, i) => {
    const s = servers.find((x) => x.id === m.server_id) || {};
    return { id: -(startId + i + 1), host: `${s.mongo_host || s.hostname}:${m.port}`, up: 1, rs_name: rs, shard,
      role: m.mongos ? "MONGOS" : m.arbiter ? "ARBITER" : i === 0 ? "PRIMARY" : "SECONDARY", metrics: {},
      info: { member: { hidden: !!m.hidden, arbiter_only: !!m.arbiter, priority: m.priority ?? 1, votes: m.votes ?? 1, delay: m.delay || 0 } } };
  });
}
function cleanBody(fd) {
  const body = Object.fromEntries([...fd.entries()].filter(([k]) => !k.startsWith("m_") && !k.startsWith("tag_") && !k.startsWith("sh_")));
  body.tags = readTags(fd);
  if (!body.cache_gb) delete body.cache_gb;
  if (!body.tarball_url) delete body.tarball_url;
  return body;
}
async function submitCreate(ev, path, body) {
  const btn = ev.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const r = await api(path, { method: "POST", body });
    toast("Creation started");
    location.hash = `#/jobs/${r.job_id}`;
  } catch (e) { toast(e.message, true); btn.disabled = false; }
}

async function pageNewReplicaSet() {
  const [servers, versions, deps] = await Promise.all([api("/servers"), api("/mongodb-versions").catch(() => ({ versions: [] })), api("/deployments")]);
  if (!servers.length) return noServers("Create replica set");
  main.innerHTML = `<div class="page-head"><div><div class="small"><a href="#/deployments">Deployments</a></div><h1>Create replica set</h1>
    <div class="sub">MongoPilot installs the binaries, configures systemd, initiates the set with authentication enabled and starts monitoring it.</div></div></div>
    <form id="rs-form" class="grid2" style="align-items:start;gap:20px">
    <div class="panel panel-pad">
      <h2>Deployment</h2>
      <div class="grid2"><div class="field"><label for="r-name">Name</label><input id="r-name" name="name" required placeholder="e.g. orders-prod"></div>
      <div class="field"><label for="r-rs">Replica set name</label><input id="r-rs" name="rs_name" class="mono" placeholder="from the name"></div></div>
      ${commonFields(versions.versions, randomPassword(), "r")}
      <h3>Tags</h3>${tagFields({}, deps)}
    </div>
    <div class="panel panel-pad rs-members">
      <div class="row" style="margin-bottom:10px"><h2 class="grow" style="margin:0">Members</h2><button type="button" class="small" id="add-member">Add member</button></div>
      ${MEMBER_HEAD}<div id="members"></div>
      <p class="small muted">A delayed member is always hidden with priority 0; it keeps a copy of the data that lags by the delay, useful to recover from mistakes.</p>
      <div id="rs-warn" style="margin-top:12px"></div>
      <h3 style="margin-top:16px">Preview</h3><div id="rs-preview" class="compact"></div>
    </div>
    <div style="grid-column:1/-1" class="row"><span class="grow"></span><a class="btn" href="#/deployments">Cancel</a><button class="primary" type="submit">Create replica set</button></div>
    </form>`;
  const opts = servers.map((s) => `<option value="${s.id}" title="${esc(s.hostname)}">${esc(s.name)}</option>`).join("");
  const box = $("#members");
  const preview = () => {
    const ms = readRows(box);
    const warn = [...setWarnings(ms, "Replica set", servers), ...forwardedWarnings(ms, servers)];
    if (new Set(ms.map((m) => m.server_id)).size < ms.length) warn.push("Several members on the same server reduce redundancy.");
    $("#rs-warn").innerHTML = warn.map((w) => `<div class="notice">${esc(w)}</div>`).join("");
    $("#rs-preview").innerHTML = topologyHTML(fakeProcs(ms, servers, "", null), null, false);
  };
  const addRow = (sid) => {
    const row = memberRow(opts, freePort(servers, sid || servers[0].id, 27017), sid, false);
    row.querySelector("button").onclick = () => { row.remove(); preview(); };
    box.appendChild(row); preview();
  };
  servers.slice(0, 3).forEach((s) => addRow(s.id));
  $("#add-member").onclick = () => addRow();
  box.addEventListener("input", preview);
  $("#rs-form").onsubmit = (ev) => {
    ev.preventDefault();
    const body = cleanBody(new FormData(ev.target));
    body.members = readRows(box);
    submitCreate(ev, "/deployments/replicaset", body);
  };
}

async function pageNewSharded() {
  const [servers, versions, deps] = await Promise.all([api("/servers"), api("/mongodb-versions").catch(() => ({ versions: [] })), api("/deployments")]);
  if (!servers.length) return noServers("Create sharded cluster");
  main.innerHTML = `<div class="page-head"><div><div class="small"><a href="#/deployments">Deployments</a></div><h1>Create sharded cluster</h1>
    <div class="sub">A config server replica set, one or more shard replica sets and the mongos routers applications connect to. Authentication is enabled with a shared keyfile.</div></div></div>
    <form id="sh-form">
    <div class="grid2" style="align-items:start;gap:20px">
      <div class="panel panel-pad"><h2>Deployment</h2>
        <div class="grid2"><div class="field"><label for="s-name">Name</label><input id="s-name" name="name" required placeholder="e.g. shop-prod"></div>
        <div class="field"><label for="s-crs">Config server replica set name</label><input id="s-crs" name="config_rs" class="mono" placeholder="from the name: <name>-config"></div></div>
        ${commonFields(versions.versions, randomPassword(), "s")}
        <h3>Tags</h3>${tagFields({}, deps)}
      </div>
      <div class="panel panel-pad"><h2>Preview</h2><div id="sh-preview" class="compact"></div><div id="sh-warn" style="margin-top:12px"></div></div>
    </div>
    <div class="section panel panel-pad rs-members"><div class="row" style="margin-bottom:10px"><h2 class="grow" style="margin:0">Config servers</h2><button type="button" class="small" data-add="cfg">Add config server</button></div>
      <p class="small muted" style="margin-top:0">Three members on different servers are recommended. Config servers cannot be arbiters.</p>
      ${MEMBER_HEAD}<div id="cfg-members"></div></div>
    <div class="section"><div class="row" style="margin-bottom:10px"><h2 class="grow" style="margin:0">Shards</h2><button type="button" class="small" id="add-shard">Add shard</button></div><div id="shards"></div></div>
    <div class="section panel panel-pad simple-members"><div class="row" style="margin-bottom:10px"><h2 class="grow" style="margin:0">mongos routers</h2><button type="button" class="small" data-add="mongos">Add mongos</button></div>
      <p class="small muted" style="margin-top:0">Applications connect to these. Use at least two for availability.</p>
      ${SIMPLE_HEAD}<div id="mongos-members"></div></div>
    <div class="row" style="margin-top:16px"><span class="grow"></span><a class="btn" href="#/deployments">Cancel</a><button class="primary" type="submit">Create sharded cluster</button></div>
    </form>`;
  const opts = servers.map((s) => `<option value="${s.id}" title="${esc(s.hostname)}">${esc(s.name)}</option>`).join("");
  const sid = (i) => servers[i % servers.length].id;
  const collect = () => ({
    config: readRows($("#cfg-members")),
    shards: $$("#shards .shard-block").map((b) => ({ name: b.querySelector("[name=sh_name]").value.trim(), members: readRows(b) })),
    mongos: readRows($("#mongos-members"), true),
  });
  const preview = () => {
    const c = collect();
    const name = $("#s-name").value.trim() || "cluster";
    let procs = fakeProcs(c.config, servers, $("#s-crs").value || `${name}-config`, "config");
    c.shards.forEach((sh, i) => { procs = procs.concat(fakeProcs(sh.members, servers, `${name}-${sh.name || "shard" + i}`, sh.name || `shard${i}`, procs.length)); });
    procs = procs.concat(fakeProcs(c.mongos.map((m) => ({ ...m, mongos: true })), servers, null, null, procs.length));
    $("#sh-preview").innerHTML = topologyHTML(procs, null, false);
    const warn = [...setWarnings(c.config, "Config servers", servers)];
    c.shards.forEach((sh, i) => warn.push(...setWarnings(sh.members, `Shard ${sh.name || i}`, servers)));
    if (c.mongos.length < 2) warn.push("A single mongos router is a single point of failure for applications.");
    const all = [...c.config, ...c.shards.flatMap((s) => s.members), ...c.mongos];
    const ports = new Set();
    all.forEach((m) => { const k = `${m.server_id}:${m.port}`; if (ports.has(k)) warn.push(`Port ${m.port} is used twice on the same server.`); ports.add(k); });
    warn.push(...forwardedWarnings(all, servers));
    $("#sh-warn").innerHTML = [...new Set(warn)].map((w) => `<div class="notice">${esc(w)}</div>`).join("");
  };
  const addTo = (box, port, s, simple, noArb) => {
    const row = memberRow(opts, freePort(servers, s || servers[0].id, port), s, simple, noArb);
    row.querySelector("button.link").onclick = () => { row.remove(); preview(); };
    box.appendChild(row);
  };
  let shardCount = 0;
  const addShard = () => {
    const n = shardCount++;
    const b = document.createElement("div");
    b.className = "shard-block rs-members";
    b.innerHTML = `<div class="row" style="margin-bottom:10px;flex-wrap:wrap"><div class="field grow" style="margin:0;max-width:260px"><label>Shard name</label><input name="sh_name" class="mono" value="sh${n}"></div>
      <span class="grow"></span><button type="button" class="small" data-addm>Add member</button><button type="button" class="link small" data-rm>Remove shard</button></div>
      ${MEMBER_HEAD}<div data-ms></div>`;
    const ms = b.querySelector("[data-ms]");
    const port = n === 0 ? 27018 : 27019 + n;   // 27019 is the config servers' port
    for (let i = 0; i < 3; i++) addTo(ms, port, sid(i));
    b.querySelector("[data-addm]").onclick = () => { addTo(ms, port); preview(); };
    b.querySelector("[data-rm]").onclick = () => { b.remove(); preview(); };
    $("#shards").appendChild(b);
    preview();
  };
  for (let i = 0; i < 3; i++) addTo($("#cfg-members"), 27019, sid(i), false, true);
  addShard(); addShard();
  for (let i = 0; i < Math.min(2, servers.length); i++) addTo($("#mongos-members"), 27017, sid(i), true);
  $$("[data-add]").forEach((b) => (b.onclick = () => {
    if (b.dataset.add === "cfg") addTo($("#cfg-members"), 27019, null, false, true);
    else addTo($("#mongos-members"), 27017, null, true);
    preview();
  }));
  $("#add-shard").onclick = addShard;
  $("#sh-form").addEventListener("input", preview);
  preview();
  $("#sh-form").onsubmit = (ev) => {
    ev.preventDefault();
    const body = cleanBody(new FormData(ev.target));
    const c = collect();
    body.config = { rs_name: body.config_rs || undefined, members: c.config };
    delete body.config_rs;
    body.shards = c.shards;
    body.mongos = c.mongos;
    submitCreate(ev, "/deployments/sharded", body);
  };
}

/* ---------------- servers ---------------- */
function meter(pct, warnAt, critAt, invert = false) {
  if (pct === undefined || pct === null) return "–";
  const bad = invert ? pct <= critAt : pct >= critAt, mid = invert ? pct <= warnAt : pct >= warnAt;
  return `<span class="meter"><i class="${bad ? "crit" : mid ? "warn" : ""}" style="width:${Math.min(100, Math.max(2, pct))}%"></i></span> <b>${fmtNum(pct, 0)}%</b>`;
}
async function pageServers() {
  const servers = await api("/servers");
  main.innerHTML = `<div class="page-head"><div><h1>Servers</h1><div class="sub">Linux VMs reachable over SSH where MongoPilot installs and manages MongoDB and reads hardware metrics</div></div>
    <div class="row"><button id="btn-vagrant">Import Vagrant machines</button><button class="primary" id="btn-add">Add server</button></div></div>
    <div class="panel table-wrap">${servers.length ? `<table><thead><tr><th>Name</th><th>SSH</th><th>MongoDB host name</th><th>Type</th><th>System</th><th>CPU</th><th>Memory</th><th>Disk free</th><th>Managed processes</th><th></th></tr></thead><tbody>
    ${servers.map((s) => { const m = s.metrics || {}; return `<tr class="clickable" data-srv="${s.id}"><td><a href="#/servers/${s.id}">${esc(s.name)}</a>${m.host_up === 0 ? ` ${pill("crit", "unreachable")}` : ""}</td>
      <td class="mono small">${esc(s.ssh_user)}@${esc(s.hostname)}${s.ssh_port !== 22 ? ":" + s.ssh_port : ""}${s.has_key ? "" : ` ${pill("warn", "no key")}`}</td>
      <td class="mono small">${esc(s.mongo_host || s.hostname)}</td>
      <td>${s.kind === "vagrant" ? `<span class="tag">Vagrant VM</span>` : `<span class="tag">standard</span>`}</td>
      <td class="small" style="white-space:nowrap">${s.facts.os_name ? esc(s.facts.os_name) + `<div class="muted">${esc(s.facts.cpus || "?")} CPU · ${fmtBytes(Number(s.facts.mem_mb) * 1048576)}</div>` : `<span class="muted">not checked</span>`}
        ${s.facts.sudo === "no" ? pill("crit", "no sudo") : ""}</td>
      <td class="small">${meter(m.sys_cpu_pct, 70, 85)}</td><td class="small">${meter(m.sys_mem_used_pct, 85, 95)}</td><td class="small">${meter(m.disk_free_pct, 20, 10, true)}</td>
      <td class="small">${s.processes.map((p) => `<a href="#/deployments/${p.deployment_id}">${esc(p.deployment)}</a> <span class="mono">:${p.port}</span>`).join("<br>") || "–"}</td>
      <td class="num"><button class="small" data-check="${s.id}">Check</button></td></tr>`; }).join("")}
    </tbody></table>` : `<div class="empty"><h3>No servers yet</h3><p class="small" style="margin:0 auto">Add your VMs with their SSH user and private key. For Vagrant, use <b>Import Vagrant machines</b> and paste the output of <code>vagrant ssh-config</code>.</p></div>`}</div>
    <div class="section panel panel-pad small muted"><h3 style="color:var(--ink)">Server requirements</h3>
      Linux with systemd (Ubuntu, Debian, RHEL/Rocky/Alma, Amazon Linux, SUSE), SSH access with a private key without passphrase, passwordless sudo
      (the <code>vagrant</code> user has it by default) and access to fastdl.mongodb.org or an internal mirror. MongoPilot must reach each member's MongoDB port.
      Hardware metrics are read from <code>/proc</code> over SSH every polling cycle, without sudo.</div>`;
  $("#btn-add").onclick = () => serverForm();
  $("#btn-vagrant").onclick = vagrantImport;
  $$("tr[data-srv]").forEach((tr) => tr.addEventListener("click", (ev) => { if (!ev.target.closest("a, button")) location.hash = `#/servers/${tr.dataset.srv}`; }));
  $$("[data-check]").forEach((b) => (b.onclick = async () => { const r = await api(`/servers/${b.dataset.check}/check`, { method: "POST" }); location.hash = `#/jobs/${r.job_id}`; }));
  autoRefresh(async () => { if (state.route === location.hash) await pageServers(); }, 30000);
}
async function pageServer(id) {
  const s = await api(`/servers/${id}`);
  const f = s.facts || {}, m = s.metrics || {};
  main.innerHTML = `<div class="page-head"><div><div class="small"><a href="#/servers">Servers</a></div><h1>${esc(s.name)}</h1>
      <div class="sub row">${m.host_up === 0 ? pill("crit", "unreachable over SSH") : m.host_up ? pill("ok", "reachable") : pill("off", "no data yet")}
      <span class="mono">${esc(s.ssh_user)}@${esc(s.hostname)}:${s.ssh_port}</span>${s.kind === "vagrant" ? `<span class="tag">Vagrant VM</span>` : ""}</div></div>
      <div class="row"><button id="b-check">Check server</button><button id="b-edit">Edit</button>${s.kind !== "vagrant" ? `<button id="b-forget">Forget host key</button>` : ""}<button class="danger" id="b-del">Delete</button></div></div>
    <div class="panel panel-pad"><dl class="kv"><dt>Operating system</dt><dd>${esc(f.os_name || "not checked yet")}</dd><dt>CPU / RAM</dt><dd>${esc(f.cpus || "?")} CPU · ${f.mem_mb ? fmtBytes(Number(f.mem_mb) * 1048576) : "?"}</dd>
      <dt>MongoDB host name</dt><dd class="mono">${esc(s.mongo_host || s.hostname)}</dd><dt>Passwordless sudo</dt><dd>${esc(f.sudo || "?")}</dd><dt>Notes</dt><dd>${esc(s.notes || "–")}</dd></dl></div>
    <div class="section"><div class="row" style="margin-bottom:12px"><h2 class="grow" style="margin:0">Hardware metrics</h2>${rangeSeg(state.range)}</div><div id="charts" data-charts></div></div>`;
  $$("[data-range]").forEach((b) => (b.onclick = () => { state.range = Number(b.dataset.range); pageServer(id); }));
  $("#b-check").onclick = async () => { const r = await api(`/servers/${id}/check`, { method: "POST" }); location.hash = `#/jobs/${r.job_id}`; };
  $("#b-edit").onclick = () => serverForm(s, () => pageServer(id));
  $("#b-del").onclick = () => confirmAction("Delete this server?", "The server is removed from MongoPilot. Nothing is changed on the machine.", "Delete",
    async () => { await api(`/servers/${id}`, { method: "DELETE" }); toast("Server deleted"); location.hash = "#/servers"; }, true);
  const bf = $("#b-forget");
  if (bf) bf.onclick = () => confirmAction("Forget the SSH host key?", "Do this only if the server was re-installed. The next connection pins the new key.", "Forget host key",
    async () => { await api(`/servers/${id}/forget-host-key`, { method: "POST" }); toast("Host key forgotten"); });
  await renderCharts($("#charts"), { srv: `srv:${id}` }, "Host");
}

/* private key picker: drop zone + file input; the key text goes in a hidden field */
function keyPicker(name, hint) {
  return `<label class="dropzone" data-key="${name}"><input type="file" data-keyfile>
    <span data-keylabel>${esc(hint)}</span></label><input type="hidden" name="${name}">`;
}
function checkKeyText(text) {
  const t = text.trim();
  if (t.includes("BEGIN CERTIFICATE")) return "This is a certificate, not a private key";
  if (t.startsWith("PuTTY-User-Key-File")) return "PuTTY key: export it in OpenSSH format with PuTTYgen";
  if (/^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-)/.test(t)) return "This is a public key (.pub)";
  if (!/^-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/.test(t)) return "Not a private key file";
  return null;
}
function bindKeyPickers(root) {
  $$("[data-key]", root).forEach((zone) => {
    const input = $("[data-keyfile]", zone), label = $("[data-keylabel]", zone);
    const hidden = zone.nextElementSibling;
    const load = async (file) => {
      if (!file) return;
      if (file.size > 64 * 1024) { zone.className = "dropzone bad"; label.textContent = "File too large for a private key"; return; }
      const text = await file.text();
      const err = checkKeyText(text);
      hidden.value = err ? "" : text;
      zone.className = "dropzone " + (err ? "bad" : "okk");
      label.textContent = err ? `${file.name}: ${err}` : `${file.name} loaded`;
    };
    input.onchange = () => load(input.files[0]);
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
    zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("drag"); load(e.dataTransfer.files[0]); });
  });
}
function serverForm(s, after) {
  openModal({
    title: s ? `Edit ${s.name}` : "Add server", submit: s ? "Save" : "Add server", wide: true,
    body: `<div class="grid3"><div class="field"><label for="s-n">Name</label><input id="s-n" name="name" required value="${esc(s?.name || "")}" placeholder="e.g. mongo1"></div>
      <div class="field"><label for="s-h">SSH host name or IP</label><input id="s-h" name="hostname" class="mono" required value="${esc(s?.hostname || "")}" placeholder="e.g. 192.168.56.11"></div>
      <div class="field"><label for="s-p">SSH port</label><input id="s-p" name="ssh_port" type="number" value="${s?.ssh_port || 22}"></div></div>
      <div class="grid3"><div class="field"><label for="s-u">SSH user</label><input id="s-u" name="ssh_user" required value="${esc(s?.ssh_user || "vagrant")}"></div>
      <div class="field"><label for="s-m">MongoDB host name</label><input id="s-m" name="mongo_host" class="mono" value="${esc(s?.mongo_host || "")}" placeholder="same as SSH host">
        <div class="hint">The name or IP replica set members use to reach this server.</div></div>
      <div class="field"><label for="s-k">Server type</label><select id="s-k" name="kind">
        <option value="vagrant" ${(s ? s.kind === "vagrant" : true) ? "selected" : ""}>Vagrant VM</option><option value="standard" ${s && s.kind !== "vagrant" ? "selected" : ""}>Standard server</option></select>
        <div class="hint">Vagrant VMs get a new SSH host key when re-created, so it is not pinned.</div></div></div>
      <div class="field"><label>SSH private key</label>${keyPicker("ssh_key", s?.has_key ? "A key is stored. Drop or choose a file only to replace it." : "Drop the private key file here, or click to choose it")}
        <div class="hint">For Vagrant: <code>.vagrant/machines/&lt;name&gt;/&lt;provider&gt;/private_key</code> in the folder with the Vagrantfile (<code>vagrant ssh-config</code> shows the exact path). The key must not have a passphrase; it is stored encrypted.</div>
        <details style="margin-top:8px"><summary class="small" style="cursor:pointer">Paste the key instead</summary><textarea name="ssh_key_paste" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea></details></div>
      <div class="field"><label for="s-no">Notes</label><input id="s-no" name="notes" value="${esc(s?.notes || "")}"></div>`,
    onOpen: (form) => bindKeyPickers(form),
    onSubmit: async (fd) => {
      const body = Object.fromEntries(fd.entries());
      body.ssh_key = body.ssh_key || body.ssh_key_paste || "";
      delete body.ssh_key_paste;
      if (body.ssh_key) { const err = checkKeyText(body.ssh_key); if (err) throw new Error(err); }
      if (!body.ssh_key) delete body.ssh_key;
      if (s) await api(`/servers/${s.id}`, { method: "PUT", body });
      else await api("/servers", { method: "POST", body });
      toast(s ? "Server updated" : "Server added: use Check to verify SSH and sudo");
      (after || pageServers)();
    },
  });
}
function vagrantImport() {
  let hosts = [];
  openModal({
    title: "Import Vagrant machines", submit: null, wide: true,
    body: `<ol class="small" style="padding-left:18px;margin-top:0"><li>In the folder with your Vagrantfile, run <code>vagrant ssh-config</code> and paste the output below.</li>
      <li>For each machine, drop its <code>private_key</code> file (the path is shown next to it).</li></ol>
      <div class="field"><textarea id="v-cfg" style="min-height:130px" placeholder="Host mongo1&#10;  HostName 192.168.56.11&#10;  User vagrant&#10;  Port 22&#10;  IdentityFile C:/projects/mongo-lab/.vagrant/machines/mongo1/virtualbox/private_key"></textarea></div>
      <button type="button" id="v-read">Read machines</button>
      <div id="v-rows" style="margin-top:14px"></div>`,
    onSubmit: () => false,
    onOpen: (form) => {
      $("#v-read", form).onclick = async () => {
        try { hosts = (await api("/servers/parse-ssh-config", { method: "POST", body: { text: $("#v-cfg", form).value } })).hosts; }
        catch (e) { toast(e.message, true); return; }
        $("#v-rows", form).innerHTML = `${hosts.some((h) => h.forwarded) ? `<div class="notice">Some machines are reached through a port forwarded on your computer (127.0.0.1). MongoPilot will use <code>host.docker.internal</code> for SSH, but replica set members need the VM's <b>private network IP</b> as "MongoDB host name" (add <code>config.vm.network "private_network", ip: "192.168.56.11"</code> to the Vagrantfile).</div>` : ""}
          <div class="import-row small muted"><span>Name</span><span>SSH</span><span>MongoDB host name</span><span>Private key</span></div>
          ${hosts.map((h, i) => `<div class="import-row" data-i="${i}"><input value="${esc(h.name)}" data-f="name" aria-label="Name">
            <span class="mono small">${esc(h.user)}@${esc(h.hostname)}:${h.port}</span>
            <input class="mono" data-f="mongo_host" value="${esc(h.forwarded ? "" : h.hostname)}" placeholder="private network IP" aria-label="MongoDB host name">
            <div>${keyPicker(`key_${i}`, "Drop private_key")}<div class="muted small mono" style="margin-top:4px;word-break:break-all">${esc(h.identity_file || "")}</div></div></div>`).join("")}
          <div class="row" style="margin-top:14px"><span class="grow small muted" id="v-status"></span><button type="button" class="primary" id="v-add">Add ${hosts.length} server${hosts.length === 1 ? "" : "s"}</button></div>`;
        bindKeyPickers(form);
        $("#v-add", form).onclick = async () => {
          const results = [];
          for (const [i, h] of hosts.entries()) {
            const row = $(`.import-row[data-i="${i}"]`, form);
            if (row.dataset.done) continue;
            const key = $(`[name=key_${i}]`, form).value;
            const name = $("[data-f=name]", row).value.trim();
            if (!key) { results.push(`${name}: private key missing`); continue; }
            try {
              await api("/servers", { method: "POST", body: { name, hostname: h.hostname, ssh_port: h.port, ssh_user: h.user,
                mongo_host: $("[data-f=mongo_host]", row).value.trim(), kind: "vagrant", ssh_key: key, notes: "Imported from vagrant ssh-config" } });
              row.dataset.done = "1"; row.style.opacity = .5;
            } catch (e) { results.push(`${name}: ${e.message}`); }
          }
          if (results.length) { $("#v-status", form).textContent = results.join(" · "); toast(`${results.length} machine(s) not added yet`, true); }
          else { $("#modal").close(); toast("Servers added: use Check on each one"); pageServers(); }
        };
      };
    },
  });
}

/* ---------------- alerts ---------------- */
function alertsTable(alerts) {
  if (!alerts.length) return `<div class="empty">No active alerts.</div>`;
  return `<table><thead><tr><th>Severity</th><th>Problem</th><th>Opened</th><th>Status</th><th></th></tr></thead><tbody>
  ${alerts.map((a) => `<tr><td>${pill(...SEVERITY[a.severity] || ["info", a.severity])}</td>
    <td>${esc(a.message)}${a.deployment_id ? ` <a class="small" href="#/deployments/${a.deployment_id}">open</a>` : ""}</td>
    <td class="small">${esc(dateTime(a.opened_at))}<div class="muted">${a.status === "resolved" ? "lasted " + esc(fmtDur((a.resolved_at || a.updated_at) - a.opened_at)) : esc(ago(a.opened_at))}</div></td>
    <td>${a.status === "open" ? pill("crit", "open") : a.status === "acknowledged" ? pill("warn", `acknowledged${a.acknowledged_by ? " by " + a.acknowledged_by : ""}`) : pill("ok", "resolved")}</td>
    <td class="num">${a.status === "open" ? `<button class="small" data-ack="${a.id}">Acknowledge</button>` : ""}</td></tr>`).join("")}</tbody></table>`;
}
function bindAlertActions(after) {
  $$("[data-ack]").forEach((b) => (b.onclick = async () => {
    await api(`/alerts/${b.dataset.ack}/ack`, { method: "POST" });
    toast("Alert acknowledged: no more reminders");
    refreshNav(); (after || route)();
  }));
}
async function pageAlerts() {
  const which = state.alertsView || "active";
  const alerts = await api(`/alerts?status=${which}`);
  main.innerHTML = `<div class="page-head"><div><h1>Alerts</h1><div class="sub">Raised by alert rules on the latest metrics</div></div>
    <div class="seg"><button data-v="active" class="${which === "active" ? "on" : ""}">Active</button><button data-v="all" class="${which === "all" ? "on" : ""}">History</button></div></div>
    <div class="panel table-wrap">${alertsTable(alerts)}</div>`;
  $$("[data-v]").forEach((b) => (b.onclick = () => { state.alertsView = b.dataset.v; pageAlerts(); }));
  bindAlertActions(pageAlerts);
  autoRefresh(pageAlerts, 15000);
}

/* ---------------- rules ---------------- */
async function pageRules() {
  const [rules, channels, deps] = await Promise.all([api("/rules"), api("/channels"), api("/deployments")]);
  const chName = (id) => channels.find((c) => c.id === id)?.name || "?";
  main.innerHTML = `<div class="page-head"><div><h1>Alert rules</h1><div class="sub">A rule fires when its condition stays true for the given duration</div></div>
    <button class="primary" id="btn-add">New rule</button></div>
    <div class="panel table-wrap"><table><thead><tr><th>Rule</th><th>Condition</th><th>Scope</th><th>Severity</th><th>Notifications</th><th>Enabled</th><th></th></tr></thead><tbody>
    ${rules.map((r) => { const m = metricMeta(r.metric); return `<tr><td>${esc(r.name)}</td>
      <td class="small">${esc(m.label)} ${esc(r.operator)} ${esc(fmtNum(r.threshold))}${m.unit === "%" ? "%" : m.unit ? " " + esc(m.unit) : ""}${r.duration_s ? `<div class="muted">for at least ${esc(fmtDur(r.duration_s))}</div>` : ""}</td>
      <td class="small">${m.scope === "srv" ? "All servers" : r.deployment_id ? esc(deps.find((d) => d.id === r.deployment_id)?.name || "?") : "All deployments"}</td>
      <td>${pill(...SEVERITY[r.severity])}</td>
      <td class="small">${r.channel_ids.length ? r.channel_ids.map((c) => esc(chName(c))).join(", ") : "default channels"}${r.renotify_s ? `<div class="muted">repeat every ${esc(fmtDur(r.renotify_s))}</div>` : ""}</td>
      <td>${r.enabled ? pill("ok", "yes") : pill("off", "no")}</td>
      <td class="num"><div class="row" style="justify-content:flex-end;flex-wrap:nowrap"><button class="small" data-edit="${r.id}">Edit</button><button class="small danger" data-del="${r.id}">Delete</button></div></td></tr>`; }).join("")}
    </tbody></table></div>`;
  $("#btn-add").onclick = () => ruleForm(null, channels, deps);
  $$("[data-edit]").forEach((b) => (b.onclick = () => ruleForm(rules.find((r) => r.id === Number(b.dataset.edit)), channels, deps)));
  $$("[data-del]").forEach((b) => (b.onclick = () => confirmAction("Delete this rule?", "Alerts opened by this rule will be closed.", "Delete",
    async () => { await api(`/rules/${b.dataset.del}`, { method: "DELETE" }); toast("Rule deleted"); pageRules(); }, true)));
}
function ruleForm(r, channels, deps) {
  const metrics = state.catalog.metrics;
  const groups = [...new Set(metrics.map((m) => m.group))];
  openModal({
    title: r ? "Edit rule" : "New rule", submit: r ? "Save" : "Create rule",
    body: `<div class="field"><label for="r-n">Name</label><input id="r-n" name="name" required value="${esc(r?.name || "")}"></div>
      <div class="grid3"><div class="field"><label for="r-m">Metric</label><select id="r-m" name="metric">
        ${groups.map((g) => `<optgroup label="${esc(g)}">${metrics.filter((m) => m.group === g).map((m) => `<option value="${m.name}" ${r?.metric === m.name ? "selected" : ""}>${esc(m.label)}${m.unit ? ` (${esc(m.unit)})` : ""}</option>`).join("")}</optgroup>`).join("")}</select></div>
      <div class="field"><label for="r-o">Condition</label><select id="r-o" name="operator">${state.catalog.operators.map((o) => `<option ${(r?.operator || ">") === o ? "selected" : ""}>${o}</option>`).join("")}</select></div>
      <div class="field"><label for="r-t">Threshold</label><input id="r-t" name="threshold" type="number" step="any" required value="${r?.threshold ?? ""}"></div></div>
      <div class="grid3"><div class="field"><label for="r-d">For at least (s)</label><input id="r-d" name="duration_s" type="number" min="0" value="${r?.duration_s ?? 60}"></div>
      <div class="field"><label for="r-s">Severity</label><select id="r-s" name="severity">${Object.entries(SEVERITY).map(([k, [, l]]) => `<option value="${k}" ${(r?.severity || "warning") === k ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div class="field"><label for="r-re">Repeat notification every (s)</label><input id="r-re" name="renotify_s" type="number" min="0" value="${r?.renotify_s ?? 0}"><div class="hint">0 = notify once</div></div></div>
      <div class="field"><label for="r-dep">Scope</label><select id="r-dep" name="deployment_id"><option value="">All deployments</option>
        ${deps.map((d) => `<option value="${d.id}" ${r?.deployment_id === d.id ? "selected" : ""}>${esc(d.name)}</option>`).join("")}</select><div class="hint">Host metrics apply to every registered server.</div></div>
      <div class="field"><label>Notification channels</label>${channels.length ? channels.map((c) => `<label class="check"><input type="checkbox" name="ch" value="${c.id}" ${r?.channel_ids.includes(c.id) ? "checked" : ""}>${esc(c.name)} <span class="muted small">&nbsp;${esc(c.type)}</span></label>`).join("")
        : `<div class="hint">No channel configured: alerts only appear in MongoPilot.</div>`}<div class="hint">No selection = default channels.</div></div>
      <label class="check"><input type="checkbox" name="enabled" ${r?.enabled === 0 ? "" : "checked"}>Rule enabled</label>`,
    onSubmit: async (fd) => {
      const body = { name: fd.get("name"), metric: fd.get("metric"), operator: fd.get("operator"), threshold: Number(fd.get("threshold")),
        duration_s: Number(fd.get("duration_s")), severity: fd.get("severity"), renotify_s: Number(fd.get("renotify_s")),
        deployment_id: fd.get("deployment_id") ? Number(fd.get("deployment_id")) : null, channel_ids: fd.getAll("ch").map(Number), enabled: fd.get("enabled") === "on" };
      if (r) await api(`/rules/${r.id}`, { method: "PUT", body }); else await api("/rules", { method: "POST", body });
      toast(r ? "Rule saved" : "Rule created");
      pageRules();
    },
  });
}

/* ---------------- channels ---------------- */
async function pageChannels() {
  const channels = await api("/channels");
  const TYPE = { email: "Email", slack: "Slack", webhook: "Webhook" };
  main.innerHTML = `<div class="page-head"><div><h1>Notifications</h1><div class="sub">Where alerts are delivered</div></div><button class="primary" id="btn-add">Add channel</button></div>
    <div class="panel table-wrap">${channels.length ? `<table><thead><tr><th>Name</th><th>Type</th><th>Destination</th><th>Default</th><th></th></tr></thead><tbody>
    ${channels.map((c) => `<tr><td>${esc(c.name)}</td><td>${TYPE[c.type]}</td><td class="mono small">${esc(c.config.to ? c.config.to.join(", ") : c.config.webhook_url || c.config.url)}</td>
      <td>${c.is_default ? pill("ok", "yes") : pill("off", "no")}</td>
      <td class="num"><div class="row" style="justify-content:flex-end"><button class="small" data-test="${c.id}">Send test</button><button class="small danger" data-del="${c.id}">Delete</button></div></td></tr>`).join("")}
    </tbody></table>` : `<div class="empty"><h3>No channels yet</h3><p class="small" style="margin:0 auto">Add an email address, a Slack webhook or a generic webhook (PagerDuty, Teams or Opsgenie through their endpoints).</p></div>`}</div>
    <p class="small muted section">Default channels receive alerts from rules that do not name specific channels. Email uses the SMTP server configured with the MP_SMTP_* variables.</p>`;
  $("#btn-add").onclick = channelForm;
  $$("[data-test]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try { await api(`/channels/${b.dataset.test}/test`, { method: "POST" }); toast("Test notification sent"); } catch (e) { toast(e.message, true); }
    b.disabled = false;
  }));
  $$("[data-del]").forEach((b) => (b.onclick = () => confirmAction("Delete this channel?", "Rules using it will stop notifying on this channel.", "Delete",
    async () => { await api(`/channels/${b.dataset.del}`, { method: "DELETE" }); toast("Channel deleted"); pageChannels(); }, true)));
}
function channelForm() {
  const fields = {
    email: `<div class="field"><label for="c-to">Recipients</label><input id="c-to" name="to" required placeholder="dba@company.com, oncall@company.com"></div>`,
    slack: `<div class="field"><label for="c-wh">Slack webhook URL</label><input id="c-wh" name="webhook_url" class="mono" required placeholder="https://hooks.slack.com/services/…"></div>`,
    webhook: `<div class="field"><label for="c-u">URL</label><input id="c-u" name="url" class="mono" required placeholder="https://…"><div class="hint">Receives a JSON POST with the alert, the rule and a link.</div></div>
      <div class="field"><label for="c-h">Authentication header (optional)</label><input id="c-h" name="auth_header" class="mono" placeholder="Authorization: Bearer …"></div>`,
  };
  openModal({
    title: "Add channel", submit: "Add channel",
    body: `<div class="grid2"><div class="field"><label for="c-n">Name</label><input id="c-n" name="name" required placeholder="e.g. DBA on-call"></div>
      <div class="field"><label for="c-t">Type</label><select id="c-t" name="type"><option value="email">Email</option><option value="slack">Slack</option><option value="webhook">Webhook</option></select></div></div>
      <div id="c-fields">${fields.email}</div><label class="check"><input type="checkbox" name="is_default" checked>Default channel</label>`,
    onOpen: (form) => { $("#c-t", form).onchange = (e) => ($("#c-fields", form).innerHTML = fields[e.target.value]); },
    onSubmit: async (fd) => {
      const type = fd.get("type");
      const config = {};
      if (type === "email") config.to = fd.get("to");
      if (type === "slack") config.webhook_url = fd.get("webhook_url");
      if (type === "webhook") {
        config.url = fd.get("url");
        const h = fd.get("auth_header");
        if (h && h.includes(":")) config.headers = { [h.split(":")[0].trim()]: h.slice(h.indexOf(":") + 1).trim() };
      }
      await api("/channels", { method: "POST", body: { name: fd.get("name"), type, config, is_default: fd.get("is_default") === "on" } });
      toast("Channel added");
      pageChannels();
    },
  });
}

/* ---------------- jobs ---------------- */
function jobsTable(jobs, withDep = true) {
  if (!jobs.length) return `<div class="empty">No jobs yet.</div>`;
  return `<table><thead><tr><th>Job</th>${withDep ? "<th>Deployment</th>" : ""}<th>Status</th><th>Started</th><th class="num">Duration</th></tr></thead><tbody>
  ${jobs.map((j) => `<tr class="clickable" data-job="${j.id}"><td><a href="#/jobs/${j.id}">${esc(j.title)}</a>${j.error ? `<div class="small" style="color:var(--crit)">${esc(j.error.split("\n")[0].slice(0, 140))}</div>` : ""}</td>
    ${withDep ? `<td>${j.deployment ? `<a href="#/deployments/${j.deployment_id}">${esc(j.deployment)}</a>` : "–"}</td>` : ""}
    <td>${pill(...JOB_STATUS[j.status] || ["off", j.status])}</td><td class="small">${esc(dateTime(j.created_at))}</td>
    <td class="num small">${j.finished_at && j.started_at ? esc(fmtDur(j.finished_at - j.started_at)) : "–"}</td></tr>`).join("")}</tbody></table>`;
}
function bindJobRows() {
  $$("tr[data-job]").forEach((tr) => tr.addEventListener("click", (ev) => { if (!ev.target.closest("a")) location.hash = `#/jobs/${tr.dataset.job}`; }));
}
async function pageJobs() {
  const jobs = await api("/jobs");
  main.innerHTML = `<div class="page-head"><div><h1>Jobs</h1><div class="sub">Automation operations run on your servers</div></div></div><div class="panel table-wrap">${jobsTable(jobs)}</div>`;
  bindJobRows();
  autoRefresh(pageJobs, 5000);
}
async function pageJob(id) {
  let job = await api(`/jobs/${id}`);
  const logs = [...job.logs];
  const render = () => {
    const done = job.status === "success" || job.status === "failed";
    main.innerHTML = `<div class="page-head"><div><div class="small"><a href="#/jobs">Jobs</a></div><h1>${esc(job.title)}</h1>
      <div class="sub row">${pill(...JOB_STATUS[job.status])}<span>started by ${esc(job.created_by || "?")} · ${esc(dateTime(job.created_at))}</span>
      ${job.finished_at && job.started_at ? `<span>took ${esc(fmtDur(job.finished_at - job.started_at))}</span>` : ""}</div></div>
      ${job.deployment_id ? `<a class="btn" href="#/deployments/${job.deployment_id}">Open deployment</a>` : ""}</div>
      ${job.status === "failed" ? `<div class="notice crit"><b>The job stopped.</b> ${esc(job.error)}<br><span class="small">Changes already applied stay on the servers; the log shows the last successful step.</span></div>` : ""}
      ${job.status === "success" && job.type === "create_replicaset" ? `<div class="notice info">Replica set created and monitored. The admin credentials are stored encrypted in MongoPilot.</div>` : ""}
      <div class="panel panel-pad"><div class="row" style="margin-bottom:10px"><h2 class="grow" style="margin:0">Log</h2>
      <label class="check small"><input type="checkbox" id="show-debug" ${state.showDebug ? "checked" : ""}>Show command output</label></div>
      <div class="log" id="log">${logs.filter((l) => state.showDebug || l.level !== "debug").map((l) =>
        `<div class="${esc(l.level)}"><span class="t">${new Date(l.ts * 1000).toLocaleTimeString(LOCALE)}</span>${esc(l.message)}</div>`).join("") || (done ? "" : '<div class="debug">Waiting…</div>')}</div></div>`;
    const box = $("#log"); box.scrollTop = box.scrollHeight;
    $("#show-debug").onchange = (e) => { state.showDebug = e.target.checked; render(); };
  };
  render();
  if (job.status === "queued" || job.status === "running") {
    const hash = location.hash;
    const tick = async () => {
      if (location.hash !== hash) return;
      const last = logs.length ? logs[logs.length - 1].id : 0;
      const next = await api(`/jobs/${id}?after=${last}`);
      logs.push(...next.logs);
      const changed = next.logs.length || next.status !== job.status;
      job = { ...next, logs };
      if (changed) render();
      if (job.status === "queued" || job.status === "running") setTimeout(tick, 1200);
      else { refreshNav(); toast(job.status === "success" ? "Job completed" : "Job failed", job.status !== "success"); }
    };
    setTimeout(tick, 1000);
  }
}

/* ---------------- activity log ---------------- */
async function pageAudit() {
  const rows = await api("/audit");
  main.innerHTML = `<div class="page-head"><div><h1>Activity log</h1><div class="sub">Changes and operations made through MongoPilot</div></div></div>
    <div class="panel table-wrap">${rows.length ? `<table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Details</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td class="small">${esc(dateTime(r.ts))}</td><td>${esc(r.actor || "system")}</td><td class="mono small">${esc(r.action)}</td><td class="mono small muted">${esc(r.detail || "")}</td></tr>`).join("")}
    </tbody></table>` : `<div class="empty">No activity recorded yet.</div>`}</div>`;
}

/* ---------------- start ---------------- */
(async function init() {
  try { state.catalog = await api("/catalog"); } catch (e) { main.innerHTML = `<div class="notice crit">${esc(e.message)}</div>`; return; }
  await refreshNav();
  setInterval(refreshNav, 15000);
  window.addEventListener("hashchange", () => { route(); main.focus({ preventScroll: true }); });
  route();
})();
