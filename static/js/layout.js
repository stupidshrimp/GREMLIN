(function () {
  const storageKey = "gremlin.sidebar.collapsed";
  const body = document.body;
  const toggle = document.getElementById("sidebarToggle");

  if (!toggle) return;

  const toggleIcon = toggle.querySelector(".sidebar-toggle-icon");

  const applyState = (collapsed) => {
    body.classList.toggle("sidebar-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute(
      "aria-label",
      collapsed ? "Expand sidebar" : "Collapse sidebar"
    );
    toggle.setAttribute(
      "title",
      collapsed ? "Expand sidebar" : "Collapse sidebar"
    );

    if (toggleIcon) {
      toggleIcon.textContent = collapsed ? "⇥" : "⇤";
    }
  };

  // Touching localStorage throws (not just returns null) when storage access is
  // blocked — private-mode settings, an opaque origin, a sandboxed iframe. That
  // must not take the toggle down with it: losing persistence is acceptable,
  // losing the ability to collapse the sidebar at all is not.
  const readState = () => {
    try {
      return localStorage.getItem(storageKey);
    } catch (e) {
      return null;
    }
  };

  const saveState = (collapsed) => {
    try {
      localStorage.setItem(storageKey, String(collapsed));
    } catch (e) {
      /* Persistence is unavailable; the toggle still works for this page. */
    }
  };

  const saved = readState();
  if (saved !== null) {
    applyState(saved === "true");
  }

  toggle.addEventListener("click", () => {
    const collapsed = !body.classList.contains("sidebar-collapsed");
    applyState(collapsed);
    saveState(collapsed);
  });
})();

(function () {
  // `kind` names a variant in sidebar.css and is optional. Left off, the toast
  // keeps the alarmed look that bare `.gremlin-toast` carries, which is what
  // every caller predating this argument wants -- all of them are reporting a
  // write the server refused. Pass "info" for one that is merely telling you
  // something, so a note about a feature that does not exist yet does not
  // arrive in the same red as a rejected save.
  //
  // Returns the toast so a caller that can fire repeatedly has something to ask
  // whether the last one it put up is still on screen -- see the notifications
  // button in topbar_tools.js. Nothing is obliged to use it.
  window.gremlinToast = function (message, kind) {
    let host = document.getElementById("gremlinToastHost");
    if (!host) {
      host = document.createElement("div");
      host.id = "gremlinToastHost";
      host.className = "gremlin-toast-host";
      document.body.appendChild(host);
    }
    const toast = document.createElement("div");
    toast.className = "gremlin-toast" + (kind ? " is-" + kind : "");
    toast.setAttribute("role", "status");
    toast.textContent = message;
    host.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("is-visible"));
    setTimeout(() => { toast.classList.remove("is-visible"); setTimeout(() => toast.remove(), 250); }, 5000);
    return toast;
  };

  const dialog = document.getElementById("accountDialog");
  const open = document.getElementById("accountButton");
  if (!dialog || !open) return;
  open.addEventListener("click", () => dialog.showModal());
  document.getElementById("accountClose")?.addEventListener("click", () => dialog.close());
  document.getElementById("logoutButton")?.addEventListener("click", async () => {
    // Carry the session's token: signing out is a session write like any
    // other, and the server refuses one that did not come from a page it
    // rendered. The token is in the page whenever somebody is signed in,
    // which is the only time this button exists.
    const body = new FormData();
    body.append("csrf_token", document.querySelector('meta[name="gremlin-csrf-token"]')?.content || "");
    await fetch("/auth/logout", { method: "POST", body });
    window.location.reload();
  });
  document.getElementById("loginForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = document.getElementById("loginError");
    const response = await fetch("/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget)))
    });
    const payload = await response.json();
    if (!response.ok) { error.textContent = payload.error; error.hidden = false; return; }
    window.location.reload();
  });
})();

