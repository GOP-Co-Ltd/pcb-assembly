"use strict";

// データセット収集の配置プレビュー。
// ジョブフォームの入力をそのまま送り（どの項目が配置に効くかは backend が知っている）、
// 総点数・撮影枚数・容量・配置は backend の /api/pasting/calibration/dataset-layout が
// 算出したものをそのまま描く（ここで再導出しない）。SVG 座標は銅板 mm をそのまま
// viewBox に使うので、ここでの座標変換は描画のための計算だけになる。
(() => {
  const { api, debounce, jobs, svgEl } = window.webui;

  const panel = document.getElementById("pdl-panel");
  const form = document.getElementById("job-form");
  if (!panel || !form) return;

  const view = document.getElementById("pdl-view");
  const summaryBox = document.getElementById("pdl-summary");
  const errorBox = document.getElementById("pdl-error");
  const legendBox = document.getElementById("pdl-legend");

  const MARGIN_MM = 2;

  // 吐出量の表示（小数点 3 桁。サーバ値の書式化だけで再導出はしない）
  function volumeText(value) {
    return `${value.toFixed(3)} uL`;
  }

  // 量の index を色に対応付ける（表示のための変換なのでクライアント側で持つ）
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
      // 除外領域が無いので capacity は格子セル総数と一致する。二重に出さない。
      `格子 ${data.capacity} セル（すべて計測可能点）`,
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

  // 必須の数値欄が空のあいだ（入力途中）は取り直さない（クライアント検証は UX 最小限に留める）
  function incomplete() {
    return [...form.querySelectorAll('input[type="number"]')].some(
      (input) => input.value === "" && input.dataset.paramOptional !== "true"
    );
  }

  async function refresh() {
    if (incomplete()) return;
    const body = jobs.formParams(form);
    const issued = ++generation;
    let data;
    try {
      data = await api("POST", "/api/pasting/calibration/dataset-layout", body);
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

  // フォームの入力が変わるたびに取り直す（配置に効かない項目でも、同じ配置が返るだけ）
  const schedule = debounce(refresh, 250);
  for (const event of ["input", "change"]) form.addEventListener(event, schedule);
  refresh();
})();
