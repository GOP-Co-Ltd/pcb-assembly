# WebUI カメラキャリブレーションページ改修

作業ディレクトリ: `/home/gop/pcb-assembly/.claude/worktrees/feature+20260722+webui-camera-calib`
（ブランチ `feature/20260722/webui-camera-calib`。以下のパスはすべて worktree 相対。
**メインリポ絶対パス `/home/gop/pcb-assembly/src/...` への Write/Edit は厳禁**）

## 要確認事項

いずれも非ブロッキング（計画の既定で実装を進めてよい。却下時の影響は局所的）。

1. **square_size の入力途中即保存**: 「ページの入力値を保存する」を、実行を待たない
   input 時即保存（loading ページと同方式の debounce POST `param-defaults`）と解釈して採用した。
   「実行時に保存されれば十分」なら JS の該当バインド（後述 `bindSquareSizePersist`）を省略できる。
2. **camera.crop.\* の正値バリデーション追加**: 要件外だが、change 即自動保存 UI では
   0 や負値が machine.toml に書かれる事故が起きやすい。`config_store._coerce` の既存
   per-key 検証（`max_failures >= 0` 等）の前例に従い「1 以上の int」を追加する。
   不要なら該当ステップ（WS2-3）を落とすだけでよい。

## 概要

posctrl タブの camera_calibration ページを改修する。square_size のデフォルトを 1.5mm に
して前回入力値を復元可能にし、クロップ幅・高さをジョブパラメータから machine.toml
`[camera.crop]` 連動の即保存フォームへ移し、クロップ変更が十字線オーバーレイの ROI 枠へ
ストリーム再接続なしで即時反映されるようにする。

## 設計判断（依頼時の論点 a〜d への立場）

- **a. クロップの真実の所在 — machine.toml に一本化**。`crop_width` / `crop_height` の
  ParamSpec を削除し、ジョブ実行時は `ctx.machine.camera.crop.size` を読む
  （`ctx.machine` はジョブ開始時に 1 回ロード済みの Machine。既存例:
  `src/webui/jobs/posctrl.py` の `_reference_point_frame` / `_stream_labeled_frames`）。
  `job_param_defaults` へは crop を保存しない（二重管理の回避。これまで
  camera_calibration は `persisted_params` 未設定だったため保存済みの旧値も存在せず、移行不要）。
- **b. 即時反映 — サーバレンダラがフレーム毎に crop を読む**。
  `PreviewService._build_renderer` の crosshair 分岐を「ストリーム開始時に 1 回読む」から
  「フレーム毎に `state.machine().camera.crop.size` を読む」closure に変える。
  併せて `PUT /api/settings/machine` の FrameHub 再構築条件から `camera.crop.*` を除外する
  （crop はデバイスパラメータではなく `AppState._build_camera` が参照しない。現行は
  camera.\* 全キーで rebuild → 全ストリーム切断 → 再接続となり「再接続なし」の要件に反する）。
  JS はレンダリングに関与しない（thin-wrapper: fetch と DOM のみ）。
- **c. 保存タイミング — change/input + debounce 400ms で即 PUT（保存ボタンなし）**。
  失敗時は toast、成功時は PUT レスポンス（サーバ truth）で input 値を更新 + toast。
  Canny 保存（`preview.js:71-86`）のボタン方式は「変更時に即時反映」の要件に合わないため不採用。
- **d. square_size — `default=1.5` + `persisted_params=("square_size",)`**。
  保存値があれば `_param_specs_with_saved_defaults`（`src/webui/routers/pages.py:211`）が
  default を上書きするため、1.5 は初回（未保存時）のみ表示される。既存機構のままで整合する。

## 公開インターフェース案

### 1. `src/webui/jobs/posctrl.py` — ジョブ定義

```python
# register_posctrl_jobs 内の camera_calibration 定義
JobDefinition(
    name="camera_calibration",
    label="カメラキャリブレーション",
    tab="posctrl",
    run=_run_camera_calibration,
    params=(
        ParamSpec("square_size", "チェッカーボードの1マス", "float", 1.5, unit="mm"),
    ),
    persisted_params=("square_size",),
    uses_machine=True,
)
```

