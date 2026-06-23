"use strict";

const SECTOR_LABEL = {
  energy: "Energy", precious_metals: "Precious Metals", base_metals: "Base Metals",
  grains: "Grains", softs: "Softs", livestock: "Livestock",
};
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

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
};

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);
const byId = (id) => document.getElementById(id);
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElementNS(attrs.ns || "http://www.w3.org/1999/xhtml", tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "ns") continue;
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

// ---------- routing ----------
function route() {
  const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  if (h === "spreads") showSpreadsView();
  else if (h === "energy-hub") showEnergyHubView();
  else if (h === "news") showNewsView();
  else if (h) showDetail(h.toLowerCase());
  else showOverview();
}

function swapViews(name) {
  for (const id of ["overview", "detail", "spreads", "energy-hub", "news"]) {
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
  if (tabs.dataset.built) return;
  tabs.dataset.built = "1";
  const mk = (key, label) => {
    const b = el("button", { text: label, "data-tab": key });
    b.onclick = () => {
      if (key === "spreads" || key === "energy-hub" || key === "news") {
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
loadOverview();
route();
