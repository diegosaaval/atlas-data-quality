// ATLAS · interfaz en vivo. Sin frameworks ni build: llega un snapshot por WebSocket y se re-dibuja.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const nf = (n, d = 0) => n == null ? "—" : Number(n).toLocaleString("es-CO", { maximumFractionDigits: d });
const big = (n) => n == null ? "—" : Math.abs(n) >= 1e12 ? nf(n / 1e12, 1) + " B" : Math.abs(n) >= 1e9 ? nf(n / 1e9, 1) + " mil M" : Math.abs(n) >= 1e6 ? nf(n / 1e6, 1) + " M" : nf(n, 2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* modo privado */ } },
};
// Solo toca el DOM si el HTML cambió: así los botones no se reemplazan entre ticks.
function setHTML(el, html) { if (el && el._html !== html) { el._html = html; el.innerHTML = html; } }

const state = {
  snap: null, view: "resumen", table: store.get("atlas.table", "cartera_creditos"),
  detail: null, detailKey: "", expanded: new Set(),
  incFilter: "activos", incSel: null, incDetail: null, incKey: "", emails: {},
  rules: null, hover: {},
};

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
  return body;
}
function toast(msg, ms = 2800) {
  const el = $("#toast"); el.textContent = msg; el.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (el.hidden = true), ms);
}

// ------------------------------------------------------------------ tiempo real
let ws, retry = 0, poll;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { retry = 0; clearInterval(poll); };
  ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.type === "snapshot") onSnapshot(m.data); };
  ws.onclose = () => {
    $("#clock").className = "clock"; $("#clock span").textContent = "reconectando…";
    clearInterval(poll); poll = setInterval(() => api("/api/state").then(onSnapshot).catch(() => {}), 2000);
    setTimeout(connect, Math.min(10000, 500 * 2 ** retry++));
  };
}
setInterval(() => ws?.readyState === 1 && ws.send("ping"), 20000);
const tickHooks = new Set();
function onSnapshot(s) {
  const prev = state.snap; state.snap = s;
  renderHeader(s); render();
  if (!prev || prev.time !== s.time || prev.date !== s.date) tickHooks.forEach((f) => f(s));
}

const ICON_PAUSE = `<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><rect x="3" y="2" width="3" height="10" rx="1" fill="currentColor"/><rect x="8" y="2" width="3" height="10" rx="1" fill="currentColor"/></svg>`;
const ICON_PLAY = `<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M4 2.2v9.6a.8.8 0 0 0 1.2.7l7.6-4.8a.8.8 0 0 0 0-1.4L5.2 1.5A.8.8 0 0 0 4 2.2z" fill="currentColor"/></svg>`;
function renderHeader(s) {
  const real = s.mode === "conector";
  // Con una fuente real no hay simulación: se ocultan sus controles.
  ["#btn-play", "#speed", "#btn-anomaly", "#btn-demo"].forEach((id) => ($(id).hidden = real));
  $("#btn-source").hidden = !state.meta || state.meta.public_demo;
  $("#source-label").textContent = real ? `Fuente: ${s.source.name.toUpperCase()}` : "Fuente: Demo";
  if (real) {
    $("#clock").className = "clock " + (s.source.status === "ok" ? "on" : "paused");
    const pub = s.source.published_at ? new Date(s.source.published_at) : null;
    $("#clock span").textContent = pub ? `${s.source.name.toUpperCase()} · ${pub.toLocaleDateString("es-CO", { day: "2-digit", month: "short" })} ${pub.toTimeString().slice(0, 5)}` : `${s.source.name.toUpperCase()} · esperando`;
    const b = $("#inc-badge"); b.hidden = !s.kpis.open_incidents; b.textContent = s.kpis.open_incidents;
    return;
  }
  $("#clock").className = "clock " + (s.running ? "on" : "paused");
  $("#clock span").textContent = `${s.weekday.slice(0, 3)} ${s.date.slice(5)} · ${s.time}`;
  setHTML($("#btn-play"), s.running ? ICON_PAUSE : ICON_PLAY);
  $("#speed").value = String(s.speed);
  const b = $("#inc-badge"); b.hidden = !s.kpis.open_incidents; b.textContent = s.kpis.open_incidents;
}

function setView(v) {
  if (state.view !== v) scrollTo({ top: 0 });
  state.view = v;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach((x) => x.classList.toggle("active", x.dataset.view === v));
  store.set("atlas.view", v);
  if (location.hash !== "#" + v) history.replaceState(null, "", "#" + v);
  render(true);
}
function render(force = false) {
  if (!state.snap) return;
  const fn = { resumen: renderResumen, tablas: renderTablas, incidentes: renderIncidentes, reglas: renderReglas }[state.view];
  fn(state.snap, force)?.catch?.((e) => toast(e.message));
}

const ST = { ok: "OK", advertencia: "Advertencia", falla: "Con fallas", no_disponible: "No disponible", retrasada: "Retrasada", esperando: "Esperando", error: "Error" };
const CHECK_ST = { ok: "OK", advertencia: "Advertencia", falla: "Falla", error: "Error" };
const cst = (s) => `<span class="st ${esc(s)}">${esc(CHECK_ST[s] ?? s)}</span>`;
const st = (s, label) => `<span class="st ${esc(s)}">${esc(label ?? ST[s] ?? s)}</span>`;
const sev = (s, l) => `<span class="pill ${esc(s)}">${esc(l)}</span>`;
const scoreColor = (v) => v == null ? "var(--text-3)" : v >= 95 ? "var(--good)" : v >= 80 ? "var(--warning)" : "var(--critical)";
const toMin = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; };

