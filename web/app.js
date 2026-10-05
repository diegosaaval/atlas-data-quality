// ATLAS ONE — live UI. No framework, no build step: one snapshot in, views re-render.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => n == null ? "—" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? (n / 1e3).toFixed(1) + "k" : Math.round(n).toLocaleString();
// Only touch the DOM when the markup actually changed: keeps hover/click targets stable between ticks.
function setHTML(el, html) {
  if (el._html === html) return;
  el._html = html;
  el.innerHTML = html;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const storage = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const state = {
  snap: null,
  view: "overview",
  role: storage.get("atlas.role", "engineer"),
  selectedIncident: null,
  incidentDetail: null,
  copilot: {},
  hover: {},
  meta: null,
  lastFetch: {},
};

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-Atlas-Role": state.role, ...(opts.headers || {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

function toast(msg, ms = 2600) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), ms);
}

// ---------------------------------------------------------- realtime
let ws, wsRetry = 0, pollTimer;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { wsRetry = 0; clearInterval(pollTimer); };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "snapshot") onSnapshot(msg.data);
  };
  ws.onclose = () => {
    $("#live").className = "live";
    $("#clock").textContent = "reconnecting…";
    clearInterval(pollTimer);
    pollTimer = setInterval(() => api("/api/state").then(onSnapshot).catch(() => {}), 2000);
    setTimeout(connect, Math.min(10000, 500 * 2 ** wsRetry++));
  };
}
setInterval(() => ws?.readyState === 1 && ws.send("ping"), 20000);

function onSnapshot(snap) {
  const prev = state.snap;
  state.snap = snap;
  renderHeader(snap);
  render();
  if (prev && snap.tick !== prev.tick) tickHooks.forEach((fn) => fn(snap));
}
const tickHooks = new Set();

// ------------------------------------------------------------ header
function renderHeader(s) {
  const live = $("#live");
  live.className = "live " + (s.running ? "on" : "paused");
  const t = s.sim_time ? new Date(s.sim_time) : null;
  $("#clock").textContent = t
    ? `${t.toLocaleDateString("en", { weekday: "short" })} ${t.toTimeString().slice(0, 5)} · run #${s.tick}`
    : "starting…";
  $("#btn-play").textContent = s.running ? "❚❚" : "▶";
  $("#speed").value = String(s.speed);
  $("#chaos").checked = s.chaos;
  const open = s.platform.open_incidents;
  const badge = $("#inc-badge");
  badge.hidden = !open;
  badge.textContent = open;
}

// ------------------------------------------------------------- views
function setView(view) {
  state.view = view;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === view));
  storage.set("atlas.view", view);
  render(true);
}

function render(force = false) {
  const s = state.snap;
  if (!s) return;
  const out = ({ overview: renderOverview, lineage: renderLineage, lab: renderLab, incidents: renderIncidents,
     contracts: renderContracts, risk: renderRisk })[state.view](s, force);
  out?.catch?.((err) => toast(err.message));
}

const statusLabel = { healthy: "healthy", warning: "warning", critical: "critical", blocked: "blocked", degraded: "degraded", fault: "fault injected" };
const st = (status) => `<span class="st st-${esc(status)}">${esc(statusLabel[status] || status)}</span>`;
const scoreClass = (v) => (v >= 90 ? "" : v >= 70 ? "warn" : "fail");
const scoreColor = (v) => (v >= 90 ? "var(--good)" : v >= 70 ? "var(--warning)" : "var(--critical)");

