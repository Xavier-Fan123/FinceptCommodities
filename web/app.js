"use strict";

const SECTOR_LABEL = {
  energy: "Energy", precious_metals: "Precious Metals", base_metals: "Base Metals",
  grains: "Grains", softs: "Softs", livestock: "Livestock",
};
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const LPG_VIEWS = [
  { id: "cockpit", label: "Cockpit" },
  { id: "curves", label: "Curves & Spreads" },
  { id: "history", label: "History & Seasonality" },
  { id: "moc", label: "MOC & Fundamentals" },
  { id: "news", label: "News" },
  { id: "explorer", label: "Data Explorer" },
  { id: "status", label: "Data Status" },
];

const CARD_TITLES = {
  price: "Price · 1Y",
  curve: "Futures Curve",
  calspread: "Calendar Spreads",
  risk: "Risk · 1Y",
  season: "Seasonality · Avg Monthly Return",
  bands: "Seasonal Price Bands",
  cot: "COT Positioning",
  context: "Energy Chain Context",
  inv: "Inventories · EIA Weekly",
  balance: "Crude Balance · EIA Weekly",
};
const PANEL_CARDS = {
  history: ["price", "risk"],
  curve: ["curve", "calspread"],
  seasonality: ["season", "bands"],
  cot: ["cot"],
  inventory: ["inv"],
  balance: ["balance"],
  context: ["context"],
};
// Commodities with an EIA weekly inventory series (mirrors sources._EIA_SERIES)
const INVENTORY_IDS = new Set(["wti", "natgas", "rbob", "heating_oil"]);
// US crude supply/demand balance is WTI-specific (mirrors server panel_balance)
const BALANCE_IDS = new Set(["wti"]);
const ENERGY_CONTEXT_IDS = new Set(["wti", "brent", "natgas", "rbob", "heating_oil"]);
const ALL_CARDS = ["price", "curve", "calspread", "risk", "season", "bands", "cot", "inv", "balance", "context"];

let state = {
  view: "overview",        // overview | detail | spreads
  detailId: null,
  sector: null,
  rows: [],
  sort: { key: "sector", dir: 1 },
  filter: "",
  lastOverviewAt: 0,
  spreadsAt: 0,
  energyHubAt: 0,
  energyHubData: null,
  energyFilter: "",
  newsAt: 0,
  newsTopic: "energy",
  newsProduct: null,
  newsData: null,
  newsFilter: "",
  lpgView: "cockpit",
  lpgAsOf: "",
  lpgData: {},
  lpgRequestToken: 0,
  lpgSeriesId: null,
  lpgSeriesQuery: "",
  lpgMocDataset: "eWMD",
  lpgNewsFilter: { query: "", region: "all", product: "all", direction: "all" },
  lpgExplorer: { dataset: "series", query: "", entitlement: "all", offset: 0, limit: 100 },
  lpgRefreshTimer: null,
  lpgRefreshJob: null,
};

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);
const byId = (id) => document.getElementById(id);
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElementNS(attrs.ns || "http://www.w3.org/1999/xhtml", tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "ns") continue;
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.setAttribute("class", v);
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c) node.appendChild(c);
  return node;
};
const fmtNum = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v))
  ? "—" : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtVol = (v) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v), s = v < 0 ? "-" : "";
  return a >= 1e6 ? s + (a / 1e6).toFixed(1) + "M"
    : a >= 1e3 ? s + (a / 1e3).toFixed(0) + "K" : String(Math.round(v));
};
const dirClass = (v) => v === null || v === undefined ? "na" : v > 0 ? "up" : v < 0 ? "down" : "flat";
const sign = (v) => v > 0 ? "+" : "";
const timeStr = (unix) => new Date((unix || Date.now() / 1000) * 1000).toLocaleTimeString();
const priceDigits = (v) => (v === null || v === undefined) ? 2 : Math.abs(v) < 10 ? 4 : 2;
const stat = (label, value, cls = "") =>
  `<div class="stat"><span class="label">${label}</span><span class="value ${cls}">${value}</span></div>`;
const cleanSeries = (items, getVal) =>
  (items || []).filter(it => { const v = getVal(it); return v !== null && v !== undefined && !Number.isNaN(v); });

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) {
    let msg = "HTTP " + res.status;
    try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (e) { /* keep status */ }
    throw new Error(msg);
  }
  return res.json();
}

async function requestJSON(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let msg = "HTTP " + res.status;
    try {
      const body = await res.json();
      if (body && (body.error || body.detail)) msg = body.error || body.detail;
    } catch (e) { /* keep status */ }
    throw new Error(msg);
  }
  return res.json();
}

// ---------- routing ----------
function route() {
  const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  const routeParts = h.split("/").filter(Boolean);
  if (routeParts[0] === "lpg") showLpgView(routeParts[1] || "cockpit");
  else if (h === "spreads") showSpreadsView();
  else if (h === "energy-hub") showEnergyHubView();
  else if (h === "news") showNewsView();
  else if (h) showDetail(h.toLowerCase());
  else showOverview();
}

function swapViews(name) {
  for (const id of ["overview", "detail", "spreads", "energy-hub", "news", "lpg"]) {
    byId(id).classList.toggle("hidden", id !== name);
  }
  window.scrollTo(0, 0);
}

function setActiveTab(key) {
  document.querySelectorAll("#tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === key));
}

function showOverview() {
  state.view = "overview";
  state.detailId = null;
  swapViews("overview");
  document.title = "Fincept Commodities — Local";
  setActiveTab(state.sector || "all");
  renderGrid();
  if (!state.rows.length || Date.now() - state.lastOverviewAt > 60000) loadOverview();
}

function showSpreadsView() {
  state.view = "spreads";
  state.detailId = null;
  swapViews("spreads");
  document.title = "Spreads — Fincept Commodities";
  setActiveTab("spreads");
  loadSpreads(false);
}

function showEnergyHubView() {
  state.view = "energy-hub";
  state.detailId = null;
  swapViews("energy-hub");
  document.title = "Energy & Petrochemicals - Fincept Commodities";
  setActiveTab("energy-hub");
  loadEnergyHub(false);
}

function showNewsView() {
  state.view = "news";
  state.detailId = null;
  swapViews("news");
  document.title = "News - Fincept Commodities";
  setActiveTab("news");
  loadNews(false);
}

function showLpgView(view) {
  const next = LPG_VIEWS.some(item => item.id === view) ? view : "cockpit";
  state.view = "lpg";
  state.detailId = null;
  state.lpgView = next;
  swapViews("lpg");
  document.title = `${LPG_VIEWS.find(item => item.id === next).label} - LPG - Fincept Commodities`;
  setActiveTab("lpg");
  renderLpgShell();
  loadLpgView(next);
}

// ---------- overview ----------
async function loadOverview(fresh = false) {
  const loading = $("#ov-loading");
  if (!state.rows.length) loading.classList.remove("hidden");
  $("#refresh").classList.add("spin");
  try {
    const data = await fetchJSON("/api/overview" + (fresh ? "?fresh=1" : ""));
    state.rows = data.rows || [];
    state.lastOverviewAt = Date.now();
    renderTabs(data.sectors || []);
    if (state.view === "overview") renderGrid();
    $("#updated").textContent = "updated " + timeStr(data.updated);
    if (state.view === "detail" && state.detailId) {
      const row = state.rows.find(r => r.id === state.detailId);
      if (row) fillHeaderFromRow(row);
    }
  } catch (e) {
    $("#ov-meta").textContent = "load error: " + e.message;
  } finally {
    loading.classList.add("hidden");
    $("#refresh").classList.remove("spin");
  }
}

function renderTabs(sectors) {
  const tabs = $("#tabs");
  tabs.replaceChildren();
  const mk = (key, label) => {
    const b = el("button", { text: label, "data-tab": key });
    b.onclick = () => {
      if (key === "spreads" || key === "energy-hub" || key === "news" || key === "lpg") {
        if (key === "news") {
          state.newsProduct = null;
          state.newsAt = 0;
        }
        location.hash = "#/" + key;
        return;
      }
      state.sector = key === "all" ? null : key;
      if (state.view !== "overview") location.hash = "";
      else { setActiveTab(key); renderGrid(); }
    };
    return b;
  };
  tabs.appendChild(mk("all", "All"));
  sectors.forEach(s => tabs.appendChild(mk(s, SECTOR_LABEL[s] || s)));
  tabs.appendChild(mk("lpg", "LPG"));
  tabs.appendChild(mk("energy-hub", "Energy Hub"));
  tabs.appendChild(mk("news", "News"));
  tabs.appendChild(mk("spreads", "Spreads"));
  setActiveTab(state.view === "overview" ? (state.sector || "all") : state.view);
}

function visibleRows() {
  let rows = [...state.rows];
  if (state.sector) rows = rows.filter(r => r.sector === state.sector);
  const f = state.filter.trim().toLowerCase();
  if (f) rows = rows.filter(r => r.id.includes(f) || r.name.toLowerCase().includes(f));
  return rows;
}

function renderGrid() {
  const body = $("#grid-body");
  body.replaceChildren();
  const rows = visibleRows();
  const { key, dir } = state.sort;
  if (key === "sector") {
    const order = Object.keys(SECTOR_LABEL);
    rows.sort((a, b) => order.indexOf(a.sector) - order.indexOf(b.sector) || a.name.localeCompare(b.name));
  } else {
    rows.sort((a, b) => {
      const av = a[key], bv = b[key];
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      return (typeof av === "number" ? av - bv : String(av).localeCompare(bv)) * dir;
    });
  }
  updateSortIndicators();
  let lastSector = null;
  for (const r of rows) {
    if (key === "sector" && r.sector !== lastSector) {
      lastSector = r.sector;
      body.appendChild(el("tr", { class: "sector-head" },
        el("td", { colspan: "7", text: SECTOR_LABEL[r.sector] || r.sector })));
    }
    const tr = el("tr", { tabindex: "0" });
    tr.appendChild(el("td", { class: "sym", text: r.id }));
    tr.appendChild(el("td", { class: "name", text: r.name }));
    tr.appendChild(el("td", { class: "num", text: fmtNum(r.price, priceDigits(r.price)) }));
    tr.appendChild(el("td", { class: "num " + dirClass(r.change), text: r.change === null || r.change === undefined ? "—" : sign(r.change) + fmtNum(r.change, priceDigits(r.price)) }));
    tr.appendChild(el("td", { class: "num " + dirClass(r.change_percent), text: r.change_percent === null || r.change_percent === undefined ? "—" : sign(r.change_percent) + fmtNum(r.change_percent) + "%" }));
    tr.appendChild(el("td", { class: "num", text: fmtVol(r.volume) }));
    tr.appendChild(el("td", { class: "exch", text: r.exchange }));
    const open = () => { location.hash = "#/" + r.id; };
    tr.onclick = open;
    tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
    body.appendChild(tr);
  }
  const quoted = rows.filter(r => r.price !== null && r.price !== undefined).length;
  $("#ov-meta").textContent = `${quoted}/${rows.length} quoted`;
  $("#ov-title").textContent = state.sector ? SECTOR_LABEL[state.sector] : "All commodities";
}

function updateSortIndicators() {
  document.querySelectorAll("thead th").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.k === state.sort.key) {
      th.classList.add(state.sort.dir === 1 ? "sort-asc" : "sort-desc");
    }
  });
}

document.addEventListener("click", (e) => {
  const th = e.target.closest("thead th");
  if (!th) return;
  const k = th.dataset.k;
  if (!k) return;
  if (state.sort.key === k) {
    // cycle: desc -> asc -> back to sector grouping
    state.sort = state.sort.dir === -1 ? { key: k, dir: 1 } : { key: "sector", dir: 1 };
  } else {
    state.sort = { key: k, dir: -1 };
  }
  renderGrid();
});

// ---------- detail ----------
function showDetail(id) {
  state.view = "detail";
  state.detailId = id;
  swapViews("detail");
  document.title = `${id.toUpperCase()} — Fincept Commodities`;
  const panel = $("#detail");
  panel.replaceChildren();

  const back = el("button", { class: "back", text: "← Back" });
  back.onclick = () => { location.hash = ""; };
  panel.appendChild(el("div", { class: "detail-head", id: "d-head" }, [
    back,
    el("span", { class: "d-sym", text: id }),
    el("span", { class: "d-name", id: "d-name" }),
    el("span", { class: "d-price", id: "d-price" }),
    el("span", { class: "d-unit", id: "d-unit" }),
    el("span", { class: "d-chg", id: "d-chg" }),
  ]));

  const cards = el("div", { class: "cards" });
  const keys = ["price", "curve", "calspread", "risk", "season", "bands", "cot"];
  if (INVENTORY_IDS.has(id)) keys.push("inv");
  if (BALANCE_IDS.has(id)) keys.push("balance");
  if (ENERGY_CONTEXT_IDS.has(id)) keys.push("context");
  for (const key of keys) {
    cards.appendChild(el("div", { class: "card", id: "card-" + key },
      [el("h3", { text: CARD_TITLES[key] }), el("div", { class: "skel" })]));
  }
  panel.appendChild(cards);

  const row = state.rows.find(r => r.id === id);
  if (row) fillHeaderFromRow(row);
  loadPanels(id, false);
}

function fillHeaderFromRow(row) {
  byId("d-name").textContent = row.name;
  byId("d-price").textContent = fmtNum(row.price, priceDigits(row.price));
  byId("d-unit").textContent = row.quote_unit || "";
  const c = byId("d-chg");
  c.className = "d-chg " + dirClass(row.change_percent);
  c.textContent = (row.change_percent === null || row.change_percent === undefined) ? ""
    : `${sign(row.change)}${fmtNum(row.change, priceDigits(row.price))} (${sign(row.change_percent)}${fmtNum(row.change_percent)}%)`;
}

function fillHeaderFromSpec(d) {
  if (d.name && !byId("d-name").textContent) byId("d-name").textContent = d.name;
  if (d.contract && !byId("d-contract")) {
    byId("d-head").appendChild(el("span", {
      class: "d-tag", id: "d-contract",
      text: `${d.exchange} · ${d.contract.size} ${d.contract.size_unit}`,
    }));
  }
}

