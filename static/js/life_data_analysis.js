/* Life Data Analysis / Weibull workspace client.
 *
 * Mirrors the desktop GREMLIN GUI's Life Data Analysis tab: select an asset,
 * review the Weibull readiness summary + Pareto + beta rankings, disposition
 * corrective work orders and PM reset events, then run a REL-style 2P Weibull
 * MLE analysis. Every action calls the same LifeDataService methods through the
 * Flask JSON API, so the backend is identical to the desktop application.
 */
(function () {
  "use strict";

  const API = "/life-data-analysis/api";
  // Analysis types offered by the Step 1 selector. Weibull and Failure Mode Trend
  // are implemented; the rest render a "Coming soon" placeholder for now. The
  // selected type only controls the secondary analysis panel — the Pareto chart
  // stays visible for every type.
  const ANALYSIS_TYPES = {
    WEIBULL: "Weibull Analysis",
    TREND: "Failure Mode Trend Analysis",
    DOWNTIME: "Downtime Driver Analysis",
    PM: "PM Effectiveness Analysis",
  };
  const SUMMARY_FIELDS = [
    ["total_entries", "Total entries for this asset"],
    ["usable_wos_for_weibull", "Usable WOs for Weibull"],
    ["usable_pms_for_weibull", "Usable PMs for Weibull"],
    ["wos_dispositioned", "WOs dispositioned"],
    ["wos_not_dispositioned", "WOs not dispositioned"],
    ["pms_dispositioned", "PMs dispositioned"],
    ["pms_not_dispositioned", "PMs not dispositioned"],
  ];

  const state = {
    assets: [],
    assetByNumber: new Map(),
    assetFiltered: [],
    assetDropdownOpen: false,
    assetActiveIndex: -1,
    selectedAsset: null,
    paretoRows: [],
    paretoMetric: "downtime_hours",
    // Selected Analysis Type (Step 1) and the data that drives the Failure Mode
    // Trend panel. `trend` is the latest payload returned alongside the summary;
    // `selectedTrend` is the failure mode/mechanism whose monthly trend is plotted.
    analysisType: ANALYSIS_TYPES.WEIBULL,
    trend: null,
    selectedTrend: null,
    // Inclusive month bounds ("YYYY-MM") for the Failure Mode Trend chart/table.
    // null means "no bound" (use the full data range on that side).
    trendRange: { from: null, to: null },
    // A single month ("YYYY-MM") drilled into by clicking a trend data point or a
    // Failure Mode Trend Detail month row. When set, the "Work Orders in Trend"
    // table is filtered to that month; null shows every WO in the active range.
    trendSelectedMonth: null,
    // Inclusive month bounds for the PM Effectiveness "Failures Following PM" chart
    // and PM-to-Failure table (same semantics as trendRange).
    pmRange: { from: null, to: null },
    // PM Effectiveness Analysis: `pmSelection` is the chosen failure mechanism
    // (from a Pareto click or the Perform Analysis picker); `pmData` is the latest
    // payload from the pm-effectiveness endpoint. `pmToken` drops stale responses.
    pmSelection: null,
    pmData: null,
    pmToken: 0,
    // Downtime Driver Analysis: `downtimeSelection` is the chosen failure
    // mode/mechanism (Pareto click or Perform Analysis picker); `downtimeData` is
    // the latest payload from the downtime-drivers endpoint. `downtimeToken` drops
    // stale responses (same pattern as the PM analysis above).
    downtimeSelection: null,
    downtimeData: null,
    downtimeToken: 0,
    latestResult: null,
    // Operating schedule (hours per day the asset actually runs) used to convert the
    // Weibull MTTF from operating hours into approximate calendar months/days. 24 =
    // continuous running; users with a fixed shift (e.g. 20 h/day) can adjust it
    // inline next to the MTTF value, and the conversion recomputes live.
    operatingHoursPerDay: 24,
    // The most recently selected failure mode/mechanism, regardless of which
    // analysis type made the selection. Carried forward when the user switches
    // analysis types so the new analysis auto-computes for the same failure focus.
    activeMechanismRow: null,
    summaryToken: 0,
    // Page context: "analysis" (Perform an Analysis) or "disposition" (the
    // dedicated disposition page). Set during init so shared helpers branch.
    pageMode: "analysis",
    dispositionKind: "wo",
    dispositionScope: "all",
    dispositionPageIndex: 0,
    // Free-text filter applied to the disposition table (matched server-side
    // across every record column, so it spans all pages, not just the visible one).
    dispositionSearch: "",
    // Monotonic token for disposition reloads. Overlapping debounced searches /
    // page changes can resolve out of order on a slow endpoint; only the load
    // whose token still matches is allowed to render, so a stale response never
    // leaves the table filtered for a previous search.
    dispositionToken: 0,
    // Redraws the current analysis charts at the live canvas size (set while a
    // result is shown, cleared with the workspace) so window resizes don't leave
    // the Weibull plots stretched or squished.
    analysisRedraw: null,
  };

  // ---- small DOM + format helpers ------------------------------------------
  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.entries(attrs).forEach(([key, value]) => {
        if (value === null || value === undefined || value === false) return;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "html") node.innerHTML = value;
        else if (key.startsWith("on") && typeof value === "function") {
          node.addEventListener(key.slice(2), value);
        } else if (value === true) node.setAttribute(key, "");
        else node.setAttribute(key, value);
      });
    }
    (children || []).forEach((child) => {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function fmt(value, sig) {
    const num = Number(value);
    if (!isFinite(num)) return "—";
    if (Math.abs(num) >= 1000) return num.toLocaleString(undefined, { maximumFractionDigits: 2 });
    return Number(num.toPrecision(sig || 4)).toString();
  }

  function fmtFixed(value, digits) {
    const num = Number(value);
    if (!isFinite(num)) return "";
    if (Math.abs(num) >= 1000) return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return Number(num.toPrecision(digits || 4)).toString();
  }

  // Convert an MTTF expressed in operating hours into approximate calendar time,
  // given how many hours per day the asset actually runs. Returns null for invalid
  // inputs (non-positive hours or schedule). 30.4375 = mean calendar-month length
  // (365.25 / 12), so the months/days split lines up with the calendar rather than a
  // flat 30-day month.
  const DAYS_PER_CALENDAR_MONTH = 30.4375;
  function mttfCalendarDuration(operatingHours, hoursPerDay) {
    const hours = Number(operatingHours);
    const hpd = Number(hoursPerDay);
    if (!isFinite(hours) || hours <= 0 || !isFinite(hpd) || hpd <= 0) return null;
    const calendarDays = hours / hpd;
    const months = Math.floor(calendarDays / DAYS_PER_CALENDAR_MONTH);
    const days = Math.round(calendarDays - months * DAYS_PER_CALENDAR_MONTH);
    return { calendarDays, months, days };
  }

  // "≈ 14 months, 13 days" style label for the MTTF calendar conversion.
  function mttfDurationText(operatingHours, hoursPerDay) {
    const d = mttfCalendarDuration(operatingHours, hoursPerDay);
    if (!d) return "";
    const monthLabel = d.months === 1 ? "month" : "months";
    const dayLabel = d.days === 1 ? "day" : "days";
    return `≈ ${d.months} ${monthLabel}, ${d.days} ${dayLabel} of calendar time`;
  }

  // Plain machine-readable number string (no thousands separators) for use as the
  // value of <input type="number">, which rejects comma-grouped values.
  function numericInputValue(value, digits) {
    const num = Number(value);
    if (!isFinite(num)) return "";
    return String(parseFloat(num.toFixed(digits || 6)));
  }

  // ---- network helpers ------------------------------------------------------
  async function requestJson(url, options) {
    const response = await fetch(url, options);
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }
    if (!response.ok) {
      const message = (data && data.error) || `Request failed (${response.status}).`;
      throw new Error(message);
    }
    return data;
  }

  const getJson = (url) => requestJson(url, { headers: { Accept: "application/json" } });
  const postJson = (url, body) =>
    requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    });

  // POST a JSON body and download the binary response (e.g. a generated Word
  // report) as a file, using the server's Content-Disposition filename.
  async function postDownload(url, body, fallbackName) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      try {
        const data = await response.json();
        if (data && data.error) message = data.error;
      } catch (err) {
        /* response body was not JSON; keep the generic message */
      }
      throw new Error(message);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = /filename="?([^";]+)"?/i.exec(disposition);
    const filename = (match && match[1]) || fallbackName || "download";
    const href = URL.createObjectURL(blob);
    const link = el("a", { href, download: filename });
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(href);
    return filename;
  }

  // ---- loading + banner -----------------------------------------------------
  let loadingDepth = 0;
  function beginLoading(message) {
    loadingDepth += 1;
    $("lda-loading-text").textContent = message || "Working…";
    $("lda-loading").hidden = false;
  }
  function endLoading() {
    loadingDepth = Math.max(0, loadingDepth - 1);
    if (loadingDepth === 0) $("lda-loading").hidden = true;
  }

  function showBanner(message, kind) {
    const banner = $("lda-status");
    banner.textContent = message;
    banner.className = "lda-banner " + (kind ? "is-" + kind : "is-info");
    banner.hidden = false;
  }
  function clearBanner() {
    $("lda-status").hidden = true;
  }

  // ---- toast ----------------------------------------------------------------
  // Transient status pill anchored to the top-right of the viewport. Used for
  // disposition save outcomes so the result is visible without scrolling back
  // to the banner at the top of the page.
  function showToast(message, kind) {
    let container = $("lda-toast-container");
    if (!container) {
      container = el("div", { id: "lda-toast-container", class: "lda-toast-container" });
      document.body.appendChild(container);
    }
    const toast = el("div", {
      class: "lda-toast " + (kind ? "is-" + kind : "is-info"),
      role: "status",
      text: message,
    });
    container.appendChild(toast);
    // Trigger the enter transition on the next frame.
    requestAnimationFrame(() => toast.classList.add("is-visible"));
    const remove = () => {
      toast.classList.remove("is-visible");
      toast.addEventListener("transitionend", () => toast.remove(), { once: true });
      // Fallback in case the transitionend never fires.
      setTimeout(() => toast.remove(), 400);
    };
    toast.addEventListener("click", remove);
    setTimeout(remove, 5000);
  }

  // ---- modal ----------------------------------------------------------------
  function openModal({ title, bodyNodes, actions }) {
    return new Promise((resolve) => {
      const backdrop = el("div", { class: "lda-modal-backdrop" });
      const actionRow = el("div", { class: "lda-modal-actions" });
      function close(value) {
        document.removeEventListener("keydown", onKey);
        backdrop.remove();
        resolve(value);
      }
      function onKey(event) {
        if (event.key === "Escape") close(null);
      }
      (actions || []).forEach((action) => {
        const button = el("button", {
          class: action.primary ? "btn-primary" : "btn-secondary",
          text: action.label,
          onclick: () => {
            if (action.validate && !action.validate()) return;
            close(action.value === undefined ? action.label : action.value());
          },
        });
        actionRow.appendChild(button);
      });
      const modal = el("div", { class: "lda-modal" }, [
        el("h3", { text: title }),
        el("div", { class: "lda-modal-body" }, bodyNodes || []),
        actionRow,
      ]);
      backdrop.appendChild(modal);
      backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop) close(null);
      });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(backdrop);
    });
  }

  // ---- asset selection ------------------------------------------------------
  // The asset list is filtered entirely in the browser against state.assets, so
  // every mapped Asset Number is searchable regardless of how many exist. (The
  // previous native <datalist> silently capped its suggestions, which made
  // higher asset numbers appear to be missing from the search.)
  const ASSET_DROPDOWN_LIMIT = 50;

  function setAssetOptions(assets) {
    state.assets = assets || [];
    state.assetByNumber = new Map(state.assets.map((a) => [a.asset_number, a]));
  }

  async function loadAssets() {
    const hint = $("lda-asset-hint");
    try {
      const data = await getJson(`${API}/assets`);
      setAssetOptions(data.assets || []);
      if (state.assetDropdownOpen) renderAssetDropdown();
      hint.textContent = state.assets.length
        ? `${state.assets.length} Asset Number(s) available. Type to search.`
        : "No mapped CMMS Asset Numbers were found in the database.";
    } catch (err) {
      hint.textContent = "";
      showBanner(err.message, "error");
    }
  }

  function filterAssets(query) {
    const q = (query || "").trim().toLowerCase();
    if (!q) return state.assets;
    return state.assets.filter((asset) => {
      const number = String(asset.asset_number || "").toLowerCase();
      const name = String(asset.asset_name || "").toLowerCase();
      return number.includes(q) || name.includes(q);
    });
  }

  function renderAssetDropdown() {
    const list = $("lda-asset-list");
    list.innerHTML = "";
    if (!state.assets.length) {
      list.appendChild(el("li", { class: "lda-combobox-empty", text: "No Asset Numbers available." }));
      state.assetFiltered = [];
      return;
    }
    const query = currentAssetValue();
    const matches = filterAssets(query);
    state.assetFiltered = matches.slice(0, ASSET_DROPDOWN_LIMIT);
    if (!matches.length) {
      list.appendChild(el("li", { class: "lda-combobox-empty", text: `No Asset Numbers match "${query}".` }));
      return;
    }
    state.assetFiltered.forEach((asset, index) => {
      list.appendChild(
        el(
          "li",
          {
            class: "lda-combobox-option" + (index === state.assetActiveIndex ? " is-active" : ""),
            role: "option",
            // Use mousedown so selection happens before the input's blur closes
            // the list; preventDefault keeps focus on the input.
            onmousedown: (event) => {
              event.preventDefault();
              chooseAsset(asset);
            },
          },
          [
            el("span", { class: "lda-combobox-number", text: asset.asset_number }),
            asset.asset_name ? el("span", { class: "lda-combobox-name", text: asset.asset_name }) : null,
          ]
        )
      );
    });
    if (matches.length > state.assetFiltered.length) {
      list.appendChild(
        el("li", {
          class: "lda-combobox-empty",
          text: `Showing first ${state.assetFiltered.length} of ${matches.length} matches. Keep typing to narrow.`,
        })
      );
    }
  }

  function openAssetDropdown() {
    renderAssetDropdown();
    $("lda-asset-list").hidden = false;
    $("lda-asset").setAttribute("aria-expanded", "true");
    state.assetDropdownOpen = true;
  }

  function closeAssetDropdown() {
    $("lda-asset-list").hidden = true;
    $("lda-asset").setAttribute("aria-expanded", "false");
    state.assetDropdownOpen = false;
    state.assetActiveIndex = -1;
  }

  function chooseAsset(asset) {
    $("lda-asset").value = asset.asset_number;
    closeAssetDropdown();
    evaluateAssetSelection();
  }

  function moveAssetActive(delta) {
    const count = state.assetFiltered.length;
    if (!count) return;
    let index = state.assetActiveIndex + delta;
    if (index < 0) index = count - 1;
    if (index >= count) index = 0;
    state.assetActiveIndex = index;
    renderAssetDropdown();
    const active = $("lda-asset-list").querySelectorAll(".lda-combobox-option")[index];
    if (active) active.scrollIntoView({ block: "nearest" });
  }

  function onAssetKeydown(event) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!state.assetDropdownOpen) openAssetDropdown();
      moveAssetActive(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!state.assetDropdownOpen) openAssetDropdown();
      moveAssetActive(-1);
    } else if (event.key === "Enter") {
      if (state.assetDropdownOpen && state.assetActiveIndex >= 0) {
        event.preventDefault();
        chooseAsset(state.assetFiltered[state.assetActiveIndex]);
      }
    } else if (event.key === "Escape") {
      closeAssetDropdown();
    }
  }

  let assetDebounce = null;
  function onAssetInput() {
    state.assetActiveIndex = -1;
    openAssetDropdown();
    if (assetDebounce) clearTimeout(assetDebounce);
    assetDebounce = setTimeout(evaluateAssetSelection, 300);
  }

  function currentAssetValue() {
    let value = ($("lda-asset").value || "").trim();
    if (value.includes(" — ")) value = value.split(" — ")[0].trim();
    return value;
  }

  function evaluateAssetSelection() {
    const value = currentAssetValue();
    const asset = state.assetByNumber.get(value) || null;
    const previous = state.selectedAsset;
    state.selectedAsset = asset ? asset.asset_number : null;
    if (previous !== state.selectedAsset) {
      state.latestResult = null;
      // A new asset invalidates the cached trend data and any failure-mechanism
      // selection driving the trend chart or PM effectiveness analysis.
      state.trend = null;
      state.selectedTrend = null;
      state.trendRange = { from: null, to: null };
      state.trendSelectedMonth = null;
      state.pmSelection = null;
      state.pmData = null;
      state.pmRange = { from: null, to: null };
      // Bump the PM token so an in-flight pm-effectiveness request for the prior
      // asset can't render after this reset (e.g. switching away and back).
      state.pmToken += 1;
      // A new asset likewise invalidates the Downtime Driver selection/data; bump
      // its token so a late downtime-drivers response for the old asset is dropped.
      state.downtimeSelection = null;
      state.downtimeData = null;
      state.downtimeToken += 1;
      // A new asset's failure mode/mechanism ids may not apply; drop the carried
      // selection so a type switch doesn't auto-run a stale mechanism on it.
      state.activeMechanismRow = null;
      clearWorkspace();
    }
    const ready = Boolean(state.selectedAsset);
    // The summary / action / calculate cards only exist on the Perform Analysis
    // page; guard so the same asset combobox can drive the disposition page too.
    const summaryCard = $("lda-summary-card");
    const actionsBar = $("lda-actions");
    const calcCard = $("lda-calculate-all-card");
    if (summaryCard) summaryCard.hidden = !ready;
    if (actionsBar) actionsBar.hidden = !ready;
    if (calcCard) calcCard.hidden = !ready;
    if (asset) {
      $("lda-asset-hint").textContent = asset.asset_name
        ? `Selected ${asset.asset_number} — ${asset.asset_name}.`
        : `Selected ${asset.asset_number}.`;
      if (state.pageMode === "disposition") reloadDispositionForSelection();
      else refreshSummary();
    } else if (value) {
      if (state.pageMode === "disposition") clearWorkspace();
      $("lda-asset-hint").textContent = `"${value}" is not a known Asset Number. Choose one from the list.`;
    }
  }

  function clearWorkspace() {
    // Failure mode/mechanism dropdown lists are portaled to <body>; remove any
    // that are still open so re-rendering the editor never orphans them.
    document.querySelectorAll("body > .lda-portal-list").forEach((node) => node.remove());
    $("lda-workspace").innerHTML = "";
    state.analysisRedraw = null;
    state.dispositionChangedFn = null;
    // Invalidate any in-flight disposition load so it can't render into the
    // workspace we just cleared (e.g. the asset selection was removed).
    state.dispositionToken += 1;
  }

  // ---- readiness summary ----------------------------------------------------
  async function refreshSummary() {
    // The summary grid / rankings / Pareto chart only exist on the Perform
    // Analysis page; the dedicated disposition page has no such markup.
    if (state.pageMode === "disposition") return;
    if (!state.selectedAsset) return;
    const asset = state.selectedAsset;
    const token = ++state.summaryToken;
    try {
      const data = await getJson(`${API}/summary?asset=${encodeURIComponent(asset)}`);
      if (token !== state.summaryToken || state.selectedAsset !== asset) return;
      renderSummary(data.summary || {});
      renderRankings(data.rankings || []);
      state.paretoRows = data.pareto || [];
      state.trend = data.trend || null;
      drawPareto();
      // The trend summary cards and chart share the same filtered dataset as the
      // Pareto, so refresh them here too (this fires on asset selection and after
      // every disposition change, keeping the cards in sync with the filters).
      if (state.analysisType === ANALYSIS_TYPES.TREND) renderTrend();
      // PM effectiveness is computed from a separate endpoint, but a disposition
      // change can move a failure in/out of the included set, so re-fetch it when
      // a mechanism is already selected so every card/chart/table stays in sync.
      // With no selection (e.g. the asset just changed, clearing it), render the
      // empty PM state so the previous asset's cards/chart/table don't linger.
      if (state.analysisType === ANALYSIS_TYPES.PM) {
        if (state.pmSelection) loadPmEffectiveness();
        else renderPm();
      }
      // Downtime Driver Analysis is computed from its own endpoint but shares the
      // included-failure dataset, so a disposition change can move work orders in/out
      // of the selected mechanism — re-fetch when a mechanism is selected so every
      // card/chart/table stays in sync; otherwise render the empty state.
      if (state.analysisType === ANALYSIS_TYPES.DOWNTIME) {
        if (state.downtimeSelection) loadDowntime();
        else renderDowntime();
      }
    } catch (err) {
      if (token === state.summaryToken) showBanner(err.message, "error");
    }
  }

  function renderSummary(summary) {
    const grid = $("lda-summary-grid");
    grid.innerHTML = "";
    SUMMARY_FIELDS.forEach(([key, label]) => {
      grid.appendChild(
        el("div", { class: "lda-metric" }, [
          el("span", { class: "lda-metric-value", text: String(summary[key] ?? "—") }),
          el("span", { class: "lda-metric-label", text: label }),
        ])
      );
    });
  }

  function renderRankings(rankings) {
    const list = $("lda-beta-rankings");
    list.innerHTML = "";
    if (!rankings.length) {
      list.appendChild(el("li", { class: "is-empty", text: "No saved Weibull mechanism results yet." }));
      return;
    }
    rankings.forEach((row) => {
      list.appendChild(
        el("li", {
          text:
            `${row.failure_mechanism_name} — beta ${fmt(row.beta_mle)} ` +
            `(${row.failure_count} failures, eta ${fmt(row.eta_mle)} h)`,
        })
      );
    });
  }

  // ---- Pareto chart ---------------------------------------------------------
  // Sort by the active metric and recompute the cumulative percentage for it, so
  // the "failure count" view is a real count Pareto rather than the downtime
  // ordering/cumulative returned by failure_mechanism_pareto().
  function paretoDisplayRows() {
    const metric = state.paretoMetric;
    const rows = state.paretoRows.map((row) => ({ ...row }));
    rows.sort((a, b) => (Number(b[metric]) || 0) - (Number(a[metric]) || 0));
    const total = rows.reduce((sum, row) => sum + (Number(row[metric]) || 0), 0) || 1;
    let cumulative = 0;
    rows.forEach((row) => {
      cumulative += Number(row[metric]) || 0;
      row._cumulative_percent = (cumulative / total) * 100;
    });
    return rows;
  }

  // Show at most this many mechanisms (the highest-ranked by the active metric).
  // Fewer are shown when fewer exist — this is a cap, not a fixed count.
  const PARETO_MAX_BARS = 15;

  function drawPareto() {
    const canvas = $("lda-pareto-chart");
    const empty = $("lda-pareto-empty");
    const allRows = paretoDisplayRows();
    // Cap the number of bars. Cumulative % is still computed across every mechanism
    // (in paretoDisplayRows), so the line reflects the true contribution of the top
    // ones instead of renormalizing to just the displayed subset.
    const rows = allRows.slice(0, PARETO_MAX_BARS);
    empty.hidden = allRows.length > 0;
    const metric = state.paretoMetric;
    const { ctx, width: W, height: H } = setupCanvas(canvas, 320);
    ctx.clearRect(0, 0, W, H);
    if (!rows.length) return;

    const left = 64; // room for the left value-axis labels + rotated title
    const right = W - 56; // room for the right cumulative %-axis labels + title
    const top = 24;
    const bottom = H - 72; // room for the rotated mechanism labels below the axis
    const plotH = bottom - top;
    const slot = (right - left) / rows.length;
    const maxVal = Math.max(...rows.map((r) => Number(r[metric]) || 0), 1);
    const barGap = 8;
    const barW = Math.max(6, slot - barGap);
    const hitboxes = [];

    // Compact axis tick label so large downtime values stay narrow (e.g. 12.3k).
    const tickLabel = (v) => {
      const n = Number(v) || 0;
      if (Math.abs(n) >= 1000) return Math.round(n / 100) / 10 + "k";
      return n >= 100 ? String(Math.round(n)) : Number(n.toPrecision(3)).toString();
    };

    // Horizontal gridlines + numeric y-axis labels (left = metric value, right = %).
    const tickCount = 5;
    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= tickCount; i += 1) {
      const frac = i / tickCount;
      const y = bottom - frac * plotH;
      ctx.strokeStyle = "#eef2f6";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.fillStyle = "#5e7082";
      ctx.textAlign = "right";
      ctx.fillText(tickLabel(maxVal * frac), left - 7, y);
      ctx.textAlign = "left";
      ctx.fillText(Math.round(frac * 100) + "%", right + 7, y);
    }
    ctx.textBaseline = "alphabetic";

    // Left and right axes.
    ctx.strokeStyle = "#c4d2dd";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.moveTo(right, top);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    // Bars + rotated mechanism labels.
    rows.forEach((row, index) => {
      const value = Number(row[metric]) || 0;
      const x = left + index * slot + barGap / 2;
      const barHeight = (value / maxVal) * plotH;
      const y = bottom - barHeight;
      ctx.fillStyle = "#3f5e77";
      ctx.fillRect(x, y, barW, barHeight);
      hitboxes.push({ x, y: top, w: barW, h: plotH, row });

      ctx.save();
      ctx.fillStyle = "#5e7082";
      ctx.font = "10px Inter, sans-serif";
      ctx.translate(x + barW / 2, bottom + 6);
      ctx.rotate(Math.PI / 5);
      const label = (row.failure_mechanism_name || "—").slice(0, 18);
      ctx.fillText(label, 0, 0);
      ctx.restore();
    });

    // Cumulative percent line + markers (right axis scale: 0..100%).
    ctx.strokeStyle = "#c2723b";
    ctx.lineWidth = 2;
    ctx.beginPath();
    rows.forEach((row, index) => {
      const x = left + index * slot + slot / 2;
      const y = bottom - ((Number(row._cumulative_percent) || 0) / 100) * plotH;
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.fillStyle = "#c2723b";
    rows.forEach((row, index) => {
      const x = left + index * slot + slot / 2;
      const y = bottom - ((Number(row._cumulative_percent) || 0) / 100) * plotH;
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    });

    // Rotated axis titles on both sides.
    ctx.fillStyle = "#5e7082";
    ctx.font = "10px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.save();
    ctx.translate(13, (top + bottom) / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText(metric === "failure_count" ? "Failure count" : "Downtime (hours)", 0, 0);
    ctx.restore();
    ctx.save();
    ctx.translate(W - 9, (top + bottom) / 2);
    ctx.rotate(Math.PI / 2);
    ctx.fillText("Cumulative %", 0, 0);
    ctx.restore();

    // Note when the bar count was capped so the cut-off is explicit.
    if (allRows.length > rows.length) {
      ctx.fillText(`Top ${rows.length} of ${allRows.length} mechanisms`, (left + right) / 2, 12);
    }
    ctx.textAlign = "left";

    canvas.onclick = (event) => {
      const rect = canvas.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      const hit = hitboxes.find((box) => px >= box.x && px <= box.x + box.w && py >= box.y && py <= box.y + box.h);
      if (hit) onParetoBarSelected(hit.row);
    };
  }

  // A Pareto bar click drives the active analysis: Weibull runs the clicked
  // mechanism's fit; Failure Mode Trend selects it as the trended mechanism. The
  // not-yet-implemented types have no secondary action, so the click is ignored.
  function onParetoBarSelected(row) {
    if (state.analysisType === ANALYSIS_TYPES.TREND) {
      selectTrendMechanism(row);
    } else if (state.analysisType === ANALYSIS_TYPES.PM) {
      selectPmMechanism(row);
    } else if (state.analysisType === ANALYSIS_TYPES.DOWNTIME) {
      selectDowntimeMechanism(row);
    } else if (state.analysisType === ANALYSIS_TYPES.WEIBULL) {
      runParetoMechanism(row);
    }
  }

  function runParetoMechanism(row) {
    if (row.failure_mode_id == null || row.failure_mechanism_id == null) {
      showBanner("The selected Pareto bar does not have a complete failure mode/mechanism selection.", "error");
      return;
    }
    setActiveMechanism(row);
    runAnalysisForGroup(
      {
        grouping_level: "FAILURE_MECHANISM",
        failure_mode_id: row.failure_mode_id,
        failure_mechanism_id: row.failure_mechanism_id,
      },
      "Running clicked mechanism Weibull analysis…"
    );
  }

  // ---- analysis type switching ----------------------------------------------
  function setHidden(node, hidden) {
    if (node) node.hidden = Boolean(hidden);
  }

  function setAnalysisType(value) {
    state.analysisType = value || ANALYSIS_TYPES.WEIBULL;
    applyAnalysisTypeUI();
  }

  // Normalize a Pareto row / Weibull group into the minimal failure mode+mechanism
  // shape that every analysis type's select function understands. Stored as the
  // "active" selection so it can be replayed onto a different analysis when the user
  // switches analysis types.
  function rowToActiveSelection(row) {
    if (!row) return null;
    return {
      failure_mode_id: row.failure_mode_id != null ? row.failure_mode_id : null,
      failure_mechanism_id: row.failure_mechanism_id != null ? row.failure_mechanism_id : null,
      failure_mode_name: row.failure_mode_name != null ? row.failure_mode_name : null,
      failure_mechanism_name: row.failure_mechanism_name != null ? row.failure_mechanism_name : null,
      // Preserve the Weibull grouping level when the source carries one (the modal's
      // "Failure mode" vs "Failure mechanism" groups). Pareto rows omit it but are
      // always mechanism-level, so it's derived from the mechanism id when absent.
      grouping_level: row.grouping_level != null ? row.grouping_level : null,
    };
  }

  // The Weibull grouping level implied by an active selection: an explicit level from
  // the source when present, otherwise mechanism-level when a mechanism is set and
  // mode-level when only a mode is. The backend accepts both FAILURE_MODE and
  // FAILURE_MECHANISM, so mode-only selections stay runnable.
  function weibullGroupingLevel(active) {
    if (active.grouping_level) return active.grouping_level;
    return active.failure_mechanism_id != null ? "FAILURE_MECHANISM" : "FAILURE_MODE";
  }

  function setActiveMechanism(row) {
    const normalized = rowToActiveSelection(row);
    if (normalized) state.activeMechanismRow = normalized;
  }

  function selectionMatches(selection, active) {
    return Boolean(
      selection &&
        active &&
        selection.failure_mode_id == active.failure_mode_id &&
        selection.failure_mechanism_id == active.failure_mechanism_id
    );
  }

  // Replay the active failure mode/mechanism onto the newly selected analysis type so
  // switching analyses keeps the same failure focus and auto-computes it. Returns
  // true when it kicked off the selection/compute for the type (so the caller can
  // skip its own empty-state render). PM needs a specific mechanism, so a mode-only
  // focus can't drive it — that case clears any stale PM result and falls back to the
  // empty prompt. Weibull and the trend/downtime analyses run at mode level too.
  function applyCarriedSelection(type) {
    const active = state.activeMechanismRow;
    if (!active) return false;
    if (type === ANALYSIS_TYPES.TREND) {
      if (active.failure_mode_id == null || selectionMatches(state.selectedTrend, active)) return false;
      selectTrendMechanism(active);
      return true;
    }
    if (type === ANALYSIS_TYPES.PM) {
      if (active.failure_mechanism_id == null) {
        // The carried focus is mode-level and can't drive PM (which needs a specific
        // mechanism). Drop any stale mechanism PM result so the panel shows the empty
        // "pick a mechanism" prompt for the current focus, not a previous mechanism's
        // data. Bump the token so an in-flight load for the old mechanism is dropped.
        if (state.pmSelection || state.pmData) {
          state.pmSelection = null;
          state.pmData = null;
          state.pmToken += 1;
        }
        return false;
      }
      if (selectionMatches(state.pmSelection, active)) return false;
      selectPmMechanism(active);
      return true;
    }
    if (type === ANALYSIS_TYPES.DOWNTIME) {
      if (active.failure_mode_id == null || selectionMatches(state.downtimeSelection, active)) return false;
      selectDowntimeMechanism(active);
      return true;
    }
    if (type === ANALYSIS_TYPES.WEIBULL) {
      if (active.failure_mode_id == null) return false;
      const groupingLevel = weibullGroupingLevel(active);
      runAnalysisForGroup(
        {
          grouping_level: groupingLevel,
          failure_mode_id: active.failure_mode_id,
          failure_mechanism_id: groupingLevel === "FAILURE_MECHANISM" ? active.failure_mechanism_id : null,
        },
        "Recomputing Weibull analysis for the carried-over selection…"
      );
      return true;
    }
    return false;
  }

  // Toggle the secondary analysis panel (and the Step 2 heading) to match the
  // selected analysis type. The Pareto panel is never touched here, so it stays
  // visible for every analysis type.
  function applyAnalysisTypeUI() {
    const type = state.analysisType;
    const isWeibull = type === ANALYSIS_TYPES.WEIBULL;
    const isTrend = type === ANALYSIS_TYPES.TREND;
    const isPm = type === ANALYSIS_TYPES.PM;
    const isDowntime = type === ANALYSIS_TYPES.DOWNTIME;
    const isPlaceholder = !isWeibull && !isTrend && !isPm && !isDowntime;

    const heading = $("lda-step-2");
    if (heading) {
      heading.textContent = isWeibull
        ? "Asset Weibull readiness summary"
        : isTrend
        ? "Failure mode trend summary"
        : isPm
        ? "PM effectiveness summary"
        : isDowntime
        ? "Downtime driver summary"
        : type;
    }

    // Weibull-specific cards/sections are hidden for every non-Weibull type so no
    // Weibull labels remain on screen.
    setHidden($("lda-weibull-summary"), !isWeibull);
    setHidden($("lda-beta-panel"), !isWeibull);
    setHidden($("lda-trend-summary"), !isTrend);
    setHidden($("lda-trend-chart-panel"), !isTrend);
    setHidden($("lda-trend-table-panel"), !isTrend);
    setHidden($("lda-trend-wo-panel"), !isTrend);
    setHidden($("lda-pm-summary"), !isPm);
    setHidden($("lda-pm-chart-panel"), !isPm);
    setHidden($("lda-pm-table-panel"), !isPm);
    setHidden($("lda-downtime-summary"), !isDowntime);
    setHidden($("lda-downtime-trend-panel"), !isDowntime);
    setHidden($("lda-downtime-dist-panel"), !isDowntime);
    setHidden($("lda-downtime-asset-panel"), !isDowntime);
    setHidden($("lda-downtime-events-panel"), !isDowntime);
    setHidden($("lda-placeholder-summary"), !isPlaceholder);

    // The beta panel now sits in its own full-width row above the Pareto, so
    // showing/hiding it no longer changes the Pareto's width. Redraw anyway when a
    // type switch could have altered the layout (e.g. a panel above appearing and
    // shifting the scrollbar) so the canvas backing store and bar hitboxes stay
    // sized to the live parent width.
    if (state.paretoRows.length) drawPareto();

    if (isPlaceholder) {
      const text = $("lda-placeholder-text");
      if (text) text.textContent = `${type} is coming soon.`;
    }
    // A rendered Weibull result lives in the workspace; drop it for non-Weibull
    // types so no Weibull-specific plots/cards linger after switching.
    if (!isWeibull) {
      state.latestResult = null;
      clearWorkspace();
    }

    // Carry the most-recently selected failure mode/mechanism onto the newly chosen
    // analysis type and auto-compute it, so switching analyses keeps the same
    // failure focus instead of resetting to "pick a mechanism". When it handles the
    // selection, skip the per-type empty/idle render below to avoid double work.
    const autoComputed = applyCarriedSelection(type);

    if (isTrend && !autoComputed) renderTrend();
    if (isPm && !autoComputed) {
      // Returning to PM mode with a selection but no data (e.g. the in-flight
      // request was dropped as stale when the user switched type mid-request)
      // must re-fetch, otherwise the panels would prompt to reselect a mechanism.
      if (state.pmSelection && !state.pmData) loadPmEffectiveness();
      else renderPm();
    }
    if (isDowntime && !autoComputed) {
      // Same re-fetch guard as PM: a selection without data (dropped as stale on a
      // mid-request type switch) re-fetches instead of showing the reselect prompt.
      if (state.downtimeSelection && !state.downtimeData) loadDowntime();
      else renderDowntime();
    }
  }

  // ---- failure mode trend ---------------------------------------------------
  function renderTrend() {
    renderTrendCards();
    renderTrendControls();
    renderTrendChart();
    renderTrendTable();
    renderTrendRecordsTable();
  }

  // Signed "+N / −N vs. prior 3 mo" label for the growth/improvement cards.
  function trendDeltaText(value) {
    const n = Number(value) || 0;
    const sign = n > 0 ? "+" : n < 0 ? "−" : "±";
    return `${sign}${Math.abs(n)} occ. vs. prior 3 mo`;
  }

  function renderTrendCards() {
    const grid = $("lda-trend-cards");
    if (!grid) return;
    grid.innerHTML = "";
    const trend = state.trend;
    const summary = (trend && trend.summary) || {};
    // Each growth card carries the wording shown when no mechanism moved in its
    // direction (distinct from "Insufficient Data", which means too few months).
    const cards = [
      ["most_frequent", "Most Frequent", (c) => `${c.value} work orders`, null],
      ["highest_downtime", "Highest Downtime", (c) => `${fmt(c.value)} downtime hours`, null],
      ["fastest_growing", "Fastest Growing", (c) => trendDeltaText(c.value), "No mechanism increased"],
      ["most_improved", "Most Improved", (c) => trendDeltaText(c.value), "No mechanism decreased"],
    ];
    cards.forEach(([key, label, detail, emptyDirectionText]) => {
      const entry = summary[key];
      let valueText;
      let detailText = "";
      if (entry) {
        valueText = entry.failure_mechanism_name || "—";
        detailText = detail(entry);
      } else if (emptyDirectionText && trend && !trend.has_growth_window) {
        // Fewer than six months of data, so growth/improvement can't be computed.
        valueText = "Insufficient Data";
      } else if (emptyDirectionText && trend) {
        // Enough data, but no mechanism moved in this card's direction.
        valueText = "—";
        detailText = emptyDirectionText;
      } else {
        valueText = "—";
      }
      grid.appendChild(
        el("div", { class: "lda-metric lda-trend-metric" }, [
          el("span", { class: "lda-metric-label", text: label }),
          el("span", { class: "lda-metric-value lda-trend-metric-value", text: valueText }),
          detailText ? el("span", { class: "lda-metric-label", text: detailText }) : null,
        ])
      );
    });
  }

  // Build the monthly occurrence series for the selected failure mode/mechanism
  // across the FULL data range. A mechanism-level selection plots that one
  // mechanism; a mode-level selection (no mechanism id) sums every mechanism under
  // the mode. Returns null when there is no selection, no trend data, or the
  // selection has no dated occurrences.
  function fullTrendSeries() {
    const trend = state.trend;
    const sel = state.selectedTrend;
    if (!trend || !sel) return null;
    const months = trend.months || [];
    if (!months.length) return null;
    const matches = (trend.mechanisms || []).filter(
      (m) =>
        Number(m.failure_mode_id) === Number(sel.failure_mode_id) &&
        (sel.failure_mechanism_id == null || Number(m.failure_mechanism_id) === Number(sel.failure_mechanism_id))
    );
    if (!matches.length) return null;
    const counts = months.map((_, index) =>
      matches.reduce((sum, m) => sum + (Number((m.monthly_counts || [])[index]) || 0), 0)
    );
    if (counts.reduce((a, b) => a + b, 0) === 0) return null;
    return { label: sel.label, months, counts };
  }

  // The full series restricted to the active date range (state.trendRange). Month
  // keys are "YYYY-MM", so string comparison gives the right chronological bounds.
  // Returns the same shape as fullTrendSeries; `months` may be empty when the
  // range excludes every dated occurrence (the renderers show a range-specific
  // empty state in that case rather than treating it as "no selection").
  function selectedTrendSeries() {
    const base = fullTrendSeries();
    if (!base) return null;
    const { from, to } = state.trendRange;
    if (!from && !to) return base;
    const months = [];
    const counts = [];
    base.months.forEach((key, index) => {
      if (from && key < from) return;
      if (to && key > to) return;
      months.push(key);
      counts.push(base.counts[index]);
    });
    return { label: base.label, months, counts };
  }

  function selectTrendMechanism(row) {
    if (row == null || row.failure_mode_id == null) return;
    setActiveMechanism(row);
    const modeName = row.failure_mode_name;
    const mechName = row.failure_mechanism_name;
    const label = mechName
      ? modeName
        ? `${modeName} / ${mechName}`
        : mechName
      : modeName || "the selected failure mode";
    state.selectedTrend = {
      failure_mode_id: row.failure_mode_id,
      failure_mechanism_id: row.failure_mechanism_id != null ? row.failure_mechanism_id : null,
      label,
    };
    // A new mode/mechanism invalidates any month the user had drilled into.
    state.trendSelectedMonth = null;
    renderTrendControls();
    renderTrendChart();
    renderTrendTable();
    renderTrendRecordsTable();
  }

  // Show and populate the date-range inputs whenever the selected mode/mechanism
  // has a plottable series. The inputs are bounded by the full data range; the
  // current value falls back to the data bounds when no explicit range is set.
  function renderTrendControls() {
    const controls = $("lda-trend-controls");
    if (!controls) return;
    const fromInput = $("lda-trend-from");
    const toInput = $("lda-trend-to");
    const base = fullTrendSeries();
    if (!base || !base.months.length) {
      controls.hidden = true;
      return;
    }
    controls.hidden = false;
    const minMonth = base.months[0];
    const maxMonth = base.months[base.months.length - 1];
    fromInput.min = minMonth;
    fromInput.max = maxMonth;
    toInput.min = minMonth;
    toInput.max = maxMonth;
    fromInput.value = state.trendRange.from || minMonth;
    toInput.value = state.trendRange.to || maxMonth;
  }

  // Apply the From/To month inputs to state.trendRange and redraw. Bounds are kept
  // ordered (from <= to) by swapping when the user picks an inverted range.
  function onTrendRangeChange() {
    const fromInput = $("lda-trend-from");
    const toInput = $("lda-trend-to");
    let from = fromInput.value || null;
    let to = toInput.value || null;
    if (from && to && from > to) {
      [from, to] = [to, from];
      fromInput.value = from;
      toInput.value = to;
    }
    state.trendRange.from = from;
    state.trendRange.to = to;
    // Drop a drilled month that the new range no longer covers so the WO table
    // can't stay filtered to a now-hidden month.
    const month = state.trendSelectedMonth;
    if (month && ((from && month < from) || (to && month > to))) {
      state.trendSelectedMonth = null;
    }
    renderTrendChart();
    renderTrendTable();
    renderTrendRecordsTable();
  }

  function resetTrendRange() {
    state.trendRange = { from: null, to: null };
    state.trendSelectedMonth = null;
    renderTrendControls();
    renderTrendChart();
    renderTrendTable();
    renderTrendRecordsTable();
  }

  // Toggle the drilled-into month for the Work Orders in Trend table. Clicking the
  // active month again clears the drill-down (back to every month in the range).
  function toggleTrendMonth(month) {
    if (!month) return;
    state.trendSelectedMonth = state.trendSelectedMonth === month ? null : month;
    renderTrendChart();
    renderTrendTable();
    renderTrendRecordsTable();
  }

  function renderTrendChart() {
    const canvas = $("lda-trend-chart");
    const hint = $("lda-trend-selection");
    if (!canvas) return;
    const series = selectedTrendSeries();
    if (!series || !series.months.length) {
      if (hint) {
        hint.hidden = false;
        if (!state.selectedTrend) {
          hint.textContent =
            "Select a failure mode or mechanism from the Pareto chart or analysis controls to view the trend.";
        } else if (series) {
          // A selection with a plottable full series, but the active date range
          // excludes every month — point the user at the range, not the data.
          hint.textContent = `No occurrences for ${state.selectedTrend.label} in the selected date range.`;
        } else {
          hint.textContent = `No occurrences found for ${state.selectedTrend.label} in the current dataset.`;
        }
      }
      canvas.hidden = true;
      return;
    }
    if (hint) {
      hint.hidden = false;
      hint.textContent = `Monthly occurrence count for ${series.label}. Click a point to show only that month's work orders below.`;
    }
    canvas.hidden = false;
    const selectedIndex = state.trendSelectedMonth ? series.months.indexOf(state.trendSelectedMonth) : -1;
    drawTrendChart(canvas, series.months, series.counts, "Occurrence count", {
      selectedIndex,
      onPointClick: (index) => toggleTrendMonth(series.months[index]),
    });
  }

  // Tabular view of the exact values feeding the trend chart: one row per month in
  // the active date range, plus a total. Kept in sync with the chart by sharing
  // selectedTrendSeries(), so range changes update both together.
  function renderTrendTable() {
    const wrap = $("lda-trend-table-wrap");
    if (!wrap) return;
    wrap.innerHTML = "";
    const series = selectedTrendSeries();
    const headers = ["Month", "Occurrences"];
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { text: h })))]));
    const tbody = el("tbody");
    const months = (series && series.months) || [];
    if (!months.length) {
      const emptyText = !state.selectedTrend
        ? "Select a failure mode or mechanism to view its monthly detail."
        : series
        ? "No occurrences in the selected date range."
        : "No occurrences found for the selected failure mode or mechanism.";
      tbody.appendChild(
        el("tr", {}, [
          el("td", { class: "lda-readonly lda-empty-row", colspan: String(headers.length), text: emptyText }),
        ])
      );
    } else {
      months.forEach((key, index) => {
        const isActive = state.trendSelectedMonth === key;
        const tr = el(
          "tr",
          {
            class: `lda-trend-month-row${isActive ? " is-active" : ""}`,
            title: "Click to show only this month's work orders below",
            onclick: () => toggleTrendMonth(key),
          },
          [el("td", { text: monthLabel(key) }), el("td", { text: String(series.counts[index]) })]
        );
        tbody.appendChild(tr);
      });
      const total = series.counts.reduce((sum, value) => sum + value, 0);
      tbody.appendChild(
        el("tr", { class: "lda-trend-total" }, [
          el("td", { text: "Total" }),
          el("td", { text: String(total) }),
        ])
      );
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  // Individual work orders backing the trend for the selected mode/mechanism,
  // honoring the active date range and (when set) the drilled-into month. Returns
  // newest-first so the most recent work appears at the top of the detail table.
  function selectedTrendRecords() {
    const trend = state.trend;
    const sel = state.selectedTrend;
    if (!trend || !sel) return [];
    const matches = (trend.mechanisms || []).filter(
      (m) =>
        Number(m.failure_mode_id) === Number(sel.failure_mode_id) &&
        (sel.failure_mechanism_id == null || Number(m.failure_mechanism_id) === Number(sel.failure_mechanism_id))
    );
    const { from, to } = state.trendRange;
    const month = state.trendSelectedMonth;
    const records = [];
    matches.forEach((m) => {
      (m.records || []).forEach((record) => {
        if (month) {
          if (record.month !== month) return;
        } else {
          if (from && record.month < from) return;
          if (to && record.month > to) return;
        }
        records.push(record);
      });
    });
    records.sort((a, b) => String(b.month || "").localeCompare(String(a.month || "")));
    return records;
  }

  // "Work Orders in Trend" table: one row per work order feeding the plotted
  // months, filtered to the drilled month when one is selected. Shows the WO id,
  // title, request description and completion notes so the trend is traceable to
  // the source records.
  function renderTrendRecordsTable() {
    const wrap = $("lda-trend-wo-wrap");
    const hint = $("lda-trend-wo-hint");
    if (!wrap) return;
    wrap.innerHTML = "";
    if (hint) {
      if (!state.selectedTrend) {
        hint.textContent = "The specific work orders that populate the months plotted above.";
      } else if (state.trendSelectedMonth) {
        hint.textContent = `Work orders for ${monthLabel(state.trendSelectedMonth)}. Click the month again to show every month in range.`;
      } else {
        hint.textContent = "The specific work orders that populate the months plotted above. Click a month/data point to drill in.";
      }
    }
    const headers = ["WO #", "WO Title", "Month", "Request Description", "Completion Notes"];
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { text: h })))]));
    const tbody = el("tbody");
    const records = selectedTrendRecords();
    if (!records.length) {
      const emptyText = !state.selectedTrend
        ? "Select a failure mode or mechanism to list its work orders."
        : state.trendSelectedMonth
        ? "No work orders for the selected month."
        : "No work orders for the selected failure mode or mechanism in this range.";
      tbody.appendChild(
        el("tr", {}, [
          el("td", { class: "lda-readonly lda-empty-row", colspan: String(headers.length), text: emptyText }),
        ])
      );
    } else {
      records.forEach((record) => {
        tbody.appendChild(
          el("tr", {}, [
            el("td", { text: record.task_id != null ? String(record.task_id) : "" }),
            el("td", { text: record.task_name || "" }),
            el("td", { text: monthLabel(record.month) }),
            el("td", { class: "lda-wo-text", text: record.requestor_description || "" }),
            el("td", { class: "lda-wo-text", text: record.completion_notes || "" }),
          ])
        );
      });
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  // "2025-01" -> "Jan '25" for compact month-axis labels.
  function monthLabel(key) {
    const parts = String(key || "").split("-");
    if (parts.length !== 2) return String(key || "");
    const monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const monthIndex = Number(parts[1]) - 1;
    const name = monthNames[monthIndex] || parts[1];
    return `${name} '${parts[0].slice(2)}`;
  }

  function drawTrendChart(canvas, months, counts, yLabel, options) {
    const opts = options || {};
    const selectedIndex = Number.isInteger(opts.selectedIndex) ? opts.selectedIndex : -1;
    const onPointClick = typeof opts.onPointClick === "function" ? opts.onPointClick : null;
    // Reset any handler from a previous render so a chart drawn without click
    // support (e.g. no selection) can't keep firing the last callback.
    canvas.onclick = null;
    const { ctx, width: W, height: H } = setupCanvas(canvas, 320);
    ctx.clearRect(0, 0, W, H);
    if (!months.length) return;

    const left = 52;
    const right = W - 18;
    const top = 22;
    const bottom = H - 64; // room for the rotated month labels + axis title
    const plotH = bottom - top;
    const plotW = right - left;
    const maxVal = Math.max(...counts, 1);
    const n = months.length;
    const xAt = (index) => (n === 1 ? left + plotW / 2 : left + (index / (n - 1)) * plotW);
    const yAt = (value) => bottom - (value / maxVal) * plotH;

    // Horizontal gridlines + integer y-axis ticks.
    const tickCount = Math.max(1, Math.min(5, maxVal));
    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= tickCount; i += 1) {
      const frac = i / tickCount;
      const y = bottom - frac * plotH;
      ctx.strokeStyle = "#eef2f6";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.fillStyle = "#5e7082";
      ctx.textAlign = "right";
      ctx.fillText(String(Math.round(maxVal * frac)), left - 7, y);
    }
    ctx.textBaseline = "alphabetic";

    // Axes.
    ctx.strokeStyle = "#c4d2dd";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    // A label is drawn for every month (the continuous axis is zero-filled, so no
    // months are skipped) — each is right-aligned and anchored just below its tick,
    // then rotated counter-clockwise so it hangs down-and-left beneath the axis
    // line. The font shrinks as the series grows so dense ranges stay legible.
    const labelFont = n > 36 ? 8 : n > 24 ? 9 : 10;
    ctx.fillStyle = "#5e7082";
    ctx.font = `${labelFont}px Inter, sans-serif`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    months.forEach((key, index) => {
      ctx.save();
      ctx.translate(xAt(index), bottom + 10);
      ctx.rotate(-Math.PI / 5);
      ctx.fillText(monthLabel(key), 0, 0);
      ctx.restore();
    });
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";

    // Occurrence-count line + markers.
    ctx.strokeStyle = "#3f5e77";
    ctx.lineWidth = 2;
    ctx.beginPath();
    counts.forEach((value, index) => {
      const x = xAt(index);
      const y = yAt(value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    const pointHitboxes = [];
    counts.forEach((value, index) => {
      const x = xAt(index);
      const y = yAt(value);
      const isSelected = index === selectedIndex;
      ctx.fillStyle = isSelected ? "#2a3f50" : "#c2723b";
      ctx.beginPath();
      ctx.arc(x, y, isSelected ? 5 : 3, 0, Math.PI * 2);
      ctx.fill();
      if (isSelected) {
        ctx.strokeStyle = "#2a3f50";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(x, y, 8, 0, Math.PI * 2);
        ctx.stroke();
      }
      pointHitboxes.push({ x, y, index });
    });

    // Axis titles.
    ctx.fillStyle = "#2a3f50";
    ctx.font = "600 11.5px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Month", (left + right) / 2, H - 6);
    ctx.save();
    ctx.translate(13, (top + bottom) / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textBaseline = "middle";
    ctx.fillText(yLabel || "Occurrence count", 0, 0);
    ctx.restore();
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";

    // Data-point clicks drill the detail table into the clicked month. Match the
    // nearest marker within a small radius so clicks near (not exactly on) a point
    // still register.
    if (onPointClick && pointHitboxes.length) {
      canvas.style.cursor = "pointer";
      canvas.onclick = (event) => {
        const rect = canvas.getBoundingClientRect();
        const px = ((event.clientX - rect.left) / rect.width) * W;
        const py = ((event.clientY - rect.top) / rect.height) * H;
        let best = null;
        let bestDist = Infinity;
        pointHitboxes.forEach((box) => {
          const dist = Math.hypot(px - box.x, py - box.y);
          if (dist < bestDist) {
            bestDist = dist;
            best = box;
          }
        });
        if (best && bestDist <= 14) onPointClick(best.index);
      };
    } else {
      canvas.style.cursor = "default";
    }
  }

  // Build the Perform Analysis (trend) picker choices: an "all mechanisms"
  // failure-mode option for every mode that spans more than one mechanism, plus
  // each individual mechanism. The mode-level option carries failure_mechanism_id
  // null so selectedTrendSeries() aggregates every mechanism under that mode —
  // matching the panel text that users can trend a failure mode or a mechanism.
  function trendPickerChoices() {
    const mechanisms = (state.trend && state.trend.mechanisms) || [];
    const byMode = new Map();
    mechanisms.forEach((mechanism) => {
      if (!byMode.has(mechanism.failure_mode_id)) byMode.set(mechanism.failure_mode_id, []);
      byMode.get(mechanism.failure_mode_id).push(mechanism);
    });
    const choices = [];
    byMode.forEach((mechs, modeId) => {
      const modeName = mechs[0].failure_mode_name;
      if (mechs.length > 1) {
        const totalCount = mechs.reduce((sum, m) => sum + (Number(m.total_count) || 0), 0);
        const totalDowntime = mechs.reduce((sum, m) => sum + (Number(m.total_downtime_hours) || 0), 0);
        choices.push({
          row: {
            failure_mode_id: modeId,
            failure_mechanism_id: null,
            failure_mode_name: modeName,
            failure_mechanism_name: null,
          },
          labelText: `${modeName} — all mechanisms (${totalCount} WOs, ${fmt(totalDowntime)} downtime h)`,
        });
      }
      mechs.forEach((mechanism) => {
        choices.push({
          row: mechanism,
          labelText:
            `${mechanism.failure_mode_name} / ${mechanism.failure_mechanism_name} ` +
            `(${mechanism.total_count} WOs, ${fmt(mechanism.total_downtime_hours)} downtime h)`,
        });
      });
    });
    return choices;
  }

  // Perform Analysis in trend mode: pick the failure mode/mechanism to trend from
  // the same filtered dataset as the Pareto, then plot its monthly occurrences.
  async function performTrendSelection() {
    // state.trend is populated by the asynchronous summary request kicked off on
    // asset selection. If the user clicks Perform Analysis before it returns,
    // fetch it first so an empty choice list isn't mistaken for "no trendable
    // mechanisms" while the data is still loading.
    if (!state.trend && state.selectedAsset) {
      beginLoading("Loading failure mechanisms…");
      try {
        await refreshSummary();
      } finally {
        endLoading();
      }
    }
    const choices = trendPickerChoices();
    if (!choices.length) {
      showBanner(
        "No failure mechanisms with included failures are available to trend yet. Disposition WO failures with a failure mechanism first.",
        "error"
      );
      return;
    }
    const options = el("div", { class: "lda-modal-options" });
    choices.forEach((choice, index) => {
      const radio = el("input", { type: "radio", name: "lda-trend-group", value: String(index) });
      if (index === 0) radio.checked = true;
      options.appendChild(el("label", { class: "lda-modal-option" }, [radio, el("span", { text: choice.labelText })]));
    });
    const choice = await openModal({
      title: "Select failure mode or mechanism to trend",
      bodyNodes: [el("p", { text: "Choose the failure mode or mechanism to view its monthly trend:" }), options],
      actions: [
        { label: "Cancel", primary: false, value: () => null },
        {
          label: "View trend",
          primary: true,
          value: () => {
            const checked = options.querySelector("input[name='lda-trend-group']:checked");
            return checked ? Number(checked.value) : null;
          },
        },
      ],
    });
    if (choice === null || choice === undefined) return;
    selectTrendMechanism(choices[choice].row);
  }

  // ---- PM effectiveness analysis --------------------------------------------
  // The selected failure mechanism (Pareto click or Perform Analysis picker)
  // drives a server-side PM-to-failure analysis: every completed PM on the asset
  // is paired with the first corrective WO for this mechanism that follows it.
  function pmSelectionLabel(row) {
    const modeName = row.failure_mode_name;
    const mechName = row.failure_mechanism_name;
    return mechName
      ? modeName
        ? `${modeName} / ${mechName}`
        : mechName
      : modeName || "the selected failure mechanism";
  }

  function selectPmMechanism(row) {
    if (row == null || row.failure_mechanism_id == null) {
      showBanner("PM effectiveness needs a failure mechanism. Pick a mechanism-level Pareto bar.", "error");
      return;
    }
    setActiveMechanism(row);
    state.pmSelection = {
      failure_mode_id: row.failure_mode_id != null ? row.failure_mode_id : null,
      failure_mechanism_id: row.failure_mechanism_id,
      label: pmSelectionLabel(row),
    };
    state.pmData = null;
    // A new mechanism's PM history has its own data range; drop the prior range.
    state.pmRange = { from: null, to: null };
    // Clear the previous mechanism's cards/chart/table immediately so a slow or
    // failed request can't leave stale results visible under the new selection.
    renderPm();
    loadPmEffectiveness();
  }

  async function loadPmEffectiveness() {
    if (state.pageMode === "disposition") return;
    if (!state.selectedAsset || !state.pmSelection) {
      renderPm();
      return;
    }
    const asset = state.selectedAsset;
    const sel = state.pmSelection;
    const token = ++state.pmToken;
    // A response is stale when a newer request superseded this one, the asset or
    // analysis type changed, or the selection was cleared/replaced (object
    // identity also covers switching away and back to the same asset before this
    // resolved). Both the success and error paths use it so a late failure can't
    // surface a PM error over the Weibull/Trend/Downtime view after a type switch.
    const isStale = () =>
      token !== state.pmToken ||
      state.selectedAsset !== asset ||
      state.analysisType !== ANALYSIS_TYPES.PM ||
      state.pmSelection !== sel;
    beginLoading("Evaluating PM effectiveness…");
    try {
      const params = new URLSearchParams({ asset, failure_mechanism_id: sel.failure_mechanism_id });
      if (sel.failure_mode_id != null) params.set("failure_mode_id", sel.failure_mode_id);
      const data = await getJson(`${API}/pm-effectiveness?${params.toString()}`);
      if (isStale()) return;
      // The endpoint wraps the service result under `pm_effectiveness` (matching
      // the other analysis routes), so unwrap it before the renderers read fields
      // like has_pm_history / months / rows directly off state.pmData.
      state.pmData = data.pm_effectiveness || null;
      renderPm();
    } catch (err) {
      if (!isStale()) {
        showBanner(err.message, "error");
        // Reflect the (now-cleared) data so a failed load doesn't leave another
        // mechanism's results on screen; an in-place refresh keeps its own data.
        renderPm();
      }
    } finally {
      endLoading();
    }
  }

  // Perform Analysis in PM mode: pick the failure mechanism to evaluate from the
  // same filtered dataset as the Pareto, then run the PM-to-failure analysis.
  async function performPmSelection() {
    if (!state.trend && state.selectedAsset) {
      beginLoading("Loading failure mechanisms…");
      try {
        await refreshSummary();
      } finally {
        endLoading();
      }
    }
    // PM effectiveness is per-mechanism, so only mechanism-level choices apply
    // (the mode-level "all mechanisms" aggregate has no single mechanism id).
    const choices = trendPickerChoices().filter((choice) => choice.row.failure_mechanism_id != null);
    if (!choices.length) {
      showBanner(
        "No failure mechanisms with included failures are available yet. Disposition WO failures with a failure mechanism first.",
        "error"
      );
      return;
    }
    const options = el("div", { class: "lda-modal-options" });
    choices.forEach((choice, index) => {
      const radio = el("input", { type: "radio", name: "lda-pm-group", value: String(index) });
      if (index === 0) radio.checked = true;
      options.appendChild(el("label", { class: "lda-modal-option" }, [radio, el("span", { text: choice.labelText })]));
    });
    const choice = await openModal({
      title: "Select failure mechanism to evaluate",
      bodyNodes: [el("p", { text: "Choose the failure mechanism to evaluate PM effectiveness for:" }), options],
      actions: [
        { label: "Cancel", primary: false, value: () => null },
        {
          label: "Evaluate",
          primary: true,
          value: () => {
            const checked = options.querySelector("input[name='lda-pm-group']:checked");
            return checked ? Number(checked.value) : null;
          },
        },
      ],
    });
    if (choice === null || choice === undefined) return;
    selectPmMechanism(choices[choice].row);
  }

  function renderPm() {
    renderPmCards();
    renderPmControls();
    renderPmChart();
    renderPmTable();
  }

  // Full continuous month series for the PM "Failures Following PM" chart, before
  // the date-range filter is applied. Returns null when there is no PM data yet.
  function pmFullSeries() {
    const data = state.pmData;
    if (!data) return null;
    const months = data.months || [];
    if (!months.length) return null;
    return { months, counts: data.monthly_counts || [] };
  }

  // The PM month series restricted to the active date range (state.pmRange). Month
  // keys are "YYYY-MM", so string comparison gives the right chronological bounds.
  function selectedPmSeries() {
    const base = pmFullSeries();
    if (!base) return null;
    const { from, to } = state.pmRange;
    if (!from && !to) return base;
    const months = [];
    const counts = [];
    base.months.forEach((key, index) => {
      if (from && key < from) return;
      if (to && key > to) return;
      months.push(key);
      counts.push(base.counts[index]);
    });
    return { months, counts };
  }

  // Show and populate the PM date-range inputs whenever there is a plottable PM
  // series, bounded by the data range (matching the Failure Mode Trend controls).
  function renderPmControls() {
    const controls = $("lda-pm-controls");
    if (!controls) return;
    const fromInput = $("lda-pm-from");
    const toInput = $("lda-pm-to");
    const base = pmFullSeries();
    if (!base || !base.months.length) {
      controls.hidden = true;
      return;
    }
    controls.hidden = false;
    const minMonth = base.months[0];
    const maxMonth = base.months[base.months.length - 1];
    fromInput.min = minMonth;
    fromInput.max = maxMonth;
    toInput.min = minMonth;
    toInput.max = maxMonth;
    fromInput.value = state.pmRange.from || minMonth;
    toInput.value = state.pmRange.to || maxMonth;
  }

  function onPmRangeChange() {
    const fromInput = $("lda-pm-from");
    const toInput = $("lda-pm-to");
    let from = fromInput.value || null;
    let to = toInput.value || null;
    if (from && to && from > to) {
      [from, to] = [to, from];
      fromInput.value = from;
      toInput.value = to;
    }
    state.pmRange.from = from;
    state.pmRange.to = to;
    renderPmChart();
    renderPmTable();
  }

  function resetPmRange() {
    state.pmRange = { from: null, to: null };
    renderPmControls();
    renderPmChart();
    renderPmTable();
  }

  // Color band for a Days to Failure value, matching the PM Effectiveness rating.
  function pmDaysClass(days) {
    const n = Number(days);
    if (!isFinite(n)) return "";
    if (n >= 180) return "lda-days-green";
    if (n >= 90) return "lda-days-yellow";
    if (n >= 30) return "lda-days-orange";
    return "lda-days-red";
  }

  // Shared empty-state text: distinguishes "no selection yet", "no PM history",
  // and "PMs but no subsequent failures" so each panel can show the right prompt.
  function pmEmptyText() {
    const data = state.pmData;
    if (!state.pmSelection) {
      return "Select a failure mode or mechanism from the Pareto chart or analysis controls to evaluate PM effectiveness.";
    }
    // A selection is set but its data hasn't arrived yet (initial load, a new
    // selection that just cleared the previous data, or a failed request): show a
    // neutral evaluating message rather than the reselect prompt or stale results.
    if (!data) {
      return `Evaluating PM effectiveness for ${state.pmSelection.label}…`;
    }
    if (!data.has_pm_history) {
      return "No completed PM work orders were found for this asset.";
    }
    return "No failures recorded following completed PMs within the selected date range.";
  }

  function renderPmCards() {
    const grid = $("lda-pm-cards");
    const message = $("lda-pm-message");
    if (!grid) return;
    grid.innerHTML = "";
    const data = state.pmData;
    if (!state.pmSelection || !data) {
      if (message) {
        message.hidden = false;
        message.textContent = pmEmptyText();
      }
      return;
    }
    const avg = data.average_days_to_failure;
    const cards = [
      ["PMs Performed", String(data.pms_performed ?? 0)],
      ["Failures After PM", String(data.failures_after_pm ?? 0)],
      ["Average Days to Failure", avg != null ? `${fmt(avg)} days` : "Insufficient Data"],
      ["PM Effectiveness", data.effectiveness || "Insufficient Data"],
    ];
    cards.forEach(([label, value]) => {
      grid.appendChild(
        el("div", { class: "lda-metric" }, [
          el("span", { class: "lda-metric-value", text: value }),
          el("span", { class: "lda-metric-label", text: label }),
        ])
      );
    });
    // Surface the informative message for the two empty-but-valid cases; hide it
    // once there is real PM-to-failure data to read from the cards/table.
    if (message) {
      if (!data.has_pm_history || !data.failures_after_pm) {
        message.hidden = false;
        message.textContent = pmEmptyText();
      } else {
        message.hidden = true;
        message.textContent = "";
      }
    }
  }

  function renderPmChart() {
    const canvas = $("lda-pm-chart");
    const hint = $("lda-pm-selection");
    if (!canvas) return;
    const data = state.pmData;
    const series = selectedPmSeries();
    if (!state.pmSelection || !data || !series || !series.months.length) {
      if (hint) {
        hint.hidden = false;
        if (series && !series.months.length) {
          // A plottable series exists, but the active date range excludes it all.
          hint.textContent = `No corrective work orders after PM for ${data.failure_mechanism_name || state.pmSelection.label} in the selected date range.`;
        } else {
          hint.textContent = pmEmptyText();
        }
      }
      canvas.hidden = true;
      return;
    }
    if (hint) {
      hint.hidden = false;
      hint.textContent = `Corrective work orders for ${data.failure_mechanism_name || state.pmSelection.label} occurring after a completed PM, by month.`;
    }
    canvas.hidden = false;
    drawTrendChart(canvas, series.months, series.counts, "Corrective WOs after PM");
  }

  function renderPmTable() {
    const wrap = $("lda-pm-table-wrap");
    if (!wrap) return;
    wrap.innerHTML = "";
    const data = state.pmData;
    // Filter to the active date range by the corrective WO's failure month so the
    // table matches the "Failures Following PM" chart above it.
    const { from, to } = state.pmRange;
    const rows = ((data && data.rows) || []).filter((row) => {
      if (!from && !to) return true;
      const month = String(row.next_failure_date || "").slice(0, 7);
      if (!month) return true;
      if (from && month < from) return false;
      if (to && month > to) return false;
      return true;
    });
    const headers = [
      "PM Completion Date",
      "Asset",
      "Next Failure Date",
      "Days to Failure",
      "Failure Mechanism",
      "Downtime",
      "Corrective WO Number",
    ];
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { text: h })))]));
    const tbody = el("tbody");
    if (!rows.length) {
      tbody.appendChild(
        el("tr", {}, [
          el("td", { class: "lda-readonly lda-empty-row", colspan: String(headers.length), text: pmEmptyText() }),
        ])
      );
    }
    rows.forEach((row) => {
      tbody.appendChild(
        el("tr", {}, [
          el("td", { text: row.pm_completion_date || "" }),
          el("td", { text: row.asset_number || "" }),
          el("td", { text: row.next_failure_date || "" }),
          el("td", { class: pmDaysClass(row.days_to_failure), text: fmt(row.days_to_failure) }),
          el("td", { text: row.failure_mechanism_name || "" }),
          el("td", { text: row.downtime_hours != null ? `${fmt(row.downtime_hours)} h` : "" }),
          el("td", { text: row.corrective_wo_number != null ? String(row.corrective_wo_number) : "" }),
        ])
      );
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  // ---- disposition editor ---------------------------------------------------
  // From the Perform Analysis page the disposition buttons now navigate to the
  // dedicated disposition page (its own route) instead of rendering the editor
  // below everything else. The selected asset and record kind ride along in the
  // query string so the disposition page opens ready to edit.
  function gotoDisposition(kind) {
    if (!state.selectedAsset) {
      showBanner("Select an Asset Number before opening the disposition page.", "error");
      return;
    }
    const params = new URLSearchParams({ asset: state.selectedAsset, kind });
    window.location.href = `/life-data-analysis/disposition?${params.toString()}`;
  }

  // On the dedicated disposition page, (re)load the editor whenever the asset,
  // record kind, or scope changes.
  function reloadDispositionForSelection() {
    if (!state.selectedAsset) {
      clearWorkspace();
      return;
    }
    loadDispositionPage(state.dispositionKind, state.dispositionScope, state.dispositionPageIndex || 0);
  }

  async function loadDispositionPage(kind, scope, pageIndex) {
    // Claim this reload; a newer one (or a workspace clear) bumps the token and
    // supersedes us, so the response below is dropped instead of rendering stale.
    const token = ++state.dispositionToken;
    beginLoading("Loading disposition editor…");
    try {
      const search = state.dispositionSearch ? `&search=${encodeURIComponent(state.dispositionSearch)}` : "";
      const url = `${API}/dispositions?asset=${encodeURIComponent(state.selectedAsset)}&kind=${kind}&scope=${scope}&page=${pageIndex}${search}`;
      const data = await getJson(url);
      if (token !== state.dispositionToken) return;
      renderDispositionEditor(data);
    } catch (err) {
      if (token === state.dispositionToken) showBanner(err.message, "error");
    } finally {
      endLoading();
    }
  }

  function dispositionPayloadFromRow(rowState, kind) {
    const payload = {
      mapped_record_id: rowState.mapped_record_id,
      kind,
      disposition_category: rowState.category.value,
      disposition_text: rowState.notes.value,
      record_class_final: rowState.recordClass.value,
      include_in_weibull_candidate: rowState.include.checked,
    };
    if (kind === "pm") {
      payload.pm_reset_decision = rowState.decision.value;
      payload.pm_reset_rationale = rowState.rationale.value;
      // PM mode/mechanism are searchable dropdowns that carry the real id of the
      // chosen option, so mechanisms sharing a display name across modes stay
      // distinct (and PMs can only point at existing taxonomy entries).
      payload.reset_target_failure_mode_id = rowState.mode.getSelectedId();
      payload.reset_target_failure_mechanism_id = rowState.mech.getSelectedId();
    } else {
      const modeName = rowState.mode.getValue();
      const mechName = rowState.mech.getValue();
      const modeId = modeName ? (rowState.modeOptions.get(modeName) ?? null) : null;
      payload.failure_mode_id = modeId;
      // Resolve the mechanism id within the selected failure mode so a duplicate
      // mechanism name under a different mode is never sent. When it cannot be
      // resolved unambiguously, send null + text and let the backend upsert the
      // mechanism under (name, selected failure mode).
      payload.failure_mechanism_id = resolveMechanismId(rowState.mechByNameMode, mechName, modeId);
      payload.failure_mode_text = modeName;
      payload.failure_mechanism_text = mechName;
    }
    return payload;
  }

  // Shared key so the mechanism map build and the lookup can never drift.
  function mechKey(name, modeId) {
    return name + String.fromCharCode(0) + (modeId == null ? "" : modeId);
  }

  function resolveMechanismId(mechByNameMode, name, modeId) {
    if (!name) return null;
    const scopedKey = mechKey(name, modeId);
    if (mechByNameMode.has(scopedKey)) return mechByNameMode.get(scopedKey);
    // mechanisms saved without a parent mode live under the empty-mode bucket
    const unscopedKey = mechKey(name, null);
    if (mechByNameMode.has(unscopedKey)) return mechByNameMode.get(unscopedKey);
    return null;
  }

  function renderDispositionEditor(data) {
    const isPm = data.kind === "pm";
    const workspace = $("lda-workspace");
    workspace.innerHTML = "";

    // Failure-mode names are globally unique, so a name -> id map is safe.
    const modeMap = new Map(data.mode_options.map((o) => [o.failure_mode_name, o.failure_mode_id]));
    // Mechanism names can repeat across modes, so key by (name, parent mode id).
    const mechByNameMode = new Map(
      data.mechanism_options.map((o) => [mechKey(o.failure_mechanism_name, o.failure_mode_id), o.failure_mechanism_id])
    );

    const extraHeaders = isPm
      ? ["Disposition Notes", "Disposition Category", "Record Class", "PM Reset Decision",
         "Reset Target Failure Mode", "Reset Target Failure Mechanism", "Modeled Population",
         "Include in Weibull Candidate", "PM Reset Renewal Rationale / Evidence"]
      : ["Disposition Notes", "Disposition Category", "Record Class", "Failure Mode",
         "Failure Mechanism", "Modeled Population", "Include in Weibull Candidate"];

    const startRow = data.rows.length ? data.offset + 1 : 0;
    const endRow = data.offset + data.rows.length;
    let metaText =
      data.scope === "new"
        ? `Showing rows ${startRow}-${endRow} of ${data.displayed_count} rows with a blank ` +
          `${isPm ? "reset target failure mode or mechanism" : "failure mode or failure mechanism"} ` +
          `(${data.all_count} eligible rows total).`
        : `Showing rows ${startRow}-${endRow} of ${data.displayed_count} eligible rows for this asset.`;
    if (data.search) {
      metaText += ` Filtered by search "${data.search}".`;
    }

    const rowStates = [];
    const table = el("table", { class: "lda-table" });
    const thead = el("thead", {}, [
      el("tr", {}, data.display_columns.concat(extraHeaders).map((h) =>
        el("th", { class: h === "name" ? "lda-col-name" : null, text: h })
      )),
    ]);
    const tbody = el("tbody");

    data.rows.forEach((row, index) => {
      const tr = el("tr");
      data.display_columns.forEach((key) => {
        const cls = key === "name" ? "lda-readonly lda-col-name" : "lda-readonly";
        tr.appendChild(el("td", { class: cls, text: row[key] == null ? "" : String(row[key]) }));
      });

      const notes = el("textarea", { class: "lda-textarea" });
      notes.value = row.disposition_notes || row.disposition_text || "";
      tr.appendChild(el("td", {}, [notes]));

      const category = buildSelect(data.categories, row.disposition_category || "UNKNOWN");
      tr.appendChild(el("td", {}, [category]));

      const recordClass = buildSelect(data.record_classes, row.effective_record_class || (isPm ? "PM" : "CORRECTIVE_WO"));
      tr.appendChild(el("td", {}, [recordClass]));

      let decision = null;
      let mode;
      let mech;
      let rationale = null;
      if (isPm) {
        decision = buildSelect(data.pm_reset_decisions, row.pm_reset_inclusion_decision || "NEEDS_REVIEW");
        tr.appendChild(el("td", {}, [decision]));
        // PMs may only reference existing modes/mechanisms, so the searchable
        // dropdown is restricted to known options (allowFreeText: false).
        mode = buildTaxonomyCombobox(data.mode_options, "failure_mode_id", "failure_mode_name", row.reset_target_failure_mode_id, { allowFreeText: false });
        tr.appendChild(el("td", {}, mode.nodes));
        mech = buildTaxonomyCombobox(data.mechanism_options, "failure_mechanism_id", "failure_mechanism_name", row.reset_target_failure_mechanism_id, {
          allowFreeText: false,
          contextIdKey: "failure_mode_id",
          getContextId: () => mode.getSelectedId(),
        });
        tr.appendChild(el("td", {}, mech.nodes));
      } else {
        // WO failure mode/mechanism allow typing a new value as well as picking
        // an existing one (allowFreeText: true); ids resolve by name on save.
        mode = buildTaxonomyCombobox(data.mode_options, "failure_mode_id", "failure_mode_name", row.failure_mode_id, { allowFreeText: true });
        tr.appendChild(el("td", {}, mode.nodes));
        mech = buildTaxonomyCombobox(data.mechanism_options, "failure_mechanism_id", "failure_mechanism_name", row.failure_mechanism_id, { allowFreeText: true });
        tr.appendChild(el("td", {}, mech.nodes));
      }

      tr.appendChild(
        el("td", { class: "lda-readonly", text: row.modeled_population_name || "Auto-create from selected asset + mode/mechanism on save" })
      );

      const currentCategory = row.disposition_category || "UNKNOWN";
      const defaultInclude =
        Boolean(row.include_in_weibull_candidate) ||
        (!isPm && currentCategory === "INCLUDED_FAILURE") ||
        (isPm && currentCategory === "INCLUDED_PM_RESET_EVENT" && row.pm_reset_inclusion_decision === "APPROVED_RESET");
      const include = el("input", { type: "checkbox" });
      include.checked = defaultInclude;
      tr.appendChild(el("td", { class: "lda-check" }, [include]));

      if (isPm) {
        rationale = el("textarea", { class: "lda-textarea" });
        rationale.value = row.pm_reset_renewal_rationale || "";
        tr.appendChild(el("td", {}, [rationale]));
      }

      const rowState = {
        mapped_record_id: Number(row.mapped_record_id),
        notes,
        category,
        recordClass,
        include,
        decision,
        rationale,
        mode,
        mech,
        modeOptions: modeMap,
        mechByNameMode,
        tr,
      };
      rowState.initial = JSON.stringify(dispositionPayloadFromRow(rowState, data.kind));
      rowStates.push(rowState);
      tbody.appendChild(tr);
    });

    if (!data.rows.length) {
      const colCount = data.display_columns.length + extraHeaders.length;
      const emptyText = data.search
        ? `No rows match "${data.search}". Clear or change the search to see more.`
        : "No eligible rows for this selection.";
      tbody.appendChild(
        el("tr", {}, [el("td", { class: "lda-readonly lda-empty-row", colspan: String(colCount), text: emptyText })])
      );
    }

    table.appendChild(thead);
    table.appendChild(tbody);
    enableTableColumnTools(table);

    const changed = () => rowStates.filter((rs) => JSON.stringify(dispositionPayloadFromRow(rs, data.kind)) !== rs.initial);
    // Exposed so the Rows/Scope selectors on the dedicated disposition page can
    // confirm before discarding unsaved edits, the same way page navigation does.
    state.dispositionChangedFn = changed;

    const checkAllButton = el("button", {
      class: "btn-secondary",
      text: "Check all Include in Weibull Candidate",
      onclick: () => {
        // Respect an active column filter: only check rows the user can currently
        // see, so filtering to a subset and clicking this never silently flips
        // (and later saves) the Weibull inclusion of hidden rows.
        rowStates.forEach((rs) => {
          if (rs.tr.style.display !== "none") rs.include.checked = true;
        });
      },
    });

    const prev = el("button", {
      class: "btn-secondary",
      text: "← Previous Page",
      disabled: data.page_index <= 0,
      onclick: () => maybeChangePage(data, changed, data.page_index - 1),
    });
    const next = el("button", {
      class: "btn-secondary",
      text: "Next Page →",
      disabled: endRow >= data.displayed_count,
      onclick: () => maybeChangePage(data, changed, data.page_index + 1),
    });
    const pager = el("div", { class: "lda-pager" }, [
      prev,
      el("span", { class: "lda-page-status", text: `Page ${data.page_index + 1} of ${data.max_page_index + 1}` }),
      next,
    ]);

    const download = el("button", {
      class: "btn-secondary",
      text: "Download Excel",
      onclick: () => downloadExcel(data.kind),
    });
    const upload = el("button", {
      class: "btn-secondary",
      text: "Disposition via Excel",
      onclick: () => uploadExcel(data.kind, data.scope),
    });
    const save = el("button", {
      class: "btn-primary",
      text: "Save Dispositions",
      onclick: () => saveDispositions(data.kind, changed),
    });
    const card = el("section", { class: "glass-card lda-card" }, [
      el("h2", { text: isPm ? "Disposition PMs" : "Disposition Work Orders" }),
      el("div", { class: "lda-disposition-meta" }, [
        el("p", { text: `Selected asset: ${data.asset_number}` }),
        el("p", { class: "lda-hint", text: metaText }),
        el("p", {
          class: "lda-hint",
          text: isPm
            ? "PMs cannot create new reset targets and must point at existing WO modes/mechanisms. PMs only become Weibull-usable with INCLUDED_PM_RESET_EVENT and APPROVED_RESET."
            : "Assign defensible failure modes/mechanisms and save asset-specific dropdown options. Corrective WOs only become Weibull-usable with INCLUDED_FAILURE.",
        }),
      ]),
      el("div", { class: "lda-row-actions", style: "justify-content:flex-start" }, [checkAllButton]),
      el("p", { class: "lda-hint", text: "Use the ▾ menu in any column header to sort or filter the rows shown on this page." }),
      el("div", { class: "lda-table-scroll" }, [table]),
      pager,
      el("div", { class: "lda-row-actions" }, [download, upload, save]),
    ]);
    $("lda-workspace").appendChild(card);
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function buildSelect(options, current) {
    const select = el("select", { class: "lda-select" });
    options.forEach((opt) => {
      const option = el("option", { value: opt, text: opt });
      if (opt === current) option.selected = true;
      select.appendChild(option);
    });
    if (!options.includes(current)) {
      const fallback = el("option", { value: current, text: current });
      fallback.selected = true;
      select.insertBefore(fallback, select.firstChild);
    }
    return select;
  }

  // Searchable failure mode / failure mechanism dropdown for the disposition
  // table. Renders a text search input with a formatted option list that appears
  // directly below it — matching the look of the Disposition Category / Record
  // Class selects. The list is portaled to <body> (position:fixed, positioned
  // from the input's bounding box) so the scrollable table container never clips
  // it.
  //   allowFreeText: true   → WO: typing a brand-new value is allowed; the id is
  //                           resolved from the typed name on save.
  //   allowFreeText: false  → PM: selection is restricted to existing options;
  //                           the chosen option's real id is tracked for save.
  function buildTaxonomyCombobox(options, idKey, nameKey, currentId, opts) {
    const allowFreeText = Boolean(opts && opts.allowFreeText);
    const contextIdKey = opts && opts.contextIdKey;
    const getContextId = opts && opts.getContextId;
    const wrap = el("div", { class: "lda-combobox lda-cell-combobox" });
    const input = el("input", {
      class: "lda-input",
      autocomplete: "off",
      role: "combobox",
      "aria-autocomplete": "list",
      "aria-expanded": "false",
      placeholder: allowFreeText ? "Select existing or type new…" : "Search…",
    });
    const list = el("ul", { class: "lda-combobox-list lda-portal-list", role: "listbox", hidden: true });
    wrap.appendChild(input);

    let selectedId = null;
    let isOpen = false;
    let activeIndex = -1;
    let filtered = [];

    if (currentId != null) {
      const match = options.find((opt) => Number(opt[idKey]) === Number(currentId));
      if (match) {
        input.value = match[nameKey];
        selectedId = Number(match[idKey]);
      }
    }

    function currentContextId() {
      return typeof getContextId === "function" ? getContextId() : null;
    }

    function optionMatchesContext(opt, contextId) {
      if (!contextIdKey || contextId == null) return true;
      return Number(opt[contextIdKey]) === Number(contextId);
    }

    function contextOptions() {
      const contextId = currentContextId();
      return options.filter((opt) => optionMatchesContext(opt, contextId));
    }

    function matchesFor(query) {
      const q = (query || "").trim().toLowerCase();
      const candidates = contextOptions();
      if (!q) return candidates.slice();
      return candidates.filter((opt) => String(opt[nameKey]).toLowerCase().includes(q));
    }

    function positionList() {
      const rect = input.getBoundingClientRect();
      list.style.top = `${rect.bottom + 4}px`;
      list.style.left = `${rect.left}px`;
      list.style.width = `${rect.width}px`;
    }

    function renderList() {
      list.innerHTML = "";
      filtered = matchesFor(input.value).slice(0, 50);
      if (!filtered.length) {
        list.appendChild(
          el("li", {
            class: "lda-combobox-empty",
            text: allowFreeText ? "No matches — keep typing to add a new value." : "No matching options.",
          })
        );
        return;
      }
      filtered.forEach((opt, index) => {
        list.appendChild(
          el(
            "li",
            {
              class: "lda-combobox-option" + (index === activeIndex ? " is-active" : ""),
              role: "option",
              // mousedown (not click) so the choice commits before the input's
              // blur fires; preventDefault keeps focus on the input.
              onmousedown: (event) => {
                event.preventDefault();
                choose(opt);
              },
            },
            [el("span", { class: "lda-combobox-option-label", text: opt[nameKey] })]
          )
        );
      });
    }

    // Keep the portaled list pinned beneath its input when the page, the inner
    // table container, or the window scrolls/resizes. A scroll *inside* the list
    // itself (browsing the options) must not move or close it, so it is ignored.
    // The list is only dismissed once the input has been scrolled out of view,
    // so it can never float detached over unrelated content.
    // The visible box the input lives in: the scrollable table container
    // intersected with the window viewport. Because the menu is portaled to
    // <body>, a row can be scrolled above/left of the table's visible area while
    // its rect is still inside the page viewport — so the menu must be dismissed
    // against this box, not the viewport alone. Falls back to the viewport when
    // there is no scroll container (defensive; these always render inside one).
    function visibleClip() {
      const view = { top: 0, left: 0, right: window.innerWidth, bottom: window.innerHeight };
      const scroller = input.closest(".lda-table-scroll");
      if (!scroller) return view;
      const r = scroller.getBoundingClientRect();
      return {
        top: Math.max(view.top, r.top),
        left: Math.max(view.left, r.left),
        right: Math.min(view.right, r.right),
        bottom: Math.min(view.bottom, r.bottom),
      };
    }

    function reflowList(event) {
      if (!isOpen) return;
      if (event && event.type === "scroll" && event.target && list.contains(event.target)) return;
      const rect = input.getBoundingClientRect();
      const clip = visibleClip();
      const clipped =
        rect.bottom <= clip.top || rect.top >= clip.bottom ||
        rect.right <= clip.left || rect.left >= clip.right;
      if (clipped) {
        closeList();
        return;
      }
      positionList();
    }

    function openList() {
      if (!isOpen) {
        document.body.appendChild(list);
        isOpen = true;
        // Capture so scrolls on the inner table container are caught too; the
        // handler re-pins the list to its input rather than dismissing it.
        window.addEventListener("scroll", reflowList, true);
        window.addEventListener("resize", reflowList, true);
      }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      positionList();
      renderList();
    }

    function closeList() {
      if (!isOpen) return;
      list.hidden = true;
      if (list.parentNode) list.parentNode.removeChild(list);
      isOpen = false;
      activeIndex = -1;
      input.setAttribute("aria-expanded", "false");
      window.removeEventListener("scroll", reflowList, true);
      window.removeEventListener("resize", reflowList, true);
    }

    function choose(opt) {
      input.value = opt[nameKey];
      selectedId = Number(opt[idKey]);
      closeList();
    }

    // Resolve the current input text to an option id by exact (case-insensitive)
    // name match, independent of the blur timer. Returns null when the text does
    // not exactly match an option. Lets getSelectedId() report a valid typed
    // entry synchronously, so a Save click that beats the 120 ms blur timer still
    // sends the right id instead of null.
    function resolveIdFromText() {
      const typed = input.value.trim().toLowerCase();
      if (!typed) return null;
      const exactMatches = contextOptions().filter((opt) => String(opt[nameKey]).toLowerCase() === typed);
      if (selectedId != null) {
        const selected = exactMatches.find((opt) => Number(opt[idKey]) === Number(selectedId));
        if (selected) return Number(selected[idKey]);
      }
      return exactMatches.length === 1 ? Number(exactMatches[0][idKey]) : null;
    }

    input.addEventListener("focus", openList);
    input.addEventListener("input", () => {
      activeIndex = -1;
      // Typing detaches any previously chosen option id; WO re-resolves by name
      // on save, PM requires an explicit pick (or exact-name match on blur).
      selectedId = null;
      if (!isOpen) openList();
      else {
        positionList();
        renderList();
      }
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (!isOpen) openList();
        if (filtered.length) {
          activeIndex = activeIndex + 1 >= filtered.length ? 0 : activeIndex + 1;
          renderList();
        }
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        if (!isOpen) openList();
        if (filtered.length) {
          activeIndex = activeIndex <= 0 ? filtered.length - 1 : activeIndex - 1;
          renderList();
        }
      } else if (event.key === "Enter") {
        if (isOpen && activeIndex >= 0 && activeIndex < filtered.length) {
          event.preventDefault();
          choose(filtered[activeIndex]);
        }
      } else if (event.key === "Escape") {
        closeList();
      }
    });
    input.addEventListener("blur", () => {
      // Delay so an option's mousedown selection runs before the list closes.
      setTimeout(() => {
        closeList();
        if (allowFreeText) return;
        // Restricted dropdown: keep the text in sync with the resolved id, adopt
        // an exact-name match, or clear an unmatched entry.
        const exactId = resolveIdFromText();
        if (exactId != null) {
          selectedId = exactId;
          const match = options.find((opt) => Number(opt[idKey]) === exactId);
          input.value = match[nameKey];
        } else if (selectedId != null) {
          const match = options.find((opt) => Number(opt[idKey]) === Number(selectedId));
          input.value = match ? match[nameKey] : "";
          if (!match) selectedId = null;
        } else {
          input.value = "";
        }
      }, 120);
    });

    return {
      nodes: [wrap],
      input,
      getValue: () => input.value.trim(),
      // Fall back to a synchronous exact-name resolution so a typed-but-not-yet-
      // committed valid entry isn't read as null when Save races the blur timer.
      getSelectedId: () => {
        if (selectedId != null) {
          const selected = options.find((opt) => Number(opt[idKey]) === Number(selectedId));
          if (selected && optionMatchesContext(selected, currentContextId())) return Number(selectedId);
        }
        return resolveIdFromText();
      },
    };
  }

  // Shared by page navigation and the Rows/Scope selectors: confirms before any
  // of them discard unsaved disposition edits.
  async function confirmDiscardUnsavedChanges(changedFn) {
    if (!changedFn || !changedFn().length) return true;
    return await openModal({
      title: "Unsaved disposition changes",
      bodyNodes: [el("p", { text: "This page has unsaved disposition changes. Continue without saving them?" })],
      actions: [
        { label: "Stay on page", primary: false, value: () => false },
        { label: "Discard and continue", primary: true, value: () => true },
      ],
    });
  }

  async function maybeChangePage(data, changedFn, targetPage) {
    if (!(await confirmDiscardUnsavedChanges(changedFn))) return;
    loadDispositionPage(data.kind, data.scope, targetPage);
  }

  async function saveDispositions(kind, changedFn) {
    const changed = changedFn();
    if (!changed.length) {
      showToast("No disposition rows changed, so nothing needed to be saved.", "info");
      return;
    }
    const payloads = changed.map((rs) => dispositionPayloadFromRow(rs, kind));
    beginLoading("Saving dispositions…");
    try {
      const result = await postJson(`${API}/dispositions/save`, { dispositions: payloads });
      changed.forEach((rs) => (rs.initial = JSON.stringify(dispositionPayloadFromRow(rs, kind))));
      showToast(`Saved ${result.saved} changed REL disposition row(s) to event_disposition.`, "success");
      refreshSummary();
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      endLoading();
    }
  }

  function downloadExcel(kind) {
    const url = `${API}/dispositions/excel?asset=${encodeURIComponent(state.selectedAsset)}&kind=${kind}`;
    window.location.href = url;
  }

  function uploadExcel(kind, scope) {
    const fileInput = el("input", { type: "file", accept: ".xlsx" });
    fileInput.style.display = "none";
    document.body.appendChild(fileInput);
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.remove();
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      beginLoading("Importing disposition Excel…");
      try {
        const url = `${API}/dispositions/excel?asset=${encodeURIComponent(state.selectedAsset)}&kind=${kind}`;
        const result = await requestJson(url, { method: "POST", body: form });
        showBanner(`Imported ${result.imported} disposition row(s) from Excel.`, "success");
        refreshSummary();
        loadDispositionPage(kind, scope || "all", 0);
      } catch (err) {
        showBanner(err.message, "error");
      } finally {
        endLoading();
      }
    });
    fileInput.click();
  }

  // ---- downtime driver analysis ---------------------------------------------
  // The selected failure mode/mechanism (Pareto click or Perform Analysis picker)
  // drives a server-side downtime breakdown for the same included-failure dataset
  // as the Pareto: summary statistics, a continuous monthly downtime trend, a
  // downtime distribution, downtime by asset/location, and the top downtime events.
  function downtimeSelectionLabel(row) {
    const modeName = row.failure_mode_name;
    const mechName = row.failure_mechanism_name;
    return mechName
      ? modeName
        ? `${modeName} / ${mechName}`
        : mechName
      : modeName || "the selected failure mechanism";
  }

  function selectDowntimeMechanism(row) {
    if (row == null || row.failure_mode_id == null) {
      showBanner("The selected Pareto bar does not have a complete failure mode/mechanism selection.", "error");
      return;
    }
    setActiveMechanism(row);
    state.downtimeSelection = {
      failure_mode_id: row.failure_mode_id,
      failure_mechanism_id: row.failure_mechanism_id != null ? row.failure_mechanism_id : null,
      label: downtimeSelectionLabel(row),
    };
    state.downtimeData = null;
    // Clear the previous mechanism's cards/charts/table immediately so a slow or
    // failed request can't leave stale results visible under the new selection.
    renderDowntime();
    loadDowntime();
  }

  async function loadDowntime() {
    if (state.pageMode === "disposition") return;
    if (!state.selectedAsset || !state.downtimeSelection) {
      renderDowntime();
      return;
    }
    const asset = state.selectedAsset;
    const sel = state.downtimeSelection;
    const token = ++state.downtimeToken;
    // A response is stale when a newer request superseded it, the asset or analysis
    // type changed, or the selection was cleared/replaced (object identity also
    // covers switching away and back to the same asset before this resolved).
    const isStale = () =>
      token !== state.downtimeToken ||
      state.selectedAsset !== asset ||
      state.analysisType !== ANALYSIS_TYPES.DOWNTIME ||
      state.downtimeSelection !== sel;
    beginLoading("Analyzing downtime drivers…");
    try {
      const params = new URLSearchParams({ asset, failure_mode_id: sel.failure_mode_id });
      if (sel.failure_mechanism_id != null) params.set("failure_mechanism_id", sel.failure_mechanism_id);
      const data = await getJson(`${API}/downtime-drivers?${params.toString()}`);
      if (isStale()) return;
      state.downtimeData = data.downtime_drivers || null;
      renderDowntime();
    } catch (err) {
      if (!isStale()) {
        showBanner(err.message, "error");
        // Reflect the (now-cleared) data so a failed load doesn't leave another
        // mechanism's results on screen.
        renderDowntime();
      }
    } finally {
      endLoading();
    }
  }

  // Perform Analysis in downtime mode: pick the failure mode/mechanism from the
  // same filtered dataset as the Pareto, then analyze its downtime drivers. Reuses
  // the trend picker choices (mode-level "all mechanisms" plus each mechanism).
  async function performDowntimeSelection() {
    if (!state.trend && state.selectedAsset) {
      beginLoading("Loading failure mechanisms…");
      try {
        await refreshSummary();
      } finally {
        endLoading();
      }
    }
    const choices = trendPickerChoices();
    if (!choices.length) {
      showBanner(
        "No failure mechanisms with included failures are available to analyze yet. Disposition WO failures with a failure mechanism first.",
        "error"
      );
      return;
    }
    const options = el("div", { class: "lda-modal-options" });
    choices.forEach((choice, index) => {
      const radio = el("input", { type: "radio", name: "lda-downtime-group", value: String(index) });
      if (index === 0) radio.checked = true;
      options.appendChild(el("label", { class: "lda-modal-option" }, [radio, el("span", { text: choice.labelText })]));
    });
    const choice = await openModal({
      title: "Select failure mode or mechanism to analyze",
      bodyNodes: [
        el("p", { text: "Choose the failure mode or mechanism to analyze downtime drivers for:" }),
        options,
      ],
      actions: [
        { label: "Cancel", primary: false, value: () => null },
        {
          label: "Analyze downtime",
          primary: true,
          value: () => {
            const checked = options.querySelector("input[name='lda-downtime-group']:checked");
            return checked ? Number(checked.value) : null;
          },
        },
      ],
    });
    if (choice === null || choice === undefined) return;
    selectDowntimeMechanism(choices[choice].row);
  }

  // Shared empty-state text: "no selection", "loading", and "no records" so each
  // downtime panel can show the right prompt (mirrors pmEmptyText()).
  function downtimeEmptyText() {
    if (!state.downtimeSelection) {
      return "Select a failure mode or mechanism from the Pareto chart or analysis controls to analyze downtime drivers.";
    }
    if (!state.downtimeData) {
      return `Analyzing downtime drivers for ${state.downtimeSelection.label}…`;
    }
    return "No downtime records found for the selected failure mechanism within the current filters.";
  }

  function renderDowntime() {
    renderDowntimeCards();
    renderDowntimeTrendChart();
    renderDowntimeDistChart();
    renderDowntimeAssetChart();
    renderDowntimeEventsTable();
  }

  function renderDowntimeCards() {
    const grid = $("lda-downtime-cards");
    const message = $("lda-downtime-message");
    if (!grid) return;
    grid.innerHTML = "";
    const data = state.downtimeData;
    const hasRecords = Boolean(data && data.has_records);
    if (!state.downtimeSelection || !data || !hasRecords) {
      if (message) {
        message.hidden = false;
        message.textContent = downtimeEmptyText();
      }
      return;
    }
    if (message) {
      message.hidden = true;
      message.textContent = "";
    }
    const s = data.summary || {};
    const cards = [
      ["Total Downtime", `${fmt(s.total_downtime_hours)} h`],
      ["Work Order Count", String(s.work_order_count ?? 0)],
      ["Average Downtime", `${fmt(s.average_downtime_hours)} h`],
      ["Median Downtime", `${fmt(s.median_downtime_hours)} h`],
      ["Max Downtime Event", `${fmt(s.max_downtime_hours)} h`],
    ];
    cards.forEach(([label, value]) => {
      grid.appendChild(
        el("div", { class: "lda-metric" }, [
          el("span", { class: "lda-metric-value", text: value }),
          el("span", { class: "lda-metric-label", text: label }),
        ])
      );
    });
  }

  function renderDowntimeTrendChart() {
    const canvas = $("lda-downtime-trend-chart");
    const hint = $("lda-downtime-trend-hint");
    if (!canvas) return;
    const data = state.downtimeData;
    if (!state.downtimeSelection || !data || !data.has_records) {
      if (hint) {
        hint.hidden = false;
        hint.textContent = downtimeEmptyText();
      }
      canvas.hidden = true;
      return;
    }
    const months = data.months || [];
    if (!months.length) {
      // There are work orders, but none carry a usable date to bucket by month.
      if (hint) {
        hint.hidden = false;
        hint.textContent = `No dated work orders for ${state.downtimeSelection.label} to plot a monthly trend.`;
      }
      canvas.hidden = true;
      return;
    }
    if (hint) {
      hint.hidden = false;
      hint.textContent = `Total monthly downtime hours for ${state.downtimeSelection.label}. Zero-downtime months are included so the trend never skips a month.`;
    }
    canvas.hidden = false;
    drawDowntimeLineChart(canvas, months, data.monthly_downtime_hours || [], "Total downtime hours");
  }

  function renderDowntimeDistChart() {
    const canvas = $("lda-downtime-dist-chart");
    const hint = $("lda-downtime-dist-hint");
    if (!canvas) return;
    const data = state.downtimeData;
    if (!state.downtimeSelection || !data || !data.has_records) {
      if (hint) {
        hint.hidden = false;
        hint.textContent = downtimeEmptyText();
      }
      canvas.hidden = true;
      return;
    }
    if (hint) {
      hint.hidden = false;
      hint.textContent = "Work order count by downtime range — shows whether downtime comes from many short events or a few long ones.";
    }
    canvas.hidden = false;
    const dist = data.distribution || [];
    drawBarChart(
      canvas,
      dist.map((b) => b.label),
      dist.map((b) => b.count),
      {
        yLabel: "Work order count",
        xLabel: "Downtime range",
        tickFormat: (v) => String(Math.round(v)),
        valueLabel: (v) => String(Math.round(v)),
      }
    );
  }

  function renderDowntimeAssetChart() {
    const canvas = $("lda-downtime-asset-chart");
    const hint = $("lda-downtime-asset-hint");
    if (!canvas) return;
    const data = state.downtimeData;
    if (!state.downtimeSelection || !data || !data.has_records) {
      if (hint) {
        hint.hidden = false;
        hint.textContent = downtimeEmptyText();
      }
      canvas.hidden = true;
      return;
    }
    const all = data.by_asset || [];
    const MAX_BARS = 12;
    const rows = all.slice(0, MAX_BARS);
    if (hint) {
      hint.hidden = false;
      hint.textContent =
        all.length > rows.length
          ? `Total downtime hours grouped by asset/location, highest first (top ${rows.length} of ${all.length}).`
          : "Total downtime hours grouped by asset (or location when no asset is recorded), highest first.";
    }
    canvas.hidden = false;
    drawBarChart(
      canvas,
      rows.map((r) => r.label),
      rows.map((r) => r.downtime_hours),
      {
        yLabel: "Total downtime hours",
        tickFormat: compactHours,
        valueLabel: (v) => fmt(v),
        rotateLabels: true,
      }
    );
  }

  function renderDowntimeEventsTable() {
    const wrap = $("lda-downtime-events-wrap");
    const hint = $("lda-downtime-events-hint");
    if (!wrap) return;
    wrap.innerHTML = "";
    const data = state.downtimeData;
    const hasRecords = Boolean(data && data.has_records);
    if (hint) {
      hint.textContent =
        !state.downtimeSelection || !hasRecords
          ? downtimeEmptyText()
          : "The highest-downtime work orders for the selected failure mechanism, sorted by downtime (top 10).";
    }
    const headers = ["Date", "Asset", "Location", "Failure Mechanism", "Downtime (h)", "Operator", "WO #", "Description"];
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { text: h })))]));
    const tbody = el("tbody");
    const events = (hasRecords && data.top_events) || [];
    if (!events.length) {
      tbody.appendChild(
        el("tr", {}, [
          el("td", {
            class: "lda-readonly lda-empty-row",
            colspan: String(headers.length),
            text: !state.downtimeSelection || !hasRecords ? downtimeEmptyText() : "No downtime events to list.",
          }),
        ])
      );
    } else {
      events.forEach((ev) => {
        const description =
          ev.requestor_description || ev.task_name || ev.request_title || ev.completion_notes || "";
        tbody.appendChild(
          el("tr", {}, [
            el("td", { text: ev.wo_date || "—" }),
            el("td", { text: ev.asset || "—" }),
            el("td", { text: ev.location || "—" }),
            el("td", { text: ev.failure_mechanism_name || "—" }),
            el("td", { text: fmt(ev.downtime_hours) }),
            el("td", { text: ev.operator || "—" }),
            el("td", { text: ev.task_id != null ? String(ev.task_id) : "—" }),
            el("td", { class: "lda-wo-text", text: description }),
          ])
        );
      });
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  // Compact numeric label for downtime-hour axes/values (e.g. 12.3k), matching the
  // Pareto's k-formatting so large totals stay narrow.
  function compactHours(value) {
    const n = Number(value) || 0;
    if (Math.abs(n) >= 1000) return Math.round(n / 100) / 10 + "k";
    if (Math.abs(n) >= 100) return String(Math.round(n));
    if (n === 0) return "0";
    return Number(n.toPrecision(3)).toString();
  }

  // Monthly downtime line chart: continuous (zero-filled) month axis so the trend
  // line never skips a missing month, with a numeric (hours) y-axis.
  function drawDowntimeLineChart(canvas, months, values, yLabel) {
    canvas.onclick = null;
    const { ctx, width: W, height: H } = setupCanvas(canvas, 320);
    ctx.clearRect(0, 0, W, H);
    if (!months.length) return;

    const left = 56;
    const right = W - 18;
    const top = 22;
    const bottom = H - 64;
    const plotH = bottom - top;
    const plotW = right - left;
    const maxVal = Math.max(...values, 1);
    const n = months.length;
    const xAt = (index) => (n === 1 ? left + plotW / 2 : left + (index / (n - 1)) * plotW);
    const yAt = (value) => bottom - (value / maxVal) * plotH;

    const tickCount = 5;
    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= tickCount; i += 1) {
      const frac = i / tickCount;
      const y = bottom - frac * plotH;
      ctx.strokeStyle = "#eef2f6";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.fillStyle = "#5e7082";
      ctx.textAlign = "right";
      ctx.fillText(compactHours(maxVal * frac), left - 7, y);
    }
    ctx.textBaseline = "alphabetic";

    ctx.strokeStyle = "#c4d2dd";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    const labelFont = n > 36 ? 8 : n > 24 ? 9 : 10;
    ctx.fillStyle = "#5e7082";
    ctx.font = `${labelFont}px Inter, sans-serif`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    months.forEach((key, index) => {
      ctx.save();
      ctx.translate(xAt(index), bottom + 10);
      ctx.rotate(-Math.PI / 5);
      ctx.fillText(monthLabel(key), 0, 0);
      ctx.restore();
    });
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";

    ctx.strokeStyle = "#3f5e77";
    ctx.lineWidth = 2;
    ctx.beginPath();
    values.forEach((value, index) => {
      const x = xAt(index);
      const y = yAt(value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.fillStyle = "#c2723b";
    values.forEach((value, index) => {
      const x = xAt(index);
      const y = yAt(value);
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    });

    ctx.fillStyle = "#2a3f50";
    ctx.font = "600 11.5px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Month", (left + right) / 2, H - 6);
    ctx.save();
    ctx.translate(13, (top + bottom) / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textBaseline = "middle";
    ctx.fillText(yLabel || "Total downtime hours", 0, 0);
    ctx.restore();
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  // Generic vertical bar chart used by the Downtime Distribution (binned counts)
  // and Downtime by Asset/Location (per-asset hours) panels. Short bin labels are
  // drawn horizontally; long asset labels are rotated (opts.rotateLabels).
  function drawBarChart(canvas, labels, values, options) {
    const opts = options || {};
    const yLabel = opts.yLabel || "";
    const tickFormat = opts.tickFormat || ((v) => String(Math.round(v)));
    const valueLabel = opts.valueLabel || tickFormat;
    const barColor = opts.barColor || "#3f5e77";
    canvas.onclick = null;
    const { ctx, width: W, height: H } = setupCanvas(canvas, 320);
    ctx.clearRect(0, 0, W, H);
    if (!labels.length) return;

    const left = 56;
    const right = W - 18;
    const top = 24;
    const bottom = H - (opts.rotateLabels ? 86 : 56);
    const plotH = bottom - top;
    const slot = (right - left) / labels.length;
    const maxVal = Math.max(...values.map((v) => Number(v) || 0), 1);
    const barGap = Math.min(20, slot * 0.32);
    const barW = Math.max(6, slot - barGap);

    const tickCount = 5;
    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= tickCount; i += 1) {
      const frac = i / tickCount;
      const y = bottom - frac * plotH;
      ctx.strokeStyle = "#eef2f6";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.fillStyle = "#5e7082";
      ctx.textAlign = "right";
      ctx.fillText(tickFormat(maxVal * frac), left - 7, y);
    }
    ctx.textBaseline = "alphabetic";

    ctx.strokeStyle = "#c4d2dd";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    labels.forEach((label, index) => {
      const value = Number(values[index]) || 0;
      const x = left + index * slot + (slot - barW) / 2;
      const barHeight = (value / maxVal) * plotH;
      const y = bottom - barHeight;
      ctx.fillStyle = barColor;
      ctx.fillRect(x, y, barW, barHeight);

      if (value > 0) {
        ctx.fillStyle = "#2a3f50";
        ctx.font = "10px Inter, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(valueLabel(value), x + barW / 2, y - 4);
      }

      ctx.fillStyle = "#5e7082";
      ctx.font = "10px Inter, sans-serif";
      if (opts.rotateLabels) {
        ctx.save();
        ctx.translate(x + barW / 2, bottom + 8);
        ctx.rotate(Math.PI / 5);
        ctx.textAlign = "left";
        ctx.fillText(String(label).slice(0, 22), 0, 0);
        ctx.restore();
      } else {
        ctx.textAlign = "center";
        ctx.fillText(String(label), x + barW / 2, bottom + 16);
      }
    });

    ctx.fillStyle = "#2a3f50";
    ctx.font = "600 11.5px Inter, sans-serif";
    ctx.textAlign = "center";
    if (opts.xLabel) ctx.fillText(opts.xLabel, (left + right) / 2, H - 6);
    ctx.save();
    ctx.translate(13, (top + bottom) / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textBaseline = "middle";
    ctx.fillText(yLabel, 0, 0);
    ctx.restore();
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  // ---- perform analysis (routed by Analysis Type) ---------------------------
  // The "Perform Analysis" button performs the action for the selected analysis
  // type: the Weibull group picker for Weibull, the failure-mechanism picker for
  // Failure Mode Trend, and a "coming soon" notice for the unimplemented types.
  async function performAnalysis() {
    if (!state.selectedAsset) return;
    if (state.analysisType === ANALYSIS_TYPES.TREND) return performTrendSelection();
    if (state.analysisType === ANALYSIS_TYPES.PM) return performPmSelection();
    if (state.analysisType === ANALYSIS_TYPES.DOWNTIME) return performDowntimeSelection();
    if (state.analysisType !== ANALYSIS_TYPES.WEIBULL) {
      showBanner(`${state.analysisType} is coming soon.`, "info");
      return;
    }
    beginLoading("Loading Weibull groups…");
    let groups;
    try {
      const data = await getJson(`${API}/weibull-groups?asset=${encodeURIComponent(state.selectedAsset)}`);
      groups = data.groups || [];
    } catch (err) {
      endLoading();
      showBanner(err.message, "error");
      return;
    }
    endLoading();
    if (!groups.length) {
      showBanner(
        "No failure modes or failure mechanisms are ready for Weibull analysis. Disposition failures and PM resets with failure-mode/mechanism selections first.",
        "error"
      );
      return;
    }
    const options = el("div", { class: "lda-modal-options" });
    groups.forEach((group, index) => {
      const labelText =
        `${group.grouping_level === "FAILURE_MECHANISM" ? "Failure mechanism" : "Failure mode"}: ` +
        `${group.label} (${group.failure_count} failures, ${group.reset_count} PM resets)`;
      const radio = el("input", { type: "radio", name: "lda-group", value: String(index) });
      if (index === 0) radio.checked = true;
      options.appendChild(el("label", { class: "lda-modal-option" }, [radio, el("span", { text: labelText })]));
    });
    const choice = await openModal({
      title: "Select failure group",
      bodyNodes: [el("p", { text: "Choose the failure mechanism or failure mode to analyze:" }), options],
      actions: [
        { label: "Cancel", primary: false, value: () => null },
        {
          label: "Run analysis",
          primary: true,
          value: () => {
            const checked = options.querySelector("input[name='lda-group']:checked");
            return checked ? Number(checked.value) : null;
          },
        },
      ],
    });
    if (choice === null || choice === undefined) return;
    setActiveMechanism(groups[choice]);
    runAnalysisForGroup(groups[choice]);
  }

  async function runAnalysisForGroup(group, message) {
    if (!state.selectedAsset) return;
    const asset = state.selectedAsset;
    // Capture the analysis type too: if the user switches away from Weibull while
    // this request is in flight, applyAnalysisTypeUI() clears the workspace, so a
    // late Weibull response must not repopulate it under the non-Weibull panel.
    const analysisType = state.analysisType;
    const isStale = () => state.selectedAsset !== asset || state.analysisType !== analysisType;
    beginLoading(message || "Running Weibull analysis…");
    try {
      const data = await postJson(`${API}/perform-analysis`, {
        asset,
        grouping_level: group.grouping_level,
        failure_mode_id: group.failure_mode_id,
        failure_mechanism_id: group.failure_mechanism_id,
      });
      if (isStale()) return; // asset or analysis type changed mid-request; drop the stale result
      state.latestResult = data.result;
      renderAnalysisResult(data.result);
      refreshSummary();
    } catch (err) {
      if (!isStale()) showBanner(err.message, "error");
    } finally {
      endLoading();
    }
  }

  function confidenceIntervalText(result) {
    const betaCi =
      result.beta_lower_ci != null && result.beta_upper_ci != null
        ? `${fmt(result.beta_lower_ci)} to ${fmt(result.beta_upper_ci)}`
        : "not available";
    const etaCi =
      result.eta_lower_ci != null && result.eta_upper_ci != null
        ? `${fmt(result.eta_lower_ci)} to ${fmt(result.eta_upper_ci)} hours`
        : "not available";
    const mttf = result.mean_time_to_failure != null ? `${fmt(result.mean_time_to_failure)} hours` : "not available";
    return `Approx. 95% CI — beta: ${betaCi}; eta: ${etaCi}; MTTF: ${mttf}.`;
  }

  function renderAnalysisResult(result) {
    clearWorkspace();
    const dataTable = buildWeibullDataTable(result);

    const betaInput = el("input", { class: "lda-input", type: "number", step: "0.2", min: "0.01", value: numericInputValue(result.beta_mle, 6) });
    const etaInput = el("input", { class: "lda-input", type: "number", step: "100", min: "0.01", value: numericInputValue(result.eta_mle, 6) });
    const reasonInput = el("input", { class: "lda-input", placeholder: "Adjustment reason based on empirical data points…" });

    const charts = el("div", { class: "lda-charts" });
    const chartApi = buildAnalysisCharts(charts, result, dataTable.highlight);

    function applyParameters() {
      const beta = Number(betaInput.value);
      const eta = Number(etaInput.value);
      if (beta > 0 && eta > 0) chartApi.update(beta, eta);
    }
    betaInput.addEventListener("input", applyParameters);
    etaInput.addEventListener("input", applyParameters);
    // Redraw the charts at the current beta/eta on window resize.
    state.analysisRedraw = applyParameters;

    const saveAdjusted = el("button", {
      class: "btn-primary",
      text: "Save Adjusted Parameters",
      onclick: () => saveAdjustedParameters(result.result_id, Number(betaInput.value), Number(etaInput.value), reasonInput.value),
    });

    const adjustRow = el("div", { class: "lda-adjust-row" }, [
      field("Adjusted beta (±0.2)", betaInput),
      field("Adjusted eta (±100 h)", etaInput),
      field("Adjustment reason", reasonInput),
      el("div", { class: "lda-field" }, [el("label", { html: "&nbsp;" }), saveAdjusted]),
    ]);

    const card = el("section", { class: "glass-card lda-card fade-in-up" }, [
      el("h2", { text: "Weibull Analysis Results" }),
      el("div", { class: "lda-result-headline" }, [
        el("strong", { text: result.analysis_label || "Selected failure group" }),
        el("span", { class: "lda-result-params", text: `MLE beta: ${fmt(result.beta_mle)}    MLE eta: ${fmt(result.eta_mle)} hours` }),
        el("span", { class: "lda-hint", text: confidenceIntervalText(result) }),
        el("span", {
          class: "lda-hint",
          text: `Observations: ${result.total_observation_count} total, ${result.failure_count} failures, ${result.censored_count} right-censored.`,
        }),
      ]),
      adjustRow,
      legend(),
      charts,
      el("p", {
        class: "lda-hint",
        text:
          "Green lines show the MLE fit; yellow lines show approximate 95% confidence-interval fits where available. " +
          "The red vertical line marks the current life — the elapsed time from the most recent valid event to the analysis cutoff. " +
          "The hazard and PDF panes intentionally show only the MLE curve. Hover any plotted point or the current-life line to see its task ID, life hours, start/end dates, request description, and completion notes; click it to jump to the source Weibull data row below.",
      }),
      panel("Results Interpretation Summary", buildInterpretationTable(result),
        "Recommendations are based on beta, eta, MTTF, and approximate 95% confidence intervals for the fitted Weibull parameters."),
      panel("Weibull Data Used for Graphs", dataTable.node,

        "Rows are the observations included in the Weibull fit. White points are completed failures; red points are right-censored observations. " +
        "Use the ▾ menu in any column header to sort or filter the rows."),
      buildReportBar(result, charts, chartApi, betaInput, etaInput),

    ]);
    $("lda-workspace").appendChild(card);
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Action bar at the bottom of the results: generates a formal, high-level
  // Weibull report (Word .docx) containing the charts and interpretation summary.
  function buildReportBar(result, charts, chartApi, betaInput, etaInput) {
    const button = el("button", {
      class: "btn-primary",
      type: "button",
      text: "Generate Weibull Report",
    });
    button.addEventListener("click", () => generateWeibullReport(result, charts, button, chartApi, betaInput, etaInput));
    return el("div", { class: "lda-report-bar" }, [
      button,
      el("p", {
        class: "lda-hint",
        text:
          "Creates a formal Word report (REL-WBL-RPT-<asset>-00x.docx) summarizing this Weibull result at a high level, " +
          "including the analysis graphs and the interpretation summary.",
      }),
    ]);
  }

  async function generateWeibullReport(result, chartsContainer, button, chartApi, betaInput, etaInput) {
    if (!state.selectedAsset) {
      showBanner("Select an Asset Number first.", "error");
      return;
    }
    // The report's parameter and interpretation tables come from the analyzed MLE
    // `result`, so the embedded graphs must show that same MLE fit — not any
    // unsaved on-screen beta/eta tweak. Redraw the charts at the MLE parameters
    // before capturing them, then restore whatever the user had on screen.
    if (chartApi && result && result.beta_mle > 0 && result.eta_mle > 0) {
      chartApi.update(Number(result.beta_mle), Number(result.eta_mle));
    }
    // Capture each rendered chart canvas as a PNG, pairing it with its heading so
    // the report figures match the analyzed MLE result.
    const charts = [];
    chartsContainer.querySelectorAll(".lda-chart-card").forEach((cardEl) => {
      const canvas = cardEl.querySelector("canvas");
      const heading = cardEl.querySelector("h4");
      if (!canvas) return;
      try {
        charts.push({ title: heading ? heading.textContent : "", image: canvas.toDataURL("image/png") });
      } catch (err) {
        /* tainted canvas should not happen for locally drawn charts; skip it */
      }
    });
    // Restore the on-screen charts to the user's current adjusted inputs.
    if (chartApi) {
      const beta = betaInput ? Number(betaInput.value) : NaN;
      const eta = etaInput ? Number(etaInput.value) : NaN;
      if (beta > 0 && eta > 0) chartApi.update(beta, eta);
    }
    if (button) button.disabled = true;
    beginLoading("Generating Weibull report…");
    try {
      const filename = await postDownload(
        `${API}/weibull-report`,
        // Send only the saved result id (plus chart images); the server reloads the
        // authoritative parameters and interpretation summary from the database.
        { asset: state.selectedAsset, result_id: result.result_id, charts },
        "weibull-report.docx"
      );
      showBanner(`Generated ${filename}.`, "success");
    } catch (err) {
      showBanner(err.message, "error");
    } finally {
      endLoading();
      if (button) button.disabled = false;
    }
  }

  function field(labelText, input) {
    return el("div", { class: "lda-field" }, [el("label", { text: labelText }), input]);
  }
  function panel(title, body, footer) {
    return el("div", { class: "lda-panel" }, [el("h3", { text: title }), body, footer ? el("p", { class: "lda-hint", text: footer }) : null]);
  }
  function legend() {
    return el("div", { class: "lda-legend" }, [
      el("span", {}, [el("span", { class: "lda-swatch", style: "border-top-color:#2f8f5b" }), document.createTextNode("MLE fit")]),
      el("span", {}, [el("span", { class: "lda-swatch", style: "border-top-color:#d6a700" }), document.createTextNode("95% CI fit")]),
      el("span", {}, [el("span", { class: "lda-dot", style: "background:#ffffff" }), document.createTextNode("Completed failure")]),
      el("span", {}, [el("span", { class: "lda-dot", style: "background:#c0392b;border-color:#7d2b2b" }), document.createTextNode("Right-censored")]),
      el("span", {}, [el("span", { class: "lda-vline" }), document.createTextNode("Current life")]),
    ]);
  }

  function buildInterpretationTable(result) {
    const rows = result.interpretation_summary || [];
    const table = el("table", { class: "lda-interpretation" });
    table.appendChild(el("thead", {}, [el("tr", {}, ["Metric", "Value", "Interpretation / Recommended Action"].map((h) => el("th", { text: h })))]));
    const tbody = el("tbody");
    if (!rows.length) {
      tbody.appendChild(el("tr", {}, [el("td", { colspan: "3", text: "No interpretation summary is available for this Weibull result." })]));
    }
    rows.forEach((row) => {
      tbody.appendChild(
        el("tr", {}, [
          el("td", { text: row.metric || "—" }),
          buildInterpretationValueCell(row, result),
          el("td", { text: row.recommendation || "—" }),
        ])
      );
    });
    table.appendChild(tbody);
    return table;
  }

  // Value cell for one interpretation-summary row. The MTTF row gets an extra
  // calendar-time conversion (months/days) plus an editable operating-schedule
  // input, so users can see the mean life in real terms for their run schedule
  // (e.g. 20 h/day). Every other metric renders as a plain value cell.
  function buildInterpretationValueCell(row, result) {
    const isMttf = String(row.metric || "").trim().toUpperCase() === "MTTF";
    const mttfHours = result ? Number(result.mean_time_to_failure) : NaN;
    if (!isMttf || !isFinite(mttfHours) || mttfHours <= 0) {
      return el("td", { text: row.value || "—" });
    }

    const durationLine = el("span", {
      class: "lda-mttf-duration",
      text: mttfDurationText(mttfHours, state.operatingHoursPerDay),
    });
    const hoursInput = el("input", {
      class: "lda-input lda-mttf-hours",
      type: "number",
      min: "0.1",
      max: "24",
      step: "0.5",
      value: numericInputValue(state.operatingHoursPerDay, 4),
      "aria-label": "Operating hours per day",
    });
    hoursInput.addEventListener("input", () => {
      // Browsers don't clamp typed values to the input's max, so validate the upper
      // bound here too: more than 24 h/day is physically impossible and would report a
      // calendar duration shorter than continuous running.
      const hpd = Number(hoursInput.value);
      if (isFinite(hpd) && hpd > 0 && hpd <= 24) {
        state.operatingHoursPerDay = hpd;
        durationLine.textContent = mttfDurationText(mttfHours, hpd);
      } else {
        durationLine.textContent = "Enter an operating schedule between 0 and 24 h/day to estimate calendar time.";
      }
    });

    return el("td", { class: "lda-mttf-cell" }, [
      el("span", { class: "lda-mttf-hours-value", text: row.value || `${fmt(mttfHours)} hours` }),
      durationLine,
      el("div", { class: "lda-mttf-schedule" }, [
        el("label", { text: "Operating schedule:" }),
        hoursInput,
        el("span", { text: "h/day" }),
      ]),
    ]);
  }

  function buildWeibullDataTable(result) {
    // Each column knows how to render its header and pull its value from an
    // observation, so the header row and body cells can never drift apart. The
    // Task ID / Work Title / Request Description / Completion Notes columns come
    // from the source CMMS work order that closed the life interval (joined in
    // perform_weibull_analysis); they are blank for trailing current-life rows.
    const columns = [
      { label: "#", get: (obs) => String(obs.ordered_index ?? "") },
      { label: "Observation ID", get: (obs) => String(obs.weibull_observation_id ?? "") },
      { label: "Task ID", get: (obs) => (obs.source_task_id != null ? String(obs.source_task_id) : "") },
      { label: "Work Title", cls: "lda-data-text", get: (obs) => obs.source_work_title || "" },
      { label: "Type", get: (obs) => obs.observation_type || "" },
      { label: "Life Hours", get: (obs) => fmtFixed(obs.life_hours_for_weibull) },
      { label: "Failure", get: (obs) => (Number(obs.failure_indicator) ? "Yes" : "No") },
      { label: "Right Censored", get: (obs) => (Number(obs.is_right_censored) ? "Yes" : "No") },
      { label: "Start Datetime", get: (obs) => obs.start_datetime || "" },
      { label: "End/Cutoff Datetime", get: (obs) => obs.end_datetime || obs.analysis_cutoff_datetime || "" },
      { label: "Request Description", cls: "lda-data-text", get: (obs) => obs.source_request_description || "" },
      { label: "Completion Notes", cls: "lda-data-text", get: (obs) => obs.source_completion_notes || "" },
      { label: "Note", cls: "lda-data-text", get: (obs) => obs.weibull_life_note || "" },
    ];
    const table = el("table", { class: "lda-data" });
    table.appendChild(
      el("thead", {}, [el("tr", {}, columns.map((c) => el("th", { text: c.label, class: c.cls || null })))])
    );
    const tbody = el("tbody");
    const rowByObs = new Map();
    (result.observations || []).forEach((obs) => {
      const tr = el("tr", {}, columns.map((c) => el("td", { text: c.get(obs), class: c.cls || null })));
      rowByObs.set(Number(obs.weibull_observation_id), tr);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    const tableTools = enableTableColumnTools(table);
    const node = el("div", { class: "lda-data-scroll" }, [table]);
    function highlight(observationId) {
      const tr = rowByObs.get(Number(observationId));
      if (!tr) return;
      // A click-to-jump from a chart point may target a row that an active column
      // filter is hiding (e.g. filtered to Failure=Yes, then clicking a censored
      // point). Clear the filters so the jump actually reveals the row.
      if (tr.style.display === "none" && tableTools) tableTools.clearFilters();
      tbody.querySelectorAll("tr.is-highlight").forEach((r) => r.classList.remove("is-highlight"));
      tr.classList.add("is-highlight");
      tr.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    return { node, highlight };
  }

  async function saveAdjustedParameters(resultId, beta, eta, reason) {
    if (!(beta > 0) || !(eta > 0)) {
      showBanner("Adjusted beta and eta must both be positive numbers.", "error");
      return;
    }
    beginLoading("Saving adjusted Weibull parameters…");
    try {
      await postJson(`${API}/parameter-adjustment`, { result_id: resultId, beta, eta, reason });
      showBanner("Adjusted beta and eta were saved without overwriting the MLE result.", "success");
    } catch (err) {
      showBanner(err.message, "error");
    } finally {
      endLoading();
    }
  }

  // ---- calculate all --------------------------------------------------------
  async function calculateAll() {
    if (!state.selectedAsset) return;
    const passwordInput = el("input", { class: "lda-input", type: "password", placeholder: "Calculation password" });
    const password = await openModal({
      title: "Password required",
      bodyNodes: [
        el("p", { text: "Enter the password to calculate MLE beta/eta for every available failure mode and mechanism on this asset:" }),
        passwordInput,
      ],
      actions: [
        { label: "Cancel", primary: false, value: () => null },
        { label: "Calculate", primary: true, value: () => passwordInput.value },
      ],
    });
    if (password === null || password === undefined) return;
    beginLoading("Calculating all Weibull MLE results…");
    try {
      const data = await postJson(`${API}/calculate-all`, { asset: state.selectedAsset, password });
      const summary = data.summary || {};
      state.latestResult = null;
      clearWorkspace();
      refreshSummary();
      let message = `Calculated and saved Weibull MLE beta/eta results for ${summary.completed || 0} of ${summary.total || 0} available failure mode/mechanism group(s).`;
      const errors = summary.errors || [];
      if ((summary.failed || 0) && errors.length) {
        message += " Groups needing review: " + errors.slice(0, 8).join("; ");
        if (errors.length > 8) message += ` …and ${errors.length - 8} more.`;
      }
      showBanner(message, (summary.failed || 0) ? "info" : "success");
    } catch (err) {
      showBanner(err.message, "error");
    } finally {
      endLoading();
    }
  }

  // ---- charts ---------------------------------------------------------------
  // Size the backing store to the on-screen width and return the CSS dimensions the
  // drawing code should use. Measuring the parent (not the canvas) avoids reading a
  // stale width back from a canvas whose `width` attribute was set on a previous
  // draw, and returning the width means callers never re-read `clientWidth` — which
  // is 0 before the canvas is attached to the DOM and was the cause of the charts
  // collapsing into a thin strip on the left.
  function setupCanvas(canvas, cssHeight) {
    const dpr = window.devicePixelRatio || 1;
    const parent = canvas.parentElement;
    const cssWidth = Math.round(
      (parent && parent.clientWidth) || canvas.clientWidth || 480
    );
    canvas.style.height = cssHeight + "px";
    canvas.width = Math.max(1, Math.round(cssWidth * dpr));
    canvas.height = Math.max(1, Math.round(cssHeight * dpr));
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, width: cssWidth, height: cssHeight };
  }

  function buildAnalysisCharts(container, result, highlight) {
    container.innerHTML = "";
    const curve = result.curve_points || [];
    const km = result.km_points || [];
    const maxTime = curve.length ? Math.max(...curve.map((p) => p.life_hours)) : 1;

    // failure observation lookup so plotted points can jump to the data table
    const failureObs = (result.observations || []).filter((o) => Number(o.failure_indicator));
    const censObs = (result.observations || []).filter((o) => Number(o.is_right_censored));

    // "Current life" markers: the trailing right-censored life that runs from the
    // last valid event to the analysis cutoff ("now"), so it has no end_datetime.
    // Like the desktop GUI, these are drawn as full-height red vertical lines (a
    // "now" marker) in every pane rather than as a point, so the ongoing life
    // since the most recent failure/reset is obvious on each graph.
    const isCurrentCensor = (o) =>
      Number(o.is_right_censored) === 1 &&
      String(o.observation_type || "").toUpperCase() === "RIGHT_CENSORED_LIFE" &&
      !o.end_datetime;
    const currentCensors = (result.observations || []).filter(isCurrentCensor);
    const historicalCensors = censObs.filter((o) => !isCurrentCensor(o));
    const currentLifeMarkers = currentCensors
      .map((obs) => [Number(obs.life_hours_for_weibull), obs])
      .filter(([t]) => isFinite(t) && t > 0);
    // Jump from a plotted point / current-life marker to its Weibull data row.
    const jumpToObs = (obs) => {
      if (highlight && obs) highlight(obs.weibull_observation_id);
    };
    // KM/scatter points carry only a time, so map one back to the closest failure
    // observation by life hours for the hover tooltip and click-to-row jump.
    const nearestFailureObs = (lifeHours) => {
      let best = null;
      let bestDelta = Infinity;
      failureObs.forEach((obs) => {
        const delta = Math.abs(Number(obs.life_hours_for_weibull) - Number(lifeHours));
        if (delta < bestDelta) {
          bestDelta = delta;
          best = obs;
        }
      });
      return best;
    };

    const panes = [
      { key: "prob", title: "Weibull Probability Plot", height: 300 },
      { key: "cdf", title: "CDF", height: 300 },
      { key: "pdf", title: "Probability Density (PDF)", height: 300 },
      { key: "hazard", title: "Hazard Rate", height: 300 },
    ];
    const canvases = {};
    panes.forEach((pane) => {
      const canvas = el("canvas", { class: "lda-canvas" });
      const card = el("div", { class: "lda-chart-card" }, [el("h4", { text: pane.title }), el("div", { class: "lda-chart-wrap" }, [canvas])]);
      container.appendChild(card);
      canvases[pane.key] = { canvas, height: pane.height };
    });

    function analyticCurves(beta, eta) {
      const pts = [];
      const steps = 80;
      for (let i = 1; i <= steps; i += 1) {
        const t = (maxTime * i) / steps;
        const z = Math.pow(t / eta, beta);
        const reliability = Math.exp(-z);
        pts.push({
          life_hours: t,
          cdf: 1 - reliability,
          pdf: (beta / eta) * Math.pow(t / eta, beta - 1) * reliability,
          hazard: (beta / eta) * Math.pow(t / eta, beta - 1),
        });
      }
      return pts;
    }

    function draw(beta, eta) {
      const curves = analyticCurves(beta, eta);
      const ciPairs = [];
      if (result.beta_lower_ci != null && result.eta_lower_ci != null) ciPairs.push([result.beta_lower_ci, result.eta_lower_ci]);
      if (result.beta_upper_ci != null && result.eta_upper_ci != null) ciPairs.push([result.beta_upper_ci, result.eta_upper_ci]);

      // 1) Weibull probability plot in (ln t, ln(-ln R)) space
      drawProbabilityPlot(canvases.prob, beta, eta, ciPairs, km, failureObs, highlight, currentCensors);
      // 2) CDF vs time. White points are completed failures (KM estimate); red
      //    points are historical right-censored observations placed on the fitted
      //    curve at their censoring time; the red vertical line marks current life.
      drawCurvePane(canvases.cdf, {
        mleLine: curves.map((p) => [p.life_hours, p.cdf]),
        ciLines: ciPairs.map(([b, e]) => analyticCurves(b, e).map((p) => [p.life_hours, p.cdf])),
        scatter: km.filter((p) => p.cdf_estimate != null).map((p) => [p.life_hours, p.cdf_estimate, p]),
        censored: historicalCensors.map((obs) => {
          const t = Number(obs.life_hours_for_weibull);
          return [t, 1 - Math.exp(-Math.pow(t / eta, beta)), obs];
        }),
        verticalMarkers: currentLifeMarkers,
        yMax: 1,
        xLabel: "Life hours",
        yLabel: "CDF",
        scatterPick: highlightFailureByTime,
        scatterObs: (p) => nearestFailureObs(p.life_hours),
        censoredPick: jumpToObs,
        markerPick: jumpToObs,
      });
      // 3) PDF (MLE only) + current-life marker
      drawCurvePane(canvases.pdf, {
        mleLine: curves.map((p) => [p.life_hours, p.pdf]),
        ciLines: [],
        scatter: [],
        verticalMarkers: currentLifeMarkers,
        xLabel: "Life hours",
        yLabel: "Density",
        markerPick: jumpToObs,
      });
      // 4) Hazard (MLE only) + current-life marker
      drawCurvePane(canvases.hazard, {
        mleLine: curves.map((p) => [p.life_hours, p.hazard]),
        ciLines: [],
        scatter: [],
        verticalMarkers: currentLifeMarkers,
        xLabel: "Life hours",
        yLabel: "Hazard",
        markerPick: jumpToObs,
      });
    }

    function highlightFailureByTime(point) {
      // map a KM/time point back to the nearest failure observation row
      const best = nearestFailureObs(point.life_hours);
      if (best && highlight) highlight(best.weibull_observation_id);
    }

    // Defer the first draw to the next frame. renderAnalysisResult appends this
    // chart container to the DOM synchronously after buildAnalysisCharts returns,
    // so by the time this callback runs the canvases are attached and report their
    // real on-screen width instead of 0 (which collapsed the plots into a strip).
    requestAnimationFrame(() => draw(result.beta_mle, result.eta_mle));
    return { update: draw };
  }

  function drawProbabilityPlot(target, beta, eta, ciPairs, km, failureObs, highlight, currentCensors) {
    const { ctx, width: W, height: H } = setupCanvas(target.canvas, target.height);
    ctx.clearRect(0, 0, W, H);
    const points = km.filter((p) => p.weibull_plot_y != null && isFinite(p.weibull_plot_x) && isFinite(p.weibull_plot_y));
    const lnEta = Math.log(eta);

    const xs = points.map((p) => p.weibull_plot_x);
    const ys = points.map((p) => p.weibull_plot_y);
    // Current-life "now" markers, plotted in ln(life hours) space like the points.
    const markers = (currentCensors || [])
      .map((obs) => ({ x: Math.log(Number(obs.life_hours_for_weibull)), obs }))
      .filter((m) => isFinite(m.x));
    // include the fit line endpoints + current-life markers in the domain
    const domainXs = xs.concat(markers.map((m) => m.x));
    const xMinData = domainXs.length ? Math.min(...domainXs) : lnEta - 1;
    const xMaxData = domainXs.length ? Math.max(...domainXs) : lnEta + 1;
    const xMin = Math.min(xMinData, lnEta - 1) - 0.3;
    const xMax = Math.max(xMaxData, lnEta + 1) + 0.3;
    const fitY = (x, b) => b * (x - lnEta);
    const yCandidates = ys.concat([fitY(xMin, beta), fitY(xMax, beta)]);
    const yMin = Math.min(...yCandidates) - 0.3;
    const yMax = Math.max(...yCandidates) + 0.3;

    const left = 52;
    const right = W - 16;
    const top = 16;
    const bottom = H - 38;
    const sx = (x) => left + ((x - xMin) / (xMax - xMin || 1)) * (right - left);
    const sy = (y) => bottom - ((y - yMin) / (yMax - yMin || 1)) * (bottom - top);

    drawAxes(ctx, left, right, top, bottom, "ln(life hours)", "ln(-ln R)");

    // CI fit lines (yellow)
    ctx.lineWidth = 1.5;
    ciPairs.forEach(([b, e]) => {
      const lnE = Math.log(e);
      ctx.strokeStyle = "#d6a700";
      ctx.beginPath();
      ctx.moveTo(sx(xMin), sy(b * (xMin - lnE)));
      ctx.lineTo(sx(xMax), sy(b * (xMax - lnE)));
      ctx.stroke();
    });

    // MLE fit line (green)
    ctx.strokeStyle = "#2f8f5b";
    ctx.lineWidth = 2.4;
    ctx.beginPath();
    ctx.moveTo(sx(xMin), sy(fitY(xMin, beta)));
    ctx.lineTo(sx(xMax), sy(fitY(xMax, beta)));
    ctx.stroke();

    // Map a plotted KM point back to the closest failure observation (by life
    // hours, in log space) for the hover tooltip and click-to-row jump.
    const nearestFailureByX = (plotX) => {
      let best = null;
      let bestDelta = Infinity;
      (failureObs || []).forEach((obs) => {
        const delta = Math.abs(Math.log(Number(obs.life_hours_for_weibull)) - plotX);
        if (delta < bestDelta) {
          bestDelta = delta;
          best = obs;
        }
      });
      return best;
    };

    // KM points
    const hits = [];
    points.forEach((p) => {
      const px = sx(p.weibull_plot_x);
      const py = sy(p.weibull_plot_y);
      ctx.fillStyle = "#ffffff";
      ctx.strokeStyle = "#33455a";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      hits.push({ px, py, point: p, obs: nearestFailureByX(p.weibull_plot_x) });
    });

    // Current-life "now" markers: full-height red vertical lines.
    markers.forEach((m) => {
      const px = sx(m.x);
      ctx.strokeStyle = "#c0392b";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(px, top);
      ctx.lineTo(px, bottom);
      ctx.stroke();
    });

    // Resolve the observation under the cursor: a current-life marker (matched on
    // x, since it spans the pane height) takes precedence, then the nearest point.
    const locate = (mx, my) => {
      const marker = markers.find((m) => Math.abs(sx(m.x) - mx) <= 5 && my >= top && my <= bottom);
      if (marker) return marker.obs;
      const hit = hits.find((h) => Math.hypot(h.px - mx, h.py - my) <= 7);
      return hit ? hit.obs : null;
    };

    target.canvas.onclick = (event) => {
      const rect = target.canvas.getBoundingClientRect();
      const obs = locate(event.clientX - rect.left, event.clientY - rect.top);
      if (obs && highlight) highlight(obs.weibull_observation_id);
    };
    attachPointHover(target.canvas, locate);
  }

  function drawCurvePane(target, opts) {
    const { ctx, width: W, height: H } = setupCanvas(target.canvas, target.height);
    ctx.clearRect(0, 0, W, H);
    const allY = [].concat(
      opts.mleLine.map((p) => p[1]),
      ...opts.ciLines.map((line) => line.map((p) => p[1])),
      (opts.scatter || []).map((p) => p[1]),
      (opts.censored || []).map((p) => p[1])
    );
    // Include any current-life marker x so its vertical line stays inside the plot
    // even when current life runs past the fitted curve's last life-hours point.
    const xMax = Math.max(...opts.mleLine.map((p) => p[0]), ...(opts.verticalMarkers || []).map((m) => m[0]), 1);
    const yMax = opts.yMax != null ? opts.yMax : Math.max(...allY, 1e-9) * 1.05;
    const left = 56;
    const right = W - 16;
    const top = 16;
    const bottom = H - 38;
    const sx = (x) => left + (x / (xMax || 1)) * (right - left);
    const sy = (y) => bottom - (y / (yMax || 1)) * (bottom - top);

    drawAxes(ctx, left, right, top, bottom, opts.xLabel, opts.yLabel, xMax, yMax);

    ctx.lineWidth = 1.5;
    opts.ciLines.forEach((line) => {
      ctx.strokeStyle = "#d6a700";
      strokePolyline(ctx, line, sx, sy);
    });

    ctx.strokeStyle = "#2f8f5b";
    ctx.lineWidth = 2.4;
    strokePolyline(ctx, opts.mleLine, sx, sy);

    const hits = [];
    (opts.scatter || []).forEach((p) => {
      const px = sx(p[0]);
      const py = sy(p[1]);
      ctx.fillStyle = "#ffffff";
      ctx.strokeStyle = "#33455a";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      hits.push({ px, py, point: p[2], obs: opts.scatterObs ? opts.scatterObs(p[2]) : null, pick: opts.scatterPick });
    });
    (opts.censored || []).forEach((p) => {
      const px = sx(p[0]);
      const py = sy(p[1]);
      ctx.fillStyle = "#c0392b";
      ctx.strokeStyle = "#7d2b2b";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      hits.push({ px, py, point: p[2], obs: p[2], pick: opts.censoredPick });
    });
    // Current-life "now" markers: full-height red vertical lines.
    (opts.verticalMarkers || []).forEach((m) => {
      const px = sx(m[0]);
      ctx.strokeStyle = "#c0392b";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(px, top);
      ctx.lineTo(px, bottom);
      ctx.stroke();
      hits.push({ px, vertical: true, top, bottom, point: m[1], obs: m[1], pick: opts.markerPick });
    });
    // Locate the hit under the cursor (canvas pixels). Vertical markers span the
    // pane height, so they match on x proximity; points match within a small radius.
    const locate = (mx, my) =>
      hits.find((h) => {
        if (h.vertical) return Math.abs(h.px - mx) <= 5 && my >= h.top && my <= h.bottom;
        return Math.hypot(h.px - mx, h.py - my) <= 7;
      });
    if (hits.some((h) => h.pick)) {
      target.canvas.onclick = (event) => {
        const rect = target.canvas.getBoundingClientRect();
        const hit = locate(event.clientX - rect.left, event.clientY - rect.top);
        if (hit && hit.pick) hit.pick(hit.point);
      };
    }
    if (hits.length) {
      attachPointHover(target.canvas, (mx, my) => {
        const hit = locate(mx, my);
        return hit ? hit.obs : null;
      });
    }
  }

  function strokePolyline(ctx, line, sx, sy) {
    ctx.beginPath();
    line.forEach((p, index) => {
      const px = sx(p[0]);
      const py = sy(p[1]);
      if (index === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
  }

  function drawAxes(ctx, left, right, top, bottom, xLabel, yLabel, xMax, yMax) {
    ctx.strokeStyle = "#c4d2dd";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    // Numeric range ticks (subtle). Only drawn when a max value is supplied, i.e.
    // the linear CDF/PDF/Hazard panes; the log-space probability plot omits them.
    ctx.fillStyle = "#5e7082";
    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "alphabetic";
    if (xMax != null) {
      ctx.textAlign = "left";
      ctx.fillText("0", left - 4, bottom + 14);
      ctx.textAlign = "right";
      ctx.fillText(fmt(xMax), right, bottom + 14);
    }
    if (yMax != null) {
      ctx.textAlign = "left";
      ctx.fillText(fmt(yMax), left - 46, top + 6);
    }

    // Axis titles: drawn prominently (darker + semibold, centered on the plot
    // area) so every Weibull graph clearly labels what its X and Y axes show.
    ctx.fillStyle = "#2a3f50";
    ctx.font = "600 11.5px Inter, sans-serif";
    if (xLabel) {
      ctx.textAlign = "center";
      ctx.textBaseline = "alphabetic";
      ctx.fillText(xLabel, (left + right) / 2, bottom + 31);
    }
    if (yLabel) {
      ctx.save();
      ctx.translate(15, (top + bottom) / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(yLabel, 0, 0);
      ctx.restore();
    }
    // Restore the default text origin so later drawing on this context is unaffected.
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  // ---- Excel-style column sort / filter ------------------------------------
  // Adds a per-column header dropdown (Sort A→Z / Z→A plus a checkable value
  // filter, like Excel's column filter) to a rendered table. It works on the rows
  // currently in the table: for the Weibull data table that is every observation,
  // and for the paginated disposition editor it is the visible page (complementing
  // the cross-page server-side search box). Sorting reorders the existing <tr>
  // nodes so editable controls keep their state; filtering hides non-matching
  // rows. The dropdown is attached to <body> so the table's scroll containers and
  // sticky headers never clip it.
  let openColumnMenu = null;
  function closeColumnMenu() {
    if (openColumnMenu) {
      openColumnMenu.remove();
      openColumnMenu = null;
    }
  }
  document.addEventListener("mousedown", (event) => {
    if (!openColumnMenu) return;
    if (openColumnMenu.contains(event.target)) return;
    if (event.target.closest && event.target.closest(".lda-col-tool")) return;
    closeColumnMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeColumnMenu();
  });

  // Comparable text of a body cell, reading inside editable controls so the
  // disposition editor sorts/filters by the chosen value rather than empty markup.
  function columnCellText(td) {
    if (!td) return "";
    const select = td.querySelector("select");
    if (select) {
      const opt = select.options[select.selectedIndex];
      return ((opt ? opt.textContent : select.value) || "").trim();
    }
    const checkbox = td.querySelector('input[type="checkbox"]');
    if (checkbox) return checkbox.checked ? "Yes" : "No";
    const field = td.querySelector("input, textarea");
    if (field) return String(field.value || "").trim();
    return (td.textContent || "").trim();
  }

  // Parse a cell's display text as a number, tolerating the thousands separators
  // that fmtFixed/toLocaleString add (e.g. "1,000.00"), which plain Number()
  // rejects. Returns NaN for anything that is not a pure (optionally grouped)
  // number so dates and free text fall through to the text comparison.
  function columnNumericValue(s) {
    if (s === "") return NaN;
    const cleaned = s.replace(/,/g, "");
    return /^[+-]?(\d+\.?\d*|\.\d+)$/.test(cleaned) ? Number(cleaned) : NaN;
  }

  // Numbers sort numerically (and before text); everything else uses a
  // numeric-aware locale compare, which also orders ISO date strings correctly.
  function columnCompare(a, b) {
    const na = columnNumericValue(a);
    const nb = columnNumericValue(b);
    const aNum = isFinite(na);
    const bNum = isFinite(nb);
    if (aNum && bNum) return na - nb;
    if (aNum) return -1;
    if (bNum) return 1;
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  }

  function enableTableColumnTools(table) {
    const thead = table.tHead;
    const tbody = table.tBodies[0];
    if (!thead || !tbody || !thead.rows.length) return;
    const headerRow = thead.rows[thead.rows.length - 1];
    const ths = Array.from(headerRow.cells);
    // Active value filter per column: a Set of allowed display values, or null
    // (no filter, every value shown).
    const filters = ths.map(() => null);

    const dataRows = () =>
      Array.from(tbody.rows).filter(
        (tr) => tr.cells.length === ths.length && !tr.querySelector(".lda-empty-row")
      );

    function applyFilters() {
      dataRows().forEach((tr) => {
        const hidden = filters.some((set, col) => set && !set.has(columnCellText(tr.cells[col])));
        tr.style.display = hidden ? "none" : "";
      });
    }

    function sortBy(col, dir) {
      const rows = dataRows();
      rows.sort((ra, rb) => {
        const cmp = columnCompare(columnCellText(ra.cells[col]), columnCellText(rb.cells[col]));
        return dir === "desc" ? -cmp : cmp;
      });
      rows.forEach((tr) => tbody.appendChild(tr));
      ths.forEach((th, i) => {
        th.classList.remove("is-sorted-asc", "is-sorted-desc");
        if (i === col) th.classList.add(dir === "desc" ? "is-sorted-desc" : "is-sorted-asc");
      });
    }

    function openMenu(col, anchorBtn) {
      closeColumnMenu();
      const menu = el("div", { class: "lda-col-menu" });
      menu.dataset.col = String(col);

      const sortAsc = el("button", { type: "button", class: "lda-col-menu-sort", text: "Sort A → Z" });
      const sortDesc = el("button", { type: "button", class: "lda-col-menu-sort", text: "Sort Z → A" });
      sortAsc.addEventListener("click", () => { sortBy(col, "asc"); closeColumnMenu(); });
      sortDesc.addEventListener("click", () => { sortBy(col, "desc"); closeColumnMenu(); });
      menu.appendChild(el("div", { class: "lda-col-menu-sorts" }, [sortAsc, sortDesc]));

      // Distinct values across every data row (not just the rows other filters
      // currently leave visible) so a filtered-out value can always be re-added.
      const BLANK = "(Blanks)";
      const values = new Set();
      dataRows().forEach((tr) => {
        const text = columnCellText(tr.cells[col]);
        values.add(text === "" ? BLANK : text);
      });
      const sortedValues = Array.from(values).sort(columnCompare);

      const search = el("input", { type: "search", class: "lda-col-menu-search", placeholder: "Search values…" });
      menu.appendChild(search);

      const allBox = el("input", { type: "checkbox" });
      allBox.checked = true;
      const allLabel = el("label", { class: "lda-col-menu-all" }, [allBox, el("span", { text: "(Select all)" })]);
      menu.appendChild(allLabel);

      const list = el("div", { class: "lda-col-menu-values" });
      const active = filters[col];
      const boxes = sortedValues.map((value) => {
        const rawValue = value === BLANK ? "" : value;
        const box = el("input", { type: "checkbox" });
        box.checked = !active || active.has(rawValue);
        box.dataset.value = rawValue;
        const label = el("label", { class: "lda-col-menu-value" }, [box, el("span", { text: value, title: value })]);
        list.appendChild(label);
        return { box, label, search: value.toLowerCase() };
      });
      menu.appendChild(list);

      const syncAll = () => {
        const visible = boxes.filter((b) => b.label.style.display !== "none");
        allBox.checked = visible.length > 0 && visible.every((b) => b.box.checked);
      };
      syncAll();

      allBox.addEventListener("change", () => {
        boxes.forEach((b) => {
          if (b.label.style.display !== "none") b.box.checked = allBox.checked;
        });
      });
      boxes.forEach((b) => b.box.addEventListener("change", syncAll));
      search.addEventListener("input", () => {
        const q = search.value.trim().toLowerCase();
        boxes.forEach((b) => { b.label.style.display = b.search.includes(q) ? "" : "none"; });
        syncAll();
      });

      const clear = el("button", { type: "button", class: "btn-secondary", text: "Clear filter" });
      const apply = el("button", { type: "button", class: "btn-primary", text: "Apply" });
      clear.addEventListener("click", () => {
        filters[col] = null;
        anchorBtn.classList.remove("is-active");
        applyFilters();
        closeColumnMenu();
      });
      apply.addEventListener("click", () => {
        const allowed = boxes.filter((b) => b.box.checked).map((b) => b.box.dataset.value);
        if (allowed.length === boxes.length) {
          filters[col] = null;
          anchorBtn.classList.remove("is-active");
        } else {
          filters[col] = new Set(allowed);
          anchorBtn.classList.add("is-active");
        }
        applyFilters();
        closeColumnMenu();
      });
      menu.appendChild(el("div", { class: "lda-col-menu-actions" }, [clear, apply]));

      document.body.appendChild(menu);
      openColumnMenu = menu;
      // Position under the trigger, clamped to the viewport.
      const rect = anchorBtn.getBoundingClientRect();
      let left = rect.left;
      if (left + menu.offsetWidth > window.innerWidth - 8) left = window.innerWidth - menu.offsetWidth - 8;
      menu.style.left = Math.round(Math.max(8, left)) + "px";
      menu.style.top = Math.round(rect.bottom + 4) + "px";
      search.focus();
    }

    ths.forEach((th, col) => {
      const label = (th.textContent || "").trim();
      th.textContent = "";
      const btn = el("button", {
        type: "button",
        class: "lda-col-tool",
        title: `Sort or filter “${label}”`,
        "aria-label": `Sort or filter ${label}`,
        text: "▾",
      });
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        if (openColumnMenu && openColumnMenu.dataset.col === String(col)) {
          closeColumnMenu();
          return;
        }
        openMenu(col, btn);
      });
      th.appendChild(el("div", { class: "lda-col-head" }, [el("span", { class: "lda-col-label", text: label }), btn]));
    });

    // Drop every active value filter and re-show all rows. Returned so callers
    // (e.g. the chart click-to-row jump) can reveal a row that an active filter
    // is currently hiding before scrolling to it.
    function clearFilters() {
      filters.forEach((_, i) => { filters[i] = null; });
      ths.forEach((th) => {
        const btn = th.querySelector(".lda-col-tool");
        if (btn) btn.classList.remove("is-active");
      });
      applyFilters();
    }

    return { clearFilters };
  }

  // ---- Weibull point hover tooltips ----------------------------------------
  // A single floating tooltip, reused by every Weibull plot, that describes the
  // observation behind a hovered point or current-life marker.
  let pointTooltipEl = null;
  function pointTooltip() {
    if (!pointTooltipEl) {
      pointTooltipEl = el("div", { class: "lda-point-tooltip", role: "tooltip" });
      pointTooltipEl.hidden = true;
      document.body.appendChild(pointTooltipEl);
    }
    return pointTooltipEl;
  }
  function hidePointTooltip() {
    if (pointTooltipEl) pointTooltipEl.hidden = true;
  }
  function showPointTooltip(clientX, clientY, obs) {
    if (!obs) { hidePointTooltip(); return; }
    const truncate = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s);
    const tip = pointTooltip();
    tip.innerHTML = "";
    const fields = [
      ["Task ID", obs.source_task_id != null && obs.source_task_id !== "" ? String(obs.source_task_id) : "—"],
      ["Life hours", fmtFixed(obs.life_hours_for_weibull) || "—"],
      ["Start", obs.start_datetime || "—"],
      ["End / cutoff", obs.end_datetime || obs.analysis_cutoff_datetime || "—"],
      ["Request", truncate(obs.source_request_description, 220) || "—"],
      ["Completion notes", truncate(obs.source_completion_notes, 220) || "—"],
    ];
    fields.forEach(([label, value]) => {
      tip.appendChild(
        el("div", { class: "lda-point-tooltip-row" }, [
          el("span", { class: "lda-point-tooltip-label", text: label }),
          el("span", { class: "lda-point-tooltip-value", text: value }),
        ])
      );
    });
    tip.hidden = false;
    const rect = tip.getBoundingClientRect();
    const margin = 14;
    let x = clientX + margin;
    let y = clientY + margin;
    if (x + rect.width > window.innerWidth - 8) x = clientX - rect.width - margin;
    if (y + rect.height > window.innerHeight - 8) y = clientY - rect.height - margin;
    tip.style.left = Math.round(Math.max(8, x)) + "px";
    tip.style.top = Math.round(Math.max(8, y)) + "px";
  }

  // Wire hover tooltips on a chart canvas. `locate(mx, my)` returns the
  // observation under the cursor (in canvas pixels) or null.
  function attachPointHover(canvas, locate) {
    canvas.onmousemove = (event) => {
      const rect = canvas.getBoundingClientRect();
      const obs = locate(event.clientX - rect.left, event.clientY - rect.top);
      canvas.style.cursor = obs ? "pointer" : "";
      if (obs) showPointTooltip(event.clientX, event.clientY, obs);
      else hidePointTooltip();
    };
    canvas.onmouseleave = () => {
      canvas.style.cursor = "";
      hidePointTooltip();
    };
  }

  // ---- wiring ---------------------------------------------------------------
  function init() {
    const assetInput = $("lda-asset");
    if (!assetInput) return;
    // Asset combobox wiring is shared by the Perform Analysis page and the
    // dedicated disposition page.
    assetInput.addEventListener("input", onAssetInput);
    assetInput.addEventListener("keydown", onAssetKeydown);
    assetInput.addEventListener("focus", openAssetDropdown);
    // Commit a manually edited value synchronously on blur so actions clicked
    // immediately after typing run against the current asset rather than the
    // previous one still held by the input debounce.
    assetInput.addEventListener("blur", evaluateAssetSelection);
    // Close the dropdown when clicking anywhere outside the combobox.
    document.addEventListener("mousedown", (event) => {
      const combobox = $("lda-asset-combobox");
      if (combobox && !combobox.contains(event.target)) closeAssetDropdown();
    });

    if ($("lda-disposition-root")) initDispositionPage();
    else initAnalysisPage();
  }

  function initAnalysisPage() {
    state.pageMode = "analysis";
    $("lda-perform").addEventListener("click", performAnalysis);
    $("lda-disposition-wo").addEventListener("click", () => gotoDisposition("wo"));
    $("lda-disposition-pm").addEventListener("click", () => gotoDisposition("pm"));
    $("lda-calculate-all").addEventListener("click", calculateAll);
    $("lda-pareto-toggle").addEventListener("change", (event) => {
      state.paretoMetric = event.target.checked ? "failure_count" : "downtime_hours";
      drawPareto();
    });
    const typeSelect = $("lda-analysis-type");
    if (typeSelect) {
      state.analysisType = typeSelect.value || ANALYSIS_TYPES.WEIBULL;
      typeSelect.addEventListener("change", () => setAnalysisType(typeSelect.value));
    }
    // Failure Mode Trend date-range filter.
    const trendFrom = $("lda-trend-from");
    const trendTo = $("lda-trend-to");
    const trendReset = $("lda-trend-reset");
    if (trendFrom) trendFrom.addEventListener("change", onTrendRangeChange);
    if (trendTo) trendTo.addEventListener("change", onTrendRangeChange);
    if (trendReset) trendReset.addEventListener("click", resetTrendRange);
    // PM Effectiveness date-range filter.
    const pmFrom = $("lda-pm-from");
    const pmTo = $("lda-pm-to");
    const pmReset = $("lda-pm-reset");
    if (pmFrom) pmFrom.addEventListener("change", onPmRangeChange);
    if (pmTo) pmTo.addEventListener("change", onPmRangeChange);
    if (pmReset) pmReset.addEventListener("click", resetPmRange);
    // Set the initial secondary-panel visibility for the default analysis type.
    applyAnalysisTypeUI();
    window.addEventListener("resize", () => {
      if (state.paretoRows.length) drawPareto();
      if (state.analysisType === ANALYSIS_TYPES.TREND) renderTrendChart();
      if (state.analysisType === ANALYSIS_TYPES.PM) renderPmChart();
      if (state.analysisType === ANALYSIS_TYPES.DOWNTIME) {
        renderDowntimeTrendChart();
        renderDowntimeDistChart();
        renderDowntimeAssetChart();
      }
      if (state.analysisRedraw) state.analysisRedraw();
    });
    loadAssets();
  }

  function initDispositionPage() {
    state.pageMode = "disposition";
    const params = new URLSearchParams(window.location.search);
    state.dispositionKind = (params.get("kind") || "wo").toLowerCase() === "pm" ? "pm" : "wo";
    state.dispositionScope = "all";
    state.dispositionPageIndex = 0;
    state.dispositionSearch = "";

    const kindSelect = $("lda-disp-kind");
    if (kindSelect) {
      kindSelect.value = state.dispositionKind;
      kindSelect.addEventListener("change", async () => {
        const requested = kindSelect.value === "pm" ? "pm" : "wo";
        if (!(await confirmDiscardUnsavedChanges(state.dispositionChangedFn))) {
          kindSelect.value = state.dispositionKind;
          return;
        }
        state.dispositionKind = requested;
        state.dispositionPageIndex = 0;
        reloadDispositionForSelection();
      });
    }
    const scopeSelect = $("lda-disp-scope");
    if (scopeSelect) {
      scopeSelect.value = state.dispositionScope;
      scopeSelect.addEventListener("change", async () => {
        const requested = scopeSelect.value === "new" ? "new" : "all";
        if (!(await confirmDiscardUnsavedChanges(state.dispositionChangedFn))) {
          scopeSelect.value = state.dispositionScope;
          return;
        }
        state.dispositionScope = requested;
        state.dispositionPageIndex = 0;
        reloadDispositionForSelection();
      });
    }

    // Free-text search box: filters the disposition table server-side (so matches
    // span every page, not just the visible 50 rows) for both WO and PM records.
    // Debounced like the asset search, and guarded by the same unsaved-edit
    // confirmation the Record Type / Rows selectors use, since reloading the
    // editor discards in-progress edits.
    const searchInput = $("lda-disp-search");
    if (searchInput) {
      let searchDebounce = null;
      let lastSearch = "";
      const applySearch = async () => {
        const value = searchInput.value.trim();
        if (value === lastSearch) return;
        if (!(await confirmDiscardUnsavedChanges(state.dispositionChangedFn))) {
          searchInput.value = lastSearch;
          return;
        }
        lastSearch = value;
        state.dispositionSearch = value;
        state.dispositionPageIndex = 0;
        reloadDispositionForSelection();
      };
      searchInput.addEventListener("input", () => {
        if (searchDebounce) clearTimeout(searchDebounce);
        searchDebounce = setTimeout(applySearch, 300);
      });
    }

    // Preselect the asset passed from the Perform Analysis page once the asset
    // list has loaded, then open its disposition editor.
    const requestedAsset = (params.get("asset") || "").trim();
    loadAssets().then(() => {
      if (requestedAsset && state.assetByNumber.has(requestedAsset)) {
        $("lda-asset").value = requestedAsset;
        evaluateAssetSelection();
      }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
