# spec-test-author ノート: webui-camera-calib

計画書: `memory/agents/implementation-planner/webui-camera-calib.md`
orchestrator 裁定: `memory/agents/orchestrator/webui-camera-calib.md`（要確認事項 1・2 とも採用）

## 結論（最終状態）

plan-implementer との並行編集を経て、`make format` / `make type` /
`make test-no-hardware`（1577 passed, 87 deselected）/ `make test-e2e`
（51 passed, 1613 deselected）すべてグリーン。テスト新規追加分もすべて green
（実装が計画書どおりに合流したため、当初想定していた「新仕様のテストは赤で
引き継ぐ」状態を経ずに収束した）。

## 変更したテストファイル

1. `tests/webui/jobs/test_posctrl.py`
2. `tests/webui/test_preview.py`
3. `tests/webui/routers/test_settings_api.py`
4. `tests/webui/test_config_store.py`
5. `tests/webui/routers/test_pages.py`
6. `tests/e2e/test_webui_e2e.py`
7. `tests/webui/routers/test_jobs.py`（**担当範囲外だったが必要になった追加修正**。下記参照）

`src/` は一切編集していない。

## 1. tests/webui/jobs/test_posctrl.py

- `CHECKERBOARD_PARAMS = {"square_size": 10.0}` に縮小（crop_width/height 削除）。
  `CHECKERBOARD_CROP_SIZE = (400, 400)` 定数を新設し crop 契約のピンに使う
- `checkerboard_state` fixture: `store.write_machine_settings("kurousagi",
  {"camera.crop.width": 400, "camera.crop.height": 400})` を AppState 構築前に
  実行（checkerboard.png が 400x400 のため）
- `TestCatalog.test_camera_calibration_params`: `{"square_size"}` のみ・
  default 1.5・`persisted_params == ("square_size",)` へ書き換え
  （計画書「公開インターフェース案 1」のピン）
- `TestCameraCalibrationJob.test_full_run_with_checkerboard_yields_apply_payload`:
  `loaded.crop_size == CHECKERBOARD_CROP_SIZE` を追加（crop が machine.toml
  由来である契約のピン。テスト観点「crop は machine.toml 値が使われる」）
- 新規 `test_missing_square_size_uses_default_and_succeeds`: params={} で開始
  しても SUCCEEDED まで完走する（旧: 必須空欄はエラー→挙動変更のピン。
  テスト観点「square_size 欠落での開始は default 1.5 で通る」）
- 新規 `test_removed_crop_param_is_rejected_as_unknown`: 旧 `crop_width` を
  付けて `manager.start()` すると `ValueError`（"未知のパラメータ" を含む）
  （テスト観点「削除済み crop_width を job params に付けて POST → 400」。
  manager.start は同期的に validate_params を呼ぶため ValueError で検証、
  router 層での 400 変換は既存の一般契約（未知パラメータ→400）に委ねる）

## 2. tests/webui/test_preview.py

- `TestOverlays` に `test_crosshair_crop_change_reflects_in_next_frame_without_reconnect`
  を追加。crosshair ストリームを開いたまま `store.write_machine_settings` で
  crop を 600→200 に変更し、次フレームの ROI 枠が新しい位置（フレーム
  1280x720 換算で x=340→x=540）へ再接続なしで移ることを、緑ドミナント画素の
  列近傍カウント（JPEG ノイズ許容の ±3px 帯）で検証する
  （テスト観点「crosshair ストリーム継続中の crop 変更が次フレームの ROI 枠へ反映」）
- ヘルパー `_green_dominant_count_near_column` を追加（既存 `_count_dominant`
  を列帯域に絞って再利用）

## 3. tests/webui/routers/test_settings_api.py

- `TestCameraSettingsRebuild` に `test_put_camera_crop_key_keeps_frame_hub` を
  追加（`camera.crop.*` の PUT で FrameHub が同一インスタンスのまま。
  既存 `test_put_camera_key_rebuilds_frame_hub`（camera.fps）は現状維持で
  対比になる）
- 新規 `TestCameraCropValidation.test_put_non_positive_crop_returns_400`:
  `camera.crop.width` / `camera.crop.height` の 0 / -1 → 400
  （orchestrator 裁定「異常系テスト『PUT camera.crop.width = 0 / 負値 →
  400』も書くこと」に対応。width/height 両方をパラメトライズ）
- `import pytest` が抜けていたため追加（plan-implementer からの指摘。
  `make type` の reportUndefinedVariable で検出）

## 4. tests/webui/test_config_store.py

- 新規 `TestCameraCropFields`:
  - `test_non_positive_value_raises`: width/height × {0, -1} → UnknownFieldError
  - `test_minimum_valid_value_is_accepted`: 境界値 1 は有効（write→reread で
    round-trip）。既存 `TestPadAlignMaxFailures` の境界テストパターンに準拠

