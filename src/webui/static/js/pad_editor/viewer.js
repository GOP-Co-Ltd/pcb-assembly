"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const MARGIN_MM = 2;

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
  const marker = document.createElementNS(SVG_NS, "marker");
  marker.setAttribute("id", "pad-route-arrow");
  marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("refX", "8");
  marker.setAttribute("refY", "5");
  marker.setAttribute("markerWidth", "5");
  marker.setAttribute("markerHeight", "5");
  marker.setAttribute("orient", "auto-start-reverse");
  const arrow = document.createElementNS(SVG_NS, "path");
  arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
  arrow.setAttribute("class", "pad-route-arrow-head");
  marker.appendChild(arrow);
  defs.appendChild(marker);
  svg.appendChild(defs);

  const group = document.createElementNS(SVG_NS, "g");
  group.setAttribute("class", "pad-route-overlay");
  group.dataset.testid = "pad-route-overlay";

  for (let index = 0; index < route.pads.length - 1; index += 1) {
    const from = route.pads[index];
    const to = route.pads[index + 1];
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", from.center[0]);
    line.setAttribute("y1", from.center[1]);
    line.setAttribute("x2", to.center[0]);
    line.setAttribute("y2", to.center[1]);
    line.setAttribute("class", "pad-route-segment");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    line.setAttribute("marker-end", "url(#pad-route-arrow)");
    line.dataset.testid = "pad-route-segment";
    line.dataset.fromPadId = from.id;
    line.dataset.toPadId = to.id;
    group.appendChild(line);
  }

  appendEndpoint(group, route.pads[0], "start", "S");
  appendEndpoint(group, route.pads[route.pads.length - 1], "end", "E");
  svg.appendChild(group);
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
