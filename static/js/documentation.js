/* The Documentation page: three tabs, each a list of expandable cards.
 *
 * Three jobs, all small:
 *
 *  - The tabs. Arrow keys, Home and End move between them, the way a tab list
 *    is expected to behave, and the address bar follows the open tab so a
 *    copied link reopens it.
 *  - Fragments. A fragment may name a tab panel, a card, or anything inside a
 *    card (a section, a figure). Whatever it names is revealed: its tab is
 *    opened, every card around it is expanded, and it is scrolled into view.
 *    The topbar search links here that way, and so do the cross references
 *    between cards, which are plain in-page links.
 *  - "Expand all", per tab, and opening the open tab's cards for printing.
 */
(() => {
  const tabs = Array.from(document.querySelectorAll("[data-tab-target]"));
  const panels = tabs.map((tab) => document.getElementById(tab.dataset.tabTarget));
  if (!tabs.length) return;

  function panelFor(tab) {
    return document.getElementById(tab.dataset.tabTarget);
  }

  function activateTab(nextTab, shouldFocus) {
    tabs.forEach((tab) => {
      const isActive = tab === nextTab;
      tab.classList.toggle("active", isActive);
      tab.setAttribute("aria-selected", String(isActive));
      tab.tabIndex = isActive ? 0 : -1;
    });
    panels.forEach((panel) => {
      if (panel) panel.hidden = panel !== panelFor(nextTab);
    });
    if (shouldFocus) nextTab.focus();
  }

  // replaceState rather than assigning location.hash: the hash change would
  // push a history entry per tab, making Back step through tabs instead of
  // leaving the page, and it would also scroll to the panel.
  function rememberInAddress(id) {
    if (window.location.hash.slice(1) === id) return;
    history.replaceState(null, "", "#" + id);
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => {
      activateTab(tab, false);
      rememberInAddress(tab.dataset.tabTarget);
    });
    tab.addEventListener("keydown", (event) => {
      const lastIndex = tabs.length - 1;
      let nextIndex = null;
      if (event.key === "ArrowRight") nextIndex = index === lastIndex ? 0 : index + 1;
      else if (event.key === "ArrowLeft") nextIndex = index === 0 ? lastIndex : index - 1;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = lastIndex;
      if (nextIndex === null) return;
      event.preventDefault();
      activateTab(tabs[nextIndex], true);
      rememberInAddress(tabs[nextIndex].dataset.tabTarget);
    });
  });

  /* Reveal whatever the fragment names. An unknown fragment, or one naming
     something outside the tabs, is left to the browser. */
  function revealFromHash() {
    let id = "";
    try {
      id = decodeURIComponent(window.location.hash.slice(1));
    } catch (error) {
      return;
    }
    if (!id) return;
    const target = document.getElementById(id);
    if (!target) return;
    const panel = target.closest(".tab-panel");
    if (!panel) return;
    const tab = tabs.find((candidate) => panelFor(candidate) === panel);
    if (tab) activateTab(tab, false);

    // Every card the target sits in, and the target itself when it is a card.
    for (let node = target; node && node !== panel; node = node.parentElement) {
      if (node.tagName === "DETAILS") node.open = true;
    }
    syncExpandButtons();

    // A tab is revealed by bringing its tab row into view; anything inside a
    // tab by bringing the thing itself there. Deferred a frame so the panel
    // that was hidden a moment ago has been laid out and has a position.
    const scrollTarget = target === panel ? document.getElementById("documentation-tabs") : target;
    requestAnimationFrame(() => scrollTarget.scrollIntoView({ block: "start" }));
  }

  /* Expand all, one button per tab. The label says what the next click will
     do, so it follows the cards as well as leading them: opening every card by
     hand and still reading "Expand all" would be a button that lies. */
  const expandButtons = Array.from(document.querySelectorAll("[data-expand-all]"));

  function cardsIn(panel) {
    return Array.from(panel.querySelectorAll("details.std-card"));
  }

  function syncExpandButtons() {
    expandButtons.forEach((button) => {
      const cards = cardsIn(button.closest(".tab-panel"));
      const allOpen = cards.length > 0 && cards.every((card) => card.open);
      button.textContent = allOpen ? "Collapse all" : "Expand all";
      button.setAttribute("aria-expanded", String(allOpen));
    });
  }

  expandButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const cards = cardsIn(button.closest(".tab-panel"));
      const open = !cards.every((card) => card.open);
      cards.forEach((card) => {
        card.open = open;
      });
      syncExpandButtons();
    });
  });

  document.querySelectorAll("details.std-card").forEach((card) => {
    card.addEventListener("toggle", syncExpandButtons);
    // A card opened by hand puts its own name in the address bar, so the link
    // somebody copies from here reopens it; closing it hands the name back to
    // its tab. Listened for on the summary's click rather than on "toggle",
    // which also fires for Expand all and for cards a fragment opened, neither
    // of which is somebody choosing this card.
    const summary = card.querySelector("summary");
    const panel = card.closest(".tab-panel");
    if (!summary || !panel) return;
    summary.addEventListener("click", () => {
      rememberInAddress(card.open ? panel.id : card.id);
    });
  });

  /* Printing prints what is open, so open the visible tab's cards for the
     printout and put them back afterwards. */
  let closedForPrint = [];
  window.addEventListener("beforeprint", () => {
    const visible = panels.find((panel) => panel && !panel.hidden);
    if (!visible) return;
    closedForPrint = cardsIn(visible).filter((card) => !card.open);
    closedForPrint.forEach((card) => {
      card.open = true;
    });
  });
  window.addEventListener("afterprint", () => {
    closedForPrint.forEach((card) => {
      card.open = false;
    });
    closedForPrint = [];
    syncExpandButtons();
  });

  syncExpandButtons();
  revealFromHash();
  // Following a second link to this page changes only the fragment, which
  // navigates nothing and would otherwise leave the old tab showing.
  window.addEventListener("hashchange", revealFromHash);
})();
