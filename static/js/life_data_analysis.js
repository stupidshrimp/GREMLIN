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
    latestResult: null,
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
      drawPareto();
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
      if (hit) runParetoMechanism(hit.row);
    };
  }

  function runParetoMechanism(row) {
    if (row.failure_mode_id == null || row.failure_mechanism_id == null) {
      showBanner("The selected Pareto bar does not have a complete failure mode/mechanism selection.", "error");
      return;
    }
    runAnalysisForGroup(
      {
        grouping_level: "FAILURE_MECHANISM",
        failure_mode_id: row.failure_mode_id,
        failure_mechanism_id: row.failure_mechanism_id,
      },
      "Running clicked mechanism Weibull analysis…"
    );
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
      el("tr", {}, data.display_columns.concat(extraHeaders).map((h) => el("th", { text: h }))),
    ]);
    const tbody = el("tbody");

    data.rows.forEach((row, index) => {
      const tr = el("tr");
      data.display_columns.forEach((key) => {
        tr.appendChild(el("td", { class: "lda-readonly", text: row[key] == null ? "" : String(row[key]) }));
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

    const changed = () => rowStates.filter((rs) => JSON.stringify(dispositionPayloadFromRow(rs, data.kind)) !== rs.initial);
    // Exposed so the Rows/Scope selectors on the dedicated disposition page can
    // confirm before discarding unsaved edits, the same way page navigation does.
    state.dispositionChangedFn = changed;

    const checkAllButton = el("button", {
      class: "btn-secondary",
      text: "Check all Include in Weibull Candidate",
      onclick: () => {
        rowStates.forEach((rs) => (rs.include.checked = true));
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

    function openList() {
      if (!isOpen) {
        document.body.appendChild(list);
        isOpen = true;
        // Close (rather than chase) the portaled list when anything scrolls, so
        // it can never float detached from its input. Capture catches scrolls on
        // the inner table container too.
        window.addEventListener("scroll", closeList, true);
        window.addEventListener("resize", closeList, true);
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
      window.removeEventListener("scroll", closeList, true);
      window.removeEventListener("resize", closeList, true);
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
      showBanner("No disposition rows changed, so nothing needed to be saved.", "info");
      return;
    }
    const payloads = changed.map((rs) => dispositionPayloadFromRow(rs, kind));
    beginLoading("Saving dispositions…");
    try {
      const result = await postJson(`${API}/dispositions/save`, { dispositions: payloads });
      changed.forEach((rs) => (rs.initial = JSON.stringify(dispositionPayloadFromRow(rs, kind))));
      showBanner(`Saved ${result.saved} changed REL disposition row(s) to event_disposition.`, "success");
      refreshSummary();
    } catch (err) {
      showBanner(err.message, "error");
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

  // ---- perform Weibull analysis ---------------------------------------------
  async function performAnalysis() {
    if (!state.selectedAsset) return;
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
    runAnalysisForGroup(groups[choice]);
  }

  async function runAnalysisForGroup(group, message) {
    if (!state.selectedAsset) return;
    const asset = state.selectedAsset;
    beginLoading(message || "Running Weibull analysis…");
    try {
      const data = await postJson(`${API}/perform-analysis`, {
        asset,
        grouping_level: group.grouping_level,
        failure_mode_id: group.failure_mode_id,
        failure_mechanism_id: group.failure_mechanism_id,
      });
      if (state.selectedAsset !== asset) return; // asset changed mid-request; ignore stale result
      state.latestResult = data.result;
      renderAnalysisResult(data.result);
      refreshSummary();
    } catch (err) {
      if (state.selectedAsset === asset) showBanner(err.message, "error");
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
          "The hazard and PDF panes intentionally show only the MLE curve. Click any plotted failure or censored point, or the current-life line, to jump to the source Weibull data row below.",
      }),
      panel("Results Interpretation Summary", buildInterpretationTable(result),
        "Recommendations are based on beta, eta, MTTF, and approximate 95% confidence intervals for the fitted Weibull parameters."),
      panel("Weibull Data Used for Graphs", dataTable.node,
        "Rows are the observations included in the Weibull fit. White points are completed failures; red points are right-censored observations."),
    ]);
    $("lda-workspace").appendChild(card);
    card.scrollIntoView({ behavior: "smooth", block: "start" });
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
          el("td", { text: row.value || "—" }),
          el("td", { text: row.recommendation || "—" }),
        ])
      );
    });
    table.appendChild(tbody);
    return table;
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
    const node = el("div", { class: "lda-data-scroll" }, [table]);
    function highlight(observationId) {
      const tr = rowByObs.get(Number(observationId));
      if (!tr) return;
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
      let best = null;
      let bestDelta = Infinity;
      failureObs.forEach((obs) => {
        const delta = Math.abs(Number(obs.life_hours_for_weibull) - Number(point.life_hours));
        if (delta < bestDelta) {
          bestDelta = delta;
          best = obs;
        }
      });
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
      hits.push({ px, py, point: p });
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

    target.canvas.onclick = (event) => {
      const rect = target.canvas.getBoundingClientRect();
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
      // Current-life marker: jump straight to the censored observation's own row.
      const marker = markers.find((m) => Math.abs(sx(m.x) - mx) <= 5 && my >= top && my <= bottom);
      if (marker) {
        if (highlight) highlight(marker.obs.weibull_observation_id);
        return;
      }
      const hit = hits.find((h) => Math.hypot(h.px - mx, h.py - my) <= 7);
      if (hit && highlight) {
        // nearest failure observation by life hours
        let best = null;
        let bestDelta = Infinity;
        failureObs.forEach((obs) => {
          const delta = Math.abs(Math.log(Number(obs.life_hours_for_weibull)) - hit.point.weibull_plot_x);
          if (delta < bestDelta) {
            bestDelta = delta;
            best = obs;
          }
        });
        if (best) highlight(best.weibull_observation_id);
      }
    };
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
      hits.push({ px, py, point: p[2], pick: opts.scatterPick });
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
      hits.push({ px, py, point: p[2], pick: opts.censoredPick });
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
      hits.push({ px, vertical: true, top, bottom, point: m[1], pick: opts.markerPick });
    });
    if (hits.some((h) => h.pick)) {
      target.canvas.onclick = (event) => {
        const rect = target.canvas.getBoundingClientRect();
        const mx = event.clientX - rect.left;
        const my = event.clientY - rect.top;
        const hit = hits.find((h) => {
          if (!h.pick) return false;
          // Vertical markers span the pane height, so match on x proximity.
          if (h.vertical) return Math.abs(h.px - mx) <= 5 && my >= h.top && my <= h.bottom;
          return Math.hypot(h.px - mx, h.py - my) <= 7;
        });
        if (hit) hit.pick(hit.point);
      };
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
    window.addEventListener("resize", () => {
      if (state.paretoRows.length) drawPareto();
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
