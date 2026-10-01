"use strict";

// 塗布パラメータの列順・ラベル・入力種別・選択肢は API（pad-config の
// `fields`、サーバ側 PASTE_PARAM_FIELDS）だけを出典とし、ここには複製しない。

export function fieldLabel(config, field) {
  const info = config?.fields?.find((item) => item.name === field);
  return info ? info.label : field;
}

export function choiceLabels(fieldInfo) {
  return Object.fromEntries(fieldInfo.choices.map((c) => [c.value, c.label]));
}

export function buildNodeIndexes(tree) {
  const parentOf = new Map();
  const nodeById = new Map();
  const walk = (node, parentId) => {
    nodeById.set(node.id, node);
    if (parentId !== null) parentOf.set(node.id, parentId);
    for (const child of node.children) walk(child, node.id);
  };
  walk(tree, null);
  return { parentOf, nodeById };
}

export function ancestorChain(parentOf, nodeId) {
  const chain = [nodeId];
  let current = nodeId;
  while (parentOf.has(current)) {
    current = parentOf.get(current);
    chain.unshift(current);
  }
  return chain;
}

export function padsUnderNode(config, nodeId) {
  return config.pads.filter((pad) => pad.node_ids.includes(nodeId));
}

export function l4NodeIdForPad(pad, nodeById = null) {
  if (nodeById) {
    return pad.node_ids.find((id) => nodeById.get(id)?.level === 4);
  }
  return pad.node_ids[pad.node_ids.length - 1];
}

export function cleanupSelection(config, selected) {
  const ids = new Set(config.pads.map((pad) => pad.id));
  for (const id of [...selected]) {
    if (!ids.has(id)) selected.delete(id);
  }
}

export function round4(value) {
  return Math.round(value * 1e4) / 1e4;
}