## 5. tests/webui/routers/test_pages.py

`test_camera_calibration_renders_param_form_fields`（旧）を 3 テストへ分割:

- `test_camera_calibration_renders_square_size_form_with_default`:
  `name="square_size"` + `value="1.5"` が出る、`crop_width` / `crop_height`
  という job param 文字列が**出ない**こと
- `test_camera_calibration_renders_crop_auto_save_form_with_current_values`:
  `data-machine-key="camera.crop.width"` / `"camera.crop.height"` が出る、
  test-fixture 現在値 600 が value として 2 箇所（width/height）出る、
  `js/camera_calibration.js` の読込
- 新規 `test_camera_calibration_renders_saved_square_size_default`:
  `appstate.save_job_param_defaults("camera_calibration", {"square_size": 3.0})`
  後の再描画で `value="3.0"` が出る（テスト観点「param-defaults →
  次回ページ描画の default に反映」を appstate 直叩きで軽量に検証。
  既存 `test_loading_page_renders_saved_loading_defaults` 等と同じ粒度）

`test_posctrl_job_page_renders_console_preview_and_overlay_switch`
（camera_calibration を含む POSCTRL_JOB_FEATURES パラメトライズ）は計画書の
とおり変更不要（新テンプレートでも job-console / preview-pane / crosshair /
feature 名を保つ回帰確認として機能）。

## 6. tests/e2e/test_webui_e2e.py

新規 `TestCameraCalibrationCropOverRealHttp`:

- `test_page_is_served_with_crop_form_and_script`: ページ 200 +
  `data-machine-key="camera.crop.width"` + `js/camera_calibration.js`
- `test_crop_put_keeps_mjpeg_stream_open_and_reflects_in_toml`: crosshair
  ストリームを開いたまま `PUT /api/settings/machine`（camera.crop.width/height
  = 300）→ 同一レスポンスから追加フレームが取得できる（切断されない）→
  ストリームを閉じた後 `GET /api/settings/machine` と隔離 tmp の
  machine.toml の両方に 300 が反映されていることを確認
  （テスト観点「e2e: ページ 200 / crop PUT でストリーム生存 /
  PUT → GET → tmp machine.toml 反映」を 1 テストで通し確認）

両テストとも `uv run pytest tests/e2e/test_webui_e2e.py::TestCameraCalibrationCropOverRealHttp -m e2e`
で個別に green を確認済み、その後 `make test-e2e` 全体（51 passed）でも green。

## 7. tests/webui/routers/test_jobs.py（担当範囲外だったが必須の追加修正）

plan-implementer からの報告で判明: `TestCameraCalibrationApplyFlow::
test_ws_full_run_then_apply_writes_calibration_files` が旧 IF
（`crop_width` / `crop_height` を job params の POST body に含める形）の
まま残っており、crop 削除後は 400 で落ちる状態だった。計画書の WS1/WS2/WS3
テスト更新リストに明記されていなかった漏れ（Phase 4 由来の既存テスト）。

修正内容:

- POST body から `crop_width` / `crop_height` を削除し `{"square_size": 10.0}`
  のみに
- テスト冒頭で `store.write_machine_settings("kurousagi",
  {"camera.crop.width": 400, "camera.crop.height": 400})` を実行し
  checkerboard.png（400x400）に crop を合わせる（test_posctrl.py の
  `checkerboard_state` fixture と同じロジックをこのテストのフィクスチャ
  構成に合わせて直接呼ぶ形で適用）
- fixture 引数を `store: ConfigStore` 追加のうえ `configs_root: Path` は
  引き続き使用するため両方保持（後段で `configs_root / "kurousagi" /
  "machine.toml"` を読むため）
- `from webui.config_store import ConfigStore` の import を追加

## 意図的にスコープ外とした項目（テスト観点との対応）

- **`POST /api/jobs/camera_calibration/param-defaults` の型不一致（str 等）
  square_size → 黙って無視**（テスト観点「エッジケース」）:
  `filter_persisted_defaults` の汎用契約は既存 `test_jobs.py::
  TestSaveParamDefaults::test_ignores_non_persisted_and_invalid_values`
  （loading ジョブで検証済み）でカバー済みの機構であり、camera_calibration
  固有の分岐は無い。camera_calibration が `persisted_params=("square_size",)`
  を正しく宣言していることは `TestCatalog.test_camera_calibration_params`
  で別途ピン済みのため、重複するフレームワーク追試は書かなかった
  （skill `testing-strategy` の「書かない」原則: フレームワーク動作の追試）。
  必要なら test_jobs.py に camera_calibration 版を追加できるが、当初の
  担当範囲（tests/webui/routers/test_jobs.py は非対象ファイル）にも
  合わないため見送った
