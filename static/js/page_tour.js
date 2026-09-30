// The "Show me around" walk-through, for any page that includes
// page_tour.html: a spotlight on one part of the page at a time and a card
// saying what it is for. It is the tour the PM calendar and Metrics pages have,
// pulled out so a page only has to say which steps it wants -- see home.js and
// life_data_analysis.js.
//
//   window.gremlinTour.start(steps, options)
//
// Each step is { target, title, body, when }. `target` is a selector, looked up
// as the step is shown rather than now, because pages draw their content after
// their scripts run; null means a step about the whole page, with the card in
// the middle and nothing lit. `body` may be a function, for text that depends
// on what is on screen. `when`, if given, is asked once as the tour starts and
// leaves the step out when it says no.
//
// A step whose target isn't drawn when the tour starts is left out too, rather
// than shown with a ring round nothing: a panel that only appears once an asset
// is picked, a button the account can't use, a control that narrow screens hide.
//
// Options:
//   seenKey    localStorage key set to "yes" when the tour closes, so a page
//              can open it by itself on a first visit only.
//   pinned     selectors for things that stay stuck to the top of the window as
//              the page scrolls. The spotlight stops at their lower edge, and a
//              target is scrolled to below them. The top bar by default.
//   onEnd      called with the steps that were shown once the tour has closed.
//   returnFocus  called with the element that had focus before the tour, to put
//              it back. Plain .focus() by default.
(function () {
  "use strict";

  const PAD = 8;
  const $ = (id) => document.getElementById(id);

  // The tour on screen: its steps, the options it was started with, which step
  // it is on, and what had focus before it. Null while no tour is open.
  let tour = null;
  let layoutWatch = null;

  // Storage throws rather than returning null when site data is blocked -- the
  // same guard layout.js and topbar_tools.js use for their settings.
  function seen(key) {
    try {
      return localStorage.getItem(key) === "yes";
    } catch (err) {
      return false;
    }
  }

  function remember(key) {
    try {
      localStorage.setItem(key, "yes");
    } catch (err) {
      // A browser that won't remember just shows the tour again; the page
      // itself is unaffected, so there is nothing to report.
    }
  }

  function isOpen() {
    return Boolean(tour);
  }

  // Anything on screen a tour mustn't open over by itself: another tour, or
  // one of the site's dialogs (the account one, Help).
  function busy() {
    return isOpen() || Boolean(document.querySelector("dialog[open]"));
  }

  // The element, but only if it takes up room on screen. Anything hidden is in
  // the document with no box at all, and an empty container is a line with no
  // height; lighting either would put a ring round nothing.
  function drawn(node) {
    if (!node) return null;
    const box = node.getBoundingClientRect();
    return box.width > 0 && box.height > 0 ? node : null;
  }

  function targetOf(step) {
    return step.target ? drawn(document.querySelector(step.target)) : null;
  }

  // Where the room for the target starts: the lower edge of whatever is pinned
  // to the top of the window over it. Something the target sits inside doesn't
  // count -- the top bar doesn't cover its own search box. `whenStuck` asks
  // where that edge will be once the page has scrolled far enough for all of
  // them to stick, which is what a scroll has to allow for; otherwise it is
  // where the edge is now, counting only the ones already stuck.
  function ceilingFor(target, whenStuck) {
    let ceiling = 0;
    (tour.options.pinned || [".topbar"]).forEach((selector) => {
      const node = drawn(document.querySelector(selector));
      if (!node || (target && node.contains(target))) return;
      const style = window.getComputedStyle(node);
      // Narrow screens let the sidebar and the like scroll away with the page.
      if (style.position !== "sticky" && style.position !== "fixed") return;
      const stuckAt = parseFloat(style.top) || 0;
      const box = node.getBoundingClientRect();
      if (whenStuck) {
        ceiling = Math.max(ceiling, stuckAt + box.height);
      } else if (box.top <= stuckAt + 1) {
        ceiling = Math.max(ceiling, box.bottom);
      }
    });
    return ceiling;
  }

  // Where the card goes beside the lit box: below it when there's room, then
  // above, then to its right or left -- a sidebar is as tall as the screen and
  // has nothing above or below it. Null when none of those fit.
  function spotFor(box, size, ceiling) {
    const width = window.innerWidth;
    const height = window.innerHeight;
    const across = (x) => Math.min(Math.max(PAD, x), Math.max(PAD, width - size.width - PAD));
    const down = (y) => Math.min(Math.max(ceiling + PAD, y), Math.max(ceiling + PAD, height - size.height - PAD));

    const below = box.bottom + PAD * 2;
    if (below + size.height <= height - PAD) return { top: below, left: across(box.left) };
    const above = box.top - PAD * 2 - size.height;
    if (above >= ceiling + PAD) return { top: above, left: across(box.left) };
    const right = box.right + PAD * 2;
    if (right + size.width <= width - PAD) return { top: down(box.top), left: right };
    const left = box.left - PAD * 2 - size.width;
    if (left >= PAD) return { top: down(box.top), left };
    return null;
  }

  // When nothing beside the lit box has room: the top or the bottom of the
  // screen, whichever covers less of what's lit. The bottom when they tie, as
  // they do for something taller than the screen, since a card pinned to the
  // top would cover its heading.
  function fallbackSpot(box, size, ceiling) {
    const litTop = Math.max(ceiling, box.top - PAD);
    const litBottom = Math.min(window.innerHeight, box.bottom + PAD);
    const covered = (y) => Math.max(0, Math.min(litBottom, y + size.height) - Math.max(litTop, y));
    const high = ceiling + PAD;
    const low = window.innerHeight - size.height - PAD * 2;
    return {
      top: covered(high) < covered(low) ? high : low,
      left: Math.min(Math.max(PAD, box.left), Math.max(PAD, window.innerWidth - size.width - PAD)),
    };
  }

  function place() {
    // Run a frame or two after a step is shown, by when Skip or Escape may
    // have closed the tour.
    if (!tour) return;
    const card = $("page-tour-card");
    const spotlight = $("page-tour-spotlight");
    const target = targetOf(tour.steps[tour.index]);

    if (!target) {
      // Nothing to point at: centre the card and leave the page evenly dimmed.
      spotlight.hidden = true;
      card.style.top = "20vh";
      card.style.left = "max(1rem, calc(50vw - 11.5rem))";
      return;
    }

    const box = target.getBoundingClientRect();
    // Something taller than the screen runs up under the sticky top bar, which
    // would then sit undimmed inside the lit box. Stop the box at the bar's
    // lower edge.
    const ceiling = ceilingFor(target, false);
    const litTop = Math.max(box.top - PAD, ceiling);
    spotlight.hidden = false;
    spotlight.style.top = `${litTop}px`;
    spotlight.style.left = `${box.left - PAD}px`;
    spotlight.style.width = `${box.width + PAD * 2}px`;
    spotlight.style.height = `${Math.max(0, box.bottom + PAD - litTop)}px`;

    const size = card.getBoundingClientRect();
    const spot = spotFor(box, size, ceiling) || fallbackSpot(box, size, ceiling);
    card.style.top = `${spot.top}px`;
    card.style.left = `${spot.left}px`;
  }

  // Brings the target on screen with room for the card beside it, and leaves
  // the page alone when it already is. Otherwise centred when that leaves room
  // above or below it, or just under the top bar with the card underneath: a
  // table centred on a laptop screen leaves too little on either side, and the
  // card, pinned to the bottom of the screen, would cover the rows the step is
  // about.
  function scrollTo(target) {
    const box = target.getBoundingClientRect();
    const size = $("page-tour-card").getBoundingClientRect();
    const now = ceilingFor(target, false);
    if (box.top >= now - 1 && box.bottom <= window.innerHeight + 1 && spotFor(box, size, now)) return;

    const ceiling = ceilingFor(target, true);
    const beside = (window.innerHeight - ceiling - box.height) / 2;
    const landAt = beside >= size.height + PAD * 3 ? ceiling + beside : ceiling + PAD * 2;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({
      top: Math.max(0, window.pageYOffset + box.top - landAt),
      behavior: still ? "auto" : "smooth",
    });
  }

  function show() {
    const steps = tour.steps;
    const step = steps[tour.index];

    $("page-tour-step").textContent = `Step ${tour.index + 1} of ${steps.length}`;
    $("page-tour-title").textContent = step.title;
    $("page-tour-body").textContent = typeof step.body === "function" ? step.body() : step.body;
    $("page-tour-back").disabled = tour.index === 0;
    $("page-tour-next").textContent = tour.index === steps.length - 1 ? "Done" : "Next ►";

    // After the text, which is what sets the card's height.
    const target = targetOf(step);
    if (target) scrollTo(target);

    // After the scroll, so the spotlight lands on where the target actually
    // ends up.
    requestAnimationFrame(() => requestAnimationFrame(place));
  }

  // Escape leaves, the same as the site's dialogs. Tab stays on the card's
  // buttons: the overlay stops the page behind it being clicked, and without
  // this a keyboard could still walk into it and drive controls it can't see.
  // The search box's shortcuts are held back for the same reason -- they would
  // put focus in the box behind the overlay. This listens in the capture phase,
  // so stopping the key here keeps it from global_search.js.
  function onKeydown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      end();
      return;
    }
    const searchKey =
      event.key === "/" || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k");
    if (searchKey) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (event.key !== "Tab") return;
    const buttons = Array.from($("page-tour-card").querySelectorAll("button:not([disabled])"));
    if (!buttons.length) return;
    const index = buttons.indexOf(document.activeElement);
    if (event.shiftKey && index <= 0) {
      event.preventDefault();
      buttons[buttons.length - 1].focus();
    } else if (!event.shiftKey && (index === -1 || index === buttons.length - 1)) {
      event.preventDefault();
      buttons[0].focus();
    }
  }

  // True when the tour opened. It doesn't when another is already open, when
  // the page has no overlay, or when none of the steps has anything to show.
  function start(steps, options) {
    if (isOpen() || !$("page-tour")) return false;
    const shown = steps.filter(
      (step) => (!step.when || step.when()) && (!step.target || targetOf(step))
    );
    if (!shown.length) return false;
    tour = {
      steps: shown,
      options: options || {},
      index: 0,
      focusBefore: document.activeElement,
    };
    $("page-tour").hidden = false;
    document.addEventListener("keydown", onKeydown, true);
    // Content landing under the tour moves things -- a banner above a card, a
    // chart taking its size -- and neither fires a scroll or a resize.
    // Anything that shifts the page changes the height of <main>.
    if (typeof ResizeObserver === "function") {
      layoutWatch = new ResizeObserver(() => place());
      layoutWatch.observe(document.querySelector("main") || document.body);
    }
    show();
    $("page-tour-card").focus();
    return true;
  }

  function end() {
    if (!tour) return;
    const { steps, options, focusBefore } = tour;
    tour = null;
    $("page-tour").hidden = true;
    document.removeEventListener("keydown", onKeydown, true);
    if (layoutWatch) {
      layoutWatch.disconnect();
      layoutWatch = null;
    }
    if (options.seenKey) remember(options.seenKey);
    if (focusBefore && focusBefore !== document.body && document.body.contains(focusBefore)) {
      if (options.returnFocus) options.returnFocus(focusBefore);
      else focusBefore.focus();
    }
    if (options.onEnd) options.onEnd(steps);
  }

  function wire() {
    if (!$("page-tour")) return;
    $("page-tour-skip").addEventListener("click", end);
    $("page-tour-back").addEventListener("click", () => {
      if (!tour || tour.index === 0) return;
      tour.index -= 1;
      show();
      // Back disables itself on the first step, and a disabled button drops
      // focus on the floor.
      if (tour.index === 0) $("page-tour-next").focus();
    });
    $("page-tour-next").addEventListener("click", () => {
      if (!tour) return;
      if (tour.index < tour.steps.length - 1) {
        tour.index += 1;
        show();
      } else {
        end();
      }
    });

    // The spotlight follows the page if the window is resized or scrolled
    // under it, and once the sections have finished sliding into place.
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, { passive: true });
    document.addEventListener("animationend", place);
  }

  window.gremlinTour = { start, end, isOpen, busy, seen, remember };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
