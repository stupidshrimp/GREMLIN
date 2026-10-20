/* What the work-order disposition standard's two pages share: the DOM and network
 * helpers, the banner, toast and loading veil (styled by life_data_analysis.css),
 * and the Asset Number combobox. Exposed as window.GremlinStandard, loaded before
 * wo_disposition.js and population_analysis.js.
 */
(function () {
  "use strict";

  const API = "/life-data-analysis/api";
  const STANDARD_API = `${API}/standard`;
  // Whether this browser session may write. Rendered by the server; the API
  // enforces the same rule on its own, this only keeps the page from offering an
  // action that would be refused.
  const CAN_EDIT = document.querySelector('meta[name="gremlin-can-edit"]')?.content === "true";
  const $ = (id) => document.getElementById(id);

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
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs, children) {
    const node = document.createElementNS(SVG_NS, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value === null || value === undefined) return;
      if (key === "text") node.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    });
    (children || []).forEach((child) => child && node.appendChild(child));
    return node;
  }

  async function requestJson(url, options) {
    const response = await fetch(url, options);
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }
    if (!response.ok) {
      const message = (data && data.error) || `Request failed (${response.status}).`;
      if ((response.status === 401 || response.status === 403) && window.gremlinToast) {
        window.gremlinToast(message);
      }
      const error = new Error(message);
      error.payload = data;
      throw error;
    }
    return data;
  }
  const getJson = (url) => requestJson(url, { headers: { Accept: "application/json" } });
  const postJson = (url, body) =>
    requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    });

  function showBanner(message, kind) {
    const banner = $("lda-status");
    banner.textContent = message;
    banner.className = "lda-banner " + (kind ? "is-" + kind : "is-info");
    banner.hidden = false;
  }
  function clearBanner() {
    $("lda-status").hidden = true;
  }

  function showToast(message, kind) {
    let container = $("lda-toast-container");
    if (!container) {
      container = el("div", { id: "lda-toast-container", class: "lda-toast-container" });
      document.body.appendChild(container);
    }
    const toast = el("div", { class: "lda-toast " + (kind ? "is-" + kind : "is-info"), role: "status", text: message });
    container.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("is-visible"));
    const remove = () => {
      toast.classList.remove("is-visible");
      setTimeout(() => toast.remove(), 400);
    };
    toast.addEventListener("click", remove);
    setTimeout(remove, 5000);
  }

  let loadingDepth = 0;
  function beginLoading(message) {
    loadingDepth += 1;
    $("lda-loading-text").textContent = message || "Working…";
    $("lda-loading").hidden = false;
  }
  function endLoading() {
    loadingDepth = Math.max(0, loadingDepth - 1);
    if (loadingDepth === 0) $("lda-loading").hidden = true;
  }

  // A stored CMMS date as YYYY-MM-DD, without letting the browser's time zone
  // move it: the server sends ISO text, and its first ten characters are the day.
  function day(value) {
    if (!value) return "";
    const text = String(value);
    return /^\d{4}-\d{2}-\d{2}/.test(text) ? text.slice(0, 10) : text;
  }

  function fmt(value, digits) {
    const number = Number(value);
    if (value === null || value === undefined || value === "" || !isFinite(number)) return "—";
    return number.toLocaleString(undefined, { maximumFractionDigits: digits === undefined ? 2 : digits });
  }

  /* The Asset Number combobox: the whole list filtered in the browser, so every
   * asset is searchable (a native <datalist> caps its suggestions). onChoose is
   * called with the asset's number, or null when the box no longer names one. */
  const ASSET_DROPDOWN_LIMIT = 50;
  function assetCombobox({ input, list, hint, onChoose }) {
    let assets = [];
    let byNumber = new Map();
    let filtered = [];
    let active = -1;
    let selected = null;
    let debounce = null;

    function current() {
      return (input.value || "").trim();
    }
    function render() {
      list.innerHTML = "";
      const query = current().toLowerCase();
      const matches = query
        ? assets.filter((asset) =>
            String(asset.asset_number).toLowerCase().includes(query) ||
            String(asset.asset_name || "").toLowerCase().includes(query))
        : assets;
      filtered = matches.slice(0, ASSET_DROPDOWN_LIMIT);
      if (!filtered.length) {
        list.appendChild(el("li", { class: "lda-combobox-empty", text: assets.length ? `No Asset Numbers match "${current()}".` : "No Asset Numbers available." }));
        return;
      }
      filtered.forEach((asset, index) => {
        list.appendChild(el("li", {
          class: "lda-combobox-option" + (index === active ? " is-active" : ""),
          role: "option",
          onmousedown: (event) => {
            event.preventDefault();
            choose(asset);
          },
        }, [
          el("span", { class: "lda-combobox-number", text: asset.asset_number }),
          asset.asset_name ? el("span", { class: "lda-combobox-name", text: asset.asset_name }) : null,
        ]));
      });
      if (matches.length > filtered.length) {
        list.appendChild(el("li", { class: "lda-combobox-empty", text: `Showing first ${filtered.length} of ${matches.length} matches. Keep typing to narrow.` }));
      }
    }
    function open() {
      render();
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }
    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      active = -1;
    }
    function evaluate() {
      const asset = byNumber.get(current()) || null;
      const number = asset ? asset.asset_number : null;
      if (asset) hint.textContent = asset.asset_name ? `Selected ${asset.asset_number}: ${asset.asset_name}.` : `Selected ${asset.asset_number}.`;
      else if (current()) hint.textContent = `"${current()}" is not a known Asset Number. Choose one from the list.`;
      if (number !== selected) {
        selected = number;
        onChoose(number);
      }
    }
    function choose(asset) {
      input.value = asset.asset_number;
      close();
      evaluate();
    }
    input.addEventListener("focus", open);
    input.addEventListener("blur", close);
    input.addEventListener("input", () => {
      active = -1;
      open();
      clearTimeout(debounce);
      debounce = setTimeout(evaluate, 300);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (list.hidden) open();
        if (!filtered.length) return;
        active = (active + (event.key === "ArrowDown" ? 1 : -1) + filtered.length) % filtered.length;
        render();
      } else if (event.key === "Enter" && !list.hidden && active >= 0) {
        event.preventDefault();
        choose(filtered[active]);
      } else if (event.key === "Escape") {
        close();
      }
    });

    return {
      async load() {
        try {
          const data = await getJson(`${API}/assets`);
          assets = data.assets || [];
          byNumber = new Map(assets.map((asset) => [asset.asset_number, asset]));
          hint.textContent = assets.length
            ? `${assets.length} Asset Number(s) available. Type to search.`
            : "No mapped CMMS Asset Numbers were found in the database.";
        } catch (err) {
          hint.textContent = "";
          showBanner(err.message, "error");
        }
      },
      select(number) {
        input.value = number || "";
        evaluate();
      },
      get value() {
        return selected;
      },
    };
  }

  window.GremlinStandard = {
    API,
    STANDARD_API,
    CAN_EDIT,
    $,
    el,
    svg,
    getJson,
    postJson,
    showBanner,
    clearBanner,
    showToast,
    beginLoading,
    endLoading,
    day,
    fmt,
    assetCombobox,
  };
})();
