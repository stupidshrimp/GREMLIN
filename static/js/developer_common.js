/* Shared plumbing for the developer pages.
 *
 * Database inspection and Limble sync are separate documents that talk to the
 * same /developer/api/* endpoints and are drawn from the same primitives, so the
 * fetch wrapper, the DOM builders and the formatters live here rather than being
 * copied into each. Published as window.GremlinDev; both page scripts load this
 * first.
 *
 * Rendering is text-only on purpose: every value that came out of the database
 * goes through textContent, never innerHTML, so stored content cannot inject
 * markup.
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

  const $ = (id) => document.getElementById(id);

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
      // A 403 means the session no longer holds an administrator account;
      // reloading shows the page that says so.
      if (response.status === 403) {
        window.location.reload();
      }
      const error = new Error((payload && payload.error) || `Request failed (${response.status}).`);
      // Callers that retry transient failures need to distinguish them from an
      // authentication failure, for which the reload above is the recovery.
      error.status = response.status;
      throw error;
    }
    return payload;
  }

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
  // ("2026-08-11 03:00:12.123456"). A browser reads that shape as *local* time,
  // so the zone has to be restated before it can be shown in the reader's own.
  function formatTimestamp(value) {
    if (!value) return "—";
    const text = String(value).trim();
    const normalised = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(text)
      ? `${text.replace(" ", "T")}Z`
      : text;
    const parsed = new Date(normalised);
    return isNaN(parsed.getTime()) ? text : parsed.toLocaleString();
  }

  // A page script runs from a <script> tag at the end of <body>, so everything
  // it touches is already parsed. Waiting on DOMContentLoaded instead would
  // couple these pages to base.html's external Google Fonts stylesheet: a
  // browser holds readyState at "loading" until a pending stylesheet resolves,
  // so on a network that cannot reach fonts.googleapis.com the page would never
  // initialise. `readySelector` names an element the page owns, and the event is
  // only used if the DOM really is not ready yet.
  function whenReady(readySelector, init) {
    if (document.querySelector(readySelector)) {
      init();
    } else {
      document.addEventListener("DOMContentLoaded", init);
    }
  }

  window.GremlinDev = {
    API: API,
    $: $,
    showStatus: showStatus,
    getJSON: getJSON,
    el: el,
    buildTable: buildTable,
    replaceChildren: replaceChildren,
    factRow: factRow,
    formatBytes: formatBytes,
    formatNumber: formatNumber,
    formatDuration: formatDuration,
    formatTimestamp: formatTimestamp,
    whenReady: whenReady,
  };
})();