// ---------------------------------------------------------- overview
function renderOverview(s) {
  const p = s.platform;
  const score = p.score;
  const C = 2 * Math.PI * 50;
  const prevScore = s.series.score.at(-13);
  const delta = prevScore == null ? null : score - prevScore;
  const label = score >= 95 ? "All systems reliable" : score >= 85 ? "Degraded" : "Reliability at risk";
  setHTML($("#score-card"), `
    <svg class="ring" viewBox="0 0 120 120" role="img" aria-label="Reliability score ${score}">
      <circle class="track" cx="60" cy="60" r="50" fill="none" stroke-width="10"/>
      <circle class="value" cx="60" cy="60" r="50" fill="none" stroke-width="10" stroke-linecap="round"
        stroke="${scoreColor(score)}" stroke-dasharray="${C}" stroke-dashoffset="${C * (1 - score / 100)}" transform="rotate(-90 60 60)"/>
      <text class="score-num" x="60" y="64" text-anchor="middle">${score.toFixed(0)}</text>
      <text class="score-sub" x="60" y="80" text-anchor="middle">/ 100</text>
    </svg>
    <div class="score-meta">
      <h3>Platform reliability</h3>
      <p>${st(score >= 95 ? "healthy" : score >= 85 ? "warning" : "critical")} <span class="muted">${label}</span></p>
      <p class="muted small">${delta == null ? "" : `${delta >= 0 ? "▲" : "▼"} ${Math.abs(delta).toFixed(1)} pts vs 1h ago`}</p>
      <p class="muted small">Criticality-weighted across ${s.datasets.filter((d) => ["bronze", "silver", "gold"].includes(d.layer)).length} managed datasets</p>
    </div>`);

  const kpi = (label, value, hint, alert = false) =>
    `<div class="kpi ${alert ? "alert" : ""}"><div class="label">${label}</div><div class="value">${value}</div><div class="hint">${hint}</div></div>`;
  setHTML($("#kpis"), [
    kpi("Open incidents", p.open_incidents, `${s.incidents.filter((i) => i.status === "resolved").length} resolved`, p.open_incidents > 0),
    kpi("Freshness SLO (tier-1)", p.freshness_slo.toFixed(2) + "%", "target 99.0% · rolling 24h", p.freshness_slo < 99),
    kpi("Mean time to detect", p.mttd_minutes == null ? "—" : `${p.mttd_minutes} min`, "from fault to incident"),
    kpi("Mean time to recover", p.mttr_minutes == null ? "—" : `${p.mttr_minutes} min`, "from incident to green"),
    kpi("Rows processed", fmt(p.rows_ingested), `${fmt(p.rows_quarantined)} quarantined · ${fmt(p.rows_blocked)} held by gate`),
    kpi("Est. compute cost", `$${p.daily_cost_usd}/day`, `$${p.run_cost_usd} last run · ${p.tick_ms} ms`),
  ].join(""));

  lineChart($("#chart-score"), "score", s.series.tick, s.series.score);
  barChart($("#chart-throughput"), s.series.tick, s.series.accepted, s.series.quarantined);

  const rows = s.datasets.filter((d) => d.layer !== "source" && d.layer !== "consumer");
  setHTML($("#datasets-table"), `
    <thead><tr><th>Status</th><th>Dataset</th><th>Layer</th><th>Owner</th><th>Freshness</th><th class="num">Rows</th><th>Checks</th><th class="num">Score</th></tr></thead>
    <tbody>${rows.map((d) => {
      const fresh = d.freshness_minutes;
      const pct = fresh == null ? 0 : Math.min(100, (fresh / d.sla_minutes) * 100);
      const fc = fresh == null ? "" : fresh > d.sla_minutes ? "fail" : fresh > 10 ? "warn" : "";
      return `<tr data-ds="${esc(d.id)}">
        <td>${st(d.status)}</td>
        <td class="ds">${esc(d.id)}</td>
        <td><span class="pill layer">${esc(d.layer)}</span> <span class="pill">${esc(d.criticality)}</span></td>
        <td class="muted">${esc(d.owner)}</td>
        <td><span class="bar"><i class="${fc}" style="width:${Math.max(4, pct)}%"></i></span> <span class="small muted num">${fresh == null ? "—" : fresh.toFixed(0)}/${d.sla_minutes}m</span></td>
        <td class="num">${fmt(d.last_rows)}</td>
        <td><span class="checks-strip">${d.checks.map((c) => `<i class="${c.status}" data-tip="${esc(c.name)}: ${esc(c.message)}"></i>`).join("")}</span></td>
        <td class="num"><b>${d.score.toFixed(0)}</b></td></tr>`;
    }).join("")}</tbody>`);

  const cost = s.dag.reduce((a, t) => a + t.cost_usd, 0);
  $("#dag-meta").textContent = `${s.dag.length} tasks · $${cost.toFixed(4)}`;
  setHTML($("#dag"), s.dag.map((t) => `
    <div class="task ${esc(t.status)}" data-tip="${esc(t.note || t.status)}">
      <div class="name">${esc(t.task)}</div>
      <div class="meta">${esc(t.status)} · ${fmt(t.rows)} rows</div>
    </div>`).join(""));

  setHTML($("#events"), s.events.length
    ? s.events.map((e) => `<li><time>${esc(e.time)}</time><span class="lvl ${esc(e.level)}"></span><span>${esc(e.message)}</span></li>`).join("")
    : `<li class="empty">No events yet — everything is quiet. Try the Failure Lab.</li>`);
}

// --------------------------------------------------------------- charts
function scaleLinear(d0, d1, r0, r1) {
  return (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);
}

function lineChart(el, id, xs, ys) {
  const W = el.clientWidth || 600, H = el.clientHeight || 170, m = { l: 30, r: 8, t: 8, b: 18 };
  if (!ys.length) return;
  const min = Math.max(0, Math.min(80, Math.floor(Math.min(...ys) / 10) * 10));
  const x = scaleLinear(0, Math.max(1, ys.length - 1), m.l, W - m.r);
  const y = scaleLinear(min, 100, H - m.b, m.t);
  const pts = ys.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  const ticks = [min, (min + 100) / 2, 100];
  const hi = state.hover[id];
  setHTML(el, `<svg viewBox="0 0 ${W} ${H}">
    <g class="grid">${ticks.map((t) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}"/>`).join("")}</g>
    <g class="axis">${ticks.map((t) => `<text x="${m.l - 6}" y="${y(t) + 3}" text-anchor="end">${t}</text>`).join("")}
      <text x="${m.l}" y="${H - 4}">run ${xs[0]}</text><text x="${W - m.r}" y="${H - 4}" text-anchor="end">run ${xs.at(-1)}</text></g>
    <path d="M${pts[0]} L${pts.join(" L")} L${x(ys.length - 1)},${H - m.b} L${m.l},${H - m.b} Z" fill="var(--series-1)" opacity=".08"/>
    <polyline points="${pts.join(" ")}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>
    ${hi != null && ys[hi] != null ? `<line class="crosshair" x1="${x(hi)}" x2="${x(hi)}" y1="${m.t}" y2="${H - m.b}"/>
      <circle cx="${x(hi)}" cy="${y(ys[hi])}" r="4" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2"/>` : ""}
  </svg>`);
  bindHover(el, id, ys.length, m.l, W - m.r, (i) => `Run <b>#${xs[i]}</b><br>Score <b>${ys[i]}</b>`);
}