`_run_camera_calibration(ctx: JobContext) -> JobResult` のシグネチャは不変。内部のみ変更:

```python
square_size = float(ctx.params["square_size"])
crop_size = ctx.machine.camera.crop.size   # 旧: ctx.params["crop_width"/"crop_height"]
calibrator = CheckerboardCalibrator(square_size, crop_size)
```

### 2. `src/webui/preview.py` — crosshair レンダラの動的 crop

```python
class PreviewService:
    def _crop_size(self) -> tuple[int, int]:
        """machine.toml の [camera.crop] を都度読む（crop 変更の即時反映用）."""
        return self._state.machine().camera.crop.size
```

`_build_renderer` の crosshair 分岐のみ変更（シグネチャ不変）:

```python
case "crosshair":
    return lambda image: draw_overlay(image, self._crop_size())
```

circle / copper レンダラはストリーム開始時 snapshot のまま（変更しない。理由はトレードオフ節）。
docstring「ストリーム開始時に machine 設定を 1 回読んで…」（`preview.py:256`）は
crosshair の例外を追記する。

### 3. `src/webui/routers/settings_api.py` — rebuild 条件の変更

`put_machine_settings` のシグネチャ不変。再構築条件のみ:

```python
# crop はレンダラが毎フレーム読むためデバイス再構築は不要（ストリームを切断しない）
if any(
    key.startswith("camera.") and not key.startswith("camera.crop.")
    for key in body.values
):
    state.rebuild_camera()
```

### 4. `src/webui/config_store.py` — crop の正値検証（要確認事項 2）

`_coerce` の `case "int":` 分岐に per-key 検証を追加（既存 `max_failures` と同形）:

```python
if spec.key in ("camera.crop.width", "camera.crop.height") and value < 1:
    raise UnknownFieldError(f"{spec.key}: 1以上の値が必要です")
```

### 5. `src/webui/routers/pages.py` — ページコンテキスト / テンプレート登録

```python
# クロップ設定の即保存フォームに載せるキー（_PASTE_AUTO_THRESHOLD_KEYS と同形）
_CAMERA_CROP_KEYS = ("camera.crop.width", "camera.crop.height")

def _camera_calibration_context(state: AppState, store: ConfigStore) -> dict[str, Any]:
    """Camera_calibration ページ専用コンテキスト（クロップ設定の現在値）."""
    return {
        "crop_fields": [
            field
            for field in machine_settings_fields(store, state.selected_machine)
            if field.key in _CAMERA_CROP_KEYS
        ]
    }
```

- `_FEATURE_CONTEXT` に `"camera_calibration": _camera_calibration_context` を追加
- `FEATURE_TEMPLATES[("posctrl", "camera_calibration")]` を `"posctrl/camera_calibration.html"` へ変更
- `_JOB_TEMPLATES` に `"posctrl/camera_calibration.html"` を追加
  （job_name / param_specs 注入を維持。board_tour / orthogonality_test は `posctrl/job.html` のまま）

### 6. `src/webui/templates/posctrl/camera_calibration.html`（新規）

`posctrl/job.html` をベースに、preview_controls と job_form の間へクロップ設定
fieldset を挿む。crop_fields（`SettingsField`: key / label / unit / value）をループし:

```html
<fieldset class="preview-controls" id="camera-crop-settings">
  <legend>クロップ設定（machine.toml と連動・変更即保存）</legend>
  {% for field in crop_fields %}
  <label for="crop-{{ loop.index }}">{{ field.label }} [{{ field.unit }}]</label>
  <input type="number" id="crop-{{ loop.index }}" step="1" min="1"
         data-machine-key="{{ field.key }}"
         value="{{ field.value if field.value is not none else '' }}">
  {% endfor %}
</fieldset>
```

scripts ブロックで `js/preview.js` に加え `js/camera_calibration.js`（新規）を読み込む。

### 7. `src/webui/static/js/camera_calibration.js`（新規）

薄いクライアント（fetch / DOM のみ。ドメイン検証はサーバ任せ、空欄・数値パース可否のみ）:

```js
"use strict";
// カメラキャリブレーションページ: クロップの即保存 + square_size の入力時復元保存。
(() => {
  const { toast, api, debounce } = window.webui;
  const DEBOUNCE_MS = 400;

  function bindCropAutoSave() {
    // [data-machine-key] input の input イベント → debounce →
    // 非空・Number.isFinite の値だけ集めて PUT /api/settings/machine {values}
    // 成功: レスポンス fields から該当キーの値を input へ書き戻し + toast
    // 失敗: toast(err.message, false)
  }

  function bindSquareSizePersist() {
    // #job-form [name="square_size"] の input → debounce →
    // POST /api/jobs/<data-job-name>/param-defaults {values:{square_size:Number}}
    // （loading_controls.js:101-112 と同パターン。失敗は握りつぶし）
  }

  bindCropAutoSave();
  bindSquareSizePersist();
})();
```

### 8. `configs/kurousagi/machine.toml`

crop 600→300 の変更は worktree に取り込み済み（未コミット）。このブランチのコミットに含める。

## 実装ステップ

WS1〜WS3 は触るファイルが disjoint なので **並列実装可能**。WS4 は合流後。

### WS1: ジョブ定義（バックエンド）

1. `src/webui/jobs/posctrl.py`: camera_calibration の ParamSpec を square_size のみ
   （default 1.5）へ変更、`persisted_params=("square_size",)` 追加、
   `_run_camera_calibration` で `crop_size = ctx.machine.camera.crop.size`
2. `tests/webui/jobs/test_posctrl.py` 更新:
   - `CHECKERBOARD_PARAMS = {"square_size": 10.0}` に縮小（`:54`）
   - checkerboard 系 fixture（`checkerboard_state` `:66-72`）で、テスト用 tmp コピーの
     machine.toml へ `store.write_machine_settings("kurousagi", {"camera.crop.width": 400,
     "camera.crop.height": 400})` を書いてから AppState を作る
     （素材 checkerboard.png は 400x400。fixture 既定 600 でははみ出す。コメント `:51-53` も更新）
   - `test_camera_calibration_params`（`:131-140`）: `{"square_size"}` のみ・default 1.5・
     `persisted_params == ("square_size",)` へ書き換え
   - `test_full_run_...`（`:187`）に `loaded.crop_size == (400, 400)` を追加
     （crop が machine.toml 由来である契約のピン）
   - `test_undetectable_image_...`（`:244`）: params は square_size のみになる
     （既定 manager は fake_camera.png 1280x720 + fixture crop 600 のままで成立）
   - `@mark_hardware` テスト（`:364-371`）は既に `{"square_size": 1.5}` で新 IF と整合。
     **実行しない**（実機確認はユーザー）

### WS2: プレビュー / 設定 API（バックエンド）

1. `src/webui/preview.py`: `_crop_size()` 追加、crosshair 分岐を動的化、docstring 更新
2. `src/webui/routers/settings_api.py`: rebuild 条件から `camera.crop.*` を除外
3. `src/webui/config_store.py`: `camera.crop.*` の 1 以上検証（要確認事項 2。採用時のみ）
4. テスト:
   - `tests/webui/test_preview.py` `TestOverlays` に追加:
     crosshair ストリーム継続中に `store.write_machine_settings` で crop を変更 →
     次フレームで ROI 枠が新しい crop 位置に描かれる（緑ドミナント画素の位置判定。
     例: 1280x720 + crop 200 なら枠左辺は x=540 付近。JPEG ノイズ許容のため近傍数画素で判定）
   - `tests/webui/routers/test_settings_api.py` `TestCameraSettingsRebuild` に追加:
     `camera.crop.width` の PUT では `frame_hub()` が同一インスタンスのまま
     （既存 `test_put_camera_key_rebuilds_frame_hub` は camera.fps なので現状維持）
   - `tests/webui/test_config_store.py`: `camera.crop.width` に 0 / 負値 → `UnknownFieldError`
     （API 経由なら 400）。既存 `:53` の `== 600`（test-fixture 読み値）は変更不要

### WS3: ページ / テンプレート / JS（フロントエンド）

1. `src/webui/routers/pages.py`: `_CAMERA_CROP_KEYS` / `_camera_calibration_context` 追加、
   `_FEATURE_CONTEXT`・`FEATURE_TEMPLATES`・`_JOB_TEMPLATES` を更新
