/* The topbar's tool rail: the theme toggle, the quick links dropdown, and the
 * notifications button.
 *
 * Nothing here decides what anything looks like -- theme.css holds both
 * palettes, and all this file does is put `data-theme` on <html>, animate the
 * moment it changes, and keep the button's label honest about what pressing it
 * will do.
 *
 * Light is the default and the machine's own `prefers-color-scheme` is not
 * consulted: dark is somewhere you go, not somewhere you are put. So the only
 * thing that ever selects it is a press of this button, remembered.
 *
 * The storage key and the default are duplicated, deliberately, by the inline
 * script in base.html. That script runs before first paint so the light theme
 * never flashes past on the way to the dark one, which means it cannot wait for
 * this file to load. Change the rule in one place and change it in the other. */
(function () {
  const STORAGE_KEY = "gremlin.theme";
  const root = document.documentElement;
  const toggle = document.getElementById("themeToggle");

  // How long the two ways of getting from one theme to the other run for. The
  // reveal is the longer of the two because it travels: a wipe that crosses the
  // window in the time a cross-fade takes reads as a flinch rather than a sweep.
  const REVEAL_MS = 520;
  const FADE_MS = 320;
  const FADE_CLASS = "is-theme-switching";

  // Touching localStorage throws rather than returning null when storage access
  // is blocked -- private-mode settings, an opaque origin, a sandboxed iframe.
  // Losing the saved preference is acceptable; losing the ability to switch
  // themes at all for this page is not, so the write swallows it. Reading it back
  // is base.html's job, before this file has loaded.
  const store = (theme) => {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (e) {
      /* The theme still holds for this page; it just will not be remembered. */
    }
  };

  // What the page is actually showing, read off the attribute rather than kept
  // in a variable of its own: the inline script in base.html sets it before this
  // file runs, and a variable initialised here would start out disagreeing with
  // the page on every load.
  const current = () => (root.getAttribute("data-theme") === "dark" ? "dark" : "light");

  function apply(theme) {
    root.setAttribute("data-theme", theme);

    // Anything that cannot be themed by the cascade alone gets told. That is the
    // canvas charts on Metrics and Life Data Analysis: a bitmap inherits nothing
    // and has to be drawn again in the new palette. See chart_theme.js, which is
    // what listens for this.
    document.dispatchEvent(new CustomEvent("gremlin:themechange", { detail: { theme: theme } }));

    if (!toggle) return;

    // The button offers the theme you are not in, and says so. Both icons are
    // already in the DOM -- topbar.css decides which of them is showing -- so
    // there is nothing to swap here beyond the label.
    //
    // Named for the action rather than carrying `aria-pressed` for the state.
    // The two together contradict each other out loud: "switch to light theme,
    // toggle button, pressed" tells a screen reader user both that the page is
    // light and that it is dark. The label alone says which theme is on by
    // saying which one is on offer, and the tooltip can then be the same
    // sentence instead of a second, different one.
    const next = theme === "dark" ? "light" : "dark";
    const label = next === "dark" ? "Switch to dark theme" : "Switch to light theme";
    toggle.setAttribute("aria-label", label);
    toggle.setAttribute("title", label);
  }

  // Applied, not switched: this is the page catching its own button up with the
  // theme base.html already set, and there is nothing to animate between.
  apply(current());

  const wantsReducedMotion = () =>
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- the two ways of changing theme ---------------------------------------

  // The fallback, and what a change from another tab uses. A class on <html>
  // turns on a colour transition for everything, the swap happens underneath it,
  // and the class comes off again. It is switched on for the duration rather
  // than left in the stylesheet because a permanent transition on every element
  // in the page would put a lag on every hover as well.
  let fadeTimer = 0;
  function crossFade(theme) {
    root.classList.add(FADE_CLASS);

    // Not a superstition, and not removable. A transition starts by comparing
    // the style before the change against the style after it, and it only starts
    // if the *before* style already had a transition on that property. Add the
    // class and swap the palette in one go and there is no such before state --
    // the browser sees one update that both turns transitions on and changes
    // every colour, and the colours simply snap. Reading a layout property here
    // forces the class to be taken up on its own first, which is what gives the
    // change something to transition from.
    void root.offsetWidth;

    apply(theme);
    window.clearTimeout(fadeTimer);
    fadeTimer = window.setTimeout(() => root.classList.remove(FADE_CLASS), FADE_MS);
  }

  // The good one, where the browser can do it. A view transition photographs the
  // page before and after, so the whole window can be wiped from one theme to the
  // other in one piece -- no element transitions its own colour, which is what
  // makes it look like a light being turned on rather than a thousand things
  // changing at once. The circle grows from the button that was pressed, so the
  // change visibly comes from there.
  function reveal(theme, origin) {
    const transition = document.startViewTransition(() => apply(theme));

    transition.ready
      .then(() => {
        const width = window.innerWidth;
        const height = window.innerHeight;
        const box = origin.getBoundingClientRect();
        const x = box.left + box.width / 2;
        const y = box.top + box.height / 2;
        // Far enough to clear the window: the distance to whichever corner is
        // furthest from the button, which is the longest of the two horizontal
        // and two vertical reaches combined.
        const radius = Math.hypot(Math.max(x, width - x), Math.max(y, height - y));

        // All four of those are CSS pixels of *this page*. The circle is not
        // drawn on this page: it is drawn on ::view-transition-new(root), a
        // snapshot the browser owns, and a pixel in there is not obliged to be
        // the pixel out here. On a display running at 250% it is not, and the
        // whole shape comes out scaled towards the top left corner -- so the
        // button in the top right draws its circle from the top middle, and a
        // radius that should have reached the far corner gives up around halfway
        // through. The clip then reverts to none as the animation ends, which is
        // the rest of the screen arriving all at once.
        //
        // Percentages are resolved against the snapshot's own box, so they carry
        // over. Turning the measurements into them here is the last point at
        // which this page's pixels are involved at all.
        //
        // A percentage radius is measured against the box's diagonal over root
        // two rather than against either side, so that is what the radius has to
        // be given as a fraction of. Both are lengths on this page and the units
        // cancel, which is the whole point.
        const atX = (x / width) * 100;
        const atY = (y / height) * 100;
        const reach = (radius * Math.SQRT2 * 100) / Math.hypot(width, height);
        const circle = (r) => "circle(" + r + "% at " + atX + "% " + atY + "%)";

        root.animate(
          {
            clipPath: [circle(0), circle(reach)],
          },
          {
            duration: REVEAL_MS,
            easing: "cubic-bezier(0.4, 0, 0.2, 1)",
            pseudoElement: "::view-transition-new(root)",
          }
        );
      })
      // `ready` rejects when the transition is skipped -- another one started, or
      // the tab was hidden mid-flight. The theme is applied either way, so there
      // is nothing to recover from; this is only here to keep a skipped animation
      // from surfacing as an unhandled rejection.
      .catch(() => {});
  }

  // `origin` is the element the change should appear to come from, and passing
  // none is how a caller says "no wipe" -- a change that arrives from another tab
  // has no button behind it to grow out of.
  function switchTo(theme, origin) {
    if (wantsReducedMotion()) {
      apply(theme);
    } else if (origin && typeof document.startViewTransition === "function") {
      reveal(theme, origin);
    } else {
      crossFade(theme);
    }
  }

  if (toggle) {
    toggle.addEventListener("click", () => {
      const next = current() === "dark" ? "light" : "dark";
      switchTo(next, toggle);
      store(next);
    });
  }

  // GREMLIN is a multi-tab application -- an analysis in one tab, the metrics it
  // is being read against in another -- and a theme that only changed in the tab
  // you pressed the button in would be a bug in all the others. `storage` fires
  // only in the tabs that did not make the write, which is exactly the set that
  // needs telling. It cross-fades rather than wipes: the wipe is meant to come
  // out of the button that was pressed, and in these tabs nobody pressed one.
  window.addEventListener("storage", (event) => {
    if (event.key !== STORAGE_KEY) return;
    const theme =
      event.newValue === "dark" || event.newValue === "light" ? event.newValue : "light";
    switchTo(theme, null);
  });
})();