function barChart(el, xs, a, b) {
  const id = "throughput";
  const W = el.clientWidth || 600, H = el.clientHeight || 170, m = { l: 30, r: 8, t: 8, b: 18 };
  const n = a.length;
  if (!n) return;
  const max = Math.max(10, ...a.map((v, i) => v + b[i]));
  const step = (W - m.l - m.r) / n;
  const bw = Math.max(1, step - 1.5);
  const y = scaleLinear(0, max, H - m.b, m.t);
  const hi = state.hover[id];
  let bars = "";
  for (let i = 0; i < n; i++) {
    const x0 = m.l + i * step;
    const ya = y(a[i]), yb = y(a[i] + b[i]);
    bars += `<rect x="${x0}" y="${ya}" width="${bw}" height="${H - m.b - ya}" rx="1" fill="var(--series-1)" opacity="${hi == null || hi === i ? 1 : .55}"/>`;
    if (b[i]) bars += `<rect x="${x0}" y="${yb}" width="${bw}" height="${Math.max(1, ya - yb - 1)}" rx="1" fill="var(--series-2)"/>`;
  }
  setHTML(el, `<svg viewBox="0 0 ${W} ${H}">
    <g class="grid"><line x1="${m.l}" x2="${W - m.r}" y1="${y(0)}" y2="${y(0)}"/><line x1="${m.l}" x2="${W - m.r}" y1="${y(max / 2)}" y2="${y(max / 2)}"/></g>
    <g class="axis"><text x="${m.l - 6}" y="${y(max / 2) + 3}" text-anchor="end">${fmt(max / 2)}</text>
      <text x="${m.l}" y="${H - 4}">run ${xs[0]}</text><text x="${W - m.r}" y="${H - 4}" text-anchor="end">run ${xs.at(-1)}</text></g>
    ${bars}</svg>`);
  bindHover(el, id, n, m.l, W - m.r, (i) => `Run <b>#${xs[i]}</b><br>Accepted <b>${fmt(a[i])}</b><br>Quarantined <b>${fmt(b[i])}</b>`);
}

function bindHover(el, id, n, x0, x1, html) {
  if (el.dataset.bound) { el._hover = { n, x0, x1, html }; return; }
  el.dataset.bound = "1";
  el._hover = { n, x0, x1, html };
  el.addEventListener("mousemove", (e) => {
    const { n, x0, x1, html } = el._hover;
    const r = el.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * (el.clientWidth || r.width);
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - x0) / (x1 - x0)) * (n - 1))));
    if (state.hover[id] !== i) { state.hover[id] = i; render(); }
    showTip(html(i), e.clientX, e.clientY);
  });
  el.addEventListener("mouseleave", () => { state.hover[id] = null; hideTip(); render(); });
}

function showTip(html, x, y) {
  const tip = $("#tooltip");
  tip.innerHTML = html;
  tip.hidden = false;
  const w = tip.offsetWidth;
  tip.style.left = Math.min(window.innerWidth - w - 8, x + 14) + "px";
  tip.style.top = y + 14 + "px";
}
const hideTip = () => ($("#tooltip").hidden = true);
document.addEventListener("mouseover", (e) => {
  const t = e.target.closest("[data-tip]");
  if (t) { const r = t.getBoundingClientRect(); showTip(esc(t.dataset.tip), r.left, r.bottom - 6); }
});
document.addEventListener("mouseout", (e) => { if (e.target.closest("[data-tip]")) hideTip(); });

// ------------------------------------------------------------- lineage
const LAYERS = ["source", "bronze", "silver", "gold", "consumer"];
let lineageHover = null;

