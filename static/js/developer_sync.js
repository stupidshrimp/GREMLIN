/* Limble sync page.
 *
 * A thin view over one server-side job: POST /developer/api/sync starts it, GET
 * reports where it is. Nothing about the run lives in the browser, so a reload
 * -- or a second person opening the page -- picks the same sync up mid-flight
 * instead of losing sight of it. This is the one developer page that writes.
 */
(function () {
  "use strict";

  const dev = window.GremlinDev;
  const $ = dev.$;
  const el = dev.el;
  const factRow = dev.factRow;
  const formatNumber = dev.formatNumber;
  const formatDuration = dev.formatDuration;
  const formatTimestamp = dev.formatTimestamp;

  // How often the page asks the server for progress, and how often it re-renders
  // the clock between those answers. The clock ticks locally so the elapsed time
  // moves smoothly instead of jumping once a second.
  const SYNC_POLL_MS = 1000;
  const SYNC_IDLE_POLL_MS = 30000;
  const SYNC_RETRY_POLL_MS = 5000;
  const SYNC_TICK_MS = 250;

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

  const SYNC_INPUT_IDS = [
    "dev-sync-since", "dev-sync-dry-run", "dev-sync-no-assets",
    "dev-sync-instructions", "dev-sync-instructions-limit",
  ];

  // `payload` is the last /developer/api/sync response; `polledAt` is when it
  // arrived, so the clock can advance past it between polls.
  const state = {
    payload: null,
    polledAt: 0,
    pollTimer: null,
    pollDelay: null,
    tickTimer: null,
    starting: false,
    requestVersion: 0,
  };

  function now() {
    return (window.performance && performance.now()) || Date.now();
  }

  function syncJob() {
    return state.payload && state.payload.job ? state.payload.job : null;
  }

  function isSyncRunning() {
    const job = syncJob();
    return Boolean(job && job.state === "running");
  }

  async function pollSync(options) {
    const propagate = Boolean(options && options.propagate);
    // A GET that began before a newer request must not replace that request's
    // result. In particular, an idle poll can still be doing history/database
    // work when the POST below has already started a job.
    const requestVersion = ++state.requestVersion;
    try {
      const payload = await dev.getJSON(dev.API.sync);
      if (requestVersion !== state.requestVersion) return;
      state.payload = payload;
      state.polledAt = now();
      dev.showStatus("");
      renderSync();
    } catch (err) {
      if (requestVersion !== state.requestVersion) return;
      stopSyncTimers();
      if (propagate) throw err;
      dev.showStatus(err.message, true);
      // A brief network or server failure must not strand a running display.
      // Retry at a slower cadence so recovery is automatic without hammering a
      // sick endpoint. Authentication failures reload the access-denied page
      // in getJSON and must not start another request during navigation.
      if (!document.hidden && err.status !== 401 && err.status !== 403) {
        state.pollDelay = SYNC_RETRY_POLL_MS;
        state.pollTimer = window.setTimeout(function () {
          state.pollTimer = null;
          state.pollDelay = null;
          pollSync();
        }, SYNC_RETRY_POLL_MS);
      }
    }
  }

  async function startSync() {
    const since = ($("dev-sync-since").value || "").trim();
    const body = {
      dry_run: $("dev-sync-dry-run").checked,
      no_assets: $("dev-sync-no-assets").checked,
      fetch_instructions: $("dev-sync-instructions").checked,
      // Sent only when the phase is on, so a number left in the box from a
      // previous run cannot cap a sync that is not reading instructions at all.
      instructions_limit: $("dev-sync-instructions").checked
        ? ($("dev-sync-instructions-limit").value || "").trim() || null
        : null,
      since: since || null,
    };
    // Invalidate any status GET already in flight before starting the job. Its
    // snapshot may say "idle" even if it completes after this POST.
    state.requestVersion += 1;
    stopSyncTimers();
    state.starting = true;
    $("dev-sync-run").disabled = true;
    dev.showStatus("");
    try {
      const job = await dev.getJSON(dev.API.sync, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      // Also supersede a poll triggered while the POST was in flight (for
      // example, by the page becoming visible).
      state.requestVersion += 1;
      // The POST answers with the job alone; keep the credentials/history
      // context the last GET provided.
      if (state.payload) {
        state.payload.job = job;
      } else {
        state.payload = { job: job };
      }
      state.polledAt = now();
      state.starting = false;
      renderSync();
      startSyncTimers();
    } catch (err) {
      state.starting = false;
      dev.showStatus(err.message, true);
      // A refused start (409: already running) means this page's picture of the
      // server is stale, so replace it with the server's.
      pollSync();
    }
  }

  function startSyncTimers() {
    const running = isSyncRunning();
    const pollDelay = running ? SYNC_POLL_MS : SYNC_IDLE_POLL_MS;

    // Keep a slow watch even while idle: another administrator can start a run
    // from a different window. Use a chained timeout so a slow response can never
    // cause status requests to overlap.
    if (!document.hidden && (!state.pollTimer || state.pollDelay !== pollDelay)) {
      if (state.pollTimer) window.clearTimeout(state.pollTimer);
      state.pollDelay = pollDelay;
      state.pollTimer = window.setTimeout(function () {
        state.pollTimer = null;
        state.pollDelay = null;
        pollSync();
      }, pollDelay);
    }
    if (running && !state.tickTimer) {
      state.tickTimer = window.setInterval(renderSyncTiming, SYNC_TICK_MS);
    } else if (!running && state.tickTimer) {
      window.clearInterval(state.tickTimer);
      state.tickTimer = null;
    }
  }

  function stopSyncTimers() {
    if (state.pollTimer) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
      state.pollDelay = null;
    }
    if (state.tickTimer) {
      window.clearInterval(state.tickTimer);
      state.tickTimer = null;
    }
  }

  function renderSync() {
    const payload = state.payload;
    if (!payload) return;
    const running = isSyncRunning();
    const credentialsOk = Boolean(payload.credentials && payload.credentials.configured);

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
      state.starting ||
      !credentialsOk ||
      (needsDatabase && !payload.db_exists) ||
      !startAllowed;
    button.textContent = running ? "Sync running…" : "Run sync now";
    SYNC_INPUT_IDS.forEach(function (id) {
      $(id).disabled = running;
    });

    startSyncTimers();
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
    return job.elapsed_seconds + Math.max(0, now() - state.polledAt) / 1000;
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

  async function init() {
    $("dev-sync-run").addEventListener("click", startSync);
    // Whether a database is required depends on this checkbox, and nothing else
    // redraws the page immediately while it sits idle, so the button would otherwise
    // stay disabled until the next status poll.
    $("dev-sync-dry-run").addEventListener("change", () => {
      if (state.payload) renderSync();
    });
    // The cap only means anything while the phase is on, so it appears with it
    // rather than sitting there inviting a number that would be ignored.
    const instructions = $("dev-sync-instructions");
    const showInstructionsLimit = () => {
      $("dev-sync-instructions-field").hidden = !instructions.checked;
    };
    instructions.addEventListener("change", showInstructionsLimit);
    showInstructionsLimit();
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        stopSyncTimers();
      } else {
        // Ask immediately on return rather than waiting through an idle interval.
        pollSync();
      }
    });
    await pollSync();
  }

  dev.whenReady("#dev-sync-run", init);
})();
