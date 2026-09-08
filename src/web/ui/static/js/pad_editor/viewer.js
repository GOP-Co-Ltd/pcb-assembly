"use strict";

const { svgEl } = window.webui;

const MARGIN_MM = 4;
const AXIS_OFFSET_MM = 0.9;
const AXIS_TICK_MM = 0.35;
const ROUTE_COLORS = [
  "#d33f49",
  "#e67700",
  "#c99700",
  "#2b9348",
  "#009e9a",
  "#2474bf",
  "#7048e8",
];
const FILL_PATH_COLOR = "#ff3333";
const MIN_DIRECTION_LENGTH_MM = 0.000001;

export function renderViewer(svg, config, state) {
  svg.replaceChildren();
  state.padEls.clear();
  const routeById = routeMap(state.route);

  const outline = config.outline;
  const xs = outline.map((point) => point[0]);
  const ys = outline.map((point) => point[1]);
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  const width = Math.max(...xs) - minX;
  const height = Math.max(...ys) - minY;
  const viewWidth = width + 2 * MARGIN_MM;
  const viewHeight = height + 2 * MARGIN_MM;
  svg.setAttribute(
    "viewBox",
    `${minX - MARGIN_MM} ${minY - MARGIN_MM} ${viewWidth} ${viewHeight}`
  );
  svg.style.aspectRatio = `${viewWidth} / ${viewHeight}`;

  const outlineEl = svgEl("polygon", {
    points: pointsAttr(outline),
    class: "pad-outline",
    "vector-effect": "non-scaling-stroke",
  });
  outlineEl.dataset.testid = "pad-outline";
  svg.appendChild(outlineEl);
  renderOutlineAxes(svg, minX, minY, width, height);

  for (const pad of config.pads) {
    const routePad = routeById.get(pad.id);
    const el = svgEl("polygon", {
      points: pointsAttr(pad.polygon),
      "vector-effect": "non-scaling-stroke",
    });
    el.dataset.testid = "pad-polygon";
    el.dataset.padId = pad.id;
    el.dataset.nodeIds = pad.node_ids.join(" ");
    el.dataset.designator = pad.designator;
    el.dataset.package = pad.package;
    el.dataset.padNumber = pad.pad_number;
    el.dataset.layer = pad.layer;
    if (routePad) el.dataset.routeOrder = routePad.order;
    else delete el.dataset.routeOrder;
    applyPadVisual(el, state, pad.enabled);
    el.style.display = pad.layer === state.layer ? "" : "none";
    const title = svgEl("title", {});
    title.textContent =
      `${pad.designator} pad ${pad.pad_number}` +
      (pad.package ? ` / ${pad.package}` : "") +
      (routePad ? ` / 塗布順 ${routePad.order}` : "");
    el.appendChild(title);
    state.padEls.set(pad.id, el);
    svg.appendChild(el);
  }

  renderFillPathOverlay(svg, state.fillPath);
  renderRouteOverlay(svg, state.route);
  renderPurgeMarker(svg, config.initial_purge);
}

function renderPurgeMarker(svg, purge) {
  // 任意点で指定されたパージ位置だけを描く（pad 指定はパッド自体が見える）
  const point = purge?.point;
  if (!point) return;
  const marker = svgEl("g", {
    class: "pad-purge-marker",
    transform: `translate(${point[0]} ${point[1]})`,
  });
  marker.dataset.testid = "pad-purge-marker";
  const title = svgEl("title", {});
  title.textContent = purge.selection_label || "パージ位置";
  marker.appendChild(title);
  marker.appendChild(svgEl("circle", { r: "0.45", "vector-effect": "non-scaling-stroke" }));
  const text = svgEl("text", { y: "0.04" });
  text.textContent = "P";
  marker.appendChild(text);
  svg.appendChild(marker);
}

