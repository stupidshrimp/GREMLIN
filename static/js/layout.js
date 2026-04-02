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

  const saved = localStorage.getItem(storageKey);
  if (saved !== null) {
    applyState(saved === "true");
  }

  toggle.addEventListener("click", () => {
    const collapsed = !body.classList.contains("sidebar-collapsed");
    applyState(collapsed);
    localStorage.setItem(storageKey, String(collapsed));
  });
})();
