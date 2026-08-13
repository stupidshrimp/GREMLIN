(function () {
  const storageKey = "gremlin.sidebar.collapsed";
  const body = document.body;
  const toggle = document.getElementById("sidebarToggle");

  if (!toggle) return;

  const toggleIcon = toggle.querySelector(".sidebar-toggle-icon");
  const toggleLabel = toggle.querySelector(".sidebar-toggle-label");

  const applyState = (collapsed) => {
    body.classList.toggle("sidebar-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute(
      "aria-label",
      collapsed ? "Expand sidebar" : "Collapse sidebar"
    );

    if (toggleIcon) {
      toggleIcon.textContent = collapsed ? "⇥" : "⇤";
    }

    if (toggleLabel) {
      toggleLabel.textContent = collapsed ? "Expand" : "Collapse";
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
  window.gremlinToast = function (message) {
    let host = document.getElementById("gremlinToastHost");
    if (!host) {
      host = document.createElement("div");
      host.id = "gremlinToastHost";
      host.className = "gremlin-toast-host";
      document.body.appendChild(host);
    }
    const toast = document.createElement("div");
    toast.className = "gremlin-toast";
    toast.setAttribute("role", "status");
    toast.textContent = message;
    host.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("is-visible"));
    setTimeout(() => { toast.classList.remove("is-visible"); setTimeout(() => toast.remove(), 250); }, 5000);
  };

  const dialog = document.getElementById("accountDialog");
  const open = document.getElementById("accountButton");
  if (!dialog || !open) return;
  open.addEventListener("click", () => dialog.showModal());
  document.getElementById("accountClose")?.addEventListener("click", () => dialog.close());
  document.getElementById("logoutButton")?.addEventListener("click", async () => {
    await fetch("/auth/logout", { method: "POST" });
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