export function applyPadVisual(el, state, enabled) {
  const selected = state.selected.has(el.dataset.padId);
  el.setAttribute(
    "class",
    `pad${enabled ? " pad-enabled" : " pad-disabled"}${selected ? " pad-selected" : ""}`
  );
}

export function refreshSelectionVisual(config, state) {
  for (const [id, el] of state.padEls) {
    const pad = config.pads.find((candidate) => candidate.id === id);
    if (pad) applyPadVisual(el, state, pad.enabled);
  }
}

export function showLayer(config, state, layer) {
  state.layer = layer;
  for (const [id, el] of state.padEls) {
    el.style.display = el.dataset.layer === state.layer ? "" : "none";
    if (el.dataset.layer !== state.layer) state.selected.delete(id);
  }
  refreshSelectionVisual(config, state);
}

export function svgPoint(svg, evt) {
  const point = svg.createSVGPoint();
  point.x = evt.clientX;
  point.y = evt.clientY;
  const ctm = svg.getScreenCTM();
  if (!ctm) return { x: 0, y: 0 };
  const local = point.matrixTransform(ctm.inverse());
  return { x: local.x, y: local.y };
}

export function createSelectionRect(svg) {
  const rect = svgEl("rect", {
    class: "pad-select-rect",
    "vector-effect": "non-scaling-stroke",
  });
  svg.appendChild(rect);
  return rect;
}

export function updateSelectionRect(rect, start, end) {
  const x = Math.min(start.x, end.x);
  const y = Math.min(start.y, end.y);
  rect.setAttribute("x", x);
  rect.setAttribute("y", y);
  rect.setAttribute("width", Math.abs(end.x - start.x));
  rect.setAttribute("height", Math.abs(end.y - start.y));
}

export function idsInRect(state, a, b) {
  const minX = Math.min(a.x, b.x);
  const maxX = Math.max(a.x, b.x);
  const minY = Math.min(a.y, b.y);
  const maxY = Math.max(a.y, b.y);
  const ids = new Set();
  for (const [id, el] of state.padEls) {
    if (el.dataset.layer !== state.layer) continue;
    const box = el.getBBox();
    const intersects =
      box.x <= maxX &&
      box.x + box.width >= minX &&
      box.y <= maxY &&
      box.y + box.height >= minY;
    if (intersects) ids.add(id);
  }
  return ids;
}

export function pointsAttr(coords) {
  return coords.map((point) => `${point[0]},${point[1]}`).join(" ");
}

function routeMap(route) {
  const byId = new Map();
  if (!route) return byId;
  for (const pad of route.pads) byId.set(pad.id, pad);
  return byId;
}

function renderRouteOverlay(svg, route) {
  if (!route || route.pads.length === 0) return;

  const defs = svgEl("defs", {});
  svg.appendChild(defs);

  const group = svgEl("g", { class: "pad-route-overlay" });
  group.dataset.testid = "pad-route-overlay";
  const segmentCount = route.pads.length - 1;

  for (let index = 0; index < segmentCount; index += 1) {
    const from = route.pads[index];
    const to = route.pads[index + 1];
    const color = routeColor(index, segmentCount);
    const markerId = `pad-route-arrow-${index}`;
    appendArrowMarker(defs, markerId, color);
    const line = svgEl("line", {
      x1: from.center[0],
      y1: from.center[1],
      x2: to.center[0],
      y2: to.center[1],
      class: "pad-route-segment",
      stroke: color,
      "vector-effect": "non-scaling-stroke",
      "marker-end": `url(#${markerId})`,
    });
    line.dataset.testid = "pad-route-segment";
    line.dataset.fromPadId = from.id;
    line.dataset.toPadId = to.id;
    line.dataset.routeColor = color;
    group.appendChild(line);
  }

  appendEndpoint(group, route.pads[0], "start", "S");
  appendEndpoint(group, route.pads[route.pads.length - 1], "end", "E");
  svg.appendChild(group);
}

