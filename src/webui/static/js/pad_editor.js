"use strict";

// はんだ塗布タブの pad 編集ビューア + 階層 override 表。
//
// GET /api/pasting/pad-config で取得した pad ジオメトリ・階層ツリー・解決済み
// 設定・疎 override を、SVG ビューアと階層表に描く。pad のクリック/ドラッグ選択で
// 有効/無効を即時 PATCH し、表のセル編集で override を即時 PATCH する。すべての
// 編集は応答 affected_pads で SVG/表を部分更新する。ジョブ実行中は編集をロックする。
//
// 座標系: pad.polygon / outline は mm 系の exterior 座標。SVG viewBox を mm 系に
// 一致させ無変換で描画する（正規化系が SVG と同方向のため Y 反転は不要）。

(() => {
  const root = document.getElementById("pad-editor");
  if (!root) return;

  const { api, toast } = window.webui;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const MARGIN_MM = 2; // viewBox の余白（mm）
  const DEBOUNCE_MS = 300;

  // override 対象 7 項目（API 契約と一致。表示順 = 表の列順）
  const FIELDS = [
    "ul_per_mm2",
    "paste_height",
    "fill_speed",
    "prime_extra_delay",
    "bead_width_factor",
    "overlap",
    "boundary_margin",
  ];

  // ---- 状態（単一オブジェクトに集約）----
  const state = {
    config: null, // 直近の GET レスポンス
    layer: "Top", // 現在表示レイヤ
    selected: new Set(), // 選択中 pad id
    expanded: new Set(), // 展開中ノード id
    locked: false, // ジョブ実行中の編集ロック
    padEls: new Map(), // pad id -> <polygon>
    rowEls: new Map(), // node id -> <tr>
    parentOf: new Map(), // node id -> 親 node id
    nodeById: new Map(), // node id -> tree ノード
    debounceTimers: new Map(), // "node|field" -> timer
  };

  // ---- DOM 参照 ----
  const emptyEl = document.getElementById("pad-editor-empty");
  const bodyEl = document.getElementById("pad-editor-body");
  const svg = document.getElementById("pad-viewer");
  const tableBody = document.getElementById("pad-table-body");
  const countEl = document.getElementById("pad-selection-count");

  // --------------------------------------------------------------------- //
  // 初期ロード
  // --------------------------------------------------------------------- //
  async function load() {
    try {
      const config = await api("GET", "/api/pasting/pad-config");
      state.config = config;
      emptyEl.hidden = true;
      bodyEl.hidden = false;
      buildIndexes(config);
      render();
    } catch (err) {
      // 409 = PCB 未選択。それ以外もメッセージを表示し編集 UI を隠す。
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
    state.parentOf.clear();
    state.nodeById.clear();
    const walk = (node, parentId) => {
      state.nodeById.set(node.id, node);
      if (parentId !== null) state.parentOf.set(node.id, parentId);
      for (const child of node.children) walk(child, node.id);
    };
    walk(config.tree, null);
    // 選択集合から消えた pad（レイヤ切替・再取得）を掃除する
    const ids = new Set(config.pads.map((p) => p.id));
    for (const id of [...state.selected]) if (!ids.has(id)) state.selected.delete(id);
  }

  // --------------------------------------------------------------------- //
  // SVG ビューア
  // --------------------------------------------------------------------- //
  function render() {
    renderViewer();
    renderTable();
    renderSelectionCount();
    applyToolbarLock();
  }

  function renderViewer() {
    const config = state.config;
    svg.replaceChildren();
    state.padEls.clear();

    const outline = config.outline;
    const xs = outline.map((p) => p[0]);
    const ys = outline.map((p) => p[1]);
    const minX = Math.min(...xs);
    const minY = Math.min(...ys);
    const w = Math.max(...xs) - minX;
    const h = Math.max(...ys) - minY;
    svg.setAttribute(
      "viewBox",
      `${minX - MARGIN_MM} ${minY - MARGIN_MM} ${w + 2 * MARGIN_MM} ${h + 2 * MARGIN_MM}`
    );

    // outline（基板外形）
    const outlineEl = document.createElementNS(SVG_NS, "polygon");
    outlineEl.setAttribute("points", pointsAttr(outline));
    outlineEl.setAttribute("class", "pad-outline");
    outlineEl.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(outlineEl);

    // pad
    for (const pad of config.pads) {
      const el = document.createElementNS(SVG_NS, "polygon");
      el.setAttribute("points", pointsAttr(pad.polygon));
      el.setAttribute("vector-effect", "non-scaling-stroke");
      el.dataset.padId = pad.id;
      el.dataset.designator = pad.designator;
      el.dataset.package = pad.package;
      el.dataset.padNumber = pad.pad_number;
      el.dataset.layer = pad.layer;
      applyPadVisual(el, pad.enabled);
      el.style.display = pad.layer === state.layer ? "" : "none";
      state.padEls.set(pad.id, el);
      svg.appendChild(el);
    }
  }

  function pointsAttr(coords) {
    return coords.map((p) => `${p[0]},${p[1]}`).join(" ");
  }

  function applyPadVisual(el, enabled) {
    const selected = state.selected.has(el.dataset.padId);
    el.setAttribute(
      "class",
      `pad${enabled ? " pad-enabled" : " pad-disabled"}${selected ? " pad-selected" : ""}`
    );
  }

  // --------------------------------------------------------------------- //
  // pad の状態更新（PATCH 応答の affected_pads を反映）
  // --------------------------------------------------------------------- //
  function applyAffected(affected) {
    const byId = new Map(state.config.pads.map((p) => [p.id, p]));
    for (const ap of affected) {
      const pad = byId.get(ap.id);
      if (!pad) continue;
      pad.enabled = ap.enabled;
      pad.resolved = ap.resolved;
      const el = state.padEls.get(ap.id);
      if (el) applyPadVisual(el, ap.enabled);
    }
    // 表の解決値（継承 placeholder）も再描画する
    renderTable();
  }

  // --------------------------------------------------------------------- //
  // レイヤ切替
  // --------------------------------------------------------------------- //
  for (const radio of root.querySelectorAll("input[name='pad-layer']")) {
    radio.addEventListener("change", () => {
      state.layer = radio.value;
      for (const [id, el] of state.padEls) {
        el.style.display = el.dataset.layer === state.layer ? "" : "none";
        if (el.dataset.layer !== state.layer) state.selected.delete(id);
      }
      renderViewer();
      renderSelectionCount();
    });
  }

  // --------------------------------------------------------------------- //
  // 選択数表示
  // --------------------------------------------------------------------- //
  function renderSelectionCount() {
    countEl.textContent = `選択: ${state.selected.size}`;
  }

  function refreshSelectionVisual() {
    for (const [id, el] of state.padEls) {
      const pad = state.config.pads.find((p) => p.id === id);
      if (pad) applyPadVisual(el, pad.enabled);
    }
    renderSelectionCount();
  }

  // --------------------------------------------------------------------- //
  // pad クリック（単 pad 即トグル）+ 矩形ドラッグ選択
  // --------------------------------------------------------------------- //
  let dragStart = null; // {x, y} (mm 系)
  let dragRect = null; // 選択矩形 <rect>
  let dragModifier = "replace";

  function svgPoint(evt) {
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    const ctm = svg.getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    const local = pt.matrixTransform(ctm.inverse());
    return { x: local.x, y: local.y };
  }

  svg.addEventListener("pointerdown", (evt) => {
    if (state.locked || evt.button !== 0) return;
    dragModifier = evt.shiftKey ? "add" : evt.altKey ? "remove" : "replace";
    dragStart = svgPoint(evt);
    svg.setPointerCapture(evt.pointerId);
  });

  svg.addEventListener("pointermove", (evt) => {
    if (!dragStart) return;
    const now = svgPoint(evt);
    if (!dragRect) {
      // 微小移動はクリック扱い。閾値を超えたら矩形を出す。
      if (Math.hypot(now.x - dragStart.x, now.y - dragStart.y) < 0.3) return;
      dragRect = document.createElementNS(SVG_NS, "rect");
      dragRect.setAttribute("class", "pad-select-rect");
      dragRect.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(dragRect);
    }
    const x = Math.min(dragStart.x, now.x);
    const y = Math.min(dragStart.y, now.y);
    dragRect.setAttribute("x", x);
    dragRect.setAttribute("y", y);
    dragRect.setAttribute("width", Math.abs(now.x - dragStart.x));
    dragRect.setAttribute("height", Math.abs(now.y - dragStart.y));
  });

  svg.addEventListener("pointerup", (evt) => {
    if (!dragStart) return;
    const start = dragStart;
    const hadRect = dragRect !== null;
    const end = svgPoint(evt);
    dragStart = null;
    if (dragRect) {
      svg.removeChild(dragRect);
      dragRect = null;
    }
    try {
      svg.releasePointerCapture(evt.pointerId);
    } catch {
      /* 既に解放済み */
    }
    if (state.locked) return;

    if (!hadRect) {
      // クリック = 単 pad 即トグル
      const target = evt.target;
      if (target instanceof SVGPolygonElement && target.dataset.padId) {
        togglePad(target.dataset.padId);
      }
      return;
    }
    applyRectSelection(start, end);
  });

  function applyRectSelection(a, b) {
    const minX = Math.min(a.x, b.x);
    const maxX = Math.max(a.x, b.x);
    const minY = Math.min(a.y, b.y);
    const maxY = Math.max(a.y, b.y);
    const hit = new Set();
    for (const [id, el] of state.padEls) {
      if (el.dataset.layer !== state.layer) continue;
      const box = el.getBBox();
      const intersects =
        box.x <= maxX &&
        box.x + box.width >= minX &&
        box.y <= maxY &&
        box.y + box.height >= minY;
      if (intersects) hit.add(id);
    }
    if (dragModifier === "replace") {
      state.selected = hit;
    } else if (dragModifier === "add") {
      for (const id of hit) state.selected.add(id);
    } else {
      for (const id of hit) state.selected.delete(id);
    }
    refreshSelectionVisual();
  }

  async function togglePad(id) {
    const pad = state.config.pads.find((p) => p.id === id);
    if (!pad) return;
    await patchPads([id], !pad.enabled);
    revealPadRow(id);
  }

  async function patchPads(ids, enabled) {
    if (ids.length === 0) return;
    try {
      const res = await api("PATCH", "/api/pasting/pad-config/pads", { ids, enabled });
      applyAffected(res.affected_pads);
    } catch (err) {
      toast(`pad 更新失敗: ${err.message}`, false);
    }
  }

  // ---- 選択ツールバー ----
  function selectedOnLayer() {
    return [...state.selected].filter(
      (id) => state.padEls.get(id)?.dataset.layer === state.layer
    );
  }

  function allOnLayer() {
    return state.config.pads
      .filter((p) => p.layer === state.layer)
      .map((p) => p.id);
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

  // --------------------------------------------------------------------- //
  // 階層 override 表
  // --------------------------------------------------------------------- //

  // ノードの祖先チェーン（L0 → ... → node）を返す
  function ancestorChain(nodeId) {
    const chain = [nodeId];
    let cur = nodeId;
    while (state.parentOf.has(cur)) {
      cur = state.parentOf.get(cur);
      chain.unshift(cur);
    }
    return chain;
  }

  // ノードの解決 enabled（最具体の明示値が勝つ。明示なしは L0=true 起点で継承）
  function resolvedEnabled(nodeId) {
    let enabled = true;
    for (const id of ancestorChain(nodeId)) {
      const ov = state.config.overrides[id];
      if (ov && ov.enabled !== null && ov.enabled !== undefined) enabled = ov.enabled;
    }
    return enabled;
  }

  // ノードでの field 解決値（祖先チェーンの override を上書き合成）
  function resolvedValue(nodeId, field) {
    let value = null;
    for (const id of ancestorChain(nodeId)) {
      const ov = state.config.overrides[id];
      if (ov && ov.values && ov.values[field] !== undefined) value = ov.values[field];
    }
    return value;
  }

  function ownOverride(nodeId) {
    return state.config.overrides[nodeId] || { enabled: null, values: {} };
  }

  function renderTable() {
    tableBody.replaceChildren();
    state.rowEls.clear();
    appendRows(state.config.tree, 0);
    if (state.locked) {
      for (const el of tableBody.querySelectorAll("input, button")) el.disabled = true;
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
    const enabled = resolvedEnabled(node.id);
    if (!enabled) tr.classList.add("pad-row-disabled");

    // --- ノード名セル（展開トグル + インデント）---
    const nameTd = document.createElement("td");
    nameTd.className = "pad-col-node";
    nameTd.style.paddingLeft = `${depth * 1.1 + 0.3}rem`;
    if (node.children.length > 0) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "pad-row-toggle";
      toggle.textContent = state.expanded.has(node.id) ? "▼" : "▶";
      toggle.addEventListener("click", (e) => {
        e.stopPropagation();
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
    nameTd.addEventListener("mouseenter", () => highlightNodePads(node.id, true));
    nameTd.addEventListener("mouseleave", () => highlightNodePads(node.id, false));
    nameTd.addEventListener("click", () => focusNodePads(node.id));
    tr.appendChild(nameTd);

    // --- enabled チェックボックス ---
    const enTd = document.createElement("td");
    enTd.className = "pad-col-enabled";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    const own = ownOverride(node.id);
    cb.checked = enabled;
    // 明示設定が無い（継承）の行は indeterminate で「継承」を示す（L0 を除く）
    cb.indeterminate = node.id !== "L0" && (own.enabled === null || own.enabled === undefined);
    cb.addEventListener("change", () => patchNodeEnabled(node.id, cb.checked));
    enTd.appendChild(cb);
    if (node.id !== "L0") {
      // 継承に戻すボタン（明示設定がある時のみ有効）
      const inheritBtn = document.createElement("button");
      inheritBtn.type = "button";
      inheritBtn.className = "pad-enabled-inherit";
      inheritBtn.textContent = "継承";
      inheritBtn.title = "有効/無効を継承に戻す";
      inheritBtn.disabled = own.enabled === null || own.enabled === undefined;
      inheritBtn.addEventListener("click", () => patchNodeEnabledInherit(node.id));
      enTd.appendChild(inheritBtn);
    }
    tr.appendChild(enTd);

    // --- 7 項目セル ---
    for (const field of FIELDS) tr.appendChild(buildValueCell(node, field));
    return tr;
  }

  function buildValueCell(node, field) {
    const td = document.createElement("td");
    td.className = "pad-col-value";
    const own = ownOverride(node.id);
    const ownValue = own.values ? own.values[field] : undefined;
    const isOverride = ownValue !== undefined;
    const resolved = resolvedValue(node.id, field);

    const input = document.createElement("input");
    input.type = "number";
    input.step = "any";
    input.className = isOverride ? "pad-cell override" : "pad-cell inherited";
    if (isOverride) {
      input.value = ownValue;
    } else {
      input.value = "";
      input.placeholder = resolved !== null ? String(round4(resolved)) : "";
    }
    input.addEventListener("change", () => commitCell(node.id, field, input));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") commitCell(node.id, field, input);
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

  function round4(v) {
    return Math.round(v * 1e4) / 1e4;
  }

  function warnIfDisabled(nodeId) {
    if (!resolvedEnabled(nodeId)) {
      toast("無効化されているパーツです", false);
    }
  }

  function commitCell(nodeId, field, input) {
    if (state.locked) return;
    const raw = input.value.trim();
    if (raw === "") {
      // 空入力 = clear（継承に戻す）。元から継承なら何もしない。
      if (ownOverride(nodeId).values?.[field] !== undefined) {
        patchNodeClear(nodeId, field);
      }
      return;
    }
    const value = Number(raw);
    if (!Number.isFinite(value)) {
      toast("数値を入力してください", false);
      return;
    }
    warnIfDisabled(nodeId);
    debouncePatchNode(nodeId, field, { values: { [field]: value } });
  }

  // 連続入力のデバウンス（preview.js 踏襲）。node|field 単位で 1 本に集約。
  function debouncePatchNode(nodeId, field, body) {
    const dkey = `${nodeId}|${field}`;
    clearTimeout(state.debounceTimers.get(dkey));
    state.debounceTimers.set(
      dkey,
      setTimeout(() => {
        state.debounceTimers.delete(dkey);
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
      // override マップを楽観更新してから affected を反映する
      updateLocalOverride(body);
      applyAffected(res.affected_pads);
      refreshSelectionVisual();
    } catch (err) {
      toast(`設定更新失敗: ${err.message}`, false);
    }
  }

  // PATCH 成功時にローカル overrides を更新（再 GET せず表を即時整合させる）
  function updateLocalOverride(body) {
    const id = body.node;
    const ov = state.config.overrides[id]
      ? {
          enabled: state.config.overrides[id].enabled,
          values: { ...state.config.overrides[id].values },
        }
      : { enabled: null, values: {} };
    if ("enabled" in body) ov.enabled = body.enabled;
    if (body.values) for (const [k, v] of Object.entries(body.values)) ov.values[k] = v;
    if (body.clear) for (const k of body.clear) delete ov.values[k];
    const empty =
      (ov.enabled === null || ov.enabled === undefined) &&
      Object.keys(ov.values).length === 0;
    if (empty && id !== "L0") delete state.config.overrides[id];
    else state.config.overrides[id] = ov;
  }

  // --------------------------------------------------------------------- //
  // ビューア ⇄ 表 連動
  // --------------------------------------------------------------------- //
  function padsUnderNode(nodeId) {
    // node_id 構造から配下 pad を属性で特定する（GET の pads は全 pad）。
    const parts = nodeId.split(":");
    return state.config.pads.filter((pad) => {
      if (parts[0] === "L0") return true;
      if (parts[0] === "L1") return pad.package === parts[1];
      if (parts[0] === "L2") return pad.designator === parts[1];
      if (parts[0] === "L3") return pad.designator === parts[1];
      if (parts[0] === "L4")
        return pad.designator === parts[1] && pad.pad_number === parts[2];
      return false;
    });
  }

  function highlightNodePads(nodeId, on) {
    for (const pad of padsUnderNode(nodeId)) {
      const el = state.padEls.get(pad.id);
      if (el) el.classList.toggle("pad-node-highlight", on);
    }
  }

  function focusNodePads(nodeId) {
    const el = state.rowEls.get(nodeId);
    if (el) el.classList.add("pad-row-focus");
    setTimeout(() => el && el.classList.remove("pad-row-focus"), 1200);
  }

  // SVG で pad を選択（クリック/ドラッグ）したとき、対応 L4 行へスクロール。
  // L4 行は祖先が展開されていないと存在しないため、祖先を展開してから探す。
  function revealPadRow(padId) {
    const pad = state.config.pads.find((p) => p.id === padId);
    if (!pad) return;
    const l4 = `L4:${pad.designator}:${pad.pad_number}`;
    for (const id of ancestorChain(l4)) {
      if (state.nodeById.get(id)?.children.length) state.expanded.add(id);
    }
    renderTable();
    const row = state.rowEls.get(l4);
    if (row) {
      row.scrollIntoView({ block: "nearest" });
      row.classList.add("pad-row-focus");
      setTimeout(() => row.classList.remove("pad-row-focus"), 1200);
    }
  }

  // --------------------------------------------------------------------- //
  // ジョブ実行中ロック
  // --------------------------------------------------------------------- //
  const TERMINAL = new Set(["succeeded", "failed", "aborted", "idle"]);

  function applyToolbarLock() {
    root.classList.toggle("pad-editor-locked", state.locked);
    for (const btn of root.querySelectorAll(".pad-select-tools button")) {
      btn.disabled = state.locked;
    }
  }

  if (window.webui.jobs) {
    window.webui.jobs.onUpdate((job) => {
      const active = Boolean(job && !TERMINAL.has(job.status));
      if (active === state.locked) return;
      state.locked = active;
      if (!state.config) return;
      // 表は再描画で行ごとの本来の disabled（継承ボタン等）を取り戻し、
      // ロック中は renderTable 内で一括 disable される。
      renderTable();
      applyToolbarLock();
    });
  }

  load();
})();