// ------------------------------------------------------------------- RESUMEN
function renderResumen(s) {
  const d = new Date(s.date + "T12:00:00");
  const long = d.toLocaleDateString("es-CO", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  const k = s.kpis;
  const pending = s.tables.filter((t) => ["esperando", "retrasada"].includes(t.status)).length;
  if (s.source) {
    const src = s.source;
    const pub = src.published_at ? new Date(src.published_at) : null;
    $("#resumen-sub").textContent = `Validando las tablas que publica ${src.title}.`;
    setHTML($("#today"), `
      <div>
        <div class="date">Última fecha validada</div>
        <div class="time">${src.last_date ? esc(new Date(src.last_date + "T12:00:00").toLocaleDateString("es-CO", { day: "numeric", month: "short" })) : "—"}</div>
        <div class="sub">${src.dates} fechas · ${k.tables_total} tablas · ${pub ? `publicado ${esc(pub.toLocaleString("es-CO", { dateStyle: "medium", timeStyle: "short" }))}` : "esperando publicación"}</div>
      </div>
      <div class="source">
        <div class="actions"><span class="pill monitor">Fuente real</span><b>${esc(src.title)}</b>${src.run_id ? `<span class="mono small muted">${esc(src.run_id)}</span>` : ""}</div>
        <p class="muted small" style="margin:8px 0">${esc(src.description)}</p>
        <p class="chips">${Object.entries(src.datasets || {}).map(([n, c]) => `<span>${esc(n)} · ${nf(c)} filas</span>`).join("") || s.tables.map((t) => `<span>${esc(t.name)}</span>`).join("")}</p>
        <p class="small muted" style="margin:10px 0 0">Publicación esperada antes de las ${esc(src.expected_at)} (+60 min de gracia) · <span class="mono">${esc(src.path)}</span></p>
      </div>`);
  } else {
  setHTML($("#today"), `
    <div>
      <div class="date">${esc(long)}</div>
      <div class="time">${esc(s.time)}</div>
      <div class="sub">${k.tables_arrived} de ${k.tables_total} tablas recibidas${pending ? ` · ${pending} pendientes` : ""}</div>
    </div>
    <div>${timeline(s)}</div>`);
  }

  const kpi = (label, value, hint, alert) => `<div class="kpi ${alert ? "alert" : ""}"><div class="label">${label}</div><div class="value">${value}</div><div class="hint">${hint}</div></div>`;
  setHTML($("#kpis"), [
    kpi("Puntaje de calidad hoy", k.score == null ? "—" : nf(k.score, 1), "tablas evaluadas hoy", k.score != null && k.score < 80),
    kpi("Tablas recibidas", `${k.tables_arrived}/${k.tables_total}`, k.tables_missing ? `${k.tables_missing} no disponibles` : "a tiempo o en espera", k.tables_missing > 0),
    kpi("Incidentes abiertos", k.open_incidents, "agrupados por tabla", k.open_incidents > 0),
    kpi("Controles fallidos hoy", `${k.checks_failed}/${k.checks_today}`, "monitores + reglas", k.checks_failed > 0),
    kpi("Registros validados hoy", nf(k.rows_today), "en todas las tablas"),
    kpi("Tiempo de resolución", k.mttr_hours == null ? "—" : `${nf(k.mttr_hours, 1)} h`, "promedio, desde la detección"),
  ].join(""));

  setHTML($("#table-cards"), s.tables.map((t) => {
    const arrival = t.arrived_at ? `llegó ${t.arrived_at}` : t.status === "no_disponible" ? "no llegó" : "pendiente";
    const rows = t.rows == null ? "" : `<span><b>${nf(t.rows)}</b> filas${t.expected_rows ? ` · normal ~${nf(t.expected_rows)}` : ""}</span>`;
    return `<div class="tcard ${esc(t.status)}" data-table="${esc(t.name)}">
      <div class="row"><span class="title">${esc(t.title)}</span>${st(t.status, t.status_label)}</div>
      <div class="name">${esc(t.name)} · ${esc(t.owner)}</div>
      <div class="meta"><span>esperada ${esc(t.expected_at)} · ${arrival}</span>${rows}</div>
      <div class="row">
        <span class="score" style="color:${scoreColor(t.score)}">${t.score == null ? "—" : nf(t.score, 0)}<span class="small muted"> /100</span></span>
        ${spark(t.sparkline)}
      </div>
      ${t.failing.length ? `<ul class="fails">${t.failing.slice(0, 3).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>`
        : t.score != null ? `<span class="small muted">${t.checks_ok}/${t.checks_total} controles OK</span>` : `<span class="small muted">Se validará al llegar</span>`}
    </div>`;
  }).join(""));

  lineChart($("#chart-global"), "global", s.score_series.map((p) => ({ x: p.fecha, v: p.score })), { min: 50, max: 100, unit: "" });

  setHTML($("#events"), s.events.length
    ? s.events.map((e) => `<li><time>${esc(e.time)}</time><span class="lvl ${esc(e.level)}"></span><span>${esc(e.message)}</span></li>`).join("")
    : `<li class="empty">Esperando las primeras cargas del día…</li>`);
}

function timeline(s) {
  const W = 900, H = 96, x0 = 20, x1 = W - 20, start = 330, end = 630;
  const x = (m) => x0 + ((m - start) / (end - start)) * (x1 - x0);
  const now = toMin(s.time);
  let out = `<svg class="timeline-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Llegada de tablas">`;
  for (let m = 360; m <= 600; m += 60) out += `<g class="tick"><line x1="${x(m)}" x2="${x(m)}" y1="44" y2="52"/><text x="${x(m)}" y="66" text-anchor="middle">${String(m / 60).padStart(2, "0")}:00</text></g>`;
  out += `<line class="axis" x1="${x0}" x2="${x1}" y1="48" y2="48"/><line class="done" x1="${x0}" x2="${x(now)}" y1="48" y2="48"/>`;
  s.tables.forEach((t, i) => {
    const ex = x(toMin(t.expected_at));
    const up = i % 2 === 0;
    const color = { ok: "var(--good)", advertencia: "var(--warning)", falla: "var(--critical)", no_disponible: "var(--critical)", retrasada: "var(--warning)" }[t.status] || "var(--text-3)";
    const stroke = t.status === "esperando" ? "var(--text-3)" : color;
    const ly = up ? 22 : 86;
    out += `<g class="marker" data-table="${esc(t.name)}" data-tip="${esc(t.title)} · esperada ${t.expected_at}${t.arrived_at ? ` · llegó ${t.arrived_at}` : ""} · ${esc(t.status_label)}">
      <line x1="${ex}" x2="${ex}" y1="${up ? 28 : 54}" y2="${up ? 42 : 72}" stroke="var(--border)"/>
      <circle cx="${ex}" cy="48" r="8" fill="${color}" opacity="${t.status === "esperando" ? 0.35 : 1}" stroke="${stroke}" stroke-width="2" ${t.status === "esperando" ? 'stroke-dasharray="2 2"' : ""}/>
      ${t.arrived_at && t.arrived_at !== t.expected_at ? `<path d="M${x(toMin(t.arrived_at))} 41 l4 4 -4 4 -4 -4z" fill="${color}" opacity=".7"/>` : ""}
      <text class="lbl ${t.status === "falla" || t.status === "no_disponible" ? "strong" : ""}" x="${ex}" y="${ly}" text-anchor="middle">${esc(t.name)}</text></g>`;
  });
  out += `<line class="now" x1="${x(now)}" x2="${x(now)}" y1="30" y2="66"/></svg>`;
  return out;
}

function spark(values) {
  if (!values?.length) return "";
  const W = 110, H = 28, n = values.length;
  const y = (v) => H - 3 - ((Math.max(50, v) - 50) / 50) * (H - 6);
  const pts = values.map((v, i) => `${(i / Math.max(1, n - 1)) * W},${y(v)}`).join(" ");
  const bad = values.map((v, i) => v < 90 ? `<circle cx="${(i / Math.max(1, n - 1)) * W}" cy="${y(v)}" r="2" fill="var(--critical)"/>` : "").join("");
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" aria-label="Puntaje últimos 30 días"><polyline points="${pts}" fill="none" stroke="var(--series-1)" stroke-width="1.5"/>${bad}</svg>`;
}

// --------------------------------------------------------------------- gráficos
function lineChart(el, id, pts, { min, max, unit = "", fmt = (v) => nf(v, 1), band = false, label = "Valor" } = {}) {
  if (!el) return;
  const W = el.clientWidth || 600, H = el.clientHeight || 190, m = { l: 52, r: 10, t: 10, b: 20 };
  if (!pts.length) { setHTML(el, `<p class="empty">Aún no hay historia.</p>`); return; }
  const vals = pts.flatMap((p) => [p.v, band ? p.lo : null, band ? p.hi : null]).filter((v) => v != null);
  const lo = min ?? Math.min(...vals), hi = max ?? Math.max(...vals);
  const pad = (hi - lo) * 0.08 || 1;
  const y0 = min ?? (lo >= 0 ? Math.max(0, lo - pad) : lo - pad), y1 = max ?? hi + pad;
  const x = (i) => m.l + (i / Math.max(1, pts.length - 1)) * (W - m.l - m.r);
  const y = (v) => H - m.b - ((v - y0) / (y1 - y0 || 1)) * (H - m.t - m.b);
  const ticks = [y0, (y0 + y1) / 2, y1];
  let bandPath = "";
  if (band) {
    const withBand = pts.map((p, i) => ({ ...p, i })).filter((p) => p.lo != null && p.hi != null);
    if (withBand.length > 1) bandPath = `<path d="M${withBand.map((p) => `${x(p.i)},${y(p.hi)}`).join(" L")} L${withBand.slice().reverse().map((p) => `${x(p.i)},${y(Math.max(y0, p.lo))}`).join(" L")} Z" fill="var(--band)"/>`;
  }
  const line = pts.map((p, i) => p.v == null ? null : `${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).filter(Boolean).join(" ");
  const dots = pts.map((p, i) => p.bad ? `<circle cx="${x(i)}" cy="${y(p.v)}" r="4.5" fill="var(--critical)" stroke="var(--surface)" stroke-width="2"/>` : "").join("");
  const h = state.hover[id];
  setHTML(el, `<svg viewBox="0 0 ${W} ${H}">
    <g class="grid">${ticks.map((t) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}"/>`).join("")}</g>
    <g class="axis">${ticks.map((t) => `<text x="${m.l - 6}" y="${y(t) + 3}" text-anchor="end">${esc(fmt(t))}</text>`).join("")}
      <text x="${m.l}" y="${H - 4}">${esc(pts[0].x.slice(5))}</text><text x="${W - m.r}" y="${H - 4}" text-anchor="end">${esc(pts.at(-1).x.slice(5))}</text></g>
    ${bandPath}<polyline points="${line}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>${dots}
    ${h != null && pts[h]?.v != null ? `<line class="crosshair" x1="${x(h)}" x2="${x(h)}" y1="${m.t}" y2="${H - m.b}"/><circle cx="${x(h)}" cy="${y(pts[h].v)}" r="4" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2"/>` : ""}
  </svg>`);
  el._hover = { n: pts.length, x0: m.l, x1: W - m.r, html: (i) => {
    const p = pts[i];
    return `<b>${esc(p.x)}</b><br>${label}: <b>${p.v == null ? "—" : esc(fmt(p.v))}${unit}</b>${band && p.lo != null ? `<br>Normal: ${esc(fmt(p.lo))} – ${esc(fmt(p.hi))}` : ""}${p.bad ? "<br><b style='color:var(--critical)'>Fuera de lo normal</b>" : ""}`;
  } };
  if (!el.dataset.bound) {
    el.dataset.bound = "1";
    el.addEventListener("mousemove", (e) => {
      const hv = el._hover; const r = el.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * (el.clientWidth || r.width);
      const i = Math.max(0, Math.min(hv.n - 1, Math.round(((px - hv.x0) / (hv.x1 - hv.x0)) * (hv.n - 1))));
      if (state.hover[id] !== i) { state.hover[id] = i; rerenderChart(); }
      showTip(hv.html(i), e.clientX, e.clientY);
    });
    el.addEventListener("mouseleave", () => { state.hover[id] = null; hideTip(); rerenderChart(); });
  }
}
function rerenderChart() { if (state.view === "tablas") drawDetailCharts(); else render(); }

function showTip(html, x, y) {
  const t = $("#tooltip"); t.innerHTML = html; t.hidden = false;
  t.style.left = Math.min(innerWidth - t.offsetWidth - 8, x + 14) + "px"; t.style.top = y + 14 + "px";
}
const hideTip = () => ($("#tooltip").hidden = true);
document.addEventListener("mouseover", (e) => { const t = e.target.closest("[data-tip]"); if (t) { const r = t.getBoundingClientRect(); showTip(esc(t.dataset.tip), r.left, r.bottom); } });
document.addEventListener("mouseout", (e) => { if (e.target.closest("[data-tip]")) hideTip(); });

// ---------------------------------------------------------------------- TABLAS
async function renderTablas(s, force) {
  if (!s.tables.some((t) => t.name === state.table)) state.table = s.tables[0].name;
  setHTML($("#table-picker"), s.tables.map((t) => `<button data-pick="${esc(t.name)}" class="${t.name === state.table ? "active" : ""}">${st(t.status, "")}${esc(t.title)}</button>`).join(""));
  const sum = s.tables.find((t) => t.name === state.table);
  const key = `${state.table}|${s.date}|${sum.status}|${sum.rows}|${sum.score}`;
  if (!force && key === state.detailKey) return;
  state.detailKey = key;
  const detail = await api(`/api/tables/${state.table}`);
  if (detail.spec.name !== state.table || state.detailKey !== key) return;  // a newer request superseded this one
  state.detail = detail;
  renderDetail();
}

function renderDetail() {
  const d = state.detail; if (!d) return;
  const sp = d.spec, sm = d.summary;
  const results = d.results;
  setHTML($("#table-detail"), `
    <div class="card">
      <div class="detail-head">
        <div>
          <div class="mono muted">${esc(sp.name)}</div>
          <h2>${esc(sp.title)} ${st(sm.status, sm.status_label)}</h2>
          <p class="muted" style="margin:4px 0 0">${esc(sp.description)}</p>
        </div>
        <div class="small muted" style="text-align:right">Responsable: <b>${esc(sp.owner)}</b><br>${esc(sp.owner_email)}<br>Alimenta: ${esc(sp.consumers.join(", "))}</div>
      </div>
      <div class="kv">
        <div><span>Hora acordada / llegada</span><b>${esc(sp.expected_at)} / ${esc(sm.arrived_at ?? "—")}</b></div>
        <div><span>Registros hoy / normal</span><b>${nf(sm.rows)} / ${sm.expected_rows ? "~" + nf(sm.expected_rows) : "—"}</b></div>
        <div><span>Puntaje de calidad</span><b style="color:${scoreColor(sm.score)}">${sm.score == null ? "—" : nf(sm.score, 1)}</b></div>
        <div><span>Controles OK</span><b>${sm.checks_ok}/${sm.checks_total}</b> <span class="small muted">(${sp.load_type === "snapshot" ? "foto diaria" : "carga incremental"})</span></div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>Controles de hoy</h3><span class="muted small">clic en una fila para ver registros de ejemplo y el SQL</span></div>
      ${results.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Estado</th><th>Control</th><th>Tipo</th><th>Severidad</th><th>Resultado</th><th class="num">Afectados</th></tr></thead><tbody>
      ${results.map((r) => `
        <tr class="clickable ${r.status === "falla" ? "failrow" : ""}" data-check="${esc(r.check_id)}">
          <td>${cst(r.status)}</td><td>${esc(r.name)}</td>
          <td>${r.kind === "monitor" ? '<span class="pill monitor">automático</span>' : '<span class="pill">regla</span>'}</td>
          <td>${sev(r.severity, r.severity_label)}</td><td class="small">${esc(r.message)}<br><span class="muted">esperado: ${esc(r.threshold)}</span></td>
          <td class="num">${r.kind === "regla" && r.rule_type !== "outlier" ? nf(r.failing_rows) : ""}</td></tr>
        ${state.expanded.has(r.check_id) ? `<tr class="expand"><td colspan="6">${examplesTable(r.examples)}${r.sql ? `<pre class="sql">${esc(r.sql)}</pre>` : '<span class="small muted">Monitor automático: no requiere configuración.</span>'}</td></tr>` : ""}`).join("")}
      </tbody></table></div>` : `<p class="empty">La tabla aún no ha llegado hoy. Se validará apenas aparezca (esperada a las ${esc(sp.expected_at)}).</p>`}
    </div>
    <div class="grid-2">
      <div class="card"><div class="card-head"><h3>Registros por día</h3><span class="legend"><span><i style="background:var(--band)"></i>banda de normalidad</span><span><i style="background:var(--critical);border-radius:50%"></i>anomalía</span></span></div><div class="chart" id="ch-volume"></div></div>
      <div class="card"><div class="card-head"><h3>Puntaje de calidad por día</h3></div><div class="chart" id="ch-score"></div></div>
    </div>
    ${Object.keys(d.outliers).length ? `<div class="grid-2">${Object.entries(d.outliers).map(([id, o]) => `
      <div class="card"><div class="card-head"><h3>${esc(o.name)}</h3><span class="mono muted small">${esc(id)}</span></div><div class="chart sm" id="ch-out-${esc(id)}"></div></div>`).join("")}</div>` : ""}
    <div class="card">
      <div class="card-head"><h3>Historial de calidad · últimos 30 días</h3><span class="legend"><span><i style="background:var(--good)"></i>OK</span><span><i style="background:var(--warning)"></i>advertencia</span><span><i style="background:var(--critical)"></i>falla</span></span></div>
      <div class="heat">${d.heatmap.rows.map((row) => `<div class="hrow"><span title="${esc(row.name)}">${esc(row.name)}</span><span class="cells">${d.heatmap.days.map((day) => `<i class="${row.days[day] ?? "na"}" data-tip="${day}: ${esc(ST[row.days[day]] ?? "sin dato")}"></i>`).join("")}</span></div>`).join("")}</div>
    </div>
    <div class="card">
      <div class="card-head"><h3>Perfil de columnas (carga de hoy)</h3></div>
      ${d.profile.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Columna</th><th>Tipo</th><th>Descripción</th><th class="num">% vacíos</th><th class="num">Distintos</th><th class="num">Mín</th><th class="num">Máx</th><th class="num">Promedio</th></tr></thead><tbody>
        ${d.profile.map((c) => `<tr><td class="mono">${esc(c.column)}</td><td class="small muted">${esc(c.type)}</td><td class="small muted">${esc(c.description)}</td>
          <td class="num" style="${c.null_pct > 0 ? "color:var(--critical);font-weight:600" : ""}">${nf(c.null_pct, 2)}%</td><td class="num">${nf(c.distinct)}</td>
          <td class="num">${c.min == null ? "" : big(c.min)}</td><td class="num">${c.max == null ? "" : big(c.max)}</td><td class="num">${c.mean == null ? "" : big(c.mean)}</td></tr>`).join("")}
      </tbody></table></div>` : `<p class="empty">Disponible cuando llegue la carga de hoy.</p>`}
    </div>`);
  drawDetailCharts();
}

function drawDetailCharts() {
  const d = state.detail; if (!d) return;
  lineChart($("#ch-volume"), "vol", d.volume.map((p) => ({ x: p.fecha, v: p.filas, lo: p.lo, hi: p.hi, bad: p.lo != null && (p.filas < p.lo || p.filas > p.hi) })), { band: true, fmt: (v) => nf(v), label: "Registros" });
  lineChart($("#ch-score"), "score", d.scores.map((p) => ({ x: p.fecha, v: p.score, bad: p.score < 80 })), { min: 0, max: 100, fmt: (v) => nf(v), label: "Puntaje" });
  Object.entries(d.outliers).forEach(([id, o]) => lineChart($(`#ch-out-${id}`), "out" + id,
    o.points.map((p) => ({ x: p.fecha, v: p.valor, lo: p.lo, hi: p.hi, bad: p.lo != null && (p.valor < p.lo || p.valor > p.hi) })), { band: true, fmt: big }));
}

function examplesTable(rows) {
  if (!rows?.length) return `<span class="small muted">Sin registros de ejemplo.</span>`;
  const cols = Object.keys(rows[0]);
  return `<div class="table-wrap"><table class="examples"><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td>${r[c] == null ? '<span style="color:var(--critical)">NULL</span>' : esc(typeof r[c] === "number" ? nf(r[c], 2) : r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

$("#table-picker").addEventListener("click", (e) => {
  const b = e.target.closest("[data-pick]"); if (!b) return;
  state.table = b.dataset.pick; store.set("atlas.table", state.table); state.expanded.clear(); render(true);
});
$("#table-detail").addEventListener("click", (e) => {
  const row = e.target.closest("[data-check]"); if (!row) return;
  const id = row.dataset.check;
  state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
  renderDetail();
});
function openTable(name) { state.table = name; store.set("atlas.table", name); state.expanded.clear(); setView("tablas"); }
document.addEventListener("click", (e) => {
  const c = e.target.closest("[data-table]");
  if (c && (c.closest("#table-cards") || c.closest("#today"))) openTable(c.dataset.table);
});

// ------------------------------------------------------------------ INCIDENTES
const INC_ST = { abierto: ["falla", "Abierto"], escalado: ["advertencia", "Escalado"], resuelto: ["ok", "Resuelto"] };
const dur = (min) => min < 60 ? `${min} min` : min < 1440 ? `${nf(min / 60, 1)} h` : `${nf(min / 1440, 1)} días`;

async function renderIncidentes(s, force) {
  const list = s.incidents.filter((i) => state.incFilter === "todos" ? true : state.incFilter === "resuelto" ? i.status === "resuelto" : i.status !== "resuelto");
  if (!state.incSel || !s.incidents.some((i) => i.id === state.incSel)) state.incSel = list[0]?.id ?? null;
  setHTML($("#inc-list"), list.length ? list.map((i) => `
    <li class="${i.id === state.incSel ? "sel" : ""}" data-inc="${esc(i.id)}">
      <div class="top">${sev(i.severity, i.severity_label)} ${st(...INC_ST[i.status])} ${i.simulated ? '<span class="pill">simulado</span>' : ""}</div>
      <div class="title"><b>${esc(i.table)}</b> · ${esc(i.title)}</div>
      <div class="sub">${esc(i.id)} · ${esc(i.opened_date)} ${esc(i.opened_time)} · ${dur(i.minutes_open)}${i.failing_checks > 1 ? ` · ${i.failing_checks} controles` : ""}</div>
    </li>`).join("") : `<li class="empty">${state.incFilter === "activos" ? "No hay incidentes activos. Todo en orden." : "Sin incidentes."}</li>`);
  if (!state.incSel) { setHTML($("#inc-detail"), `<p class="muted">Selecciona un incidente.</p>`); return; }
  const sum = s.incidents.find((i) => i.id === state.incSel);
  const key = `${sum.id}|${sum.status}|${sum.loads_failed}|${sum.failing_checks}|${sum.status === "resuelto" ? "" : s.time}`;
  if (!force && key === state.incKey) return;
  state.incKey = key;
  const detail = await api(`/api/incidents/${state.incSel}`);
  if (detail.id !== state.incSel || state.incKey !== key) return;  // stale response
  state.incDetail = detail;
  renderIncidentDetail();
}

function renderIncidentDetail() {
  const i = state.incDetail; if (!i) return;
  const root = $("#inc-detail");
  if (!$("#inc-head", root)) root.innerHTML = `<div id="inc-head"></div><div id="inc-body"></div><div id="inc-email"></div>`;
  const done = i.status === "resuelto";
  setHTML($("#inc-head"), `
    <div class="inc-head">
      <div>
        ${sev(i.severity, i.severity_label)} ${st(...INC_ST[i.status])} <span class="small muted">${esc(i.id)}</span>
        <h2>${esc(i.table_title)}: ${esc(i.title)}</h2>
        <p class="small muted" style="margin:0">Tabla <code>${esc(i.table)}</code> · Responsable <b>${esc(i.owner)}</b> · Abierto ${esc(i.opened_date)} ${esc(i.opened_time)}</p>
      </div>
      <div class="actions">
        <button class="btn" id="btn-email" ${done ? "disabled" : ""}>✉ Redactar correo de escalamiento</button>
        <button class="btn primary" id="btn-resolve" ${done ? "disabled" : ""}>Marcar resuelto</button>
      </div>
    </div>`);
  setHTML($("#inc-body"), `
    <div class="kv">
      <div><span>Tiempo ${done ? "hasta resolver" : "abierto"}</span><b>${dur(i.minutes_open)}</b></div>
      <div><span>Cargas con falla</span><b>${i.loads_failed}</b></div>
      <div><span>Fallas en 30 días previos</span><b>${i.recurrence_30d}</b></div>
      <div><span>Resolución</span><b>${esc(i.resolution ?? "pendiente")}</b></div>
    </div>
    <h4>Qué falló</h4>
    ${i.checks.map((c) => `<div class="check-card">
      <div class="actions">${sev(c.severity, c.severity_label)} <b>${esc(c.name)}</b> ${c.kind === "monitor" ? '<span class="pill monitor">automático</span>' : ""}</div>
      <p class="msg">${esc(c.message)} <span class="muted small">(esperado: ${esc(c.threshold)})</span></p>
      ${c.examples?.length ? examplesTable(c.examples) : ""}
      ${c.sql ? `<details><summary class="small muted">Ver SQL del control</summary><pre class="sql">${esc(c.sql)}</pre></details>` : ""}
    </div>`).join("")}
    <div class="grid-2" style="margin-top:6px">
      <div><h4>Impacto</h4><p class="chips">${i.consumers.map((c) => `<span>${esc(c)}</span>`).join("")}</p></div>
      <div><h4>Línea de tiempo</h4><ul class="tl">${i.timeline.map((t) => `<li><time>${esc(t.date.slice(5))} ${esc(t.time)}</time><span>${esc(t.text)}</span></li>`).join("")}</ul></div>
    </div>`);
  const em = state.emails[i.id];
  setHTML($("#inc-email"), em ? emailView(em, i) : "");
}

function emailView(e, i) {
  if (e.loading) return `<div class="email"><div class="hdr">Redactando correo…</div></div>`;
  return `<div class="email">
    <div class="hdr"><span>Para: <b>${esc(e.to)}</b></span><span>Asunto: <b>${esc(e.asunto)}</b></span></div>
    <pre>${esc(e.cuerpo)}</pre>
    <div class="foot"><span class="small muted">Redactado por ${e.mode === "claude" ? `Claude (${esc(e.model)})` : "plantilla"}${e.fallback_reason ? " · IA no disponible" : ""} · los controles deciden, el copiloto redacta</span>
      <span class="actions"><button class="btn small" id="btn-copy">Copiar</button>
      <button class="btn small primary" id="btn-escalate" ${i.status !== "abierto" ? "disabled" : ""}>${i.status === "abierto" ? "Marcar como escalado" : "Escalado ✓"}</button></span></div>
  </div>`;
}

$("#inc-filter").addEventListener("click", (e) => {
  const b = e.target.closest("[data-f]"); if (!b) return;
  state.incFilter = b.dataset.f; $$("#inc-filter button").forEach((x) => x.classList.toggle("active", x === b));
  state.incSel = null; render(true);
});
$("#inc-list").addEventListener("click", (e) => {
  const li = e.target.closest("[data-inc]"); if (!li) return;
  state.incSel = li.dataset.inc; state.incKey = ""; render(true);
});
$("#inc-detail").addEventListener("click", async (e) => {
  const i = state.incDetail; if (!i) return;
  try {
    if (e.target.id === "btn-email") await draftEmail(i.id);
    if (e.target.id === "btn-copy") {
      const em = state.emails[i.id];
      await navigator.clipboard.writeText(`Para: ${em.to}\nAsunto: ${em.asunto}\n\n${em.cuerpo}`).catch(() => {});
      toast("Correo copiado al portapapeles");
    }
    if (e.target.id === "btn-escalate") {
      state.incDetail = await api(`/api/incidents/${i.id}/escalate`, { method: "POST" });
      renderIncidentDetail(); toast(`Escalado a ${i.owner}`);
    }
    if (e.target.id === "btn-resolve") {
      const note = prompt("Comentario de resolución (qué se hizo):", "Se recargó la tabla con el archivo correcto");
      if (note === null) return;
      state.incDetail = await api(`/api/incidents/${i.id}/resolve`, { method: "POST", body: JSON.stringify({ note }) });
      renderIncidentDetail(); toast("Incidente resuelto");
    }
  } catch (err) { toast(err.message); }
});
async function draftEmail(id) {
  state.emails[id] = { loading: true }; renderIncidentDetail();
  try { state.emails[id] = await api(`/api/incidents/${id}/copilot`, { method: "POST" }); }
  catch (err) { delete state.emails[id]; toast(err.message); }
  renderIncidentDetail();
}

// ---------------------------------------------------------------------- REGLAS
// En la demo pública las reglas base quedan protegidas: solo se editan las que crea cada visitante.
const locked = (r) => state.meta?.public_demo && r.author !== "usuario";
async function loadRules() { state.rules = await api("/api/rules"); }

async function renderReglas(s, force) {
  if (!state.rules || force) await loadRules();
  const R = state.rules;
  const filter = $("#rules-filter");
  const tableKey = s.tables.map((t) => t.name).join(",");
  if (filter.dataset.tables !== tableKey) {
    filter.dataset.tables = tableKey;
    filter.length = 1; $("#rf-table").length = 0; $("#rf-type").length = 0; $("#rf-severity").length = 0;
    s.tables.forEach((t) => filter.add(new Option(t.title, t.name)));
    const tSel = $("#rf-table"); s.tables.forEach((t) => tSel.add(new Option(t.title, t.name)));
    Object.entries(R.types).forEach(([k, v]) => $("#rf-type").add(new Option(v, k)));
    Object.entries(R.severities).forEach(([k, v]) => $("#rf-severity").add(new Option(v, k)));
    $("#rf-severity").value = "alta";
    renderParams();
  }
  const rows = R.rules.filter((r) => !filter.value || r.table === filter.value);
  setHTML($("#rules-table"), `<thead><tr><th>Activa</th><th>Tabla</th><th>Regla</th><th>Tipo</th><th>Severidad</th><th></th></tr></thead><tbody>
    ${rows.map((r) => `<tr>
      <td><label class="switch"><input type="checkbox" data-toggle="${esc(r.id)}" ${r.enabled ? "checked" : ""} ${locked(r) ? "disabled" : ""}><span></span></label></td>
      <td class="mono small">${esc(r.table)}</td>
      <td>${esc(r.description)}${r.author === "usuario" ? '<span class="tag-user">tuya</span>' : r.author === "sugerida" ? '<span class="tag-user">sugerida por ATLAS</span>' : ""}${r.note ? `<br><span class="small muted">${esc(r.note)}</span>` : ""}</td>
      <td class="small muted">${esc(r.type_label)}</td><td>${sev(r.severity, r.severity_label)}</td>
      <td><button class="icon-btn" data-del="${esc(r.id)}" ${locked(r) ? "hidden" : ""} title="Eliminar regla" aria-label="Eliminar regla"><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></button></td></tr>`).join("")}</tbody>`);
}

const numericCols = (t) => state.rules.tables[t].filter((c) => ["entero", "decimal"].includes(c.type));
function colSelect(name, cols, label = "Columna") {
  return `<label>${label}<select data-param="${name}">${cols.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("")}</select></label>`;
}
function renderParams() {
  const t = $("#rf-table").value, type = $("#rf-type").value, all = state.rules.tables[t];
  const html = {
    no_nulos: colSelect("column", all) + `<label>Tolerancia (% de registros que pueden venir vacíos)<input type="number" min="0" max="100" step="0.1" value="0" data-param="max_pct"></label>`,
    unico: `<label>Columnas que no se pueden repetir (juntas)</label><div class="chips">${all.map((c, i) => `<label class="small"><input type="checkbox" data-multi="columns" value="${esc(c.name)}" ${i === 0 ? "checked" : ""}> ${esc(c.name)}</label>`).join("")}</div>`,
    rango: colSelect("column", numericCols(t)) + `<div class="row2"><label>Mínimo<input type="number" step="any" data-param="min" placeholder="sin mínimo"></label><label>Máximo<input type="number" step="any" data-param="max" placeholder="sin máximo"></label></div>`,
    valores_permitidos: colSelect("column", all) + `<label>Valores permitidos (separados por coma)<input data-param="values" placeholder="app, pse, oficina"></label>`,
    comparacion: `<div class="row2">${colSelect("column", all, "Columna")}<label>Operador<select data-param="operator">${state.rules.operators.map((o) => `<option>${esc(o)}</option>`).join("")}</select></label></div>${colSelect("other_column", all, "Comparada con")}`,
    fecha_del_dia: colSelect("column", all.filter((c) => c.type === "fecha")) + `<p class="hint">Detecta cuando se recarga un archivo de otra fecha.</p>`,
    outlier: `<div class="row2"><label>Medida<select data-param="aggregate">${state.rules.aggregates.map((a) => `<option>${esc(a)}</option>`).join("")}</select></label>${colSelect("column", numericCols(t))}</div>
      <div class="row2"><label>Desviaciones (σ)<input type="number" min="1" max="10" step="0.5" value="3" data-param="z"></label><label>Comparar con<select data-param="same_weekday"><option value="true">mismo día de la semana</option><option value="false">últimos 14 días</option></select></label></div>
      <p class="hint">Alerta si la medida de hoy se aleja más de N desviaciones estándar de su histórico.</p>`,
    sql: `<label>Condición que identifica los registros <b>malos</b><textarea data-param="condition" placeholder="estado = 'aplicado' AND valor_pago <= 0"></textarea></label><p class="hint">Solo lectura: se ejecuta en modo consulta (sin escritura).</p>`,
  }[type];
  setHTML($("#rf-params"), html);
}
function readRule() {
  const f = $("#rule-form"); const params = {};
  $$("[data-param]", f).forEach((el) => { if (el.value !== "") params[el.dataset.param] = el.dataset.param === "same_weekday" ? el.value === "true" : el.value; });
  const multi = $$("[data-multi]:checked", f).map((el) => el.value);
  if (multi.length) params.columns = multi;
  return { table: f.table.value, type: f.type.value, severity: f.severity.value, note: f.note.value, params };
}
function resultBox(r) {
  const cls = r.status === "falla" ? "falla" : r.status === "error" ? "error" : "ok";
  return `<div class="result-box ${cls}"><b>${r.status === "falla" ? "La última carga incumple la regla" : r.status === "error" ? "Error en la regla" : "La última carga cumple la regla"}</b> <span class="small muted">(${esc(r.date)})</span>
    <p style="margin:4px 0">${esc(r.message)}</p>${r.examples?.length ? examplesTable(r.examples) : ""}<pre class="sql">${esc(r.sql)}</pre></div>`;
}
$("#rf-table").addEventListener("change", renderParams);
$("#rf-type").addEventListener("change", renderParams);
$("#rules-filter").addEventListener("change", () => render(true));
$("#rf-preview").addEventListener("click", async () => {
  try { setHTML($("#rf-result"), resultBox(await api("/api/rules/preview", { method: "POST", body: JSON.stringify(readRule()) }))); }
  catch (err) { setHTML($("#rf-result"), `<div class="result-box error">${esc(err.message)}</div>`); }
});
$("#rule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/rules", { method: "POST", body: JSON.stringify(readRule()) });
    setHTML($("#rf-result"), `<div class="result-box ok">Regla <b>${esc(r.id)}</b> guardada: ${esc(r.description)}. Se evaluará desde la próxima carga de <code>${esc(r.table)}</code>.</div>`);
    $("#rules-filter").value = r.table; await renderReglas(state.snap, true);
  } catch (err) { setHTML($("#rf-result"), `<div class="result-box error">${esc(err.message)}</div>`); }
});
$("#rules-table").addEventListener("change", async (e) => {
  const id = e.target.dataset.toggle; if (!id) return;
  try { await api(`/api/rules/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: e.target.checked }) }); toast(e.target.checked ? "Regla activada" : "Regla desactivada"); await loadRules(); }
  catch (err) { toast(err.message); }
});
$("#rules-table").addEventListener("click", async (e) => {
  const id = e.target.closest("[data-del]")?.dataset.del; if (!id || !confirm(`¿Eliminar la regla ${id}?`)) return;
  try { await api(`/api/rules/${id}`, { method: "DELETE" }); toast("Regla eliminada"); await renderReglas(state.snap, true); }
  catch (err) { toast(err.message); }
});