function renderFillPathOverlay(svg, fillPath) {
  if (!fillPath || fillPath.pads.length === 0) return;

  const defs = svgEl("defs", {});
  svg.appendChild(defs);

  const group = svgEl("g", { class: "pad-fill-path-overlay" });
  group.dataset.testid = "pad-fill-path-overlay";
  let markerIndex = 0;

  for (const pad of fillPath.pads) {
    for (const [index, path] of pad.paths.entries()) {
      if (path.length === 0) continue;
      if (path.length > 1) appendFillPathLine(group, pad, path, index);
      appendFillPathPoint(group, pad, path[0], index);

      const directionTo = fillPathDirectionTarget(path);
      if (!directionTo) continue;
      const markerId = `pad-fill-path-arrow-${markerIndex}`;
      appendArrowMarker(defs, markerId, FILL_PATH_COLOR, "pad-fill-path-arrow-head");
      appendFillPathDirection(group, pad, path[0], directionTo, index, markerId);
      markerIndex += 1;
    }
  }

  svg.appendChild(group);
}

function appendFillPathLine(group, pad, path, index) {
  const line = svgEl("polyline", {
    points: pointsAttr(path),
    class: "pad-fill-path-polyline",
    "vector-effect": "non-scaling-stroke",
  });
  line.dataset.testid = "pad-fill-path-polyline";
  line.dataset.padId = pad.id;
  line.dataset.pathIndex = String(index);
  group.appendChild(line);
}

function appendFillPathPoint(group, pad, point, index) {
  const marker = svgEl("circle", {
    cx: point[0],
    cy: point[1],
    r: "0.18",
    class: "pad-fill-path-point",
    "vector-effect": "non-scaling-stroke",
  });
  marker.dataset.testid = "pad-fill-path-point";
  marker.dataset.padId = pad.id;
  marker.dataset.pathIndex = String(index);
  group.appendChild(marker);
}

function appendFillPathDirection(group, pad, from, to, index, markerId) {
  const line = svgEl("line", {
    x1: from[0],
    y1: from[1],
    x2: to[0],
    y2: to[1],
    class: "pad-fill-path-direction",
    "vector-effect": "non-scaling-stroke",
    "marker-end": `url(#${markerId})`,
  });
  line.dataset.testid = "pad-fill-path-direction";
  line.dataset.padId = pad.id;
  line.dataset.pathIndex = String(index);
  group.appendChild(line);
}

function fillPathDirectionTarget(path) {
  const start = path[0];
  for (const point of path.slice(1)) {
    const dx = point[0] - start[0];
    const dy = point[1] - start[1];
    if (Math.hypot(dx, dy) > MIN_DIRECTION_LENGTH_MM) return point;
  }
  return null;
}

function renderOutlineAxes(svg, minX, minY, width, height) {
  const maxX = minX + width;
  const maxY = minY + height;
  const axis = svgEl("g", { class: "pad-axis" });
  axis.dataset.testid = "pad-axis";

  const xAxisY = maxY + AXIS_OFFSET_MM;
  appendAxisLine(axis, minX, xAxisY, maxX, xAxisY);
  const xTicks = axisTicks(width);
  for (const [index, tick] of xTicks.entries()) {
    const x = minX + tick;
    appendAxisLine(axis, x, xAxisY - AXIS_TICK_MM, x, xAxisY + AXIS_TICK_MM);
    appendAxisLabel(
      axis,
      x,
      xAxisY + AXIS_TICK_MM + 0.5,
      axisTickLabel(tick, index === xTicks.length - 1),
      "middle"
    );
  }

  const yAxisX = minX - AXIS_OFFSET_MM;
  appendAxisLine(axis, yAxisX, minY, yAxisX, maxY);
  const yTicks = axisTicks(height);
  for (const [index, tick] of yTicks.entries()) {
    const y = minY + tick;
    appendAxisLine(axis, yAxisX - AXIS_TICK_MM, y, yAxisX + AXIS_TICK_MM, y);
    appendAxisLabel(
      axis,
      yAxisX - AXIS_TICK_MM - 0.18,
      y + 0.18,
      axisTickLabel(tick, index === yTicks.length - 1),
      "end"
    );
  }

  svg.appendChild(axis);
}

