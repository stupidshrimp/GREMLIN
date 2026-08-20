/* The topbar's tool rail: the theme toggle and the quick links dropdown.
 *
 * Nothing here decides what anything looks like -- theme.css holds both
 * palettes, and all this file does is put `data-theme` on <html> and keep the
 * button's label honest about what pressing it will do.
 *
 * The storage key and the resolve-the-theme logic are duplicated, deliberately,
 * by the inline script in base.html. That script runs before first paint so the
 * light theme never flashes past on the way to the dark one, which means it
 * cannot wait for this file to load. Change the rule in one place and change it
 * in the other. */
(function () {
  const STORAGE_KEY = "gremlin.theme";
  const root = document.documentElement;
  const toggle = document.getElementById("themeToggle");

  // Touching localStorage throws rather than returning null when storage access
  // is blocked -- private-mode settings, an opaque origin, a sandboxed iframe.
  // Losing the saved preference is acceptable; losing the ability to switch
  // themes at all for this page is not, so both accesses swallow it.
  const readStored = () => {
    try {
      const value = localStorage.getItem(STORAGE_KEY);
      return value === "dark" || value === "light" ? value : null;
    } catch (e) {
      return null;
    }
  };

  const store = (theme) => {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (e) {
      /* The theme still holds for this page; it just will not be remembered. */
    }
  };

  const systemQuery = window.matchMedia
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : null;

  const systemTheme = () => (systemQuery && systemQuery.matches ? "dark" : "light");

  // What the page is actually showing, read off the attribute rather than kept
  // in a variable of its own: the inline script in base.html sets it before this
  // file runs, and a variable initialised here would start out disagreeing with
  // the page on every load.
  const current = () => (root.getAttribute("data-theme") === "dark" ? "dark" : "light");

  function apply(theme) {
    root.setAttribute("data-theme", theme);
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

  apply(current());

  if (toggle) {
    toggle.addEventListener("click", () => {
      const next = current() === "dark" ? "light" : "dark";
      apply(next);
      store(next);
    });
  }

  // Somebody who has never used the toggle follows their machine, including
  // when it switches at sunset. Once they have chosen, the choice wins and this
  // stops applying -- which is the whole reason the stored value is a theme and
  // not a boolean: "no entry" is what "follow the machine" looks like.
  if (systemQuery) {
    const onSystemChange = () => {
      if (readStored()) return;
      apply(systemTheme());
    };
    // Safari only grew addEventListener on MediaQueryList in 14.
    if (systemQuery.addEventListener) {
      systemQuery.addEventListener("change", onSystemChange);
    } else if (systemQuery.addListener) {
      systemQuery.addListener(onSystemChange);
    }
  }

  // GREMLIN is a multi-tab application -- an analysis in one tab, the metrics it
  // is being read against in another -- and a theme that only changed in the tab
  // you pressed the button in would be a bug in all the others. `storage` fires
  // only in the tabs that did not make the write, which is exactly the set that
  // needs telling.
  window.addEventListener("storage", (event) => {
    if (event.key !== STORAGE_KEY) return;
    apply(event.newValue === "dark" || event.newValue === "light"
      ? event.newValue
      : systemTheme());
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
