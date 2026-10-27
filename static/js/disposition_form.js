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
 * With the disposition helper on, the page hands the form a suggestion
 * (form.setSuggestion): a work order nobody has decided is filled in from it,
 * field by field where the helper is sure enough, each field shows how sure it
 * is, and a panel above the fields says why. Nothing is saved until Save.
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
    if (source === "AUTO") {
      chips.push(" ", el("span", {
        class: "std-chip is-auto",
        text: "Auto",
        title: "Dispositioned by the disposition helper; not yet confirmed by an engineer.",
      }));
    }
    return chips;
  }

  // A confidence as the helper's chips show it, and the band that colours it.
  const pct = (value) => `${Math.round((Number(value) || 0) * 100)}%`;
  const band = (value) => (value >= 0.8 ? "is-high" : value >= 0.5 ? "is-mid" : "is-low");
  // The fields the helper suggests, as the form names them.
  const HELPER_FIELDS = {
    disposition_category: "Category",
    sub_asset: "Sub-asset",
    component_code: "Component",
    failure_mechanism_code: "Failure mechanism",
    failure_mode_code: "Failure mode",
    action_code: "Action",
  };

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

  // The field of a disposition each code list fills.
  const CODE_FIELDS = {
    COMPONENT: "component_code",
    FAILURE_MECHANISM: "failure_mechanism_code",
    FAILURE_MODE: "failure_mode_code",
    ACTION: "action_code",
    FAILURE_CAUSE: "failure_cause_code",
  };

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
    // The disposition helper's suggestion for this work order, when the page
    // asked for one: "loading", the suggestion, or an error message.
    let helper = { status: "off", suggestion: null, error: null };
    // The fields filled in from the suggestion, to mark them until changed.
    const filled = new Set();
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
        // A work order filed under the asset can name the sub-asset it was on.
        row.logged_asset_name ? fact("Asset named on the work order", row.logged_asset_name, true) : null,
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

    function field(name, labelText, control, { required, hint, wide, fieldId, helperKey } = {}) {
      return el("div", { class: "lda-field" + (wide ? " std-wide" : "") + (helperKey && isFilled(helperKey) ? " is-helper-filled" : ""), id: fieldId || null }, [
        el("div", { class: "std-field-head" }, [
          el("label", { for: id(name) }, [labelText, required ? el("span", { class: "std-required", text: "*", title: "Required for this category" }) : null]),
          helperKey ? helperChip(helperKey) : null,
        ]),
        control,
        hint ? el("p", { class: "lda-hint", text: hint }) : null,
      ]);
    }

    // ---- the disposition helper -------------------------------------------
    function suggested(key) {
      const s = helper.suggestion;
      return s && s.fields && s.fields[key] ? s.fields[key] : null;
    }

    // Beside a field's label: how sure the helper is of what the field holds,
    // or, when it holds something else, the helper's value as a button.
    function helperChip(key) {
      const f = suggested(key);
      if (!f || f.value === undefined || (f.value === null && key !== "sub_asset")) return null;
      const value = f.value || "";
      const why = [f.name, ...(f.reasons || [])].filter(Boolean).join(" · ");
      if ((draft[key] || "") === value) {
        return el("span", {
          class: `std-helper-chip ${band(f.confidence)}`,
          title: `Disposition helper: ${pct(f.confidence)} sure. ${why}`,
          "aria-label": `Disposition helper is ${pct(f.confidence)} sure of this`,
          text: pct(f.confidence),
        });
      }
      return el("button", {
        type: "button",
        class: `std-helper-chip is-offer ${band(f.confidence)}`,
        title: `Use the disposition helper's suggestion. ${why}`,
        text: `Helper: ${key === "disposition_category" ? f.name : value || "whole asset"} · ${pct(f.confidence)}`,
        onclick: () => useSuggested(key),
      });
    }

    // Whether a field still holds what the helper filled it with.
    const isFilled = (key) => {
      const f = suggested(key);
      return filled.has(key) && Boolean(f) && (draft[key] || "") === (f.value || "");
    };

    function useSuggested(key) {
      const f = suggested(key);
      if (!f) return;
      draft[key] = f.value || "";
      filled.add(key);
      if (key === "disposition_category") error = null;
      render();
    }

    // Fill the form from the suggestion: each empty field the helper is at
    // least `threshold` sure of (all of them, with `everything`). The category
    // only replaces NOT_REVIEWED, and the coded fields are filled only when the
    // category is the one they were read for.
    function fillFromSuggestion(everything) {
      const s = helper.suggestion;
      if (!s) return 0;
      const threshold = everything ? 0 : s.prefill_confidence;
      const f = s.fields;
      let count = 0;
      const category = f.disposition_category;
      if (category && category.value && category.confidence >= threshold &&
          (draft.disposition_category === "NOT_REVIEWED" || (everything && draft.disposition_category !== category.value))) {
        draft.disposition_category = category.value;
        filled.add("disposition_category");
        count += 1;
      }
      if (!category || draft.disposition_category !== category.value) return count;
      if (!isCoded(draft.disposition_category)) return count;
      const put = (key, value, confidence) => {
        if (value === null || value === undefined || value === "") return;
        if (confidence < threshold || (draft[key] && !everything) || draft[key] === value) return;
        draft[key] = value;
        filled.add(key);
        count += 1;
      };
      put("sub_asset", f.sub_asset && f.sub_asset.value, f.sub_asset ? f.sub_asset.confidence : 0);
      ["component_code", "failure_mechanism_code", "failure_mode_code", "action_code"].forEach((key) => {
        if (f[key]) put(key, f[key].value, f[key].confidence);
      });
      if (filled.size && !draft.evidence && f.evidence && f.evidence.value) {
        draft.evidence = f.evidence.value;
        filled.add("evidence");
      }
      const cause = f.failure_cause_code;
      if (cause && cause.value && !draft.failure_cause_code && draft.failure_mechanism_code === s.disposition.failure_mechanism_code) {
        draft.failure_cause_code = cause.value;
        if (!draft.disposition_note && cause.note) draft.disposition_note = cause.note;
        filled.add("failure_cause_code");
      }
      const renewed = f.renewed_items;
      if (isRenewing(draft.disposition_category) && draft.disposition_category === "PM_RESET" && !draft.renewed_items.length &&
          renewed && renewed.value && renewed.value.length) {
        draft.renewed_items = renewed.value.slice();
        filled.add("renewed_items");
      }
      return count;
    }

    // Whether a work order is the helper's to fill in: nobody has decided it,
    // or only the migration or the helper itself has. An engineer's decision is
    // never filled over; the helper's view of it is only offered.
    const fillable = () => row.disposition.source !== "ENGINEER" || row.disposition.disposition_category === "NOT_REVIEWED";

    function helperPanel() {
      if (helper.status === "off") return null;
      const head = (children) => el("div", { class: "std-helper-head" }, [
        el("span", { class: "std-helper-title" }, [el("span", { class: "std-helper-icon", "aria-hidden": "true", text: "✦" }), "Disposition helper"]),
        ...children,
      ]);
      if (helper.status === "loading") {
        return el("section", { class: "std-helper", id: id("helper"), "aria-busy": "true" }, [
          head([el("span", { class: "lda-hint" }, [el("span", { class: "lda-spinner is-small", "aria-hidden": "true" }), " Reading the work order…"])]),
        ]);
      }
      if (helper.status === "error") {
        return el("section", { class: "std-helper", id: id("helper") }, [
          head([el("span", { class: "lda-hint", text: helper.error || "The helper could not read this work order." })]),
        ]);
      }
      const s = helper.suggestion;
      const f = s.fields;
      const category = f.disposition_category;
      const filledCount = filled.size;
      const summary = filledCount
        ? `Filled in ${filledCount} field${filledCount === 1 ? "" : "s"} from what it read; check them, then save.`
        : fillable()
          ? "Not sure enough to fill anything in; its suggestions are beside each field."
          : "This work order is already dispositioned; the helper's view is beside each field.";
      const actions = el("div", { class: "std-helper-actions" }, [
        filledCount ? el("button", {
          type: "button",
          class: "btn-secondary",
          text: "Clear what it filled",
          onclick: () => {
            draft = draftOf(row);
            filled.clear();
            error = null;
            render();
          },
        }) : null,
        el("button", {
          type: "button",
          class: "btn-secondary",
          text: "Use every suggestion",
          title: "Fill in every field the helper has a suggestion for, however sure it is.",
          onclick: () => {
            fillFromSuggestion(true);
            error = null;
            render();
          },
        }),
      ]);
      const blocks = [
        head([
          el("span", {
            class: `std-helper-overall ${band(s.overall_confidence)}`,
            title: s.calibrated
              ? "How often the helper's past suggestions this sure matched the engineer's disposition."
              : "An estimate: the helper has not yet measured itself on enough dispositioned work orders.",
          }, [
            el("span", { class: "std-helper-meter", "aria-hidden": "true" }, [
              el("span", { style: `width:${Math.round(s.overall_confidence * 100)}%` }),
            ]),
            `${pct(s.overall_confidence)} sure${s.calibrated ? "" : " (estimate)"}`,
          ]),
        ]),
        el("p", { class: "std-helper-summary", text: `${category && category.value ? `Reads as ${category.name}. ` : ""}${summary}` }),
        actions,
      ];
      (s.possible_duplicates || []).slice(0, 1).forEach((dup) => {
        blocks.push(el("div", { class: "std-helper-note is-warning" }, [
          `Task ${dup.task_id} on this asset reads much the same (${pct(dup.similarity)} alike` +
            (dup.days_apart !== null && dup.days_apart !== undefined ? `, ${dup.days_apart} day${dup.days_apart === 1 ? "" : "s"} apart` : "") +
            `): “${dup.title}”. `,
          el("button", {
            type: "button",
            class: "std-link-btn",
            text: `Mark this one a duplicate of ${dup.task_id}`,
            onclick: () => {
              draft.disposition_category = "DUPLICATE_MISFILED";
              draft.linked_wo = String(dup.task_id);
              filled.add("disposition_category");
              error = null;
              render();
            },
          }),
        ]));
      });
      (s.notes || []).filter((note) => !/reads much the same/.test(note)).forEach((note) => {
        blocks.push(el("p", { class: "std-helper-note", text: note }));
      });
      const why = el("details", { class: "std-helper-why" }, [
        el("summary", { text: "Why these suggestions" }),
        el("dl", { class: "std-helper-reasons" }, Object.entries(HELPER_FIELDS).flatMap(([key, label]) => {
          const entry = f[key];
          if (!entry || entry.value === undefined) return [];
          const alternatives = (entry.alternatives || []).map((alt) => el("button", {
            type: "button",
            class: "std-helper-alt",
            title: (alt.reasons || []).join(" · ") || null,
            text: `${alt.name || alt.value || "whole asset"} ${pct(alt.confidence)}`,
            onclick: () => {
              draft[key] = alt.value || "";
              filled.add(key);
              render();
            },
          }));
          return [
            el("dt", { text: label }),
            el("dd", {}, [
              el("strong", { text: entry.name || "—" }),
              ` · ${pct(entry.confidence)}`,
              (entry.reasons || []).length ? el("span", { class: "std-helper-because", text: ` — ${entry.reasons.join("; ")}` }) : null,
              alternatives.length ? el("span", { class: "std-helper-alts" }, ["Or: ", ...alternatives]) : null,
            ]),
          ];
        })),
      ]);
      blocks.push(why);
      if ((s.similar || []).length) {
        blocks.push(el("details", { class: "std-helper-why" }, [
          el("summary", { text: `Similar work orders already dispositioned (${s.similar.length})` }),
          el("ul", { class: "std-helper-similar" }, s.similar.map((item) => el("li", {}, [
            el("span", { class: "std-helper-sim", text: pct(item.similarity) }),
            el("span", { class: "std-helper-sim-task", text: `Task ${item.task_id}` }),
            el("span", { class: "std-helper-sim-title", text: item.title || "(no title)" }),
            el("span", {
              class: "std-helper-sim-codes",
              text: [
                item.disposition_category,
                item.sub_asset_label || (item.same_asset ? null : item.asset_number),
                item.component_code,
                item.failure_mechanism_code,
                item.failure_mode_code,
                item.action_code,
              ].filter(Boolean).join(" · "),
            }),
          ]))),
        ]));
      }
      return el("section", { class: "std-helper", id: id("helper"), "aria-label": "Disposition helper" }, blocks);
    }

    function render() {
      const focused = document.activeElement && node.contains(document.activeElement) ? document.activeElement.id : null;
      node.innerHTML = "";
      node.appendChild(recordText());
      const panel = helperPanel();
      if (panel) node.appendChild(panel);
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
      form.appendChild(el("div", {
        class: "lda-field std-wide" + (isFilled("disposition_category") ? " is-helper-filled" : ""),
        id: id("category-field"),
      }, [
        el("div", { class: "std-field-head" }, [
          el("span", { class: "lda-field-label", text: "Disposition category" }),
          helperChip("disposition_category"),
        ]),
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
        // Every asset under this one, as the PM calendar's picker has them: each
        // level indented under its parent. Shown even with none, so the note
        // says where the list came from.
        const subAssets = (options && options.sub_assets) || [];
        const select = el("select", { id: id("sub-asset"), class: "lda-select" }, [
          el("option", { value: "", text: `${row.asset_number} (whole asset)` }),
          ...subAssets.map((option) => el("option", {
            value: option.asset_number,
            text: `${"\u00a0\u00a0\u00a0".repeat(Math.max(0, (option.depth || 1) - 1))}${(option.depth || 1) > 1 ? "└ " : ""}` +
              (option.label || option.asset_number),
          })),
        ]);
        if (draft.sub_asset && !subAssets.some((option) => option.asset_number === draft.sub_asset)) {
          // Saved before the hierarchy said otherwise: still shown, so the record reads true.
          select.appendChild(el("option", { value: draft.sub_asset, text: `${draft.sub_asset} (no longer under ${row.asset_number})` }));
        }
        select.value = draft.sub_asset || "";
        select.addEventListener("change", update("sub_asset", true));
        form.appendChild(field("sub-asset", "Sub-asset", select, {
          helperKey: "sub_asset",
          hint: "Where it failed; leave at the whole asset when the work order does not say. " +
            ((options && options.sub_asset_note) || ""),
        }));
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
          helperKey: "component_code", required: coded, hint: "The item found failed, not the one named in the request title.",
        }));
        const mechanism = codeSelect("mechanism", "FAILURE_MECHANISM", draft.failure_mechanism_code);
        mechanism.addEventListener("change", update("failure_mechanism_code", true));
        form.appendChild(field("mechanism", "Failure mechanism", mechanism, {
          helperKey: "failure_mechanism_code", required: coded, hint: "The physical process. UNK rather than a guess.",
        }));
        const mode = codeSelect("mode", "FAILURE_MODE", draft.failure_mode_code);
        mode.addEventListener("change", update("failure_mode_code", true));
        form.appendChild(field("mode", "Failure mode", mode, {
          helperKey: "failure_mode_code", required: coded, hint: "What the equipment or operator experienced.",
        }));
        const action = codeSelect("action", "ACTION", draft.action_code);
        action.addEventListener("change", update("action_code", true));
        form.appendChild(field("action", "Action", action, {
          helperKey: "action_code", required: coded, hint: "The most significant work done.",
        }));
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
          filled.clear();
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
      const s = helper.suggestion;
      if (s) {
        // What the helper suggested, kept with the save so how often its
        // suggestions are kept can be measured.
        base.helper_suggestion = {
          v: s.helper_version,
          overall: s.overall_confidence,
          filled: Array.from(filled),
          fields: Object.fromEntries(Object.keys(HELPER_FIELDS).filter((key) => s.fields[key]).map((key) => [
            key, { value: s.fields[key].value, confidence: s.fields[key].confidence },
          ])),
        };
      }
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
      // The code lists changed while this work order is open (a code added,
      // renamed, retired or deleted): offer them now, keeping everything typed.
      setCodeLists(codeLists) {
        options = { ...options, code_lists: codeLists };
        render();
      },
      // A code just added to a list: set it on this work order's field for that
      // list, as an unsaved change. False when the category has no such field.
      useCode(codeList, code) {
        const field = CODE_FIELDS[codeList];
        if (!field || draft.disposition_category === "DUPLICATE_MISFILED") return false;
        draft[field] = code;
        render();
        return true;
      },
      focus() {
        const first = node.querySelector(".std-category.is-active") || node.querySelector("select, input");
        if (first) first.focus();
      },
      // The disposition helper is reading this work order.
      setHelperLoading() {
        helper = { status: "loading", suggestion: null, error: null };
        render();
      },
      setHelperError(message) {
        helper = { status: "error", suggestion: null, error: message };
        render();
      },
      // The helper's suggestion. With `prefill`, a work order nobody has decided
      // (and nothing typed into yet) is filled in from it; what it filled counts
      // as the form's starting point, so leaving it unsaved asks nothing.
      setSuggestion(suggestion, { prefill } = {}) {
        const untouched = JSON.stringify(draft) === JSON.stringify(saved);
        helper = { status: "ready", suggestion, error: null };
        if (prefill && untouched && fillable() && fillFromSuggestion(false)) {
          saved = JSON.parse(JSON.stringify(draft));
        }
        render();
      },
      // The helper turned off: its suggestion goes, and so does what it filled
      // in, unless something has been typed since.
      clearSuggestion() {
        const untouched = JSON.stringify(draft) === JSON.stringify(saved);
        helper = { status: "off", suggestion: null, error: null };
        if (untouched && filled.size) {
          draft = draftOf(row);
          saved = draftOf(row);
        }
        filled.clear();
        render();
      },
    };
  }

  window.GremlinDispositionForm = { create, categoryChip };
})();
