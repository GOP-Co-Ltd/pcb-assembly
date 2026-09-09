"use strict";

(() => {
  const root = document.getElementById("paste-test-board");
  if (!root) return;

  const { api, debounce, downloadApi, svgEl, toast } = window.webui;
  const endpoint = "/api/pasting/paste-test-board";
  const rows = document.getElementById("ptb-pattern-rows");
  const footprintSearch = document.getElementById("ptb-footprint-search");
  const footprintResults = document.getElementById("ptb-footprint-results");
  const footprintCount = document.getElementById("ptb-footprint-count");
  const addButton = document.getElementById("ptb-add-pattern");
  const addCustomPadButton = document.getElementById("ptb-add-custom-pad");
  const customPadName = document.getElementById("ptb-custom-pad-name");
  const customPadShape = document.getElementById("ptb-custom-pad-shape");
  const customPadWidth = document.getElementById("ptb-custom-pad-width");
  const customPadHeight = document.getElementById("ptb-custom-pad-height");
  const customPadRadius = document.getElementById("ptb-custom-pad-radius");
  const customPadHeightField = document.getElementById(
    "ptb-custom-pad-height-field"
  );
  const customPadRadiusField = document.getElementById(
    "ptb-custom-pad-radius-field"
  );
  const preview = document.getElementById("ptb-preview");
  const previewStatus = document.getElementById("ptb-preview-status");
  const previewSummary = document.getElementById("ptb-preview-summary");
  const importButton = document.getElementById("ptb-import");
  const importFile = document.getElementById("ptb-import-file");
  const exportButton = document.getElementById("ptb-export");
  const generateButton = document.getElementById("ptb-generate");
  const interactiveRegions = root.querySelectorAll("[data-ptb-interactive]");

  const boardFields = {
    width_mm: document.getElementById("ptb-board-width"),
    height_mm: document.getElementById("ptb-board-height"),
    edge_margin_mm: document.getElementById("ptb-edge-margin"),
    pad_gap_mm: document.getElementById("ptb-pad-gap"),
  };
  const flowFields = {
    size_mm: document.getElementById("ptb-flow-size"),
    count: document.getElementById("ptb-flow-count"),
  };
  const purgeFields = {
    width_mm: document.getElementById("ptb-purge-width"),
    height_mm: document.getElementById("ptb-purge-height"),
  };

  const catalog = new Map();
  const rowsByCatalogId = new Map();
  const customPadShapes = new Map();
  let config = null;
  let lastLayout = null;
  let previewGeneration = 0;
  let searchGeneration = 0;
  let sortField = null;
  let sortDirection = "ascending";
  let documentKind = null;
  let documentSchemaVersion = null;
  const optionLabelMaxLength = 48;
  const draftStorageKey =
    "pcbasm:paste-test-board:draft:" +
    (document.body.dataset.machineBase || "unscoped");

  function numberFrom(input, label) {
    const value = input.valueAsNumber;
    if (!Number.isFinite(value)) throw new Error(`${label}を数値で入力してください`);
    return value;
  }

  function setEditingLocked(locked) {
    for (const region of interactiveRegions) {
      region.disabled = locked;
    }
  }

  function mergeCatalog(items) {
    for (const item of items) catalog.set(item.catalog_id, item);
  }

  function rowForCatalogId(catalogId) {
    return rowsByCatalogId.get(catalogId);
  }

  function collectPattern(pattern) {
    const row = rowForCatalogId(pattern.catalog_id);
    if (!row) throw new Error("パッドパターンの表示を復元してください");
    return {
      catalog_id: pattern.catalog_id,
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
        "繰り返し数"
      ),
    };
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
      flow_pads: {
        size_mm: numberFrom(flowFields.size_mm, "流量計測パッド寸法"),
        count: numberFrom(flowFields.count, "流量計測パッド個数"),
      },
      custom_pads: config.custom_pads,
      patterns: config.patterns.map(collectPattern),
    };
  }

  function discardDraft() {
    try {
      localStorage.removeItem(draftStorageKey);
    } catch {
      // Storage can be unavailable in privacy-restricted browser contexts.
    }
  }

  function loadDraft() {
    try {
      const stored = localStorage.getItem(draftStorageKey);
      if (!stored) return null;
      return JSON.parse(stored);
    } catch {
      discardDraft();
      return null;
    }
  }

  function saveDraft(nextConfig) {
    if (!documentKind || documentSchemaVersion === null) return;
    try {
      localStorage.setItem(
        draftStorageKey,
        JSON.stringify({
          kind: documentKind,
          schema_version: documentSchemaVersion,
          ...nextConfig,
        })
      );
    } catch {
      // Keep editing available even when localStorage is disabled or full.
    }
  }

  function saveCurrentDraft() {
    if (!config) return;
    try {
      saveDraft(collectConfig());
    } catch {
      // Preserve the last valid draft while a numeric input is incomplete.
    }
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
    text.className = "ptb-truncated-text";
    text.textContent = value;
    text.title = value;
    cell.appendChild(text);
    return cell;
  }

  function truncateLabel(value, maxLength = optionLabelMaxLength) {
    if (value.length <= maxLength) return value;
    return `${value.slice(0, maxLength - 1)}…`;
  }

  function updateFootprintResultTitle() {
    const option = footprintResults.selectedOptions[0];
    footprintResults.title = option?.dataset.fullLabel || "";
  }

  function setCustomPadShapes(items) {
    customPadShapes.clear();
    customPadShape.replaceChildren();
    for (const item of items) {
      customPadShapes.set(item.shape, item);
      const option = document.createElement("option");
      option.value = item.shape;
      option.textContent = item.label;
      customPadShape.appendChild(option);
    }
    updateCustomPadFields();
  }

  function updateCustomPadFields() {
    const shape = customPadShapes.get(customPadShape.value);
    customPadHeightField.hidden = !shape?.uses_height;
    customPadHeight.disabled = !shape?.uses_height;
    customPadRadiusField.hidden = !shape?.uses_corner_radius;
    customPadRadius.disabled = !shape?.uses_corner_radius;
  }

  function collectCustomPadDraft() {
    const shape = customPadShapes.get(customPadShape.value);
    if (!shape) throw new Error("任意パッド形状を選択してください");
    const draft = {
      name: customPadName.value,
      shape: shape.shape,
      width_mm: numberFrom(customPadWidth, "任意パッドの幅／直径"),
    };
    if (shape.uses_height) {
      draft.height_mm = numberFrom(customPadHeight, "任意パッドの高さ");
    }
    if (shape.uses_corner_radius) {
      draft.corner_radius_mm = numberFrom(
        customPadRadius,
        "任意パッドの角丸半径"
      );
    }
    return draft;
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

  function patternsForDisplay(patterns) {
    if (!sortField) return patterns;
    const sorted = [...patterns].sort((first, second) => {
      const firstItem = catalog.get(first.catalog_id);
      const secondItem = catalog.get(second.catalog_id);
      const firstValue =
        sortField === "name" ? firstItem?.footprint_label : firstItem?.label;
      const secondValue =
        sortField === "name" ? secondItem?.footprint_label : secondItem?.label;
      return (firstValue || "").localeCompare(secondValue || "", "ja", {
        numeric: true,
        sensitivity: "base",
      });
    });
    return sortDirection === "ascending" ? sorted : sorted.reverse();
  }

  function renderPatternRows(patterns) {
    rows.replaceChildren();
    rowsByCatalogId.clear();
    for (const pattern of patternsForDisplay(patterns)) {
      const item = catalog.get(pattern.catalog_id);
      if (!item) continue;
      const row = document.createElement("tr");
      row.dataset.catalogId = pattern.catalog_id;
      row.appendChild(truncatedTextCell(item.footprint_label, "ptb-name-label"));
      row.appendChild(truncatedTextCell(item.label, "ptb-pad-label"));

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
      row.appendChild(textCell("—", "ptb-resolved-angles"));
      const repeatCell = document.createElement("td");
      repeatCell.appendChild(
        numberInput(pattern, "repeat_count", "1", "繰り返し数")
      );
      row.appendChild(repeatCell);
      const actionCell = document.createElement("td");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "ptb-remove-pattern";
      remove.textContent = "削除";
      remove.dataset.removeCatalogId = pattern.catalog_id;
      remove.setAttribute(
        "aria-label",
        `${item.footprint_label} ${item.label}を削除`
      );
      actionCell.appendChild(remove);
      row.appendChild(actionCell);
      rows.appendChild(row);
      rowsByCatalogId.set(pattern.catalog_id, row);
    }
  }

  function applyConfig(nextConfig) {
    config = nextConfig;
    for (const [field, input] of Object.entries(boardFields)) {
      input.value = config.board[field];
    }
    for (const [field, input] of Object.entries(flowFields)) {
      input.value = config.flow_pads[field];
    }
    for (const [field, input] of Object.entries(purgeFields)) {
      input.value = config.purge_pad[field];
    }
    renderPatternRows(config.patterns);
  }

  function applyAndSaveConfig(nextConfig) {
    applyConfig(nextConfig);
    saveDraft(nextConfig);
  }

  function acceptResolvedConfig(nextConfig) {
    config = nextConfig;
    saveDraft(nextConfig);
  }

  function updateSortIndicators() {
    for (const button of root.querySelectorAll("[data-sort-field]")) {
      const header = button.closest("th");
      header.setAttribute(
        "aria-sort",
        button.dataset.sortField === sortField ? sortDirection : "none"
      );
    }
  }

  function clearResolvedValues() {
    for (const cell of rows.querySelectorAll(".ptb-resolved-angles")) {
      cell.textContent = "—";
    }
  }

  function clearPreview(message, error = false) {
    lastLayout = null;
    preview.replaceChildren();
    preview.removeAttribute("viewBox");
    previewSummary.textContent = "";
    previewStatus.textContent = message;
    previewStatus.classList.toggle("error", error);
    clearResolvedValues();
    generateButton.disabled = true;
  }

  function polygonElement(polygon, className = null) {
    return svgEl("polygon", {
      points: polygon.points.map((point) => `${point.x},${point.y}`).join(" "),
      class:
        className || (polygon.layer === "F.Cu" ? "ptb-copper" : "ptb-paste"),
    });
  }

  function appendPolygons(target, layout, className = null) {
    const purge = svgEl("g", { class: "ptb-preview-pad" });
    const purgeTitle = svgEl("title", {});
    purgeTitle.textContent = "PURGE";
    purge.appendChild(purgeTitle);
    for (const polygon of layout.purge_polygons) {
      purge.appendChild(polygonElement(polygon, className));
    }
    target.appendChild(purge);

    layout.flow_polygons.forEach((polygons, index) => {
      const flow = svgEl("g", { class: "ptb-preview-pad" });
      const flowTitle = svgEl("title", {});
      flowTitle.textContent = `FLOW${index + 1}`;
      flow.appendChild(flowTitle);
      for (const polygon of polygons) {
        flow.appendChild(polygonElement(polygon, className));
      }
      target.appendChild(flow);
    });

    for (const pad of layout.pads) {
      const element = svgEl("g", {
        class: "ptb-preview-pad",
        "aria-label": pad.display_name,
      });
      const title = svgEl("title", {});
      title.textContent = pad.display_name;
      element.appendChild(title);
      for (const polygon of pad.polygons) {
        element.appendChild(polygonElement(polygon, className));
      }
      target.appendChild(element);
    }
  }

  function rectanglePath(bounds) {
    return (
      `M ${bounds.x} ${bounds.y} ` +
      `h ${bounds.width} v ${bounds.height} ` +
      `h ${-bounds.width} Z`
    );
  }

  function appendOverflowLayer(layout) {
    const clipId = "ptb-placement-overflow-clip";
    const definitions = svgEl("defs", {});
    const clip = svgEl("clipPath", {
      id: clipId,
      clipPathUnits: "userSpaceOnUse",
    });
    clip.appendChild(
      svgEl("path", {
        d:
          `${rectanglePath(layout.preview_bounds)} ` +
          rectanglePath(layout.placement_area),
        "clip-rule": "evenodd",
        "fill-rule": "evenodd",
      })
    );
    definitions.appendChild(clip);
    preview.appendChild(definitions);

    const overflow = svgEl("g", {
      class: "ptb-overflow-layer",
      "clip-path": `url(#${clipId})`,
    });
    appendPolygons(overflow, layout, "ptb-overflow-shape");
    preview.appendChild(overflow);
  }

  function renderResolvedRows(layout) {
    for (const pattern of layout.patterns) {
      const row = rowForCatalogId(pattern.catalog_id);
      if (!row) continue;
      row.querySelector(".ptb-resolved-angles").textContent = pattern.angles_deg
        .map((angle) => `${Number(angle.toFixed(3))}°`)
        .join(", ");
    }
  }

  function renderLayout(layout) {
    lastLayout = layout;
    preview.replaceChildren();
    const bounds = layout.preview_bounds;
    preview.setAttribute(
      "viewBox",
      `${bounds.x} ${bounds.y} ${bounds.width} ${bounds.height}`
    );
    preview.appendChild(
      svgEl("rect", {
        x: 0,
        y: 0,
        width: layout.board.width_mm,
        height: layout.board.height_mm,
        class: "ptb-board-outline",
      })
    );

    appendPolygons(preview, layout);

    if (layout.overflow_message !== null) {
      appendOverflowLayer(layout);
    }

    renderResolvedRows(layout);
    previewSummary.textContent =
      `${layout.pad_count}パッド + purge pad` +
      (layout.flow_pads.length > 0
        ? ` + 流量計測 ${layout.flow_pads.length}パッド`
        : "");
    const hasOverflow = layout.overflow_message !== null;
    previewStatus.textContent = hasOverflow
      ? layout.overflow_message
      : "配置可能です";
    previewStatus.classList.toggle("error", hasOverflow);
    generateButton.disabled = hasOverflow;
  }

  async function refreshPreview(generation, throwOnUnavailable = false) {
    if (generation !== previewGeneration) return;
    generateButton.disabled = true;
    previewStatus.textContent = "配置を計算中…";
    previewStatus.classList.remove("error");
    let nextConfig;
    try {
      nextConfig = collectConfig();
    } catch (err) {
      if (generation !== previewGeneration) return;
      clearPreview(err.message, true);
      if (throwOnUnavailable) throw err;
      return;
    }
    saveDraft(nextConfig);
    try {
      const layout = await api("POST", `${endpoint}/preview`, nextConfig);
      if (generation !== previewGeneration) return;
      mergeCatalog(layout.catalog);
      acceptResolvedConfig(layout.config);
      renderLayout(layout);
    } catch (err) {
      if (generation !== previewGeneration) return;
      clearPreview(err.message, true);
      if (throwOnUnavailable && err.status !== 400 && err.status !== 422) throw err;
    }
  }

  async function refreshFootprintSearch(generation, throwOnUnavailable = false) {
    if (generation !== searchGeneration) return;
    addButton.disabled = true;
    try {
      const query = encodeURIComponent(footprintSearch.value.trim());
      const response = await api(
        "GET",
        `${endpoint}/footprints?query=${query}&limit=100`
      );
      if (generation !== searchGeneration) return;
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
      if (generation !== searchGeneration) return;
      footprintResults.replaceChildren();
      footprintCount.textContent = err.message;
      addButton.disabled = true;
      if (throwOnUnavailable) throw err;
    }
  }

  function requestPreview(throwOnUnavailable = false) {
    const generation = ++previewGeneration;
    return refreshPreview(generation, throwOnUnavailable);
  }

  function requestFootprintSearch(throwOnUnavailable = false) {
    const generation = ++searchGeneration;
    return refreshFootprintSearch(generation, throwOnUnavailable);
  }

  const schedulePreview = debounce((generation) => {
    void refreshPreview(generation);
  }, 300);
  const scheduleSearch = debounce((generation) => {
    void refreshFootprintSearch(generation);
  }, 250);

  function queuePreview() {
    const generation = ++previewGeneration;
    schedulePreview(generation);
  }

  function invalidatePreview() {
    ++previewGeneration;
  }

  function queueFootprintSearch() {
    const generation = ++searchGeneration;
    addButton.disabled = true;
    scheduleSearch(generation);
  }

  root.addEventListener("input", (event) => {
    if (
      event.target.matches("[data-config-field]") ||
      event.target.matches("[data-pattern-field]")
    ) {
      generateButton.disabled = true;
      saveCurrentDraft();
      queuePreview();
    }
  });

  footprintSearch.addEventListener("input", queueFootprintSearch);
  footprintResults.addEventListener("change", updateFootprintResultTitle);
  customPadShape.addEventListener("change", updateCustomPadFields);

  root.addEventListener("click", (event) => {
    const sortButton = event.target.closest("[data-sort-field]");
    if (!sortButton) return;
    const nextField = sortButton.dataset.sortField;
    if (sortField === nextField) {
      sortDirection =
        sortDirection === "ascending" ? "descending" : "ascending";
    } else {
      sortField = nextField;
      sortDirection = "ascending";
    }
    config = collectConfig();
    renderPatternRows(config.patterns);
    if (lastLayout) renderResolvedRows(lastLayout);
    updateSortIndicators();
  });

  rows.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-catalog-id]");
    if (!button) return;
    config = {
      ...config,
      patterns: config.patterns.filter(
        (pattern) => pattern.catalog_id !== button.dataset.removeCatalogId
      ),
    };
    rowsByCatalogId.delete(button.dataset.removeCatalogId);
    button.closest("tr").remove();
    saveCurrentDraft();
    queuePreview();
  });

  addButton.addEventListener("click", async () => {
    const footprintId = footprintResults.value;
    if (!footprintId) return;
    setEditingLocked(true);
    invalidatePreview();
    addButton.disabled = true;
    try {
      const response = await api("POST", `${endpoint}/patterns/from-footprint`, {
        config: collectConfig(),
        footprint_id: footprintId,
      });
      mergeCatalog(response.catalog);
      applyAndSaveConfig(response.config);
      await requestPreview();
      if (!response.added_count) {
        toast("選択した名称のパッド種は追加済みです", false);
        return;
      }
      toast(`${response.added_count}種類のパッドを追加しました`);
    } catch (err) {
      toast(`パッド追加失敗: ${err.message}`, false);
    } finally {
      addButton.disabled = footprintResults.options.length === 0;
      setEditingLocked(false);
    }
  });

  addCustomPadButton.addEventListener("click", async () => {
    setEditingLocked(true);
    invalidatePreview();
    addCustomPadButton.disabled = true;
    try {
      const response = await api("POST", `${endpoint}/custom-pads`, {
        config: collectConfig(),
        custom_pad: collectCustomPadDraft(),
      });
      mergeCatalog(response.catalog);
      applyAndSaveConfig(response.config);
      await requestPreview();
      customPadName.value = "";
      toast("任意サイズパッドを追加しました");
    } catch (err) {
      toast(`任意パッド追加失敗: ${err.message}`, false);
    } finally {
      addCustomPadButton.disabled = false;
      setEditingLocked(false);
    }
  });

  importButton.addEventListener("click", () => importFile.click());
  importFile.addEventListener("change", async () => {
    const file = importFile.files[0];
    if (!file) return;
    setEditingLocked(true);
    invalidatePreview();
    try {
      const document = JSON.parse(await file.text());
      const response = await api("POST", `${endpoint}/import`, { document });
      mergeCatalog(response.catalog);
      applyAndSaveConfig(response.config);
      await requestPreview();
      toast("設定をImportしました");
    } catch (err) {
      toast(`Import失敗: ${err.message}`, false);
    } finally {
      importFile.value = "";
      setEditingLocked(false);
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
      await requestPreview();
    }
  });

  async function initialize() {
    try {
      const options = await api("GET", `${endpoint}/options`);
      documentKind = options.kind;
      documentSchemaVersion = options.schema_version;
      mergeCatalog(options.catalog);
      setCustomPadShapes(options.custom_pad_shapes);
      let initialConfig = options.config;
      const draft = loadDraft();
      if (draft) {
        try {
          const restored = await api("POST", `${endpoint}/import`, {
            document: draft,
          });
          mergeCatalog(restored.catalog);
          initialConfig = restored.config;
        } catch (err) {
          if (err.status === 400 || err.status === 422) {
            discardDraft();
            toast(
              "保存していた設定を復元できなかったため、初期設定を使用します",
              false
            );
          } else {
            throw new Error(
              `保存していた設定の復元に失敗しました: ${err.message}`
            );
          }
        }
      }
      applyAndSaveConfig(initialConfig);
      updateSortIndicators();
      footprintCount.textContent =
        `登録済み${options.footprint_count.toLocaleString()}件を検索できます`;
      const initialization = await Promise.allSettled([
        requestPreview(true),
        requestFootprintSearch(true),
      ]);
      const failure = initialization.find(
        (result) => result.status === "rejected"
      );
      if (failure) throw failure.reason;
      setEditingLocked(false);
    } catch (err) {
      clearPreview(err.message, true);
    }
  }

  initialize();
})();
