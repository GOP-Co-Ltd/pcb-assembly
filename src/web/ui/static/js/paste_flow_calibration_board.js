"use strict";

(() => {
  const root = document.getElementById("paste-flow-calibration-board");
  if (!root) return;

  const { api, debounce, downloadApi, svgEl, toast } = window.webui;
  const endpoint = "/api/pasting/paste-flow-calibration-board";
  const rows = document.getElementById("pfc-pattern-rows");
  const footprintSearch = document.getElementById("pfc-footprint-search");
  const footprintResults = document.getElementById("pfc-footprint-results");
  const footprintCount = document.getElementById("pfc-footprint-count");
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
    pad_gap_mm: document.getElementById("pfc-pad-gap"),
  };
  const purgeFields = {
    width_mm: document.getElementById("pfc-purge-width"),
    height_mm: document.getElementById("pfc-purge-height"),
  };

  const catalog = new Map();
  let config = null;
  let previewRequest = 0;
  let searchRequest = 0;
  const optionLabelMaxLength = 48;

  function numberFrom(input, label) {
    const value = input.valueAsNumber;
    if (!Number.isFinite(value)) throw new Error(`${label}を数値で入力してください`);
    return value;
  }

  function mergeCatalog(items) {
    for (const item of items) catalog.set(item.catalog_id, item);
  }

  function itemFor(catalogId) {
    return catalog.get(catalogId);
  }

  function rowForCatalogId(catalogId) {
    return [...rows.querySelectorAll("tr")].find(
      (row) => row.dataset.catalogId === catalogId
    );
  }

  function collectConfig() {
    return {
      board: {
        width_mm: numberFrom(boardFields.width_mm, "基板幅"),
        height_mm: numberFrom(boardFields.height_mm, "基板高さ"),
        edge_margin_mm: numberFrom(boardFields.edge_margin_mm, "外周余白"),
        pad_gap_mm: numberFrom(boardFields.pad_gap_mm, "パッド間余白"),
      },
      purge_pad: {
        width_mm: numberFrom(purgeFields.width_mm, "purge pad幅"),
        height_mm: numberFrom(purgeFields.height_mm, "purge pad高さ"),
      },
      patterns: [...rows.querySelectorAll("tr")].map((row) => ({
        catalog_id: row.dataset.catalogId,
        rotation_span_deg: numberFrom(
          row.querySelector('[data-pattern-field="rotation_span_deg"]'),
          "回転範囲"
        ),
        rotation_count: numberFrom(
          row.querySelector('[data-pattern-field="rotation_count"]'),
          "回転分割数"
        ),
        repeat_count: numberFrom(
          row.querySelector('[data-pattern-field="repeat_count"]'),
          "繰り返し行数"
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

  function truncatedTextCell(value, className) {
    const cell = document.createElement("td");
    cell.className = className;
    const text = document.createElement("span");
    text.className = "pfc-truncated-text";
    text.textContent = value;
    text.title = value;
    cell.appendChild(text);
    return cell;
  }

  function truncateLabel(value) {
    if (value.length <= optionLabelMaxLength) return value;
    return `${value.slice(0, optionLabelMaxLength - 1)}…`;
  }

  function updateFootprintResultTitle() {
    const option = footprintResults.selectedOptions[0];
    footprintResults.title = option?.dataset.fullLabel || "";
  }

  function numberInput(pattern, field, step, label) {
    const input = document.createElement("input");
    input.type = "number";
    input.step = step;
    input.value = pattern[field];
    input.dataset.patternField = field;
    input.setAttribute("aria-label", label);
    return input;
  }

  function renderPatternRows(patterns) {
    rows.replaceChildren();
    for (const pattern of patterns) {
      const item = itemFor(pattern.catalog_id);
      if (!item) continue;
      const row = document.createElement("tr");
      row.dataset.catalogId = pattern.catalog_id;
      row.appendChild(truncatedTextCell(item.footprint_label, "pfc-name-label"));
      row.appendChild(truncatedTextCell(item.label, "pfc-pad-label"));

      const spanCell = document.createElement("td");
      spanCell.appendChild(
        numberInput(pattern, "rotation_span_deg", "any", "回転範囲")
      );
      row.appendChild(spanCell);
      const countCell = document.createElement("td");
      countCell.appendChild(
        numberInput(pattern, "rotation_count", "1", "回転分割数")
      );
      row.appendChild(countCell);
      row.appendChild(textCell("—", "pfc-resolved-angles"));
      const repeatCell = document.createElement("td");
      repeatCell.appendChild(
        numberInput(pattern, "repeat_count", "1", "繰り返し行数")
      );
      row.appendChild(repeatCell);
      row.appendChild(textCell("—", "pfc-resolved-size"));

      const actionCell = document.createElement("td");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "pfc-remove-pattern";
      remove.textContent = "削除";
      remove.dataset.removeCatalogId = pattern.catalog_id;
      remove.setAttribute(
        "aria-label",
        `${item.footprint_label} ${item.label}を削除`
      );
      actionCell.appendChild(remove);
      row.appendChild(actionCell);
      rows.appendChild(row);
    }
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
        for (const pad of group.pads) {
          for (const polygon of pad.polygons) {
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
      label.textContent = `${group.footprint_label} / ${group.label}`;
      preview.appendChild(label);

      const row = rowForCatalogId(group.catalog_id);
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
    previewSummary.textContent = `${layout.pad_count}パッド + purge pad`;
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
      mergeCatalog(layout.catalog);
      applyConfig(layout.config);
      renderLayout(layout);
    } catch (err) {
      if (request === previewRequest) clearPreview(err.message, true);
    }
  }

  async function refreshFootprintSearch() {
    const request = ++searchRequest;
    addButton.disabled = true;
    try {
      const query = encodeURIComponent(footprintSearch.value.trim());
      const response = await api(
        "GET",
        `${endpoint}/footprints?query=${query}&limit=100`
      );
      if (request !== searchRequest) return;
      footprintResults.replaceChildren();
      for (const item of response.results) {
        const option = document.createElement("option");
        option.value = item.footprint_id;
        option.textContent = truncateLabel(item.label);
        option.title = item.label;
        option.dataset.fullLabel = item.label;
        footprintResults.appendChild(option);
      }
      updateFootprintResultTitle();
      const qualifier = footprintSearch.value.trim() ? "検索結果" : "一般的な候補";
      footprintCount.textContent =
        `登録済み${response.footprint_count.toLocaleString()}件から` +
        `${qualifier}${response.results.length}件を表示`;
      addButton.disabled = footprintResults.options.length === 0;
    } catch (err) {
      if (request !== searchRequest) return;
      footprintResults.replaceChildren();
      footprintCount.textContent = err.message;
      addButton.disabled = true;
    }
  }

  const schedulePreview = debounce(refreshPreview, 300);
  const scheduleSearch = debounce(refreshFootprintSearch, 250);

  root.addEventListener("input", (event) => {
    if (
      event.target.matches("[data-config-field]") ||
      event.target.matches("[data-pattern-field]")
    ) {
      generateButton.disabled = true;
      schedulePreview();
    }
  });

  footprintSearch.addEventListener("input", scheduleSearch);
  footprintResults.addEventListener("change", updateFootprintResultTitle);

  rows.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-catalog-id]");
    if (!button) return;
    button.closest("tr").remove();
    schedulePreview();
  });

  addButton.addEventListener("click", async () => {
    const footprintId = footprintResults.value;
    if (!footprintId) return;
    addButton.disabled = true;
    try {
      const response = await api(
        "GET",
        `${endpoint}/pad-patterns?footprint_id=${encodeURIComponent(footprintId)}`
      );
      mergeCatalog(response.catalog);
      const current = collectConfig();
      const selected = new Set(current.patterns.map((item) => item.catalog_id));
      const additions = response.catalog.filter(
        (item) => !selected.has(item.catalog_id)
      );
      for (const item of additions) {
        current.patterns.push({
          catalog_id: item.catalog_id,
          rotation_span_deg: item.default_rotation_span_deg,
          rotation_count: item.default_rotation_count,
          repeat_count: item.default_repeat_count,
        });
      }
      if (!additions.length) {
        toast("選択した名称のパッド種は追加済みです", false);
        return;
      }
      applyConfig(current);
      await refreshPreview();
      toast(`${additions.length}種類のパッドを追加しました`);
    } catch (err) {
      toast(`パッド追加失敗: ${err.message}`, false);
    } finally {
      addButton.disabled = footprintResults.options.length === 0;
    }
  });

  importButton.addEventListener("click", () => importFile.click());
  importFile.addEventListener("change", async () => {
    const file = importFile.files[0];
    if (!file) return;
    try {
      const document = JSON.parse(await file.text());
      const response = await api("POST", `${endpoint}/import`, { document });
      mergeCatalog(response.catalog);
      applyConfig(response.config);
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
      mergeCatalog(options.catalog);
      applyConfig(options.config);
      footprintCount.textContent =
        `登録済み${options.footprint_count.toLocaleString()}件を検索できます`;
      await Promise.all([refreshPreview(), refreshFootprintSearch()]);
    } catch (err) {
      clearPreview(err.message, true);
    }
  }

  initialize();
})();
