/* ============================================================
   Ember Capital — Concept C interactions
   • Pill toggle: Active / Pipeline / Returns
   • Theme toggle (shared with /returns via localStorage)
   • Asset-class chip popover with PATCH on save
   ============================================================ */

(() => {
  "use strict";

  const $  = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ── Theme — read the app-wide `ember-theme` key ────────────────
  // Existing app stores 'light' or 'dark'; we map them to the cockpit's
  // 'paper' / 'navy' palettes so a user's site-wide preference carries
  // straight through to /capital without us building a second toggle.
  const THEME_KEY = "ember-theme";
  const applyTheme = (t) => document.body.setAttribute("data-theme", t === "navy" || t === "dark" ? "navy" : "paper");
  applyTheme(localStorage.getItem(THEME_KEY) || "light");

  window.setEmberTheme = (t) => { applyTheme(t); localStorage.setItem(THEME_KEY, t); };
  window.addEventListener("theme:set", (e) => window.setEmberTheme(e.detail));

  // ── View toggle (pill segment) ──────────────────────────────
  const segBtns = $$(".seg-btn");
  const views   = $$(".view");

  const setView = (id) => {
    segBtns.forEach(b => b.classList.toggle("active", b.dataset.view === id));
    views.forEach(v => { v.hidden = v.dataset.view !== id; });
    history.replaceState(null, "", "#" + id);
  };
  segBtns.forEach(b => b.addEventListener("click", () => setView(b.dataset.view)));
  // Restore deep-link
  const initial = (location.hash || "").replace("#", "");
  if (["active", "pipeline", "returns", "commitments"].includes(initial)) setView(initial);

  // ── Asset-class chip popover ────────────────────────────────
  const popover = $("#class-popover");
  let activeChip = null;

  const closePopover = () => {
    popover.hidden = true;
    if (activeChip) activeChip.classList.remove("popover-open");
    activeChip = null;
  };

  const openPopover = (chip) => {
    activeChip = chip;
    chip.classList.add("popover-open");
    const r = chip.getBoundingClientRect();
    popover.style.top  = (window.scrollY + r.bottom + 6) + "px";
    popover.style.left = (window.scrollX + r.left) + "px";
    popover.hidden = false;
  };

  document.addEventListener("click", (e) => {
    const chip = e.target.closest(".class-chip.editable");
    if (chip) {
      if (chip === activeChip) { closePopover(); return; }
      e.stopPropagation();
      openPopover(chip);
      return;
    }
    const opt = e.target.closest(".class-opt");
    if (opt && activeChip) {
      const newId = opt.dataset.classId;
      const row = activeChip.closest("[data-project-id]");
      if (row && newId) saveAssetClass(row.dataset.projectId, newId, activeChip);
      closePopover();
      return;
    }
    if (!e.target.closest(".class-popover")) closePopover();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePopover(); });

  // ── PATCH the asset class ───────────────────────────────────
  async function saveAssetClass(projectId, classId, chip) {
    const cfg = window.CAPITAL_CONFIG || {};
    const url = `${cfg.patchUrl || "/api/projects"}/${projectId}/asset-class`;

    // Optimistic update — re-skin the chip immediately
    const opt = $(`.class-opt[data-class-id="${classId}"]`);
    const dotColor = opt ? getComputedStyle(opt.querySelector(".dot")).backgroundColor : null;
    const labelText = opt ? opt.textContent.trim() : null;
    if (dotColor && labelText) {
      chip.style.setProperty("--c", dotColor);
      chip.dataset.classId = classId;
      chip.firstChild.nextSibling && (chip.childNodes.forEach(n => { if (n.nodeType === 3) n.textContent = ""; }));
      // Rebuild chip content to reflect new label/color (keep dot + caret)
      chip.innerHTML = `<span class="dot"></span>${labelText}<svg class="caret" width="9" height="9" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 5l3 3 3-3"/></svg>`;
    }

    try {
      const res = await fetch(url, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(cfg.csrfToken ? { "X-CSRFToken": cfg.csrfToken } : {}),
        },
        body: JSON.stringify({ asset_class: classId }),
      });
      if (!res.ok) throw new Error(`PATCH failed: ${res.status}`);
      // The donut, legend percentages, and Returns rail color are all
      // server-rendered, so a soft reload is the cleanest way to pick
      // up the change. The view-toggle hash (#active / #pipeline / etc.)
      // is preserved by `history.replaceState` in setView, so we land
      // back on the same tab.
      window.location.reload();
    } catch (err) {
      console.error("[capital] failed to save asset class", err);
      // TODO: revert chip + show toast
    }
  }

  // ── Excel export hook ────────────────────────────────────────
  // Same Excel that ships with the existing /api/admin/send-reports
  // payload — the endpoint we already use to email out monthly. We
  // pass the active view so the future server can scope it.
  $("#export-excel")?.addEventListener("click", () => {
    const view = $$(".seg-btn.active")[0]?.dataset.view || "active";
    window.location.href = `/api/ember-capital/excel?view=${encodeURIComponent(view)}`;
  });

  // ── "Show in Capital Report" toggles on each pipeline row ───
  document.addEventListener("change", (e) => {
    const cb = e.target.closest(".js-report-toggle");
    if (!cb) return;
    const projectId = cb.dataset.projectId;
    if (!projectId) return;
    fetch(`/api/ember-capital/pipeline-visibility/${encodeURIComponent(projectId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ show: cb.checked }),
    }).catch(err => {
      console.error("[capital] visibility toggle failed", err);
      cb.checked = !cb.checked;  // revert on failure
    });
  });

  // ── Pipeline modal (manual entries) ──────────────────────────
  initPipelineModal();

  // ── Commitments editor ──────────────────────────────────────
  initCommitmentsEditor();

  function initPipelineModal() {
    const modal      = $("#pipeline-modal");
    const form       = $("#pipeline-form");
    const titleEl    = $("#pipeline-modal-title");
    const idInput    = form?.querySelector('input[name="id"]');
    const statusEl   = $("#pipeline-modal-status");
    const cancelBtn  = $("#pipeline-modal-cancel");
    const closeBtn   = $("#pipeline-modal-close");
    const addBtn     = $("#pipeline-add-btn");
    const backdrop   = modal?.querySelector(".cap-modal-backdrop");
    if (!modal || !form || !addBtn) return;

    const open = () => {
      modal.hidden = false;
      // Focus the name field for instant typing
      setTimeout(() => $('#pf-name', form)?.focus(), 0);
    };
    const close = () => {
      modal.hidden = true;
      statusEl.textContent = "";
      statusEl.className = "modal-status";
    };

    const reset = () => {
      form.reset();
      idInput.value = "";
      titleEl.textContent = "Add Pipeline Project";
      // Clear all year inputs explicitly (form.reset doesn't always
      // wipe inputs that started with no `value` attribute).
      $$(".cf-grid input", form).forEach(i => { i.value = ""; });
    };

    const fillForm = (data) => {
      reset();
      titleEl.textContent = "Edit Pipeline Project";
      idInput.value = data.id || "";
      form.querySelector('[name="name"]').value         = data.name        || "";
      form.querySelector('[name="location"]').value     = data.location    || "";
      form.querySelector('[name="asset_class"]').value  = data.asset_class || "mpc-hub";
      form.querySelector('[name="irr"]').value          = data.irr ?? "";
      form.querySelector('[name="em"]').value           = data.em  ?? "";
      const contribs = data.contributions_yearly || [];
      const distribs = data.distributions_yearly || [];
      $$('.cf-grid input[data-kind="contrib"]', form).forEach((inp, i) => {
        inp.value = contribs[i] ? Math.abs(contribs[i]) : "";
      });
      $$('.cf-grid input[data-kind="dist"]', form).forEach((inp, i) => {
        inp.value = distribs[i] || "";
      });
    };

    addBtn.addEventListener("click", () => { reset(); open(); });
    cancelBtn?.addEventListener("click", close);
    closeBtn?.addEventListener("click", close);
    backdrop?.addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.hidden) close();
    });

    // ─── Excel import inside the Add/Edit modal ────────────────
    const excelPick   = $("#pf-excel-pick", form);
    const excelFile   = $("#pf-excel-file", form);
    const excelStatus = $("#pf-excel-status", form);
    const setExcelStatus = (msg, kind) => {
      if (!excelStatus) return;
      excelStatus.textContent = msg || "";
      excelStatus.className = "modal-status" + (kind ? " " + kind : "");
      excelStatus.style.fontSize = "11px";
    };
    excelPick?.addEventListener("click", () => excelFile?.click());
    excelFile?.addEventListener("change", async (e) => {
      const f = e.target.files?.[0];
      if (!f) return;
      setExcelStatus(`Parsing ${f.name}…`);
      const fd = new FormData();
      fd.append("file", f);
      try {
        const res = await fetch("/api/ember-capital/pipeline/parse-excel", {
          method: "POST",
          body: fd,
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
        // Fill IRR + EM
        const irrInp = form.querySelector('[name="irr"]');
        const emInp  = form.querySelector('[name="em"]');
        if (irrInp) irrInp.value = data.irr ?? "";
        if (emInp)  emInp.value  = data.em ?? "";
        // Fill yearly arrays. Inputs are rendered in order matching pipeline_year_range.
        const contribs = data.contributions_yearly || [];
        const distribs = data.distributions_yearly || [];
        $$('.cf-grid input[data-kind="contrib"]', form).forEach((inp, i) => {
          inp.value = contribs[i] ? Math.abs(contribs[i]) : "";
        });
        $$('.cf-grid input[data-kind="dist"]', form).forEach((inp, i) => {
          inp.value = distribs[i] ? Math.abs(distribs[i]) : "";
        });
        const oor = (data.out_of_range_years || []);
        const oorMsg = oor.length ? ` (${oor.length} year${oor.length>1?"s":""} outside the pipeline window were skipped)` : "";
        setExcelStatus(`Imported ${data.irr}% IRR, ${data.em}× EM${oorMsg}.`, "ok");
      } catch (err) {
        console.error("[capital] excel parse failed", err);
        setExcelStatus("Couldn't parse — see console.", "error");
      } finally {
        // Allow re-uploading the same file later (browsers won't re-fire change).
        e.target.value = "";
      }
    });

    // Edit / Delete / Update-from-Excel buttons on each manual pipeline row
    document.addEventListener("click", (e) => {
      const editBtn = e.target.closest(".js-pipeline-edit");
      const delBtn  = e.target.closest(".js-pipeline-del");
      const xlsxBtn = e.target.closest(".js-pipeline-xlsx");
      if (!editBtn && !delBtn && !xlsxBtn) return;
      const row = e.target.closest(".tr.grid-pipeline[data-manual-id]");
      if (!row) return;
      const manualId = row.dataset.manualId;
      if (editBtn) {
        try {
          const data = JSON.parse(row.dataset.form || "{}");
          data.id = manualId;
          fillForm(data);
          open();
        } catch (err) { console.error("[capital] could not parse row data-form", err); }
      } else if (delBtn) {
        if (!confirm("Delete this pipeline project? This can't be undone.")) return;
        fetch(`/api/ember-capital/pipeline/${encodeURIComponent(manualId)}`, { method: "DELETE" })
          .then(r => { if (!r.ok) throw new Error(r.status); window.location.reload(); })
          .catch(err => alert("Delete failed: " + err.message));
      } else if (xlsxBtn) {
        // Update an existing project's IRR / EM / yearly cashflows from a
        // freshly-uploaded Returns tab. Identity fields (name, location,
        // asset class) are preserved from the existing record.
        try {
          const existing = JSON.parse(row.dataset.form || "{}");
          existing.id = manualId;
          const picker = document.createElement("input");
          picker.type = "file";
          picker.accept = ".xlsx,.xls";
          picker.style.display = "none";
          document.body.appendChild(picker);
          picker.addEventListener("change", async (evt) => {
            const f = evt.target.files?.[0];
            if (!f) { picker.remove(); return; }
            const fd = new FormData();
            fd.append("file", f);
            try {
              const res = await fetch("/api/ember-capital/pipeline/parse-excel", {
                method: "POST", body: fd,
              });
              const data = await res.json();
              if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
              const payload = {
                name:                 existing.name        || "",
                location:             existing.location    || "",
                asset_class:          existing.asset_class || "mpc-hub",
                irr:                  data.irr ?? 0,
                em:                   data.em ?? 0,
                contributions_yearly: (data.contributions_yearly || []).map(Math.abs),
                distributions_yearly: (data.distributions_yearly || []).map(Math.abs),
              };
              const putRes = await fetch(
                `/api/ember-capital/pipeline/${encodeURIComponent(manualId)}`,
                { method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(payload) }
              );
              if (!putRes.ok) throw new Error(`Update failed: ${putRes.status}`);
              window.location.reload();
            } catch (err) {
              console.error("[capital] update-from-excel failed", err);
              alert("Update failed: " + err.message);
            } finally {
              picker.remove();
            }
          });
          picker.click();
        } catch (err) {
          console.error("[capital] could not parse row data-form for xlsx update", err);
        }
      }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd       = new FormData(form);
      const id       = (fd.get("id") || "").toString();
      // Collect year-by-year cashflow inputs into ordered arrays.
      const contribs = [], distribs = [];
      $$('.cf-grid input[data-kind="contrib"]', form).forEach(inp => {
        contribs.push(parseFloat(inp.value || 0) || 0);
      });
      $$('.cf-grid input[data-kind="dist"]', form).forEach(inp => {
        distribs.push(parseFloat(inp.value || 0) || 0);
      });
      const payload = {
        name:                 (fd.get("name")        || "").toString().trim(),
        location:             (fd.get("location")    || "").toString().trim(),
        asset_class:          (fd.get("asset_class") || "mpc-hub").toString(),
        irr:                  parseFloat(fd.get("irr") || 0) || 0,
        em:                   parseFloat(fd.get("em")  || 0) || 0,
        contributions_yearly: contribs,
        distributions_yearly: distribs,
      };
      if (!payload.name) {
        statusEl.textContent = "Project name is required";
        statusEl.className = "modal-status error";
        return;
      }

      const url = id
        ? `/api/ember-capital/pipeline/${encodeURIComponent(id)}`
        : `/api/ember-capital/pipeline`;
      const method = id ? "PUT" : "POST";
      statusEl.textContent = "Saving…";
      statusEl.className = "modal-status";
      try {
        const res = await fetch(url, {
          method,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error(`Save failed: ${res.status}`);
        // Soft reload so the donut + pipeline table pick up the change.
        window.location.reload();
      } catch (err) {
        console.error("[capital] pipeline save failed", err);
        statusEl.textContent = "Save failed — see console";
        statusEl.className = "modal-status error";
      }
    });
  }

  function initCommitmentsEditor() {
    const root = $("#commitments-editor");
    if (!root) return;

    const rowsWrap = $(".commit-rows", root);
    const addBtn   = $("#commit-add");
    const saveBtn  = $("#commit-save");
    const status   = $("#commit-status");
    const saveUrl  = root.dataset.saveUrl || "/api/ember-capital/commitments";
    const csrf     = root.dataset.csrf || "";

    let dirty = false;
    const setDirty = (v) => {
      dirty = v;
      status.classList.toggle("dirty", v);
      status.classList.toggle("saved", !v && !!status.dataset.savedAt);
      status.textContent = v ? "Unsaved changes" : (status.dataset.savedAt ? `Saved ${status.dataset.savedAt}` : "");
    };

    const fmtRaw = (v) => {
      if (!v) return "—";
      const a = Math.abs(v);
      if (a >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
      if (a >= 1e3) return `$${Math.round(v / 1e3).toLocaleString()}K`;
      return `$${Math.round(v).toLocaleString()}`;
    };
    const numVal = (input) => parseFloat(input.value) || 0;

    const recompute = () => {
      const totals = { mpc: 0, mpc_allocated: 0, vertical: 0, vertical_allocated: 0 };
      $$(".commit-row", root).forEach(row => {
        const get = (n) => numVal(row.querySelector(`.commit-input[name="${n}"]`));
        const mpc = get("mpc"), mpcA = get("mpc_allocated"),
              vert = get("vertical"), vertA = get("vertical_allocated");
        totals.mpc                += mpc;
        totals.mpc_allocated      += mpcA;
        totals.vertical           += vert;
        totals.vertical_allocated += vertA;
        const rem = (mpc + vert) - (mpcA + vertA);
        row.querySelector(".commit-rem").textContent = rem ? fmtRaw(rem) : "—";
      });
      const available = (totals.mpc + totals.vertical) - (totals.mpc_allocated + totals.vertical_allocated);
      $(`[data-total="mpc"]`,                root).textContent = fmtRaw(totals.mpc);
      $(`[data-total="mpc_allocated"]`,      root).textContent = fmtRaw(totals.mpc_allocated);
      $(`[data-total="vertical"]`,           root).textContent = fmtRaw(totals.vertical);
      $(`[data-total="vertical_allocated"]`, root).textContent = fmtRaw(totals.vertical_allocated);
      $(`[data-total="available"]`,          root).textContent = fmtRaw(available);
    };

    const collect = () => $$(".commit-row", root).map(row => ({
      id:                 row.dataset.groupId || "",
      name:               row.querySelector('.commit-input[name="name"]').value.trim(),
      mpc:                numVal(row.querySelector('.commit-input[name="mpc"]')),
      mpc_allocated:      numVal(row.querySelector('.commit-input[name="mpc_allocated"]')),
      vertical:           numVal(row.querySelector('.commit-input[name="vertical"]')),
      vertical_allocated: numVal(row.querySelector('.commit-input[name="vertical_allocated"]')),
    }));

    rowsWrap.addEventListener("input", (e) => {
      if (e.target.matches(".commit-input")) { recompute(); setDirty(true); }
    });

    rowsWrap.addEventListener("click", (e) => {
      const btn = e.target.closest(".commit-remove");
      if (!btn) return;
      btn.closest(".commit-row").remove();
      recompute(); setDirty(true);
    });

    addBtn?.addEventListener("click", () => {
      const row = document.createElement("div");
      row.className = "commit-grid commit-row";
      row.dataset.groupId = "";
      row.innerHTML = `
        <input type="text"   class="commit-input commit-name" name="name"               placeholder="Investor / Group name" />
        <input type="number" class="commit-input commit-num"  name="mpc"                min="0" step="1000" placeholder="0" />
        <input type="number" class="commit-input commit-num"  name="mpc_allocated"      min="0" step="1000" placeholder="0" />
        <input type="number" class="commit-input commit-num"  name="vertical"           min="0" step="1000" placeholder="0" />
        <input type="number" class="commit-input commit-num"  name="vertical_allocated" min="0" step="1000" placeholder="0" />
        <div class="commit-rem num">—</div>
        <button type="button" class="commit-remove" title="Remove group" aria-label="Remove">×</button>`;
      rowsWrap.appendChild(row);
      row.querySelector('.commit-input[name="name"]').focus();
      setDirty(true);
    });

    saveBtn?.addEventListener("click", async () => {
      const groups = collect();
      saveBtn.disabled = true;
      try {
        const res = await fetch(saveUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(csrf ? { "X-CSRFToken": csrf } : {}),
          },
          body: JSON.stringify({ groups }),
        });
        if (!res.ok) throw new Error(`Save failed: ${res.status}`);
        const t = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
        status.dataset.savedAt = t;
        setDirty(false);
      } catch (err) {
        console.error("[capital] commitments save failed", err);
        status.textContent = "Save failed — see console";
        status.classList.add("dirty");
      } finally {
        saveBtn.disabled = false;
      }
    });

    recompute();
  }
})();
