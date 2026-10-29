/* What Life Data Analysis's two pages share: the DOM and network helpers, the
 * banner, toast, loading veil and section loading states (styled by
 * life_data_analysis.css), and the Asset Number combobox. Exposed as window.GremlinStandard, loaded before
 * disposition_form.js and the page's own script (disposition.js or
 * life_data_analysis.js).
 */
(function () {
  "use strict";

  const API = "/life-data-analysis/api";
  // Whether this browser session may write. Rendered by the server; the API
  // enforces the same rule on its own, this only keeps the page from offering an
  // action that would be refused.
  const CAN_EDIT = document.querySelector('meta[name="gremlin-can-edit"]')?.content === "true";
  // Limble's web app, rendered by the server from LIMBLE_APP_URL. A work order
  // opens there by its task ID, as the PM calendar's links open one.
  const LIMBLE_APP_URL = document.querySelector('meta[name="gremlin-limble-app-url"]')?.content || "";
  const limbleTaskUrl = (taskId) =>
    LIMBLE_APP_URL && taskId !== null && taskId !== undefined && String(taskId).trim() !== ""
      ? `${LIMBLE_APP_URL}/taskList?taskID=${encodeURIComponent(String(taskId).trim())}`
      : null;
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
      const error = new Error(message);
      error.payload = data;
      if ((response.status === 401 || response.status === 403) && window.gremlinToast) {
        // Said once, by the site's own toast: the caller need not say it again.
        window.gremlinToast(message);
        error.toastShown = true;
      }
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

  // GET a file and hand it to the browser as a download, named as the server names it.
  async function download(url, fallbackName, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      try {
        const data = await response.json();
        if (data && data.error) message = data.error;
      } catch (err) {
        /* not JSON: keep the generic message */
      }
      throw new Error(message);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = /filename="?([^";]+)"?/i.exec(disposition);
    const filename = (match && match[1]) || fallbackName || "download";
    const href = URL.createObjectURL(blob);
    const link = el("a", { href, download: filename });
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(href);
    return filename;
  }

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

  // The veil over the whole page while something the page waits on runs: a fit,
  // a save, a list being read. It takes the clicks at once, so nothing is pressed
  // twice, but only fades in after a moment (the stylesheet's delay), so a quick
  // request never flashes it; a slow one says how long it has been going.
  const STILL_WORKING_AFTER_S = 3;
  let loadingDepth = 0;
  let loadingStarted = 0;
  let loadingClock = null;
  function beginLoading(message) {
    loadingDepth += 1;
    $("lda-loading-text").textContent = message || "Working…";
    if (loadingDepth > 1) return;
    loadingStarted = Date.now();
    const veil = $("lda-loading");
    veil.hidden = false;
    veil.setAttribute("aria-busy", "true");
    showElapsed();
    clearInterval(loadingClock);
    loadingClock = setInterval(showElapsed, 1000);
  }
  function showElapsed() {
    const node = $("lda-loading-elapsed");
    if (!node) return;
    const seconds = Math.floor((Date.now() - loadingStarted) / 1000);
    node.textContent = seconds >= STILL_WORKING_AFTER_S ? `Still working · ${seconds} s` : "";
  }
  function endLoading() {
    loadingDepth = Math.max(0, loadingDepth - 1);
    if (loadingDepth) return;
    clearInterval(loadingClock);
    const veil = $("lda-loading");
    veil.hidden = true;
    veil.removeAttribute("aria-busy");
  }

  // One part of a page being read again while the rest stays usable: it dims
  // under a moving bar, after the same moment as the veil, and a status pill at
  // the foot of the window says what is loading -- the part may be scrolled out of
  // view. Overlapping reads of one part are counted, so the first to finish never
  // clears a later one. Returns the function that ends this read.
  const busyReads = [];
  function busy(node, message) {
    const read = { message: message || "Loading…" };
    busyReads.push(read);
    showBusyStatus();
    if (node) {
      node.gremlinBusy = (node.gremlinBusy || 0) + 1;
      node.classList.add("is-busy");
      node.setAttribute("aria-busy", "true");
    }
    let ended = false;
    return () => {
      if (ended) return;
      ended = true;
      busyReads.splice(busyReads.indexOf(read), 1);
      showBusyStatus();
      if (!node) return;
      node.gremlinBusy -= 1;
      if (node.gremlinBusy > 0) return;
      node.classList.remove("is-busy");
      node.removeAttribute("aria-busy");
    };
  }
  function showBusyStatus() {
    let status = $("lda-busy-status");
    if (!status) {
      status = el("div", { id: "lda-busy-status", class: "lda-busy-status", role: "status", "aria-live": "polite", hidden: true }, [
        el("span", { class: "lda-spinner is-small", "aria-hidden": "true" }),
        el("span", { class: "lda-busy-text" }),
      ]);
      document.body.appendChild(status);
    }
    const latest = busyReads[busyReads.length - 1];
    status.hidden = !latest;
    if (latest) status.querySelector(".lda-busy-text").textContent = latest.message;
  }

  // Play an element's entrance again after its content changed.
  function reveal(node, className) {
    if (!node) return;
    const name = className || "lda-reveal";
    node.classList.remove(name);
    void node.offsetWidth;
    node.classList.add(name);
  }

  // Placeholder blocks that shimmer where content is on its way.
  function skeleton(className, count) {
    return Array.from({ length: count || 1 }, () => el("div", { class: `lda-skeleton ${className || ""}`.trim(), "aria-hidden": "true" }));
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
        // A spinner beside the hint until the list is in.
        hint.classList.add("is-loading");
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
        } finally {
          hint.classList.remove("is-loading");
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
    CAN_EDIT,
    limbleTaskUrl,
    $,
    el,
    svg,
    requestJson,
    getJson,
    postJson,
    download,
    showBanner,
    clearBanner,
    showToast,
    beginLoading,
    endLoading,
    busy,
    reveal,
    skeleton,
    day,
    fmt,
    assetCombobox,
  };
})();
