/* Database inspection page.
 *
 * Six panels over the read-only /developer/api/* endpoints:
 *   - Overview      : database file facts + how this Flask process is configured
 *   - Pipeline      : row counts down the ingestion -> Weibull path, recent imports
 *   - Schema        : the live catalogue, table by table (columns, FKs, indexes, DDL)
 *   - Drift         : tables/columns that exist in the code but not the file, or vice versa
 *   - Data browser  : paginated rows from one table
 *   - SQL console   : a single read-only statement
 *
 * Panels fetch lazily on first activation and cache the result, so switching
 * tabs stays instant and the shared database is not re-read on every click.
 * Nothing here writes: every endpoint this page calls is read-only server-side.
 */
(function () {
  "use strict";

  const dev = window.GremlinDev;
  const $ = dev.$;
  const el = dev.el;
  const getJSON = dev.getJSON;
  const buildTable = dev.buildTable;
  const replaceChildren = dev.replaceChildren;
  const factRow = dev.factRow;
  const formatBytes = dev.formatBytes;
  const formatNumber = dev.formatNumber;

  const state = {
    loaded: new Set(), // panel keys already fetched
    // `panelGeneration` counts invalidations; `panelLoads` holds the load in
    // flight for each panel, so a second load queues behind the first instead
    // of racing it.
    panelGeneration: 0,
    panelLoads: new Map(),
    tables: [], // [{name, type, origin, note, row_count, column_count}]
    selectedTable: null,
    // `history` records the offset each page started at. Pages can be shorter
    // than `limit` when the server's byte budget ends one early, so neither
    // direction can be navigated by arithmetic on `limit`.
    data: { table: null, offset: 0, limit: 50, total: null, nextOffset: null, history: [] },
    // The last completed sync whose changes these panels already account for;
    // undefined until the first status answer. See checkForCompletedSync.
    lastWritingRun: undefined,
  };

  // --- Overview -----------------------------------------------------------

  async function loadOverview() {
    const [overview, runtime] = await Promise.all([getJSON(dev.API.overview), getJSON(dev.API.runtime)]);

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
    const payload = await getJSON(dev.API.pipeline);
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

  // --- Staleness ----------------------------------------------------------
  //
  // These panels cache what they read because only a sync changes the database
  // underneath them. A sync now runs on its own page, so the realistic way this
  // page goes stale is a sync run in another tab while this one sat in the
  // background -- which is exactly when this page stops being visible and comes
  // back. Asking on that transition (and once at load, to establish the
  // baseline) costs nothing while the page is being used and catches the case
  // that matters. Navigating here after a sync needs no help at all: a fresh
  // document has no cache to invalidate.

  function refreshPanels() {
    state.panelGeneration += 1;
    ["overview", "pipeline", "schema", "drift", "data"].forEach((key) => state.loaded.delete(key));
    const active = document.querySelector(".dev-tab.is-active");
    if (active) activate(active.dataset.tab);
    // Reloading the Schema panel rebuilds the table *list*; an open table's
    // detail is drawn separately and would sit there showing the row count and
    // columns from before the import, which mapping can genuinely change.
    if (state.selectedTable) selectTable(state.selectedTable);
  }

  async function checkForCompletedSync() {
    let payload;
    try {
      payload = await getJSON(dev.API.sync);
    } catch (err) {
      // Losing sight of syncs is not worth a banner on a page that is otherwise
      // working; the panels are still showing what they last read.
      return;
    }
    const runId = dev.writingRunId(payload.job);
    if (state.lastWritingRun === undefined) {
      // The first answer of this page load is the baseline: every panel read
      // after it is already reading post-sync data, so there is nothing to drop.
      state.lastWritingRun = runId;
      return;
    }
    if (runId && runId !== state.lastWritingRun) {
      state.lastWritingRun = runId;
      refreshPanels();
    }
  }

  // --- Schema -------------------------------------------------------------

  function originPill(origin) {
    if (origin === "code") return el("span", "dev-pill dev-pill-ok", "in code");
    if (origin === "internal") return el("span", "dev-pill dev-pill-muted", "sqlite");
    // "unknown" means the reference schema could not be built, so no comparison
    // was possible -- not that the table is orphaned.
    if (origin === "unknown") return el("span", "dev-pill dev-pill-muted", "not compared");
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
    // This detail is fetched outside the panel loaders, so it carries its own
    // copy of the invalidation era: a sync that finished mid-request would
    // otherwise be answered with columns and a row count from before it.
    const generation = state.panelGeneration;
    try {
      const detail = await getJSON(dev.API.table(name));
      if (generation !== state.panelGeneration) return;
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
    const payload = await getJSON(dev.API.tables);
    state.tables = payload.tables || [];
    renderTableList();
    populateDataTableSelect();
  }

  // --- Drift --------------------------------------------------------------

  async function loadDrift() {
    const payload = await getJSON(dev.API.drift);
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
      const payload = await getJSON(`${dev.API.rows(table)}?${params.toString()}`);
      state.data.total = payload.total_rows;
      state.data.nextOffset = payload.next_offset;
      const first = payload.total_rows === 0 ? 0 : payload.offset + 1;
      const last = payload.offset + payload.rows.length;
      const short = payload.rows.length < payload.limit && payload.has_more;
      $("dev-data-summary").textContent =
        `Showing ${formatNumber(first)}–${formatNumber(last)} of ${formatNumber(payload.total_rows)} rows.` +
        (short ? " Page ended early because these rows are large." : "");
      replaceChildren($("dev-data-rows"), buildTable(payload.columns, payload.rows));
      $("dev-data-prev").disabled = state.data.history.length === 0;
      $("dev-data-next").disabled = !payload.has_more;
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
      const payload = await getJSON(dev.API.query, {
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
      state.data.history = [];
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
    dev.showStatus("");
    // A panel can be loading when a sync finishes, and the invalidation that
    // follows starts a second load of the same panel. Two things then have to
    // be true: the older response must not land on top of the newer one, and it
    // must not mark the panel as loaded -- either would leave pre-sync numbers
    // on screen and cached, which is what this whole mechanism exists to stop.
    // So loads of one panel are run in order, and each carries the era it began
    // in.
    const previous = state.panelLoads.get(key) || Promise.resolve();
    const attempt = previous
      .catch(() => {})
      .then(() => {
        const generation = state.panelGeneration;
        return Promise.resolve(LOADERS[key]()).then(() => generation);
      });
    state.panelLoads.set(key, attempt);
    try {
      const generation = await attempt;
      if (generation === state.panelGeneration) state.loaded.add(key);
    } catch (err) {
      dev.showStatus(err.message, true);
    }
  }

  async function init() {
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
      state.data.history = [];
      loadRows();
    });
    $("dev-data-limit").addEventListener("change", () => {
      state.data.offset = 0;
      state.data.history = [];
      loadRows();
    });
    $("dev-data-prev").addEventListener("click", () => {
      // Step back to where the previous page actually started.
      const previous = state.data.history.pop();
      state.data.offset = previous === undefined ? 0 : previous;
      loadRows();
    });
    $("dev-data-next").addEventListener("click", () => {
      // Resume where the server said this page stopped, which is not
      // offset + limit when the byte budget ended the page early.
      if (state.data.nextOffset === null || state.data.nextOffset <= state.data.offset) return;
      state.data.history.push(state.data.offset);
      state.data.offset = state.data.nextOffset;
      loadRows();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") checkForCompletedSync();
    });

    // Ask about syncs *before* reading any of the database. A sync outlives the
    // tab it was started from, so this first answer says which one the data is
    // about to reflect -- and it is taken as the baseline, meaning it does not
    // invalidate anything. Loading a panel first would let it read the database
    // a moment before another tab's sync committed, while that same baseline
    // reported the sync as already finished: nothing would then invalidate the
    // panel that had just read the older numbers. Establishing the baseline
    // first costs one round trip on a page that is about to make several.
    await checkForCompletedSync();
    const requested = window.location.hash.replace(/^#/, "");
    const initial = Object.prototype.hasOwnProperty.call(LOADERS, requested) ? requested : "overview";
    activate(initial);
  }

  dev.whenReady(".dev-tabs", init);
})();
