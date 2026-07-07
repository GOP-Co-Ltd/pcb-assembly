"use strict";

import {
  DISPENSE_MODE_LABELS,
  FIELDS,
  FIELD_LABELS,
  FIELD_KINDS,
  round4,
} from "./model.js";

// 階層 override 表の描画。DOM 生成とセル内の数値パース検証（Number.isFinite）
// のみを持ち、状態遷移と API 呼び出しは actions 経由で index.js に委ねる。
// state は index.js と共有参照（expanded / locked / rowEls / nodeById 等）。
// actions: {
//   patchNode(body, options), patchNodeClear(nodeId, field),
//   patchNodeEnabled(nodeId, enabled), patchNodeEnabledInherit(nodeId),
//   debouncePatchNode(nodeId, field, body, options),
//   focusNodePads(nodeId), hoverNode(nodeId | null), rerender(),
// }

const { toast } = window.webui;

export function renderTable(tableBody, state, actions) {
  tableBody.replaceChildren();
  state.rowEls.clear();
  appendRows(tableBody, state, actions, state.config.tree, 0);
  if (state.locked) {
    for (const el of tableBody.querySelectorAll("input, select, button")) {
      el.disabled = true;
    }
  }
}

function appendRows(tableBody, state, actions, node, depth) {
  const tr = buildRow(state, actions, node, depth);
  tableBody.appendChild(tr);
  state.rowEls.set(node.id, tr);
  if (node.children.length > 0 && state.expanded.has(node.id)) {
    for (const child of node.children) {
      appendRows(tableBody, state, actions, child, depth + 1);
    }
  }
}

function buildRow(state, actions, node, depth) {
  const tr = document.createElement("tr");
  tr.dataset.nodeId = node.id;
  tr.dataset.testid = "pad-tree-row";
  const enabled = node.resolved.enabled;
  const own = node.own_override;
  // own_summary はサーバ算出（own_override は isOverride 判定・select 初期値用に残る）
  const ownSummary = node.own_summary;
  const descendantSummary = node.descendant_summary;
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
      actions.rerender();
    });
    nameTd.appendChild(toggle);
  }

  const label = document.createElement("span");
  label.className = "pad-node-label";
  label.textContent = node.label;
  nameTd.appendChild(label);
  appendNodeOverrideBadges(nameTd, ownSummary, descendantSummary);
  tr.addEventListener("mouseenter", () => actions.hoverNode(node.id));
  tr.addEventListener("mouseleave", () => {
    if (state.hoveredNode === node.id) actions.hoverNode(null);
  });
  tr.addEventListener("click", () => actions.focusNodePads(node.id));
  tr.appendChild(nameTd);

  const enTd = document.createElement("td");
  enTd.className = "pad-col-enabled";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.dataset.testid = "pad-enabled-checkbox";
  cb.checked = enabled;
  cb.indeterminate =
    node.id !== "L0" && (own.enabled === null || own.enabled === undefined);
  cb.addEventListener("change", () =>
    actions.patchNodeEnabled(node.id, cb.checked)
  );
  enTd.appendChild(cb);
  const inheritBtn = document.createElement("button");
  inheritBtn.type = "button";
  inheritBtn.className = "pad-enabled-inherit";
  inheritBtn.dataset.testid = "pad-enabled-inherit";
  inheritBtn.textContent = "継承";
  inheritBtn.title = "有効/無効を継承に戻す";
  inheritBtn.disabled = own.enabled === null || own.enabled === undefined;
  inheritBtn.addEventListener("click", () =>
    actions.patchNodeEnabledInherit(node.id)
  );
  enTd.appendChild(inheritBtn);
  if (descendantSummary.enabled_count > 0) {
    appendDescendantMarker(
      enTd,
      `子孫ノードの有効/無効 override が ${descendantSummary.enabled_count} 件あります。`,
      "pad-descendant-enabled-marker",
      descendantSummary.enabled_count
    );
  }
  tr.appendChild(enTd);

  for (const field of FIELDS) {
    const kind = FIELD_KINDS[field] || "number";
    if (kind === "mode") {
      tr.appendChild(buildModeCell(state, actions, node, field, descendantSummary));
    } else if (kind === "height") {
      tr.appendChild(buildHeightCell(state, actions, node, field, descendantSummary));
    } else {
      tr.appendChild(buildValueCell(state, actions, node, field, descendantSummary));
    }
  }
  return tr;
}