function loadPanels(id, fresh) {
  fetchPanel(id, "history", renderHistoryPanels, fresh);
  fetchPanel(id, "curve", renderCurvePanel, fresh);
  fetchPanel(id, "seasonality", renderSeasonPanels, fresh);
  fetchPanel(id, "cot", renderCotPanel, fresh);
  if (INVENTORY_IDS.has(id)) fetchPanel(id, "inventory", renderInventoryPanel, fresh);
  if (BALANCE_IDS.has(id)) fetchPanel(id, "balance", renderBalancePanel, fresh);
  if (ENERGY_CONTEXT_IDS.has(id)) fetchContextPanel(id, fresh);
}

async function fetchPanel(id, panel, render, fresh) {
  try {
    const d = await fetchJSON(`/api/commodity/${encodeURIComponent(id)}/${panel}` + (fresh ? "?fresh=1" : ""));
    if (state.view !== "detail" || state.detailId !== id) return;
    render(d);
  } catch (e) {
    if (state.view !== "detail" || state.detailId !== id) return;
    for (const key of PANEL_CARDS[panel]) {
      cardError(key, e.message, () => retryPanel(id, panel, render));
    }
  }
}

async function fetchContextPanel(id, fresh) {
  try {
    const d = await fetchJSON(`/api/energy-chemicals/product/${encodeURIComponent(id)}` + (fresh ? "?fresh=1" : ""));
    if (state.view !== "detail" || state.detailId !== id) return;
    renderContextPanel(d);
  } catch (e) {
    if (state.view !== "detail" || state.detailId !== id) return;
    cardError("context", e.message, () => fetchContextPanel(id, true));
  }
}

function retryPanel(id, panel, render) {
  for (const key of PANEL_CARDS[panel]) setCardLoading(key);
  fetchPanel(id, panel, render, true);
}

function setCard(key, meta, children) {
  const c = byId("card-" + key);
  if (!c) return;
  const h = el("h3", { text: CARD_TITLES[key] });
  if (meta) h.appendChild(el("span", { class: "h-meta", text: meta }));
  c.replaceChildren(h, ...[].concat(children).filter(Boolean));
}

function setCardLoading(key) {
  setCard(key, null, el("div", { class: "skel" }));
}

function cardError(key, msg, retryFn) {
  const children = [el("div", { class: "err", text: msg || "unavailable" })];
  if (retryFn) {
    const b = el("button", { class: "retry", text: "retry" });
    b.onclick = retryFn;
    children.push(b);
  }
  setCard(key, null, children);
}

function renderHistoryPanels(d) {
  fillHeaderFromSpec(d);
  const hist = cleanSeries(d.history, h => h.close);
  if (hist.length < 2) {
    cardError("price", d.history_error || "no history", () => retryPanel(state.detailId, "history", renderHistoryPanels));
  } else {
    const closes = hist.map(h => h.close);
    const digits = priceDigits(closes[closes.length - 1]);
    const chart = svgLine(closes, { area: true });
    const wrap = withHover(chart, closes.length, (i) => `${hist[i].date} · ${fmtNum(closes[i], digits)}`);
    setCard("price", `${hist[0].date} → ${hist[hist.length - 1].date}`, wrap);
    if (!byId("d-price").textContent) {
      const last = closes[closes.length - 1], prev = closes[closes.length - 2];
      const chg = last - prev, pct = prev ? chg / prev * 100 : 0;
      byId("d-price").textContent = fmtNum(last, digits);
      byId("d-unit").textContent = d.quote_unit || "";
      const c = byId("d-chg");
      c.className = "d-chg " + dirClass(chg);
      c.textContent = `${sign(chg)}${fmtNum(chg, digits)} (${sign(pct)}${fmtNum(pct)}%)`;
    }
  }

  const r = d.risk;
  if (!r || !r.success) {
    cardError("risk", r && r.error, () => retryPanel(state.detailId, "history", renderHistoryPanels));
    return;
  }
  const v = r.volatility, var_ = r.value_at_risk, dd = r.max_drawdown, mo = r.moments || {};
  const body = [el("div", { class: "stat-grid", html:
    stat("Total return", `${sign(r.return_total_pct)}${fmtNum(r.return_total_pct)}%`, dirClass(r.return_total_pct)) +
    stat("Ann. volatility", `${fmtNum(v.annualized_pct)}%`) +
    stat("VaR 95% (1d)", `${fmtNum(var_.var_95_daily_pct)}%`, "down") +
    stat("CVaR 95% (1d)", `${fmtNum(var_.cvar_95_daily_pct)}%`, "down") +
    stat("Max drawdown", `${fmtNum(dd.depth_pct)}%`, "down") +
    stat("Skew", fmtNum(mo.skewness)),
  })];
  const dds = cleanSeries(r.drawdown_series, p => p.value);
  if (dds.length > 2) {
    body.push(el("div", { class: "note", text: "drawdown · trailing 1y (%)" }));
    const chart = svgLine(dds.map(p => p.value), { area: true, h: 64 });
    body.push(withHover(chart, dds.length, (i) => `${dds[i].date} · ${fmtNum(dds[i].value, 1)}%`, { h: 64 }));
  }
  setCard("risk", `max DD ${dd.peak_date} → ${dd.trough_date}`, body);
}

function renderCurvePanel(d) {
  const ts = d.term_structure;
  if (ts && ts.success) {
    let tag = byId("d-struct");
    if (!tag) {
      tag = el("span", { class: "d-tag", id: "d-struct" });
      byId("d-head").appendChild(tag);
    }
    tag.setAttribute("class", "d-tag " + ts.market_structure);
    tag.textContent = ts.market_structure;
  }
  const c = cleanSeries(d.curve, x => x.price);
  if (c.length < 2) {
    cardError("curve", (ts && ts.error) || "no curve data", () => retryPanel(state.detailId, "curve", renderCurvePanel));
    return;
  }
  const prices = c.map(x => x.price);
  const digits = priceDigits(prices[0]);
  const chart = svgLine(prices, { dots: true, xlabels: c.map(x => x.expiry.slice(2)) });
  const body = [withHover(chart, prices.length, (i) => `${c[i].expiry} · ${fmtNum(prices[i], digits)}`)];
  if (ts && ts.success) {
    const roll = ts.front_roll_yield_annualized_pct;
    body.push(el("div", { class: "stat-grid", html:
      stat("Structure", ts.market_structure,
        ts.market_structure === "backwardation" ? "up" : ts.market_structure === "contango" ? "down" : "flat") +
      stat("Front→back", `${sign(ts.front_to_back_pct)}${fmtNum(ts.front_to_back_pct)}%`) +
      stat("Roll yield (ann)", `${sign(roll)}${fmtNum(roll)}%`, dirClass(roll)) +
      stat("Front→12m (ann)", `${sign(ts.front_to_12m_annualized_pct)}${fmtNum(ts.front_to_12m_annualized_pct)}%`),
    }));
  }
  setCard("curve", ts && ts.success ? `${ts.contracts_used} contracts` : null, body);
  renderCalspreadPanel(d);
}

// Calendar spreads ride on the curve fetch (d.calendar_spreads, d.quote_unit).
function renderCalspreadPanel(d) {
  const cs = d.calendar_spreads;
  if (!cs || !cs.success) {
    cardError("calspread", (cs && cs.error) || "no curve data",
      () => retryPanel(state.detailId, "curve", renderCurvePanel));
    return;
  }
  const unit = d.quote_unit || "";
  const p = cs.prompt_spread || {};
  const struct = cs.structure;
  const structCls = struct === "backwardation" ? "up" : struct === "contango" ? "down" : "flat";
  const body = [el("div", { class: "spread-cur " + structCls, html:
    `${sign(p.spread)}${fmtNum(p.spread, 2)} <span class="spread-unit">${unit} · M1−M2 · ${struct}</span>` })];
  const ladder = cs.ladder || [];
  if (ladder.length >= 2) {
    const bars = svgBars(ladder.map(L => L.spread), { labels: ladder.map(L => L.bucket_months + "m"), h: 92 });
    const wrap = withHover(bars, ladder.length, (i) => {
      const L = ladder[i];
      return `M1 vs +${L.bucket_months}mo (${L.far}) · ${sign(L.spread)}${fmtNum(L.spread, 2)} ${unit} · ann ${sign(L.annualized_roll_yield_pct)}${fmtNum(L.annualized_roll_yield_pct, 1)}%`;
    }, { band: true, h: 92 });
    body.push(el("div", { class: "note", text: `front spread vs deferred tenors (${unit}) · +ve = backwardation` }));
    body.push(wrap);
  }
  const b2b = cs.front_to_back || {};
  body.push(el("div", { class: "stat-grid", html:
    stat("Prompt M1−M2", `${sign(p.spread)}${fmtNum(p.spread, 2)}`, structCls) +
    stat("Roll yield (ann)", `${sign(p.annualized_roll_yield_pct)}${fmtNum(p.annualized_roll_yield_pct, 1)}%`, dirClass(p.annualized_roll_yield_pct)) +
    stat("Front→back", `${sign(b2b.spread)}${fmtNum(b2b.spread, 2)}`, dirClass(b2b.spread)) +
    stat("Structure", struct || "—", structCls),
  }));
  setCard("calspread", cs.contracts_used ? `${cs.contracts_used} contracts · ${p.near}→${p.far}` : null, body);
}

function renderSeasonPanels(d) {
  const s = d.seasonality;
  if (!s || !s.success) {
    cardError("season", s && s.error, () => retryPanel(state.detailId, "seasonality", renderSeasonPanels));
    cardError("bands", s && s.error);
    return;
  }
  const ms = s.monthly_stats || [];
  const chart = svgBars(ms.map(m => m.avg_return_pct), { labels: MONTHS.map(m => m[0]) });
  const wrap = withHover(chart, 12, (i) => {
    const m = ms[i];
    if (!m || m.avg_return_pct === null || m.avg_return_pct === undefined) return `${MONTHS[i]} · no data`;
    return `${MONTHS[i]} · avg ${sign(m.avg_return_pct)}${fmtNum(m.avg_return_pct)}% · win ${fmtNum(m.win_rate_pct, 0)}% · ${m.years_observed}y`;
  }, { band: true });
  const body = [wrap, el("div", { class: "stat-grid", html:
    stat("Best months", (s.best_months || []).join(", "), "up") +
    stat("Worst months", (s.worst_months || []).join(", "), "down"),
  })];
  const st = s.seasonal_strength;
  if (st && st.test) body.push(el("div", { class: "note", text: `${st.interpretation} (p=${fmtNum(st.p_value, 2)})` }));
  setCard("season", `${fmtNum(s.window_years, 0)}y window`, body);

  const sb = s.seasonal_bands;
  const rows = (sb && sb.bands) || [];
  if (rows.filter(r => r.hist_min !== null && r.hist_min !== undefined).length < 2) {
    cardError("bands", "not enough history for bands");
    return;
  }
  const chart2 = svgBands(rows, { h: 130 });
  const wrap2 = withHover(chart2, 12, (i) => {
    const b = rows[i];
    if (!b || b.hist_min === null || b.hist_min === undefined) return `${MONTHS[i]} · no band`;
    const dg = Math.abs(b.hist_avg) < 10 ? 3 : 1;
    let txt = `${MONTHS[i]} · ${fmtNum(b.hist_min, dg)}–${fmtNum(b.hist_max, dg)} · avg ${fmtNum(b.hist_avg, dg)}`;
    if (b.current_year !== null && b.current_year !== undefined) txt += ` · now ${fmtNum(b.current_year, dg)}`;
    return txt;
  }, { h: 130 });
  setCard("bands", `${sb.years_in_band}y range · ${sb.current_year} overlay`, [
    wrap2,
    el("div", { class: "note", text: "grey band = historical monthly range · grey line = avg · amber = current year" }),
  ]);
}

function renderCotPanel(d) {
  const p = d.positioning;
  if (!p || !p.success) {
    cardError("cot", p && p.error, () => retryPanel(state.detailId, "cot", renderCotPanel));
    return;
  }
  const cats = p.categories || {};
  const spec = cats.managed_money || cats.noncommercial;
  if (!spec) { cardError("cot", "no speculator data"); return; }
  const specLabel = cats.managed_money ? "managed money" : "non-commercial";
  const hedger = cats.producer_merchant || cats.commercial;
  const oi = p.open_interest;
  const idx = spec.cot_index;

  const body = [el("div", { class: "stat-grid", html:
    stat("Spec net", fmtVol(spec.net), dirClass(spec.net)) +
    stat("Spec Δ 1w", `${sign(spec.net_change_1w)}${fmtVol(spec.net_change_1w)}`, dirClass(spec.net_change_1w)) +
    stat("Commercials net", hedger ? fmtVol(hedger.net) : "—", hedger ? dirClass(hedger.net) : "") +
    stat("Open interest", oi ? fmtVol(oi.current) : "—"),
  })];
  body.push(el("div", { html:
    `<div class="meter"><div class="fill" style="width:${Math.max(0, Math.min(100, idx))}%"></div></div>` +
    `<div class="meter-scale"><span>0 bearish</span><span>COT index ${fmtNum(idx, 0)}${spec.extreme ? " · " + spec.extreme.replace("_", " ") : ""}</span><span>100 bullish</span></div>`,
  }));
  const ns = cleanSeries(spec.net_series, q => q.value);
  if (ns.length > 2) {
    body.push(el("div", { class: "note", text: `${specLabel} net · 52w` }));
    const chart = svgLine(ns.map(q => q.value), { area: true, h: 64 });
    body.push(withHover(chart, ns.length, (i) => `${ns[i].date} · net ${fmtVol(ns[i].value)}`, { h: 64 }));
  }
  if (p.divergence_signal) body.push(el("div", { class: "divergence", text: "⚠ " + p.divergence_signal }));
  setCard("cot", `${specLabel} · as of ${p.as_of || "—"}`, body);
}