/* The quick links dropdown.
 *
 * A menu button over a static list: open, close, and arrow through it. The links
 * themselves are ordinary anchors rendered by topbar.html, so a middle click or
 * a modified click still does what the browser would do with any other link --
 * nothing here intercepts activation. */
(function () {
  const root = document.getElementById("quickLinks");
  const button = document.getElementById("quickLinksButton");
  const menu = document.getElementById("quickLinksMenu");

  if (!root || !button || !menu) return;

  const items = () => Array.from(menu.querySelectorAll(".topbar-menu-item"));

  function open(focusFirst) {
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    if (focusFirst) {
      const first = items()[0];
      if (first) first.focus();
    }
  }

  function close(returnFocus) {
    if (menu.hidden) return;
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    // Only on a deliberate dismissal -- Escape, or a second press of the button.
    // Pulling focus back after a click elsewhere would take it off whatever the
    // click had just landed on.
    if (returnFocus) button.focus();
  }

  const isOpen = () => !menu.hidden;

  // Wraps at both ends: from the last item down is the first, and from the first
  // item up is the last. A menu this short is quicker to leave by wrapping than
  // by arrowing back through it. A negative index wraps the same way, which is
  // what End uses to mean "the last one" without having to count.
  function focusItem(index) {
    const list = items();
    if (!list.length) return;
    list[((index % list.length) + list.length) % list.length].focus();
  }

  // Focus is on the button rather than on an item the first time this runs, so
  // there is no "from" to step off: opening downwards lands on the first item
  // and upwards on the last.
  function move(step) {
    const from = items().indexOf(document.activeElement);
    if (from === -1) focusItem(step > 0 ? 0 : -1);
    else focusItem(from + step);
  }

  button.addEventListener("click", () => {
    if (isOpen()) close(false);
    else open(false);
  });

  button.addEventListener("keydown", (event) => {
    // Down and Up open the menu onto an item, which is what a menu button is
    // expected to do; Enter and Space are already handled as a click.
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      open(false);
      move(event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Escape") {
      close(true);
    }
  });

  menu.addEventListener("keydown", (event) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        move(1);
        break;
      case "ArrowUp":
        event.preventDefault();
        move(-1);
        break;
      case "Home":
        event.preventDefault();
        focusItem(0);
        break;
      case "End":
        event.preventDefault();
        focusItem(-1);
        break;
      case "Escape":
        event.preventDefault();
        close(true);
        break;
      case "Tab":
        // Tabbing out of a menu closes it, but the focus move is the browser's
        // to make -- so no preventDefault, and no focus returned to the button.
        close(false);
        break;
      default:
        break;
    }
  });

  // A link in here opens in a new tab, so this one stays where it is. Closing
  // the menu anyway means coming back to a tab that is not still holding a panel
  // open over the page.
  menu.addEventListener("click", (event) => {
    if (event.target.closest(".topbar-menu-item")) close(false);
  });

  document.addEventListener("pointerdown", (event) => {
    if (!root.contains(event.target)) close(false);
  });

  // Escape from anywhere, not only from inside the menu. Focus can be sitting
  // somewhere else entirely while this is open -- the search box, a card on the
  // page -- and the key that dismisses a panel should not depend on having kept
  // focus inside it. The handlers above have already closed it when focus was
  // in the menu, so this is a no-op in that case rather than a second close.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isOpen()) close(true);
  });
})();

