# 初回パージ痕によるツールヘッドオフセット自動キャリブレーション — 実装計画

ユーザー承認済み計画（2026-07-10）。spec-test-author と plan-implementer はこの計画書を唯一の仕様源とする。

## 要件（ユーザー確定）

1. paste_solder の初回パージ痕をカメラで検出し、ツールヘッドオフセット（`[paste_dispenser.toolhead]` x/y）を毎回キャリブレーション
2. キャリブ結果は毎回 machine.toml を**即時更新**（`ctx.apply_machine_settings` 経由）し、**そのランの pad 塗布から新オフセットを使用**
3. 検出失敗＝パージ不良とみなし**ジョブをエラー中止**（RuntimeError 伝播でよい）
4. on/off は WebUI はんだペーストタブの「実行」付近のチェックボックス（default ON）。選択は localStorage に保存。machine.toml には保存しない
5. height plane は機械座標計測のためオフセット変化の影響なし（対応不要）

## 前提事実（調査済み・信頼してよい）

- 検出フローの原型: `src/webui/jobs/pasting.py` `_run_toolhead_offset` の 1740-1772 行（カメラ移動→sleep(1.0)→CircleDetector+OffsetObserver+XYPositionAdjustor.adjust()）
- HeightPlane は Point2d をそのまま返す（XY 保存）→ パージ吐出ステージ XY = `session.toolhead_offset.apply(session.board_transform.apply(purge_point))`、観察カメラ位置 = `session.board_transform.apply(purge_point)`
- `PasteApplicator.__enter__/__exit__` は AirPump ON/OFF → ラン途中の applicator 再作成不可 → `set_transform()` を新設
- posctrl は pasting を import していない → pasting → posctrl の import 追加は循環しない
- bool ParamSpec は `partials/job_params.html` の `param_field` マクロでチェックボックス描画（`partials/job_form.html` → 実行ボタン直上）
- ジョブパラメータは永続化されない → `ParamSpec.persist` フラグ + localStorage を新設
- e2e は test-fixture（Klipper port 7126 非リッスン）で paste_solder が接続失敗するため検出コードに到達しない → default ON で既存 e2e は壊れない
- fake 基盤: `tests/helpers.py` に FakeCamera、`tests/pcbasm/posctrl/test_position.py` に XYPositionAdjustor の fake klipper/stage パターン

## 公開 IF（シグネチャ確定・変更禁止。変更が必要なら本ファイルに「IF変更通知」セクションを追記）

### src/pcbasm/pasting/toolhead_offset.py に追加

```python
def locate_paste_blob(
    *,
    camera: Camera,
    klipper: Klipper,
    stage: XYZStage,
    calibration: CalibrationResult,
    offset_transform: Transform,
    crop_size: tuple[int, int],
    camera_position: Point2d,
    diameter_min: float,
    diameter_max: float,
    tolerance: float,
    frame_sink: FrameSink | None = None,
    settle_time: float = 1.0,
) -> Point2d:
    """カメラをパージ痕へ移動して円検出し、収束後の最終カメラ位置を返す.

    1. stage.move(x=camera_position.x, y=camera_position.y, z=calibration.z_position)
       + gcode.wait_for_done() を送信
    2. time.sleep(settle_time)
    3. CircleDetector(pixel_per_mm=calibration.pixel_per_mm,
       target_diameter_mm=(diameter_min+diameter_max)/2,
       diameter_tolerance_mm=(diameter_max-diameter_min)/2, crop_size=crop_size)
    4. OffsetObserver(detector, camera, crop_size, frame_sink=frame_sink)
    5. XYPositionAdjustor(observe=observer.observe, klipper=klipper, stage=stage,
       offset_transform=offset_transform, tolerance=tolerance).adjust() を返す

    Raises:
        RuntimeError: 円検出失敗・収束失敗（OffsetObserver / XYPositionAdjustor から伝播）
    """

def validate_offset_correction(
    measured: Point2d, current: Point2d, max_correction: float
) -> str | None:
    """計測オフセットと現行設定の差を検証し、不正なら日本語エラー文、正常なら None.

    (measured - current).norm > max_correction のときエラー文（差の値と閾値を含める）。
    境界値ちょうど（== max_correction）は許容（None）。
    """
```

両方 `src/pcbasm/pasting/__init__.py` に export を追加する。

### src/pcbasm/pasting/applicator.py に追加

```python
def set_transform(self, transform: Transform) -> None:
    """塗布座標変換を差し替える（ツールヘッドオフセット較正後のラン内反映用）.

    machine.toml 書き込み後も session/ctx.machine は旧値のままであり、
    実行中ランへの反映点はこのメソッドのみ。
    """
```

### src/webui/jobs/catalog.py

`ParamSpec` に field 追加: `persist: bool = False`（docstring: 選択を localStorage に保存するかの opt-in）。

### src/webui/templates/partials/job_params.html

`param_field` マクロ: `spec.persist` が真のとき入力要素に `data-persist="1"` 属性を出力（全 value_type 共通でよいが、対象は bool checkbox の想定）。

### src/webui/static/js/job_console.js

フォーム初期化時: `#job-form [data-persist]` を走査し、localStorage キー `jobParam:<data-job-name>:<input name>` に保存値があれば復元（bool は "1"/"0" → checked）。change イベントで保存。保存値がなければテンプレートの default（checked 状態）のまま。ドメインロジックは置かない（UI 状態のみ）。

### src/webui/jobs/pasting.py

