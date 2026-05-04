/* ============================================================
   Ember · Operating Cashflows — vanilla JS controller.
   Reads the data island (#ops-data), wires the pivot/window
   filter, re-renders the pivot table on change, persists
   theme to localStorage["ember-theme"] (shared with /returns + /capital).
   ============================================================ */

(function () {
  "use strict";

  // ── Data load ──────────────────────────────────────────────
  var dataNode = document.getElementById("ops-data");
  if (!dataNode) return;
  var DATA;
  try { DATA = JSON.parse(dataNode.textContent); }
  catch (e) { console.error("ops-data is not valid JSON", e); return; }

  // DATA shape:
  // {
  //   cats: ["Development Fees", ...],            // 6 strings
  //   projects: ["Grand Prairie Development", ...],
  //   cat_colors: { "Development Fees": "#F25929", ... },
  //   month_dates: [{ short:"Jan", yy:"26", year:2026 }, ...],   // 18 entries
  //   now_idx: 3,
  //   monthly_totals_by_cat: { "Development Fees": [..18..], ... },     // raw $
  //   project_monthly_totals: { "Grand Prairie...": [..18..], ... },     // raw $
  //   monthly_grand_total: [..18..],
  //   default_from_idx: 0, default_to_idx: 17,
  // }

  // ── Helpers ────────────────────────────────────────────────
  function fmtIntComma(n) {
    if (n === 0) return "0";
    return Math.round(n).toLocaleString();
  }
  function classes(arr) { return arr.filter(Boolean).join(" "); }

  // Sparkline SVG (matches design ref) — 88×22
  function sparkline(values, color, bold, nowOffsetInWindow) {
    var w = 88, h = 22, pad = 2;
    var max = Math.max.apply(null, values), min = Math.min.apply(null, values);
    var range = (max - min) || 1;
    var step = (w - pad * 2) / Math.max(1, values.length - 1);
    var d = "";
    values.forEach(function (v, i) {
      var x = pad + i * step;
      var y = pad + (1 - (v - min) / range) * (h - pad * 2);
      d += (i === 0 ? "M" : "L") + " " + x.toFixed(1) + " " + y.toFixed(1) + " ";
    });
    var dot = "";
    if (nowOffsetInWindow >= 0 && nowOffsetInWindow < values.length) {
      var nx = pad + nowOffsetInWindow * step;
      var ny = pad + (1 - (values[nowOffsetInWindow] - min) / range) * (h - pad * 2);
      dot = '<circle cx="' + nx.toFixed(1) + '" cy="' + ny.toFixed(1) + '" r="1.8" fill="' + color + '"/>';
    }
    return (
      '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">' +
        '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="' + (bold ? 1.6 : 1.2) + '" opacity="' + (bold ? 1 : 0.85) + '"/>' +
        dot +
      "</svg>"
    );
  }

  // ── Theme handling ─────────────────────────────────────────
  // Removed — unified in templates/_partials/_account_modal.html, which
  // syncs localStorage["ember-theme"] (dark/light) to .ops-page's
  // data-theme (navy/paper) on DOMContentLoaded. The previous local
  // handler here expected "navy"/"paper" in localStorage and was
  // OVERWRITING the canonical "dark"/"light" value with "paper",
  // resetting the theme on every other page.

  // ── Filter bar wiring ──────────────────────────────────────
  var bar = document.querySelector("[data-filter-bar]");
  if (!bar) return;
  var fromSel = bar.querySelector("[data-filter-from]");
  var toSel   = bar.querySelector("[data-filter-to]");
  var meta    = bar.querySelector("[data-window-meta]");
  var titleEl = document.querySelector("[data-panel-title]");
  var mount   = document.querySelector("[data-pivot-mount]");
  var pivotGroup = document.querySelector("[data-pivot-group]");

  var state = {
    pivot: "project",
    from: parseInt(bar.getAttribute("data-default-from"), 10) || 0,
    to:   parseInt(bar.getAttribute("data-default-to"), 10) || (DATA.month_dates.length - 1),
    nowIdx: parseInt(bar.getAttribute("data-now-idx"), 10) || 3,
  };

  // ── Render ─────────────────────────────────────────────────
  function render() {
    if (state.from > state.to) { state.from = state.to; fromSel.value = state.from; }
    var months = DATA.month_dates.slice(state.from, state.to + 1);
    var isProj = state.pivot === "project";
    var rows = isProj ? DATA.projects : DATA.cats;

    titleEl.textContent = isProj ? "Project × Month" : "Category × Month";
    meta.textContent = "Showing " + months.length + " months · default = past 3 + next 15";

    var headHtml = '<tr><th class="l">' + (isProj ? "Project" : "Category") + '</th>' +
                   '<th class="trend">Trend</th><th class="tot">Total</th>';
    months.forEach(function (m, i) {
      var globalIdx = state.from + i;
      var cls = (globalIdx === state.nowIdx) ? "now" : "";
      headHtml += '<th class="' + cls + '">' + m.short + "<br/>" + m.yy + "</th>";
    });
    headHtml += "</tr>";

    var bodyHtml = "";
    rows.forEach(function (key) {
      var src = isProj ? DATA.project_monthly_totals[key] : DATA.monthly_totals_by_cat[key];
      var series = src.slice(state.from, state.to + 1);
      var total = series.reduce(function (s, v) { return s + v; }, 0);
      var color = isProj ? "var(--accent)" : (DATA.cat_colors[key] || "var(--ink)");
      var nowInWin = state.nowIdx - state.from;

      bodyHtml += "<tr>";
      var swatch = isProj ? "" : '<span class="swatch" style="background:' + color + '"></span>';
      bodyHtml += '<td class="l">' + swatch + key + "</td>";
      bodyHtml += '<td class="trend">' + sparkline(series, color, false, nowInWin) + "</td>";
      bodyHtml += '<td class="tot">' + fmtIntComma(total / 1000) + "</td>";
      series.forEach(function (v, i) {
        var globalIdx = state.from + i;
        var cls = (globalIdx === state.nowIdx) ? "now" : "";
        bodyHtml += '<td class="' + cls + '">' + fmtIntComma(v / 1000) + "</td>";
      });
      bodyHtml += "</tr>";
    });

    // Total row
    var totalSeries = DATA.monthly_grand_total.slice(state.from, state.to + 1);
    var grand = totalSeries.reduce(function (s, v) { return s + v; }, 0);
    bodyHtml += '<tr class="tot-row"><td class="l">Total · $K</td>';
    bodyHtml += '<td class="trend">' + sparkline(totalSeries, "var(--ink)", true, state.nowIdx - state.from) + "</td>";
    bodyHtml += '<td class="tot accent">' + fmtIntComma(grand / 1000) + "</td>";
    totalSeries.forEach(function (v, i) {
      var globalIdx = state.from + i;
      var cls = (globalIdx === state.nowIdx) ? "now" : "";
      bodyHtml += '<td class="' + cls + '">' + fmtIntComma(v / 1000) + "</td>";
    });
    bodyHtml += "</tr>";

    mount.innerHTML =
      '<table class="ops-pivot"><thead>' + headHtml + "</thead><tbody>" + bodyHtml + "</tbody></table>";
  }

  // ── Events ─────────────────────────────────────────────────
  pivotGroup.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-pivot]");
    if (!btn) return;
    state.pivot = btn.getAttribute("data-pivot");
    pivotGroup.querySelectorAll("[data-pivot]").forEach(function (b) {
      b.classList.toggle("is-active", b === btn);
    });
    render();
    syncHash();
  });

  fromSel.addEventListener("change", function () { state.from = parseInt(fromSel.value, 10); render(); syncHash(); });
  toSel.addEventListener("change",   function () { state.to   = parseInt(toSel.value, 10);   render(); syncHash(); });

  // ── Hash deep-linking (#pivot=category&from=3&to=14) ──────
  function syncHash() {
    var h = "pivot=" + state.pivot + "&from=" + state.from + "&to=" + state.to;
    history.replaceState(null, "", "#" + h);
  }
  function readHash() {
    var h = (location.hash || "").replace(/^#/, "");
    if (!h) return;
    h.split("&").forEach(function (kv) {
      var p = kv.split("=");
      if (p[0] === "pivot" && (p[1] === "project" || p[1] === "category")) state.pivot = p[1];
      if (p[0] === "from") state.from = parseInt(p[1], 10);
      if (p[0] === "to")   state.to   = parseInt(p[1], 10);
    });
    if (state.pivot === "category") {
      pivotGroup.querySelectorAll("[data-pivot]").forEach(function (b) {
        b.classList.toggle("is-active", b.getAttribute("data-pivot") === "category");
      });
    }
    fromSel.value = state.from;
    toSel.value   = state.to;
  }
  readHash();
  render();
})();
