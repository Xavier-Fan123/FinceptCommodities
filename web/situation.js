(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const WIDTH = 1000;
  const HEIGHT = 430;
  const LAT_TOP = 72;
  const LAT_BOTTOM = -52;
  const SEVERITY = { critical: 4, high: 3, medium: 2, low: 1 };

  const LAND = [
    [[-168,70],[-140,70],[-124,55],[-130,48],[-124,40],[-117,32],[-106,25],[-97,19],[-82,24],[-80,31],[-66,45],[-58,52],[-72,60],[-95,72]],
    [[-82,12],[-74,7],[-66,-2],[-50,-8],[-36,-22],[-48,-54],[-66,-50],[-74,-18]],
    [[-17,36],[-8,52],[10,60],[34,68],[64,72],[88,65],[118,53],[148,55],[165,44],[151,28],[122,20],[106,7],[94,6],[80,20],[61,26],[43,35],[27,40],[12,34]],
    [[-17,36],[10,37],[34,30],[51,12],[42,-11],[28,-34],[17,-35],[5,-25],[-7,4]],
    [[95,5],[108,-7],[119,-7],[130,-3],[141,-9],[151,-22],[145,-39],[122,-35],[113,-22]],
    [[-52,60],[-22,64],[-18,78],[-45,82],[-62,74]],
    [[130,34],[142,42],[146,32],[138,30]],
    [[47,-14],[51,-26],[44,-25]],
  ];

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }

  function svgNode(tag, attrs) {
    const item = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => item.setAttribute(key, String(value)));
    return item;
  }

  function append(parent, children) {
    (Array.isArray(children) ? children : [children]).filter(Boolean).forEach(child => parent.appendChild(child));
    return parent;
  }

  function project(lon, lat) {
    return [
      ((Number(lon) + 180) / 360) * WIDTH,
      ((LAT_TOP - Number(lat)) / (LAT_TOP - LAT_BOTTOM)) * HEIGHT,
    ];
  }

  function pathFor(points) {
    return points.map((point, index) => {
      const [x, y] = project(point[0], point[1]);
      return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ") + " Z";
  }

  function values(items, key) {
    return [...new Set((items || []).map(item => item[key]).filter(Boolean).map(String))].sort();
  }

  function list(value) {
    if (Array.isArray(value)) return value;
    if (value === null || value === undefined || value === "") return [];
    return [value];
  }

  function formatDate(value, withTime) {
    if (!value) return "N/A";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("en-GB", {
      year: "numeric", month: "short", day: "2-digit",
      hour: withTime ? "2-digit" : undefined,
      minute: withTime ? "2-digit" : undefined,
      hour12: false,
    }).format(date);
  }

  function optionSelect(label, current, options, onChange) {
    const select = node("select", "sit-select");
    select.setAttribute("aria-label", label);
    const all = node("option", "", `${label}: All`);
    all.value = "all";
    select.appendChild(all);
    options.forEach(value => {
      const option = node("option", "", String(value).replace(/_/g, " "));
      option.value = value;
      select.appendChild(option);
    });
    select.value = options.includes(current) ? current : "all";
    select.onchange = event => onChange(event.target.value);
    return select;
  }

  function filteredEvents(events, state) {
    const query = String(state.query || "").trim().toLowerCase();
    return events.filter(event => {
      if (state.severity !== "all" && event.severity !== state.severity) return false;
      if (state.eventType !== "all" && event.event_type !== state.eventType) return false;
      if (state.region !== "all" && event.region !== state.region) return false;
      if (state.confirmation !== "all" && event.confirmation_state !== state.confirmation) return false;
      if (!query) return true;
      return JSON.stringify({
        headline: event.headline, location: event.location_name, sources: event.sources,
        series: event.affected_series, type: event.event_type_label, gaps: event.data_gaps,
      }).toLowerCase().includes(query);
    });
  }

  function stat(label, value, tone) {
    const box = node("div", `sit-stat ${tone || ""}`);
    append(box, [node("span", "", label), node("strong", "", value)]);
    return box;
  }

  function drawSegment(group, first, second, className) {
    const [x1, y1] = project(first.longitude, first.latitude);
    const [x2, y2] = project(second.longitude, second.latitude);
    if (Math.abs(x2 - x1) <= WIDTH / 2) {
      group.appendChild(svgNode("line", { x1, y1, x2, y2, class: className }));
      return;
    }
    if (x1 < x2) {
      group.appendChild(svgNode("line", { x1, y1, x2: x2 - WIDTH, y2, class: className }));
      group.appendChild(svgNode("line", { x1: x1 + WIDTH, y1, x2, y2, class: className }));
    } else {
      group.appendChild(svgNode("line", { x1, y1, x2: x2 + WIDTH, y2, class: className }));
      group.appendChild(svgNode("line", { x1: x1 - WIDTH, y1, x2, y2, class: className }));
    }
  }

  function buildMap(data, events, state, rerender) {
    const wrap = node("div", "sit-map-wrap");
    const svg = svgNode("svg", {
      class: "sit-map", viewBox: `0 0 ${WIDTH} ${HEIGHT}`,
      role: "img", "aria-label": "LPG situation reference map",
    });
    const background = svgNode("g", { class: "sit-map-background" });
    for (let lon = -150; lon <= 150; lon += 30) {
      const [x] = project(lon, 0);
      background.appendChild(svgNode("line", { x1: x, y1: 0, x2: x, y2: HEIGHT, class: "sit-gridline" }));
    }
    for (let lat = -30; lat <= 60; lat += 30) {
      const [, y] = project(0, lat);
      background.appendChild(svgNode("line", { x1: 0, y1: y, x2: WIDTH, y2: y, class: "sit-gridline" }));
    }
    LAND.forEach(polygon => background.appendChild(svgNode("path", { d: pathFor(polygon), class: "sit-land" })));
    svg.appendChild(background);

    const assetById = Object.fromEntries((data.assets || []).map(asset => [asset.id, asset]));
    const scenario = state.scenarioResult || {};
    const scenarioAssets = new Set(list(scenario.asset_ids));
    const scenarioRoutes = new Set(list(scenario.route_ids));
    const scenarioAlternatives = new Set(list(scenario.alternative_route_ids));
    if (state.layers.routes) {
      const routeGroup = svgNode("g", { class: "sit-route-layer" });
      (data.routes || []).forEach(route => {
        const points = route.points || list(route.asset_ids).map(id => assetById[id]).filter(Boolean);
        const routeClasses = ["sit-route"];
        if (route.status === "reference_alternative") routeClasses.push("alternative");
        if (scenarioRoutes.has(route.id)) routeClasses.push("scenario-active");
        if (scenarioAlternatives.has(route.id)) routeClasses.push("scenario-alternative");
        for (let index = 1; index < points.length; index += 1) {
          drawSegment(routeGroup, points[index - 1], points[index], routeClasses.join(" "));
        }
      });
      svg.appendChild(routeGroup);
    }

    if (state.layers.assets) {
      const assetGroup = svgNode("g", { class: "sit-asset-layer" });
      (data.assets || []).forEach(asset => {
        const [x, y] = project(asset.longitude, asset.latitude);
        const marker = svgNode("rect", {
          x: x - 3, y: y - 3, width: 6, height: 6,
          class: `sit-asset kind-${asset.kind}${scenarioAssets.has(asset.id) ? " scenario-active" : ""}`,
        });
        marker.appendChild(svgNode("title", {})).textContent = `${asset.name} · ${asset.kind} · ${asset.geo_precision}`;
        assetGroup.appendChild(marker);
      });
      svg.appendChild(assetGroup);
    }

    const vesselIntelligence = data.vessel_intelligence || {};
    const vesselCalls = list(vesselIntelligence.port_calls)
      .filter(call => call.latitude !== null && call.longitude !== null);
    const vesselPositions = list(vesselIntelligence.positions)
      .filter(position => position.latitude !== null && position.longitude !== null);
    if (state.layers.vessels && (vesselCalls.length || vesselPositions.length)) {
      const callGroups = new Map();
      vesselCalls.forEach(call => {
        const calls = callGroups.get(call.vessel_id) || [];
        calls.push(call);
        callGroups.set(call.vessel_id, calls);
      });
      const positionGroups = new Map();
      vesselPositions.forEach(position => {
        const positions = positionGroups.get(position.vessel_id) || [];
        positions.push(position);
        positionGroups.set(position.vessel_id, positions);
      });
      const trails = svgNode("g", { class: "sit-vessel-trail-layer" });
      const markers = svgNode("g", { class: "sit-vessel-marker-layer" });
      const fleet = list(vesselIntelligence.vessels);
      fleet.forEach(vessel => {
        const calls = (callGroups.get(vessel.vessel_id) || []).sort((a, b) =>
          String(a.arrived_at || a.departed_at || "").localeCompare(String(b.arrived_at || b.departed_at || ""))
        ).slice(-12);
        for (let index = 1; index < calls.length; index += 1) {
          drawSegment(trails, calls[index - 1], calls[index], "sit-vessel-trail historical");
        }
        const positions = (positionGroups.get(vessel.vessel_id) || []).sort((a, b) =>
          String(a.observed_at || "").localeCompare(String(b.observed_at || ""))
        );
        const latestPosition = positions[positions.length - 1];
        const latestCall = calls[calls.length - 1];
        const point = latestPosition || latestCall;
        if (!point) return;
        const [x, y] = project(point.longitude, point.latitude);
        const isPosition = Boolean(latestPosition);
        const marker = svgNode("path", {
          d: `M${x},${y - 8} L${x + 7},${y + 6} L${x - 7},${y + 6} Z`,
          class: `sit-vessel-marker ${isPosition ? `position-${latestPosition.freshness || "unknown"}` : "historical-call"}${state.selectedVesselId === vessel.vessel_id ? " selected" : ""}`,
          tabindex: "0", role: "button", "aria-label": vessel.name,
        });
        const label = isPosition
          ? `${String(latestPosition.freshness || "unknown").toUpperCase()} POSITION · ${latestPosition.observed_at}`
          : `LAST HISTORICAL PORT CALL · ${latestCall.port_name} · ${latestCall.arrived_at || latestCall.departed_at || "time unavailable"}`;
        marker.appendChild(svgNode("title", {})).textContent = `${vessel.name} · ${label}`;
        marker.onclick = () => { state.selectedVesselId = vessel.vessel_id; rerender(); };
        marker.onkeydown = keyEvent => {
          if (keyEvent.key === "Enter" || keyEvent.key === " ") {
            keyEvent.preventDefault(); state.selectedVesselId = vessel.vessel_id; rerender();
          }
        };
        markers.appendChild(marker);
      });
      svg.appendChild(trails);
      svg.appendChild(markers);
    }

    if (state.layers.events) {
      const eventGroup = svgNode("g", { class: "sit-event-layer" });
      const positions = new Map();
      events.filter(event => event.latitude !== null && event.longitude !== null).forEach(event => {
        const [baseX, baseY] = project(event.longitude, event.latitude);
        const key = `${baseX.toFixed(0)}:${baseY.toFixed(0)}`;
        const overlap = positions.get(key) || 0;
        positions.set(key, overlap + 1);
        const angle = overlap * 2.2;
        const radius = overlap ? 5 + overlap * 1.8 : 0;
        const x = baseX + Math.cos(angle) * radius;
        const y = baseY + Math.sin(angle) * radius;
        const marker = svgNode("circle", {
          cx: x, cy: y, r: 4.5 + Math.min(5, Number(event.risk_score || 0) / 25),
          class: `sit-event severity-${event.severity}${state.selectedEventKey === event.event_key ? " selected" : ""}`,
          tabindex: "0", role: "button", "aria-label": event.headline,
        });
        marker.appendChild(svgNode("title", {})).textContent = `${event.severity.toUpperCase()} · ${event.headline}`;
        marker.onclick = () => { state.selectedEventKey = event.event_key; rerender(); };
        marker.onkeydown = keyEvent => {
          if (keyEvent.key === "Enter" || keyEvent.key === " ") {
            keyEvent.preventDefault(); state.selectedEventKey = event.event_key; rerender();
          }
        };
        eventGroup.appendChild(marker);
      });
      svg.appendChild(eventGroup);
    }

    wrap.appendChild(svg);
    const overlay = node("div", "sit-map-overlay");
    overlay.textContent = state.scenarioResult
      ? "HYPOTHETICAL SCENARIO OVERLAY · NOT A FORECAST OR LIVE AIS"
      : vesselIntelligence.coverage && vesselIntelligence.coverage.display_state === "historical_only"
        ? "HISTORICAL PORT CALLS · NOT LIVE AIS POSITIONS"
        : "REFERENCE GEOGRAPHY · ROUTES ARE NOT LIVE AIS TRACKS";
    wrap.appendChild(overlay);
    const legend = node("div", "sit-map-legend");
    ["critical", "high", "medium", "low"].forEach(level => {
      const item = node("span", "");
      append(item, [node("i", `severity-${level}`), document.createTextNode(level)]);
      legend.appendChild(item);
    });
    if (state.scenarioResult) {
      const item = node("span", "sit-scenario-legend");
      append(item, [node("i", ""), document.createTextNode("scenario exposure")]);
      legend.appendChild(item);
    }
    if (state.layers.vessels && vesselCalls.length) {
      const item = node("span", "sit-vessel-legend");
      append(item, [node("i", ""), document.createTextNode("historical vessel call")]);
      legend.appendChild(item);
    }
    wrap.appendChild(legend);
    return wrap;
  }

  function chips(values, className) {
    const row = node("div", className || "sit-chips");
    list(values).forEach(value => row.appendChild(node("span", "", String(value).replace(/_/g, " "))));
    return row;
  }

  function marketRows(event, market) {
    const targets = new Set(list(event && event.affected_series));
    const prices = list(market && market.prices);
    const matched = prices.filter(row => targets.has(row.canonical_key) || targets.has(row.series_id) || targets.has(row.id));
    const table = node("div", "sit-market-table");
    const header = node("div", "sit-market-row head");
    append(header, [node("span", "", "Exposed series"), node("span", "", "Latest"), node("span", "", "As of")]);
    table.appendChild(header);
    if (!targets.size) {
      table.appendChild(node("div", "sit-empty-inline", "No benchmark exposure was inferred."));
      return table;
    }
    [...targets].slice(0, 12).forEach(key => {
      const row = matched.find(item => [item.canonical_key, item.series_id, item.id].includes(key));
      const value = row ? (row.value ?? row.price ?? row.value_normalized) : null;
      const unit = row ? [row.currency, row.unit].filter(Boolean).join("/") : "";
      const line = node("div", `sit-market-row${row ? "" : " missing"}`);
      append(line, [
        node("span", "", row ? (row.name || key) : key),
        node("span", "", value === null || value === undefined ? "Unavailable" : `${Number(value).toLocaleString("en-US", { maximumFractionDigits: 3 })} ${unit}`),
        node("span", "", row ? (row.observation_date || row.as_of || market.as_of || "N/A") : "No entitled current row"),
      ]);
      table.appendChild(line);
    });
    return table;
  }

  function eventDetail(event, data) {
    const detail = node("div", "sit-detail");
    if (!event) {
      append(detail, [
        node("strong", "sit-detail-title", "No event selected"),
        node("p", "", "Adjust the filters or refresh News sources. Assets and routes remain reference context."),
      ]);
      return detail;
    }
    const top = node("div", "sit-detail-head");
    append(top, [
      node("span", `sit-severity severity-${event.severity}`, event.severity),
      node("span", `sit-confirm ${event.confirmation_state}`, event.confirmation_state),
      node("span", "sit-risk", `Risk ${event.risk_score}/100`),
      node("span", "sit-confidence", `Confidence ${event.confidence_score}/100`),
    ]);
    append(detail, [top, node("h3", "sit-detail-title", event.headline)]);
    const location = event.location_name
      ? `${event.location_name} · ${event.geo_precision}`
      : "Location unresolved — event intentionally not placed on the map";
    append(detail, [
      node("div", "sit-detail-meta", `${event.event_type_label || event.event_type} · ${location} · last seen ${formatDate(event.last_seen_at, true)}`),
      node("p", "sit-impact-summary", event.impact && event.impact.summary),
    ]);

    const grid = node("div", "sit-detail-grid");
    const mechanisms = node("div", "sit-detail-block");
    append(mechanisms, [node("h4", "", "Transmission mechanisms"), chips(event.impact && event.impact.mechanisms)]);
    const routes = node("div", "sit-detail-block");
    const routeById = Object.fromEntries((data.routes || []).map(route => [route.id, route.name]));
    append(routes, [node("h4", "", "Exposed reference routes"), chips(list(event.route_ids).map(id => routeById[id] || id))]);
    grid.appendChild(mechanisms);
    grid.appendChild(routes);
    detail.appendChild(grid);
    append(detail, [node("h4", "sit-subhead", "Market exposure — inferred, not an official assessment"), marketRows(event, data.market_snapshot || {})]);

    const evidence = node("div", "sit-evidence");
    list(event.evidence).forEach(item => {
      const row = node("div", "sit-evidence-row");
      const title = item.url ? node("a", "", item.headline || "Evidence") : node("strong", "", item.headline || "Evidence");
      if (item.url) { title.href = item.url; title.target = "_blank"; title.rel = "noopener"; }
      append(row, [title, node("span", "", `${item.source || "Unknown"} · ${formatDate(item.published_at, true)} · ${item.content_boundary || "source"}`)]);
      evidence.appendChild(row);
    });
    append(detail, [node("h4", "sit-subhead", `Observed evidence · ${event.evidence_count} item(s) / ${event.source_count} source(s)`), evidence]);
    if (list(event.data_gaps).length) {
      append(detail, [node("h4", "sit-subhead", "Known intelligence gaps"), chips(event.data_gaps, "sit-chips gaps")]);
    }
    return detail;
  }

  function eventList(events, state, rerender) {
    const panel = node("div", "sit-event-list");
    if (!events.length) {
      panel.appendChild(node("div", "sit-empty", "No events match the current filter."));
      return panel;
    }
    events.slice(0, 80).forEach(event => {
      const button = node("button", `sit-event-row${event.event_key === state.selectedEventKey ? " selected" : ""}`);
      button.type = "button";
      const top = node("div", "sit-event-row-top");
      append(top, [
        node("span", `sit-severity severity-${event.severity}`, event.severity),
        node("span", "", event.event_type_label || event.event_type),
        node("span", "sit-event-time", formatDate(event.last_seen_at, false)),
      ]);
      append(button, [top, node("strong", "", event.headline), node("small", "", `${event.location_name || "Unlocated"} · ${event.source_count} source(s) · risk ${event.risk_score}`)]);
      button.onclick = () => { state.selectedEventKey = event.event_key; rerender(); };
      panel.appendChild(button);
    });
    return panel;
  }

  function coverageStrip(data, events) {
    const located = events.filter(event => event.latitude !== null && event.longitude !== null).length;
    const confirmed = events.filter(event => event.confirmation_state === "confirmed").length;
    const critical = events.filter(event => event.severity === "critical").length;
    const sourceSet = new Set(events.flatMap(event => list(event.sources)));
    const strip = node("div", "sit-stat-strip");
    append(strip, [
      stat("Visible events", events.length), stat("Located", `${located}/${events.length}`),
      stat("Confirmed", confirmed), stat("Critical", critical, critical ? "critical" : ""),
      stat("Evidence sources", sourceSet.size),
      stat("Baseline alerts", list(data.alerting && data.alerting.alerts).length),
    ]);
    return strip;
  }

  function alertAndGapPanel(data) {
    const wrap = node("div", "sit-health-grid");
    const alert = node("section", "sit-health-card");
    const alerting = data.alerting || {};
    append(alert, [
      node("h3", "", "Baseline-aware alerts"),
      node("strong", `sit-health-state ${alerting.coverage_state || "unknown"}`, String(alerting.coverage_state || "unknown").replace(/_/g, " ")),
      node("p", "", list(alerting.alerts).length
        ? `${alerting.alerts.length} multi-source event surge(s) exceeded the 2h vs 7d baseline.`
        : "No qualified surge. Thin history remains explicitly marked insufficient instead of being treated as normal."),
    ]);
    list(alerting.alerts).forEach(item => alert.appendChild(node("div", "sit-alert-row", `${item.event_type_label}: ${item.recent_count} recent · ${item.ratio}x baseline · ${item.source_diversity} sources`)));
    const gaps = node("section", "sit-health-card");
    append(gaps, [node("h3", "", "Intelligence gaps")]);
    list(data.intelligence_gaps).forEach(gap => {
      const row = node("div", "sit-gap-row");
      append(row, [node("strong", `status-${gap.status}`, `${gap.id.replace(/_/g, " ")} · ${gap.status}`), node("span", "", gap.detail)]);
      gaps.appendChild(row);
    });
    wrap.appendChild(alert);
    wrap.appendChild(gaps);
    return wrap;
  }

  function vesselPanel(data, state, rerender) {
    const intelligence = data.vessel_intelligence || {};
    const fleet = list(intelligence.vessels);
    const panel = node("section", "sit-vessel-panel");
    const heading = node("div", "sit-vessel-heading");
    const coverage = intelligence.coverage || {};
    append(heading, [
      node("strong", "", "VESSEL INTELLIGENCE"),
      node("span", "", `${coverage.vessels || 0} vessels · ${coverage.historical_port_calls || 0} historical calls · ${coverage.fresh_live_positions || 0} fresh live positions`),
    ]);
    panel.appendChild(heading);
    if (!fleet.length) {
      panel.appendChild(node("div", "sit-vessel-empty",
        "No vessel snapshot is loaded. Reference routes remain visible, but no vessel marker is inferred."));
      return panel;
    }
    if (!fleet.some(vessel => vessel.vessel_id === state.selectedVesselId)) {
      state.selectedVesselId = fleet[0].vessel_id;
    }
    const selected = fleet.find(vessel => vessel.vessel_id === state.selectedVesselId);
    const tabs = node("div", "sit-vessel-tabs");
    fleet.forEach(vessel => {
      const button = node("button", vessel.vessel_id === selected.vessel_id ? "active" : "", vessel.name);
      button.type = "button";
      button.onclick = () => { state.selectedVesselId = vessel.vessel_id; rerender(); };
      tabs.appendChild(button);
    });
    panel.appendChild(tabs);

    const positions = list(intelligence.positions)
      .filter(item => item.vessel_id === selected.vessel_id)
      .sort((a, b) => String(b.observed_at || "").localeCompare(String(a.observed_at || "")));
    const calls = list(intelligence.port_calls)
      .filter(item => item.vessel_id === selected.vessel_id)
      .sort((a, b) => String(b.arrived_at || b.departed_at || "").localeCompare(String(a.arrived_at || a.departed_at || "")));
    const current = positions[0];
    const detail = node("div", "sit-vessel-detail");
    const summary = node("div", "sit-vessel-summary");
    const stateClass = current ? `position-${current.freshness || "unknown"}` : "historical-only";
    append(summary, [
      node("span", `sit-vessel-state ${stateClass}`, current
        ? `${current.freshness || "unknown"} position`
        : "historical port calls only"),
      node("h3", "", selected.name),
      node("p", "", `IMO ${selected.imo || "N/A"} · MMSI ${selected.mmsi || "N/A"} · ${selected.fleet_group || "reference fleet"}`),
      node("small", "", current
        ? `Position observed ${formatDate(current.observed_at, true)} · ${current.source} · age ${current.age_hours ?? "unknown"}h`
        : "No timestamped current-position record exists. The map marker is the latest historical port call."),
    ]);
    const stats = node("div", "sit-vessel-stats");
    append(stats, [
      stat("Port calls", selected.port_call_count || calls.length),
      stat("Positions", selected.position_count || positions.length),
      stat("Latest call", calls[0] ? formatDate(calls[0].arrived_at || calls[0].departed_at, false) : "N/A"),
      stat("Last port", calls[0] ? calls[0].port_name : "N/A"),
    ]);
    append(detail, [summary, stats]);

    const table = node("div", "sit-vessel-call-table");
    const header = node("div", "sit-vessel-call-row head");
    append(header, [
      node("span", "", "Historical call"), node("span", "", "Port"),
      node("span", "", "Operation signal"), node("span", "", "Evidence"),
    ]);
    table.appendChild(header);
    calls.slice(0, 12).forEach(call => {
      const row = node("div", "sit-vessel-call-row");
      append(row, [
        node("span", "", formatDate(call.arrived_at || call.departed_at, true)),
        node("span", "", `${call.port_name}${call.locode ? ` · ${call.locode}` : ""}`),
        node("span", `operation-${call.operation_signal}`, `${String(call.operation_signal || "unknown").replace(/_/g, " ")} · inferred`),
        node("span", "", `${call.source} · ${String(call.timestamp_state || "unknown").replace(/_/g, " ")}`),
      ]);
      table.appendChild(row);
    });
    if (!calls.length) table.appendChild(node("div", "sit-vessel-empty", "No historical calls for this vessel."));
    panel.appendChild(detail);
    panel.appendChild(table);

    const sources = node("div", "sit-vessel-sources");
    list(intelligence.source_health).forEach(source => {
      const item = node("div", "sit-vessel-source");
      append(item, [
        node("strong", "", `${source.source_name} · ${source.status}`),
        node("span", "", `${source.access_state} · entitlement ${source.entitlement_state} · ${source.row_count} rows · latest ${formatDate(source.latest_observation_at, true)}`),
      ]);
      sources.appendChild(item);
    });
    panel.appendChild(sources);
    panel.appendChild(node("div", "sit-vessel-boundary",
      "Map triangles are age-labelled positions only when vessel_positions contains a timestamped record. Otherwise they are historical port-call markers. Draught-change signals are not cargo proof."));
    return panel;
  }

  function scenarioInputState(state, template) {
    state.scenarioInputs = state.scenarioInputs || {};
    if (!state.scenarioInputs[template.id]) {
      state.scenarioInputs[template.id] = Object.fromEntries(
        list(template.parameters).map(spec => [spec.key, spec.default]),
      );
    }
    return state.scenarioInputs[template.id];
  }

  function scenarioResultPanel(result, data) {
    if (!result) {
      return node("div", "sit-scenario-empty",
        "Select assumptions and run the template. Nothing is inferred from missing flow, AIS, terminal, freight, or price data.");
    }
    const panel = node("div", "sit-scenario-result");
    const index = result.stress_index || {};
    const score = node("div", `sit-stress-score band-${index.band || "contained"}`);
    append(score, [
      node("strong", "", index.score ?? "N/A"),
      node("span", "", `stress · ${index.band || "unknown"}`),
    ]);
    const summary = node("div", "sit-scenario-summary");
    append(summary, [
      node("span", "sit-hypothetical", "HYPOTHETICAL · NOT A FORECAST"),
      node("h3", "", result.name),
      node("p", "", result.premise),
      node("small", "", index.meaning),
    ]);
    append(panel, [score, summary]);

    const detail = node("div", "sit-scenario-result-grid");
    const mechanisms = node("section", "");
    append(mechanisms, [node("h4", "", "Transmission mechanisms"), chips(result.mechanisms)]);
    const questions = node("section", "");
    append(questions, [node("h4", "", "Commercial questions"), chips(result.commercial_questions)]);
    const audit = node("section", "sit-scenario-audit");
    append(audit, [
      node("h4", "", "Calculation audit"),
      node("code", "", index.formula),
      node("p", "", Object.entries(index.components || {})
        .map(([key, value]) => `${key.replace(/_/g, " ")} ${value}/100`).join(" · ")),
    ]);
    append(detail, [mechanisms, questions, audit]);
    panel.appendChild(detail);
    append(panel, [
      node("h4", "sit-subhead", "Current market context · exposed rows only, no calculated outcome"),
      marketRows(result, result.market_snapshot || data.market_snapshot || {}),
      node("p", "sit-scenario-guardrail", result.guardrail || "No price or freight outcome is calculated."),
    ]);
    return panel;
  }

  function scenarioLab(data, state, hooks, rerender) {
    const engine = data.scenario_engine || {};
    const templates = list(engine.templates);
    const lab = node("section", "sit-scenario-lab");
    const heading = node("div", "sit-scenario-heading");
    append(heading, [
      node("div", "", "SCENARIO ENGINE / STRESS LAB"),
      node("span", "", "User assumptions + reference exposure; zero synthetic market data"),
    ]);
    lab.appendChild(heading);
    if (!templates.length) {
      lab.appendChild(node("div", "sit-scenario-empty", "Scenario templates are unavailable."));
      return lab;
    }
    if (!templates.some(item => item.id === state.selectedScenarioId)) {
      state.selectedScenarioId = templates[0].id;
    }
    const selected = templates.find(item => item.id === state.selectedScenarioId);
    const tabs = node("div", "sit-scenario-tabs");
    templates.forEach(template => {
      const button = node("button", template.id === selected.id ? "active" : "", template.name);
      button.type = "button";
      button.onclick = () => {
        state.selectedScenarioId = template.id;
        state.scenarioResult = null;
        state.scenarioError = null;
        rerender();
      };
      tabs.appendChild(button);
    });
    lab.appendChild(tabs);

    const assumptions = scenarioInputState(state, selected);
    const setup = node("div", "sit-scenario-setup");
    const premise = node("div", "sit-scenario-premise");
    append(premise, [
      node("strong", "", selected.premise),
      node("span", "", `Category: ${selected.category.replace(/_/g, " ")} · ${list(selected.route_ids).length} exposed route(s) · ${list(selected.affected_series).length} benchmark(s)`),
    ]);
    const controls = node("div", "sit-scenario-controls");
    list(selected.parameters).forEach(spec => {
      const label = node("label", "sit-scenario-control");
      const caption = node("span", "", spec.label);
      const input = node("input", "");
      input.type = "number";
      input.min = spec.minimum;
      input.max = spec.maximum;
      input.step = spec.step;
      input.value = assumptions[spec.key];
      input.title = spec.meaning;
      input.onchange = event => {
        assumptions[spec.key] = Number(event.target.value);
        state.scenarioResult = null;
      };
      append(label, [caption, input, node("small", "", spec.unit)]);
      controls.appendChild(label);
    });
    const actions = node("div", "sit-scenario-actions");
    const run = node("button", "sit-run-scenario", state.scenarioLoading ? "Running…" : "Run scenario");
    run.type = "button";
    run.disabled = Boolean(state.scenarioLoading);
    run.onclick = async () => {
      state.scenarioLoading = true;
      state.scenarioError = null;
      rerender();
      try {
        const request = hooks && hooks.requestJSON
          ? hooks.requestJSON
          : async (url, options) => {
            const response = await fetch(url, options);
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
            return payload;
          };
        state.scenarioResult = await request("/api/lpg/scenarios/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ scenario_id: selected.id, inputs: assumptions }),
        });
      } catch (error) {
        state.scenarioError = error.message || String(error);
      } finally {
        state.scenarioLoading = false;
        rerender();
      }
    };
    const reset = node("button", "sit-reset-scenario", "Reset assumptions");
    reset.type = "button";
    reset.onclick = () => {
      state.scenarioInputs[selected.id] = Object.fromEntries(
        list(selected.parameters).map(spec => [spec.key, spec.default]),
      );
      state.scenarioResult = null;
      state.scenarioError = null;
      rerender();
    };
    append(actions, [run, reset]);
    append(setup, [premise, controls, actions]);
    lab.appendChild(setup);
    if (state.scenarioError) lab.appendChild(node("div", "sit-scenario-error", state.scenarioError));
    lab.appendChild(scenarioResultPanel(state.scenarioResult, data));
    const guardrail = node("div", "sit-scenario-boundary");
    append(guardrail, list(engine.guardrails).map(item => node("span", "", item)));
    lab.appendChild(guardrail);
    return lab;
  }

  function render(data, state, hooks) {
    const body = hooks && hooks.body ? hooks.body : document.getElementById("lpg-body");
    if (!body) return;
    state.layers = { events: true, assets: true, routes: true, vessels: true, ...(state.layers || {}) };
    const allEvents = list(data.events || data.items);
    const rerender = () => render(data, state, hooks);
    const events = filteredEvents(allEvents, state);
    if (!events.some(event => event.event_key === state.selectedEventKey)) {
      state.selectedEventKey = (events.find(event => event.latitude !== null && event.longitude !== null) || events[0] || {}).event_key || null;
    }
    const selected = events.find(event => event.event_key === state.selectedEventKey) || null;

    const toolbar = node("div", "sit-toolbar");
    const search = node("input", "sit-search");
    search.type = "search";
    search.value = state.query || "";
    search.placeholder = "Search event, terminal, route, source, benchmark...";
    search.setAttribute("aria-label", "Search LPG situation intelligence");
    search.oninput = event => {
      state.query = event.target.value;
      rerender();
      const next = body.querySelector(".sit-search");
      if (next) { next.focus(); next.setSelectionRange(next.value.length, next.value.length); }
    };
    append(toolbar, [
      search,
      optionSelect("Severity", state.severity, ["critical", "high", "medium", "low"], value => { state.severity = value; rerender(); }),
      optionSelect("Event", state.eventType, values(allEvents, "event_type"), value => { state.eventType = value; rerender(); }),
      optionSelect("Region", state.region, values(allEvents, "region"), value => { state.region = value; rerender(); }),
      optionSelect("Confidence", state.confirmation, ["confirmed", "developing"], value => { state.confirmation = value; rerender(); }),
    ]);
    const layers = node("div", "sit-layer-controls");
    Object.keys(state.layers).forEach(key => {
      const button = node("button", state.layers[key] ? "active" : "", key);
      button.type = "button";
      button.setAttribute("aria-pressed", String(state.layers[key]));
      button.onclick = () => { state.layers[key] = !state.layers[key]; rerender(); };
      layers.appendChild(button);
    });
    toolbar.appendChild(layers);

    const hero = node("div", "sit-hero");
    const mapColumn = node("div", "sit-map-column");
    mapColumn.appendChild(buildMap(data, events, state, rerender));
    mapColumn.appendChild(coverageStrip(data, events));
    const rail = node("aside", "sit-rail");
    const railHead = node("div", "sit-rail-head");
    append(railHead, [node("strong", "", "Ranked event stream"), node("span", "", `${events.length}/${allEvents.length}`)]);
    append(rail, [railHead, eventList(events, state, rerender)]);
    hero.appendChild(mapColumn);
    hero.appendChild(rail);

    const boundary = node("div", "sit-boundary");
    append(boundary, [
      node("strong", "", "Observed evidence and inferred exposure are kept separate."),
      node("span", "", "Named locations are mapped at disclosed precision. Routes are curated context. Missing satellite AIS, terminal operations, or Platts News entitlements remain visible gaps."),
    ]);
    body.replaceChildren(
      toolbar,
      boundary,
      scenarioLab(data, state, hooks, rerender),
      hero,
      vesselPanel(data, state, rerender),
      eventDetail(selected, data),
      alertAndGapPanel(data),
    );
  }

  window.FinceptLpgSituation = { render };
}());
