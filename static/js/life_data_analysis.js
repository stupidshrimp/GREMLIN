/* Perform an Analysis: every analysis of one population of work orders.
 *
 * Pick an asset, and narrow it to the population to understand -- a sub-asset,
 * a component, a failure mechanism -- in the selector, or by clicking a Pareto
 * bar or a matrix cell, which drills down a level at a time. Every view on the page
 * reads that same population from the work-order dispositions
 * (services/population_analysis_service.py), so the Pareto, the trend, the
 * downtime, PM effectiveness, repeat fixes and the Weibull fit always count the
 * same records. The Weibull is the renewal-stream fit of the disposition
 * standard: lives built item by item, pooled, saved with the population's
 * definition string.
 */
(function () {
  "use strict";

  const API = "/life-data-analysis/api";
  // The chart palette, read out of theme.css by chart_theme.js: a canvas inherits
  // nothing from the cascade. Reassigned on a theme change, next to the redraw.
  let C = window.gremlinChartPalette();
  // Whether this browser session may write. Rendered by the server; the API
  // enforces the same rule on its own, this only keeps the page from offering an
  // action that would be refused.
  const CAN_EDIT = document.querySelector('meta[name="gremlin-can-edit"]')?.content === "true";
  const RECORD_EDIT_HINT = CAN_EDIT ? " Click a task ID to review or change its disposition." : "";
  const ANALYSIS_TYPES = {
    WEIBULL: "Weibull Analysis",
    TRIAGE: "Triage Analysis",
    DOWNTIME: "Downtime Driver Analysis",
    PM: "PM Effectiveness Analysis",
    REPEAT: "Repeat Fix Rate Analysis",
  };
  // What a population can be narrowed by, in the order it is narrowed: where on
  // the asset, what, how. The sub-asset list follows the asset's hierarchy, and a
  // Pareto bar or matrix cell narrows it too. The Weibull offers only the first
  // three, the ones that say which item fails which way; the other views offer all
  // six, and the categories.
  const DIMENSIONS = [
    ["locations", "Sub-asset", true],
    ["components", "Component", true],
    ["failure_mechanisms", "Failure mechanism", true],
    ["failure_modes", "Failure mode", false],
    ["failure_causes", "Failure cause", false],
    ["actions", "Action", false],
  ];
  const isWeibull = () => state.analysisType === ANALYSIS_TYPES.WEIBULL;
  // The dimensions the analysis type showing offers.
  function shownDimensions() {
    return DIMENSIONS.filter(([, , weibull]) => weibull || !isWeibull());
  }
  const READINESS_FIELDS = [
    ["in_reach", "Work orders in reach"],
    ["dispositioned", "Dispositioned"],
    ["not_reviewed", "Not reviewed yet"],
    ["to_confirm", "Migrated, still to confirm"],
    ["failures", "Failures in the population"],
    ["pm_resets", "PM resets in the population"],
    ["uncoded", "Failures and PM resets with no component"],
  ];
  const PARETO_MAX_BARS = 15;

  function emptySelection() {
    return Object.fromEntries(DIMENSIONS.map(([key]) => [key, []]));
  }

  const state = {
    asset: null,
    analysisType: ANALYSIS_TYPES.WEIBULL,
    // The population: what each selector list has ticked, the categories the
    // pattern views read, and whether induced failures count.
    selection: emptySelection(),
    categories: ["FAILURE"],
    excludeInduced: false,
    // The From and To days ("YYYY-MM-DD"), null for no bound. Every view takes
    // them; a Weibull run takes them as its start and cutoff.
    dateRange: { from: null, to: null },
    // Each narrowing a Pareto or matrix click made, to step back out of.
    drillHistory: [],
    options: null,
    optionsToken: 0,
    groupBy: "sub_asset",
    metric: "count",
    summary: null,
    summaryToken: 0,
    plantTimeZone: null,
    trendMonth: null,
    pm: null,
    pmToken: 0,
    downtime: null,
    downtimeToken: 0,
    repeat: null,
    repeatToken: 0,
    repeatWindow: 24,
    repeatFilter: null,
    // The Weibull result in the workspace, and a token that drops stale ones.
    result: null,
    analysisToken: 0,
    analysisRedraw: null,
    riskWeeks: 4,
    riskToken: 0,
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
  // The veil over the page, a part of the page being read again, placeholders and
  // entrances: the Disposition page's too (standard_common.js).
  const { beginLoading, endLoading, busy, reveal, skeleton } = window.GremlinStandard;
  const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");

  // What an asset shows while its first reads are on their way: placeholders in
  // the selector, the readiness counts, the rankings, the Pareto and each view's
  // cards, rather than an empty card.
  function showLoadingSkeletons() {
    const selector = $("lda-population-selector");
    selector.innerHTML = "";
    shownDimensions().forEach(() => {
      selector.appendChild(el("div", { class: "lda-field" }, [...skeleton("lda-skeleton-label"), ...skeleton("lda-skeleton-list")]));
    });
    // The drill bar and the Triage note are the last asset's until this one's are in.
    $("lda-drill").innerHTML = "";
    $("lda-drill").hidden = true;
    $("lda-trend-note").hidden = true;
    const grid = $("lda-summary-grid");
    grid.innerHTML = "";
    grid.classList.remove("lda-reveal-stagger");
    skeleton("lda-skeleton-metric", READINESS_FIELDS.length).forEach((node) => grid.appendChild(node));
    ["lda-beta-rankings", "lda-risk-rankings"].forEach((id) => {
      const list = $(id);
      list.innerHTML = "";
      ["", "is-short", ""].forEach((width) => list.appendChild(el("li", { class: "lda-skeleton-row" }, skeleton(`lda-skeleton-line ${width}`))));
    });
    [["lda-trend-cards", 4], ["lda-pm-cards", 4], ["lda-downtime-cards", 5], ["lda-repeat-cards", 4]].forEach(([id, count]) => {
      const cards = $(id);
      cards.innerHTML = "";
      skeleton("lda-skeleton-metric", count).forEach((node) => cards.appendChild(node));
    });
    $("lda-matrix-wrap").innerHTML = "";
    showParetoSkeleton(true);
  }

  function showParetoSkeleton(shown) {
    setHidden($("lda-pareto-skeleton"), !shown);
    setHidden($("lda-pareto-chart"), shown);
    if (shown) {
      // The title the bars on their way will have.
      const group = $("lda-pareto-group");
      const by = group.selectedIndex >= 0 ? group.options[group.selectedIndex].text.toLowerCase() : "sub-asset";
      $("lda-pareto-title").textContent = isWeibull() ? "Failures by sub-asset, component and mechanism" : `Pareto by ${by}`;
      $("lda-pareto-legend").innerHTML = "";
      setHidden($("lda-pareto-empty"), true);
      setHidden($("lda-pareto-not-coded"), true);
    }
  }

  // A view's own read (PM effectiveness, downtime, repeat fixes): its panels dim
  // while it runs, and its cards hold placeholders until its first result is in.
  function viewLoading(panelIds, cardsId, cardCount, message, hasResult) {
    if (!hasResult) {
      const cards = $(cardsId);
      cards.innerHTML = "";
      skeleton("lda-skeleton-metric", cardCount).forEach((node) => cards.appendChild(node));
    }
    const ends = panelIds.map((id, index) => busy($(id), index === 0 ? message : null));
    return () => ends.forEach((end) => end());
  }

  // A saved fit opened from the rankings is read before it shows: a placeholder
  // result card holds its place meanwhile.
  function showFitSkeleton(message) {
    const workspace = $("lda-workspace");
    workspace.innerHTML = "";
    workspace.appendChild(el("section", { class: "glass-card lda-card lda-skeleton-card lda-reveal", id: "lda-fit-loading", "aria-busy": "true" }, [
      el("p", { class: "lda-hint lda-skeleton-caption" }, [el("span", { class: "lda-spinner is-small", "aria-hidden": "true" }), message]),
      ...skeleton("lda-skeleton-line is-short"),
      ...skeleton("lda-skeleton-line"),
      el("div", { class: "lda-skeleton-pair" }, skeleton("lda-skeleton-block", 2)),
    ]));
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

  // ---- the population -------------------------------------------------------
  // What every request is sent: the population and the From/To days. The Weibull
  // is sent only what it offers; the mode, cause, action and categories ticked on
  // the other views wait there for them.
  function selectionPayload() {
    const selection = { assets: state.asset ? [state.asset] : [] };
    shownDimensions().forEach(([key]) => {
      selection[key] = state.selection[key].slice();
    });
    if (!isWeibull()) selection.categories = state.categories.slice();
    selection.exclude_induced = state.excludeInduced;
    return selection;
  }
  function populationBody(extra) {
    return {
      selection: selectionPayload(),
      date_from: state.dateRange.from,
      date_to: state.dateRange.to,
      ...(extra || {}),
    };
  }

  async function chooseAsset(asset) {
    if (asset === state.asset) return;
    state.asset = asset;
    state.selection = emptySelection();
    state.drillHistory = [];
    state.groupBy = "sub_asset";
    $("lda-pareto-group").value = "sub_asset";
    state.summary = null;
    state.options = null;
    resetAnalyses();
    clearWorkspace();
    $("lda-population-card").hidden = !asset;
    $("lda-summary-card").hidden = !asset;
    state.renderedReadiness = null;
    if (!asset) return;
    showLoadingSkeletons();
    const link = $("lda-disposition-link");
    if (link) link.href = `/life-data-analysis/disposition?asset=${encodeURIComponent(asset)}`;
    await loadOptions();
    if (asset !== state.asset) return;
    setGroupBy(defaultGroupBy());
    await refresh();
  }

  // The Pareto's first dimension: sub-asset, unless the asset has none, when a
  // sub-asset Pareto would be the one bar and it opens by component instead.
  function defaultGroupBy() {
    const places = state.options && state.options.dimensions && state.options.dimensions.locations;
    return !places || places.length > 1 ? "sub_asset" : "component";
  }

  function setGroupBy(value) {
    state.groupBy = value;
    $("lda-pareto-group").value = value;
  }

  function resetAnalyses() {
    state.pm = null;
    state.downtime = null;
    state.repeat = null;
    state.repeatFilter = null;
    state.trendMonth = null;
    state.result = null;
    state.pmToken += 1;
    state.downtimeToken += 1;
    state.repeatToken += 1;
    state.analysisToken += 1;
  }

  async function loadOptions() {
    if (!state.asset) return;
    const token = ++state.optionsToken;
    const end = busy($("lda-population-card"), `Loading ${state.asset}'s sub-assets and codes…`);
    try {
      const data = await getJson(`${API}/population-options?asset=${encodeURIComponent(state.asset)}`);
      if (token !== state.optionsToken) return;
      state.options = data;
      renderSelector();
      reveal($("lda-population-selector"));
    } catch (err) {
      showBanner(err.message, "error");
    } finally {
      end();
    }
  }

  // The selector: one list per dimension, each value with how many work orders
  // in reach carry it, plus the categories, the induced-failure switch and the
  // dates. A value that is selected but not in the list (a drill into "not
  // coded", or a retired code) still shows, ticked.
  function renderSelector() {
    const wrap = $("lda-population-selector");
    wrap.innerHTML = "";
    const dimensions = (state.options && state.options.dimensions) || {};
    shownDimensions().forEach(([key, label]) => {
      const values = (dimensions[key] || []).slice();
      const chosen = state.selection[key];
      chosen.forEach((value) => {
        if (!values.some((option) => option.value === value)) values.unshift({ value, label: value, count: 0 });
      });
      const list = el("div", { class: "std-multi", role: "group", "aria-label": label });
      if (!values.length) list.appendChild(el("span", { class: "std-multi-empty", text: "None coded yet" }));
      values.forEach((option) => {
        const box = el("input", {
          type: "checkbox",
          value: option.value,
          checked: chosen.includes(option.value),
          onchange: (event) => {
            const next = state.selection[key].filter((value) => value !== option.value);
            if (event.target.checked) next.push(option.value);
            state.selection[key] = next;
            state.drillHistory = [];
            onPopulationChange();
          },
        });
        // A sub-asset sits indented under its parent, as in the Disposition page's dropdown.
        const indent = option.depth > 1 ? `${"\u00a0\u00a0\u00a0".repeat(option.depth - 1)}└ ` : "";
        list.appendChild(el("label", {}, [box, el("span", { text: indent + option.label }), el("span", { class: "std-count", text: String(option.count) })]));
      });
      wrap.appendChild(el("div", { class: "lda-field", id: `lda-dim-${key}` }, [
        el("div", { class: "std-multi-head" }, [
          el("span", { class: "lda-field-label", text: label }),
          chosen.length
            ? el("button", { type: "button", text: "Clear", onclick: () => {
                state.selection[key] = [];
                state.drillHistory = [];
                onPopulationChange();
              } })
            : null,
        ]),
        list,
      ]));
    });
    const categories = $("lda-categories");
    categories.innerHTML = "";
    ((state.options && state.options.categories) || []).forEach((option) => {
      categories.appendChild(el("label", { class: "lda-checkbox" }, [
        el("input", {
          type: "checkbox",
          checked: state.categories.includes(option.value),
          onchange: (event) => {
            const next = state.categories.filter((value) => value !== option.value);
            if (event.target.checked) next.push(option.value);
            state.categories = next;
            // A drill remembers the categories it came from: Back would undo this.
            state.drillHistory = [];
            onPopulationChange();
          },
        }),
        option.label,
      ]));
    });
    $("lda-exclude-induced").checked = state.excludeInduced;
    renderDrill();
  }

  // A selected value as the selector lists it: a sub-asset by its name, a code
  // with its name.
  function valueLabel(key, value) {
    if (key === "categories") {
      const category = ((state.options && state.options.categories) || []).find((item) => item.value === value);
      return category ? category.label : value;
    }
    const options = (state.options && state.options.dimensions && state.options.dimensions[key]) || [];
    const option = options.find((item) => item.value === value);
    return option ? option.label : value;
  }

  // Whether a selection narrows what the analysis type showing reads: the mode,
  // cause and action ticked for the other views wait unread while the Weibull shows.
  const narrows = (selection) => shownDimensions().some(([key]) => selection[key].length);
  const hasNarrowing = () => narrows(state.selection);

  // A drill step as its crumb reads: what the click narrowed to.
  function stepLabel(step) {
    return Object.entries(step || {}).map(([key, value]) => valueLabel(key, value)).join(" · ");
  }

  // Right above the Pareto's bars: how it got here. After a drill, a breadcrumb
  // from where the drilling started, each step clickable to go back to it, with
  // Back for the last one and Start over. Filters ticked by hand show as chips
  // that each take one back out. So undoing a drill never means scrolling back
  // up to the population.
  function renderDrill() {
    const wrap = $("lda-drill");
    wrap.innerHTML = "";
    const history = state.drillHistory;
    const startOverButton = () => el("button", {
      type: "button",
      class: "btn-secondary lda-drill-btn",
      text: "Start over",
      title: "Take every filter back out",
      onclick: startOver,
    });
    if (history.length) {
      const trail = el("ol", { class: "lda-crumbs" });
      const rootNarrowed = narrows(history[0].selection);
      const assetLabel = (state.options && state.options.asset_labels && state.options.asset_labels[state.asset]) || state.asset || "the asset";
      const crumb = (text, index) => el("li", {}, [el("button", {
        type: "button",
        class: "lda-crumb",
        text,
        title: "Go back to here",
        onclick: () => restoreDrill(index),
      })]);
      trail.appendChild(crumb(rootNarrowed ? "Your filters" : `All of ${assetLabel}`, 0));
      for (let index = 1; index < history.length; index += 1) {
        trail.appendChild(crumb(stepLabel(history[index - 1].step), index));
      }
      trail.appendChild(el("li", {}, [el("span", {
        class: "lda-crumb is-current",
        "aria-current": "true",
        text: stepLabel(history[history.length - 1].step),
      })]));
      wrap.appendChild(trail);
      wrap.appendChild(el("div", { class: "lda-drill-actions" }, [
        el("button", { type: "button", class: "btn-secondary lda-drill-btn", text: "← Back", title: "Undo the last drill", onclick: drillBack }),
        startOverButton(),
      ]));
      wrap.hidden = false;
      return;
    }
    const chips = [];
    shownDimensions().forEach(([key, label]) => {
      state.selection[key].forEach((value) => {
        const named = valueLabel(key, value);
        chips.push(el("button", {
          type: "button",
          class: "std-chip lda-drill-chip",
          title: `Take ${named} out of the population`,
          text: `${label}: ${named} ×`,
          onclick: () => {
            state.selection[key] = state.selection[key].filter((item) => item !== value);
            onPopulationChange();
          },
        }));
      });
    });
    if (chips.length) {
      wrap.appendChild(el("div", { class: "lda-drill-chips" }, [el("span", { class: "lda-hint", text: "Filtered by:" }), ...chips]));
      wrap.appendChild(el("div", { class: "lda-drill-actions" }, [startOverButton()]));
      wrap.hidden = false;
      return;
    }
    // A Pareto by detection method has nothing to narrow by: a click only says so.
    const narrowsBy = state.options && state.options.group_by_selection;
    if (!isWeibull() && (!narrowsBy || narrowsBy[state.groupBy])) {
      wrap.appendChild(el("span", { class: "lda-hint", text: "Click a bar to narrow the population to it." }));
      wrap.hidden = false;
      return;
    }
    wrap.hidden = true;
  }

  function startOver() {
    // A drill into a disposition category narrowed the categories too: back to
    // the ones from before the first drill, as the first crumb would.
    if (state.drillHistory.length) state.categories = state.drillHistory[0].categories.slice();
    state.selection = emptySelection();
    state.drillHistory = [];
    setGroupBy(defaultGroupBy());
    onPopulationChange();
  }

  let populationDebounce = null;
  function onPopulationChange() {
    renderSelector();
    resetAnalyses();
    clearWorkspace();
    clearTimeout(populationDebounce);
    populationDebounce = setTimeout(refresh, 250);
  }

  // Where the population was, and the step taken from there, for the
  // breadcrumb and Back to return to.
  function rememberDrill(step) {
    state.drillHistory.push({
      selection: JSON.parse(JSON.stringify(state.selection)),
      categories: state.categories.slice(),
      groupBy: state.groupBy,
      step,
    });
  }

  // Narrow the population by one value of a dimension, remembering where it was.
  function drillInto(changes, nextGroupBy) {
    // The one bar left once the drilling bottoms out, or the cell already
    // narrowed to, changes nothing: no step for the breadcrumb or Back.
    const only = (list, value) => list.length === 1 && list[0] === value;
    const unchanged = Object.entries(changes).every(([key, value]) =>
      only(key === "categories" ? state.categories : state.selection[key], value));
    if (unchanged && (!nextGroupBy || isWeibull() || nextGroupBy === state.groupBy)) return;
    rememberDrill(changes);
    Object.entries(changes).forEach(([key, value]) => {
      if (key === "categories") state.categories = [value];
      else state.selection[key] = [value];
    });
    // The Weibull's Pareto is always by sub-asset, component and mechanism.
    if (nextGroupBy && !isWeibull()) setGroupBy(nextGroupBy);
    onPopulationChange();
  }

  // Back to where the population was before drill `index`, forgetting the
  // drills after it. Read as any change is, so a drill's read still waiting is
  // replaced rather than run as well.
  function restoreDrill(index) {
    const target = state.drillHistory[index];
    if (!target) return;
    state.drillHistory = state.drillHistory.slice(0, index);
    state.selection = target.selection;
    state.categories = target.categories;
    setGroupBy(target.groupBy);
    onPopulationChange();
  }

  function drillBack() {
    restoreDrill(state.drillHistory.length - 1);
  }

  // ---- loading what the page shows ------------------------------------------
  // The summary (readiness, Pareto, matrix, trend, rankings) on every change,
  // then whatever the analysis type showing needs on top of it.
  async function refresh() {
    if (!state.asset) return;
    await loadSummary();
    loadAnalysis();
  }

  async function loadSummary() {
    const asset = state.asset;
    const token = ++state.summaryToken;
    const riskToken = ++state.riskToken;
    const end = busy($("lda-summary-card"), isWeibull() ? "Counting the population's failures…" : "Reading the population…");
    try {
      const data = await postJson(
        `${API}/summary`,
        populationBody({ group_by: state.groupBy, weeks: riskWeeks(), view: isWeibull() ? "weibull" : "triage" }),
      );
      if (token !== state.summaryToken || asset !== state.asset) return;
      clearBanner();
      state.summary = data;
      state.plantTimeZone = data.plant_time_zone || null;
      renderSummary();
      if (riskToken === state.riskToken) renderRiskRankings(data.risk_rankings || []);
    } catch (err) {
      if (token === state.summaryToken) showBanner(err.message, "error");
    } finally {
      end();
    }
  }

  function loadAnalysis() {
    if (!state.asset) return;
    if (state.analysisType === ANALYSIS_TYPES.PM) loadPm();
    else if (state.analysisType === ANALYSIS_TYPES.DOWNTIME) loadDowntime();
    else if (state.analysisType === ANALYSIS_TYPES.REPEAT) loadRepeat();
    else if (state.analysisType === ANALYSIS_TYPES.WEIBULL) showFitPrompt();
  }

  function renderSummary() {
    const summary = state.summary;
    if (!summary) return;
    $("lda-pop-string").textContent = summary.population_definition;
    // The counts rise in the first time an asset's are shown; after that a count
    // that changed flashes once.
    const grid = $("lda-summary-grid");
    const previous = state.renderedReadiness;
    const values = Object.fromEntries(READINESS_FIELDS.map(([key]) => [key, String((summary.readiness || {})[key] ?? "—")]));
    grid.innerHTML = "";
    grid.classList.toggle("lda-reveal-stagger", !previous);
    READINESS_FIELDS.forEach(([key, label]) => {
      const changed = Boolean(previous) && previous[key] !== values[key];
      grid.appendChild(el("div", { class: "lda-metric" + (changed ? " is-changed" : "") }, [
        el("span", { class: "lda-metric-value", text: values[key] }),
        el("span", { class: "lda-metric-label", text: label }),
      ]));
    });
    state.renderedReadiness = values;
    renderRankings(summary.rankings || []);
    showParetoSkeleton(false);
    animatePareto();
    renderMatrix();
    if (state.analysisType === ANALYSIS_TYPES.TRIAGE) renderTrend();
    renderPerformHint();
  }

  // ---- rankings of the saved fits ---------------------------------------------
  function rankingMarker(row) {
    if (row.method_current === false) return " — saved under an earlier method; run it again";
    if (row.schedule_current === false) return " — counted on a schedule since changed; run it again";
    if (row.time_zone_current === false) return " — counted in an earlier plant time zone; run it again";
    return "";
  }

  // Opening a ranked fit makes its population the page's and shows that saved
  // fit (or, given only a population, as the tour's example is, the latest fit
  // saved for it). Nothing else opens a saved fit by itself.
  function openRankedFit(row) {
    const selection = row.selection || {};
    state.selection = emptySelection();
    DIMENSIONS.forEach(([key]) => {
      state.selection[key] = (selection[key] || []).slice();
    });
    state.excludeInduced = Boolean(selection.exclude_induced);
    state.drillHistory = [];
    if (state.analysisType !== ANALYSIS_TYPES.WEIBULL) {
      $("lda-analysis-type").value = ANALYSIS_TYPES.WEIBULL;
      setAnalysisType(ANALYSIS_TYPES.WEIBULL, { skipLoad: true });
    }
    renderSelector();
    resetAnalyses();
    clearWorkspace();
    clearTimeout(populationDebounce);
    const opened = loadSavedFit(row.analysis_id);
    return Promise.all([refresh(), opened]);
  }

  function rankingItem(row, text) {
    return el("li", {}, [
      el("button", { type: "button", class: "lda-ranking-link", text: row.label, onclick: () => openRankedFit(row) }),
      document.createTextNode(text + rankingMarker(row)),
    ]);
  }

  // A ranking list rises in again when what it says has changed.
  function revealIfChanged(list, before) {
    if (list.textContent !== before) reveal(list);
  }

  function renderRankings(rankings) {
    const list = $("lda-beta-rankings");
    const before = list.textContent;
    list.innerHTML = "";
    if (!rankings.length) {
      list.appendChild(el("li", { class: "is-empty", text: "No saved one-component Weibull fit on this asset has enough failures to rank yet." }));
      revealIfChanged(list, before);
      return;
    }
    rankings.forEach((row) => {
      list.appendChild(rankingItem(row,
        `: beta ${fmt(row.beta_mle)} (${row.failure_count} failures, eta ${fmt(row.eta_mle)} h` +
        (row.probability_plot_r_squared != null
          ? `, plot R² ${Number(row.probability_plot_r_squared).toFixed(2)}${row.probability_plot_review ? ", below its review threshold" : ""}`
          : "") + ")"));
    });
    revealIfChanged(list, before);
  }

  function renderRiskRankings(rankings) {
    const list = $("lda-risk-rankings");
    const before = list.textContent;
    list.innerHTML = "";
    if (!rankings.length) {
      const ranked = state.summary && (state.summary.rankings || []).length;
      list.appendChild(el("li", {
        class: "is-empty",
        text: ranked
          ? "None of the ranked fits had an item still in service at its run's cutoff: each ended in a failure that did not renew the item."
          : "No saved one-component Weibull fit on this asset has enough failures to rank yet.",
      }));
      revealIfChanged(list, before);
      return;
    }
    rankings.forEach((row) => {
      const percent = 100 * Number(row.probability);
      const asOf = cutoffPlantDate(row.analysis_cutoff, row.analysis_cutoff_source, row.time_zone);
      list.appendChild(rankingItem(row,
        `: ${percent.toFixed(percent < 10 ? 1 : 0)}% chance of a failure in the next ${fmt(row.window_weeks)} weeks ` +
        `(${row.items_in_service} item${row.items_in_service === 1 ? "" : "s"} in service, oldest ${fmt(row.current_life_hours)} h, ` +
        `beta ${fmt(row.beta_mle)}${asOf ? `, as of ${asOf}` : ""})`));
    });
    revealIfChanged(list, before);
  }

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
  async function onRiskWeeksChange() {
    if (riskWeekValue($("lda-risk-weeks")) == null) {
      riskWeeks();
      showBanner("The window must be a whole number of weeks from 1 to 52.", "error");
      return;
    }
    if (!state.asset) return;
    const token = ++state.riskToken;
    const end = busy($("lda-risk-panel"), "Ranking the saved fits…");
    try {
      const data = await postJson(`${API}/risk-rankings`, populationBody({ weeks: riskWeeks() }));
      if (token === state.riskToken) renderRiskRankings(data.rankings || []);
    } catch (err) {
      if (token === state.riskToken) showBanner(err.message, "error");
    } finally {
      end();
    }
  }

  // ---- From/To dates ------------------------------------------------------------
  // Today as a Weibull run counts it: the plant's day right now.
  function plantToday() {
    if (state.plantTimeZone) return plantTimeText(new Date().toISOString(), state.plantTimeZone).slice(0, 10);
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  }

  // The dates as a Weibull run's window. A To date of today or later is left
  // off: with no cutoff the run ends at the last completed import, which never
  // credits the current life with hours the database has heard nothing about.
  function weibullDateWindow() {
    const { from, to } = state.dateRange;
    return { analysis_start: from || null, analysis_cutoff: to && to < plantToday() ? to : null };
  }

  let dateDebounce = null;
  function onDateRangeChange() {
    clearTimeout(dateDebounce);
    dateDebounce = setTimeout(applyDateRange, 400);
  }
  function applyDateRange() {
    const fromInput = $("lda-date-from");
    const toInput = $("lda-date-to");
    let from = fromInput.value || null;
    let to = toInput.value || null;
    if (from && to && from > to) {
      [from, to] = [to, from];
      fromInput.value = from;
      toInput.value = to;
    }
    if (from === state.dateRange.from && to === state.dateRange.to) return;
    state.dateRange = { from, to };
    resetAnalyses();
    clearWorkspace();
    refresh();
  }
  function resetDateRange() {
    $("lda-date-from").value = "";
    $("lda-date-to").value = "";
    applyDateRange();
  }

  // ---- Pareto ---------------------------------------------------------------------
  // One bar per value of the dimension chosen, ranked by count or downtime, each
  // split by failure cause; a click narrows the population to the bar and steps
  // the dimension down (sub-asset, then component, then mechanism).
  function paretoRows() {
    const pareto = state.summary && state.summary.pareto;
    if (!pareto) return [];
    const metric = state.metric;
    const rows = pareto.rows.map((row) => ({ ...row }));
    rows.sort((a, b) => (Number(b[metric]) || 0) - (Number(a[metric]) || 0));
    const total = rows.reduce((sum, row) => sum + (Number(row[metric]) || 0), 0) || 1;
    let cumulative = 0;
    rows.forEach((row) => {
      cumulative += Number(row[metric]) || 0;
      row._cumulative_percent = (cumulative / total) * 100;
    });
    return rows;
  }

  // Every cause in the population gets one colour, the same across the bars.
  function causeColours(rows) {
    const order = [];
    rows.forEach((row) => row.causes.forEach((cause) => {
      if (!order.some((item) => item.key === cause.key)) order.push({ key: cause.key, label: cause.label });
    }));
    const series = (C.series && C.series.length ? C.series : [C.bar]);
    return order.map((item, index) => ({ ...item, colour: series[index % series.length] }));
  }

  function paretoClick(row) {
    const pareto = state.summary.pareto;
    if (row.selection) {
      fitBar(row);
      return;
    }
    if (!pareto.selection_key) {
      showToast(`${pareto.label} can't narrow the population; group by another dimension to drill down.`, "info");
      return;
    }
    drillInto({ [pareto.selection_key]: row.key }, pareto.next_group_by);
  }

  // New bars grow up from the axis, and the cumulative line with them.
  const PARETO_GROW_MS = 480;
  let paretoFrame = null;
  function animatePareto() {
    cancelAnimationFrame(paretoFrame);
    if (REDUCED_MOTION.matches) {
      drawPareto();
      return;
    }
    const started = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - started) / PARETO_GROW_MS);
      drawPareto(1 - (1 - t) ** 3);
      if (t < 1) paretoFrame = requestAnimationFrame(step);
    };
    paretoFrame = requestAnimationFrame(step);
  }

  // ``grown`` is how far the bars have risen, 0 to 1, while they animate in.
  function drawPareto(grown = 1) {
    const canvas = $("lda-pareto-chart");
    const pareto = state.summary && state.summary.pareto;
    // The Weibull's bars are populations to fit: failures, by sub-asset, component and mechanism.
    const fitting = Boolean(pareto && pareto.group_by === "weibull_population");
    const counted = fitting ? "Failures" : "Work orders";
    $("lda-pareto-title").textContent = pareto
      ? `${fitting ? "Failures" : "Pareto"} by ${pareto.label.toLowerCase()}`
      : "Pareto";
    document.querySelectorAll("#lda-pareto-metric [data-metric]").forEach((button) => {
      const active = button.dataset.metric === state.metric;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
    const allRows = paretoRows();
    const rows = allRows.slice(0, PARETO_MAX_BARS);
    $("lda-pareto-empty").hidden = allRows.length > 0;
    // Work orders not coded on the dimension make no bar; say how many there are.
    const notCoded = (pareto && pareto.not_coded) || { count: 0 };
    const notCodedNote = $("lda-pareto-not-coded");
    notCodedNote.hidden = !notCoded.count;
    const notCodedWhat = fitting
      ? `failure${notCoded.count === 1 ? "" : "s"} with no component or mechanism`
      : `work order${notCoded.count === 1 ? "" : "s"} not coded on ${pareto.label.toLowerCase()}`;
    notCodedNote.textContent = notCoded.count
      ? `${notCoded.count} ${notCodedWhat} ` +
        `(${fmt(notCoded.downtime_hours)} h downtime) ${notCoded.count === 1 ? "is" : "are"} left out of the bars; ` +
        "disposition them to bring them in."
      : "";
    const metric = state.metric;
    const { ctx, width: W, height: H } = setupCanvas(canvas, fitting ? 360 : 340);
    ctx.clearRect(0, 0, W, H);
    canvas.onclick = null;
    canvas.onmousemove = null;
    canvas.onmouseleave = null;
    canvas.style.cursor = "";
    hidePointTooltip();
    const colours = causeColours(rows);
    const legend = $("lda-pareto-legend");
    legend.innerHTML = "";
    if (rows.length) {
      legend.appendChild(el("span", { text: "Failure cause:" }));
      colours.forEach((cause) => legend.appendChild(el("span", {}, [el("i", { style: `background:${cause.colour}` }), cause.label])));
    }
    if (!rows.length) return;

    const left = 64;
    const right = W - 56;
    const top = 24;
    // Room under the axis for the slanted names; a sub-asset, component and
    // mechanism together need more than one value does.
    const nameLength = fitting ? 28 : 20;
    const bottom = H - (fitting ? 100 : 80);
    const plotH = bottom - top;
    const slot = (right - left) / rows.length;
    const maxVal = Math.max(...rows.map((r) => Number(r[metric]) || 0), 1);
    const barGap = 8;
    // A few bars stay bars rather than slabs across the whole chart.
    const barW = Math.max(6, Math.min(slot - barGap, 110));
    const hitboxes = [];
    const tickLabel = (v) => {
      const n = Number(v) || 0;
      if (Math.abs(n) >= 1000) return Math.round(n / 100) / 10 + "k";
      return n >= 100 ? String(Math.round(n)) : Number(n.toPrecision(3)).toString();
    };
    ctx.font = "10px Inter, sans-serif";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 5; i += 1) {
      const frac = i / 5;
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
    ctx.strokeStyle = C.axis;
    ctx.beginPath();
    ctx.moveTo(left, top);
    ctx.lineTo(left, bottom);
    ctx.lineTo(right, bottom);
    ctx.moveTo(right, top);
    ctx.lineTo(right, bottom);
    ctx.stroke();

    rows.forEach((row, index) => {
      const value = Number(row[metric]) || 0;
      const x = left + index * slot + (slot - barW) / 2;
      let y = bottom;
      // Each cause's share of the bar, stacked from the axis up.
      row.causes.forEach((cause) => {
        const part = metric === "count" ? Number(cause.count) : Number(cause.downtime_hours);
        const height = (part / maxVal) * plotH * grown;
        ctx.fillStyle = (colours.find((item) => item.key === cause.key) || { colour: C.bar }).colour;
        ctx.fillRect(x, y - height, barW, height);
        y -= height;
      });
      hitboxes.push({ x, y: top, w: barW, h: plotH, row });
      ctx.save();
      ctx.fillStyle = C.label;
      ctx.font = "10px Inter, sans-serif";
      ctx.translate(x + barW / 2, bottom + 6);
      ctx.rotate(Math.PI / 5);
      const name = String(row.label || "—");
      ctx.fillText(name.length > nameLength ? `${name.slice(0, nameLength - 1)}…` : name, 0, 0);
      ctx.restore();
      if (value > 0) {
        ctx.save();
        ctx.globalAlpha = grown;
        ctx.fillStyle = C.ink;
        ctx.textAlign = "center";
        ctx.fillText(tickLabel(value), x + barW / 2, bottom - (value / maxVal) * plotH * grown - 4);
        ctx.restore();
      }
    });

    ctx.strokeStyle = C.highlight;
    ctx.lineWidth = 2;
    ctx.beginPath();
    rows.forEach((row, index) => {
      const x = left + index * slot + slot / 2;
      const y = bottom - ((Number(row._cumulative_percent) || 0) / 100) * plotH * grown;
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.fillStyle = C.highlight;
    rows.forEach((row, index) => {
      ctx.beginPath();
      ctx.arc(left + index * slot + slot / 2, bottom - ((Number(row._cumulative_percent) || 0) / 100) * plotH * grown, 3, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.fillStyle = C.label;
    ctx.textAlign = "center";
    ctx.save();
    ctx.translate(13, (top + bottom) / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText(metric === "count" ? counted : "Downtime (hours)", 0, 0);
    ctx.restore();
    ctx.save();
    ctx.translate(W - 9, (top + bottom) / 2);
    ctx.rotate(Math.PI / 2);
    ctx.fillText("Cumulative %", 0, 0);
    ctx.restore();
    if (allRows.length > rows.length) ctx.fillText(`Top ${rows.length} of ${allRows.length}`, (left + right) / 2, 12);
    ctx.textAlign = "left";

    const barAt = (event) => {
      const rect = canvas.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      return hitboxes.find((box) => px >= box.x && px <= box.x + box.w && py >= box.y && py <= box.y + box.h);
    };
    const canDrill = fitting || Boolean(pareto.selection_key);
    canvas.onclick = (event) => {
      const hit = barAt(event);
      if (hit) paretoClick(hit.row);
    };
    canvas.onmousemove = (event) => {
      const hit = barAt(event);
      canvas.style.cursor = hit ? "pointer" : "";
      if (!hit) return hidePointTooltip();
      const row = hit.row;
      let clickTo = "—";
      if (fitting) clickTo = `fit ${row.label}`;
      else if (canDrill && row.key != null) {
        clickTo = `narrow to ${row.label}${pareto.next_group_by ? ` and show it by ${pareto.next_group_by.replace("_", " ")}` : ""}`;
      }
      showTooltipRows(event.clientX, event.clientY, [
        ...(fitting ? (row.parts || []).map((part) => [part.label, part.value]) : [[pareto.label, row.label]]),
        [counted, String(row.count)],
        ["Downtime", `${fmt(row.downtime_hours)} h`],
        ["Cumulative", `${fmt(row._cumulative_percent, 3)}%`],
        ...row.causes.slice(0, 4).map((cause) => [cause.label, `${cause.count} (${fmt(cause.downtime_hours)} h)`]),
        ["Click to", clickTo],
      ]);
    };
    canvas.onmouseleave = () => {
      canvas.style.cursor = "";
      hidePointTooltip();
    };
  }

  // ---- sub-asset x component matrix --------------------------------------------
  function renderMatrix() {
    const wrap = $("lda-matrix-wrap");
    wrap.innerHTML = "";
    const matrix = state.summary && state.summary.matrix;
    if (!matrix || !matrix.rows.length) {
      wrap.appendChild(el("p", { class: "lda-hint", text: "No work orders in this population yet." }));
      return;
    }
    const table = el("table", { class: "std-matrix" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { scope: "col", text: "Sub-asset \\ component" }),
      ...matrix.columns.map((column) => el("th", { scope: "col", text: column.label, title: `${column.total} work orders` })),
    ])]));
    const body = el("tbody");
    const peak = matrix.max_count || 1;
    matrix.rows.forEach((row, rowIndex) => {
      body.appendChild(el("tr", {}, [
        el("th", { scope: "row", text: row.label }),
        ...matrix.columns.map((column, columnIndex) => {
          const cell = matrix.cells[rowIndex][columnIndex];
          if (!cell.count) return el("td", { class: "is-zero", text: "·" });
          const drillable = row.key != null && column.key != null;
          return el("td", {
            class: "has-count",
            style: `background: color-mix(in srgb, ${C.bar} ${Math.round(12 + 60 * (cell.count / peak))}%, transparent)`,
            title: `${cell.count} work orders, ${fmt(cell.downtime_hours)} h downtime` + (drillable ? ". Click to narrow to it." : ""),
            text: String(cell.count),
            onclick: drillable ? () => drillInto({ locations: row.key, components: column.key }, "failure_mechanism") : null,
          });
        }),
      ]));
    });
    table.appendChild(body);
    wrap.appendChild(table);
  }

  // ---- analysis type ---------------------------------------------------------------
  function setHidden(node, hidden) {
    if (node) node.hidden = Boolean(hidden);
  }

  function setAnalysisType(value, { skipLoad } = {}) {
    const wasWeibull = isWeibull();
    state.analysisType = value || ANALYSIS_TYPES.WEIBULL;
    // The Weibull and the other views read different populations and draw
    // different Pareto bars, so going between them reads the summary again.
    const viewChanged = wasWeibull !== isWeibull();
    if (viewChanged) {
      state.drillHistory = [];
      if (state.options) renderSelector();
    }
    applyAnalysisTypeUI();
    if (skipLoad || !state.asset || !state.summary) return;
    if (viewChanged) {
      resetAnalyses();
      clearWorkspace();
      // The other view's bars are not this one's: a placeholder until these are in.
      showParetoSkeleton(true);
      $("lda-matrix-wrap").innerHTML = "";
      refresh();
      return;
    }
    if (state.analysisType === ANALYSIS_TYPES.TRIAGE) renderTrend();
    loadAnalysis();
  }

  function applyAnalysisTypeUI() {
    const type = state.analysisType;
    const is = (other) => type === other;
    $("lda-step-3").textContent = {
      [ANALYSIS_TYPES.WEIBULL]: "Weibull readiness",
      [ANALYSIS_TYPES.TRIAGE]: "Triage",
      [ANALYSIS_TYPES.PM]: "PM effectiveness",
      [ANALYSIS_TYPES.DOWNTIME]: "Downtime drivers",
      [ANALYSIS_TYPES.REPEAT]: "Repeat fix rate",
    }[type] || type;
    setHidden($("lda-weibull-summary"), !is(ANALYSIS_TYPES.WEIBULL));
    setHidden($("lda-beta-panel"), !is(ANALYSIS_TYPES.WEIBULL));
    setHidden($("lda-risk-panel"), !is(ANALYSIS_TYPES.WEIBULL));
    setHidden($("lda-actions"), !is(ANALYSIS_TYPES.WEIBULL));
    setHidden($("lda-perform-hint"), !is(ANALYSIS_TYPES.WEIBULL));
    // The Weibull narrows by sub-asset, component and mechanism only, and its
    // Pareto is always by all three.
    setHidden($("lda-categories-field"), is(ANALYSIS_TYPES.WEIBULL));
    setHidden($("lda-pareto-group-field"), is(ANALYSIS_TYPES.WEIBULL));
    setHidden($("lda-pop-string"), !is(ANALYSIS_TYPES.WEIBULL));
    setHidden($("lda-population-hint-weibull"), !is(ANALYSIS_TYPES.WEIBULL));
    setHidden($("lda-population-hint-triage"), is(ANALYSIS_TYPES.WEIBULL));
    setHidden($("lda-pareto-weibull-hint"), !is(ANALYSIS_TYPES.WEIBULL));
    ["lda-trend-summary", "lda-trend-chart-panel", "lda-trend-table-panel", "lda-trend-wo-panel"].forEach((id) => setHidden($(id), !is(ANALYSIS_TYPES.TRIAGE)));
    ["lda-pm-summary", "lda-pm-chart-panel", "lda-pm-table-panel"].forEach((id) => setHidden($(id), !is(ANALYSIS_TYPES.PM)));
    ["lda-downtime-summary", "lda-downtime-trend-panel", "lda-downtime-dist-panel", "lda-downtime-asset-panel", "lda-downtime-events-panel"]
      .forEach((id) => setHidden($(id), !is(ANALYSIS_TYPES.DOWNTIME)));
    ["lda-repeat-summary", "lda-repeat-rate-panel", "lda-repeat-pairs-panel"].forEach((id) => setHidden($(id), !is(ANALYSIS_TYPES.REPEAT)));
    if (!is(ANALYSIS_TYPES.WEIBULL)) clearWorkspace();
    if (state.summary) drawPareto();
  }

  // ---- record numbers that open the record ---------------------------------------
  // A task ID in an analysis table, as a button that opens that work order's
  // disposition over the page for an editor, and as text for anybody else.
  function recordNumberCell(taskId, mappedRecordId, emptyText) {
    const text = taskId != null && taskId !== "" ? String(taskId) : emptyText || "";
    if (!CAN_EDIT || mappedRecordId == null || !text) return el("td", { class: "lda-nowrap", text });
    return el("td", { class: "lda-nowrap", "data-column-text": text }, [
      el("button", {
        type: "button",
        class: "lda-record-link",
        title: "Review or change this work order's disposition",
        text,
        onclick: () => openRecordEditor(mappedRecordId),
      }),
    ]);
  }

  // ---- failure trend -----------------------------------------------------------------
  function trendDeltaText(value) {
    const n = Number(value) || 0;
    const sign = n > 0 ? "+" : n < 0 ? "−" : "±";
    return `${sign}${Math.abs(n)} vs. the 3 months before`;
  }

  function renderTrend() {
    const trend = state.summary && state.summary.trend;
    if (!trend) return;
    const grid = $("lda-trend-cards");
    const before = grid.textContent;
    grid.innerHTML = "";
    const summary = trend.summary || {};
    const dimension = trend.label.toLowerCase();
    // [key, title, detail of a value, what an empty card says, whether it compares months]
    [
      ["most_frequent", `Most frequent ${dimension}`, (c) => `${c.value} work orders`, null, false],
      // Downtime on the asset itself or on work orders not coded here isn't any value's.
      ["highest_downtime", "Highest downtime", (c) => `${fmt(c.value)} downtime hours`, `No ${dimension} has downtime recorded`, false],
      ["fastest_growing", "Fastest growing", (c) => trendDeltaText(c.value), `No ${dimension} increased`, true],
      ["most_improved", "Most improved", (c) => trendDeltaText(c.value), `No ${dimension} decreased`, true],
    ].forEach(([key, label, detail, emptyText, overMonths]) => {
      const entry = summary[key];
      let valueText = "—";
      let detailText = "";
      if (entry) {
        valueText = entry.label;
        detailText = detail(entry);
      } else if (summary.note) {
        // Nothing to compare: the note under the cards says why.
      } else if (overMonths && !trend.has_growth_window) {
        valueText = "Insufficient Data";
        detailText = "Needs six months of data";
      } else if (emptyText) {
        detailText = emptyText;
      }
      grid.appendChild(el("div", { class: "lda-metric lda-trend-metric" }, [
        el("span", { class: "lda-metric-label", text: label }),
        el("span", { class: "lda-metric-value lda-trend-metric-value", text: valueText }),
        detailText ? el("span", { class: "lda-metric-label", text: detailText }) : null,
      ]));
    });
    const note = $("lda-trend-note");
    note.textContent = summary.note || "";
    note.hidden = !summary.note;
    revealIfChanged(grid, before);
    renderTrendChart();
    renderTrendTable();
    renderTrendRecords();
  }

  function renderTrendChart() {
    const trend = state.summary && state.summary.trend;
    const canvas = $("lda-trend-chart");
    const hint = $("lda-trend-selection");
    if (!trend || !trend.months.length) {
      hint.textContent = "No dated work orders in this population.";
      canvas.hidden = true;
      return;
    }
    canvas.hidden = false;
    hint.textContent =
      `Work orders per month in the population (${state.summary.population_label}). Click a point to list only that month's work orders.` +
      (trend.undated ? ` ${trend.undated} without a date are left out.` : "");
    drawTrendChart(canvas, trend.months, trend.monthly_counts, "Work orders", {
      selectedIndex: state.trendMonth ? trend.months.indexOf(state.trendMonth) : -1,
      onPointClick: (index) => toggleTrendMonth(trend.months[index]),
    });
  }

  function toggleTrendMonth(month) {
    state.trendMonth = state.trendMonth === month ? null : month;
    renderTrendChart();
    renderTrendTable();
    renderTrendRecords();
  }

  // Each month's total, and the five values of the Pareto's dimension with the most
  // work orders beside it, so a rise can be traced to what rose.
  function renderTrendTable() {
    const wrap = $("lda-trend-table-wrap");
    wrap.innerHTML = "";
    const trend = state.summary && state.summary.trend;
    if (!trend || !trend.months.length) return;
    const top = trend.groups.slice(0, 5);
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: "Month" }),
      el("th", { text: "Work orders" }),
      el("th", { text: "Downtime (h)" }),
      ...top.map((group) => el("th", { text: group.label })),
    ])]));
    const body = el("tbody");
    trend.months.forEach((month, index) => {
      body.appendChild(el("tr", {
        class: `lda-trend-month-row${state.trendMonth === month ? " is-active" : ""}`,
        title: "Click to list only this month's work orders below",
        onclick: () => toggleTrendMonth(month),
      }, [
        el("td", { text: monthLabel(month) }),
        el("td", { text: String(trend.monthly_counts[index]) }),
        el("td", { text: fmtFixed(trend.monthly_downtime_hours[index]) || "0" }),
        ...top.map((group) => el("td", { text: String(group.monthly_counts[index]) })),
      ]));
    });
    body.appendChild(el("tr", { class: "lda-trend-total" }, [
      el("td", { text: "Total" }),
      el("td", { text: String(trend.monthly_counts.reduce((a, b) => a + b, 0)) }),
      el("td", { text: fmtFixed(trend.monthly_downtime_hours.reduce((a, b) => a + b, 0)) || "0" }),
      ...top.map((group) => el("td", { text: String(group.total_count) })),
    ]));
    table.appendChild(body);
    wrap.appendChild(table);
  }

  // The population's work orders as one table: what the trend counts, the
  // downtime list sorts, and a record can be opened from.
  // ---- long tables, drawn a batch at a time -----------------------------------------
  // A view's tables can list hundreds of work orders, and laying every row out is
  // most of what drawing the view costs: the page froze for half a second each time
  // one came in. The first batch is drawn at once and the rest as the table is
  // scrolled toward its end, or all at once from "Show all". Anything that needs
  // every row -- a column's sort or filter -- draws the rest first, through
  // table.drawAllRows (enableTableColumnTools calls it), so it works on all of them.
  const ROW_BATCH = 150;
  function fillRows(table, tbody, items, makeRow) {
    let next = 0;
    let more = null;
    let observer = null;
    const countText = () =>
      `Showing ${next.toLocaleString()} of ${items.length.toLocaleString()} rows. The rest appear as you scroll down.`;
    function finish() {
      if (observer) observer.disconnect();
      observer = null;
      if (more) more.remove();
      more = null;
      table.drawAllRows = null;
    }
    function draw(count) {
      const end = Math.min(items.length, next + count);
      const fragment = document.createDocumentFragment();
      for (; next < end; next += 1) fragment.appendChild(makeRow(items[next]));
      tbody.insertBefore(fragment, more);
      if (next >= items.length) finish();
      else if (more) more.querySelector(".lda-more-text").textContent = countText();
    }
    draw(ROW_BATCH);
    if (next >= items.length) return;
    const columns = table.tHead ? table.tHead.rows[table.tHead.rows.length - 1].cells.length : 1;
    // One cell across the table, so the column tools never count it as a row.
    more = el("tr", { class: "lda-more-row" }, [
      el("td", { class: "lda-readonly lda-more-cell", colspan: String(columns) }, [
        el("span", { class: "lda-more-text", text: countText() }),
        " ",
        el("button", { type: "button", class: "lda-more-all", text: "Show all", onclick: () => draw(items.length) }),
      ]),
    ]);
    tbody.appendChild(more);
    table.drawAllRows = () => draw(items.length);
    if (!("IntersectionObserver" in window)) return;
    // Watched once the table is on the page, against the box it scrolls in.
    setTimeout(() => {
      if (!more) return;
      const root = table.closest(".lda-table-scroll, .lda-data-scroll");
      observer = new window.IntersectionObserver((entries) => {
        if (!more || !entries.some((entry) => entry.isIntersecting)) return;
        draw(ROW_BATCH);
        // Still in reach after a short batch: look again rather than wait for a scroll.
        if (more && observer) {
          observer.unobserve(more);
          observer.observe(more);
        }
      }, { root, rootMargin: "0px 0px 600px 0px" });
      observer.observe(more);
    }, 0);
  }

  function recordsTable(records, emptyText) {
    const columns = [
      { label: "Task ID", type: "number", get: (r) => String(r.task_id ?? ""), node: (r) => recordNumberCell(r.task_id, r.mapped_record_id) },
      { label: "Found", type: "datetime", get: (r) => r.event_date || "" },
      { label: "Work order", cls: "lda-wo-text", get: (r) => r.task_name || "" },
      { label: "Category", get: (r) => r.category || "" },
      { label: "Sub-asset", get: (r) => r.location_label || r.location || "" },
      { label: "Component", get: (r) => r.component || "" },
      { label: "Mechanism", get: (r) => r.failure_mechanism || "" },
      { label: "Mode", get: (r) => r.failure_mode || "" },
      { label: "Cause", get: (r) => r.failure_cause || "" },
      { label: "Downtime (h)", type: "number", get: (r) => (r.downtime_hours != null ? fmtFixed(r.downtime_hours) : "") },
      { label: "Request", cls: "lda-wo-text", get: (r) => r.requestor_description || "" },
      { label: "Completion notes", cls: "lda-wo-text", get: (r) => r.completion_notes || "" },
      { label: "Failure narrative", cls: "lda-wo-text", get: (r) => narrativeText(r), node: (r) => narrativeCell(r, { cls: "lda-wo-text" }) },
    ];
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, columns.map((c) => el("th", { text: c.label, class: c.cls || null })))]));
    const body = el("tbody");
    if (!records.length) {
      body.appendChild(el("tr", {}, [el("td", { class: "lda-readonly lda-empty-row", colspan: String(columns.length), text: emptyText })]));
    }
    table.appendChild(body);
    fillRows(table, body, records, (record) =>
      el("tr", {}, columns.map((c) => (c.node ? c.node(record) : el("td", { text: c.get(record), class: c.cls || null })))));
    if (records.length) enableTableColumnTools(table, { columns });
    return table;
  }

  function renderTrendRecords() {
    const wrap = $("lda-trend-wo-wrap");
    wrap.innerHTML = "";
    const trend = state.summary && state.summary.trend;
    if (!trend) return;
    const month = state.trendMonth;
    const records = month ? trend.records.filter((record) => record.month === month) : trend.records;
    $("lda-trend-wo-hint").textContent =
      (month
        ? `Work orders found in ${monthLabel(month)}. Click the month again to list every month.`
        : "The work orders the trend counts, newest first. Click a month above to list only its work orders.") +
      (trend.records_truncated ? " Only the newest 500 are listed." : "") +
      RECORD_EDIT_HINT;
    wrap.appendChild(recordsTable(records, month ? "No work orders that month." : "No work orders in this population."));
  }

  // ---- PM effectiveness ---------------------------------------------------------------
  async function loadPm() {
    const token = ++state.pmToken;
    const end = viewLoading(["lda-pm-summary", "lda-pm-chart-panel", "lda-pm-table-panel"], "lda-pm-cards", 4,
      "Pairing each PM with the next failure…", Boolean(state.pm));
    try {
      const data = await postJson(`${API}/pm-effectiveness`, populationBody());
      if (token !== state.pmToken) return;
      state.pm = data;
      renderPm();
      reveal($("lda-pm-cards"));
    } catch (err) {
      if (token === state.pmToken) showBanner(err.message, "error");
    } finally {
      end();
    }
  }

  function pmDaysClass(days) {
    const n = Number(days);
    if (!isFinite(n)) return "";
    if (n >= 180) return "lda-days-green";
    if (n >= 90) return "lda-days-yellow";
    if (n >= 30) return "lda-days-orange";
    return "lda-days-red";
  }

  function renderPm() {
    const data = state.pm;
    const grid = $("lda-pm-cards");
    grid.innerHTML = "";
    if (!data) return;
    [
      [String(data.pms_performed), "PMs performed", `${data.pm_resets} renewed an item of the population`],
      [String(data.failures_after_pm), "Failures after a PM", `of ${data.failures} failures in the population`],
      [data.average_days_to_failure != null ? `${fmt(data.average_days_to_failure)} days` : "—", "Average days to failure", "from each PM to the next failure"],
      [data.effectiveness, "PM effectiveness", "Excellent ≥180 · Good ≥90 · Fair ≥30 days"],
    ].forEach(([value, label, detail]) => {
      grid.appendChild(el("div", { class: "lda-metric" }, [
        el("span", { class: "lda-metric-value", text: value }),
        el("span", { class: "lda-metric-label", text: label }),
        el("span", { class: "lda-metric-label", text: detail }),
      ]));
    });
    const message = $("lda-pm-message");
    message.hidden = data.has_pm_history;
    message.textContent = data.has_pm_history ? "" : "No completed PM is on record on the asset or its sub-assets for the population's items.";
    const canvas = $("lda-pm-chart");
    const hint = $("lda-pm-selection");
    if (data.months.length) {
      canvas.hidden = false;
      hint.textContent = "Failures of the population found after a PM, by month: each failure once, however many PMs it followed.";
      drawTrendChart(canvas, data.months, data.monthly_counts, "Failures after a PM", {});
    } else {
      canvas.hidden = true;
      hint.textContent = "No failure of the population followed a PM.";
    }
    const wrap = $("lda-pm-table-wrap");
    wrap.innerHTML = "";
    const columns = [
      { label: "PM", type: "number", get: (r) => String(r.pm_task_id ?? ""), node: (r) => recordNumberCell(r.pm_task_id, r.pm_mapped_record_id) },
      { label: "PM completed", type: "datetime", get: (r) => r.pm_completion_date },
      { label: "PM", cls: "lda-wo-text", get: (r) => r.pm_task_name || "" },
      { label: "Kind", get: (r) => r.pm_kind + (r.renewed_items.length ? ` (${r.renewed_items.join(", ")})` : "") },
      { label: "Sub-asset", get: (r) => r.location_label || r.location },
      { label: "Next failure", type: "datetime", get: (r) => r.next_failure_date },
      { label: "Days to failure", type: "number", get: (r) => fmt(r.days_to_failure), cls: null },
      { label: "Failure", get: (r) => r.failure },
      { label: "Downtime (h)", type: "number", get: (r) => fmtFixed(r.downtime_hours) || "0" },
      { label: "Failure WO", type: "number", get: (r) => String(r.corrective_wo_number ?? ""), node: (r) => recordNumberCell(r.corrective_wo_number, r.corrective_mapped_record_id) },
    ];
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, columns.map((c) => el("th", { text: c.label })))]));
    const body = el("tbody");
    if (!data.rows.length) {
      body.appendChild(el("tr", {}, [el("td", { class: "lda-readonly lda-empty-row", colspan: String(columns.length), text: "No PM was followed by a failure of the population." })]));
    }
    table.appendChild(body);
    fillRows(table, body, data.rows, (row) => el("tr", {}, columns.map((c) => {
      if (c.node) return c.node(row);
      return el("td", { text: c.get(row), class: c.label === "Days to failure" ? pmDaysClass(row.days_to_failure) : c.cls || null });
    })));
    if (data.rows.length) enableTableColumnTools(table, { columns });
    wrap.appendChild(table);
    $("lda-pm-table-hint").textContent =
      "Each completed PM on the asset or one of its sub-assets -- a reset that renewed one of the population's items, or " +
      "planned work there that renewed nothing -- paired with the next failure of the population found on that same " +
      "sub-asset." + RECORD_EDIT_HINT;
  }

  // ---- repeat fixes ----------------------------------------------------------------
  function repeatWindowHours() {
    const box = $("lda-repeat-window");
    const value = Number(box.value);
    return box.value !== "" && isFinite(value) && value >= 0 && value <= 720 ? value : null;
  }

  function onRepeatWindowChange() {
    const hours = repeatWindowHours();
    if (hours == null) {
      $("lda-repeat-window").value = String(state.repeatWindow);
      showBanner("The repeat window is a number of scheduled hours from 0 to 720.", "error");
      return;
    }
    state.repeatWindow = hours;
    if (state.asset) loadRepeat();
  }

  async function loadRepeat() {
    const token = ++state.repeatToken;
    const end = viewLoading(["lda-repeat-summary", "lda-repeat-rate-panel", "lda-repeat-pairs-panel"], "lda-repeat-cards", 4,
      "Finding failures that came back…", Boolean(state.repeat));
    try {
      const data = await postJson(`${API}/repeat-fixes`, populationBody({ window_hours: state.repeatWindow }));
      if (token !== state.repeatToken) return;
      state.repeat = data;
      if (state.repeatFilter && !data.groups.some((group) => group.key === state.repeatFilter)) state.repeatFilter = null;
      renderRepeat();
      reveal($("lda-repeat-cards"));
    } catch (err) {
      if (token === state.repeatToken) showBanner(err.message, "error");
    } finally {
      end();
    }
  }

  const fmtRate = (rate) => (rate == null ? "—" : `${(100 * Number(rate)).toFixed(rate < 0.1 ? 1 : 0)}%`);

  function renderRepeat() {
    const data = state.repeat;
    const grid = $("lda-repeat-cards");
    grid.innerHTML = "";
    if (!data) return;
    const highest = data.highest_rate;
    const most = data.most_repeats;
    [
      [String(data.failures), "Failures with a component", data.uncoded_failures ? `${data.uncoded_failures} more have none and are left out` : "item by item"],
      [String(data.repeats), "Repeats", `of ${data.intervals} gaps between failures`],
      [fmtRate(data.repeat_rate), "Repeat rate", `within ${fmt(data.window_hours)} scheduled hours`],
      [highest ? highest.label : "—", "Highest rate", highest ? `${fmtRate(highest.repeat_rate)} over ${highest.intervals} gaps` : `needs ${data.min_intervals_for_rate}+ gaps and a repeat`],
      [most ? most.label : "—", "Most repeats", most ? `${most.repeats} repeats` : "no repeats"],
    ].forEach(([value, label, detail]) => {
      grid.appendChild(el("div", { class: "lda-metric" }, [
        el("span", { class: "lda-metric-value lda-trend-metric-value", text: value }),
        el("span", { class: "lda-metric-label", text: label }),
        el("span", { class: "lda-metric-label", text: detail }),
      ]));
    });
    const notes = [];
    if (data.undated_failures) notes.push(`${data.undated_failures} failures have no date and are left out.`);
    if (data.future_failures) notes.push(`${data.future_failures} are dated in the future and are left out.`);
    if (data.possible_duplicates) notes.push(`${data.possible_duplicates} repeats look like duplicate work orders; check them below.`);
    const message = $("lda-repeat-message");
    message.hidden = !notes.length;
    message.textContent = notes.join(" ");
    $("lda-repeat-basis").textContent =
      `Counted on ${data.schedules.length ? data.schedules.join(", ") : "each sub-asset's Weibull schedule"}, days split at midnight ${data.time_zone}.` +
      (data.time_zone_warning ? ` ${data.time_zone_warning}` : "");

    const rates = $("lda-repeat-rate-wrap");
    rates.innerHTML = "";
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, ["Item", "Schedule", "Failures", "Gaps", "Repeats", "Rate"].map((h) => el("th", { text: h })))]));
    const body = el("tbody");
    if (!data.groups.length) body.appendChild(el("tr", {}, [el("td", { class: "lda-readonly lda-empty-row", colspan: "6", text: "No failures with a component in this population." })]));
    data.groups.forEach((group) => {
      body.appendChild(el("tr", {
        class: `lda-trend-month-row${state.repeatFilter === group.key ? " is-active" : ""}`,
        title: "Click to list only this item's repeats",
        onclick: () => {
          state.repeatFilter = state.repeatFilter === group.key ? null : group.key;
          renderRepeat();
        },
      }, [
        el("td", { text: group.label }),
        el("td", { text: group.schedule_name }),
        el("td", { text: String(group.failures) }),
        el("td", { text: String(group.intervals) }),
        el("td", { text: String(group.repeats) }),
        el("td", { text: group.intervals >= data.min_intervals_for_rate ? fmtRate(group.repeat_rate) : `${fmtRate(group.repeat_rate)} (few gaps)` }),
      ]));
    });
    table.appendChild(body);
    rates.appendChild(table);

    const filter = state.repeatFilter;
    const filterGroup = data.groups.find((group) => group.key === filter);
    setHidden($("lda-repeat-filter"), !filterGroup);
    if (filterGroup) $("lda-repeat-filter-text").textContent = `Showing only ${filterGroup.label}.`;
    const pairs = data.pairs.filter((pair) => !filter || pair.group_key === filter);
    const wrap = $("lda-repeat-pairs-wrap");
    wrap.innerHTML = "";
    const columns = [
      { label: "Item", get: (p) => p.label },
      { label: "Fixed", type: "number", get: (p) => String(p.prior_task_id ?? ""), node: (p) => recordNumberCell(p.prior_task_id, p.prior_mapped_record_id) },
      { label: "Fixed on", type: "datetime", get: (p) => formatRecordDate(p.prior_fixed) },
      { label: "Mechanism", get: (p) => p.prior_mechanism },
      { label: "Came back", type: "number", get: (p) => String(p.repeat_task_id ?? ""), node: (p) => recordNumberCell(p.repeat_task_id, p.repeat_mapped_record_id) },
      { label: "Found on", type: "datetime", get: (p) => formatRecordDate(p.repeat_found) },
      { label: "Mechanism", get: (p) => p.repeat_mechanism },
      { label: "Scheduled h", type: "number", get: (p) => fmtFixed(p.scheduled_hours) || "0" },
      { label: "Raw h", type: "number", get: (p) => fmtFixed(p.raw_hours) || "0" },
      { label: "Check", cls: "lda-wo-text", get: (p) => p.duplicate_check || "" },
    ];
    const pairTable = el("table", { class: "lda-table" });
    pairTable.appendChild(el("thead", {}, [el("tr", {}, columns.map((c) => el("th", { text: c.label })))]));
    const pairBody = el("tbody");
    if (!pairs.length) pairBody.appendChild(el("tr", {}, [el("td", { class: "lda-readonly lda-empty-row", colspan: String(columns.length), text: "No failure came back within the window." })]));
    pairTable.appendChild(pairBody);
    fillRows(pairTable, pairBody, pairs, (pair) =>
      el("tr", {}, columns.map((c) => (c.node ? c.node(pair) : el("td", { text: c.get(pair), class: c.cls || null })))));
    if (pairs.length) enableTableColumnTools(pairTable, { columns });
    wrap.appendChild(pairTable);
    $("lda-repeat-pairs-hint").textContent = "Each failure that came back within the window, with the fix before it." + RECORD_EDIT_HINT;
  }

  // ---- downtime drivers ------------------------------------------------------------
  async function loadDowntime() {
    const token = ++state.downtimeToken;
    const end = viewLoading(
      ["lda-downtime-summary", "lda-downtime-trend-panel", "lda-downtime-dist-panel", "lda-downtime-asset-panel", "lda-downtime-events-panel"],
      "lda-downtime-cards", 5, "Adding up the downtime…", Boolean(state.downtime),
    );
    try {
      const data = await postJson(`${API}/downtime-drivers`, populationBody());
      if (token !== state.downtimeToken) return;
      state.downtime = data;
      renderDowntime();
      reveal($("lda-downtime-cards"));
    } catch (err) {
      if (token === state.downtimeToken) showBanner(err.message, "error");
    } finally {
      end();
    }
  }

  function renderDowntime() {
    const data = state.downtime;
    const grid = $("lda-downtime-cards");
    grid.innerHTML = "";
    if (!data) return;
    const s = data.summary;
    [
      [`${fmt(s.total_downtime_hours)} h`, "Total downtime"],
      [String(s.work_order_count), "Work orders"],
      [`${fmt(s.average_downtime_hours)} h`, "Average per work order"],
      [`${fmt(s.median_downtime_hours)} h`, "Median"],
      [`${fmt(s.max_downtime_hours)} h`, "Longest stop"],
    ].forEach(([value, label]) => {
      grid.appendChild(el("div", { class: "lda-metric" }, [
        el("span", { class: "lda-metric-value", text: value }),
        el("span", { class: "lda-metric-label", text: label }),
      ]));
    });
    const message = $("lda-downtime-message");
    message.hidden = data.has_records;
    message.textContent = data.has_records ? "" : "No work orders in this population.";
    renderDowntimeCharts();
    const wrap = $("lda-downtime-events-wrap");
    wrap.innerHTML = "";
    wrap.appendChild(recordsTable(data.top_events, "No work orders in this population."));
    $("lda-downtime-events-hint").textContent = "The ten work orders with the most downtime." + RECORD_EDIT_HINT;
  }

  function renderDowntimeCharts() {
    const data = state.downtime;
    if (!data) return;
    const trend = $("lda-downtime-trend-chart");
    trend.hidden = !data.months.length;
    if (data.months.length) drawDowntimeLineChart(trend, data.months, data.monthly_downtime_hours, "Downtime (hours)");
    drawBarChart($("lda-downtime-dist-chart"), data.distribution.map((bin) => bin.label), data.distribution.map((bin) => bin.count), {
      yLabel: "Work orders",
      xLabel: "Downtime per work order",
    });
    const locations = data.by_location.slice(0, 15);
    drawBarChart($("lda-downtime-asset-chart"), locations.map((row) => row.label), locations.map((row) => row.downtime_hours), {
      yLabel: "Downtime (hours)",
      tickFormat: compactHours,
      rotateLabels: locations.length > 6,
    });
  }

  // ---- the Weibull fit ----------------------------------------------------------------
  // What the action bar says under the button: what pressing it will do, given
  // what the population has.
  function renderPerformHint() {
    const hint = $("lda-perform-hint");
    if (!hint || !state.summary) return;
    const readiness = state.summary.readiness || {};
    const pieces = [
      CAN_EDIT
        ? "Perform Analysis builds the population's lives item by item, fits them, and saves the run with its population definition."
        : "Preview the fit builds and fits the population's lives without saving anything; an editor saves a run.",
      CAN_EDIT
        ? "Clicking a Pareto bar fits and saves that one sub-asset, component and mechanism instead."
        : "Clicking a Pareto bar previews the fit of that one sub-asset, component and mechanism instead.",
    ];
    if (readiness.uncoded) pieces.push(`${readiness.uncoded} failures or PM resets have no component yet and are left out until dispositioned.`);
    if (readiness.not_reviewed) pieces.push(`${readiness.not_reviewed} work orders in reach are not reviewed yet.`);
    hint.textContent = pieces.join(" ");
  }

  // The Weibull's workspace before anything is fitted. Choosing an asset, or
  // changing the population, never fits or opens a fit by itself: a bar of the
  // Pareto or Perform Analysis does, and a saved fit opens from the rankings.
  // Every population change has already emptied the workspace and dropped any
  // fit on its way (resetAnalyses), so the prompt only fills an empty one: a fit
  // run, or a saved fit opened, while the summary was being read stays.
  function showFitPrompt() {
    const workspace = $("lda-workspace");
    if (state.result || workspace.childElementCount) return;
    workspace.appendChild(el("section", { class: "glass-card lda-card lda-empty-result", id: "lda-no-fit" }, [
      el("h2", { text: "No Weibull fitted yet" }),
      el("p", {
        class: "lda-hint",
        text: CAN_EDIT
          ? "Click a Pareto bar to fit and save one sub-asset, component and mechanism, or Perform Analysis to fit the " +
            "population ticked. A Weibull life is measured on one kind of item, so pick one component at least. Saved " +
            "fits open from the rankings above."
          : "Click a Pareto bar, or Preview the fit, to see one. Saved fits open from the rankings above.",
      }),
    ]));
  }

  // A saved fit, opened from the rankings: that run when its id is known, else
  // the latest run saved for exactly the population on screen. Never fits anything.
  async function loadSavedFit(analysisId) {
    const asset = state.asset;
    const token = ++state.analysisToken;
    showFitSkeleton("Opening the saved Weibull fit…");
    $("lda-fit-loading").dataset.token = String(token);
    // Something newer took over: its own placeholder or result stays, but this
    // one's placeholder is not left spinning in its place.
    const superseded = () => {
      const loading = $("lda-fit-loading");
      if (loading && loading.dataset.token === String(token)) {
        clearWorkspace();
        if (isWeibull()) showFitPrompt();
      }
    };
    const end = busy(null, "Opening the saved fit…");
    try {
      const data = analysisId != null
        ? await getJson(`${API}/saved-analysis/${encodeURIComponent(analysisId)}`)
        : await postJson(`${API}/saved-analysis`, populationBody());
      if (token !== state.analysisToken || asset !== state.asset || !isWeibull()) {
        superseded();
        return;
      }
      clearWorkspace();
      if (data.result) renderAnalysisResult(data.result, { scroll: true });
      else showFitPrompt();
    } catch (err) {
      if (token === state.analysisToken) {
        clearWorkspace();
        showFitPrompt();
        if (!err.toastShown) showBanner(err.message, "error");
      } else {
        superseded();
      }
    } finally {
      end();
    }
  }

  // Perform Analysis saves the run for an editor; anybody else previews it.
  function performAnalysis() {
    return runFit(CAN_EDIT);
  }

  // A bar of the Weibull's Pareto is one sub-asset's one component failing by one
  // mechanism. Clicking it runs exactly that, as Perform Analysis would (saved for
  // an editor, a preview for anybody else), and leaves the population on the card
  // and the Pareto as they are.
  function fitBar(row) {
    return runFit(CAN_EDIT, row.selection);
  }

  // ``narrowedTo`` puts a bar's sub-asset, component and mechanism in place of the
  // card's; the dates and the induced switch are the card's either way.
  async function runFit(save, narrowedTo) {
    if (!state.asset) {
      showBanner("Select an Asset Number first.", "error");
      return undefined;
    }
    if (!isWeibull()) return undefined;
    const token = ++state.analysisToken;
    beginLoading(save ? "Fitting and saving the population's lives…" : "Fitting the population's lives…");
    try {
      const body = populationBody(weibullDateWindow());
      if (narrowedTo) body.selection = { ...body.selection, ...narrowedTo };
      const data = await postJson(`${API}/${save ? "weibull/save" : "weibull"}`, body);
      if (token !== state.analysisToken) return undefined;
      renderAnalysisResult(data.result, { scroll: true });
      if (save) loadSummary();
    } catch (err) {
      if (token === state.analysisToken && !err.toastShown) showBanner(err.message, "error");
    } finally {
      endLoading();
    }
    return undefined;
  }

  function clearWorkspace() {
    const workspace = $("lda-workspace");
    if (workspace) workspace.innerHTML = "";
    state.analysisRedraw = null;
    state.result = null;
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
        `${result.censored_count} suspended (${result.suspension_count || 0} renewed by other work, ` +
        `${result.current_life_censored_count || 0} still in service at the cutoff).`,
      result.saved
        ? `Saved ${result.run_at ? `${result.run_at} UTC` : ""}${result.run_by ? ` by ${result.run_by}` : ""}, method ${result.method_version}.`
        : "A preview: not saved, so it can't be adjusted or reported.",
    ];
    return lines;
  }

  // Notices a result has to carry: saved under an earlier method or on schedules
  // since changed, too few failures to fit, a crooked probability plot, lives that
  // look like duplicates.
  function resultNotices(result) {
    const notices = [];
    const rerun = CAN_EDIT ? "Run it again to apply them; " : "An editor has to run it again; ";
    if (result.method_current === false) {
      notices.push(`Saved by an earlier version of the method (${result.method_version || "unrecorded"}). ${rerun}it can't be reported until then.`);
    }
    if (result.method_current !== false && result.schedule_current === false) {
      const changed = (result.schedules_changed || []).map((row) => `${row.location_label || row.location} from ${row.counted_on} to ${row.now_on}`).join("; ");
      notices.push(`Counted on schedules since changed (${changed}), so its life hours have changed. ${rerun}it can't be reported until then.`);
    }
    if (result.method_current !== false && result.schedule_current !== false && result.time_zone_current === false) {
      notices.push(
        `Its days were split at midnight ${(result.life_basis || {}).time_zone || "UTC"}, but the plant's time zone is now ` +
          `${result.current_time_zone}. ${rerun}it can't be reported until then.`
      );
    }
    if (!result.fitted) notices.push(result.fit_reason || "No fit could be made of these lives.");
    if (result.probability_plot_review) {
      notices.push(
        `The probability plot is less straight than 90% of genuine Weibull samples with ${result.failure_count} failures ` +
          `(R² ${Number(result.probability_plot_r_squared).toFixed(3)}, review threshold ` +
          `${Number(result.probability_plot_r_squared_threshold).toFixed(3)}). Review the population before acting on beta: ` +
          "mixed components or mechanisms, or a duplicate work order, are the usual causes."
      );
    }
    const flagged = (result.observations || []).filter((obs) => obs.data_quality_assumption_flag).length;
    if (flagged) {
      notices.push(
        `${flagged} ${flagged === 1 ? "life ends" : "lives end"} within an hour of the event before, which is what two work ` +
          "orders for one breakdown look like. Check the flagged rows in the data table."
      );
    }
    return notices;
  }

  function warningsList(result) {
    const warnings = result.warnings || [];
    if (!warnings.length) return null;
    return el("div", { class: "std-warnings", id: "lda-result-warnings" }, warnings.map((warning) =>
      el("div", { class: `std-warning${warning.level === "info" ? " is-info" : ""}`, role: "note" }, [
        el("strong", { text: warning.title }),
        warning.message,
      ])
    ));
  }

  // The pooled streams: every item whose lives the fit rests on.
  function streamsTable(result) {
    const streams = result.streams || [];
    const table = el("table", { class: "lda-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, ["Sub-asset", "Component", "Failures", "Suspensions", "Schedule", ""].map((h) => el("th", { text: h })))]));
    const body = el("tbody");
    if (!streams.length) body.appendChild(el("tr", {}, [el("td", { colspan: "6", class: "lda-readonly lda-empty-row", text: "No item in this population has a renewal on record." })]));
    streams.forEach((stream) => {
      body.appendChild(el("tr", {}, [
        el("td", { text: stream.location_label || stream.location }),
        el("td", { text: stream.component_name && stream.component_name !== stream.component ? `${stream.component} · ${stream.component_name}` : stream.component }),
        el("td", { class: "std-failure", text: String(stream.failures) }),
        el("td", { class: "std-suspension", text: String(stream.suspensions) }),
        el("td", { text: stream.schedule_name || "" }),
        el("td", { text: stream.recurrent ? "Recurrent: a failure did not renew the item (Crow-AMSAA)" : "" }),
      ]));
    });
    table.appendChild(body);
    return el("div", { class: "lda-table-scroll" }, [table]);
  }

  // When a failure of unknown mechanism could be the selected one, both readings
  // are fitted, and both shown.
  function sensitivityPanel(result) {
    const fit = result.sensitivity_fit;
    if (!fit) return null;
    const main = result.fit || {};
    const describe = (f) => (f.fitted ? `beta ${fmt(f.beta)}, eta ${fmt(f.eta)} h` : f.reason || "not fitted");
    return el("div", { class: "lda-panel", id: "lda-sensitivity-panel" }, [
      el("h3", { text: "Unknown mechanisms: both readings" }),
      el("div", { class: "std-fits" }, [
        el("div", { class: "std-fit" }, [
          el("h3", { text: main.label || "Unknown mechanisms as suspensions" }),
          el("p", { class: "std-params", text: describe(main) }),
          el("p", { class: "lda-hint", text: `${main.failure_count} failures, ${main.suspension_count} suspensions. The fit shown above.` }),
        ]),
        el("div", { class: "std-fit" }, [
          el("h3", { text: fit.label }),
          el("p", { class: "std-params", text: describe(fit) }),
          el("p", { class: "lda-hint", text: `${fit.failure_count} failures, ${fit.suspension_count} suspensions.` }),
        ]),
      ]),
      el("p", { class: "lda-hint", text: "If the two disagree, the unknowns matter: dispositioning them is worth more than either fit." }),
    ]);
  }

  function renderAnalysisResult(result, options) {
    const scroll = !options || options.scroll !== false;
    clearWorkspace();
    state.result = result;
    let chartApi = null;
    const dataTable = buildWeibullDataTable(result, (obs) => {
      if (chartApi) chartApi.focus(obs);
    });
    const eventTable = buildEventProcessingTable(result);
    const notices = resultNotices(result);
    const header = [
      el("h2", { text: "Weibull Analysis Results" }),
      el("p", { class: "std-pop-string", id: "lda-result-pop", text: result.population_definition }),
    ];
    if (!result.fitted) {
      const card = el("section", { class: "glass-card lda-card fade-in-up", id: "lda-result-card" }, [
        ...header,
        el("div", { class: "lda-result-context", id: "lda-result-context" },
          resultContextLines(result).map((line) => el("span", { class: "lda-hint", text: line }))),
        ...notices.map((notice) => el("p", { class: "lda-banner is-warning lda-result-notice", role: "note", text: notice })),
        warningsList(result),
        panel("Items pooled", streamsTable(result), "Each renewal stream: one physical item on one sub-asset.", "lda-streams-panel"),
        panel("Lives", dataTable.node, "The lives there are, listed so they can be checked.", "lda-weibull-data-panel"),
        eventTable.node,
      ]);
      $("lda-workspace").appendChild(card);
      if (scroll) scrollBelowSticky(card);
      return;
    }
    const agePanel = buildTargetAgePanel(result);
    const adjustment = result.adjustment || null;
    const betaInput = el("input", { class: "lda-input", type: "number", step: "0.2", min: "0.01", value: numericInputValue(adjustment ? adjustment.adjusted_beta : result.beta_mle, 6) });
    const etaInput = el("input", { class: "lda-input", type: "number", step: "100", min: "0.01", value: numericInputValue(adjustment ? adjustment.adjusted_eta : result.eta_mle, 6) });
    const reasonInput = el("input", { class: "lda-input", placeholder: "Why, from the data points…", value: adjustment ? adjustment.adjustment_reason || "" : "" });
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
    state.analysisRedraw = applyParameters;
    const canAdjust = CAN_EDIT && result.saved && result.analysis_id != null;
    const adjustRow = canAdjust
      ? el("div", { class: "lda-adjust-row", id: "lda-adjust-row" }, [
          field("Adjusted beta (±0.2)", betaInput),
          field("Adjusted eta (±100 h)", etaInput),
          field("Adjustment reason", reasonInput),
          el("div", { class: "lda-field" }, [el("label", { html: "&nbsp;" }), el("button", {
            class: "btn-primary",
            text: "Save Adjusted Parameters",
            onclick: () => saveAdjustedParameters(result, Number(betaInput.value), Number(etaInput.value), reasonInput.value),
          })]),
        ])
      : null;
    const metrics = lifeMetricsText(result);
    const card = el("section", { class: "glass-card lda-card fade-in-up", id: "lda-result-card" }, [
      ...header,
      el("div", { class: "lda-result-headline" }, [
        el("strong", { text: result.analysis_label || "Selected population" }),
        el("span", { class: "lda-result-params", id: "lda-result-params", text: `MLE beta: ${fmt(result.beta_mle)}    MLE eta: ${fmt(result.eta_mle)} hours` }),
        el("span", { class: "lda-hint", id: "lda-result-ci", text: confidenceIntervalText(result) }),
        metrics ? el("span", { class: "lda-hint", id: "lda-result-life", text: metrics }) : null,
        el("span", { class: "lda-hint", id: "lda-result-fit-check", text: fitCheckText(result) }),
        adjustment
          ? el("span", { class: "lda-hint", text: `Adjusted by ${adjustment.adjusted_by || "an editor"} to beta ${fmt(adjustment.adjusted_beta)}, eta ${fmt(adjustment.adjusted_eta)} h: ${adjustment.adjustment_reason || ""}` })
          : null,
      ]),
      el("div", { class: "lda-result-context", id: "lda-result-context" },
        resultContextLines(result).map((line) => el("span", { class: "lda-hint", text: line }))),
      ...notices.map((notice) => el("p", { class: "lda-banner is-warning lda-result-notice", role: "note", text: notice })),
      warningsList(result),
      adjustRow,
      legend(),
      charts,
      el("p", {
        class: "lda-hint",
        text:
          "Green lines show the MLE fit; yellow lines the approximate 95% confidence-interval fits. Red vertical lines mark " +
          "the items still in service: each one's life from its last renewal to the cutoff. Hover a point for its work " +
          "order; click it to jump to its row below.",
      }),
      agePanel.node,
      sensitivityPanel(result),
      panel("Results Interpretation Summary", buildInterpretationTable(result),
        "Recommendations are based on beta, eta, MTTF, the approximate 95% confidence intervals and the probability plot R².",
        "lda-interpretation-panel"),
      panel("Items pooled", streamsTable(result),
        "Each renewal stream is one physical item on one sub-asset; its lives run from one renewal to the next event in that stream.",
        "lda-streams-panel"),
      panel("Weibull Data Used for Graphs", dataTable.node,
        "Rows are the lives the fit rests on. Raw elapsed hours are calendar hours; weekend and non-run hours are taken out " +
          "to give the life hours. Check flags a life that ends within an hour of the event before, a likely duplicate. " +
          "Click a row to ring it on the graph that plots it." + RECORD_EDIT_HINT + " Use the ▾ menu in a column header to sort or filter.",
        "lda-weibull-data-panel"),
      eventTable.node,
      CAN_EDIT ? buildReportBar(result, charts, chartApi, betaInput, etaInput, agePanel) : null,
    ]);
    $("lda-workspace").appendChild(card);
    if (adjustment) applyParameters();
    if (scroll) scrollBelowSticky(card);
  }

  // Every event the pooled streams saw, and what it did there: starts a life,
  // ends one as a failure or a suspension, or nothing, and why. Collapsed by
  // default; it is the audit trail behind the lives.
  const ROLE_LABELS = {
    STARTS_LIFE: "Starts a life",
    FAILURE: "Failure",
    SUSPENSION: "Suspension",
    NO_EFFECT: "No effect",
    EXCLUDED: "Left out",
  };
  function buildEventProcessingTable(result) {
    const events = result.events || [];
    const excluded = events.filter((event) => event.role === "EXCLUDED").length;
    const columns = [
      { label: "#", type: "number", get: (e) => String(e.sequence ?? "") },
      { label: "Item", get: (e) => e.stream || "" },
      { label: "Task ID", type: "number", get: (e) => String(e.task_id ?? ""), node: (e) => recordNumberCell(e.task_id, e.mapped_record_id) },
      { label: "Work Title", cls: "lda-data-text", get: (e) => e.task_name || "" },
      { label: "Category", get: (e) => e.category || "" },
      { label: "When", type: "datetime", get: (e) => formatRecordDate(e.when) },
      { label: "Role", get: (e) => ROLE_LABELS[e.role] || e.role || "" },
      { label: "Note", cls: "lda-data-text", get: (e) => e.note || "" },
    ];
    const table = el("table", { class: "lda-data" });
    table.appendChild(el("thead", {}, [el("tr", {}, columns.map((c) => el("th", { text: c.label, class: c.cls || null })))]));
    const tbody = el("tbody");
    events.forEach((event) => {
      tbody.appendChild(el("tr", { class: event.role === "EXCLUDED" ? "is-excluded" : null }, columns.map((c) =>
        c.node ? c.node(event) : el("td", { text: c.get(event), class: c.cls || null })
      )));
    });
    table.appendChild(tbody);
    if (events.length) enableTableColumnTools(table, { columns });
    const details = el("details", { class: "lda-panel lda-events", id: "lda-event-table" }, [
      el("summary", { text: `Event processing table: ${events.length} ${events.length === 1 ? "event" : "events"}` + (excluded ? `, ${excluded} left out` : "") }),
      el("div", { class: "lda-data-scroll" }, [table]),
      el("p", {
        class: "lda-hint",
        text:
          "Every failure and PM reset that touched a pooled item, item by item in date order, with what it did there. The " +
          "first renewal of an item only starts its clock; an event with no date, or outside the window, is listed but left out.",
      }),
    ]);
    return { node: details };
  }

  // Generating a report reserves a numbered REL report for a saved run.
  function buildReportBar(result, charts, chartApi, betaInput, etaInput, agePanel) {
    let blocked = "";
    if (!result.saved || result.analysis_id == null) blocked = "Only a saved run can be reported: Perform Analysis saves one.";
    else if (result.method_current === false) blocked = "Run the analysis again to report it: it was saved by an earlier version of the method.";
    else if (result.schedule_current === false) blocked = "Run the analysis again to report it: a sub-asset has moved to another schedule since.";
    else if (result.time_zone_current === false) blocked = "Run the analysis again to report it: the plant's time zone has changed since.";
    const button = el("button", { class: "btn-primary", type: "button", text: "Generate Weibull Report", disabled: Boolean(blocked) });
    button.addEventListener("click", () => generateWeibullReport(result, charts, button, chartApi, betaInput, etaInput, agePanel));
    return el("div", { class: "lda-report-bar", id: "lda-report-bar" }, [
      button,
      el("p", {
        class: "lda-hint",
        text:
          blocked ||
          "Creates a formal Word report (REL-WBL-RPT-<asset>-00x.docx) of this saved run: the population definition, the items " +
            "pooled, the life basis and window, the fitted values with B10, B50 and the reliability at the age entered above, " +
            "the validity warnings, the graphs, the interpretation summary, the limitations, and every life it rests on.",
      }),
    ]);
  }

  async function generateWeibullReport(result, chartsContainer, button, chartApi, betaInput, etaInput, agePanel) {
    // The report's tables are the MLE fit, so its graphs must show that fit, not an
    // adjustment being tried on screen: redraw at the MLE, capture, then restore.
    if (chartApi && result.beta_mle > 0 && result.eta_mle > 0) chartApi.update(Number(result.beta_mle), Number(result.eta_mle));
    const charts = [];
    chartsContainer.querySelectorAll(".lda-chart-card").forEach((cardEl) => {
      const canvas = cardEl.querySelector("canvas");
      const heading = cardEl.querySelector("h4");
      if (!canvas) return;
      try {
        charts.push({ title: heading ? heading.textContent : "", image: canvas.toDataURL("image/png") });
      } catch (err) {
        /* a locally drawn canvas is never tainted */
      }
    });
    if (chartApi) {
      const beta = Number(betaInput.value);
      const eta = Number(etaInput.value);
      if (beta > 0 && eta > 0) chartApi.update(beta, eta);
    }
    button.disabled = true;
    beginLoading("Generating Weibull report…");
    try {
      const filename = await postDownload(
        `${API}/weibull-report`,
        { analysis_id: result.analysis_id, charts, target_age_hours: agePanel ? agePanel.value() : null },
        "weibull-report.docx"
      );
      showBanner(`Generated ${filename}.`, "success");
    } catch (err) {
      showBanner(err.message, "error");
    } finally {
      endLoading();
      button.disabled = false;
    }
  }

  async function saveAdjustedParameters(result, beta, eta, reason) {
    if (!(beta > 0) || !(eta > 0)) {
      showBanner("Adjusted beta and eta must both be positive numbers.", "error");
      return;
    }
    beginLoading("Saving adjusted Weibull parameters…");
    try {
      const data = await postJson(`${API}/parameter-adjustment`, { analysis_id: result.analysis_id, beta, eta, reason });
      result.adjustment = data.adjustment;
      showBanner("The adjusted beta and eta were saved beside the MLE, which is unchanged.", "success");
    } catch (err) {
      if (!err.toastShown) showBanner(err.message, "error");
    } finally {
      endLoading();
    }
  }

  function buildWeibullDataTable(result, onRowActivate) {
    const columns = [
      { label: "#", type: "number", get: (obs) => String(obs.ordered_index ?? "") },
      { label: "Item", get: (obs) => obs.stream || "" },
      { label: "Started by", type: "number", get: (obs) => (obs.start_task_id != null ? String(obs.start_task_id) : ""), node: (obs) => recordNumberCell(obs.start_task_id, obs.start_mapped_record_id) },
      { label: "Ended by", type: "number", get: (obs) => (obs.source_task_id != null ? String(obs.source_task_id) : ""), node: (obs) => recordNumberCell(obs.source_task_id, obs.source_mapped_record_id, "cutoff") },
      { label: "Work Title", cls: "lda-data-text", get: (obs) => obs.source_work_title || "" },
      { label: "Downtime (h)", type: "number", get: (obs) => (obs.source_downtime_hours != null ? fmtFixed(obs.source_downtime_hours) : "") },
      { label: "Type", get: (obs) => OBSERVATION_TYPE_LABELS[obs.observation_type] || obs.observation_type || "" },
      { label: "Life Hours", type: "number", get: (obs) => fmtFixed(obs.life_hours_for_weibull) },
      { label: "Raw Elapsed (h)", type: "number", get: (obs) => (obs.life_hours_raw_elapsed != null ? fmtFixed(obs.life_hours_raw_elapsed) : "") },
      { label: "Excl. Weekend (h)", type: "number", get: (obs) => (obs.excluded_weekend_hours != null ? fmtFixed(obs.excluded_weekend_hours) : "") },
      { label: "Excl. Non-run (h)", type: "number", get: (obs) => (obs.excluded_schedule_non_run_hours != null ? fmtFixed(obs.excluded_schedule_non_run_hours) : "") },
      { label: "Failure", type: "boolean", get: (obs) => (Number(obs.failure_indicator) ? "Yes" : "No") },
      { label: "Start", type: "datetime", get: (obs) => formatRecordDate(obs.start_datetime) },
      { label: "End/Cutoff", type: "datetime", get: (obs) => formatRecordDate(obs.end_datetime || obs.analysis_cutoff_datetime) },
      { label: "Request Description", cls: "lda-data-text", get: (obs) => obs.source_request_description || "" },
      { label: "Completion Notes", cls: "lda-data-text", get: (obs) => obs.source_completion_notes || "" },
      { label: "Failure Narrative", cls: "lda-data-text", get: (obs) => narrativeText(obs, "source_"), node: (obs) => narrativeCell(obs, { prefix: "source_", cls: "lda-data-text" }) },
      { label: "Note", cls: "lda-data-text", get: (obs) => obs.weibull_life_note || "" },
      { label: "Check", cls: "lda-data-text lda-check", get: (obs) => obs.data_quality_assumption_flag || "" },
    ];
    const table = el("table", { class: "lda-data" });
    table.appendChild(el("thead", {}, [el("tr", {}, columns.map((c) => el("th", { text: c.label, class: c.cls || null })))]));
    const tbody = el("tbody");
    const rowByObs = new Map();
    (result.observations || []).forEach((obs) => {
      const tr = el("tr", {}, columns.map((c) => (c.node ? c.node(obs) : el("td", { text: c.get(obs), class: c.cls || null }))));
      if (typeof onRowActivate === "function" && result.fitted) {
        tr.classList.add("is-clickable");
        tr.title = "Show this life on the graphs above";
        tr.addEventListener("click", (event) => {
          if (event.target.closest("button")) return;
          markRow(tr);
          onRowActivate(obs);
        });
      }
      rowByObs.set(Number(obs.weibull_observation_id), tr);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    const tableTools = (result.observations || []).length ? enableTableColumnTools(table, { columns }) : null;
    const node = el("div", { class: "lda-data-scroll" }, [table]);
    function markRow(tr) {
      tbody.querySelectorAll("tr.is-highlight").forEach((r) => r.classList.remove("is-highlight"));
      tr.classList.add("is-highlight");
    }
    function highlight(observationId) {
      const tr = rowByObs.get(Number(observationId));
      if (!tr) return;
      if (tr.style.display === "none" && tableTools) tableTools.clearFilters();
      markRow(tr);
      tr.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    return { node, highlight };
  }

  // ---- a work order's disposition, from any table -----------------------------------
  // The disposition form over the page, for the record a table lists. Saving it
  // reloads everything on the page, since a disposition can move a work order in
  // or out of every view.
  async function openRecordEditor(mappedRecordId) {
    let data;
    beginLoading("Opening the work order…");
    try {
      data = await getJson(`${API}/dispositions/record?mapped_record_id=${encodeURIComponent(mappedRecordId)}`);
    } catch (err) {
      if (!err.toastShown) showBanner(err.message, "error");
      return;
    } finally {
      endLoading();
    }
    const backdrop = el("div", { class: "lda-modal-backdrop" });
    let form = null;
    function close(force) {
      if (!force && form && form.isDirty() && !window.confirm("Discard the unsaved changes to this disposition?")) return;
      document.removeEventListener("keydown", onKey);
      backdrop.remove();
    }
    function onKey(event) {
      if (event.key === "Escape") close(false);
    }
    form = window.GremlinDispositionForm.create({
      row: data.record,
      options: data.options,
      history: data.history,
      saveLabel: "Save and re-run the analysis",
      onCancel: () => close(false),
      onSave: async (payload) => {
        await postJson(`${API}/dispositions/save`, { dispositions: [payload] });
        close(true);
        showToast(`Task ${data.record.task_id} saved.`, "success");
        // Read before resetAnalyses(), which clears the fit on screen.
        const rerun = state.analysisType === ANALYSIS_TYPES.WEIBULL && Boolean(state.result) && CAN_EDIT && state.result.saved;
        await loadOptions();
        resetAnalyses();
        if (rerun) {
          await loadSummary();
          await performAnalysis();
        } else {
          clearWorkspace();
          await refresh();
        }
      },
    });
    const modal = el("div", { class: "lda-modal lda-record-modal", role: "dialog", "aria-modal": "true", "aria-labelledby": form.id("title") }, [
      el("button", { type: "button", class: "lda-help-close lda-record-close", "aria-label": "Close the disposition", text: "×", onclick: () => close(false) }),
      form.node,
    ]);
    backdrop.appendChild(modal);
    backdrop.addEventListener("mousedown", (event) => {
      if (event.target === backdrop) close(false);
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(backdrop);
    form.focus();
  }

  // ---- Weibull result text ---------------------------------------------------
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
    const node = el("div", { class: "lda-panel lda-age-panel", id: "lda-age-panel" }, [
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


  function field(labelText, input) {
    return el("div", { class: "lda-field" }, [el("label", { text: labelText }), input]);
  }
  function panel(title, body, footer, id) {
    return el("div", { class: "lda-panel", id: id || null }, [el("h3", { text: title }), body, footer ? el("p", { class: "lda-hint", text: footer }) : null]);
  }
  function legend() {
    return el("div", { class: "lda-legend", id: "lda-result-legend" }, [
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

  // What a life is, in the words the data table and the report use: a life ends in
  // a failure, is suspended when other work renewed the item, or is still running
  // at the cutoff.
  const OBSERVATION_TYPE_LABELS = {
    COMPLETED_FAILURE_LIFE: "Ends in a failure",
    PM_RESET_CENSORED_LIFE: "Censored at a PM reset",
    SUSPENDED_LIFE: "Suspended: renewed by other work",
    RIGHT_CENSORED_LIFE: "Censored at the cutoff (current life)",
  };


  // ---- charts -----------------------------------------------------------------
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
  // and Downtime by Sub-asset (per-sub-asset hours) panels. Short bin labels are
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

    // Each pane's card has an id for the tour, which takes them one at a time.
    const panes = [
      { key: "prob", id: "lda-chart-prob", title: "Weibull Probability Plot", height: 300 },
      { key: "cdf", id: "lda-chart-cdf", title: "CDF", height: 300 },
      { key: "pdf", id: "lda-chart-pdf", title: "Probability Density (PDF)", height: 300 },
      { key: "hazard", id: "lda-chart-hazard", title: "Hazard Rate", height: 300 },
    ];
    const canvases = {};
    panes.forEach((pane) => {
      const canvas = el("canvas", { class: "lda-canvas" });
      const card = el("div", { class: "lda-chart-card", id: pane.id }, [
        el("h4", { text: pane.title }),
        el("div", { class: "lda-chart-wrap" }, [canvas]),
      ]);
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
  // pointed, the way a spreadsheet leaves blanks last, as the Disposition list's
  // server-side sort does (StandardDispositionService._list_order).
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

    // A table drawn a batch at a time (fillRows) draws the rest first, so a sort
    // or a filter takes in every row rather than the ones scrolled to so far.
    const dataRows = () => {
      if (typeof table.drawAllRows === "function") table.drawAllRows();
      return Array.from(tbody.rows).filter(
        (tr) => tr.cells.length === ths.length && !tr.querySelector(".lda-empty-row")
      );
    };

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
  // observation behind a hovered point or current-life marker, and by the Pareto's
  // bars.
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
  // Fill the tooltip with [label, value] rows and put it beside the pointer, on
  // the other side of it where it would run off the window. The Pareto's bars use
  // it too.
  function showTooltipRows(clientX, clientY, fields) {
    const tip = pointTooltip();
    tip.innerHTML = "";
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
  function showPointTooltip(clientX, clientY, obs) {
    if (!obs) { hidePointTooltip(); return; }
    const truncate = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s);
    showTooltipRows(clientX, clientY, [
      ["Task ID", obs.source_task_id != null && obs.source_task_id !== "" ? String(obs.source_task_id) : "—"],
      ["Life hours", fmtFixed(obs.life_hours_for_weibull) || "—"],
      ["Start", obs.start_datetime || "—"],
      ["End / cutoff", obs.end_datetime || obs.analysis_cutoff_datetime || "—"],
      ["Request", truncate(obs.source_request_description, 220) || "—"],
      ["Completion notes", truncate(obs.source_completion_notes, 220) || "—"],
      ["Failure narrative", truncate(narrativeText(obs, "source_"), 260) || "—"],
    ]);
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


  // ---- Show me around -------------------------------------------------------------
  // The page draws its population, summary and results only once an asset is
  // picked, so the tour does the picking: with no asset chosen, Next on the asset
  // step opens an example (a population with a saved fit when there is one), and
  // on the results step it opens that saved fit -- never running and saving one of
  // its own. Which panels it covers depends on the Analysis Type, which can't
  // change while it is open.
  const ANALYSIS_TOUR_SEEN_KEY = "gremlin.analysis.tour-seen";
  const DOCS_URL = "/standards-and-documentation/documentation";
  const docsLink = (fragment, text) => ({ href: `${DOCS_URL}#${fragment}`, text });
  const forType = (type) => () => state.analysisType === type;
  let tourExampleTried = false;
  let tourFitTried = false;

  function waitFor(test, timeout) {
    return new Promise((resolve) => {
      const started = Date.now();
      (function check() {
        if (test() || Date.now() - started > (timeout || 8000)) resolve();
        else setTimeout(check, 80);
      })();
    });
  }

  const TOUR_EXAMPLE_ACTION = {
    label: "Pick an example ►",
    needed: () => !state.asset && !tourExampleTried,
    run: async () => {
      tourExampleTried = true;
      try {
        const data = await getJson(`${API}/tour-example`);
        const example = data.example;
        if (!example) return;
        combobox.select(example.asset);
        await waitFor(() => Boolean(state.summary) && Boolean(state.options));
        if (example.saved_fit) {
          await openRankedFit({ selection: example.selection });
          await waitFor(() => Boolean(state.result) || Boolean($("lda-no-fit")));
        }
      } catch (err) {
        /* the tour carries on over an empty page */
      }
    },
  };

  // The results step opens the fit saved for the population on screen, or for the
  // first ranked population when this one has none.
  const TOUR_FIT_ACTION = {
    label: "Show a fit ►",
    needed: () => state.analysisType === ANALYSIS_TYPES.WEIBULL && !state.result && !tourFitTried && Boolean(state.asset),
    run: async () => {
      tourFitTried = true;
      const ranked = (state.summary && state.summary.rankings) || [];
      if (ranked.length) {
        await openRankedFit(ranked[0]);
        await waitFor(() => Boolean(state.result) || Boolean($("lda-no-fit")));
      }
    },
  };

  const ANALYSIS_TOUR_STEPS = [
    {
      target: null,
      title: "Perform an Analysis",
      body:
        "Pick an asset, narrow it to a population, and the analysis chosen reads that population from the work-order " +
        "dispositions. The Weibull narrows by sub-asset, component and mechanism, the three that say which item fails " +
        "which way; Triage and the other views narrow by mode, cause, action and category as well.",
    },
    {
      target: "#lda-asset-field",
      title: "Pick an asset",
      body: () =>
        state.asset
          ? `Showing ${state.asset}. Its sub-assets' work orders are in reach too.`
          : "Choose the asset to analyse. Next picks an example if you haven't.",
      action: TOUR_EXAMPLE_ACTION,
    },
    {
      target: "#lda-type-field",
      title: "Analysis type",
      body:
        "Decides which views appear under the Pareto. The sub-asset, component and mechanism ticked stay when you switch; " +
        "the mode, cause, action and categories ticked for Triage and the others wait there while the Weibull is showing.",
    },
    {
      target: "#lda-population-selector",
      title: "The population",
      body: () =>
        isWeibull()
          ? "Tick values to narrow it; leave a list unticked for no restriction. Sub-asset and component choose which " +
            "items' lives are pooled -- a life is measured on one kind of item -- and mechanism which of their failures count."
          : "Tick values to narrow it; leave a list unticked for no restriction. Every view under the Pareto reads the " +
            "work orders matching all six lists.",
    },
    {
      target: "#lda-categories-field",
      title: "Categories",
      body: "Which dispositions the views count: failures by default.",
      when: () => !isWeibull(),
    },
    {
      target: "#lda-dates",
      title: "From and To",
      body: "Every view counts only work orders found in the range, and a Weibull run takes the dates as its start and cutoff.",
    },
    {
      target: "#lda-pop-string",
      title: "The population definition",
      body: "The population as one line, the way a saved Weibull run keeps it, so a result can always be traced to exactly what it read.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
    },
    {
      target: "#lda-summary-grid",
      title: "Readiness",
      body: "How far the work orders in reach have got: how many are dispositioned, how many failures the population has, and how many still have no component, which keeps them out of every life.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
      more: docsLink("common-readiness", "Readiness, in the documentation"),
    },
    {
      target: "#lda-beta-panel",
      title: "Highest beta",
      body: "Saved fits of one-component populations, strongest wear-out first: where an age-based PM is most likely to pay off. Click one to open it.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
      more: docsLink("common-beta", "Beta rankings, in the documentation"),
    },
    {
      target: "#lda-risk-panel",
      title: "Most likely to fail soon",
      body: "The same fits by the chance one of their items in service fails within the weeks you choose, given how long each has run.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
      more: docsLink("common-risk", "Risk rankings, in the documentation"),
    },
    {
      target: "#lda-pareto-panel",
      title: "Pareto",
      body: () =>
        isWeibull()
          ? "The population's failures, one bar per sub-asset, component and mechanism, each split by failure cause and " +
            "ranked by count or downtime. Each bar is one kind of item failing one way; click it to fit it. The Pareto " +
            "and the population stay as they are."
          : "One bar per value of the dimension chosen, each split by failure cause, ranked by count or downtime. Click a " +
            "bar to narrow the population to it: a sub-asset opens into its components, a component into its mechanisms.",
    },
    {
      target: "#lda-drill",
      title: "Where you are",
      body: "Right above the bars: after a drill, the way here, each step a click back to it, with Back to undo the last " +
        "drill and Start over. Filters ticked by hand show as chips that each take one back out.",
      when: () => !isWeibull() || state.drillHistory.length > 0 || hasNarrowing(),
    },
    {
      target: "#lda-matrix-panel",
      title: "Where it happens",
      body: "The population by sub-asset and component. Click a cell to narrow to that item on that sub-asset.",
    },
    {
      target: "#lda-trend-chart-panel",
      title: "Failure trend",
      body: "Work orders per month in the population; click a month to list its work orders below. The cards above name what grew and what improved.",
      when: forType(ANALYSIS_TYPES.TRIAGE),
    },
    {
      target: "#lda-trend-wo-panel",
      title: "The work orders behind it",
      body: () => "Every work order the trend counts." + RECORD_EDIT_HINT,
      when: forType(ANALYSIS_TYPES.TRIAGE),
    },
    {
      target: "#lda-pm-chart-panel",
      title: "Failures following a PM",
      body: "Each completed PM on the asset or a sub-asset paired with the next failure found there. Short gaps mean the PM is not preventing that failure.",
      when: forType(ANALYSIS_TYPES.PM),
    },
    {
      target: "#lda-pm-table-panel",
      title: "PM-to-failure detail",
      body: () => "One row per PM followed by a failure, with the days between." + RECORD_EDIT_HINT,
      when: forType(ANALYSIS_TYPES.PM),
    },
    {
      target: "#lda-repeat-rate-panel",
      title: "Repeat fix rate",
      body: "Item by item, how often a failure came back within the window of the previous repair being finished: the fix did not hold.",
      when: forType(ANALYSIS_TYPES.REPEAT),
    },
    {
      target: "#lda-repeat-pairs-panel",
      title: "The repeats",
      body: "Each repeat with the fix before it. A gap under an hour is flagged: it is what a duplicate work order looks like.",
      when: forType(ANALYSIS_TYPES.REPEAT),
    },
    {
      target: "#lda-downtime-trend-panel",
      title: "Downtime over time",
      body: "The population's downtime hours per month.",
      when: forType(ANALYSIS_TYPES.DOWNTIME),
    },
    {
      target: "#lda-downtime-dist-panel",
      title: "Short stops or long ones",
      body: "Work orders by length of stop: many short stops call for a different fix than a few long ones.",
      when: forType(ANALYSIS_TYPES.DOWNTIME),
    },
    {
      target: "#lda-downtime-events-panel",
      title: "The biggest stops",
      body: () => "The ten work orders with the most downtime." + RECORD_EDIT_HINT,
      when: forType(ANALYSIS_TYPES.DOWNTIME),
    },
    {
      target: "#lda-perform",
      title: CAN_EDIT ? "Perform Analysis" : "Preview the fit",
      body: CAN_EDIT
        ? "Builds the population ticked on the card item by item, fits a 2-parameter Weibull, and saves the run with its population definition. A Pareto bar does the same for its one sub-asset, component and mechanism."
        : "Builds and fits the population's lives without saving anything. An editor saves runs, which the rankings and reports read.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
    },
    {
      target: "#lda-workspace",
      title: "The Weibull results",
      body: () =>
        state.result
          ? `The fit of ${state.result.analysis_label}: ${state.result.failure_count} failures among ${state.result.total_observation_count} lives.`
          : "A fit shows here, with everything it rests on, once a Pareto bar or Perform Analysis runs one. Nothing is fitted until then.",
      action: TOUR_FIT_ACTION,
      when: forType(ANALYSIS_TYPES.WEIBULL),
      more: docsLink("analysis-weibull", "The Weibull analysis, in the documentation"),
    },
    {
      target: "#lda-result-pop",
      title: "What it read",
      body: "The population definition this run kept: the same line in the report, so the result can be reproduced.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
    },
    {
      target: "#lda-result-warnings",
      title: "Validity warnings",
      body: "What could make the fit mislead: mixed components or mechanisms, too few failures, unknown mechanisms, induced failures, records still to code.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
    },
    {
      target: "#lda-result-params",
      title: "Beta and eta",
      body: "Beta is the failure pattern -- below 1 early-life, about 1 random, above 1 wear-out -- and eta the life by which 63% have failed.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
      more: docsLink("weibull-fit", "The fit, in the documentation"),
    },
    {
      target: "#lda-chart-prob",
      title: "The graphs",
      body: "The probability plot, CDF, PDF and hazard rate of the fit, with each life plotted. Hover a point for its work order.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
      more: docsLink("weibull-charts", "The graphs, in the documentation"),
    },
    {
      target: "#lda-age-panel",
      title: "Reliability at an age",
      body: "Enter an age -- a PM interval, say -- for the chance of surviving to it and of failing by it.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
    },
    {
      target: "#lda-streams-panel",
      title: "Items pooled",
      body: "Every item the fit rests on, with its failures and suspensions. A recurrent item had a failure that did not renew it.",
      when: forType(ANALYSIS_TYPES.WEIBULL),
    },
    {
      target: "#lda-weibull-data-panel",
      title: "The lives",
      body: () => "Each life: which work order started it and which ended it, its life hours, and why it ended as it did." + RECORD_EDIT_HINT,
      when: forType(ANALYSIS_TYPES.WEIBULL),
    },
    {
      target: "#lda-adjust-row",
      title: "Adjusting beta and eta",
      body: "Try other values and save them, with why, beside the MLE: the fit itself is never overwritten.",
      when: () => CAN_EDIT && state.analysisType === ANALYSIS_TYPES.WEIBULL,
    },
    {
      target: "#lda-report-bar",
      title: "The report",
      body: "A numbered Word report of the saved run: its population, warnings, graphs and every life.",
      when: () => CAN_EDIT && state.analysisType === ANALYSIS_TYPES.WEIBULL,
    },
    {
      target: null,
      title: "That's the page",
      body: () =>
        `The rest of this tour was about ${state.analysisType}; choose another type and take the tour again for its views.` +
        (CAN_EDIT ? " Disposition, in the sidebar, is where the work orders it reads are coded." : ""),
    },
  ];

  function startAnalysisTour() {
    if (!window.gremlinTour || window.gremlinTour.isOpen()) return;
    tourExampleTried = false;
    tourFitTried = false;
    window.gremlinTour.start(ANALYSIS_TOUR_STEPS, { seenKey: ANALYSIS_TOUR_SEEN_KEY, pinned: [".topbar", "#lda-step1-card"] });
  }

  // ---- start -------------------------------------------------------------------------
  let combobox = null;

  function redrawCharts() {
    if (state.summary) drawPareto();
    if (state.analysisType === ANALYSIS_TYPES.TRIAGE) renderTrendChart();
    if (state.analysisType === ANALYSIS_TYPES.PM && state.pm) renderPm();
    if (state.analysisType === ANALYSIS_TYPES.DOWNTIME) renderDowntimeCharts();
    if (state.analysisRedraw) state.analysisRedraw();
  }

  function init() {
    if (!$("lda-asset")) return;
    combobox = window.GremlinStandard.assetCombobox({
      input: $("lda-asset"),
      list: $("lda-asset-list"),
      hint: $("lda-asset-hint"),
      onChoose: (asset) => chooseAsset(asset),
    });
    const typeSelect = $("lda-analysis-type");
    // ?analysis= preselects the type, which the topbar's search links to.
    const params = new URLSearchParams(window.location.search);
    let requested = (params.get("analysis") || "").trim().toLowerCase();
    // Triage was called Failure Mode Trend Analysis; old links still open it.
    if (requested === "failure mode trend analysis") requested = ANALYSIS_TYPES.TRIAGE.toLowerCase();
    if (requested) {
      const match = Array.from(typeSelect.options).find((option) => option.value.toLowerCase() === requested);
      if (match) typeSelect.value = match.value;
    }
    state.analysisType = typeSelect.value || ANALYSIS_TYPES.WEIBULL;
    typeSelect.addEventListener("change", () => setAnalysisType(typeSelect.value));
    const group = $("lda-pareto-group");
    group.addEventListener("change", () => {
      state.groupBy = group.value;
      // Whether a bar click narrows depends on the dimension.
      renderDrill();
      loadSummary();
    });
    $("lda-pareto-metric").addEventListener("click", (event) => {
      const button = event.target.closest("[data-metric]");
      if (!button || button.dataset.metric === state.metric) return;
      state.metric = button.dataset.metric;
      animatePareto();
    });
    $("lda-date-from").addEventListener("change", onDateRangeChange);
    $("lda-date-to").addEventListener("change", onDateRangeChange);
    $("lda-date-reset").addEventListener("click", resetDateRange);
    $("lda-exclude-induced").addEventListener("change", (event) => {
      state.excludeInduced = event.target.checked;
      onPopulationChange();
    });
    $("lda-risk-weeks").addEventListener("change", onRiskWeeksChange);
    $("lda-repeat-window").addEventListener("change", onRepeatWindowChange);
    $("lda-repeat-filter-clear").addEventListener("click", () => {
      state.repeatFilter = null;
      renderRepeat();
    });
    $("lda-perform").addEventListener("click", performAnalysis);
    $("analysis-tour-btn").addEventListener("click", startAnalysisTour);
    applyAnalysisTypeUI();
    window.addEventListener("resize", redrawCharts);
    // A canvas keeps the pixels it was painted with, so a theme change has to
    // redraw every chart with the new palette.
    window.gremlinOnThemeChange(() => {
      C = window.gremlinChartPalette();
      redrawCharts();
    });
    combobox.load().then(async () => {
      // The Pareto's dimensions come with the first options; offered until then
      // as the drill-down's.
      const asset = (params.get("asset") || "").trim();
      if (asset) combobox.select(asset);
      else if (window.gremlinTour && !window.gremlinTour.seen(ANALYSIS_TOUR_SEEN_KEY) && !window.gremlinTour.busy()) {
        startAnalysisTour();
      }
    });
    group.innerHTML = "";
    [
      ["sub_asset", "Sub-asset"],
      ["component", "Component"],
      ["failure_mechanism", "Failure mechanism"],
      ["failure_mode", "Failure mode"],
      ["failure_cause", "Failure cause"],
      ["action", "Action"],
      ["category", "Disposition category"],
      ["detection_method", "Detection method"],
    ].forEach(([value, label]) => group.appendChild(el("option", { value, text: label })));
    group.value = state.groupBy;
  }

  init();
})();