2. `src/webui/templates/posctrl/camera_calibration.html` 新規（IF 案 6）
3. `src/webui/static/js/camera_calibration.js` 新規（IF 案 7）
4. `tests/webui/routers/test_pages.py` 更新:
   - `test_camera_calibration_renders_param_form_fields`（`:285-289`）:
     square_size のみレンダリング・`crop_width` / `crop_height` の job param が**無い**こと・
     `camera.crop.width` の data-machine-key input が fixture 現在値 600 で出ること・
     `js/camera_calibration.js` が読み込まれることへ書き換え
   - `test_posctrl_job_page_renders_console_preview_and_overlay_switch`（`:272`）は
     新テンプレートでも job-console / preview-pane / crosshair を保つので変更不要（回帰確認）

### WS4: 統合検証・E2E・コミット

1. `tests/e2e/test_webui_e2e.py` に追加（`TestAirPumpToggleOverRealHttp` のパターン踏襲）:
   - `GET /posctrl/camera_calibration` → 200、crop input と `js/camera_calibration.js` を含む
   - crosshair MJPEG ストリームを開いてフレームを 1 枚取得 → `PUT /api/settings/machine` で
     `camera.crop.*` を変更 → **同一レスポンスから追加フレームが取得できる**（切断されない =
     要件 4 の通し検証）
   - PUT 後の `GET /api/settings/machine` と隔離 tmp の machine.toml に反映されている
2. 合流時に `grep -rn "</content>" src/ tests/` で subagent Write の混入を一掃
   （memory: agent-content-artifact。特に新規 .js は unit/type を素通りする）
3. `make format && make type && make test-no-hardware && make test-e2e`
   （**`make test` / `@mark_hardware` は実行しない** — memory: no-hardware-test-execution）
4. コミット分割案（1 コミット 1 関心事）:
   - `feat(webui): move camera_calibration crop to machine.toml [camera.crop]`（WS1 + WS2-2,3）
   - `feat(webui): reflect crop changes to crosshair overlay per frame`（WS2-1）
   - `feat(webui): add crop auto-save form and square_size persistence to camera_calibration page`（WS3）
   - `chore(config): kurousagi camera crop 600 -> 300`（machine.toml。既に worktree で変更済み）

## テスト観点

spec-test-author が観点単位でテスト関数へ落とせる粒度で列挙する。

### 正常系

- [unit/catalog] camera_calibration の params が square_size のみ・default 1.5・
  `persisted_params == ("square_size",)`（test_posctrl.py::TestCatalog）
- [integration-with-fakes] チェッカーボード完走: `{"square_size": 10.0}` だけで開始でき、
  crop は machine.toml 値（400 に設定した fixture）が使われる
  （`loaded.crop_size == (400, 400)`）
- [integration-with-fakes] crosshair ストリーム継続中の crop 変更が次フレームの ROI 枠に反映
- [integration-with-fakes] `PUT camera.crop.*` は FrameHub を再構築しない /
  `PUT camera.fps` は従来どおり再構築する（既存テスト維持）
- [integration-with-fakes] ページ描画: square_size フォーム（初回 1.5）+ crop 即保存 input
  （machine.toml 現在値）+ camera_calibration.js の読み込み
- [integration-with-fakes] `POST /api/jobs/camera_calibration/param-defaults` に square_size →
  次回ページ描画の default に反映（既存の param-defaults 機構テストがあればパラメタライズ追加で可）
- [e2e] ページ 200 / crop PUT でストリーム生存 / PUT → GET → tmp machine.toml 反映

### 異常系

- [integration-with-fakes] `PUT camera.crop.width = 0` / 負値 → 400（要確認事項 2 採用時）
- [integration-with-fakes] 削除済み `crop_width` を job params に付けて POST → 400
  「未知のパラメータです」（validate_params の既存挙動のピン）
- [integration-with-fakes] square_size 欠落での開始は default 1.5 で通る
  （必須空欄だった旧挙動からの変更点）

### エッジケース

- [integration-with-fakes] param-defaults に型不一致（str 等）の square_size → 黙って無視され
  default 1.5 のまま（`filter_persisted_defaults` の既存契約）
