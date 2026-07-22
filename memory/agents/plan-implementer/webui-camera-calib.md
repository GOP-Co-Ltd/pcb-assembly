# plan-implementer ノート: webui-camera-calib

計画書: `memory/agents/implementation-planner/webui-camera-calib.md`
担当範囲: src/ のみ（WS1〜WS3）。tests/ は一切編集していない。

## 実装内容（計画どおり）

- WS1 `src/webui/jobs/posctrl.py`: camera_calibration の ParamSpec を
  `square_size`（default 1.5, unit mm）のみへ縮小、`persisted_params=("square_size",)`
  追加。`_run_camera_calibration` は `crop_size = ctx.machine.camera.crop.size` を読む
  （旧 `ctx.params["crop_width"/"crop_height"]` を削除）。
- WS2-1 `src/webui/preview.py`: `_crop_size()` を追加し、crosshair 分岐を
  `lambda image: draw_overlay(image, self._crop_size())` へ変更（フレーム毎に
  machine.toml を読む）。circle / copper は snapshot のまま（計画どおり不変）。
  `_build_renderer` の docstring に crosshair 例外を追記。
- WS2-2 `src/webui/routers/settings_api.py`: `put_machine_settings` の rebuild 条件を
  `key.startswith("camera.") and not key.startswith("camera.crop.")` へ変更。
- WS2-3 `src/webui/config_store.py`: `_coerce` の `"int"` 分岐に
  `camera.crop.width` / `camera.crop.height` の 1 以上検証を追加
  （`max_failures` の前例と同形）。
- WS3-1 `src/webui/routers/pages.py`: `_CAMERA_CROP_KEYS`、
  `_camera_calibration_context`（`crop_fields` を返す）を追加。
  `_FEATURE_CONTEXT["camera_calibration"]` に登録。
  `FEATURE_TEMPLATES[("posctrl", "camera_calibration")]` を
  `"posctrl/camera_calibration.html"` に変更。`_JOB_TEMPLATES` へ同テンプレートを追加
  （job_name / param_specs 注入を維持）。
- WS3-2 `src/webui/templates/posctrl/camera_calibration.html`（新規）:
  `posctrl/job.html` 相当（preview_pane + preview_controls + job_form + job_console）
  に、preview_controls と job_form の間へ crop 即保存 fieldset を追加。
  各 input は `data-machine-key="{{ field.key }}"`（計画 IF 案 6 のとおり）。
  scripts ブロックへ `js/camera_calibration.js` を追加読み込み。
- WS3-3 `src/webui/static/js/camera_calibration.js`（新規）:
  `bindCropAutoSave`（`[data-machine-key]` input を debounce 400ms で集約し
  `PUT /api/settings/machine`、成功時はレスポンス fields で input を書き戻し、
  失敗は toast）と `bindSquareSizePersist`（`#job-form [name="square_size"]` の
  input を debounce 400ms で `POST /api/jobs/camera_calibration/param-defaults`、
  失敗は握りつぶし。loading_controls.js:101-112 のパターン踏襲）。
  `window.webui` に `debounce` が既存（app.js）のためそれを利用、新規ヘルパ追加なし。
- `configs/kurousagi/machine.toml` の crop 600→300 は worktree 取り込み済みのまま
  （触っていない）。

## 検証結果

- `make format` / `make type`: グリーン（自分の変更分について。tests/ 側の
  一時的な状態については下記「他エージェントの進行中変更」参照）。
- 手動確認（TestClient・直接呼び出し、テストファイルは触らず）:
  - `GET /posctrl/camera_calibration`: square_size フォーム default 1.5、
    crop_width/crop_height の job param 無し、`data-machine-key="camera.crop.width"`
    ほかが fixture 現在値（600）で出力、`js/camera_calibration.js` 読み込みを確認。
  - `PUT /api/settings/machine {"camera.crop.width": 0}` → 400
    `"camera.crop.width: 1以上の値が必要です"`。
  - `PUT /api/settings/machine {"camera.crop.width": 200}` → 200、応答 fields に反映。
  - `PreviewService._crop_size()` が `store.write_machine_settings` 直後の値を
    即座に読み直すことを確認（ストリーム再接続なしで反映される契約の裏付け）。
