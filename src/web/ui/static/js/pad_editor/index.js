"use strict";

import {
  ancestorChain,
  buildNodeIndexes,
  cleanupSelection,
  fieldLabel,
  l4NodeIdForPad,
  padsUnderNode,
} from "./model.js";
import { createPendingSaves } from "./saves.js";
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

  const { api, downloadApi, toast } = window.webui;
  const DEBOUNCE_MS = 300;
  const saves = createPendingSaves((error) => toast(error.message, false));
  // 追加モード中にこの距離まで近づけてクリックしたら、その測定位置を消す [mm]
  const FLOW_CALIBRATION_HIT_MM = 0.5;
  const configUrl = "/api/pasting/pad-config";
  const copperUrl = "/api/pasting/pad-config/copper";

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
    purgePointMode: false,
    flowCalibrationSaving: false,
    flowCalibrationPointMode: false,
    copper: null,
    padEls: new Map(),
    rowEls: new Map(),
    parentOf: new Map(),
    nodeById: new Map(),
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
  const initialPurgeSetPointButton = document.getElementById(
    "pad-set-initial-purge-point"
  );
  const initialPurgeClearButton = document.getElementById(
    "pad-clear-initial-purge-point"
  );
  const flowCalibrationStatus = document.getElementById(
    "pad-flow-calibration-point"
  );
  const flowCalibrationSetButton = document.getElementById(
    "pad-set-flow-calibration-point"
  );
  const flowCalibrationClearButton = document.getElementById(
    "pad-clear-flow-calibration-point"
  );

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
    const config = await api("GET", configUrl);
    state.config = config;
    if (options.invalidateRoute) clearRoute();
    if (options.invalidateFillPath) clearFillPath();
    buildIndexes(config);
    render();
    // 銅箔は基板ごとに 1 回だけ取る（pad 編集では変わらず、点数が多い）
    if (state.copper?.pcb_file !== config.pcb_file) loadCopper(config.pcb_file);
  }

  async function loadCopper(pcbFile) {
    state.copper = null;
    try {
      const copper = await api("GET", copperUrl);
      // 取得中に PCB が切り替わった場合は捨てる
      if (state.config?.pcb_file !== pcbFile) return;
      state.copper = copper;
      render();
    } catch (err) {
      toast(`銅箔を取得できません: ${err.message}`, "warning");
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
    renderInitialPurgeControls();
    renderFlowCalibrationControls();
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
    renderFlowCalibrationControls();
    applyToolbarLock();
  }

  function renderFlowCalibrationControls() {
    if (!state.config) return;
    // 表示文字列はサーバーが組む（selection_label）。ここで連結しない
    const flow = state.config.flow_calibration;
    if (flowCalibrationStatus) {
      flowCalibrationStatus.textContent = flow?.selection_label || "未設定";
      flowCalibrationStatus.title = flow?.error || "";
      flowCalibrationStatus.dataset.mode = flow?.points?.length ? "set" : "unset";
    }
    if (flowCalibrationSetButton) {
      flowCalibrationSetButton.textContent = state.flowCalibrationPointMode
        ? "追加を終了"
        : "測定位置を追加";
      flowCalibrationSetButton.title = state.flowCalibrationPointMode
        ? "基板ビューをクリックするたび測定位置が 1 つ増えます（既存の点をクリックすると消えます）"
        : "基板上の任意位置を測定位置として 1 点ずつ追加";
      flowCalibrationSetButton.dataset.mode = state.flowCalibrationPointMode
        ? "picking"
        : "idle";
    }
    if (flowCalibrationClearButton) {
      flowCalibrationClearButton.title =
        "測定位置を全部消して補正しない状態へ戻す";
    }
  }

  function renderInitialPurgeControls({ syncAmount = true } = {}) {
    if (!state.config) return;
    const purge = state.config.initial_purge;
    if (syncAmount && initialPurgeAmount) {
      initialPurgeAmount.value =
        purge?.initial_purge_ul !== undefined
          ? String(purge.initial_purge_ul)
          : "";
      initialPurgeAmount.defaultValue = initialPurgeAmount.value;
    }
    if (initialPurgePadStatus) {
      initialPurgePadStatus.textContent = purge?.selection_label || "自動";
      initialPurgePadStatus.title = purge?.error || "";
      initialPurgePadStatus.dataset.mode = purge?.point ? "explicit" : "auto";
    }
    if (initialPurgeSetPointButton) {
      initialPurgeSetPointButton.textContent = state.purgePointMode
        ? "位置をクリック（取消）"
        : "パージ位置を設定";
      initialPurgeSetPointButton.title = state.purgePointMode
        ? "基板ビューの任意位置をクリックするとそこがパージ位置になります"
        : "基板上の任意位置をパージ位置に設定";
      initialPurgeSetPointButton.dataset.mode = state.purgePointMode
        ? "picking"
        : "idle";
    }
    if (initialPurgeClearButton) {
      initialPurgeClearButton.title = "塗布順路先頭の中心へ戻す";
    }
  }

  function setPurgePointMode(active) {
    state.purgePointMode = active;
    if (active) state.flowCalibrationPointMode = false;
    svg.classList.toggle("pad-viewer-picking", active || state.flowCalibrationPointMode);
    renderInitialPurgeControls({ syncAmount: false });
    renderFlowCalibrationControls();
  }

  function setFlowCalibrationPointMode(active) {
    state.flowCalibrationPointMode = active;
    if (active) state.purgePointMode = false;
    svg.classList.toggle("pad-viewer-picking", active || state.purgePointMode);
    renderInitialPurgeControls({ syncAmount: false });
    renderFlowCalibrationControls();
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
    if (state.purgePointMode) {
      const point = svgPoint(svg, evt);
      setPurgePointMode(false);
      patchInitialPurge({ point: [point.x, point.y] });
      return;
    }
    if (state.flowCalibrationPointMode) {
      // 1 点ずつ増やす。続けて置けるようモードは抜けない
      toggleFlowCalibrationPointAt(svgPoint(svg, evt));
      return;
    }
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

  // 編集開始時の PCB をそのまま添えて送る。切替済みなら サーバが 409 を返す。
  function withExpectedPcb(body) {
    return { expected_pcb: state.config?.pcb_file ?? null, ...body };
  }

  function patchPads(ids, enabled) {
    if (ids.length === 0) return;
    const request = withExpectedPcb({ ids, enabled });
    return saves.run("pad 更新失敗", async () => {
      const res = await api(
        "PATCH",
        "/api/pasting/pad-config/pads",
        request
      );
      applyPadVisuals(res.affected_pads);
      await reloadConfig({ invalidateRoute: true, invalidateFillPath: true });
    });
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
    const request = withExpectedPcb({ node: nodeId, ...body });
    saves.schedule(`${nodeId}|${field}`, () => patchNode(request, options), DEBOUNCE_MS);
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

  let nodeWrites = Promise.resolve();

  function patchNode(body, options = {}) {
    // 明示的な保存・継承への復帰は、同じ欄の未送信の入力を置き換える。
    for (const field of [...Object.keys(body.values || {}), ...(body.clear || [])]) {
      const key = `${body.node}|${field}`;
      saves.cancel(key);
    }
    const request = withExpectedPcb(body);
    // 先に入力した値が、後から選んだ継承や別の値を追い越して保存されないようにする。
    const previous = nodeWrites;
    nodeWrites = saves.run("設定更新失敗", async () => {
      await previous;
      await saveNode(request, options);
    });
    return nodeWrites;
  }

  async function saveNode(body, options) {
    const res = await api(
      "PATCH",
      "/api/pasting/pad-config/node",
      body
    );
    applyPadVisuals(res.affected_pads);
    await reloadConfig({
      invalidateRoute: "enabled" in body,
      invalidateFillPath: true,
    });
    if (options.descendantCount > 0) {
      const label = fieldLabel(state.config, options.descendantField);
      toast(
        `保存しました。子孫ノードの ${label} override ${options.descendantCount} 件は引き続き優先されます。`,
        "warning"
      );
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
    const request = withExpectedPcb(body);
    saves.schedule("initial-purge", () => patchInitialPurge(request), DEBOUNCE_MS);
  }

  function patchInitialPurge(body) {
    if (state.locked || state.initialPurgeSaving) return;
    state.initialPurgeSaving = true;
    applyToolbarLock();
    const request = withExpectedPcb(body);
    return saves.run("初回パージ設定更新失敗", async () => {
      try {
        await api(
          "PATCH",
          "/api/pasting/pad-config/initial-purge",
          request
        );
        await reloadConfig({});
      } finally {
        state.initialPurgeSaving = false;
        applyToolbarLock();
      }
    });
  }

  // クリック位置が既存の測定位置なら消し、そうでなければ末尾へ足す
  function toggleFlowCalibrationPointAt(point) {
    const points = state.config?.flow_calibration?.points || [];
    const hit = points.findIndex(
      (candidate) =>
        Math.hypot(candidate[0] - point.x, candidate[1] - point.y) <
        FLOW_CALIBRATION_HIT_MM
    );
    const next =
      hit >= 0
        ? points.filter((_, index) => index !== hit)
        : [...points, [point.x, point.y]];
    patchFlowCalibration({ points: next });
  }

  function patchFlowCalibration(body) {
    if (state.locked || state.flowCalibrationSaving) return;
    state.flowCalibrationSaving = true;
    applyToolbarLock();
    const request = withExpectedPcb(body);
    return saves.run("流量キャリブレーション位置の更新失敗", async () => {
      try {
        await api(
          "PATCH",
          "/api/pasting/pad-config/flow-calibration",
          request
        );
        await reloadConfig({});
      } finally {
        state.flowCalibrationSaving = false;
        applyToolbarLock();
      }
    });
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
    if (initialPurgeSetPointButton) {
      initialPurgeSetPointButton.disabled = editingLocked;
    }
    if (initialPurgeClearButton) {
      initialPurgeClearButton.disabled =
        editingLocked || !state.config?.initial_purge?.point;
    }
    const flowLocked = state.locked || state.flowCalibrationSaving;
    if (flowCalibrationSetButton) flowCalibrationSetButton.disabled = flowLocked;
    if (flowCalibrationClearButton) {
      flowCalibrationClearButton.disabled =
        flowLocked || !state.config?.flow_calibration?.points?.length;
    }
  }

  let routeRevision = 0;
  let fillPathRevision = 0;

  function clearRoute() {
    routeRevision += 1;
    state.route = null;
  }

  function clearFillPath() {
    fillPathRevision += 1;
    state.fillPath = null;
  }

  async function calculateRoute() {
    if (!state.config || state.locked || state.routeLoading) return;
    const revision = routeRevision;
    state.routeLoading = true;
    applyToolbarLock();
    try {
      const route = await api("POST", "/api/pasting/pad-config/route", {
        layer: state.layer,
      });
      if (revision !== routeRevision) return;
      state.route = route;
      renderViewer(svg, state.config, state);
      syncNodePadHighlights();
    } catch (err) {
      if (revision !== routeRevision) return;
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
    const revision = fillPathRevision;
    state.fillPathLoading = true;
    applyToolbarLock();
    try {
      const fillPath = await api("POST", "/api/pasting/pad-config/fill-path", {
        layer: state.layer,
      });
      if (revision !== fillPathRevision) return;
      state.fillPath = fillPath;
      renderViewer(svg, state.config, state);
      syncNodePadHighlights();
    } catch (err) {
      if (revision !== fillPathRevision) return;
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

  if (initialPurgeSetPointButton) {
    initialPurgeSetPointButton.addEventListener("click", () => {
      if (state.locked) return;
      setPurgePointMode(!state.purgePointMode);
    });
  }

  if (initialPurgeClearButton) {
    initialPurgeClearButton.addEventListener("click", () => {
      if (state.locked) return;
      setPurgePointMode(false);
      patchInitialPurge({ point: null });
    });
  }

  if (flowCalibrationSetButton) {
    flowCalibrationSetButton.addEventListener("click", () => {
      if (state.locked) return;
      setFlowCalibrationPointMode(!state.flowCalibrationPointMode);
    });
  }

  if (flowCalibrationClearButton) {
    flowCalibrationClearButton.addEventListener("click", () => {
      if (state.locked) return;
      setFlowCalibrationPointMode(false);
      patchFlowCalibration({ points: [] });
    });
  }

  if (window.webui.jobs) {
    window.webui.jobs.beforeStart(async () => {
      if (!state.config) return;
      for (const input of root.querySelectorAll("input")) {
        if (!input.reportValidity()) throw new Error("入力内容を確認してください。");
      }
      // 以前の保存が拒否された欄も、表示している値で改めて保存・検証する。
      for (const input of tableBody.querySelectorAll("input[type='number']")) {
        if (!input.disabled && !input.hidden && input.value !== input.defaultValue) {
          input.dispatchEvent(new Event("change"));
        }
      }
      if (initialPurgeAmount?.value !== initialPurgeAmount?.defaultValue) {
        commitInitialPurgeAmount();
      }
      const failure = await saves.flush();
      if (failure) throw new Error(`保存できなかったため開始しませんでした。${failure.message}`);
    });
    window.webui.jobs.onUpdate((job) => {
      const active = window.webui.jobs.isActive(job);
      if (active === state.locked) return;
      state.locked = active;
      if (active) {
        setPurgePointMode(false);
        setFlowCalibrationPointMode(false);
      }
      if (!state.config) return;
      renderTable();
      applyToolbarLock();
    });
  }

  if (exportButton) {
    exportButton.addEventListener("click", async () => {
      try {
        // 保存名はサーバーが Content-Disposition で決める（基板名 + 日時）
        const filename = await downloadApi(
          "GET",
          "/api/pasting/pad-config/export",
        );
        toast(`基板 override 設定を書き出しました: ${filename}`);
      } catch (err) {
        toast(`書き出し失敗: ${err.message}`, false);
      }
    });
  }

  if (importButton && importInput) {
    importButton.addEventListener("click", () => importInput.click());
    importInput.addEventListener("change", () => {
      const file = importInput.files[0];
      if (!file) return;
      saves.run("読み込み失敗", async () => {
        try {
          const document = JSON.parse(await file.text());
          await api("POST", "/api/pasting/pad-config/import", {
            document,
          });
          clearRoute();
          clearFillPath();
          state.selected.clear();
          await reloadConfig({});
          toast("基板 override 設定を読み込みました");
        } finally {
          importInput.value = "";
        }
      });
    });
  }

  load();
})();
