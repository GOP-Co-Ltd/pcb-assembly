"use strict";

// データセット収集の配置プレビュー。
// 総点数・撮影枚数・容量・配置は backend の /api/pasting/paste-dataset-layout が
// 算出したものをそのまま描く（ここで再導出しない）。SVG 座標は銅板 mm をそのまま
// viewBox に使うので、座標変換は描画のためだけの計算に留まる。
(() => {
  const { api, debounce, svgEl } = window.webui;

  const panel = document.getElementById("pdl-panel");
  if (!panel) return;

  const view = document.getElementById("pdl-view");
  const summaryBox = document.getElementById("pdl-summary");
  const errorBox = document.getElementById("pdl-error");
  const legendBox = document.getElementById("pdl-legend");

  // リクエストのキーは収集ジョブの ParamSpec 名と一致する（サーバが extra=forbid なので
  // レイアウトに関係しない tolerance / paste_height / paste_id は送らない）。
  const FIELDS = [
    "plate_width",
    "plate_height",
    "edge_margin",
    "cell_size",
    "cell_gap",
    "crop_size",
    "purge_cell_size",
    "volume_min",
    "volume_max",
    "volume_divisions",
    "samples_per_volume",
    "blank_count",
    "view_count",
    "view_offset",
    "shuffle_seed",
  ];
  const INT_FIELDS = new Set([
    "volume_divisions",
    "samples_per_volume",
    "blank_count",
    "view_count",
    "shuffle_seed",
  ]);
  const MARGIN_MM = 2;

  function collect() {
    const body = {};
    for (const name of FIELDS) {
      const input = document.getElementById(`param-${name}`);
      if (!input) return null;
      const value = Number(input.value);
      // 空欄・非数はサーバへ送らない（クライアント検証は UX 最小限に留める）
      if (input.value === "" || !Number.isFinite(value)) return null;
      body[name] = INT_FIELDS.has(name) ? Math.trunc(value) : value;
    }
    return body;
  }

  // 吐出量の表示（小数点 3 桁。サーバ値の書式化だけで再導出はしない）
  function volumeText(value) {
    return `${value.toFixed(3)} uL`;
  }

  // 量の index を色へ写す（表示のための変換なのでクライアント側で持つ）
  function volumeColor(index, total) {
    if (total <= 1) return "hsl(210 70% 55%)";
    const ratio = index / (total - 1);
    return `hsl(${Math.round(210 - 190 * ratio)} 70% 55%)`;
  }

  function rectEl(rect, attrs) {
    return svgEl("rect", {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      ...attrs,
    });
  }

  function titled(element, text) {
    const title = svgEl("title", {});
    title.textContent = text;
    element.appendChild(title);
    return element;
  }

  function renderLegend(data) {
    legendBox.replaceChildren();
    data.volumes_ul.forEach((volume, index) => {
      const item = document.createElement("li");
      const swatch = document.createElement("span");
      swatch.className = "pdl-swatch";
      swatch.style.background = volumeColor(index, data.volumes_ul.length);
      item.append(swatch, document.createTextNode(volumeText(volume)));
      legendBox.appendChild(item);
    });
    if (data.blanks.length > 0) {
      const item = document.createElement("li");
      const swatch = document.createElement("span");
      swatch.className = "pdl-swatch pdl-swatch-blank";
      item.append(swatch, document.createTextNode("blank（真値 0.000 uL）"));
      legendBox.appendChild(item);
    }
  }

  function renderSummary(data) {
    const seed = data.spec.shuffle_seed;
    const seedText =
      seed === 0 ? "シード 実行時に生成（図は暫定配置）" : `シード ${seed}`;
    summaryBox.textContent = [
      `対象 ${data.target_count} 点（塗布 ${data.sample_count} / blank ${data.blanks.length}）`,
      `view ${data.views_per_cell}（中心を含む）`,
      `撮影 ${data.image_count} 枚`,
      `格子 ${data.grid.length} セル / 配置可 ${data.capacity} セル`,
      seedText,
    ].join(" ・ ");
  }

  function renderView(data) {
    view.replaceChildren();
    const plate = data.plate;
    if (plate.width <= 0 || plate.height <= 0) {
      view.removeAttribute("viewBox");
      return;
    }
    view.setAttribute(
      "viewBox",
      [
        -MARGIN_MM,
        -MARGIN_MM,
        plate.width + 2 * MARGIN_MM,
        plate.height + 2 * MARGIN_MM,
      ].join(" ")
    );
    view.appendChild(rectEl(plate, { class: "pdl-plate" }));
    if (data.usable_area) {
      view.appendChild(rectEl(data.usable_area, { class: "pdl-usable" }));
    }
    for (const cell of data.grid) {
      view.appendChild(rectEl(cell, { class: "pdl-grid-cell" }));
    }
    if (data.purge_cell) {
      view.appendChild(
        titled(rectEl(data.purge_cell, { class: "pdl-purge" }), "パージ領域")
      );
    }
    for (const cell of data.cells) {
      const element = rectEl(cell.rect, {
        class: "pdl-cell",
        fill: volumeColor(cell.volume_index, data.volumes_ul.length),
      });
      view.appendChild(
        titled(
          element,
          `sample ${cell.index}（順 ${cell.order}）: ${volumeText(
            cell.commanded_volume_ul
          )}`
        )
      );
    }
    for (const blank of data.blanks) {
      view.appendChild(
        titled(
          rectEl(blank.rect, { class: "pdl-cell pdl-blank" }),
          `blank ${blank.index}: 真値 0.000 uL`
        )
      );
    }
  }

  // 応答が入れ替わって古い配置を描かないための世代番号
  let generation = 0;

  async function refresh() {
    const body = collect();
    if (body === null) return;
    const issued = ++generation;
    let data;
    try {
      data = await api("POST", "/api/pasting/paste-dataset-layout", body);
    } catch (error) {
      if (issued === generation) errorBox.textContent = error.message;
      return;
    }
    if (issued !== generation) return;
    errorBox.textContent = data.error ?? "";
    renderSummary(data);
    renderLegend(data);
    renderView(data);
  }

  const schedule = debounce(refresh, 250);
  for (const event of ["input", "change"]) {
    document.addEventListener(event, (evt) => {
      if (evt.target?.dataset?.paramType !== undefined) schedule();
    });
  }
  refresh();
})();