- モジュール定数:
  - `TOOLHEAD_OFFSET_PASTE_DIAMETER_MIN = 0.0`
  - `TOOLHEAD_OFFSET_PASTE_DIAMETER_MAX = 2.0`
  - `TOOLHEAD_OFFSET_MAX_CORRECTION = 1.0`  # mm
  - 単発 toolhead_offset ジョブの JobDefinition の paste_diameter_min/max default（0.0/2.0 リテラル）もこの定数参照に置換（値は不変）
- paste_solder の JobDefinition params に追加:
  `ParamSpec("calibrate_toolhead_offset", "オフセットキャリブレーション", "bool", default=True, persist=True)`
- `_run_toolhead_offset`: 1740-1772 行の検出ブロックを `locate_paste_blob(...)` 呼び出しに置換（挙動不変。ctx.progress("ペースト検出") はジョブ側に残す）
- 新規ヘルパー:

```python
def _calibrate_toolhead_offset_from_purge(
    ctx: JobContext,
    result: BoardCalibrationResult,
    session: PasteSession,
    purge_point: Point2d,
    tolerance: float,
) -> ToolheadOffsetResult:
    """パージ痕からツールヘッドオフセットを較正し machine.toml へ即時反映する.

    1. purge_camera = session.board_transform.apply(purge_point)
       purge_toolhead = session.toolhead_offset.apply(purge_camera)
    2. locate_paste_blob(... camera_position=purge_camera,
       diameter_min/max=定数, tolerance=tolerance, frame_sink=ctx.frame ...)
    3. ToolheadOffsetResult.measure(dispense_position=purge_toolhead,
       camera_position=最終カメラ位置, tolerance=tolerance, calibrated_at=datetime.now())
    4. validate_offset_correction(measured, 現行 Toolhead の Point2d,
       TOOLHEAD_OFFSET_MAX_CORRECTION) → エラー文なら RuntimeError（書き込み前に中止）
    5. offset_result.save(ctx.artifacts_dir / "toolhead_offset.json")
    6. _apply_to_machine_toml(ctx, {"paste_dispenser.toolhead.x": offset.x,
       "paste_dispenser.toolhead.y": offset.y})
    7. 現行設定との差分を ctx.log
    """
```

- `_run_paste_solder` の初回パージ `deposit_at` 直後:
  - `ctx.params["calibrate_toolhead_offset"]` が真かつ initial_purge 実施時:
    `ctx.progress("オフセット較正")` → `_calibrate_toolhead_offset_from_purge(...)` →
    `applicator.set_transform(Compose([session.board_transform, Shift(x=offset.x, y=offset.y), height_plane]))`
  - チェック ON だがパージ無効（initial_purge is None）: スキップを ctx.log
  - JobResult summary に較正結果 1 行追加（例: `オフセット較正 X=... Y=...` / スキップ時はなし）
  - JobResult artifacts に toolhead_offset.json を追加（単発ジョブと同様 `ctx.artifact(...)`）
  - 検出失敗の RuntimeError は捕捉しない（ジョブエラー中止 = 要件）

## テスト計画（spec-test-author 担当。tests/ 配下のみ）

| 区分 | ファイル | 内容 |
|---|---|---|
| unit | tests/pcbasm/pasting/test_toolhead_offset.py | `TestValidateOffsetCorrection`: 範囲内→None / X 超過・Y 超過・対角超過→エラー文（substring 検証）/ 境界値 == max_correction→None。parametrize |
| fakes | 同上 | `TestLocatePasteBlob`: FakeCamera（tests/helpers.py）+ 実 OpenCV で描いた合成ブロブ画像。中心一致→収束し位置を返す / ブロブなし画像→RuntimeError。fake klipper/stage は tests/pcbasm/posctrl/test_position.py のパターンを踏襲 |
| unit | tests/pcbasm/pasting/test_applicator.py | `TestSetTransform`: set_transform 後の deposit_at が新変換の XY を G-code に出す（既存 fixture・検証手法を流用） |
| fakes | tests/webui/jobs/test_catalog.py ほか | paste_solder の新 param（calibrate_toolhead_offset, bool, default True, persist）を既存テストが列挙・ピンしていれば追従。ParamSpec.persist の default False |
| e2e | tests/e2e/test_paste_solder_browser.py | チェックボックスが job-form 内に表示・default checked・トグル→リロードで保存値復元（localStorage）。既存 browser テストのパターン踏襲 |
| hardware | なし | 新規 @mark_hardware テストは書かない（実機通し確認はユーザー担当） |

規約: class TestXxx 集約 / parametrize / 3rd-party（cv2, time.sleep, Klipper RPC）モック禁止 / 自前 HAL ABC のみ fake 可 / 例外メッセージは substring 検証 / private を直接テストしない。

## 進め方・検証

- spec-test-author は tests/ のみ、plan-implementer は src/ のみ（同時並行、互いのファイルに触らない）
- 検証: `make format` → `make type` → `make test-no-hardware`。**`make test` は実機が動くため実行禁止**。e2e は合流後に `make test-e2e`
- 中間メモ: `memory/agents/<agent-name>/purge-toolhead-calibration.md`
- 既知の落とし穴: サブエージェントの Write がファイル末尾に `</content>` を混入することがある → 各自セルフチェックすること

## リスク・許容事項（実装時に対処不要、把握のみ）

- ≤1.0mm の誤検出はガードをすり抜ける（ROI 小前提で許容）
- パージ量増で痕直径が 0–2mm 帯を外れると中止しうる → エスケープはチェック OFF（実機確認項目）
- localStorage 保存はブラウザ単位
