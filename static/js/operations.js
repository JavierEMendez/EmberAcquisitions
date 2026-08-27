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
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
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
    // Projects expanded into their revenue sources. Any number can be open
    // at once, so several projects' detail can be compared side by side.
    expanded: {},
  };

  // Categories a project actually has data for, over the WHOLE file rather
  // than the current window — so a row doesn't appear and vanish as the
  // window moves.
  function catsWithData(project) {
    var pcm = (DATA.project_cat_monthly || {})[project] || {};
    return (DATA.cats || []).filter(function (c) {
      var arr = pcm[c];
      return arr && arr.some(function (v) { return v !== 0; });
    });
  }

  // ── Render ─────────────────────────────────────────────────
  function render() {
    if (state.from > state.to) { state.from = state.to; fromSel.value = state.from; }
    var months = DATA.month_dates.slice(state.from, state.to + 1);
    var isProj = state.pivot === "project";
    var rows = isProj ? DATA.projects : DATA.cats;

    titleEl.textContent = isProj ? "Project × Month" : "Category × Month";
    var span = meta.getAttribute("data-axis-span");
    meta.textContent = "Showing " + months.length + " months"
                     + (span ? " · file covers " + span : "");

    var headHtml = '<tr><th class="l">' + (isProj ? "Project" : "Category") + '</th>' +
                   '<th class="trend">Trend</th><th class="tot">Total</th>';
    months.forEach(function (m, i) {
      var globalIdx = state.from + i;
      var cls = (globalIdx === state.nowIdx) ? "now" : "";
      headHtml += '<th class="' + cls + '">' + m.short + "<br/>" + m.yy + "</th>";
    });
    headHtml += "</tr>";

    var bodyHtml = "";
    var nowInWin = state.nowIdx - state.from;

    // One data row: label cell is caller-supplied so parents can carry a
    // caret and children can be indented.
    function dataRow(labelHtml, src, color, rowCls, bold) {
      var series = src.slice(state.from, state.to + 1);
      var total = series.reduce(function (s, v) { return s + v; }, 0);
      var html = '<tr' + (rowCls ? ' class="' + rowCls + '"' : "") + ">";
      html += '<td class="l">' + labelHtml + "</td>";
      html += '<td class="trend">' + sparkline(series, color, !!bold, nowInWin) + "</td>";
      html += '<td class="tot">' + fmtIntComma(total / 1000) + "</td>";
      series.forEach(function (v, i) {
        var cls = (state.from + i === state.nowIdx) ? "now" : "";
        html += '<td class="' + cls + '">' + fmtIntComma(v / 1000) + "</td>";
      });
      return html + "</tr>";
    }

    rows.forEach(function (key) {
      if (!isProj) {
        var color = DATA.cat_colors[key] || "var(--ink)";
        bodyHtml += dataRow(
          '<span class="swatch" style="background:' + color + '"></span>' + esc(key),
          DATA.monthly_totals_by_cat[key], color, null, false);
        return;
      }
      // Project row — clickable, expands into its revenue sources.
      var open = !!state.expanded[key];
      var subCats = catsWithData(key);
      var caret = subCats.length
        ? '<span class="ops-caret">' + (open ? "▾" : "▸") + "</span>"
        : '<span class="ops-caret ops-caret-empty"></span>';
      var label = '<button type="button" class="ops-rowtoggle" data-project="'
                + encodeURIComponent(key) + '"'
                + (subCats.length ? "" : " disabled")
                + ' aria-expanded="' + (open ? "true" : "false") + '">'
                + caret + esc(key) + "</button>";
      bodyHtml += dataRow(label, DATA.project_monthly_totals[key], "var(--accent)",
                          "ops-prow" + (open ? " is-open" : ""), false);
      if (!open) return;
      subCats.forEach(function (c) {
        var ccolor = DATA.cat_colors[c] || "var(--ink)";
        bodyHtml += dataRow(
          '<span class="ops-sub-label"><span class="swatch" style="background:'
            + ccolor + '"></span>' + esc(c) + "</span>",
          DATA.project_cat_monthly[key][c], ccolor, "ops-subrow", false);
      });
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
  // Expand/collapse a project into its revenue sources. Delegated because the
  // table is re-rendered wholesale on every change.
  mount.addEventListener("click", function (e) {
    var btn = e.target.closest(".ops-rowtoggle");
    if (!btn || btn.disabled) return;
    var key = decodeURIComponent(btn.getAttribute("data-project"));
    if (state.expanded[key]) delete state.expanded[key];
    else state.expanded[key] = true;
    render();
    syncHash();
  });

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
    var open = Object.keys(state.expanded);
    var h = "pivot=" + state.pivot + "&from=" + state.from + "&to=" + state.to
          + (open.length ? "&open=" + open.map(encodeURIComponent).join("|") : "");
    history.replaceState(null, "", "#" + h);
  }
  function readHash() {
    var h = (location.hash || "").replace(/^#/, "");
    if (!h) return;
    var last = DATA.month_dates.length - 1;
    h.split("&").forEach(function (kv) {
      var i = kv.indexOf("=");
      var p = [kv.slice(0, i), kv.slice(i + 1)];
      if (p[0] === "pivot" && (p[1] === "project" || p[1] === "category")) state.pivot = p[1];
      if (p[0] === "from") state.from = parseInt(p[1], 10);
      if (p[0] === "to")   state.to   = parseInt(p[1], 10);
      if (p[0] === "open" && p[1]) {
        p[1].split("|").forEach(function (k) {
          var name = decodeURIComponent(k);
          if (DATA.projects.indexOf(name) !== -1) state.expanded[name] = true;
        });
      }
    });
    // Clamp: the month axis length can change between uploads, so a stale
    // bookmark must not index past the end.
    if (!(state.from >= 0 && state.from <= last)) state.from = 0;
    if (!(state.to   >= 0 && state.to   <= last)) state.to   = last;
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
