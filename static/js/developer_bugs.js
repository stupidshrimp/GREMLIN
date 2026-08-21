/* Bug reports dashboard.
 *
 * One GET answers the page: /developer/api/bugs?status=&search= returns the
 * tiles' counts and the filtered list. Filtering and searching re-ask rather
 * than sifting an in-browser copy, so what is drawn is always what the shared
 * database currently says -- which matters more here than elsewhere, because
 * two administrators can be working the same queue at once.
 *
 * Resolving and reopening post JSON and then re-read the list, rather than
 * patching the row in place: the re-read is what makes a report somebody else
 * already resolved show up as resolved instead of quietly reverting.
 *
 * Every value from the database is written with textContent, never innerHTML.
 * These strings were typed by whoever filed the report, so they are the one
 * place on the developer pages where untrusted text is displayed.
 */
(function () {
  "use strict";

  const dev = window.GremlinDev;
  const $ = dev.$;
  const el = dev.el;
  const formatNumber = dev.formatNumber;
  const formatTimestamp = dev.formatTimestamp;

  const SEVERITY_LABELS = {
    blocking: "Blocking",
    major: "Major",
    minor: "Minor",
  };

  // Severity decides the pill's colour, so the queue can be read at a glance:
  // blocking is the one that should look like an alarm.
  const SEVERITY_PILL = {
    blocking: "dev-pill-warn",
    major: "dev-pill-major",
    minor: "dev-pill-muted",
  };

  // A request in flight is stale the moment another is made -- typing in the
  // search box makes several. Only the newest is allowed to draw.
  const state = { status: "open", search: "", requestVersion: 0, busy: false };

  function bugsUrl() {
    const params = new URLSearchParams({ status: state.status });
    if (state.search) params.set("search", state.search);
    return `/developer/api/bugs?${params.toString()}`;
  }

  async function load() {
    const version = ++state.requestVersion;
    try {
      const payload = await dev.getJSON(bugsUrl());
      if (version !== state.requestVersion) return;
      dev.showStatus("");
      render(payload);
    } catch (err) {
      if (version !== state.requestVersion) return;
      // The share being unreachable is the expected failure here, and it is
      // the one the message names, so it is shown as-is rather than replaced
      // with something generic.
      dev.showStatus(err.message, true);
      $("dev-bugs-count").textContent = "Could not read the reports.";
      dev.replaceChildren($("dev-bugs-list"), null);
    }
  }

  function render(payload) {
    renderStats(payload.summary || {});
    renderList(payload.reports || []);
  }

  function renderStats(summary) {
    const container = $("dev-bugs-stats");
    if (!container) return;
    container.textContent = "";
    const cards = [
      ["Open", summary.open, summary.open === 0 ? "Nothing outstanding." : "Waiting on somebody."],
      [
        "Blocking",
        summary.open_blocking,
        summary.open_blocking ? "Somebody is completely stuck." : "Nobody is stuck.",
      ],
      ["Resolved", summary.resolved, "Closed out."],
      [
        "Filed in total",
        summary.total,
        summary.latest_report ? `Latest ${formatTimestamp(summary.latest_report)}` : "Nothing filed yet.",
      ],
    ];
    cards.forEach(function (card) {
      const box = el("div", "dev-stat");
      box.appendChild(el("span", "dev-stat-label", card[0]));
      box.appendChild(el("span", "dev-stat-value", formatNumber(card[1] || 0)));
      box.appendChild(el("span", "dev-stat-note", card[2]));
      container.appendChild(box);
    });
  }

  function renderList(reports) {
    const list = $("dev-bugs-list");
    const count = $("dev-bugs-count");
    if (!list || !count) return;

    if (!reports.length) {
      count.textContent = state.search
        ? "No reports match that search."
        : state.status === "open"
        ? "No open reports. Nothing is outstanding."
        : "No reports here yet.";
      list.textContent = "";
      return;
    }
    count.textContent = `${formatNumber(reports.length)} report${reports.length === 1 ? "" : "s"}.`;

    const fragment = document.createDocumentFragment();
    reports.forEach(function (report) {
      fragment.appendChild(buildReport(report));
    });
    list.textContent = "";
    list.appendChild(fragment);
  }

  function buildReport(report) {
    const isOpen = report.status === "open";
    const details = el("details", `dev-bug${isOpen ? "" : " is-resolved"}`);

    const summary = el("summary", "dev-bug-summary");
    summary.appendChild(el("span", "dev-bug-id", `#${report.id}`));
    summary.appendChild(el("span", "dev-bug-title", report.title));

    const tags = el("span", "dev-bug-tags");
    tags.appendChild(
      el(
        "span",
        `dev-pill ${isOpen ? "dev-pill-warn" : "dev-pill-ok"}`,
        isOpen ? "Open" : "Resolved"
      )
    );
    tags.appendChild(
      el(
        "span",
        `dev-pill ${SEVERITY_PILL[report.severity] || "dev-pill-muted"}`,
        SEVERITY_LABELS[report.severity] || report.severity
      )
    );
    if (report.area) tags.appendChild(el("span", "dev-pill dev-pill-muted", report.area));
    summary.appendChild(tags);
    summary.appendChild(el("span", "dev-bug-when", formatTimestamp(report.created_at)));
    details.appendChild(summary);

    const body = el("div", "dev-bug-body");
    // pre-wrap rather than splitting into paragraphs: the reporter's own line
    // breaks are usually the steps to reproduce, and joining them loses that.
    body.appendChild(el("p", "dev-bug-description", report.description));

    const facts = el("dl", "dev-facts");
    dev.factRow(facts, "Filed by", report.reporter || "Not given");
    dev.factRow(facts, "Filed", formatTimestamp(report.created_at));
    if (report.page_url) dev.factRow(facts, "From page", report.page_url);
    if (report.user_agent) dev.factRow(facts, "Browser", report.user_agent);
    if (!isOpen) {
      dev.factRow(facts, "Resolved", formatTimestamp(report.resolved_at));
      dev.factRow(facts, "Resolved by", report.resolved_by || "Not recorded");
    }
    if (report.resolution_note) dev.factRow(facts, "Note", report.resolution_note);
    body.appendChild(facts);

    body.appendChild(buildActions(report, isOpen));
    details.appendChild(body);
    return details;
  }

  function buildActions(report, isOpen) {
    const actions = el("div", "dev-bug-actions");

    const note = el("input", "dev-bug-note");
    note.type = "text";
    note.maxLength = 2000;
    note.placeholder = isOpen
      ? "What was done about it (optional)"
      : "Why it is being reopened (optional)";
    note.setAttribute(
      "aria-label",
      isOpen ? "Note to file with the resolution" : "Note to file with the reopening"
    );
    actions.appendChild(note);

    const toggle = el("button", isOpen ? "btn-primary" : "btn-secondary", isOpen ? "Mark resolved" : "Reopen");
    toggle.type = "button";
    toggle.addEventListener("click", function () {
      setStatus(report.id, isOpen ? "resolved" : "open", note.value, actions);
    });
    actions.appendChild(toggle);

    const remove = el("button", "btn-secondary dev-bug-delete", "Delete");
    remove.type = "button";
    remove.addEventListener("click", function () {
      // Deleting is the one action here that cannot be undone -- resolving is
      // reversible by reopening -- so it is the only one that asks first.
      if (!window.confirm(`Delete report #${report.id}? This cannot be undone.`)) return;
      deleteReport(report.id, actions);
    });
    actions.appendChild(remove);

    return actions;
  }

  function setButtonsDisabled(container, disabled) {
    container.querySelectorAll("button").forEach(function (button) {
      button.disabled = disabled;
    });
  }

  async function setStatus(reportId, status, note, container) {
    if (state.busy) return;
    state.busy = true;
    setButtonsDisabled(container, true);
    try {
      await dev.getJSON(`/developer/api/bugs/${reportId}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: status, note: note || "" }),
      });
      dev.showStatus(
        status === "resolved" ? `Report #${reportId} resolved.` : `Report #${reportId} reopened.`
      );
    } catch (err) {
      dev.showStatus(err.message, true);
      setButtonsDisabled(container, false);
      state.busy = false;
      return;
    }
    state.busy = false;
    await load();
  }

  async function deleteReport(reportId, container) {
    if (state.busy) return;
    state.busy = true;
    setButtonsDisabled(container, true);
    try {
      await dev.getJSON(`/developer/api/bugs/${reportId}/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      dev.showStatus(`Report #${reportId} deleted.`);
    } catch (err) {
      dev.showStatus(err.message, true);
      setButtonsDisabled(container, false);
      state.busy = false;
      return;
    }
    state.busy = false;
    await load();
  }

  dev.whenReady("#dev-bugs-list", function () {
    const status = $("dev-bugs-status");
    const search = $("dev-bugs-search");
    const refresh = $("dev-bugs-refresh");

    if (status) {
      status.addEventListener("change", function () {
        state.status = status.value;
        load();
      });
    }
    if (search) {
      // Debounced: a search re-asks the server, and asking on every keystroke
      // would put a query on the shared drive per letter typed.
      let timer = null;
      search.addEventListener("input", function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(function () {
          state.search = search.value.trim();
          load();
        }, 250);
      });
    }
    if (refresh) refresh.addEventListener("click", load);

    load();
  });
})();