function renderInventoryPanel(d) {
  const inv = d.inventory;
  if (!inv || !inv.success) {
    cardError("inv", inv && inv.error, () => retryPanel(state.detailId, "inventory", renderInventoryPanel));
    return;
  }
  const band = inv.five_year_band;
  const streak = inv.streak || {};
  const chg = inv.last_change;
  const hasChg = chg !== null && chg !== undefined;
  const lvlDigits = Math.abs(inv.current_level) >= 1000 ? 0 : 1;
  // draws (falling stocks) are price-bullish -> green; builds -> red
  const body = [el("div", { class: "stat-grid", html:
    stat("Level", `${fmtNum(inv.current_level, lvlDigits)} ${inv.unit}`) +
    stat("Wkly change", hasChg
      ? `${sign(chg)}${fmtNum(chg, 1)} (${streak.periods}w ${streak.direction})` : "—",
      hasChg ? (chg < 0 ? "up" : chg > 0 ? "down" : "flat") : "") +
    stat("vs 5y avg", band && band.vs_avg_pct !== null && band.vs_avg_pct !== undefined
      ? `${sign(band.vs_avg_pct)}${fmtNum(band.vs_avg_pct, 1)}%` : "—",
      band && band.vs_avg_pct ? dirClass(-band.vs_avg_pct) : "") +
    stat("5y range", band ? band.position.replace(/_/g, " ") : "—"),
  })];
  const sc = inv.seasonal_chart || {};
  const weeks = sc.weeks || [];
  const rows = weeks.map(w => ({ month: String(w.week), hist_min: w.hist_min,
    hist_max: w.hist_max, hist_avg: w.hist_avg, current_year: w.current_year }));
  if (rows.filter(r => r.hist_min !== null && r.hist_min !== undefined).length >= 2) {
    const chart = svgBands(rows, { h: 130,
      labels: rows.map((r, i) => String(i + 1)), labelStep: 9 });
    const wrap = withHover(chart, rows.length, (i) => {
      const b = rows[i];
      if (!b || b.hist_min === null || b.hist_min === undefined) return `wk ${i + 1} · no band`;
      let txt = `wk ${i + 1} · ${fmtNum(b.hist_min, 0)}–${fmtNum(b.hist_max, 0)} · avg ${fmtNum(b.hist_avg, 0)}`;
      if (b.current_year !== null && b.current_year !== undefined) txt += ` · now ${fmtNum(b.current_year, 0)}`;
      return txt;
    }, { h: 130 });
    body.push(el("div", { class: "note",
      text: `${inv.series_label} (${inv.unit}) · grey = 5y weekly range · amber = ${sc.current_year}` }));
    body.push(wrap);
  }
  setCard("inv", `${inv.series_label || "EIA"} · as of ${inv.as_of}`, body);
}

function renderBalancePanel(d) {
  const bal = d.balance;
  if (!bal || !bal.success) {
    cardError("balance", (bal && bal.error) || "no balance data",
      () => retryPanel(state.detailId, "balance", renderBalancePanel));
    return;
  }
  const cmap = {};
  for (const c of bal.components || []) cmap[c.id] = c;
  const cushing = cmap.cushing;
  const body = [];
  if (cushing && cushing.current !== undefined && cushing.current !== null) {
    const posCls = cushing.position === "below_5yr_range" ? "up"
      : cushing.position === "above_5yr_range" ? "down" : "flat";
    body.push(el("div", { class: "spread-cur", html:
      `${fmtNum(cushing.current, 1)} <span class="spread-unit">${cushing.unit} Cushing · </span>` +
      `<span class="${posCls}">${(cushing.position || "").replace(/_/g, " ") || "—"}</span>` }));
    if (cushing.note) body.push(el("div", { class: "note", text: cushing.note }));
  }
  const ch = bal.cushing && bal.cushing.seasonal_chart;
  const weeks = (ch && ch.weeks) || [];
  const rows = weeks.map(w => ({ month: String(w.week), hist_min: w.hist_min,
    hist_max: w.hist_max, hist_avg: w.hist_avg, current_year: w.current_year }));
  if (rows.filter(r => r.hist_min !== null && r.hist_min !== undefined).length >= 2) {
    const chart = svgBands(rows, { h: 120, labels: rows.map((r, i) => String(i + 1)), labelStep: 9 });
    const wrap = withHover(chart, rows.length, (i) => {
      const b = rows[i];
      if (!b || b.hist_min === null || b.hist_min === undefined) return `wk ${i + 1} · no band`;
      let txt = `wk ${i + 1} · ${fmtNum(b.hist_min, 1)}–${fmtNum(b.hist_max, 1)} · avg ${fmtNum(b.hist_avg, 1)}`;
      if (b.current_year !== null && b.current_year !== undefined) txt += ` · now ${fmtNum(b.current_year, 1)}`;
      return txt;
    }, { h: 120 });
    body.push(el("div", { class: "note",
      text: `Cushing stocks (${cushing ? cushing.unit : "MMbbl"}) · grey = 5y weekly range · amber = ${ch.current_year}` }));
    body.push(wrap);
  }
  const order = ["production", "imports", "runs", "utilization", "exports", "spr"];
  const cells = order.map(id => cmap[id]).filter(Boolean).map(balanceCell).join("");
  if (cells) {
    body.push(el("div", { class: "note", text: "supply · refinery demand · trade — colour = crude-price read" }));
    body.push(el("div", { class: "bal-grid", html: cells }));
  }
  setCard("balance", `US balance · as of ${bal.as_of || "—"}`, body);
}

// crude-price read of a component's latest weekly move (draws/exports/runs up = bullish)
function balanceChangeClass(comp) {
  const ch = comp.last_change;
  if (ch === null || ch === undefined || !comp.bullish) return "flat";
  if (comp.bullish === "low") return ch < 0 ? "up" : ch > 0 ? "down" : "flat";
  return ch > 0 ? "up" : ch < 0 ? "down" : "flat";
}

function balanceCell(comp) {
  if (comp.error || comp.current === undefined || comp.current === null) {
    return `<div class="bal-cell"><span class="bal-name">${comp.short}</span>` +
      `<span class="bal-val na">—</span></div>`;
  }
  const dg = comp.unit === "%" ? 1 : (Math.abs(comp.current) >= 100 ? 0 : 1);
  const chg = comp.last_change;
  const chgTxt = (chg === null || chg === undefined) ? ""
    : `${sign(chg)}${fmtNum(chg, Math.abs(chg) < 1 ? 2 : 1)}`;
  const vs = (comp.vs_avg_pct === null || comp.vs_avg_pct === undefined) ? ""
    : `${sign(comp.vs_avg_pct)}${fmtNum(comp.vs_avg_pct, 0)}% vs5y`;
  const sub = [chgTxt, vs].filter(Boolean).join(" · ");
  return `<div class="bal-cell">` +
    `<span class="bal-name">${comp.short}</span>` +
    `<span class="bal-val">${fmtNum(comp.current, dg)}<span class="bal-unit"> ${comp.unit}</span></span>` +
    `<span class="bal-sub ${balanceChangeClass(comp)}">${sub || "—"}</span></div>`;
}

function renderContextPanel(d) {
  if (!d || !d.success) {
    cardError("context", d && d.error, () => fetchContextPanel(state.detailId, true));
    return;
  }
  const p = d.product;
  const body = [el("div", { class: "product-role", text: p.role })];
  body.push(el("div", { class: "stat-grid", html:
    stat("Coverage", p.coverage) +
    stat("Screen", p.screen || "physical") +
    stat("Unit", p.unit || "-") +
    stat("Group", d.group ? d.group.name : p.group),
  }));
  if (p.trade_lens) body.push(el("div", { class: "trade-lens", text: p.trade_lens }));
  if (p.benchmarks && p.benchmarks.length) {
    body.push(el("div", { class: "note", text: "benchmarks" }));
    body.push(el("div", { class: "chip-row" },
      p.benchmarks.slice(0, 6).map(v => el("span", { class: "chip benchmark", text: v }))));
  }
  body.push(el("div", { class: "note", text: "drivers" }));
  body.push(el("div", { class: "chip-row" },
    (p.drivers || []).map(v => el("span", { class: "chip", text: v }))));
  body.push(el("div", { class: "note", text: "signals" }));
  body.push(el("div", { class: "chip-row" },
    (p.signals || []).map(v => el("span", { class: "chip alt", text: v }))));
  if (d.flows && d.flows.length) {
    body.push(el("div", { class: "flow-list" }, d.flows.slice(0, 6).map(f =>
      el("div", { class: "flow-row", text: `${f.from} -> ${f.to} · ${f.label}` }))));
  }
  if (p.watch_questions && p.watch_questions.length) {
    body.push(el("div", { class: "question-list" },
      p.watch_questions.slice(0, 3).map(q => el("div", { class: "question-row", text: q }))));
  }
  if (d.related && d.related.length) {
    body.push(el("div", { class: "note", text: "related products" }));
    body.push(el("div", { class: "chip-row" }, d.related.slice(0, 8).map(r => {
      const chip = el("button", { class: "chip chip-button", text: r.id });
      chip.onclick = () => {
        if (r.dashboard_id) location.hash = "#/" + r.dashboard_id;
        else {
          state.newsTopic = productTopic(r);
          state.newsProduct = r.id;
          state.newsAt = 0;
          location.hash = "#/news";
        }
      };
      return chip;
    })));
  }
  const actions = el("div", { class: "action-row" });
  const news = el("button", { class: "retry", text: "open related news" });
  news.onclick = () => {
    state.newsTopic = d.news_topic || "energy";
    state.newsProduct = p.id;
    state.newsAt = 0;
    location.hash = "#/news";
  };
  const hub = el("button", { class: "retry", text: "open energy hub" });
  hub.onclick = () => { location.hash = "#/energy-hub"; };
  actions.appendChild(news);
  actions.appendChild(hub);
  body.push(actions);
  setCard("context", p.name, body);
}

// ---------- spreads ----------
async function loadSpreads(fresh) {
  const box = $("#spreads-cards");
  if (!fresh && state.spreadsAt && Date.now() - state.spreadsAt < 60000 && box.childElementCount) return;
  box.replaceChildren(el("div", { class: "loading", text: "Computing spreads…" }));
  $("#refresh").classList.add("spin");
  try {
    const data = await fetchJSON("/api/spreads" + (fresh ? "?fresh=1" : ""));
    state.spreadsAt = Date.now();
    if (state.view !== "spreads") return;
    box.replaceChildren(...(data.spreads || []).map(spreadCard));
    $("#updated").textContent = "updated " + timeStr(data.updated);
  } catch (e) {
    box.replaceChildren(el("div", { class: "loading", text: "load error: " + e.message }));
  } finally {
    $("#refresh").classList.remove("spin");
  }
}

function spreadCard(sp) {
  const card = el("div", { class: "card" });
  const h = el("h3", { text: sp.title });
  h.appendChild(el("span", { class: "h-meta", text: sp.unit }));
  card.appendChild(h);
  card.appendChild(el("div", { class: "note", text: sp.note }));
  const r = sp.result;
  if (!r || !r.success) {
    card.appendChild(el("div", { class: "err", text: (r && r.error) || "unavailable" }));
    return card;
  }
  card.appendChild(el("div", { class: "spread-cur", html:
    `${fmtNum(r.current, 2)} <span class="spread-unit">${sp.unit}</span>` }));
  const ser = cleanSeries(r.series, q => q.value);
  if (ser.length > 2) {
    const chart = svgLine(ser.map(q => q.value), { area: true, h: 90 });
    card.appendChild(withHover(chart, ser.length, (i) => `${ser[i].date} · ${fmtNum(ser[i].value, 2)}`, { h: 90 }));
  }
  const hl = r.mean_reversion_half_life_days;
  card.appendChild(el("div", { class: "stat-grid", html:
    stat("1y mean", fmtNum(r.mean, 2)) +
    stat("Z-score (60d)", r.zscore_current === null || r.zscore_current === undefined
      ? "—" : `${sign(r.zscore_current)}${fmtNum(r.zscore_current, 2)}`) +
    stat("Percentile (1y)", `${fmtNum(r.percentile_of_current, 0)}th`) +
    stat("Mean-rev half-life", hl ? `${fmtNum(hl, 0)}d` : "—"),
  }));
  return card;
}

// ---------- energy hub ----------
async function loadEnergyHub(fresh) {
  const box = $("#energy-hub-body");
  if (!fresh && state.energyHubAt && Date.now() - state.energyHubAt < 3600000 && box.childElementCount) return;
  box.replaceChildren(el("div", { class: "loading", text: "Loading energy products..." }));
  $("#refresh").classList.add("spin");
  try {
    const data = await fetchJSON("/api/energy-chemicals" + (fresh ? "?fresh=1" : ""));
    state.energyHubAt = Date.now();
    state.energyHubData = data;
    if (state.view !== "energy-hub") return;
    renderEnergyHub(data);
    $("#updated").textContent = "updated " + timeStr(data.updated);
  } catch (e) {
    box.replaceChildren(el("div", { class: "loading", text: "load error: " + e.message }));
  } finally {
    $("#refresh").classList.remove("spin");
  }
}