function renderLineage(s) {
  const el = $("#lineage");
  const W = 1240, colW = W / LAYERS.length, nodeW = 196, nodeH = 48, gap = 18, top = 34;
  const byLayer = LAYERS.map((l) => s.datasets.filter((d) => d.layer === l));
  const H = top + Math.max(...byLayer.map((c) => c.length)) * (nodeH + gap) + 10;
  const pos = {};
  byLayer.forEach((col, ci) => {
    const colH = col.length * (nodeH + gap) - gap;
    const y0 = top + (H - top - colH) / 2;
    col.forEach((d, i) => (pos[d.id] = { x: ci * colW + (colW - nodeW) / 2, y: y0 + i * (nodeH + gap) }));
  });
  const ds = Object.fromEntries(s.datasets.map((d) => [d.id, d]));
  const roots = new Set(s.incidents.filter((i) => i.status !== "resolved").map((i) => i.dataset));
  const related = lineageHover ? relatedSet(lineageHover, s.edges) : null;

  const edges = s.edges.map(([a, b]) => {
    const p = pos[a], q = pos[b];
    const x1 = p.x + nodeW, y1 = p.y + nodeH / 2, x2 = q.x, y2 = q.y + nodeH / 2, mx = (x1 + x2) / 2;
    const src = ds[a], dst = ds[b];
    let cls = "";
    if (dst.blocked) cls = "blocked";
    else if (src.status === "critical") cls = "critical";
    else if (src.status === "warning") cls = "warning";
    const dim = related && !(related.has(a) && related.has(b)) ? "dim" : "";
    return `<path class="edge ${cls} ${dim}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/>`;
  }).join("");

  const nodes = s.datasets.map((d) => {
    const p = pos[d.id];
    const dim = related && !related.has(d.id) ? "dim" : "";
    const color = { healthy: "var(--good)", warning: "var(--warning)", critical: "var(--critical)", blocked: "var(--serious)", degraded: "var(--serious)", fault: "var(--critical)" }[d.status];
    const label = d.layer === "source" || d.layer === "consumer" ? d.name : d.id;
    const sub = d.layer === "source" ? d.owner : d.layer === "consumer" ? `${d.criticality} · ${d.owner}` :
      `score ${d.score.toFixed(0)} · ${d.freshness_minutes == null ? "—" : d.freshness_minutes.toFixed(0) + "m"}${d.blocked ? " · held" : ""}`;
    return `<g class="node ${d.status} ${roots.has(d.id) ? "root" : ""} ${dim}" data-ds="${esc(d.id)}" transform="translate(${p.x},${p.y})">
      <rect class="halo" x="-4" y="-4" width="${nodeW + 8}" height="${nodeH + 8}" rx="12" fill="none"/>
      <rect class="box" width="${nodeW}" height="${nodeH}" rx="9"/>
      <circle cx="14" cy="17" r="4" fill="${color}"/>
      <text x="25" y="21">${esc(label.length > 27 ? label.slice(0, 26) + "…" : label)}</text>
      <text class="sub" x="25" y="37">${esc(sub)}</text>
    </g>`;
  }).join("");

  setHTML(el, `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Lineage graph">
    ${LAYERS.map((l, i) => `<text class="col-label" x="${i * colW + colW / 2}" y="14" text-anchor="middle">${l}</text>`).join("")}
    ${edges}${nodes}</svg>`);
}

function relatedSet(id, edges) {
  const set = new Set([id]);
  const walk = (node, dir) => edges.forEach(([a, b]) => {
    const next = dir === "down" ? (a === node ? b : null) : (b === node ? a : null);
    if (next && !set.has(next)) { set.add(next); walk(next, dir); }
  });
  walk(id, "down"); walk(id, "up");
  return set;
}

$("#lineage").addEventListener("mouseover", (e) => {
  const n = e.target.closest(".node");
  const id = n?.dataset.ds ?? null;
  if (id !== lineageHover) { lineageHover = id; render(); }
});
$("#lineage").addEventListener("mouseleave", () => { lineageHover = null; render(); });

// ---------------------------------------------------------- failure lab
const STEPS = [
  ["injected", "Fault injected", "A realistic failure enters the pipeline on the next run."],
  ["detected", "ATLAS detects it", "Deterministic checks fail on the affected dataset."],
  ["gate", "Quality gate contains it", "Gold models downstream are held back; consumers keep the last good version."],
  ["incident", "Incident opened", "Root cause classified, owner and severity assigned."],
  ["blast_radius", "Blast radius computed", "Every downstream asset and its criticality, from lineage."],
  ["proposed", "Runbook proposed", "Remediation steps attached to the incident for the owner."],
  ["remediation", "Remediation applied", "By an engineer (Incidents tab) or on-call automation."],
  ["recovered", "Recovered", "Checks green for 2 consecutive runs; backfill completed."],
];

function renderLab(s) {
  const canWrite = state.role !== "viewer";
  setHTML($("#faults"), s.faults.map((f) => `
    <div class="fault ${f.active ? "active" : ""}">
      <div class="row"><h4>${esc(f.name)}</h4>${f.active ? st("critical") : ""}</div>
      <p>${esc(f.description)}</p>
      <div class="target">→ ${esc(f.target)}</div>
      <div class="row">
        <span class="small muted">expects: ${esc(f.root_cause.replace(/_/g, " "))}</span>
        ${f.active
          ? `<button class="btn small" data-clear="${f.id}" ${canWrite ? "" : "disabled"}>Clear</button>`
          : `<button class="btn small primary" data-inject="${f.id}" ${canWrite ? "" : "disabled title='Viewer role is read-only'"}>Inject</button>`}
      </div>
    </div>`).join(""));

  const tr = s.traces[0];
  $("#trace-name").textContent = tr ? tr.name : "";
  if (!tr) {
    setHTML($("#stepper"), `<li class="empty">Inject a fault to see ATLAS respond step by step.</li>`);
    return;
  }
  const injectedTick = tr.steps.injected?.[1];
  let waiting = false;
  setHTML($("#stepper"), STEPS.map(([key, title, desc]) => {
    const step = tr.steps[key];
    const note = tr.notes?.[key];
    let cls = "";
    if (step && note && key === "gate") cls = "skip";
    else if (step) cls = "done";
    else if (!waiting) { cls = "wait"; waiting = true; }
    const lat = step && key !== "injected" ? ` · +${(step[1] - injectedTick + 1) * 5}m` : "";
    return `<li class="${cls}"><span class="dot">${cls === "done" ? "✓" : cls === "skip" ? "–" : ""}</span>
      ${step ? `<span class="when">${step[0]}${lat}</span>` : ""}
      <div class="t">${title}</div><div class="s">${esc(note && key === "gate" ? note : desc)}</div></li>`;
  }).join(""));
}