- 参考実行 `uv run pytest tests/webui -m "not hardware"`: 実装時点で
  spec-test-author が並行更新中の内容と突き合わせ、新規テスト
  （test_preview.py::TestOverlays::test_crosshair_crop_change_...,
  test_config_store.py::TestCameraCropFields, test_settings_api.py の
  camera.crop 関連）は全てグリーン。旧 IF 依存の残テストと未更新テストの状況は
  下記「spec-test-author への報告」のとおり。

## 計画からの逸脱（無し）・検討したが採用しなかった案

- `src/webui/static/js/settings.js` に既存の汎用 `form[data-machine-settings]`
  自動保存機構（debounce PUT + toast、`paste_solder.html` の
  auto_threshold_fields で使用中）がある。これは crop フォームの要件と機能的に
  ほぼ重複する。しかし計画書 IF 案 6/7 は `data-machine-key` 属性 + 専用
  `camera_calibration.js`（`bindCropAutoSave`）を明示しており、spec-test-author
  側のテスト観点にも `data-machine-key` 属性の存在が明記されているため、
  計画どおり専用実装とした。**別タスク提案**: 将来的に `settings.js` の
  `data-machine-settings` フォーム方式へ寄せて `camera_calibration.js` の
  `bindCropAutoSave` を削減できる余地がある（シンプルさの観点で優位）が、
  今回はスコープ外として着手していない。

## spec-test-author への報告（送信済み）

1. `tests/webui/routers/test_settings_api.py` が `@pytest.mark.parametrize` を
   使うが `import pytest` が無く、`make type` が reportUndefinedVariable で失敗
   （275-276 行目）。
2. 計画書のテスト更新リストに `tests/webui/routers/test_jobs.py::
   TestCameraCalibrationApplyFlow::test_ws_full_run_then_apply_writes_calibration_files`
   が入っておらず、旧 IF（crop_width/crop_height を job params に渡す）のままで
   現状 400 で失敗する。`checkerboard_camera_client` fixture（conftest 共有）は
   crop=600 のままのため、当該テスト内で `store.write_machine_settings("kurousagi",
   {"camera.crop.width": 400, "camera.crop.height": 400})` を行い、POST body から
   crop_width/crop_height を外す必要がある旨を通知した。

いずれも tests/ 側の修正のため自分では手を入れていない。

## code-reviewer 差し戻し対応（must-fix 1: torn read 競合）

対象: `src/webui/config_store.py::ConfigStore.write_machine_settings`。
reviewer 指摘: `path.write_text(...)` の非アトミック上書きと `PreviewService._crop_size()`
のフレーム毎 `Machine(path)` 読みが競合し、書き込み中の読みが `KeyError` を起こして
MJPEG ストリームが落ちる（要件4「再接続なし」への反例経路）。

### 修正内容

`write_machine_settings` を atomic replace 方式へ変更。`board_settings.py::_write_doc`
に既存の同型パターン（`tempfile.NamedTemporaryFile(dir=path.parent, ...)` →
書き込み → `tmp_path.replace(path)`、例外時は tmp を unlink して re-raise）があったため
それに揃えた。フレーム毎読みの設計（`_crop_size`）自体は変更していない。
docstring に atomic replace により torn read が発生しない旨を追記。

### 検証結果

- `make format` / `make type`: pass
- `uv run pytest tests/webui/test_config_store.py tests/webui/test_preview.py -m "not hardware"`:
  77 passed、回帰なし