// Sidebar entries for pages that need an account: the toast on click, and the
// hint beside them on hover.
//
// After the block above, which is where gremlinToast is defined.
(function () {
  "use strict";

  var GAP = 8;

  var locked = document.querySelectorAll(".sidebar .nav-locked");
  if (!locked.length) {
    // Everybody is offered the whole sidebar, so there is nothing locked to
    // explain -- which is every page load by somebody signed in.
    return;
  }

  // Which entry's hint is currently on screen. The hint is positioned against
  // the viewport (see sidebar.css), so anything that moves the entry -- the
  // sidebar scrolling under a still pointer, the window resizing -- has to move
  // the hint with it or it is left pointing at nothing.
  var showing = null;

  function place(entry) {
    var hint = entry.querySelector(".nav-lock-hint");
    if (!hint) {
      return;
    }
    var anchorBox = entry.getBoundingClientRect();
    // Measured after the entry, and while the hint is still invisible: it is
    // laid out either way, so this costs nothing and avoids a flash at the
    // wrong coordinates on the first hover.
    var hintBox = hint.getBoundingClientRect();
    var left;
    var top;

    if (anchorBox.right + GAP + hintBox.width <= window.innerWidth - GAP) {
      // The usual case: beside the sidebar, centred on the entry.
      left = anchorBox.right + GAP;
      top = anchorBox.top + anchorBox.height / 2 - hintBox.height / 2;
    } else {
      // Under 900px the sidebar spans the whole width as a grid of entries, so
      // there is no "beside" -- and squeezing the hint in anyway would lay it
      // over the entry whose name is the thing being asked about. Below the
      // entry instead, which covers a neighbour rather than the subject.
      left = anchorBox.left;
      top = anchorBox.bottom + GAP;
    }

    left = Math.max(GAP, Math.min(left, window.innerWidth - GAP - hintBox.width));
    top = Math.max(GAP, Math.min(top, window.innerHeight - GAP - hintBox.height));
    hint.style.left = Math.round(left) + "px";
    hint.style.top = Math.round(top) + "px";
  }

  function show(entry) {
    showing = entry;
    place(entry);
  }

  function hide(entry) {
    if (showing === entry) {
      showing = null;
    }
  }

  Array.prototype.forEach.call(locked, function (entry) {
    entry.addEventListener("click", function () {
      var message = entry.getAttribute("data-locked-message");
      if (message && window.gremlinToast) {
        // "info", not the bare alarm: nothing was refused that the person had
        // any business expecting to work. It is telling them where the door is.
        window.gremlinToast(message, "info");
      }
    });

    // pointerenter rather than mouseenter so a pen or a finger counts the same
    // as a cursor; focus is the keyboard's equivalent, and the stylesheet
    // reveals the hint on :focus-visible to match.
    entry.addEventListener("pointerenter", function () { show(entry); });
    entry.addEventListener("focus", function () { show(entry); });
    entry.addEventListener("pointerleave", function () { hide(entry); });
    entry.addEventListener("blur", function () { hide(entry); });
  });

  var follow = function () {
    if (showing) {
      place(showing);
    }
  };
  // Capturing, because the scrolling is the sidebar's own and a scroll event on
  // an element does not bubble.
  window.addEventListener("scroll", follow, true);
  window.addEventListener("resize", follow);
})();

// The help contacts, which are read from a dialog in both of the places they
// are offered. Wired up on its own rather than inside the account dialog's
// block above, because that block gives up early on a page without an account
// button and neither of these dialogs should disappear with it.
(function () {
  const wire = (buttonId, dialogId, closeId) => {
    const dialog = document.getElementById(dialogId);
    const open = document.getElementById(buttonId);
    // Either half can be legitimately absent: the login help button is drawn
    // only for a visitor who is not signed in.
    if (!dialog || !open) return;
    open.addEventListener("click", () => dialog.showModal());
    document.getElementById(closeId)?.addEventListener("click", () => dialog.close());
  };

  // Under the login form. Opened over the account dialog, which stays where it
  // is: closing this one hands the login form straight back, still filled in.
  wire("accountHelpButton", "supportDialog", "supportClose");
  // In the footer, on every page and whether or not anybody is signed in.
  wire("footerHelpButton", "footerHelpDialog", "footerHelpClose");
})();