$("#faults").addEventListener("click", async (e) => {
  const inject = e.target.closest("[data-inject]")?.dataset.inject;
  const clear = e.target.closest("[data-clear]")?.dataset.clear;
  try {
    if (inject) {
      await api(`/api/faults/${inject}`, { method: "POST", body: JSON.stringify({ auto_heal_ticks: $("#autoheal").checked ? 8 : null }) });
      toast("Fault injected — it lands on the next pipeline run");
    } else if (clear) {
      await api(`/api/faults/${clear}`, { method: "DELETE" });
    }
  } catch (err) { toast(err.message); }
});

// ----------------------------------------------------------- incidents
function renderIncidents(s, force) {
  const list = s.incidents;
  if (!state.selectedIncident && list.length) state.selectedIncident = list[0].id;
  setHTML($("#inc-list"), list.length ? list.map((i) => `
    <li class="${i.id === state.selectedIncident ? "sel" : ""}" data-inc="${i.id}">
      <div class="top"><span class="pill ${i.severity.toLowerCase()}">${i.severity}</span>
        ${st(i.status === "resolved" ? "healthy" : i.status === "mitigating" ? "warning" : "critical")}
        <span class="small muted">${esc(i.status)}</span></div>
      <div class="title">${esc(i.category_label)}</div>
      <div class="sub">${esc(i.id)} · ${esc(i.dataset)} · ${esc(i.opened_at)}</div>
    </li>`).join("") : `<li class="empty">No incidents yet. Break something in the Failure Lab.</li>`);

  if (state.selectedIncident && (force || !state.incidentDetail || state.incidentDetail.id !== state.selectedIncident
      || state.incidentDetail.status !== "resolved")) {
    loadIncident(state.selectedIncident);
  }
}

async function loadIncident(id) {
  if (state.lastFetch.incident === `${id}:${state.snap.tick}` && state.incidentDetail?.id === id) return;
  state.lastFetch.incident = `${id}:${state.snap.tick}`;
  try {
    state.incidentDetail = await api(`/api/incidents/${id}`);
    renderIncidentDetail();
  } catch (err) { toast(err.message); }
}

function renderIncidentDetail() {
  const i = state.incidentDetail;
  if (!i) return;
  const cp = state.copilot[i.id];
  const canWrite = state.role !== "viewer";
  const root = $("#inc-detail");
  if (!root.querySelector("#inc-head")) root.innerHTML = `<div id="inc-head"></div><div id="inc-body"></div>`;
  setHTML($("#inc-head"), `
    <div class="inc-head">
      <div>
        <span class="pill ${i.severity.toLowerCase()}">${i.severity}</span> <span class="small muted">${esc(i.id)} · owner <b>${esc(i.owner)}</b></span>
        <h2>${esc(i.title)}</h2>
        <p class="muted small" style="margin:0">Opened ${esc(i.opened_at)} · status <b>${esc(i.status)}</b>${i.fault_ids.length ? ` · linked fault: <code>${esc(i.fault_ids.join(", "))}</code>` : ""}</p>
      </div>
      <div class="actions">
        <button class="btn" id="btn-copilot">${cp ? "↻ Re-run copilot" : "✦ Ask copilot"}</button>
        <button class="btn primary" id="btn-remediate" ${i.status === "resolved" || !canWrite ? "disabled" : ""}>Apply remediation</button>
      </div>
    </div>`);
  setHTML($("#inc-body"), `
    <div class="kv">
      <div><span>Root cause</span><b>${esc(i.category_label)}</b></div>
      <div><span>Time to detect</span><b>${i.mttd_minutes == null ? "organic" : i.mttd_minutes + " min"}</b></div>
      <div><span>Time to recover</span><b>${i.mttr_minutes == null ? "ongoing" : i.mttr_minutes + " min"}</b></div>
      <div><span>Gold held back</span><b>${i.blocked_models.length}</b></div>
    </div>
    <h4>Failing checks</h4>
    <table class="table"><tbody>${i.checks.map((c) => `<tr><td>${st(c.status === "fail" ? "critical" : "warning")}</td>
      <td class="ds">${esc(c.dataset)}.${esc(c.name)}</td><td class="small">${esc(c.message)}</td>
      <td>${c.blocking ? '<span class="pill">blocking</span>' : ""}</td></tr>`).join("")}</tbody></table>
    ${i.symptoms.length ? `<h4>Downstream symptoms (grouped, not paged)</h4><p class="chips">${i.symptoms.map((x) => `<span>${esc(x)}</span>`).join("")}</p>` : ""}
    <h4>Blast radius · ${i.blast_radius.length} assets</h4>
    <p class="chips">${i.blast_radius.map((d) => `<span class="${d.criticality === "tier1" ? "t1" : ""}" data-tip="${esc(d.criticality)} · ${esc(d.owner)}">${esc(d.id)}</span>`).join("")}</p>
    <div class="grid-2" style="margin-top:6px">
      <div><h4>Runbook · ${esc(i.runbook.title)}</h4><ol class="steps">${i.runbook.steps.map((x) => `<li>${esc(x)}</li>`).join("")}</ol></div>
      <div><h4>Timeline</h4><ul class="timeline">${i.timeline.map((t) => `<li><time>${esc(t.time)}</time><span>${esc(t.message)}</span></li>`).join("")}</ul></div>
    </div>
    ${cp ? renderCopilot(cp) : ""}`);
}

