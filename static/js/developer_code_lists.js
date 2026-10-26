/* Code lists: download every code list as a workbook, upload one back.
 *
 * The lists themselves come from the same read the Disposition page's Code lists
 * dialog uses, so the tiles and the table show exactly what the dropdowns offer.
 * An upload posts the file as a form, with the page's CSRF token, and the reply
 * carries the lists as saved, so the page redraws from that rather than
 * re-asking.
 *
 * Every value from the database is written with textContent, never innerHTML.
 */
(function () {
  "use strict";

  const dev = window.GremlinDev;
  const $ = dev.$;
  const el = dev.el;

  const LISTS_URL = "/life-data-analysis/api/code-lists";
  const UPLOAD_URL = "/developer/api/code-lists/excel";

  // The order the Disposition page's dropdowns and the workbook take the lists
  // in; the JSON reply has them alphabetical.
  const LIST_ORDER = ["COMPONENT", "FAILURE_MECHANISM", "FAILURE_MODE", "FAILURE_CAUSE", "ACTION"];

  const state = { codeLists: null, list: null, busy: false };

  function listKeys() {
    const keys = Object.keys(state.codeLists);
    return LIST_ORDER.filter((key) => keys.includes(key)).concat(keys.filter((key) => !LIST_ORDER.includes(key)));
  }

  function plural(count, one, many) {
    return `${count.toLocaleString()} ${count === 1 ? one : many || `${one}s`}`;
  }

  function drawStats() {
    const grid = $("dev-codes-stats");
    grid.textContent = "";
    listKeys().forEach((key) => {
      const list = state.codeLists[key];
      const active = list.codes.filter((code) => code.is_active).length;
      const retired = list.codes.length - active;
      const box = el("div", "dev-stat");
      box.appendChild(el("span", "dev-stat-label", list.label));
      box.appendChild(el("span", "dev-stat-value", active.toLocaleString()));
      box.appendChild(el("span", "dev-stat-note", retired ? `active, ${retired.toLocaleString()} retired` : "active"));
      grid.appendChild(box);
    });
  }

  function drawListPicker() {
    const select = $("dev-codes-list");
    const keys = listKeys();
    if (!state.list || !keys.includes(state.list)) state.list = keys[0];
    select.textContent = "";
    keys.forEach((key) => {
      const option = el("option", null, state.codeLists[key].label);
      option.value = key;
      select.appendChild(option);
    });
    select.value = state.list;
  }

  function chip(modifier, text, title) {
    const node = el("span", modifier ? `cl-chip ${modifier}` : "cl-chip", text);
    if (title) node.title = title;
    return node;
  }

  // The codes as the Disposition page's Code lists dialog shows them: each
  // code's name over its whole description, and chips for the rest.
  function drawTable() {
    const list = state.codeLists[state.list];
    const meanings = {};
    list.semantics.forEach((semantic) => {
      meanings[semantic.code] = semantic;
    });
    const causes = {};
    ((state.codeLists.FAILURE_CAUSE && state.codeLists.FAILURE_CAUSE.codes) || []).forEach((cause) => {
      causes[cause.code] = cause.name;
    });
    const none = () => el("span", "cl-none", "—");
    const text = (value) => document.createTextNode(String(value));
    // [heading, class, the cell's content]
    const columns = [
      ["Code", "cl-code", (code) => [el("span", "cl-code-pill", code.code)]],
      ["Name and description", "cl-main", (code) => {
        const name = el("div", "cl-name", code.name);
        if (!code.is_active) name.appendChild(chip("is-retired", "Retired"));
        return code.description ? [name, el("div", "cl-desc", code.description)] : [name];
      }],
    ];
    if (list.codes.some((code) => code.iso_ref)) {
      columns.push(["ISO ref", "cl-meta", (code) => [code.iso_ref ? text(code.iso_ref) : none()]]);
    }
    if (state.list === "FAILURE_MECHANISM") {
      columns.push(["Default cause", "cl-fit", (code) => [code.default_cause_code
        ? chip(null, causes[code.default_cause_code] || code.default_cause_code, code.default_cause_code)
        : none()]]);
    }
    if (state.list === "ACTION") {
      columns.push(["Renews item", "cl-fit", (code) => [code.renews_item ? chip("is-renews", "Renews") : none()]]);
    }
    if (list.semantics.length) {
      columns.push(["Means", "cl-fit", (code) => {
        const meaning = meanings[code.semantic];
        if (!code.semantic) return [none()];
        return [chip("is-meaning", meaning ? meaning.short : code.semantic, meaning ? meaning.label : code.semantic)];
      }]);
    }
    columns.push(["Order", "cl-meta cl-num", (code) => [text(code.sort_order)]]);

    const table = el("table", "cl-table");
    const head = el("tr");
    columns.forEach(([label, className]) => {
      const th = el("th", className, label);
      th.scope = "col";
      head.appendChild(th);
    });
    table.appendChild(el("thead")).appendChild(head);
    const body = el("tbody");
    if (!list.codes.length) {
      const cell = el("td", "cl-empty", "This list has no codes.");
      cell.colSpan = columns.length;
      body.appendChild(el("tr")).appendChild(cell);
    }
    list.codes.forEach((code) => {
      const row = el("tr", code.is_active ? "cl-row" : "cl-row is-retired");
      columns.forEach(([, className, content]) => {
        const cell = el("td", className);
        content(code).forEach((node) => cell.appendChild(node));
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
    table.appendChild(body);
    dev.replaceChildren($("dev-codes-table"), table);
  }

  function draw(codeLists) {
    state.codeLists = codeLists;
    drawStats();
    drawListPicker();
    drawTable();
  }

  function drawResult(outcome) {
    const box = $("dev-codes-result");
    box.textContent = "";
    box.hidden = false;
    const added = outcome.added || [];
    const updated = outcome.updated || [];
    const parts = [];
    parts.push(`${plural(added.length, "code")} added`);
    parts.push(`${updated.length.toLocaleString()} changed`);
    parts.push(`${outcome.unchanged.toLocaleString()} unchanged`);
    if (outcome.not_in_workbook) {
      parts.push(`${outcome.not_in_workbook.toLocaleString()} not in the workbook and kept as they were`);
    }
    box.appendChild(el("p", "dev-code-result-summary", `${parts.join(", ")}.`));
    [["Added", added], ["Changed", updated]].forEach(([title, items]) => {
      if (!items.length) return;
      box.appendChild(el("h3", null, title));
      const scroll = el("div", "dev-table-scroll");
      scroll.appendChild(dev.buildTable(["List", "Code", "Name"], items.map((item) => [item.list_label, item.code, item.name])));
      box.appendChild(scroll);
    });
  }

  async function load() {
    try {
      const payload = await dev.getJSON(LISTS_URL);
      draw(payload.code_lists);
    } catch (err) {
      dev.showStatus(err.message, true);
      $("dev-codes-stats").textContent = "";
      $("dev-codes-table").textContent = "";
    }
  }

  async function upload(file) {
    if (state.busy || !file) return;
    state.busy = true;
    const button = $("dev-codes-upload");
    button.disabled = true;
    button.textContent = "Uploading…";
    dev.showStatus("");
    $("dev-codes-result").hidden = true;
    const form = new FormData();
    form.append("file", file);
    form.append("csrf_token", $("dev-codes-csrf").value);
    try {
      const outcome = await dev.getJSON(UPLOAD_URL, { method: "POST", body: form });
      draw(outcome.code_lists);
      drawResult(outcome);
      const changed = (outcome.added || []).length + (outcome.updated || []).length;
      dev.showStatus(changed ? `Saved ${file.name}.` : `${file.name} matches the lists already; nothing to save.`);
    } catch (err) {
      dev.showStatus(err.message, true);
    } finally {
      state.busy = false;
      button.disabled = false;
      button.textContent = "Upload code lists…";
    }
  }

  function init() {
    $("dev-codes-list").addEventListener("change", (event) => {
      state.list = event.target.value;
      drawTable();
    });
    const picker = $("dev-codes-file");
    $("dev-codes-upload").addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => {
      const file = picker.files && picker.files[0];
      // Cleared at once, so choosing the same file again after fixing it still fires.
      picker.value = "";
      upload(file);
    });
    load();
  }

  dev.whenReady("#dev-codes-table", init);
})();
