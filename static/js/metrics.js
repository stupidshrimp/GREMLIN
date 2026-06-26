/* Metrics dashboard client.
 *
 * Drives the high-level equipment / maintenance reliability dashboard:
 *   - asset search + multi-select filter and a date range, shared by every card;
 *   - three clickable cards (Operational KPIs, Alerting Readiness, Availability)
 *     that show a small preview while collapsed and expand to full charts/tables;
 *   - all charts compare assets against each other (x-axis = asset, y-axis = the
 *     selected performance metric).
 *
 * Data comes from /metrics/api/reliability, which reuses the same maintenance /
 * corrective-work-order dataset as the Life Data Analysis pages. The full set of
 * assets for the active date range is fetched once; the asset filter narrows the
 * comparison client-side so toggling assets stays instant. Changing the date
 * range refetches.
 */
(function () {
  "use strict";

  const ASSET_API = "/life-data-analysis/api/assets";
  const METRICS_API = "/metrics/api/reliability";
  const ALERT_THRESHOLD = 70;

  // Forest palette (matches theme.css) used for the comparison bars.
  const BAR_COLOR = "#3f5e77";
  const BAR_COLOR_ALT = "#7fa6c0";
  const ACCENT = "#2f8f5b";
  const WARN = "#c0392b";

  const state = {
    assets: [], // [{asset_number, asset_name}]
    selected: new Set(), // asset_number values; empty = all
    payload: null, // last /metrics/api/reliability response
    expanded: null, // currently expanded card key or null
    dropdownOpen: false,
    assetQuery: "",
    dateFrom: "",
    dateTo: "",
    dataWindow: { start: null, end: null },
    // Preserve the all-asset extent learned on the initial unfiltered request so
    // Reset can restore true global bounds even after selected-asset requests
    // replace dataWindow with a narrower extent.
    globalDataWindow: { start: null, end: null },
    dateInitialized: false,
    fetchToken: 0,
  };

  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.entries(attrs).forEach(([key, value]) => {
        if (value === null || value === undefined || value === false) return;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
        else if (value === true) node.setAttribute(key, "");
        else node.setAttribute(key, value);
      });
    }
    (children || []).forEach((child) => {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function fmtNum(value, digits) {
    const num = Number(value);
    if (!isFinite(num)) return "—";
    return num.toLocaleString(undefined, { maximumFractionDigits: digits == null ? 1 : digits });
  }

  function fmtPct(value) {
    if (value === null || value === undefined) return "—";
    const num = Number(value);
    if (!isFinite(num)) return "—";
    const sign = num > 0 ? "+" : "";
    return `${sign}${num.toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
  }

  // ---- network -------------------------------------------------------------
  async function getJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }
    if (!response.ok) {
      throw new Error((data && data.error) || `Request failed (${response.status}).`);
    }
    return data;
  }

  function showBanner(message, kind) {
    const banner = $("metrics-status");
    banner.textContent = message;
    banner.className = "metrics-banner " + (kind === "error" ? "is-error" : "is-info");
    banner.hidden = false;
  }
  function clearBanner() {
    $("metrics-status").hidden = true;
  }

  let loadingDepth = 0;
  function beginLoading() {
    loadingDepth += 1;
    $("metrics-loading").hidden = false;
  }
  function endLoading() {
    loadingDepth = Math.max(0, loadingDepth - 1);
    if (loadingDepth === 0) $("metrics-loading").hidden = true;
  }

  // ---- asset filter --------------------------------------------------------
  async function loadAssets() {
    const hint = $("metrics-asset-hint");
    try {
      const data = await getJson(ASSET_API);
      state.assets = data.assets || [];
      hint.textContent = state.assets.length
        ? `${state.assets.length} asset(s) available. Type to search, click to compare.`
        : "No mapped assets were found in the database.";
    } catch (err) {
      hint.textContent = "";
      // The metrics fetch surfaces the real error banner; keep the hint quiet.
      state.assets = [];
    }
  }

  function filteredAssetOptions() {
    const query = state.assetQuery.trim().toLowerCase();
    if (!query) return state.assets;
    return state.assets.filter((a) => {
      const number = (a.asset_number || "").toLowerCase();
      const name = (a.asset_name || "").toLowerCase();
      return number.includes(query) || name.includes(query);
    });
  }

  function renderAssetMenu() {
    const menu = $("metrics-asset-menu");
    menu.innerHTML = "";
    if (!state.dropdownOpen) {
      menu.hidden = true;
      return;
    }
    const options = filteredAssetOptions();
    if (!options.length) {
      menu.appendChild(el("div", { class: "metrics-asset-empty", text: "No matching assets." }));
      menu.hidden = false;
      return;
    }
    options.slice(0, 60).forEach((asset) => {
      const checked = state.selected.has(asset.asset_number);
      const checkbox = el("input", { type: "checkbox" });
      checkbox.checked = checked;
      const row = el(
        "div",
        {
          class: "metrics-asset-option" + (checked ? " is-active" : ""),
          role: "option",
          "aria-selected": checked ? "true" : "false",
          onclick: (event) => {
            if (event.target !== checkbox) checkbox.checked = !checkbox.checked;
            toggleAsset(asset.asset_number, checkbox.checked);
          },
        },
        [
          checkbox,
          el("span", { text: asset.asset_number }),
          asset.asset_name ? el("span", { class: "metrics-asset-meta", text: asset.asset_name }) : null,
        ]
      );
      menu.appendChild(row);
    });
    if (options.length > 60) {
      menu.appendChild(
        el("div", { class: "metrics-asset-empty", text: `Showing first 60 of ${options.length}. Keep typing to narrow.` })
      );
    }
    menu.hidden = false;
  }

  function toggleAsset(assetNumber, on) {
    if (on) state.selected.add(assetNumber);
    else state.selected.delete(assetNumber);
    renderAssetMenu();
    renderSelectedChips();
    renderScopeHint();
    // Risk scoring depends on the compared set, so refetch (debounced) rather
    // than just re-filtering the existing payload client-side.
    scheduleReload();
  }

  function renderSelectedChips() {
    const wrap = $("metrics-selected-wrap");
    const chips = $("metrics-selected-chips");
    chips.innerHTML = "";
    if (!state.selected.size) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    Array.from(state.selected).forEach((number) => {
      chips.appendChild(
        el("span", { class: "metrics-chip" }, [
          number,
          el("button", {
            type: "button",
            "aria-label": `Remove ${number}`,
            text: "×",
            onclick: () => toggleAsset(number, false),
          }),
        ])
      );
    });
  }

  function renderScopeHint() {
    const hint = $("metrics-scope-hint");
    if (state.selected.size) {
      // Use the selection size (not the payload) so the count is right even
      // during the brief window before a selection-triggered refetch resolves.
      hint.textContent = `Comparing ${state.selected.size} selected asset(s).`;
    } else {
      const total = state.payload && state.payload.assets ? state.payload.assets.length : state.assets.length;
      hint.textContent = `No assets selected — comparing all ${total} asset(s) by default.`;
    }
  }

  // ---- data + selection ----------------------------------------------------
  function visibleAssets() {
    const rows = (state.payload && state.payload.assets) || [];
    if (!state.selected.size) return rows;
    return rows.filter((row) => state.selected.has(row.asset_number));
  }

  async function loadMetrics() {
    const token = ++state.fetchToken;
    beginLoading();
    try {
      const params = new URLSearchParams();
      // Send the selected assets so the server computes the risk score (and, in
      // relative mode, the cross-asset ranking) over exactly the compared set —
      // not over hidden unselected assets. Empty selection = all assets.
      state.selected.forEach((assetNumber) => params.append("assets", assetNumber));
      if (state.dateFrom) params.set("start", state.dateFrom);
      if (state.dateTo) params.set("end", state.dateTo);
      const url = params.toString() ? `${METRICS_API}?${params.toString()}` : METRICS_API;
      const data = await getJson(url);
      if (token !== state.fetchToken) return; // a newer request superseded this one
      state.payload = data;
      state.dataWindow = data.data_window || { start: null, end: null };
      if (!state.selected.size && !state.dateFrom && !state.dateTo) {
        state.globalDataWindow = state.dataWindow;
      }
      initDateInputs();
      clearBanner();
      renderScopeHint();
      renderAll();
    } catch (err) {
      if (token !== state.fetchToken) return;
      state.payload = { assets: [] };
      showBanner(err.message || "Could not load metrics data.", "error");
      renderScopeHint();
      renderAll();
    } finally {
      endLoading();
    }
  }

  function initDateInputs() {
    // Default the date pickers to the data's own extent the first time we learn
    // it, without clobbering a range the user has already chosen.
    if (state.dateInitialized) return;
    const from = $("metrics-date-from");
    const to = $("metrics-date-to");
    if (state.dataWindow.start) {
      from.value = state.dataWindow.start;
      to.value = state.dataWindow.end || "";
      state.dateFrom = from.value;
      state.dateTo = to.value;
      state.dateInitialized = true;
    }
  }

  // ---- canvas helpers ------------------------------------------------------
  function setupCanvas(canvas, cssHeight) {
    const dpr = window.devicePixelRatio || 1;
    const parent = canvas.parentElement;
    const cssWidth = Math.max(220, Math.round((parent && parent.clientWidth) || canvas.clientWidth || 480));
    canvas.style.height = cssHeight + "px";
    canvas.width = Math.max(1, Math.round(cssWidth * dpr));
    canvas.height = Math.max(1, Math.round(cssHeight * dpr));
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, width: cssWidth, height: cssHeight };
  }

  function tickLabel(value) {
    const n = Number(value) || 0;
    if (Math.abs(n) >= 1000) return Math.round(n / 100) / 10 + "k";
    return n >= 100 ? String(Math.round(n)) : Number(n.toPrecision(3)).toString();
  }

  // Single-series bar chart. items = [{name, value, color?}].
  // Optional opts: { height, color, threshold, thresholdLabel, valueSuffix }.
  function drawBarChart(canvas, items, opts) {
    const options = opts || {};
    const height = options.height || 260;
    const { ctx, width: W, height: H } = setupCanvas(canvas, height);
    ctx.clearRect(0, 0, W, H);
    if (!items.length) return;

    const left = 52;
    const right = W - 14;
    const top = 16;
    const bottom = H - 64;
    const plotH = bottom - top;
    const slot = (right - left) / items.length;
    const dataMax = Math.max(...items.map((it) => Number(it.value) || 0), 0);
    const maxVal = Math.max(dataMax, options.threshold ? options.threshold * 1.1 : 0, 1);
    const barGap = Math.min(18, slot * 0.3);
    const barW = Math.max(6, slot - barGap);

    // gridlines + y labels
    const ticks = 5;
    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= ticks; i += 1) {
      const frac = i / ticks;
      const y = bottom - frac * plotH;
      ctx.strokeStyle = "#eef2f6";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.fillStyle = "#5e7082";
      ctx.textAlign = "right";
      ctx.fillText(tickLabel(maxVal * frac), left - 6, y);
    }
    ctx.textBaseline = "alphabetic";

    // axes
    ctx.strokeStyle = "#c4d2dd";
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    // bars + rotated asset labels + value labels
    items.forEach((item, index) => {
      const value = Number(item.value) || 0;
      const x = left + index * slot + barGap / 2;
      const barHeight = (value / maxVal) * plotH;
      const y = bottom - barHeight;
      ctx.fillStyle = item.color || options.color || BAR_COLOR;
      ctx.fillRect(x, y, barW, barHeight);

      ctx.fillStyle = "#33485a";
      ctx.font = "10px Inter, sans-serif";
      ctx.textAlign = "center";
      if (barHeight > 12) {
        ctx.fillText(tickLabel(value), x + barW / 2, y - 4);
      }

      ctx.save();
      ctx.fillStyle = "#5e7082";
      ctx.translate(x + barW / 2, bottom + 6);
      ctx.rotate(Math.PI / 5);
      ctx.textAlign = "left";
      ctx.fillText(String(item.name).slice(0, 16), 0, 0);
      ctx.restore();
    });

    // threshold line
    if (options.threshold) {
      const y = bottom - (options.threshold / maxVal) * plotH;
      ctx.strokeStyle = WARN;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = WARN;
      ctx.font = "10px Inter, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(options.thresholdLabel || `Threshold ${options.threshold}`, left + 4, y - 4);
    }
    ctx.textAlign = "left";
  }

  // Grouped two-series bar chart for current vs baseline comparisons.
  // items = [{name, current, baseline}]. series labels via opts.labels.
  function drawGroupedBars(canvas, items, opts) {
    const options = opts || {};
    const height = options.height || 260;
    const { ctx, width: W, height: H } = setupCanvas(canvas, height);
    ctx.clearRect(0, 0, W, H);
    if (!items.length) return;

    const left = 52;
    const right = W - 14;
    const top = 16;
    const bottom = H - 64;
    const plotH = bottom - top;
    const slot = (right - left) / items.length;
    const maxVal = Math.max(
      ...items.map((it) => Math.max(Number(it.current) || 0, Number(it.baseline) || 0)),
      1
    );
    const groupGap = Math.min(20, slot * 0.34);
    const groupW = Math.max(8, slot - groupGap);
    const barW = groupW / 2;

    const ticks = 5;
    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= ticks; i += 1) {
      const frac = i / ticks;
      const y = bottom - frac * plotH;
      ctx.strokeStyle = "#eef2f6";
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.fillStyle = "#5e7082";
      ctx.textAlign = "right";
      ctx.fillText(tickLabel(maxVal * frac), left - 6, y);
    }
    ctx.textBaseline = "alphabetic";

    ctx.strokeStyle = "#c4d2dd";
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    items.forEach((item, index) => {
      const baseX = left + index * slot + groupGap / 2;
      const baseVal = Number(item.baseline) || 0;
      const curVal = Number(item.current) || 0;
      const baseH = (baseVal / maxVal) * plotH;
      const curH = (curVal / maxVal) * plotH;
      ctx.fillStyle = BAR_COLOR_ALT;
      ctx.fillRect(baseX, bottom - baseH, barW, baseH);
      ctx.fillStyle = curVal >= baseVal && baseVal > 0 ? WARN : BAR_COLOR;
      ctx.fillRect(baseX + barW, bottom - curH, barW, curH);

      ctx.save();
      ctx.fillStyle = "#5e7082";
      ctx.font = "10px Inter, sans-serif";
      ctx.translate(baseX + groupW / 2, bottom + 6);
      ctx.rotate(Math.PI / 5);
      ctx.textAlign = "left";
      ctx.fillText(String(item.name).slice(0, 16), 0, 0);
      ctx.restore();
    });

    // legend
    const labels = options.labels || ["Baseline", "Current"];
    const legend = [
      { color: BAR_COLOR_ALT, text: labels[0] },
      { color: BAR_COLOR, text: labels[1] },
    ];
    let lx = left;
    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "middle";
    legend.forEach((entry) => {
      ctx.fillStyle = entry.color;
      ctx.fillRect(lx, top - 8, 10, 10);
      ctx.fillStyle = "#5e7082";
      ctx.textAlign = "left";
      ctx.fillText(entry.text, lx + 14, top - 3);
      lx += 18 + ctx.measureText(entry.text).width + 16;
    });
    ctx.textBaseline = "alphabetic";
  }

  // ---- empty-state helpers -------------------------------------------------
  function setEmpty(key, message) {
    const node = document.querySelector(`[data-empty="${key}"]`);
    if (!node) return;
    if (message) {
      node.textContent = message;
      node.hidden = false;
    } else {
      node.hidden = true;
    }
  }

  function noDataMessage() {
    if (state.payload && state.payload.assets && state.payload.assets.length && !visibleAssets().length) {
      return "No data for the selected assets in this date range.";
    }
    return "No corrective work order data is available for the selected filters.";
  }

  // ---- render: previews ----------------------------------------------------
  function renderKpiPreview() {
    const canvas = $("kpis-preview-chart");
    const empty = $("kpis-preview-empty");
    const rows = visibleAssets().filter((r) => r.mtbf_hours !== null && r.mtbf_hours !== undefined);
    const items = rows
      .map((r) => ({ name: r.asset_number, value: r.mtbf_hours }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 12);
    if (!items.length) {
      drawBarChart(canvas, [], { height: 160 });
      empty.textContent = visibleAssets().length
        ? "MTBF needs operating or scheduled work hours — Data Required."
        : noDataMessage();
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    drawBarChart(canvas, items, { height: 160 });
  }

  function renderAlertPreview() {
    const canvas = $("alerts-preview-chart");
    const empty = $("alerts-preview-empty");
    const items = visibleAssets()
      .map((r) => ({
        name: r.asset_number,
        value: r.risk_score || 0,
        color: (r.risk_score || 0) >= ALERT_THRESHOLD ? WARN : BAR_COLOR,
      }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 12);
    if (!items.length) {
      drawBarChart(canvas, [], { height: 160 });
      empty.textContent = noDataMessage();
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    drawBarChart(canvas, items, { height: 160, threshold: ALERT_THRESHOLD, thresholdLabel: "Alert 70" });
  }

  // ---- render: Operational KPIs expanded -----------------------------------
  function renderKpiExpanded() {
    const rows = visibleAssets();

    const mtbfItems = rows
      .filter((r) => r.mtbf_hours !== null && r.mtbf_hours !== undefined)
      .map((r) => ({ name: r.asset_number, value: r.mtbf_hours }));
    drawBarChart($("kpis-mtbf-chart"), mtbfItems);
    setEmpty(
      "kpis-mtbf",
      mtbfItems.length
        ? null
        : rows.length
        ? "Data Required — operating or scheduled work hours are not available to compute MTBF."
        : noDataMessage()
    );

    const mttrItems = rows
      .filter((r) => r.mttr_hours !== null && r.mttr_hours !== undefined)
      .map((r) => ({ name: r.asset_number, value: r.mttr_hours }));
    drawBarChart($("kpis-mttr-chart"), mttrItems);
    setEmpty("kpis-mttr", mttrItems.length ? null : noDataMessage());

    const wocItems = rows.map((r) => ({ name: r.asset_number, value: r.work_order_count }));
    drawBarChart($("kpis-woc-chart"), wocItems);
    setEmpty("kpis-woc", wocItems.length ? null : noDataMessage());

    const dtItems = rows.map((r) => ({ name: r.asset_number, value: r.total_downtime_hours }));
    drawBarChart($("kpis-downtime-chart"), dtItems);
    setEmpty("kpis-downtime", dtItems.length ? null : noDataMessage());

    renderKpiTable(rows);
  }

  function trendCell(trend) {
    if (trend === "up") return el("span", { class: "metrics-trend-up", text: "▲ Rising" });
    if (trend === "down") return el("span", { class: "metrics-trend-down", text: "▼ Falling" });
    return el("span", { class: "metrics-trend-flat", text: "▬ Flat" });
  }

  function renderKpiTable(rows) {
    const tbody = $("kpis-table").querySelector("tbody");
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.appendChild(
        el("tr", {}, [el("td", { class: "metrics-empty-row", colspan: "6", text: noDataMessage() })])
      );
      return;
    }
    rows.forEach((r) => {
      tbody.appendChild(
        el("tr", {}, [
          el("td", { text: r.asset_name ? `${r.asset_number} · ${r.asset_name}` : r.asset_number }),
          el("td", { text: r.mtbf_hours == null ? "Data Required" : fmtNum(r.mtbf_hours, 1) }),
          el("td", { text: r.mttr_hours == null ? "—" : fmtNum(r.mttr_hours, 2) }),
          el("td", { text: fmtNum(r.work_order_count, 0) }),
          el("td", { text: fmtNum(r.total_downtime_hours, 1) }),
          el("td", {}, [trendCell(r.trend)]),
        ])
      );
    });
  }

  // ---- render: Alerting Readiness expanded ---------------------------------
  function renderAlertExpanded() {
    const rows = visibleAssets();

    const riskItems = rows
      .map((r) => ({
        name: r.asset_number,
        value: r.risk_score || 0,
        color: (r.risk_score || 0) >= ALERT_THRESHOLD ? WARN : BAR_COLOR,
      }))
      .sort((a, b) => b.value - a.value);
    drawBarChart($("alerts-risk-chart"), riskItems, { threshold: ALERT_THRESHOLD, thresholdLabel: "Alert threshold (70)" });
    setEmpty("alerts-risk", riskItems.length ? null : noDataMessage());

    const spikeItems = rows
      .map((r) => ({ name: r.asset_number, current: r.current_downtime_hours, baseline: r.baseline_downtime_hours }))
      .filter((it) => (it.current || 0) > 0 || (it.baseline || 0) > 0);
    drawGroupedBars($("alerts-spike-chart"), spikeItems, { labels: ["Baseline downtime", "Current downtime"] });
    setEmpty(
      "alerts-spike",
      spikeItems.length
        ? null
        : rows.length
        ? "No dated downtime to compare across periods for the selected assets."
        : noDataMessage()
    );

    renderThresholdSummary(rows);
    renderAlertTable(rows);
  }

  function renderThresholdSummary(rows) {
    const node = $("alerts-threshold-summary");
    const flagged = rows.filter((r) => (r.risk_score || 0) >= ALERT_THRESHOLD);
    const mode = state.payload && state.payload.baseline_mode === "relative" ? " (scored relative to the other selected assets — no baseline period available)" : "";
    if (!rows.length) {
      node.textContent = noDataMessage();
      return;
    }
    if (!flagged.length) {
      node.textContent = `No assets currently exceed the alert threshold of ${ALERT_THRESHOLD}${mode}.`;
      return;
    }
    const names = flagged
      .sort((a, b) => (b.risk_score || 0) - (a.risk_score || 0))
      .map((r) => `${r.asset_number} (${fmtNum(r.risk_score, 0)})`)
      .join(", ");
    node.textContent = `${flagged.length} asset(s) exceed the alert threshold of ${ALERT_THRESHOLD}: ${names}${mode}.`;
  }

  function statusPill(status) {
    const cls = status === "High" ? "is-high" : status === "Medium" ? "is-medium" : "is-low";
    return el("span", { class: `metrics-pill ${cls}`, text: status });
  }

  function renderAlertTable(rows) {
    const tbody = $("alerts-table").querySelector("tbody");
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.appendChild(
        el("tr", {}, [el("td", { class: "metrics-empty-row", colspan: "7", text: noDataMessage() })])
      );
      return;
    }
    rows
      .slice()
      .sort((a, b) => (b.risk_score || 0) - (a.risk_score || 0))
      .forEach((r) => {
        tbody.appendChild(
          el("tr", {}, [
            el("td", { text: r.asset_name ? `${r.asset_number} · ${r.asset_name}` : r.asset_number }),
            el("td", { text: fmtNum(r.risk_score, 0) }),
            el("td", { text: r.downtime_change_pct == null ? "New" : fmtPct(r.downtime_change_pct) }),
            el("td", { text: r.wo_change_pct == null ? "New" : fmtPct(r.wo_change_pct) }),
            el("td", { text: r.mttr_change_pct == null ? "New" : fmtPct(r.mttr_change_pct) }),
            el("td", {}, [statusPill(r.status)]),
            el("td", { text: fmtNum(r.alert_count, 0) }),
          ])
        );
      });
  }

  // ---- render orchestration ------------------------------------------------
  function renderAll() {
    renderKpiPreview();
    renderAlertPreview();
    if (state.expanded === "kpis") renderKpiExpanded();
    if (state.expanded === "alerts") renderAlertExpanded();
  }

  // ---- card expand / collapse ----------------------------------------------
  function setExpanded(key) {
    // Only one card expanded at a time; clicking the open card collapses it.
    const next = state.expanded === key ? null : key;
    state.expanded = next;
    document.querySelectorAll(".metrics-card").forEach((card) => {
      const cardKey = card.getAttribute("data-card");
      const isOpen = cardKey === next;
      card.classList.toggle("is-expanded", isOpen);
      card.setAttribute("aria-expanded", isOpen ? "true" : "false");
      const expanded = $(`${cardKey}-expanded`);
      if (expanded) expanded.hidden = !isOpen;
    });
    // Draw lazily once the panel is visible (canvases have no width while hidden).
    if (next === "kpis") renderKpiExpanded();
    if (next === "alerts") renderAlertExpanded();
  }

  function wireCards() {
    document.querySelectorAll(".metrics-card").forEach((card) => {
      const key = card.getAttribute("data-card");
      const activate = () => setExpanded(key);
      card.addEventListener("click", (event) => {
        // Ignore clicks on interactive children inside the expanded body.
        if (event.target.closest("a, button, input, select, textarea, .metrics-table-scroll")) return;
        activate();
      });
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      });
    });
  }

  // ---- filter wiring -------------------------------------------------------
  // Shared debounce so rapid filter changes (date edits or multi-select clicks)
  // batch into a single refetch; the fetchToken guard drops any stale response.
  let reloadDebounce = null;
  function scheduleReload() {
    clearTimeout(reloadDebounce);
    reloadDebounce = setTimeout(loadMetrics, 220);
  }

  function wireFilters() {
    const search = $("metrics-asset-search");
    const menu = $("metrics-asset-menu");
    search.addEventListener("focus", () => {
      state.dropdownOpen = true;
      renderAssetMenu();
    });
    search.addEventListener("input", () => {
      state.assetQuery = search.value;
      state.dropdownOpen = true;
      renderAssetMenu();
    });
    document.addEventListener("click", (event) => {
      if (event.target === search || menu.contains(event.target)) return;
      if (state.dropdownOpen) {
        state.dropdownOpen = false;
        renderAssetMenu();
      }
    });

    const from = $("metrics-date-from");
    const to = $("metrics-date-to");
    const onDateChange = () => {
      state.dateFrom = from.value;
      state.dateTo = to.value;
      scheduleReload();
    };
    from.addEventListener("change", onDateChange);
    to.addEventListener("change", onDateChange);

    $("metrics-reset").addEventListener("click", () => {
      state.selected.clear();
      state.assetQuery = "";
      search.value = "";
      const resetWindow = state.globalDataWindow.start || state.globalDataWindow.end
        ? state.globalDataWindow
        : state.dataWindow;
      from.value = resetWindow.start || "";
      to.value = resetWindow.end || "";
      state.dateFrom = from.value;
      state.dateTo = to.value;
      renderSelectedChips();
      renderAssetMenu();
      loadMetrics();
    });
  }

  // Redraw the visible canvases on resize (debounced) so charts track the layout.
  let resizeRaf = null;
  function wireResize() {
    window.addEventListener("resize", () => {
      if (resizeRaf) cancelAnimationFrame(resizeRaf);
      resizeRaf = requestAnimationFrame(renderAll);
    });
  }

  // ---- init ----------------------------------------------------------------
  function init() {
    wireCards();
    wireFilters();
    wireResize();
    renderSelectedChips();
    // Assets (for the filter menu) and the metric payload load in parallel.
    loadAssets();
    loadMetrics();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
