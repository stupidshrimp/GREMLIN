/* The topbar's global search: one box over a catalog of pages and functions.
 *
 * The catalog is embedded in the page by topbar.html and already filtered to
 * what the signed-in account may open, so everything here is presentation --
 * nothing in this file decides what somebody is allowed to reach.
 *
 * Matching mirrors _search_matches in app.py so the dropdown and the no-script
 * /search page rank identically; change one and change the other. */
(function () {
  const root = document.getElementById("globalSearch");
  const input = document.getElementById("globalSearchInput");
  const panel = document.getElementById("globalSearchPanel");
  const list = document.getElementById("globalSearchResults");
  const empty = document.getElementById("globalSearchEmpty");
  const status = document.getElementById("globalSearchStatus");
  const footer = document.getElementById("globalSearchFooter");
  const kbd = document.getElementById("globalSearchKbd");
  const indexNode = document.getElementById("globalSearchIndex");

  if (!root || !input || !panel || !list || !indexNode) return;

  let entries = [];
  try {
    entries = JSON.parse(indexNode.textContent) || [];
  } catch (e) {
    // A catalog that will not parse leaves the box inert rather than throwing on
    // every keystroke. The form still submits to /search, which builds its own.
    return;
  }

  // How many results the dropdown will show at once. Past this the list stops
  // being something you scan and starts being something you scroll, and the
  // answer is to type another letter.
  const MAX_RESULTS = 8;
  // What an empty box offers: the first few pages, in catalog order, so opening
  // the search is itself a way to get around.
  const SUGGESTION_COUNT = 5;

  const ICONS = {
    page: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 2.8h7.8c.2 0 .5.1.6.3l3.8 3.8c.2.2.3.4.3.6v13.7c0 .5-.4.9-.9.9H6c-.5 0-.9-.4-.9-.9V3.7c0-.5.4-.9.9-.9Zm7.2 1.9v2.6c0 .5.4.9.9.9h2.6L13.2 4.7ZM8.2 11.2c0-.5.4-.9.9-.9h5.8a1 1 0 1 1 0 2H9.1a.9.9 0 0 1-.9-.9Zm0 3.8c0-.5.4-.9.9-.9h5.8a1 1 0 1 1 0 2H9.1a.9.9 0 0 1-.9-.9Z"/></svg>',
    function: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M13.6 2.2a1 1 0 0 1 .8 1.2l-1 5.1h4.4a1 1 0 0 1 .8 1.6l-8 11a1 1 0 0 1-1.8-.8l1-5.1H5.4a1 1 0 0 1-.8-1.6l8-11a1 1 0 0 1 1-.4Z"/></svg>',
  };

  const KIND_LABELS = { page: "Page", function: "Function" };
  const GROUPS = [
    { kind: "page", heading: "Pages" },
    { kind: "function", heading: "Functions" },
  ];

  const state = { results: [], activeIndex: -1, open: false };

  // ---- matching -------------------------------------------------------------

  function scoreTerm(entry, term) {
    const label = entry.label.toLowerCase();
    if (label === term) return 100;
    if (label.startsWith(term)) return 80;
    if (label.split(/\s+/).some((word) => word.startsWith(term))) return 65;
    if (label.includes(term)) return 50;

    const keywords = (entry.keywords || "").toLowerCase();
    if (keywords.split(/\s+/).some((word) => word.startsWith(term))) return 40;
    if (keywords.includes(term)) return 30;
    if ((entry.context || "").toLowerCase().includes(term)) return 20;
    return 0;
  }

  function search(query) {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length) return entries.filter((e) => e.kind === "page").slice(0, SUGGESTION_COUNT);

    const ranked = [];
    entries.forEach((entry, position) => {
      let total = 0;
      // Every term has to land somewhere: a second word narrows the search
      // rather than widening it, which is what typing one is for.
      for (const term of terms) {
        const score = scoreTerm(entry, term);
        if (!score) return;
        total += score;
      }
      ranked.push({ entry, total, position });
    });

    ranked.sort((a, b) =>
      b.total - a.total ||
      a.entry.label.length - b.entry.label.length ||
      a.position - b.position
    );
    return ranked.slice(0, MAX_RESULTS).map((row) => row.entry);
  }

  // ---- rendering ------------------------------------------------------------

  /* The label with each search term wrapped in <mark>. Built from text nodes
   * rather than an HTML string: the terms come from whatever was typed, and
   * this is the one place in the dropdown where that text reaches the DOM. */
  function highlight(label, terms) {
    const fragment = document.createDocumentFragment();
    if (!terms.length) {
      fragment.appendChild(document.createTextNode(label));
      return fragment;
    }

    const lower = label.toLowerCase();
    // One pass over the label, taking at each position the longest term that
    // starts there. Overlapping matches ("data" and "database") then produce one
    // mark instead of two nested ones.
    let cursor = 0;
    let plain = "";
    while (cursor < label.length) {
      let hit = 0;
      for (const term of terms) {
        if (term.length > hit && lower.startsWith(term, cursor)) hit = term.length;
      }
      if (hit) {
        if (plain) {
          fragment.appendChild(document.createTextNode(plain));
          plain = "";
        }
        const mark = document.createElement("mark");
        mark.textContent = label.slice(cursor, cursor + hit);
        fragment.appendChild(mark);
        cursor += hit;
      } else {
        plain += label[cursor];
        cursor += 1;
      }
    }
    if (plain) fragment.appendChild(document.createTextNode(plain));
    return fragment;
  }

  function buildOption(entry, index, terms) {
    // An <a>, so the browser's own affordances come free: the status bar shows
    // where a result goes, and middle-click or ctrl-click opens it in a tab.
    const option = document.createElement("a");
    option.className = "global-search-option";
    option.href = entry.url;
    option.id = `globalSearchOption-${index}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    option.dataset.index = String(index);

    const icon = document.createElement("span");
    icon.className = "global-search-option-icon";
    icon.innerHTML = ICONS[entry.kind] || ICONS.page;

    const text = document.createElement("span");
    text.className = "global-search-option-text";

    const label = document.createElement("span");
    label.className = "global-search-option-label";
    label.appendChild(highlight(entry.label, terms));

    const context = document.createElement("span");
    context.className = "global-search-option-context";
    context.textContent = entry.context;

    text.append(label, context);

    const kind = document.createElement("span");
    kind.className = "global-search-option-kind";
    kind.textContent = KIND_LABELS[entry.kind] || entry.kind;

    option.append(icon, text, kind);
    return option;
  }

  function render(query) {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    state.results = search(query);
    state.activeIndex = state.results.length ? 0 : -1;

    list.innerHTML = "";

    if (!state.results.length) {
      empty.textContent = `Nothing matches “${query}”. Try an asset workflow, a metric, or a page name.`;
      empty.hidden = false;
      // The hints name keys that do nothing with an empty list: there is
      // nothing to move between and nothing to open.
      if (footer) footer.hidden = true;
      announce("No results.");
      paintActive();
      return;
    }
    empty.hidden = true;
    if (footer) footer.hidden = false;

    // Results are grouped under Pages/Functions, and the group holding the best
    // match is drawn first -- searching "downtime driver" should not open with
    // the Metrics page just because pages are conventionally listed above
    // functions. Within a group the ranking stands.
    const grouped = GROUPS
      .map((group) => ({
        heading: query ? group.heading : "Jump to",
        entries: state.results.filter((entry) => entry.kind === group.kind),
      }))
      .filter((group) => group.entries.length)
      .sort((a, b) => state.results.indexOf(a.entries[0]) - state.results.indexOf(b.entries[0]));

    // Flattened back in the order they are about to be drawn in, so the arrow
    // keys walk the list the way the eye does. Every option's data-index is its
    // position in this array, and the highlight starts on the first row shown --
    // which is still the top-ranked result, because its group leads.
    state.results = grouped.flatMap((group) => group.entries);

    let index = 0;
    grouped.forEach((group) => {
      // A real listbox group rather than a heading dropped between options, so
      // the panel stays a valid listbox.
      const container = document.createElement("div");
      container.setAttribute("role", "group");
      container.setAttribute("aria-label", group.heading);

      const caption = document.createElement("p");
      caption.className = "global-search-group";
      caption.textContent = group.heading;
      // The group's aria-label already carries this; the text is left for the
      // eye alone so it is not announced twice.
      caption.setAttribute("aria-hidden", "true");
      container.appendChild(caption);

      group.entries.forEach((entry) => {
        container.appendChild(buildOption(entry, index, terms));
        index += 1;
      });
      list.appendChild(container);
    });

    // An empty box is offering suggestions, not answering anything, so it says
    // nothing: this fires on every focus, and a count read out each time the box
    // is tabbed into is noise.
    if (query) {
      announce(state.results.length === 1 ? "1 result." : `${state.results.length} results.`);
    } else {
      announce("");
    }
    paintActive();
  }

  function paintActive(scroll) {
    const options = list.querySelectorAll(".global-search-option");
    options.forEach((option) => {
      const isActive = Number(option.dataset.index) === state.activeIndex;
      option.classList.toggle("is-active", isActive);
      option.setAttribute("aria-selected", isActive ? "true" : "false");
      if (isActive) {
        input.setAttribute("aria-activedescendant", option.id);
        // Only when the keyboard moved the highlight. Doing it on every render
        // would let the panel scroll the window on the very first keystroke,
        // before there is anything out of view to scroll to.
        if (scroll) option.scrollIntoView({ block: "nearest" });
      }
    });
    if (state.activeIndex < 0) input.removeAttribute("aria-activedescendant");
  }

  function announce(message) {
    if (status) status.textContent = message;
  }

  // ---- open / close ---------------------------------------------------------

  function open() {
    if (state.open) return;
    state.open = true;
    panel.hidden = false;
    root.classList.add("is-active");
    input.setAttribute("aria-expanded", "true");
  }

  function close() {
    if (!state.open) return;
    state.open = false;
    panel.hidden = true;
    root.classList.remove("is-active");
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    state.activeIndex = -1;
    announce("");
  }

  function move(step) {
    if (!state.results.length) return;
    const count = state.results.length;
    // Wraps at both ends: pressing up from the first result is the quickest way
    // to the last one.
    state.activeIndex = (state.activeIndex + step + count) % count;
    paintActive(true);
  }

  /* Follow a result. `#account` is not a link to anywhere -- it names the
   * account dialog the sidebar owns, which is the only way in or out of a
   * session and has no URL of its own. */
  function activate(entry) {
    if (!entry) return;
    close();
    input.blur();

    if (entry.url === "#account") {
      const dialog = document.getElementById("accountDialog");
      if (dialog && typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        document.getElementById("accountButton")?.click();
      }
      return;
    }

    // Assigning rather than following the anchor keeps one path for the mouse
    // and the keyboard, and a same-page fragment still moves the view because
    // the hash changes even when the path does not.
    window.location.assign(entry.url);
  }

  // ---- wiring ---------------------------------------------------------------

  input.addEventListener("focus", () => {
    render(input.value.trim());
    open();
  });

  input.addEventListener("input", () => {
    render(input.value.trim());
    open();
  });

  input.addEventListener("keydown", (event) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!state.open) {
          render(input.value.trim());
          open();
        } else {
          move(1);
        }
        break;
      case "ArrowUp":
        event.preventDefault();
        move(-1);
        break;
      case "Home":
        if (!state.open) break;
        event.preventDefault();
        state.activeIndex = state.results.length ? 0 : -1;
        paintActive(true);
        break;
      case "End":
        if (!state.open) break;
        event.preventDefault();
        state.activeIndex = state.results.length - 1;
        paintActive(true);
        break;
      case "Enter":
        // With a result highlighted this is the whole point of the control, so
        // it takes precedence over submitting the form. With none -- an empty
        // box, or a query nothing matched -- the form is left alone and /search
        // says so on a full page.
        if (state.open && state.activeIndex >= 0) {
          event.preventDefault();
          activate(state.results[state.activeIndex]);
        }
        break;
      case "Escape":
        // First press closes the dropdown, second clears the box. Escaping out
        // of a search should not also throw away what was typed.
        if (state.open) {
          event.preventDefault();
          close();
        } else if (input.value) {
          event.preventDefault();
          input.value = "";
        }
        break;
      case "Tab":
        close();
        break;
      default:
        break;
    }
  });

  // Pointer-down rather than click: the mousedown that starts the click would
  // otherwise blur the input and close the panel before the click landed.
  list.addEventListener("mousedown", (event) => {
    const option = event.target.closest(".global-search-option");
    if (!option) return;
    // Let the browser handle a modified click -- that is somebody asking for a
    // new tab, and hijacking it would take the tab away.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    activate(state.results[Number(option.dataset.index)]);
  });

  list.addEventListener("mousemove", (event) => {
    const option = event.target.closest(".global-search-option");
    if (!option) return;
    const index = Number(option.dataset.index);
    if (index === state.activeIndex) return;
    state.activeIndex = index;
    paintActive();
  });

  document.addEventListener("pointerdown", (event) => {
    if (!root.contains(event.target)) close();
  });

  input.addEventListener("blur", () => {
    // Deferred so a click that started inside the panel gets to finish. The
    // pointerdown handler above is what closes it on a click elsewhere.
    window.setTimeout(() => {
      if (!root.contains(document.activeElement)) close();
    }, 0);
  });

  // ---- keyboard shortcut ----------------------------------------------------

  const isApple = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || "");
  if (kbd) kbd.textContent = isApple ? "⌘K" : "Ctrl K";

  function isTyping(node) {
    if (!node) return false;
    if (node.isContentEditable) return true;
    return ["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName);
  }

  document.addEventListener("keydown", (event) => {
    const shortcut = (isApple ? event.metaKey : event.ctrlKey) && event.key.toLowerCase() === "k";
    // A bare "/" is the other convention, but only where it is not being typed
    // into something -- every page here has fields of its own.
    const slash = event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey;

    if (!shortcut && !(slash && !isTyping(document.activeElement))) return;
    event.preventDefault();
    input.focus();
    input.select();
  });
})();