function renderCopilot(c) {
  if (c.loading) return `<div class="copilot"><span class="mode">copilot · analysing evidence…</span></div>`;
  return `<div class="copilot">
    <div class="mode">copilot · ${c.mode === "claude" ? `Claude (${esc(c.model)})` : "rules engine"}${c.fallback_reason ? " · LLM unavailable, used rules" : ""} · checks decide, AI explains</div>
    <p style="margin:6px 0"><b>${esc(c.probable_root_cause)}</b></p>
    <p class="small" style="margin:4px 0">${esc(c.summary)}</p>
    <h4>Evidence</h4><ul>${c.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
    <h4>Stakeholder update</h4><blockquote>${esc(c.stakeholder_update)}</blockquote>
  </div>`;
}

$("#inc-list").addEventListener("click", (e) => {
  const li = e.target.closest("[data-inc]");
  if (!li) return;
  state.selectedIncident = li.dataset.inc;
  state.incidentDetail = null;
  render(true);
});

$("#inc-detail").addEventListener("click", async (e) => {
  const i = state.incidentDetail;
  if (!i) return;
  if (e.target.id === "btn-copilot") await runCopilot(i.id, !!state.copilot[i.id]);
  if (e.target.id === "btn-remediate") {
    try {
      state.incidentDetail = await api(`/api/incidents/${i.id}/remediate`, { method: "POST" });
      renderIncidentDetail();
      toast("Remediation applied — waiting for 2 green runs");
    } catch (err) { toast(err.message); }
  }
});

async function runCopilot(id, refresh = false) {
  state.copilot[id] = { loading: true };
  renderIncidentDetail();
  try {
    state.copilot[id] = await api(`/api/incidents/${id}/copilot${refresh ? "?refresh=true" : ""}`, { method: "POST" });
  } catch (err) { delete state.copilot[id]; toast(err.message); }
  renderIncidentDetail();
}

// ----------------------------------------------------------- contracts
async function renderContracts(s, force) {
  if (!force && state.lastFetch.contracts > s.tick - 10) return;
  state.lastFetch.contracts = s.tick;
  const contracts = await api("/api/contracts");
  setHTML($("#contracts"), contracts.map((c) => `
    <div class="card">
      <div class="card-head"><h3 class="ds">${esc(c.dataset)}</h3><span class="pill">v${esc(c.version)}</span></div>
      <p class="muted small" style="margin:0 0 8px">${esc(c.description)} Owner <b>${esc(c.owner)}</b> · PK <code>${esc(c.primary_key.join(", "))}</code> · freshness ≤ ${c.freshness_sla_minutes} min · <b>${fmt(c.quarantined_rows)}</b> rows quarantined (24h)</p>
      <table class="table"><thead><tr><th>Column</th><th>Type</th><th>Rules</th><th>Class</th></tr></thead><tbody>
      ${c.columns.map((col) => `<tr style="cursor:default"><td class="ds">${esc(col.name)}${col.required ? " *" : ""}</td><td class="small">${esc(col.type)}</td>
        <td class="small muted">${[col.enum ? `in [${col.enum.join(", ")}]` : "", col.min != null ? `≥ ${col.min}` : "", col.max != null ? `≤ ${fmt(col.max)}` : "", col.pattern ? `~ ${col.pattern}` : ""].filter(Boolean).map(esc).join(" · ")}</td>
        <td><span class="cls ${esc(col.classification)}">${esc(col.classification)}</span></td></tr>`).join("")}
      </tbody></table>
    </div>`).join(""));
}