function buildModeCell(state, actions, node, field, descendantSummary) {
  const td = document.createElement("td");
  td.className = "pad-col-value";
  const own = node.own_override;
  const ownValue = own.values ? own.values[field] : undefined;
  const isOverride = ownValue !== undefined;
  const resolved = node.resolved[field];
  const descendantCount = descendantSummary.field_counts[field] || 0;

  const select = document.createElement("select");
  select.className = isOverride ? "pad-cell override" : "pad-cell inherited";
  select.dataset.field = field;
  select.dataset.nodeId = node.id;
  select.dataset.testid = "pad-dispense-mode-select";
  select.title = FIELD_LABELS[field] || field;
  appendSelectOption(
    select,
    "",
    resolved ? `継承 (${DISPENSE_MODE_LABELS[resolved] || resolved})` : "継承",
    !isOverride
  );
  for (const [value, label] of Object.entries(DISPENSE_MODE_LABELS)) {
    appendSelectOption(select, value, label, ownValue === value);
  }
  select.addEventListener("change", () => {
    if (state.locked) return;
    if (select.value === "") actions.patchNodeClear(node.id, field);
    else {
      actions.patchNode(
        { node: node.id, values: { [field]: select.value } },
        { descendantField: field, descendantCount }
      );
    }
  });
  appendOverrideControls(td, actions, node.id, field, isOverride);
  td.appendChild(select);
  appendDescendantFieldMarker(td, field, descendantCount);
  return td;
}

function buildHeightCell(state, actions, node, field, descendantSummary) {
  const td = document.createElement("td");
  td.className = "pad-col-value pad-col-height";
  const own = node.own_override;
  const ownValue = own.values ? own.values[field] : undefined;
  const isOverride = ownValue !== undefined;
  const resolved = node.resolved[field];
  const descendantCount = descendantSummary.field_counts[field] || 0;

  const select = document.createElement("select");
  select.className = isOverride ? "pad-cell override" : "pad-cell inherited";
  select.dataset.field = field;
  select.dataset.nodeId = node.id;
  select.dataset.testid = "pad-height-mode-select";
  select.title = FIELD_LABELS[field] || field;
  const resolvedLabel =
    resolved === "auto"
      ? "Auto"
      : resolved === null || resolved === undefined
        ? "未設定"
        : round4(resolved);
  appendSelectOption(select, "", `継承 (${resolvedLabel})`, !isOverride);
  appendSelectOption(select, "auto", "Auto", ownValue === "auto");
  appendSelectOption(
    select,
    "manual",
    "手動",
    isOverride && ownValue !== "auto"
  );

  const input = document.createElement("input");
  input.type = "number";
  input.step = "any";
  input.className = isOverride ? "pad-cell override" : "pad-cell inherited";
  input.dataset.field = field;
  input.dataset.nodeId = node.id;
  input.dataset.testid = "pad-setting-input";
  input.title = FIELD_LABELS[field] || field;
  if (isOverride && ownValue !== "auto") {
    input.value = ownValue;
  } else {
    input.value = "";
    input.placeholder =
      resolved === "auto" || resolved === null ? "" : String(round4(resolved));
  }
  input.hidden = select.value !== "manual";
  input.addEventListener("change", () =>
    commitHeightCell(state, actions, node.id, field, select, input, descendantCount)
  );
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      commitHeightCell(state, actions, node.id, field, select, input, descendantCount);
    }
  });
  select.addEventListener("change", () => {
    if (state.locked) return;
    input.hidden = select.value !== "manual";
    if (select.value === "") actions.patchNodeClear(node.id, field);
    else if (select.value === "auto") {
      actions.patchNode(
        { node: node.id, values: { [field]: "auto" } },
        { descendantField: field, descendantCount }
      );
    } else {
      commitHeightCell(state, actions, node.id, field, select, input, descendantCount);
    }
  });

  appendOverrideControls(td, actions, node.id, field, isOverride);
  td.appendChild(select);
  td.appendChild(input);
  appendDescendantFieldMarker(td, field, descendantCount);
  return td;
}

