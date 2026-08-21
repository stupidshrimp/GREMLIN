/* Availability configuration editor on the Configuration page.
 *
 * Holds the structural configuration behind the Metrics page's Availability
 * card: each asset group's shift schedule and membership, and the linked
 * downtime rules. These live there rather than on the card because they are
 * rare edits with a wide blast radius -- availability is always recomputed from
 * the current schedule, so changing one of these values moves every month that
 * has data, including months already reported. The page keeps them behind
 * collapsed panels for the same reason; this renders into those panels whether
 * or not one is open yet.
 *
 * Per-month values (Goal % and manual OT hours) are edited inline on the card
 * instead: they are keyed by month, so editing one cannot disturb another.
 */
(function () {
  "use strict";

  const CONFIG_API = "/metrics/api/availability/config";
  const RULES_API = "/metrics/api/availability/linked-rules";
  const DISPLAY_NAME_API = "/metrics/api/availability/display-name";

  const groupsHost = document.getElementById("config-availability-groups");
  const rulesHost = document.getElementById("config-linked-rules");
  if (!groupsHost) return;

  let config = null;

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.entries(attrs).forEach(([key, value]) => {
        if (value === null || value === undefined || value === false) return;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
        else if (value === true) node.setAttribute(key, "");
        else node.setAttribute(key, value);
      });
    }
    (children || []).forEach((child) => {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function show(hostId, message, kind) {
    if (kind === "error" && /log in|role is required|guest access is read-only/i.test(message) && window.gremlinToast) {
      window.gremlinToast(message);
      return;
    }
    const node = document.getElementById(hostId);
    if (!node) return;
    node.textContent = message;
    node.className = "lda-banner" + (kind ? " is-" + kind : "");
    node.hidden = !message;
  }

  async function request(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = data.error || `Request failed (${response.status}).`;
      if ((response.status === 401 || response.status === 403) && window.gremlinToast) window.gremlinToast(message);
      throw new Error(message);
    }
    return data;
  }

  function netHours(values) {
    // Mirrors the server's derivation exactly (design doc §3) so the preview
    // never disagrees with what gets stored.
    return Math.max(0, values.schedule - (values.brk + values.lunch + values.setup));
  }

  function renderGroup(group) {
    const inputs = {};
    const netLabel = el("strong", { text: `${group.net_scheduled_hours_per_day.toFixed(1)} h` });

    function readValues() {
      return {
        schedule: Number(inputs.schedule.value),
        brk: Number(inputs.brk.value),
        lunch: Number(inputs.lunch.value),
        setup: Number(inputs.setup.value),
      };
    }

    function refreshNet() {
      const values = readValues();
      const net = netHours(values);
      netLabel.textContent = `${net.toFixed(1)} h`;
      const invalid = values.brk + values.lunch + values.setup > values.schedule;
      netLabel.style.color = invalid ? "#c0392b" : "";
      return !invalid;
    }

    function field(key, label, value) {
      const input = el("input", {
        type: "number", min: "0", max: "24", step: "0.5",
        class: "availability-input", value: String(value),
      });
      input.addEventListener("input", refreshNet);
      inputs[key] = input;
      return el("label", { class: "availability-config-field" }, [
        el("span", { text: label }),
        input,
      ]);
    }

    const includeBox = el("input", { type: "checkbox", checked: group.include });
    const assetsInput = el("input", {
      type: "text",
      class: "availability-config-assets",
      value: group.asset_numbers.join(", "),
    });

    const save = el("button", { type: "button", class: "btn-secondary", text: "Save" });
    save.addEventListener("click", async () => {
      const values = readValues();
      if (!refreshNet()) {
        show("config-availability-status",
          `${group.asset_group}: break, lunch and setup exceed the scheduled hours — net would be negative.`,
          "error");
        return;
      }
      // The recompute is global, so make the reader confirm it knowingly.
      const proceed = window.confirm(
        `Save the ${group.asset_group} schedule?\n\n` +
        "This changes availability for every month, including ones already reported."
      );
      if (!proceed) return;
      save.disabled = true;
      try {
        config = await request(`${CONFIG_API}/group/${encodeURIComponent(group.asset_group)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            schedule_hours_per_day: values.schedule,
            break_hours_per_day: values.brk,
            lunch_hours_per_day: values.lunch,
            setup_hours_per_day: values.setup,
            include: includeBox.checked,
            asset_numbers: assetsInput.value.split(",").map((s) => s.trim()).filter(Boolean),
          }),
        });
        show("config-availability-status", `Saved ${group.asset_group}.`, "success");
        renderGroups();
      } catch (err) {
        show("config-availability-status", err.message || "Could not save.", "error");
      } finally {
        save.disabled = false;
      }
    });

    const reset = el("button", { type: "button", class: "btn-secondary", text: "Reset to defaults" });
    reset.addEventListener("click", async () => {
      if (!window.confirm(`Restore the seeded schedule and asset list for ${group.asset_group}?`)) return;
      try {
        config = await request(
          `${CONFIG_API}/group/${encodeURIComponent(group.asset_group)}/reset`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          }
        );
        show("config-availability-status", `${group.asset_group} restored to defaults.`, "success");
        renderGroups();
      } catch (err) {
        show("config-availability-status", err.message || "Could not reset.", "error");
      }
    });

    // Display names are what the chart labels its bars with, so they are edited
    // next to the membership that decides which bars exist. Saved per asset on
    // blur rather than batched with the schedule: a label is a cosmetic fix
    // someone makes in passing, and it carries none of the schedule's
    // recompute-every-month weight.
    const displayNames = el("div", { class: "availability-config-names" },
      group.asset_numbers.map((asset) => {
        const input = el("input", {
          type: "text",
          class: "availability-config-name-input",
          value: (config.display_names || {})[asset] || asset,
          "aria-label": `Display name for asset ${asset}`,
        });
        input.addEventListener("change", async () => {
          const name = input.value.trim();
          if (!name) {
            input.value = (config.display_names || {})[asset] || asset;
            show("config-availability-status", "A display name cannot be blank.", "error");
            return;
          }
          try {
            await request(DISPLAY_NAME_API, {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ asset_number: asset, display_name: name }),
            });
            config.display_names = config.display_names || {};
            config.display_names[asset] = name;
            show("config-availability-status", `Renamed ${asset} to “${name}”.`, "success");
          } catch (err) {
            show("config-availability-status", err.message || "Could not save the display name.", "error");
          }
        });
        return el("label", { class: "availability-config-field" }, [
          el("span", { text: asset }),
          input,
        ]);
      })
    );

    return el("details", { class: "availability-config-group" }, [
      el("summary", {}, [
        el("span", { text: group.asset_group }),
        el("span", {
          class: "availability-config-summary",
          text: `${group.net_scheduled_hours_per_day.toFixed(1)} net h/day · ` +
                `${group.asset_numbers.length} asset(s)${group.include ? "" : " · excluded"}`,
        }),
      ]),
      el("div", { class: "availability-config-body" }, [
        el("div", { class: "availability-config-row" }, [
          field("schedule", "Schedule h/day", group.schedule_hours_per_day),
          field("brk", "Break h/day", group.break_hours_per_day),
          field("lunch", "Lunch h/day", group.lunch_hours_per_day),
          field("setup", "Setup h/day", group.setup_hours_per_day),
          el("div", { class: "availability-config-field" }, [
            el("span", { text: "Net h/day (derived)" }),
            netLabel,
          ]),
        ]),
        el("label", { class: "availability-config-field availability-config-wide" }, [
          el("span", { text: "Asset numbers (comma separated)" }),
          assetsInput,
        ]),
        group.asset_numbers.length
          ? el("div", { class: "availability-config-wide" }, [
              el("p", { class: "availability-config-names-label", text: "Chart labels" }),
              displayNames,
            ])
          : null,
        el("label", { class: "availability-config-check" }, [
          includeBox,
          el("span", { text: "Include this group on the dashboard" }),
        ]),
        group.notes ? el("p", { class: "metrics-hint", text: group.notes }) : null,
        el("div", { class: "lda-row-actions", style: "justify-content:flex-start" }, [save, reset]),
      ]),
    ]);
  }

  function renderGroups() {
    groupsHost.innerHTML = "";
    if (!config || !config.groups.length) {
      groupsHost.appendChild(el("p", { class: "metrics-hint", text: "No asset groups are configured." }));
      return;
    }
    config.groups.forEach((group) => groupsHost.appendChild(renderGroup(group)));
    renderRules();
  }

  function renderRules() {
    if (!rulesHost) return;
    rulesHost.innerHTML = "";
    const rules = (config.linked_rules || []).map((rule) => ({ ...rule }));

    const rows = el("tbody", {});
    function addRow(rule) {
      const parent = el("input", { type: "text", class: "availability-input", value: rule.parent_asset_number || "" });
      const linked = el("input", { type: "text", class: "availability-input", value: rule.linked_asset_number || "" });
      const factor = el("input", {
        type: "number", min: "0", max: "1", step: "0.05",
        class: "availability-input",
        value: String(rule.impact_factor === undefined ? 0.5 : rule.impact_factor),
      });
      const remove = el("button", { type: "button", class: "btn-secondary", text: "Remove" });
      const row = el("tr", {}, [
        el("td", {}, [parent]), el("td", {}, [linked]), el("td", {}, [factor]), el("td", {}, [remove]),
      ]);
      remove.addEventListener("click", () => row.remove());
      row._read = () => ({
        rule_group: rule.rule_group || "",
        parent_asset_number: parent.value.trim(),
        linked_asset_number: linked.value.trim(),
        impact_factor: Number(factor.value),
      });
      rows.appendChild(row);
    }
    rules.forEach(addRow);

    const add = el("button", { type: "button", class: "btn-secondary", text: "Add rule" });
    add.addEventListener("click", () => addRow({ impact_factor: 0.5 }));

    const save = el("button", { type: "button", class: "btn-secondary", text: "Save rules" });
    save.addEventListener("click", async () => {
      const payload = Array.from(rows.querySelectorAll("tr")).map((row) => row._read());
      if (!window.confirm(
        "Save linked downtime rules?\n\n" +
        "This changes availability for every month, including ones already reported."
      )) return;
      save.disabled = true;
      try {
        config = await request(RULES_API, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rules: payload }),
        });
        show("config-linked-status", `Saved ${payload.length} rule(s).`, "success");
        renderGroups();
      } catch (err) {
        show("config-linked-status", err.message || "Could not save rules.", "error");
      } finally {
        save.disabled = false;
      }
    });

    rulesHost.appendChild(
      el("div", { class: "metrics-table-scroll" }, [
        el("table", { class: "metrics-table" }, [
          el("thead", {}, [
            el("tr", {}, [
              el("th", { text: "Parent asset" }),
              el("th", { text: "Linked asset" }),
              el("th", { text: "Impact factor (0–1)" }),
              el("th", { text: "" }),
            ]),
          ]),
          rows,
        ]),
      ])
    );
    rulesHost.appendChild(el("div", { class: "lda-row-actions", style: "justify-content:flex-start" }, [add, save]));
  }

  async function load() {
    try {
      config = await request(CONFIG_API, {});
      renderGroups();
    } catch (err) {
      groupsHost.innerHTML = "";
      groupsHost.appendChild(el("p", { class: "metrics-empty", text: err.message || "Could not load configuration." }));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