function renderEnergyHub(data) {
  const box = $("#energy-hub-body");
  const cov = data.coverage || {};
  $("#eh-meta").textContent = `${cov.live_contracts || 0} live screens · ${cov.physical_products || 0} physical products`;
  const filter = state.energyFilter.trim().toLowerCase();
  const filteredGroups = (data.groups || []).map(group => {
    const products = (group.products || []).filter(p => productMatches(p, filter));
    return Object.assign({}, group, { products });
  }).filter(group => group.products.length || !filter);
  const toolbar = el("div", { class: "hub-toolbar" }, [
    el("div", { class: "hub-summary" }, [
      metricBox("Live screens", cov.live_contracts || 0),
      metricBox("Physical products", cov.physical_products || 0),
      metricBox("Total coverage", cov.total_products || 0),
    ]),
    energyFilterInput(),
  ]);
  const groups = el("div", { class: "product-groups" },
    filteredGroups.map(group => productGroup(group)));
  const flows = el("div", { class: "hub-section" }, [
    el("h3", { text: "Chain map" }),
    el("div", { class: "chain-grid" }, (data.flows || []).slice(0, 18).map(flow =>
      el("div", { class: "chain-edge" }, [
        el("span", { class: "chain-node", text: flow.from }),
        el("span", { class: "chain-arrow", text: "->" }),
        el("span", { class: "chain-node", text: flow.to }),
        el("span", { class: "chain-label", text: flow.label }),
      ]))),
  ]);
  const calendar = el("div", { class: "hub-section" }, [
    el("h3", { text: "Event calendar" }),
    el("div", { class: "event-list" }, (data.calendar || []).map(ev =>
      el("div", { class: "event-row" }, [
        el("span", { class: "event-time", text: `${ev.day} ${ev.time}` }),
        el("span", { class: "event-name", text: ev.name }),
        el("span", { class: "event-use", text: ev.use }),
      ]))),
  ]);
  box.replaceChildren(toolbar, groups, flows, calendar);
}

function productMatches(p, filter) {
  if (!filter) return true;
  const hay = [
    p.id, p.name, p.group, p.coverage, p.screen, p.role, p.trade_lens,
    ...(p.drivers || []), ...(p.signals || []), ...(p.benchmarks || []),
  ].join(" ").toLowerCase();
  return hay.includes(filter);
}

function energyFilterInput() {
  const input = el("input", {
    id: "energy-filter",
    type: "text",
    placeholder: "filter product, benchmark, driver...",
    value: state.energyFilter,
    autocomplete: "off",
    spellcheck: "false",
  });
  input.oninput = (e) => {
    state.energyFilter = e.target.value || "";
    if (state.energyHubData) renderEnergyHub(state.energyHubData);
    const next = byId("energy-filter");
    if (next) {
      next.focus();
      next.setSelectionRange(next.value.length, next.value.length);
    }
  };
  return input;
}

function metricBox(label, value) {
  return el("div", { class: "metric-box" }, [
    el("span", { class: "metric-value", text: String(value) }),
    el("span", { class: "metric-label", text: label }),
  ]);
}

function productGroup(group) {
  const node = el("div", { class: "product-group" });
  node.appendChild(el("h3", { text: group.name }));
  const rows = el("div", { class: "product-table" });
  for (const p of group.products || []) rows.appendChild(productRow(p));
  node.appendChild(rows);
  return node;
}

function productRow(p) {
  const row = el("div", { class: "product-row" });
  const name = el("button", { class: "product-name", text: p.name });
  name.onclick = () => {
    if (p.dashboard_id) location.hash = "#/" + p.dashboard_id;
    else {
      state.newsTopic = productTopic(p);
      state.newsProduct = p.id;
      state.newsAt = 0;
      location.hash = "#/news";
    }
  };
  row.appendChild(name);
  row.appendChild(el("span", { class: "coverage-badge " + coverageClass(p.coverage), text: p.coverage }));
  row.appendChild(el("span", { class: "product-screen", text: p.screen || "" }));
  row.appendChild(el("span", { class: "product-lens-line", text: p.trade_lens || p.role }));
  row.appendChild(el("span", { class: "product-question-line", text: (p.watch_questions || [])[0] || "" }));
  return row;
}

function coverageClass(value) {
  const v = (value || "").toLowerCase();
  if (v.includes("live")) return "live";
  if (v.includes("physical")) return "physical";
  return "external";
}

function productTopic(p) {
  if (p.group === "gas_lng_ngl") return p.id.includes("lpg") || p.id.includes("butane") ? "lpg_ngl" : "natgas_lng";
  if (p.group === "refined_products") return "refined_products";
  if (p.group === "olefins_aromatics" || p.group === "polymers_fertilizers") return "petrochemicals";
  if (p.group === "crude_feedstocks") return "crude";
  return "energy";
}

// ---------- news ----------
async function loadNews(fresh) {
  const box = $("#news-body");
  const productParam = state.newsProduct ? `&product=${encodeURIComponent(state.newsProduct)}` : "";
  if (!fresh && state.newsAt && Date.now() - state.newsAt < 300000 && box.childElementCount) return;
  box.replaceChildren(el("div", { class: "loading", text: "Loading headlines..." }));
  $("#refresh").classList.add("spin");
  try {
    const data = await fetchJSON(`/api/news?topic=${encodeURIComponent(state.newsTopic)}&limit=50${productParam}` + (fresh ? "&fresh=1" : ""));
    state.newsAt = Date.now();
    state.newsData = data;
    if (state.view !== "news") return;
    renderNews(data);
    $("#updated").textContent = "updated " + timeStr(data.updated);
  } catch (e) {
    box.replaceChildren(el("div", { class: "loading", text: "load error: " + e.message }));
  } finally {
    $("#refresh").classList.remove("spin");
  }
}

function renderNews(data) {
  renderNewsTopics(data.topics || []);
  const filter = state.newsFilter.trim().toLowerCase();
  const articles = (data.articles || []).filter(item => newsMatches(item, filter));
  const briefs = (data.briefs || []).filter(item => newsMatches(item, filter));
  const productLabel = data.product ? ` · ${data.product}` : "";
  $("#news-meta").textContent = `${articles.length}/${data.available || articles.length} headlines${productLabel}`;
  const status = data.source_status || {};
  const toolbar = el("div", { class: "news-toolbar" }, [
    metricBox("Live headlines", data.available || 0),
    metricBox("Sources ok", status.ok || 0),
    metricBox("Watch briefs", (data.briefs || []).length),
    newsFilterInput(),
  ]);
  const sourceLine = el("div", { class: "source-line" }, (data.sources || []).map(s =>
    el("span", { class: "source-pill " + (s.ok ? "ok" : "bad"),
      title: s.error || "",
      text: s.ok ? `${s.source} ${s.count}` : `${s.source} error` })));
  const list = el("div", { class: "news-list" });
  if (!articles.length) {
    list.appendChild(el("div", { class: "data-empty", text: status.degraded
      ? "Live sources unavailable in this environment; watch briefs below remain available."
      : "No live headlines matched this filter." }));
  } else {
    for (const article of articles) list.appendChild(newsItem(article));
  }
  const briefBlock = el("div", { class: "brief-block" }, [
    el("div", { class: "brief-head", text: "Watch briefs" }),
    el("div", { class: "news-list brief-list" }, briefs.length
      ? briefs.map(newsItem)
      : [el("div", { class: "data-empty", text: "No watch briefs matched this filter." })]),
  ]);
  $("#news-body").replaceChildren(toolbar, sourceLine, list, briefBlock);
}

function newsMatches(item, filter) {
  if (!filter) return true;
  const hay = [
    item.title, item.summary, item.source, item.market_bias, item.priority_label,
    ...(item.products || []), ...(item.matched_terms || []),
  ].join(" ").toLowerCase();
  return hay.includes(filter);
}

function newsFilterInput() {
  const input = el("input", {
    id: "news-filter",
    type: "text",
    placeholder: "filter headline, source, product...",
    value: state.newsFilter,
    autocomplete: "off",
    spellcheck: "false",
  });
  input.oninput = (e) => {
    state.newsFilter = e.target.value || "";
    if (state.newsData) renderNews(state.newsData);
    const next = byId("news-filter");
    if (next) {
      next.focus();
      next.setSelectionRange(next.value.length, next.value.length);
    }
  };
  return input;
}

function renderNewsTopics(topics) {
  const box = $("#news-topics");
  box.replaceChildren(...topics.map(topic => {
    const b = el("button", { text: topic.name, "data-topic": topic.id });
    b.classList.toggle("active", topic.id === state.newsTopic && !state.newsProduct);
    b.onclick = () => {
      state.newsTopic = topic.id;
      state.newsProduct = null;
      state.newsAt = 0;
      loadNews(true);
    };
    return b;
  }));
  if (state.newsProduct) {
    const b = el("button", { class: "active product-topic", text: state.newsProduct });
    b.onclick = () => {
      state.newsProduct = null;
      state.newsAt = 0;
      loadNews(true);
    };
    box.appendChild(b);
  }
}

function newsItem(article) {
  const item = el("article", { class: "news-item " + (article.market_bias || "neutral") });
  const top = el("div", { class: "news-item-top" }, [
    el("span", { class: "news-source", text: article.source || "News" }),
    el("span", { class: "news-age", text: article.is_brief ? "brief" : ageStr(article.published) }),
    el("span", { class: "priority-tag " + (article.priority_label || "low"), text: article.priority_label || "low" }),
    el("span", { class: "bias-tag", text: article.market_bias || "neutral" }),
  ]);
  const title = article.url
    ? el("a", { class: "news-title", text: article.title || "(untitled)", href: article.url, target: "_blank", rel: "noopener" })
    : el("div", { class: "news-title", text: article.title || "(untitled)" });
  item.appendChild(top);
  item.appendChild(title);
  if (article.summary) item.appendChild(el("p", { text: article.summary }));
  if (article.products && article.products.length) {
    item.appendChild(el("div", { class: "chip-row" },
      article.products.slice(0, 6).map(p => el("span", { class: "chip", text: p }))));
  }
  if (article.matched_terms && article.matched_terms.length) {
    item.appendChild(el("div", { class: "chip-row" },
      article.matched_terms.slice(0, 8).map(t => el("span", { class: "chip benchmark", text: t }))));
  }
  return item;
}

function ageStr(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))}m`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h`;
  return `${Math.round(diff / 86400)}d`;
}

// ---------- LPG trader workspace ----------
function lpgPayload(data) {
  if (data && data.data && typeof data.data === "object" && !Array.isArray(data.data)) return data.data;
  return data || {};
}

function lpgList(data, keys) {
  if (Array.isArray(data)) return data;
  const src = lpgPayload(data);
  for (const key of keys) if (Array.isArray(src[key])) return src[key];
  return [];
}

function lpgValue(obj, ...keys) {
  for (const key of keys) {
    if (obj && obj[key] !== null && obj[key] !== undefined && obj[key] !== "") return obj[key];
  }
  return null;
}