function buildValueCell(state, actions, node, field, descendantSummary) {
  const td = document.createElement("td");
  td.className = "pad-col-value";
  const own = node.own_override;
  const ownValue = own.values ? own.values[field] : undefined;
  const isOverride = ownValue !== undefined;
  const resolved = node.resolved[field];
  const descendantCount = descendantSummary.field_counts[field] || 0;

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
    commitCell(state, actions, node.id, field, input, descendantCount)
  );
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      commitCell(state, actions, node.id, field, input, descendantCount);
    }
  });
  td.appendChild(input);

  appendOverrideControls(td, actions, node.id, field, isOverride);
  appendDescendantFieldMarker(td, field, descendantCount);
  return td;
}

function appendSelectOption(select, value, label, selected) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  option.selected = selected;
  select.appendChild(option);
}

function appendOverrideControls(td, actions, nodeId, field, isOverride) {
  if (!isOverride) return;
  const marker = document.createElement("span");
  marker.className = "pad-override-marker";
  marker.dataset.testid = "pad-own-override-marker";
  marker.dataset.field = field;
  marker.textContent = "●";
  td.appendChild(marker);
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "pad-cell-clear";
  clearBtn.dataset.testid = "pad-clear-override";
  clearBtn.dataset.field = field;
  clearBtn.textContent = "×";
  clearBtn.title = "継承に戻す";
  clearBtn.addEventListener("click", () => actions.patchNodeClear(nodeId, field));
  td.appendChild(clearBtn);
}

function appendDescendantFieldMarker(td, field, descendantCount) {
  if (descendantCount <= 0) return;
  appendDescendantMarker(
    td,
    `子孫ノードの ${FIELD_LABELS[field] || field} override が ${descendantCount} 件あります。`,
    "pad-descendant-field-marker",
    descendantCount,
    field
  );
}

function commitHeightCell(state, actions, nodeId, field, select, input, descendantCount) {
  if (state.locked || select.value !== "manual") return;
  const raw = input.value.trim();
  if (raw === "") {
    input.focus();
    return;
  }
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    toast("数値を入力してください", false);
    return;
  }
  actions.patchNode(
    { node: nodeId, values: { [field]: value } },
    { descendantField: field, descendantCount }
  );
}

function commitCell(state, actions, nodeId, field, input, descendantCount) {
  if (state.locked) return;
  const raw = input.value.trim();
  if (raw === "") {
    if (
      state.nodeById.get(nodeId)?.own_override.values?.[field] !== undefined
    ) {
      actions.patchNodeClear(nodeId, field);
    }
    return;
  }
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    toast("数値を入力してください", false);
    return;
  }
  if (!state.nodeById.get(nodeId)?.resolved.enabled) {
    toast("無効化されているパーツです", false);
  }
  actions.debouncePatchNode(
    nodeId,
    field,
    { values: { [field]: value } },
    { descendantField: field, descendantCount }
  );
}

// ---- バッジ・マーカー ----

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
  if (summary.enabled_count > 0) {
    parts.push(`有効/無効 ${summary.enabled_count}件`);
  }
  if (summary.fields.length > 0) {
    parts.push(overrideFieldsTitle(summary.fields));
  }
  return `子孫 ${summary.node_count} ノードに override: ${parts.join("、")}`;
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
