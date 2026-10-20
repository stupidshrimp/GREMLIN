/* Analysis Population: the triage views and the renewal-stream Weibull run over
 * work orders dispositioned under the work-order standard.
 *
 * One selection drives everything (spec section 7). The intended path is broad to
 * narrow: an asset and a mode for the Pareto, trend and matrix, then down to one
 * location's one component and one mechanism for the Weibull. The server builds
 * every life within one renewal stream and says, as warnings, when a population
 * would mislead (services/disposition_standard.py, validity_warnings).
 *
 * Charts are inline SVG painted with the theme's own variables, so they follow
 * the light and dark themes with no repaint of their own.
 */
(function () {
  "use strict";

  const G = window.GremlinStandard;
  const { $, el, svg, getJson, postJson, showBanner, clearBanner, showToast, beginLoading, endLoading, day, fmt } = G;
  const API = G.STANDARD_API;

  const DIMENSIONS = [
    ["locations", "Location"],
    ["components", "Component"],
    ["failure_modes", "Failure mode"],
    ["failure_mechanisms", "Failure mechanism"],
    ["failure_causes", "Failure cause"],
    ["actions", "Action"],
  ];
  // The Pareto's group-by dimensions that a click can narrow the population by.
  const GROUP_TO_DIMENSION = {
    location: "locations",
    component: "components",
    failure_mode: "failure_modes",
    failure_mechanism: "failure_mechanisms",
    failure_cause: "failure_causes",
    action: "actions",
    category: "categories",
  };
  const POP_KEYS = [
    ["assets", "asset"],
    ["locations", "location"],
    ["components", "component"],
    ["failure_modes", "failure_mode"],
    ["failure_mechanisms", "failure_mech"],
    ["failure_causes", "failure_cause"],
    ["actions", "action"],
  ];
  const PARETO_MAX_BARS = 15;

  function emptySelection() {
    return { locations: [], components: [], failure_modes: [], failure_mechanisms: [], failure_causes: [], actions: [], categories: [] };
  }

  const state = {
    asset: null,
    options: null,
    selection: emptySelection(),
    excludeInduced: false,
    dateFrom: "",
    dateTo: "",
    groupBy: "location",
    period: "month",
    topN: 5,
    view: "pareto",
    patterns: null,
    // The Weibull result on screen, kept so a resize can draw it again.
    weibullResult: null,
    token: 0,
    weibullToken: 0,
  };

  // ---- the selection -----------------------------------------------------------
  function selectionPayload() {
    const selection = { assets: state.asset ? [state.asset] : [], exclude_induced: state.excludeInduced };
    Object.keys(state.selection).forEach((key) => {
      selection[key] = state.selection[key].slice();
    });
    return selection;
  }

  function popPreview() {
    const selection = selectionPayload();
    const parts = [];
    POP_KEYS.forEach(([key, label]) => {
      if (selection[key] && selection[key].length) parts.push(`${label}=${selection[key].slice().sort().join(",")}`);
    });
    if (state.excludeInduced) parts.push("induced=suspensions");
    parts.push("suspensions=other renewals");
    parts.push(`window=${state.dateFrom || "first-record"}..${state.dateTo || "cutoff"}`);
    return "POP: " + parts.join("; ");
  }

  function renderSelector() {
    const container = $("std-selector");
    container.innerHTML = "";
    const dimensions = (state.options && state.options.dimensions) || {};
    const lists = DIMENSIONS.map(([key, label]) => [key, label, dimensions[key] || []]);
    lists.push(["categories", "Category (pattern views)", ((state.options && state.options.categories) || []).map(
      (category) => ({ value: category.value, label: category.label, count: null })
    )]);
    lists.forEach(([key, label, values]) => {
      const box = el("div", { class: "std-multi", role: "group", "aria-label": label });
      if (!values.length) box.appendChild(el("span", { class: "std-multi-empty", text: "Nothing coded yet" }));
      values.forEach((option) => {
        const input = el("input", {
          type: "checkbox",
          value: option.value,
          checked: state.selection[key].includes(option.value),
          onchange: (event) => {
            const chosen = state.selection[key].filter((value) => value !== option.value);
            if (event.target.checked) chosen.push(option.value);
            state.selection[key] = chosen;
            selectionChanged();
          },
        });
        box.appendChild(el("label", {}, [
          input,
          el("span", { text: option.label }),
          option.count !== null && option.count !== undefined ? el("span", { class: "std-count", text: String(option.count) }) : null,
        ]));
      });
      container.appendChild(el("div", { class: "lda-field" }, [
        el("div", { class: "std-multi-head" }, [
          el("span", { class: "lda-field-label", text: label }),
          el("button", {
            type: "button",
            text: state.selection[key].length ? `Clear (${state.selection[key].length})` : "All",
            onclick: () => {
              state.selection[key] = [];
              renderSelector();
              selectionChanged();
            },
          }),
        ]),
        box,
      ]));
    });
    $("std-pop-preview").textContent = popPreview();
  }

  let selectionDebounce = null;
  function selectionChanged() {
    $("std-pop-preview").textContent = popPreview();
    clearWeibull("The population changed since the last fit. Fit again to see its lives.");
    clearTimeout(selectionDebounce);
    selectionDebounce = setTimeout(() => {
      renderSelector();
      loadPatterns();
    }, 250);
  }

  // ---- loading -----------------------------------------------------------------
  async function loadAsset(asset) {
    state.asset = asset;
    state.selection = emptySelection();
    state.patterns = null;
    clearWeibull("");
    ["std-population-card", "std-views-card", "std-weibull-card"].forEach((id) => {
      $(id).hidden = !asset;
    });
    if (!asset) return;
    beginLoading("Loading the population…");
    try {
      const options = await getJson(`${API}/population-options?asset=${encodeURIComponent(asset)}`);
      if (state.asset !== asset) return;
      state.options = options;
      clearBanner();
      const groupBy = $("std-group-by");
      groupBy.innerHTML = "";
      options.group_by.forEach((option) => groupBy.appendChild(el("option", { value: option.value, text: option.label })));
      groupBy.value = state.groupBy;
      renderSelector();
      await Promise.all([loadPatterns(), loadSaved()]);
    } catch (err) {
      showBanner(err.message, "error");
    } finally {
      endLoading();
    }
  }

  async function loadPatterns() {
    if (!state.asset) return;
    const token = ++state.token;
    try {
      const data = await postJson(`${API}/patterns`, {
        selection: selectionPayload(),
        date_from: state.dateFrom || null,
        date_to: state.dateTo || null,
        group_by: state.groupBy,
        period: state.period,
        top_n: state.topN,
      });
      if (token !== state.token) return;
      clearBanner();
      state.patterns = data;
      renderViews();
    } catch (err) {
      if (token === state.token) showBanner(err.message, "error");
    }
  }

  // ---- views -------------------------------------------------------------------
  function renderViews() {
    const data = state.patterns;
    if (!data) return;
    $("std-views-summary").textContent =
      `${data.record_count} work orders, ${fmt(data.downtime_hours, 1)} downtime hours in this population.`;
    ["pareto", "trend", "matrix", "records"].forEach((view) => {
      $(`std-view-${view}`).hidden = view !== state.view;
    });
    $("std-view-tabs").querySelectorAll("button").forEach((button) => {
      const active = button.dataset.view === state.view;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    if (state.view === "pareto") renderPareto();
    else if (state.view === "trend") renderTrend();
    else if (state.view === "matrix") renderMatrix();
    else renderRecords();
  }

  // Failure causes keep one colour across both Pareto charts and the legend.
  function causePalette(rows) {
    const totals = new Map();
    rows.forEach((row) => row.causes.forEach((cause) => {
      totals.set(cause.key, (totals.get(cause.key) || 0) + cause.count);
    }));
    const order = Array.from(totals.keys()).sort((a, b) => totals.get(b) - totals.get(a));
    const labels = new Map();
    rows.forEach((row) => row.causes.forEach((cause) => labels.set(cause.key, cause.label)));
    return {
      color(key) {
        if (key === null || key === undefined) return "var(--chart-axis)";
        return `var(--chart-type-${(order.indexOf(key) % 7) + 1})`;
      },
      entries: order.map((key) => [key, labels.get(key)]),
    };
  }

  function truncate(text, length) {
    const value = String(text ?? "");
    return value.length > length ? value.slice(0, length - 1) + "…" : value;
  }

  function horizontalBars(container, rows, { valueKey, palette, onClick, format, label }) {
    container.innerHTML = "";
    const sorted = rows.slice().sort((a, b) => b[valueKey] - a[valueKey]).filter((row) => row[valueKey] > 0);
    if (!sorted.length) {
      container.appendChild(el("p", { class: "lda-hint", text: "Nothing to show for this population." }));
      return;
    }
    const shown = sorted.slice(0, PARETO_MAX_BARS);
    const width = Math.max(320, container.clientWidth || 630);
    const labelWidth = Math.min(190, Math.round(width * 0.32));
    const barArea = width - labelWidth - 110;
    const rowHeight = 24;
    const height = shown.length * rowHeight + 8;
    const max = Math.max(...shown.map((row) => row[valueKey])) || 1;
    const total = sorted.reduce((sum, row) => sum + row[valueKey], 0) || 1;
    let cumulative = 0;
    const chart = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label });
    chart.appendChild(svg("line", { class: "std-axis", x1: labelWidth, x2: labelWidth, y1: 0, y2: height }));
    shown.forEach((row, index) => {
      const y = 4 + index * rowHeight;
      cumulative += row[valueKey];
      const clickable = Boolean(onClick) && row.key !== null && row.key !== undefined;
      const group = svg("g", {
        class: clickable ? "std-bar" : null,
        tabindex: clickable ? "0" : null,
        role: clickable ? "button" : null,
        "aria-label": `${row.label}: ${format(row[valueKey])}`,
        onclick: clickable ? () => onClick(row) : null,
        onkeydown: clickable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onClick(row);
              }
            }
          : null,
      });
      group.appendChild(svg("title", { text: `${row.label}: ${format(row[valueKey])}` }));
      group.appendChild(svg("text", { x: labelWidth - 6, y: y + 15, "text-anchor": "end", text: truncate(row.label, Math.floor(labelWidth / 6.5)) }));
      let x = labelWidth;
      row.causes.forEach((cause) => {
        const value = valueKey === "count" ? cause.count : cause.downtime_hours;
        if (!value) return;
        const w = (value / max) * barArea;
        const rect = svg("rect", { x, y: y + 3, width: Math.max(w, 0.5), height: rowHeight - 7, rx: 2, style: `fill: ${palette.color(cause.key)}` });
        rect.appendChild(svg("title", { text: `${row.label} · ${cause.label}: ${format(value)}` }));
        group.appendChild(rect);
        x += w;
      });
      group.appendChild(svg("text", {
        class: "std-value",
        x: x + 6,
        y: y + 15,
        text: `${format(row[valueKey])}  (${Math.round((cumulative / total) * 100)}%)`,
      }));
      chart.appendChild(group);
    });
    container.appendChild(chart);
    if (sorted.length > shown.length) {
      container.appendChild(el("p", { class: "lda-hint", text: `Showing the top ${shown.length} of ${sorted.length}.` }));
    }
  }

  function drill(row) {
    const pareto = state.patterns.pareto;
    const key = GROUP_TO_DIMENSION[pareto.group_by];
    if (!key) return;
    state.selection[key] = [row.key];
    if (pareto.next_group_by) {
      state.groupBy = pareto.next_group_by;
      $("std-group-by").value = state.groupBy;
    }
    renderSelector();
    selectionChanged();
  }

  function renderPareto() {
    const pareto = state.patterns.pareto;
    const palette = causePalette(pareto.rows);
    const clickable = Boolean(GROUP_TO_DIMENSION[pareto.group_by]);
    $("std-drill-hint").textContent = clickable
      ? "Click a bar to narrow the population to it" + (pareto.next_group_by ? ` and drill down to ${pareto.next_group_by.replace("_", " ")}.` : ".")
      : "These bars describe the population; narrow it with the lists above.";
    horizontalBars($("std-pareto-count"), pareto.rows, {
      valueKey: "count", palette, onClick: clickable ? drill : null, format: (value) => fmt(value, 0), label: `Work orders by ${pareto.label}`,
    });
    horizontalBars($("std-pareto-downtime"), pareto.rows, {
      valueKey: "downtime_hours", palette, onClick: clickable ? drill : null, format: (value) => fmt(value, 1), label: `Downtime by ${pareto.label}`,
    });
    const legend = $("std-pareto-legend");
    legend.innerHTML = "";
    if (palette.entries.length) legend.appendChild(el("span", { text: "Stacked by failure cause:" }));
    palette.entries.forEach(([key, label]) => {
      legend.appendChild(el("span", {}, [el("i", { style: `background: ${palette.color(key)}` }), label]));
    });
  }

  function renderTrend() {
    const trend = state.patterns.trend;
    const container = $("std-trend-chart");
    const legend = $("std-trend-legend");
    container.innerHTML = "";
    legend.innerHTML = "";
    if (!trend.periods.length) {
      container.appendChild(el("p", { class: "lda-hint", text: "No dated work orders in this population." }));
      return;
    }
    const left = 40;
    const bottom = 56;
    const top = 10;
    const available = Math.max(320, container.clientWidth || 640) - left - 10;
    const columnWidth = Math.max(14, Math.min(48, available / trend.periods.length));
    const width = left + trend.periods.length * columnWidth + 10;
    const height = 260;
    const plotHeight = height - top - bottom;
    const maxTotal = Math.max(1, ...trend.totals);
    const step = Math.max(1, Math.ceil(maxTotal / 5));
    const yMax = step * Math.ceil(maxTotal / step);
    const colorOf = (index, key) => (key === "__other__" ? "var(--chart-axis)" : `var(--chart-series-${(index % 7) + 1})`);
    const chart = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Work orders per period by component" });
    for (let tick = 0; tick <= yMax; tick += step) {
      const y = top + plotHeight - (tick / yMax) * plotHeight;
      chart.appendChild(svg("line", { class: "std-grid", x1: left, x2: width - 10, y1: y, y2: y }));
      chart.appendChild(svg("text", { x: left - 6, y: y + 4, "text-anchor": "end", text: String(tick) }));
    }
    const labelEvery = Math.ceil(trend.periods.length / 24);
    trend.periods.forEach((period, index) => {
      const x = left + index * columnWidth;
      let y = top + plotHeight;
      trend.series.forEach((series, seriesIndex) => {
        const value = series.counts[index];
        if (!value) return;
        const h = (value / yMax) * plotHeight;
        y -= h;
        const rect = svg("rect", { x: x + 2, y, width: columnWidth - 4, height: h, style: `fill: ${colorOf(seriesIndex, series.key)}` });
        rect.appendChild(svg("title", { text: `${period} · ${series.label}: ${value}` }));
        chart.appendChild(rect);
      });
      if (index % labelEvery === 0) {
        const lx = x + columnWidth / 2;
        const ly = top + plotHeight + 12;
        chart.appendChild(svg("text", { x: lx, y: ly, "text-anchor": "end", transform: `rotate(-45 ${lx} ${ly})`, text: period }));
      }
    });
    chart.appendChild(svg("line", { class: "std-axis", x1: left, x2: width - 10, y1: top + plotHeight, y2: top + plotHeight }));
    container.appendChild(chart);
    trend.series.forEach((series, index) => {
      legend.appendChild(el("span", {}, [el("i", { style: `background: ${colorOf(index, series.key)}` }), series.label]));
    });
    if (trend.undated) legend.appendChild(el("span", { text: `${trend.undated} undated work orders not shown.` }));
  }

  function renderMatrix() {
    const matrix = state.patterns.matrix;
    const table = $("std-matrix");
    table.innerHTML = "";
    if (!matrix.rows.length) {
      table.appendChild(el("tbody", {}, [el("tr", {}, [el("td", { text: "Nothing to show for this population." })])]));
      return;
    }
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { scope: "col", text: "Location" }),
      ...matrix.columns.map((column) => el("th", { scope: "col", title: column.label, text: truncate(column.label, 22) })),
      el("th", { scope: "col", text: "Total" }),
    ])]));
    const body = el("tbody");
    matrix.rows.forEach((row, rowIndex) => {
      const cells = matrix.columns.map((column, columnIndex) => {
        const cell = matrix.cells[rowIndex][columnIndex];
        if (!cell.count) return el("td", { class: "is-zero", text: "·" });
        const share = Math.round((cell.count / Math.max(1, matrix.max_count)) * 55) + 8;
        return el("td", {
          class: "has-count",
          tabindex: "0",
          title: `${row.label} · ${column.label}: ${cell.count} work orders, ${fmt(cell.downtime_hours, 1)} h downtime`,
          style: `background: color-mix(in srgb, var(--chart-accent) ${share}%, transparent)`,
          text: String(cell.count),
          onclick: () => narrowTo(row.key, column.key),
          onkeydown: (event) => {
            if (event.key === "Enter") narrowTo(row.key, column.key);
          },
        });
      });
      body.appendChild(el("tr", {}, [el("th", { scope: "row", text: row.label }), ...cells, el("td", { text: String(row.total) })]));
    });
    table.appendChild(body);
  }

  function narrowTo(location, component) {
    state.selection.locations = [location];
    if (component !== null && component !== undefined) state.selection.components = [component];
    state.groupBy = "failure_mechanism";
    $("std-group-by").value = state.groupBy;
    state.view = "pareto";
    renderSelector();
    selectionChanged();
  }

  function renderRecords() {
    const data = state.patterns;
    const table = $("std-records");
    table.innerHTML = "";
    const heads = ["Task", "Found", "Work order", "Category", "Location", "Component", "Mechanism", "Mode", "Cause", "Action", "Qty", "Downtime h"];
    table.appendChild(el("thead", {}, [el("tr", {}, heads.map((text) => el("th", { scope: "col", text })))]));
    const body = el("tbody");
    data.records.forEach((record) => {
      const task = G.CAN_EDIT
        ? el("a", {
            href: `/life-data-analysis/disposition/standard?asset=${encodeURIComponent(record.asset_number)}&search=${encodeURIComponent(record.task_id ?? "")}`,
            text: String(record.task_id ?? ""),
            title: "Open this work order's disposition",
          })
        : String(record.task_id ?? "");
      body.appendChild(el("tr", {}, [
        el("td", { class: "std-nowrap" }, [task]),
        el("td", { class: "std-nowrap", text: record.event_date || "" }),
        el("td", { class: "std-title", text: record.task_name || "" }),
        el("td", { text: record.category }),
        el("td", { class: "std-nowrap", text: record.location }),
        el("td", { text: record.component }),
        el("td", { text: record.failure_mechanism }),
        el("td", { text: record.failure_mode }),
        el("td", { text: record.failure_cause }),
        el("td", { text: record.action }),
        el("td", { text: record.qty ? String(record.qty) : "" }),
        el("td", { text: fmt(record.downtime_hours, 2) }),
      ]));
    });
    if (!data.records.length) body.appendChild(el("tr", {}, [el("td", { colspan: String(heads.length), text: "No work orders in this population." })]));
    table.appendChild(body);
    if (data.records_truncated) {
      body.appendChild(el("tr", {}, [el("td", { colspan: String(heads.length), class: "lda-readonly", text: "Showing the most recent 500." })]));
    }
  }

  // ---- Weibull -----------------------------------------------------------------
  function clearWeibull(message) {
    state.weibullResult = null;
    const container = $("std-weibull-result");
    container.innerHTML = "";
    if (message) container.appendChild(el("p", { class: "lda-hint", text: message }));
  }

  async function runWeibull(save) {
    if (!state.asset) return;
    const token = ++state.weibullToken;
    beginLoading(save ? "Fitting and saving…" : "Fitting…");
    try {
      const data = await postJson(`${API}/weibull${save ? "/save" : ""}`, {
        selection: selectionPayload(),
        analysis_start: state.dateFrom || null,
        analysis_cutoff: state.dateTo || null,
      });
      if (token !== state.weibullToken) return;
      renderWeibull(data.result);
      if (save) {
        showToast("Weibull analysis saved with its population definition.", "success");
        loadSaved();
      }
    } catch (err) {
      if (token === state.weibullToken) showBanner(err.message, "error");
    } finally {
      endLoading();
    }
  }

  function interval(lo, hi, digits) {
    return lo === null || lo === undefined || hi === null || hi === undefined ? "" : ` (95% CI ${fmt(lo, digits)}–${fmt(hi, digits)})`;
  }

  function fitCard(fit) {
    if (!fit) return null;
    const card = el("div", { class: "std-fit" }, [el("h3", { text: fit.label })]);
    if (!fit.fitted) {
      card.appendChild(el("p", { class: "lda-hint", text: fit.reason || "Not fitted." }));
      card.appendChild(el("p", { text: `${fit.failure_count} failures, ${fit.suspension_count} suspensions.` }));
      return card;
    }
    card.appendChild(el("p", { class: "std-params", text: `β = ${fmt(fit.beta, 3)}   η = ${fmt(fit.eta, 0)} h` }));
    const rows = [
      ["β", `${fmt(fit.beta, 3)}${interval(fit.beta_lower_ci, fit.beta_upper_ci, 3)}`],
      ["η", `${fmt(fit.eta, 0)} h${interval(fit.eta_lower_ci, fit.eta_upper_ci, 0)}`],
      ["Lives", `${fit.failure_count} failures, ${fit.suspension_count} suspensions`],
      ["MTTF", `${fmt(fit.mean_time_to_failure, 0)} h`],
      ["B10 / B50", `${fmt(fit.b10_life, 0)} h / ${fmt(fit.b50_life, 0)} h`],
      ["Plot R²", fit.probability_plot_r_squared === null || fit.probability_plot_r_squared === undefined
        ? "Not available (fewer than three failure points)"
        : `${fmt(fit.probability_plot_r_squared, 3)}${fit.probability_plot_review ? ` — below ${fmt(fit.probability_plot_r_squared_threshold, 3)}: review the population` : ""}`],
    ];
    card.appendChild(el("dl", {}, rows.flatMap(([label, value]) => [el("dt", { text: label }), el("dd", { text: value })])));
    const beta = fit.beta;
    card.appendChild(el("p", {
      class: "lda-hint",
      text: beta < 0.9 ? "β below 1: early-life failures — look at installation and repair quality before PM intervals."
        : beta <= 1.1 ? "β near 1: random failures — an age-based replacement is weakly justified."
          : "β above 1: wear-out — an age-based PM or planned replacement is worth evaluating.",
    }));
    return card;
  }

  function probabilityPlot(fits) {
    const fitted = fits.filter((fit) => fit && fit.fitted);
    if (!fitted.length) return null;
    const points = fitted[0].km_points.filter((point) => point.weibull_plot_y !== null && point.weibull_plot_y !== undefined);
    if (!points.length) return null;
    const xs = points.map((point) => point.weibull_plot_x);
    const ys = points.map((point) => point.weibull_plot_y);
    const xMin = Math.min(...xs) - 0.3;
    const xMax = Math.max(...xs, ...fitted.map((fit) => Math.log(fit.eta))) + 0.3;
    const yMin = Math.min(...ys, -4) - 0.2;
    const yMax = Math.max(...ys, 1) + 0.2;
    const width = Math.max(320, Math.min(640, $("std-weibull-result").clientWidth || 560));
    const height = 280;
    const left = 46;
    const bottom = 34;
    const sx = (x) => left + ((x - xMin) / (xMax - xMin)) * (width - left - 10);
    const sy = (y) => 10 + (1 - (y - yMin) / (yMax - yMin)) * (height - bottom - 10);
    const chart = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Weibull probability plot" });
    chart.appendChild(svg("line", { class: "std-axis", x1: left, x2: width - 10, y1: height - bottom, y2: height - bottom }));
    chart.appendChild(svg("line", { class: "std-axis", x1: left, x2: left, y1: 10, y2: height - bottom }));
    chart.appendChild(svg("text", { x: (left + width) / 2, y: height - 6, "text-anchor": "middle", text: "ln(life hours)" }));
    chart.appendChild(svg("text", { x: 12, y: (height - bottom) / 2, transform: `rotate(-90 12 ${(height - bottom) / 2})`, "text-anchor": "middle", text: "ln(−ln R)" }));
    [Math.ceil(xMin), Math.floor(xMax)].forEach((tick) => {
      chart.appendChild(svg("text", { x: sx(tick), y: height - bottom + 14, "text-anchor": "middle", text: String(tick) }));
    });
    fitted.forEach((fit, index) => {
      const line = (x) => fit.beta * (x - Math.log(fit.eta));
      chart.appendChild(svg("line", {
        x1: sx(xMin), y1: sy(line(xMin)), x2: sx(xMax), y2: sy(line(xMax)),
        style: `stroke: ${index ? "var(--chart-highlight)" : "var(--chart-brand)"}; stroke-width: 2; ${index ? "stroke-dasharray: 6 4;" : ""}`,
      }));
    });
    points.forEach((point) => {
      const circle = svg("circle", {
        cx: sx(point.weibull_plot_x), cy: sy(point.weibull_plot_y), r: 4,
        style: "fill: var(--chart-point-fill); stroke: var(--chart-danger); stroke-width: 2",
      });
      circle.appendChild(svg("title", { text: `${fmt(point.life_hours, 0)} h, F = ${fmt(point.cdf_estimate * 100, 1)}%` }));
      chart.appendChild(circle);
    });
    return el("div", { class: "std-chart" }, [
      el("h3", { text: "Probability plot" }),
      chart,
      el("p", { class: "lda-hint", text: fitted.length > 1 ? "Points: failures (Kaplan-Meier). Solid: unknown mechanisms as suspensions; dashed: as failures." : "Points: failures (Kaplan-Meier). Line: the fit." }),
    ]);
  }

  function tableOf(heads, rows) {
    return el("div", { class: "lda-table-scroll" }, [el("table", { class: "lda-table" }, [
      el("thead", {}, [el("tr", {}, heads.map((text) => el("th", { scope: "col", text })))]),
      el("tbody", {}, rows),
    ])]);
  }

  function renderWeibull(result) {
    state.weibullResult = result;
    const container = $("std-weibull-result");
    container.innerHTML = "";
    container.appendChild(el("div", { class: "std-pop-string", text: result.population_definition }));
    const saved = result.analysis_id
      ? ` Saved as analysis ${result.analysis_id}${result.run_by ? ` by ${result.run_by}` : ""}${result.run_at ? ` at ${result.run_at} UTC` : ""}.`
      : " Not saved.";
    container.appendChild(el("p", {
      class: "lda-hint",
      text: `Window ${result.window_start_day || "first record"} to ${result.window_end_day} (cutoff: ${
        { USER: "the To date", LAST_IMPORT: "the last Limble import", NOW: "the time of the run" }[result.cutoff_source] || result.cutoff_source
      }). ${result.life_basis}, days split in ${result.time_zone}. Method ${result.method_version}.${saved}`,
    }));
    if (result.method_current === false) {
      container.appendChild(el("div", { class: "std-warning", text: "Saved under an earlier method version: fit it again before relying on it." }));
    }
    if (result.warnings.length) {
      container.appendChild(el("div", { class: "std-warnings" }, result.warnings.map((warning) =>
        el("div", { class: "std-warning" + (warning.level === "info" ? " is-info" : "") }, [el("strong", { text: warning.title }), warning.message])
      )));
    }
    const fits = [result.fit, result.sensitivity_fit].filter(Boolean);
    container.appendChild(el("div", { class: "std-fits" }, fits.map(fitCard)));
    const plot = probabilityPlot(fits);
    if (plot) container.appendChild(plot);

    container.appendChild(el("h3", { text: "Renewal streams pooled" }));
    container.appendChild(tableOf(
      ["Location", "Component", "Failures", "Suspensions", "Schedule", "Note"],
      result.streams.length
        ? result.streams.map((stream) => el("tr", {}, [
            el("td", { text: stream.location }),
            el("td", { text: stream.component_name && stream.component_name !== stream.component ? `${stream.component} · ${stream.component_name}` : stream.component }),
            el("td", { text: String(stream.failures) }),
            el("td", { text: String(stream.suspensions) }),
            el("td", { text: stream.schedule_name || "" }),
            el("td", { text: stream.recurrent ? "Failures fixed without renewal: consider Crow-AMSAA" : "" }),
          ]))
        : [el("tr", {}, [el("td", { colspan: "6", text: "No failure or PM reset in this population names a component in it." })])]
    ));

    container.appendChild(el("h3", { text: "Lives" }));
    const lifeRows = (intervals) => intervals.map((life) => el("tr", {}, [
      el("td", { class: "std-nowrap", text: `${life.location} | ${life.component}` }),
      el("td", { class: "std-nowrap", text: `${day(life.start)} → ${day(life.end)}` }),
      el("td", { text: fmt(life.life_hours, 1) }),
      el("td", { text: fmt(life.raw_days, 1) }),
      el("td", { class: life.end_type === "FAILURE" ? "std-failure" : "std-suspension", text: life.end_type }),
      el("td", { text: life.start_task_id ? String(life.start_task_id) : "" }),
      el("td", { text: life.end_task_id ? String(life.end_task_id) : "cutoff" }),
      el("td", { text: life.note }),
    ]));
    const lifeHeads = ["Stream", "From → to", "Life hours", "Calendar days", "Ends as", "Started by", "Ended by", "Why"];
    container.appendChild(result.intervals.length
      ? tableOf(lifeHeads, lifeRows(result.intervals))
      : el("p", { class: "lda-hint", text: "No lives: no item in this population has been renewed and then seen again." }));
    if (result.sensitivity_intervals && result.sensitivity_intervals.length) {
      container.appendChild(el("details", {}, [
        el("summary", { text: "Lives with unknown mechanisms counted as failures" }),
        tableOf(lifeHeads, lifeRows(result.sensitivity_intervals)),
      ]));
    }
    container.appendChild(el("details", {}, [
      el("summary", { text: `What each work order did, stream by stream (${result.events.length})` }),
      tableOf(["Stream", "Date", "Task", "Effect", "Note"], result.events.map((event) => el("tr", {}, [
        el("td", { class: "std-nowrap", text: `${event.location} | ${event.component}` }),
        el("td", { class: "std-nowrap", text: day(event.when) }),
        el("td", { text: event.task_id ? String(event.task_id) : String(event.record_id) }),
        el("td", { text: event.role.replace("_", " ") }),
        el("td", { text: event.note }),
      ]))),
    ]));
  }

  async function loadSaved() {
    if (!state.asset) return;
    const table = $("std-saved");
    try {
      const data = await getJson(`${API}/weibull/saved?asset=${encodeURIComponent(state.asset)}`);
      table.innerHTML = "";
      const rows = data.analyses.map((analysis) => el("tr", {}, [
        el("td", { class: "std-nowrap", text: analysis.run_at || "" }),
        el("td", { text: analysis.population_definition }),
        el("td", { text: analysis.failure_count === null ? "" : `${analysis.failure_count} / ${analysis.suspension_count}` }),
        el("td", { text: analysis.beta === null ? "not fitted" : `β ${fmt(analysis.beta, 3)}, η ${fmt(analysis.eta, 0)} h` }),
        el("td", { text: analysis.run_by || "" }),
        el("td", {}, [el("button", {
          type: "button",
          class: "btn-secondary",
          text: "Open",
          onclick: async () => {
            try {
              const saved = await getJson(`${API}/weibull/saved/${analysis.analysis_id}`);
              renderWeibull(saved.result);
              $("std-weibull-result").scrollIntoView({ behavior: "smooth", block: "start" });
            } catch (err) {
              showBanner(err.message, "error");
            }
          },
        })]),
      ]));
      table.appendChild(el("thead", {}, [el("tr", {}, ["Run at (UTC)", "Population", "Failures / suspensions", "Fit", "By", ""].map(
        (text) => el("th", { scope: "col", text })
      ))]));
      table.appendChild(el("tbody", {}, rows.length ? rows : [el("tr", {}, [el("td", { colspan: "6", text: "No saved analyses read this asset yet." })])]));
    } catch (err) {
      table.innerHTML = "";
    }
  }

  // ---- start -------------------------------------------------------------------
  function init() {
    const combobox = G.assetCombobox({
      input: $("lda-asset"),
      list: $("lda-asset-list"),
      hint: $("lda-asset-hint"),
      onChoose: loadAsset,
    });
    combobox.load();
    let dateDebounce = null;
    const onDate = () => {
      state.dateFrom = $("std-date-from").value || "";
      state.dateTo = $("std-date-to").value || "";
      $("std-pop-preview").textContent = popPreview();
      clearTimeout(dateDebounce);
      dateDebounce = setTimeout(loadPatterns, 300);
    };
    $("std-date-from").addEventListener("change", onDate);
    $("std-date-to").addEventListener("change", onDate);
    $("std-exclude-induced").addEventListener("change", (event) => {
      state.excludeInduced = event.target.checked;
      selectionChanged();
    });
    $("std-group-by").addEventListener("change", (event) => {
      state.groupBy = event.target.value;
      loadPatterns();
    });
    $("std-period").addEventListener("change", (event) => {
      state.period = event.target.value;
      loadPatterns();
    });
    $("std-top-n").addEventListener("change", (event) => {
      state.topN = Number(event.target.value);
      loadPatterns();
    });
    $("std-view-tabs").querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => {
        state.view = button.dataset.view;
        renderViews();
      });
    });
    $("std-weibull-preview").addEventListener("click", () => runWeibull(false));
    if ($("std-weibull-save")) $("std-weibull-save").addEventListener("click", () => runWeibull(true));
    // The charts are drawn at their container's pixel width, so a new width
    // wants them drawn again.
    let resizeDebounce = null;
    let lastWidth = window.innerWidth;
    window.addEventListener("resize", () => {
      if (window.innerWidth === lastWidth) return;
      lastWidth = window.innerWidth;
      clearTimeout(resizeDebounce);
      resizeDebounce = setTimeout(() => {
        if (state.patterns) renderViews();
        if (state.weibullResult) renderWeibull(state.weibullResult);
      }, 200);
    });
  }

  init();
})();