function appendAxisLine(group, x1, y1, x2, y2) {
  const line = svgEl("line", {
    class: "pad-axis-line",
    x1,
    y1,
    x2,
    y2,
    "vector-effect": "non-scaling-stroke",
  });
  group.appendChild(line);
}

function appendAxisLabel(group, x, y, label, anchor) {
  const text = svgEl("text", {
    class: "pad-axis-label",
    x,
    y,
    "text-anchor": anchor,
  });
  text.textContent = label;
  text.dataset.testid = "pad-axis-label";
  group.appendChild(text);
}

function axisTicks(length) {
  const roundedLength = roundAxisValue(length);
  const step = roundedLength <= 8 ? 2 : roundedLength <= 25 ? 5 : 10;
  const ticks = [0];
  for (let tick = step; tick < roundedLength; tick += step) {
    ticks.push(roundAxisValue(tick));
  }
  if (roundedLength > 0 && ticks[ticks.length - 1] !== roundedLength) {
    ticks.push(roundedLength);
  }
  return ticks;
}

function axisTickLabel(value, includesUnit) {
  const label = roundAxisValue(value).toString();
  return includesUnit ? `${label} mm` : label;
}

function roundAxisValue(value) {
  return Number(value.toFixed(4));
}

function appendArrowMarker(defs, id, color, className = "pad-route-arrow-head") {
  const marker = svgEl("marker", {
    id,
    viewBox: "0 0 10 10",
    refX: "8",
    refY: "5",
    markerWidth: "5",
    markerHeight: "5",
    orient: "auto-start-reverse",
  });
  const arrow = svgEl("path", {
    d: "M 0 0 L 10 5 L 0 10 z",
    class: className,
    fill: color,
  });
  marker.appendChild(arrow);
  defs.appendChild(marker);
}

function routeColor(index, segmentCount) {
  if (segmentCount <= 1) return ROUTE_COLORS[0];
  const scaledIndex = (index / (segmentCount - 1)) * (ROUTE_COLORS.length - 1);
  const fromIndex = Math.floor(scaledIndex);
  const toIndex = Math.min(fromIndex + 1, ROUTE_COLORS.length - 1);
  return mixHexColor(
    ROUTE_COLORS[fromIndex],
    ROUTE_COLORS[toIndex],
    scaledIndex - fromIndex
  );
}

function mixHexColor(from, to, ratio) {
  const fromRgb = hexToRgb(from);
  const toRgb = hexToRgb(to);
  const mixed = fromRgb.map((channel, index) =>
    Math.round(channel + (toRgb[index] - channel) * ratio)
  );
  const hex = mixed
    .map((channel) => channel.toString(16).padStart(2, "0"))
    .join("");
  return `#${hex}`;
}

function hexToRgb(value) {
  return [1, 3, 5].map((start) =>
    Number.parseInt(value.slice(start, start + 2), 16)
  );
}

function appendEndpoint(group, pad, kind, label) {
  const marker = svgEl("g", {
    class: `pad-route-endpoint pad-route-${kind}`,
    transform: `translate(${pad.center[0]} ${pad.center[1]})`,
  });
  marker.dataset.testid = `pad-route-${kind}`;
  marker.dataset.padId = pad.id;

  const circle = svgEl("circle", {
    r: "0.45",
    "vector-effect": "non-scaling-stroke",
  });
  marker.appendChild(circle);

  const text = svgEl("text", { y: "0.04" });
  text.textContent = label;
  marker.appendChild(text);
  group.appendChild(marker);
}
