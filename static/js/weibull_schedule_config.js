/* The Weibull schedules panel on Configuration: the register of assets whose Weibull
   life hours are not counted on the plant default schedule, the form that changes
   one, and the record of every change (REL-WBL-DAT-003 §7). Editors only; the page
   leaves the panel and this script out for everyone else. */
(function () {
  "use strict";

  const API = "/life-data-analysis/api/schedule-register";
  const ASSETS_API = "/life-data-analysis/api/assets";

  const form = document.getElementById("config-weibull-form");
  if (!form) return;
  const assetInput = document.getElementById("config-weibull-asset");
  const assetList = document.getElementById("config-weibull-asset-list");
  const scheduleSelect = document.getElementById("config-weibull-schedule");
  const reasonInput = document.getElementById("config-weibull-reason");
  const saveButton = document.getElementById("config-weibull-save");
  const assignmentsHost = document.getElementById("config-weibull-assignments");
  const historyHost = document.getElementById("config-weibull-history");

  let register = null;

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value === null || value === undefined || value === false) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    });
    (children || []).forEach((child) => {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function show(message, kind) {
    if (kind === "error" && /log in|role is required|guest access is read-only/i.test(message) && window.gremlinToast) {
      window.gremlinToast(message);
      return;
    }
    const node = document.getElementById("config-weibull-status");
    if (!node) return;
    node.textContent = message;
    node.className = "lda-banner" + (kind ? " is-" + kind : "");
    node.hidden = !message;
  }

  async function request(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "Request failed (" + response.status + ")");
    return data;
  }

  // "2026-10-06 14:03:11" is SQLite's UTC; shown on the reader's own clock.
  function when(value) {
    if (!value) return "";
    const date = new Date(String(value).replace(" ", "T") + "Z");
    return isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function table(headers, rows, empty) {
    if (!rows.length) return el("p", { class: "metrics-empty", text: empty });
    return el("table", { class: "metrics-table" }, [
      el("thead", {}, [el("tr", {}, headers.map((text) => el("th", { scope: "col", text })))]),
      el("tbody", {}, rows.map((cells) => el("tr", {}, cells.map((text) => el("td", { text: text || "" }))))),
    ]);
  }

  function render() {
    const defaultCode = register.default_code;
    scheduleSelect.replaceChildren(
      ...register.schedules.map((schedule) =>
        el("option", { value: schedule.code, text: schedule.name + (schedule.code === defaultCode ? " (plant default)" : "") })
      )
    );
    assignmentsHost.replaceChildren(
      table(
        ["Asset Number", "Asset", "Schedule", "Changed by", "When"],
        register.assignments.map((row) => [row.asset_number, row.asset_name, row.schedule_name, row.changed_by, when(row.changed_at)]),
        "Every asset is on the plant default schedule."
      )
    );
    historyHost.replaceChildren(
      table(
        ["When", "Asset Number", "From", "To", "Why", "By"],
        register.history.map((row) => [when(row.changed_at), row.asset_number, row.from_name, row.to_name, row.reason, row.changed_by]),
        "No schedule changes yet."
      )
    );
  }

  // Picking an asset puts the schedule it is on now in the dropdown, so the form
  // starts from where the asset is rather than from the plant default.
  function syncScheduleToAsset() {
    if (!register) return;
    const asset = assetInput.value.trim();
    const listed = register.assignments.find((row) => row.asset_number === asset);
    scheduleSelect.value = listed ? listed.schedule_code : register.default_code;
  }

  async function load() {
    try {
      const [data, assets] = await Promise.all([request(API), request(ASSETS_API).catch(() => ({ assets: [] }))]);
      register = data;
      render();
      assetList.replaceChildren(
        ...(assets.assets || []).map((asset) =>
          el("option", { value: asset.asset_number, text: asset.asset_name ? asset.asset_name : asset.asset_number })
        )
      );
    } catch (err) {
      assignmentsHost.replaceChildren(el("p", { class: "metrics-empty", text: err.message || "Could not load the Weibull schedule register." }));
    }
  }

  assetInput.addEventListener("change", syncScheduleToAsset);
  assetInput.addEventListener("input", syncScheduleToAsset);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const asset = assetInput.value.trim();
    const reason = reasonInput.value.trim();
    if (!asset) return show("Enter the Asset Number to change.", "error");
    if (!reason) return show("Say why the schedule is changing; the change record keeps the reason.", "error");
    // Read before saving: render() rebuilds the dropdown, which resets its value.
    const code = scheduleSelect.value;
    saveButton.disabled = true;
    try {
      register = await request(API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset, schedule_code: code, reason }),
      });
      render();
      const chosen = register.schedules.find((schedule) => schedule.code === code);
      reasonInput.value = "";
      syncScheduleToAsset();
      show(
        `Asset ${asset} is now on ${chosen ? chosen.name : "that schedule"}. Its saved Weibull results are flagged to be run again.`,
        "success"
      );
    } catch (err) {
      show(err.message || "Could not change the schedule.", "error");
    } finally {
      saveButton.disabled = false;
    }
  });

  load();
})();
