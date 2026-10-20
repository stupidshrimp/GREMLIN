/* Disposition: every work order on an asset, dispositioned under the standard.
 *
 * Two windows side by side: the asset's work orders on the left, and the one
 * picked on the right in the disposition form (disposition_form.js), with the
 * work order's own words above the fields. Save & next moves down the list, so a
 * backlog is worked through without leaving the keyboard. The same records can
 * be dispositioned offline in a workbook (Download Excel / Upload Excel), and the
 * code lists the dropdowns offer are edited here too.
 */
(function () {
  "use strict";

  const G = window.GremlinStandard;
  const { $, el, getJson, postJson, showBanner, clearBanner, showToast, beginLoading, endLoading, day } = G;
  const API = G.API;
  const Form = window.GremlinDispositionForm;

  const state = {
    asset: null,
    scope: "all",
    search: "",
    page: 0,
    data: null,
    options: null,
    selectedId: null,
    form: null,
    token: 0,
    historyToken: 0,
  };

  const rows = () => (state.data && state.data.rows) || [];
  const currentRow = () => rows().find((row) => row.mapped_record_id === state.selectedId) || null;

  // ---- the list ---------------------------------------------------------------
  async function load(selectId) {
    if (!state.asset) return;
    const token = ++state.token;
    beginLoading("Loading work orders…");
    try {
      const params = new URLSearchParams({ asset: state.asset, scope: state.scope, page: String(state.page) });
      if (state.search) params.set("search", state.search);
      const data = await getJson(`${API}/dispositions?${params}`);
      if (token !== state.token) return;
      clearBanner();
      state.data = data;
      state.options = data.options;
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
      `${counts.all || 0} work orders on ${state.asset}: ${counts.not_reviewed || 0} not reviewed, ` +
      `${counts.migrated || 0} migrated from the REL categories and still to confirm.`;
    if (!rows().length) {
      body.appendChild(el("tr", {}, [el("td", { colspan: "8", class: "lda-readonly", text: "No work orders match." })]));
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
        el("td", { class: "std-nowrap", text: row.derived.location || "" }),
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
      const data = await getJson(`${API}/dispositions/record?mapped_record_id=${id}`);
      if (token === state.historyToken) form.setHistory(data.history || []);
    } catch (err) {
      /* the history is a convenience; the editor works without it */
    }
  }

  async function save(payload, nextId) {
    const row = currentRow();
    beginLoading("Saving…");
    try {
      await postJson(`${API}/dispositions/save`, { dispositions: [payload] });
      showToast(`Task ${row ? row.task_id : payload.mapped_record_id} saved.`, "success");
      state.form = null;
      await load(nextId);
    } finally {
      endLoading();
    }
  }

  // ---- Excel ----------------------------------------------------------------------
  async function downloadExcel() {
    if (!state.asset) {
      showBanner("Select an Asset Number first.", "error");
      return;
    }
    beginLoading("Building the workbook…");
    try {
      const params = new URLSearchParams({ asset: state.asset, scope: state.scope });
      const name = await G.download(`${API}/dispositions/excel?${params}`, "dispositions.xlsx");
      showToast(`Downloaded ${name}.`, "success");
    } catch (err) {
      showBanner(err.message, "error");
    } finally {
      endLoading();
    }
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
      clearBanner();
      showBanner(
        `Saved ${outcome.saved} disposition${outcome.saved === 1 ? "" : "s"} from the workbook; ` +
          `${outcome.unchanged} row${outcome.unchanged === 1 ? " was" : "s were"} already as saved.`,
        "success"
      );
      state.form = null;
      await load(state.selectedId);
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
  const codeState = { lists: null, active: "COMPONENT" };
  // The order a disposition is filled in, not the alphabetical order the JSON
  // arrives in.
  const CODE_LIST_ORDER = ["COMPONENT", "FAILURE_MECHANISM", "FAILURE_MODE", "FAILURE_CAUSE", "ACTION"];

  async function openCodeLists() {
    openDialog($("disp-code-dialog"));
    try {
      const data = await getJson(`${API}/code-lists`);
      codeState.lists = data.code_lists;
      renderCodeLists();
    } catch (err) {
      showBanner(err.message, "error");
    }
  }

  function renderCodeLists() {
    const tabs = $("disp-code-tabs");
    tabs.innerHTML = "";
    CODE_LIST_ORDER.filter((key) => codeState.lists[key]).forEach((key) => {
      tabs.appendChild(el("button", {
        type: "button",
        role: "tab",
        "aria-selected": key === codeState.active ? "true" : "false",
        class: "std-category" + (key === codeState.active ? " is-active" : ""),
        text: codeState.lists[key].label,
        onclick: () => {
          codeState.active = key;
          renderCodeLists();
        },
      }));
    });
    const listKey = codeState.active;
    const list = codeState.lists[listKey];
    const isMechanism = listKey === "FAILURE_MECHANISM";
    const isAction = listKey === "ACTION";
    const hasMeaning = list.semantics.length > 0;
    const table = $("disp-code-table");
    table.innerHTML = "";
    const heads = ["Code", "Name", "Description"];
    if (isMechanism) heads.push("ISO ref", "Default cause");
    if (isAction) heads.push("Renews item");
    if (hasMeaning) heads.push("Means");
    heads.push("Order", "Active", "");
    table.appendChild(el("thead", {}, [el("tr", {}, heads.map((text) => el("th", { scope: "col", text })))]));
    const body = el("tbody");
    const causes = (codeState.lists.FAILURE_CAUSE && codeState.lists.FAILURE_CAUSE.codes) || [];
    const rowFor = (entry, isNew) => {
      const inputs = {
        code: el("input", { type: "text", value: entry.code || "", readonly: !isNew, "aria-label": "Code", placeholder: isNew ? "New code" : null }),
        name: el("input", { type: "text", value: entry.name || "", "aria-label": "Name" }),
        description: el("input", { type: "text", value: entry.description || "", "aria-label": "Description" }),
        iso_ref: el("input", { type: "text", value: entry.iso_ref || "", "aria-label": "ISO reference" }),
        default_cause_code: el("select", { "aria-label": "Default cause" }, [
          el("option", { value: "", text: "—" }),
          ...causes.map((cause) => el("option", { value: cause.code, text: `${cause.code} · ${cause.name}` })),
        ]),
        renews_item: el("input", { type: "checkbox", checked: Boolean(entry.renews_item), "aria-label": "Renews the item" }),
        semantic: el("select", { "aria-label": "Means" }, [
          el("option", { value: "", text: "—" }),
          ...list.semantics.map((option) => el("option", { value: option.code, text: option.label })),
        ]),
        sort_order: el("input", { type: "number", step: "1", value: String(entry.sort_order ?? ""), "aria-label": "Order" }),
        is_active: el("input", { type: "checkbox", checked: isNew || Boolean(entry.is_active), "aria-label": "Active" }),
      };
      inputs.default_cause_code.value = entry.default_cause_code || "";
      inputs.semantic.value = entry.semantic || "";
      const cells = [inputs.code, inputs.name, inputs.description];
      if (isMechanism) cells.push(inputs.iso_ref, inputs.default_cause_code);
      if (isAction) cells.push(inputs.renews_item);
      if (hasMeaning) cells.push(inputs.semantic);
      cells.push(inputs.sort_order, inputs.is_active);
      const button = el("button", {
        type: "button",
        class: "btn-secondary",
        text: isNew ? "Add" : "Save",
        onclick: async () => {
          try {
            const data = await postJson(`${API}/code-lists`, {
              code_list: listKey,
              code: inputs.code.value.trim(),
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
            renderCodeLists();
            showToast(`${list.label} ${inputs.code.value.trim()} saved.`, "success");
            if (state.asset && !isDirty()) load(state.selectedId);
          } catch (err) {
            if (!err.toastShown) showToast(err.message, "error");
          }
        },
      });
      return el("tr", { class: entry.is_active === false ? "is-retired" : "" }, [
        ...cells.map((cell) => el("td", { class: cell === inputs.name ? "std-code-name" : null }, [cell])),
        el("td", {}, [button]),
      ]);
    };
    list.codes.forEach((entry) => body.appendChild(rowFor(entry, false)));
    const last = list.codes.length ? list.codes[list.codes.length - 1].sort_order : 0;
    body.appendChild(rowFor({ sort_order: (last || 0) + 10, is_active: true }, true));
    table.appendChild(body);
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
      body: "Every work order, only those nobody has reviewed yet, or those carried over from the REL categories that still need confirming.",
      points: [
        ["Not reviewed", "the backlog: nothing has been decided about these."],
        ["Migrated", "a REL disposition was carried over with a review flag saying what to check."],
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
      target: "#disp-editor-card",
      title: "The disposition window",
      body:
        "The work order's own words first -- request, completion notes, area, condition, cause and action -- so the " +
        "disposition is read off the record, and any review flag from the migration above them.",
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
        if (asset) load(null);
        else $("disp-workspace").hidden = true;
      },
    });
    // An analysis table links a work order here as ?asset=…&search=<task id>.
    const params = new URLSearchParams(window.location.search);
    combobox.load().then(() => {
      const scope = (params.get("scope") || "").trim();
      if (["all", "not_reviewed", "migrated"].includes(scope)) {
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
    $("disp-code-close").addEventListener("click", () => $("disp-code-dialog").close());
    $("disposition-tour-btn").addEventListener("click", startTour);
    window.addEventListener("beforeunload", (event) => {
      if (isDirty()) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  init();
})();
