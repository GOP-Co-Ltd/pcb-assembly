"use strict";

(() => {
  const root = document.getElementById("paste-flow-calibration-board");
  if (!root) return;

  const { api, debounce, downloadApi, svgEl, toast } = window.webui;
  const endpoint = "/api/pasting/paste-flow-calibration-board";
  const rows = document.getElementById("pfc-pattern-rows");
  const catalogSelect = document.getElementById("pfc-catalog-select");
  const addButton = document.getElementById("pfc-add-pattern");
  const preview = document.getElementById("pfc-preview");
  const previewStatus = document.getElementById("pfc-preview-status");
  const previewSummary = document.getElementById("pfc-preview-summary");
  const importButton = document.getElementById("pfc-import");
  const importFile = document.getElementById("pfc-import-file");
  const exportButton = document.getElementById("pfc-export");
  const generateButton = document.getElementById("pfc-generate");

  const boardFields = {
    width_mm: document.getElementById("pfc-board-width"),
    height_mm: document.getElementById("pfc-board-height"),
    edge_margin_mm: document.getElementById("pfc-edge-margin"),
    component_gap_mm: document.getElementById("pfc-component-gap"),
  };
  const purgeFields = {
    width_mm: document.getElementById("pfc-purge-width"),
    height_mm: document.getElementById("pfc-purge-height"),
  };

  let catalog = [];
  let config = null;
  let previewRequest = 0;

  function numberFrom(input, label) {
    const value = input.valueAsNumber;
    if (!Number.isFinite(value)) throw new Error(`${label}を数値で入力してください`);
    return value;
  }

  function itemFor(catalogId) {
    return catalog.find((item) => item.catalog_id === catalogId);
  }

  function collectConfig() {
    return {
      board: {
        width_mm: numberFrom(boardFields.width_mm, "基板幅"),
        height_mm: numberFrom(boardFields.height_mm, "基板高さ"),
        edge_margin_mm: numberFrom(boardFields.edge_margin_mm, "外周余白"),
        component_gap_mm: numberFrom(boardFields.component_gap_mm, "部品間余白"),
      },
      purge_pad: {
        width_mm: numberFrom(purgeFields.width_mm, "purge pad幅"),
        height_mm: numberFrom(purgeFields.height_mm, "purge pad高さ"),
      },
      patterns: [...rows.querySelectorAll("tr")].map((row) => ({
        catalog_id: row.dataset.catalogId,
        rotation_span_deg: numberFrom(
          row.querySelector('[data-pattern-field="rotation_span_deg"]'),
          "theta"
        ),
        rotation_count: numberFrom(
          row.querySelector('[data-pattern-field="rotation_count"]'),
          "回転パターン数n"
        ),
        repeat_count: numberFrom(
          row.querySelector('[data-pattern-field="repeat_count"]'),
          "繰り返し数m"
        ),
      })),
    };
  }

  function textCell(value, className = "") {
    const cell = document.createElement("td");
    cell.textContent = value;
    if (className) cell.className = className;
    return cell;
  }

  function numberInput(pattern, field, step) {
    const input = document.createElement("input");
    input.type = "number";
    input.step = step;
    input.value = pattern[field];
    input.dataset.patternField = field;
    input.setAttribute("aria-label", field);
    return input;
  }

  function renderPatternRows(patterns) {
    rows.replaceChildren();
    for (const pattern of patterns) {
      const item = itemFor(pattern.catalog_id);
      if (!item) continue;
      const row = document.createElement("tr");
      row.dataset.catalogId = pattern.catalog_id;
      row.appendChild(textCell(item.label, "pfc-part-label"));
      row.appendChild(textCell(item.family_label));

      const thetaCell = document.createElement("td");
      thetaCell.appendChild(numberInput(pattern, "rotation_span_deg", "any"));
      row.appendChild(thetaCell);
      const countCell = document.createElement("td");
      countCell.appendChild(numberInput(pattern, "rotation_count", "1"));
      row.appendChild(countCell);
      row.appendChild(textCell("—", "pfc-resolved-angles"));
      const repeatCell = document.createElement("td");
      repeatCell.appendChild(numberInput(pattern, "repeat_count", "1"));
      row.appendChild(repeatCell);
      row.appendChild(textCell("—", "pfc-resolved-size"));

      const actionCell = document.createElement("td");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "pfc-remove-pattern";
      remove.textContent = "削除";
      remove.dataset.removeCatalogId = pattern.catalog_id;
      remove.setAttribute("aria-label", `${item.label}を削除`);
      actionCell.appendChild(remove);
      row.appendChild(actionCell);
      rows.appendChild(row);
    }
  }

  function updateCatalogSelect() {
    const selected = new Set(
      [...rows.querySelectorAll("tr")].map((row) => row.dataset.catalogId)
    );
    catalogSelect.replaceChildren();
    for (const item of catalog) {
      if (selected.has(item.catalog_id)) continue;
      const option = document.createElement("option");
      option.value = item.catalog_id;
      option.textContent = `${item.family_label} / ${item.label}`;
      catalogSelect.appendChild(option);
    }
    addButton.disabled = catalogSelect.options.length === 0;
  }

  function applyConfig(nextConfig) {
    config = nextConfig;
    for (const [field, input] of Object.entries(boardFields)) {
      input.value = config.board[field];
    }
    for (const [field, input] of Object.entries(purgeFields)) {
      input.value = config.purge_pad[field];
    }
    renderPatternRows(config.patterns);
    updateCatalogSelect();
  }

  function clearResolvedValues() {
    for (const cell of rows.querySelectorAll(
      ".pfc-resolved-angles, .pfc-resolved-size"
    )) {
      cell.textContent = "—";
    }
  }

  function clearPreview(message, error = false) {
    preview.replaceChildren();
    preview.removeAttribute("viewBox");
    previewSummary.textContent = "";
    previewStatus.textContent = message;
    previewStatus.classList.toggle("error", error);
    clearResolvedValues();
    generateButton.disabled = true;
  }

  function polygonElement(polygon) {
    return svgEl("polygon", {
      points: polygon.points.map((point) => `${point.x},${point.y}`).join(" "),
      class: polygon.layer === "F.Cu" ? "pfc-copper" : "pfc-paste",
    });
  }

  function renderLayout(layout) {
    preview.replaceChildren();
    preview.setAttribute(
      "viewBox",
      `0 0 ${layout.board.width_mm} ${layout.board.height_mm}`
    );
    preview.appendChild(
      svgEl("rect", {
        x: 0,
        y: 0,
        width: layout.board.width_mm,
        height: layout.board.height_mm,
        class: "pfc-board-outline",
      })
    );

    for (const layer of ["F.Cu", "F.Paste"]) {
      for (const polygon of layout.purge_polygons) {
        if (polygon.layer === layer) preview.appendChild(polygonElement(polygon));
      }
      for (const group of layout.groups) {
        for (const component of group.components) {
          for (const polygon of component.polygons) {
            if (polygon.layer === layer) preview.appendChild(polygonElement(polygon));
          }
        }
      }
    }

    for (const group of layout.groups) {
      preview.appendChild(
        svgEl("rect", {
          x: group.bounds.x,
          y: group.bounds.y,
          width: group.bounds.width,
          height: group.bounds.height,
          class: "pfc-group-boundary",
        })
      );
      const label = svgEl("text", {
        x: group.bounds.x + 0.25,
        y: group.bounds.y + 1.05,
        class: "pfc-group-label",
      });
      label.textContent = group.label;
      preview.appendChild(label);

      const row = rows.querySelector(`[data-catalog-id="${group.catalog_id}"]`);
      if (row) {
        row.querySelector(".pfc-resolved-angles").textContent = group.angles_deg
          .map((angle) => `${Number(angle.toFixed(3))}°`)
          .join(", ");
        row.querySelector(".pfc-resolved-size").textContent =
          `${group.bounds.width.toFixed(2)} × ${group.bounds.height.toFixed(2)}`;
      }
    }

    const purgeLabel = svgEl("text", {
      x: layout.purge_pad.x + layout.purge_pad.width + 0.25,
      y: layout.purge_pad.y + layout.purge_pad.height * 0.72,
      class: "pfc-group-label",
    });
    purgeLabel.textContent = "PURGE";
    preview.appendChild(purgeLabel);
    previewSummary.textContent = `${layout.component_count}部品 + purge pad`;
    previewStatus.textContent = "配置可能です";
    previewStatus.classList.remove("error");
    generateButton.disabled = false;
  }

  async function refreshPreview() {
    const request = ++previewRequest;
    generateButton.disabled = true;
    previewStatus.textContent = "配置を計算中…";
    previewStatus.classList.remove("error");
    let nextConfig;
    try {
      nextConfig = collectConfig();
    } catch (err) {
      if (request === previewRequest) clearPreview(err.message, true);
      return;
    }
    try {
      const layout = await api("POST", `${endpoint}/preview`, nextConfig);
      if (request !== previewRequest) return;
      applyConfig(layout.config);
      renderLayout(layout);
    } catch (err) {
      if (request === previewRequest) clearPreview(err.message, true);
    }
  }

  const schedulePreview = debounce(refreshPreview, 300);

  root.addEventListener("input", (event) => {
    if (
      event.target.matches("[data-config-field]") ||
      event.target.matches("[data-pattern-field]")
    ) {
      generateButton.disabled = true;
      schedulePreview();
    }
  });

  rows.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-catalog-id]");
    if (!button) return;
    button.closest("tr").remove();
    updateCatalogSelect();
    schedulePreview();
  });

  addButton.addEventListener("click", () => {
    const item = itemFor(catalogSelect.value);
    if (!item) return;
    let current;
    try {
      current = collectConfig();
    } catch (err) {
      toast(err.message, false);
      return;
    }
    current.patterns.push({
      catalog_id: item.catalog_id,
      rotation_span_deg: item.default_rotation_span_deg,
      rotation_count: item.default_rotation_count,
      repeat_count: item.default_repeat_count,
    });
    applyConfig(current);
    schedulePreview();
  });

  importButton.addEventListener("click", () => importFile.click());
  importFile.addEventListener("change", async () => {
    const file = importFile.files[0];
    if (!file) return;
    try {
      const document = JSON.parse(await file.text());
      const normalized = await api("POST", `${endpoint}/import`, { document });
      applyConfig(normalized);
      await refreshPreview();
      toast("設定をImportしました");
    } catch (err) {
      toast(`Import失敗: ${err.message}`, false);
    } finally {
      importFile.value = "";
    }
  });

  exportButton.addEventListener("click", async () => {
    try {
      await downloadApi("POST", `${endpoint}/export`, collectConfig());
    } catch (err) {
      toast(`Export失敗: ${err.message}`, false);
    }
  });

  generateButton.addEventListener("click", async () => {
    generateButton.disabled = true;
    try {
      await downloadApi("POST", `${endpoint}/generate`, collectConfig());
      toast("KiCad基板を生成しました");
    } catch (err) {
      toast(`基板生成失敗: ${err.message}`, false);
    } finally {
      await refreshPreview();
    }
  });

  async function initialize() {
    try {
      const options = await api("GET", `${endpoint}/options`);
      catalog = options.catalog;
      applyConfig(options.config);
      await refreshPreview();
    } catch (err) {
      clearPreview(err.message, true);
    }
  }

  initialize();
})();