- crop がフレームより大きい値: 現行と同じく検証しない（overlay の枠が画面外になるだけ。
  calibrator は検出失敗 → 再 prompt で継続できる）。テスト追加は不要（過剰防御にしない）

### テストしないこと

- JS の DOM 挙動の単体テスト（インフラなし。e2e のページ配信 + API 経路で担保）
- 実機カメラでの pixel/mm 精度・1.5mm 実ボード（`@mark_hardware`、ユーザーが実機確認）

## 想定リスク・トレードオフ

- **フレーム毎の machine.toml 読み**: `Machine()` は呼び出し毎に tomllib parse + cattrs 構築
  （sub-ms オーダー）。max_fps 15 × 数ストリームなら Pi 5 で無視できる想定。
  問題が出たら `_crop_size` に TTL キャッシュ（例 0.5s）を足す後付け容易な設計にしてある。
  ※ 先回りのキャッシュ実装は過剰なので入れない。
- **circle / copper レンダラは snapshot のまま**: circle overlay は現在どのテンプレートの UI
  にも露出しておらず（URL query 直指定のみ）、crop 編集はこのページ（none/crosshair のみ）で
  行うため影響なし。新規接続は新 crop を拾う。
- **rebuild 挙動の変更**: 旧挙動では crop 変更も camera.\* として rebuild → 全ストリーム切断 →
  JS 自動再接続で結果的に新 crop が反映されていた。新挙動では crosshair は即時反映になる一方、
  切断による「偶発的な反映」はなくなる（上記のとおり実害なし）。/settings ページから crop を
  変えた場合も rebuild されなくなるが、crop は `_build_camera` が参照しないため本来 rebuild 不要。
- **ジョブ実行中の crop 変更**: `ctx.machine` はジョブ開始時ロードなので、実行中の
  calibrator には効かない（次回実行から）。プレビューの枠だけ先に動く可能性があるが、
  キャリブレーションは短時間ジョブであり許容。
- **square_size 必須 → default 1.5 への変更**: 空欄のまま実行が「エラー」から「1.5 で実行」に
  変わる。要件 1 の意図どおりだが挙動変更として明記。
- **却下した案**:
  - crop をジョブ param のまま残し実行時に machine.toml へ書き戻す — 二重管理になり、
    実行するまでオーバーレイに反映されず要件 4 を満たさない
  - JS で crop 保存後にストリーム再接続（`preview.js` の reconnect 呼び出し）— rebuild +
    カメラ再オープン（数秒）とちらつきが出る。「再接続なしで枠が更新」の要件に反する
  - state_changed イベント購読でレンダラを更新 — PreviewService にイベント配線を足す
    変更量に見合う利点がない（フレーム毎読みで足りる）
  - クロップ入力をジョブフォーム内に残し `POST param-defaults` と `PUT settings` の両方へ
    送る — 真実が 2 箇所になる。不採用

## 参照

- 既存コード: `src/webui/jobs/posctrl.py:71-84,261-331` / `src/webui/preview.py:253-292` /
  `src/webui/routers/settings_api.py:32-44` / `src/webui/routers/pages.py:86-119,211-226,331-344` /
  `src/webui/config_store.py:125-135,144-210` / `src/webui/static/js/loading_controls.js:82-122`
  （入力時即保存の先行例）/ `src/webui/static/js/preview.js`（自動再接続・Canny 保存）
- skill: `webui-thin-wrapper`（JS は fetch/DOM のみ）/ `testing-strategy`（3rd-party モック禁止・
  4 区分）/ `refactor-conventions` / `webui-e2e`（make test-e2e / webui-fake）
- memory: `feedback-no-hardware-test-execution`（実機テスト禁止）/
  `feedback-agent-content-artifact`（subagent Write の `</content>` 混入を合流時 grep）/
  `feedback-worktree-abs-path`（worktree 内は worktree 絶対パスで編集）
- 下流: 公開 IF はシグネチャレベルで確定済みのため、spec-test-author（tests/ 専用）と
  plan-implementer の並列起動が可能。分割は WS1 / WS2 / WS3（ファイル disjoint）