function lpgNumber(value, digits = 2) {
  const num = Number(value);
  if (value === null || value === undefined || value === "" || !Number.isFinite(num)) return "N/A";
  return num.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function lpgDate(value, withTime = false) {
  if (value === null || value === undefined || value === "") return "N/A";
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  let raw = value;
  if (typeof raw === "number" && raw < 1e12) raw *= 1000;
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return String(value);
  return withTime
    ? d.toLocaleString("en-SG", { hour12: false, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("en-CA");
}

function lpgStateInfo(value, stale = false) {
  const key = String(value || "unknown").toLowerCase().replace(/[\s-]+/g, "_");
  if (stale || key.includes("stale")) return { cls: "stale", label: "Stale / 已过期" };
  if (["entitled", "active", "ok", "success", "succeeded", "completed", "available", "fresh", "configured"].includes(key)) {
    return { cls: "ok", label: key === "entitled" ? "Entitled / 已授权" : "Active / 正常" };
  }
  if (["unentitled", "denied", "forbidden", "unauthorized", "retired"].includes(key)) {
    return { cls: "bad", label: key === "retired" ? "Retired / 已停用" : "Unentitled / 未授权" };
  }
  if (["pending", "pending_review", "queued", "running", "in_progress", "partial", "deferred", "no_data", "not_configured"].includes(key)) {
    if (key === "partial") return { cls: "warn", label: "Partial / 部分完成" };
    if (key === "deferred") return { cls: "warn", label: "Deferred / 已延后" };
    if (key === "no_data") return { cls: "warn", label: "No data / 暂无数据" };
    if (key === "not_configured") return { cls: "warn", label: "Not configured / 未配置" };
    return { cls: "warn", label: key.includes("pending") ? "Pending / 待确认" : "Running / 刷新中" };
  }
  if (["error", "failed", "degraded", "blocked"].includes(key)) return { cls: "bad", label: "Error / 异常" };
  return { cls: "neutral", label: value ? String(value) : "Unknown / 未知" };
}

function lpgBadge(value, stale = false, title = "") {
  const info = lpgStateInfo(value, stale);
  return el("span", { class: `lpg-badge ${info.cls}`, text: info.label, title });
}

function lpgIsStale(item) {
  if (!item) return false;
  if (item.stale === true || item.is_stale === true) return true;
  const freshness = item.freshness;
  if (typeof freshness === "string") return freshness.toLowerCase().includes("stale");
  if (freshness && typeof freshness === "object") {
    return freshness.stale === true || String(freshness.status || "").toLowerCase().includes("stale");
  }
  return false;
}

function lpgEntitlement(item) {
  return lpgValue(item, "entitlement_state", "entitlement", "access_status", "status") || "unknown";
}

function lpgIsActive(item) {
  const value = item && item.active;
  return value !== false && value !== 0 && String(value).toLowerCase() !== "false";
}

function lpgSeriesAllowed(series) {
  return String(lpgEntitlement(series)).toLowerCase() === "entitled" && lpgIsActive(series);
}

function lpgNewsAllowed(article) {
  const stateName = String(lpgEntitlement(article)).toLowerCase();
  return ["entitled", "public"].includes(stateName) && lpgIsActive(article);
}

function lpgSeriesKey(item) {
  const value = lpgValue(item, "series_id", "id");
  return value === null ? "" : String(value);
}

function lpgAllowedSeriesIds(catalog) {
  return new Set(lpgList(catalog || {}, ["items", "series"]).filter(lpgSeriesAllowed).map(lpgSeriesKey).filter(Boolean));
}

function lpgEmpty(title, detail = "") {
  const box = el("div", { class: "lpg-empty" }, [
    el("strong", { text: title }),
  ]);
  if (detail) box.appendChild(el("span", { text: detail }));
  return box;
}

function lpgSection(title, meta, children, cls = "") {
  const head = el("div", { class: "lpg-section-head" }, [el("h3", { text: title })]);
  if (meta) head.appendChild(el("span", { class: "meta", text: meta }));
  return el("section", { class: `lpg-section ${cls}`.trim() }, [head, ...[].concat(children || []).filter(Boolean)]);
}

function lpgTable(columns, rows, options = {}) {
  if (!rows.length) return lpgEmpty(options.empty || "No data / 暂无数据", options.detail || "");
  const table = el("table", { class: "lpg-table" });
  const headRow = el("tr");
  for (const col of columns) headRow.appendChild(el("th", { class: col.class || "", text: col.label }));
  table.appendChild(el("thead", {}, headRow));
  const body = el("tbody");
  for (const row of rows) {
    const clickable = Boolean(options.onRow && (!options.isRowClickable || options.isRowClickable(row)));
    const tr = el("tr", clickable ? { tabindex: "0", class: "lpg-clickable" } : {});
    for (const col of columns) {
      const value = col.render ? col.render(row) : lpgValue(row, col.key);
      const td = el("td", { class: col.class || "" });
      if (value instanceof Node) td.appendChild(value);
      else td.textContent = value === null || value === undefined || value === "" ? "N/A" : String(value);
      tr.appendChild(td);
    }
    if (clickable) {
      const open = () => options.onRow(row);
      tr.onclick = open;
      tr.onkeydown = event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
      };
    }
    body.appendChild(tr);
  }
  table.appendChild(body);
  return el("div", { class: "lpg-table-wrap" }, table);
}

function lpgGenericTable(data, options = {}) {
  const src = lpgPayload(data);
  const rawColumns = options.columns || src.columns || [];
  let rows = options.rows || src.rows || [];
  let keys = rawColumns.map(col => typeof col === "string" ? col : (col.key || col.name || col.id)).filter(Boolean);
  if (!keys.length && rows.length && !Array.isArray(rows[0])) keys = Object.keys(rows[0]);
  if (rows.length && Array.isArray(rows[0])) {
    rows = rows.map(values => Object.fromEntries(keys.map((key, index) => [key, values[index]])));
  }
  const columns = keys.map((key, index) => {
    const def = rawColumns[index];
    const label = typeof def === "object" && def ? (def.label || def.title || key) : key;
    return { key, label: String(label).replace(/_/g, " "), render: row => lpgGenericValue(row[key]) };
  });
  return lpgTable(columns, rows, { empty: options.empty || "No rows / 暂无记录" });
}

function lpgGenericValue(value) {
  if (value === null || value === undefined || value === "") return "N/A";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return value.toLocaleString("en-US", { maximumFractionDigits: 4 });
  if (typeof value === "object") {
    const text = JSON.stringify(value);
    return text.length > 180 ? text.slice(0, 177) + "..." : text;
  }
  return String(value);
}

function renderLpgShell() {
  const tabs = byId("lpg-tabs");
  const buttons = LPG_VIEWS.map(item => {
    const button = el("button", {
      id: `lpg-tab-${item.id}`,
      type: "button",
      role: "tab",
      text: item.label,
      tabindex: item.id === state.lpgView ? "0" : "-1",
      "aria-controls": "lpg-body",
      "aria-selected": String(item.id === state.lpgView),
      class: item.id === state.lpgView ? "active" : "",
    });
    button.onclick = () => { location.hash = item.id === "cockpit" ? "#/lpg" : `#/lpg/${item.id}`; };
    return button;
  });
  tabs.replaceChildren(...buttons);
  buttons.forEach((button, index) => {
    button.onkeydown = event => {
      let next = null;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % buttons.length;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = buttons.length - 1;
      if (next === null) return;
      event.preventDefault();
      buttons[next].focus();
      buttons[next].click();
    };
  });
  const body = byId("lpg-body");
  body.setAttribute("aria-labelledby", `lpg-tab-${state.lpgView}`);

  const asOf = el("input", { type: "date", value: state.lpgAsOf, "aria-label": "LPG as-of date", title: "As-of date / 估值日期" });
  asOf.onchange = event => {
    state.lpgAsOf = event.target.value || "";
    state.lpgData = {};
    loadLpgView(state.lpgView);
    updateLpgExportLinks();
  };
  const scope = el("select", { id: "lpg-refresh-scope", "aria-label": "LPG refresh scope", title: "Refresh scope / 刷新范围" }, [
    el("option", { value: "all", text: "All sources" }),
    el("option", { value: "asia", text: "Asia close" }),
    el("option", { value: "overnight", text: "Overnight / US" }),
  ]);
  const refresh = el("button", { id: "lpg-refresh", type: "button", class: "lpg-command", text: "Refresh", title: "Start LPG source refresh / 启动数据刷新" });
  refresh.onclick = () => startLpgRefresh(scope.value);
  const csv = el("a", { id: "lpg-export-csv", class: "lpg-export", text: "CSV", title: "Export current LPG view as CSV" });
  const xlsx = el("a", { id: "lpg-export-xlsx", class: "lpg-export", text: "XLSX", title: "Export current LPG view as XLSX" });
  const job = el("span", {
    id: "lpg-refresh-state",
    class: "lpg-job-state",
    text: state.lpgRefreshJob ? (state.lpgRefreshJob.label || "Refresh running / 刷新中") : "",
  });
  byId("lpg-actions").replaceChildren(
    el("label", { class: "lpg-field", text: "As of" }, asOf),
    scope,
    refresh,
    el("span", { class: "lpg-export-group" }, [csv, xlsx]),
    job,
  );
  updateLpgExportLinks();
}

function updateLpgExportLinks() {
  const query = new URLSearchParams({ view: state.lpgView });
  if (state.lpgAsOf) query.set("as_of", state.lpgAsOf);
  if (state.lpgView === "history" && state.lpgSeriesId) query.set("series_id", state.lpgSeriesId);
  if (state.lpgView === "moc") query.set("dataset", state.lpgMocDataset);
  if (state.lpgView === "explorer") {
    query.set("dataset", state.lpgExplorer.dataset);
    if (state.lpgExplorer.query) query.set("q", state.lpgExplorer.query);
    if (state.lpgExplorer.entitlement !== "all") query.set("entitlement", state.lpgExplorer.entitlement);
  }
  if (state.lpgView === "news") {
    const filter = state.lpgNewsFilter;
    if (filter.query) query.set("q", filter.query);
    for (const key of ["region", "product", "direction"]) {
      if (filter[key] && filter[key] !== "all") query.set(key, filter[key]);
    }
  }
  const csv = byId("lpg-export-csv"), xlsx = byId("lpg-export-xlsx");
  if (csv) csv.href = `/api/lpg/export?${query.toString()}&format=csv`;
  if (xlsx) xlsx.href = `/api/lpg/export?${query.toString()}&format=xlsx`;
}

function lpgApiUrl(path, params = {}, options = {}) {
  const query = new URLSearchParams();
  const asOfParam = Object.prototype.hasOwnProperty.call(options, "asOf") ? options.asOf : "as_of";
  if (state.lpgAsOf && asOfParam) query.set(asOfParam, state.lpgAsOf);
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") query.set(key, value);
  }
  const qs = query.toString();
  return `/api/lpg/${path}${qs ? "?" + qs : ""}`;
}

function lpgEntitledCatalogUrl() {
  return lpgApiUrl("series", { entitlement_state: "entitled", active: "1", limit: 5000 }, { asOf: null });
}

function lpgExplorerAsOfParam(dataset) {
  return ["observations", "curves", "news", "dataset_rows"].includes(dataset) ? "end" : null;
}

async function loadLpgView(view) {
  if (view === "history") return loadLpgHistory();
  const token = ++state.lpgRequestToken;
  const body = byId("lpg-body");
  body.replaceChildren(el("div", { class: "loading", text: "Loading LPG data / 正在加载..." }));
  byId("refresh").classList.add("spin");
  try {
    let payload;
    if (view === "cockpit") {
      const [summary, catalog] = await Promise.all([
        fetchJSON(lpgApiUrl("summary")), fetchJSON(lpgEntitledCatalogUrl()),
      ]);
      payload = { ...lpgPayload(summary), access_catalog: lpgPayload(catalog) };
    }
    else if (view === "curves") {
      const [curves, spreads, catalog] = await Promise.all([
        fetchJSON(lpgApiUrl("curves")), fetchJSON(lpgApiUrl("spreads")), fetchJSON(lpgEntitledCatalogUrl()),
      ]);
      payload = { curves: lpgPayload(curves), spreads: lpgPayload(spreads), access_catalog: lpgPayload(catalog) };
    } else if (view === "moc") {
      const [data, catalog] = await Promise.all([
        fetchJSON(lpgApiUrl("explorer", { dataset: state.lpgMocDataset }, { asOf: "end" })),
        fetchJSON(lpgEntitledCatalogUrl()),
      ]);
      payload = { ...lpgPayload(data), access_catalog: lpgPayload(catalog) };
    }
    else if (view === "news") payload = await fetchJSON(lpgApiUrl("news", {}, { asOf: "end" }));
    else if (view === "explorer") {
      const request = fetchJSON(lpgApiUrl("explorer", {
        dataset: state.lpgExplorer.dataset,
        q: state.lpgExplorer.query,
        entitlement: state.lpgExplorer.entitlement === "all" ? "" : state.lpgExplorer.entitlement,
        offset: state.lpgExplorer.offset,
        limit: state.lpgExplorer.limit,
      }, { asOf: lpgExplorerAsOfParam(state.lpgExplorer.dataset) }));
      const needsCatalog = ["observations", "curves", "revisions"].includes(state.lpgExplorer.dataset);
      const [data, catalog] = await Promise.all([request, needsCatalog ? fetchJSON(lpgEntitledCatalogUrl()) : Promise.resolve(null)]);
      payload = { ...lpgPayload(data), access_catalog: catalog ? lpgPayload(catalog) : null };
    } else payload = await fetchJSON(lpgApiUrl("status", {}, { asOf: null }));
    if (token !== state.lpgRequestToken || state.view !== "lpg" || state.lpgView !== view) return;
    state.lpgData[view] = lpgPayload(payload);
    if (view === "cockpit") renderLpgCockpit(state.lpgData[view]);
    else if (view === "curves") renderLpgCurves(state.lpgData[view]);
    else if (view === "moc") renderLpgMoc(state.lpgData[view]);
    else if (view === "news") renderLpgNews(state.lpgData[view]);
    else if (view === "explorer") renderLpgExplorer(state.lpgData[view]);
    else renderLpgStatus(state.lpgData[view]);
    updateLpgMeta(state.lpgData[view]);
  } catch (error) {
    if (token !== state.lpgRequestToken) return;
    body.replaceChildren(lpgEmpty("LPG data unavailable / 数据暂不可用", error.message));
    byId("lpg-meta").textContent = `Load error / 加载失败: ${error.message}`;
  } finally {
    if (token === state.lpgRequestToken && !(state.lpgRefreshJob && state.lpgRefreshJob.running)) byId("refresh").classList.remove("spin");
  }
}

function updateLpgMeta(data) {
  const src = data && data.curves ? data.curves : lpgPayload(data);
  const asOf = lpgValue(src, "as_of", "observation_date");
  const updated = lpgValue(src, "updated_at", "updated", "fetched_at");
  const bits = [];
  if (asOf) bits.push(`As of ${lpgDate(asOf)}`);
  if (updated) bits.push(`Updated ${lpgDate(updated, true)}`);
  byId("lpg-meta").textContent = bits.length ? bits.join(" | ") : "Platts entitlement-aware workspace";
  if (updated) byId("updated").textContent = "updated " + lpgDate(updated, true);
}

function lpgPriceMetric(price) {
  const name = lpgValue(price, "name", "canonical_key", "symbol", "series_id") || "LPG series";
  const value = lpgValue(price, "value", "price", "last");
  const unit = [price.currency, price.unit].filter(Boolean).join("/");
  const button = el("button", {
    type: "button",
    class: "lpg-metric",
    title: price.series_id ? "Open history / 查看历史" : name,
  }, [
    el("span", { class: "lpg-metric-label", text: name }),
    el("span", { class: "lpg-metric-value", text: `${lpgNumber(value, Math.abs(Number(value)) < 10 ? 3 : 2)}${unit ? " " + unit : ""}` }),
    el("span", { class: "lpg-metric-meta", text: `${lpgDate(lpgValue(price, "observation_date", "date"))} | ${price.source || "source N/A"}` }),
    lpgBadge(lpgEntitlement(price), lpgIsStale(price)),
  ]);
  if (price.series_id || price.id) {
    button.onclick = () => {
      state.lpgSeriesId = price.series_id || price.id;
      location.hash = "#/lpg/history";
    };
  } else button.disabled = true;
  return button;
}

function lpgSpreadColumns() {
  return [
    { label: "Spread", render: row => lpgValue(row, "name", "id") },
    { label: "Value", class: "num", render: row => row.success === false ? "Blocked" : `${lpgNumber(lpgValue(row, "value", "spread"), 2)} ${[row.currency, row.unit].filter(Boolean).join("/")}`.trim() },
    { label: "As of", render: row => lpgDate(lpgValue(row, "observation_date", "date", "as_of")) },
    { label: "Z-score", class: "num", render: row => lpgNumber(row.zscore, 2) },
    { label: "Percentile", class: "num", render: row => row.percentile === null || row.percentile === undefined ? "N/A" : `${lpgNumber(Number(row.percentile) <= 1 ? Number(row.percentile) * 100 : row.percentile, 0)}%` },
    { label: "Legs / State", render: row => row.blocked_reason ? `Blocked / 已阻断: ${row.blocked_reason}` : `${(row.legs || []).length} legs | Matched / 日期已对齐` },
  ];
}

function lpgSpreadAllowed(row, allowedIds) {
  if (row.success === false) return true;
  const legs = Array.isArray(row.legs) ? row.legs : [];
  return legs.length > 0 && legs.every(leg => {
    const id = lpgSeriesKey(leg);
    return id && allowedIds.has(id);
  });
}

function renderLpgCockpit(data) {
  const allowedIds = lpgAllowedSeriesIds(data.access_catalog);
  const prices = lpgList(data, ["prices", "benchmarks", "items"])
    .filter(price => lpgSeriesAllowed(price) && allowedIds.has(lpgSeriesKey(price)));
  const spreads = lpgList(data, ["spreads"]).filter(row => lpgSpreadAllowed(row, allowedIds));
  const sources = lpgList(data, ["source_status", "sources"]);
  const body = byId("lpg-body");
  const priceGrid = prices.length
    ? el("div", { class: "lpg-metric-grid" }, prices.map(lpgPriceMetric))
    : lpgEmpty("No entitled price assessments / 暂无已授权价格", "Use Data Status to review entitlement discovery and source health.");
  const sourceStrip = sources.length ? el("div", { class: "lpg-source-strip" }, sources.map(source => {
    const status = lpgValue(source, "status", "state") || "unknown";
    return el("div", { class: "lpg-source-item" }, [
      el("strong", { text: lpgValue(source, "source", "name") || "Source" }),
      lpgBadge(status, lpgIsStale(source), source.error || ""),
      el("span", { text: lpgDate(lpgValue(source, "last_success_at", "updated_at"), true) }),
    ]);
  })) : lpgEmpty("Source status pending / 数据源状态待确认");
  body.replaceChildren(
    lpgSection("Key Assessments", `${prices.length} series`, priceGrid),
    lpgSection("Trading Spreads & Arb Signals", `${spreads.length} calculations`, lpgTable(lpgSpreadColumns(), spreads, {
      empty: "No valid spread calculations / 暂无有效价差",
      detail: "A stale or mismatched leg is blocked rather than silently combined.",
    })),
    lpgSection("Source Health", "entitlement and freshness", sourceStrip),
  );
}

function renderLpgCurves(payload) {
  const curvesData = lpgPayload(payload.curves || {});
  const spreadsData = lpgPayload(payload.spreads || {});
  const allowedIds = lpgAllowedSeriesIds(payload.access_catalog);
  const curves = lpgList(curvesData, ["curves", "items"])
    .filter(curve => allowedIds.has(lpgSeriesKey(curve)));
  const spreads = lpgList(spreadsData, ["items", "spreads"]).filter(row => lpgSpreadAllowed(row, allowedIds));
  const curveList = el("div", { class: "lpg-curve-list" });
  for (const curve of curves) {
    const points = lpgList(curve, ["points", "contracts"]).filter(point => {
      const access = lpgValue(point, "entitlement_state", "entitlement", "access_status");
      return Number.isFinite(Number(lpgValue(point, "value", "price")))
        && (!access || String(access).toLowerCase() === "entitled") && lpgIsActive(point);
    });
    const values = points.map(point => Number(lpgValue(point, "value", "price")));
    const labels = points.map(point => lpgValue(point, "contract_month", "tenor", "delivery_start") || "");
    const chart = values.length > 1
      ? withHover(svgLine(values, { h: 150, dots: true, xlabels: labels }), values.length,
        index => `${labels[index]} | ${lpgNumber(values[index], 2)} ${curve.currency || ""}/${curve.unit || ""}`, { h: 150 })
      : lpgEmpty("Curve needs at least two points / 曲线点不足");
    const table = lpgTable([
      { label: "Contract", render: row => lpgValue(row, "contract_month", "tenor", "delivery_start") },
      { label: "Delivery", render: row => [row.delivery_start, row.delivery_end].filter(Boolean).map(lpgDate).join(" to ") || "N/A" },
      { label: "Value", class: "num", render: row => `${lpgNumber(lpgValue(row, "value", "price"), 2)} ${lpgValue(row, "currency") || curve.currency || ""}/${lpgValue(row, "unit") || curve.unit || ""}` },
      { label: "As of", render: row => lpgDate(lpgValue(row, "as_of_date", "observation_date", "date", "as_of") || curve.as_of_date || curvesData.as_of) },
      { label: "State", render: row => lpgBadge(lpgValue(row, "entitlement_state", "entitlement", "access_status") || lpgValue(curve, "entitlement_state", "entitlement") || "entitled", lpgIsStale(row) || lpgIsStale(curve)) },
    ], points, { empty: "No entitled curve points / 暂无已授权曲线点" });
    curveList.appendChild(el("article", { class: "lpg-curve-block" }, [
      el("div", { class: "lpg-curve-title" }, [
        el("strong", { text: lpgValue(curve, "name", "canonical_key", "series_id") || "Forward curve" }),
        el("span", { class: "meta", text: `${points.length} points | ${curve.currency || "N/A"}/${curve.unit || "N/A"}` }),
      ]),
      el("div", { class: "lpg-curve-layout" }, [chart, table]),
    ]));
  }
  if (!curves.length) curveList.appendChild(lpgEmpty("No forward curves / 暂无远期曲线", "Review entitlements in Data Status or select a different as-of date."));
  byId("lpg-body").replaceChildren(
    lpgSection("Forward Curves", `${curves.length} curves`, curveList),
    lpgSection("Calendar, Feedstock & Arb Spreads", `${spreads.length} calculations`, lpgTable(lpgSpreadColumns(), spreads, {
      empty: "No spreads for this as-of date / 当前日期暂无价差",
    })),
  );
}

async function loadLpgHistory() {
  const token = ++state.lpgRequestToken;
  byId("lpg-body").replaceChildren(el("div", { class: "loading", text: "Loading series history / 正在加载历史..." }));
  byId("refresh").classList.add("spin");
  try {
    const catalogRaw = await fetchJSON(lpgApiUrl("series", { limit: 5000 }, { asOf: null }));
    const catalog = lpgPayload(catalogRaw);
    const series = lpgList(catalog, ["items", "series"]);
    const current = series.find(item => String(item.id || item.series_id) === String(state.lpgSeriesId));
    if (!current || !lpgSeriesAllowed(current)) {
      const first = series.find(lpgSeriesAllowed);
      state.lpgSeriesId = first ? (first.id || first.series_id) : null;
    }
    updateLpgExportLinks();
    let history = null, historyError = "";
    if (state.lpgSeriesId) {
      try {
        history = lpgPayload(await fetchJSON(lpgApiUrl(
          `series/${encodeURIComponent(state.lpgSeriesId)}/history`, {}, { asOf: "end" },
        )));
      } catch (error) { historyError = error.message; }
    }
    if (token !== state.lpgRequestToken || state.view !== "lpg" || state.lpgView !== "history") return;
    const payload = { catalog, history, historyError };
    state.lpgData.history = payload;
    renderLpgHistory(payload);
    updateLpgMeta(history || catalog);
  } catch (error) {
    if (token === state.lpgRequestToken) byId("lpg-body").replaceChildren(lpgEmpty("History unavailable / 历史数据不可用", error.message));
  } finally {
    if (token === state.lpgRequestToken && !(state.lpgRefreshJob && state.lpgRefreshJob.running)) byId("refresh").classList.remove("spin");
  }
}

function lpgMean(values) {
  const nums = values.filter(value => Number.isFinite(value));
  return nums.length ? nums.reduce((sum, value) => sum + value, 0) / nums.length : null;
}

function lpgSeasonality(observations) {
  const monthly = new Map();
  for (const row of observations) {
    const date = new Date(row.date);
    if (Number.isNaN(date.getTime())) continue;
    const key = `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}`;
    if (!monthly.has(key)) monthly.set(key, []);
    monthly.get(key).push(row.value);
  }
  const averages = [...monthly.entries()].map(([key, values]) => ({ key, value: lpgMean(values) })).sort((a, b) => a.key.localeCompare(b.key));
  const returns = Array.from({ length: 12 }, () => []);
  for (let index = 1; index < averages.length; index++) {
    const prev = averages[index - 1], cur = averages[index];
    const prevDate = new Date(prev.key + "-01T00:00:00Z"), curDate = new Date(cur.key + "-01T00:00:00Z");
    const monthGap = (curDate.getUTCFullYear() - prevDate.getUTCFullYear()) * 12 + curDate.getUTCMonth() - prevDate.getUTCMonth();
    if (monthGap === 1 && prev.value) returns[curDate.getUTCMonth()].push((cur.value / prev.value - 1) * 100);
  }
  return returns.map((values, month) => ({ month: MONTHS[month], value: lpgMean(values), samples: values.length }));
}

function renderLpgHistory(payload) {
  const series = lpgList(payload.catalog, ["items", "series"]);
  const selected = series.find(item => String(item.id || item.series_id) === String(state.lpgSeriesId));
  const entitled = series.filter(lpgSeriesAllowed);
  const select = el("select", { "aria-label": "Select LPG series" });
  if (!entitled.length) select.appendChild(el("option", { value: "", text: "No entitled series / 无已授权系列" }));
  for (const item of entitled) {
    const option = el("option", {
      value: item.id || item.series_id,
      text: `${lpgValue(item, "name", "canonical_key", "symbol", "id")} (${item.unit || "N/A"})`,
    });
    option.selected = String(item.id || item.series_id) === String(state.lpgSeriesId);
    select.appendChild(option);
  }
  select.onchange = event => {
    state.lpgSeriesId = event.target.value || null;
    updateLpgExportLinks();
    loadLpgHistory();
  };
  const search = el("input", {
    type: "search", value: state.lpgSeriesQuery, placeholder: "Filter catalog...", "aria-label": "Filter LPG series catalog",
  });
  search.oninput = event => {
    state.lpgSeriesQuery = event.target.value || "";
    renderLpgHistory(state.lpgData.history);
    const next = document.querySelector(".lpg-series-search");
    if (next) { next.focus(); next.setSelectionRange(next.value.length, next.value.length); }
  };
  search.className = "lpg-series-search";
  const toolbar = el("div", { class: "lpg-toolbar" }, [
    el("label", { class: "lpg-field", text: "Series" }, select),
    search,
  ]);
  const content = [toolbar];
  const historySeries = payload.history && (payload.history.series || selected);
  const historyAuthorized = Boolean(selected && lpgSeriesAllowed(selected)
    && (!historySeries || lpgSeriesAllowed(historySeries)));
  const history = historyAuthorized ? payload.history : null;
  const historyError = payload.history && !historyAuthorized
    ? "History blocked because the series is not both entitled and active."
    : payload.historyError;
  if (!history) {
    content.push(lpgEmpty(historyError ? "Series history unavailable / 历史不可用" : "Select an entitled series / 请选择已授权系列", historyError));
  } else {
    const observations = lpgList(history, ["observations", "history", "items"])
      .map(row => ({ raw: row, date: lpgValue(row, "date", "observation_date"), value: Number(lpgValue(row, "value", "price", "close")) }))
      .filter(row => row.date && Number.isFinite(row.value)).sort((a, b) => String(a.date).localeCompare(String(b.date)));
    const values = observations.map(row => row.value);
    const latest = values.length ? values[values.length - 1] : null;
    const stats = history.statistics || {};
    const statGrid = el("div", { class: "lpg-stat-strip" }, [
      lpgStat("Latest", lpgNumber(lpgValue(stats, "current", "latest", "last") ?? latest, 2)),
      lpgStat("Average", lpgNumber(lpgValue(stats, "average", "mean") ?? lpgMean(values), 2)),
      lpgStat("Low", lpgNumber(lpgValue(stats, "minimum", "min") ?? (values.length ? Math.min(...values) : null), 2)),
      lpgStat("High", lpgNumber(lpgValue(stats, "maximum", "max") ?? (values.length ? Math.max(...values) : null), 2)),
      lpgStat("Observations", values.length.toLocaleString()),
    ]);
    const chart = values.length > 1
      ? withHover(svgLine(values, { h: 190, area: true }), values.length,
        index => `${observations[index].date} | ${lpgNumber(values[index], 3)}`, { h: 190 })
      : lpgEmpty("Insufficient history / 历史样本不足");
    const serverSeasonality = history.seasonality || {};
    const serverMonthly = lpgList(serverSeasonality, ["monthly_stats"]);
    const fallbackSeason = lpgSeasonality(observations);
    const byMonth = new Map(serverMonthly.map(row => [
      Number(row.month_number) || MONTHS.indexOf(row.month) + 1,
      {
        month: row.month || MONTHS[(Number(row.month_number) || 1) - 1],
        value: lpgValue(row, "avg_return_pct", "value"),
        median: lpgValue(row, "median_return_pct", "median"),
        winRate: lpgValue(row, "win_rate_pct", "win_rate"),
        samples: lpgValue(row, "years_observed", "samples") || 0,
      },
    ]));
    const season = serverMonthly.length
      ? MONTHS.map((month, index) => byMonth.get(index + 1) || { month, value: null, median: null, winRate: null, samples: 0 })
      : fallbackSeason;
    const seasonValues = season.map(row => row.value);
    const seasonChart = season.some(row => row.value !== null)
      ? withHover(svgBars(seasonValues, { h: 150, labels: MONTHS.map(month => month[0]) }), 12,
        index => `${MONTHS[index]} | ${lpgNumber(season[index].value, 2)}% | ${season[index].samples} samples`, { h: 150, band: true })
      : lpgEmpty("Seasonality needs consecutive monthly history / 月度样本不足");
    const historySections = [
      lpgSection("Price History", `${selected ? selected.name || selected.canonical_key : "Series"} | ${history.series?.currency || selected?.currency || "N/A"}/${history.series?.unit || selected?.unit || "N/A"}`, [statGrid, el("div", { class: "lpg-history-chart" }, chart)]),
      lpgSection("Seasonality", serverMonthly.length ? "server-calculated month-end returns" : "average month-on-month return", [seasonChart, lpgTable([
        { label: "Month", render: row => row.month },
        { label: "Avg return", class: "num", render: row => row.value === null ? "N/A" : `${lpgNumber(row.value, 2)}%` },
        { label: "Median", class: "num", render: row => row.median === null || row.median === undefined ? "N/A" : `${lpgNumber(row.median, 2)}%` },
        { label: "Win rate", class: "num", render: row => row.winRate === null || row.winRate === undefined ? "N/A" : `${lpgNumber(row.winRate, 1)}%` },
        { label: "Samples", class: "num", render: row => row.samples },
      ], season)]),
      lpgSection("Recent Assessments", `latest ${Math.min(100, observations.length)} rows`, lpgTable([
        { label: "Date", render: row => lpgDate(row.date) },
        { label: "Value", class: "num", render: row => lpgNumber(row.value, 3) },
        { label: "Bate", render: row => row.raw.bate || "N/A" },
        { label: "Published", render: row => lpgDate(row.raw.publication_time, true) },
        { label: "Revisions", class: "num", render: row => row.raw.revision_count || 0 },
      ], observations.slice(-100).reverse())),
    ];
    const rawBands = lpgList(serverSeasonality, ["bands"]);
    if (rawBands.length) {
      const bandByMonth = new Map(rawBands.map(row => [Number(row.month_number) || MONTHS.indexOf(row.month) + 1, row]));
      const bands = MONTHS.map((month, index) => bandByMonth.get(index + 1) || {
        month, hist_min: null, hist_max: null, hist_avg: null, current_year: null, years_in_band: 0,
      });
      const hasBands = bands.some(row => row.hist_min !== null && row.hist_max !== null);
      if (hasBands) {
        const bandChart = withHover(svgBands(bands, { h: 160, labels: MONTHS.map(month => month[0]) }), 12,
          index => `${MONTHS[index]} | avg ${lpgNumber(bands[index].hist_avg, 2)} | range ${lpgNumber(bands[index].hist_min, 2)}-${lpgNumber(bands[index].hist_max, 2)}`,
          { h: 160, band: true });
        historySections.splice(2, 0, lpgSection("Seasonal Price Bands", `${serverSeasonality.years || 0} years`, [
          el("div", { class: "lpg-history-chart" }, bandChart),
        ]));
      }
    }
    content.push(...historySections);
  }
  const query = state.lpgSeriesQuery.trim().toLowerCase();
  const visible = series.filter(item => !query || [item.id, item.symbol, item.name, item.product, item.market, item.location, item.canonical_key].join(" ").toLowerCase().includes(query));
  content.push(lpgSection("Series Catalog", `${visible.length}/${series.length} series`, lpgTable([
    { label: "Series", render: row => lpgValue(row, "name", "canonical_key", "symbol", "id") },
    { label: "Market", render: row => [row.product, row.market, row.location].filter(Boolean).join(" | ") || "N/A" },
    { label: "Basis", render: row => row.basis || "N/A" },
    { label: "Unit", render: row => [row.currency, row.unit].filter(Boolean).join("/") || "N/A" },
    { label: "Coverage", render: row => [row.first_date, row.last_date].filter(Boolean).map(lpgDate).join(" to ") || "N/A" },
    { label: "Access", render: row => lpgBadge(lpgEntitlement(row), lpgIsStale(row)) },
  ], visible, {
    empty: "No catalog matches / 没有匹配系列",
    onRow: row => { if (lpgSeriesAllowed(row)) { state.lpgSeriesId = row.id || row.series_id; loadLpgHistory(); } },
    isRowClickable: lpgSeriesAllowed,
  })));
  byId("lpg-body").replaceChildren(...content);
}

function lpgStat(label, value) {
  return el("div", { class: "lpg-stat" }, [el("span", { text: label }), el("strong", { text: String(value) })]);
}

function renderLpgMoc(data) {
  const rows = lpgList(data, ["rows", "items", "trades", "moc"]);
  const fundamentals = lpgList(data, ["fundamentals", "balances", "flows"]);
  const select = el("select", { "aria-label": "MOC and fundamentals dataset" }, [
    el("option", { value: "moc", text: "eWindow / MOC" }),
    el("option", { value: "fundamentals", text: "Fundamentals" }),
    el("option", { value: "all", text: "All observations" }),
  ]);
  select.value = state.lpgMocDataset;
  select.onchange = event => { state.lpgMocDataset = event.target.value; loadLpgView("moc"); };
  const toolbar = el("div", { class: "lpg-toolbar" }, [el("label", { class: "lpg-field", text: "Dataset" }, select)]);
  const sections = [toolbar];
  if (rows.length) sections.push(lpgSection("Market on Close & eWindow", `${rows.length} records`, lpgGenericTable(data, { rows })));
  if (fundamentals.length) sections.push(lpgSection("Physical Fundamentals", `${fundamentals.length} records`, lpgGenericTable({}, { rows: fundamentals })));
  if (!rows.length && !fundamentals.length) sections.push(lpgEmpty(
    "No MOC or fundamentals records / 暂无MOC或基本面数据",
    "The view remains available while entitlement discovery or Excel ingestion is pending.",
  ));
  byId("lpg-body").replaceChildren(...sections);
}

function lpgOptionValues(items, key) {
  return [...new Set(items.map(item => item[key]).filter(Boolean).map(String))].sort((a, b) => a.localeCompare(b));
}

function lpgFilterSelect(label, value, values, onChange) {
  const select = el("select", { "aria-label": label });
  select.appendChild(el("option", { value: "all", text: `${label}: All` }));
  for (const item of values) select.appendChild(el("option", { value: item, text: item }));
  select.value = values.includes(value) ? value : "all";
  select.onchange = event => onChange(event.target.value);
  return select;
}

function renderLpgNews(data) {
  const items = lpgList(data, ["items", "articles", "news"]);
  const filter = state.lpgNewsFilter;
  const query = filter.query.trim().toLowerCase();
  const visible = items.filter(item => {
    const hay = [item.headline, item.title, item.summary, item.source, item.region, item.product, item.direction, ...(item.tags || [])].join(" ").toLowerCase();
    return (!query || hay.includes(query))
      && (filter.region === "all" || String(item.region) === filter.region)
      && (filter.product === "all" || String(item.product) === filter.product)
      && (filter.direction === "all" || String(item.direction) === filter.direction);
  });
  const search = el("input", { type: "search", value: filter.query, placeholder: "Search headline, market, tag...", "aria-label": "Filter LPG news" });
  search.oninput = event => {
    state.lpgNewsFilter.query = event.target.value || "";
    updateLpgExportLinks();
    renderLpgNews(state.lpgData.news);
    const next = document.querySelector(".lpg-news-search");
    if (next) { next.focus(); next.setSelectionRange(next.value.length, next.value.length); }
  };
  search.className = "lpg-news-search";
  const rerender = (key, value) => {
    state.lpgNewsFilter[key] = value;
    updateLpgExportLinks();
    renderLpgNews(state.lpgData.news);
  };
  const toolbar = el("div", { class: "lpg-toolbar lpg-news-toolbar" }, [
    search,
    lpgFilterSelect("Region", filter.region, lpgOptionValues(items, "region"), value => rerender("region", value)),
    lpgFilterSelect("Product", filter.product, lpgOptionValues(items, "product"), value => rerender("product", value)),
    lpgFilterSelect("Direction", filter.direction, lpgOptionValues(items, "direction"), value => rerender("direction", value)),
  ]);
  const list = el("div", { class: "lpg-news-list" });
  if (!visible.length) list.appendChild(lpgEmpty(
    items.length ? "No headlines match / 没有匹配新闻" : "No licensed LPG headlines / 暂无授权LPG新闻",
    items.length ? "Adjust filters to widen the result." : "Public fallback and Platts news entitlement status are shown in Data Status.",
  ));
  for (const article of visible) {
    const title = article.url
      ? el("a", { class: "lpg-news-title", text: article.headline || article.title || "Untitled", href: article.url, target: "_blank", rel: "noopener" })
      : el("span", { class: "lpg-news-title", text: article.headline || article.title || "Untitled" });
    const tags = [article.region, article.product, ...(article.tags || [])].filter(Boolean);
    list.appendChild(el("article", { class: "lpg-news-row" }, [
      el("div", { class: "lpg-news-meta" }, [
        el("span", { class: "news-source", text: article.source || "News" }),
        el("span", { text: lpgDate(lpgValue(article, "published_at", "published"), true) }),
        lpgBadge(article.direction || "neutral", false),
        el("span", { class: `lpg-importance ${String(article.importance || "normal").toLowerCase()}`, text: article.importance || "normal" }),
      ]),
      title,
      article.summary ? el("p", { text: article.summary }) : null,
      tags.length ? el("div", { class: "chip-row" }, tags.slice(0, 10).map(tag => el("span", { class: "chip", text: tag }))) : null,
    ].filter(Boolean)));
  }
  byId("lpg-body").replaceChildren(
    toolbar,
    lpgSection("LPG News Flow", `${visible.length}/${data.total || items.length} headlines`, list),
  );
}

function renderLpgExplorer(data) {
  const explorer = state.lpgExplorer;
  const form = el("form", { class: "lpg-toolbar lpg-explorer-toolbar" });
  const dataset = el("select", { "aria-label": "Explorer dataset" });
  for (const item of ["series", "observations", "curves", "spreads", "news", "candidates", "revisions", "runs"]) {
    dataset.appendChild(el("option", { value: item, text: item[0].toUpperCase() + item.slice(1) }));
  }
  dataset.value = explorer.dataset;
  const query = el("input", { type: "search", value: explorer.query, placeholder: "Search dataset...", "aria-label": "Search LPG dataset" });
  const entitlement = el("select", { "aria-label": "Entitlement filter" }, [
    el("option", { value: "all", text: "All access states" }),
    el("option", { value: "entitled", text: "Entitled / 已授权" }),
    el("option", { value: "unentitled", text: "Unentitled / 未授权" }),
    el("option", { value: "pending_review", text: "Pending / 待确认" }),
    el("option", { value: "error", text: "Error / 异常" }),
  ]);
  entitlement.value = explorer.entitlement;
  const submit = el("button", { type: "submit", class: "lpg-command", text: "Apply" });
  form.append(dataset, query, entitlement, submit);
  form.onsubmit = event => {
    event.preventDefault();
    state.lpgExplorer = { ...explorer, dataset: dataset.value, query: query.value.trim(), entitlement: entitlement.value, offset: 0 };
    updateLpgExportLinks();
    loadLpgView("explorer");
  };
  const total = Number(data.total || (data.rows || []).length || 0);
  const offset = Number(data.offset ?? explorer.offset);
  const limit = Number(data.limit || explorer.limit);
  const pager = el("div", { class: "lpg-pager" }, [
    el("span", { text: total ? `${offset + 1}-${Math.min(offset + limit, total)} of ${total}` : "0 rows" }),
  ]);
  const prev = el("button", { type: "button", text: "Previous", disabled: offset <= 0 ? "disabled" : null });
  const next = el("button", { type: "button", text: "Next", disabled: offset + limit >= total ? "disabled" : null });
  prev.onclick = () => { state.lpgExplorer.offset = Math.max(0, offset - limit); loadLpgView("explorer"); };
  next.onclick = () => { state.lpgExplorer.offset = offset + limit; loadLpgView("explorer"); };
  pager.append(prev, next);
  byId("lpg-body").replaceChildren(
    form,
    lpgSection("Dataset Results", `${data.dataset || explorer.dataset} | ${total} rows`, [lpgGenericTable(data), pager]),
  );
}

function lpgRefreshRows(row) {
  if (!row || typeof row !== "object") return 0;
  const counts = row.counts || {};
  const direct = [
    row.rows_written, row.rows_imported, row.row_count,
    counts.rows_inserted, counts.rows_updated,
    row.rows_inserted, row.rows_updated,
  ].map(Number).filter(Number.isFinite);
  if (direct.length) return direct.reduce((sum, value) => sum + value, 0);
  for (const key of ["result", "import", "market", "news"]) {
    const nested = lpgRefreshRows(row[key]);
    if (nested) return nested;
  }
  return 0;
}

function renderLpgStatus(data) {
  const catalog = data.catalog || {};
  const states = catalog.states || catalog.entitlement_states || {};
  const sources = lpgList(data, ["sources", "source_status"]);
  const runs = lpgList(data, ["runs", "refresh_runs", "jobs"]);
  const candidates = data.candidates || {};
  const candidateStates = candidates.states || {};
  const matrix = lpgList(data, ["entitlement_matrix"]);
  const statStrip = el("div", { class: "lpg-stat-strip" }, [
    lpgStat("Candidates", Number(candidates.total || matrix.length || 0).toLocaleString()),
    lpgStat("Entitled", Number(states.entitled || candidateStates.entitled || 0).toLocaleString()),
    lpgStat("Unentitled", Number(states.unentitled || candidateStates.unentitled || 0).toLocaleString()),
    lpgStat("Pending review", Number(states.pending_review || candidateStates.pending_review || 0).toLocaleString()),
    lpgStat("Sources active", sources.filter(source => lpgStateInfo(source.status).cls === "ok").length.toString()),
  ]);
  const database = data.database;
  const databaseLine = database ? el("div", { class: "lpg-database-line" }, [
    el("strong", { text: "Local database" }),
    el("span", { text: typeof database === "string" ? database : lpgGenericValue(database) }),
  ]) : null;
  byId("lpg-body").replaceChildren(
    lpgSection("Coverage & Entitlements", `Updated ${lpgDate(data.updated_at, true)}`, [statStrip, databaseLine]),
    lpgSection("Entitlement Matrix", `${matrix.length} candidates`, lpgTable([
      { label: "Target", render: row => lpgValue(row, "name", "canonical_key", "candidate_id") },
      { label: "Symbol / Curve", render: row => lpgValue(row, "mapped_symbol", "discovered_symbol", "discovered_curve_code") || "Pending discovery" },
      { label: "Market", render: row => [row.product, row.market, row.location].filter(Boolean).join(" | ") || "N/A" },
      { label: "Access", render: row => lpgBadge(lpgValue(row, "discovery_status", "entitlement_state", "status")) },
      { label: "Coverage", render: row => [row.first_date, row.last_date].filter(Boolean).map(lpgDate).join(" to ") || "N/A" },
      { label: "Reason", render: row => row.error || row.entitlement_reason || "N/A" },
    ], matrix, { empty: "Entitlement discovery has not run / 尚未执行授权发现" })),
    lpgSection("Source Health", `${sources.length} sources`, lpgTable([
      { label: "Source", render: row => lpgValue(row, "source", "name") },
      { label: "State", render: row => lpgBadge(lpgValue(row, "status", "state"), lpgIsStale(row), row.error || "") },
      { label: "Last success", render: row => lpgDate(row.last_success_at, true) },
      { label: "Last attempt", render: row => lpgDate(row.last_attempt_at, true) },
      { label: "Message", render: row => row.error || row.message || "OK" },
    ], sources, { empty: "No source checks recorded / 暂无数据源检查" })),
    lpgSection("Refresh Runs", `${runs.length} recent runs`, lpgTable([
      { label: "Run", render: row => lpgValue(row, "id", "job_id", "run_id") },
      { label: "Scope", render: row => [row.source, row.scope].filter(Boolean).join(" / ") || "N/A" },
      { label: "State", render: row => lpgBadge(lpgValue(row, "status", "state")) },
      { label: "Started", render: row => lpgDate(row.started_at, true) },
      { label: "Finished", render: row => lpgDate(row.finished_at, true) },
      { label: "Rows", class: "num", render: row => lpgRefreshRows(row) },
      { label: "Message", render: row => row.error || row.message || "N/A" },
    ], runs, { empty: "No refresh runs recorded / 暂无刷新记录" })),
  );
}

function setLpgRefreshState(label, tone = "") {
  state.lpgRefreshJob = label ? { ...(state.lpgRefreshJob || {}), label } : null;
  const status = byId("lpg-refresh-state");
  if (status) { status.textContent = label || ""; status.className = `lpg-job-state ${tone}`.trim(); }
}

async function startLpgRefresh(scope = "all") {
  if (state.lpgRefreshJob && state.lpgRefreshJob.running) return;
  if (state.lpgRefreshTimer) clearTimeout(state.lpgRefreshTimer);
  state.lpgRefreshJob = { running: true, scope, label: "Starting refresh / 正在启动" };
  setLpgRefreshState(state.lpgRefreshJob.label, "running");
  const button = byId("lpg-refresh");
  if (button) button.disabled = true;
  byId("refresh").classList.add("spin");
  try {
    const response = lpgPayload(await requestJSON("/api/lpg/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope }),
    }));
    const job = response.job || response;
    const id = job.job_id || job.id || response.run_id;
    if (!id) {
      finishLpgRefresh(true, "Refresh completed / 刷新完成");
      return;
    }
    state.lpgRefreshJob = { running: true, id, scope, label: `Running ${scope} / 刷新中` };
    setLpgRefreshState(state.lpgRefreshJob.label, "running");
    pollLpgRefresh(id, 0);
  } catch (error) {
    finishLpgRefresh(false, `Refresh failed / 刷新失败: ${error.message}`);
  }
}

