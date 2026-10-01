// The "Show me around" walk-through, for any page that includes
// page_tour.html: a spotlight on one part of the page at a time and a card
// saying what it is for. It is the tour the PM calendar and Metrics pages have,
// pulled out so a page only has to say which steps it wants -- see home.js and
// life_data_analysis.js.
//
//   window.gremlinTour.start(steps, options)
//
// Each step is { target, title, body, when, action }. `target` is a selector,
// looked up as the step is shown rather than now, because pages draw their
// content after their scripts run; null means a step about the whole page, with
// the card in the middle and nothing lit. A selector matching several elements
// -- a table column's header and each of its cells -- lights the box round all
// of them, and a target inside a box that scrolls on its own, such as a wide
// table, is scrolled into that box's view first. `body` may be a function, for
// text that depends on what is on screen. `when`, if given, is asked once as the
// tour starts and leaves the step out when it says no.
//
// A step that needs more than a paragraph can also have `points`, a list under
// the body, each either a string or [term, text] for a term and what it means;
// `cite`, a line naming where the card's content comes from; and `label`, said
// after the step count, for a run of steps that are one part of the tour (the
// columns of a table, say). Each may be a function, as `body` may.
//
// A step whose target isn't drawn when the tour starts is left out too, rather
// than shown with a ring round nothing: a button the account can't use, a
// control that narrow screens hide.
//
// `action` is for a page that draws most of itself only once something is
// chosen, so that the tour can do the choosing rather than stop short of
// everything after it: { label, needed, run }. While `needed()` says yes, Next
// reads `label` and, pressed, awaits `run()` and shows the same step again,
// now with whatever it drew; each action is offered once a tour. From the first
// step with an action on, steps are kept whether or not their target is drawn
// yet, since the action may be what draws it, so the count on the card is the
// whole tour rather than the part of it the page happened to be showing when it
// started. A page with actions therefore says with `when` which of those steps
// apply to it. One whose target still isn't drawn by the time it is shown --
// the action had nothing to show -- gets the card in the middle, as a step
// about the whole page does.
//
// Options:
//   seenKey    localStorage key set to "yes" when the tour closes, so a page
//              can open it by itself on a first visit only.
//   pinned     selectors for things that stay stuck to the top of the window as
//              the page scrolls. The spotlight stops at their lower edge, and a
//              target is scrolled to below them. The top bar by default.
//   onEnd      called once the tour has closed with the steps it got as far as,
//              which is fewer than it had when it was skipped part way.
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

  // Every element the step's selector matches that is drawn, or null when none
  // is. Usually one; for a column of a table, its header and each cell showing.
  function targetOf(step) {
    if (!step.target) return null;
    const nodes = Array.from(document.querySelectorAll(step.target)).filter((node) => drawn(node));
    return nodes.length ? nodes : null;
  }

  // The boxes between the target and the page that cut off what overflows them,
  // innermost first: a table in a scrolling box of its own, for one.
  function clippersOf(nodes) {
    const found = [];
    for (let node = nodes[0].parentElement; node && node !== document.body; node = node.parentElement) {
      const style = window.getComputedStyle(node);
      if (/auto|scroll|hidden|clip/.test(`${style.overflowX} ${style.overflowY}`)) found.push(node);
    }
    return found;
  }

  // The part of a box its content shows through: inside its borders, and not
  // under its scrollbars.
  function viewOf(node) {
    const box = node.getBoundingClientRect();
    const top = box.top + node.clientTop;
    const left = box.left + node.clientLeft;
    return { top, left, right: left + node.clientWidth, bottom: top + node.clientHeight };
  }

  // The box round the whole target, or with `visible`, round only the part of
  // it the boxes it sits in are showing: a column runs on below the bottom of
  // its table's scrolling box, and the spotlight stops where the box does.
  function boxOf(nodes, visible) {
    let top = Infinity;
    let left = Infinity;
    let right = -Infinity;
    let bottom = -Infinity;
    nodes.forEach((node) => {
      const box = node.getBoundingClientRect();
      top = Math.min(top, box.top);
      left = Math.min(left, box.left);
      right = Math.max(right, box.right);
      bottom = Math.max(bottom, box.bottom);
    });
    if (visible) {
      clippersOf(nodes).forEach((node) => {
        const view = viewOf(node);
        top = Math.max(top, view.top);
        left = Math.max(left, view.left);
        right = Math.min(right, view.right);
        bottom = Math.min(bottom, view.bottom);
      });
      right = Math.max(right, left);
      bottom = Math.max(bottom, top);
    }
    return { top, left, right, bottom, width: right - left, height: bottom - top };
  }

  // How far to scroll along one direction to show start..end within from..to:
  // nothing when it already shows, or when it is too long to fit and some of it
  // shows; otherwise enough to centre it, or, too long to fit, to bring in its
  // start.
  function shiftToShow(start, end, from, to) {
    if (start >= from && end <= to) return 0;
    if (end - start > to - from) return start < to && end > from ? 0 : start - from;
    return (start + end) / 2 - (from + to) / 2;
  }

  // Scrolls each box the target sits in that scrolls by itself, innermost
  // first, until the target is in its view. The page is scrollTo's to move. At
  // once rather than smoothly: the spotlight is measured straight after, and a
  // box scrolling by itself tells the window nothing.
  function reveal(nodes) {
    clippersOf(nodes).forEach((node) => {
      const style = window.getComputedStyle(node);
      const box = boxOf(nodes, false);
      const view = viewOf(node);
      if (/auto|scroll/.test(style.overflowX)) node.scrollLeft += shiftToShow(box.left, box.right, view.left, view.right);
      if (/auto|scroll/.test(style.overflowY)) node.scrollTop += shiftToShow(box.top, box.bottom, view.top, view.bottom);
    });
  }

  // Whether Next is the step's action rather than a move on: it has one, the
  // page still wants it, and this tour hasn't run it already. Asked afresh at
  // each show, since the page's answer changes as the tour's actions land.
  function actionDue(step) {
    return Boolean(step.action && !(tour && tour.ran.includes(step)) && step.action.needed());
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
      if (!node || (target && target.some((part) => node.contains(part)))) return;
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

  // The top that keeps the whole card on screen, buttons and all: the one
  // asked for, unless that would run the card's foot off the bottom of the
  // screen. A card with a long list can be nearly the screen's height, and its
  // text scrolls inside it but its buttons don't. Over the top bar rather than
  // with Next out of reach.
  function onScreen(top, size) {
    return Math.max(PAD, Math.min(top, window.innerHeight - size.height - PAD));
  }

  function place() {
    // Run a frame or two after a step is shown, by when Skip or Escape may
    // have closed the tour.
    if (!tour) return;
    const card = $("page-tour-card");
    const spotlight = $("page-tour-spotlight");
    const target = targetOf(tour.steps[tour.index]);
    // A target its scrolling box has none of on show is as good as not drawn.
    const box = target && boxOf(target, true);
    const lit = Boolean(box && box.width > 0 && box.height > 0);
    // With nothing lit there is no spotlight to cast the shadow that dims the
    // page, so the overlay does it instead.
    $("page-tour").classList.toggle("is-whole-page", !lit);

    if (!lit) {
      // Nothing to point at: centre the card and leave the page evenly dimmed.
      // Its size is read rather than assumed, as a card with a list is wider
      // and may be too tall to start a fifth of the way down.
      const size = card.getBoundingClientRect();
      spotlight.hidden = true;
      card.style.top = `${onScreen(window.innerHeight * 0.2, size)}px`;
      card.style.left = `max(1rem, calc(50vw - ${size.width / 2}px))`;
      return;
    }

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
    card.style.top = `${onScreen(spot.top, size)}px`;
    card.style.left = `${spot.left}px`;
  }

  // Brings the target on screen with room for the card beside it, and leaves
  // the page alone when it already is. Otherwise centred when that leaves room
  // above or below it, or just under the top bar with the card underneath: a
  // table centred on a laptop screen leaves too little on either side, and the
  // card, pinned to the bottom of the screen, would cover the rows the step is
  // about.
  function scrollTo(target) {
    const box = boxOf(target, true);
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

  // A step field that may be given as it is or as a function returning it.
  function valueOf(field) {
    return typeof field === "function" ? field() : field;
  }

  // One entry of a step's `points`: a string, or [term, text], the term in bold
  // on a line of its own above what it means.
  function pointItem(point) {
    const item = document.createElement("li");
    if (!Array.isArray(point)) {
      item.textContent = point;
      return item;
    }
    const term = document.createElement("strong");
    term.className = "page-tour-term";
    term.textContent = point[0];
    item.appendChild(term);
    item.appendChild(document.createTextNode(point[1]));
    return item;
  }

  function show() {
    const steps = tour.steps;
    const step = steps[tour.index];
    const label = valueOf(step.label);
    const points = valueOf(step.points) || [];
    const cite = valueOf(step.cite) || "";

    $("page-tour-step").textContent = `Step ${tour.index + 1} of ${steps.length}` + (label ? ` · ${label}` : "");
    $("page-tour-title").textContent = step.title;
    $("page-tour-body").textContent = valueOf(step.body);
    $("page-tour-points").replaceChildren(...points.map(pointItem));
    $("page-tour-points").hidden = !points.length;
    $("page-tour-cite").textContent = cite;
    $("page-tour-cite").hidden = !cite;
    // A card with a list is wider, so the list reads in fewer, longer lines.
    $("page-tour-card").classList.toggle("has-points", points.length > 0);
    // A step long enough to scroll on a short screen starts from its top, not
    // from wherever the one before it had been scrolled to.
    $("page-tour-content").scrollTop = 0;
    $("page-tour-back").disabled = tour.index === 0;
    $("page-tour-next").disabled = false;
    $("page-tour-next").textContent = actionDue(step)
      ? step.action.label
      : tour.index === steps.length - 1
      ? "Done"
      : "Next ►";
    tour.furthest = Math.max(tour.furthest, tour.index);

    // After the text, which is what sets the card's height. Within its own
    // scrolling box first, so the page is scrolled to where it then is.
    const target = targetOf(step);
    if (target) {
      reveal(target);
      scrollTo(target);
    }

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

  // Runs the step's action with the card's buttons held, then shows the step
  // again. Skip still works while it runs: the page carries on without the
  // tour, and a tour that has closed, or been closed and opened again, by the
  // time it lands is left alone. Focus goes to the card for the wait, as Next
  // is disabled and a disabled button drops it.
  async function runAction(step) {
    const running = tour;
    running.ran.push(step);
    $("page-tour-card").focus();
    $("page-tour-back").disabled = true;
    $("page-tour-next").disabled = true;
    $("page-tour-next").textContent = "Loading…";
    $("page-tour-card").setAttribute("aria-busy", "true");
    try {
      await step.action.run();
    } catch (err) {
      // The page reports its own failures; the step is shown again either way,
      // and its text can say what didn't happen.
    }
    if (tour !== running) return;
    $("page-tour-card").removeAttribute("aria-busy");
    show();
    $("page-tour-next").focus();
  }

  // True when the tour opened. It doesn't when another is already open, when
  // the page has no overlay, or when none of the steps has anything to show.
  function start(steps, options) {
    if (isOpen() || !$("page-tour")) return false;
    let gated = false;
    const shown = steps.filter((step) => {
      if (step.when && !step.when()) return false;
      if (step.action) gated = true;
      return gated || !step.target || Boolean(targetOf(step));
    });
    if (!shown.length) return false;
    tour = {
      steps: shown,
      options: options || {},
      index: 0,
      furthest: 0,
      ran: [],
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
    const { steps, furthest, options, focusBefore } = tour;
    tour = null;
    $("page-tour").hidden = true;
    $("page-tour-card").removeAttribute("aria-busy");
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
    if (options.onEnd) options.onEnd(steps.slice(0, furthest + 1));
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
      const step = tour.steps[tour.index];
      if (actionDue(step)) {
        runAction(step);
      } else if (tour.index < tour.steps.length - 1) {
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
