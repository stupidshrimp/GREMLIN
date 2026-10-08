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
  // The chart palette. The values live in theme.css like every other colour in
  // the application -- chart_theme.js reads them back out, since a canvas
  // inherits nothing from the cascade and has to be told. Held in a variable
  // rather than read at each use because getComputedStyle is a layout read and
  // these are wanted once per plotted point; reassigned on a theme change, next
  // to the redraw it goes with.
  let C = window.gremlinChartPalette();
  // Whether this browser session may write. Rendered by the server into every
  // page, so it reflects the account's role rather than anything the client
  // decided. The API enforces the same rule on its own -- this only keeps the
  // page from offering an action that would be refused.
  const CAN_EDIT = document.querySelector('meta[name="gremlin-can-edit"]')?.content === "true";
  // Added to the hint over each analysis table whose record numbers open that
  // record's disposition, for the accounts that get those buttons.
  const RECORD_EDIT_HINT = CAN_EDIT ? " Click a WO number to review or change its disposition." : "";
  // Analysis types offered by the Step 1 selector. Weibull and Failure Mode Trend
  // are implemented; the rest render a "Coming soon" placeholder for now. The
  // selected type only controls the secondary analysis panel — the Pareto chart
  // stays visible for every type.
  const ANALYSIS_TYPES = {
    WEIBULL: "Weibull Analysis",
    TREND: "Failure Mode Trend Analysis",
    DOWNTIME: "Downtime Driver Analysis",
    PM: "PM Effectiveness Analysis",
    REPEAT: "Repeat Fix Rate Analysis",
  };
  // The editable half of a disposition, per record kind, as columns: what the
  // disposition table draws after the read-only record columns, and what the
  // analysis page's single-record editor labels its fields with, so the two
  // always call a field the same thing. Each key is the name the server orders
  // that column by.
  const DISPOSITION_EDIT_COLUMNS = {
    wo: [
      { key: "disposition_notes", label: "Disposition Notes" },
      { key: "disposition_category", label: "Disposition Category" },
      { key: "effective_record_class", label: "Record Class" },
      { key: "failure_mode", label: "Failure Mode" },
      { key: "failure_mechanism", label: "Failure Mechanism" },
      { key: "modeled_population_name", label: "Modeled Population" },
      { key: "include_in_weibull_candidate", label: "Include in Weibull Candidate" },
    ],
    pm: [
      { key: "disposition_notes", label: "Disposition Notes" },
      { key: "disposition_category", label: "Disposition Category" },
      { key: "effective_record_class", label: "Record Class" },
      { key: "pm_reset_inclusion_decision", label: "PM Reset Decision" },
      { key: "reset_target_failure_mode", label: "Reset Target Failure Mode" },
      { key: "reset_target_failure_mechanism", label: "Reset Target Failure Mechanism" },
      { key: "modeled_population_name", label: "Modeled Population" },
      { key: "include_in_weibull_candidate", label: "Include in Weibull Candidate" },
      { key: "pm_reset_renewal_rationale", label: "PM Reset Renewal Rationale / Evidence" },
    ],
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
    // Highest-beta mechanisms, as the summary last sent them: the fits already
    // saved on this asset, which the tour picks its Weibull example from.
    rankings: [],
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
    // Repeat Fix Rate Analysis: `repeatData` is the latest asset-wide payload from the
    // repeat-fixes endpoint, `repeatWindow` the scheduled hours it was counted with,
    // `repeatFilter` the one mechanism the repeats list is narrowed to (null for
    // all), and `repeatToken` drops stale responses.
    repeatData: null,
    repeatWindow: 24,
    repeatFilter: null,
    repeatToken: 0,
    // "Most likely to fail soon": the window in weeks the list was last asked for, and
    // a token both requests that draw it (the summary and the risk-rankings call)
    // take, so a response for an older window cannot land on top of a newer one.
    riskWeeks: 4,
    riskToken: 0,
    // `latestResult` is the Weibull result rendered in the workspace; `analysisToken`
    // drops stale responses (same pattern as the PM and Downtime analyses above), so an
    // older group's result -- or its "nothing saved" empty state -- cannot land on top
    // of a newer one.
    latestResult: null,
    // The failure group `latestResult` was fitted for, so a disposition changed
    // from its data table can run the same group again. Only read alongside a
    // non-null latestResult, which every path that drops the result clears.
    latestResultGroup: null,
    // The analysis start and cutoff dates an editor entered for that run, if any
    // ({ start, cutoff } as YYYY-MM-DD, either blank), so the re-run after a
    // disposition change keeps the same window rather than quietly widening it.
    latestResultWindow: null,
    analysisToken: 0,
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
    // Column ordering for the disposition table, applied server-side for the
    // same reason the search is: the table is paginated, so ordering has to
    // cover every eligible row or "oldest first" only means oldest of the 50 on
    // screen. `key` is a column name the server advertised; "" is its own
    // default order.
    dispositionSort: { key: "", dir: "asc" },
    // Active column value filters for the disposition table, keyed by column, so
    // paging and sorting -- both of which rebuild the table from the server --
    // land on the view they left instead of quietly dropping the filter.
    // dispositionFilterSelection is the selection they were chosen against;
    // when that changes the rows do too, and the filter goes with them.
    dispositionFilters: {},
    dispositionFilterSelection: "",
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

  // The four structured text boxes the maintenance teams fill out on a Limble
  // work order, in the order they read: where it happened, what was seen, why,
  // and what was done about it. Mirrors NARRATIVE_FIELDS in
  // services/wo_narrative.py -- keep the two in step.
  const NARRATIVE_FIELDS = [
    { key: "area_affected", label: "Area Affected" },
    { key: "condition_found", label: "Condition" },
    { key: "cause", label: "Cause" },
    { key: "action_taken", label: "Action" },
  ];

  // Whichever of the four boxes this record actually filled in, captioned and in
  // order. `prefix` names the key prefix the caller's rows use (the Weibull
  // observation rows carry them as source_*), and `fields` lets the disposition
  // table pass the server's own labels instead of the constant above.
  function narrativeLines(row, prefix, fields) {
    return (fields || NARRATIVE_FIELDS)
      .map((field) => ({ label: field.label, text: row[(prefix || "") + field.key] }))
      .filter((line) => line.text != null && String(line.text).trim() !== "")
      .map((line) => ({ label: line.label, text: String(line.text).trim() }));
  }

  // One cell holding the whole failure narrative, stacked. Four separate columns
  // would have cost ~900px of horizontal scrolling on tables that are already
  // wide, and the four boxes read as one story anyway.
  //
  // The plain-text form goes on data-column-text so the column header's sort and
  // filter tools read something sensible rather than the run-together
  // concatenation of the labels and values.
  function narrativeCell(row, options) {
    const settings = options || {};
    const lines = narrativeLines(row, settings.prefix, settings.fields);
    const cls = ["lda-narrative-cell", settings.cls || null].filter(Boolean).join(" ");
    if (!lines.length) {
      return el("td", { class: cls, "data-column-text": "" }, [
        el("span", { class: "lda-narrative-empty", text: settings.emptyText || "—" }),
      ]);
    }
    return el(
      "td",
      { class: cls, "data-column-text": narrativeText(row, settings.prefix, settings.fields) },
      lines.map((line) =>
        el("p", { class: "lda-narrative-line" }, [
          el("span", { class: "lda-narrative-label", text: line.label }),
          el("span", { class: "lda-narrative-text", text: line.text }),
        ])
      )
    );
  }

  // The same narrative on one line, for tooltips, sorting and filtering.
  function narrativeText(row, prefix, fields) {
    return narrativeLines(row, prefix, fields)
      .map((line) => `${line.label}: ${line.text}`)
      .join(" · ");
  }

  // ---- record dates ---------------------------------------------------------
  // The CMMS dates arrive as text in whichever shape the source system wrote
  // them -- "2026-01-15T15:00:00+00:00" from the Limble sync, "1/15/2026 15:00"
  // from an older import -- because SQLite has no date type to have normalised
  // them on the way through. Parsed to a UTC instant here so a date column can
  // be shown, sorted and filtered as a date rather than as the string it is
  // stored as. Mirrors LifeDataService._parse_datetime; keep the two in step.
  //
  // Returns { ms, hasTime } or null for a value that is not a date at all.
  //
  // A day that does not exist is not a date. Both Date.UTC and Date.parse roll
  // an impossible one forward -- 2025-02-31 becomes March 3, an hour of 25
  // becomes the next morning -- which would put a day on screen that the record
  // does not have, leave it unfindable by searching for what the cell shows
  // (the server's parser refuses the original outright), and sort it under a
  // date nobody wrote. So every instant is read back and checked against the
  // digits it was built from.
  function utcInstant(year, month, day, hour, minute, second, milli) {
    const ms = Date.UTC(year, month - 1, day, hour, minute, second, milli || 0);
    const when = new Date(ms);
    const rolled =
      when.getUTCFullYear() !== year ||
      when.getUTCMonth() !== month - 1 ||
      when.getUTCDate() !== day ||
      when.getUTCHours() !== hour ||
      when.getUTCMinutes() !== minute ||
      when.getUTCSeconds() !== second;
    return rolled ? NaN : ms;
  }

  // "123000" or "5" after the decimal point, as whole milliseconds.
  function fractionMillis(fraction) {
    return fraction ? Math.floor(Number("0." + fraction) * 1000) : 0;
  }

  function parseRecordDate(value) {
    if (value == null) return null;
    const text = String(value).trim();
    if (!text) return null;
    // The fractional second is optional but common: the ingestion path converts a
    // millisecond timestamp by dividing, so every date it writes from one carries
    // ".123000". Python's parser takes it, so this has to as well -- rejecting it
    // would leave those values shown raw and sorted as though they were blank.
    //
    // No whitespace at all before the offset. The server's answer there is not a
    // rule so much as a set of accidents -- it takes "...T15:00:00 -05:00" and
    // "...15:00:00.123000  +0000" but refuses "...T15:00:00  -05:00" and
    // "...15:00:00.5 +00:00", because fromisoformat is picky about how many
    // fractional digits it will tolerate alongside a spaced offset. Trying to
    // trace that line is what kept putting values on the wrong side of it, and
    // being wrong in this direction is the harmful one: a value this side reads
    // and the other refuses is drawn as a normalised date while the ORDER BY
    // files it with the blanks. So the offset must follow the time directly.
    // Anything looser the server happens to accept is simply shown as stored,
    // which costs nothing but the tidier rendering.
    const iso = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?(Z|[+-]\d{2}:?\d{2})?$/.exec(text);
    if (iso) {
      const [, year, month, day, hour, minute, second, fraction, zone] = iso;
      const hasTime = hour !== undefined;
      const milli = fractionMillis(fraction);
      // Checked before the offset is applied, since shifting a rolled-over date
      // by a few hours only hides that it rolled over.
      if (!isFinite(utcInstant(+year, +month, +day, +(hour || 0), +(minute || 0), +(second || 0), milli))) return null;
      if (zone) {
        const ms = Date.parse(
          `${year}-${month}-${day}T${hour || "00"}:${minute || "00"}:${second || "00"}` +
            `${fraction ? "." + fraction : ""}${zone === "Z" ? "Z" : zone}`
        );
        return isFinite(ms) ? { ms, hasTime } : null;
      }
      // No offset means UTC, which is how the server reads the same value.
      return {
        ms: Date.UTC(+year, +month - 1, +day, +(hour || 0), +(minute || 0), +(second || 0), milli),
        hasTime,
      };
    }
    // Deliberately no seconds here, and a space rather than [ T]. The server
    // reads slash dates with the four strptime formats "%m/%d/%Y",
    // "%m/%d/%Y %H:%M", "%m/%d/%y" and "%m/%d/%y %H:%M" -- no seconds in any of
    // them, and a literal space in the two that carry a time. Accepting
    // "1/15/2026 15:00:30" or "1/15/2026T15:00" would have this side call a
    // value a date that the other side refuses: shown normalised on screen,
    // sorted with the blanks, and unfindable by searching for the text in its
    // own cell. This mirrors that list exactly; widening it belongs in
    // _parse_datetime, which the whole analysis pipeline reads, not here.
    const us = /^(\d{1,2})\/(\d{1,2})\/(\d{2}|\d{4})(?: (\d{1,2}):(\d{2}))?$/.exec(text);
    if (us) {
      const [, month, day, year, hour, minute] = us;
      // Two-digit years the way Python's strptime reads them: 00-68 is 2000s.
      const fullYear = year.length === 2 ? (+year < 69 ? 2000 + +year : 1900 + +year) : +year;
      const ms = utcInstant(fullYear, +month, +day, +(hour || 0), +(minute || 0), 0);
      return isFinite(ms) ? { ms, hasTime: hour !== undefined } : null;
    }
    return null;
  }

  // A record date in the one shape the tables show it in (UTC). A value that
  // carried no clock time keeps none, and a value that is not a date at all is
  // shown as it was stored rather than blanked.
  function formatRecordDate(value) {
    const parsed = parseRecordDate(value);
    if (!parsed) return value == null ? "" : String(value).trim();
    const pad = (n) => String(n).padStart(2, "0");
    const when = new Date(parsed.ms);
    const day = `${when.getUTCFullYear()}-${pad(when.getUTCMonth() + 1)}-${pad(when.getUTCDate())}`;
    return parsed.hasTime ? `${day} ${pad(when.getUTCHours())}:${pad(when.getUTCMinutes())}` : day;
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

  // How many calendar weeks it takes to build up `hours` of life on a Weibull result's
  // schedule. Life hours are scheduled hours and a weekend adds none, so a week holds
  // five scheduled days of hours_per_day: 100 life hours on the 20-hour schedule, 120
  // on the 24-hour one. Mirrors LifeDataService.calendar_weeks_for_life_hours, which
  // the Word report uses. Null when the result does not say what its schedule is.
  function calendarWeeksForLifeHours(hours, lifeBasis) {
    const value = Number(hours);
    const perDay = lifeBasis ? Number(lifeBasis.hours_per_day) : NaN;
    if (!isFinite(value) || value <= 0 || !isFinite(perDay) || perDay <= 0) return null;
    const daysPerWeek = lifeBasis.exclude_weekends === false ? 7 : 5;
    return value / (perDay * daysPerWeek);
  }

  // "about 7.5 calendar weeks" for a life-hours figure, or "" when it can't be worked out.
  function calendarWeeksText(hours, lifeBasis) {
    const weeks = calendarWeeksForLifeHours(hours, lifeBasis);
    return weeks == null ? "" : `about ${weeks.toFixed(1)} calendar weeks`;
  }

  // "20 hours Monday-Friday, weekends excluded": the schedule a result's hours count.
  function scheduleLabel(lifeBasis) {
    if (!lifeBasis || !lifeBasis.schedule_name) return "the weekday schedule";
    return lifeBasis.schedule_name + (lifeBasis.exclude_weekends ? ", weekends excluded" : "");
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
      if ((response.status === 401 || response.status === 403) && window.gremlinToast) {
        window.gremlinToast(message);
        const error = new Error(message);
        error.toastShown = true;
        throw error;
      }
      const error = new Error(message);
      // The rest of the error body, for a caller that needs more than the message.
      error.payload = data;
      throw error;
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
    if (loadingDepth === 0) {
      $("lda-loading").hidden = true;
      startOwedDispositionTour();
    }
  }

  function showBanner(message, kind) {
    if (kind === "error" && /log in|role is required|guest access is read-only/i.test(message) && window.gremlinToast) {
      window.gremlinToast(message);
      return;
    }
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

  // ---- help tooltips ---------------------------------------------------------
  // Wraps a control in a bubble that opens on hover and on keyboard focus, for
  // buttons whose label has no room to say what the button actually does. The
  // cascade drives both states (see .lda-tip in the stylesheet), so re-rendering
  // the editor can never strand an open bubble on the page. The bubble
  // *describes* the control rather than naming it, so a screen reader still
  // announces the button's own text first.
  let tooltipSeq = 0;
  function withTooltip(control, text) {
    const id = `lda-tip-${++tooltipSeq}`;
    control.setAttribute("aria-describedby", id);
    return el("span", { class: "lda-tip-wrap" }, [
      control,
      el("span", { class: "lda-tip", id, role: "tooltip", text }),
    ]);
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
        startOwedDispositionTour();
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

  // The asset list's first load, set as the page starts. The tour waits on it
  // before it asks for its example: Show me around works from the moment the
  // page is drawn, before the list it looks the example up in is in, and before
  // the records it is chosen from have been mapped on a first visit after an
  // import (see asset_number_options).
  let assetsLoaded = Promise.resolve();

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
    startOwedDispositionTour();
  }

  // Resolves once the asset's summary is on the page, or on the Disposition
  // page its editor, which is what the tour waits for after picking its example.
  function chooseAsset(asset) {
    $("lda-asset").value = asset.asset_number;
    closeAssetDropdown();
    return evaluateAssetSelection();
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
      state.repeatData = null;
      state.repeatFilter = null;
      state.repeatToken += 1;
      // Same for a Weibull lookup still in flight: clearWorkspace() below empties the
      // workspace, and a late response for the old asset must not refill it.
      state.analysisToken += 1;
      // A new asset's failure mode/mechanism ids may not apply; drop the carried
      // selection so a type switch doesn't auto-run a stale mechanism on it.
      state.activeMechanismRow = null;
      clearWorkspace();
    }
    const ready = Boolean(state.selectedAsset);
    // The summary / action cards only exist on the Perform Analysis page; guard
    // so the same asset combobox can drive the disposition page too.
    const summaryCard = $("lda-summary-card");
    const actionsBar = $("lda-actions");
    if (summaryCard) summaryCard.hidden = !ready;
    // The action bar holds a read-only Pareto toggle alongside the write
    // buttons, so it still appears; its buttons are hidden by the template for
    // a viewer.
    if (actionsBar) actionsBar.hidden = !ready;
    if (asset) {
      $("lda-asset-hint").textContent = asset.asset_name
        ? `Selected ${asset.asset_number}: ${asset.asset_name}.`
        : `Selected ${asset.asset_number}.`;
      if (state.pageMode === "disposition") return reloadDispositionForSelection();
      return refreshSummary();
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
    // The summary carries the risk list too, so it takes a risk token: a weeks change
    // made while it is in flight asks again, and the older list must not land last.
    const riskToken = ++state.riskToken;
    try {
      const data = await getJson(`${API}/summary?asset=${encodeURIComponent(asset)}&weeks=${riskWeeks()}`);
      if (token !== state.summaryToken || state.selectedAsset !== asset) return;
      renderSummary(data.summary || {});
      state.rankings = data.rankings || [];
      renderRankings(state.rankings);
      if (riskToken === state.riskToken) renderRiskRankings(data.risk_rankings || []);
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
      // Repeat Fix Rate reads the same included failures, so a disposition change
      // can make or break a repeat: re-fetch it whenever it is showing.
      if (state.analysisType === ANALYSIS_TYPES.REPEAT) loadRepeatFixes();
      offerAnalysisResultsTour();
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
      list.appendChild(el("li", { class: "is-empty", text: "No saved Weibull mechanism results with enough failures to rank yet." }));
      return;
    }
    // Only fits with enough failure lives are ranked at all; one saved under an earlier
    // method still is, marked, because its beta is close but not what a run today gives.
    rankings.forEach((row) => {
      list.appendChild(
        el("li", {
          text:
            `${row.failure_mechanism_name}: beta ${fmt(row.beta_mle)} ` +
            `(${row.failure_count} failures, eta ${fmt(row.eta_mle)} h` +
            (row.probability_plot_r_squared != null
              ? `, plot R² ${Number(row.probability_plot_r_squared).toFixed(2)}${row.probability_plot_review ? ", below its review threshold" : ""}`
              : "") +
            ")" +
            rankingMarker(row),
        })
      );
    });
  }

  // Why a ranked fit wants running again, if it does: saved under an earlier method,
  // or counted on a schedule the asset has since been moved off.
  function rankingMarker(row) {
    if (row.method_current === false) return " — saved under an earlier method; run it again";
    if (row.schedule_current === false) return " — counted on the asset's old schedule; run it again";
    if (row.time_zone_current === false) return " — counted in an earlier plant time zone; run it again";
    return "";
  }

  // The "Most likely to fail soon" window, in weeks: what the box says when that is a
  // whole number of weeks from 1 to 52 (a fraction is refused, not rounded),
  // otherwise the last window that was. The box is
  // put back to the window used, so the sentence around it never names another.
  function riskWeekValue(box) {
    const text = box ? String(box.value).trim() : "";
    const weeks = Number(text);
    return text !== "" && Number.isInteger(weeks) && weeks >= 1 && weeks <= 52 ? weeks : null;
  }

  function riskWeeks() {
    const box = $("lda-risk-weeks");
    const weeks = riskWeekValue(box);
    if (weeks != null) state.riskWeeks = weeks;
    if (box && box.value !== String(state.riskWeeks)) box.value = String(state.riskWeeks);
    return state.riskWeeks;
  }

  function onRiskWeeksChange() {
    if (riskWeekValue($("lda-risk-weeks")) == null) {
      riskWeeks();
      showBanner("The window must be a whole number of weeks from 1 to 52.", "error");
      return;
    }
    refreshRiskRankings();
  }

  function renderRiskRankings(rankings) {
    const list = $("lda-risk-rankings");
    if (!list) return;
    list.innerHTML = "";
    if (!rankings.length) {
      list.appendChild(el("li", { class: "is-empty", text: "No saved Weibull mechanism results with enough failures to rank yet." }));
      return;
    }
    rankings.forEach((row) => {
      const percent = 100 * Number(row.probability);
      const asOf = cutoffPlantDate(row.analysis_cutoff, row.analysis_cutoff_source, row.time_zone);
      list.appendChild(
        el("li", {
          text:
            `${row.failure_mechanism_name}: ${percent.toFixed(percent < 10 ? 1 : 0)}% chance of failing in the next ` +
            `${fmt(row.window_weeks)} weeks (current life ${fmt(row.current_life_hours)} h, beta ${fmt(row.beta_mle)}, ` +
            `${row.failure_count} failures${row.probability_plot_review ? ", plot R² below its review threshold" : ""}` +
            `${asOf ? `, as of ${asOf}` : ""})` +
            rankingMarker(row),
        })
      );
    });
  }

  async function refreshRiskRankings() {
    if (!state.selectedAsset || !$("lda-risk-rankings")) return;
    const asset = state.selectedAsset;
    const token = ++state.riskToken;
    try {
      const data = await getJson(`${API}/risk-rankings?asset=${encodeURIComponent(asset)}&weeks=${riskWeeks()}`);
      if (token !== state.riskToken || state.selectedAsset !== asset) return;
      renderRiskRankings(data.rankings || []);
    } catch (err) {
      if (token === state.riskToken) showBanner(err.message, "error");
    }
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
      ctx.strokeStyle = C.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.fillStyle = C.label;
      ctx.textAlign = "right";
      ctx.fillText(tickLabel(maxVal * frac), left - 7, y);
      ctx.textAlign = "left";
      ctx.fillText(Math.round(frac * 100) + "%", right + 7, y);
    }
    ctx.textBaseline = "alphabetic";

    // Left and right axes.
    ctx.strokeStyle = C.axis;
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
      ctx.fillStyle = C.bar;
      ctx.fillRect(x, y, barW, barHeight);
      hitboxes.push({ x, y: top, w: barW, h: plotH, row });

      ctx.save();
      ctx.fillStyle = C.label;
      ctx.font = "10px Inter, sans-serif";
      ctx.translate(x + barW / 2, bottom + 6);
      ctx.rotate(Math.PI / 5);
      const label = (row.failure_mechanism_name || "—").slice(0, 18);
      ctx.fillText(label, 0, 0);
      ctx.restore();
    });

    // Cumulative percent line + markers (right axis scale: 0..100%).
    ctx.strokeStyle = C.highlight;
    ctx.lineWidth = 2;
    ctx.beginPath();
    rows.forEach((row, index) => {
      const x = left + index * slot + slot / 2;
      const y = bottom - ((Number(row._cumulative_percent) || 0) / 100) * plotH;
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.fillStyle = C.highlight;
    rows.forEach((row, index) => {
      const x = left + index * slot + slot / 2;
      const y = bottom - ((Number(row._cumulative_percent) || 0) / 100) * plotH;
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    });

    // Rotated axis titles on both sides.
    ctx.fillStyle = C.label;
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

  // The topbar is pinned at the top of the viewport and the Step 1 card is pinned
  // directly below it, so a plain scrollIntoView({block:"start"}) parks the target's
  // heading underneath them. scrollIntoView honours scroll-margin-top, so reserve the
  // pinned height there. offsetHeight (not getBoundingClientRect) is used for the
  // Step 1 card because its on-screen box depends on how far the page is already
  // scrolled, while its layout height does not.
  const STICKY_TOPBAR_HEIGHT = 74;
  function scrollBelowSticky(node) {
    if (!node) return;
    // While the tour is open it decides what is on screen. A scroll of the page's
    // own, landing a frame after the tour's, would carry the lit part away.
    if (window.gremlinTour && window.gremlinTour.isOpen()) return;
    const step1 = $("lda-step1-card");
    const pinned = STICKY_TOPBAR_HEIGHT + (step1 && !step1.hidden ? step1.offsetHeight : 0);
    node.style.scrollMarginTop = `${pinned + 12}px`;
    node.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // First panel a Pareto click should bring into view, per analysis type. Weibull
  // renders a whole results card into the workspace and scrolls to that itself; the
  // other types update panels that are already on the page, so nothing would move the
  // viewport without this.
  const ANALYSIS_SCROLL_TARGETS = {
    [ANALYSIS_TYPES.TREND]: "lda-trend-chart-panel",
    [ANALYSIS_TYPES.PM]: "lda-pm-chart-panel",
    [ANALYSIS_TYPES.DOWNTIME]: "lda-downtime-trend-panel",
    [ANALYSIS_TYPES.REPEAT]: "lda-repeat-pairs-panel",
  };

  // Scroll to the active analysis type's first result panel. Deferred to the next
  // frame so it runs after the render that precedes it has laid out — the summary
  // cards above the panel change height when a first selection populates them, which
  // would otherwise move the target out from under an already-started smooth scroll.
  function scrollToAnalysisPanel() {
    const id = ANALYSIS_SCROLL_TARGETS[state.analysisType];
    const panel = id ? $(id) : null;
    if (!panel || panel.hidden) return;
    requestAnimationFrame(() => scrollBelowSticky(panel));
  }

  // A Pareto bar click drives the active analysis: Weibull runs the clicked
  // mechanism's fit; Failure Mode Trend selects it as the trended mechanism. The
  // not-yet-implemented types have no secondary action, so the click is ignored.
  // Resolves once the analysis has drawn, for the tour, which clicks one too.
  function onParetoBarSelected(row) {
    if (state.analysisType === ANALYSIS_TYPES.TREND) {
      return selectTrendMechanism(row);
    } else if (state.analysisType === ANALYSIS_TYPES.PM) {
      return selectPmMechanism(row);
    } else if (state.analysisType === ANALYSIS_TYPES.DOWNTIME) {
      return selectDowntimeMechanism(row);
    } else if (state.analysisType === ANALYSIS_TYPES.REPEAT) {
      return selectRepeatMechanism(row);
    } else if (state.analysisType === ANALYSIS_TYPES.WEIBULL) {
      return runParetoMechanism(row);
    }
    return undefined;
  }

  // `options` goes on to runAnalysisForGroup.
  function runParetoMechanism(row, options) {
    if (row.failure_mode_id == null || row.failure_mechanism_id == null) {
      showBanner("The selected Pareto bar does not have a complete failure mode/mechanism selection.", "error");
      return undefined;
    }
    setActiveMechanism(row);
    return runAnalysisForGroup(
      {
        grouping_level: "FAILURE_MECHANISM",
        failure_mode_id: row.failure_mode_id,
        failure_mechanism_id: row.failure_mechanism_id,
      },
      "Running clicked mechanism Weibull analysis…",
      options
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
    if (type === ANALYSIS_TYPES.REPEAT) {
      // The repeats load for every mechanism anyway; the carried one only narrows
      // the list, so the type's own load below still runs.
      state.repeatFilter =
        active.failure_mechanism_id != null
          ? {
              failure_mode_id: active.failure_mode_id,
              failure_mechanism_id: active.failure_mechanism_id,
              label: pmSelectionLabel(active),
            }
          : null;
      return false;
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
    const isRepeat = type === ANALYSIS_TYPES.REPEAT;
    const isPlaceholder = !isWeibull && !isTrend && !isPm && !isDowntime && !isRepeat;

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
        : isRepeat
        ? "Repeat fix rate summary"
        : type;
    }

    // Weibull-specific cards/sections are hidden for every non-Weibull type so no
    // Weibull labels remain on screen.
    setHidden($("lda-weibull-summary"), !isWeibull);
    setHidden($("lda-beta-panel"), !isWeibull);
    setHidden($("lda-risk-panel"), !isWeibull);
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
    setHidden($("lda-repeat-summary"), !isRepeat);
    setHidden($("lda-repeat-rate-panel"), !isRepeat);
    setHidden($("lda-repeat-pairs-panel"), !isRepeat);
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
    if (isRepeat) {
      if (state.selectedAsset && !state.repeatData) loadRepeatFixes();
      else renderRepeat();
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
    // The trend is computed from data already on the client, so the panels are fully
    // rendered by this point and the viewport can follow the selection down to them.
    scrollToAnalysisPanel();
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
        hint.textContent =
          `Work orders for ${monthLabel(state.trendSelectedMonth)}. Click the month again to show every month in range.` +
          RECORD_EDIT_HINT;
      } else {
        hint.textContent =
          "The specific work orders that populate the months plotted above. Click a month/data point to drill in." +
          RECORD_EDIT_HINT;
      }
    }
    const headers = ["WO #", "WO Title", "Month", "Downtime (h)", "Request Description", "Completion Notes", "Failure Narrative"];
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
            recordNumberCell(record.task_id, { mappedRecordId: record.mapped_record_id, kind: "wo" }),
            el("td", { text: record.task_name || "" }),
            el("td", { text: monthLabel(record.month) }),
            el("td", { text: record.downtime_hours != null ? fmtFixed(record.downtime_hours) : "" }),
            el("td", { class: "lda-wo-text", text: record.requestor_description || "" }),
            el("td", { class: "lda-wo-text", text: record.completion_notes || "" }),
            narrativeCell(record),
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
      ctx.strokeStyle = C.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.fillStyle = C.label;
      ctx.textAlign = "right";
      ctx.fillText(String(Math.round(maxVal * frac)), left - 7, y);
    }
    ctx.textBaseline = "alphabetic";

    // Axes.
    ctx.strokeStyle = C.axis;
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
    ctx.fillStyle = C.label;
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
    ctx.strokeStyle = C.bar;
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
      ctx.fillStyle = isSelected ? C.ink : C.highlight;
      ctx.beginPath();
      ctx.arc(x, y, isSelected ? 5 : 3, 0, Math.PI * 2);
      ctx.fill();
      if (isSelected) {
        ctx.strokeStyle = C.ink;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(x, y, 8, 0, Math.PI * 2);
        ctx.stroke();
      }
      pointHitboxes.push({ x, y, index });
    });

    // Axis titles.
    ctx.fillStyle = C.ink;
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
          labelText: `${modeName}: all mechanisms (${totalCount} WOs, ${fmt(totalDowntime)} downtime h)`,
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
    // Scroll only once the response has rendered: the summary cards sit above the
    // chart panel and change height when they populate, which would drag the panel
    // out from under a scroll started now.
    return loadPmEffectiveness({ scrollToPanel: true });
  }

  async function loadPmEffectiveness(opts) {
    const scrollWhenRendered = Boolean(opts && opts.scrollToPanel);
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
      if (scrollWhenRendered) scrollToAnalysisPanel();
    } catch (err) {
      if (!isStale()) {
        showBanner(err.message, "error");
        // Reflect the (now-cleared) data so a failed load doesn't leave another
        // mechanism's results on screen; an in-place refresh keeps its own data.
        renderPm();
        // The panel now says why there is nothing to plot, so the click still lands
        // somewhere the user can read rather than leaving them where they clicked.
        if (scrollWhenRendered) scrollToAnalysisPanel();
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
          recordNumberCell(row.corrective_wo_number, { mappedRecordId: row.corrective_mapped_record_id, kind: "wo" }),
        ])
      );
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  // ---- repeat fix rate -------------------------------------------------------
  // Asset-wide: every mechanism's repeats load at once, so there is nothing to pick
  // before it draws. A Pareto click, or a row in the rate table, narrows the list of
  // repeats to one mechanism; clicking the same one again shows them all.
  function repeatWindowHours() {
    const input = $("lda-repeat-window");
    const value = input ? Number(input.value) : state.repeatWindow;
    if (!isFinite(value) || value <= 0 || value > 720) return null;
    return value;
  }

  function onRepeatWindowChange() {
    const value = repeatWindowHours();
    if (value == null) {
      showBanner("The repeat window must be more than 0 and at most 720 scheduled hours.", "error");
      const input = $("lda-repeat-window");
      if (input) input.value = String(state.repeatWindow);
      return;
    }
    if (value === state.repeatWindow && state.repeatData) return;
    state.repeatWindow = value;
    loadRepeatFixes();
  }

  async function loadRepeatFixes(opts) {
    const scrollWhenRendered = Boolean(opts && opts.scrollToPanel);
    if (state.pageMode === "disposition") return;
    if (!state.selectedAsset) {
      renderRepeat();
      return;
    }
    const asset = state.selectedAsset;
    const windowHours = state.repeatWindow;
    const token = ++state.repeatToken;
    const isStale = () =>
      token !== state.repeatToken ||
      state.selectedAsset !== asset ||
      state.analysisType !== ANALYSIS_TYPES.REPEAT;
    beginLoading("Finding repeat failures…");
    try {
      const params = new URLSearchParams({ asset, window_hours: String(windowHours) });
      const data = await getJson(`${API}/repeat-fixes?${params.toString()}`);
      if (isStale()) return;
      state.repeatData = data.repeat_fixes || null;
      renderRepeat();
      if (scrollWhenRendered) scrollToAnalysisPanel();
    } catch (err) {
      if (!isStale()) {
        state.repeatData = null;
        showBanner(err.message, "error");
        renderRepeat();
      }
    } finally {
      endLoading();
    }
  }

  // Narrow the repeats list to one mechanism, or show them all again when it is the
  // one already shown. Resolves once drawn, for the tour.
  function selectRepeatMechanism(row) {
    if (row == null || row.failure_mechanism_id == null) {
      showBanner("Repeat fix rate is per failure mechanism. Pick a mechanism-level Pareto bar.", "error");
      return undefined;
    }
    setActiveMechanism(row);
    state.repeatFilter = selectionMatches(state.repeatFilter, row)
      ? null
      : {
          failure_mode_id: row.failure_mode_id,
          failure_mechanism_id: row.failure_mechanism_id,
          label: pmSelectionLabel(row),
        };
    if (!state.repeatData) return loadRepeatFixes({ scrollToPanel: true });
    renderRepeat();
    scrollToAnalysisPanel();
    return undefined;
  }

  function clearRepeatFilter() {
    state.repeatFilter = null;
    renderRepeat();
  }

  function renderRepeat() {
    renderRepeatCards();
    renderRepeatRates();
    renderRepeatPairs();
  }

  function fmtRate(rate) {
    return rate == null ? "—" : `${fmt(rate * 100, 3)}%`;
  }

  function renderRepeatCards() {
    const grid = $("lda-repeat-cards");
    const message = $("lda-repeat-message");
    if (!grid) return;
    grid.innerHTML = "";
    const data = state.repeatData;
    if (!data) {
      if (message) {
        message.hidden = false;
        message.textContent = state.selectedAsset ? "Finding repeat failures…" : "Select an asset to find its repeat failures.";
      }
      return;
    }
    // [label, value, detail]: a mechanism's card names it under the value, the way
    // the Failure Mode Trend cards do.
    const mechanismCard = (label, entry, value) =>
      entry
        ? [label, value(entry), `${entry.failure_mechanism_name}: ${entry.repeats} of ${entry.intervals} gaps`]
        : [label, "None", null];
    const cards = [
      [
        "Repeat Fix Rate",
        data.repeat_rate == null ? "Insufficient Data" : fmtRate(data.repeat_rate),
        data.repeat_rate == null ? null : `${data.repeats} of ${data.intervals} gaps`,
      ],
      mechanismCard(`Highest Rate (${data.min_intervals_for_rate}+ gaps)`, data.highest_rate, (e) => fmtRate(e.repeat_rate)),
      mechanismCard("Most Repeats", data.most_repeats, (e) => String(e.repeats)),
      ["Possible Duplicates", String(data.possible_duplicates ?? 0), "repeats within 1 calendar hour"],
    ];
    cards.forEach(([label, value, detail]) => {
      grid.appendChild(
        el("div", { class: "lda-metric lda-trend-metric" }, [
          el("span", { class: "lda-metric-label", text: label }),
          el("span", { class: "lda-metric-value lda-trend-metric-value", text: value }),
          detail ? el("span", { class: "lda-metric-label", text: detail }) : null,
        ])
      );
    });
    if (message) {
      const notes = [];
      if (!data.intervals) {
        notes.push("No mechanism on this asset has two dated included failures yet, so there is no gap to measure.");
      }
      if (data.undated_failures) {
        notes.push(
          `${data.undated_failures} included ${data.undated_failures === 1 ? "failure has" : "failures have"} no completed ` +
            "date and could not be placed."
        );
      }
      if (data.possible_duplicates) {
        notes.push(
          "A possible duplicate closed within an hour of the failure before it: check it is not the same breakdown " +
            "recorded twice before reading it as a repeat."
        );
      }
      message.hidden = !notes.length;
      message.textContent = notes.join(" ");
    }
  }

  function renderRepeatRates() {
    const wrap = $("lda-repeat-rate-wrap");
    const basis = $("lda-repeat-basis");
    const input = $("lda-repeat-window");
    if (input && document.activeElement !== input) input.value = String(state.repeatWindow);
    if (!wrap) return;
    wrap.innerHTML = "";
    const data = state.repeatData;
    if (basis) {
      basis.textContent = data
        ? `Counted on the ${data.schedule_name} schedule, with days split at midnight ${data.time_zone}.` +
          (data.time_zone_warning ? ` ${data.time_zone_warning}` : "")
        : "";
    }
    const headers = ["Failure Mechanism", "Failure Mode", "Failures", "Gaps", "Repeats", "Repeat Rate"];
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { text: h })))]));
    const tbody = el("tbody");
    const rows = (data && data.mechanisms) || [];
    if (!rows.length) {
      tbody.appendChild(
        el("tr", {}, [
          el("td", {
            class: "lda-readonly lda-empty-row",
            colspan: String(headers.length),
            text: data ? "No included failures with a failure mechanism on this asset yet." : "",
          }),
        ])
      );
    }
    rows.forEach((row) => {
      const isActive = selectionMatches(state.repeatFilter, row);
      tbody.appendChild(
        el(
          "tr",
          {
            class: `lda-trend-month-row${isActive ? " is-active" : ""}`,
            title: "Click to list only this mechanism's repeats below",
            onclick: () => selectRepeatMechanism(row),
          },
          [
            el("td", { text: row.failure_mechanism_name || "" }),
            el("td", { text: row.failure_mode_name || "" }),
            el("td", { text: String(row.failures) }),
            el("td", { text: String(row.intervals) }),
            el("td", { text: String(row.repeats) }),
            el("td", { text: fmtRate(row.repeat_rate) }),
          ]
        )
      );
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
  }

  function renderRepeatPairs() {
    const wrap = $("lda-repeat-pairs-wrap");
    if (!wrap) return;
    wrap.innerHTML = "";
    const data = state.repeatData;
    const filter = state.repeatFilter;
    const filterBar = $("lda-repeat-filter");
    const filterText = $("lda-repeat-filter-text");
    setHidden(filterBar, !filter);
    if (filterText) filterText.textContent = filter ? `Showing ${filter.label} only.` : "";
    const rows = ((data && data.pairs) || []).filter((pair) => !filter || selectionMatches(filter, pair));
    const headers = [
      "Failure Mechanism",
      "Previous WO",
      "Previous Completed",
      "Repeat WO",
      "Repeat Completed",
      "Scheduled Hours",
      "Calendar Hours",
      "Check",
    ];
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { text: h })))]));
    const tbody = el("tbody");
    if (!rows.length) {
      const text = !data
        ? ""
        : filter
        ? `No repeats of ${filter.label} within ${fmt(data.window_hours)} scheduled hours.`
        : `No failure came back within ${fmt(data.window_hours)} scheduled hours.`;
      tbody.appendChild(
        el("tr", {}, [el("td", { class: "lda-readonly lda-empty-row", colspan: String(headers.length), text })])
      );
    }
    rows.forEach((pair) => {
      tbody.appendChild(
        el("tr", {}, [
          el("td", { text: pair.failure_mechanism_name || "" }),
          recordNumberCell(pair.prior_task_id, { mappedRecordId: pair.prior_mapped_record_id, kind: "wo" }),
          el("td", { text: formatRecordDate(pair.prior_completed_raw ?? pair.prior_completed) }),
          recordNumberCell(pair.repeat_task_id, { mappedRecordId: pair.repeat_mapped_record_id, kind: "wo" }),
          el("td", { text: formatRecordDate(pair.repeat_completed_raw ?? pair.repeat_completed) }),
          el("td", { text: fmtFixed(pair.scheduled_hours, 3) }),
          el("td", { text: fmtFixed(pair.raw_hours, 3) }),
          el("td", { text: pair.duplicate_check ? "Possible duplicate" : "" }),
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
  // record kind, or scope changes. Resolves once the editor is drawn.
  function reloadDispositionForSelection() {
    if (!state.selectedAsset) {
      clearWorkspace();
      return Promise.resolve();
    }
    return loadDispositionPage(state.dispositionKind, state.dispositionScope, state.dispositionPageIndex || 0);
  }

  async function loadDispositionPage(kind, scope, pageIndex) {
    // Claim this reload; a newer one (or a workspace clear) bumps the token and
    // supersedes us, so the response below is dropped instead of rendering stale.
    const token = ++state.dispositionToken;
    beginLoading("Loading disposition editor…");
    try {
      const search = state.dispositionSearch ? `&search=${encodeURIComponent(state.dispositionSearch)}` : "";
      const sort = state.dispositionSort.key
        ? `&sort=${encodeURIComponent(state.dispositionSort.key)}&dir=${state.dispositionSort.dir}`
        : "";
      const url = `${API}/dispositions?asset=${encodeURIComponent(state.selectedAsset)}&kind=${kind}&scope=${scope}&page=${pageIndex}${search}${sort}`;
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

  // The name -> id maps a WO save resolves typed taxonomy through. Built once per
  // payload rather than once per row: every row of a page shares the options.
  function dispositionTaxonomy(data) {
    return {
      // Failure-mode names are globally unique, so a name -> id map is safe.
      modeMap: new Map(data.mode_options.map((o) => [o.failure_mode_name, o.failure_mode_id])),
      // Mechanism names can repeat across modes, so key by (name, parent mode id).
      mechByNameMode: new Map(
        data.mechanism_options.map((o) => [mechKey(o.failure_mechanism_name, o.failure_mode_id), o.failure_mechanism_id])
      ),
    };
  }

  // The editable half of one record's disposition: a control per
  // DISPOSITION_EDIT_COLUMNS key, plus the row state a save is read from. Shared
  // by the disposition table (a row per record) and the analysis tables'
  // single-record editor, so a record starts from the same defaults and saves the
  // same payload wherever it is edited. Each cell carries the nodes to place, the
  // class the table gives its <td>, and the one control a <label> points at.
  function buildDispositionControls(row, data, taxonomy) {
    const isPm = data.kind === "pm";
    const notes = el("textarea", { class: "lda-textarea" });
    notes.value = row.disposition_notes || row.disposition_text || "";

    const category = buildSelect(data.categories, row.disposition_category || "UNKNOWN");
    const recordClass = buildSelect(data.record_classes, row.effective_record_class || (isPm ? "PM" : "CORRECTIVE_WO"));

    let decision = null;
    let mode;
    let mech;
    let rationale = null;
    if (isPm) {
      decision = buildSelect(data.pm_reset_decisions, row.pm_reset_inclusion_decision || "NEEDS_REVIEW");
      // PMs may only reference existing modes/mechanisms, so the searchable
      // dropdown is restricted to known options (allowFreeText: false).
      mode = buildTaxonomyCombobox(data.mode_options, "failure_mode_id", "failure_mode_name", row.reset_target_failure_mode_id, { allowFreeText: false });
      mech = buildTaxonomyCombobox(data.mechanism_options, "failure_mechanism_id", "failure_mechanism_name", row.reset_target_failure_mechanism_id, {
        allowFreeText: false,
        contextIdKey: "failure_mode_id",
        getContextId: () => mode.getSelectedId(),
      });
      rationale = el("textarea", { class: "lda-textarea" });
      rationale.value = row.pm_reset_renewal_rationale || "";
    } else {
      // WO failure mode/mechanism allow typing a new value as well as picking
      // an existing one (allowFreeText: true); ids resolve by name on save.
      mode = buildTaxonomyCombobox(data.mode_options, "failure_mode_id", "failure_mode_name", row.failure_mode_id, { allowFreeText: true });
      mech = buildTaxonomyCombobox(data.mechanism_options, "failure_mechanism_id", "failure_mechanism_name", row.failure_mechanism_id, { allowFreeText: true });
    }

    // A saved disposition always stores an explicit include flag, so the box
    // shows that: an INCLUDED_FAILURE saved unticked has to stay unticked, or an
    // edit to anything else on the row would save it back into the Weibull fit.
    // Only a record never dispositioned falls back to what its category implies.
    const currentCategory = row.disposition_category || "UNKNOWN";
    const defaultInclude =
      row.event_disposition_id != null
        ? Boolean(row.include_in_weibull_candidate)
        : (!isPm && currentCategory === "INCLUDED_FAILURE") ||
          (isPm && currentCategory === "INCLUDED_PM_RESET_EVENT" && row.pm_reset_inclusion_decision === "APPROVED_RESET");
    const include = el("input", { type: "checkbox" });
    include.checked = defaultInclude;

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
      modeOptions: taxonomy.modeMap,
      mechByNameMode: taxonomy.mechByNameMode,
    };
    rowState.initial = JSON.stringify(dispositionPayloadFromRow(rowState, data.kind));

    const cells = {
      disposition_notes: { nodes: [notes], control: notes },
      disposition_category: { nodes: [category], control: category },
      effective_record_class: { nodes: [recordClass], control: recordClass },
      [isPm ? "reset_target_failure_mode" : "failure_mode"]: { nodes: mode.nodes, control: mode.input },
      [isPm ? "reset_target_failure_mechanism" : "failure_mechanism"]: { nodes: mech.nodes, control: mech.input },
      modeled_population_name: {
        cls: "lda-readonly",
        // Named by the server, which also orders this column by it, so the two
        // cannot drift into sorting by something the cell does not say.
        nodes: [row.modeled_population_name || data.modeled_population_placeholder],
        control: null,
      },
      include_in_weibull_candidate: { cls: "lda-check", nodes: [include], control: include },
    };
    if (isPm) {
      cells.pm_reset_inclusion_decision = { nodes: [decision], control: decision };
      cells.pm_reset_renewal_rationale = { nodes: [rationale], control: rationale };
    }
    return { rowState, cells };
  }

  function renderDispositionEditor(data) {
    const isPm = data.kind === "pm";
    const workspace = $("lda-workspace");
    workspace.innerHTML = "";

    const taxonomy = dispositionTaxonomy(data);

    // Every column of the table, in the order it is drawn, each named by the key
    // the server orders that column by and typed by what its values really are.
    // The header row, the cells and the column menus are all built from this, so
    // a header can never offer a sort the server does not have -- and the two
    // date columns and the two numeric ones are compared as dates and numbers
    // rather than as the text they are stored and rendered as.
    const columnTypes = data.sortable_columns || {};
    const typed = (column) => Object.assign({}, column, { type: columnTypes[column.key] || "text" });
    // The read-only source columns are named by the server; it labels them with
    // their own keys, which is what the Excel workbook calls them too.
    const sourceColumns = data.display_columns.map((key) =>
      typed({ key, label: key, cls: key === "name" ? "lda-col-name" : null })
    );
    const narrativeColumn = typed({ key: "failure_narrative", label: "Failure Narrative" });
    const extraColumns = DISPOSITION_EDIT_COLUMNS[isPm ? "pm" : "wo"].map(typed);
    const columns = sourceColumns.concat([narrativeColumn], extraColumns);

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
    // The narrative reports what the maintenance team recorded, so it belongs with
    // the other read-only source columns rather than among the editable ones.
    const narrativeFields = data.narrative_columns || NARRATIVE_FIELDS;
    // The editable columns' header and cells carry their key, which is how the
    // tour lights one column at a time (see DISPOSITION_COLUMN_TOUR_STEPS).
    const editColumn = (column) => (extraColumns.includes(column) ? column.key : null);
    const thead = el("thead", {}, [
      el(
        "tr",
        {},
        columns.map((column) =>
          el("th", { class: column.cls || null, "data-disp-col": editColumn(column), text: column.label })
        )
      ),
    ]);
    const tbody = el("tbody");

    data.rows.forEach((row) => {
      const tr = el("tr");
      sourceColumns.forEach((column) => {
        const value = row[column.key];
        // A date column is shown in one normalised shape rather than in whatever
        // the source system wrote, so the column reads (and filters) as a date.
        const text =
          column.type === "datetime" ? formatRecordDate(value) : value == null ? "" : String(value);
        tr.appendChild(el("td", { class: ["lda-readonly", column.cls].filter(Boolean).join(" "), text }));
      });
      tr.appendChild(
        narrativeCell(row, {
          fields: narrativeFields,
          cls: "lda-readonly",
          // Work orders completed before the Limble template asked for these
          // boxes have nothing to show, and saying so beats an empty cell that
          // looks like a rendering fault.
          emptyText: "Not recorded",
        })
      );

      const controls = buildDispositionControls(row, data, taxonomy);
      extraColumns.forEach((column) => {
        const cell = controls.cells[column.key];
        tr.appendChild(el("td", { class: cell.cls || null, "data-disp-col": column.key }, cell.nodes));
      });
      controls.rowState.tr = tr;
      rowStates.push(controls.rowState);
      tbody.appendChild(tr);
    });

    if (!data.rows.length) {
      const colCount = columns.length;
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

    // The server did the ordering, so trust what it says it applied: a column
    // that does not exist for this record type (a WO sort still selected when
    // the Record Type switches to PM) comes back cleared.
    state.dispositionSort = { key: data.sort || "", dir: data.sort_dir === "desc" ? "desc" : "asc" };

    // A value filter narrows what is on screen, so it is kept while the same
    // selection is paged and sorted, and dropped when the selection itself
    // changes: a different asset, record type, scope or search shows a different
    // set of records, and values picked out of the old one would hide most of it.
    const selection = [data.asset_number, data.kind, data.scope, data.search || ""].join("\u0000");
    if (state.dispositionFilterSelection !== selection) {
      state.dispositionFilters = {};
      state.dispositionFilterSelection = selection;
    }

    enableTableColumnTools(table, {
      columns,
      sort: state.dispositionSort,
      filters: state.dispositionFilters,
      onFiltersChanged: (active) => { state.dispositionFilters = active; },
      // Re-asks for the selection ordered by this column, from its first page:
      // the point of sorting is to bring the top of the whole selection into
      // view, which a page that stayed put would not do. A reload, so it asks
      // before dropping unsaved edits the way paging does.
      onSort: async (key, dir) => {
        if (!(await confirmDiscardUnsavedChanges(changed))) return;
        state.dispositionSort = { key, dir };
        state.dispositionPageIndex = 0;
        loadDispositionPage(data.kind, data.scope, 0);
      },
    });

    const checkAllButton = el("button", {
      id: "lda-disp-check-all",
      class: "btn-secondary",
      text: "Check all Include in Weibull Candidate",
      onclick: async () => {
        // These rows are only worth ticking while this render is the table on
        // screen and nothing is loading to replace it. A search or asset change
        // can start a reload while the confirmation is open, or before it opens
        // (the loading veil stops a mouse, not a keyboard); ticking rows that are
        // gone, or about to be, would quietly do nothing the user can see or save.
        const settled = () => table.isConnected && loadingDepth === 0;
        if (!settled()) {
          showToast("The table is still loading. Try Check all again once it has finished.", "info");
          return;
        }
        // Respect an active column filter: only check rows the user can currently
        // see, so filtering to a subset and clicking this never silently flips
        // (and later saves) the Weibull inclusion of hidden rows.
        const visible = rowStates.filter((rs) => rs.tr.style.display !== "none");
        const toCheck = visible.filter((rs) => !rs.include.checked);
        if (!toCheck.length) {
          showToast(
            visible.length
              ? "Include in Weibull Candidate is already ticked on every row showing."
              : "No rows are showing, so there is nothing to tick.",
            "info"
          );
          return;
        }
        // One click can put a whole page of records into the fit, so say what it
        // does and let the user back out before anything changes.
        const confirmed = await openModal({
          title: "Check all Include in Weibull Candidate?",
          bodyNodes: [
            el("p", {
              text:
                `This ticks Include in Weibull Candidate on ${toCheck.length} of the ${visible.length} ` +
                "row(s) showing on this page. Rows a column filter hides and rows on other pages are left " +
                "as they are.",
            }),
            el("p", {
              text: isPm
                ? "A ticked PM reset event is used by the Weibull analysis only once it is also " +
                  "INCLUDED_PM_RESET_EVENT with APPROVED_RESET, a reset target and a rationale."
                : "A ticked work order is used by the Weibull analysis only once it is also " +
                  "INCLUDED_FAILURE with a failure mode.",
            }),
            el("p", {
              text:
                "Nothing is saved until you click Save Dispositions, so any row you did not mean to include " +
                "can still be unticked before then.",
            }),
          ],
          actions: [
            { label: "Cancel", primary: false, value: () => false },
            { label: "Check all", primary: true, value: () => true },
          ],
        });
        if (!confirmed) return;
        if (!settled()) {
          showToast(
            "The table reloaded while you were confirming, so nothing was ticked. Click Check all again " +
              "once it has loaded if you still want it.",
            "info"
          );
          return;
        }
        toCheck.forEach((rs) => {
          rs.include.checked = true;
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
    const pager = el("div", { class: "lda-pager", id: "lda-disp-pager" }, [
      prev,
      el("span", { class: "lda-page-status", text: `Page ${data.page_index + 1} of ${data.max_page_index + 1}` }),
      next,
    ]);

    const recordWord = isPm ? "PM reset event" : "work order";
    const download = el("button", {
      class: "btn-secondary",
      text: "Download Excel",
      onclick: () => downloadExcel(data.kind, data.scope),
    });
    const upload = el("button", {
      class: "btn-secondary",
      text: "Disposition via Excel",
      onclick: () => uploadExcel(data.kind, data.scope),
    });
    // Columns whose values a save can change. Sorting by one of them means the
    // saved rows may have just moved in the ordering the next page will be cut
    // from -- see the reload below.
    const editableKeys = new Set(extraColumns.map((column) => column.key));
    const save = el("button", {
      id: "lda-disp-save",
      class: "btn-primary",
      text: "Save Dispositions",
      onclick: () =>
        saveDispositions(data.kind, changed, () => {
          // A page is a slice of a global ordering, so saving a value the
          // ordering is built from moves rows across the page boundaries while
          // the screen still shows the slice from before the write. Paging on
          // from there cuts the next page out of an order that no longer
          // matches: a row already dispositioned comes round again, and one
          // that moved up past the offset is never shown at all -- silently, on
          // the screen whose whole job is to visit every record once. Reloading
          // the current page puts the view back in step with the order the next
          // OFFSET will be taken from. mapped_record_id cannot help here: it
          // breaks ties within one ordering, not between two different ones.
          if (!editableKeys.has(state.dispositionSort.key)) return;
          loadDispositionPage(data.kind, data.scope, data.page_index);
        }),
    });
    // The ids are what the page's tour points at (see DISPOSITION_EDITOR_TOUR_STEPS).
    const card = el("section", { class: "glass-card lda-card" }, [
      el("h2", { text: isPm ? "Disposition PMs" : "Disposition Work Orders" }),
      el("div", { class: "lda-disposition-meta", id: "lda-disp-meta" }, [
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
      el("p", {
        class: "lda-hint",
        text:
          "Use the ▾ menu in any column header to sort or filter. Sorting covers every row in this " +
          "selection, not just this page, so the first page holds the top of the sort. A value filter is " +
          "picked from the values on the current page and stays on while you sort and page.",
      }),
      el("div", { class: "lda-table-scroll", id: "lda-disp-table" }, [table]),
      pager,
      el("div", { class: "lda-row-actions", id: "lda-disp-actions" }, [
        excelHelpButton(),
        withTooltip(
          download,
          `Downloads ${
            data.scope === "new"
              ? `only the new / undispositioned ${recordWord} rows`
              : `every eligible ${recordWord} row`
          } for the asset you selected as an .xlsx workbook with the disposition dropdowns built in, ` +
            "dates and numbers typed so the columns sort, so you can fill them in offline. " +
            "The columns you fill in are highlighted yellow; grey-headed columns are read-only."
        ),
        withTooltip(
          upload,
          "Uploads that filled-in workbook back. Rows are matched by mapped_record_id, and every row that " +
            "differs from the saved disposition is written, so an old workbook can put stale values back " +
            "over someone else's newer edits."
        ),
        save,
      ]),
    ]);
    $("lda-workspace").appendChild(card);
    // Not under the tour, which draws the editor when it picks its example and
    // then decides for itself what is on screen: the glide would carry the step
    // it had just lit off the top.
    if (!(window.gremlinTour && window.gremlinTour.isOpen())) {
      card.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    offerDispositionTour();
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
            text: allowFreeText ? "No matches. Keep typing to add a new value." : "No matching options.",
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
    // there is no scroll container. The single-record editor on the analysis page
    // puts these in a scrolling modal instead of a table, so that clips too.
    function visibleClip() {
      const view = { top: 0, left: 0, right: window.innerWidth, bottom: window.innerHeight };
      const scroller = input.closest(".lda-table-scroll, .lda-modal");
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
      startOwedDispositionTour();
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
        // An Escape that closes the open list is spent on it: marked so a modal
        // this combobox sits in keeps itself open rather than closing too.
        if (isOpen) event.preventDefault();
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

  async function saveDispositions(kind, changedFn, afterSave) {
    const changed = changedFn();
    if (!changed.length) {
      showToast("No disposition rows changed, so nothing needed to be saved.", "info");
      return;
    }
    const payloads = changed.map((rs) => dispositionPayloadFromRow(rs, kind));
    beginLoading("Saving dispositions…");
    let saved = false;
    try {
      const result = await postJson(`${API}/dispositions/save`, { dispositions: payloads });
      changed.forEach((rs) => (rs.initial = JSON.stringify(dispositionPayloadFromRow(rs, kind))));
      showToast(`Saved ${result.saved} changed REL disposition row(s) to event_disposition.`, "success");
      saved = true;
      refreshSummary();
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      endLoading();
    }
    // Only once the write actually landed: a failed save leaves the rows on
    // screen as the user typed them, which is what they need to fix and retry.
    if (saved && typeof afterSave === "function") afterSave();
  }

  // ---- Excel dispositioning -------------------------------------------------
  // The "how it works" explainer is markup in disposition.html rather than an
  // openModal() call: it is static prose, so a native <dialog> gives it the
  // browser's own focus trap and Escape handling for free. The button that opens
  // it is built here because it belongs with the two Excel buttons, which the
  // disposition editor renders. A page without the dialog (Perform Analysis)
  // simply gets no button -- el() drops a null child.
  function excelHelpButton() {
    const dialog = $("lda-excel-help-dialog");
    if (!dialog || typeof dialog.showModal !== "function") return null;
    return el("button", {
      class: "btn-secondary lda-help-button",
      text: "How dispositioning on Excel works",
      "aria-haspopup": "dialog",
      "aria-controls": "lda-excel-help-dialog",
      onclick: () => dialog.showModal(),
    });
  }

  // Wired once at page init, since the dialog is part of the page rather than of
  // the editor that re-renders under it.
  function wireExcelHelpDialog() {
    const dialog = $("lda-excel-help-dialog");
    if (!dialog) return;
    const close = $("lda-excel-help-close");
    if (close) close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      // A native dialog reports a click on its backdrop as a click on itself.
      // Only that closes it, never a click on the content inside.
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", startOwedDispositionTour);
  }

  // The Rows selector travels with the download: the workbook is the offline
  // copy of this table, so asking for only the new rows on screen and getting a
  // file of every eligible row is the screen and the file disagreeing about what
  // was asked for. The search box and the page number deliberately do not travel
  // -- those narrow the view to look at something, while the scope names which
  // records are still outstanding.
  function downloadExcel(kind, scope) {
    const url =
      `${API}/dispositions/excel?asset=${encodeURIComponent(state.selectedAsset)}` +
      `&kind=${kind}&scope=${scope === "new" ? "new" : "all"}`;
    window.location.href = url;
  }

  async function uploadExcel(kind, scope) {
    // A successful import reloads the editor, which throws away whatever is
    // half-typed in the table. Every other path that reloads it -- paging, the
    // Record Type and Rows selectors, the search box -- asks first, so this one
    // does too, before the file picker rather than after the upload: being asked
    // once the import has already been written would be a question with no
    // answer left.
    if (!(await confirmDiscardUnsavedChanges(state.dispositionChangedFn))) return;
    const fileInput = el("input", { type: "file", accept: ".xlsx" });
    fileInput.style.display = "none";
    document.body.appendChild(fileInput);
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.remove();
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      const csrf = document.querySelector('meta[name="gremlin-csrf-token"]');
      if (csrf) form.append("csrf_token", csrf.content);
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

  // ---- single-record disposition (analysis tables) -------------------------
  // An analysis lists the records it was built from, and that is where a
  // misclassified one gets noticed: a work order sitting under the wrong
  // mechanism, or one that was never a failure at all. Its number in those
  // tables opens that one record's disposition here, with the controls the
  // disposition table uses, rather than sending the user off to page through the
  // disposition screen for it. Saving refreshes the analysis on screen, so the
  // effect of the change shows up where it was spotted.

  // The disposition kind behind a Weibull observation: the event that closed its
  // life interval was either a failure (a corrective work order) or a PM reset.
  // The trailing current-life row has no closing event, so no record to open.
  function observationRecordKind(obs) {
    if (obs.source_event_role === "FAILURE_EVENT") return "wo";
    if (obs.source_event_role === "PM_RESET_EVENT") return "pm";
    return null;
  }

  // A table cell holding a record's number. For an editor, on a row with a
  // record behind it, the number is a button that opens that record's
  // disposition; everyone else gets the number as plain text, since saving a
  // disposition is an editor's write. `ref` is { mappedRecordId, kind }, with
  // kind "wo" or "pm".
  function recordNumberCell(taskId, ref, emptyText) {
    const number = taskId != null && String(taskId).trim() !== "" ? String(taskId) : "";
    if (!CAN_EDIT || !ref || ref.mappedRecordId == null || !ref.kind) {
      return el("td", { text: number || emptyText || "" });
    }
    const noun = ref.kind === "pm" ? "PM" : "work order";
    const description = number
      ? `Review or change the disposition of ${noun} ${number}`
      : `Review or change this ${noun}'s disposition`;
    const button = el(
      "button",
      {
        type: "button",
        class: "lda-record-link",
        title: description,
        "aria-label": description,
        onclick: (event) => {
          // A row that is clickable itself (the Weibull data table jumps to the
          // graph) must not also act on a click meant for this one record.
          event.stopPropagation();
          openRecordDisposition({ mappedRecordId: ref.mappedRecordId, kind: ref.kind, trigger: button });
        },
      },
      [
        el("span", { class: "lda-record-link-number", text: number || "Edit" }),
        el("span", { class: "lda-record-link-icon", "aria-hidden": "true", text: "✎" }),
      ]
    );
    // Column sorting and filtering read the number, not the pencil beside it.
    return el("td", { class: "lda-record-cell", "data-column-text": number }, [button]);
  }

  async function openRecordDisposition(ref) {
    if (!CAN_EDIT || !state.selectedAsset || ref.mappedRecordId == null) return;
    const asset = state.selectedAsset;
    const params = new URLSearchParams({
      asset,
      kind: ref.kind === "pm" ? "pm" : "wo",
      mapped_record_id: String(ref.mappedRecordId),
    });
    beginLoading("Loading this record's disposition…");
    let data;
    try {
      data = await getJson(`${API}/dispositions/record?${params.toString()}`);
    } catch (err) {
      if (!err.toastShown) showToast(err.message, "error");
      return;
    } finally {
      endLoading();
    }
    // Nothing can change the asset under the loading overlay today, but an editor
    // opened for one asset must never save beside another asset's analysis.
    if (state.selectedAsset !== asset) return;
    const saved = await editRecordDisposition(data);
    if (ref.trigger && ref.trigger.isConnected) ref.trigger.focus();
    if (saved) refreshAfterRecordDisposition(saved);
  }

  // Field order in the editor: what decides a disposition first, free text last.
  // The table leads with the notes, which suits scanning rows but not one form.
  const RECORD_EDITOR_FIELDS = {
    wo: [
      "disposition_category",
      "effective_record_class",
      "failure_mode",
      "failure_mechanism",
      "include_in_weibull_candidate",
      "modeled_population_name",
      "disposition_notes",
    ],
    pm: [
      "disposition_category",
      "pm_reset_inclusion_decision",
      "reset_target_failure_mode",
      "reset_target_failure_mechanism",
      "effective_record_class",
      "include_in_weibull_candidate",
      "modeled_population_name",
      "pm_reset_renewal_rationale",
      "disposition_notes",
    ],
  };
  // Fields that take the editor's full width rather than half of it.
  const RECORD_EDITOR_WIDE = {
    wo: new Set(["include_in_weibull_candidate", "modeled_population_name", "disposition_notes"]),
    pm: new Set(["modeled_population_name", "pm_reset_renewal_rationale", "disposition_notes"]),
  };
  let recordEditorSeq = 0;

  // The editor itself: the record's own details for reference, then the same
  // disposition controls its table row would have. Resolves with the saved
  // record, or null when it is closed without saving. A refused save leaves it
  // open with the reason, so what was entered can be corrected rather than
  // entered again.
  //
  // Its own modal rather than openModal(), which closes on any button and on a
  // backdrop click: a form should not lose its edits to a stray click, and it
  // has to stay open across a save that fails. Not a native <dialog> either --
  // the mode/mechanism option lists are portaled to <body>, which sits beneath a
  // dialog's top layer, so they would open out of sight behind it.
  function editRecordDisposition(data) {
    return new Promise((resolve) => {
      const row = data.row;
      const kind = data.kind === "pm" ? "pm" : "wo";
      const isPm = kind === "pm";
      const number =
        row.taskID != null && String(row.taskID).trim() !== "" ? String(row.taskID) : `record ${row.mapped_record_id}`;
      const recordLabel = `${isPm ? "PM" : "WO"} ${number}`;
      const controls = buildDispositionControls(row, data, dispositionTaxonomy(data));
      const rowState = controls.rowState;
      const labels = new Map(DISPOSITION_EDIT_COLUMNS[kind].map((column) => [column.key, column.label]));
      const idPrefix = `lda-record-${++recordEditorSeq}`;

      const facts = [
        ["Title", row.name],
        ["Request title", row.requestTitle],
        ["Created", formatRecordDate(row.createdDate_Final)],
        ["Completed", formatRecordDate(row.completedDate_Final)],
        ["Downtime", row.downtime != null && row.downtime !== "" ? `${fmtFixed(row.downtime)} h` : ""],
        ["Request description", row.requestorDescription],
        ["Completion notes", row.completionNotes],
        ...narrativeLines(row, "", data.narrative_columns).map((line) => [line.label, line.text]),
      ].filter(([, value]) => value != null && String(value).trim() !== "");

      const fields = RECORD_EDITOR_FIELDS[kind].map((key) => {
        const cell = controls.cells[key];
        const label = labels.get(key);
        const wide = RECORD_EDITOR_WIDE[kind].has(key) ? " is-wide" : "";
        if (key === "include_in_weibull_candidate") {
          // Not an .lda-field: its input rules would pad and border the checkbox.
          return el("div", { class: "lda-record-check" + wide }, [
            el("label", { class: "lda-checkbox" }, [cell.control, document.createTextNode(label)]),
          ]);
        }
        const cls = "lda-field" + wide;
        if (!cell.control) {
          // Modeled Population is derived on save from the asset and the
          // mode/mechanism, so it is shown rather than edited.
          return el("div", { class: cls }, [
            el("span", { class: "lda-record-field-label", text: label }),
            el("p", { class: "lda-record-readonly", text: String(cell.nodes[0] || "") }),
          ]);
        }
        cell.control.id = `${idPrefix}-${key}`;
        return el("div", { class: cls }, [el("label", { for: cell.control.id, text: label }), ...cell.nodes]);
      });

      const message = el("p", { class: "lda-banner", role: "alert", hidden: true });
      const cancelButton = el("button", { type: "button", class: "btn-secondary", text: "Cancel" });
      const saveButton = el("button", { type: "button", class: "btn-primary", text: "Save disposition" });
      const titleId = `${idPrefix}-title`;
      const modal = el(
        "div",
        { class: "lda-modal lda-record-modal", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
        [
          el("div", {}, [
            el("p", { class: "eyebrow", text: `Asset ${data.asset_number}` }),
            el("h3", { id: titleId, text: `Edit disposition: ${recordLabel}` }),
          ]),
          facts.length
            ? el(
                "dl",
                { class: "lda-record-facts" },
                facts.flatMap(([label, value]) => [el("dt", { text: label }), el("dd", { text: String(value).trim() })])
              )
            : null,
          el("div", { class: "lda-record-fields" }, fields),
          el("p", {
            class: "lda-hint",
            text:
              (isPm
                ? "A PM only feeds the Weibull as INCLUDED_PM_RESET_EVENT with APPROVED_RESET, an existing reset " +
                  "target and a rationale, with Include in Weibull Candidate checked."
                : "A corrective WO only feeds the Weibull as INCLUDED_FAILURE with a failure mode and Include in " +
                  "Weibull Candidate checked. Moving it to another mode or mechanism moves it to that group's analysis.") +
              " Saving keeps the previous disposition in the record's history and refreshes this analysis.",
          }),
          message,
          el("div", { class: "lda-modal-actions" }, [cancelButton, saveButton]),
        ]
      );
      const backdrop = el("div", { class: "lda-modal-backdrop" }, [modal]);

      let saving = false;
      function showMessage(text, kindClass) {
        message.textContent = text;
        message.className = `lda-banner is-${kindClass}`;
        message.hidden = false;
      }
      function close(value) {
        document.removeEventListener("keydown", onKey);
        // A focused combobox closes its portaled list on blur, and removing the
        // modal from under it would skip that, stranding the list on the page.
        if (modal.contains(document.activeElement)) document.activeElement.blur();
        document.querySelectorAll("body > .lda-portal-list").forEach((node) => node.remove());
        backdrop.remove();
        resolve(value);
      }
      function onKey(event) {
        if (saving) return;
        // An Escape that closed a mode/mechanism list is marked as spent there.
        if (event.key === "Escape" && !event.defaultPrevented) {
          close(null);
          return;
        }
        if (event.key !== "Tab") return;
        // Keep Tab inside the dialog; aria-modal promises the page behind is inert.
        const focusable = Array.from(modal.querySelectorAll("button, input, select, textarea")).filter(
          (node) => !node.disabled
        );
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
      async function save() {
        const payload = dispositionPayloadFromRow(rowState, kind);
        if (JSON.stringify(payload) === rowState.initial) {
          showMessage("Nothing has changed yet. Change a field and save, or cancel to leave it as it is.", "info");
          return;
        }
        message.hidden = true;
        saving = true;
        saveButton.disabled = true;
        cancelButton.disabled = true;
        saveButton.textContent = "Saving…";
        try {
          await postJson(`${API}/dispositions/save`, { dispositions: [payload] });
        } catch (err) {
          saving = false;
          saveButton.disabled = false;
          cancelButton.disabled = false;
          saveButton.textContent = "Save disposition";
          showMessage(err.message, "error");
          return;
        }
        close({ mappedRecordId: rowState.mapped_record_id, kind, label: recordLabel });
      }

      cancelButton.addEventListener("click", () => close(null));
      saveButton.addEventListener("click", save);
      document.addEventListener("keydown", onKey);
      document.body.appendChild(backdrop);
      controls.cells.disposition_category.control.focus();
    });
  }

  // After a save from the editor, bring what is on screen back in line with the
  // data: the readiness counts, rankings and Pareto always, and the analysis the
  // record was opened from. Trend, PM and downtime re-read inside
  // refreshSummary(); a Weibull fit has to be run again, and
  // runAnalysisForGroup() refreshes the summary itself once it has.
  function refreshAfterRecordDisposition(saved) {
    showToast(`Saved the disposition for ${saved.label}. Updating the analysis…`, "success");
    if (state.analysisType === ANALYSIS_TYPES.WEIBULL && state.latestResult && state.latestResultGroup) {
      runAnalysisForGroup(state.latestResultGroup, "Re-running the Weibull analysis with the updated disposition…", {
        changedRecord: saved,
        window: state.latestResultWindow,
      });
      return;
    }
    refreshSummary();
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
    // Scroll only once the response has rendered, for the same reason as PM: the
    // summary cards above the first chart panel resize when they populate.
    return loadDowntime({ scrollToPanel: true });
  }

  async function loadDowntime(opts) {
    const scrollWhenRendered = Boolean(opts && opts.scrollToPanel);
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
      if (scrollWhenRendered) scrollToAnalysisPanel();
    } catch (err) {
      if (!isStale()) {
        showBanner(err.message, "error");
        // Reflect the (now-cleared) data so a failed load doesn't leave another
        // mechanism's results on screen.
        renderDowntime();
        // Same as PM: the panel explains the empty state, so still scroll to it.
        if (scrollWhenRendered) scrollToAnalysisPanel();
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
      hint.textContent = "Work order count by downtime range shows whether downtime comes from many short events or a few long ones.";
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
          : "The highest-downtime work orders for the selected failure mechanism, sorted by downtime (top 10)." +
            RECORD_EDIT_HINT;
    }
    const headers = ["Date", "Asset", "Location", "Failure Mechanism", "Downtime (h)", "Operator", "WO #", "Description", "Failure Narrative"];
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
            recordNumberCell(ev.task_id, { mappedRecordId: ev.mapped_record_id, kind: "wo" }, "—"),
            el("td", { class: "lda-wo-text", text: description }),
            narrativeCell(ev),
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
      ctx.strokeStyle = C.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.fillStyle = C.label;
      ctx.textAlign = "right";
      ctx.fillText(compactHours(maxVal * frac), left - 7, y);
    }
    ctx.textBaseline = "alphabetic";

    ctx.strokeStyle = C.axis;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    const labelFont = n > 36 ? 8 : n > 24 ? 9 : 10;
    ctx.fillStyle = C.label;
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

    ctx.strokeStyle = C.bar;
    ctx.lineWidth = 2;
    ctx.beginPath();
    values.forEach((value, index) => {
      const x = xAt(index);
      const y = yAt(value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.fillStyle = C.highlight;
    values.forEach((value, index) => {
      const x = xAt(index);
      const y = yAt(value);
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    });

    ctx.fillStyle = C.ink;
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
    const barColor = opts.barColor || C.bar;
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
      ctx.strokeStyle = C.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.fillStyle = C.label;
      ctx.textAlign = "right";
      ctx.fillText(tickFormat(maxVal * frac), left - 7, y);
    }
    ctx.textBaseline = "alphabetic";

    ctx.strokeStyle = C.axis;
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
        ctx.fillStyle = C.ink;
        ctx.font = "10px Inter, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(valueLabel(value), x + barW / 2, y - 4);
      }

      ctx.fillStyle = C.label;
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

    ctx.fillStyle = C.ink;
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
    // Repeat Fix Rate covers every mechanism at once, so there is nothing to pick:
    // the button counts the repeats again, with the window in the box.
    if (state.analysisType === ANALYSIS_TYPES.REPEAT) {
      state.repeatWindow = repeatWindowHours() || state.repeatWindow;
      return loadRepeatFixes({ scrollToPanel: true });
    }
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
    // A group whose events make fewer lives ending in a failure than the minimum --
    // counted as the run counts them, the first event only starting the clock -- can
    // never reach it, so it is shown, with why, but can't be chosen.
    const options = el("div", { class: "lda-modal-options" });
    let anyFittable = false;
    groups.forEach((group, index) => {
      const fittable = group.fittable !== false;
      const labelText =
        `${group.grouping_level === "FAILURE_MECHANISM" ? "Failure mechanism" : "Failure mode"}: ` +
        `${group.label} (${group.failure_count} failures, ${group.reset_count} PM resets)` +
        (fittable
          ? ""
          : ` — too few to fit: ${
              group.failure_lives_possible != null ? `${group.failure_lives_possible} ${group.failure_lives_possible === 1 ? "life ends" : "lives end"} in a failure, and ` : ""
            }a Weibull fit needs ${group.min_failure_lives || 5}`);
      const radio = el("input", { type: "radio", name: "lda-group", value: String(index) });
      if (!fittable) radio.disabled = true;
      else if (!anyFittable) radio.checked = true;
      anyFittable = anyFittable || fittable;
      options.appendChild(
        el("label", { class: "lda-modal-option" + (fittable ? "" : " is-disabled") }, [radio, el("span", { text: labelText })])
      );
    });
    // The window the lives are built in. Both are optional plant-calendar dates: the
    // start counts from that day's first minute and the cutoff to its last.
    const startInput = el("input", { class: "lda-input", type: "date", id: "lda-window-start" });
    const cutoffInput = el("input", { class: "lda-input", type: "date", id: "lda-window-cutoff" });
    const windowFields = el("div", { class: "lda-window-fields" }, [
      el("div", { class: "lda-field" }, [el("label", { for: "lda-window-start", text: "Analysis start date (optional)" }), startInput]),
      el("div", { class: "lda-field" }, [el("label", { for: "lda-window-cutoff", text: "Analysis cutoff date (optional)" }), cutoffInput]),
    ]);
    const modalError = el("p", { class: "lda-modal-error", role: "alert", hidden: true });
    const choice = await openModal({
      title: "Select failure group",
      bodyNodes: [
        el("p", { text: "Choose the failure mechanism or failure mode to analyze:" }),
        anyFittable
          ? null
          : el("p", {
              class: "lda-hint",
              text: "None of these has enough failures for a Weibull fit yet. Disposition more failures, or come back when more have been recorded.",
            }),
        options,
        windowFields,
        el("p", {
          class: "lda-hint",
          text:
            "Leave both dates blank to use all of the asset's history and censor the current life at the last " +
            "completed Limble import. Dates are days on the plant's clock: the start counts from midnight, and the " +
            "cutoff runs to the end of its day.",
        }),
        modalError,
      ],
      actions: [
        { label: "Cancel", primary: false, value: () => null },
        {
          label: "Run analysis",
          primary: true,
          validate: () => {
            const checked = options.querySelector("input[name='lda-group']:checked");
            let problem = "";
            if (!checked) problem = "Choose a failure group that has enough failures to fit.";
            // The start counts from midnight and the cutoff runs to the end of its
            // day, so the same date is a one-day window; only a later start is wrong.
            else if (startInput.value && cutoffInput.value && startInput.value > cutoffInput.value) {
              problem = "The analysis start date can't be later than the cutoff date.";
            }
            modalError.textContent = problem;
            modalError.hidden = !problem;
            return !problem;
          },
          value: () => {
            const checked = options.querySelector("input[name='lda-group']:checked");
            return checked ? { index: Number(checked.value), start: startInput.value, cutoff: cutoffInput.value } : null;
          },
        },
      ],
    });
    if (choice === null || choice === undefined) return;
    setActiveMechanism(groups[choice.index]);
    const chosenWindow = choice.start || choice.cutoff ? { start: choice.start, cutoff: choice.cutoff } : null;
    runAnalysisForGroup(groups[choice.index], undefined, { window: chosenWindow });
  }

  // Whether two Weibull groups ({ grouping_level, failure_mode_id, failure_mechanism_id })
  // name the same population. A mode-level group's mechanism id may be null or absent.
  function sameWeibullGroup(a, b) {
    if (!a || !b) return false;
    const mechanism = (group) => (group.grouping_level === "FAILURE_MECHANISM" ? group.failure_mechanism_id ?? null : null);
    return (
      a.grouping_level === b.grouping_level &&
      a.failure_mode_id == b.failure_mode_id &&
      mechanism(a) == mechanism(b)
    );
  }

  // Query string for the read-only saved-analysis lookup. The mechanism id is omitted
  // for a mode-level group so the server matches the mode-only population.
  function savedAnalysisQuery(asset, group) {
    const params = new URLSearchParams({
      asset,
      grouping_level: group.grouping_level,
      failure_mode_id: group.failure_mode_id,
    });
    if (group.failure_mechanism_id != null) params.set("failure_mechanism_id", group.failure_mechanism_id);
    return params.toString();
  }

  // `options.changedRecord` marks a re-run after a disposition saved from the data
  // table ({ mappedRecordId, label }): the new table scrolls back to that record,
  // and a fit the change has made impossible clears the old one off the screen.
  // `options.savedOnly` opens the fit already saved even for an editor, the way a
  // viewer always does: what the tour uses, so that showing somebody around never
  // runs and stores a fit of its own. `options.window` ({ start, cutoff } as
  // YYYY-MM-DD, either blank) is the analysis window an editor chose; without one
  // the run uses all history and the server's default cutoff.
  async function runAnalysisForGroup(group, message, options) {
    const changedRecord = (options && options.changedRecord) || null;
    const savedOnly = !CAN_EDIT || Boolean(options && options.savedOnly);
    const analysisWindow = (options && options.window) || null;
    if (!state.selectedAsset) return;
    const asset = state.selectedAsset;
    // Capture the analysis type too: if the user switches away from Weibull while
    // this request is in flight, applyAnalysisTypeUI() clears the workspace, so a
    // late Weibull response must not repopulate it under the non-Weibull panel.
    const analysisType = state.analysisType;
    // A response is stale when a newer lookup superseded this one, or the asset or
    // analysis type changed while it was in flight. The asset/type checks alone cannot
    // separate two groups on the same asset, so the token covers that: a slower
    // response would otherwise render the older group over the newer one, or clear the
    // workspace when the older group has nothing saved. Overlapping lookups are not
    // reachable from the UI today -- the loading overlay covers the viewport and
    // swallows the second click -- but every other analysis here carries a token for
    // the same reason, and that overlay is styling, not a guarantee.
    const token = ++state.analysisToken;
    const isStale = () =>
      token !== state.analysisToken ||
      state.selectedAsset !== asset ||
      state.analysisType !== analysisType;
    // Computing a fit rebuilds and stores event processing, observations, a dataset,
    // a run and a result, so it is an editor-only write. A viewer (or a signed-out
    // visitor) reads back the fit an editor already saved for the same group instead:
    // same shape, same rendered view, nothing written.
    beginLoading(savedOnly ? "Loading the saved Weibull analysis…" : message || "Running Weibull analysis…");
    try {
      const data = !savedOnly
        ? await postJson(`${API}/perform-analysis`, {
            asset,
            grouping_level: group.grouping_level,
            failure_mode_id: group.failure_mode_id,
            failure_mechanism_id: group.failure_mechanism_id,
            analysis_start: (analysisWindow && analysisWindow.start) || null,
            analysis_cutoff: (analysisWindow && analysisWindow.cutoff) || null,
          })
        : await getJson(`${API}/saved-analysis?${savedAnalysisQuery(asset, group)}`);
      if (isStale()) return; // superseded, or the asset/analysis type changed mid-request
      if (!data.result) {
        // Only reachable on the read-only path: the group has never been analyzed, so
        // there is nothing saved to show and a viewer cannot create it.
        clearWorkspace();
        showBanner(
          "No Weibull analysis has been saved for this failure group yet. An editor has to run it once before it can be viewed.",
          "info"
        );
        return;
      }
      state.latestResult = data.result;
      state.latestResultGroup = group;
      // A fit opened read-only (the tour, or a viewer) keeps the window it was saved
      // with, so a disposition changed from its tables re-runs the same dates.
      state.latestResultWindow = savedOnly ? savedResultWindow(data.result) : analysisWindow;
      renderAnalysisResult(data.result, { changedRecord });
      refreshSummary();
    } catch (err) {
      if (isStale()) return;
      if (changedRecord) {
        // The fit on screen predates the change -- the change may have taken away
        // the last failure it rested on -- so leaving it up would show numbers the
        // data no longer supports. The summary still has to catch up with the save.
        state.latestResult = null;
        clearWorkspace();
        showBanner(`The disposition for ${changedRecord.label} was saved, but the Weibull analysis could not be re-run: ${err.message}`, "error");
        refreshSummary();
      } else {
        // A run over all history that the data cannot support (too few failure lives,
        // no likelihood root) also removes the result saved for that group, and the
        // server says so: take a fit of it still on screen down, and let the rankings
        // drop it too. A refusal over an entered window, or any other error, leaves
        // the saved result as it was, so the fit on screen stays.
        const removed = Boolean(err.payload && err.payload.result_removed);
        if (removed && state.latestResult && sameWeibullGroup(state.latestResultGroup, group)) {
          state.latestResult = null;
          clearWorkspace();
        }
        showBanner(err.message, "error");
        if (removed) refreshSummary();
      }
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
    return `Approx. 95% CI: beta ${betaCi}; eta ${etaCi}; MTTF ${mttf}.`;
  }

  // The probability plot's R²: how straight its Kaplan-Meier failure points lie. It
  // checks the model rather than being part of the fit, so adjusting beta and eta
  // leaves it where it is.
  function fitCheckText(result) {
    const r2 = Number(result.probability_plot_r_squared);
    if (result.probability_plot_r_squared == null || !isFinite(r2)) {
      return "Probability plot R²: not available (fewer than three distinct failure points).";
    }
    const threshold = Number(result.probability_plot_r_squared_threshold);
    const against =
      result.probability_plot_r_squared_threshold == null || !isFinite(threshold)
        ? ""
        : result.probability_plot_review
          ? `, below the ${threshold.toFixed(3)} review threshold for ${result.failure_count} failures`
          : `, meeting the ${threshold.toFixed(3)} review threshold for ${result.failure_count} failures`;
    return `Probability plot R² ${r2.toFixed(3)}${against}: how straight the plotted failure points lie, a check on the model rather than part of the fit.`;
  }

  // "B10 412 h · B50 741 h", each with its calendar weeks on the result's schedule.
  function lifeMetricsText(result) {
    const part = (label, hours) => {
      if (hours == null || !isFinite(Number(hours))) return null;
      const weeks = calendarWeeksText(hours, result.life_basis);
      return `${label} ${fmt(hours)} h${weeks ? ` (${weeks})` : ""}`;
    };
    const parts = [
      part("B10 life", result.b10_life),
      part("B50 (median) life", result.b50_life),
      part("MTTF", result.mean_time_to_failure),
    ].filter(Boolean);
    return parts.length ? `${parts.join(" · ")}.` : "";
  }

  // A stored UTC timestamp on the plant's clock, "2026-03-02 07:00 America/Chicago".
  // The zone is the one the result's days were split in, so the two always agree.
  // The window an editor chose for a saved fit, as the YYYY-MM-DD plant dates the
  // Perform Analysis dialog takes, or null for all history to the default cutoff.
  // The start is the first instant of its plant day. An entered cutoff ends at the
  // next plant midnight, or at the moment of the run when its day was that day, so
  // the second before it falls on the cutoff day either way. A default cutoff (the
  // last import or the time of the run) is not an entered date and stays blank.
  function savedResultWindow(result) {
    if (!result) return null;
    const zone = (result.life_basis && result.life_basis.time_zone) || "UTC";
    const start = result.analysis_start ? plantDateOf(result.analysis_start, zone, 0) : "";
    const cutoff =
      result.analysis_cutoff_source === "USER" && result.analysis_cutoff
        ? cutoffPlantDate(result.analysis_cutoff, "USER", zone)
        : "";
    return start || cutoff ? { start, cutoff } : null;
  }

  // The YYYY-MM-DD plant date of a stored UTC instant, `secondsBefore` it.
  function plantDateOf(value, zone, secondsBefore) {
    if (!value) return "";
    const when = new Date(String(value).replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(String(value)) ? "" : "Z"));
    if (isNaN(when.getTime())) return "";
    return plantTimeText(new Date(when.getTime() - (secondsBefore || 0) * 1000).toISOString(), zone || "UTC").slice(0, 10);
  }

  // The plant day a run's cutoff falls on. An entered cutoff is stored as the next
  // plant midnight (or the moment of the run, when its day was that day), so its day
  // is the one the second before it falls on; any other cutoff is the day it is on.
  function cutoffPlantDate(value, source, zone) {
    return plantDateOf(value, zone, source === "USER" ? 1 : 0);
  }

  function plantTimeText(value, zoneName) {
    if (!value) return "";
    const when = new Date(String(value).replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(String(value)) ? "" : "Z"));
    if (isNaN(when.getTime())) return String(value);
    const zone = zoneName || "UTC";
    try {
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: zone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      }).formatToParts(when);
      const get = (type) => (parts.find((p) => p.type === type) || {}).value || "";
      return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")} ${zone}`;
    } catch (err) {
      return when.toISOString().replace("T", " ").slice(0, 16) + " UTC";
    }
  }

  const CUTOFF_SOURCE_TEXT = {
    LAST_IMPORT: "the last completed Limble import",
    USER: "the cutoff date entered for the run",
    NOW: "the time of the run",
  };

  // An entered cutoff date runs to the end of that plant day, which is stored as the
  // next day's midnight; read it back as "the end of 2025-06-30" rather than as
  // "2025-07-01 00:00". A cutoff capped at the moment of the run keeps its time.
  function cutoffText(result, zone) {
    const shown = plantTimeText(result.analysis_cutoff, zone);
    if (result.analysis_cutoff_source !== "USER" || !/ 00:00 /.test(shown)) return shown;
    const instant = new Date(String(result.analysis_cutoff).replace(" ", "T"));
    if (isNaN(instant.getTime())) return shown;
    const dayBefore = plantTimeText(new Date(instant.getTime() - 60000).toISOString(), zone);
    return `the end of ${dayBefore.slice(0, 10)} (${zone})`;
  }

  // What the lives count and the window they were built in, in one line each.
  function resultContextLines(result) {
    const basis = result.life_basis || {};
    const zone = basis.time_zone || "UTC";
    const lines = [
      `Life basis: schedule-adjusted hours, an exposure proxy rather than run-meter hours, on ${scheduleLabel(basis)}, ` +
        `with days split at midnight ${zone}.`,
      `Window: ${result.analysis_start ? `from ${plantTimeText(result.analysis_start, zone)}` : "all history"} to ` +
        `${cutoffText(result, zone) || "an unrecorded cutoff"}` +
        `${CUTOFF_SOURCE_TEXT[result.analysis_cutoff_source] ? `, ${CUTOFF_SOURCE_TEXT[result.analysis_cutoff_source]}` : ""}.`,
      `Lives: ${result.total_observation_count} in total, ${result.failure_count} ending in a failure and ` +
        `${result.censored_count} right-censored (${result.pm_reset_censored_count || 0} at a PM reset, ` +
        `${result.current_life_censored_count || 0} current life).`,
    ];
    if (result.grouping_level === "FAILURE_MODE") {
      lines.push(
        "Grouping: failure mode, the fallback for when the records cannot support one mechanism. It pools every " +
          "mechanism under the mode, which can blur beta." +
          (result.fallback_rationale ? ` Why not a mechanism: ${result.fallback_rationale}` : "")
      );
    }
    return lines;
  }

  // Notices a result has to carry: saved under an earlier method, below the minimum
  // failure count, days split in UTC for want of the plant zone, or lives that look
  // like duplicate work orders.
  function resultNotices(result) {
    const notices = [];
    const minimum = result.min_failure_lives || 5;
    if (result.method_current === false) {
      // What the current method changed since the version this result was saved under.
      const pmScope =
        "let a PM aimed at one mechanism restart its whole failure mode, and didn't let a PM aimed at the whole mode " +
        "restart the mechanisms under it";
      const changed =
        result.method_version === "life-data-v2"
          ? pmScope
          : "split days at midnight UTC, dated a work order with no completed date by its start or created date, had no " +
            `minimum failure count, and ${pmScope}`;
      notices.push(
        `Saved by an earlier version of GREMLIN's Weibull method (${result.method_version || "unrecorded"}), which ${changed}. ` +
          (CAN_EDIT ? "Run it again to apply the current rules; " : "An editor has to run it again to apply the current rules; ") +
          "it can't be reported until then."
      );
    }
    if (result.method_current !== false && result.schedule_current === false) {
      const countedOn = (result.life_basis && result.life_basis.schedule_name) || "an earlier schedule";
      notices.push(
        `Counted on the ${countedOn} schedule, but this asset is now on ${result.current_schedule_name || "another schedule"}, ` +
          "so its life hours have changed. " +
          (CAN_EDIT ? "Run it again to count them on the new schedule; " : "An editor has to run it again; ") +
          "it can't be reported until then."
      );
    }
    if (result.method_current !== false && result.schedule_current !== false && result.time_zone_current === false) {
      const splitIn = (result.life_basis && result.life_basis.time_zone) || "UTC";
      notices.push(
        `Its days were split at midnight ${splitIn}, but the plant's time zone is now ${result.current_time_zone || "another zone"}, ` +
          "so its life hours have changed. " +
          (CAN_EDIT ? "Run it again to count them in the plant's zone; " : "An editor has to run it again; ") +
          "it can't be reported until then."
      );
    }
    if (result.meets_minimum === false) {
      notices.push(
        `This result rests on ${result.failure_count} lives that end in a failure; GREMLIN needs at least ${minimum} to ` +
          "fit and report a Weibull distribution, so it is not ranked and can't be reported."
      );
    }
    if (result.probability_plot_review) {
      notices.push(
        `The probability plot is less straight than 90% of genuine Weibull samples with ${result.failure_count} failures ` +
          `(R² ${Number(result.probability_plot_r_squared).toFixed(3)}, review threshold ` +
          `${Number(result.probability_plot_r_squared_threshold).toFixed(3)}). Review the population before acting on ` +
          "beta: mixed mechanisms, a life missing its real start point, or a duplicate work order are the usual causes."
      );
    }
    if (result.life_basis && result.life_basis.time_zone_warning) notices.push(result.life_basis.time_zone_warning);
    const flagged = (result.observations || []).filter((obs) => obs.data_quality_assumption_flag).length;
    if (flagged) {
      notices.push(
        `${flagged} ${flagged === 1 ? "life ends" : "lives end"} within an hour of the event before, which is what two work ` +
          "orders for one breakdown look like. Check the flagged rows in the data table: a duplicate makes a near-zero " +
          "life that pulls beta down."
      );
    }
    return notices;
  }

  // R(t) and F(t) at an age the user enters -- the current PM interval, say -- at the
  // beta and eta on screen: the fitted values, or the ones an editor is trying.
  function buildTargetAgePanel(result) {
    const input = el("input", {
      class: "lda-input",
      type: "number",
      min: "0",
      step: "50",
      id: "lda-target-age",
      placeholder: "Life hours, e.g. a PM interval",
    });
    const output = el("p", { class: "lda-age-output", "aria-live": "polite" });
    let beta = Number(result.beta_mle);
    let eta = Number(result.eta_mle);
    let fitted = true;
    function render() {
      const age = Number(input.value);
      if (!input.value || !isFinite(age) || age <= 0) {
        output.textContent = "Enter an age in life hours to see the chance of surviving to it and of failing by it.";
        return;
      }
      if (!(beta > 0) || !(eta > 0)) {
        output.textContent = "Enter a positive beta and eta to evaluate an age.";
        return;
      }
      const reliability = Math.exp(-Math.pow(age / eta, beta));
      const weeks = calendarWeeksText(age, result.life_basis);
      output.textContent =
        `At ${fmt(age)} life hours${weeks ? ` (${weeks})` : ""}: R = ${reliability.toFixed(3)}, so about ` +
        `${(reliability * 100).toFixed(1)}% are expected to survive to that age, and F = ${(1 - reliability).toFixed(3)} ` +
        `to fail by it. At the ${fitted ? "fitted" : "beta and eta entered above, not the fitted"} values.`;
    }
    input.addEventListener("input", render);
    render();
    const node = el("div", { class: "lda-panel lda-age-panel" }, [
      el("h3", { text: "Reliability at an age" }),
      el("div", { class: "lda-field" }, [el("label", { for: "lda-target-age", text: "Age (life hours)" }), input]),
      output,
    ]);
    // The inputs open on the fitted values rounded to six places, so a pair that
    // still reads as those is the fit itself, evaluated at its unrounded values.
    const fittedText = [numericInputValue(result.beta_mle, 6), numericInputValue(result.eta_mle, 6)];
    return {
      node,
      // Redraw at another beta/eta: the editor's inputs moved.
      update(nextBeta, nextEta) {
        fitted =
          numericInputValue(nextBeta, 6) === fittedText[0] && numericInputValue(nextEta, 6) === fittedText[1];
        beta = fitted ? Number(result.beta_mle) : Number(nextBeta);
        eta = fitted ? Number(result.eta_mle) : Number(nextEta);
        render();
      },
      // The age entered, for the report, or null.
      value() {
        const age = Number(input.value);
        return input.value && isFinite(age) && age > 0 ? age : null;
      },
    };
  }

  function renderAnalysisResult(result, options) {
    const changedRecord = (options && options.changedRecord) || null;
    clearWorkspace();
    // The table and the charts point at each other: clicking a plotted point highlights
    // its data row, and clicking a data row rings that observation on the graph that
    // plots it. The charts are built second, so the row handler reads `chartApi` when it
    // fires rather than closing over the value it had here.
    let chartApi = null;
    const dataTable = buildWeibullDataTable(result, (obs) => {
      if (chartApi) chartApi.focus(obs);
    });
    const eventTable = buildEventProcessingTable(result);
    const agePanel = buildTargetAgePanel(result);

    const betaInput = el("input", { class: "lda-input", type: "number", step: "0.2", min: "0.01", value: numericInputValue(result.beta_mle, 6) });
    const etaInput = el("input", { class: "lda-input", type: "number", step: "100", min: "0.01", value: numericInputValue(result.eta_mle, 6) });
    const reasonInput = el("input", { class: "lda-input", placeholder: "Adjustment reason based on empirical data points…" });

    const charts = el("div", { class: "lda-charts" });
    chartApi = buildAnalysisCharts(charts, result, dataTable.highlight);

    function applyParameters() {
      const beta = Number(betaInput.value);
      const eta = Number(etaInput.value);
      if (beta > 0 && eta > 0) chartApi.update(beta, eta);
      agePanel.update(beta, eta);
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

    const metrics = lifeMetricsText(result);
    const notices = resultNotices(result);
    const card = el("section", { class: "glass-card lda-card fade-in-up" }, [
      el("h2", { text: "Weibull Analysis Results" }),
      el("div", { class: "lda-result-headline" }, [
        el("strong", { text: result.analysis_label || "Selected failure group" }),
        el("span", { class: "lda-result-params", text: `MLE beta: ${fmt(result.beta_mle)}    MLE eta: ${fmt(result.eta_mle)} hours` }),
        el("span", { class: "lda-hint", text: confidenceIntervalText(result) }),
        metrics ? el("span", { class: "lda-hint", text: metrics }) : null,
        el("span", { class: "lda-hint", text: fitCheckText(result) }),
      ]),
      el("div", { class: "lda-result-context" }, resultContextLines(result).map((line) => el("span", { class: "lda-hint", text: line }))),
      ...notices.map((notice) => el("p", { class: "lda-banner is-warning", role: "note", text: notice })),
      // Adjusting beta/eta and saving the adjustment are writes, so the whole row is
      // left out for a viewer or a signed-out visitor rather than shown inert. They
      // still get the full read-only view at the fitted MLE parameters.
      CAN_EDIT ? adjustRow : null,
      legend(),
      charts,
      el("p", {
        class: "lda-hint",
        text:
          "Green lines show the MLE fit; yellow lines show approximate 95% confidence-interval fits where available. " +
          "The red vertical line marks the current life: the schedule-adjusted hours from the most recent event to the analysis cutoff. " +
          "The hazard and PDF panes intentionally show only the MLE curve. Hover any plotted point or the current-life line to see its task ID, life hours, start/end dates, request description, and completion notes; click it to jump to the source Weibull data row below.",
      }),
      agePanel.node,
      panel("Results Interpretation Summary", buildInterpretationTable(result),
        "Recommendations are based on beta, eta, MTTF, the approximate 95% confidence intervals for the fitted Weibull parameters, and the probability plot R²."),
      panel("Weibull Data Used for Graphs", dataTable.node,

        "Rows are the lives included in the Weibull fit. White points are completed failures; red points are right-censored observations. " +
        "Raw elapsed hours are the calendar hours between the two events; weekend and non-run hours are taken out of them to give the life hours. " +
        "Check flags a life that ends within an hour of the event before it, a likely duplicate work order. " +
        "Click a row to jump back up to the graph that plots it, with that observation ringed. " +
        (CAN_EDIT
          ? "Click a Task ID to review or change that work order's disposition; the analysis re-runs when you save it. "
          : "") +
        "Use the ▾ menu in any column header to sort or filter the rows."),
      eventTable.node,
      // Generating the report reserves a REL report number, which is a write, so the
      // whole section is editor-only.
      CAN_EDIT ? buildReportBar(result, charts, chartApi, betaInput, etaInput, agePanel) : null,

    ]);
    $("lda-workspace").appendChild(card);
    // After a disposition saved from the data table, land back on that record
    // rather than at the top of the card it was edited from. A change that took
    // it out of this failure group leaves nothing to land on, so say so.
    if (changedRecord && (dataTable.revealRecord(changedRecord.mappedRecordId) || eventTable.revealRecord(changedRecord.mappedRecordId))) return;
    scrollBelowSticky(card);
    if (changedRecord) showToast(`${changedRecord.label} is no longer part of this Weibull analysis.`, "info");
  }

  // REL-WBL-DAT-004's event processing table: every event the failure group's
  // dispositions offered, in date order, with the note saying how it was used --
  // including the ones left out of the timeline and why. Collapsed by default; it is
  // the audit trail behind the data table, not something every reader needs.
  function buildEventProcessingTable(result) {
    const events = result.events || [];
    const excluded = events.filter((event) => event.event_role === "EXCLUDED_EVENT").length;
    const kindOf = (event) => (Number(event.is_failure_event) ? "wo" : Number(event.is_pm_reset_event) ? "pm" : null);
    const columns = [
      { label: "Seq", type: "number", get: (event) => (event.weibull_sequence_number != null ? String(event.weibull_sequence_number) : "") },
      {
        label: "Task ID",
        type: "number",
        get: (event) => (event.task_id != null ? String(event.task_id) : ""),
        node: (event) => recordNumberCell(event.task_id, { mappedRecordId: event.mapped_record_id, kind: kindOf(event) }),
      },
      { label: "Work Title", cls: "lda-data-text", get: (event) => event.task_name || "" },
      { label: "Event", get: (event) => (Number(event.is_pm_reset_event) ? "PM reset" : "Failure") },
      { label: "Completed Date", type: "datetime", get: (event) => event.completed_date_raw || "" },
      { label: "Note", cls: "lda-data-text", get: (event) => event.weibull_life_note || "" },
      { label: "Check", cls: "lda-data-text lda-check", get: (event) => event.data_quality_assumption_flag || "" },
    ];
    const table = el("table", { class: "lda-data" });
    table.appendChild(el("thead", {}, [el("tr", {}, columns.map((c) => el("th", { text: c.label, class: c.cls || null })))]));
    const tbody = el("tbody");
    const rowByRecord = new Map();
    events.forEach((event) => {
      const tr = el("tr", { class: event.event_role === "EXCLUDED_EVENT" ? "is-excluded" : null }, columns.map((c) =>
        c.node ? c.node(event) : el("td", { text: c.get(event), class: c.cls || null })
      ));
      if (event.mapped_record_id != null) rowByRecord.set(Number(event.mapped_record_id), tr);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    if (events.length) enableTableColumnTools(table, { columns });
    const summaryText =
      `Event processing table: ${events.length} ${events.length === 1 ? "event" : "events"}` +
      (excluded ? `, ${excluded} left out of the timeline` : "");
    const details = el("details", { class: "lda-panel lda-events" }, [
      el("summary", { text: summaryText }),
      events.length
        ? el("div", { class: "lda-data-scroll" }, [table])
        : el("p", { class: "lda-hint", text: "This result was saved before GREMLIN kept its events." }),
      el("p", {
        class: "lda-hint",
        text:
          "Every event this failure group's dispositions put forward, in date order. The first only starts the clock; each " +
          "later one ends a life and starts the next. An event with no completed date, a date that can't be read, or a date " +
          "outside the analysis window is listed but left out, and its note says which.",
      }),
    ]);
    // The row a record is on, opened up and highlighted: where a disposition saved
    // from this table lands when its record is no longer one of the lives.
    function revealRecord(mappedRecordId) {
      const tr = rowByRecord.get(Number(mappedRecordId));
      if (!tr) return false;
      details.open = true;
      tbody.querySelectorAll("tr.is-highlight").forEach((row) => row.classList.remove("is-highlight"));
      tr.classList.add("is-highlight");
      tr.scrollIntoView({ behavior: "smooth", block: "center" });
      return true;
    }
    return { node: details, revealRecord };
  }

  // Action bar at the bottom of the results: generates a formal Weibull report (Word
  // .docx) with the life basis and window, the charts, the interpretation summary,
  // the limitations and the lives themselves. A result saved under an earlier method,
  // or resting on fewer failure lives than the minimum, can't be reported, and the
  // bar says so rather than offering a button the server would refuse.
  function buildReportBar(result, charts, chartApi, betaInput, etaInput, agePanel) {
    let blocked = "";
    if (result.method_current === false) {
      blocked = "Run the analysis again to report it: this result was saved by an earlier version of the method.";
    } else if (result.schedule_current === false) {
      blocked = "Run the analysis again to report it: the asset has been moved to another schedule since this result was counted.";
    } else if (result.time_zone_current === false) {
      blocked = "Run the analysis again to report it: the plant's time zone has changed since this result was counted.";
    } else if (result.meets_minimum === false) {
      blocked = `A report needs at least ${result.min_failure_lives || 5} lives that end in a failure.`;
    }
    const button = el("button", {
      class: "btn-primary",
      type: "button",
      text: "Generate Weibull Report",
      disabled: Boolean(blocked),
    });
    button.addEventListener("click", () => generateWeibullReport(result, charts, button, chartApi, betaInput, etaInput, agePanel));
    return el("div", { class: "lda-report-bar" }, [
      button,
      el("p", {
        class: "lda-hint",
        text:
          blocked ||
          "Creates a formal Word report (REL-WBL-RPT-<asset>-00x.docx) for this Weibull result: the population and " +
            "grouping level, the life basis and analysis window, the fitted values with B10, B50 and the reliability at " +
            "the age entered above, the graphs, the interpretation summary, the limitations, and every life it rests on. " +
            (result.grouping_level === "FAILURE_MODE"
              ? "A failure-mode report also states why a mechanism could not be fitted instead, which it asks you for."
              : ""),
      }),
    ]);
  }

  // The reason a failure-mode population was fitted rather than one of its
  // mechanisms, which its report must state (REL-WBL-PLN-003 §8). Prefilled with
  // the one saved for the population; resolves null when cancelled.
  async function askFallbackRationale(result) {
    const textarea = el("textarea", {
      class: "lda-input lda-rationale-input",
      id: "lda-fallback-rationale",
      rows: "4",
      placeholder: "e.g. The completion notes don't separate seal wear from pressure loss, so no single mechanism can be assigned.",
    });
    textarea.value = result.fallback_rationale || "";
    const error = el("p", { class: "lda-modal-error", role: "alert", hidden: true });
    return openModal({
      title: "Why a failure mode, not a mechanism?",
      bodyNodes: [
        el("p", {
          text:
            "Failure mode is the fallback grouping, for when the records cannot support one mechanism. The report " +
            "states why this one was fitted at mode level, and the reason is kept with the population for its next report.",
        }),
        el("div", { class: "lda-field" }, [el("label", { for: "lda-fallback-rationale", text: "Reason" }), textarea]),
        error,
      ],
      actions: [
        { label: "Cancel", primary: false, value: () => null },
        {
          label: "Generate report",
          primary: true,
          validate: () => {
            const empty = !textarea.value.trim();
            error.textContent = empty ? "Write the reason before generating the report." : "";
            error.hidden = !empty;
            return !empty;
          },
          value: () => textarea.value.trim(),
        },
      ],
    });
  }

  async function generateWeibullReport(result, chartsContainer, button, chartApi, betaInput, etaInput, agePanel) {
    if (!state.selectedAsset) {
      showBanner("Select an Asset Number first.", "error");
      return;
    }
    let fallbackRationale = null;
    if (result.grouping_level === "FAILURE_MODE") {
      fallbackRationale = await askFallbackRationale(result);
      if (fallbackRationale === null || fallbackRationale === undefined) return;
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
        // Send only the saved result id (plus chart images, the age to evaluate and a
        // failure mode's rationale); the server reloads the authoritative parameters,
        // lives and interpretation summary from the database.
        {
          asset: state.selectedAsset,
          result_id: result.result_id,
          charts,
          target_age_hours: agePanel ? agePanel.value() : null,
          fallback_rationale: fallbackRationale,
        },
        "weibull-report.docx"
      );
      if (fallbackRationale) result.fallback_rationale = fallbackRationale;
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

  // Value cell for one interpretation-summary row. The MTTF row adds how many calendar
  // weeks that many life hours take on the result's own schedule: life hours exclude
  // weekends and count each weekday at the schedule's hours, so 24 hours a day of
  // running would overstate how soon the mean life comes round. Every other metric
  // renders as a plain value cell.
  function buildInterpretationValueCell(row, result) {
    const isMttf = String(row.metric || "").trim().toUpperCase() === "MTTF";
    const mttfHours = result ? Number(result.mean_time_to_failure) : NaN;
    if (!isMttf || !isFinite(mttfHours) || mttfHours <= 0) {
      return el("td", { text: row.value || "—" });
    }
    const weeks = calendarWeeksText(mttfHours, result.life_basis);
    return el("td", { class: "lda-mttf-cell" }, [
      el("span", { class: "lda-mttf-hours-value", text: row.value || `${fmt(mttfHours)} hours` }),
      weeks
        ? el("span", { class: "lda-mttf-duration", text: `${weeks[0].toUpperCase()}${weeks.slice(1)} on ${scheduleLabel(result.life_basis)}` })
        : null,
    ]);
  }

  // What a life is, in the words the data table and the report use. The stored codes
  // predate REL-WBL-DAT-004's vocabulary, so these name the interval itself: a life
  // that ends at a PM reset is censored there, and the current life at the cutoff.
  const OBSERVATION_TYPE_LABELS = {
    COMPLETED_FAILURE_LIFE: "Ends in a failure",
    PM_RESET_CENSORED_LIFE: "Censored at a PM reset",
    RIGHT_CENSORED_LIFE: "Censored at the cutoff (current life)",
  };

  function buildWeibullDataTable(result, onRowActivate) {
    // Each column knows how to render its header and pull its value from an
    // observation, so the header row and body cells can never drift apart. The
    // Task ID / Work Title / Downtime / Request Description / Completion Notes columns
    // come from the source CMMS work order that closed the life interval (joined in
    // perform_weibull_analysis); they are blank for trailing current-life rows.
    // `type` is what the column header's sort compares by: without it a date
    // column would be ordered by the digits its text happens to start with and
    // "10" would land before "9".
    const columns = [
      { label: "#", type: "number", get: (obs) => String(obs.ordered_index ?? "") },
      { label: "Observation ID", type: "number", get: (obs) => String(obs.weibull_observation_id ?? "") },
      {
        label: "Task ID",
        type: "number",
        get: (obs) => (obs.source_task_id != null ? String(obs.source_task_id) : ""),
        node: (obs) =>
          recordNumberCell(obs.source_task_id, {
            mappedRecordId: obs.source_mapped_record_id,
            kind: observationRecordKind(obs),
          }),
      },
      { label: "Work Title", cls: "lda-data-text", get: (obs) => obs.source_work_title || "" },
      { label: "Downtime (h)", type: "number", get: (obs) => (obs.source_downtime_hours != null ? fmtFixed(obs.source_downtime_hours) : "") },
      { label: "Type", get: (obs) => OBSERVATION_TYPE_LABELS[obs.observation_type] || obs.observation_type || "" },
      { label: "Life Hours", type: "number", get: (obs) => fmtFixed(obs.life_hours_for_weibull) },
      { label: "Raw Elapsed (h)", type: "number", get: (obs) => (obs.life_hours_raw_elapsed != null ? fmtFixed(obs.life_hours_raw_elapsed) : "") },
      { label: "Excl. Weekend (h)", type: "number", get: (obs) => (obs.excluded_weekend_hours != null ? fmtFixed(obs.excluded_weekend_hours) : "") },
      { label: "Excl. Non-run (h)", type: "number", get: (obs) => (obs.excluded_schedule_non_run_hours != null ? fmtFixed(obs.excluded_schedule_non_run_hours) : "") },
      { label: "Failure", type: "boolean", get: (obs) => (Number(obs.failure_indicator) ? "Yes" : "No") },
      { label: "Right Censored", type: "boolean", get: (obs) => (Number(obs.is_right_censored) ? "Yes" : "No") },
      { label: "Start Datetime", type: "datetime", get: (obs) => obs.start_datetime || "" },
      { label: "End/Cutoff Datetime", type: "datetime", get: (obs) => obs.end_datetime || obs.analysis_cutoff_datetime || "" },
      { label: "Request Description", cls: "lda-data-text", get: (obs) => obs.source_request_description || "" },
      { label: "Completion Notes", cls: "lda-data-text", get: (obs) => obs.source_completion_notes || "" },
      {
        label: "Failure Narrative",
        cls: "lda-data-text",
        get: (obs) => narrativeText(obs, "source_"),
        node: (obs) => narrativeCell(obs, { prefix: "source_", cls: "lda-data-text" }),
      },
      { label: "Note", cls: "lda-data-text", get: (obs) => obs.weibull_life_note || "" },
      { label: "Check", cls: "lda-data-text lda-check", get: (obs) => obs.data_quality_assumption_flag || "" },
    ];
    const table = el("table", { class: "lda-data" });
    table.appendChild(
      el("thead", {}, [el("tr", {}, columns.map((c) => el("th", { text: c.label, class: c.cls || null })))])
    );
    const tbody = el("tbody");
    const rowByObs = new Map();
    const rowByRecord = new Map();
    const activate = typeof onRowActivate === "function" ? onRowActivate : null;
    (result.observations || []).forEach((obs) => {
      const tr = el("tr", {}, columns.map((c) =>
        c.node ? c.node(obs) : el("td", { text: c.get(obs), class: c.cls || null })
      ));
      if (activate) {
        tr.classList.add("is-clickable");
        tr.title = "Show this observation on the graphs above";
        // The reverse of the chart's click-to-row jump: mark the row so it stays
        // identifiable once the viewport leaves it, then hand the observation to the
        // charts, which ring it and scroll to the pane that plots it.
        tr.addEventListener("click", () => {
          markRow(tr);
          activate(obs);
        });
      }
      rowByObs.set(Number(obs.weibull_observation_id), tr);
      if (obs.source_mapped_record_id != null) rowByRecord.set(Number(obs.source_mapped_record_id), tr);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    const tableTools = enableTableColumnTools(table, { columns });
    const node = el("div", { class: "lda-data-scroll" }, [table]);
    function markRow(tr) {
      tbody.querySelectorAll("tr.is-highlight").forEach((r) => r.classList.remove("is-highlight"));
      tr.classList.add("is-highlight");
    }
    function reveal(tr) {
      // A click-to-jump from a chart point may target a row that an active column
      // filter is hiding (e.g. filtered to Failure=Yes, then clicking a censored
      // point). Clear the filters so the jump actually reveals the row.
      if (tr.style.display === "none" && tableTools) tableTools.clearFilters();
      markRow(tr);
      tr.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    function highlight(observationId) {
      const tr = rowByObs.get(Number(observationId));
      if (tr) reveal(tr);
    }
    // The row a record closes, by the record's id. False when the record closes
    // no row here -- it has left the failure group, or it is the group's first
    // event, which only ever opens an interval.
    function revealRecord(mappedRecordId) {
      const tr = rowByRecord.get(Number(mappedRecordId));
      if (!tr) return false;
      reveal(tr);
      return true;
    }
    return { node, highlight, revealRecord };
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
      // The card, not just the canvas, is what focus() scrolls to, so the pane's title
      // comes along with its plot.
      canvases[pane.key] = { canvas, height: pane.height, card };
    });

    // The observation a Weibull data-table row click asked to see, and the panes that
    // turned out to plot it. Each draw refills the set, because which panes show a
    // given observation depends on what it is: a failure lands on the probability plot
    // and the CDF, a historical censored observation only on the CDF, and a current-life
    // marker on all four.
    let focusedObs = null;
    const focusedPanes = new Set();
    // Last parameters drawn, so focus() can redraw with the ring without disturbing an
    // on-screen beta/eta adjustment.
    let lastBeta = result.beta_mle;
    let lastEta = result.eta_mle;

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
      lastBeta = beta;
      lastEta = eta;
      focusedPanes.clear();
      const curves = analyticCurves(beta, eta);
      const ciPairs = [];
      if (result.beta_lower_ci != null && result.eta_lower_ci != null) ciPairs.push([result.beta_lower_ci, result.eta_lower_ci]);
      if (result.beta_upper_ci != null && result.eta_upper_ci != null) ciPairs.push([result.beta_upper_ci, result.eta_upper_ci]);
      // Each pane reports whether it drew the focus ring, which is how focus() knows
      // which pane is worth scrolling to.
      const record = (key, drewRing) => {
        if (drewRing) focusedPanes.add(key);
      };

      // 1) Weibull probability plot in (ln t, ln(-ln R)) space
      record("prob", drawProbabilityPlot(canvases.prob, beta, eta, ciPairs, km, failureObs, highlight, currentCensors, focusedObs));
      // 2) CDF vs time. White points are completed failures (KM estimate); red
      //    points are historical right-censored observations placed on the fitted
      //    curve at their censoring time; the red vertical line marks current life.
      record("cdf", drawCurvePane(canvases.cdf, {
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
        highlightObs: focusedObs,
      }));
      // 3) PDF (MLE only) + current-life marker
      record("pdf", drawCurvePane(canvases.pdf, {
        mleLine: curves.map((p) => [p.life_hours, p.pdf]),
        ciLines: [],
        scatter: [],
        verticalMarkers: currentLifeMarkers,
        xLabel: "Life hours",
        yLabel: "Density",
        markerPick: jumpToObs,
        highlightObs: focusedObs,
      }));
      // 4) Hazard (MLE only) + current-life marker
      record("hazard", drawCurvePane(canvases.hazard, {
        mleLine: curves.map((p) => [p.life_hours, p.hazard]),
        ciLines: [],
        scatter: [],
        verticalMarkers: currentLifeMarkers,
        xLabel: "Life hours",
        yLabel: "Hazard",
        markerPick: jumpToObs,
        highlightObs: focusedObs,
      }));
    }

    function highlightFailureByTime(point) {
      // map a KM/time point back to the nearest failure observation row
      const best = nearestFailureObs(point.life_hours);
      if (best && highlight) highlight(best.weibull_observation_id);
    }

    // The other direction of the table/chart link: ring one observation on the graphs
    // and bring the pane that actually plots it into view. Redrawn at the parameters
    // currently on screen so a beta/eta adjustment being explored is not thrown away.
    function focus(obs) {
      if (!obs || obs.weibull_observation_id == null) return;
      focusedObs = obs;
      draw(lastBeta, lastEta);
      // Probability plot first: it is the primary Weibull graph, so when an observation
      // appears on several panes that is the one to land on. Failing that, whichever
      // pane did plot it; failing that (nothing plotted), the probability plot anyway.
      const pane = ["prob", "cdf", "pdf", "hazard"].find((key) => focusedPanes.has(key)) || "prob";
      scrollBelowSticky(canvases[pane].card);
    }

    // Defer the first draw to the next frame. renderAnalysisResult appends this
    // chart container to the DOM synchronously after buildAnalysisCharts returns,
    // so by the time this callback runs the canvases are attached and report their
    // real on-screen width instead of 0 (which collapsed the plots into a strip).
    requestAnimationFrame(() => draw(result.beta_mle, result.eta_mle));
    return { update: draw, focus };
  }

  // Emphasis ring marking the observation a Weibull data-table row click focused, so it
  // is identifiable among the other plotted points once the graph scrolls into view.
  function drawFocusRing(ctx, hit) {
    ctx.save();
    ctx.strokeStyle = C.brand;
    ctx.lineWidth = 2.5;
    if (hit.vertical) {
      // Current-life markers are full-height lines, so bracket the line in a band
      // instead of ringing a point that isn't there. Nothing is drawn over the line
      // itself: its red is what identifies it as current life in the legend.
      const halfWidth = 6;
      ctx.globalAlpha = 0.16;
      ctx.fillStyle = C.brand;
      ctx.fillRect(hit.px - halfWidth, hit.top, halfWidth * 2, hit.bottom - hit.top);
      ctx.globalAlpha = 1;
      ctx.lineWidth = 1.5;
      [-halfWidth, halfWidth].forEach((offset) => {
        ctx.beginPath();
        ctx.moveTo(hit.px + offset, hit.top);
        ctx.lineTo(hit.px + offset, hit.bottom);
        ctx.stroke();
      });
    } else {
      ctx.beginPath();
      ctx.arc(hit.px, hit.py, 9, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
  }

  // Does this plotted hit stand for the observation a data-table row focused?
  //
  // Censored points and current-life markers carry their own observation, so an id
  // match settles it. Kaplan-Meier points do not: every failure sharing a lifetime is
  // aggregated into one point, and the nearest-observation lookup that maps a point
  // back to a row keeps only the first of them as its representative. Clicking any of
  // the others would then find no hit and get no ring, so an aggregate point also
  // matches a focused failure plotted at the same lifetime. The failure check is what
  // stops a censored row from ringing the failure point that happens to share its
  // lifetime.
  function hitMatchesFocus(hit, focus) {
    if (!hit || !hit.obs || !focus) return false;
    if (Number(hit.obs.weibull_observation_id) === Number(focus.weibull_observation_id)) return true;
    return Boolean(
      hit.aggregate &&
        Number(focus.failure_indicator) &&
        Number(hit.obs.life_hours_for_weibull) === Number(focus.life_hours_for_weibull)
    );
  }

  function drawProbabilityPlot(target, beta, eta, ciPairs, km, failureObs, highlight, currentCensors, highlightObs) {
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
      ctx.strokeStyle = C.goal;
      ctx.beginPath();
      ctx.moveTo(sx(xMin), sy(b * (xMin - lnE)));
      ctx.lineTo(sx(xMax), sy(b * (xMax - lnE)));
      ctx.stroke();
    });

    // MLE fit line (green)
    ctx.strokeStyle = C.accent;
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
      ctx.fillStyle = C.pointFill;
      ctx.strokeStyle = C.ink;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      // aggregate: this point stands for every failure at its lifetime, not only the
      // representative observation the nearest-match lookup returned.
      hits.push({ px, py, point: p, obs: nearestFailureByX(p.weibull_plot_x), aggregate: true });
    });

    // Current-life "now" markers: full-height red vertical lines.
    markers.forEach((m) => {
      const px = sx(m.x);
      ctx.strokeStyle = C.danger;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(px, top);
      ctx.lineTo(px, bottom);
      ctx.stroke();
    });

    // Ring the focused observation last so it sits over the points and fit lines. A
    // current-life marker takes precedence over a KM point, matching how `locate`
    // below resolves an ambiguous cursor position.
    let drewRing = false;
    if (highlightObs) {
      const marker = markers.find((m) => hitMatchesFocus(m, highlightObs));
      const hit = marker ? null : hits.find((h) => hitMatchesFocus(h, highlightObs));
      if (marker) drawFocusRing(ctx, { vertical: true, px: sx(marker.x), top, bottom });
      else if (hit) drawFocusRing(ctx, hit);
      drewRing = Boolean(marker || hit);
    }

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
    return drewRing;
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
      ctx.strokeStyle = C.goal;
      strokePolyline(ctx, line, sx, sy);
    });

    ctx.strokeStyle = C.accent;
    ctx.lineWidth = 2.4;
    strokePolyline(ctx, opts.mleLine, sx, sy);

    const hits = [];
    (opts.scatter || []).forEach((p) => {
      const px = sx(p[0]);
      const py = sy(p[1]);
      ctx.fillStyle = C.pointFill;
      ctx.strokeStyle = C.ink;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      // Scatter points come from the Kaplan-Meier series, so like the probability
      // plot's they stand for every failure at their lifetime, not just the one the
      // nearest-match lookup returned.
      hits.push({
        px,
        py,
        point: p[2],
        obs: opts.scatterObs ? opts.scatterObs(p[2]) : null,
        pick: opts.scatterPick,
        aggregate: true,
      });
    });
    (opts.censored || []).forEach((p) => {
      const px = sx(p[0]);
      const py = sy(p[1]);
      ctx.fillStyle = C.danger;
      ctx.strokeStyle = C.dangerDeep;
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
      ctx.strokeStyle = C.danger;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(px, top);
      ctx.lineTo(px, bottom);
      ctx.stroke();
      hits.push({ px, vertical: true, top, bottom, point: m[1], obs: m[1], pick: opts.markerPick });
    });
    // Ring the focused observation last so it sits over the points and curves. A pane
    // that does not plot it (the PDF and hazard panes carry only current-life markers)
    // simply reports back that it drew nothing.
    const focusedHit = opts.highlightObs
      ? hits.find((h) => hitMatchesFocus(h, opts.highlightObs))
      : null;
    if (focusedHit) drawFocusRing(ctx, focusedHit);
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
    return Boolean(focusedHit);
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
    ctx.strokeStyle = C.axis;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    // Numeric range ticks (subtle). Only drawn when a max value is supplied, i.e.
    // the linear CDF/PDF/Hazard panes; the log-space probability plot omits them.
    ctx.fillStyle = C.label;
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
    ctx.fillStyle = C.ink;
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
  // Adds a per-column header dropdown (a sort pair plus a checkable value filter,
  // like Excel's column filter) to a rendered table.
  //
  // Callers describe their columns (`options.columns`, one entry per header cell,
  // each `{ key, type }`) so a column is compared as what it holds rather than as
  // the text it renders: dates chronologically, numbers numerically. An
  // undeclared column falls back to the mixed number/text compare, which is what
  // every table did before any of them declared types.
  //
  // Where the sort runs depends on the table. The Weibull data table holds every
  // observation, so it sorts in place: the existing <tr> nodes are reordered,
  // which keeps editable controls and row handlers alive. The disposition editor
  // is paginated, and sorting its 50 visible rows would answer a different
  // question from the one asked, so it passes `options.onSort` and the server
  // orders the whole selection instead (`options.sort` then says which column and
  // direction came back, so the header can show it). Filtering hides non-matching
  // rows either way. The dropdown is attached to <body> so the table's scroll
  // containers and sticky headers never clip it.
  let openColumnMenu = null;
  function closeColumnMenu() {
    if (openColumnMenu) {
      openColumnMenu.remove();
      openColumnMenu = null;
      startOwedDispositionTour();
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
    // A cell whose rendered markup does not read back as plain text (the stacked
    // failure narrative) states its own sortable/filterable form.
    if (td.dataset && td.dataset.columnText !== undefined) return td.dataset.columnText;
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

  // The value a typed column really holds, as a number to compare: the number
  // itself, or a date's UTC instant. NaN means "this cell holds nothing of that
  // type", which sorts and groups as a blank.
  function columnTypedValue(type, text) {
    if (type === "number") return columnNumericValue(text);
    if (type === "datetime") {
      const parsed = parseRecordDate(text);
      return parsed ? parsed.ms : NaN;
    }
    return NaN;
  }

  function columnCompareFor(type) {
    if (type !== "number" && type !== "datetime") return columnCompare;
    return (a, b) => {
      const na = columnTypedValue(type, a);
      const nb = columnTypedValue(type, b);
      if (isFinite(na) && isFinite(nb)) return na - nb;
      // Neither is a value of this type, so fall back rather than call them equal.
      return columnCompare(a, b);
    };
  }

  // A cell holding nothing of the column's type -- blank, or free text where a
  // number or a date belongs -- sits at the bottom whichever way the column is
  // pointed, the way a spreadsheet leaves blanks last. Mirrors the ORDER BY the
  // server builds for the disposition table (LifeDataService._disposition_order_by).
  function columnIsBlank(type, text) {
    if (text === "") return true;
    if (type === "number" || type === "datetime") return !isFinite(columnTypedValue(type, text));
    return false;
  }

  // What the two sort buttons say, so the menu names the ordering the column
  // actually has instead of calling every column alphabetical.
  const COLUMN_SORT_LABELS = {
    text: ["Sort A → Z", "Sort Z → A"],
    number: ["Sort 0 → 9", "Sort 9 → 0"],
    datetime: ["Sort Oldest → Newest", "Sort Newest → Oldest"],
    boolean: ["Sort No → Yes", "Sort Yes → No"],
  };

  function enableTableColumnTools(table, options) {
    const settings = options || {};
    const thead = table.tHead;
    const tbody = table.tBodies[0];
    if (!thead || !tbody || !thead.rows.length) return;
    const headerRow = thead.rows[thead.rows.length - 1];
    const ths = Array.from(headerRow.cells);
    // One entry per header cell, or {} for a column the caller did not describe.
    const columns = ths.map((_, i) => (settings.columns && settings.columns[i]) || {});
    const typeOfColumn = (col) => columns[col].type || "text";
    // Set by a caller that sorts elsewhere (the disposition editor, which has the
    // server order every row in the selection rather than the page on screen).
    const onSort = typeof settings.onSort === "function" ? settings.onSort : null;
    // Active value filter per column: a Set of allowed display values, or null
    // (no filter, every value shown).
    //
    // A caller whose table is rebuilt from the server keeps this between renders
    // (`settings.filters`, keyed by column rather than by position, and reported
    // back through `settings.onFiltersChanged`). Without it, sorting -- which is
    // a reload -- would silently drop an active filter and bring the hidden rows
    // back, which is not what pointing a column at a different order asks for.
    const onFiltersChanged = typeof settings.onFiltersChanged === "function" ? settings.onFiltersChanged : null;
    const restored = settings.filters || {};
    const filters = columns.map((column) =>
      column.key && Array.isArray(restored[column.key]) ? new Set(restored[column.key]) : null
    );

    // The active filters keyed by column, for a caller to hand back on the next
    // render. Columns are named rather than numbered because the two record
    // types do not draw the same ones in the same places.
    function activeFilters() {
      const active = {};
      filters.forEach((set, col) => {
        if (set && columns[col].key) active[columns[col].key] = Array.from(set);
      });
      return active;
    }

    function filtersChanged() {
      if (onFiltersChanged) onFiltersChanged(activeFilters());
    }

    const dataRows = () =>
      Array.from(tbody.rows).filter(
        (tr) => tr.cells.length === ths.length && !tr.querySelector(".lda-empty-row")
      );

    // Shown when the filters hide every row on the page -- which a filter carried
    // across a reload can easily do, since it was picked from values the new page
    // may not have. A header sitting over nothing otherwise reads as a page that
    // failed to load, and on a table this wide the marked column that is doing
    // the hiding is offscreen.
    let allHiddenRow = null;
    function showAllHidden(show) {
      if (!show && !allHiddenRow) return;
      if (!allHiddenRow) {
        allHiddenRow = el("tr", {}, [
          el("td", {
            class: "lda-readonly lda-empty-row",
            colspan: String(ths.length),
            text:
              "Every row on this page is hidden by a column filter. Clear the filter from the ▾ menu of the " +
              "highlighted column headers to bring them back.",
          }),
        ]);
        tbody.appendChild(allHiddenRow);
      }
      allHiddenRow.style.display = show ? "" : "none";
    }

    function applyFilters() {
      const rows = dataRows();
      let shown = 0;
      rows.forEach((tr) => {
        const hidden = filters.some((set, col) => set && !set.has(columnCellText(tr.cells[col])));
        tr.style.display = hidden ? "none" : "";
        if (!hidden) shown += 1;
      });
      showAllHidden(rows.length > 0 && shown === 0);
    }

    function markSorted(col, dir) {
      ths.forEach((th, i) => {
        th.classList.remove("is-sorted-asc", "is-sorted-desc");
        if (i === col) th.classList.add(dir === "desc" ? "is-sorted-desc" : "is-sorted-asc");
      });
    }

    function sortBy(col, dir) {
      const type = typeOfColumn(col);
      const compare = columnCompareFor(type);
      const sign = dir === "desc" ? -1 : 1;
      const rows = dataRows();
      rows.sort((ra, rb) => {
        const a = columnCellText(ra.cells[col]);
        const b = columnCellText(rb.cells[col]);
        const aBlank = columnIsBlank(type, a);
        const bBlank = columnIsBlank(type, b);
        if (aBlank || bBlank) return aBlank && bBlank ? 0 : aBlank ? 1 : -1;
        return sign * compare(a, b);
      });
      rows.forEach((tr) => tbody.appendChild(tr));
      markSorted(col, dir);
    }

    // Sorting a table the caller paginates belongs to the caller: it reloads the
    // page ordered by this column, so every row in the selection takes part
    // rather than only the ones already rendered.
    function requestSort(col, dir) {
      if (onSort && columns[col].key) onSort(columns[col].key, dir);
      else sortBy(col, dir);
    }

    function openMenu(col, anchorBtn) {
      closeColumnMenu();
      const menu = el("div", { class: "lda-col-menu" });
      menu.dataset.col = String(col);

      const labels = COLUMN_SORT_LABELS[typeOfColumn(col)] || COLUMN_SORT_LABELS.text;
      const sortAsc = el("button", { type: "button", class: "lda-col-menu-sort", text: labels[0] });
      const sortDesc = el("button", { type: "button", class: "lda-col-menu-sort", text: labels[1] });
      sortAsc.addEventListener("click", () => { requestSort(col, "asc"); closeColumnMenu(); });
      sortDesc.addEventListener("click", () => { requestSort(col, "desc"); closeColumnMenu(); });
      menu.appendChild(el("div", { class: "lda-col-menu-sorts" }, [sortAsc, sortDesc]));

      // Distinct values across every data row (not just the rows other filters
      // currently leave visible) so a filtered-out value can always be re-added.
      const BLANK = "(Blanks)";
      const values = new Set();
      dataRows().forEach((tr) => {
        const text = columnCellText(tr.cells[col]);
        values.add(text === "" ? BLANK : text);
      });
      const sortedValues = Array.from(values).sort(columnCompareFor(typeOfColumn(col)));

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
        filtersChanged();
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
        filtersChanged();
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

    // A filter carried over from the previous render hides its rows and marks its
    // header now, so a reload lands on the same view it left.
    if (filters.some(Boolean)) {
      ths.forEach((th, col) => {
        const btn = th.querySelector(".lda-col-tool");
        if (btn && filters[col]) btn.classList.add("is-active");
      });
      applyFilters();
    }

    // A sort the caller ran (the server ordering a disposition page) still has to
    // show on the header it was run from.
    if (settings.sort && settings.sort.key) {
      const sortedCol = columns.findIndex((column) => column.key === settings.sort.key);
      if (sortedCol >= 0) markSorted(sortedCol, settings.sort.dir);
    }

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
      filtersChanged();
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
      ["Failure narrative", truncate(narrativeText(obs, "source_"), 260) || "—"],
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

  // ---- tour -----------------------------------------------------------------
  // The page's "Show me around", driven by page_tour.js: a spotlight on one part
  // of the page at a time and a card saying what it is for, the same as the
  // Metrics page and the PM calendar have.
  //
  // Most of the page is only drawn once an asset is picked, and the Weibull
  // results only once a mechanism is. Rather than stop at whatever is on screen,
  // the tour does the picking: with no asset chosen, Next on the first step picks
  // an example, and with no mechanism shown, Next on the results step shows one.
  // So the tour is the same length however far somebody had got, and the first
  // card says how long. Which panels it covers depends on the Analysis Type,
  // which can't change while it is open, so each step says which type it is for
  // and which account, rather than page_tour.js finding out from what is drawn.
  //
  // It opens by itself on a first visit. The part about the results offers
  // itself as well, the first time an asset's numbers arrive on a browser that
  // hasn't seen it, for somebody who skipped the first before it got that far.

  // The asset this tour picked as its example, so the cards can say so rather
  // than leave somebody wondering where it came from; whether there was none to
  // pick; and whether it has shown a mechanism. All three are for the tour that
  // is open, and start over with the next. The Disposition page's tour picks an
  // example too, and uses the first two and what follows the same way.
  let tourExampleAsset = null;
  let tourNoExample = false;
  let tourMechanismTried = false;
  // Counts the tours started, so an answer arriving for one that has since
  // closed can tell.
  let tourRun = 0;
  // Set while the example's summary loads, or on the Disposition page its
  // editor. Those are the tour's doing, so the results or editor tour mustn't
  // take them as a reason to offer itself -- least of all when Skip has closed
  // the tour that picked them before they land.
  let tourExampleLoading = false;

  const forType = (type) => () => state.analysisType === type;

  function tourAssetLabel(number) {
    const asset = state.assetByNumber.get(number);
    return asset && asset.asset_name ? `${number} (${asset.asset_name})` : number;
  }

  // Asks the server, at `url`, for the asset with the most to show and picks it
  // the way choosing it from the list would. Resolves once its summary is on the
  // page, or its editor on the Disposition page.
  async function pickTourExampleAsset(url) {
    const run = tourRun;
    let number = null;
    try {
      // Not until the asset list is in: the example is looked up in it, and on
      // the first visit after an import, loading it is what maps the records the
      // example is chosen from. loadAssets never rejects.
      await assetsLoaded;
      number = (await getJson(url)).asset_number || null;
    } catch (err) {
      // The step says there's no example, which is all the tour can do about it.
    }
    // Skip stays live while this is out, and nothing covers the page, so the
    // tour may have closed and somebody picked an asset of their own since.
    // The page is theirs again then; an example now would undo their choice.
    if (run !== tourRun || !window.gremlinTour.isOpen() || state.selectedAsset) return;
    const asset = number ? state.assetByNumber.get(number) : null;
    if (!asset) {
      tourNoExample = true;
      return;
    }
    tourExampleAsset = asset.asset_number;
    tourExampleLoading = true;
    try {
      await chooseAsset(asset);
    } finally {
      tourExampleLoading = false;
    }
  }

  // Whether the chosen analysis is showing a mechanism yet.
  function tourMechanismShown() {
    if (state.analysisType === ANALYSIS_TYPES.WEIBULL) return Boolean(state.latestResult);
    if (state.analysisType === ANALYSIS_TYPES.TREND) return Boolean(state.selectedTrend);
    if (state.analysisType === ANALYSIS_TYPES.PM) return Boolean(state.pmSelection);
    if (state.analysisType === ANALYSIS_TYPES.DOWNTIME) return Boolean(state.downtimeSelection);
    if (state.analysisType === ANALYSIS_TYPES.REPEAT) return Boolean(state.repeatData);
    return true;
  }

  // The mechanism the tour shows: the first bar on the Pareto as it is ranked
  // now. For Weibull, the first bar with a saved fit, since the tour only opens
  // fits already saved, or failing that the first Highest-beta mechanism, every
  // one of which is. Null when there is nothing to show.
  function tourMechanism() {
    const bars = paretoDisplayRows();
    if (state.analysisType !== ANALYSIS_TYPES.WEIBULL) return bars[0] || null;
    const saved = (bar) => state.rankings.some((fit) => selectionMatches(bar, fit));
    return bars.find(saved) || state.rankings[0] || null;
  }

  function tourMechanismName() {
    const row = tourMechanism();
    return (row && (row.failure_mechanism_name || row.failure_mode_name)) || "the top mechanism";
  }

  const TOUR_MECHANISM_ACTION = {
    label: "Show an example ►",
    needed: () => !tourMechanismTried && Boolean(state.selectedAsset) && !tourMechanismShown() && Boolean(tourMechanism()),
    run: () => {
      tourMechanismTried = true;
      const row = tourMechanism();
      // Clicking the bar would run and save a new fit for an editor. Showing
      // somebody around shouldn't write anything, so it opens the saved one.
      if (state.analysisType === ANALYSIS_TYPES.WEIBULL) return runParetoMechanism(row, { savedOnly: true });
      return onParetoBarSelected(row);
    },
  };

  // Ahead of a step's own text while its button is still to show a mechanism.
  function tourMechanismLead(what) {
    return TOUR_MECHANISM_ACTION.needed()
      ? `Show an example ${what} ${tourMechanismName()}, the top bar on the Pareto, as if it had been clicked. `
      : "";
  }

  const ANALYSIS_SETUP_TOUR_STEPS = [
    {
      target: "#lda-asset-field",
      title: "Pick an asset",
      body: () => {
        if (state.selectedAsset && state.selectedAsset === tourExampleAsset) {
          return (
            `The tour has picked ${tourAssetLabel(tourExampleAsset)} as its example, being the asset with the ` +
            "most to show, and the rest of it is about that one. To look at your own, type part of its Asset " +
            "Number or name here and choose it from the list."
          );
        }
        const how =
          "Type part of an Asset Number or an asset's name and choose it from the list. The list is every " +
          "asset mapped from the CMMS.";
        if (state.selectedAsset) return how;
        if (tourNoExample) {
          return how + " There's no asset with failures to use as an example, so the rest of the tour describes the page instead.";
        }
        return how + " The rest of the page appears once one is picked, so for this tour, Pick an example chooses one for you.";
      },
      action: {
        label: "Pick an example ►",
        needed: () => !state.selectedAsset,
        run: () => pickTourExampleAsset(`${API}/tour-example`),
      },
    },
    {
      target: "#lda-type-field",
      title: "Choose the analysis",
      body: () =>
        "Weibull Analysis fits a life distribution to one failure mode or mechanism. Failure Mode " +
        "Trend counts it month by month, Downtime Driver shows where its downtime comes from, and " +
        "PM Effectiveness how soon it fails after a PM. Repeat Fix Rate shows how often each mechanism comes " +
        "straight back after a repair. Switching keeps the mechanism you last picked. " +
        `The rest of this tour is about ${state.analysisType}; choose another and take the tour again for its panels.`,
    },
  ];

  // What a Pareto bar does when clicked, by Analysis Type.
  const PARETO_CLICK_TOUR_TEXT = {
    [ANALYSIS_TYPES.TREND]: "chart its trend below.",
    [ANALYSIS_TYPES.PM]: "see how soon it follows a PM.",
    [ANALYSIS_TYPES.DOWNTIME]: "break its downtime down below.",
    [ANALYSIS_TYPES.REPEAT]: "list only its repeat failures below.",
  };

  const ANALYSIS_RESULTS_TOUR_STEPS = [
    {
      target: "#lda-weibull-summary",
      title: "Is there enough to fit?",
      when: forType(ANALYSIS_TYPES.WEIBULL),
      body:
        "How many records this asset has, how many work orders and PMs a Weibull fit can use, and " +
        "how many are still to be dispositioned. A record is only usable once it has been " +
        "dispositioned as an included failure or an approved PM reset, and a fit needs at least five " +
        "lives that end in a failure.",
    },
    {
      target: "#lda-trend-summary",
      title: "Trends at a glance",
      when: forType(ANALYSIS_TYPES.TREND),
      body:
        "The mechanisms with the most work orders and the most downtime on this asset, and the ones " +
        "growing fastest and improving most. Growth compares the last three months with the three " +
        "before, so it needs at least six months of data.",
    },
    {
      target: "#lda-pm-summary",
      title: "PM effectiveness at a glance",
      when: forType(ANALYSIS_TYPES.PM),
      // These cards are empty until a mechanism is picked, so the example is
      // shown here rather than on the chart further down.
      action: TOUR_MECHANISM_ACTION,
      body: () =>
        tourMechanismLead("shows") +
        "For the selected mechanism: how many PMs were done, how many were followed by a failure, " +
        "the average days from a PM to that failure, and a rating for how well the PM holds it off.",
    },
    {
      target: "#lda-downtime-summary",
      title: "Downtime at a glance",
      when: forType(ANALYSIS_TYPES.DOWNTIME),
      // Empty until a mechanism is picked, as PM's are.
      action: TOUR_MECHANISM_ACTION,
      body: () =>
        tourMechanismLead("shows") +
        "For the selected mechanism: its total, average, median and longest downtime, and how many " +
        "work orders it came from.",
    },
    {
      target: "#lda-repeat-summary",
      title: "Repeat fixes at a glance",
      when: forType(ANALYSIS_TYPES.REPEAT),
      body:
        "How often a failure on this asset came back within the repeat window of the last one of the same " +
        "mechanism, which says the repair did not hold. Highest Rate only ranks mechanisms with enough gaps " +
        "between failures for the rate to mean something, and a possible duplicate is a repeat that closed " +
        "within an hour, worth checking is not the same breakdown recorded twice.",
    },
    {
      target: "#lda-beta-panel",
      title: "Highest-beta mechanisms",
      when: forType(ANALYSIS_TYPES.WEIBULL),
      body:
        "Where an age-based PM is most likely to pay off: the five mechanisms with the highest beta in their " +
        "last saved Weibull fit, counting only fits with at least five failure lives. A beta above 1 means " +
        "failures get likelier with age, which a PM can get ahead of; below 1 points to early-life failures.",
    },
    {
      target: "#lda-risk-panel",
      title: "Most likely to fail soon",
      when: forType(ANALYSIS_TYPES.WEIBULL),
      body:
        "What needs attention before the next planning cycle: the same saved fits, ranked by the chance the " +
        "current life ends in a failure within the weeks in the box (four unless you change it), given how " +
        "long it has already run.",
    },
    {
      target: "#lda-pareto-panel",
      title: "Failure mechanism Pareto",
      body: () =>
        "Each bar is a failure mechanism, largest first by downtime hours; tick the box to rank by " +
        "failure count instead. The line is the running share of the total. Click a bar to " +
        (PARETO_CLICK_TOUR_TEXT[state.analysisType] ||
          (CAN_EDIT ? "run a Weibull fit on it." : "open the Weibull fit last saved for it.")),
    },
    {
      target: "#lda-actions",
      title: "Run it, or tidy the data first",
      body:
        "Perform Analysis picks a failure mode or mechanism from a list rather than the chart. " +
        "Disposition Work Orders and Disposition PMs open this asset's records on the Disposition " +
        "page, to classify them before analysing.",
      // The bar stays on screen for a viewer, with its buttons hidden.
      when: () => CAN_EDIT,
    },
    {
      target: "#lda-workspace",
      title: "Weibull results",
      when: forType(ANALYSIS_TYPES.WEIBULL),
      action: TOUR_MECHANISM_ACTION,
      body: () => {
        if (TOUR_MECHANISM_ACTION.needed()) {
          return (
            "Clicking a bar on the Pareto opens its Weibull fit here, under the chart. Show an example opens " +
            `the fit already saved for ${tourMechanismName()}.`
          );
        }
        if (!state.latestResult) {
          return (
            "Clicking a bar on the Pareto " +
            (CAN_EDIT ? "runs a Weibull fit on it" : "opens the fit last saved for it") +
            ", and the results appear under the chart: beta and eta with their confidence bounds, the " +
            "fitted curves, what they mean, and the data behind them. A fit needs at least five lives " +
            "that end in a failure."
          );
        }
        return (
          "Beta and eta with their confidence bounds, B10 and B50, the life basis and window the lives " +
          "were built in, the fitted curves, the reliability at an age you enter, what it all means, and " +
          "the data behind it, down to every event left out and why. Hover a plotted point for its work " +
          "order, and click it to find its row in the table." +
          (CAN_EDIT
            ? " Change beta or eta to see the curves move, save the adjustment with a reason, or " +
              "generate a Weibull report."
            : "")
        );
      },
    },
    {
      target: "#lda-trend-chart-panel",
      title: "The trend",
      when: forType(ANALYSIS_TYPES.TREND),
      action: TOUR_MECHANISM_ACTION,
      body: () =>
        tourMechanismLead("charts") +
        "Work orders a month for the selected mechanism. From and To narrow the months. The table " +
        "under it has the same numbers, and clicking a month there lists only that month's work " +
        "orders in the table after it.",
    },
    {
      target: "#lda-pm-chart-panel",
      title: "Failures following PM",
      when: forType(ANALYSIS_TYPES.PM),
      action: TOUR_MECHANISM_ACTION,
      body: () =>
        tourMechanismLead("shows") +
        "Failures of the selected mechanism that came after a completed PM, month by month. From " +
        "and To narrow the months, and the table under it pairs each PM with the failure that " +
        "followed it.",
    },
    {
      target: "#lda-repeat-rate-panel",
      title: "Repeat fix rate by mechanism",
      when: forType(ANALYSIS_TYPES.REPEAT),
      body:
        "Each mechanism's failures, the gaps between them, and how many of those gaps were within the " +
        "window, in scheduled hours on the asset's Weibull schedule just as a Weibull life is counted, so a " +
        "weekend does not hide a repeat. Change the hours in the box to widen or narrow it. The table under " +
        "it lists every repeat with the failure before it; click a row here or a Pareto bar to see one " +
        "mechanism's.",
    },
    {
      target: "#lda-downtime-trend-panel",
      title: "Where the downtime comes from",
      when: forType(ANALYSIS_TYPES.DOWNTIME),
      action: TOUR_MECHANISM_ACTION,
      body: () =>
        tourMechanismLead("breaks down") +
        "Downtime a month for the selected mechanism. Below it: how long its outages run, which " +
        "assets or locations they hit, and the ten work orders with the most downtime.",
    },
  ];

  const ANALYSIS_TOUR_END_STEPS = [
    {
      target: "#analysis-tour-btn",
      title: "Come back any time",
      body: () => {
        const again = "The tour only opens by itself once. Press Show me around to take it again.";
        if (state.selectedAsset && state.selectedAsset === tourExampleAsset) {
          return `${tourExampleAsset} stays selected from the tour; pick your own in the Asset Number box whenever you like. ${again}`;
        }
        if (state.selectedAsset) return again;
        return "Pick an asset and press Show me around again to see each of those parts on the page.";
      },
    },
  ];

  const ANALYSIS_TOUR_SEEN_KEY = "gremlin.analysis.tour-seen";
  const ANALYSIS_RESULTS_TOUR_SEEN_KEY = "gremlin.analysis.results-tour-seen";
  // Set while the tour hands focus back to the Asset Number box, which would
  // otherwise open its list over the page as if it had been clicked into.
  let quietAssetFocus = false;

  function onAssetFocus() {
    if (!quietAssetFocus) openAssetDropdown();
  }

  function tourReturnFocus(node) {
    quietAssetFocus = node === $("lda-asset");
    try {
      node.focus();
    } finally {
      quietAssetFocus = false;
    }
  }

  // Anything on screen a tour mustn't open over by itself: another tour or a
  // dialog, this page's own modals, the loading veil, or the asset list open
  // under somebody who is still choosing.
  function analysisTourBlocked() {
    return (
      window.gremlinTour.busy() ||
      Boolean(document.querySelector(".lda-modal-backdrop")) ||
      !$("lda-loading").hidden ||
      state.assetDropdownOpen
    );
  }

  function startAnalysisTour(steps, seenKey) {
    if (window.gremlinTour.isOpen()) return;
    closeAssetDropdown();
    tourRun += 1;
    tourExampleAsset = null;
    tourNoExample = false;
    tourMechanismTried = false;
    window.gremlinTour.start(steps.concat(ANALYSIS_TOUR_END_STEPS), {
      seenKey,
      pinned: [".topbar", "#lda-step1-card"],
      returnFocus: tourReturnFocus,
      // A page tour that got as far as the results, with an asset to show them
      // on, has covered what the results tour would, so that one needn't offer
      // itself as well.
      onEnd: (reached) => {
        if (state.selectedAsset && reached.some((step) => ANALYSIS_RESULTS_TOUR_STEPS.includes(step))) {
          window.gremlinTour.remember(ANALYSIS_RESULTS_TOUR_SEEN_KEY);
        }
      },
    });
  }

  function startAnalysisPageTour() {
    startAnalysisTour(ANALYSIS_SETUP_TOUR_STEPS.concat(ANALYSIS_RESULTS_TOUR_STEPS), ANALYSIS_TOUR_SEEN_KEY);
  }

  // First visit on this browser, once the Asset Numbers are in, so the hint
  // under the box says how many there are rather than that they're loading. Not
  // if somebody has already started picking one: the tour would take the box
  // from under them. It stays unseen for next time.
  function offerAnalysisPageTour() {
    if (window.gremlinTour.seen(ANALYSIS_TOUR_SEEN_KEY)) return;
    const input = $("lda-asset");
    if (input.value.trim() || document.activeElement === input) return;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (!analysisTourBlocked()) startAnalysisPageTour();
      })
    );
  }

  // The first time an asset's numbers land, on a browser that has seen neither
  // this nor a page tour with an asset picked. Two frames, so the Pareto has
  // taken its size before the spotlight goes round it. Anything in the way
  // just means it offers itself again the next time numbers land.
  function offerAnalysisResultsTour() {
    if (!window.gremlinTour || window.gremlinTour.seen(ANALYSIS_RESULTS_TOUR_SEEN_KEY)) return;
    if (tourExampleLoading) return;
    if (analysisTourBlocked()) return;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (!state.selectedAsset || analysisTourBlocked()) return;
        startAnalysisTour(ANALYSIS_RESULTS_TOUR_STEPS, ANALYSIS_RESULTS_TOUR_SEEN_KEY);
      })
    );
  }

  function wireAnalysisTour() {
    const button = $("analysis-tour-btn");
    if (!button || !window.gremlinTour) return;
    button.addEventListener("click", startAnalysisPageTour);
  }

  // The Disposition page's "Show me around", on the same engine: the page's
  // purpose and the Step 1 controls, then the editor under them -- what the table
  // holds, how a row is filled in, and the ways to save it. The editor is only
  // drawn once an asset is picked, so the tour does the picking, as it does on
  // Perform an Analysis: with no asset chosen, Next on the Asset Number step
  // picks an example, the asset with the most rows for the Record Type, Rows and
  // search showing, and draws its editor. That only reads; nothing is saved. The
  // editor part offers itself the first time an editor is drawn on a browser that
  // hasn't seen it, which is also when the whole tour offers itself if the page
  // was opened with an asset already picked -- the Disposition buttons on Perform
  // an Analysis do that.

  // The records the table shows, as Record Type, Rows and the search have it,
  // for the cards that name them.
  function dispositionRecordWords() {
    const records =
      (state.dispositionScope === "new" ? "undispositioned " : "") +
      (state.dispositionKind === "pm" ? "PM reset events" : "work orders");
    return state.dispositionSearch ? `${records} matching "${state.dispositionSearch}"` : records;
  }

  // Applies a search still waiting out its debounce; initDispositionPage sets it.
  // The example is chosen for the search in the box, and one landing after it
  // would reload the example's table for rows it wasn't chosen for.
  let flushDispositionSearch = () => Promise.resolve();

  // Where the tour asks for its example: the Step 1 controls as they stand, so
  // the table it draws has rows in it.
  function dispositionTourExampleUrl() {
    const params = new URLSearchParams({ kind: state.dispositionKind, scope: state.dispositionScope });
    if (state.dispositionSearch) params.set("search", state.dispositionSearch);
    return `${API}/disposition-tour-example?${params.toString()}`;
  }

  const DISPOSITION_SETUP_TOUR_STEPS = [
    {
      target: "#lda-disp-intro",
      title: "What dispositioning is for",
      body:
        "Dispositioning says what each of an asset's work orders and PMs was, and whether a Weibull " +
        "fit may use it. Perform an Analysis only counts records dispositioned here. Skip or Esc ends " +
        "this tour at any time.",
    },
    {
      target: "#lda-asset-field",
      title: "Pick an asset",
      body: () => {
        if (state.selectedAsset && state.selectedAsset === tourExampleAsset) {
          return (
            `The tour has picked ${tourAssetLabel(tourExampleAsset)} as its example, being the asset with the ` +
            `most ${dispositionRecordWords()} to show, and the rest of it is about that one's records. To ` +
            "disposition your own, type part of its Asset Number or name here and choose it from the list."
          );
        }
        const how =
          "Type part of an Asset Number or an asset's name and choose it from the list. The Disposition " +
          "buttons on Perform an Analysis open this page with their asset already picked.";
        if (state.selectedAsset) return how;
        if (tourNoExample) {
          return (
            how +
            ` There's no asset with ${dispositionRecordWords()} to use as an example, so the rest of the tour ` +
            "describes the page instead."
          );
        }
        return how + " Its records appear below once one is picked, so for this tour, Pick an example chooses one for you.";
      },
      action: {
        label: "Pick an example ►",
        needed: () => !state.selectedAsset,
        run: async () => {
          // No asset is picked, so there are no unsaved rows for it to ask about.
          await flushDispositionSearch();
          return pickTourExampleAsset(dispositionTourExampleUrl());
        },
      },
    },
    {
      target: "#lda-disp-kind-field",
      title: "Work orders or PMs",
      body:
        "Work Orders are the corrective jobs: say whether each was a failure, and of which failure " +
        "mode and mechanism. PM Reset Events are completed PMs: say whether each one renewed the " +
        "asset against a failure mode, which starts that mode's clock again in the fit.",
    },
    {
      target: "#lda-disp-scope-field",
      title: "Everything, or just the backlog",
      body: () =>
        "All eligible rows shows every record. Only new / undispositioned shows the ones still without " +
        (state.dispositionKind === "pm" ? "a reset target failure mode or mechanism" : "a failure mode or mechanism") +
        ", which is where to start when catching up.",
    },
    {
      target: "#lda-disp-search-field",
      title: "Find a record",
      body:
        "Narrows the table to rows with this text or number in any column: a task ID, a date, a word " +
        "from the notes. It searches the whole selection, not just the page on screen.",
    },
  ];

  // "Filling in a row", in depth: a card on how the disposition columns are
  // decided, then one card per column saying what it means and what to put in
  // it, with the column lit. The rules are Reliability Engineering's, from the
  // two documents named below, and follow the order the Failure Definition
  // Document says a record is reviewed in rather than the order the columns sit
  // in; the save rules the cards mention are _save_disposition_with_conn's.
  const FAILURE_DEFINITION = "REL-WBL-DAT-002 Failure Definition";
  const DATA_REQUIREMENTS = "REL-WBL-PLN-003 Data Requirements";

  // Per record type, each editable column in the order the cards take them.
  // Each entry is a card for the column `key`, titled with its label in
  // DISPOSITION_EDIT_COLUMNS, so the card and the header can't disagree.
  const DISPOSITION_COLUMN_GUIDE = {
    wo: [
      {
        key: "effective_record_class",
        body:
          "What kind of job this record really was. The import guessed it from the record's own text, so " +
          "check the guess against the title, the notes and what was done, and correct it when it's wrong. " +
          "It describes the job; whether the fit uses the row is up to Disposition Category.",
        points: [
          ["CORRECTIVE_WO", "Unplanned work to restore a function that was lost or degraded: a repair, a replacement because the part failed, an emergency or reactive call-out."],
          ["PM", "Planned preventive work, or a scheduled replacement or overhaul, that ended up among the work orders."],
          ["INSPECTION", "A check or audit that found no defect and no loss of function."],
          ["PARTS_ORDER", "Ordering, fetching or stocking parts, with no repair done."],
          ["ADMINISTRATIVE", "Paperwork, or a record with no technical event behind it."],
          ["PROJECT_WORK", "Upgrades, installs and scheduled project jobs."],
          ["UNKNOWN", "Not enough in the record to tell what the job was."],
        ],
        cite: `${FAILURE_DEFINITION} §4, Table 1; §7.4.7`,
      },
      {
        key: "disposition_category",
        body:
          "The decision itself: the standard's Yes, Review or No. Only INCLUDED_FAILURE puts a work order " +
          "into the fit. Anything vague, mixed or conflicting is held or excluded, never forced in to " +
          "raise the count.",
        points: [
          ["INCLUDED_FAILURE", "Yes. The item couldn't do its job, or did it unacceptably, and needed unplanned corrective action. It counts with or without logged downtime, including a bypass, workaround or manual recovery. Needs a Failure Mode."],
          ["HELD_AMBIGUOUS", "Review. Probably a real failure, but the record is too vague (\"machine down\", \"issue resolved\"), contradicts itself, or doesn't show which population it belongs to. Kept out until resolved; the note says what evidence is missing or what review is needed."],
          ["EXCLUDED_NON_FAILURE", "No. Not a failure: preventive or scheduled work, an inspection with no defect, cosmetic, housekeeping, documentation or administrative jobs, or out of scope for this analysis."],
          ["EXCLUDED_MIXED_CONTAMINATING", "No. A real event, but it mixes more than one failure story or conflicts with the population's definition, so it would muddy the fit. The note says why."],
          ["INCLUDED_CENSORED_ASSET_EVENT", "Survival with no recurrence through the cutoff date. Needs a Failure Mode, but is never counted as a failure, and the fit already adds the censored life from the last failure or reset to the cutoff by itself, so it's rarely needed."],
          ["UNKNOWN", "Not dispositioned yet. Leave it only on rows you haven't reviewed."],
        ],
        cite: `${FAILURE_DEFINITION} §4, §5, §7.4 Table 5, §7.4.3, §7.4.4, §9`,
      },
      {
        key: "failure_mode",
        body:
          "The failure behavior this record shows: a specific, repeatable symptom or effect. Rows sharing " +
          "a mode are fitted together, so name a behavior, not a department or a system.",
        points: [
          ["Good", "\"Hydraulic clamp not reaching position\", \"Pallet translator positioning / homing fault\": one coherent symptom family."],
          ["Too broad", "\"Controls faults\", \"Motion faults\", \"Laser head faults\", \"General electrical issues\": each mixes several behaviors, and the fit would describe none of them."],
          ["Where to look", "The failure or cause code, then the title and request, the alarm or fault code, the component named, the technician's notes, and what was actually done or replaced. When they disagree, take the most defensible technical reading and say why in the notes."],
          ["Reuse or add", "Pick an existing mode when it is the same behavior, so the population stays together. Type a new name only for a genuinely different one; it's added to this asset's list when you save."],
          ["Leave it blank", "On excluded and held rows. INCLUDED_FAILURE won't save without one, and a vague description alone isn't grounds for one."],
        ],
        cite: `${FAILURE_DEFINITION} §3.3, §7 Tables 2–3, §7.4.5, §8, §9.2`,
      },
      {
        key: "failure_mechanism",
        body:
          "The specific physical, functional or adjustment-driven cause under the failure mode. It is the " +
          "preferred grouping, as the narrower population fits more cleanly, but only when this record's " +
          "own evidence supports it.",
        points: [
          ["Fill it in when", "The request, notes, alarm, findings and corrective action all point to one cause, and the fix is specific and repeatable: the same bracket adjusted, the same sensor replaced, the same drift corrected. For example \"SQ87 bracket / switch out of adjustment\"."],
          ["Not from", "A shared alarm code or symptom alone: bracket drift and jammed hardware can raise the same alarm and still be different mechanisms. Nor from an assumed root cause, a guess at what the technician meant, or hindsight from later records."],
          ["Leave it blank when", "The record doesn't isolate one mechanism. The row then sits in the failure-mode population, the controlled fallback; say why in the notes."],
          ["How it counts", "A mechanism belongs to the failure mode beside it. A row with one is fitted in that mechanism's population and its mode's; a row without one, only in the mode's."],
        ],
        cite: `${FAILURE_DEFINITION} §3.4, §7.1–7.3, §7.4.6`,
      },
      {
        key: "modeled_population_name",
        body:
          "The set of records one Weibull fit is run on. You don't type it: saving names it from the " +
          "asset, the failure mode and the mechanism, and it reads \"Auto-create…\" until then. With a " +
          "mechanism it is a mechanism-level population; without one, the failure-mode fallback.",
        points: [
          ["Read it back", "Would a reviewer know what beta and eta describe from the name alone? If not, the mode or mechanism name needs work."],
          ["Check the bucket", "Its rows should share one symptom (and, at mechanism level, one cause), with fixes, parts and machine areas that hang together. Short repeat intervals should be the same problem coming back, not unrelated stories."],
          ["If it doesn't hold together", "Split it, merge it up to the mode, hold the doubtful rows, or leave it out of this analysis. Don't force a fit."],
        ],
        cite: `${FAILURE_DEFINITION} §3.6, §7, §7.5`,
      },
      {
        key: "include_in_weibull_candidate",
        body:
          "Lets the fit use this row. A work order is used only as INCLUDED_FAILURE, with a failure mode, " +
          "and this ticked.",
        points: [
          ["Tick it yourself", "Choosing INCLUDED_FAILURE doesn't tick it. Tick it on every row the fit should use, or use Check all for the page."],
          ["Cleared on save", "For both EXCLUDED_ categories, whatever the box says."],
          ["Ignored", "On every other category: only INCLUDED_FAILURE rows reach the fit."],
        ],
        cite: `${FAILURE_DEFINITION} §7.4 Table 5`,
      },
      {
        key: "disposition_notes",
        body:
          "Your reasoning, kept with the record so a later reviewer can see why it was included, excluded, " +
          "or grouped at the mechanism or the mode level. Required for HELD_AMBIGUOUS and " +
          "EXCLUDED_MIXED_CONTAMINATING, and worth a line on any call that isn't obvious.",
        points: [
          ["Included", "The evidence that puts it in this bucket: \"Tech re-set SQ87 bracket, same fix as the previous events.\" At mode level, why no mechanism could be isolated."],
          ["Held", "What's missing, or who needs to look: \"Only says 'machine down'; ask second shift what was reset.\""],
          ["Mixed", "Which failure stories it mixes, or why the bucket itself needs revising."],
          ["Excluded", "Why it isn't a failure: \"Inspection only, no defect found.\""],
          ["Reclassified", "When the record's fields disagreed, which one you went with and why."],
        ],
        cite: `${FAILURE_DEFINITION} §7.4.7, §8, §10`,
      },
    ],
    pm: [
      {
        key: "effective_record_class",
        body:
          "What kind of job this record really was. The import guessed it from the record's own text; " +
          "correct it when it's wrong. CORRECTIVE_WO isn't offered: a PM is never counted as a failure here.",
        points: [
          ["PM", "A routine preventive task: an inspection round, lubrication, cleaning, a route PM."],
          ["PM_RESET_CANDIDATE", "A PM that may restore a specific item, such as a scheduled replacement, an overhaul or a re-set to spec, and so is worth weighing as a reset."],
          ["INSPECTION", "A check or audit only, with nothing restored."],
          ["PARTS_ORDER", "Ordering, fetching or stocking parts."],
          ["ADMINISTRATIVE", "Paperwork, or a record with no technical event behind it."],
          ["PROJECT_WORK", "Upgrades, installs and scheduled project jobs."],
          ["UNKNOWN", "Not enough in the record to tell what the job was."],
        ],
        cite: `${FAILURE_DEFINITION} §4, Table 1`,
      },
      {
        key: "pm_reset_inclusion_decision",
        body:
          "Whether this PM renewed the asset against one specific failure mode or mechanism. An approved " +
          "reset starts that population's clock again; it is never counted as a failure.",
        points: [
          ["APPROVED_RESET", "Its scope credibly restored the item or function behind one named failure mode or mechanism: it replaced the wearing part, re-set the adjustment to spec, rebuilt the assembly. Needs a reset target and a rationale; goes with INCLUDED_PM_RESET_EVENT."],
          ["REJECTED_RESET", "Weighed as a reset and turned down: its scope doesn't restore the target, as with a general inspection, a broad route PM or housekeeping. Goes with REJECTED_PM_RESET."],
          ["CONTEXT_ONLY", "A routine PM kept only as history around the failures. Goes with PM_CONTEXT_ONLY."],
          ["NEEDS_REVIEW", "Not decided yet, or the record can't show what the PM restored. Goes with HELD_AMBIGUOUS and a note, or UNKNOWN until it's reviewed."],
        ],
        cite: `${FAILURE_DEFINITION} §3.8, §6, §7.3; ${DATA_REQUIREMENTS} §8`,
      },
      {
        key: "disposition_category",
        body:
          "The disposition itself, which has to agree with the PM Reset Decision. Only " +
          "INCLUDED_PM_RESET_EVENT puts a PM into the fit, as the start of a new life rather than a failure.",
        points: [
          ["INCLUDED_PM_RESET_EVENT", "A valid reset for the population. Needs APPROVED_RESET, a Reset Target Failure Mode and a renewal rationale."],
          ["PM_CONTEXT_ONLY", "Kept for traceability only. Goes with CONTEXT_ONLY."],
          ["REJECTED_PM_RESET", "Reviewed and refused as a reset. Goes with REJECTED_RESET."],
          ["HELD_AMBIGUOUS", "Can't tell yet whether it restored the target. Kept out until resolved; the note says what's missing."],
          ["EXCLUDED_NON_FAILURE", "No part in the analysis at all: parts handling, administration, housekeeping."],
          ["UNKNOWN", "Not dispositioned yet. Leave it only on rows you haven't reviewed."],
        ],
        cite: `${FAILURE_DEFINITION} §7.4 Table 5`,
      },
      {
        key: "reset_target_failure_mode",
        body:
          "The failure mode this PM resets: the population in which it marks the start of a new life.",
        points: [
          ["From the list", "Only modes this asset's work orders already use are offered, since a PM can't create one. If the mode you need isn't there, disposition the work orders first."],
          ["One explicit target", "The mode the PM's scope actually restores. A general PM that can't be tied to one mode shouldn't be approved as a reset."],
          ["Required", "For INCLUDED_PM_RESET_EVENT. Leave it blank on context-only, rejected and excluded PMs."],
        ],
        cite: `${FAILURE_DEFINITION} §3.8, §7.3; ${DATA_REQUIREMENTS} §8`,
      },
      {
        key: "reset_target_failure_mechanism",
        body:
          "The mechanism under the target mode that the PM restores, when it is that specific: re-setting " +
          "the SQ87 bracket to spec resets \"SQ87 bracket / switch out of adjustment\".",
        points: [
          ["From the list", "It offers the mechanisms already dispositioned under the chosen Reset Target Failure Mode."],
          ["Leave it blank when", "The PM restores the whole mode: every mechanism under it, not one in particular."],
          ["How it counts", "A PM restarts only what it restores. With a mechanism, the reset counts in that mechanism's population alone, not its mode's. Left blank, it counts in the mode's population and in every mechanism's under it."],
        ],
        cite: `${FAILURE_DEFINITION} §3.4, §3.8, §7.3`,
      },
      {
        key: "pm_reset_renewal_rationale",
        body:
          "Why this PM is technically capable of resetting the target: the part of its scope that restores " +
          "the item or function, and the evidence that it was done.",
        points: [
          ["Required", "For APPROVED_RESET and INCLUDED_PM_RESET_EVENT."],
          ["Good", "\"Task replaces the clamp cylinder seals and re-sets pressure to spec; completion notes confirm both.\""],
          ["Not enough", "\"PM completed\", or a general inspection, housekeeping or route PM with no targeted restoration written down."],
        ],
        cite: `${FAILURE_DEFINITION} §7.3; ${DATA_REQUIREMENTS} §7, §8`,
      },
      {
        key: "modeled_population_name",
        body:
          "The population this reset belongs to, named on save from the asset and the reset target's mode " +
          "and mechanism; it reads \"Auto-create…\" until then. Check that it is the population whose " +
          "failures this PM prevents, named just as on those work orders.",
        cite: `${FAILURE_DEFINITION} §3.6, §7`,
      },
      {
        key: "include_in_weibull_candidate",
        body:
          "Lets the fit use this PM as a reset. A PM is used only as INCLUDED_PM_RESET_EVENT with " +
          "APPROVED_RESET, a reset target, a rationale, and this ticked.",
        points: [
          ["Tick it yourself", "Choosing INCLUDED_PM_RESET_EVENT and APPROVED_RESET doesn't tick it. Tick it on every reset the fit should use."],
          ["Cleared on save", "For REJECTED_RESET, CONTEXT_ONLY and EXCLUDED_NON_FAILURE, whatever the box says."],
          ["Ignored", "On every other category: only INCLUDED_PM_RESET_EVENT rows reach the fit."],
        ],
        cite: `${FAILURE_DEFINITION} §7.4 Table 5`,
      },
      {
        key: "disposition_notes",
        body:
          "Your reasoning, kept with the record for later review. Required for HELD_AMBIGUOUS. The evidence " +
          "for an approved reset goes under PM Reset Renewal Rationale instead.",
        points: [
          ["Held", "What's missing: \"Task list doesn't say whether the seals were replaced; check with the planner.\""],
          ["Rejected or context only", "Why the scope restores nothing specific: \"Route inspection, no parts replaced or adjustments made.\""],
          ["Reclassified", "When the record's fields disagreed, which one you went with and why."],
        ],
        cite: `${FAILURE_DEFINITION} §8, §10`,
      },
    ],
  };

  // The in-depth "Filling in a row": its opening card, which lights the header
  // of every column there is to fill in, then a card per column of the record
  // type showing. The Record Type can't change while the tour is open, so each
  // card is kept or dropped for the whole tour as it starts.
  const DISPOSITION_COLUMN_TOUR_STEPS = [
    {
      target: "#lda-disp-table th[data-disp-col]",
      title: "Filling in a row",
      body: () =>
        state.dispositionKind === "pm"
          ? "The columns after Failure Narrative are the disposition. A PM is never a failure: the one " +
            "question is whether it renewed the asset against one specific failure mode or mechanism, which " +
            "restarts that population's clock in the fit. The next cards take each column in this order:"
          : "The columns after Failure Narrative are the disposition: what this record was, and whether the " +
            "Weibull fit may use it. The next cards take each column in the order the standard reviews a " +
            "record in:",
      points: () =>
        state.dispositionKind === "pm"
          ? [
              ["What was it?", "Confirm it really is a PM, and not an inspection, a parts run or a project."],
              ["Did it restore something specific?", "Only a PM whose scope credibly restores the item or function behind one failure mode or mechanism can reset it. Route PMs, general inspections and housekeeping don't, unless that restoration is written down."],
              ["Decision and category", "They come in matching pairs."],
              ["Name the target", "The failure mode, and the mechanism if there is one, that it resets."],
              ["Show the evidence", "Why its scope renews that target, in words a reviewer can check."],
            ]
          : [
              ["Is it a failure?", "A real loss or degradation of function that needed unplanned correction, downtime or not. Not PM, inspection, admin or housekeeping."],
              ["Does it belong?", "It has to fit one repeatable failure behavior, not a catch-all label."],
              ["How narrow?", "The narrowest level the record itself supports: a failure mechanism first, the failure mode as the fallback."],
              ["Yes, Review or No?", "Include it, hold it, or exclude it. Weak records are held, never forced in to raise the count."],
              ["Why?", "Write it down wherever the call isn't obvious, so a later reviewer can follow it."],
            ],
      cite: () =>
        state.dispositionKind === "pm"
          ? `${FAILURE_DEFINITION} §3.8, §6, §7.3; ${DATA_REQUIREMENTS} §8`
          : `${FAILURE_DEFINITION} §6, §7.4.7, §7.4.8`,
    },
  ].concat(
    ...["wo", "pm"].map((kind) => {
      const labels = new Map(DISPOSITION_EDIT_COLUMNS[kind].map((column) => [column.key, column.label]));
      const guide = DISPOSITION_COLUMN_GUIDE[kind];
      return guide.map((entry, index) => ({
        target: `#lda-disp-table [data-disp-col="${entry.key}"]`,
        title: labels.get(entry.key),
        label: `Column ${index + 1} of ${guide.length}`,
        body: entry.body,
        points: entry.points,
        cite: entry.cite,
        when: () => state.dispositionKind === kind,
      }));
    })
  );

  const DISPOSITION_EDITOR_TOUR_STEPS = [
    {
      target: "#lda-disp-meta",
      title: "What the table holds",
      body: () =>
        "Which asset this is, how many records the selection has and which of them are on screen. " +
        (state.dispositionKind === "pm"
          ? "A PM only reaches a Weibull fit as INCLUDED_PM_RESET_EVENT with APPROVED_RESET, a reset " +
            "target, a rationale, and Include in Weibull Candidate ticked."
          : "A work order only reaches a Weibull fit as INCLUDED_FAILURE with a failure mode and " +
            "Include in Weibull Candidate ticked."),
    },
    {
      target: "#lda-disp-table",
      title: "One row per record",
      body:
        "The columns up to Failure Narrative are what was recorded in the CMMS, and can't be changed " +
        "here. The ones after it, off to the right, are yours to fill in, apart from Modeled Population, " +
        "which fills itself in when you save. The ▾ on any column header sorts the whole selection by " +
        "it, or filters this page to the values you pick.",
    },
    ...DISPOSITION_COLUMN_TOUR_STEPS,
    {
      target: "#lda-disp-check-all",
      title: "Include a whole page",
      body:
        "Ticks Include in Weibull Candidate on every row showing, after asking you to confirm. Rows a " +
        "column filter hides are left as they are, and nothing is kept until you save.",
    },
    {
      target: "#lda-disp-pager",
      title: "Fifty rows to a page",
      body:
        "Changing page reloads the table, and so do sorting, searching and the Step 1 controls. Each " +
        "asks before throwing away unsaved changes, but it's simplest to save a page before moving on.",
    },
    {
      target: "#lda-disp-save",
      title: "Save as you go",
      body:
        "Writes every row you changed on this page, and leaves the rest alone. The rows are saved " +
        "together: if one is refused, say a HELD_AMBIGUOUS row with no note, none are, and the " +
        "message says which rule it broke.",
    },
    {
      target: "#lda-disp-actions",
      title: "Or do it in Excel",
      body:
        "Download Excel gets this asset's records as a workbook with the same dropdowns, to fill in " +
        "offline: all of them, or only the new ones if that's what Rows says. Disposition via Excel " +
        "uploads it back and saves every row that changed. How dispositioning on Excel works explains " +
        "the rest.",
    },
  ];

  const DISPOSITION_TOUR_END_STEPS = [
    {
      target: "#disposition-tour-btn",
      title: "Come back any time",
      body: () => {
        const again = "The tour only opens by itself once. Press Show me around to take it again.";
        if (state.selectedAsset && state.selectedAsset === tourExampleAsset) {
          return (
            `${tourExampleAsset} stays selected from the tour. Its records are real, so anything you save on ` +
            `them is kept; pick your own in the Asset Number box whenever you like. ${again}`
          );
        }
        if (state.selectedAsset) return again;
        return "Pick an asset and press Show me around again to see each of those parts on the page.";
      },
    },
  ];

  const DISPOSITION_TOUR_SEEN_KEY = "gremlin.disposition.tour-seen";
  const DISPOSITION_EDITOR_TOUR_SEEN_KEY = "gremlin.disposition.editor-tour-seen";

  // Anything a tour mustn't open over by itself: the same as on the analysis
  // page, plus a column's ▾ menu or a failure mode list open in the table,
  // somebody typing a search -- each search draws the editor again -- and a
  // mouse button or finger still down. A click elsewhere closes a list, or
  // takes the focus from the search box, as it goes down, so a tour opened
  // then would be under the pointer when it comes up and take the click.
  let pointerHeld = false;

  function dispositionTourBlocked() {
    return (
      analysisTourBlocked() ||
      pointerHeld ||
      Boolean(document.querySelector(".lda-col-menu, body > .lda-portal-list")) ||
      document.activeElement === $("lda-disp-search")
    );
  }

  function startDispositionTour(steps, seenKey) {
    if (window.gremlinTour.isOpen()) return;
    closeAssetDropdown();
    tourRun += 1;
    tourExampleAsset = null;
    tourNoExample = false;
    window.gremlinTour.start(steps.concat(DISPOSITION_TOUR_END_STEPS), {
      seenKey,
      returnFocus: tourReturnFocus,
      // A page tour that got as far as the editor, with one drawn to show it on,
      // has covered what the editor tour would, so that one needn't offer itself
      // as well. Without one -- there was no example to pick -- its steps were
      // cards pointing at nothing, and the first real editor still offers it.
      onEnd: (shown) => {
        if ($("lda-disp-meta") && shown.some((step) => DISPOSITION_EDITOR_TOUR_STEPS.includes(step))) {
          window.gremlinTour.remember(DISPOSITION_EDITOR_TOUR_SEEN_KEY);
        }
        startOwedDispositionTour();
      },
    });
  }

  function startDispositionPageTour() {
    startDispositionTour(
      DISPOSITION_SETUP_TOUR_STEPS.concat(DISPOSITION_EDITOR_TOUR_STEPS),
      DISPOSITION_TOUR_SEEN_KEY
    );
  }

  // Set when the tour has offered itself and hasn't been able to start yet:
  // something was in the way, or the editor it waits for isn't drawn. The
  // places those clear -- the loading veil going down, a list, menu, dialog or
  // modal closing, the search box losing focus, a click being let go, another
  // tour ending -- each call startOwedDispositionTour, which runs it once
  // nothing stands in the way.
  let dispositionTourOwed = false;

  // Called once the Asset Numbers are in and each time the editor is drawn --
  // except the editor the tour draws for its example, which it is already
  // showing, or which somebody skipped past before it landed.
  function offerDispositionTour() {
    if (!window.gremlinTour || state.pageMode !== "disposition") return;
    if (tourExampleLoading) return;
    if (
      window.gremlinTour.seen(DISPOSITION_TOUR_SEEN_KEY) &&
      window.gremlinTour.seen(DISPOSITION_EDITOR_TOUR_SEEN_KEY)
    ) {
      return;
    }
    dispositionTourOwed = true;
    startOwedDispositionTour();
  }

  // The whole tour, if this browser hasn't had it; otherwise the editor part,
  // the first time there is an editor to show. Not while an asset is still being
  // chosen, since the tour would take the box from under them -- unless one is
  // picked already, when it waits for that asset's editor instead. Two frames,
  // so the table has taken its size before the spotlight goes round anything.
  // Anything in the way leaves it owed.
  function startOwedDispositionTour() {
    if (!dispositionTourOwed) return;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (!dispositionTourOwed || dispositionTourBlocked()) return;
        const editorDrawn = Boolean($("lda-disp-meta"));
        if (!window.gremlinTour.seen(DISPOSITION_TOUR_SEEN_KEY)) {
          const input = $("lda-asset");
          const choosing = !state.selectedAsset && (input.value.trim() || document.activeElement === input);
          if (choosing || (state.selectedAsset && !editorDrawn)) return;
          dispositionTourOwed = false;
          stopScrolling();
          startDispositionPageTour();
          return;
        }
        // Without an editor there is nothing more to show; the next one drawn
        // offers the tour again.
        dispositionTourOwed = false;
        if (window.gremlinTour.seen(DISPOSITION_EDITOR_TOUR_SEEN_KEY) || !state.selectedAsset || !editorDrawn) return;
        stopScrolling();
        startDispositionTour(DISPOSITION_EDITOR_TOUR_STEPS, DISPOSITION_EDITOR_TOUR_SEEN_KEY);
      })
    );
  }

  // A freshly drawn editor is still gliding into view when the tour starts. The
  // tour moves the page to each step itself, and leaves it alone when the step
  // is already on screen -- which the page tour's first step, at the top, still
  // is as the glide begins, so the glide would then carry it off the top with
  // the card following. A scroll to where the page is now cuts the glide short.
  function stopScrolling() {
    window.scrollTo({ top: window.pageYOffset, behavior: "auto" });
  }

  function wireDispositionTour() {
    const button = $("disposition-tour-btn");
    if (!button || !window.gremlinTour) return;
    button.addEventListener("click", startDispositionPageTour);
    // Captured, so a control that stops the event can't hide it from here.
    document.addEventListener("pointerdown", () => { pointerHeld = true; }, true);
    ["pointerup", "pointercancel"].forEach((type) =>
      document.addEventListener(type, () => {
        pointerHeld = false;
        startOwedDispositionTour();
      }, true)
    );
  }

  // ---- wiring ---------------------------------------------------------------
  function init() {
    const assetInput = $("lda-asset");
    if (!assetInput) return;
    // Asset combobox wiring is shared by the Perform Analysis page and the
    // dedicated disposition page.
    assetInput.addEventListener("input", onAssetInput);
    assetInput.addEventListener("keydown", onAssetKeydown);
    assetInput.addEventListener("focus", onAssetFocus);
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
    // Only wire the writing controls for an account that may write. Un-hiding
    // the buttons in the developer tools then leaves them inert, rather than
    // sending a request the server is about to refuse.
    if (CAN_EDIT) {
      $("lda-perform").addEventListener("click", performAnalysis);
      $("lda-disposition-wo").addEventListener("click", () => gotoDisposition("wo"));
      $("lda-disposition-pm").addEventListener("click", () => gotoDisposition("pm"));
    }
    $("lda-pareto-toggle").addEventListener("change", (event) => {
      state.paretoMetric = event.target.checked ? "failure_count" : "downtime_hours";
      drawPareto();
    });
    const riskWeeksBox = $("lda-risk-weeks");
    if (riskWeeksBox) riskWeeksBox.addEventListener("change", onRiskWeeksChange);
    const typeSelect = $("lda-analysis-type");
    if (typeSelect) {
      // ?analysis= preselects the Analysis Type, which is what the topbar's
      // global search links to for each of them. Matched against the option
      // values themselves so the four names live in one place -- the markup --
      // and an unrecognised value simply leaves the default selected.
      const requested = (new URLSearchParams(window.location.search).get("analysis") || "").trim().toLowerCase();
      if (requested) {
        const match = Array.from(typeSelect.options).find(
          (option) => option.value.toLowerCase() === requested
        );
        if (match) typeSelect.value = match.value;
      }
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
    // Repeat Fix Rate window and mechanism filter.
    const repeatWindow = $("lda-repeat-window");
    const repeatClear = $("lda-repeat-filter-clear");
    if (repeatWindow) repeatWindow.addEventListener("change", onRepeatWindowChange);
    if (repeatClear) repeatClear.addEventListener("click", clearRepeatFilter);
    // Set the initial secondary-panel visibility for the default analysis type.
    applyAnalysisTypeUI();
    window.addEventListener("resize", redrawCharts);

    // A canvas keeps the pixels it was painted with, so a theme change cannot
    // reach these the way it reaches everything else: they have to be drawn
    // again. Same redraw as a resize -- what changed is the palette rather than
    // the width, and every chart on the page reads both at draw time.
    // chart_theme.js has already dropped its cached read by the time this runs,
    // so picking the palette back up here gets the new values.
    window.gremlinOnThemeChange(() => {
      C = window.gremlinChartPalette();
      redrawCharts();
    });

    wireAnalysisTour();
    assetsLoaded = loadAssets();
    assetsLoaded.then(() => {
      if (window.gremlinTour) offerAnalysisPageTour();
    });
  }

  // Every chart currently on screen, redrawn. Which ones those are depends on
  // the analysis type showing, so the checks are the same ones the initial
  // render made.
  function redrawCharts() {
    if (state.paretoRows.length) drawPareto();
    if (state.analysisType === ANALYSIS_TYPES.TREND) renderTrendChart();
    if (state.analysisType === ANALYSIS_TYPES.PM) renderPmChart();
    if (state.analysisType === ANALYSIS_TYPES.DOWNTIME) {
      renderDowntimeTrendChart();
      renderDowntimeDistChart();
      renderDowntimeAssetChart();
    }
    if (state.analysisRedraw) state.analysisRedraw();
  }

  function initDispositionPage() {
    state.pageMode = "disposition";
    wireExcelHelpDialog();
    const params = new URLSearchParams(window.location.search);
    state.dispositionKind = (params.get("kind") || "wo").toLowerCase() === "pm" ? "pm" : "wo";
    state.dispositionScope = "all";
    state.dispositionPageIndex = 0;
    state.dispositionSearch = "";
    state.dispositionSort = { key: "", dir: "asc" };
    state.dispositionFilters = {};
    state.dispositionFilterSelection = "";

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
      // For the tour, which picks its example for what is in the box now.
      // applySearch does nothing if the search has been applied already.
      flushDispositionSearch = () => {
        clearTimeout(searchDebounce);
        return applySearch();
      };
      // Somebody typing a search is one of the things an owed tour waits out.
      searchInput.addEventListener("blur", startOwedDispositionTour);
    }

    wireDispositionTour();

    // Preselect the asset passed from the Perform Analysis page once the asset
    // list has loaded, then open its disposition editor.
    const requestedAsset = (params.get("asset") || "").trim();
    assetsLoaded = loadAssets();
    assetsLoaded.then(() => {
      if (requestedAsset && state.assetByNumber.has(requestedAsset)) {
        $("lda-asset").value = requestedAsset;
        evaluateAssetSelection();
      }
      offerDispositionTour();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
