"use strict";

export const FIELDS = [
  "ul_per_mm2",
  "paste_height",
  "fill_speed",
  "prime_extra_delay",
  "bead_width_factor",
  "overlap",
  "boundary_margin",
];

export const FIELD_LABELS = {
  ul_per_mm2: "面積あたりのペースト量",
  paste_height: "塗布高さ",
  fill_speed: "塗布速度",
  prime_extra_delay: "プライム後追加遅延",
  bead_width_factor: "ビード幅係数",
  overlap: "重なり率",
  boundary_margin: "外周余白",
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

export function ownOverride(config, nodeId) {
  return config.overrides[nodeId] || { enabled: null, values: {} };
}

export function resolvedEnabled(config, parentOf, nodeId) {
  let enabled = config.defaults?.enabled ?? true;
  for (const id of ancestorChain(parentOf, nodeId)) {
    const override = config.overrides[id];
    if (
      override &&
      override.enabled !== null &&
      override.enabled !== undefined
    ) {
      enabled = override.enabled;
    }
  }
  return enabled;
}

export function resolvedValue(config, parentOf, nodeId, field) {
  let value =
    config.defaults && config.defaults[field] !== undefined
      ? config.defaults[field]
      : null;
  for (const id of ancestorChain(parentOf, nodeId)) {
    const override = config.overrides[id];
    if (override && override.values && override.values[field] !== undefined) {
      value = override.values[field];
    }
  }
  return value;
}

export function padsUnderNode(config, nodeId) {
  return config.pads.filter((pad) => pad.node_ids.includes(nodeId));
}

export function l4NodeIdForPad(pad) {
  return pad.node_ids.find((id) => id.startsWith("L4:"));
}

export function cleanupSelection(config, selected) {
  const ids = new Set(config.pads.map((pad) => pad.id));
  for (const id of [...selected]) {
    if (!ids.has(id)) selected.delete(id);
  }
}

export function updateLocalOverride(config, body) {
  const id = body.node;
  const override = config.overrides[id]
    ? {
        enabled: config.overrides[id].enabled,
        values: { ...config.overrides[id].values },
      }
    : { enabled: null, values: {} };

  if ("enabled" in body) override.enabled = body.enabled;
  if (body.values) {
    for (const [key, value] of Object.entries(body.values)) {
      override.values[key] = value;
    }
  }
  if (body.clear) {
    for (const key of body.clear) delete override.values[key];
  }

  const empty =
    (override.enabled === null || override.enabled === undefined) &&
    Object.keys(override.values).length === 0;
  if (empty) delete config.overrides[id];
  else config.overrides[id] = override;
}

export function round4(value) {
  return Math.round(value * 1e4) / 1e4;
}