async function pollLpgRefresh(id, attempt) {
  try {
    const response = lpgPayload(await fetchJSON(`/api/lpg/refresh/${encodeURIComponent(id)}`));
    const job = response.job || response;
    const status = String(job.status || job.state || "running").toLowerCase();
    if (["completed", "complete", "success", "succeeded", "done"].includes(status)) {
      finishLpgRefresh(true, "Refresh completed / 刷新完成");
      return;
    }
    if (status === "partial") {
      finishLpgRefresh(true, "Refresh partially completed / 部分刷新完成", "warn");
      return;
    }
    if (status === "deferred") {
      finishLpgRefresh(false, "Refresh deferred because Excel is open / Excel占用，刷新已延后", "warn");
      return;
    }
    if (["failed", "error", "blocked", "cancelled"].includes(status)) {
      finishLpgRefresh(false, `Refresh failed / 刷新失败${job.error ? ": " + job.error : ""}`);
      return;
    }
    const rows = lpgRefreshRows(job);
    setLpgRefreshState(`Running / 刷新中${rows ? ` | ${rows} rows` : ""}`, "running");
    if (attempt >= 119) {
      finishLpgRefresh(false, "Refresh still running / 后台仍在刷新");
      return;
    }
    state.lpgRefreshTimer = setTimeout(() => pollLpgRefresh(id, attempt + 1), 1500);
  } catch (error) {
    if (attempt < 3) {
      state.lpgRefreshTimer = setTimeout(() => pollLpgRefresh(id, attempt + 1), 2000);
    } else finishLpgRefresh(false, `Refresh status unavailable / 无法获取刷新状态: ${error.message}`);
  }
}

