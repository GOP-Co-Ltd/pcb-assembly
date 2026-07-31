"use strict";

export const FIELDS = [
  "dispense_mode",
  "ul_per_mm2",
  "paste_height",
  "prime_extra_delay",
  "bead_width_factor",
  "overlap",
  "boundary_margin",
];

export const FIELD_LABELS = {
  dispense_mode: "塗布方式",
  ul_per_mm2: "面積あたりのペースト量",
  paste_height: "塗布高さ",
  prime_extra_delay: "プライム後追加遅延",
  bead_width_factor: "ビード幅係数",
  overlap: "重なり率",
  boundary_margin: "外周余白",
};

export const FIELD_KINDS = {
  dispense_mode: "mode",
  paste_height: "height",
};

export const DISPENSE_MODE_LABELS = {
  auto: "Auto",
  dot: "点",
  line: "線",
  area: "面",
};

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