// ---------------------------------------------------------------- risk
async function renderRisk(s, force) {
  if (!force && state.lastFetch.risk > s.tick - 2) return;
  state.lastFetch.risk = s.tick;
  const data = await api("/api/risk/signals?limit=12");
  setHTML($("#risk-meta"), `${data.masked ? `PII masked for role <b>${esc(data.role)}</b>` : "admin: unmasked"}${data.stale ? ` · ${st("warning")} features stale or at risk` : ""}`);
  setHTML($("#risk-table"), `
    <thead><tr><th>Account</th><th>Holder</th><th>Segment</th><th class="num">Txns 1h</th><th class="num">Amount 1h (COP)</th><th class="num">Max</th><th class="num">CNP</th><th>Risk score</th></tr></thead>
    <tbody>${data.rows.map((r) => `<tr style="cursor:default">
      <td class="ds">${esc(r.account_id)}</td><td>${esc(r.holder_name ?? "—")}</td>
      <td><span class="pill">${esc(r.segment ?? "?")}</span> <span class="small muted">${esc(r.risk_tier ?? "")}</span></td>
      <td class="num">${r.txn_count_1h}</td><td class="num">${fmt(r.amount_sum_1h)}</td><td class="num">${fmt(r.max_amount_1h)}</td>
      <td class="num">${Math.round(r.cnp_ratio * 100)}%</td>
      <td><span class="bar"><i class="${r.risk_score >= 70 ? "fail" : r.risk_score >= 50 ? "warn" : ""}" style="width:${r.risk_score}%"></i></span> <b class="num">${r.risk_score}</b></td>
    </tr>`).join("")}</tbody>`);
}

// -------------------------------------------------------------- drawer
async function openDrawer(id) {
  const d = await api(`/api/datasets/${encodeURIComponent(id)}`);
  const byCheck = {};
  d.history.forEach((h) => (byCheck[h.check_name] ??= {})[h.tick] = h.status);
  const ticks = [...new Set(d.history.map((h) => h.tick))].sort((a, b) => a - b);
  setHTML($("#drawer-body"), `
    <span class="pill layer">${esc(d.layer)}</span> <span class="pill">${esc(d.criticality)}</span> ${st(d.status)}
    <h2 style="margin-top:8px">${esc(d.id)}</h2>
    <p class="muted">${esc(d.description)}</p>
    <div class="kv">
      <div><span>Owner</span><b>${esc(d.owner)}</b></div>
      <div><span>Score</span><b>${d.score}</b></div>
      <div><span>Freshness</span><b>${d.freshness_minutes ?? "—"} / ${d.sla_minutes} min</b></div>
      <div><span>SLO 24h</span><b>${d.slo == null ? "—" : d.slo + "%"}</b></div>
    </div>
    <h4>Checks (current run)</h4>
    <table class="table"><tbody>${d.checks.map((c) => `<tr style="cursor:default"><td>${st(c.status === "pass" ? "healthy" : c.status === "warn" ? "warning" : "critical")}</td>
      <td class="ds">${esc(c.name)}</td><td class="small muted">${esc(c.message)}<br>threshold ${esc(c.threshold)}</td></tr>`).join("") || `<tr><td class="empty">No checks on this layer.</td></tr>`}</tbody></table>
    ${ticks.length ? `<h4>Check history · last ${ticks.length} runs</h4><div class="heat">${Object.entries(byCheck).map(([name, m]) =>
      `<div class="row"><span>${esc(name)}</span><span class="cells">${ticks.map((t) => `<i class="${m[t] || ""}" data-tip="run ${t}: ${m[t] || "n/a"}"></i>`).join("")}</span></div>`).join("")}</div>` : ""}
    <h4>Upstream</h4><p class="chips">${d.upstream.map((u) => `<span>${esc(u)}</span>`).join("") || '<span>none (source)</span>'}</p>
    <h4>Downstream · blast radius</h4><p class="chips">${d.downstream.map((u) => `<span>${esc(u)}</span>`).join("") || "<span>none</span>"}</p>
    ${d.model_sql ? `<h4>Model SQL</h4><pre>${esc(d.model_sql)}</pre>` : ""}`);
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
}
const closeDrawer = () => { $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); };
document.addEventListener("click", (e) => {
  const row = e.target.closest("[data-ds]");
  if (row && (row.closest("#datasets-table") || row.closest("#lineage"))) openDrawer(row.dataset.ds).catch((err) => toast(err.message));
});
$("#drawer-close").addEventListener("click", closeDrawer);

// ------------------------------------------------------------ controls
async function control(body) {
  try { await api("/api/control", { method: "POST", body: JSON.stringify(body) }); } catch (err) { toast(err.message); }
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
$("#btn-play").addEventListener("click", () => control({ running: !state.snap?.running }));
$("#speed").addEventListener("change", (e) => control({ speed: Number(e.target.value) }));
$("#chaos").addEventListener("change", (e) => { control({ chaos: e.target.checked }); if (e.target.checked) toast("Chaos mode: random faults will appear and auto-heal"); });
$("#role").value = state.role;
$("#role").addEventListener("change", (e) => {
  state.role = e.target.value;
  storage.set("atlas.role", state.role);
  state.lastFetch = {};
  toast(`Role: ${state.role}${state.role === "viewer" ? " (read-only, PII masked)" : state.role === "admin" ? " (PII visible)" : ""}`);
  render(true);
});

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
applyTheme(storage.get("atlas.theme", null));
$("#btn-theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  applyTheme(next);
  storage.set("atlas.theme", next);
  render(true);
});