function finishLpgRefresh(success, message, tone = success ? "ok" : "bad") {
  if (state.lpgRefreshTimer) clearTimeout(state.lpgRefreshTimer);
  state.lpgRefreshTimer = null;
  state.lpgRefreshJob = null;
  setLpgRefreshState(message, tone);
  const button = byId("lpg-refresh");
  if (button) button.disabled = false;
  byId("refresh").classList.remove("spin");
  if (success && state.view === "lpg") {
    state.lpgData = {};
    loadLpgView(state.lpgView);
  }
}

// ---------- SVG charts (dependency-free) ----------
const SVGNS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs) => el(tag, Object.assign({ ns: SVGNS }, attrs));

function svgLine(values, opts = {}) {
  const W = 300, H = opts.h || 120, pad = { l: 6, r: 6, t: 8, b: opts.xlabels ? 18 : 8 };
  const vals = values.filter(v => v !== null && v !== undefined && !Number.isNaN(v));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
  if (vals.length < 2) return svg;
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const x = (i) => pad.l + (i / (vals.length - 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - min) / span) * (H - pad.t - pad.b);
  for (let g = 0; g <= 2; g++) {
    const gy = pad.t + (g / 2) * (H - pad.t - pad.b);
    svg.appendChild(svgEl("line", { x1: pad.l, y1: gy, x2: W - pad.r, y2: gy, class: "chart-grid" }));
  }
  const dPath = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  if (opts.area) {
    const area = `${dPath} L${x(vals.length - 1).toFixed(1)} ${H - pad.b} L${x(0).toFixed(1)} ${H - pad.b} Z`;
    svg.appendChild(svgEl("path", { d: area, class: "chart-area" }));
  }
  svg.appendChild(svgEl("path", { d: dPath, class: "chart-line" }));
  if (opts.dots) {
    vals.forEach((v, i) => svg.appendChild(svgEl("circle", { cx: x(i), cy: y(v), r: 2.2, class: "chart-dot" })));
  }
  svg.appendChild(svgEl("circle", { cx: x(vals.length - 1), cy: y(vals[vals.length - 1]), r: 2.6, class: "chart-dot" }));
  svg.appendChild(svgEl("text", { x: pad.l + 1, y: pad.t + 8, class: "chart-label" })).textContent = fmtNum(max, Math.abs(max) < 10 ? 3 : 1);
  svg.appendChild(svgEl("text", { x: pad.l + 1, y: H - pad.b - 2, class: "chart-label" })).textContent = fmtNum(min, Math.abs(min) < 10 ? 3 : 1);
  if (opts.xlabels) {
    const step = Math.ceil(opts.xlabels.length / 6);
    opts.xlabels.forEach((lab, i) => {
      if (i % step === 0 || i === opts.xlabels.length - 1) {
        const t = svgEl("text", { x: x(i), y: H - 4, class: "chart-label", "text-anchor": "middle" });
        t.textContent = lab;
        svg.appendChild(t);
      }
    });
  }
  return svg;
}