- **crop がフレームより大きい値の検証**: 計画書「エッジケース」節で
  明示的に「テスト追加は不要（過剰防御にしない）」と裁定済みのため未着手
- circle / copper レンダラの crop snapshot 挙動（変更なし）の回帰テスト追加
  は不要と判断（計画書「想定リスク」節: 現在どの UI にも露出していない導線）

## 3rd-party モック・内部モックの有無

なし。すべて実 `ConfigStore` / 実 `AppState` / 実 `PreviewService` /
実 `JobManager`（fake camera・test-fixture の Klipper 非リッスンポートのみ
使用）。cv2 / picamera2 / Moonraker RPC の直接モックはしていない。

## 共有 fixture への変更点

- `tests/webui/jobs/test_posctrl.py::checkerboard_state`（モジュール内
  fixture。他モジュールと共有していないので影響範囲はこのファイル内に閉じる）
  に `store.write_machine_settings` の 1 行を追加
- `tests/webui/conftest.py` / `tests/webui/jobs/conftest.py`
  （真の共有 conftest）は変更していない

## 追記（差し戻し対応）: code-reviewer should-fix #2 — camera.crop.* + 他 camera.* 混在 PUT の rebuild ピン

レビューノート: `memory/agents/code-reviewer/webui-camera-calib.md` should-fix 2。

### 追加したテスト

- `tests/webui/routers/test_settings_api.py::TestCameraSettingsRebuild::
  test_put_camera_crop_and_other_camera_key_rebuilds_frame_hub`
  （既存 3 テスト `test_put_camera_key_rebuilds_frame_hub` /
  `test_put_without_camera_key_keeps_frame_hub` /
  `test_put_camera_crop_key_keeps_frame_hub` と同じクラス・同じ
  `fake_camera_client` / `fake_camera_appstate` fixture パターンに合わせた
  独立ケースとして追加。既存 2 テストのパラメトライズ化は境界の意味が
  「fps 単独」「crop 単独」「混在」で異なり、パラメトライズだと
  期待値（rebuild する/しない）を都度分岐させる必要が出て可読性が落ちる
  ため見送り、素直な独立メソッドにした）

### テスト内容

`PUT /api/settings/machine` に `{"camera.fps": 20.0, "camera.crop.width": 300}`
（camera.crop.\* 以外の camera.\* キーである `camera.fps` と、除外対象の
`camera.crop.width` が混在）を投げ、応答 200 かつ
`fake_camera_appstate.frame_hub()` が PUT 前と**別インスタンス**（rebuild
された）であることを検証。

### 仕様根拠

- `memory/agents/implementation-planner/webui-camera-calib.md`「設計判断 b」
  「公開インターフェース案 3」: 「camera.crop.\* は再構築条件から除外する」
  ＝ camera.crop.\* 以外の camera.\* キーが 1 つでもあれば通常どおり rebuild
  する、という排他ではなく包含の境界。混在時は「crop 以外の camera.\* が
  存在する」ので rebuild が正しい仕様上の帰結
- orchestrator 裁定「要確認事項 2 採用」により、crop 単独除外の対象は
  あくまで crop キーに限定（他 camera.\* を道連れに除外はしない）

### 実装確認

`src/webui/routers/settings_api.py` の現行実装:

```python
if any(
    key.startswith("camera.") and not key.startswith("camera.crop.")
    for key in body.values
):
    state.rebuild_camera()
```

は仕様どおり（`any()` + 個別キー除外）であり、混在ケースでも `camera.fps`
キーが述語を満たすため rebuild される。追加テストは現行実装に対して green。

### ミューテーション確認（レビュー指摘の誤変種で実際に検出できるかを検証）

一時的に `any(...)` → `all(...)` に書き換えて実行し、新規追加テスト
`test_put_camera_crop_and_other_camera_key_rebuilds_frame_hub` のみが
FAIL、既存 2 テスト（fps 単独 / crop 単独）は PASS のままであることを
確認（= レビュー指摘どおり既存 2 テストだけでは `all()` 変種を素通り
させていた）。確認後 `src/webui/routers/settings_api.py` は元の内容に
復元済み（`src/` は最終的に無変更、`git diff --stat` で確認済み）。

### 検証結果

- `uv run pre-commit run --files tests/webui/routers/test_settings_api.py`:
  pass（docformatter が新規 docstring を自動整形、内容変更なし）
- `uv run pytest tests/webui/routers/test_settings_api.py -m "not hardware"`:
  23 passed（既存 22 + 新規 1）
- `@mark_hardware` / `make test` は実行していない（本タスクの制約どおり）
- `tests/webui/test_config_store.py` は触っていない（plan-implementer が
  並行修正中のため未接触。衝突なし）
