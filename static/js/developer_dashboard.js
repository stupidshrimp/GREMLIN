/* Developer dashboard client.
 *
 * Seven panels over the /developer/api/* endpoints:
 *   - Overview      : database file facts + how this Flask process is configured
 *   - Pipeline      : row counts down the ingestion -> Weibull path, recent imports
 *   - Limble sync   : run the nightly import now, and watch it run
 *   - Schema        : the live catalogue, table by table (columns, FKs, indexes, DDL)
 *   - Drift         : tables/columns that exist in the code but not the file, or vice versa
 *   - Data browser  : paginated rows from one table
 *   - SQL console   : a single read-only statement
 *
 * Panels fetch lazily on first activation and cache the result, so switching tabs
 * stays instant and the shared database is not re-read on every click. Every
 * inspection endpoint is read-only server-side; the sync panel is the one that
 * changes GREMLIN.db, and it does so by starting the same background import the
 * scheduled job runs.
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
    sync: "/developer/api/sync",
  };

  // How often the sync panel asks the server for progress, and how often it
  // re-renders the clock between those answers. The clock ticks locally so the
  // elapsed time moves smoothly instead of jumping once a second.
  const SYNC_POLL_MS = 1000;
  const SYNC_TICK_MS = 250;

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
    // `payload` is the last /developer/api/sync response; `polledAt` is when it
    // arrived, so the clock can advance past it between polls.
    // `lastWritingRun` identifies the last completed sync whose changes this
    // page has already accounted for; undefined until the first status answer.
    sync: { payload: null, polledAt: 0, pollTimer: null, tickTimer: null, starting: false, lastWritingRun: undefined },
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

  function formatDuration(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds) || seconds < 0) return "—";
    const whole = Math.round(seconds);
    if (whole < 60) return `${whole}s`;
    const minutes = Math.floor(whole / 60);
    const rest = whole % 60;
    if (minutes < 60) return `${minutes}m ${String(rest).padStart(2, "0")}s`;
    return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
  }

  // SQLite writes import_batch timestamps as UTC without a zone suffix
  // ("2026-08-11 03:00:12"). A browser reads that shape as *local* time, so the
  // zone has to be restated before it can be shown in the reader's own.
  function formatTimestamp(value) {
    if (!value) return "—";
    const text = String(value).trim();
    const normalised = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$/.test(text)
      ? `${text.replace(" ", "T")}Z`
      : text;
    const parsed = new Date(normalised);
    return isNaN(parsed.getTime()) ? text : parsed.toLocaleString();
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

  // --- Limble sync --------------------------------------------------------
  //
  // The panel is a thin view over one server-side job: POST starts it, GET
  // reports where it is. Nothing about the run lives in the browser, so a
  // reload -- or a second person opening the page -- picks the same sync up
  // mid-flight instead of losing sight of it.

  const SYNC_SUMMARY_FIELDS = [
    ["fetched_tasks", "Tasks fetched"],
    ["excluded_templates", "Template tasks excluded"],
    ["fetched_assets", "Assets fetched"],
    ["records", "Records transformed"],
    ["inserted", "Rows inserted"],
    ["updated", "Rows updated"],
    ["skipped", "Rows unchanged"],
    ["mapped", "Records mapped"],
    ["import_batch_id", "Import batch"],
  ];

  const SYNC_INPUT_IDS = ["dev-sync-since", "dev-sync-dry-run", "dev-sync-no-assets"];

  function now() {
    return (window.performance && performance.now()) || Date.now();
  }

  function syncJob() {
    return state.sync.payload && state.sync.payload.job ? state.sync.payload.job : null;
  }

  function isSyncRunning() {
    const job = syncJob();
    return Boolean(job && job.state === "running");
  }

  async function pollSync(options) {
    const propagate = Boolean(options && options.propagate);
    try {
      const payload = await getJSON(API.sync);
      state.sync.payload = payload;
      state.sync.polledAt = now();
      state.loaded.add("sync");
      renderSync();
    } catch (err) {
      // Stop the interval rather than retry into a wall: a failing poll is
      // either an expired PIN (getJSON reloads) or a server-side problem, and
      // one banner is more useful than one per second.
      stopSyncTimers();
      if (propagate) throw err;
      showStatus(err.message, true);
    }
  }

  async function startSync() {
    const since = ($("dev-sync-since").value || "").trim();
    const body = {
      dry_run: $("dev-sync-dry-run").checked,
      no_assets: $("dev-sync-no-assets").checked,
      since: since || null,
    };
    state.sync.starting = true;
    $("dev-sync-run").disabled = true;
    showStatus("");
    try {
      const job = await getJSON(API.sync, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      // The POST answers with the job alone; keep the credentials/history
      // context the last GET provided.
      if (state.sync.payload) {
        state.sync.payload.job = job;
      } else {
        state.sync.payload = { job: job };
      }
      state.sync.polledAt = now();
      state.sync.starting = false;
      renderSync();
      startSyncTimers();
    } catch (err) {
      state.sync.starting = false;
      showStatus(err.message, true);
      // A refused start (409: already running) means the panel's picture of the
      // server is stale, so replace it with the server's.
      pollSync();
    }
  }

  function startSyncTimers() {
    if (!state.sync.pollTimer) {
      state.sync.pollTimer = window.setInterval(function () {
        pollSync();
      }, SYNC_POLL_MS);
    }
    if (!state.sync.tickTimer) {
      state.sync.tickTimer = window.setInterval(renderSyncTiming, SYNC_TICK_MS);
    }
  }

  function stopSyncTimers() {
    if (state.sync.pollTimer) {
      window.clearInterval(state.sync.pollTimer);
      state.sync.pollTimer = null;
    }
    if (state.sync.tickTimer) {
      window.clearInterval(state.sync.tickTimer);
      state.sync.tickTimer = null;
    }
  }

  // The other panels cache what they read because only a sync changes the
  // database underneath them -- so when one finishes, that cache is stale by
  // definition and the row counts, batches and schema have to be read again.
  function refreshDatabasePanels() {
    state.panelGeneration += 1;
    ["overview", "pipeline", "schema", "drift", "data"].forEach((key) => state.loaded.delete(key));
    const active = document.querySelector(".dev-tab.is-active");
    if (active && active.dataset.tab !== "sync") activate(active.dataset.tab);
    // Reloading the Schema panel rebuilds the table *list*; an open table's
    // detail is drawn separately and would sit there showing the row count and
    // columns from before the import, which mapping can genuinely change.
    if (state.selectedTable) selectTable(state.selectedTable);
  }

  // Identifies a finished run that changed the database, so the panels can be
  // invalidated by *which* sync completed rather than by this browser having
  // watched it happen. A tab that was elsewhere while another one ran a sync
  // never sees the running state at all -- it goes straight from idle to
  // succeeded -- and would otherwise keep serving pre-sync counts. Dry runs
  // change nothing and deliberately produce no id.
  function writingRunId(job) {
    if (!job || job.state !== "succeeded") return null;
    if (job.options && job.options.dry_run) return null;
    const summary = job.summary || {};
    const batch = summary.import_batch_id === undefined || summary.import_batch_id === null ? "" : summary.import_batch_id;
    return `${job.started_at || ""}#${batch}`;
  }

  function renderSync() {
    const payload = state.sync.payload;
    if (!payload) return;
    const running = isSyncRunning();
    const credentialsOk = Boolean(payload.credentials && payload.credentials.configured);

    const runId = writingRunId(syncJob());
    if (state.sync.lastWritingRun === undefined) {
      // First answer of this page load is the baseline: every panel read after
      // it is already reading post-sync data, so there is nothing to drop.
      state.sync.lastWritingRun = runId;
    } else if (runId && runId !== state.sync.lastWritingRun) {
      state.sync.lastWritingRun = runId;
      refreshDatabasePanels();
    }

    renderSyncFacts(payload);
    renderSyncWarning(payload);
    renderSyncProgress();

    // `start_allowed` is the server's answer, not a hint: it refuses the POST
    // too. A payload without the field predates it, so treat it as allowed.
    const startAllowed = payload.start_allowed !== false;
    // Only a run that writes needs somewhere to write to. A dry run against a
    // missing database is a legitimate thing to want -- it is how you check
    // that credentials and the API still work when the path is wrong -- and the
    // server allows it, so the button must not be stricter than the endpoint.
    const needsDatabase = !$("dev-sync-dry-run").checked;
    const button = $("dev-sync-run");
    button.disabled =
      running ||
      state.sync.starting ||
      !credentialsOk ||
      (needsDatabase && !payload.db_exists) ||
      !startAllowed;
    button.textContent = running ? "Sync running…" : "Run sync now";
    SYNC_INPUT_IDS.forEach(function (id) {
      $(id).disabled = running;
    });

    if (running) startSyncTimers();
    else stopSyncTimers();
  }

  function renderSyncFacts(payload) {
    const facts = el("dl", "dev-facts");
    const credentials = payload.credentials || {};
    factRow(facts, "Limble credentials", credentials.configured ? "Configured" : "Not configured");
    factRow(facts, "Limble base URL", credentials.base_url);
    factRow(facts, "Database", payload.db_exists ? payload.db_path : `${payload.db_path} (missing)`);

    const history = payload.history || {};
    const rows = typeof history.last_row_count === "number" ? ` · ${formatNumber(history.last_row_count)} records` : "";
    factRow(
      facts,
      "Last completed import",
      history.last_completed_at ? `${formatTimestamp(history.last_completed_at)}${rows}` : "None recorded"
    );
    factRow(
      facts,
      "Typical full sync",
      history.median_seconds
        ? `${formatDuration(history.median_seconds)} (median of the last ${formatNumber(history.timed_runs)})`
        : "Not known yet — no sync has been timed against this database"
    );

    facts.id = "dev-sync-facts";
    $("dev-sync-facts").replaceWith(facts);
  }

  function renderSyncWarning(payload) {
    const box = $("dev-sync-warning");
    const warnings = [];
    if (payload.start_allowed === false && payload.start_blocked_reason) {
      warnings.push(payload.start_blocked_reason);
    }
    const credentials = payload.credentials || {};
    if (!credentials.configured) {
      warnings.push(credentials.detail || "Limble credentials are not configured on this server.");
    }
    if (!payload.db_exists) {
      warnings.push(
        `No database file at ${payload.db_path}. A sync writes into an existing GREMLIN.db; ` +
          "set GREMLIN_DB_PATH, or create the file from the command line with --create. " +
          "A dry run needs no database and can still be run from here."
      );
    }
    const flight = payload.in_flight_batch;
    if (flight) {
      warnings.push(
        `Import batch ${flight.import_batch_id === null ? "?" : flight.import_batch_id} has been open since ` +
          `${formatTimestamp(flight.started_at)} (${formatDuration(flight.age_seconds)} ago). Another process — most ` +
          "likely the scheduled nightly task — may still be importing, and a second sync would repeat its work."
      );
    }

    box.textContent = "";
    box.hidden = warnings.length === 0;
    warnings.forEach(function (text) {
      box.appendChild(el("p", null, text));
    });
  }

  function renderSyncProgress() {
    const job = syncJob();
    const card = $("dev-sync-progress-card");
    if (!job || job.state === "idle") {
      card.hidden = true;
      return;
    }
    card.hidden = false;

    const running = job.state === "running";
    const options = job.options || {};
    const dryRun = options.dry_run ? " (dry run)" : "";
    const heading =
      running ? `Sync running${dryRun}` : job.state === "succeeded" ? `Sync complete${dryRun}` : `Sync failed${dryRun}`;
    $("dev-sync-state").textContent = heading;

    const bar = $("dev-sync-bar");
    const fill = $("dev-sync-fill");
    let fraction = typeof job.progress === "number" ? Math.max(0, Math.min(1, job.progress)) : null;
    if (job.state === "succeeded") fraction = 1;
    const indeterminate = running && fraction === null;
    bar.classList.toggle("is-indeterminate", indeterminate);
    bar.classList.toggle("is-error", job.state === "failed");
    fill.style.width = indeterminate ? "100%" : `${((fraction === null ? 0 : fraction) * 100).toFixed(1)}%`;
    if (indeterminate) {
      bar.removeAttribute("aria-valuenow");
    } else {
      bar.setAttribute("aria-valuenow", String(Math.round((fraction === null ? 0 : fraction) * 100)));
    }

    const phase = $("dev-sync-phase");
    phase.textContent = syncPhaseText(job);
    phase.classList.toggle("is-error", job.state === "failed");
    renderSyncTiming();
    renderSyncSummary(job);
    renderSyncLog(job);
  }

  function syncPhaseText(job) {
    if (job.state === "succeeded") return "Every phase finished.";
    if (job.state === "failed") return job.error ? `Stopped: ${job.error}` : "Stopped before finishing.";

    const parts = [];
    if (job.phase_index && job.phase_count) parts.push(`Step ${job.phase_index} of ${job.phase_count}`);
    parts.push(job.phase_label || "Starting");

    const counts = job.counts || {};
    const current = counts[job.phase];
    if (typeof current === "number") {
      // An exact target comes from the phase itself; an estimated one is this
      // page's guess from previous syncs, and says so with a "~".
      const exact = (job.targets || {})[job.phase];
      const estimated = (job.expected || {})[job.phase];
      if (typeof exact === "number" && exact > 0) {
        parts.push(`${formatNumber(current)} of ${formatNumber(exact)}`);
      } else if (typeof estimated === "number" && estimated > current) {
        parts.push(`${formatNumber(current)} of ~${formatNumber(estimated)}`);
      } else {
        parts.push(`${formatNumber(current)} so far`);
      }
    }
    return parts.join(" · ");
  }

  function syncElapsedSeconds(job) {
    if (!job || typeof job.elapsed_seconds !== "number") return null;
    if (job.state !== "running") return job.elapsed_seconds;
    // Advance the server's number locally so the clock does not sit still
    // between polls.
    return job.elapsed_seconds + Math.max(0, now() - state.sync.polledAt) / 1000;
  }

  function renderSyncTiming() {
    const job = syncJob();
    const label = $("dev-sync-timing");
    if (!job || job.state === "idle") {
      label.textContent = "";
      return;
    }
    const elapsed = syncElapsedSeconds(job);
    if (job.state !== "running") {
      const finished = job.finished_at ? ` at ${formatTimestamp(job.finished_at)}` : "";
      label.textContent = `Took ${formatDuration(elapsed)}${finished}`;
      return;
    }

    const parts = [`Elapsed ${formatDuration(elapsed)}`];
    const estimate = job.estimated_total_seconds;
    if (typeof estimate === "number" && elapsed !== null) {
      const basis = job.estimate_basis ? ` (${job.estimate_basis})` : "";
      parts.push(
        elapsed > estimate
          ? `longer than usual — expected about ${formatDuration(estimate)}${basis}`
          : `about ${formatDuration(estimate - elapsed)} remaining${basis}`
      );
    } else {
      parts.push("no estimate yet — this is the first sync being timed");
    }
    label.textContent = parts.join(" · ");
  }

  function renderSyncSummary(job) {
    const list = $("dev-sync-summary");
    const summary = job.summary;
    list.textContent = "";
    if (!summary) {
      list.hidden = true;
      return;
    }
    list.hidden = false;
    SYNC_SUMMARY_FIELDS.forEach(function (entry) {
      const key = entry[0];
      if (summary[key] === undefined) return;
      factRow(list, entry[1], typeof summary[key] === "number" ? formatNumber(summary[key]) : summary[key]);
    });
    if (summary.dry_run) {
      factRow(list, "Dry run", "Nothing was written to the database.");
    }
    // A raw import can succeed while the mapping refresh does not, which leaves
    // the new rows invisible to the dashboards. Say so rather than let a green
    // "complete" imply the data arrived.
    if (summary.mapping_ok === false && summary.mapping_note) {
      factRow(list, "Mapping", summary.mapping_note);
    }
  }

  function renderSyncLog(job) {
    const log = $("dev-sync-log");
    const messages = job.messages || [];
    // Keep following the tail only if the reader is already there; scrolling
    // back to read a rate-limit notice should not be undone a second later.
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    log.textContent = "";
    messages.forEach(function (message) {
      const line = el("div", "dev-log-line");
      const stamp = new Date(message.at);
      line.appendChild(el("span", "dev-log-time", isNaN(stamp.getTime()) ? "" : stamp.toLocaleTimeString()));
      line.appendChild(el("span", "dev-log-text", message.text));
      log.appendChild(line);
    });
    if (atBottom) log.scrollTop = log.scrollHeight;
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
    // copy of the invalidation era: a sync that finishes mid-request would
    // otherwise be answered with columns and a row count from before it.
    const generation = state.panelGeneration;
    try {
      const detail = await getJSON(API.table(name));
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
    sync: () => pollSync({ propagate: true }),
    schema: loadTables,
    drift: loadDrift,
    data: async () => {
      if (!state.tables.length) await loadTables();
      state.data.offset = 0;
      state.data.history = [];
      await loadRows();
    },
    console: async () => {},
    access: async () => {},
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

    if (state.loaded.has(key)) {
      // Cached panels describe a database that only this page changes, so they
      // stay valid. A sync does not: one may have been started from another tab
      // (or by the scheduled job) since this panel was last drawn.
      if (key === "sync") pollSync();
      return;
    }
    showStatus("");
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
      showStatus(err.message, true);
    }
  }

  async function init() {
    document.querySelectorAll(".dev-tab").forEach((tab) => {
      tab.addEventListener("click", () => activate(tab.dataset.tab));
    });
    $("dev-table-filter").addEventListener("input", renderTableList);
    $("dev-sync-run").addEventListener("click", startSync);
    // Whether a database is required depends on this checkbox, and nothing else
    // redraws the panel while the page sits idle -- polling only runs during a
    // sync -- so the button would stay disabled until something else happened.
    $("dev-sync-dry-run").addEventListener("change", () => {
      if (state.sync.payload) renderSync();
    });
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

    // Ask about syncs *before* reading any of the database. A sync outlives the
    // tab it was started from, so this first answer says which one the data is
    // about to reflect -- and it is taken as the baseline, meaning it does not
    // invalidate anything. Loading a panel first would let it read the database
    // a moment before another tab's sync committed, while that same baseline
    // reported the sync as already finished: nothing would then invalidate the
    // panel that had just read the older numbers. Establishing the baseline
    // first costs one round trip on a page that is about to make several.
    await pollSync();
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