- 競合再現スクリプト（reviewer 相当構成: reader 1 スレッド 15Hz + writer 1 スレッド
  `ConfigStore.write_machine_settings` 実呼び出し 5Hz、30 秒、scratchpad 上の
  configs コピーに対して実行・スクリプトは実行後削除）
  - before（reviewer 実測、レビューノート記載）: reads=431 / writes=144 / errors=24
    （約17%が torn read で KeyError）
  - after（本修正後、同条件で再実行）: reads=429 / writes=143 / **errors=0**
  - 参考として reader 3 並列・30秒でも実行: reads=1274 / writes=142 / errors=0
- `@mark_hardware` / `make test`: 実行していない（実機確認はユーザー）

### 範囲

should-fix（混在 PUT の rebuild テスト追加）・nit（settings.js 統合、debounce 中間値上書き）
は本対応の範囲外（must-fix のみ修正の指示のため着手せず）。

## 追加指示対応: crop 編集 UI を settings ページへ統一（MR !137 提出後）

ユーザー追加指示「crop 値の編集 UI を settings ページのみに置く形に統一」を受け、
orchestrator 指示どおり camera_calibration ページの crop 即保存フォームを撤去した。
奇しくも上記「検討したが採用しなかった案」（settings.js の汎用フォームへ寄せる案）と
同方向の結論に、ユーザー判断で着地した形になる。

### 変更内容

- `src/webui/templates/posctrl/camera_calibration.html`: crop fieldset
  （`#camera-crop-settings`、`data-machine-key` input 群）を削除。
  テンプレート自体・`js/camera_calibration.js` の読み込みは維持（square_size
  即保存に必要）。結果として `posctrl/job.html` と同じ main 構成（preview_pane +
  preview_controls + job_form + job_console）に戻った。
- `src/webui/static/js/camera_calibration.js`: `bindCropAutoSave` を削除し
  `bindSquareSizePersist` のみ残す。未使用になった `toast` の分割代入も削除
  （orphan 除去）。先頭コメントを「square_size の入力時復元保存 + crop は
  settings ページへ統一済み」に更新。
- `src/webui/routers/pages.py`: `_CAMERA_CROP_KEYS` 定数、
  `_camera_calibration_context` 関数、`_FEATURE_CONTEXT["camera_calibration"]`
  エントリを削除（orphan なし。`grep` で `_CAMERA_CROP_KEYS` /
  `_camera_calibration_context` / `crop_fields` / `data-machine-key` の残存
  無しを確認）。

### 変更していないもの（維持指示どおり）

- `preview.py` のフレーム毎 crop 読み（`_crop_size`）: settings ページ経由の
  保存でも crosshair オーバーレイへ即時反映される前提として維持。
- `settings_api.py` の `camera.crop.*` rebuild 除外。
- `config_store.py` の 1 以上検証・atomic 書込（上セクションの reviewer 対応）。
- `square_size` の `default=1.5` + `persisted_params` + `bindSquareSizePersist`。
- settings ページ側: 変更不要（`data-machine-settings` 汎用フォームが
  `MACHINE_FIELDS` 全件＝`camera.crop.width`/`camera.crop.height` 含む を
  既に描画していることを実際に `GET /settings` して確認した）。

### 検証結果

- `make format` / `make type`: グリーン。
- 手動確認（TestClient）: `GET /posctrl/camera_calibration` に
  `camera-crop-settings` / `data-machine-key` / `square_size` フォーム欠落が
  無いこと、`js/camera_calibration.js` 読み込みは維持、を確認。
  `GET /settings` に `camera.crop.width` が含まれることを確認。
- `uv run pytest tests/webui -m "not hardware"`: 631 passed, 20 deselected
  （spec-test-author が並行して `tests/webui/routers/test_pages.py` を
  同方向に更新済みで、突き合わせ済み。テストは自分では編集していない）。
- `</content>` 等の混入なし（`grep -rn "</content>" src/`）。
- `make test` / `@mark_hardware`: 実行していない。

### コミット

orchestrator が行う方針のため、本対応でもコミットはしていない。
