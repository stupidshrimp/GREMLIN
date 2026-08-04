/* Developer dashboard client.
 *
 * Six panels over the /developer/api/* endpoints:
 *   - Overview      : database file facts + how this Flask process is configured
 *   - Pipeline      : row counts down the ingestion -> Weibull path, recent imports
 *   - Schema        : the live catalogue, table by table (columns, FKs, indexes, DDL)
 *   - Drift         : tables/columns that exist in the code but not the file, or vice versa
 *   - Data browser  : paginated rows from one table
 *   - SQL console   : a single read-only statement
 *
 * Panels fetch lazily on first activation and cache the result, so switching tabs
 * stays instant and the shared database is not re-read on every click. Every
 * endpoint is read-only server-side; nothing here can modify GREMLIN.db.
 */
(function () {
  "use strict";

  const API = {
    overview: "/developer/api/overview",
    runtime: "/developer/api/runtime",
    pipeline: "/developer/api/pipeline",
    drift: "/developer/api/drift",
    tables: "/developer/api/tables",
    table: (name) => `/developer/api/tables/${encodeURIComponent(name)}`,
    rows: (name) => `/developer/api/tables/${encodeURIComponent(name)}/rows`,
    query: "/developer/api/query",
  };

  const state = {
    loaded: new Set(), // panel keys already fetched
    tables: [], // [{name, type, origin, note, row_count, column_count}]
    selectedTable: null,
    data: { table: null, offset: 0, limit: 50, total: null },
  };

  const $ = (id) => document.getElementById(id);

  // --- helpers ------------------------------------------------------------

  function showStatus(message, isError) {
    const banner = $("dev-status");
    if (!banner) return;
    if (!message) {
      banner.hidden = true;
      banner.textContent = "";
      return;
    }
    banner.hidden = false;
    banner.textContent = message;
    banner.classList.toggle("is-error", Boolean(isError));
  }

  async function getJSON(url, options) {
    const response = await fetch(url, options);
    let payload = null;
    try {
      payload = await response.json();
    } catch (err) {
      throw new Error(`Server returned a non-JSON response (${response.status}).`);
    }
    if (!response.ok) {
      // A 403 means the PIN session expired; reloading shows the lock screen.
      if (response.status === 403) {
        window.location.reload();
      }
      throw new Error((payload && payload.error) || `Request failed (${response.status}).`);
    }
    return payload;
  }

  // Text-only rendering: every value from the database goes through
  // textContent, never innerHTML, so stored content cannot inject markup.
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function buildTable(columns, rows, options) {
    const settings = options || {};
    const table = el("table", "dev-data-table");
    const thead = el("thead");
    const headRow = el("tr");
    columns.forEach((column) => headRow.appendChild(el("th", null, column)));
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = el("tbody");
    rows.forEach((row) => {
      const tr = el("tr");
      row.forEach((value) => {
        const td = el("td");
        if (value === null || value === undefined) {
          td.appendChild(el("span", "dev-null", "NULL"));
        } else {
          td.textContent = String(value);
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    if (!rows.length) {
      const empty = el("p", "dev-muted", settings.emptyText || "No rows.");
      const wrapper = el("div");
      wrapper.appendChild(table);
      wrapper.appendChild(empty);
      return wrapper;
    }
    return table;
  }

  function replaceChildren(container, node) {
    container.textContent = "";
    if (node) container.appendChild(node);
  }

  function factRow(list, label, value) {
    list.appendChild(el("dt", null, label));
    list.appendChild(el("dd", null, value === null || value === undefined || value === "" ? "—" : value));
  }

  function formatBytes(bytes) {
    if (typeof bytes !== "number") return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
  }

  function formatNumber(value) {
    return typeof value === "number" ? value.toLocaleString() : "—";
  }

  // --- Overview -----------------------------------------------------------

  async function loadOverview() {
    const [overview, runtime] = await Promise.all([getJSON(API.overview), getJSON(API.runtime)]);

    const dbFacts = el("dl", "dev-facts");
    factRow(dbFacts, "Path", overview.db_path);
    factRow(dbFacts, "Exists", overview.exists ? "Yes" : "No");
    if (overview.error) {
      factRow(dbFacts, "Error", overview.error);
    } else {
      factRow(dbFacts, "Size", formatBytes(overview.size_bytes));
      factRow(dbFacts, "Last modified", overview.modified_at);
      factRow(dbFacts, "SQLite version", overview.sqlite_version);
      factRow(dbFacts, "Journal mode", overview.journal_mode);
      factRow(dbFacts, "Encoding", overview.encoding);
      factRow(dbFacts, "Page size", formatNumber(overview.page_size));
      factRow(dbFacts, "Page count", formatNumber(overview.page_count));
      const counts = overview.object_counts || {};
      factRow(dbFacts, "Tables", formatNumber(counts.table || 0));
      factRow(dbFacts, "Indexes", formatNumber(counts.index || 0));
      factRow(dbFacts, "Views", formatNumber(counts.view || 0));
      factRow(dbFacts, "Triggers", formatNumber(counts.trigger || 0));
    }
    dbFacts.id = "dev-overview-facts";
    $("dev-overview-facts").replaceWith(dbFacts);

    const runtimeFacts = el("dl", "dev-facts");
    factRow(runtimeFacts, "Python", runtime.python_version);
    factRow(runtimeFacts, "Flask", runtime.flask_version);
    factRow(runtimeFacts, "Database path source", runtime.db_path_source);
    factRow(runtimeFacts, "Default path", runtime.default_db_path);
    factRow(runtimeFacts, "Secret key", runtime.secret_key_source);
    factRow(runtimeFacts, "LifeDataService", runtime.life_data_service_status);
    if (runtime.life_data_service_error) {
      factRow(runtimeFacts, "Service error", runtime.life_data_service_error);
    }
    factRow(runtimeFacts, "Routes", formatNumber(runtime.route_count));
    runtimeFacts.id = "dev-runtime-facts";
    $("dev-runtime-facts").replaceWith(runtimeFacts);

    replaceChildren(
      $("dev-routes"),
      buildTable(
        ["Rule", "Methods", "Endpoint"],
        (runtime.routes || []).map((route) => [route.rule, route.methods.join(", "), route.endpoint])
      )
    );
  }

  // --- Pipeline -----------------------------------------------------------

  async function loadPipeline() {
    const payload = await getJSON(API.pipeline);
    const container = $("dev-pipeline-stages");
    container.textContent = "";

    (payload.stages || []).forEach((stage) => {
      const row = el("div", "dev-stage");
      const label = el("div", "dev-stage-label");
      label.appendChild(el("strong", null, stage.label));
      label.appendChild(el("code", null, stage.table));
      row.appendChild(label);

      if (!stage.present) {
        row.appendChild(el("span", "dev-pill dev-pill-warn", "table missing"));
      } else {
        const count = stage.row_count || 0;
        row.appendChild(
          el("span", count === 0 ? "dev-pill dev-pill-warn" : "dev-pill", `${formatNumber(count)} rows`)
        );
      }
      container.appendChild(row);
    });

    const batches = payload.recent_batches || [];
    const columns = batches.length ? Object.keys(batches[0]) : ["import_batch_id", "status"];
    replaceChildren(
      $("dev-batches"),
      buildTable(
        columns,
        batches.map((batch) => columns.map((column) => batch[column])),
        { emptyText: "No import batches recorded." }
      )
    );
  }

  // --- Schema -------------------------------------------------------------

  function originPill(origin) {
    if (origin === "code") return el("span", "dev-pill dev-pill-ok", "in code");
    if (origin === "internal") return el("span", "dev-pill dev-pill-muted", "sqlite");
    return el("span", "dev-pill dev-pill-warn", "orphaned");
  }

  function renderTableList() {
    const filter = ($("dev-table-filter").value || "").trim().toLowerCase();
    const container = $("dev-table-list");
    container.textContent = "";

    const matches = state.tables.filter((table) => !filter || table.name.toLowerCase().includes(filter));
    if (!matches.length) {
      container.appendChild(el("p", "dev-muted", "No tables match that filter."));
      return;
    }

    matches.forEach((table) => {
      const button = el("button", "dev-table-item");
      button.type = "button";
      if (table.name === state.selectedTable) button.classList.add("is-active");
      // Name on its own line: table names are long and the sidebar column is
      // narrow, so sharing the line with the pill forced mid-word wrapping.
      button.appendChild(el("span", "dev-table-name", table.name));
      const meta = el("div", "dev-table-item-meta");
      meta.appendChild(originPill(table.origin));
      meta.appendChild(
        el("span", "dev-muted", `${formatNumber(table.row_count)} rows · ${table.column_count} cols`)
      );
      button.appendChild(meta);
      button.addEventListener("click", () => selectTable(table.name));
      container.appendChild(button);
    });
  }

  async function selectTable(name) {
    state.selectedTable = name;
    renderTableList();
    const container = $("dev-table-detail");
    replaceChildren(container, el("p", "dev-muted", "Loading…"));
    try {
      const detail = await getJSON(API.table(name));
      container.textContent = "";

      const heading = el("div", "dev-detail-head");
      heading.appendChild(el("h2", null, detail.name));
      heading.appendChild(originPill(detail.origin));
      container.appendChild(heading);
      if (detail.note) container.appendChild(el("p", "dev-muted", detail.note));
      container.appendChild(el("p", "dev-muted", `${formatNumber(detail.row_count)} rows`));

      const drift = detail.drift || {};
      if ((drift.missing_in_file || []).length || (drift.extra_in_file || []).length) {
        const box = el("div", "dev-callout");
        if (drift.missing_in_file.length) {
          box.appendChild(el("p", null, `Columns in code but not in this file: ${drift.missing_in_file.join(", ")}`));
        }
        if (drift.extra_in_file.length) {
          box.appendChild(el("p", null, `Columns in this file but not in code: ${drift.extra_in_file.join(", ")}`));
        }
        container.appendChild(box);
      }

      container.appendChild(el("h3", null, "Columns"));
      const columnScroll = el("div", "dev-table-scroll");
      columnScroll.appendChild(
        buildTable(
          ["#", "Name", "Type", "Not null", "Default", "PK"],
          detail.columns.map((column) => [
            column.position,
            column.name,
            column.type || "—",
            column.not_null ? "yes" : "",
            column.default === null ? null : column.default,
            column.primary_key ? "yes" : "",
          ])
        )
      );
      container.appendChild(columnScroll);

      container.appendChild(el("h3", null, "Foreign keys"));
      const fkScroll = el("div", "dev-table-scroll");
      fkScroll.appendChild(
        buildTable(
          ["Column", "References", "On update", "On delete"],
          detail.foreign_keys.map((fk) => [
            fk.column,
            `${fk.references_table}(${fk.references_column})`,
            fk.on_update,
            fk.on_delete,
          ]),
          { emptyText: "No foreign keys." }
        )
      );
      container.appendChild(fkScroll);

      container.appendChild(el("h3", null, "Indexes"));
      const indexScroll = el("div", "dev-table-scroll");
      indexScroll.appendChild(
        buildTable(
          ["Name", "Unique", "Columns"],
          detail.indexes.map((index) => [index.name, index.unique ? "yes" : "", index.columns.join(", ")]),
          { emptyText: "No indexes." }
        )
      );
      container.appendChild(indexScroll);

      if (detail.ddl) {
        container.appendChild(el("h3", null, "DDL"));
        const pre = el("pre", "dev-ddl");
        pre.appendChild(el("code", null, detail.ddl));
        container.appendChild(pre);
      }
    } catch (err) {
      replaceChildren(container, el("p", "dev-error", err.message));
    }
  }

  async function loadTables() {
    const payload = await getJSON(API.tables);
    state.tables = payload.tables || [];
    renderTableList();
    populateDataTableSelect();
  }

  // --- Drift --------------------------------------------------------------

  async function loadDrift() {
    const payload = await getJSON(API.drift);
    const container = $("dev-drift");
    container.textContent = "";

    if (!payload.available) {
      container.appendChild(el("p", "dev-error", payload.error || "Drift report unavailable."));
      return;
    }

    container.appendChild(
      el(
        "p",
        "dev-muted",
        `Current code defines ${payload.reference_table_count} tables; this file has ${payload.live_table_count}.`
      )
    );

    const sections = [
      ["Defined in code, missing from this file", payload.missing_tables, "dev-pill-warn"],
      ["In this file, not defined by current code", payload.extra_tables, "dev-pill-warn"],
    ];
    sections.forEach(([title, entries, pillClass]) => {
      container.appendChild(el("h3", null, title));
      if (!entries.length) {
        container.appendChild(el("p", "dev-muted", "None."));
        return;
      }
      const list = el("ul", "dev-drift-list");
      entries.forEach((entry) => {
        const item = el("li");
        item.appendChild(el("code", null, entry.name));
        if (entry.note) item.appendChild(el("span", "dev-muted", ` ${entry.note}`));
        list.appendChild(item);
      });
      container.appendChild(list);
    });

    container.appendChild(el("h3", null, "Column differences"));
    if (!payload.column_drift.length) {
      container.appendChild(el("p", "dev-muted", "Every shared table has the columns the code expects."));
      return;
    }
    const scroll = el("div", "dev-table-scroll");
    scroll.appendChild(
      buildTable(
        ["Table", "Missing in file", "Extra in file"],
        payload.column_drift.map((entry) => [
          entry.table,
          entry.missing_in_file.join(", ") || "—",
          entry.extra_in_file.join(", ") || "—",
        ])
      )
    );
    container.appendChild(scroll);
  }

  // --- Data browser -------------------------------------------------------

  function populateDataTableSelect() {
    const select = $("dev-data-table");
    const current = select.value;
    select.textContent = "";
    state.tables.forEach((table) => {
      const option = el("option", null, `${table.name} (${formatNumber(table.row_count)})`);
      option.value = table.name;
      select.appendChild(option);
    });
    if (current && state.tables.some((table) => table.name === current)) {
      select.value = current;
    }
  }

  async function loadRows() {
    const table = $("dev-data-table").value;
    if (!table) return;
    state.data.table = table;
    state.data.limit = parseInt($("dev-data-limit").value, 10) || 50;

    const params = new URLSearchParams({
      limit: String(state.data.limit),
      offset: String(state.data.offset),
    });
    try {
      const payload = await getJSON(`${API.rows(table)}?${params.toString()}`);
      state.data.total = payload.total_rows;
      const first = payload.total_rows === 0 ? 0 : payload.offset + 1;
      const last = payload.offset + payload.rows.length;
      $("dev-data-summary").textContent =
        `Showing ${formatNumber(first)}–${formatNumber(last)} of ${formatNumber(payload.total_rows)} rows.`;
      replaceChildren($("dev-data-rows"), buildTable(payload.columns, payload.rows));
      $("dev-data-prev").disabled = payload.offset <= 0;
      $("dev-data-next").disabled = last >= (payload.total_rows || 0);
    } catch (err) {
      $("dev-data-summary").textContent = "";
      replaceChildren($("dev-data-rows"), el("p", "dev-error", err.message));
    }
  }

  // --- SQL console --------------------------------------------------------

  async function runQuery() {
    const sql = $("dev-sql").value;
    const meta = $("dev-sql-meta");
    meta.textContent = "Running…";
    try {
      const payload = await getJSON(API.query, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sql }),
      });
      const truncated = payload.truncated ? ` (capped at ${payload.max_rows})` : "";
      meta.textContent = `${formatNumber(payload.row_count)} rows in ${payload.elapsed_ms} ms${truncated}`;
      replaceChildren($("dev-sql-results"), buildTable(payload.columns, payload.rows));
    } catch (err) {
      meta.textContent = "";
      replaceChildren($("dev-sql-results"), el("p", "dev-error", err.message));
    }
  }

  // --- Tabs / bootstrap ---------------------------------------------------

  const LOADERS = {
    overview: loadOverview,
    pipeline: loadPipeline,
    schema: loadTables,
    drift: loadDrift,
    data: async () => {
      if (!state.tables.length) await loadTables();
      state.data.offset = 0;
      await loadRows();
    },
    console: async () => {},
  };

  async function activate(key) {
    document.querySelectorAll(".dev-tab").forEach((tab) => {
      const active = tab.dataset.tab === key;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".dev-panel").forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.panel === key);
    });

    if (state.loaded.has(key)) return;
    showStatus("");
    try {
      await LOADERS[key]();
      state.loaded.add(key);
    } catch (err) {
      showStatus(err.message, true);
    }
  }

  function init() {
    document.querySelectorAll(".dev-tab").forEach((tab) => {
      tab.addEventListener("click", () => activate(tab.dataset.tab));
    });
    $("dev-table-filter").addEventListener("input", renderTableList);
    $("dev-run-sql").addEventListener("click", runQuery);
    $("dev-sql").addEventListener("keydown", (event) => {
      // Ctrl/Cmd + Enter runs, matching most SQL consoles.
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        runQuery();
      }
    });
    $("dev-data-table").addEventListener("change", () => {
      state.data.offset = 0;
      loadRows();
    });
    $("dev-data-limit").addEventListener("change", () => {
      state.data.offset = 0;
      loadRows();
    });
    $("dev-data-prev").addEventListener("click", () => {
      state.data.offset = Math.max(0, state.data.offset - state.data.limit);
      loadRows();
    });
    $("dev-data-next").addEventListener("click", () => {
      state.data.offset += state.data.limit;
      loadRows();
    });

    activate("overview");
  }

  // This script tag sits at the end of <body>, so every element it touches is
  // already parsed by the time it runs. Waiting on DOMContentLoaded instead
  // would couple the dashboard to base.html's external Google Fonts stylesheet:
  // a browser holds readyState at "loading" until a pending stylesheet resolves,
  // so on a network that cannot reach fonts.googleapis.com the page would never
  // initialise. Only fall back to the event if the DOM really is not ready yet.
  if (document.querySelector(".dev-tabs")) {
    init();
  } else {
    document.addEventListener("DOMContentLoaded", init);
  }
})();
