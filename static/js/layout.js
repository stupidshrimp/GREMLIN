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

// The help contacts behind the login form's "Need log in access?" button. Wired
// up on its own rather than inside the account dialog's block above, because
// that block gives up early on a page without an account button and this dialog
// should not disappear with it.
(function () {
  const dialog = document.getElementById("supportDialog");
  const open = document.getElementById("accountHelpButton");
  if (!dialog || !open) return;
  // Opened over the account dialog, which stays where it is: closing this one
  // hands the login form straight back, still filled in.
  open.addEventListener("click", () => dialog.showModal());
  document.getElementById("supportClose")?.addEventListener("click", () => dialog.close());
})();
