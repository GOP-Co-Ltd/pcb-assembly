"use strict";

import {
  FIELD_LABELS,
  ancestorChain,
  buildNodeIndexes,
  cleanupSelection,
  l4NodeIdForPad,
  padsUnderNode,
} from "./model.js";
import { renderTable as renderHierarchyTable } from "./table.js";
import {
  applyPadVisual,
  createSelectionRect,
  idsInRect,
  refreshSelectionVisual,
  renderViewer,
  showLayer,
  svgPoint,
  updateSelectionRect,
} from "./viewer.js";

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
    fillPath: null,
    fillPathLoading: false,
    initialPurgeSaving: false,
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
  const initialPurgeAmount = document.getElementById(
    "pad-initial-purge-amount"
  );
  const initialPurgePadStatus = document.getElementById(
    "pad-initial-purge-pad"
  );
  const initialPurgeSetPadButton = document.getElementById(
    "pad-set-initial-purge-pad"
  );
  const initialPurgeClearPadButton = document.getElementById(
    "pad-clear-initial-purge-pad"
  );
  const alignmentSampleCount = document.getElementById(
    "pad-alignment-sample-count"
  );
  const alignmentSafeCount = document.getElementById(
    "pad-alignment-safe-count"
  );
  const alignmentPreferredCount = document.getElementById(
    "pad-alignment-preferred-count"
  );
  const alignmentLimit = document.getElementById("pad-alignment-limit");

  async function load() {
    try {
      await reloadConfig({ invalidateRoute: true, invalidateFillPath: true });
      emptyEl.hidden = true;
      bodyEl.hidden = false;
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

  // 編集後にサーバから設定を取り直し、index と表示を更新する。
  // tree の resolved/own_override/descendant_summary はサーバ算出なので、
  // ローカル楽観更新ではなく再取得で同期する。
  async function reloadConfig(options = {}) {
    const config = await api("GET", "/api/pasting/pad-config");
    state.config = config;
    if (options.invalidateRoute) clearRoute();
    if (options.invalidateFillPath) clearFillPath();
    buildIndexes(config);
    render();
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
    renderInitialPurgeControls();
    renderAlignmentControls();
    applyToolbarLock();
    syncNodePadHighlights();
  }

  function renderSelectionCount() {
    countEl.textContent = `選択: ${state.selected.size}`;
  }

  function refreshViewerState() {
    refreshSelectionVisual(state.config, state);
    syncNodePadHighlights();
    renderSelectionCount();
    renderInitialPurgeControls({ syncAmount: false });
    applyToolbarLock();
  }

  function renderInitialPurgeControls({ syncAmount = true } = {}) {
    if (!state.config) return;
    const purge = state.config.initial_purge;
    if (syncAmount && initialPurgeAmount) {
      initialPurgeAmount.value =
        purge?.initial_purge_ul !== undefined
          ? String(purge.initial_purge_ul)
          : "";
    }
    if (initialPurgePadStatus) {
      const currentLabel = purge?.pad_id
        ? purge.pad_id
        : purge?.default_pad_id
          ? `自動 (${purge.default_pad_id})`
          : "自動";
      initialPurgePadStatus.textContent = currentLabel;
      initialPurgePadStatus.dataset.padId = purge?.pad_id || "";
      initialPurgePadStatus.dataset.mode = purge?.pad_id ? "explicit" : "auto";
    }
    if (initialPurgeSetPadButton) {
      const pad = selectedInitialPurgePad();
      initialPurgeSetPadButton.textContent = pad
        ? `${pad.id} を設定`
        : "選択パッドを設定";
      initialPurgeSetPadButton.title = pad
        ? `${pad.id} を初回パージパッドに設定`
        : "パッドマップで Top 面のパッドを1つ選択";
    }
    if (initialPurgeClearPadButton) {
      initialPurgeClearPadButton.title = "塗布順路先頭の自動選択に戻す";
    }
  }

  function renderAlignmentControls() {
    if (!state.config?.alignment) return;
    const alignment = state.config.alignment;
    if (alignmentSampleCount) {
      alignmentSampleCount.textContent = String(alignment.sample_count);
    }
    if (alignmentSafeCount) {
      alignmentSafeCount.textContent = String(alignment.safe_pad_count);
    }
    if (alignmentPreferredCount) {
      alignmentPreferredCount.textContent = String(
        alignment.preferred_component_count
      );
    }
    if (alignmentLimit) {
      alignmentLimit.hidden = !alignment.sample_count_limited;
      alignmentLimit.textContent = alignment.sample_count_limited
        ? `実行時の目標は安全候補数 ${alignment.effective_sample_count} 件へ下がります。`
        : "";
    }
  }

  function padById(id) {
    return state.config?.pads.find((pad) => pad.id === id) || null;
  }

  function selectedInitialPurgePad() {
    if (state.selected.size !== 1) return null;
    const pad = padById([...state.selected][0]);
    // Top 面制約の真実はサーバ（PATCH initial-purge が Layer.TOP を検証し 400）。
    // ここはボタン活性の描画ゲートとして同じ規則を写している。
    return pad?.layer === "Top" ? pad : null;
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
      renderInitialPurgeControls({ syncAmount: false });
      applyToolbarLock();
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
      applyPadVisuals(res.affected_pads);
      await reloadConfig({ invalidateRoute: true, invalidateFillPath: true });
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

  // PATCH 応答の affected_pads で pad 色を即時更新する（再取得前のスナップ反応）。
  // tree/表は後続の reloadConfig がサーバ算出値で確定させる。
  function applyPadVisuals(affected) {
    for (const ap of affected) {
      const el = state.padEls.get(ap.id);
      if (el) applyPadVisual(el, state, ap.enabled);
    }
  }

  // 階層表の DOM 生成は table.js に委譲し、状態遷移と API 呼び出しを
  // actions で受け取る（state は共有参照）。
  const tableActions = {
    patchNode,
    patchNodeClear,
    patchNodeEnabled,
    patchNodeEnabledInherit,
    debouncePatchNode,
    focusNodePads,
    hoverNode,
    rerender: renderTable,
  };

  function renderTable() {
    renderHierarchyTable(tableBody, state, tableActions);
  }

  function hoverNode(nodeId) {
    state.hoveredNode = nodeId;
    syncNodePadHighlights();
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
      applyPadVisuals(res.affected_pads);
      await reloadConfig({
        invalidateRoute: "enabled" in body,
        invalidateFillPath: true,
      });
      if (options.descendantCount > 0) {
        const label = FIELD_LABELS[options.descendantField] || options.descendantField;
        toast(
          `保存しました。子孫ノードの ${label} override ${options.descendantCount} 件は引き続き優先されます。`,
          "warning"
        );
      }
    } catch (err) {
      toast(`設定更新失敗: ${err.message}`, false);
    }
  }

  function commitInitialPurgeAmount() {
    if (state.locked || state.initialPurgeSaving || !initialPurgeAmount) return;
    const raw = initialPurgeAmount.value.trim();
    if (raw === "") return;
    const value = Number(raw);
    if (!Number.isFinite(value)) {
      toast("数値を入力してください", false);
      return;
    }
    debounceInitialPurgePatch({ initial_purge_ul: value });
  }

  function debounceInitialPurgePatch(body) {
    const key = "initial-purge";
    clearTimeout(state.debounceTimers.get(key));
    state.debounceTimers.set(
      key,
      setTimeout(() => {
        state.debounceTimers.delete(key);
        patchInitialPurge(body);
      }, DEBOUNCE_MS)
    );
  }

  async function patchInitialPurge(body) {
    if (state.locked || state.initialPurgeSaving) return;
    state.initialPurgeSaving = true;
    applyToolbarLock();
    try {
      await api("PATCH", "/api/pasting/pad-config/initial-purge", body);
      await reloadConfig({});
    } catch (err) {
      toast(`初回パージ設定更新失敗: ${err.message}`, false);
    } finally {
      state.initialPurgeSaving = false;
      applyToolbarLock();
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
    const editingLocked = state.locked || state.initialPurgeSaving;
    if (initialPurgeAmount) initialPurgeAmount.disabled = editingLocked;
    if (initialPurgeSetPadButton) {
      initialPurgeSetPadButton.disabled =
        editingLocked || selectedInitialPurgePad() === null;
    }
    if (initialPurgeClearPadButton) {
      initialPurgeClearPadButton.disabled =
        editingLocked || !state.config?.initial_purge?.pad_id;
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

  if (initialPurgeAmount) {
    initialPurgeAmount.addEventListener("input", commitInitialPurgeAmount);
    initialPurgeAmount.addEventListener("keydown", (event) => {
      if (event.key === "Enter") commitInitialPurgeAmount();
    });
  }

  if (initialPurgeSetPadButton) {
    initialPurgeSetPadButton.addEventListener("click", () => {
      if (state.locked) return;
      const pad = selectedInitialPurgePad();
      if (!pad) return;
      patchInitialPurge({ pad_id: pad.id });
    });
  }

  if (initialPurgeClearPadButton) {
    initialPurgeClearPadButton.addEventListener("click", () => {
      if (state.locked) return;
      patchInitialPurge({ pad_id: null });
    });
  }

  if (window.webui.jobs) {
    window.webui.jobs.onUpdate((job) => {
      const active = window.webui.jobs.isActive(job);
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
