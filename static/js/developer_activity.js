/* User activity page.
 *
 * One GET answers the whole page: /developer/api/activity?days=N returns the
 * summary, a row per account, the daily and hour-of-day distributions, the
 * recent sign-ins and changes, and how much history exists behind them. Changing
 * the window re-asks rather than filtering in the browser, so what is drawn is
 * always what the access database currently says.
 *
 * The charts are built from ordinary elements sized as percentages rather than a
 * canvas: these are two small distributions, they need to stay readable at any
 * width, and every label is real text a screen reader can reach.
 */
(function () {
  "use strict";

  const dev = window.GremlinDev;
  const $ = dev.$;
  const el = dev.el;
  const factRow = dev.factRow;
  const formatNumber = dev.formatNumber;
  const formatTimestamp = dev.formatTimestamp;

  const OUTCOME_LABELS = {
    success: "Signed in",
    failure: "Wrong PIN",
    blocked: "Refused — locked out",
  };

  const state = { days: 30, requestVersion: 0 };

  // Counts here are frequently 1 -- one account never used, one attempt refused
  // -- and "1 attempts" is the kind of wrong that makes a page look unfinished.
  function plural(count, singular, pluralForm) {
    return `${formatNumber(count)} ${count === 1 ? singular : pluralForm || `${singular}s`}`;
  }

  // The server buckets by UTC hour, because that is what its timestamps are in.
  // A plant reading "most sign-ins at 13:00" wants its own clock, so the buckets
  // are rotated by the reader's offset -- but only when that offset is a whole
  // number of hours. Where it is not (India, parts of Australia), rotating would
  // put half of one local hour into another, so the chart keeps UTC and says so.
  function hourOffset() {
    const minutes = -new Date().getTimezoneOffset();
    return minutes % 60 === 0 ? minutes / 60 : null;
  }

  function render(payload) {
    renderWindow(payload);
    renderStats(payload.summary);
    renderDaily(payload.daily);
    renderHourly(payload.hourly);
    renderActions(payload.top_actions);
    renderUsers(payload.users);
    renderRecentLogins(payload.recent_logins);
    renderRecentChanges(payload.recent_changes);
    renderRetention(payload);
  }

  function renderWindow(payload) {
    const window_ = payload.window || {};
    const summary = payload.summary || {};
    $("dev-activity-window").textContent =
      `${window_.first_day} to ${window_.last_day} (${formatNumber(window_.days)} days, ` +
      `counted in ${window_.timezone}) · ${formatNumber(summary.accounts)} accounts configured.`;
  }

  function renderStats(summary) {
    const cards = [
      ["Sign-ins", summary.successful_logins, "successful logins in the window"],
      ["Accounts used", `${formatNumber(summary.users_seen)} of ${formatNumber(summary.accounts)}`,
        summary.never_signed_in
          ? `${plural(summary.never_signed_in, "account has", "accounts have")} never signed in at all`
          : "every account has signed in at some point"],
      ["Days with a sign-in", summary.days_with_logins, "days somebody actually used GREMLIN"],
      ["Changes recorded", summary.changes, `by ${plural(summary.change_authors, "account")}`],
      ["Wrong PINs", summary.failed_logins,
        `${plural(summary.blocked_logins, "attempt")} refused while locked out`],
      ["Unrecognised accounts", summary.unknown_account_attempts, "attempts on names matching no account"],
    ];
    const grid = $("dev-activity-stats");
    grid.textContent = "";
    cards.forEach(function (card) {
      const box = el("div", "dev-stat");
      box.appendChild(el("span", "dev-stat-label", card[0]));
      box.appendChild(el("span", "dev-stat-value", typeof card[1] === "number" ? formatNumber(card[1]) : card[1]));
      box.appendChild(el("span", "dev-stat-note", card[2]));
      grid.appendChild(box);
    });
    // Activity by an account that has since been removed belongs to nobody in
    // the table below, so it is only visible if it is said out loud.
    if (summary.logins_by_removed_accounts) {
      const box = el("div", "dev-stat");
      box.appendChild(el("span", "dev-stat-label", "Removed accounts"));
      box.appendChild(el("span", "dev-stat-value", formatNumber(summary.logins_by_removed_accounts)));
      box.appendChild(el("span", "dev-stat-note", "sign-ins by accounts that no longer exist"));
      grid.appendChild(box);
    }
  }

  // Both charts are the same shape: a row of columns scaled against the busiest
  // one, each labelled with what it counts.
  function buildChart(container, bars, options) {
    const settings = options || {};
    const peak = bars.reduce(function (max, bar) {
      return Math.max(max, bar.value);
    }, 0);
    container.textContent = "";
    if (!bars.length) {
      container.appendChild(el("p", "dev-muted", settings.emptyText || "Nothing recorded."));
      return;
    }
    const chart = el("div", bars.length > 60 ? "dev-chart-bars is-dense" : "dev-chart-bars");
    bars.forEach(function (bar) {
      const column = el("div", "dev-chart-col");
      column.title = bar.title;
      const track = el("div", "dev-chart-track");
      const fill = el("div", "dev-chart-fill");
      // Zero stays flat; anything counted keeps a sliver so a quiet day is
      // visibly different from a day with one sign-in.
      fill.style.height = peak && bar.value ? `${Math.max(4, (bar.value / peak) * 100)}%` : "0";
      if (!bar.value) fill.classList.add("is-empty");
      track.appendChild(fill);
      column.appendChild(track);
      if (bar.label !== null && bar.label !== undefined) {
        column.appendChild(el("span", "dev-chart-label", bar.label));
      }
      chart.appendChild(column);
    });
    container.appendChild(chart);
    if (settings.caption) container.appendChild(el("p", "dev-chart-caption", settings.caption));
    if (!peak) {
      container.appendChild(el("p", "dev-muted", settings.emptyText || "Nothing recorded in this window."));
    }
  }

  function renderDaily(daily) {
    const days = daily || [];
    // A year of daily labels cannot be read at any width, so label the ends and
    // roughly every twelfth column; the tooltip on each carries the exact date.
    const step = Math.max(1, Math.ceil(days.length / 12));
    const busiest = days.reduce(function (best, day) {
      return !best || day.logins > best.logins ? day : best;
    }, null);
    buildChart(
      $("dev-activity-daily"),
      days.map(function (day, index) {
        const showLabel = index === 0 || index === days.length - 1 || index % step === 0;
        return {
          value: day.logins,
          label: showLabel ? day.day.slice(5) : "",
          title:
            `${day.day}: ${plural(day.logins, "sign-in")} by ${plural(day.users, "account")}, ` +
            `${formatNumber(day.refused)} refused, ${plural(day.changes, "change")}`,
        };
      }),
      {
        // Columns are scaled against each other, so the busiest day is what
        // gives the rest of them a size.
        caption: busiest && busiest.logins
          ? `Busiest day: ${plural(busiest.logins, "sign-in")} on ${busiest.day}. ` +
            "Hover a column for that day's accounts, refused attempts and changes."
          : null,
        emptyText: "No sign-ins recorded in this window.",
      }
    );
  }

  function renderHourly(hourly) {
    const buckets = hourly || [];
    const offset = hourOffset();
    const local = offset !== null;
    const values = [];
    for (let hour = 0; hour < 24; hour += 1) {
      // Column `hour` is local, so it holds the UTC bucket that hour came from.
      const source = local ? (hour + ((24 - (offset % 24)) % 24)) % 24 : hour;
      values.push(buckets[source] || 0);
    }
    $("dev-activity-hourly-note").textContent = local
      ? "Successful sign-ins by hour of the day, in this browser's local time."
      : "Successful sign-ins by hour of the day, in UTC — this browser's time zone is offset by part of an hour.";
    buildChart(
      $("dev-activity-hourly"),
      values.map(function (value, hour) {
        return {
          value: value,
          label: hour % 3 === 0 ? String(hour).padStart(2, "0") : "",
          title: `${String(hour).padStart(2, "0")}:00 — ${plural(value, "sign-in")}`,
        };
      }),
      { emptyText: "No sign-ins recorded in this window." }
    );
  }

  function renderUsers(users) {
    const rows = (users || []).map(function (user) {
      return [
        user.username,
        user.role,
        formatNumber(user.logins),
        formatNumber(user.active_days),
        user.active_days ? user.logins_per_active_day.toFixed(1) : "—",
        formatNumber(user.distinct_sources),
        user.failed_logins || user.blocked_logins
          ? `${formatNumber(user.failed_logins)} / ${formatNumber(user.blocked_logins)}`
          : "—",
        formatNumber(user.changes),
        user.last_login ? formatTimestamp(user.last_login) : "Never signed in",
        user.last_change ? formatTimestamp(user.last_change) : "—",
      ];
    });
    dev.replaceChildren(
      $("dev-activity-users"),
      dev.buildTable(
        ["Account", "Role", "Sign-ins", "Active days", "Per active day", "Sources",
         "Wrong PIN / refused", "Changes", "Last sign-in", "Last change"],
        rows,
        { emptyText: "No accounts are configured." }
      )
    );
  }

  function renderRecentLogins(events) {
    const rows = (events || []).map(function (event) {
      return [
        event.known_account ? event.username : "unrecognised account",
        OUTCOME_LABELS[event.outcome] || event.outcome,
        formatTimestamp(event.occurred_at),
      ];
    });
    dev.replaceChildren(
      $("dev-activity-logins"),
      dev.buildTable(["Account", "Outcome", "When"], rows, {
        emptyText: "No sign-in attempts in this window.",
      })
    );
  }

  function renderRecentChanges(changes) {
    const rows = (changes || []).map(function (change) {
      return [change.username, change.action, formatTimestamp(change.changed_at)];
    });
    dev.replaceChildren(
      $("dev-activity-changes"),
      dev.buildTable(["Account", "Action", "When"], rows, {
        emptyText: "No protected writes in this window.",
      })
    );
  }

  function renderActions(actions) {
    const rows = (actions || []).map(function (action) {
      return [action.action, formatNumber(action.count), formatNumber(action.users), formatTimestamp(action.last_used)];
    });
    dev.replaceChildren(
      $("dev-activity-actions"),
      dev.buildTable(["Action", "Times", "Accounts", "Last used"], rows, {
        emptyText: "No protected writes in this window.",
      })
    );
  }

  function renderRetention(payload) {
    const retention = payload.retention || {};
    const list = el("dl", "dev-facts");
    factRow(list, "Access database", payload.access_db_path);
    factRow(list, "Sign-in attempts stored", formatNumber(retention.login_events));
    factRow(
      list,
      "History begins",
      retention.first_event ? formatTimestamp(retention.first_event) : "No sign-in has been recorded yet"
    );
    factRow(list, "Most recent attempt", retention.last_event ? formatTimestamp(retention.last_event) : "—");
    factRow(
      list,
      "Retention",
      retention.capped
        ? `At the ${formatNumber(retention.login_event_cap)}-row cap — the oldest attempts have been dropped, ` +
          "so a window reaching further back than the start of this history is under-counted."
        : `Capped at ${formatNumber(retention.login_event_cap)} attempts; below it, so nothing has been dropped.`
    );
    factRow(
      list,
      "What is counted",
      "Sign-ins have been recorded since this version was deployed. Changes come from the " +
        "audit trail, which every protected write has always stamped."
    );
    list.id = "dev-activity-retention";
    $("dev-activity-retention").replaceWith(list);
  }

  async function load() {
    const requestVersion = ++state.requestVersion;
    const button = $("dev-activity-refresh");
    button.disabled = true;
    try {
      const payload = await dev.getJSON(dev.API.activity(state.days));
      // A window changed twice in quick succession must not be drawn by
      // whichever response happens to arrive last: only the newest request may
      // paint, whenever it lands.
      if (requestVersion !== state.requestVersion) return;
      dev.showStatus("");
      render(payload);
    } catch (err) {
      if (requestVersion !== state.requestVersion) return;
      dev.showStatus(err.message, true);
    } finally {
      // An older request finishing must not re-enable the button while a newer
      // one is still in flight.
      if (requestVersion === state.requestVersion) button.disabled = false;
    }
  }

  function init() {
    const select = $("dev-activity-days");
    state.days = Number(select.value) || 30;
    select.addEventListener("change", function () {
      state.days = Number(select.value) || 30;
      load();
    });
    $("dev-activity-refresh").addEventListener("click", load);
    load();
  }

  dev.whenReady("#dev-activity-days", init);
})();
