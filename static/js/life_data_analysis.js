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
    assetLoadError: null,
    assetDropdownOpen: false,
    assetActiveIndex: -1,
    selectedAsset: null,
    paretoRows: [],
    paretoMetric: "downtime_hours",
    latestResult: null,
    summaryToken: 0,
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
      state.assetLoadError = null;
      setAssetOptions(data.assets || []);
      if (state.assetDropdownOpen) renderAssetDropdown();
      hint.textContent = state.assets.length
        ? `${state.assets.length} Asset Number(s) available. Type to search.`
        : "No mapped CMMS Asset Numbers were found in the database.";
    } catch (err) {
      // Distinguish a failed load from a genuinely empty asset list so the
      // dropdown does not misreport a backend error as "No Asset Numbers
      // available." Keep the error visible inline and in the banner.
      state.assetLoadError = err.message;
      if (state.assetDropdownOpen) renderAssetDropdown();
      hint.textContent = "Could not load Asset Numbers.";
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
    if (state.assetLoadError) {
      list.appendChild(
        el("li", { class: "lda-combobox-empty", text: `Could not load Asset Numbers: ${state.assetLoadError}` })
      );
      state.assetFiltered = [];
      return;
    }
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
    $("lda-summary-card").hidden = !ready;
    $("lda-actions").hidden = !ready;
    $("lda-calculate-all-card").hidden = !ready;
    if (asset) {
      $("lda-asset-hint").textContent = asset.asset_name
        ? `Selected ${asset.asset_number} — ${asset.asset_name}.`
        : `Selected ${asset.asset_number}.`;
      refreshSummary();
    } else if (value) {
      $("lda-asset-hint").textContent = `"${value}" is not a known Asset Number. Choose one from the list.`;
    }
  }

  function clearWorkspace() {
    $("lda-workspace").innerHTML = "";
  }

  async function refreshMapping() {
    beginLoading("Refreshing CMMS mapping…");
    try {
      const data = await postJson(`${API}/refresh-mapping`, {});
      state.assetLoadError = null;
      setAssetOptions(data.assets || []);
      if (state.assetDropdownOpen) renderAssetDropdown();
      showBanner(`Refreshed ${data.mapped || 0} mapped CMMS row(s). ${state.assets.length} Asset Number(s) available.`, "success");
      // Re-evaluate the current asset in case its data changed.
      evaluateAssetSelection();
    } catch (err) {
      showBanner(err.message, "error");
    } finally {
      endLoading();
    }
  }

  // ---- readiness summary ----------------------------------------------------
  async function refreshSummary() {
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

  function drawPareto() {
    const canvas = $("lda-pareto-chart");
    const empty = $("lda-pareto-empty");
    const rows = paretoDisplayRows();
    empty.hidden = rows.length > 0;
    const metric = state.paretoMetric;
    const ctx = setupCanvas(canvas, 320);
    const W = canvas.clientWidth;
    const H = 320;
    ctx.clearRect(0, 0, W, H);
    if (!rows.length) return;

    const left = 52;
    const right = W - 46;
    const top = 18;
    const bottom = H - 64;
    const maxVal = Math.max(...rows.map((r) => Number(r[metric]) || 0), 1);
    const barGap = 8;
    const barW = Math.max(6, (right - left) / rows.length - barGap);
    const hitboxes = [];

    // axes
    ctx.strokeStyle = "#c4d2dd";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    rows.forEach((row, index) => {
      const value = Number(row[metric]) || 0;
      const x = left + index * ((right - left) / rows.length) + barGap / 2;
      const barHeight = (value / maxVal) * (bottom - top);
      const y = bottom - barHeight;
      ctx.fillStyle = "#3f5e77";
      ctx.fillRect(x, y, barW, barHeight);
      hitboxes.push({ x, y: top, w: barW, h: bottom - top, row });

      ctx.save();
      ctx.fillStyle = "#5e7082";
      ctx.font = "10px Inter, sans-serif";
      ctx.translate(x + barW / 2, bottom + 6);
      ctx.rotate(Math.PI / 5);
      const label = (row.failure_mechanism_name || "—").slice(0, 18);
      ctx.fillText(label, 0, 0);
      ctx.restore();
    });

    // cumulative percent line
    ctx.strokeStyle = "#c2723b";
    ctx.lineWidth = 2;
    ctx.beginPath();
    rows.forEach((row, index) => {
      const x = left + index * ((right - left) / rows.length) + ((right - left) / rows.length) / 2;
      const y = bottom - (Number(row._cumulative_percent) || 0) / 100 * (bottom - top);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.fillStyle = "#c2723b";
    rows.forEach((row, index) => {
      const x = left + index * ((right - left) / rows.length) + ((right - left) / rows.length) / 2;
      const y = bottom - (Number(row._cumulative_percent) || 0) / 100 * (bottom - top);
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    });

    // y axis labels
    ctx.fillStyle = "#5e7082";
    ctx.font = "10px Inter, sans-serif";
    ctx.fillText(metric === "failure_count" ? "Failures" : "Downtime h", 2, top + 4);
    ctx.fillText("100%", right - 26, top + 4);

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
  async function openDisposition(kind) {
    if (!state.selectedAsset) {
      showBanner("Select an Asset Number before opening a disposition page.", "error");
      return;
    }
    const recordLabel = kind === "pm" ? "PM reset events" : "work orders";
    const scope = await openModal({
      title: "Choose disposition rows",
      bodyNodes: [
        el("p", {
          text:
            `Which ${recordLabel} do you want to disposition? Choose only new/undispositioned rows to ` +
            "show records with a blank failure mode or mechanism, or All to show every eligible row.",
        }),
      ],
      actions: [
        { label: "Only new/undispositioned", primary: false, value: () => "new" },
        { label: "All", primary: true, value: () => "all" },
        { label: "Cancel", primary: false, value: () => null },
      ],
    });
    if (!scope) return;
    loadDispositionPage(kind, scope, 0);
  }

  async function loadDispositionPage(kind, scope, pageIndex) {
    beginLoading("Loading disposition editor…");
    try {
      const url = `${API}/dispositions?asset=${encodeURIComponent(state.selectedAsset)}&kind=${kind}&scope=${scope}&page=${pageIndex}`;
      const data = await getJson(url);
      renderDispositionEditor(data);
    } catch (err) {
      showBanner(err.message, "error");
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
      // PM mode/mechanism are <select>s that carry the real id per option, so
      // mechanisms that share a display name across modes stay distinct.
      payload.reset_target_failure_mode_id = selectId(rowState.mode);
      payload.reset_target_failure_mechanism_id = selectId(rowState.mech);
    } else {
      const modeName = rowState.mode.value.trim();
      const mechName = rowState.mech.value.trim();
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

  function selectId(select) {
    const option = select.options[select.selectedIndex];
    const raw = option ? option.dataset.id : "";
    return raw ? Number(raw) : null;
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
    const metaText =
      data.scope === "new"
        ? `Showing rows ${startRow}-${endRow} of ${data.displayed_count} rows with a blank ` +
          `${isPm ? "reset target failure mode or mechanism" : "failure mode or failure mechanism"} ` +
          `(${data.all_count} eligible rows total).`
        : `Showing rows ${startRow}-${endRow} of ${data.displayed_count} eligible rows for this asset.`;

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
        mode = buildTaxonomySelect(data.mode_options, "failure_mode_id", "failure_mode_name", row.reset_target_failure_mode_id);
        tr.appendChild(el("td", {}, [mode]));
        mech = buildTaxonomySelect(data.mechanism_options, "failure_mechanism_id", "failure_mechanism_name", row.reset_target_failure_mechanism_id);
        tr.appendChild(el("td", {}, [mech]));
      } else {
        mode = buildTaxonomyInput(data.mode_options, "failure_mode_id", "failure_mode_name", row.failure_mode_id, "lda-modelist-" + index);
        tr.appendChild(el("td", {}, mode.nodes));
        mech = buildTaxonomyInput(data.mechanism_options, "failure_mechanism_id", "failure_mechanism_name", row.failure_mechanism_id, "lda-mechlist-" + index);
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
        mode: isPm ? mode : mode.input,
        mech: isPm ? mech : mech.input,
        modeOptions: modeMap,
        mechByNameMode,
        tr,
      };
      rowState.initial = JSON.stringify(dispositionPayloadFromRow(rowState, data.kind));
      rowStates.push(rowState);
      tbody.appendChild(tr);
    });

    table.appendChild(thead);
    table.appendChild(tbody);

    const changed = () => rowStates.filter((rs) => JSON.stringify(dispositionPayloadFromRow(rs, data.kind)) !== rs.initial);

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
    const back = el("button", {
      class: "btn-secondary",
      text: "← Close disposition editor",
      onclick: clearWorkspace,
    });

    const card = el("section", { class: "glass-card lda-card" }, [
      el("div", { class: "lda-row-actions", style: "justify-content:flex-start" }, [back]),
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

  function buildTaxonomySelect(options, idKey, nameKey, currentId) {
    // Each option carries its real id (data-id) so selectId() reads the exact
    // taxonomy id even when two mechanisms share a display name across modes.
    const select = el("select", { class: "lda-select" });
    const blank = el("option", { value: "", text: "" });
    blank.dataset.id = "";
    select.appendChild(blank);
    options.forEach((opt) => {
      const option = el("option", { value: opt[nameKey], text: opt[nameKey] });
      option.dataset.id = String(opt[idKey]);
      if (currentId != null && Number(opt[idKey]) === Number(currentId)) option.selected = true;
      select.appendChild(option);
    });
    return select;
  }

  function buildTaxonomyInput(options, idKey, nameKey, currentId, listId) {
    const input = el("input", { class: "lda-input", list: listId, placeholder: "Select existing or type new…" });
    const datalist = el("datalist", { id: listId });
    options.forEach((opt) => datalist.appendChild(el("option", { value: opt[nameKey] })));
    if (currentId != null) {
      const match = options.find((opt) => Number(opt[idKey]) === Number(currentId));
      if (match) input.value = match[nameKey];
    }
    return { input, nodes: [input, datalist] };
  }

  async function maybeChangePage(data, changedFn, targetPage) {
    if (changedFn().length) {
      const proceed = await openModal({
        title: "Unsaved disposition changes",
        bodyNodes: [el("p", { text: "This page has unsaved disposition changes. Continue without saving them?" })],
        actions: [
          { label: "Stay on page", primary: false, value: () => false },
          { label: "Discard and continue", primary: true, value: () => true },
        ],
      });
      if (!proceed) return;
    }
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
          "The hazard and PDF panes intentionally show only the MLE curve. Click any plotted failure or censored point to jump to the source Weibull data row below.",
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
    const headers = ["#", "Observation ID", "Type", "Life Hours", "Failure", "Right Censored", "Start Datetime", "End/Cutoff Datetime", "Note"];
    const table = el("table", { class: "lda-data" });
    table.appendChild(el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { text: h })))]));
    const tbody = el("tbody");
    const rowByObs = new Map();
    (result.observations || []).forEach((obs) => {
      const endOrCutoff = obs.end_datetime || obs.analysis_cutoff_datetime || "";
      const tr = el("tr", {}, [
        el("td", { text: String(obs.ordered_index ?? "") }),
        el("td", { text: String(obs.weibull_observation_id ?? "") }),
        el("td", { text: obs.observation_type || "" }),
        el("td", { text: fmtFixed(obs.life_hours_for_weibull) }),
        el("td", { text: Number(obs.failure_indicator) ? "Yes" : "No" }),
        el("td", { text: Number(obs.is_right_censored) ? "Yes" : "No" }),
        el("td", { text: obs.start_datetime || "" }),
        el("td", { text: endOrCutoff }),
        el("td", { text: obs.weibull_life_note || "" }),
      ]);
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
  function setupCanvas(canvas, cssHeight) {
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || canvas.parentElement.clientWidth || 480;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(cssHeight * dpr);
    canvas.style.height = cssHeight + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  function buildAnalysisCharts(container, result, highlight) {
    container.innerHTML = "";
    const curve = result.curve_points || [];
    const km = result.km_points || [];
    const maxTime = curve.length ? Math.max(...curve.map((p) => p.life_hours)) : 1;

    // failure observation lookup so plotted points can jump to the data table
    const failureObs = (result.observations || []).filter((o) => Number(o.failure_indicator));
    const censObs = (result.observations || []).filter((o) => Number(o.is_right_censored));

    const panes = [
      { key: "prob", title: "Weibull Probability Plot", height: 300 },
      { key: "cdf", title: "Unreliability (CDF)", height: 300 },
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
      drawProbabilityPlot(canvases.prob, beta, eta, ciPairs, km, failureObs, highlight);
      // 2) CDF / unreliability vs time. White points are completed failures
      //    (KM estimate); red points are right-censored observations placed on
      //    the fitted unreliability curve at their censoring time.
      drawCurvePane(canvases.cdf, {
        mleLine: curves.map((p) => [p.life_hours, p.cdf]),
        ciLines: ciPairs.map(([b, e]) => analyticCurves(b, e).map((p) => [p.life_hours, p.cdf])),
        scatter: km.filter((p) => p.cdf_estimate != null).map((p) => [p.life_hours, p.cdf_estimate, p]),
        censored: censObs.map((obs) => {
          const t = Number(obs.life_hours_for_weibull);
          return [t, 1 - Math.exp(-Math.pow(t / eta, beta)), obs];
        }),
        yMax: 1,
        xLabel: "Life hours",
        yLabel: "Unreliability",
        scatterPick: highlightFailureByTime,
        censoredPick: (obs) => {
          if (highlight) highlight(obs.weibull_observation_id);
        },
      });
      // 3) PDF (MLE only)
      drawCurvePane(canvases.pdf, {
        mleLine: curves.map((p) => [p.life_hours, p.pdf]),
        ciLines: [],
        scatter: [],
        xLabel: "Life hours",
        yLabel: "Density",
      });
      // 4) Hazard (MLE only)
      drawCurvePane(canvases.hazard, {
        mleLine: curves.map((p) => [p.life_hours, p.hazard]),
        ciLines: [],
        scatter: [],
        xLabel: "Life hours",
        yLabel: "Hazard",
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

    draw(result.beta_mle, result.eta_mle);
    return { update: draw };
  }

  function drawProbabilityPlot(target, beta, eta, ciPairs, km, failureObs, highlight) {
    const ctx = setupCanvas(target.canvas, target.height);
    const W = target.canvas.clientWidth;
    const H = target.height;
    ctx.clearRect(0, 0, W, H);
    const points = km.filter((p) => p.weibull_plot_y != null && isFinite(p.weibull_plot_x) && isFinite(p.weibull_plot_y));
    const lnEta = Math.log(eta);

    const xs = points.map((p) => p.weibull_plot_x);
    const ys = points.map((p) => p.weibull_plot_y);
    // include the fit line endpoints in the domain
    const xMinData = xs.length ? Math.min(...xs) : lnEta - 1;
    const xMaxData = xs.length ? Math.max(...xs) : lnEta + 1;
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

    target.canvas.onclick = (event) => {
      const rect = target.canvas.getBoundingClientRect();
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
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
    const ctx = setupCanvas(target.canvas, target.height);
    const W = target.canvas.clientWidth;
    const H = target.height;
    ctx.clearRect(0, 0, W, H);
    const allY = [].concat(
      opts.mleLine.map((p) => p[1]),
      ...opts.ciLines.map((line) => line.map((p) => p[1])),
      (opts.scatter || []).map((p) => p[1]),
      (opts.censored || []).map((p) => p[1])
    );
    const xMax = Math.max(...opts.mleLine.map((p) => p[0]), 1);
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
    if (hits.some((h) => h.pick)) {
      target.canvas.onclick = (event) => {
        const rect = target.canvas.getBoundingClientRect();
        const mx = event.clientX - rect.left;
        const my = event.clientY - rect.top;
        const hit = hits.find((h) => h.pick && Math.hypot(h.px - mx, h.py - my) <= 7);
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
    ctx.fillStyle = "#5e7082";
    ctx.font = "10px Inter, sans-serif";
    if (xLabel) ctx.fillText(xLabel, (left + right) / 2 - 24, bottom + 26);
    if (yLabel) {
      ctx.save();
      ctx.translate(14, (top + bottom) / 2 + 24);
      ctx.rotate(-Math.PI / 2);
      ctx.fillText(yLabel, 0, 0);
      ctx.restore();
    }
    if (xMax != null) {
      ctx.fillText("0", left - 4, bottom + 14);
      ctx.fillText(fmt(xMax), right - 26, bottom + 14);
    }
    if (yMax != null) {
      ctx.fillText(fmt(yMax), left - 46, top + 6);
    }
  }

  // ---- wiring ---------------------------------------------------------------
  function init() {
    const assetInput = $("lda-asset");
    if (!assetInput) return;
    assetInput.addEventListener("input", onAssetInput);
    assetInput.addEventListener("keydown", onAssetKeydown);
    assetInput.addEventListener("focus", openAssetDropdown);
    // Commit a manually edited value synchronously on blur so actions clicked
    // immediately after typing (Perform/Disposition/Calculate) run against the
    // current asset rather than the previous one still held by the input debounce.
    assetInput.addEventListener("blur", evaluateAssetSelection);
    // Close the dropdown when clicking anywhere outside the combobox.
    document.addEventListener("mousedown", (event) => {
      const combobox = $("lda-asset-combobox");
      if (combobox && !combobox.contains(event.target)) closeAssetDropdown();
    });
    $("lda-perform").addEventListener("click", performAnalysis);
    $("lda-disposition-wo").addEventListener("click", () => openDisposition("wo"));
    $("lda-disposition-pm").addEventListener("click", () => openDisposition("pm"));
    $("lda-calculate-all").addEventListener("click", calculateAll);
    $("lda-refresh-mapping").addEventListener("click", refreshMapping);
    $("lda-pareto-toggle").addEventListener("change", (event) => {
      state.paretoMetric = event.target.checked ? "failure_count" : "downtime_hours";
      drawPareto();
    });
    window.addEventListener("resize", () => {
      if (state.paretoRows.length) drawPareto();
    });
    loadAssets();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