// --------------------------------------------------------------- simulación
function openModal(id) { $(id).hidden = false; }
$$(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m || e.target.closest("[data-close]")) m.hidden = true; }));
$("#btn-anomaly").addEventListener("click", () => {
  const s = state.snap;
  setHTML($("#scenarios"), s.scenarios.map((sc) => {
    const t = s.tables.find((x) => x.name === sc.table);
    const when = t.arrived_at || t.status === "no_disponible" ? "mañana" : "hoy";
    return `<div class="scenario"><h4>${esc(sc.title)}</h4><p>${esc(sc.description)}</p>
      <div class="row"><span class="mono small muted">${esc(sc.table)} · ${when}</span>
      <button class="btn small primary" data-sc="${esc(sc.id)}" ${sc.queued ? "disabled" : ""}>${sc.queued ? "Programada" : "Aplicar"}</button></div></div>`;
  }).join(""));
  openModal("#anomaly-modal");
});
$("#scenarios").addEventListener("click", async (e) => {
  const id = e.target.closest("[data-sc]")?.dataset.sc; if (!id) return;
  try { const r = await api(`/api/scenarios/${id}`, { method: "POST" }); $("#anomaly-modal").hidden = true; toast(`Anomalía programada para ${r.when} en ${r.table}`); }
  catch (err) { toast(err.message); }
});
async function control(body) { try { return await api("/api/control", { method: "POST", body: JSON.stringify(body) }); } catch (err) { toast(err.message); } }
$$(".tabs button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
$("#btn-play").addEventListener("click", () => control({ running: !state.snap?.running }));
$("#speed").addEventListener("change", (e) => control({ speed: Number(e.target.value) }));
$("#btn-about").addEventListener("click", () => openModal("#about"));

// ---------------------------------------------------------- fuentes de datos
$("#btn-source").addEventListener("click", async () => {
  try {
    const data = await api("/api/sources");
    setHTML($("#sources"), data.sources.map((src) => `
      <div class="scenario">
        <h4>${esc(src.title)}${src.name === data.active ? ' <span class="pill monitor">en uso</span>' : ""}</h4>
        <p>${esc(src.available ? src.description : src.error || "No disponible")}</p>
        ${src.tables?.length ? `<p class="chips">${src.tables.map((t) => `<span>${esc(t)}</span>`).join("")}</p>` : ""}
        ${src.path ? `<p class="mono small muted" style="margin:0">${esc(src.path)}</p>` : ""}
        <div class="row"><span></span><button class="btn small primary" data-src="${esc(src.name)}" ${!src.available || src.name === data.active ? "disabled" : ""}>
          ${src.name === data.active ? "Conectada" : "Usar esta fuente"}</button></div>
      </div>`).join(""));
    openModal("#source-modal");
  } catch (err) { toast(err.message); }
});
$("#sources").addEventListener("click", async (e) => {
  const name = e.target.closest("[data-src]")?.dataset.src; if (!name) return;
  e.target.disabled = true; e.target.textContent = "Conectando…";
  try {
    await api("/api/source", { method: "POST", body: JSON.stringify({ name }) });
    $("#source-modal").hidden = true;
    state.rules = null; state.detailKey = ""; state.incSel = null; state.incKey = "";
    state.table = state.snap.tables[0]?.name || state.table;
    toast(name === "demo" ? "Volviste a la demo" : `Conectado a ${name.toUpperCase()}: validando sus tablas reales`);
    render(true);
  } catch (err) { toast(err.message); e.target.disabled = false; e.target.textContent = "Usar esta fuente"; }
});
$("#btn-demo").addEventListener("click", () => runTour());

function applyTheme(t) { t ? (document.documentElement.dataset.theme = t) : delete document.documentElement.dataset.theme; }
applyTheme(store.get("atlas.theme", null));
$("#btn-theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(dark ? "light" : "dark"); store.set("atlas.theme", dark ? "light" : "dark"); render(true);
});
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea") || e.metaKey || e.ctrlKey) return;
  const views = ["resumen", "tablas", "incidentes", "reglas"];
  if (e.key >= "1" && e.key <= "4") setView(views[Number(e.key) - 1]);
  else if (e.key === " ") { e.preventDefault(); $("#btn-play").click(); }
  else if (e.key === "Escape") { $$(".modal").forEach((m) => (m.hidden = true)); stopTour(); }
});
addEventListener("resize", () => render());

