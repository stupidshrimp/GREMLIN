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

  // Every other timestamp on this page is shown in the reader's own zone,
  // which is right for "when did this happen". The dates that describe where
  // the history becomes complete are a different kind of statement and get a
  // different treatment: shown to the second, and in UTC.
  //
  // To the second because a trim ends whenever it ends -- 09:14 on the 1st,
  // say -- and rendering that as "the 1st" claims the whole of the 1st is
  // accounted for while that morning's attempts are exactly what was dropped.
  // In UTC because the window this is read against is a run of UTC days: a
  // boundary converted to local time would be compared against a start that
  // was not, and the two can fall on different dates.
  //
  // The server sends "YYYY-MM-DD HH:MM:SS" in UTC, so this is a relabelling
  // rather than a conversion, and nothing is rounded in either direction.
  function formatUtc(value) {
    if (!value) return "";
    const text = String(value).trim();
    return /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/.test(text)
      ? `${text.slice(0, 10)} ${text.slice(11, 19)} UTC`
      : text;
  }

  // Whether the window reaches back past the start of the recorded history.
  // A window is only counted in full if two things hold, and "nothing has been
  // dropped" is one of them: the other is that something was being recorded for
  // the whole of it. On a deployment that has just upgraded, neither the daily
  // chart nor the totals can tell the days before this feature existed from
  // days nobody signed in -- both are zero -- and that is the reading somebody
  // acts on by concluding GREMLIN is not being used.
  //
  // Both values are UTC "YYYY-MM-DD HH:MM:SS" from the server, so comparing
  // them as text compares them as instants.
  function startsBeforeHistory(payload) {
    const start = (payload.window || {}).start;
    const first = (payload.retention || {}).first_event;
    if (!start) return false;
    return !first || start < first;
  }

  // Counts here are frequently 1 -- one account never used, one attempt refused
  // -- and "1 attempts" is the kind of wrong that makes a page look unfinished.
  function plural(count, singular, pluralForm) {
    return `${formatNumber(count)} ${count === 1 ? singular : pluralForm || `${singular}s`}`;
  }

  // The server buckets by UTC hour and keeps each bucket's date, because a
  // plant reading "most sign-ins at 13:00" wants its own clock and an hour of
  // the year is not a fixed distance from UTC. Converting each bucket through a
  // Date built from its own day applies the daylight-saving rules that were in
  // force *then*, so a winter sign-in read in summer stays in the hour it
  // happened. The server buckets to quarter-hours, and the minutes come across
  // with them, so a zone offset by half or three-quarters of an hour (India,
  // Nepal, parts of Australia) lands in the right local hour too.
  function localHour(day, hour, minute) {
    const parts = String(day).split("-");
    const at = new Date(
      Date.UTC(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]), hour, minute || 0)
    );
    return isNaN(at.getTime()) ? null : at.getHours();
  }

  function render(payload) {
    state.historyStart = (payload.retention || {}).sign_in_history_from || null;
    renderWindow(payload);
    renderStats(payload.summary, payload.users);
    renderDaily(payload);
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

  function renderStats(summary, users) {
    // The same care as the table's last-sign-in cell: an account that existed
    // before the history did has not been shown to be unused, so the tile says
    // "no sign-in recorded since <date>" unless every unused account has been
    // watched from the day it was created.
    const unused = (users || []).filter(function (user) {
      return !user.last_login;
    });
    const allWatched = unused.every(function (user) {
      return user.tracked_since_created;
    });
    const since = formatUtc(state.historyStart);
    const cards = [
      ["Sign-ins", summary.successful_logins, "successful logins in the window"],
      ["Accounts used", `${formatNumber(summary.users_seen)} of ${formatNumber(summary.accounts)}`,
        summary.never_signed_in
          ? allWatched
            ? `${plural(summary.never_signed_in, "account has", "accounts have")} never signed in at all`
            : `${plural(summary.never_signed_in, "account has", "accounts have")} no sign-in recorded` +
              (since ? ` since ${since}` : " yet")
          : "every account has signed in at some point"],
      ["Days with a sign-in", summary.days_with_logins, "days somebody actually used GREMLIN"],
      ["Changes recorded", summary.changes,
        !summary.changes
          ? "nothing was written in this window"
          : summary.change_authors
            ? `by ${plural(summary.change_authors, "account")}`
            // Every change in the window was made by somebody whose account has
            // since been removed. "by 0 accounts" is true and reads as a bug.
            : "all by accounts that no longer exist"],
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
    // Every kind of activity a removed account can leave behind, not just the
    // two that were reported first: an account deleted after somebody spent a
    // week guessing at it has only refusals, and leaving those out meant the
    // Wrong PIN total could not be reconciled with any row in the table.
    const goneLogins = summary.logins_by_removed_accounts || 0;
    const goneChanges = summary.changes_by_removed_accounts || 0;
    const goneRefusals = summary.refusals_by_removed_accounts || 0;
    if (goneLogins || goneChanges || goneRefusals) {
      // One number for the tile, with the split in the note: any one of the
      // three is often zero while another is not, and showing only the first
      // would put a prominent 0 on a tile that exists to say something happened.
      const box = el("div", "dev-stat");
      box.appendChild(el("span", "dev-stat-label", "Removed accounts"));
      box.appendChild(el("span", "dev-stat-value", formatNumber(goneLogins + goneChanges + goneRefusals)));
      box.appendChild(
        el(
          "span",
          "dev-stat-note",
          `${plural(goneLogins, "sign-in")}, ${plural(goneRefusals, "refused attempt")} and ` +
            `${plural(goneChanges, "change")} by accounts that no longer exist — counted in the ` +
            "totals above, but in no row below"
        )
      );
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

  function renderDaily(payload) {
    const days = payload.daily || [];
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
        //
        // And a flat run at the start of the window is two different things
        // drawn identically: days nobody signed in, and days from before
        // anything was being recorded. The second is the one that misleads --
        // it looks like evidence GREMLIN is going unused, on a deployment
        // where it simply was not watching yet -- so where the window reaches
        // back that far, the chart says which is which.
        caption: [
          busiest && busiest.logins
            ? `Busiest day: ${plural(busiest.logins, "sign-in")} on ${busiest.day}. ` +
              "Hover a column for that day's accounts, refused attempts and changes."
            : null,
          startsBeforeHistory(payload)
            ? ((payload.retention || {}).first_event
                ? `Columns before ${formatUtc((payload.retention || {}).first_event)} are days nothing ` +
                  "was being recorded, not days without a sign-in."
                : "Nothing has been recorded yet, so every column here is an unwatched day rather than a quiet one.")
            : null,
        ].filter(Boolean).join(" ") || null,
        emptyText: "No sign-ins recorded in this window.",
      }
    );
  }

  function renderHourly(hourly) {
    const values = new Array(24).fill(0);
    (hourly || []).forEach(function (bucket) {
      const hour = localHour(bucket.day, bucket.hour, bucket.minute);
      if (hour !== null) values[hour] += bucket.logins;
    });
    $("dev-activity-hourly-note").textContent =
      "Successful sign-ins by hour of the day, in this browser's local time — each day " +
      "converted at the offset that was in force on it.";
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

  // An account with no sign-in in a history that started after it did has not
  // been shown to be unused -- it was in use, or not, before anything was being
  // recorded. Saying "never" there would be a false statement about a real
  // person, and the kind an administrator might act on by deleting the account.
  function lastSignIn(user) {
    if (user.last_login) return formatTimestamp(user.last_login);
    if (user.tracked_since_created) return "Never signed in";
    const since = formatUtc(state.historyStart);
    return since ? `None recorded since ${since}` : "No sign-in recorded yet";
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
        lastSignIn(user),
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
    // The cap does not drop rows in date order -- it keeps sign-ins ahead of
    // refusals -- so saying "anything older than the start of the history is
    // under-counted" would be wrong in the direction that matters: at the cap
    // the sign-in history can still reach back years while the refusals only
    // reach back weeks, and a window well inside the history would quietly
    // report too few wrong PINs. Each tier is pruned newest-first, so naming
    // where each one starts says exactly what is missing.
    if (retention.pruned) {
      factRow(
        list,
        "Retention",
        `${formatNumber(retention.rows_dropped)} attempts dropped to stay within the ` +
          `${formatNumber(retention.login_event_cap)}-row cap, most recently ` +
          `${formatTimestamp(retention.last_pruned_at)}. Attempts on unrecognised names go first, ` +
          "then failures and lockouts against real accounts; successful sign-ins are the last to go. " +
          "So the counts that go short first are the ones somebody watching for an attack is reading."
      );
      // Each tier is pruned newest-first, so each has one clean cutoff and
      // these three dates describe the history exactly. Shown separately
      // because the tiers do not run out together -- claiming one date for
      // "refusals" would date the attributed ones and overstate the rest.
      const TIER_NAMES = {
        success: "sign-ins",
        attributed_refusal: "refusals on real accounts",
        unattributed_refusal: "attempts on unrecognised names",
      };
      // A tier with nothing left still says something, and it is the something
      // most worth saying: "none recorded" and "all of them dropped" look
      // identical in a count, and only one of them means nothing happened.
      const described = (retention.tiers || [])
        .map(function (tier) {
          const name = TIER_NAMES[tier.kind] || tier.kind;
          if (tier.first_kept) {
            // Where the tier is complete from, which is normally its oldest
            // surviving attempt but is not the same claim. Put the clock back
            // and an attempt can be written dated below a stretch already
            // trimmed away; the row is real and still held, but the history
            // does not run unbroken from it, and this line is the one thing on
            // the page somebody reads to decide whether a window can be
            // trusted. The server takes whichever date reaches further
            // forward, so what is shown here is never earlier than the point
            // the tier actually becomes whole.
            const since = tier.complete_since || tier.first_kept;
            return tier.dropped
              ? `${name} ${formatUtc(since)} (${formatNumber(tier.dropped)} older dropped)`
              : `${name} ${formatUtc(since)}`;
          }
          // Nothing survives in this tier, so there is no date to give -- a
          // trim line without a row behind it would read as history that is
          // still here.
          return tier.dropped ? `${name} none kept — all ${formatNumber(tier.dropped)} dropped` : null;
        })
        .filter(Boolean);
      factRow(
        list,
        "Complete since",
        described.length
          ? described.join(" · ") + ". A window starting before one of these under-counts that kind of attempt."
          : "Nothing has been recorded yet."
      );
      // Drops recorded before this version counted them by tier. Which kind
      // they were is not knowable, so the dates above cannot be read as a
      // complete account of what is missing, and saying nothing would let them
      // be read exactly that way.
      if (retention.dropped_unclassified) {
        factRow(
          list,
          "Not attributable",
          `${formatNumber(retention.dropped_unclassified)} of those attempts were dropped by an ` +
            "earlier version that did not record which kind they were, so any of the dates above " +
            "may reach back further than the history behind them actually does."
        );
      }
    } else {
      // Nothing dropped is only half of "counted in full", and on an upgraded
      // deployment it is the half that is easy to satisfy: no attempt has been
      // trimmed because there is barely any history to trim, and a 365-day
      // window over a fortnight-old record is mostly days that were never
      // watched. Claiming the window is complete there states the one thing
      // this panel exists to be right about, backwards.
      const kept = retention.capped
        ? `At the ${formatNumber(retention.login_event_cap)}-row cap. Nothing has been dropped yet — ` +
          "trimming starts once the history runs past the cap."
        : `Capped at ${formatNumber(retention.login_event_cap)} attempts; below it, so nothing has been dropped.`;
      let covered;
      if (!startsBeforeHistory(payload)) {
        covered = "Every attempt this window covers is still here, so it is counted in full.";
      } else if (retention.first_event) {
        covered =
          `This window reaches back past ${formatUtc(retention.first_event)}, where the record begins: ` +
          "the days before that are days nothing was being recorded, not quiet ones.";
      } else {
        covered = "No sign-in has been recorded yet, so there is nothing behind this window at all.";
      }
      factRow(list, "Retention", `${kept} ${covered}`);
    }
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