$("#btn-about").addEventListener("click", () => $("#about").showModal());
$("#btn-demo").addEventListener("click", () => runTour());

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea") || e.metaKey || e.ctrlKey) return;
  const views = ["overview", "lineage", "lab", "incidents", "contracts", "risk"];
  if (e.key >= "1" && e.key <= "6") setView(views[Number(e.key) - 1]);
  else if (e.key === " ") { e.preventDefault(); $("#btn-play").click(); }
  else if (e.key === "t") $("#btn-theme").click();
  else if (e.key === "d") runTour();
  else if (e.key === "Escape") { closeDrawer(); stopTour(); }
});
window.addEventListener("resize", () => render());

// ------------------------------------------------------------- guided tour
let tourToken = 0;
function stopTour() {
  tourToken++;
  $("#tour").hidden = true;
  $$(".spotlight").forEach((el) => el.classList.remove("spotlight"));
}
$("#tour-skip").addEventListener("click", stopTour);

function waitFor(pred, timeout = 30000) {
  return new Promise((resolve) => {
    const started = Date.now();
    const check = () => {
      if (state.snap && pred(state.snap)) { tickHooks.delete(check); resolve(true); }
      else if (Date.now() - started > timeout) { tickHooks.delete(check); resolve(false); }
    };
    tickHooks.add(check);
    check();
  });
}

async function runTour() {
  stopTour();
  const token = tourToken;
  const alive = () => token === tourToken;
  if (state.role === "viewer") { state.role = "engineer"; $("#role").value = "engineer"; }
  const steps = 10;
  let n = 0;
  const say = async (text, view, ms, spot) => {
    if (!alive()) throw new Error("stopped");
    n++;
    if (view) setView(view);
    $$(".spotlight").forEach((el) => el.classList.remove("spotlight"));
    if (spot) $(spot)?.classList.add("spotlight");
    $("#tour").hidden = false;
    setHTML($("#tour-text"), text);
    $("#tour-bar").style.width = `${(n / steps) * 100}%`;
    if (ms) await sleep(ms);
  };
  try {
    for (const f of state.snap.faults.filter((f) => f.active)) await api(`/api/faults/${f.id}`, { method: "DELETE" });
    await control({ running: true, speed: 2, chaos: false });
    await say("This is a <b>live</b> financial data platform. Every few seconds a new 5-minute batch of payments flows <b>source → bronze → silver → gold</b>.", "overview", 6500);
    await say("The <b>reliability score</b> blends freshness, volume, schema, integrity and more — weighted by business criticality.", null, 6000, "#score-card");
    await say("<b>Lineage</b>: 17 assets from core banking to the regulatory report. Hover any node to trace it.", "lineage", 6500);
    await say("Now let's break it. The payments gateway ships a <b>breaking schema change</b> without telling anyone…", "lab", 4500);
    await api("/api/faults/schema_drift", { method: "POST", body: "{}" });
    await waitFor((s) => s.traces[0]?.fault === "schema_drift" && s.traces[0].steps.detected, 20000);
    await control({ running: false });  // freeze simulated time while we narrate, so MTTR stays honest
    await say("<b>Detected in the same run.</b> The data contract check failed, records were quarantined and the <b>quality gate</b> held back every gold product.", null, 7000);
    await say("Red dashed edges = blocked by the gate. Consumers keep the <b>last good version</b> instead of wrong numbers.", "lineage", 7000);
    const inc = state.snap.incidents.find((i) => i.status !== "resolved" && i.fault_ids.includes("schema_drift"));
    if (inc) { state.selectedIncident = inc.id; state.incidentDetail = null; }
    await say("Simulation paused while we look. An <b>incident</b> was opened with root cause, severity, owner, blast radius and runbook — no human triage needed.", "incidents", 6500);
    if (inc) await runCopilot(inc.id);
    await say("The <b>copilot</b> turns the evidence into a briefing and a stakeholder update. <b>Checks decide, AI explains.</b>", null, 8000, "#inc-detail");
    if (inc) {
      await api(`/api/incidents/${inc.id}/remediate`, { method: "POST" });
      await control({ running: true });
      await say("Applying the runbook remediation… ATLAS waits for <b>2 consecutive green runs</b> before closing.", null, 0);
      await waitFor((s) => s.incidents.find((i) => i.id === inc.id)?.status === "resolved", 30000);
      render(true);
      await sleep(2500);
    }
    await control({ running: true });
    await say("<b>Recovered.</b> Detect → contain → explain → recover, with MTTD/MTTR tracked. Explore the Failure Lab to try the other 9 failure modes.", "overview", 8000);
    await control({ speed: 1 });
    stopTour();
  } catch {
    if (alive()) stopTour();
  } finally {
    if (state.snap && (!state.snap.running || state.snap.speed !== 1)) control({ running: true, speed: 1 });
  }
}

// ----------------------------------------------------------------- boot
api("/api/meta").then((m) => {
  state.meta = m;
  $("#gh-link").href = m.github_url;
}).catch(() => {});
const savedView = storage.get("atlas.view", "overview");
if (savedView && $(`.view[data-view="${savedView}"]`)) setView(savedView);
connect();
