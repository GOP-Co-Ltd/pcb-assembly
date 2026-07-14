# board-corner-calibration-webui — plan-implementer メモ

ブランチ: `feature/20260714/board-corner-calibration-webui`（コアブランチに stack）。
計画書: `memory/agents/implementation-planner/board-corner-calibration.md`「設計要点 4. WebUI」「テスト計画」節。

## 実装内容（計画通り）

- `OverlayKind` に `"board"` 追加。`_build_renderer` の copper/board を共通ヘルパ
  `_build_copper_renderer(params: PadAlign | BoardAlign, ...)` に集約し既存 `_CopperRenderer` を再利用
  （描画は Canny エッジ緑マスクのみ、投影外形は描かない）
- `/api/preview/stream` に `blur_ksize` クエリ追加。検証はルーター側（正の奇数のみ、不正は 422）。
  copper / board 両 overlay で canny_low/high と同じ override 流儀
- feature `copper_detection` → `contour_tuning`（輪郭調整）。TABS / FEATURE_LABELS /
  FEATURE_TEMPLATES / コンテキストプロバイダ / テンプレート（git mv）を追随。互換リダイレクトなし（404）
- モード定義（label / overlay / params{key, value}×3）は `_contour_tuning_context` がサーバ解決し、
  テンプレートが `<script type="application/json" id="contour-modes">` で JS へ渡す
- preview.js を data 駆動に一般化: `input[data-param]` スライダー列挙でストリーム URL 構築、
  モード切替でフォーム値入替、保存はモードの 3 キーを 1 回の `PUT /api/settings/machine` で一括書込。
  pad_align キーのハードコードは除去。既定モードは銅箔検出（テンプレート側で初期値レンダリング）
- reference_point_setup: `_reference_point_setup_context` が `anchor_label`（CORNER_LABELS で
  日本語解決）を提供、テンプレ文言を「アンカーコーナー（{{ anchor_label }}）の基準点マーカーを十字に…」へ。
  reference_point_setup.js は無変更

## 計画外の判断

1. **blur_ksize「正常値」のルーター単体テストは書いていない。** starlette TestClient は無限 MJPEG を
   完全受信まで返らずハングする（tests/webui/routers/test_preview.py の既存 docstring の制約）。
   正常経路は service 層（tests/webui/test_preview.py の board/copper override テスト）と
   e2e（overlay=board&blur_ksize=7 の実 HTTP MJPEG デコード）で担保
2. **ブラウザ e2e のスライダー操作は focus + ArrowRight。** Playwright の `fill()` は
   `input[type=range]` に使えないため、1 ステップのキーボード操作（input イベント発火）で値を変えた
3. preview_controls.html のコメント中の copper_detection 表記を contour_tuning へ追随（表示影響なし）
4. 旧 URL `/posctrl/copper_detection` が 404 になることをテストで明示（リダイレクト不要の決定を固定）

## IF 変更通知

なし（コアブランチの公開 IF は無変更。webui 内のみ）。

## 検証結果

- `make format` パス（docformatter の自動整形 1 巡後）
- `make type` パス（pyright 0 errors）
- `uv run pytest tests/ -q -m "not hardware and not e2e" --ignore=tests/e2e` → 1551 passed
- `make test-e2e` → 46 passed（live_server fixture。常駐サーバー起動なし）
- ハードウェアテストは未実行（実行禁止）