function svgBars(values, opts = {}) {
  const W = 300, H = opts.h || 120, pad = { l: 6, r: 6, t: 8, b: 16 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
  const vals = values.map(v => (v === null || v === undefined || Number.isNaN(v)) ? 0 : v);
  if (!vals.length) return svg;
  const maxAbs = Math.max(0.001, ...vals.map(Math.abs));
  const zero = pad.t + (H - pad.t - pad.b) / 2;
  const half = (H - pad.t - pad.b) / 2;
  const bw = (W - pad.l - pad.r) / vals.length;
  svg.appendChild(svgEl("line", { x1: pad.l, y1: zero, x2: W - pad.r, y2: zero, class: "chart-axis" }));
  vals.forEach((v, i) => {
    const h = (Math.abs(v) / maxAbs) * half;
    const bx = pad.l + i * bw + bw * 0.15;
    const by = v >= 0 ? zero - h : zero;
    svg.appendChild(svgEl("rect", { x: bx, y: by, width: bw * 0.7, height: Math.max(h, 0.5),
      class: v >= 0 ? "bar-up" : "bar-down", rx: 1 }));
    if (opts.labels) {
      const t = svgEl("text", { x: pad.l + i * bw + bw / 2, y: H - 4, class: "bar-label" });
      t.textContent = opts.labels[i];
      svg.appendChild(t);
    }
  });
  return svg;
}

function svgBands(rows, opts = {}) {
  const W = 300, H = opts.h || 130, pad = { l: 6, r: 6, t: 8, b: 16 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
  const has = (v) => v !== null && v !== undefined && !Number.isNaN(v);
  const idxs = rows.map((r, i) => has(r.hist_min) && has(r.hist_max) ? i : -1).filter(i => i >= 0);
  if (idxs.length < 2) return svg;
  let lo = Infinity, hi = -Infinity;
  for (const r of rows) {
    for (const v of [r.hist_min, r.hist_max, r.hist_avg, r.current_year]) {
      if (has(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    }
  }
  const span = hi - lo || 1;
  const n = rows.length;
  const x = (i) => pad.l + (i / (n - 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - lo) / span) * (H - pad.t - pad.b);
  const top = idxs.map((i, k) => `${k ? "L" : "M"}${x(i).toFixed(1)} ${y(rows[i].hist_max).toFixed(1)}`).join(" ");
  const bottom = [...idxs].reverse().map(i => `L${x(i).toFixed(1)} ${y(rows[i].hist_min).toFixed(1)}`).join(" ");
  svg.appendChild(svgEl("path", { d: `${top} ${bottom} Z`, class: "band-area" }));
  const avgIdx = idxs.filter(i => has(rows[i].hist_avg));
  if (avgIdx.length >= 2) {
    svg.appendChild(svgEl("path", { class: "band-avg",
      d: avgIdx.map((i, k) => `${k ? "L" : "M"}${x(i).toFixed(1)} ${y(rows[i].hist_avg).toFixed(1)}`).join(" ") }));
  }
  const curIdx = rows.map((r, i) => has(r.current_year) ? i : -1).filter(i => i >= 0);
  if (curIdx.length >= 2) {
    svg.appendChild(svgEl("path", { class: "band-cur",
      d: curIdx.map((i, k) => `${k ? "L" : "M"}${x(i).toFixed(1)} ${y(rows[i].current_year).toFixed(1)}`).join(" ") }));
  } else if (curIdx.length === 1) {
    svg.appendChild(svgEl("circle", { cx: x(curIdx[0]), cy: y(rows[curIdx[0]].current_year), r: 2.6, class: "chart-dot" }));
  }
  svg.appendChild(svgEl("text", { x: pad.l + 1, y: pad.t + 8, class: "chart-label" })).textContent = fmtNum(hi, Math.abs(hi) < 10 ? 3 : 1);
  svg.appendChild(svgEl("text", { x: pad.l + 1, y: H - pad.b - 2, class: "chart-label" })).textContent = fmtNum(lo, Math.abs(lo) < 10 ? 3 : 1);
  rows.forEach((r, i) => {
    if (opts.labelStep && i % opts.labelStep !== 0) return;
    const lab = opts.labels ? opts.labels[i] : ((r.month || MONTHS[i] || "")[0]);
    if (!lab) return;
    const t = svgEl("text", { x: x(i), y: H - 4, class: "bar-label" });
    t.textContent = lab;
    svg.appendChild(t);
  });
  return svg;
}

function withHover(svg, n, fmt, opts = {}) {
  const W = 300, H = opts.h || 120, padL = 6, padR = 6;
  const wrap = el("div", { class: "chartwrap" }, [svg]);
  if (n < 2) return wrap;
  const tip = el("div", { class: "chart-tip hidden" });
  wrap.appendChild(tip);
  const cross = svgEl("line", { class: "chart-cross", x1: -10, x2: -10, y1: 0, y2: H });
  svg.appendChild(cross);
  const hide = () => {
    tip.classList.add("hidden");
    cross.setAttribute("x1", -10);
    cross.setAttribute("x2", -10);
  };
  svg.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const frac = (e.clientX - rect.left) / rect.width;
    let i = opts.band ? Math.floor(frac * n) : Math.round(frac * (n - 1));
    i = Math.max(0, Math.min(n - 1, i));
    const text = fmt(i);
    if (!text) { hide(); return; }
    tip.textContent = text;
    tip.classList.remove("hidden");
    const vx = opts.band
      ? padL + ((i + 0.5) / n) * (W - padL - padR)
      : padL + (i / (n - 1)) * (W - padL - padR);
    cross.setAttribute("x1", vx);
    cross.setAttribute("x2", vx);
    const left = e.clientX - rect.left;
    tip.style.left = Math.max(0, Math.min(left + 12, rect.width - tip.offsetWidth - 4)) + "px";
  });
  svg.addEventListener("mouseleave", hide);
  return wrap;
}

// ---------- init ----------
$("#refresh").onclick = () => {
  if (state.view === "spreads") loadSpreads(true);
  else if (state.view === "energy-hub") loadEnergyHub(true);
  else if (state.view === "news") loadNews(true);
  else if (state.view === "lpg") startLpgRefresh((byId("lpg-refresh-scope") || {}).value || "all");
  else if (state.view === "detail" && state.detailId) {
    for (const key of ALL_CARDS) if (byId("card-" + key)) setCardLoading(key);
    loadPanels(state.detailId, true);
  } else {
    loadOverview(true);
  }
};
$("#filter").addEventListener("input", (e) => {
  state.filter = e.target.value || "";
  if (state.view === "overview") renderGrid();
});
$("#filter").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.target.value = ""; state.filter = ""; renderGrid(); }
});

setInterval(() => {
  if (document.visibilityState !== "visible") return;
  if (state.view !== "overview") return;
  if (Date.now() - state.lastOverviewAt > 55000) loadOverview();
}, 15000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.view === "overview"
      && Date.now() - state.lastOverviewAt > 55000) loadOverview();
});

window.addEventListener("hashchange", route);
$("#footer-note").textContent = location.host;
renderTabs([]);
loadOverview();
route();
