/* ============================================================
   Loans & Debt — page JS.
   Minimal: theme toggle (shared with /returns, /capital, /operations).
   The page is server-rendered — no client-side data manipulation.
   ============================================================ */
(function () {
  "use strict";

  const PAGE = document.querySelector(".loans-page");
  if (!PAGE) return;

  // ── Theme toggle (shared key with sibling pages) ───────────
  const THEME_KEY = "ember-theme";
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === "navy" || stored === "paper") {
    PAGE.setAttribute("data-theme", stored);
  }

  const toggle = document.querySelector("[data-theme-toggle]");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const next = PAGE.getAttribute("data-theme") === "navy" ? "paper" : "navy";
      PAGE.setAttribute("data-theme", next);
      try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
    });
  }
})();
