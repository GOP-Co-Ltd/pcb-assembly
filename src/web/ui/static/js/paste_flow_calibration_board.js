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
  const addCustomPadButton = document.getElementById("pfc-add-custom-pad");
  const autoPack = document.getElementById("pfc-auto-pack");
  const customPadName = document.getElementById("pfc-custom-pad-name");
  const customPadShape = document.getElementById("pfc-custom-pad-shape");
  const customPadWidth = document.getElementById("pfc-custom-pad-width");
  const customPadHeight = document.getElementById("pfc-custom-pad-height");
  const customPadRadius = document.getElementById("pfc-custom-pad-radius");
  const customPadHeightField = document.getElementById(
    "pfc-custom-pad-height-field"
  );
  const customPadRadiusField = document.getElementById(
    "pfc-custom-pad-radius-field"
  );
  const preview = document.getElementById("pfc-preview");
  const previewStatus = document.getElementById("pfc-preview-status");
  const previewSummary = document.getElementById("pfc-preview-summary");
  const importButton = document.getElementById("pfc-import");
  const importFile = document.getElementById("pfc-import-file");
  const exportButton = document.getElementById("pfc-export");
  const generateButton = document.getElementById("pfc-generate");
  const interactiveRegions = root.querySelectorAll("[data-pfc-interactive]");

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
    "pcbasm:paste-flow-calibration-board:draft:" +
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
      transpose: row.querySelector('[data-pattern-field="transpose"]').checked,
    };
  }

  function collectConfig() {
    return {
      auto_pack: autoPack.checked,
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
    text.className = "pfc-truncated-text";
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

  function transposeInput(pattern) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = pattern.transpose;
    input.disabled = config.auto_pack;
    input.dataset.patternField = "transpose";
    input.setAttribute("aria-label", "転置配置");
    input.title = config.auto_pack
      ? "自動最適配置では転置方向をサーバーが決定します"
      : "ON: 繰り返しを横、回転角を縦に配置";
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
        numberInput(pattern, "repeat_count", "1", "繰り返し数")
      );
      row.appendChild(repeatCell);
      const transposeCell = document.createElement("td");
      transposeCell.className = "pfc-transpose";
      transposeCell.appendChild(transposeInput(pattern));
      row.appendChild(transposeCell);
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
      rowsByCatalogId.set(pattern.catalog_id, row);
    }
  }

  function applyConfig(nextConfig) {
    config = nextConfig;
    autoPack.checked = config.auto_pack;
    for (const [field, input] of Object.entries(boardFields)) {
      input.value = config.board[field];
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

  function updateTransposeControls() {
    for (const input of rows.querySelectorAll(
      '[data-pattern-field="transpose"]'
    )) {
      input.disabled = autoPack.checked;
      input.title = autoPack.checked
        ? "自動最適配置では転置方向をサーバーが決定します"
        : "ON: 繰り返しを横、回転角を縦に配置";
    }
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
    for (const cell of rows.querySelectorAll(
      ".pfc-resolved-angles, .pfc-resolved-size"
    )) {
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

  function polygonElement(polygon) {
    return svgEl("polygon", {
      points: polygon.points.map((point) => `${point.x},${point.y}`).join(" "),
      class: polygon.layer === "F.Cu" ? "pfc-copper" : "pfc-paste",
    });
  }

  function renderResolvedRows(layout) {
    for (const group of layout.groups) {
      const row = rowForCatalogId(group.catalog_id);
      if (!row) continue;
      row.querySelector(".pfc-resolved-angles").textContent = group.angles_deg
        .map((angle) => `${Number(angle.toFixed(3))}°`)
        .join(", ");
      const transpose = group.transpose ? " · 転置" : "";
      row.querySelector(".pfc-resolved-size").textContent =
        `${group.bounds.width.toFixed(2)} × ${group.bounds.height.toFixed(2)}` +
        transpose;
    }
  }

  function renderLayout(layout) {
    lastLayout = layout;
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
      const fullLabel = `${group.footprint_label} / ${group.label}`;
      label.textContent = truncateLabel(fullLabel, 38);
      const title = svgEl("title", {});
      title.textContent = fullLabel;
      label.appendChild(title);
      preview.appendChild(label);
    }

    renderResolvedRows(layout);

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
      if (event.target === autoPack) updateTransposeControls();
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