// ----------------------------------------------------------------- demo guiada
let tourToken = 0;
function stopTour() { tourToken++; $("#tour").hidden = true; $$(".spotlight").forEach((el) => el.classList.remove("spotlight")); }
$("#tour-skip").addEventListener("click", stopTour);
function waitFor(pred, timeout = 30000) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const check = () => { if (state.snap && pred(state.snap)) { tickHooks.delete(check); resolve(true); } else if (Date.now() - t0 > timeout) { tickHooks.delete(check); resolve(false); } };
    tickHooks.add(check); check();
  });
}
async function runTour() {
  stopTour(); const token = tourToken; const steps = 9; let n = 0;
  const say = async (html, view, ms, spot) => {
    if (token !== tourToken) throw new Error("stop");
    n++; if (view) setView(view);
    $$(".spotlight").forEach((el) => el.classList.remove("spotlight")); if (spot) { const el = $(spot); el?.classList.add("spotlight"); el?.scrollIntoView({ block: "center", behavior: "smooth" }); }
    $("#tour").hidden = false; $("#tour-text").innerHTML = html; $("#tour-bar").style.width = `${(n / steps) * 100}%`;
    if (ms) await sleep(ms);
  };
  try {
    await control({ next_day: true, running: false, random_anomalies: false });
    await say("Cada mañana el equipo de TI carga <b>6 tablas</b> del banco. ATLAS <b>no mueve datos: los vigila</b>, y valida cada tabla apenas llega.", "resumen", 6500);
    await say("La línea de tiempo muestra la <b>hora acordada</b> de cada tabla. Si una no llega a tiempo, ATLAS lo detecta solo.", null, 6000, "#today");
    await api("/api/scenarios/carga_duplicada", { method: "POST" });
    await api("/api/scenarios/no_llega", { method: "POST" });
    await say("Para la demo simulamos dos problemas reales: la <b>cartera se carga dos veces</b> y la tabla de <b>pagos no llega</b>.", null, 4500);
    await control({ running: true, speed: 2 });
    await waitFor((s) => s.tables.find((t) => t.name === "cartera_creditos").arrived_at, 25000);
    await say("Llegó la cartera: <b>el doble de registros</b> de lo normal y créditos repetidos. ATLAS abre un <b>incidente crítico</b> y notifica a Riesgo de Crédito.", null, 6500, "#table-cards");
    await waitFor((s) => s.tables.find((t) => t.name === "pagos").status === "no_disponible", 30000);
    await control({ running: false });
    await say("Son las 8:00 y <b>pagos no ha llegado</b> (se esperaba a las 7:00). Otro incidente, con el equipo de Recaudo como responsable.", null, 6500, "#today");
    const inc = state.snap.incidents.find((i) => i.table === "cartera_creditos" && i.status !== "resuelto");
    if (inc) { state.incFilter = "activos"; state.incSel = inc.id; state.incKey = ""; }
    await say("Cada incidente trae la <b>evidencia</b>: qué control falló, cuántos registros y ejemplos concretos.", "incidentes", 6500);
    if (inc) await draftEmail(inc.id);
    await say("Con un clic se redacta el <b>correo de escalamiento</b> al equipo responsable, con causa probable, impacto y acción solicitada.", null, 8000, "#inc-email");
    state.table = "cartera_creditos"; state.detailKey = "";
    await say("En la ficha de cada tabla: el <b>volumen diario con su banda de normalidad</b> (el punto rojo es la anomalía), el historial de 30 días y el perfil de columnas.", "tablas", 9000);
    await say("Las <b>reglas de negocio</b> se crean sin programar, por ejemplo <i>tasa_mora entre 0 y 100</i>. Cada regla se convierte en SQL y se puede probar antes de guardarla.", "reglas", 8000);
    await control({ running: true, speed: 1, random_anomalies: true });
    await say("Cuando la próxima carga llegue bien, <b>los incidentes se cierran solos</b> y queda medido el tiempo de resolución. ¡Ahora explora tú! (botón «Simular anomalía»)", "resumen", 8000);
    stopTour();
  } catch { if (token === tourToken) stopTour(); }
  finally { if (state.snap && !state.snap.running) control({ running: true, speed: 1 }); }
}

// ---------------------------------------------------------------------- inicio
api("/api/meta").then((m) => {
  state.meta = m;
  $("#gh-link").href = m.github_url;
  if (m.public_demo) toast("Demo pública: lo que hagas lo ven también otros visitantes.", 6000);
}).catch(() => {});
// La vista inicial sale del enlace (/#incidentes) o de la última visitada.
const [hashView, hashArg] = location.hash.slice(1).split("/");
if (hashArg && hashView === "tablas") state.table = hashArg;
if (hashArg && hashView === "incidentes") state.incSel = hashArg;
const saved = $(`.view[data-view="${hashView}"]`) ? hashView : store.get("atlas.view", "resumen");
if ($(`.view[data-view="${saved}"]`)) setView(saved);
addEventListener("hashchange", () => { const v = location.hash.slice(1).split("/")[0]; if ($(`.view[data-view="${v}"]`)) setView(v); });
connect();
