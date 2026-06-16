"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
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

  const outlineEl = document.createElementNS(SVG_NS, "polygon");
  outlineEl.setAttribute("points", pointsAttr(outline));
  outlineEl.setAttribute("class", "pad-outline");
  outlineEl.setAttribute("vector-effect", "non-scaling-stroke");
  outlineEl.dataset.testid = "pad-outline";
  svg.appendChild(outlineEl);
  renderOutlineAxes(svg, minX, minY, width, height);

  for (const pad of config.pads) {
    const el = document.createElementNS(SVG_NS, "polygon");
    const routePad = routeById.get(pad.id);
    el.setAttribute("points", pointsAttr(pad.polygon));
    el.setAttribute("vector-effect", "non-scaling-stroke");
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
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent =
      `${pad.designator} pad ${pad.pad_number}` +
      (pad.package ? ` / ${pad.package}` : "") +
      (routePad ? ` / 塗布順 ${routePad.order}` : "");
    el.appendChild(title);
    state.padEls.set(pad.id, el);
    svg.appendChild(el);
  }

  renderRouteOverlay(svg, state.route);
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
  const rect = document.createElementNS(SVG_NS, "rect");
  rect.setAttribute("class", "pad-select-rect");
  rect.setAttribute("vector-effect", "non-scaling-stroke");
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

  const defs = document.createElementNS(SVG_NS, "defs");
  svg.appendChild(defs);

  const group = document.createElementNS(SVG_NS, "g");
  group.setAttribute("class", "pad-route-overlay");
  group.dataset.testid = "pad-route-overlay";
  const segmentCount = route.pads.length - 1;

  for (let index = 0; index < segmentCount; index += 1) {
    const from = route.pads[index];
    const to = route.pads[index + 1];
    const color = routeColor(index, segmentCount);
    const markerId = `pad-route-arrow-${index}`;
    appendArrowMarker(defs, markerId, color);
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", from.center[0]);
    line.setAttribute("y1", from.center[1]);
    line.setAttribute("x2", to.center[0]);
    line.setAttribute("y2", to.center[1]);
    line.setAttribute("class", "pad-route-segment");
    line.setAttribute("stroke", color);
    line.setAttribute("vector-effect", "non-scaling-stroke");
    line.setAttribute("marker-end", `url(#${markerId})`);
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

function renderOutlineAxes(svg, minX, minY, width, height) {
  const maxX = minX + width;
  const maxY = minY + height;
  const axis = document.createElementNS(SVG_NS, "g");
  axis.setAttribute("class", "pad-axis");
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
  const line = document.createElementNS(SVG_NS, "line");
  line.setAttribute("class", "pad-axis-line");
  line.setAttribute("x1", x1);
  line.setAttribute("y1", y1);
  line.setAttribute("x2", x2);
  line.setAttribute("y2", y2);
  line.setAttribute("vector-effect", "non-scaling-stroke");
  group.appendChild(line);
}

function appendAxisLabel(group, x, y, label, anchor) {
  const text = document.createElementNS(SVG_NS, "text");
  text.setAttribute("class", "pad-axis-label");
  text.setAttribute("x", x);
  text.setAttribute("y", y);
  text.setAttribute("text-anchor", anchor);
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

function appendArrowMarker(defs, id, color) {
  const marker = document.createElementNS(SVG_NS, "marker");
  marker.setAttribute("id", id);
  marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("refX", "8");
  marker.setAttribute("refY", "5");
  marker.setAttribute("markerWidth", "5");
  marker.setAttribute("markerHeight", "5");
  marker.setAttribute("orient", "auto-start-reverse");
  const arrow = document.createElementNS(SVG_NS, "path");
  arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
  arrow.setAttribute("class", "pad-route-arrow-head");
  arrow.setAttribute("fill", color);
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
  const marker = document.createElementNS(SVG_NS, "g");
  marker.setAttribute("class", `pad-route-endpoint pad-route-${kind}`);
  marker.dataset.testid = `pad-route-${kind}`;
  marker.dataset.padId = pad.id;
  marker.setAttribute("transform", `translate(${pad.center[0]} ${pad.center[1]})`);

  const circle = document.createElementNS(SVG_NS, "circle");
  circle.setAttribute("r", "0.45");
  circle.setAttribute("vector-effect", "non-scaling-stroke");
  marker.appendChild(circle);

  const text = document.createElementNS(SVG_NS, "text");
  text.textContent = label;
  text.setAttribute("y", "0.04");
  marker.appendChild(text);
  group.appendChild(marker);
}