/* Notifications.
 *
 * There is no feed behind the bell yet, so pressing it says so. That is the
 * whole feature for now, and it is here because the alternative is worse: a
 * button that swallows every press is indistinguishable from a broken one, and
 * this is a button people press.
 *
 * A toast rather than a panel, deliberately. The markup in topbar.html carries
 * no `aria-expanded` or `aria-controls` because those promise a screen reader
 * that something opens, and nothing does -- so the message goes to the live
 * region the toast already is. Building the real feed means adding the panel,
 * those attributes, and the unread badge, and deleting this block. */
(function () {
  const button = document.getElementById("notificationsButton");

  if (!button) return;

  const MESSAGE = "Notifications are still under development.";

  // Holding the last toast is what keeps a second press from stacking the same
  // sentence up the screen. `isConnected` asks the toast itself rather than
  // this file keeping its own copy of how long one lasts -- that timing lives
  // in gremlinToast, and a duplicate of it here would go stale the first time
  // it changed there.
  let toast = null;

  button.addEventListener("click", () => {
    // layout.js defines it and base.html loads that first, so this is only a
    // guard against the ordering being changed out from under the button.
    if (typeof window.gremlinToast !== "function") return;
    if (toast && toast.isConnected) return;
    toast = window.gremlinToast(MESSAGE, "info");
  });
})();
