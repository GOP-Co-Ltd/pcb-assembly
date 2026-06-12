# WebUI Phase 2: FrameHub + MJPEG preview の仕様テスト

計画書 `memory/agents/implementation-planner/webui-phase2.md` の公開 IF 契約と
`docs/webui/specification.md` §3/§5/§7/§9/§11/§12 Phase 2 に対して記述。
plan-implementer と並列作業（着手時点で src 側はほぼ実装済みだった）。

## 書いたテスト一覧

### tests/pcbasm/hal/test_framehub.py（unit + integration-hardware）

- TestFrameHubLifecycle
  - test_start_makes_latest_return_frame_and_running_true — 正常系
  - test_latest_after_stop_returns_last_frame — 正常系（停止後も最後のフレーム）
  - test_start_is_idempotent / test_stop_is_idempotent — エッジ（冪等）
  - test_restart_after_stop_resumes_existing_subscriber — エッジ（stop→start 再開）
  - test_stop_does_not_close_camera — エッジ
  - test_real_csi_camera_smoke — `@mark_hardware` + `skip_if_no_csi_camera`（**ユーザー実行**）
- TestFrameSource
  - test_two_subscribers_receive_the_same_frame — 正常系（複数 subscriber、奪い合いなし）
  - test_capture_waits_for_newer_frame_and_never_repeats — 正常系（新フレーム待ち）
  - test_resolution_and_info_delegate_to_camera — 正常系
  - test_frame_source_feeds_circle_detector_statistics — 正常系（Camera ABC 注入で
    detect_with_statistics 30 フレーム消費が成立）
- TestErrors
  - test_latest_before_start_raises_runtime_error / test_capture_before_start_… — 異常系
  - test_capture_after_stop_raises_runtime_error_once_drained — 異常系
  - test_latest_raises_timeout_error_when_no_frame_arrives — 異常系
  - test_capture_error_is_reraised_repeatedly — 異常系（例外保持・繰り返し再送出・running False）
  - test_restart_clears_held_error — エッジ（start でエラークリア）

### tests/webui/

- test_fake_camera.py::TestFixedImageCamera — サイズ・同一インスタンス・resolution/info・
  FileNotFoundError。fps ペーシングのタイミングアサートは計画どおり書いていない
- test_settings.py::TestFakeCameraSettings — fake_camera 既定 False / env 上書き（追記）
- test_state.py::TestCameraLifecycle — frame_hub 遅延構築 + キャッシュ / rebuild_camera /
  select_machine 再構築 / close / 未構築 no-op / 構築失敗伝播（追記）
- test_preview.py — TestMjpegStream（multipart パート形式 + JPEG 復号、参照カウント、
  2 ストリーム並行、構築失敗伝播）/ TestOverlays（crosshair 緑・copper 緑・circle 赤の
  描画スモーク）/ TestOverrideSlot（優先配信 + ttl 失効）/ TestSnapshot（JPEG + hub 停止復帰）
- routers/test_preview.py — stream の Content-Type / boundary / 復号、canny クエリ受理、
  422 / 503、/api/state の preview_clients 増減
- routers/test_pages.py::TestPreviewPages — camera_preview / copper_detection 専用ページ、
  他 feature のプレースホルダ維持（追記）
- routers/test_settings_api.py::TestCameraSettingsRebuild — camera.* PUT で hub 再構築、
  非 camera キーでは維持（追記）
- routers/test_machine.py — /api/state に preview_clients フィールド追加をピン（1 行追記）

### conftest / アセット

- tests/webui/conftest.py に追加: `FAKE_CAMERA_IMAGE`、`fake_camera_settings`（webui_settings
  の attrs.evolve。既存 `webui_settings` は不変なので Phase 1 テストに影響なし）、
  `fake_camera_app/client/appstate`、MJPEG ヘルパ `jpeg_payload()` / `decode_jpeg()`
- `data/testing/webui/fake_camera.png` を生成・コミット対象に追加（1280x720。明るい無彩色の
  基板風背景 + 中心から (+45,-30) ずらした直径 120px の暗色円 + 矩形/直線パターン）。
  CircleDetector(pixel_per_mm=40, target_diameter=3.0, crop 600x600) で検出成立、
  CopperEdgeDetector(81/192/5) でエッジ約 4000px（crop 内）を実測確認。生成スクリプトは
  使い捨て（/tmp）でコミットしない
- tests/helpers.py は変更なし（FakeCamera は後方互換のまま流用。ゲート付き/例外カメラは
  test_framehub.py モジュール内に定義 — 計画書の指示どおり）

## 仕様根拠の対応表（要点）

- 複数 subscriber 同一フレーム / 新フレーム待ち / エラー伝播 / 冪等 → 計画書「テスト観点
  tests/pcbasm/hal/test_framehub.py」節 + spec §3
- 停止中の待機者 RuntimeError・latest の停止後返却・シーケンス非リセット → 計画書
  「設計判断」表 2〜3 行目
- MJPEG パート形式（boundary 行 + ヘッダ + JPEG + CRLF）/ 参照カウント / オーバーライド
  スロット / snapshot → 計画書「src/webui/preview.py」節 + spec §7
- 422 / 503 / preview_clients → 計画書「src/webui/routers/preview.py」「既存ルーターへの変更」節 + spec §9
- オーバーレイ色: crosshair=緑（draw_overlay）、circle=赤（draw_detected_circle 相当）、
  copper=緑（display[edges>0]=(0,255,0)）。JPEG q80 ラウンドトリップ後の「チャネル差 > 60」
  マスクで生画像の誤検出 0 を実測した上で閾値を決定（座標の厳密検証はしない）

## 期待される失敗

なし。並列の plan-implementer が先行しており、最終実行で
`uv run pytest tests/pcbasm/hal/test_framehub.py tests/webui -m "not hardware" -q`
→ **144 passed, 6 deselected（hardware）**、`uv run pyright tests/` → 0 errors。
`@mark_hardware`（test_real_csi_camera_smoke ほか）はユーザー実行待ち。

## 実装側に求める修正

なし（テスト失敗による修正要求はない）。確認済みの注意点のみ:

- **starlette TestClient は無限 MJPEG を読めない**（この環境の TestClient は
  `portal.call(app, ...)` でレスポンス完了まで待つため、`client.stream()` の `__enter__` で
  ハングする — 計画書リスク 6 の「N パート読んで break」では回避不能だった）。
  ストリーム実読み込みと preview_clients の増減は **実 uvicorn（エフェメラルポート）+ httpx**
  の `live_server_url` fixture で検証する方式に変更した。snapshot / 422 / 503 は TestClient のまま
- camera_preview ページの検証マーカーは `/api/preview/stream` リテラルではなく
  `preview-pane` / `preview.js` / `crosshair` にした（計画書 templates/static 節自身が
  「preview.js が <img src> を動的装着」と規定しており、HTML に URL が出ない実装が正）
- test_framehub の「stop→start でシーケンス非リセット」は公開 API からは
  「停止前からの FrameSource が再開後も capture 続行できる」ことまでを検証
  （シーケンス値そのものは非公開のため厳密なリセット検出はしていない）

## tests/helpers.py への追加

なし。fake は自前 Camera ABC の実装のみ（GatedCamera / FailingCamera / FlakyCamera を
test_framehub.py 内に定義）。3rd-party モック・time.sleep モックは不使用
（オーバーライド ttl 失効は ttl=0.2 注入 + 実 sleep 0.25s で検証）。

## 検証結果

- make format: pass
- uv run pytest tests/pcbasm/hal/test_framehub.py tests/webui -m "not hardware" -q: 144 passed
- uv run pyright tests/: 0 errors
