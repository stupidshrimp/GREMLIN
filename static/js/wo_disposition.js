/* Work-Order Disposition: the editor for the work-order disposition standard.
 *
 * Pick an asset, pick a work order from the list, and fill in the few fields the
 * standard asks for (services/disposition_standard.py holds the rules; the server
 * enforces every one of them, this page only shows them). Category first, since
 * it decides which fields apply; then where (sub-asset) and what (component)
 * failed, and how (mechanism, mode), what was done (action), and how sure the
 * record is (evidence). The derived fields are shown as they will be stored, so
 * the renewal stream a work order lands in is visible before it is saved.
 */
(function () {
  "use strict";

  const G = window.GremlinStandard;
  const { $, el, getJson, postJson, showBanner, clearBanner, showToast, beginLoading, endLoading, day, fmt } = G;
  const API = G.STANDARD_API;

  const NARRATIVE = [
    ["area_affected", "Area affected"],
    ["condition_found", "Condition"],
    ["cause", "Cause"],
    ["action_taken", "Action"],
  ];
  const CATEGORY_CHIP = {
    FAILURE: "is-failure",
    PM_RESET: "is-pm-reset",
    HELD: "is-held",
    NOT_REVIEWED: "is-not-reviewed",
  };
  const PM_RECORD_CLASSES = ["PM", "PM_RESET_CANDIDATE"];
  const DETECTION_LABELS = {
    INSPECTION: "Inspection",
    PERIODIC_MAINTENANCE: "Periodic maintenance",
    PRODUCTION_INTERFERENCE: "Production interference",
  };

  const state = {
    asset: null,
    scope: "all",
    search: "",
    page: 0,
    data: null,
    options: null,
    selectedId: null,
    draft: null,
    savedDraft: null,
    error: null,
    // The selected record's disposition history: { id, entries }.
    history: null,
    token: 0,
    historyToken: 0,
  };

  // ---- the code lists -------------------------------------------------------
  function codes(list) {
    return (state.options && state.options.code_lists && state.options.code_lists[list]) || [];
  }
  function codeEntry(list, code) {
    return codes(list).find((entry) => entry.code === code) || null;
  }
  function codeLabel(list, code) {
    if (!code) return "";
    const entry = codeEntry(list, code);
    return entry && entry.name !== code ? `${code} · ${entry.name}` : code;
  }
  function categoryInfo(code) {
    return ((state.options && state.options.categories) || []).find((category) => category.code === code) || null;
  }
  function isCoded(category) {
    return ((state.options && state.options.coded_categories) || []).includes(category);
  }
  function isRenewing(category) {
    return ((state.options && state.options.renewing_categories) || []).includes(category);
  }

  // ---- the list -------------------------------------------------------------
  function rows() {
    return (state.data && state.data.rows) || [];
  }
  function currentRow() {
    return rows().find((row) => row.mapped_record_id === state.selectedId) || null;
  }

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
      $("std-workspace").hidden = false;
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
    const body = $("std-table").querySelector("tbody");
    body.innerHTML = "";
    const data = state.data;
    const counts = data.counts || {};
    $("std-list-summary").textContent =
      `${counts.all || 0} work orders on ${state.asset}: ${counts.not_reviewed || 0} not reviewed, ` +
      `${counts.migrated || 0} migrated from the REL categories and still to confirm.`;
    if (!rows().length) {
      body.appendChild(el("tr", {}, [el("td", { colspan: "8", class: "lda-readonly", text: "No work orders match." })]));
    }
    rows().forEach((row) => {
      const d = row.disposition;
      const chips = [
        el("span", { class: `std-chip ${CATEGORY_CHIP[d.disposition_category] || ""}`, text: d.disposition_category }),
      ];
      if (d.source === "MIGRATION") chips.push(" ", el("span", { class: "std-chip is-migrated", text: "Migrated" }));
      const tr = el("tr", {
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
        el("td", {}, chips),
        el("td", { class: "std-nowrap", text: row.derived.location || "" }),
        el("td", { text: d.component_code || "" }),
        el("td", { text: d.failure_mechanism_code || "" }),
        el("td", { text: d.failure_mode_code || "" }),
      ]);
      body.appendChild(tr);
    });
    const pages = data.max_page_index + 1;
    $("std-page-status").textContent = data.displayed_count
      ? `Page ${data.page_index + 1} of ${pages} · ${data.displayed_count} rows`
      : "No rows";
    $("std-prev").disabled = data.page_index <= 0;
    $("std-next").disabled = data.page_index >= data.max_page_index;
  }

  function markSelectedRow() {
    $("std-table").querySelectorAll("tr.std-row").forEach((tr) => {
      tr.classList.toggle("is-selected", Number(tr.dataset.id) === state.selectedId);
    });
  }

  // ---- the draft ------------------------------------------------------------
  function draftOf(row) {
    const d = row.disposition;
    return {
      disposition_category: d.disposition_category || "NOT_REVIEWED",
      sub_asset: d.sub_asset || "",
      component_code: d.component_code || "",
      failure_mechanism_code: d.failure_mechanism_code || "",
      failure_mode_code: d.failure_mode_code || "",
      action_code: d.action_code || "",
      evidence: d.evidence || "",
      failure_cause_code: d.failure_cause_code || "",
      renewed_items: (d.renewed_items || []).slice(),
      qty: d.qty === null || d.qty === undefined ? "" : String(d.qty),
      item_detail: d.item_detail || "",
      linked_wo: d.linked_task_id ? String(d.linked_task_id) : "",
      disposition_note: d.disposition_note || "",
    };
  }
  function isDirty() {
    return Boolean(state.draft && state.savedDraft) && JSON.stringify(state.draft) !== JSON.stringify(state.savedDraft);
  }
  function confirmDiscard() {
    if (!isDirty()) return true;
    const row = currentRow();
    return window.confirm(`Discard the unsaved changes to task ${row ? row.task_id : ""}?`);
  }

  function selectRecord(id, { force } = {}) {
    if (!force && id === state.selectedId) return;
    if (!force && !confirmDiscard()) return;
    state.selectedId = id;
    const row = currentRow();
    if (!row) return clearEditor();
    if (!force || !state.draft || state.savedDraft === null || !isDirty()) {
      state.draft = draftOf(row);
      state.savedDraft = draftOf(row);
      state.error = null;
    }
    markSelectedRow();
    renderEditor();
    loadHistory(row.mapped_record_id);
  }

  function clearEditor() {
    state.selectedId = null;
    state.draft = null;
    state.savedDraft = null;
    markSelectedRow();
    $("std-editor-body").innerHTML = "";
    $("std-editor-body").appendChild(el("p", { class: "lda-hint", text: "Choose a work order from the list." }));
  }

  // ---- derived fields, as the server will store them -------------------------
  function derived(row, draft) {
    const category = draft.disposition_category;
    const location = draft.sub_asset || row.asset_number;
    const mechanism = codeEntry("FAILURE_MECHANISM", draft.failure_mechanism_code);
    const defaultCause = mechanism ? mechanism.default_cause_code : null;
    const cause = draft.failure_cause_code || defaultCause;
    let detection = null;
    if (category !== "DUPLICATE_MISFILED" && category !== "NOT_REVIEWED") {
      const mode = codeEntry("FAILURE_MODE", draft.failure_mode_code);
      if (mode && mode.semantic === "INSPECTION_FINDING") detection = "INSPECTION";
      else if (PM_RECORD_CLASSES.includes(String(row.record_class || "").toUpperCase())) detection = "PERIODIC_MAINTENANCE";
      else detection = "PRODUCTION_INTERFERENCE";
    }
    const action = codeEntry("ACTION", draft.action_code);
    return {
      location,
      defaultCause,
      cause,
      detection,
      renews: Boolean(action && action.renews_item),
      stream: draft.component_code ? `${location} | ${draft.component_code}` : null,
    };
  }

  // ---- the editor ------------------------------------------------------------
  function recordText(row) {
    const d = row.disposition;
    const blocks = [
      el("h3", { text: `${row.task_id ?? ""} · ${row.task_name || row.request_title || "(no title)"}` }),
      el("p", {
        class: "std-record-meta",
        text: [
          `Asset ${row.asset_number}${row.asset_name ? " (" + row.asset_name + ")" : ""}`,
          `found ${day(row.created_date) || "—"}`,
          `completed ${day(row.completed_date) || "—"}`,
          `downtime ${fmt(row.downtime_hours)} h`,
          `record class ${row.record_class || "—"}`,
        ].join(" · "),
      }),
    ];
    const text = (label, value) => value && el("p", { class: "std-record-block" }, [el("strong", { text: label }), value]);
    blocks.push(text("Request", [row.request_title, row.requestor_description].filter(Boolean).join(" — ")));
    blocks.push(text("Completion notes", row.completion_notes));
    NARRATIVE.forEach(([key, label]) => blocks.push(text(label, row[key])));
    if (d.legacy_disposition_category || d.legacy_failure_mode || d.legacy_failure_mechanism) {
      blocks.push(text("REL disposition (legacy)", [
        d.legacy_disposition_category, d.legacy_failure_mode, d.legacy_failure_mechanism,
      ].filter(Boolean).join(" · ")));
    }
    if (d.review_flag) blocks.push(el("div", { class: "std-flag", text: d.review_flag }));
    return el("div", { class: "std-record-text" }, blocks);
  }

  function codeSelect(id, list, value, blankLabel) {
    const select = el("select", { id, class: "lda-select" }, [el("option", { value: "", text: blankLabel || "—" })]);
    const entries = codes(list);
    entries.forEach((entry) => {
      select.appendChild(el("option", { value: entry.code, text: entry.name !== entry.code ? `${entry.code} · ${entry.name}` : entry.code }));
    });
    if (value && !entries.some((entry) => entry.code === value)) {
      // A code retired since it was chosen: still shown, so the record reads true.
      select.appendChild(el("option", { value, text: `${value} (retired)` }));
    }
    select.value = value || "";
    return select;
  }

  function field(id, labelText, control, { required, hint, wide } = {}) {
    return el("div", { class: "lda-field" + (wide ? " std-wide" : "") }, [
      el("label", { for: id }, [labelText, required ? el("span", { class: "std-required", text: "*", title: "Required for this category" }) : null]),
      control,
      hint ? el("p", { class: "lda-hint", text: hint }) : null,
    ]);
  }

  function renderEditor() {
    const row = currentRow();
    if (!row) return clearEditor();
    const body = $("std-editor-body");
    const focusedId = document.activeElement && body.contains(document.activeElement) ? document.activeElement.id : null;
    body.innerHTML = "";
    body.appendChild(recordText(row));
    body.appendChild(renderForm(row));
    if (focusedId && $(focusedId)) $(focusedId).focus();
  }

  function renderForm(row) {
    const draft = state.draft;
    const category = draft.disposition_category;
    const info = categoryInfo(category);
    const coded = isCoded(category);
    const form = el("form", {
      class: "std-form",
      onsubmit: (event) => {
        event.preventDefault();
        save(false);
      },
    });

    const update = (key, rerender) => (event) => {
      draft[key] = event.target.value;
      if (rerender) renderEditor();
      else refreshDerived(row);
    };

    // Category: decides which of the rest apply.
    const categories = el("div", { class: "std-categories", role: "radiogroup", "aria-label": "Disposition category" });
    ((state.options && state.options.categories) || []).forEach((option) => {
      categories.appendChild(el("button", {
        type: "button",
        class: "std-category" + (option.code === category ? " is-active" : ""),
        role: "radio",
        "aria-checked": option.code === category ? "true" : "false",
        title: option.use_when,
        text: option.label,
        onclick: () => {
          draft.disposition_category = option.code;
          // A refusal was about the category it was saved as.
          state.error = null;
          renderEditor();
        },
      }));
    });
    form.appendChild(el("div", { class: "lda-field std-wide" }, [
      el("span", { class: "lda-field-label", text: "Disposition category" }),
      categories,
      info ? el("p", { class: "lda-hint", text: `${info.use_when} Weibull: ${info.weibull_role}` }) : null,
    ]));

    if (state.error) form.appendChild(el("div", { class: "lda-banner is-error std-wide", role: "alert", text: state.error }));

    if (category === "DUPLICATE_MISFILED") {
      const linked = el("input", { id: "std-linked", class: "lda-input", value: draft.linked_wo, placeholder: "Task ID" });
      linked.addEventListener("input", update("linked_wo"));
      form.appendChild(field("std-linked", "Linked WO", linked, {
        hint: "The work order this one continues or duplicates, by task ID. Its completion becomes that work order's " +
          "renewal date (diagnose now, repair later). Leave blank for a work order logged against the wrong asset.",
      }));
    } else {
      const subAssets = (state.options && state.options.sub_assets) || [];
      if (subAssets.length) {
        const select = el("select", { id: "std-sub-asset", class: "lda-select" }, [
          el("option", { value: "", text: `${row.asset_number} (whole asset)` }),
          ...subAssets.map((option) => el("option", {
            value: option.asset_number,
            text: option.asset_name ? `${option.asset_number} · ${option.asset_name}` : option.asset_number,
          })),
        ]);
        select.value = draft.sub_asset || "";
        select.addEventListener("change", update("sub_asset"));
        form.appendChild(field("std-sub-asset", "Sub-asset", select, {
          hint: "Where it failed. Leave at the whole asset when the work order does not say.",
        }));
      }
      const component = codeSelect("std-component", "COMPONENT", draft.component_code);
      component.addEventListener("change", (event) => {
        draft.component_code = event.target.value;
        // A PM resets what it renewed: start the list from the component chosen.
        if (category === "PM_RESET" && draft.component_code && !draft.renewed_items.length) {
          draft.renewed_items = [draft.component_code];
        }
        if (category === "FAILURE") draft.renewed_items = draft.renewed_items.filter((code) => code !== draft.component_code);
        renderEditor();
      });
      form.appendChild(field("std-component", "Component", component, {
        required: coded, hint: "The item found failed, not the one named in the request title.",
      }));
      const mechanism = codeSelect("std-mechanism", "FAILURE_MECHANISM", draft.failure_mechanism_code);
      mechanism.addEventListener("change", update("failure_mechanism_code", true));
      form.appendChild(field("std-mechanism", "Failure mechanism", mechanism, {
        required: coded, hint: "The physical process. UNK rather than a guess.",
      }));
      const mode = codeSelect("std-mode", "FAILURE_MODE", draft.failure_mode_code);
      mode.addEventListener("change", update("failure_mode_code", true));
      form.appendChild(field("std-mode", "Failure mode", mode, {
        required: coded, hint: "What the equipment or operator experienced.",
      }));
      const action = codeSelect("std-action", "ACTION", draft.action_code);
      action.addEventListener("change", update("action_code", true));
      form.appendChild(field("std-action", "Action", action, { required: coded, hint: "The most significant work done." }));
      const evidence = el("select", { id: "std-evidence", class: "lda-select" }, [
        el("option", { value: "", text: "—" }),
        ...((state.options && state.options.evidence) || []).map((option) => el("option", { value: option.code, text: option.label })),
      ]);
      evidence.value = draft.evidence || "";
      evidence.addEventListener("change", update("evidence"));
      form.appendChild(field("std-evidence", "Evidence", evidence, { required: coded }));

      const facts = derived(row, draft);
      const defaultName = facts.defaultCause ? codeLabel("FAILURE_CAUSE", facts.defaultCause) : "none (choose a mechanism)";
      const cause = codeSelect("std-cause", "FAILURE_CAUSE", draft.failure_cause_code, `Default: ${defaultName}`);
      cause.addEventListener("change", update("failure_cause_code", true));
      form.appendChild(field("std-cause", "Failure cause", cause, {
        hint: "Leave on the default unless the record says otherwise; an override needs a note.",
      }));

      if (isRenewing(category)) {
        const list = el("div", { class: "std-checklist", id: "std-renewed", role: "group", "aria-label": "Renewed items" });
        const offered = codes("COMPONENT").map((entry) => entry.code);
        draft.renewed_items.forEach((code) => offered.includes(code) || offered.push(code));
        offered.forEach((code) => {
          const isPrimary = category === "FAILURE" && code === draft.component_code;
          const box = el("input", {
            type: "checkbox",
            value: code,
            checked: draft.renewed_items.includes(code),
            disabled: isPrimary,
            onchange: (event) => {
              draft.renewed_items = event.target.checked
                ? draft.renewed_items.concat([code])
                : draft.renewed_items.filter((item) => item !== code);
              refreshDerived(row);
            },
          });
          list.appendChild(el("label", { class: "lda-checkbox", title: isPrimary ? "The component's own renewal follows the action." : null }, [box, codeLabel("COMPONENT", code)]));
        });
        form.appendChild(el("div", { class: "lda-field std-wide" }, [
          el("span", { class: "lda-field-label" }, [
            "Renewed items",
            category === "PM_RESET" ? el("span", { class: "std-required", text: "*" }) : null,
          ]),
          list,
          el("p", {
            class: "lda-hint",
            text: category === "PM_RESET"
              ? "Each item the PM renewed gets a suspension and a fresh life clock in its own stream."
              : "Other items this work renewed besides the component (two failures in one work order).",
          }),
        ]));
      }

      const qty = el("input", { id: "std-qty", type: "number", min: "1", step: "1", class: "lda-input", value: draft.qty });
      qty.addEventListener("input", update("qty"));
      form.appendChild(field("std-qty", "Qty", qty, { hint: "For several identical units at once (risers, guns, nozzles)." }));
      const detail = el("input", { id: "std-item-detail", class: "lda-input", value: draft.item_detail, maxlength: "200" });
      detail.addEventListener("input", update("item_detail"));
      form.appendChild(field("std-item-detail", "Item detail", detail, {
        hint: "Tells two items of one component type apart (combustion-air filter vs motor cooling filter).",
      }));
    }

    const note = el("input", { id: "std-note", class: "lda-input", value: draft.disposition_note, maxlength: "2000" });
    note.addEventListener("input", update("disposition_note"));
    form.appendChild(field("std-note", "Disposition note", note, {
      wide: true,
      required: category === "HELD" || Boolean(draft.failure_cause_code),
      hint: "One line. Required for HELD and when the cause is overridden.",
    }));

    form.appendChild(el("dl", { class: "std-derived std-wide", id: "std-derived" }));
    form.appendChild(el("div", { class: "lda-row-actions std-wide" }, [
      el("button", { type: "button", class: "btn-secondary", text: "Revert", onclick: () => {
        state.draft = draftOf(row);
        state.error = null;
        renderEditor();
      } }),
      el("button", { type: "submit", class: "btn-secondary", text: "Save" }),
      el("button", { type: "button", class: "btn-primary", text: "Save & next", onclick: () => save(true) }),
    ]));
    form.appendChild(el("div", { class: "std-wide" }, [
      el("h3", { class: "lda-hint", text: "History" }),
      el("ol", { class: "std-history", id: "std-history" }),
    ]));
    setTimeout(() => {
      refreshDerived(row);
      renderHistory();
    }, 0);
    return form;
  }

  function refreshDerived(row) {
    const target = $("std-derived");
    if (!target) return;
    const facts = derived(row, state.draft);
    const category = state.draft.disposition_category;
    const pairs = [
      ["Location", facts.location],
      ["Renewal stream", facts.stream || "— (no component)"],
      ["Cause (effective)", facts.cause ? codeLabel("FAILURE_CAUSE", facts.cause) : "—"],
      ["Detection", facts.detection ? DETECTION_LABELS[facts.detection] : "—"],
      ["Renews the item", category === "FAILURE" ? (facts.renews ? "Yes — the clock restarts" : "No — the clock does not restart") : "—"],
    ];
    if (isRenewing(category) && state.draft.renewed_items.length) {
      pairs.push(["Also renews", state.draft.renewed_items.map((code) => `${facts.location} | ${code}`).join(", ")]);
    }
    target.innerHTML = "";
    pairs.forEach(([label, value]) => {
      target.appendChild(el("dt", { text: label }));
      target.appendChild(el("dd", { text: value }));
    });
  }

  async function loadHistory(id) {
    const token = ++state.historyToken;
    state.history = { id, entries: null };
    try {
      const data = await getJson(`${API}/dispositions/record?mapped_record_id=${id}`);
      if (token !== state.historyToken) return;
      state.history = { id, entries: data.history || [] };
      renderHistory();
    } catch (err) {
      /* the history is a convenience; the editor works without it */
    }
  }

  // Kept on state rather than only in the DOM: the editor is rebuilt on every
  // change of category or code, and the list has to survive that.
  function renderHistory() {
    const list = $("std-history");
    if (!list || !state.history || state.history.id !== state.selectedId || !state.history.entries) return;
    list.innerHTML = "";
    if (!state.history.entries.length) list.appendChild(el("li", { text: "Never dispositioned under this standard." }));
    state.history.entries.forEach((entry) => {
      const codesText = [entry.sub_asset, entry.component_code, entry.failure_mechanism_code, entry.failure_mode_code, entry.action_code]
        .filter(Boolean).join(" · ");
      list.appendChild(el("li", {
        text: `${day(entry.decided_at)} ${entry.decided_by || ""} — ${entry.disposition_category}` +
          (codesText ? ` · ${codesText}` : "") + (entry.is_current ? " (current)" : ""),
      }));
    });
  }

  function payloadOf(row) {
    const draft = state.draft;
    const category = draft.disposition_category;
    const blank = (value) => (value === "" ? null : value);
    if (category === "DUPLICATE_MISFILED") {
      return {
        mapped_record_id: row.mapped_record_id,
        disposition_category: category,
        linked_wo: blank(draft.linked_wo.trim()),
        disposition_note: blank(draft.disposition_note.trim()),
      };
    }
    return {
      mapped_record_id: row.mapped_record_id,
      disposition_category: category,
      sub_asset: blank(draft.sub_asset),
      component_code: blank(draft.component_code),
      failure_mechanism_code: blank(draft.failure_mechanism_code),
      failure_mode_code: blank(draft.failure_mode_code),
      action_code: blank(draft.action_code),
      evidence: blank(draft.evidence),
      failure_cause_code: blank(draft.failure_cause_code),
      renewed_items: isRenewing(category) ? draft.renewed_items : [],
      qty: blank(String(draft.qty).trim()),
      item_detail: blank(draft.item_detail.trim()),
      disposition_note: blank(draft.disposition_note.trim()),
    };
  }

  async function save(goNext) {
    const row = currentRow();
    if (!row) return;
    const list = rows();
    const index = list.findIndex((item) => item.mapped_record_id === row.mapped_record_id);
    const next = goNext && index >= 0 && index + 1 < list.length ? list[index + 1].mapped_record_id : row.mapped_record_id;
    beginLoading("Saving…");
    try {
      await postJson(`${API}/dispositions/save`, { dispositions: [payloadOf(row)] });
      showToast(`Task ${row.task_id} saved.`, "success");
      state.error = null;
      state.draft = null;
      state.savedDraft = null;
      await load(next);
    } catch (err) {
      state.error = err.message;
      renderEditor();
    } finally {
      endLoading();
    }
  }

  // ---- code lists --------------------------------------------------------------
  const codeState = { lists: null, active: "COMPONENT" };
  // The order a disposition is filled in, not the alphabetical order the JSON
  // arrives in.
  const CODE_LIST_ORDER = ["COMPONENT", "FAILURE_MECHANISM", "FAILURE_MODE", "FAILURE_CAUSE", "ACTION"];

  async function openCodeLists() {
    const dialog = $("std-code-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    try {
      const data = await getJson(`${API}/code-lists`);
      codeState.lists = data.code_lists;
      renderCodeLists();
    } catch (err) {
      showBanner(err.message, "error");
    }
  }

  function renderCodeLists() {
    const tabs = $("std-code-tabs");
    tabs.innerHTML = "";
    CODE_LIST_ORDER.filter((key) => codeState.lists[key]).forEach((key) => {
      const list = codeState.lists[key];
      tabs.appendChild(el("button", {
        type: "button",
        role: "tab",
        "aria-selected": key === codeState.active ? "true" : "false",
        class: "std-category" + (key === codeState.active ? " is-active" : ""),
        text: list.label,
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
    const table = $("std-code-table");
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
            if (state.asset) load(state.selectedId);
          } catch (err) {
            showToast(err.message, "error");
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

  // ---- start -------------------------------------------------------------------
  function init() {
    const combobox = G.assetCombobox({
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
        state.draft = null;
        state.savedDraft = null;
        state.selectedId = null;
        if (asset) load(null);
        else $("std-workspace").hidden = true;
      },
    });
    // Analysis Population links a work order here as ?asset=…&search=<task id>.
    const params = new URLSearchParams(window.location.search);
    combobox.load().then(() => {
      const search = (params.get("search") || "").trim();
      if (search) {
        $("std-search").value = search;
        state.search = search;
      }
      if (params.get("asset")) combobox.select(params.get("asset").trim());
    });
    $("std-scope").addEventListener("change", (event) => {
      if (!confirmDiscard()) {
        event.target.value = state.scope;
        return;
      }
      state.scope = event.target.value;
      state.page = 0;
      state.draft = null;
      load(null);
    });
    let searchDebounce = null;
    $("std-search").addEventListener("input", (event) => {
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(() => {
        state.search = event.target.value.trim();
        state.page = 0;
        load();
      }, 350);
    });
    $("std-prev").addEventListener("click", () => {
      if (!confirmDiscard()) return;
      state.page = Math.max(0, state.page - 1);
      state.draft = null;
      load(null);
    });
    $("std-next").addEventListener("click", () => {
      if (!confirmDiscard()) return;
      state.page += 1;
      state.draft = null;
      load(null);
    });
    $("std-code-lists-btn").addEventListener("click", openCodeLists);
    $("std-code-close").addEventListener("click", () => $("std-code-dialog").close());
    window.addEventListener("beforeunload", (event) => {
      if (isDirty()) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  init();
})();
