# board-corner-calibration（コアブランチ）

## 計画外の判断ログ

- `fit_affine_transform` に「対応点数の不一致」の ValueError も追加（3点未満チェックの前提として自然な検証。計画は3点未満のみ明記）
- `_corner_order` は module-level 関数、矩形順は `_RECT_ORDER = (TL, TR, BR, BL)` の巡回でアンカー先頭（例: BL 起点 → BL, TL, TR, BR）
- `BoardTransformMeasurer` は `posctrl/__init__.py` の既存 export を維持。`fit_affine_transform` は `pcbasm.posctrl.board` からの import を想定し `__init__` には追加していない（計画に明記なし、外科的変更優先）
- jobs/posctrl.py の reference_point_setup は docstring 変更 + 「アンカーコーナー: {corner.value}」のログ 1 行追加のみ。Apply ラベルには元々 "top left" が無いため不変。CORNER_LABELS は import 循環回避のため使わず corner.value を表示
- settings.html の corner select は `corner_labels` を settings_page コンテキスト（routers/pages.py）で渡す方式。macro は settings.html 内でのみ使用されていることを確認済み

## 他implementerへのIF変更通知（並列時）

- IF 変更なし。確定シグネチャどおり実装（Corner/CORNERS/ReferencePoint/BoardAlign/Machine.board_align/fit_affine_transform/BoardTransformMeasurer.measure(marker_pos)）
- webui 追加分: `SettingValueType` に `"corner"`、`FieldSpec("reference_point.corner"/"reference_point.offset")`、`board_align.*` 8 項目、`SECTION_LABELS["board_align"]="基板コーナー照合"`、`CORNER_LABELS`（routers/common.py、後続 WebUI ブランチと共用）

## 既知の制約・残課題

- 輪郭調整ページ（copper_detection→contour_tuning リネーム、OverlayKind "board"、blur_ksize クエリ）は後続 WebUI ブランチの範囲。templates/posctrl/ の文言も未変更（計画どおり）
- machine_minimal.toml は意図的に `[board_align]` 節なし（既定値パスの検証素材）
- e2e（make test-e2e）はコアブランチでは未実行（WebUI ブランチで検証予定）。tests/e2e/test_browser_ui.py は spec-test-author が `reference_point.offset` へ更新済み

## 検証結果

- make format: pass
- make type: pass（0 errors。spec-test-author の新テスト合流後も 0）
- pytest -m "not hardware and not e2e" --ignore=tests/e2e: **1542 passed / 0 failed**（spec-test-author の新テスト test_board.py / test_config.py / test_config_store.py / test_settings_api.py / test_pages.py 含む）
- スモーク: 5 TOML すべて Machine 読込 OK（corner/offset/board_align、minimal は既定値）。fit_affine_transform は既知アフィン（回転+スケール+並進、5点）を残差 <1e-9 で復元、3点未満/点数不一致で ValueError
- ハードウェアテスト・make test-e2e は未実行（実行禁止）
