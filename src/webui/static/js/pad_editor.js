"use strict";

import {
  FIELDS,
  FIELD_LABELS,
  ancestorChain,
  buildNodeIndexes,
  cleanupSelection,
  descendantOverrideSummary,
  l4NodeIdForPad,
  ownOverride,
  ownOverrideSummary,
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
  const TOAST_MS = 5000;

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
    fillPath: null,
    fillPathLoading: false,
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
  const fillPathButton = document.getElementById("pad-calculate-fill-path");

  async function load() {
    try {
      const config = await api("GET", "/api/pasting/pad-config");
      state.config = config;
      clearRoute();
      clearFillPath();
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
    syncNodePadHighlights();
  }

  function renderSelectionCount() {
    countEl.textContent = `選択: ${state.selected.size}`;
  }

  function warningToast(message) {
    const container = document.getElementById("toasts");
    if (!container) {
      toast(message);
      return;
    }
    const el = document.createElement("div");
    el.className = "toast warning";
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => el.remove(), TOAST_MS);
  }

  function overrideFieldsTitle(fields) {
    return fields.map((field) => FIELD_LABELS[field] || field).join("、");
  }

  function ownOverrideTitle(summary) {
    const parts = [];
    if (summary.enabled) parts.push("有効/無効");
    if (summary.fields.length > 0) parts.push(overrideFieldsTitle(summary.fields));
    return `このノードの override: ${parts.join("、")}`;
  }

  function descendantOverrideTitle(summary) {
    const parts = [];
    if (summary.enabledCount > 0) {
      parts.push(`有効/無効 ${summary.enabledCount}件`);
    }
    if (summary.fields.length > 0) {
      parts.push(overrideFieldsTitle(summary.fields));
    }
    return `子孫 ${summary.nodeCount} ノードに override: ${parts.join("、")}`;
  }

  function appendOverrideBadge(parent, label, count, title, testid, scope) {
    if (count === 0) return;
    const badge = document.createElement("span");
    badge.className = "pad-override-badge";
    badge.classList.add(`pad-${scope}-override-badge`);
    if (scope === "descendant") {
      badge.classList.add("pad-descendant-override-marker");
    }
    badge.dataset.testid = testid;
    badge.dataset.scope = scope;
    badge.dataset.count = String(count);
    badge.textContent = count > 1 ? `${label}${count}` : label;
    badge.title = title;
    badge.setAttribute("aria-label", title);
    parent.appendChild(badge);
  }

  function appendNodeOverrideBadges(parent, ownSummary, descendantSummary) {
    if (ownSummary.count === 0 && descendantSummary.count === 0) return;
    const badges = document.createElement("span");
    badges.className = "pad-node-badges";
    appendOverrideBadge(
      badges,
      "*",
      ownSummary.count,
      ownOverrideTitle(ownSummary),
      "pad-own-override-badge",
      "own"
    );
    appendOverrideBadge(
      badges,
      "v",
      descendantSummary.count,
      descendantOverrideTitle(descendantSummary),
      "pad-descendant-override-badge",
      "descendant"
    );
    parent.appendChild(badges);
  }

  function appendDescendantMarker(parent, title, testid, count, field = null) {
    const marker = document.createElement("span");
    marker.className = "pad-descendant-marker";
    marker.classList.add(testid);
    marker.dataset.testid = testid;
    marker.dataset.count = String(count);
    if (field !== null) marker.dataset.field = field;
    marker.textContent = "v";
    marker.title = title;
    marker.setAttribute("aria-label", title);
    parent.appendChild(marker);
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
      clearFillPath();
      showLayer(state.config, state, radio.value);
      renderSelectionCount();
      renderViewer(svg, state.config, state);
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
      updateLocalPadOverrides(ids, enabled);
      applyAffected(res.affected_pads, {
        invalidateRoute: true,
        invalidateFillPath: true,
      });
    } catch (err) {
      toast(`pad 更新失敗: ${err.message}`, false);
    }
  }

  function updateLocalPadOverrides(ids, enabled) {
    const byId = new Map(state.config.pads.map((pad) => [pad.id, pad]));
    for (const id of ids) {
      const pad = byId.get(id);
      if (!pad) continue;
      const node = l4NodeIdForPad(pad, state.nodeById);
      if (node) updateLocalOverride(state.config, { node, enabled });
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
    if (options.invalidateFillPath) clearFillPath();
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
    const own = ownOverride(state.config, node.id);
    const ownSummary = ownOverrideSummary(state.config, node.id);
    const descendantSummary = descendantOverrideSummary(state.config, node);
    if (ownSummary.count > 0) {
      tr.classList.add("pad-row-own-override");
      tr.dataset.ownOverrides = String(ownSummary.count);
    }
    if (descendantSummary.count > 0) {
      tr.classList.add("pad-row-descendant-override");
      tr.dataset.descendantOverrides = String(descendantSummary.count);
    }
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
    appendNodeOverrideBadges(nameTd, ownSummary, descendantSummary);
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
    cb.dataset.testid = "pad-enabled-checkbox";
    cb.checked = enabled;
    cb.indeterminate =
      node.id !== "L0" && (own.enabled === null || own.enabled === undefined);
    cb.addEventListener("change", () => patchNodeEnabled(node.id, cb.checked));
    enTd.appendChild(cb);
    const inheritBtn = document.createElement("button");
    inheritBtn.type = "button";
    inheritBtn.className = "pad-enabled-inherit";
    inheritBtn.dataset.testid = "pad-enabled-inherit";
    inheritBtn.textContent = "継承";
    inheritBtn.title = "有効/無効を継承に戻す";
    inheritBtn.disabled = own.enabled === null || own.enabled === undefined;
    inheritBtn.addEventListener("click", () => patchNodeEnabledInherit(node.id));
    enTd.appendChild(inheritBtn);
    if (descendantSummary.enabledCount > 0) {
      appendDescendantMarker(
        enTd,
        `子孫ノードの有効/無効 override が ${descendantSummary.enabledCount} 件あります。`,
        "pad-descendant-enabled-marker",
        descendantSummary.enabledCount
      );
    }
    tr.appendChild(enTd);

    for (const field of FIELDS) {
      tr.appendChild(buildValueCell(node, field, descendantSummary));
    }
    return tr;
  }

  function buildValueCell(node, field, descendantSummary) {
    const td = document.createElement("td");
    td.className = "pad-col-value";
    const own = ownOverride(state.config, node.id);
    const ownValue = own.values ? own.values[field] : undefined;
    const isOverride = ownValue !== undefined;
    const resolved = resolvedValue(state.config, state.parentOf, node.id, field);
    const descendantCount = descendantSummary.fieldCounts[field] || 0;

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
    input.addEventListener("change", () =>
      commitCell(node.id, field, input, descendantCount)
    );
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        commitCell(node.id, field, input, descendantCount);
      }
    });
    td.appendChild(input);

    if (isOverride) {
      const marker = document.createElement("span");
      marker.className = "pad-override-marker";
      marker.dataset.testid = "pad-own-override-marker";
      marker.dataset.field = field;
      marker.textContent = "●";
      td.insertBefore(marker, input);
      const clearBtn = document.createElement("button");
      clearBtn.type = "button";
      clearBtn.className = "pad-cell-clear";
      clearBtn.dataset.testid = "pad-clear-override";
      clearBtn.dataset.field = field;
      clearBtn.textContent = "×";
      clearBtn.title = "継承に戻す";
      clearBtn.addEventListener("click", () => patchNodeClear(node.id, field));
      td.appendChild(clearBtn);
    }
    if (descendantCount > 0) {
      appendDescendantMarker(
        td,
        `子孫ノードの ${FIELD_LABELS[field] || field} override が ${descendantCount} 件あります。`,
        "pad-descendant-field-marker",
        descendantCount,
        field
      );
    }
    return td;
  }

  function commitCell(nodeId, field, input, descendantCount) {
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
    debouncePatchNode(
      nodeId,
      field,
      { values: { [field]: value } },
      { descendantField: field, descendantCount }
    );
  }

  function debouncePatchNode(nodeId, field, body, options = {}) {
    const key = `${nodeId}|${field}`;
    clearTimeout(state.debounceTimers.get(key));
    state.debounceTimers.set(
      key,
      setTimeout(() => {
        state.debounceTimers.delete(key);
        patchNode({ node: nodeId, ...body }, options);
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

  async function patchNode(body, options = {}) {
    try {
      const res = await api("PATCH", "/api/pasting/pad-config/node", body);
      updateLocalOverride(state.config, body);
      applyAffected(res.affected_pads, {
        invalidateRoute: "enabled" in body,
        invalidateFillPath: true,
      });
      if (options.descendantCount > 0) {
        const label = FIELD_LABELS[options.descendantField] || options.descendantField;
        warningToast(
          `保存しました。子孫ノードの ${label} override ${options.descendantCount} 件は引き続き優先されます。`
        );
      }
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
    const l4 = l4NodeIdForPad(pad, state.nodeById);
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
      btn.disabled = state.locked || state.routeLoading || state.fillPathLoading;
    }
    if (routeButton) {
      routeButton.textContent = state.routeLoading ? "順路計算中" : "順路計算";
    }
    if (fillPathButton) {
      fillPathButton.textContent = state.fillPathLoading
        ? "塗布パス計算中"
        : "塗布パス計算";
    }
  }

  function clearRoute() {
    state.route = null;
  }

  function clearFillPath() {
    state.fillPath = null;
  }

  async function calculateRoute() {
    if (!state.config || state.locked || state.routeLoading) return;
    state.routeLoading = true;
    applyToolbarLock();
    try {
      state.route = await api("POST", "/api/pasting/pad-config/route", {
        layer: state.layer,
      });
      renderViewer(svg, state.config, state);
      syncNodePadHighlights();
    } catch (err) {
      clearRoute();
      renderViewer(svg, state.config, state);
      toast(`順路計算失敗: ${err.message}`, false);
    } finally {
      state.routeLoading = false;
      applyToolbarLock();
    }
  }

  async function calculateFillPath() {
    if (!state.config || state.locked || state.fillPathLoading) return;
    state.fillPathLoading = true;
    applyToolbarLock();
    try {
      state.fillPath = await api("POST", "/api/pasting/pad-config/fill-path", {
        layer: state.layer,
      });
      renderViewer(svg, state.config, state);
      syncNodePadHighlights();
    } catch (err) {
      clearFillPath();
      renderViewer(svg, state.config, state);
      toast(`塗布パス計算失敗: ${err.message}`, false);
    } finally {
      state.fillPathLoading = false;
      applyToolbarLock();
    }
  }

  if (routeButton) {
    routeButton.addEventListener("click", calculateRoute);
  }

  if (fillPathButton) {
    fillPathButton.addEventListener("click", calculateFillPath);
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
        clearFillPath();
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
