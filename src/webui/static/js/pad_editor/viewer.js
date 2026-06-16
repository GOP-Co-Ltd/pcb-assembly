"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const MARGIN_MM = 2;

export function renderViewer(svg, config, state) {
  svg.replaceChildren();
  state.padEls.clear();

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
    el.setAttribute("points", pointsAttr(pad.polygon));
    el.setAttribute("vector-effect", "non-scaling-stroke");
    el.dataset.testid = "pad-polygon";
    el.dataset.padId = pad.id;
    el.dataset.nodeIds = pad.node_ids.join(" ");
    el.dataset.designator = pad.designator;
    el.dataset.package = pad.package;
    el.dataset.padNumber = pad.pad_number;
    el.dataset.layer = pad.layer;
    applyPadVisual(el, state, pad.enabled);
    el.style.display = pad.layer === state.layer ? "" : "none";
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent =
      `${pad.designator} pad ${pad.pad_number}` +
      (pad.package ? ` / ${pad.package}` : "");
    el.appendChild(title);
    state.padEls.set(pad.id, el);
    svg.appendChild(el);
  }
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
