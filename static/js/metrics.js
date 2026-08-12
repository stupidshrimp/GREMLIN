/* Metrics dashboard client.
 *
 * Drives the high-level equipment / maintenance reliability dashboard:
 *   - asset search + multi-select filter and a date range, shared by every card;
 *   - three clickable cards (Operational KPIs, Alerting Readiness, Availability)
 *     that show a small preview while collapsed and expand to full charts/tables;
 *   - all charts compare assets against each other (x-axis = asset, y-axis = the
 *     selected performance metric), with the bars ranked largest to smallest so
 *     the assets that need attention sit at the left edge of every chart.
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
  const AVAILABILITY_API = "/metrics/api/availability";
  const ALERT_THRESHOLD = 70;

  // Forest palette (matches theme.css) used for the comparison bars.
  const BAR_COLOR = "#3f5e77";
  const BAR_COLOR_ALT = "#7fa6c0";
  const ACCENT = "#2f8f5b";
  const WARN = "#c0392b";

  // Per-asset bar colours for the availability charts. Seven entries covers the
  // largest group (Salvagnini and Building 1 Secondary Finishing); the palette
  // wraps for anything larger.
  const SERIES_COLORS = [
    "#3f5e77", "#7fa6c0", "#2f8f5b", "#8fbf6a",
    "#c9a227", "#a1668f", "#5b7f8c",
  ];
  const AVERAGE_LINE = "#1f4d33";
  const GOAL_LINE = "#c0392b";
  const TYPE_COLORS = ["#c0392b", "#c9a227", "#a1668f", "#7fa6c0", "#8a6d00", "#5b7f8c", "#7b8794"];

  const state = {
    assets: [], // [{asset_number, asset_name}]
    selected: new Set(), // asset_number values; empty = all
    payload: null, // last /metrics/api/reliability response
    expanded: null, // currently expanded card key or null
    dropdownOpen: false,
    assetQuery: "",
    // Availability is fetched separately and shares none of the filter state:
    // the card always shows every configured group over whole months.
    availability: null,
    availabilityWindow: 5,
    availabilityStacked: false,
    availabilityError: "",
    // A misconfiguration the page can compute around but must not hide -- kept
    // separate from availabilityError so a warning never reads as a failure,
    // and never stands in for the empty-state explanation.
    availabilityWarning: "",
    // Per-group table mode: "availability" (read-only) or "ot" (editable).
    availabilityTableMode: {},
    // Open availability drill-down, or null. Holds which group is being read,
    // which bar was clicked, and how its table is currently sorted/filtered --
    // not the rows themselves, so a refresh underneath it redraws live data
    // rather than a snapshot taken when it opened.
    availabilityDetail: null,
    // Asset numbers the Availability config considers in scope. Seeds the KPI /
    // Alerts default comparison set, so the plant equipment list lives in one
    // place (the availability config) rather than being duplicated here.
    defaultAssets: [],
    // Active API date filters. The inputs may display the default data extent,
    // but these stay empty until the user explicitly changes the date range so
    // the backend can keep its default unbounded semantics.
    dateFrom: "",
    dateTo: "",
    dataWindow: { start: null, end: null },
    fetchToken: 0,
  };

  const $ = (id) => document.getElementById(id);

  // A fresh copy of the default equipment selection so callers can mutate the
  // returned set without disturbing the canonical list. The list comes from the
  // Availability configuration, which is the single source of truth for which
  // assets the plant reports on; before it loads this is empty, which the API
  // reads as "every asset".
  function defaultSelection() {
    return new Set(state.defaultAssets);
  }

  // True while the active selection is exactly the curated default set, so the
  // scope hint can say "default" instead of a bare count.
  function selectionIsDefault() {
    if (!state.defaultAssets.length) return false;
    if (state.selected.size !== state.defaultAssets.length) return false;
    return state.defaultAssets.every((number) => state.selected.has(number));
  }

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
    if (selectionIsDefault()) {
      // Use the selection size (not the payload) so the count is right even
      // during the brief window before a selection-triggered refetch resolves.
      hint.textContent =
        `Showing the default equipment set (${state.selected.size} assets). ` +
        "Search to add others, remove a chip to narrow, or clear all to compare every asset.";
    } else if (state.selected.size) {
      hint.textContent = `Comparing ${state.selected.size} selected asset(s).`;
    } else {
      const total = state.payload && state.payload.assets ? state.payload.assets.length : state.assets.length;
      hint.textContent = `No assets selected — comparing all ${total} asset(s).`;
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
      syncDateInputs();
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

  function syncDateInputs() {
    // While no explicit date filter is active, the From/To controls only display
    // the current data extent as a hint — they are not sent to the API. Re-sync
    // them to whatever the active asset selection returned on each fetch so the
    // hint never goes stale relative to what's shown. Otherwise, after the
    // selection widens (e.g. clearing the default chips to compare every asset),
    // editing a single field would commit the previous, narrower window's
    // opposite bound and silently truncate the comparison.
    if (state.dateFrom || state.dateTo) return; // respect a user-set range
    if (!state.dataWindow.start) return; // nothing meaningful to display yet
    $("metrics-date-from").value = state.dataWindow.start;
    $("metrics-date-to").value = state.dataWindow.end || "";
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

  // Every comparison chart is read left-to-right as a ranking, so bars are
  // always ordered largest to smallest. Returns a sorted copy — the payload rows
  // behind `items` must keep the natural asset-number order the API sent them
  // in, which the (stable) sort then reuses as the tie-break for equal values.
  // `valueOf` reads the magnitude to rank by; it defaults to `item.value` for
  // single-series charts.
  function sortedDesc(items, valueOf) {
    const magnitude = valueOf || ((item) => item.value);
    return items.slice().sort((a, b) => (Number(magnitude(b)) || 0) - (Number(magnitude(a)) || 0));
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

  // ---- scrollable table regions --------------------------------------------
  // Capping the wrapper's height is what makes the sticky headers work, but it
  // also puts rows behind a scrollbar — and a scroll container is only in the
  // tab order in some browsers (Firefox and recent Chrome, not Safari or older
  // Chrome). Without a tab stop those rows are unreachable by keyboard, so the
  // wrapper gets one, named after the panel it belongs to.
  //
  // Only while it actually scrolls, though: a focusable element that scrolls
  // nowhere is a dead stop between the reader and the next real control, and
  // these tables are short far more often than not. Both axes count — the
  // availability table hides months off the right the same way the KPI table
  // hides assets off the bottom.
  function markScrollRegion(wrap, label) {
    // A pixel of tolerance: sub-pixel row heights round the scroll size just
    // past the client size on tables that visibly fit.
    const scrolls =
      wrap.scrollHeight > wrap.clientHeight + 1 || wrap.scrollWidth > wrap.clientWidth + 1;
    if (!scrolls) {
      wrap.removeAttribute("tabindex");
      wrap.removeAttribute("role");
      wrap.removeAttribute("aria-label");
      return;
    }
    wrap.setAttribute("tabindex", "0");
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", `${label} (scrollable)`);
  }

  function syncScrollRegions() {
    document.querySelectorAll(".metrics-card .metrics-table-scroll").forEach((wrap) => {
      const panel = wrap.closest(".metrics-panel");
      const heading = panel ? panel.querySelector("h3") : null;
      markScrollRegion(wrap, heading ? heading.textContent.trim() : "Table");
    });
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

  // ---- chart hover tooltip -------------------------------------------------
  // One node reused by every chart, parked on <body>: a tooltip inside a card
  // would be clipped by the card's rounded corners and by the table wrappers'
  // overflow, and would have to be rebuilt every time a panel re-renders.
  let tooltipNode = null;

  function tooltipEl() {
    if (!tooltipNode) {
      tooltipNode = el("div", { class: "metrics-tooltip", role: "tooltip" });
      tooltipNode.hidden = true;
      document.body.appendChild(tooltipNode);
    }
    return tooltipNode;
  }

  function hideTooltip() {
    if (tooltipNode) tooltipNode.hidden = true;
  }

  // Anything can hide the tooltip out from under a hover — a scroll, a resize —
  // so a chart cannot infer that it is still showing from its own hover state.
  function tooltipVisible() {
    return Boolean(tooltipNode) && !tooltipNode.hidden;
  }

  // Repositions the tooltip without rebuilding it: the cursor moving across a
  // bar it is already describing fires a mousemove per pixel, and rebuilding the
  // contents on each one is a dozen DOM nodes thrown away per frame.
  function moveTooltip(clientX, clientY) {
    if (tooltipVisible()) positionTooltip(clientX, clientY);
  }

  // Offsets the tooltip from the cursor, flipping to the other side once it
  // would run past the viewport rather than letting it clip at the edge.
  function positionTooltip(clientX, clientY) {
    const node = tooltipEl();
    const gap = 16;
    const box = node.getBoundingClientRect();
    let x = clientX + gap;
    let y = clientY + gap;
    if (x + box.width > window.innerWidth - 8) x = clientX - gap - box.width;
    if (y + box.height > window.innerHeight - 8) y = clientY - gap - box.height;
    node.style.left = Math.max(8, x) + "px";
    node.style.top = Math.max(8, y) + "px";
  }

  // content = { title, color, subtitle, rows: [[label, value], …], notes: [] }
  function showTooltip(clientX, clientY, content) {
    const node = tooltipEl();
    node.innerHTML = "";
    node.appendChild(
      el("p", { class: "metrics-tooltip-title" }, [
        content.color
          ? el("span", { class: "metrics-tooltip-swatch", style: `background:${content.color}` })
          : null,
        content.title,
      ])
    );
    if (content.subtitle) node.appendChild(el("p", { class: "metrics-tooltip-sub", text: content.subtitle }));
    const rows = el("dl", { class: "metrics-tooltip-rows" });
    (content.rows || []).forEach(([label, value]) => {
      rows.appendChild(el("dt", { text: label }));
      rows.appendChild(el("dd", { text: value }));
    });
    node.appendChild(rows);
    (content.notes || []).forEach((note) => {
      node.appendChild(el("p", { class: "metrics-tooltip-note", text: note }));
    });
    node.hidden = false;
    // Measure after the content is in place, or the flip test uses the previous
    // tooltip's box and a tall one still runs off the bottom of the window.
    positionTooltip(clientX, clientY);
  }

  // ---- render: previews ----------------------------------------------------
  function renderKpiPreview() {
    const canvas = $("kpis-preview-chart");
    const empty = $("kpis-preview-empty");
    const rows = visibleAssets().filter((r) => r.mtbf_hours !== null && r.mtbf_hours !== undefined);
    const items = sortedDesc(rows.map((r) => ({ name: r.asset_number, value: r.mtbf_hours }))).slice(0, 12);
    if (!items.length) {
      drawBarChart(canvas, [], { height: 160 });
      empty.textContent = visibleAssets().length
        ? "MTBF needs at least two dated corrective work orders — Data Required."
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
    const items = sortedDesc(
      visibleAssets().map((r) => ({
        name: r.asset_number,
        value: r.risk_score || 0,
        color: (r.risk_score || 0) >= ALERT_THRESHOLD ? WARN : BAR_COLOR,
      }))
    ).slice(0, 12);
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

    const mtbfItems = sortedDesc(
      rows
        .filter((r) => r.mtbf_hours !== null && r.mtbf_hours !== undefined)
        .map((r) => ({ name: r.asset_number, value: r.mtbf_hours }))
    );
    drawBarChart($("kpis-mtbf-chart"), mtbfItems);
    setEmpty(
      "kpis-mtbf",
      mtbfItems.length
        ? null
        : rows.length
        ? "Data Required — at least two dated corrective work orders are required to compute MTBF."
        : noDataMessage()
    );

    const mttrItems = sortedDesc(
      rows
        .filter((r) => r.mttr_hours !== null && r.mttr_hours !== undefined)
        .map((r) => ({ name: r.asset_number, value: r.mttr_hours }))
    );
    drawBarChart($("kpis-mttr-chart"), mttrItems);
    setEmpty("kpis-mttr", mttrItems.length ? null : noDataMessage());

    const wocItems = sortedDesc(rows.map((r) => ({ name: r.asset_number, value: r.work_order_count })));
    drawBarChart($("kpis-woc-chart"), wocItems);
    setEmpty("kpis-woc", wocItems.length ? null : noDataMessage());

    const dtItems = sortedDesc(rows.map((r) => ({ name: r.asset_number, value: r.total_downtime_hours })));
    drawBarChart($("kpis-downtime-chart"), dtItems);
    setEmpty("kpis-downtime", dtItems.length ? null : noDataMessage());

    renderKpiTable(rows);
    syncScrollRegions();
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

    const riskItems = sortedDesc(
      rows.map((r) => ({
        name: r.asset_number,
        value: r.risk_score || 0,
        color: (r.risk_score || 0) >= ALERT_THRESHOLD ? WARN : BAR_COLOR,
      }))
    );
    drawBarChart($("alerts-risk-chart"), riskItems, { threshold: ALERT_THRESHOLD, thresholdLabel: "Alert threshold (70)" });
    setEmpty("alerts-risk", riskItems.length ? null : noDataMessage());

    // Each group here is two bars, so rank by whichever period is taller: that
    // keeps the chart descending left-to-right like the single-series ones, and
    // an asset that improved sharply still sorts by the downtime it used to
    // carry instead of dropping into the tail where the drop goes unnoticed.
    const spikeItems = sortedDesc(
      rows
        .map((r) => ({ name: r.asset_number, current: r.current_downtime_hours, baseline: r.baseline_downtime_hours }))
        .filter((it) => (it.current || 0) > 0 || (it.baseline || 0) > 0),
      (it) => Math.max(Number(it.current) || 0, Number(it.baseline) || 0)
    );
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
    syncScrollRegions();
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

  // ---- availability --------------------------------------------------------
  // The availability chart is the one shape the shared helpers above cannot
  // draw: N bars per month (one per asset) with an Average and a Goal line
  // overlaid. The y-axis is pinned to 0-100% to match the workbook, which fixes
  // its value axis rather than auto-scaling.
  function drawAvailabilityChart(canvas, group, opts) {
    const options = opts || {};
    const height = options.height || 300;
    const { ctx, width: W, height: H } = setupCanvas(canvas, height);
    ctx.clearRect(0, 0, W, H);

    // Hit-testing data for the hover tooltip, republished on every draw because
    // a resize or a highlight redraw moves every bar.
    const hits = { width: W, height: H, regions: [] };
    canvas._availabilityHits = hits;

    const labels = group.month_labels || [];
    const assets = group.assets || [];
    if (!labels.length || !assets.length) return;

    const highlight = options.highlight || null;
    const stacked = Boolean(options.stacked);
    const rowsByKey = options.rowsByKey || {};
    const typeNames = options.typeNames || [];

    const left = 52;
    const right = W - 12;
    // Stacked bars still sit side-by-side in asset order. Asset colours are
    // therefore retained as bar outlines and as outlined legend keys, while
    // filled keys explain the work-order segments inside those outlines.
    const entries = (stacked
      ? assets
          .map((asset, index) => ({
            color: SERIES_COLORS[index % SERIES_COLORS.length],
            text: asset.display_name,
            outline: true,
          }))
          .concat([{ color: "#dceee4", text: "Available" }])
          .concat(typeNames.map((name, index) => ({
            color: TYPE_COLORS[index % TYPE_COLORS.length], text: name,
          })))
      : assets.map((asset, index) => ({
        color: SERIES_COLORS[index % SERIES_COLORS.length], text: asset.display_name,
      })))
      .concat([{ color: AVERAGE_LINE, text: "Average" }, { color: GOAL_LINE, text: "Goal %" }]);

    ctx.font = "10px Inter, sans-serif";
    let legendRows = 1;
    let legendWidth = 0;
    entries.forEach((entry) => {
      const width = 14 + ctx.measureText(entry.text).width + 12;
      if (legendWidth && legendWidth + width > right - left) {
        legendRows += 1;
        legendWidth = 0;
      }
      legendWidth += width;
    });
    const top = Math.max(26, 8 + legendRows * 12);
    const bottom = H - 46;
    const plotH = Math.max(1, bottom - top);
    const slot = (right - left) / labels.length;
    // gapWidth 75 in the workbook: the gap is 75% of one bar's width.
    const groupW = slot / (1 + 0.75 / assets.length);
    const barW = Math.max(2, groupW / assets.length);

    const y = (value) => bottom - Math.max(0, Math.min(1, value)) * plotH;

    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 5; i += 1) {
      const frac = i / 5;
      const gy = bottom - frac * plotH;
      ctx.strokeStyle = "#eef2f6";
      ctx.beginPath();
      ctx.moveTo(left, gy);
      ctx.lineTo(right, gy);
      ctx.stroke();
      ctx.fillStyle = "#5e7082";
      ctx.textAlign = "right";
      ctx.fillText(Math.round(frac * 100) + "%", left - 6, gy);
    }
    ctx.textBaseline = "alphabetic";

    ctx.strokeStyle = "#c4d2dd";
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    // With a long window the month labels are both wider (they carry the year
    // once the window spans one) and closer together, so draw every nth to keep
    // them legible instead of letting them overlap into a smear.
    ctx.font = "10px Inter, sans-serif";
    const widestLabel = labels.reduce((w, label) => Math.max(w, ctx.measureText(label).width), 0);
    const labelStep = Math.max(1, Math.ceil((widestLabel + 8) / slot));

    // Bars, grouped by month.
    labels.forEach((label, monthIndex) => {
      const baseX = left + monthIndex * slot + (slot - groupW) / 2;
      assets.forEach((asset, assetIndex) => {
        const value = asset.values[monthIndex];
        if (value === null || value === undefined) return;
        const x = baseX + assetIndex * barW;
        const w = Math.max(1, barW - 1);
        const barTop = y(value);
        const isHot = highlight && highlight.monthIndex === monthIndex && highlight.assetIndex === assetIndex;
        // The hit box is the bar's full column slice, not the drawn bar: a bad
        // month is a couple of pixels tall and would be impossible to point at.
        hits.regions.push({ x, w, top, bottom, monthIndex, assetIndex });
        if (isHot) {
          ctx.fillStyle = "rgba(47, 143, 91, 0.1)";
          ctx.fillRect(x, top, w, bottom - top);
        }
        if (stacked) {
          // Availability occupies the lower portion and each WO class occupies
          // its share of scheduled time above it, making the complete column
          // read as 100%. Overrun is clamped just like the availability value.
          ctx.fillStyle = "#dceee4";
          ctx.fillRect(x, barTop, w, bottom - barTop);
          const month = (options.months || [])[monthIndex];
          const row = rowsByKey[`${asset.asset_number}|${month}`];
          const scheduled = Number(row && row.adjusted_scheduled_hours) || 0;
          let cursor = value;
          typeNames.forEach((name, typeIndex) => {
            const hours = Number(row && row.work_order_type_hours && row.work_order_type_hours[name]) || 0;
            const fraction = scheduled > 0 ? Math.min(hours / scheduled, Math.max(0, 1 - cursor)) : 0;
            if (fraction <= 0) return;
            const next = cursor + fraction;
            ctx.fillStyle = TYPE_COLORS[typeIndex % TYPE_COLORS.length];
            ctx.fillRect(x, y(next), w, Math.max(1, y(cursor) - y(next)));
            cursor = next;
          });
          // The outline identifies the asset without stealing fill colour from
          // the WO-type percentages. This remains usable on touch devices,
          // where the hover tooltip is not available.
          ctx.strokeStyle = SERIES_COLORS[assetIndex % SERIES_COLORS.length];
          ctx.lineWidth = 1.5;
          ctx.strokeRect(x + 0.5, top + 0.5, Math.max(1, w - 1), Math.max(1, bottom - top - 1));
          ctx.lineWidth = 1;
        } else {
          ctx.fillStyle = SERIES_COLORS[assetIndex % SERIES_COLORS.length];
          ctx.fillRect(x, barTop, w, bottom - barTop);
        }
        // A flagged month is one where downtime exceeded scheduled hours, so
        // the bar sits at zero. Mark it rather than let it read as "no data".
        if (asset.flagged && asset.flagged[monthIndex]) {
          ctx.fillStyle = WARN;
          ctx.fillRect(x, bottom - 3, w, 3);
        }
        // Outline rather than recolour the hovered bar: the series colour is how
        // the legend names it, so it has to survive the hover.
        if (isHot) {
          ctx.strokeStyle = AVERAGE_LINE;
          ctx.lineWidth = 1.5;
          ctx.strokeRect(x + 0.5, barTop + 0.5, Math.max(1, w - 1), Math.max(1, bottom - barTop - 1));
          ctx.lineWidth = 1;
        }
      });

      // Always label the last month; it is the one a reader looks for first.
      const isLast = monthIndex === labels.length - 1;
      if (monthIndex % labelStep === 0 || isLast) {
        ctx.fillStyle = "#5e7082";
        ctx.font = "10px Inter, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(label, left + monthIndex * slot + slot / 2, bottom + 16);
      }
    });

    // Average and Goal lines, centred on each month's group of bars.
    const centre = (index) => left + index * slot + slot / 2;
    const drawLine = (values, color, dashed) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.setLineDash(dashed ? [5, 4] : []);
      ctx.beginPath();
      let started = false;
      values.forEach((value, index) => {
        if (value === null || value === undefined) {
          started = false;
          return;
        }
        const px = centre(index);
        const py = y(value);
        if (!started) {
          ctx.moveTo(px, py);
          started = true;
        } else {
          ctx.lineTo(px, py);
        }
      });
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.lineWidth = 1;
      if (dashed) return;
      ctx.fillStyle = color;
      values.forEach((value, index) => {
        if (value === null || value === undefined) return;
        ctx.beginPath();
        ctx.arc(centre(index), y(value), 2.5, 0, Math.PI * 2);
        ctx.fill();
      });
    };
    drawLine(group.average || [], AVERAGE_LINE, false);
    drawLine(group.goal || [], GOAL_LINE, true);

    // Legend across the top. Outlined keys identify the side-by-side assets in
    // stacked mode; filled keys identify the segments within each asset bar.
    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    let lx = left;
    let ly = 10;
    entries.forEach((entry) => {
      const w = 14 + ctx.measureText(entry.text).width + 12;
      if (lx > left && lx + w > right) {
        lx = left;
        ly += 12;
      }
      if (entry.outline) {
        ctx.strokeStyle = entry.color;
        ctx.lineWidth = 1.5;
        ctx.strokeRect(lx + 0.5, ly - 3.5, 8, 8);
        ctx.lineWidth = 1;
      } else {
        ctx.fillStyle = entry.color;
        ctx.fillRect(lx, ly - 4, 9, 9);
      }
      ctx.fillStyle = "#5e7082";
      ctx.fillText(entry.text, lx + 13, ly);
      lx += w;
    });
    ctx.textBaseline = "alphabetic";
  }

  // The chart plots one number per bar; everything that explains it — the hours
  // it was computed from, the work orders behind it, and the caveats the table
  // carries as cell titles — only exists in the row detail. The tooltip is what
  // puts them together at the bar the reader is actually looking at.
  function availabilityTooltipContent(group, data, hit, byKey) {
    const asset = (group.assets || [])[hit.assetIndex];
    if (!asset) return null;
    const month = (data.months || [])[hit.monthIndex];
    const row = byKey[`${asset.asset_number}|${month}`];
    const value = asset.values[hit.monthIndex];

    const rows = [
      ["Availability", value === null || value === undefined ? "—" : `${(value * 100).toFixed(2)}%`],
    ];
    const notes = [];

    if (row) {
      const ot = Number(row.manual_ot_hours) || 0;
      rows.push([
        "Scheduled",
        `${fmtNum(row.adjusted_scheduled_hours, 1)} h` + (ot > 0 ? ` (incl. ${fmtNum(ot, 1)} h OT)` : ""),
      ]);
      const linked = Number(row.linked_downtime_hours) || 0;
      rows.push([
        "Downtime",
        `${fmtNum(row.adjusted_downtime_hours, 1)} h` + (linked > 0 ? ` (${fmtNum(linked, 1)} h linked)` : ""),
      ]);
      const zero = Number(row.zero_downtime_wo_count) || 0;
      rows.push([
        "Work orders",
        `${fmtNum(row.total_wo_count, 0)}` + (zero > 0 ? ` (${fmtNum(zero, 0)} with no downtime)` : ""),
      ]);
      if (state.availabilityStacked) {
        const scheduled = Number(row.adjusted_scheduled_hours) || 0;
        Object.entries(row.work_order_type_hours || {})
          .filter(([, hours]) => Number(hours) > 0)
          .sort((a, b) => Number(b[1]) - Number(a[1]))
          .forEach(([name, hours]) => rows.push([
            name,
            `${fmtNum(hours, 1)} h${scheduled > 0 ? ` · ${(Number(hours) / scheduled * 100).toFixed(2)}%` : ""}`,
          ]));
      }
    }

    const average = (group.average || [])[hit.monthIndex];
    if (average !== null && average !== undefined) {
      rows.push(["Group average", `${(average * 100).toFixed(2)}%`]);
    }
    const goal = (group.goal || [])[hit.monthIndex];
    if (goal !== null && goal !== undefined) {
      rows.push(["Goal", `${(goal * 100).toFixed(2)}%`]);
    }

    if (row && row.flagged) {
      notes.push(
        `Downtime ${fmtNum(row.adjusted_downtime_hours, 1)} h exceeded scheduled ` +
          `${fmtNum(row.adjusted_scheduled_hours, 1)} h — clamped to 0%.`
      );
    }
    // 100% off no work orders at all is arithmetically the same as a genuinely
    // perfect month, so the bar has to say which one it is.
    if (row && row.no_wo_entries) notes.push(row.note || "No WO entries this month.");
    if (row && row.overlap_count > 0) {
      notes.push(`${row.overlap_count} work order(s) cross a month boundary.`);
    }

    return {
      title: asset.display_name || asset.asset_number,
      color: SERIES_COLORS[hit.assetIndex % SERIES_COLORS.length],
      subtitle: `${group.asset_group} · ${(group.month_labels || [])[hit.monthIndex] || ""}`,
      rows,
      notes,
    };
  }

  // Resolves a pointer position to the bar under it. Shared by the hover tooltip
  // and by the click that opens the drill-down, so the row the modal lands on is
  // always the one the tooltip was naming.
  function availabilityHitAt(canvas, event) {
    const hits = canvas._availabilityHits;
    if (!hits) return null;
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    // The canvas is laid out at 100% width, so its rendered size can drift
    // from the size it was drawn at between a resize and the redraw.
    const x = (event.clientX - box.left) * (hits.width / box.width);
    const y = (event.clientY - box.top) * (hits.height / box.height);
    // Scanned back to front, because the regions are pushed in paint order and
    // the last bar drawn over a pixel is the one the reader sees there. The
    // two only diverge when a group is wider than its month slot -- a bar has
    // a 2px floor so it stays visible, which a long window on a narrow screen
    // can push past the slot into the following months -- but when they do
    // diverge, a front-to-back scan names a bar that is buried out of sight.
    for (let index = hits.regions.length - 1; index >= 0; index -= 1) {
      const region = hits.regions[index];
      if (x >= region.x && x <= region.x + region.w && y >= region.top && y <= region.bottom) {
        return region;
      }
    }
    return null;
  }

  function wireAvailabilityChart(canvas, group, data) {
    // Built once per draw rather than per mousemove: the same lookup the table
    // builds, over the rows this group already carries.
    const byKey = {};
    (group.rows || []).forEach((row) => {
      byKey[`${row.asset_number}|${row.month}`] = row;
    });
    let hovered = null;

    const hitAt = (event) => availabilityHitAt(canvas, event);
    const typeNames = availabilityTypeNames(data);
    const draw = (highlight) => drawAvailabilityChart(canvas, group, {
      height: 300, highlight, stacked: state.availabilityStacked,
      rowsByKey: byKey, typeNames, months: data.months || [],
    });

    canvas.addEventListener("mousemove", (event) => {
      const hit = hitAt(event);
      // Every bar opens something, so the pointer has to say so over a bar and
      // stop saying so over the whitespace between them.
      canvas.style.cursor = hit ? "pointer" : "";
      if (!hit) {
        if (hovered) {
          hovered = null;
          draw();
        }
        hideTooltip();
        return;
      }
      // Only redraw and rebuild when the hovered bar changes; doing either per
      // mousemove would repaint the whole chart dozens of times crossing a
      // single bar. Within one bar the tooltip just follows the cursor — but
      // only while it is actually showing. Scrolling the page under a resting
      // pointer hides it without changing which bar is hovered, and taking the
      // cheap path then would leave the tooltip unrecoverable until the pointer
      // crossed into a different bar.
      if (
        hovered &&
        hovered.monthIndex === hit.monthIndex &&
        hovered.assetIndex === hit.assetIndex &&
        tooltipVisible()
      ) {
        moveTooltip(event.clientX, event.clientY);
        return;
      }
      hovered = hit;
      draw(hit);
      const content = availabilityTooltipContent(group, data, hit, byKey);
      if (content) showTooltip(event.clientX, event.clientY, content);
      else hideTooltip();
    });

    canvas.addEventListener("mouseleave", () => {
      if (hovered) {
        hovered = null;
        draw();
      }
      hideTooltip();
    });

    canvas.addEventListener("click", (event) => {
      const hit = hitAt(event);
      if (!hit) return;
      const asset = (group.assets || [])[hit.assetIndex];
      const month = (data.months || [])[hit.monthIndex];
      if (!asset || !month) return;
      // No stopPropagation needed: the card's own click handler already ignores
      // anything that lands in an open card's body, and this chart only exists
      // while the card is open. Stopping it here would instead swallow the
      // document-level click that closes the asset dropdown.
      openAvailabilityDetail(group.asset_group, { asset_number: asset.asset_number, month });
    });
  }

  // ---- availability drill-down ---------------------------------------------
  // A bar is one number standing in for a month of scheduled hours, downtime and
  // work order activity, and when one of them looks wrong the next question is
  // always which of those inputs produced it. Clicking a bar opens the rows the
  // chart was drawn from -- the whole group, not just the bar, because an
  // outlier is only an outlier next to the months and machines around it.

  // One list drives the header, the sort and the body, so a column can never end
  // up sorted by a different number than the one it displays. `sort` returns the
  // comparable behind the cell; ties fall back to the chart's own order.
  const DETAIL_COLUMNS = [
    {
      key: "asset",
      label: "Asset",
      sort: (item) => item.row.display_name || item.row.asset_number,
      cell: (item) =>
        el("td", { class: "availability-detail-asset" }, [
          el("span", { text: item.row.display_name || item.row.asset_number }),
          item.row.display_name && item.row.display_name !== item.row.asset_number
            ? el("span", { class: "availability-detail-sub", text: item.row.asset_number })
            : null,
        ]),
    },
    {
      key: "month",
      label: "Month",
      sort: (item) => item.monthIndex,
      cell: (item) => el("td", { text: item.monthLabel }),
    },
    {
      key: "availability",
      label: "Availability",
      numeric: true,
      sort: (item) => item.availability,
      cell: (item) => {
        const attrs = { class: "availability-cell" };
        if (item.row.flagged) attrs.class += " is-flagged";
        else if (item.row.no_wo_entries) attrs.class += " is-no-data";
        attrs.text = item.availability == null ? "—" : `${(item.availability * 100).toFixed(2)}%`;
        return el("td", attrs);
      },
    },
    {
      key: "delta",
      label: "vs group avg",
      numeric: true,
      // The group average is what makes a month readable as unusual rather than
      // merely low: a 91% in a group averaging 92% is a normal month, and the
      // same 91% in a group averaging 99% is the thing being looked for.
      sort: (item) => item.delta,
      cell: (item) => {
        if (item.delta == null) return el("td", { class: "availability-cell", text: "—" });
        const sign = item.delta > 0 ? "+" : item.delta < 0 ? "−" : "";
        const cls = item.delta < -0.005 ? " is-below" : item.delta > 0.005 ? " is-above" : "";
        return el("td", {
          class: `availability-cell availability-detail-delta${cls}`,
          text: `${sign}${Math.abs(item.delta).toFixed(2)} pts`,
        });
      },
    },
    { key: "scheduled", label: "Scheduled (h)", numeric: true, field: "scheduled_hours", digits: 1 },
    { key: "ot", label: "OT (h)", numeric: true, field: "manual_ot_hours", digits: 1 },
    { key: "adj_scheduled", label: "Adj. scheduled (h)", numeric: true, field: "adjusted_scheduled_hours", digits: 1 },
    { key: "direct", label: "Direct downtime (h)", numeric: true, field: "direct_downtime_hours", digits: 2 },
    { key: "linked", label: "Linked downtime (h)", numeric: true, field: "linked_downtime_hours", digits: 2 },
    { key: "downtime", label: "Total downtime (h)", numeric: true, field: "adjusted_downtime_hours", digits: 2 },
    {
      key: "wo",
      label: "Work orders",
      numeric: true,
      field: "total_wo_count",
      digits: 0,
      // The count doubles as the way in. Someone asking which work orders made a
      // month look like this is already pointing at how many of them there were,
      // and a row with none is worth opening too: it is the difference between a
      // machine that ran and a machine nobody logged.
      cell: (item) =>
        el("td", { class: "availability-cell" }, [
          el("button", {
            type: "button",
            class: "availability-wo-link",
            "data-focus-id": `wo:${item.key}`,
            text: fmtNum(item.row.total_wo_count, 0),
            title:
              `List the work orders behind ${item.row.display_name || item.row.asset_number}` +
              ` · ${item.monthLabel}`,
            onclick: () => showWorkOrders(item.row.asset_number, item.row.month),
          }),
        ]),
    },
    { key: "zero_wo", label: "WOs w/o downtime", numeric: true, field: "zero_downtime_wo_count", digits: 0 },
    { key: "overlap", label: "Cross-month WOs", numeric: true, field: "overlap_count", digits: 0 },
    {
      key: "notes",
      label: "Notes",
      // The notes are {kind, text} objects, so they have to be reduced to their
      // text before joining: coercing them straight to string yields a row of
      // "[object Object]" whose only remaining signal is how many notes a row
      // carries, which sorts a flagged month and a linked-downtime month as
      // equal and leaves them in whatever order they arrived.
      sort: (item) => item.notes.map((note) => note.text).join(" "),
      cell: (item) =>
        el(
          "td",
          { class: "availability-detail-notes" },
          item.notes.length
            ? item.notes.map((note) => el("span", { class: `availability-detail-flag is-${note.kind}`, text: note.text }))
            : [el("span", { class: "availability-detail-quiet", text: "—" })]
        ),
    },
  ];

  // The plain numeric columns are all the same cell, so they declare a field and
  // a precision instead of repeating a builder each. A column that brings its
  // own cell keeps it and still gets the field's sort, so the two can never
  // disagree about which number the column is.
  DETAIL_COLUMNS.forEach((column) => {
    if (!column.field) return;
    column.sort = (item) => {
      const value = Number(item.row[column.field]);
      return isFinite(value) ? value : null;
    };
    if (column.cell) return;
    column.cell = (item) =>
      el("td", { class: "availability-cell", text: fmtNum(item.row[column.field], column.digits) });
  });

  function availabilityGroup(name) {
    const groups = (state.availability && state.availability.groups) || [];
    return groups.find((group) => group.asset_group === name) || null;
  }

  // The flags a bar cannot show. Each one is a case where the number alone reads
  // as an ordinary month and is not one, which is exactly what this table exists
  // to surface.
  function detailNotes(row) {
    const notes = [];
    if (row.flagged) {
      notes.push({
        kind: "flagged",
        text:
          `Downtime ${fmtNum(row.adjusted_downtime_hours, 1)} h exceeded scheduled ` +
          `${fmtNum(row.adjusted_scheduled_hours, 1)} h — clamped to 0%`,
      });
    }
    if (row.no_wo_entries) notes.push({ kind: "no-data", text: row.note || "No WO entries this month" });
    if (row.overlap_count > 0) {
      notes.push({ kind: "overlap", text: `${fmtNum(row.overlap_count, 0)} WO(s) cross a month boundary` });
    }
    if (Number(row.linked_downtime_hours) > 0) {
      notes.push({
        kind: "linked",
        text: `Includes ${fmtNum(row.linked_downtime_hours, 1)} h linked from another asset`,
      });
    }
    return notes;
  }

  function availabilityDetailRows(group, data) {
    const months = data.months || [];
    const labels = group.month_labels || [];
    const average = group.average || [];
    const monthIndex = new Map(months.map((month, index) => [month, index]));
    // Assets are ordered as the chart draws and the legend names them; a row for
    // an asset the chart dropped (every month undefined) sorts after all of them
    // rather than being hidden, because a column of blanks is itself a finding.
    const assetIndex = new Map((group.assets || []).map((asset, index) => [asset.asset_number, index]));

    return (group.rows || []).map((row) => {
      const index = monthIndex.has(row.month) ? monthIndex.get(row.month) : -1;
      const groupAverage = index >= 0 ? average[index] : null;
      const availability = row.availability;
      return {
        key: `${row.asset_number}|${row.month}`,
        row,
        assetIndex: assetIndex.has(row.asset_number) ? assetIndex.get(row.asset_number) : assetIndex.size,
        monthIndex: index,
        monthLabel: labels[index] || row.month_label || row.month,
        availability,
        delta:
          availability == null || groupAverage == null ? null : (availability - groupAverage) * 100,
        notes: detailNotes(row),
        attention: Boolean(row.flagged || row.no_wo_entries || row.overlap_count > 0),
      };
    });
  }

  function isMissing(value) {
    return value === null || value === undefined || value === "";
  }

  // Sorts a list that is already in its natural order. Array.prototype.sort is
  // stable, so that order becomes the tie-break for every column without any
  // column having to say so.
  function sortRows(items, sort, columns) {
    const column = sort && sort.key ? columns.find((c) => c.key === sort.key) : null;
    if (!column) return items.slice();
    const dir = sort.dir === "desc" ? -1 : 1;
    return items.slice().sort((a, b) => {
      const left = column.sort(a);
      const right = column.sort(b);
      // Blanks sort last in both directions. A month with no availability at all
      // is not the best or the worst month, it is an absent one, and letting a
      // descending sort stack blanks on top buries the rows being looked for.
      if (isMissing(left) || isMissing(right)) {
        if (isMissing(left) && isMissing(right)) return 0;
        return isMissing(left) ? 1 : -1;
      }
      if (typeof left === "number" && typeof right === "number") return dir * (left - right);
      return dir * String(left).localeCompare(String(right), undefined, { numeric: true });
    });
  }

  function sortDetailRows(items, sort) {
    // The chart's own order -- assets as the legend names them, months left to
    // right -- is what an unsorted table shows and what every sort falls back to.
    const natural = items
      .slice()
      .sort((a, b) => a.assetIndex - b.assetIndex || a.monthIndex - b.monthIndex);
    return sortRows(natural, sort, DETAIL_COLUMNS);
  }

  function detailHeadCell(column, sort, onSort) {
    const active = sort.key === column.key;
    const direction = active ? (sort.dir === "desc" ? "descending" : "ascending") : "none";
    const button = el(
      "button",
      {
        type: "button",
        class: "metrics-sort" + (active ? " is-active" : ""),
        "data-focus-id": `sort:${column.key}`,
        onclick: () => onSort(column.key),
      },
      [
        el("span", { text: column.label }),
        el("span", {
          class: "metrics-sort-mark",
          "aria-hidden": "true",
          text: active ? (sort.dir === "desc" ? "▼" : "▲") : "↕",
        }),
      ]
    );
    return el("th", { scope: "col", "aria-sort": direction, class: column.numeric ? "is-numeric" : null }, [
      button,
    ]);
  }

  let detailNode = null;

  function openAvailabilityDetail(assetGroup, focus) {
    state.availabilityDetail = {
      assetGroup,
      focus: focus || null,
      sort: { key: null, dir: "asc" },
      attentionOnly: false,
      // Only on the first render: a sort or a filter should not yank the reader
      // back to the bar they started from.
      scrollToFocus: Boolean(focus),
      // Which of the two tables is showing. The summary opens first even when a
      // bar was clicked: a month is only unusual next to its neighbours, so the
      // shape of the group is what to read before the records behind one cell.
      view: "summary",
      wo: emptyWorkOrderState(),
      // Set only by a view switch, which moves focus somewhere the control that
      // asked for it no longer exists. Null every other render.
      focusAfterRender: null,
    };
    // The tooltip is a viewport-positioned node describing a chart that is about
    // to be covered up.
    hideTooltip();
    renderAvailabilityDetail();
  }

  // The button that opens a group's drill-down, looked up rather than held onto:
  // an availability refresh rebuilds the expanded card and throws every one of
  // these away, so a reference captured when the dialog opened can be detached
  // by the time it closes.
  function detailOpener(assetGroup) {
    if (!assetGroup) return null;
    return document.querySelector(
      `.availability-group-actions [data-availability-group="${CSS.escape(assetGroup)}"]`
    );
  }

  function closeAvailabilityDetail() {
    const detail = state.availabilityDetail;
    state.availabilityDetail = null;
    document.removeEventListener("keydown", onDetailKeydown, true);
    document.body.classList.remove("metrics-modal-open");
    if (detailNode) {
      detailNode.remove();
      detailNode = null;
    }
    const restore = detail && detailOpener(detail.assetGroup);
    if (restore) restore.focus();
  }

  // Which control inside the dialog holds focus, as an id that outlives the
  // node. Every render replaces the whole subtree, so the focused element is
  // about to be discarded and can only be found again by name.
  function detailFocusId() {
    const active = document.activeElement;
    if (!detailNode || !active || !detailNode.contains(active)) return null;
    return active.getAttribute ? active.getAttribute("data-focus-id") : null;
  }

  function detailFocusables() {
    if (!detailNode) return [];
    return Array.from(
      detailNode.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ).filter((node) => node.offsetParent !== null || node === document.activeElement);
  }

  function onDetailKeydown(event) {
    if (!state.availabilityDetail || !detailNode) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeAvailabilityDetail();
      return;
    }
    if (event.key !== "Tab") return;
    // Without a trap, Tab walks straight out of the dialog and into the page
    // behind it -- which is still there, just covered, so a keyboard reader ends
    // up driving controls they cannot see.
    const focusables = detailFocusables();
    if (!focusables.length) return;
    // Anything the list does not hold counts as out of range, not just focus
    // that has already escaped the dialog. The dialog itself holds focus when it
    // opens -- it is tabindex="-1", so it is deliberately not in the list -- and
    // the backdrop is appended to the end of <body>. Forward Tab from there
    // happens to land on the close button because that is the next focusable in
    // document order, but Shift+Tab as the first keypress walks backwards into
    // the covered page.
    const index = focusables.indexOf(document.activeElement);
    if (event.shiftKey && index <= 0) {
      event.preventDefault();
      focusables[focusables.length - 1].focus();
    } else if (!event.shiftKey && (index === -1 || index === focusables.length - 1)) {
      event.preventDefault();
      focusables[0].focus();
    }
  }

  // ---- availability drill-down: the work orders behind one bar -------------
  // The summary table explains a bar in terms of hours. This explains those
  // hours in terms of the records they were added up from -- which work order,
  // on which machine, opened when, and how much of its downtime this particular
  // bar was charged.
  //
  // Fetched a bar at a time rather than shipped with the chart: the card draws
  // several hundred asset-months and each one's work orders carry a name, a
  // description and completion notes, which is far more payload than the
  // numbers they explain.

  const WORK_ORDER_API = `${AVAILABILITY_API}/work-orders`;

  function emptyWorkOrderState() {
    return {
      assetNumber: null,
      month: null,
      // The (asset, month, generated_at) the loaded rows belong to. Holding the
      // dashboard's timestamp too means a recompute underneath the dialog --
      // an OT edit on the card behind it, a changed month window -- re-pulls
      // rather than leaving figures that no longer match the chart.
      key: "",
      data: null,
      error: "",
      loading: false,
      sort: { key: null, dir: "asc" },
      token: 0,
    };
  }

  // Stamps arrive as plant-local wall-clock times that the server has already
  // converted, so they are formatted by slicing the string. Handing them to
  // Date would re-interpret them in the browser's timezone, which moves a work
  // order created near midnight onto the wrong day -- and the created day is
  // what decides which month's bar it lands in.
  function fmtStamp(iso) {
    if (!iso) return "—";
    const [day, time] = String(iso).split("T");
    return time ? `${day} ${time.slice(0, 5)}` : day;
  }

  function roundTo(value, digits) {
    return Number(Number(value || 0).toFixed(digits));
  }

  // How many decimals the hours columns need before the rows, as displayed,
  // still reach the totals the header states.
  //
  // Downtime is recorded in minutes, so an hours column routinely holds thirds
  // of a hundredth: three one-minute orders are 0.0167 h each, which at two
  // decimals renders as three 0.02s beneath a total of 0.05. Two decimals is
  // the right granularity for hours on a plant floor and stays the default, but
  // a column that visibly refuses to add up undermines the one thing this view
  // promises, so it gains decimals until it does.
  //
  // How many it can need scales with how many rows there are, because each row
  // contributes up to half a unit of rounding error: reconciling N rows to the
  // header's own two decimals needs roughly 2 + log10(N) of them. Six covers
  // ten thousand work orders in a single asset-month, which is more than the
  // whole database holds across every asset and every year.
  //
  // Every subtotal has to hold, not just the combined one, because their errors
  // can cancel: eight minute-granular direct orders can run a hundredth high
  // while four linked ones run a hundredth low, leaving the combined figure
  // reconciled and *both* of the breakdowns the header leads with wrong. Each
  // group is checked against its own stat, so a precision is only accepted when
  // all of them add up.
  //
  // And sometimes none of them do, at any width. Two linked orders of 734 and
  // 397 minutes at 50% sum to exactly 9.425 h, but in binary that addition
  // lands on 9.424999999999999, so the total rounds down to 9.42 while the rows
  // -- rounded first, then added -- reach 9.425 and round up to 9.43. More
  // decimals cannot cross that: the two sides sit on opposite sides of a tie.
  // So the search reports whether it succeeded rather than returning a width
  // that was never checked, and the caller says so plainly instead of drawing a
  // footer that contradicts the header above it.
  function hoursPrecision(groups) {
    const reconciles = (digits) =>
      groups.every((group) => {
        const shown = group.values.reduce((sum, value) => sum + roundTo(value, digits), 0);
        return roundTo(shown, 2) === roundTo(group.total, 2);
      });
    for (let digits = 2; digits <= 6; digits += 1) {
      if (reconciles(digits)) return { digits, reconciled: true };
    }
    // Back to the readable default: extra decimals earn their clutter only by
    // making the column add up, and here nothing does.
    return { digits: 2, reconciled: false };
  }

  // Limble's own task type. Not the same thing as the app's classification and
  // frequently at odds with it -- the workbook's belief that "type 4 is a PM"
  // is what made it drop 2,410 rows of real repairs (design doc §2.1). Both are
  // shown, and neither filters anything, so a reader auditing a counted row can
  // see exactly what each system called it.
  const LIMBLE_TYPES = {
    1: "PM",
    2: "Request template",
    4: "Project / misc repair",
    6: "Work request",
    7: "Parts alert",
  };

  const WORK_ORDER_COLUMNS = [
    {
      key: "wo",
      label: "WO",
      // The number to paste into Limble, so it leads.
      sort: (item) => item.wo.task_id,
      cell: (item) =>
        el("td", { class: "availability-wo-id", text: item.wo.task_id || "—" }),
    },
    {
      key: "name",
      label: "Work order",
      sort: (item) => item.wo.task_name || item.wo.description,
      cell: (item) =>
        el("td", { class: "availability-wo-name" }, [
          el("span", { text: item.wo.task_name || "(unnamed)" }),
          item.wo.description
            ? el("span", { class: "availability-detail-sub", text: item.wo.description })
            : null,
        ]),
    },
    {
      key: "asset",
      label: "Asset",
      sort: (item) => `${item.wo.source === "direct" ? 0 : 1}${item.assetLabel}`,
      // A linked row is a work order on a *different* machine that this bar was
      // charged a share of, which reads as a mistake unless the row says so
      // itself. See the design doc §2.4.
      cell: (item) =>
        el("td", { class: "availability-wo-asset" }, [
          el("span", { text: item.assetLabel }),
          item.wo.source === "linked"
            ? el("span", {
                class: "availability-detail-flag is-linked",
                text: `linked · ${(item.wo.impact_factor * 100).toFixed(0)}%`,
              })
            : null,
        ]),
    },
    {
      key: "created",
      label: "Created",
      sort: (item) => item.wo.created,
      cell: (item) =>
        el("td", { class: "availability-wo-when" }, [
          el("span", { text: fmtStamp(item.wo.created) }),
          item.wo.crosses_month
            ? el("span", {
                class: "availability-detail-flag is-overlap",
                text: "crosses month end",
                title:
                  "Downtime is charged to the month the work order was created, " +
                  "so all of this one's hours land here.",
              })
            : null,
        ]),
    },
    {
      key: "completed",
      label: "Completed",
      sort: (item) => item.wo.completed,
      cell: (item) => el("td", { class: "availability-wo-when", text: fmtStamp(item.wo.completed) }),
    },
    {
      key: "downtime",
      label: "Downtime (h)",
      numeric: true,
      sort: (item) => item.wo.downtime_hours,
      cell: (item) =>
        el("td", { class: "availability-cell", text: fmtNum(item.wo.downtime_hours, item.digits) }),
    },
    {
      key: "counted",
      label: "Counted here (h)",
      numeric: true,
      // Not the same number as the column before it whenever the row is linked
      // or its downtime was recorded negative, which is exactly the arithmetic
      // this view exists to make visible.
      sort: (item) => item.wo.counted_hours,
      cell: (item) => {
        const attrs = { class: "availability-cell availability-wo-counted" };
        if (!item.wo.counted_hours) attrs.class += " is-quiet";
        attrs.text = fmtNum(item.wo.counted_hours, item.digits);
        if (item.wo.counted_hours !== item.wo.downtime_hours) {
          attrs.title =
            item.wo.source === "linked"
              ? `${fmtNum(item.wo.downtime_hours, item.digits)} h on ${item.assetLabel} × ` +
                `${item.wo.impact_factor} impact factor`
              : "Negative downtime counts as zero.";
        }
        return el("td", attrs);
      },
    },
    {
      key: "type",
      label: "Type",
      sort: (item) => item.wo.type_raw,
      cell: (item) => {
        const raw = item.wo.type_raw;
        if (!raw) return el("td", {}, [el("span", { class: "availability-detail-quiet", text: "—" })]);
        const label = LIMBLE_TYPES[raw];
        return el("td", {}, [
          el("span", { text: label || `Type ${raw}` }),
          // The bare number is what Limble itself shows, so it stays reachable
          // for anyone comparing this row against the source system.
          label ? el("span", { class: "availability-detail-sub", text: `Limble type ${raw}` }) : null,
        ]);
      },
    },
    {
      key: "status",
      label: "Status",
      sort: (item) => item.wo.status,
      cell: (item) => {
        // Shown because a reader will ask what kind of work order this was;
        // nothing here filters on it, which is the whole point of §2.1.
        // UNKNOWN is what the classifier says when it has nothing to say, so
        // printing it only adds a column of noise to scan past.
        const cls = item.wo.record_class === "UNKNOWN" ? "" : item.wo.record_class;
        return el("td", {}, [
          el("span", { text: item.wo.status || "—" }),
          cls ? el("span", { class: "availability-detail-sub", text: cls }) : null,
        ]);
      },
    },
    {
      key: "notes",
      label: "Completion notes",
      sort: (item) => item.wo.completion_notes,
      cell: (item) =>
        el("td", { class: "availability-detail-notes" }, [
          item.wo.completion_notes
            ? el("span", { text: item.wo.completion_notes })
            : el("span", { class: "availability-detail-quiet", text: "—" }),
        ]),
    },
  ];

  // Which bar the work-order view is showing, falling back through the reasons
  // a reader would have opened it. A stored target is re-checked every render
  // because a refresh can drop the asset out of the group or the month out of
  // the window underneath an open dialog.
  function workOrderTarget(group, data, detail) {
    const months = data.months || [];
    const assets = group.assets || [];
    const valid = (asset, month) =>
      Boolean(asset) &&
      Boolean(month) &&
      assets.some((a) => a.asset_number === asset) &&
      months.indexOf(month) >= 0;

    const wo = detail.wo;
    if (valid(wo.assetNumber, wo.month)) return { assetNumber: wo.assetNumber, month: wo.month };
    const focus = detail.focus;
    if (focus && valid(focus.asset_number, focus.month)) {
      return { assetNumber: focus.asset_number, month: focus.month };
    }
    // Opened from "View data" rather than from a bar, so nothing has been
    // pointed at yet. Start on the group's weakest asset-month: it is the row
    // the summary's own sort defaults treat as the one worth reading first.
    let worst = null;
    (group.rows || []).forEach((row) => {
      if (!valid(row.asset_number, row.month)) return;
      if (row.availability === null || row.availability === undefined) return;
      if (
        !worst ||
        row.availability < worst.availability ||
        (row.availability === worst.availability &&
          row.adjusted_downtime_hours > worst.adjusted_downtime_hours)
      ) {
        worst = row;
      }
    });
    if (worst) return { assetNumber: worst.asset_number, month: worst.month };
    return {
      assetNumber: assets.length ? assets[0].asset_number : null,
      month: months.length ? months[months.length - 1] : null,
    };
  }

  async function loadWorkOrders(assetGroup, target, key) {
    const detail = state.availabilityDetail;
    if (!detail) return;
    const wo = detail.wo;
    const token = (wo.token += 1);
    // Claimed before the request goes out, so the render this returns to does
    // not read the state as still needing a fetch and start another one.
    wo.key = key;
    wo.loading = true;
    wo.error = "";
    try {
      const params = new URLSearchParams({
        asset_group: assetGroup,
        asset_number: target.assetNumber,
        month: target.month,
      });
      const payload = await getJson(`${WORK_ORDER_API}?${params.toString()}`);
      if (token !== wo.token) return;
      wo.data = payload;
    } catch (err) {
      if (token !== wo.token) return;
      wo.data = null;
      wo.error = err.message || "Could not load the work orders behind this bar.";
    } finally {
      // A newer request has taken over, or the dialog closed while this one was
      // in flight; either way this response is no longer what is on screen.
      if (token === wo.token && state.availabilityDetail === detail) {
        wo.loading = false;
        if (detail.view === "work-orders") renderAvailabilityDetail();
      }
    }
  }

  // Switch the open dialog to the work orders behind one asset-month.
  function showWorkOrders(assetNumber, month) {
    const detail = state.availabilityDetail;
    if (!detail) return;
    const switching = detail.view !== "work-orders";
    detail.view = "work-orders";
    detail.wo.assetNumber = assetNumber;
    detail.wo.month = month;
    // Arriving from a count in the summary table, whose button is about to stop
    // existing. Land on the view switch rather than falling back to the dialog
    // itself, so a keyboard reader is told which view they are now in and can
    // step straight back.
    if (switching) detail.focusAfterRender = "view:work-orders";
    renderAvailabilityDetail();
  }

  function workOrderPicker(label, focusId, options, value, onPick) {
    const select = el("select", { class: "availability-wo-select", "data-focus-id": focusId });
    options.forEach((option) => {
      const node = el("option", { value: option.value, text: option.label });
      if (option.value === value) node.selected = true;
      select.appendChild(node);
    });
    select.addEventListener("change", () => onPick(select.value));
    return el("label", { class: "availability-wo-picker" }, [
      el("span", { class: "eyebrow", text: label }),
      select,
    ]);
  }

  function workOrderStat(label, value, extra) {
    return el("div", { class: `availability-wo-stat${extra ? " " + extra : ""}` }, [
      el("span", { class: "eyebrow", text: label }),
      el("span", { class: "availability-wo-stat-value", text: value }),
    ]);
  }

  // The bar restated as the sum it is, so the rows underneath can be checked
  // against it: direct plus linked is the downtime, measured against the
  // scheduled hours, giving the percentage the chart drew.
  function workOrderSummary(payload, precision) {
    const stats = [
      workOrderStat("Direct", `${fmtNum(payload.direct_downtime_hours, 2)} h`),
      workOrderStat("Linked", `${fmtNum(payload.linked_downtime_hours, 2)} h`),
      workOrderStat("Total downtime", `${fmtNum(payload.adjusted_downtime_hours, 2)} h`, "is-total"),
      workOrderStat("Scheduled", `${fmtNum(payload.adjusted_scheduled_hours, 1)} h`),
      workOrderStat(
        "Availability",
        payload.availability === null || payload.availability === undefined
          ? "—"
          : `${(payload.availability * 100).toFixed(2)}%`,
        payload.flagged ? "is-flagged" : ""
      ),
      workOrderStat("Goal", `${((payload.goal ?? 0.95) * 100).toFixed(2)}%`),
    ];
    const children = [el("div", { class: "availability-wo-stats" }, stats)];

    if (payload.flagged) {
      children.push(
        el("p", {
          class: "availability-wo-caveat",
          text:
            `Downtime exceeded the scheduled hours, so the bar was clamped to 0%. ` +
            `The rows below still add up to ${fmtNum(payload.adjusted_downtime_hours, 2)} h.`,
        })
      );
    }
    if ((payload.linked_assets || []).length) {
      const named = payload.linked_assets
        .map((a) => `${a.display_name} (${(a.impact_factor * 100).toFixed(0)}%)`)
        .join(", ");
      children.push(
        el("p", {
          class: "availability-wo-caveat",
          text:
            `Linked hours are a share of another machine's downtime, charged here ` +
            `by the coupling rules: ${named}. Those rows are marked "linked".`,
        })
      );
    }
    if (payload.manual_ot_hours > 0) {
      children.push(
        el("p", {
          class: "availability-wo-caveat",
          text:
            `Scheduled hours include ${fmtNum(payload.manual_ot_hours, 1)} h of manual ` +
            `overtime on top of ${fmtNum(payload.scheduled_hours, 1)} h of weekday shifts.`,
        })
      );
    }
    // Said out loud rather than papered over. This month's hours fall either
    // side of a rounding boundary, so no column width makes the rows add to the
    // figures above them; the totals stay authoritative and the reader is told
    // why their own addition may land a hundredth away.
    if (precision && !precision.reconciled) {
      children.push(
        el("p", {
          class: "availability-wo-caveat is-warn",
          text:
            "These hours sit on a rounding boundary, so adding the rows below by hand " +
            "can come out a hundredth away from the totals above. The totals are the " +
            "computed figures and are what the chart drew.",
        })
      );
    }
    return el("div", { class: "availability-wo-summary" }, children);
  }

  function workOrderEmptyText(payload) {
    // Distinguishing these two matters: one is a machine with a clean month and
    // the other is a machine nobody wrote anything down about, and both draw the
    // same 100% bar.
    if ((payload.linked_assets || []).length) {
      return (
        `No work orders were created for ${payload.display_name} in ${payload.month_label}, ` +
        `and none of its linked assets carried any either. An availability of 100% here ` +
        `means nothing was recorded, not that nothing broke.`
      );
    }
    return (
      `No work orders were created for ${payload.display_name} in ${payload.month_label}. ` +
      `An availability of 100% here means nothing was recorded, not that nothing broke.`
    );
  }

  function renderWorkOrderBody(group, data, detail) {
    const wo = detail.wo;
    const target = workOrderTarget(group, data, detail);
    wo.assetNumber = target.assetNumber;
    wo.month = target.month;

    const months = data.months || [];
    const labels = group.month_labels || [];
    const children = [];

    if (target.assetNumber && target.month) {
      children.push(
        el("div", { class: "availability-wo-controls" }, [
          workOrderPicker(
            "Asset",
            "wo-asset",
            (group.assets || []).map((asset) => ({
              value: asset.asset_number,
              label:
                asset.display_name && asset.display_name !== asset.asset_number
                  ? `${asset.display_name} · ${asset.asset_number}`
                  : asset.asset_number,
            })),
            target.assetNumber,
            (value) => showWorkOrders(value, wo.month)
          ),
          workOrderPicker(
            "Month",
            "wo-month",
            months.map((month, index) => ({ value: month, label: labels[index] || month })),
            target.month,
            (value) => showWorkOrders(wo.assetNumber, value)
          ),
        ])
      );

      // Kicked off from the render rather than from the click, so the same path
      // covers opening the view, changing the picker, and a recompute landing
      // underneath the dialog.
      const key = `${target.assetNumber}|${target.month}|${data.generated_at || ""}`;
      if (wo.key !== key) loadWorkOrders(detail.assetGroup, target, key);
    }

    if (wo.error) {
      children.push(
        el("div", { class: "metrics-empty" }, [
          el("p", { text: wo.error }),
          el("button", {
            type: "button",
            class: "btn-secondary",
            "data-focus-id": "wo-retry",
            text: "Try again",
            onclick: () => {
              wo.key = "";
              wo.error = "";
              renderAvailabilityDetail();
            },
          }),
        ])
      );
      return { children, scroll: null };
    }

    const payload = wo.data;
    // Rows from the previously selected bar are worse than none: they would sit
    // under the new bar's heading and read as its evidence.
    const stale = payload && (payload.asset_number !== target.assetNumber || payload.month !== target.month);
    if (!payload || stale) {
      children.push(
        el("p", {
          class: "metrics-empty",
          role: "status",
          text: wo.loading ? "Loading the work orders behind this bar…" : "Select an asset and month.",
        })
      );
      return { children, scroll: null };
    }

    // One precision for the whole table, chosen from the rows it is about to
    // draw, so every hours cell in it is directly comparable with every other --
    // and checked against each of the three figures the header leads with, not
    // only their sum.
    const entries = payload.work_orders || [];
    const countedFrom = (source) =>
      entries.filter((entry) => entry.source === source).map((entry) => entry.counted_hours);
    const precision = hoursPrecision([
      { values: countedFrom("direct"), total: payload.direct_downtime_hours },
      { values: countedFrom("linked"), total: payload.linked_downtime_hours },
      { values: entries.map((entry) => entry.counted_hours), total: payload.adjusted_downtime_hours },
    ]);
    const digits = precision.digits;

    children.push(workOrderSummary(payload, precision));

    const rows = (payload.work_orders || []).map((entry, index) => ({
      key: `${entry.source}|${entry.asset_number}|${entry.task_id}|${index}`,
      wo: entry,
      digits,
      assetLabel:
        entry.asset_number === payload.asset_number
          ? payload.display_name || entry.asset_number
          : (payload.linked_assets || []).reduce(
              (name, linked) => (linked.asset_number === entry.asset_number ? linked.display_name : name),
              entry.asset_number
            ),
    }));

    if (!rows.length) {
      children.push(el("p", { class: "metrics-empty", text: workOrderEmptyText(payload) }));
      return { children, scroll: null };
    }

    const onSort = (key) => {
      const sort = wo.sort;
      if (sort.key === key) sort.dir = sort.dir === "asc" ? "desc" : "asc";
      else {
        sort.key = key;
        // The hours columns open on their largest value -- the biggest
        // contributor is the reason this list was opened -- and everything else
        // reads naturally from the top.
        sort.dir = key === "downtime" || key === "counted" ? "desc" : "asc";
      }
      renderAvailabilityDetail();
    };

    const shown = sortRows(rows, wo.sort, WORK_ORDER_COLUMNS);
    const head = el(
      "tr",
      {},
      WORK_ORDER_COLUMNS.map((column) => detailHeadCell(column, wo.sort, onSort))
    );
    const body = shown.map((item) =>
      el(
        "tr",
        { class: `availability-wo-row is-${item.wo.source}` },
        WORK_ORDER_COLUMNS.map((column) => column.cell(item))
      )
    );

    // Normally the sum of the values as *displayed*, not of the values
    // underneath them, so the figure at the foot of the column is the one a
    // reader adding the column up arrives at. It is the bar, restated by the
    // rows that made it.
    //
    // When no precision could reconcile the two, the header's own figure wins
    // instead. A footer disagreeing with the stat directly above it is a dialog
    // contradicting itself, which is worse than a hand-sum landing a hundredth
    // out -- and the summary says which is happening.
    const countedTotal = precision.reconciled
      ? shown.reduce((sum, item) => sum + roundTo(item.wo.counted_hours, digits), 0)
      : payload.adjusted_downtime_hours;
    const totals = el(
      "tr",
      { class: "availability-wo-total" },
      WORK_ORDER_COLUMNS.map((column) => {
        if (column.key === "wo") return el("td", { text: "Total counted" });
        if (column.key === "counted") {
          return el("td", { class: "availability-cell", text: fmtNum(countedTotal, digits) });
        }
        return el("td", {});
      })
    );

    const scroll = el("div", { class: "metrics-table-scroll", "data-focus-id": "wo-rows" }, [
      el("table", { class: "metrics-table availability-detail-table availability-wo-table" }, [
        el("thead", {}, [head]),
        el("tbody", {}, body),
        el("tfoot", {}, [totals]),
      ]),
    ]);

    const counted = payload.work_orders.filter((entry) => entry.counted_hours > 0).length;
    children.push(
      el("div", { class: "metrics-modal-toolbar" }, [
        el("p", {
          class: "metrics-hint",
          text:
            `${rows.length} work order(s) · ${counted} carrying downtime · ` +
            "click a column heading to sort",
        }),
      ]),
      scroll
    );
    return { children, scroll };
  }

  // The group's asset-months, one row each -- the view the dialog opens on.
  function renderSummaryBody(group, detail, items, focusKey, rerender) {
    const shown = sortDetailRows(detail.attentionOnly ? items.filter((i) => i.attention) : items, detail.sort);
    const attentionCount = items.filter((item) => item.attention).length;

    const onSort = (key) => {
      const sort = detail.sort;
      // Re-clicking the active column flips it; a new column starts ascending,
      // except for the ones a reader opens looking for the extreme -- the worst
      // availability and the biggest downtime are what an investigation wants
      // first, and they sit at opposite ends of the sort.
      if (sort.key === key) sort.dir = sort.dir === "asc" ? "desc" : "asc";
      else {
        sort.key = key;
        sort.dir = key === "asset" || key === "month" || key === "availability" || key === "delta" ? "asc" : "desc";
      }
      rerender();
    };

    const attentionToggle = el("input", { type: "checkbox", "data-focus-id": "attention" });
    attentionToggle.checked = detail.attentionOnly;
    // Disabled only when there is nothing to filter to and the filter is off. A
    // refresh can empty the set out from under an active filter -- an OT edit
    // that un-clamps the group's one flagged month does it -- and disabling the
    // checkbox then would strand the reader on "no rows match" with no way to
    // turn the filter back off short of closing the dialog.
    attentionToggle.disabled = !attentionCount && !detail.attentionOnly;
    attentionToggle.addEventListener("change", () => {
      detail.attentionOnly = attentionToggle.checked;
      rerender();
    });

    const head = el("tr", {}, DETAIL_COLUMNS.map((column) => detailHeadCell(column, detail.sort, onSort)));
    const body = shown.length
      ? shown.map((item) => {
          const row = el(
            "tr",
            {
              class:
                "availability-detail-row" +
                (item.key === focusKey ? " is-focused" : "") +
                (item.attention ? " is-attention" : ""),
            },
            DETAIL_COLUMNS.map((column) => column.cell(item))
          );
          if (item.key === focusKey) row.setAttribute("aria-current", "true");
          return row;
        })
      : [
          el("tr", {}, [
            el("td", {
              class: "metrics-empty-row",
              colspan: String(DETAIL_COLUMNS.length),
              text: "No rows match the current filter.",
            }),
          ]),
        ];

    const scroll = el("div", { class: "metrics-table-scroll", "data-focus-id": "rows" }, [
      el("table", { class: "metrics-table availability-detail-table" }, [
        el("thead", {}, [head]),
        el("tbody", {}, body),
      ]),
    ]);

    const toolbar = el("div", { class: "metrics-modal-toolbar" }, [
      el("label", { class: "metrics-modal-check" }, [
        attentionToggle,
        el("span", {
          text: attentionCount
            ? `Only rows needing attention (${attentionCount})`
            : "Only rows needing attention (none)",
        }),
      ]),
      el("p", {
        class: "metrics-hint",
        text:
          `Showing ${shown.length} of ${items.length} row(s) · click a column heading to sort · ` +
          "click a work-order count to list them",
      }),
    ]);

    return { children: [toolbar, scroll], scroll };
  }

  function renderAvailabilityDetail() {
    const detail = state.availabilityDetail;
    if (!detail) return;
    const data = state.availability;
    const group = data ? availabilityGroup(detail.assetGroup) : null;
    // A refresh can drop the group out from under an open modal -- its assets
    // get unconfigured, or a shorter window no longer reaches its data. Close
    // rather than leave a dialog describing a chart that is no longer there.
    if (!group) {
      closeAvailabilityDetail();
      return;
    }

    const items = availabilityDetailRows(group, data);
    const focusKey = detail.focus ? `${detail.focus.asset_number}|${detail.focus.month}` : null;
    const focused = focusKey ? items.find((item) => item.key === focusKey) : null;

    const rerender = () => renderAvailabilityDetail();
    const workOrders = detail.view === "work-orders";
    const view = workOrders
      ? renderWorkOrderBody(group, data, detail)
      : renderSummaryBody(group, detail, items, focusKey, rerender);

    const title = `${group.asset_group} — the rows behind the chart`;
    const closeButton = el("button", {
      type: "button",
      class: "metrics-modal-close",
      "data-focus-id": "close",
      "aria-label": "Close the data table",
      title: "Close (Esc)",
      text: "×",
      onclick: closeAvailabilityDetail,
    });

    // Two readings of the same chart, one level of detail apart: the months and
    // machines a bar sits among, and the individual records its hours were added
    // up from. Plain buttons rather than ARIA tabs -- aria-pressed describes what
    // these actually are, and a tablist would owe the reader arrow-key
    // navigation and a roving tabindex that nothing else on the page has.
    const viewSwitch = el(
      "div",
      { class: "metrics-modal-views", role: "group", "aria-label": "Choose a view" },
      [
        ["summary", "Monthly summary", "Availability by asset and month, for the whole group"],
        ["work-orders", "Work orders", "The individual work orders behind one asset-month"],
      ].map(([key, label, hint]) => {
        const active = detail.view === key;
        return el("button", {
          type: "button",
          class: "metrics-modal-view" + (active ? " is-active" : ""),
          "data-focus-id": `view:${key}`,
          "aria-pressed": active ? "true" : "false",
          title: hint,
          text: label,
          onclick: () => {
            if (detail.view === key) return;
            detail.view = key;
            rerender();
          },
        });
      })
    );

    const monthSpan = (group.month_labels || []).length;
    const dialog = el(
      "div",
      {
        class: "metrics-modal",
        role: "dialog",
        "aria-modal": "true",
        "aria-labelledby": "availability-detail-title",
        tabindex: "-1",
      },
      [
        el("header", { class: "metrics-modal-head" }, [
          el("div", { class: "metrics-modal-heading" }, [
            el("p", { class: "eyebrow", text: "Availability data" }),
            el("h2", { id: "availability-detail-title", text: title }),
            el("p", {
              class: "metrics-modal-sub",
              text:
                `${(group.assets || []).length} asset(s) · ${monthSpan} month(s) · ` +
                `${Number(group.net_scheduled_hours_per_day).toFixed(1)} net scheduled hours/day`,
            }),
            focused
              ? el("p", { class: "metrics-modal-focus" }, [
                  el("span", {
                    class: "metrics-tooltip-swatch",
                    style: `background:${SERIES_COLORS[focused.assetIndex % SERIES_COLORS.length]}`,
                  }),
                  `Selected bar: ${focused.row.display_name || focused.row.asset_number} · ${focused.monthLabel}` +
                    (focused.availability == null ? "" : ` · ${(focused.availability * 100).toFixed(2)}%`),
                ])
              : null,
          ]),
          closeButton,
        ]),
        viewSwitch,
      ].concat(view.children)
    );
    const scroll = view.scroll;

    const backdrop = el("div", { class: "metrics-modal-backdrop" }, [dialog]);
    backdrop.addEventListener("mousedown", (event) => {
      // mousedown, not click: a drag that starts inside the table and releases
      // over the backdrop is a text selection, not a dismissal.
      if (event.target === backdrop) closeAvailabilityDetail();
    });

    // Read before the swap: the node holding focus is part of what is about to
    // be replaced. Every render restores focus, not just the ones a control in
    // here started -- an availability refresh landing under an open dialog
    // (an OT edit committed by the same click that opened it) rebuilds this
    // subtree too, and dropping focus there puts it on <body> behind the
    // backdrop, where the reader has nothing to close the dialog with but Esc.
    const focusId = detailFocusId();
    const first = !detailNode;
    if (detailNode) detailNode.replaceWith(backdrop);
    else document.body.appendChild(backdrop);
    detailNode = backdrop;

    // Before focus is restored: the rows region is only focusable once this has
    // given it a tab stop. A view showing a message rather than a table has no
    // region to mark.
    if (scroll) {
      markScrollRegion(
        scroll,
        workOrders
          ? `${group.asset_group} work orders`
          : `${group.asset_group} availability data`
      );
    }

    if (first) {
      document.body.classList.add("metrics-modal-open");
      document.addEventListener("keydown", onDetailKeydown, true);
      dialog.focus();
    } else {
      // A view switch names where focus should land, because the control that
      // asked for it is not in the new view; every other render just puts it
      // back where it was.
      const wanted = detail.focusAfterRender || focusId;
      detail.focusAfterRender = null;
      const target = wanted ? dialog.querySelector(`[data-focus-id="${wanted}"]`) : null;
      if (target) target.focus();
      // Checked rather than predicted, because a control can be present and
      // still refuse focus: unchecking the attention filter when a refresh has
      // already emptied it re-renders the checkbox disabled, and focus() on a
      // disabled input is a no-op. The node that had focus is detached by now,
      // so anything that fails to land leaves focus on <body> behind the
      // backdrop. Asking where focus actually went covers that and every other
      // way an element can decline it.
      if (!dialog.contains(document.activeElement)) dialog.focus();
    }

    if (detail.scrollToFocus && scroll) {
      detail.scrollToFocus = false;
      const row = dialog.querySelector(".availability-detail-row.is-focused");
      if (row) {
        // Centred by hand rather than with scrollIntoView, which also scrolls
        // every ancestor -- including the page behind the modal.
        const rowBox = row.getBoundingClientRect();
        const scrollBox = scroll.getBoundingClientRect();
        scroll.scrollTop += rowBox.top - scrollBox.top - (scrollBox.height - rowBox.height) / 2;
      }
    }
  }

  async function loadAvailability() {
    try {
      const data = await getJson(`${AVAILABILITY_API}?months=${state.availabilityWindow}`);
      state.availability = data;
      state.availabilityError = "";
      // A timezone that could not be loaded is a misconfiguration, not a
      // failure: the page still computes, in UTC, which quietly moves work
      // orders created near local midnight into the wrong month. Surface it
      // rather than letting the numbers look ordinary.
      state.availabilityWarning = data.timezone_warning || "";
      if (Array.isArray(data.all_asset_numbers) && data.all_asset_numbers.length) {
        state.defaultAssets = data.all_asset_numbers.slice();
      }
      return data;
    } catch (err) {
      state.availability = null;
      state.availabilityError = err.message || "Could not load availability data.";
      return null;
    }
  }

  function availabilityBasisText() {
    const data = state.availability;
    if (!data) return "";
    const groups = data.groups || [];
    // State the assumptions on the page so a screenshot carries them: config
    // changes apply to every month, so a number is only meaningful alongside
    // the schedule it was computed with.
    const schedules = groups
      .map((g) => `${g.asset_group} ${Number(g.net_scheduled_hours_per_day).toFixed(1)} net h/day`)
      .join(" · ");
    const stamp = (data.generated_at || "").replace("T", " ");
    return `Computed ${stamp} · ${data.timezone || "local"} · ${schedules}`;
  }

  function renderAvailabilityPreview() {
    const canvas = $("availability-preview-chart");
    const empty = $("availability-preview-empty");
    const data = state.availability;
    if (!canvas) return;

    if (!data || !(data.groups || []).length) {
      drawBarChart(canvas, [], { height: 160 });
      empty.textContent =
        state.availabilityError || (data && data.empty_reason) || "No availability data is available yet.";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    // Collapsed preview: the most recent month's average per group, ranked, so
    // the groups needing attention sit at the left edge like every other card.
    const lastIndex = (data.months || []).length - 1;
    const items = data.groups
      .map((group) => ({
        name: group.asset_group,
        value: (group.average || [])[lastIndex],
      }))
      .filter((item) => item.value !== null && item.value !== undefined)
      .map((item) => ({ ...item, value: item.value * 100 }));

    const label = $("availability-preview-label");
    if (label && lastIndex >= 0) {
      const months = data.groups[0].month_labels || [];
      label.textContent = `Average availability by group — ${months[lastIndex] || ""}`;
    }
    drawBarChart(canvas, sortedDesc(items), { height: 160, valueSuffix: "%" });
  }

  function availabilityCell(row) {
    if (!row) return el("td", { class: "availability-cell", text: "—" });
    const attrs = { class: "availability-cell" };
    const parts = [];
    if (row.no_wo_entries) {
      attrs.class += " is-no-data";
      parts.push(row.note);
    }
    if (row.flagged) {
      attrs.class += " is-flagged";
      parts.push(
        `Downtime ${row.adjusted_downtime_hours} h exceeded scheduled ${row.adjusted_scheduled_hours} h`
      );
    }
    if (row.linked_downtime_hours > 0) {
      parts.push(`Includes ${row.linked_downtime_hours} h linked downtime`);
    }
    if (row.overlap_count > 0) {
      parts.push(`${row.overlap_count} WO(s) cross a month boundary`);
    }
    if (parts.length) attrs.title = parts.join(" · ");
    attrs.text = row.availability === null || row.availability === undefined
      ? "—"
      : `${(row.availability * 100).toFixed(2)}%`;
    return el("td", attrs);
  }

  async function saveAvailabilityValue(url, body, onDone) {
    try {
      const response = await fetch(url, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
      clearBanner();
      if (onDone) await onDone();
    } catch (err) {
      showBanner(err.message || "Could not save.", "error");
    }
  }

  function renderAvailabilityTable(group, data) {
    const months = data.months || [];
    const labels = group.month_labels || [];
    const mode = state.availabilityTableMode[group.asset_group] || "availability";
    const byKey = {};
    (group.rows || []).forEach((row) => {
      byKey[`${row.asset_number}|${row.month}`] = row;
    });

    const head = el("tr", {}, [el("th", { text: mode === "ot" ? "Asset — OT hours" : "Asset" })].concat(
      labels.map((label) => el("th", { text: label }))
    ));

    const body = group.assets.map((asset) =>
      el("tr", {}, [el("td", { text: asset.display_name })].concat(
        months.map((month) => {
          const row = byKey[`${asset.asset_number}|${month}`];
          if (mode !== "ot") return availabilityCell(row);
          const input = el("input", {
            type: "number",
            min: "0",
            step: "0.5",
            class: "availability-input",
            value: row ? String(row.manual_ot_hours) : "0",
          });
          input.addEventListener("change", () => {
            saveAvailabilityValue(
              `${AVAILABILITY_API}/ot`,
              { asset_number: asset.asset_number, month, hours: Number(input.value) },
              refreshAvailability
            );
          });
          return el("td", {}, [input]);
        })
      ))
    );

    const averageRow = el("tr", { class: "availability-summary-row" }, [el("td", { text: "Average" })].concat(
      (group.average || []).map((value) =>
        el("td", { text: value === null || value === undefined ? "—" : `${(value * 100).toFixed(2)}%` })
      )
    ));

    const goalRow = el("tr", { class: "availability-summary-row" }, [el("td", { text: "Goal %" })].concat(
      months.map((month, index) => {
        const input = el("input", {
          type: "number",
          min: "0",
          max: "100",
          step: "0.5",
          class: "availability-input",
          value: (((group.goal || [])[index] ?? 0.95) * 100).toFixed(2),
        });
        input.addEventListener("change", () => {
          saveAvailabilityValue(
            `${AVAILABILITY_API}/goal`,
            { asset_group: group.asset_group, month, goal_percent: Number(input.value) / 100 },
            refreshAvailability
          );
        });
        return el("td", {}, [input]);
      })
    ));

    return el("div", { class: "metrics-table-scroll" }, [
      el("table", { class: "metrics-table availability-table" }, [
        el("thead", {}, [head]),
        el("tbody", {}, body.concat([averageRow, goalRow])),
      ]),
    ]);
  }

  function availabilityTypeNames(data) {
    const totals = {};
    (data.groups || []).forEach((group) => (group.rows || []).forEach((row) => {
      Object.entries(row.work_order_type_hours || {}).forEach(([name, hours]) => {
        totals[name] = (totals[name] || 0) + (Number(hours) || 0);
      });
    }));
    return Object.keys(totals).sort((a, b) => totals[b] - totals[a] || a.localeCompare(b));
  }

  function renderAvailabilityExpanded() {
    const host = $("availability-charts");
    const data = state.availability;
    if (!host) return;
    // The canvases the tooltip is anchored to are about to be discarded.
    hideTooltip();
    host.innerHTML = "";

    const basis = $("availability-basis");
    if (basis) basis.textContent = availabilityBasisText();
    const stackedControl = $("availability-stacked");
    if (stackedControl) stackedControl.checked = state.availabilityStacked;

    if (!data || !(data.groups || []).length) {
      setEmpty(
        "availability",
        state.availabilityError || (data && data.empty_reason) || "No availability data is available yet."
      );
      return;
    }
    setEmpty("availability", "");

    data.groups.forEach((group) => {
      const canvas = el("canvas", { height: "300" });
      const byKey = {};
      (group.rows || []).forEach((row) => { byKey[`${row.asset_number}|${row.month}`] = row; });
      const typeNames = availabilityTypeNames(data);
      const mode = state.availabilityTableMode[group.asset_group] || "availability";
      const toggle = el("button", {
        type: "button",
        class: "btn-secondary availability-mode-toggle",
        text: mode === "ot" ? "Show availability %" : "Edit OT hours",
      });
      toggle.addEventListener("click", () => {
        state.availabilityTableMode[group.asset_group] = mode === "ot" ? "availability" : "ot";
        renderAvailabilityExpanded();
      });

      // The same drill-down the bars open. A canvas cannot hold focus, so
      // without this the whole table would be mouse-only -- and it is also the
      // way in when the question is about the group rather than one bad month.
      const detailButton = el("button", {
        type: "button",
        class: "btn-secondary availability-mode-toggle",
        // Named so the dialog can find it again on close. This button is
        // rebuilt on every availability render, so it cannot be held by
        // reference across one.
        "data-availability-group": group.asset_group,
        text: "View data",
        title: `Open the ${group.asset_group} rows behind this chart`,
      });
      detailButton.addEventListener("click", () => {
        openAvailabilityDetail(group.asset_group, null);
      });

      host.appendChild(
        el("div", { class: "metrics-panel availability-group" }, [
          el("div", { class: "availability-group-head" }, [
            el("h3", { text: `${group.asset_group} Availability % by Month` }),
            el("div", { class: "availability-group-actions" }, [detailButton, toggle]),
          ]),
          el("p", {
            class: "metrics-hint",
            text:
              `${Number(group.net_scheduled_hours_per_day).toFixed(1)} net scheduled hours/day · ` +
              "click a bar to open the rows behind it, and the work orders behind those",
          }),
          el("div", { class: "metrics-chart-wrap" }, [canvas]),
          renderAvailabilityTable(group, data),
        ])
      );
      drawAvailabilityChart(canvas, group, {
        height: 300,
        stacked: state.availabilityStacked,
        rowsByKey: byKey,
        typeNames,
        months: data.months || [],
      });
      wireAvailabilityChart(canvas, group, data);
    });

    syncScrollRegions();
  }

  // Draws whichever availability views are currently on screen. Both the first
  // load and every refresh go through here, so a card expanded while the first
  // fetch is still in flight gets drawn as soon as the data lands instead of
  // keeping the empty state it was opened with.
  function renderAvailability() {
    renderAvailabilityPreview();
    if (state.expanded === "availability") renderAvailabilityExpanded();
    // The modal reads from state rather than from a snapshot, so a refresh that
    // lands under an open drill-down redraws it with the new numbers instead of
    // leaving stale ones on screen.
    if (state.availabilityDetail) renderAvailabilityDetail();
    // Re-asserted on every render, not just the first load: the numbers stay
    // wrong for as long as the misconfiguration lasts.
    if (state.availabilityError) showBanner(state.availabilityError, "error");
    else if (state.availabilityWarning) showBanner(state.availabilityWarning, "info");
  }

  async function refreshAvailability() {
    await loadAvailability();
    renderAvailability();
  }

  // ---- render orchestration ------------------------------------------------
  function renderAll() {
    renderKpiPreview();
    renderAlertPreview();
    renderAvailabilityPreview();
    if (state.expanded === "kpis") renderKpiExpanded();
    if (state.expanded === "alerts") renderAlertExpanded();
    if (state.expanded === "availability") renderAvailabilityExpanded();
  }

  // ---- card expand / collapse ----------------------------------------------
  // Brings a freshly opened card to the top of the reading area. scrollIntoView
  // would do it, except the topbar is sticky and would sit on top of the card
  // header it just scrolled to, so the offset is measured and subtracted.
  function scrollCardIntoView(card) {
    const topbar = document.querySelector(".topbar");
    const offset = (topbar ? topbar.getBoundingClientRect().height : 0) + 12;
    const reduceMotion =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // After a frame: expanding one card collapses another, and if that one was
    // above this one the card is still moving up as the position is read.
    requestAnimationFrame(() => {
      const top = card.getBoundingClientRect().top + window.pageYOffset - offset;
      window.scrollTo({ top: Math.max(0, top), behavior: reduceMotion ? "auto" : "smooth" });
    });
  }

  function setExpanded(key) {
    // Only one card expanded at a time; re-activating the open card collapses it.
    const next = state.expanded === key ? null : key;
    state.expanded = next;
    hideTooltip();
    let opened = null;
    document.querySelectorAll(".metrics-card").forEach((card) => {
      const cardKey = card.getAttribute("data-card");
      const isOpen = cardKey === next;
      card.classList.toggle("is-expanded", isOpen);
      const toggle = card.querySelector(".metrics-card-toggle");
      if (toggle) {
        toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
        toggle.title = isOpen ? "Collapse this card" : "Expand this card";
      }
      const expanded = $(`${cardKey}-expanded`);
      if (expanded) expanded.hidden = !isOpen;
      if (isOpen) opened = card;
    });
    // Draw lazily once the panel is visible (canvases have no width while hidden).
    if (next === "kpis") renderKpiExpanded();
    if (next === "alerts") renderAlertExpanded();
    if (next === "availability") renderAvailabilityExpanded();
    if (opened) scrollCardIntoView(opened);
  }

  function wireCards() {
    // The dedicated toggle is the card's only expand/collapse control for
    // assistive technology. The card remains a generous mouse target, but is
    // not itself a button: that avoids nesting the Availability info button (or
    // any expanded-panel control) inside another interactive role.
    const INTERACTIVE = "a, button, input, select, textarea, .metrics-table-scroll";
    document.querySelectorAll(".metrics-card").forEach((card) => {
      const key = card.getAttribute("data-card");
      const activate = () => setExpanded(key);
      const fromControl = (event) => Boolean(event.target.closest(INTERACTIVE));
      const toggle = card.querySelector(".metrics-card-toggle");
      if (toggle) {
        toggle.title = "Expand this card";
        toggle.addEventListener("click", activate);
      }
      card.addEventListener("click", (event) => {
        if (fromControl(event)) return;
        // Clicking the body of an open card does nothing: the expanded panel is
        // a workspace full of charts and hint text, and a click landing on any
        // of it used to throw the whole panel away. Closing is the header's job.
        if (state.expanded === key && !event.target.closest(".metrics-card-head")) return;
        activate();
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
      // Reset restores the curated default equipment set, not an all-asset view.
      state.selected = defaultSelection();
      state.assetQuery = "";
      search.value = "";
      // Drop any active date filter; the post-reset fetch re-seeds the pickers
      // from the default set's own extent via syncDateInputs(). Blank them now so
      // no stale bound shows while that fetch is in flight.
      state.dateFrom = "";
      state.dateTo = "";
      from.value = "";
      to.value = "";
      renderSelectedChips();
      renderAssetMenu();
      loadMetrics();
    });
  }

  // Redraw the visible canvases on resize (debounced) so charts track the layout.
  let resizeRaf = null;
  function wireResize() {
    window.addEventListener("resize", () => {
      hideTooltip();
      if (resizeRaf) cancelAnimationFrame(resizeRaf);
      resizeRaf = requestAnimationFrame(renderAll);
    });
    // The tooltip is positioned in viewport coordinates, so it does not travel
    // with the chart it points at — and scrolling fires no mousemove to correct
    // it, including the smooth scroll a card expansion kicks off.
    window.addEventListener("scroll", hideTooltip, { passive: true });
  }

  function wireAvailability() {
    const windowSelect = $("availability-window");
    if (!windowSelect) return;
    windowSelect.value = String(state.availabilityWindow);
    windowSelect.addEventListener("change", () => {
      state.availabilityWindow = Number(windowSelect.value) || 5;
      refreshAvailability();
    });
    const stacked = $("availability-stacked");
    if (stacked) stacked.addEventListener("change", () => {
      state.availabilityStacked = stacked.checked;
      renderAvailabilityExpanded();
    });
  }

  function wireAvailabilityInfo() {
    const dialog = $("availability-info-dialog");
    const openButton = $("availability-info-open");
    const closeButton = $("availability-info-close");
    if (!dialog || !openButton || !closeButton) return;

    openButton.addEventListener("click", () => {
      dialog.showModal();
      document.body.classList.add("metrics-modal-open");
      closeButton.focus();
    });
    closeButton.addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      // Native dialogs include the backdrop in their click target. Only close
      // when that backdrop itself was clicked, never from content inside it.
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", () => {
      document.body.classList.remove("metrics-modal-open");
      openButton.focus();
    });
  }

  // ---- init ----------------------------------------------------------------
  async function init() {
    wireCards();
    wireFilters();
    wireAvailability();
    wireAvailabilityInfo();
    wireResize();

    // Availability loads first because it also supplies the curated equipment
    // list the KPI and Alerts cards default to comparing. That list used to be
    // duplicated here; sourcing it from the availability config keeps the plant
    // equipment defined in exactly one place. If the request fails the
    // selection stays empty, which the metrics API reads as "every asset".
    await loadAvailability();
    state.selected = defaultSelection();
    renderSelectedChips();
    renderScopeHint();
    renderAvailability();

    loadAssets();
    loadMetrics();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
