"use strict";

import {
  FIELDS,
  FIELD_LABELS,
  ancestorChain,
  buildNodeIndexes,
  cleanupSelection,
  l4NodeIdForPad,
  ownOverride,
  padsUnderNode,
  resolvedEnabled,
  resolvedValue,
  round4,
  updateLocalOverride,
} from "./pad_editor/model.js";
import {
  applyPadVisual,
  createSelectionRect,
  idsInRect,
  refreshSelectionVisual,
  renderViewer,
  showLayer,
  svgPoint,
  updateSelectionRect,
} from "./pad_editor/viewer.js";

// はんだ塗布タブの pad 編集ビューア + 階層 override 表。
//
// API から返る node_ids を唯一の所属情報として使い、SVG と階層表の
// component / shape / pad preview を同じ規則で同期する。

(() => {
  const root = document.getElementById("pad-editor");
  if (!root) return;

  const { api, toast } = window.webui;
  const DEBOUNCE_MS = 300;

  const state = {
    config: null,
    layer: "Top",
    selected: new Set(),
    expanded: new Set(["L0"]),
    locked: false,
    hoveredNode: null,
    focusedNode: null,
    route: null,
    routeLoading: false,
    padEls: new Map(),
    rowEls: new Map(),
    parentOf: new Map(),
    nodeById: new Map(),
    debounceTimers: new Map(),
  };

  const emptyEl = document.getElementById("pad-editor-empty");
  const bodyEl = document.getElementById("pad-editor-body");
  const svg = document.getElementById("pad-viewer");
  const tableBody = document.getElementById("pad-table-body");
  const countEl = document.getElementById("pad-selection-count");
  const exportButton = document.getElementById("pad-export-config");
  const importButton = document.getElementById("pad-import-config-button");
  const importInput = document.getElementById("pad-import-config");
  const routeButton = document.getElementById("pad-calculate-route");
  const routeStatus = document.getElementById("pad-route-status");

  async function load() {
    try {
      const config = await api("GET", "/api/pasting/pad-config");
      state.config = config;
      clearRoute();
      emptyEl.hidden = true;
      bodyEl.hidden = false;
      buildIndexes(config);
      render();
    } catch (err) {
      state.config = null;
      bodyEl.hidden = true;
      emptyEl.hidden = false;
      emptyEl.textContent =
        err.message && !/^409/.test(err.message)
          ? `pad 設定を取得できません: ${err.message}`
          : "PCB を選択してください。";
    }
  }

  function buildIndexes(config) {
    const indexes = buildNodeIndexes(config.tree);
    state.parentOf = indexes.parentOf;
    state.nodeById = indexes.nodeById;
    cleanupSelection(config, state.selected);
  }

  function render() {
    renderViewer(svg, state.config, state);
    renderTable();
    renderSelectionCount();
    applyToolbarLock();
    renderRouteStatus();
    syncNodePadHighlights();
  }

  function renderSelectionCount() {
    countEl.textContent = `選択: ${state.selected.size}`;
  }

  function refreshViewerState() {
    refreshSelectionVisual(state.config, state);
    syncNodePadHighlights();
    renderSelectionCount();
  }

  for (const radio of root.querySelectorAll("input[name='pad-layer']")) {
    radio.addEventListener("change", () => {
      if (!state.config) return;
      clearRoute();
      showLayer(state.config, state, radio.value);
      renderSelectionCount();
      renderViewer(svg, state.config, state);
      renderRouteStatus();
      syncNodePadHighlights();
    });
  }

  let dragStart = null;
  let dragRect = null;
  let dragModifier = "replace";
  let dragPadId = null;

  svg.addEventListener("pointerdown", (evt) => {
    if (state.locked || evt.button !== 0) return;
    dragModifier = evt.shiftKey ? "add" : evt.altKey ? "remove" : "replace";
    dragStart = svgPoint(svg, evt);
    dragPadId =
      evt.target instanceof SVGPolygonElement && evt.target.dataset.padId
        ? evt.target.dataset.padId
        : null;
    svg.setPointerCapture(evt.pointerId);
  });

  svg.addEventListener("pointermove", (evt) => {
    if (!dragStart) return;
    const now = svgPoint(svg, evt);
    if (!dragRect) {
      if (Math.hypot(now.x - dragStart.x, now.y - dragStart.y) < 0.3) return;
      dragRect = createSelectionRect(svg);
    }
    updateSelectionRect(dragRect, dragStart, now);
  });

  svg.addEventListener("pointerup", (evt) => {
    if (!dragStart) return;
    const start = dragStart;
    const hadRect = dragRect !== null;
    const end = svgPoint(svg, evt);
    const padId = dragPadId;
    dragStart = null;
    dragPadId = null;
    if (dragRect) {
      svg.removeChild(dragRect);
      dragRect = null;
    }
    try {
      svg.releasePointerCapture(evt.pointerId);
    } catch {
      /* already released */
    }
    if (state.locked) return;

    if (!hadRect) {
      selectSinglePad(padId);
      return;
    }
    applyRectSelection(start, end);
  });

  function applyRectSelection(a, b) {
    const hit = idsInRect(state, a, b);
    if (dragModifier === "replace") {
      state.selected = hit;
    } else if (dragModifier === "add") {
      for (const id of hit) state.selected.add(id);
    } else {
      for (const id of hit) state.selected.delete(id);
    }
    refreshViewerState();
  }

  function selectSinglePad(id) {
    state.selected.clear();
    if (id) {
      state.selected.add(id);
      revealPadRow(id);
    }
    refreshViewerState();
  }

  async function patchPads(ids, enabled) {
    if (ids.length === 0) return;
    try {
      const res = await api("PATCH", "/api/pasting/pad-config/pads", {
        ids,
        enabled,
      });
      applyAffected(res.affected_pads, { invalidateRoute: true });
    } catch (err) {
      toast(`pad 更新失敗: ${err.message}`, false);
    }
  }

  function selectedOnLayer() {
    return [...state.selected].filter(
      (id) => state.padEls.get(id)?.dataset.layer === state.layer
    );
  }

  function allOnLayer() {
    return state.config.pads
      .filter((pad) => pad.layer === state.layer)
      .map((pad) => pad.id);
  }

  document
    .getElementById("pad-enable-selected")
    .addEventListener("click", () => patchPads(selectedOnLayer(), true));
  document
    .getElementById("pad-disable-selected")
    .addEventListener("click", () => patchPads(selectedOnLayer(), false));
  document
    .getElementById("pad-enable-all")
    .addEventListener("click", () => patchPads(allOnLayer(), true));
  document
    .getElementById("pad-disable-all")
    .addEventListener("click", () => patchPads(allOnLayer(), false));

  function applyAffected(affected, options = {}) {
    if (options.invalidateRoute) clearRoute();
    const byId = new Map(state.config.pads.map((pad) => [pad.id, pad]));
    for (const ap of affected) {
      const pad = byId.get(ap.id);
      if (!pad) continue;
      pad.enabled = ap.enabled;
      pad.resolved = ap.resolved;
      const el = state.padEls.get(ap.id);
      if (el) applyPadVisual(el, state, ap.enabled);
    }
    renderTable();
    renderViewer(svg, state.config, state);
    renderRouteStatus();
    refreshViewerState();
  }

  function renderTable() {
    tableBody.replaceChildren();
    state.rowEls.clear();
    appendRows(state.config.tree, 0);
    if (state.locked) {
      for (const el of tableBody.querySelectorAll("input, button")) {
        el.disabled = true;
      }
    }
  }

  function appendRows(node, depth) {
    const tr = buildRow(node, depth);
    tableBody.appendChild(tr);
    state.rowEls.set(node.id, tr);
    if (node.children.length > 0 && state.expanded.has(node.id)) {
      for (const child of node.children) appendRows(child, depth + 1);
    }
  }

  function buildRow(node, depth) {
    const tr = document.createElement("tr");
    tr.dataset.nodeId = node.id;
    tr.dataset.testid = "pad-tree-row";
    const enabled = resolvedEnabled(state.config, state.parentOf, node.id);
    if (!enabled) tr.classList.add("pad-row-disabled");
    if (node.id === state.focusedNode) tr.classList.add("pad-row-focus");

    const nameTd = document.createElement("td");
    nameTd.className = "pad-col-node";
    nameTd.style.paddingLeft = `${depth * 1.1 + 0.3}rem`;
    if (node.children.length > 0) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "pad-row-toggle";
      toggle.dataset.testid = "pad-tree-toggle";
      toggle.textContent = state.expanded.has(node.id) ? "▼" : "▶";
      toggle.addEventListener("click", (event) => {
        event.stopPropagation();
        if (state.expanded.has(node.id)) state.expanded.delete(node.id);
        else state.expanded.add(node.id);
        renderTable();
      });
      nameTd.appendChild(toggle);
    }

    const label = document.createElement("span");
    label.className = "pad-node-label";
    label.textContent = node.label;
    nameTd.appendChild(label);
    tr.addEventListener("mouseenter", () => {
      state.hoveredNode = node.id;
      syncNodePadHighlights();
    });
    tr.addEventListener("mouseleave", () => {
      if (state.hoveredNode === node.id) state.hoveredNode = null;
      syncNodePadHighlights();
    });
    tr.addEventListener("click", () => focusNodePads(node.id));
    tr.appendChild(nameTd);

    const enTd = document.createElement("td");
    enTd.className = "pad-col-enabled";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    const own = ownOverride(state.config, node.id);
    cb.checked = enabled;
    cb.indeterminate =
      node.id !== "L0" && (own.enabled === null || own.enabled === undefined);
    cb.addEventListener("change", () => patchNodeEnabled(node.id, cb.checked));
    enTd.appendChild(cb);
    const inheritBtn = document.createElement("button");
    inheritBtn.type = "button";
    inheritBtn.className = "pad-enabled-inherit";
    inheritBtn.textContent = "継承";
    inheritBtn.title = "有効/無効を継承に戻す";
    inheritBtn.disabled = own.enabled === null || own.enabled === undefined;
    inheritBtn.addEventListener("click", () => patchNodeEnabledInherit(node.id));
    enTd.appendChild(inheritBtn);
    tr.appendChild(enTd);

    for (const field of FIELDS) tr.appendChild(buildValueCell(node, field));
    return tr;
  }

  function buildValueCell(node, field) {
    const td = document.createElement("td");
    td.className = "pad-col-value";
    const own = ownOverride(state.config, node.id);
    const ownValue = own.values ? own.values[field] : undefined;
    const isOverride = ownValue !== undefined;
    const resolved = resolvedValue(state.config, state.parentOf, node.id, field);

    const input = document.createElement("input");
    input.type = "number";
    input.step = "any";
    input.className = isOverride ? "pad-cell override" : "pad-cell inherited";
    input.dataset.field = field;
    input.dataset.nodeId = node.id;
    input.dataset.testid = "pad-setting-input";
    input.title = FIELD_LABELS[field] || field;
    if (isOverride) {
      input.value = ownValue;
    } else {
      input.value = "";
      input.placeholder = resolved !== null ? String(round4(resolved)) : "";
    }
    input.addEventListener("change", () => commitCell(node.id, field, input));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") commitCell(node.id, field, input);
    });
    td.appendChild(input);

    if (isOverride) {
      const marker = document.createElement("span");
      marker.className = "pad-override-marker";
      marker.textContent = "●";
      td.insertBefore(marker, input);
      const clearBtn = document.createElement("button");
      clearBtn.type = "button";
      clearBtn.className = "pad-cell-clear";
      clearBtn.textContent = "×";
      clearBtn.title = "継承に戻す";
      clearBtn.addEventListener("click", () => patchNodeClear(node.id, field));
      td.appendChild(clearBtn);
    }
    return td;
  }

  function commitCell(nodeId, field, input) {
    if (state.locked) return;
    const raw = input.value.trim();
    if (raw === "") {
      if (ownOverride(state.config, nodeId).values?.[field] !== undefined) {
        patchNodeClear(nodeId, field);
      }
      return;
    }
    const value = Number(raw);
    if (!Number.isFinite(value)) {
      toast("数値を入力してください", false);
      return;
    }
    if (!resolvedEnabled(state.config, state.parentOf, nodeId)) {
      toast("無効化されているパーツです", false);
    }
    debouncePatchNode(nodeId, field, { values: { [field]: value } });
  }

  function debouncePatchNode(nodeId, field, body) {
    const key = `${nodeId}|${field}`;
    clearTimeout(state.debounceTimers.get(key));
    state.debounceTimers.set(
      key,
      setTimeout(() => {
        state.debounceTimers.delete(key);
        patchNode({ node: nodeId, ...body });
      }, DEBOUNCE_MS)
    );
  }

  function patchNodeClear(nodeId, field) {
    if (state.locked) return;
    patchNode({ node: nodeId, clear: [field] });
  }

  function patchNodeEnabled(nodeId, enabled) {
    if (state.locked) return;
    patchNode({ node: nodeId, enabled });
  }

  function patchNodeEnabledInherit(nodeId) {
    if (state.locked) return;
    patchNode({ node: nodeId, enabled: null });
  }

  async function patchNode(body) {
    try {
      const res = await api("PATCH", "/api/pasting/pad-config/node", body);
      updateLocalOverride(state.config, body);
      applyAffected(res.affected_pads, { invalidateRoute: "enabled" in body });
    } catch (err) {
      toast(`設定更新失敗: ${err.message}`, false);
    }
  }

  function focusNodePads(nodeId) {
    state.focusedNode = nodeId;
    for (const [id, row] of state.rowEls) {
      row.classList.toggle("pad-row-focus", id === nodeId);
    }
    syncNodePadHighlights();
  }

  function syncNodePadHighlights() {
    for (const el of state.padEls.values()) {
      el.classList.remove("pad-node-highlight", "pad-node-focus");
      delete el.dataset.highlighted;
      el.removeAttribute("aria-selected");
    }
    toggleNodeClass(state.hoveredNode, "pad-node-highlight", true);
    toggleNodeClass(state.focusedNode, "pad-node-focus", true);
  }

  function toggleNodeClass(nodeId, className, on) {
    if (!nodeId || !state.config) return;
    for (const pad of padsUnderNode(state.config, nodeId)) {
      const el = state.padEls.get(pad.id);
      if (el && el.dataset.layer === state.layer) {
        el.classList.toggle(className, on);
        if (on) {
          el.dataset.highlighted = "true";
          el.setAttribute("aria-selected", "true");
        }
      }
    }
  }

  function revealPadRow(padId) {
    const pad = state.config.pads.find((candidate) => candidate.id === padId);
    if (!pad) return;
    const l4 = l4NodeIdForPad(pad);
    if (!l4) return;
    for (const id of ancestorChain(state.parentOf, l4)) {
      if (state.nodeById.get(id)?.children.length) state.expanded.add(id);
    }
    renderTable();
    const row = state.rowEls.get(l4);
    if (row) {
      row.scrollIntoView({ block: "nearest" });
      focusNodePads(l4);
    }
  }

  const TERMINAL = new Set(["succeeded", "failed", "aborted", "idle"]);

  function applyToolbarLock() {
    root.classList.toggle("pad-editor-locked", state.locked);
    for (const btn of root.querySelectorAll(".pad-select-tools button")) {
      btn.disabled = state.locked;
    }
    for (const btn of root.querySelectorAll(".pad-route-tools button")) {
      btn.disabled = state.locked || state.routeLoading;
    }
  }

  function clearRoute() {
    state.route = null;
  }

  function renderRouteStatus() {
    if (!routeStatus) return;
    if (state.routeLoading) {
      routeStatus.textContent = "計算中";
      return;
    }
    if (!state.route) {
      routeStatus.textContent = "順路: --";
      return;
    }
    routeStatus.textContent = `順路: ${state.route.pads.length} pads`;
  }

  async function calculateRoute() {
    if (!state.config || state.locked || state.routeLoading) return;
    state.routeLoading = true;
    applyToolbarLock();
    renderRouteStatus();
    try {
      state.route = await api("POST", "/api/pasting/pad-config/route", {
        layer: state.layer,
      });
      renderViewer(svg, state.config, state);
      renderRouteStatus();
      syncNodePadHighlights();
    } catch (err) {
      clearRoute();
      renderViewer(svg, state.config, state);
      renderRouteStatus();
      toast(`順路計算失敗: ${err.message}`, false);
    } finally {
      state.routeLoading = false;
      applyToolbarLock();
      renderRouteStatus();
    }
  }

  if (routeButton) {
    routeButton.addEventListener("click", calculateRoute);
  }

  if (window.webui.jobs) {
    window.webui.jobs.onUpdate((job) => {
      const active = Boolean(job && !TERMINAL.has(job.status));
      if (active === state.locked) return;
      state.locked = active;
      if (!state.config) return;
      renderTable();
      applyToolbarLock();
    });
  }

  if (exportButton) {
    exportButton.addEventListener("click", async () => {
      try {
        const doc = await api("GET", "/api/pasting/pad-config/export");
        const blob = new Blob([JSON.stringify(doc, null, 2)], {
          type: "application/json",
        });
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `pcbasm-paste-overrides-${Date.now()}.json`;
        link.click();
        URL.revokeObjectURL(link.href);
        toast("基板 override 設定を書き出しました");
      } catch (err) {
        toast(`書き出し失敗: ${err.message}`, false);
      }
    });
  }

  if (importButton && importInput) {
    importButton.addEventListener("click", () => importInput.click());
    importInput.addEventListener("change", async () => {
      const file = importInput.files[0];
      if (!file) return;
      try {
        const document = JSON.parse(await file.text());
        const config = await api("POST", "/api/pasting/pad-config/import", {
          document,
        });
        state.config = config;
        clearRoute();
        state.selected.clear();
        buildIndexes(config);
        render();
        toast("基板 override 設定を読み込みました");
      } catch (err) {
        toast(`読み込み失敗: ${err.message}`, false);
      } finally {
        importInput.value = "";
      }
    });
  }

  load();
})();
