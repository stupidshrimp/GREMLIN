/* The disposition form: one work order's disposition under the standard.
 *
 * Shared by the Disposition page, where it is the right-hand window beside the
 * list, and by Perform an Analysis, where it opens over the page from any work
 * order an analysis table lists. Both build it the same way, so a record offers
 * the same choices and says the same things wherever it is opened.
 *
 * Category first, since it decides which fields apply; then where (sub-asset)
 * and what (component) failed, how (mechanism, mode), what was done (action),
 * and how sure the record is (evidence). The derived fields are shown as they
 * will be stored, so the renewal stream a work order lands in is visible before
 * it is saved. The server enforces every rule (services/disposition_standard.py);
 * the form only shows them.
 *
 *   const form = GremlinDispositionForm.create({ row, options, onSave, onSaveNext });
 *   container.appendChild(form.node);
 *
 * Exposed as window.GremlinDispositionForm; needs standard_common.js first.
 */
(function () {
  "use strict";

  const { el, day, fmt } = window.GremlinStandard;

  const NARRATIVE = [
    ["area_affected", "Area affected"],
    ["condition_found", "Condition found"],
    ["cause", "Cause"],
    ["action_taken", "Action taken"],
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

  // A record's category as a small coloured chip, for the list and the tables.
  function categoryChip(category, source) {
    const chips = [el("span", { class: `std-chip ${CATEGORY_CHIP[category] || ""}`, text: category || "NOT_REVIEWED" })];
    if (source === "MIGRATION") chips.push(" ", el("span", { class: "std-chip is-migrated", text: "Migrated" }));
    return chips;
  }

  function draftOf(row) {
    const d = row.disposition;
    return {
      disposition_category: d.disposition_category || "NOT_REVIEWED",
      record_class: row.record_class || "",
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

  let formSeq = 0;

  /* One record's form. `options` is what /dispositions hands back for the
   * record's asset; `history` its past dispositions when already known.
   * `onSave(payload)` is awaited on Save, and `onSaveNext(payload)` on Save &
   * next, offered only when given. Either may throw: its message is shown on the
   * form, which keeps what was typed. */
  function create({ row, options, history, onSave, onSaveNext, onCancel, saveLabel }) {
    const id = (name) => `dform-${formSeq}-${name}`;
    formSeq += 1;
    let draft = draftOf(row);
    let saved = draftOf(row);
    let error = null;
    let historyEntries = history || null;
    const node = el("div", { class: "std-form-wrap" });

    const codes = (list) => (options && options.code_lists && options.code_lists[list]) || [];
    const codeEntry = (list, code) => codes(list).find((entry) => entry.code === code) || null;
    const codeLabel = (list, code) => {
      if (!code) return "";
      const entry = codeEntry(list, code);
      return entry && entry.name !== code ? `${code} · ${entry.name}` : code;
    };
    const categoryInfo = (code) => ((options && options.categories) || []).find((c) => c.code === code) || null;
    const isCoded = (category) => ((options && options.coded_categories) || []).includes(category);
    const isRenewing = (category) => ((options && options.renewing_categories) || []).includes(category);

    function derived() {
      const category = draft.disposition_category;
      const location = draft.sub_asset || row.asset_number;
      const mechanism = codeEntry("FAILURE_MECHANISM", draft.failure_mechanism_code);
      const defaultCause = mechanism ? mechanism.default_cause_code : null;
      const cause = draft.failure_cause_code || defaultCause;
      let detection = null;
      if (category !== "DUPLICATE_MISFILED" && category !== "NOT_REVIEWED") {
        const mode = codeEntry("FAILURE_MODE", draft.failure_mode_code);
        if (mode && mode.semantic === "INSPECTION_FINDING") detection = "INSPECTION";
        else if (PM_RECORD_CLASSES.includes(String(draft.record_class || "").toUpperCase())) detection = "PERIODIC_MAINTENANCE";
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

    // The work order as the CMMS has it, read-only above the fields, each part
    // under its own label: when it happened and how long it stopped the line,
    // what was asked for, the technician's failure narrative, and their notes.
    // A part left empty says so rather than disappearing, so a missing
    // narrative is noticed instead of overlooked.
    function recordText() {
      const d = row.disposition;
      const times = row.plant_times || {};
      const blank = (text) => text === null || text === undefined || String(text).trim() === "";
      const missing = (text) => el("span", { class: "std-missing", text });
      const shown = (text, emptyText) => (blank(text) ? missing(emptyText || "Not recorded") : String(text));
      const fact = (label, content, wide) =>
        el("div", { class: "std-fact" + (wide ? " std-fact-wide" : "") }, [el("dt", { text: label }), el("dd", {}, [content])]);
      const facts = el("dl", { class: "std-facts", id: id("facts") }, [
        fact("Asset", shown(row.asset_label || [row.asset_number, row.asset_name].filter(Boolean).join(" · ")), true),
        fact("Created", shown(times.created || day(row.created_date))),
        fact("Started", shown(times.started || day(row.start_date))),
        fact("Completed", shown(times.completed || day(row.completed_date))),
        fact("Downtime", blank(row.downtime_hours) ? missing("Not recorded") : `${fmt(row.downtime_hours)} h`),
        fact("Record class", shown(row.record_class)),
        fact("Category", el("span", {}, categoryChip(d.disposition_category, d.source))),
      ]);
      const section = (title, fields, options = {}) =>
        el("section", { class: options.class || "std-record-section", id: options.id || null }, [
          el("h4", { text: title }),
          options.note ? el("p", { class: "lda-hint", text: options.note }) : null,
          el("dl", { class: "std-record-fields" }, fields.map(([label, text, emptyText]) =>
            el("div", { class: "std-record-field" }, [
              label ? el("dt", { text: label }) : null,
              el("dd", {}, [shown(text, emptyText)]),
            ])
          )),
        ]);
      const narrativeEmpty = NARRATIVE.every(([key]) => blank(row[key]));
      const blocks = [
        el("div", { class: "std-record-head" }, [
          el("p", { class: "std-record-task", text: `Task ${row.task_id ?? "—"}` }),
          el("h3", { id: id("title"), text: row.task_name || row.request_title || "(no title)" }),
        ]),
        facts,
        section("Request", [
          ["Request title", row.request_title, "No request title"],
          ["Request description", row.requestor_description, "No description"],
        ], { id: id("request") }),
        section("Failure narrative", NARRATIVE.map(([key, label]) => [label, row[key], "Not filled in"]), {
          class: "std-record-section std-narrative",
          id: id("narrative"),
          note: narrativeEmpty ? "The technician left the failure narrative empty; the completion notes may say what happened." : null,
        }),
        section("Completion notes", [[null, row.completion_notes, "No completion notes"]], { id: id("notes") }),
      ];
      if (d.legacy_disposition_category || d.legacy_failure_mode || d.legacy_failure_mechanism) {
        blocks.push(section("REL disposition (legacy)", [[null, [
          d.legacy_disposition_category, d.legacy_failure_mode, d.legacy_failure_mechanism,
        ].filter(Boolean).join(" · ")]]));
      }
      if (d.review_flag) blocks.push(el("div", { class: "std-flag", text: d.review_flag }));
      return el("div", { class: "std-record-text", id: id("record") }, blocks);
    }

    function codeSelect(name, list, value, blankLabel) {
      const select = el("select", { id: id(name), class: "lda-select" }, [el("option", { value: "", text: blankLabel || "—" })]);
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

    function field(name, labelText, control, { required, hint, wide, fieldId } = {}) {
      return el("div", { class: "lda-field" + (wide ? " std-wide" : ""), id: fieldId || null }, [
        el("label", { for: id(name) }, [labelText, required ? el("span", { class: "std-required", text: "*", title: "Required for this category" }) : null]),
        control,
        hint ? el("p", { class: "lda-hint", text: hint }) : null,
      ]);
    }

    function render() {
      const focused = document.activeElement && node.contains(document.activeElement) ? document.activeElement.id : null;
      node.innerHTML = "";
      node.appendChild(recordText());
      node.appendChild(renderForm());
      if (focused && document.getElementById(focused)) document.getElementById(focused).focus();
    }

    function renderForm() {
      const category = draft.disposition_category;
      const info = categoryInfo(category);
      const coded = isCoded(category);
      const form = el("form", {
        class: "std-form",
        id: id("form"),
        onsubmit: (event) => {
          event.preventDefault();
          submit(onSave);
        },
      });
      const update = (key, rerender) => (event) => {
        draft[key] = event.target.value;
        if (rerender) render();
        else refreshDerived();
      };

      // Category: decides which of the rest apply.
      const categories = el("div", { class: "std-categories", role: "radiogroup", "aria-label": "Disposition category" });
      ((options && options.categories) || []).forEach((option) => {
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
            error = null;
            render();
          },
        }));
      });
      form.appendChild(el("div", { class: "lda-field std-wide", id: id("category-field") }, [
        el("span", { class: "lda-field-label", text: "Disposition category" }),
        categories,
        info ? el("p", { class: "lda-hint", text: `${info.use_when} Weibull: ${info.weibull_role}` }) : null,
      ]));

      if (error) form.appendChild(el("div", { class: "lda-banner is-error std-wide", role: "alert", text: error }));

      if (category === "DUPLICATE_MISFILED") {
        const linked = el("input", { id: id("linked"), class: "lda-input", value: draft.linked_wo, placeholder: "Task ID" });
        linked.addEventListener("input", update("linked_wo"));
        form.appendChild(field("linked", "Linked WO", linked, {
          hint: "The work order this one continues or duplicates, by task ID. Its completion becomes that work order's " +
            "renewal date (diagnose now, repair later). Leave blank for a work order logged against the wrong asset.",
        }));
      } else {
        const subAssets = (options && options.sub_assets) || [];
        if (subAssets.length) {
          // Every asset under this one in Limble's hierarchy, each level indented
          // under its parent.
          const select = el("select", { id: id("sub-asset"), class: "lda-select" }, [
            el("option", { value: "", text: `${row.asset_label || row.asset_number} (whole asset)` }),
            ...subAssets.map((option) => el("option", {
              value: option.asset_number,
              text: `${"\u00a0\u00a0\u00a0".repeat(Math.max(0, (option.depth || 1) - 1))}${(option.depth || 1) > 1 ? "└ " : ""}` +
                (option.label || option.asset_number),
            })),
          ]);
          select.value = draft.sub_asset || "";
          select.addEventListener("change", update("sub_asset"));
          form.appendChild(field("sub-asset", "Sub-asset", select, {
            hint: "Where it failed. Leave at the whole asset when the work order does not say.",
          }));
        }
        const component = codeSelect("component", "COMPONENT", draft.component_code);
        component.addEventListener("change", (event) => {
          draft.component_code = event.target.value;
          // A PM resets what it renewed: start the list from the component chosen.
          if (category === "PM_RESET" && draft.component_code && !draft.renewed_items.length) {
            draft.renewed_items = [draft.component_code];
          }
          if (category === "FAILURE") draft.renewed_items = draft.renewed_items.filter((code) => code !== draft.component_code);
          render();
        });
        form.appendChild(field("component", "Component", component, {
          required: coded, hint: "The item found failed, not the one named in the request title.",
        }));
        const mechanism = codeSelect("mechanism", "FAILURE_MECHANISM", draft.failure_mechanism_code);
        mechanism.addEventListener("change", update("failure_mechanism_code", true));
        form.appendChild(field("mechanism", "Failure mechanism", mechanism, {
          required: coded, hint: "The physical process. UNK rather than a guess.",
        }));
        const mode = codeSelect("mode", "FAILURE_MODE", draft.failure_mode_code);
        mode.addEventListener("change", update("failure_mode_code", true));
        form.appendChild(field("mode", "Failure mode", mode, {
          required: coded, hint: "What the equipment or operator experienced.",
        }));
        const action = codeSelect("action", "ACTION", draft.action_code);
        action.addEventListener("change", update("action_code", true));
        form.appendChild(field("action", "Action", action, { required: coded, hint: "The most significant work done." }));
        const evidence = el("select", { id: id("evidence"), class: "lda-select" }, [
          el("option", { value: "", text: "—" }),
          ...((options && options.evidence) || []).map((option) => el("option", { value: option.code, text: option.label })),
        ]);
        evidence.value = draft.evidence || "";
        evidence.addEventListener("change", update("evidence"));
        form.appendChild(field("evidence", "Evidence", evidence, { required: coded }));

        const facts = derived();
        const defaultName = facts.defaultCause ? codeLabel("FAILURE_CAUSE", facts.defaultCause) : "none (choose a mechanism)";
        const cause = codeSelect("cause", "FAILURE_CAUSE", draft.failure_cause_code, `Default: ${defaultName}`);
        cause.addEventListener("change", update("failure_cause_code", true));
        form.appendChild(field("cause", "Failure cause", cause, {
          hint: "Leave on the default unless the record says otherwise; an override needs a note.",
        }));

        if (isRenewing(category)) {
          const list = el("div", { class: "std-checklist", id: id("renewed"), role: "group", "aria-label": "Renewed items" });
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
                refreshDerived();
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

        const qty = el("input", { id: id("qty"), type: "number", min: "1", step: "1", class: "lda-input", value: draft.qty });
        qty.addEventListener("input", update("qty"));
        form.appendChild(field("qty", "Qty", qty, { hint: "For several identical units at once (risers, guns, nozzles)." }));
        const detail = el("input", { id: id("item-detail"), class: "lda-input", value: draft.item_detail, maxlength: "200" });
        detail.addEventListener("input", update("item_detail"));
        form.appendChild(field("item-detail", "Item detail", detail, {
          hint: "Tells two items of one component type apart (combustion-air filter vs motor cooling filter).",
        }));
      }

      // Record class: what kind of work order this is, which the CMMS guessed.
      const classes = (options && options.record_classes) || [];
      if (classes.length) {
        const select = el("select", { id: id("record-class"), class: "lda-select" }, classes.map((code) => el("option", { value: code, text: code })));
        if (draft.record_class && !classes.includes(draft.record_class)) select.appendChild(el("option", { value: draft.record_class, text: draft.record_class }));
        select.value = draft.record_class || "";
        select.addEventListener("change", update("record_class"));
        const auto = row.record_class_auto;
        form.appendChild(field("record-class", "Record class", select, {
          hint: auto && auto !== draft.record_class
            ? `Read from Limble as ${auto}; saving keeps your correction on the record.`
            : "What kind of work order this is. A PM decides the detection method (periodic maintenance).",
        }));
      }

      const note = el("input", { id: id("note"), class: "lda-input", value: draft.disposition_note, maxlength: "2000" });
      note.addEventListener("input", update("disposition_note"));
      form.appendChild(field("note", "Disposition note", note, {
        wide: true,
        required: category === "HELD" || Boolean(draft.failure_cause_code),
        hint: "One line. Required for HELD and when the cause is overridden.",
      }));

      form.appendChild(el("dl", { class: "std-derived std-wide", id: id("derived") }));
      form.appendChild(el("div", { class: "lda-row-actions std-wide", id: id("actions") }, [
        onCancel ? el("button", { type: "button", class: "btn-secondary", text: "Cancel", onclick: () => onCancel() }) : null,
        el("button", { type: "button", class: "btn-secondary", text: "Revert", onclick: () => {
          draft = draftOf(row);
          error = null;
          render();
        } }),
        el("button", { type: "submit", class: onSaveNext ? "btn-secondary" : "btn-primary", text: saveLabel || "Save" }),
        onSaveNext ? el("button", { type: "button", class: "btn-primary", text: "Save & next", onclick: () => submit(onSaveNext) }) : null,
      ]));
      form.appendChild(el("div", { class: "std-wide" }, [
        el("h3", { class: "lda-hint", text: "History" }),
        el("ol", { class: "std-history", id: id("history") }),
      ]));
      setTimeout(() => {
        refreshDerived();
        renderHistory();
      }, 0);
      return form;
    }

    function refreshDerived() {
      const target = document.getElementById(id("derived"));
      if (!target) return;
      const facts = derived();
      const category = draft.disposition_category;
      const pairs = [
        ["Location", facts.location],
        ["Renewal stream", facts.stream || "— (no component)"],
        ["Cause (effective)", facts.cause ? codeLabel("FAILURE_CAUSE", facts.cause) : "—"],
        ["Detection", facts.detection ? DETECTION_LABELS[facts.detection] : "—"],
        ["Renews the item", category === "FAILURE" ? (facts.renews ? "Yes — the clock restarts" : "No — the clock does not restart") : "—"],
      ];
      if (isRenewing(category) && draft.renewed_items.length) {
        pairs.push(["Also renews", draft.renewed_items.map((code) => `${facts.location} | ${code}`).join(", ")]);
      }
      target.innerHTML = "";
      pairs.forEach(([label, value]) => {
        target.appendChild(el("dt", { text: label }));
        target.appendChild(el("dd", { text: value }));
      });
    }

    function renderHistory() {
      const list = document.getElementById(id("history"));
      if (!list) return;
      list.innerHTML = "";
      if (historyEntries === null) {
        list.appendChild(el("li", { text: "Loading…" }));
        return;
      }
      if (!historyEntries.length) list.appendChild(el("li", { text: "Never dispositioned under this standard." }));
      historyEntries.forEach((entry) => {
        const codesText = [entry.sub_asset, entry.component_code, entry.failure_mechanism_code, entry.failure_mode_code, entry.action_code]
          .filter(Boolean).join(" · ");
        list.appendChild(el("li", {
          text: `${day(entry.decided_at)} ${entry.decided_by || ""} — ${entry.disposition_category}` +
            (codesText ? ` · ${codesText}` : "") + (entry.is_current ? " (current)" : ""),
        }));
      });
    }

    function payload() {
      const category = draft.disposition_category;
      const blank = (value) => (value === "" ? null : value);
      const base = {
        mapped_record_id: row.mapped_record_id,
        disposition_category: category,
        record_class: blank(draft.record_class),
        disposition_note: blank(draft.disposition_note.trim()),
      };
      if (category === "DUPLICATE_MISFILED") return { ...base, linked_wo: blank(draft.linked_wo.trim()) };
      return {
        ...base,
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
      };
    }

    async function submit(handler) {
      if (!handler) return;
      try {
        await handler(payload());
        error = null;
        saved = JSON.parse(JSON.stringify(draft));
      } catch (err) {
        if (err && err.toastShown) return;
        error = (err && err.message) || "The disposition could not be saved.";
        render();
      }
    }

    render();
    return {
      node,
      id,
      isDirty: () => JSON.stringify(draft) !== JSON.stringify(saved),
      setHistory(entries) {
        historyEntries = entries;
        renderHistory();
      },
      focus() {
        const first = node.querySelector(".std-category.is-active") || node.querySelector("select, input");
        if (first) first.focus();
      },
    };
  }

  window.GremlinDispositionForm = { create, categoryChip };
})();
