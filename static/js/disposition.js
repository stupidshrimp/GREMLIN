/* Disposition: every work order on an asset, dispositioned under the standard.
 *
 * Two windows side by side: the asset's work orders on the left, and the one
 * picked on the right in the disposition form (disposition_form.js), with the
 * work order's own words above the fields. Save & next moves down the list, so a
 * backlog is worked through without leaving the keyboard. Each column of the
 * list sorts and filters from its header; the server does both, so they reach
 * every row in the selection rather than only the page on screen. The same
 * records can be dispositioned offline in a workbook (Download Excel / Upload
 * Excel), which holds what the list shows, and the code lists the dropdowns
 * offer are edited here too.
 *
 * The Auto-disposition switch turns on the disposition helper
 * (services/disposition_helper.py): each work order opened is read and filled in
 * where the helper is sure enough, for the engineer to check and save. From the
 * same bar it can disposition the list's backlog on its own, as one run that can
 * be undone, and report how often it agrees with the team.
 */
(function () {
  "use strict";

  const G = window.GremlinStandard;
  const { $, el, getJson, postJson, showBanner, clearBanner, showToast, beginLoading, endLoading, busy, skeleton, day } = G;
  const API = G.API;
  const Form = window.GremlinDispositionForm;

  const state = {
    asset: null,
    scope: "all",
    search: "",
    // The column filters, { column key: [values kept] } ("" for a blank cell),
    // and the sort, { key, dir } or null for newest first.
    filters: {},
    sort: null,
    page: 0,
    data: null,
    options: null,
    selectedId: null,
    form: null,
    token: 0,
    historyToken: 0,
    // The disposition helper: whether it is on (this browser remembers), and the
    // suggestions already read for the list on screen.
    helperOn: false,
    helperToken: 0,
    suggestions: new Map(),
  };
  const HELPER_KEY = "gremlin.disposition.helper";

  const rows = () => (state.data && state.data.rows) || [];
  const currentRow = () => rows().find((row) => row.mapped_record_id === state.selectedId) || null;
  const hasFilters = () => Object.keys(state.filters).length > 0;

  // The list's columns, by the key the server sorts and filters each on, with
  // the words its sort buttons use.
  const TEXT_SORTS = ["Sort A → Z", "Sort Z → A"];
  const COLUMNS = [
    { key: "task_id", label: "Task", sorts: ["Sort 0 → 9", "Sort 9 → 0"] },
    { key: "found", label: "Found", sorts: ["Sort Oldest → Newest", "Sort Newest → Oldest"] },
    { key: "title", label: "Work order" },
    { key: "category", label: "Category" },
    { key: "sub_asset", label: "Sub-asset" },
    { key: "component", label: "Component" },
    { key: "mechanism", label: "Mech.", title: "Failure mechanism" },
    { key: "mode", label: "Mode", title: "Failure mode" },
  ];

  // What the list is showing, as the server reads it: the asset, Show, search,
  // column filters and sort.
  function listParams(extra) {
    const params = new URLSearchParams({ asset: state.asset, scope: state.scope });
    if (state.search) params.set("search", state.search);
    if (hasFilters()) params.set("filters", JSON.stringify(state.filters));
    if (state.sort) {
      params.set("sort", state.sort.key);
      params.set("dir", state.sort.dir);
    }
    Object.entries(extra || {}).forEach(([key, value]) => params.set(key, value));
    return params;
  }

  // ---- the list ---------------------------------------------------------------
  async function load(selectId) {
    if (!state.asset) return;
    const token = ++state.token;
    beginLoading("Loading work orders…");
    try {
      const data = await getJson(`${API}/dispositions?${listParams({ page: String(state.page) })}`);
      if (token !== state.token) return;
      clearBanner();
      state.data = data;
      state.options = data.options;
      state.suggestions.clear();
      state.page = data.page_index;
      $("disp-workspace").hidden = false;
      renderTable();
      const wanted = selectId !== undefined ? selectId : state.selectedId;
      const target = rows().find((row) => row.mapped_record_id === wanted) || (selectId !== undefined ? rows()[0] : null);
      if (target) selectRecord(target.mapped_record_id, { force: true });
      else clearEditor();
    } catch (err) {
      if (token === state.token) showBanner(err.message, "error");
    } finally {
      endLoading();
    }
  }

  function renderTable() {
    const body = $("disp-table").querySelector("tbody");
    body.innerHTML = "";
    const data = state.data;
    const counts = data.counts || {};
    $("disp-list-summary").textContent =
      `${counts.all || 0} work orders on ${data.asset_label || state.asset}: ${counts.not_reviewed || 0} not reviewed, ` +
      `${counts.migrated || 0} migrated from the REL categories and still to confirm` +
      (counts.auto ? `, ${counts.auto} auto-dispositioned and still to confirm.` : ".");
    const filtered = hasFilters();
    $("disp-filter-status").hidden = !filtered;
    $("disp-filter-text").textContent = filtered
      ? `${data.displayed_count} match the column filters (${Object.keys(state.filters)
        .map((key) => (COLUMNS.find((column) => column.key === key) || { label: key }).label)
        .join(", ")}).`
      : "";
    markHeader();
    if (!rows().length) {
      body.appendChild(el("tr", {}, [el("td", {
        colspan: String(COLUMNS.length),
        class: "lda-readonly",
        text: filtered ? "No work orders match the column filters." : "No work orders match.",
      })]));
    }
    rows().forEach((row) => {
      const d = row.disposition;
      body.appendChild(el("tr", {
        class: "std-row" + (row.mapped_record_id === state.selectedId ? " is-selected" : ""),
        tabindex: "0",
        "data-id": String(row.mapped_record_id),
        onclick: () => selectRecord(row.mapped_record_id),
        onkeydown: (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectRecord(row.mapped_record_id);
          }
        },
      }, [
        el("td", { class: "std-nowrap", text: String(row.task_id ?? "") }),
        el("td", { class: "std-nowrap", text: day(row.created_date || row.completed_date) }),
        el("td", { class: "std-title", text: row.task_name || row.request_title || "" }),
        el("td", {}, Form.categoryChip(d.disposition_category, d.source)),
        el("td", { class: "std-sub-asset", text: row.location_label || row.derived.location || "" }),
        el("td", { text: d.component_code || "" }),
        el("td", { text: d.failure_mechanism_code || "" }),
        el("td", { text: d.failure_mode_code || "" }),
      ]));
    });
    const pages = data.max_page_index + 1;
    $("disp-page-status").textContent = data.displayed_count
      ? `Page ${data.page_index + 1} of ${pages} · ${data.displayed_count} rows`
      : "No rows";
    $("disp-prev").disabled = data.page_index <= 0;
    $("disp-next").disabled = data.page_index >= data.max_page_index;
  }

  // ---- sorting and filtering ---------------------------------------------------
  function renderHeader() {
    const headerRow = $("disp-table").querySelector("thead tr");
    headerRow.innerHTML = "";
    COLUMNS.forEach((column) => {
      const name = column.title || column.label;
      const tool = el("button", {
        type: "button",
        class: "lda-col-tool",
        title: `Sort or filter “${name}”`,
        "aria-label": `Sort or filter ${name}`,
        "aria-haspopup": "dialog",
        text: "▾",
      });
      tool.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleMenu(column, tool);
      });
      headerRow.appendChild(el("th", { scope: "col", "data-key": column.key, title: column.title || null }, [
        el("div", { class: "lda-col-head" }, [el("span", { class: "lda-col-label", text: column.label }), tool]),
      ]));
    });
  }

  function markHeader() {
    $("disp-table").querySelectorAll("thead th").forEach((th) => {
      const key = th.dataset.key;
      const dir = state.sort && state.sort.key === key ? state.sort.dir : null;
      th.classList.toggle("is-sorted-asc", dir === "asc");
      th.classList.toggle("is-sorted-desc", dir === "desc");
      if (dir) th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
      const tool = th.querySelector(".lda-col-tool");
      if (tool) tool.classList.toggle("is-active", Boolean(state.filters[key]));
    });
  }

  // A new sort or filter reloads the list from its first page, keeping the
  // work order open when it is still on it.
  function applyView(change) {
    if (!confirmDiscard()) return;
    if (change.sort !== undefined) state.sort = change.sort;
    if (change.filters !== undefined) state.filters = change.filters;
    state.page = 0;
    state.form = null;
    load(state.selectedId);
  }

  let openMenu = null;
  function closeMenu() {
    if (!openMenu) return;
    openMenu.node.remove();
    openMenu = null;
  }

  // A column's menu: sort it either way, or keep only the values ticked. The
  // values are the server's, across every row the rest of the view keeps, so a
  // value this column's filter hides can be ticked back.
  function toggleMenu(column, anchor) {
    if (openMenu && openMenu.key === column.key) {
      closeMenu();
      return;
    }
    closeMenu();
    const name = column.title || column.label;
    const menu = el("div", { class: "lda-col-menu", role: "dialog", "aria-label": `Sort or filter ${name}` });
    openMenu = { key: column.key, node: menu };
    const labels = column.sorts || TEXT_SORTS;
    const sortButton = (dir, text) =>
      el("button", {
        type: "button",
        class: "lda-col-menu-sort" + (state.sort && state.sort.key === column.key && state.sort.dir === dir ? " is-active" : ""),
        text,
        onclick: () => {
          closeMenu();
          applyView({ sort: { key: column.key, dir } });
        },
      });
    const sorts = [sortButton("asc", labels[0]), sortButton("desc", labels[1])];
    if (state.sort && state.sort.key === column.key) {
      sorts.push(el("button", {
        type: "button",
        class: "lda-col-menu-sort",
        text: "Newest first (default)",
        onclick: () => {
          closeMenu();
          applyView({ sort: null });
        },
      }));
    }
    menu.appendChild(el("div", { class: "lda-col-menu-sorts" }, sorts));

    const search = el("input", { type: "search", class: "lda-col-menu-search", placeholder: "Search values…", "aria-label": `Search ${name} values` });
    const allBox = el("input", { type: "checkbox", checked: true });
    const list = el("div", { class: "lda-col-menu-values" }, [
      el("p", { class: "lda-hint lda-skeleton-caption" }, [el("span", { class: "lda-spinner is-small", "aria-hidden": "true" }), "Loading values…"]),
      ...skeleton("lda-skeleton-line", 4),
    ]);
    const apply = el("button", { type: "button", class: "btn-primary", text: "Apply", disabled: true });
    const clear = el("button", {
      type: "button",
      class: "btn-secondary",
      text: "Clear filter",
      disabled: !state.filters[column.key],
      onclick: () => {
        const filters = { ...state.filters };
        delete filters[column.key];
        closeMenu();
        applyView({ filters });
      },
    });
    menu.append(
      search,
      el("label", { class: "lda-col-menu-all" }, [allBox, el("span", { text: "(Select all)" })]),
      list,
      el("div", { class: "lda-col-menu-actions" }, [clear, apply])
    );
    document.body.appendChild(menu);
    const rect = anchor.getBoundingClientRect();
    let left = rect.left;
    if (left + menu.offsetWidth > window.innerWidth - 8) left = window.innerWidth - menu.offsetWidth - 8;
    menu.style.left = `${Math.round(Math.max(8, left))}px`;
    menu.style.top = `${Math.round(Math.min(rect.bottom + 4, Math.max(8, window.innerHeight - menu.offsetHeight - 8)))}px`;
    search.focus();

    getJson(`${API}/dispositions/column-values?${listParams({ column: column.key })}`)
      .then((data) => {
        if (!openMenu || openMenu.node !== menu) return;
        const active = state.filters[column.key];
        list.innerHTML = "";
        const boxes = data.values.map((item) => {
          const box = el("input", { type: "checkbox" });
          box.checked = !active || active.includes(item.value);
          const label = el("label", { class: "lda-col-menu-value" }, [
            box,
            el("span", { text: item.label, title: item.label }),
            el("span", { class: "std-col-count", text: String(item.count) }),
          ]);
          list.appendChild(label);
          return { box, label, value: item.value, search: item.label.toLowerCase() };
        });
        if (!boxes.length) list.appendChild(el("p", { class: "lda-hint", text: "No values to filter by." }));
        if (data.truncated) {
          list.appendChild(el("p", { class: "lda-hint", text: "Only the first values are listed; search the list to narrow it first." }));
        }
        const visible = () => boxes.filter((b) => b.label.style.display !== "none");
        const syncAll = () => {
          const shown = visible();
          allBox.checked = shown.length > 0 && shown.every((b) => b.box.checked);
        };
        syncAll();
        allBox.addEventListener("change", () => visible().forEach((b) => { b.box.checked = allBox.checked; }));
        boxes.forEach((b) => b.box.addEventListener("change", syncAll));
        search.addEventListener("input", () => {
          const query = search.value.trim().toLowerCase();
          boxes.forEach((b) => { b.label.style.display = b.search.includes(query) ? "" : "none"; });
          syncAll();
        });
        const commit = () => {
          const kept = boxes.filter((b) => b.box.checked).map((b) => b.value);
          const filters = { ...state.filters };
          if (kept.length === boxes.length && !data.truncated) delete filters[column.key];
          else filters[column.key] = kept;
          closeMenu();
          applyView({ filters });
        };
        apply.disabled = false;
        apply.addEventListener("click", commit);
        search.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          }
        });
      })
      .catch((err) => {
        list.innerHTML = "";
        list.appendChild(el("p", { class: "lda-hint", text: err.message }));
      });
  }

  document.addEventListener("mousedown", (event) => {
    if (!openMenu || openMenu.node.contains(event.target)) return;
    if (event.target.closest && event.target.closest(".lda-col-tool")) return;
    closeMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenu();
  });
  window.addEventListener("resize", closeMenu);
  window.addEventListener("scroll", (event) => {
    if (openMenu && !openMenu.node.contains(event.target)) closeMenu();
  }, true);

  function markSelectedRow() {
    $("disp-table").querySelectorAll("tr.std-row").forEach((tr) => {
      tr.classList.toggle("is-selected", Number(tr.dataset.id) === state.selectedId);
    });
  }

  // ---- the editor ---------------------------------------------------------------
  function isDirty() {
    return Boolean(state.form && state.form.isDirty());
  }

  function confirmDiscard() {
    if (!isDirty()) return true;
    const row = currentRow();
    return window.confirm(`Discard the unsaved changes to task ${row ? row.task_id : ""}?`);
  }

  function selectRecord(id, { force } = {}) {
    if (!force && id === state.selectedId) return;
    if (!force && !confirmDiscard()) return;
    // A reload after somebody else's save keeps what is being typed here.
    if (force && id === state.selectedId && isDirty()) return;
    state.selectedId = id;
    const row = currentRow();
    if (!row) return clearEditor();
    markSelectedRow();
    const list = rows();
    const index = list.findIndex((item) => item.mapped_record_id === id);
    const nextId = index >= 0 && index + 1 < list.length ? list[index + 1].mapped_record_id : null;
    state.form = Form.create({
      row,
      options: state.options,
      onSave: (payload) => save(payload, row.mapped_record_id),
      onSaveNext: (payload) => save(payload, nextId === null ? row.mapped_record_id : nextId),
    });
    const body = $("disp-editor-body");
    body.innerHTML = "";
    body.appendChild(state.form.node);
    loadHistory(row.mapped_record_id, state.form);
    if (state.helperOn) loadSuggestion(row, state.form);
  }

  // ---- the disposition helper ----------------------------------------------------
  // The helper's read of one work order, filled into its form when it is still
  // the one open. Read once per list: a save or a reload reads again.
  async function loadSuggestion(row, form) {
    const token = ++state.helperToken;
    const cached = state.suggestions.get(row.mapped_record_id);
    if (cached) {
      form.setSuggestion(cached, { prefill: true });
      return;
    }
    form.setHelperLoading();
    try {
      const suggestion = await getJson(`${API}/dispositions/suggest?mapped_record_id=${row.mapped_record_id}`);
      state.suggestions.set(row.mapped_record_id, suggestion);
      if (token === state.helperToken && state.form === form) form.setSuggestion(suggestion, { prefill: true });
    } catch (err) {
      if (token === state.helperToken && state.form === form) form.setHelperError(err.message);
    }
  }

  function renderHelperStatus() {
    $("disp-helper-toggle").checked = state.helperOn;
    $("disp-helper-status").textContent = state.helperOn
      ? "On. Each work order you open is read and filled in where the helper is at least 50% sure; the % beside a " +
        "field is how sure it is. Check the fields, then save: nothing is saved until you do."
      : "Off. Turn it on and the disposition helper reads each work order you open and fills in its disposition " +
        "from the code lists, for you to check and save.";
  }

  // Whether meaning matching is on, and how far it has read the work orders;
  // asked again every 15 s while it is still reading them.
  let meaningTimer = null;
  async function loadHelperStatus() {
    clearTimeout(meaningTimer);
    const line = $("disp-helper-meaning");
    if (!state.helperOn) {
      line.hidden = true;
      return;
    }
    try {
      const status = await getJson(`${API}/disposition-helper/status`);
      const meaning = status.meaning || {};
      line.hidden = false;
      line.textContent = meaningText(meaning);
      if (meaning.enabled && !meaning.complete && !meaning.error) meaningTimer = setTimeout(loadHelperStatus, 15000);
    } catch (err) {
      line.hidden = true;
    }
  }

  function meaningText(meaning) {
    if (!meaning.enabled) return `Meaning matching is off, so work orders are matched by their words: ${meaning.reason || "not set up."}`;
    if (meaning.error) return `Meaning matching stopped reading the work orders (${meaning.error}); the rest are matched by their words.`;
    if (!meaning.complete) {
      return `Meaning matching (${meaning.model}) is reading the work orders: ${(meaning.done || 0).toLocaleString()} of ` +
        `${(meaning.total || 0).toLocaleString()} so far. The rest are matched by their words until it has.`;
    }
    return `Meaning matching is on (${meaning.model}): work orders that say the same thing in other words are found too.`;
  }

  function setHelper(on) {
    state.helperOn = on;
    try {
      window.localStorage.setItem(HELPER_KEY, on ? "on" : "off");
    } catch (err) {
      /* remembered for this visit only */
    }
    renderHelperStatus();
    loadHelperStatus();
    const row = currentRow();
    if (!state.form || !row) return;
    if (on) loadSuggestion(row, state.form);
    else state.form.clearSuggestion();
  }

  const helperState = { preview: null, busy: false };

  function helperRequest() {
    return {
      asset: state.asset,
      search: state.search,
      filters: state.filters,
      min_confidence: Number($("disp-helper-threshold").value) / 100,
    };
  }

  function openHelperDialog() {
    if (!state.asset) {
      showBanner("Select an Asset Number first.", "error");
      return;
    }
    helperState.preview = null;
    $("disp-helper-preview-body").innerHTML = "";
    $("disp-helper-preview-body").appendChild(el("p", {
      class: "lda-hint",
      text: "Preview shows what a run would do, work order by work order, before anything is saved.",
    }));
    $("disp-helper-apply").disabled = true;
    $("disp-helper-apply").textContent = "Auto-disposition";
    openDialog($("disp-helper-dialog"));
    loadRuns();
  }

  const pct = (value) => `${Math.round((Number(value) || 0) * 100)}%`;
  const CATEGORY_LABELS = {
    FAILURE: "failures", PM_RESET: "PM resets", EXTERNAL: "external", NON_FAILURE: "non-failures",
  };

  async function previewBatch() {
    if (helperState.busy) return;
    helperState.busy = true;
    const body = $("disp-helper-preview-body");
    const end = busy(body, "Reading the backlog…");
    $("disp-helper-apply").disabled = true;
    try {
      const data = await postJson(`${API}/disposition-helper/preview`, helperRequest());
      helperState.preview = data;
      renderPreview(data);
    } catch (err) {
      body.innerHTML = "";
      if (!err.toastShown) body.appendChild(el("div", { class: "lda-banner is-error", role: "alert", text: err.message }));
    } finally {
      end();
      helperState.busy = false;
    }
  }

  function renderPreview(data) {
    const body = $("disp-helper-preview-body");
    body.innerHTML = "";
    const by = Object.entries(data.by_category || {})
      .map(([code, count]) => `${count} ${CATEGORY_LABELS[code] || code}`).join(", ");
    body.appendChild(el("p", { class: "std-helper-preview-summary" }, [
      el("strong", { text: `${data.would_apply} of ${data.considered}` }),
      ` not-reviewed work order${data.considered === 1 ? "" : "s"} reach ${pct(data.threshold)}` + (by ? `: ${by}.` : "."),
      data.limited ? ` Only the newest ${data.considered} of ${data.backlog_total} are read in one run; run it again for the rest.` : "",
    ]));
    if (!data.calibrated) {
      body.appendChild(el("div", {
        class: "lda-banner is-error",
        role: "alert",
        text: "The helper has not yet measured itself on enough work orders dispositioned by engineers, so it may " +
          "not disposition on its own. Use the Auto-disposition switch to have it suggest instead.",
      }));
    }
    // How sure it is across the backlog.
    const most = Math.max(1, ...data.histogram.map((band) => band.count));
    body.appendChild(el("div", { class: "std-helper-histogram", role: "img", "aria-label": "How sure the helper is across the backlog" },
      data.histogram.map((band) => el("div", {
        class: "std-helper-bar-col" + (band.from >= data.threshold || (band.to > data.threshold && band.from < data.threshold) ? " is-applied" : ""),
        title: `${band.count} work order${band.count === 1 ? "" : "s"} ${pct(band.from)}–${pct(band.to)} sure`,
      }, [
        el("span", { class: "std-helper-bar-count", text: String(band.count) }),
        el("span", { class: "std-helper-bar-fill", style: `height:${Math.round((band.count / most) * 100)}%` }),
        el("span", { class: "std-helper-bar-label", text: `${pct(band.from)}+` }),
      ]))));
    const table = el("table", { class: "lda-table std-helper-table" }, [
      el("thead", {}, [el("tr", {}, ["Task", "Work order", "Category", "Sub-asset", "Comp.", "Mech.", "Mode", "Action", "Sure", "Least sure of"]
        .map((label) => el("th", { scope: "col", text: label })))]),
      el("tbody", {}, data.rows.slice(0, 300).map((row) => el("tr", { class: row.will_apply ? "is-applied" : "is-skipped" }, [
        el("td", { class: "std-nowrap", text: String(row.task_id ?? "") }),
        el("td", { class: "std-title", text: row.title }),
        el("td", {}, row.disposition_category ? Form.categoryChip(row.disposition_category) : []),
        el("td", { text: row.sub_asset_name || "" }),
        el("td", { text: row.component_code || "" }),
        el("td", { text: row.failure_mechanism_code || "" }),
        el("td", { text: row.failure_mode_code || "" }),
        el("td", { text: row.action_code || "" }),
        el("td", { class: "std-nowrap" }, [el("span", { class: `std-helper-chip ${row.confidence >= 0.8 ? "is-high" : row.confidence >= 0.5 ? "is-mid" : "is-low"}`, text: pct(row.confidence) })]),
        el("td", { class: "std-nowrap", text: row.possible_duplicate ? "possible duplicate" : `${row.weakest.label} ${pct(row.weakest.confidence)}` }),
      ]))),
    ]);
    body.appendChild(el("div", { class: "lda-table-scroll std-helper-table-scroll" }, [table]));
    if (data.rows.length > 300) body.appendChild(el("p", { class: "lda-hint", text: `The first 300 of ${data.rows.length} are listed.` }));
    const apply = $("disp-helper-apply");
    apply.disabled = !data.calibrated || !data.would_apply || !G.CAN_EDIT;
    apply.textContent = `Auto-disposition ${data.would_apply} work order${data.would_apply === 1 ? "" : "s"}`;
  }

  async function applyBatch() {
    const preview = helperState.preview;
    if (!preview || helperState.busy) return;
    if (!window.confirm(
      `Save ${preview.would_apply} disposition${preview.would_apply === 1 ? "" : "s"} marked Auto, each for an ` +
      "engineer to confirm? The run can be undone from this dialog."
    )) return;
    if (!confirmDiscard()) return;
    helperState.busy = true;
    beginLoading("Auto-dispositioning…");
    try {
      const result = await postJson(`${API}/disposition-helper/apply`, helperRequest());
      const skipped = (result.skipped || []).length;
      showToast(
        `Auto-dispositioned ${result.applied} work order${result.applied === 1 ? "" : "s"}` +
          (skipped ? `; ${skipped} left as they were.` : ".") + " Show “Auto-dispositioned” to confirm them.",
        "success"
      );
      const body = $("disp-helper-preview-body");
      body.innerHTML = "";
      body.appendChild(el("div", { class: "lda-banner is-success", role: "status" }, [
        `Saved ${result.applied} disposition${result.applied === 1 ? "" : "s"} marked Auto.`,
        ...(result.skipped || []).slice(0, 10).map((item) => el("div", { class: "lda-hint", text: `Task ${item.task_id ?? item.mapped_record_id}: ${item.reason}` })),
      ]));
      $("disp-helper-apply").disabled = true;
      helperState.preview = null;
      state.form = null;
      await load(state.selectedId);
      loadRuns();
    } catch (err) {
      if (!err.toastShown) showToast(err.message, "error");
    } finally {
      endLoading();
      helperState.busy = false;
    }
  }

  async function loadRuns() {
    const target = $("disp-helper-runs");
    target.innerHTML = "";
    if (!state.asset) return;
    const end = busy(target, "Loading earlier runs…");
    try {
      const data = await getJson(`${API}/disposition-helper/runs?asset=${encodeURIComponent(state.asset)}`);
      target.innerHTML = "";
      if (!data.runs.length) {
        target.appendChild(el("p", { class: "lda-hint", text: "No runs on this asset yet." }));
        return;
      }
      target.appendChild(el("ul", { class: "std-helper-runs" }, data.runs.map((run) => el("li", {}, [
        el("span", {}, [
          el("strong", { text: `${day(run.run_at)} ${run.run_by || ""}`.trim() }),
          ` · ${run.applied} saved at ${pct(run.min_confidence)} or more` +
            (run.undone_at ? ` · undone ${day(run.undone_at)}${run.undone_by ? ` by ${run.undone_by}` : ""}` : ` · ${run.still_auto} still to confirm`),
        ]),
        !run.undone_at && run.still_auto && G.CAN_EDIT ? el("button", {
          type: "button",
          class: "btn-secondary",
          text: "Undo",
          title: "Put back the disposition each work order had before this run. Work orders an engineer has saved since keep theirs.",
          onclick: () => undoRun(run),
        }) : null,
      ]))));
    } catch (err) {
      target.innerHTML = "";
      target.appendChild(el("p", { class: "lda-hint", text: err.message }));
    } finally {
      end();
    }
  }

  async function undoRun(run) {
    if (!window.confirm(
      `Undo the run of ${day(run.run_at)}? Its ${run.still_auto} unconfirmed disposition${run.still_auto === 1 ? "" : "s"} ` +
      "go back to what they were before; any an engineer has saved since are kept."
    )) return;
    if (!confirmDiscard()) return;
    beginLoading("Undoing the run…");
    try {
      const result = await postJson(`${API}/disposition-helper/undo`, { helper_run_id: run.helper_run_id });
      showToast(
        `Undid ${result.undone} disposition${result.undone === 1 ? "" : "s"}` +
          (result.kept ? `; ${result.kept} confirmed since were kept.` : "."),
        "success"
      );
      state.form = null;
      await load(state.selectedId);
      loadRuns();
    } catch (err) {
      if (!err.toastShown) showToast(err.message, "error");
    } finally {
      endLoading();
    }
  }

  async function openAccuracy() {
    openDialog($("disp-helper-accuracy-dialog"));
    const body = $("disp-helper-accuracy-body");
    body.innerHTML = "";
    const end = busy(body, "Replaying dispositioned work orders…");
    try {
      const query = state.asset ? `?asset=${encodeURIComponent(state.asset)}` : "";
      renderAccuracy(await getJson(`${API}/disposition-helper/accuracy${query}`));
    } catch (err) {
      body.innerHTML = "";
      body.appendChild(el("p", { class: "lda-hint", text: err.message }));
    } finally {
      end();
    }
  }

  function renderAccuracy(data) {
    const body = $("disp-helper-accuracy-body");
    body.innerHTML = "";
    const where = data.asset_number ? `on ${data.asset_number}` : "across the plant";
    if (!data.replayed) {
      body.appendChild(el("p", {
        class: "lda-hint",
        text: `No work orders ${where} have been dispositioned by an engineer yet, so there is nothing to measure the helper against.`,
      }));
    } else {
      body.appendChild(el("p", {}, [
        `Replayed ${data.replayed} work order${data.replayed === 1 ? "" : "s"} ${where} that engineers dispositioned; ` +
          `the helper learns from every decision engineers have made (${data.learnt_from}).`,
        data.enough ? "" : " That is too few to say much yet: the figures will steady as more are dispositioned.",
      ]));
      if (data.meaning) body.appendChild(el("p", { class: "lda-hint", text: meaningText(data.meaning) }));
      const head = ["", "Right overall", ...data.thresholds.map((t) => `At ${pct(t.threshold)}+`)];
      body.appendChild(el("h3", { class: "std-helper-subhead", text: "Whole dispositions" }));
      body.appendChild(el("p", { class: "lda-hint", text: "How many work orders a batch run at each setting would disposition, and how many of those it gets entirely right (every field)." }));
      // With meaning matching on, the same work orders matched by their words
      // alone sit beside each figure: what the model adds on this plant's records.
      const plain = data.without_meaning;
      const plainAt = (threshold) => (plain ? plain.thresholds.find((t) => t.threshold === threshold) : null);
      const plainCell = (value) => el("td", { class: "std-helper-plain", text: value === null || value === undefined ? "—" : value });
      body.appendChild(el("div", { class: "lda-table-scroll" }, [el("table", { class: "lda-table std-helper-table" }, [
        el("thead", {}, [el("tr", {}, [
          "At least", "Would disposition", "Entirely right", "Category right",
          ...(plain ? ["Words only: would disposition", "Words only: entirely right"] : []),
        ].map((t) => el("th", { scope: "col", text: t })))]),
        el("tbody", {}, data.thresholds.map((t) => el("tr", {}, [
          el("td", { text: pct(t.threshold) }),
          el("td", { text: `${t.count} (${pct(t.coverage)})` }),
          el("td", { text: t.accuracy === null ? "—" : pct(t.accuracy) }),
          el("td", { text: t.category_right === null ? "—" : pct(t.category_right) }),
          ...(plain ? [
            plainCell(plainAt(t.threshold) ? `${plainAt(t.threshold).count} (${pct(plainAt(t.threshold).coverage)})` : null),
            plainCell(plainAt(t.threshold) && plainAt(t.threshold).accuracy !== null ? pct(plainAt(t.threshold).accuracy) : null),
          ] : []),
        ]))),
      ])]));
      body.appendChild(el("h3", { class: "std-helper-subhead", text: "Field by field" }));
      body.appendChild(el("p", { class: "lda-hint", text: "Right overall, then, at each confidence, how often it is that sure and how often it is then right." }));
      const plainField = (field) => (plain ? plain.fields.find((f) => f.field === field) : null);
      body.appendChild(el("div", { class: "lda-table-scroll" }, [el("table", { class: "lda-table std-helper-table" }, [
        el("thead", {}, [el("tr", {}, [...head.slice(0, 2), ...(plain ? ["Words only"] : []), ...head.slice(2)]
          .map((t) => el("th", { scope: "col", text: t })))]),
        el("tbody", {}, data.fields.map((f) => el("tr", {}, [
          el("th", { scope: "row", text: f.label }),
          el("td", { text: pct(f.accuracy) }),
          ...(plain ? [plainCell(plainField(f.field) ? pct(plainField(f.field).accuracy) : null)] : []),
          ...f.at.map((a) => el("td", { title: `${a.count} of ${f.judged} this sure` }, [
            a.accuracy === null ? "—" : `${pct(a.accuracy)} `,
            el("span", { class: "lda-hint", text: `of ${pct(a.coverage)}` }),
          ])),
        ]))),
      ])]));
      if (data.mistakes.length) {
        body.appendChild(el("h3", { class: "std-helper-subhead", text: "Its commonest mistakes" }));
        body.appendChild(el("ul", { class: "std-helper-mistakes" }, data.mistakes.map((m) => el("li", {
          text: `${m.label}: suggested ${m.suggested} where the engineer chose ${m.decided} (${m.count}×)`,
        }))));
      }
    }
    const use = data.in_use;
    body.appendChild(el("h3", { class: "std-helper-subhead", text: "In use" }));
    const kept = use.kept.filter((k) => k.of);
    body.appendChild(el("p", {}, [
      use.assisted_saves
        ? `${use.assisted_saves} work order${use.assisted_saves === 1 ? " was" : "s were"} saved with a suggestion open. Kept as suggested: ` +
          kept.map((k) => `${k.label.toLowerCase()} ${pct(k.rate)}`).join(", ") + "."
        : "No work order has been saved with a suggestion open yet.",
      " ",
      use.auto_reviewed
        ? `Of ${use.auto_reviewed} auto-disposition${use.auto_reviewed === 1 ? "" : "s"} an engineer has since saved, ` +
          `${use.auto_confirmed_unchanged} were confirmed unchanged` +
          (use.auto_changed_fields.length ? `; the field most often changed was ${use.auto_changed_fields[0].label.toLowerCase()}.` : ".")
        : "",
      use.auto_pending ? ` ${use.auto_pending} auto-disposition${use.auto_pending === 1 ? " is" : "s are"} still to confirm.` : "",
    ]));
  }

  function clearEditor() {
    state.selectedId = null;
    state.form = null;
    markSelectedRow();
    const body = $("disp-editor-body");
    body.innerHTML = "";
    body.appendChild(el("p", { class: "lda-hint", text: "Choose a work order from the list." }));
  }

  async function loadHistory(id, form) {
    const token = ++state.historyToken;
    try {
      const data = await getJson(`${API}/dispositions/record?mapped_record_id=${id}&history_only=1`);
      if (token === state.historyToken) form.setHistory(data.history || []);
    } catch (err) {
      /* the history is a convenience; the editor works without it */
    }
  }

  // A save hands back the record as the list holds it, so only that row changes:
  // the list, its counts and the editor options stay as they are, rather than the
  // whole page being read again. The next record opens from the list in memory.
  async function save(payload, nextId) {
    const row = currentRow();
    beginLoading("Saving…");
    try {
      const result = await postJson(`${API}/dispositions/save`, { dispositions: [payload] });
      showToast(`Task ${row ? row.task_id : payload.mapped_record_id} saved.`, "success");
      state.form = null;
      const saved = (result.records || []).find((record) => record.mapped_record_id === payload.mapped_record_id);
      if (!saved || !replaceRow(saved)) {
        await load(nextId);
        return;
      }
      renderTable();
      selectRecord(nextId === null ? saved.mapped_record_id : nextId, { force: true });
    } finally {
      endLoading();
    }
  }

  // Put a saved record in its row and keep the counts above the list true.
  function replaceRow(record) {
    const list = rows();
    const index = list.findIndex((item) => item.mapped_record_id === record.mapped_record_id);
    if (index < 0) return false;
    const before = list[index].disposition;
    const after = record.disposition;
    const counts = state.data.counts || {};
    const notReviewed = (d) => (d.disposition_category === "NOT_REVIEWED" ? 1 : 0);
    const migrated = (d) => (d.source === "MIGRATION" ? 1 : 0);
    const auto = (d) => (d.source === "AUTO" ? 1 : 0);
    counts.not_reviewed = Math.max(0, (counts.not_reviewed || 0) + notReviewed(after) - notReviewed(before));
    counts.migrated = Math.max(0, (counts.migrated || 0) + migrated(after) - migrated(before));
    counts.auto = Math.max(0, (counts.auto || 0) + auto(after) - auto(before));
    state.suggestions.delete(record.mapped_record_id);
    list[index] = record;
    return true;
  }

  // ---- Excel ----------------------------------------------------------------------
  async function downloadExcel() {
    if (!state.asset) {
      showBanner("Select an Asset Number first.", "error");
      return;
    }
    beginLoading("Building the workbook…");
    try {
      const name = await G.download(`${API}/dispositions/excel?${listParams()}`, "dispositions.xlsx");
      showToast(`Downloaded ${name}.`, "success");
    } catch (err) {
      showBanner(err.message, "error");
    } finally {
      endLoading();
    }
  }

  // What an upload did, a line for each thing worth knowing: what it saved,
  // what it left as GREMLIN has it, and any column it didn't read.
  function uploadSummary(outcome) {
    const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;
    const lines = [
      `Saved ${plural(outcome.saved, "disposition", "dispositions")} from the workbook; ` +
        `${plural(outcome.unchanged, "row was", "rows were")} already as saved.`,
    ];
    const kept = outcome.kept_newer || [];
    if (kept.length) {
      const shown = kept.slice(0, 8).join(", ") + (kept.length > 8 ? ` and ${kept.length - 8} more` : "");
      lines.push(
        `${plural(kept.length, "row was", "rows were")} left as GREMLIN has ${kept.length === 1 ? "it" : "them"}: ` +
          `someone changed ${kept.length === 1 ? "it" : "them"} here after the workbook was downloaded, and the ` +
          `workbook didn't (task ${shown}).`
      );
    }
    if ((outcome.columns_missing || []).length) {
      lines.push(`Not in the workbook, so kept as saved: ${outcome.columns_missing.join(", ")}.`);
    }
    if ((outcome.columns_not_read || []).length) {
      lines.push(`Columns not read (not GREMLIN's): ${outcome.columns_not_read.join(", ")}.`);
    }
    return lines.join("\n");
  }

  async function uploadExcel(file) {
    if (!file || !state.asset) return;
    if (!confirmDiscard()) return;
    const form = new FormData();
    form.append("file", file);
    form.append("csrf_token", document.querySelector('meta[name="gremlin-csrf-token"]')?.content || "");
    beginLoading("Saving the workbook's dispositions…");
    try {
      const outcome = await G.requestJson(`${API}/dispositions/excel?asset=${encodeURIComponent(state.asset)}`, {
        method: "POST",
        body: form,
        headers: { Accept: "application/json" },
      });
      state.form = null;
      await load(state.selectedId);
      // After the list is read again, which clears the banner, unless reading
      // it failed and the banner says so.
      if ($("lda-status").hidden || !$("lda-status").classList.contains("is-error")) {
        showBanner(uploadSummary(outcome), "success");
      }
    } catch (err) {
      if (!err.toastShown) showBanner(err.message, "error");
    } finally {
      endLoading();
      $("disp-excel-file").value = "";
    }
  }

  function openDialog(dialog) {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  // ---- code lists -------------------------------------------------------------------
  // Each code reads as a row, its whole description under its name; Edit (or a
  // click on the row) opens that one code's form under it, and Add code opens a
  // blank one at the top. `editing` is the code whose form is open.
  const NEW_CODE = "\u0000new";
  const codeState = { lists: null, active: "COMPONENT", editing: null, form: null, query: "", hideRetired: false };
  // The order a disposition is filled in, not the alphabetical order the JSON
  // arrives in.
  const CODE_LIST_ORDER = ["COMPONENT", "FAILURE_MECHANISM", "FAILURE_MODE", "FAILURE_CAUSE", "ACTION"];

  async function openCodeLists() {
    openDialog($("disp-code-dialog"));
    codeState.editing = null;
    codeState.form = null;
    // The lists are read afresh each time the dialog opens: the ones from last
    // time dim meanwhile, and the first time placeholders hold their place.
    if (!codeState.lists) {
      const tabs = $("disp-code-tabs");
      tabs.innerHTML = "";
      skeleton("lda-skeleton-tab", CODE_LIST_ORDER.length).forEach((node) => tabs.appendChild(node));
      const table = $("disp-code-table");
      table.innerHTML = "";
      table.appendChild(el("tbody", {}, Array.from({ length: 6 }, () => el("tr", {}, [
        el("td", { colspan: "4" }, skeleton("lda-skeleton-line")),
      ]))));
    }
    const end = busy($("disp-code-body"), "Loading the code lists…");
    try {
      const data = await getJson(`${API}/code-lists`);
      codeState.lists = data.code_lists;
      renderCodeLists();
    } catch (err) {
      showBanner(err.message, "error");
    } finally {
      end();
    }
  }

  function renderCodeLists() {
    renderCodeTabs();
    renderCodeTable();
  }

  function renderCodeTabs() {
    const tabs = $("disp-code-tabs");
    tabs.innerHTML = "";
    CODE_LIST_ORDER.filter((key) => codeState.lists[key]).forEach((key) => {
      const list = codeState.lists[key];
      const active = list.codes.filter((entry) => entry.is_active).length;
      const retired = list.codes.length - active;
      tabs.appendChild(el("button", {
        type: "button",
        role: "tab",
        "aria-selected": String(key === codeState.active),
        class: "cl-tab" + (key === codeState.active ? " is-active" : ""),
        title: `${active} active${retired ? `, ${retired} retired` : ""}`,
        onclick: () => {
          if (key === codeState.active || !mayLeaveCodeForm()) return;
          codeState.active = key;
          closeCodeForm();
          renderCodeLists();
        },
      }, [list.label, el("span", { class: "cl-tab-count", text: String(active) })]));
    });
  }

  function codeCauses() {
    return (codeState.lists.FAILURE_CAUSE && codeState.lists.FAILURE_CAUSE.codes) || [];
  }

  // The columns a list's rows show: code, name over description, then only what
  // that list's codes can hold.
  function codeColumns(listKey, list) {
    const none = () => el("span", { class: "cl-none", text: "—" });
    const columns = [
      { label: "Code", className: "cl-code", cell: (entry) => [el("span", { class: "cl-code-pill", text: entry.code })] },
      {
        label: "Name and description",
        className: "cl-main",
        cell: (entry) => [
          el("div", { class: "cl-name" }, [
            entry.name,
            entry.is_active ? null : el("span", { class: "cl-chip is-retired", text: "Retired" }),
          ]),
          entry.description ? el("div", { class: "cl-desc", text: entry.description }) : null,
        ],
      },
    ];
    if (list.codes.some((entry) => entry.iso_ref)) {
      columns.push({ label: "ISO ref", className: "cl-meta", cell: (entry) => [entry.iso_ref ? entry.iso_ref : none()] });
    }
    if (listKey === "FAILURE_MECHANISM") {
      const causeNames = Object.fromEntries(codeCauses().map((cause) => [cause.code, cause.name]));
      columns.push({
        label: "Default cause",
        className: "cl-fit",
        cell: (entry) => {
          if (!entry.default_cause_code) return [none()];
          const name = causeNames[entry.default_cause_code];
          return [el("span", { class: "cl-chip", title: entry.default_cause_code, text: name || entry.default_cause_code })];
        },
      });
    }
    if (listKey === "ACTION") {
      columns.push({
        label: "Renews item",
        className: "cl-fit",
        cell: (entry) => [entry.renews_item ? el("span", { class: "cl-chip is-renews", text: "Renews" }) : none()],
      });
    }
    if (list.semantics.length) {
      // A word or two on the chip; the full wording is its tooltip.
      const meanings = Object.fromEntries(list.semantics.map((option) => [option.code, option]));
      columns.push({
        label: "Means",
        className: "cl-fit",
        cell: (entry) => {
          const meaning = meanings[entry.semantic];
          if (!entry.semantic) return [none()];
          return [el("span", {
            class: "cl-chip is-meaning",
            title: meaning ? meaning.label : entry.semantic,
            text: meaning ? meaning.short : entry.semantic,
          })];
        },
      });
    }
    columns.push({ label: "Order", className: "cl-meta cl-num", cell: (entry) => [String(entry.sort_order)] });
    columns.push({
      label: "",
      className: "cl-actions",
      cell: (entry) => [el("button", {
        type: "button",
        class: "cl-edit-btn",
        text: entry.code === codeState.editing ? "Close" : "Edit",
        "aria-label": `${entry.code === codeState.editing ? "Close" : "Edit"} ${list.label.toLowerCase()} ${entry.code}`,
        onclick: () => openCodeForm(entry.code === codeState.editing ? null : entry.code),
      })],
    });
    return columns;
  }

  function renderCodeTable() {
    const listKey = codeState.active;
    const list = codeState.lists[listKey];
    const columns = codeColumns(listKey, list);
    const query = codeState.query.trim().toLowerCase();
    // The code being edited stays in view whatever the search or the switch say.
    const shown = list.codes.filter((entry) => entry.code === codeState.editing || (
      (!codeState.hideRetired || entry.is_active) &&
      (!query || [entry.code, entry.name, entry.description, entry.iso_ref].some((text) => (text || "").toLowerCase().includes(query)))
    ));
    const table = $("disp-code-table");
    table.innerHTML = "";
    table.appendChild(el("thead", {}, [el("tr", {}, columns.map((column) => el("th", {
      scope: "col",
      class: column.className || null,
    }, [column.label || el("span", { class: "visually-hidden", text: "Edit" })])))]));
    const body = el("tbody");
    if (codeState.editing === NEW_CODE) body.appendChild(codeFormRow(listKey, list, null, columns.length));
    shown.forEach((entry) => {
      const editing = entry.code === codeState.editing;
      body.appendChild(el("tr", {
        class: "cl-row is-clickable" + (entry.is_active ? "" : " is-retired") + (editing ? " is-editing" : ""),
        onclick: (event) => {
          // A click on the row opens it, but not one that selected text or hit a control.
          if (event.target.closest("button, a, input, select, textarea")) return;
          if (String(window.getSelection() || "")) return;
          openCodeForm(editing ? null : entry.code);
        },
      }, columns.map((column) => el("td", { class: column.className || null }, column.cell(entry)))));
      if (editing) body.appendChild(codeFormRow(listKey, list, entry, columns.length));
    });
    if (!shown.length && codeState.editing !== NEW_CODE) {
      body.appendChild(el("tr", {}, [el("td", {
        colspan: String(columns.length),
        class: "cl-empty",
        text: query || codeState.hideRetired
          ? `No ${list.label.toLowerCase()} code matches${query ? ` “${codeState.query.trim()}”` : ""}${codeState.hideRetired ? " among the active ones" : ""}.`
          : "This list has no codes yet. Add the first one.",
      })]));
    }
    table.appendChild(body);
  }

  function closeCodeForm() {
    codeState.editing = null;
    codeState.form = null;
  }

  // Whether the open form may be closed: asked first when it holds changes not saved.
  function mayLeaveCodeForm() {
    const open = codeState.form;
    if (!open || !open.changed()) return true;
    return window.confirm(`Discard your changes to ${open.label}?`);
  }

  // Opens one code's form (null closes it). Leaving a form with changes asks
  // first, except from its own Cancel (`discard`).
  function openCodeForm(code, { discard = false } = {}) {
    if (code !== null && code === codeState.editing) {
      const open = codeState.form && codeState.form.row.querySelector("input:not([readonly])");
      if (open) open.focus();
      return;
    }
    if (!discard && !mayLeaveCodeForm()) return;
    closeCodeForm();
    codeState.editing = code;
    renderCodeTable();
    if (code === null) return;
    const form = codeState.form && codeState.form.row;
    if (!form) return;
    form.querySelectorAll("textarea").forEach(fitText);
    // The code's own row comes to the top, under the column headings, with its form below.
    const anchor = code === NEW_CODE ? form : form.previousElementSibling;
    anchor.scrollIntoView({ block: "start", behavior: "smooth" });
    const first = form.querySelector(code === NEW_CODE ? "input" : "input:not([readonly])");
    if (first) first.focus({ preventScroll: true });
  }

  // A description box as tall as what it holds, so a long one reads without scrolling.
  function fitText(area) {
    area.style.height = "auto";
    area.style.height = `${area.scrollHeight + 2}px`;
  }

  // One code's form. It is kept while it is open, so searching, the retired
  // switch or a redraw never loses what was typed in it.
  function codeFormRow(listKey, list, entry, span) {
    const key = `${listKey}:${entry ? entry.code : NEW_CODE}`;
    if (codeState.form && codeState.form.key === key) {
      codeState.form.row.firstChild.colSpan = span;
      return codeState.form.row;
    }
    const isNew = !entry;
    const last = list.codes.length ? list.codes[list.codes.length - 1].sort_order : 0;
    const values = entry || { sort_order: (last || 0) + 10, is_active: true };
    const input = (attrs) => el("input", { class: "lda-input", ...attrs });
    const inputs = {
      code: input({
        type: "text",
        value: values.code || "",
        readonly: !isNew,
        required: true,
        maxlength: "32",
        placeholder: isNew ? "e.g. Filter" : null,
        title: isNew ? "1-32 letters, digits, '-', '_' or '.'. It can't be changed once added." : "A code can't be renamed once added.",
      }),
      name: input({ type: "text", value: values.name || "", required: true }),
      description: el("textarea", {
        class: "lda-textarea",
        rows: "3",
        text: values.description || "",
        oninput: (event) => fitText(event.target),
      }),
      iso_ref: input({ type: "text", value: values.iso_ref || "", placeholder: "e.g. 5.2" }),
      default_cause_code: el("select", { class: "lda-select" }, [
        el("option", { value: "", text: "None" }),
        ...codeCauses().map((cause) => el("option", { value: cause.code, text: `${cause.name} (${cause.code})` })),
      ]),
      renews_item: el("input", { type: "checkbox", checked: Boolean(values.renews_item) }),
      semantic: el("select", { class: "lda-select" }, [
        el("option", { value: "", text: "Nothing special" }),
        ...list.semantics.map((option) => el("option", { value: option.code, text: option.label })),
      ]),
      sort_order: input({ type: "number", step: "1", value: String(values.sort_order ?? "") }),
      is_active: el("input", { type: "checkbox", checked: Boolean(values.is_active) }),
    };
    inputs.default_cause_code.value = values.default_cause_code || "";
    inputs.semantic.value = values.semantic || "";
    const field = (label, control, size) => el("label", { class: "cl-field" + (size ? ` is-${size}` : "") }, [
      el("span", { text: label }),
      control,
    ]);
    const toggle = (label, control, text) => el("div", { class: "cl-field" }, [
      el("span", { text: label }),
      el("label", { class: "cl-switch" }, [control, text]),
    ]);
    const fields = [
      field("Code", inputs.code),
      field("Name", inputs.name, "wide"),
      field("Description", inputs.description, "full"),
      field("ISO ref", inputs.iso_ref),
    ];
    if (listKey === "FAILURE_MECHANISM") fields.push(field("Default cause", inputs.default_cause_code));
    if (listKey === "ACTION") fields.push(toggle("Renews item", inputs.renews_item, "Makes the item as good as new"));
    if (list.semantics.length) fields.push(field("Means", inputs.semantic));
    fields.push(field("Order", inputs.sort_order));
    fields.push(toggle("Status", inputs.is_active, "Active, offered on the dropdowns"));

    const submit = el("button", { type: "submit", class: "btn-primary", text: isNew ? "Add code" : "Save changes" });
    const remove = isNew ? null : el("button", {
      type: "button",
      class: "cl-delete-btn",
      text: "Delete code",
      title: "Delete this code. A code in use can only be retired.",
      onclick: async () => {
        if (!window.confirm(`Delete ${list.label.toLowerCase()} code ${entry.code}? This cannot be undone.`)) return;
        try {
          const data = await postJson(`${API}/code-lists/delete`, { code_list: listKey, code: entry.code });
          codeState.lists = data.code_lists;
          closeCodeForm();
          renderCodeLists();
          applyCodeLists(data.code_lists);
          showToast(`${list.label} ${entry.code} deleted.`, "success");
        } catch (err) {
          if (!err.toastShown) showToast(err.message, "error");
        }
      },
    });
    const form = el("form", {
      class: "cl-form",
      novalidate: true,
      onsubmit: async (event) => {
        event.preventDefault();
        const code = inputs.code.value.trim();
        submit.disabled = true;
        try {
          const data = await postJson(`${API}/code-lists`, {
            code_list: listKey,
            code,
            name: inputs.name.value,
            description: inputs.description.value,
            iso_ref: inputs.iso_ref.value,
            default_cause_code: inputs.default_cause_code.value,
            renews_item: inputs.renews_item.checked,
            semantic: inputs.semantic.value,
            sort_order: inputs.sort_order.value || 0,
            is_active: inputs.is_active.checked,
          });
          codeState.lists = data.code_lists;
          closeCodeForm();
          renderCodeLists();
          // A new active code is set on the open work order, ready to save.
          const used = applyCodeLists(data.code_lists, isNew && inputs.is_active.checked ? listKey : null, code);
          showToast(
            used
              ? `${list.label} ${code} added and set on task ${currentRow().task_id}; save the work order to keep it.`
              : `${list.label} ${code} ${isNew ? "added" : "saved"}.`,
            "success"
          );
        } catch (err) {
          if (!err.toastShown) showToast(err.message, "error");
        } finally {
          submit.disabled = false;
        }
      },
    }, [
      isNew ? el("h3", { class: "cl-form-title", text: `New ${list.label.toLowerCase()} code` }) : null,
      el("div", { class: "cl-form-grid" }, fields),
      el("div", { class: "cl-form-actions" }, [
        remove,
        el("span", { class: "cl-spacer" }),
        el("button", { type: "button", class: "btn-secondary", text: "Cancel", onclick: () => openCodeForm(null, { discard: true }) }),
        submit,
      ]),
    ]);
    const row = el("tr", { class: "cl-form-row" }, [el("td", { colspan: String(span) }, [form])]);
    // What the form held when it opened, to tell whether anything was changed since.
    const snapshot = () => JSON.stringify(Object.values(inputs).map((input) => (input.type === "checkbox" ? input.checked : input.value)));
    const opened = snapshot();
    codeState.form = {
      key,
      row,
      label: isNew ? "the new code" : `${list.label.toLowerCase()} ${entry.code}`,
      changed: () => snapshot() !== opened,
    };
    return row;
  }

  // A change to the code lists reaches the open work order at once: its
  // dropdowns offer the lists as they now are, with everything typed kept, and a
  // code just added is set on its field, ready to save. Returns whether it was.
  function applyCodeLists(codeLists, addedList, addedCode) {
    const active = {};
    Object.entries(codeLists).forEach(([key, list]) => {
      active[key] = list.codes.filter((entry) => entry.is_active);
    });
    if (state.options) state.options = { ...state.options, code_lists: active };
    if (!state.form) return false;
    state.form.setCodeLists(active);
    return Boolean(addedList && state.form.useCode(addedList, addedCode));
  }

  // ---- Show me around ----------------------------------------------------------------
  // The page draws its two windows only once an asset is picked, so the tour's
  // second step picks one for somebody who hasn't: the asset with the most work
  // orders, whose list has the most to show. Its steps are about the parts of the
  // page in the order a disposition is made.
  const TOUR_SEEN_KEY = "gremlin.disposition.tour-seen";
  let combobox = null;
  let tourExampleTried = false;

  const TOUR_PICK_EXAMPLE = {
    label: "Pick an example ►",
    needed: () => !state.asset && !tourExampleTried,
    run: async () => {
      tourExampleTried = true;
      try {
        const data = await getJson(`${API}/disposition-tour-example`);
        if (data.asset_number) {
          combobox.select(data.asset_number);
          await waitFor(() => Boolean(state.form));
        }
      } catch (err) {
        /* the tour carries on over an empty page */
      }
    },
  };

  function waitFor(test, timeout) {
    return new Promise((resolve) => {
      const started = Date.now();
      (function check() {
        if (test() || Date.now() - started > (timeout || 6000)) resolve();
        else setTimeout(check, 80);
      })();
    });
  }

  const DISPOSITION_TOUR_STEPS = [
    {
      target: null,
      title: "Disposition",
      body:
        "Every work order is dispositioned once, in a few fields that serve both the pattern views and the Weibull " +
        "life analysis on Perform an Analysis: what kind of event it was, where and what failed, how, and what was done.",
    },
    {
      target: "#lda-asset-field",
      title: "Pick an asset",
      body: () =>
        state.asset
          ? `Showing ${state.asset}: its work orders, and those logged on its sub-assets' own records, are listed below.`
          : "Choose the asset whose work orders you are dispositioning. Next picks an example if you haven't.",
      action: TOUR_PICK_EXAMPLE,
    },
    {
      target: "#disp-scope-field",
      title: "Which work orders",
      body: "Every work order, only those nobody has reviewed yet, those carried over from the REL categories that still need confirming, or those the disposition helper dispositioned on its own.",
      points: [
        ["Not reviewed", "the backlog: nothing has been decided about these."],
        ["Migrated", "a REL disposition was carried over with a review flag saying what to check."],
        ["Auto-dispositioned", "saved by the helper's batch run; open one and save it to confirm it."],
      ],
    },
    {
      target: "#disp-search-field",
      title: "Search",
      body: "Narrows the list by task ID, dates, title, notes or the failure narrative, across every page of it.",
    },
    {
      target: "#disp-list-card",
      title: "The work orders",
      body:
        "One row per work order, newest first, with how it is dispositioned now. Click a row, or press Enter on it, " +
        "to open it in the window beside.",
    },
    {
      target: "#disp-table-head",
      title: "Sort and filter",
      body:
        "The ▾ on each column header sorts by it or keeps only the values you tick, as a spreadsheet's filter does. " +
        "Both reach every work order in the list, not only this page, and the workbook download follows them.",
    },
    {
      target: "#disp-editor-card",
      title: "The disposition window",
      body:
        "The work order itself first, each part under its own label: when it was created, started and completed, its " +
        "downtime and record class, then the request title and description -- so the disposition is read off the record.",
    },
    {
      target: ".std-narrative",
      title: "The failure narrative",
      body:
        "What the technician found: the area affected, the condition found, the cause and the action taken. It is " +
        "the best evidence for the mechanism, mode and component; a part left empty says so.",
    },
    {
      target: ".std-categories",
      title: "Category first",
      body: "It decides which fields apply. Hover a category for when to use it.",
      points: [
        ["Failure", "the item failed; it ends a life in the Weibull."],
        ["PM reset", "planned work that renewed items; their lives restart."],
        ["External / Non-failure", "no failure of the equipment; never a life event."],
        ["Duplicate / misfiled", "an echo of another work order, linked to it."],
        ["Held", "reviewed, but the record can't say; read as unknown."],
      ],
    },
    {
      target: ".std-form",
      title: "Where, what and how",
      body:
        "Sub-asset and component say where and what failed -- every Weibull life is measured on one physical item -- " +
        "and the failure mechanism, mode and cause say how, in the ISO 14224 lists. The action says whether the item " +
        "was renewed, which decides whether its clock restarts.",
    },
    {
      target: ".std-derived",
      title: "What is derived",
      body:
        "Nobody types these: the location, the renewal stream the work order lands in, the effective cause and the " +
        "detection method follow from the fields above, shown as they will be stored.",
    },
    {
      target: "#disp-excel",
      title: "Disposition in Excel",
      body:
        "Download the work orders the list is showing as a workbook with every coded column a dropdown, fill it in " +
        "offline, and upload it. Only the rows you changed are saved, and one refused row saves none.",
    },
    {
      target: "#disp-helper-bar",
      title: "Auto-disposition",
      body:
        "Turn the switch on and the disposition helper reads each work order you open -- its words, and the work " +
        "orders the team has already dispositioned that read most like it -- and fills in the fields it is sure " +
        "enough of. The % beside a field is how sure it is; nothing is saved until you save.",
      points: [
        ["Auto-disposition the backlog", "saves the not-reviewed work orders it is very sure of, marked Auto, as one run you can undo."],
        ["How accurate is it?", "replays work orders the team has decided and shows how often it agrees."],
      ],
    },
    {
      target: "#disp-code-lists-btn",
      title: "Code lists",
      body:
        "The lists the dropdowns offer belong to the team: add a code, rename one, or retire one, which keeps the " +
        "records already coded with it reading as they were.",
    },
    {
      target: null,
      title: "Then analyse",
      body:
        "Perform an Analysis reads these dispositions: the Pareto, trend, downtime, PM effectiveness, repeat fixes " +
        "and the Weibull fit all count the same records.",
    },
  ];

  function startTour() {
    if (!window.gremlinTour || window.gremlinTour.isOpen()) return;
    tourExampleTried = false;
    window.gremlinTour.start(DISPOSITION_TOUR_STEPS, { seenKey: TOUR_SEEN_KEY });
  }

  // ---- start -----------------------------------------------------------------------
  function init() {
    renderHeader();
    $("disp-clear-filters").addEventListener("click", () => applyView({ filters: {} }));
    combobox = G.assetCombobox({
      input: $("lda-asset"),
      list: $("lda-asset-list"),
      hint: $("lda-asset-hint"),
      onChoose: (asset) => {
        if (asset === state.asset) return;
        if (!confirmDiscard()) {
          combobox.select(state.asset);
          return;
        }
        state.asset = asset;
        state.page = 0;
        state.form = null;
        state.selectedId = null;
        // Filters name another asset's values; the sort still applies.
        state.filters = {};
        if (asset) load(null);
        else $("disp-workspace").hidden = true;
      },
    });
    // An analysis table links a work order here as ?asset=…&search=<task id>.
    const params = new URLSearchParams(window.location.search);
    combobox.load().then(() => {
      const scope = (params.get("scope") || "").trim();
      if (["all", "not_reviewed", "migrated", "auto"].includes(scope)) {
        $("disp-scope").value = scope;
        state.scope = scope;
      }
      const search = (params.get("search") || "").trim();
      if (search) {
        $("disp-search").value = search;
        state.search = search;
      }
      if (params.get("asset")) combobox.select(params.get("asset").trim());
      if (window.gremlinTour && !window.gremlinTour.seen(TOUR_SEEN_KEY) && !window.gremlinTour.busy() && !params.get("asset")) {
        startTour();
      }
    });
    $("disp-scope").addEventListener("change", (event) => {
      if (!confirmDiscard()) {
        event.target.value = state.scope;
        return;
      }
      state.scope = event.target.value;
      state.page = 0;
      state.form = null;
      load(null);
    });
    let searchDebounce = null;
    $("disp-search").addEventListener("input", (event) => {
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(() => {
        if (!confirmDiscard()) {
          event.target.value = state.search;
          return;
        }
        state.search = event.target.value.trim();
        state.page = 0;
        state.form = null;
        load(null);
      }, 350);
    });
    $("disp-prev").addEventListener("click", () => {
      if (!confirmDiscard()) return;
      state.page = Math.max(0, state.page - 1);
      state.form = null;
      load(null);
    });
    $("disp-next").addEventListener("click", () => {
      if (!confirmDiscard()) return;
      state.page += 1;
      state.form = null;
      load(null);
    });
    $("disp-excel-download").addEventListener("click", downloadExcel);
    $("disp-excel-upload").addEventListener("click", () => {
      if (!state.asset) {
        showBanner("Select an Asset Number first.", "error");
        return;
      }
      $("disp-excel-file").click();
    });
    $("disp-excel-file").addEventListener("change", (event) => uploadExcel(event.target.files[0]));
    $("disp-excel-help").addEventListener("click", () => openDialog($("lda-excel-help-dialog")));
    $("lda-excel-help-close").addEventListener("click", () => $("lda-excel-help-dialog").close());
    $("disp-code-lists-btn").addEventListener("click", openCodeLists);
    $("disp-code-close").addEventListener("click", () => {
      if (mayLeaveCodeForm()) $("disp-code-dialog").close();
    });
    // Escape closes the dialog too: not over a form's unsaved changes without asking.
    $("disp-code-dialog").addEventListener("cancel", (event) => {
      if (!mayLeaveCodeForm()) event.preventDefault();
    });
    $("disp-code-search").addEventListener("input", (event) => {
      codeState.query = event.target.value;
      if (codeState.lists) renderCodeTable();
    });
    $("disp-code-hide-retired").addEventListener("change", (event) => {
      codeState.hideRetired = event.target.checked;
      if (codeState.lists) renderCodeTable();
    });
    $("disp-code-add").addEventListener("click", () => {
      if (codeState.lists) openCodeForm(NEW_CODE);
    });
    $("disposition-tour-btn").addEventListener("click", startTour);
    try {
      state.helperOn = window.localStorage.getItem(HELPER_KEY) === "on";
    } catch (err) {
      state.helperOn = false;
    }
    renderHelperStatus();
    loadHelperStatus();
    $("disp-helper-toggle").addEventListener("change", (event) => setHelper(event.target.checked));
    $("disp-helper-batch").hidden = !G.CAN_EDIT;
    $("disp-helper-batch").addEventListener("click", openHelperDialog);
    $("disp-helper-accuracy").addEventListener("click", openAccuracy);
    $("disp-helper-close").addEventListener("click", () => $("disp-helper-dialog").close());
    $("disp-helper-accuracy-close").addEventListener("click", () => $("disp-helper-accuracy-dialog").close());
    $("disp-helper-threshold").addEventListener("input", (event) => {
      $("disp-helper-threshold-out").textContent = `${event.target.value}%`;
      // A preview was for the old setting.
      if (helperState.preview) {
        helperState.preview = null;
        $("disp-helper-apply").disabled = true;
        $("disp-helper-apply").textContent = "Auto-disposition";
        $("disp-helper-preview-body").innerHTML = "";
        $("disp-helper-preview-body").appendChild(el("p", { class: "lda-hint", text: "Preview again for the new setting." }));
      }
    });
    $("disp-helper-preview").addEventListener("click", previewBatch);
    $("disp-helper-apply").addEventListener("click", applyBatch);
    window.addEventListener("beforeunload", (event) => {
      if (isDirty()) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  init();
})();
