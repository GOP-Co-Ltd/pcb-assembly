# WebUI Phase 4: posctrl ジョブ + pcbasm 表示責務分離

## 概要

`docs/webui/specification.md` §12 Phase 4 の実装計画。pcbasm の posctrl から cv2 表示を完全に分離（**破壊的変更可**: ユーザー宣言 2026-06-12）し、webui に posctrl 系 4 ジョブ（reference_point_setup / camera_calibration / board_tour / orthogonality_test）と posctrl タブの feature ページを実装する。Phase 3 から繰り越したジョブ終了時の M84（relax）も実装する。

前提（調査済み事実）:

- `PasteSession.setup()` の呼び出し元は現状ゼロ（scripts/pasting は `setup_board_calibration` + `from_calibration` を直接使用）→ シグネチャ変更の影響は session.py 内のみ
- `tests/pcbasm/posctrl/test_alignment.py` は `from_calibration(result)`（window_name なし）で構築済み → ほぼ無風。`test_setup.py` は `window_name` 引数 + `mock_cv2` fixture を使用 → 追従必要
- `webui/preview.py` の `_draw_detected_circle` は `scripts/posctrl/reference_point_setup.py` の `draw_detected_circle` と重複（Phase 2 で意図的に複製）→ 今回 pcbasm へ昇格して解消
- `configs/test-fixture/` には `ov9281_test_fixture.json`（pixel_per_mm=40, z_position あり）が存在。`data/testing/checkerboard.png` は `CheckerboardCalibrator` で検出可能（既存テストで実証済み）。`data/testing/webui/fake_camera.png` は直径 120px（3.0mm 相当）の円を中心からずらして配置した合成画像
- `camera.calibration_file` は **config_store のホワイトリスト（MACHINE_FIELDS）に未登録**。Machine 読込時に config dir 相対で解決される（`config.py` L318）→ Apply 値はファイル名のみで良い
- Apply の `payload.files` 書き込みは `routers/jobs.py` の `post_apply` に**実装済み**（Phase 3）。変更不要
- `PreviewService` の hub 参照カウント（`_acquire`/`_release`）は私的 → ジョブがカメラを使うには公開 IF が必要
- `machine_control.js` のジョブモード（accepts_commands ジョブへの WS command 切替）は Phase 3 で実装済み。command スキーマは `{type:"jog", axis, dist}` / `{type:"home", axes}` / `{type:"move", x,y,z}` / `{type:"relax"}` / `{type:"focus_z"}`
- `BoardTransformMeasurer` は 3 点法で 2x2 行列 + 並進を計測 → board_transform から軸間角・スケールの数値が導出できる（orthogonality_test の result 数値の根拠）
- Copper Detection の調整値保存（canny → `[paste_dispenser.pad_align]`）は Phase 2 で実装済み（`static/js/preview.js` L83-84、既存設定 API 経由）→ **本 Phase は確認のみ、実装なし**
- **注意: code-simplifier が Phase 3 分の `src/webui/` 内部を整理中**（公開 IF 不変）。plan-implementer は着手時に最新の `src/webui/` を読み直すこと

## 1. pcbasm 注入点の再設計（spec §4 からの差分と理由）

方針: 「camera / frame_sink の注入点を開ける」のではなく、**posctrl / session から cv2 表示コードを全廃**する。表示は (a) 純粋な画像合成関数（pcbasm、テスト可能）と (b) cv2 ウィンドウへの出力 sink（scripts 専用ヘルパ）に分解し、webui は (a) + `ctx.frame()` を使う。

### spec §4 との差分表

| spec §4 の案 | 本計画の最終形 | 理由 |
| --- | --- | --- |
| `frame_sink=None` なら従来どおり cv2 ウィンドウ（後方互換） | **`frame_sink=None` = 表示なし**。cv2 表示は scripts が `window_sink(name)` を明示注入 | `PadAligner` の既存セマンティクス（`window_name=None`=非表示）と統一。pcbasm から cv2 GUI 依存が消え、headless テストで cv2 モック（`mock_cv2` fixture）が不要になる |
| `window_name` と `frame_sink` を同列のシンクとして併存 | **`window_name` 引数は全廃**（破壊的変更） | 2 系統の表示口は混乱のもと。sink 1 本に統一 |
| `setup_board_calibration` に camera/frame_sink を追加 | 同じ（`camera=None` → 内部 `create_camera()` は維持） | camera のデフォルト生成は互換ラッパーではなく妥当な既定値（scripts 6 本の boilerplate 削減）。webui は `hub.subscribe()` を渡す |
| `machine_session` / `PasteSession.__exit__` の `cv2.destroyAllWindows()` は触らない | **削除**（M84 のみに）。ウィンドウ破棄は開いた側（scripts）の責務 | セッション層から cv2 依存を消す。webui はそもそもウィンドウを開かない |
| tour.py の表示ループは pcbasm 据え置き、webui 側で表示を再実装 | 画像合成部分（ラベル付き crosshair、pad 照合結果 overlay）を**純粋関数として `posctrl/render.py` へ昇格**し scripts / webui で共用。cv2.imshow ループは scripts（board_tour.py の `_show_pad_result` 等）と tour.py に残す | webui 側での合成ロジック再実装は重複（Phase 3 の render_pcb 昇格と同じ判断）。imshow/waitKey だけが表示固有 |
| （記載なし） | `draw_detected_circle` を `pcbasm.vision.overlay` へ昇格 | scripts/reference_point_setup と webui/preview の重複解消。Phase 4 の reference_point ジョブで 3 箇所目の利用が発生するため |

### 変更シグネチャ全列挙（pcbasm）

```python
# src/pcbasm/vision/image.py（追加）
type FrameSink = Callable[[Image], None]
# vision/__init__.py から FrameSink, draw_detected_circle を export 追加

# src/pcbasm/vision/overlay.py（追加。scripts/posctrl/reference_point_setup.py から移動）
def draw_detected_circle(
    img: ImageArray, circle: DetectedCircle, crop_size: tuple[int, int]
) -> None
    # 検出円・中心・カメラ中心と結ぶ線を in-place 描画（クロップ座標→フル画像座標変換込み）

# src/pcbasm/posctrl/setup.py（cv2 import 全廃）
class OffsetObserver:
    def __init__(
        self,
        detector: CircleDetector,
        camera: Camera,
        crop_size: tuple[int, int],
        *,
        frame_sink: FrameSink | None = None,   # window_name 削除
        sample_count: int = 30,
    ) -> None
    # observe() 成功時、draw_overlay の注釈画像を frame_sink へ送る（None なら送らない）

def setup_board_calibration(
    machine: Machine,
    pcb_file_path: Path,
    tolerance: float = 0.1,
    *,
    camera: Camera | None = None,        # None → 従来どおり create_camera()
    frame_sink: FrameSink | None = None, # window_name 削除。OffsetObserver へ伝播
) -> BoardCalibrationResult
    # cv2.namedWindow 削除（cv2.imshow はウィンドウを自動生成するため scripts 側も不要）

@contextmanager
def machine_session(klipper: Klipper) -> Generator[None]
    # finally は M84 のみ（cv2.destroyAllWindows 削除）

# src/pcbasm/posctrl/pad.py
class CopperPadObserver:
    def __init__(..., frame_sink: FrameSink | None = None, max_offset_mm=...)
    # window_name 削除。_show → render.render_edge_match() + frame_sink 呼び出し

class PadAligner:
    def __init__(..., frame_sink: FrameSink | None = None)   # window_name 削除

# src/pcbasm/posctrl/alignment.py
class PadAlignmentSession:
    def __init__(self, result: BoardCalibrationResult,
                 frame_sink: FrameSink | None = None) -> None
    @classmethod
    def from_calibration(cls, result, frame_sink: FrameSink | None = None) -> Self

# src/pcbasm/posctrl/render.py（新設。純粋な画像合成、cv2 GUI 非依存）
def render_label(image: Image, crop_size: tuple[int, int], label: str) -> Image
    # draw_overlay + 緑ラベル文字（tour.py の display_at_point の合成部分）

def render_edge_match(
    image: Image, edges: ImageArray, edge_mask: ImageArray, roi: PixelRect
) -> Image
    # ROI 枠（白）+ 想定エッジ（赤）+ 検出エッジ（緑）+ 中心十字
    # （pad.py の CopperPadObserver._show の合成部分）

class PadResultRenderer:
    """pad 照合結果 overlay の単一フレーム合成器（scripts board_tour の
    _show_pad_result から昇格。projection/ROI/塗りマスクは __init__ で
    position 固定で 1 回だけ計算し、render() は毎フレームのエッジ検出と
    合成のみ行う）."""
    def __init__(
        self,
        *,
        projector: CopperProjector,
        edge_detector: CopperEdgeDetector,
        roi_polygons: Sequence[Polygon],
        paste_polygons: Sequence[Polygon],
        pad_align: PadAlign,
        position: Point2d,
    ) -> None
    def render(self, image: Image, lines: Sequence[str]) -> Image

# src/pcbasm/posctrl/tour.py（cv2 表示ユーティリティとして存続）
def window_sink(window_name: str) -> FrameSink
    # cv2.imshow(window_name, image.numpy()) + cv2.waitKey(1) を行う sink を返す
# display_at_point / interactive_display_at_point / wait_for_keypress は
# render_label を使う形に内部整理（公開 IF 不変）

# src/pcbasm/posctrl/__init__.py
# 追加 export: render_label, render_edge_match, PadResultRenderer, window_sink

# src/pcbasm/session.py（cv2 import 全廃）
class PasteSession:
    @classmethod
    def setup(
        cls,
        machine_name: str,
        pcb_file_path: Path,
        tolerance: float = 0.1,
        *,
        camera: Camera | None = None,        # window_name 削除
        frame_sink: FrameSink | None = None,
    ) -> Self
    def __exit__(self, *args) -> None        # M84 のみ（destroyAllWindows 削除）
```

検出・補正コア（`CircleDetector`, `CopperEdgeDetector`, `CopperEdgeMatcher`, `CopperProjector`, `XYPositionAdjustor`, `OffsetTransformMeasurer`, `BoardTransformMeasurer`, `CheckerboardCalibrator`）は spec どおり**無改造**（調査で表示非依存を確認済み）。

### scripts 追従箇所（全列挙。振る舞いは従来同等）

| ファイル | 変更内容 |
| --- | --- |
| `scripts/posctrl/board_tour.py` | `setup_board_calibration(..., frame_sink=window_sink(WINDOW_NAME))` / `PadAlignmentSession.from_calibration(result, frame_sink=window_sink(WINDOW_NAME))` / `_show_pad_result` を `PadResultRenderer` + imshow ループに書き換え（合成ロジック削除）/ 終了時 `cv2.destroyAllWindows()` を main の finally へ |
| `scripts/posctrl/orthogonality_test.py` | `setup_board_calibration` の `window_name` → `frame_sink=window_sink(...)` / finally で `cv2.destroyAllWindows()` |
| `scripts/posctrl/reference_point_setup.py` | ローカル `draw_detected_circle` を削除し `pcbasm.vision` から import。他は不変（`update_reference_point` は script 独自機能として温存） |
| `scripts/posctrl/camera_calibration.py` | 変更なし（`setup_board_calibration` 非使用、独立フロー） |
| `scripts/posctrl/copper_detection.py` / `camera_preview.py` | 変更なし |
| `scripts/pasting/paste_solder.py` | `setup_board_calibration` + `PadAlignmentSession.from_calibration` の frame_sink 化 / `cv2.destroyAllWindows()` 追加 |
| `scripts/pasting/toolhead_offset.py` | `setup_board_calibration` + `OffsetObserver(... frame_sink=window_sink(WINDOW_NAME))` / `cv2.destroyAllWindows()` 追加 |
| `scripts/pasting/height_plane.py` | `setup_board_calibration` の `window_name` → `frame_sink` |

## 2. webui 側の公開インターフェース

### `src/webui/config_store.py`（ホワイトリスト追加）

```python
# MACHINE_FIELDS の [camera] 群に追加
FieldSpec("camera.calibration_file", "キャリブレーションファイル", "str")
```

camera_calibration の Apply（`values={"camera.calibration_file": <filename>}`）に必要。設定画面にも文字列フィールドとして現れる（許容）。

### `src/webui/preview.py`（公開メソッド追加。既存 IF 不変）

```python
class PreviewService:
    @contextlib.contextmanager
    def hold_camera(self) -> Iterator[FrameHub]:
        """参照カウントを保持して FrameHub を貸し出す（0→1 で start、
        1→0 で stop）。MJPEG ストリームとジョブが同一カウントを共有する。

        Raises:
            OSError / RuntimeError: カメラ初期化失敗（AppState.frame_hub 由来）
        """
    # mjpeg_stream / snapshot は内部で hold_camera を使う形に再実装（挙動不変）
    # 私的 _draw_detected_circle を削除し pcbasm.vision.draw_detected_circle を使用
```

### `src/webui/jobs/context.py`（追加）

```python
class JobBridge(Protocol):
    ...  # 既存 6 メソッド
    def hold_camera(self) -> AbstractContextManager[FrameHub]: ...

class JobContext:
    ...  # 既存 IF 不変
    @contextlib.contextmanager
    def open_camera(self) -> Iterator[Camera]:
        """カメラパイプラインを起動保持し FrameSource を貸し出す.

        PreviewService と参照カウントを共有するため、preview クライアントの
        切断でジョブ使用中の hub が止まることはない（逆も同様）。

        Raises:
            OSError / RuntimeError: カメラ初期化失敗
        """
        # 実装: with self._bridge.hold_camera() as hub: yield hub.subscribe()
```

### `src/webui/jobs/manager.py`（M84 finally。既存 IF 不変）

- `_JobRuntime.hold_camera()` → `PreviewService.hold_camera()` へ委譲
- `_run_worker`: 終端ステータス確定後・`publish_status` 前に、`definition.uses_machine` なら **best-effort で M84 を送信**:
  - `Klipper(host, port, timeout=RELAX_TIMEOUT)`（モジュール定数 `RELAX_TIMEOUT = 5.0`）で `gcode.relax()` を送る
  - 失敗（Moonraker 不通等）は `runtime.log("relax (M84) 送信失敗: ...")` のみ。ジョブの終端ステータスは変えない
  - machine 設定は `context.machine` を流用（再ロードしない）
- 順序: run → status/result 確定 → relax → 終端 `publish_status` → `release_machine`

### `src/webui/jobs/posctrl.py`（新設）

```python
def register_posctrl_jobs(catalog: JobCatalog) -> None

@attrs.frozen
class OrthogonalityMetrics:
    axis_angle_error_deg: float   # 変換後の X/Y 軸間角の 90° からのずれ
    scale_x: float                # |T(1,0)-T(0,0)|
    scale_y: float                # |T(0,1)-T(0,0)|

def orthogonality_metrics(transform: Transform) -> OrthogonalityMetrics
    # board_transform（3 点法計測の affine）から直行性指標を導出する純粋関数
```

`default_catalog()` に `register_posctrl_jobs` を追加。ジョブ名は feature slug と一致させる。

#### ジョブ定義表

| name | params（ParamSpec） | requires_pcb | uses_machine | accepts_commands |
| --- | --- | --- | --- | --- |
| `reference_point_setup` | なし | False | True | **True** |
| `camera_calibration` | `square_size: float 必須 [mm]`, `crop_width: int=600 [px]`, `crop_height: int=600 [px]` | False | True | False |
| `board_tour` | `tolerance: float=0.1 [mm]` | True | True | False |
| `orthogonality_test` | `tolerance: float=0.1 [mm]` | True | True | False |

Klipper 構築は私的ヘルパ `_create_klipper(machine: Machine) -> Klipper`（`timeout=COMMAND_TIMEOUT = 60.0`。M400 を含む移動完了待ちのため長め。machine_control.py の MOVE_TIMEOUT と同値）。

#### `_run_reference_point_setup(ctx) -> JobResult`（対話ジョブ）

1. `ctx.machine` から Klipper / XYZStage / `CalibrationResult.load`（失敗は RuntimeError → FAILED）/ CircleDetector（script と同パラメータ）を構築
2. `progress("ホーミング")` → G28 + wait_for_done → `calibration.z_position` へ移動（None なら log 警告して Z 移動なし。script と同じ）
3. `with ctx.open_camera() as camera:` ループ（`progress("ジョグ待機")`）:
   - `camera.capture()` → `detect_nearest_center` → `draw_overlay` + `draw_detected_circle` + 現在位置テキスト → `ctx.frame()`
     - 現在位置は `stage.get_position()` を **0.5 秒キャッシュ**（毎フレームの Moonraker 往復を回避）
   - `ctx.next_command(timeout=0)` を毎フレーム poll し dispatch:
     - `{"type":"jog","axis","dist"}` → `stage.move(relative=True)` + wait_for_done（machine_control.js の toCommand スキーマと一致。距離キーは `dist`）
     - `{"type":"home","axes"}` / `{"type":"move",x,y,z}` / `{"type":"relax"}` → machine-control ルーターと同じ gcode 構築で送信（パネルのジョブモードがこれらも送ってくるため）
     - `{"type":"record"}` → `pos = stage.get_position()` → ループ離脱
     - `{"type":"quit"}` → `raise JobAborted`（→ ABORTED、Apply なし）
     - 不明 type / ValueError → log 警告して継続
   - `ctx.checkpoint()`
4. 戻り値:
   ```python
   JobResult(
       summary=f"基準点: x={pos.x:.3f}, y={pos.y:.3f} mm",
       apply=ApplyPayload(
           label=f"[reference_point] x={pos.x:.3f}, y={pos.y:.3f} を設定に反映",
           values={"reference_point.x": round(pos.x, 3),
                   "reference_point.y": round(pos.y, 3)},
       ),
   )
   ```
   （machine.toml 書込は Apply フロー経由。script の `update_reference_point` 直接書込とは異なり spec §8 どおり）

#### `_run_camera_calibration(ctx) -> JobResult`

1. `CheckerboardCalibrator(square_size_mm, (crop_width, crop_height))`
2. `with ctx.open_camera() as camera:` ループ:
   - `prompt(confirm "チェッカーボードを配置して撮影しますか?（いいえで中止）", default=True)` → False なら `raise JobAborted`
   - `image = camera.capture()` → `ctx.frame(image)` → `calibrator.calibrate(image)`
   - None なら log「チェッカーボードが検出できませんでした」して再ループ
3. **Z 位置は best-effort**: `Klipper(timeout=5.0)` で `stage.get_position().z` を取得。失敗は log 警告し `z_position=None` のまま続行（`CalibrationResult.z_position` は元々 Optional。判断保留点 1 参照）
4. `attrs.evolve(result, z_position=z)` → artifacts: コーナー描画 PNG（image）+ calibration JSON（file）を `ctx.artifacts_dir` へ
5. `filename = f"{camera.info.name}_{YYYYmmdd_HHMMSS}.json"`（script の命名踏襲）:
   ```python
   JobResult(
       summary=f"pixel/mm: {r.pixel_per_mm:.2f} / σ: {r.std_distance_px:.2f} px"
               f" / Z: {z if z is not None else '未取得'}",
       artifacts=(コーナー描画 PNG, calibration JSON),
       apply=ApplyPayload(
           label=f"{filename} を保存し [camera].calibration_file に設定",
           values={"camera.calibration_file": filename},
           files=(ApplyFile(filename, json_bytes),),
       ),
   )
   ```
   Apply で `routers/jobs.py` 既存実装が `configs/<machine>/<filename>` へ書き、`calibration_file` は config 読込時に config dir 相対解決される。

#### `_run_board_tour(ctx) -> JobResult`

1. `with ctx.open_camera() as camera:`
2. `progress("セットアップ")` → `result = setup_board_calibration(machine=ctx.machine, pcb_file_path=ctx.pcb_path, tolerance=tolerance, camera=camera, frame_sink=ctx.frame)`（OffsetObserver の注釈画像が自動配信される）
3. **四隅巡回**（`progress("四隅巡回", …)`）: script と同じ 5 点。各点 `stage.move(Speed.absolute(30))` + wait_for_done → **1.0 秒間** `render_label(camera.capture(), crop, f"Corner: {name}")` → `ctx.frame()`（毎フレーム `ctx.checkpoint()`）
4. **銅箔照合**: `sorted_top_component_pads(result)` → `PadAlignmentSession.from_calibration(result, frame_sink=ctx.frame)` → 各部品 `session.align(group)`（`progress("銅箔照合", 100*i/n)` + `ctx.checkpoint()`、結果 dx/dy/theta を log）。失敗部品は `PadResultRenderer`（lines=["…", "FAILED"]）で 1.0 秒表示して続行
5. **補正適用済み全 pad 巡回**（`progress("補正巡回", …)`）: script の `_tour_corrected_pads` と同じ entries 構築 + nearest ソート → 各 pad へ移動 → `PadResultRenderer` で 1.0 秒表示
6. board 原点へ戻して終了:
   ```python
   JobResult(summary=f"照合成功 {len(alignments)}/{len(groups)} 部品"
                     f"（{aligned_pads} pads）/ 補正巡回 {len(entries)} pads")
   ```
7. 中止: フレームループ・部品ループ境界の checkpoint で ABORTED（`align()` 内部の収束ループ中は次境界まで持ち越し。リスク 3）。終了時の M84 は manager の finally

#### `_run_orthogonality_test(ctx) -> JobResult`

1. `setup_board_calibration(camera=…, frame_sink=ctx.frame, tolerance=…)`
2. `metrics = orthogonality_metrics(result.board_transform)` を log
3. 四隅 + TOP pad（グリッド交点）を `render_label` フレーム配信つきで**各 1.0 秒自動巡回**（script の対話キー待ちを廃し、board_tour と同じ自動進行に統一）
4. ```python
   JobResult(summary=f"軸間角ずれ {m.axis_angle_error_deg:+.3f} deg / "
                     f"scale X {m.scale_x:.5f} Y {m.scale_y:.5f}")
   ```
   （script は数値出力なしの無限対話ループだったため、result 数値の定義は本計画で新規確定。判断保留点 2）

### `src/webui/routers/pages.py`

- `FEATURE_TEMPLATES` 追加:
  - `("posctrl", "camera_calibration" | "board_tour" | "orthogonality_test")` → `"posctrl/job.html"`
  - `("posctrl", "reference_point_setup")` → `"posctrl/reference_point_setup.html"`
- ジョブコンテキスト注入（`job_name` / `param_specs`）の条件を「テンプレートがジョブ系（`dev/job.html`, `posctrl/job.html`, `posctrl/reference_point_setup.html`）」に一般化

### templates / static

```
templates/partials/job_form.html      # dev/job.html のフォーム生成部を抽出（共用 partial。dev/job.html も使用に変更）
templates/posctrl/job.html            # preview_pane（overlay 切替 none/crosshair）+ job_form + job_console
templates/posctrl/reference_point_setup.html
    # preview_pane + 開始ボタン + Record / Quit ボタン + job_console
    # ジョグはサイドバーのマシン操作パネル（ジョブモード切替は Phase 3 実装済み）である旨の説明文
static/js/reference_point_setup.js
    # Record / Quit → window.webui.jobs.sendCommand({type:"record"} / {type:"quit"})
    # jobs.onUpdate 購読でジョブ非実行中（または accepts_commands でない）は disabled
static/app.css                        # 必要分の追記のみ
```

## 3. 実装ステップ（ファイル単位・依存順）

並列レーン: **A（pcbasm + scripts）** と **B（webui 基盤）** は独立着手可。**C（ジョブ実装）** は A・B の後、**D（UI）** は C の後。spec-test-author は本計画確定後すぐ並列着手可（§2 のシグネチャが契約）。

**レーン A（pcbasm 表示分離 + scripts 追従）**

1. `src/pcbasm/vision/image.py`（FrameSink）+ `overlay.py`（draw_detected_circle）+ `vision/__init__.py` export
2. `src/pcbasm/posctrl/render.py` 新設（render_label / render_edge_match / PadResultRenderer）
3. `src/pcbasm/posctrl/setup.py`（OffsetObserver / setup_board_calibration / machine_session。cv2 全廃）
4. `src/pcbasm/posctrl/pad.py`（CopperPadObserver / PadAligner → frame_sink + render_edge_match）
5. `src/pcbasm/posctrl/alignment.py`（PadAlignmentSession → frame_sink）
6. `src/pcbasm/posctrl/tour.py`（window_sink 追加、render_label 利用に内部整理）+ `posctrl/__init__.py` export
7. `src/pcbasm/session.py`（PasteSession.setup / __exit__）
8. scripts 追従 6 本（§1 の表どおり）
9. 既存テスト追従（`tests/pcbasm/posctrl/test_setup.py` ほか。§4 参照）

**レーン B（webui 基盤）** — 着手前に最新の `src/webui/` を読み直すこと

10. `src/webui/config_store.py`（calibration_file ホワイトリスト）
11. `src/webui/preview.py`（hold_camera 公開化、draw_detected_circle の pcbasm 利用）— 依存: 1
12. `src/webui/jobs/context.py`（JobBridge.hold_camera / JobContext.open_camera）
13. `src/webui/jobs/manager.py`（_JobRuntime.hold_camera、finally の M84）— 依存: 11, 12

**レーン C（posctrl ジョブ）** — 依存: A, B

14. `src/webui/jobs/posctrl.py`（4 ジョブ + orthogonality_metrics + register_posctrl_jobs）
15. `src/webui/jobs/catalog.py`（default_catalog へ登録追加）

**レーン D（UI）** — 依存: C

16. `routers/pages.py` + `templates/partials/job_form.html`（抽出）+ `posctrl/job.html` + `posctrl/reference_point_setup.html`
17. `static/js/reference_point_setup.js` + `app.css`

**統合**

18. `make format && make type && make test-no-hardware` グリーン化 → E2E（§5）

## 4. テスト観点（spec-test-author 担当。tests/ は src を 1 対 1 ミラー）

skill `testing-strategy` 準拠。cv2 / Moonraker / time.sleep のモック禁止。装置依存（実カメラ・実 Klipper でのフロー実行）は `@mark_hardware` でユーザー実行。**Klipper 不通の実挙動**（test-fixture の port 7126 は非リッスン）は接続拒否が即時に返るため、モックなしで異常系の結合テストに使える。

### pcbasm（既存追従 + 新規）

`tests/pcbasm/posctrl/test_setup.py`（追従）:
- 正常系: `OffsetObserver(frame_sink=記録リスト.append)` で observe 成功時に注釈画像が 1 枚届く / `frame_sink=None` で例外なく Transform を返す（`mock_cv2` fixture と `window_name` を削除）
- `machine_session`: M84 送信のみをピン（destroyAllWindows アサーション削除）

`tests/pcbasm/posctrl/test_render.py`（新設）:
- 正常系: `render_label` が元画像サイズを保ち非破壊（入力 numpy 不変）/ `render_edge_match` で ROI 内の expected/detected 画素が赤/緑になる（合成エッジマスクでピン）/ `PadResultRenderer.render` が paste 領域薄塗り + 想定輪郭 + lines 描画済みの Image を返す（test_alignment.py の合成画像 fixture を流用）
- エッジ: roi_polygons が空でも min_roi で ROI が成立

`tests/pcbasm/posctrl/test_alignment.py`（追記）:
- `from_calibration(result, frame_sink=collector)` で `align()` 中に 1 枚以上のフレームが届く（既存の合成画像列シナリオに相乗り）。既存テストは window_name 未使用のため無風確認

`tests/pcbasm/posctrl/test_pad.py` / `tests/scripts/`: 無風確認（window_name 不使用）

### webui（unit / integration-with-fakes）

`tests/webui/test_config_store.py`（追記）: `camera.calibration_file` の read（既存値 "ov9281_test_fixture.json"）/ write（str、コメント保持）/ 型不一致 400 系

`tests/webui/test_preview.py`（追記）: `hold_camera` の参照カウント（with 中 hub.running、退出で停止、ネスト併用で 0 になるまで停止しない）

`tests/webui/jobs/test_context.py`（追記）: `open_camera` が bridge.hold_camera 経由で Camera（FrameSource）を貸し、退出で解放する

`tests/webui/jobs/test_manager.py`（追記）:
- `uses_machine=True` の合成ジョブ終了時に relax が試行され、Moonraker 不通（test-fixture）でも終端ステータスは変わらず log に M84 失敗警告が残る
- `uses_machine=False`（dev ジョブ相当）では relax を試行しない（log に出ない）

`tests/webui/jobs/test_posctrl.py`（新設・本 Phase の主戦場）:
- catalog: `default_catalog()` に posctrl 4 ジョブ（name / requires_pcb / uses_machine / accepts_commands / params の default をピン）
- `orthogonality_metrics`: Identity → (0, 1, 1) / 既知の回転 + 非直交 shear 合成 Transform で数値ピン
- **camera_calibration（フル結合・装置なし）**: FakeCamera 画像を `data/testing/checkerboard.png` に差し替えた settings で manager 経由実行 → prompt(confirm) 応答 → SUCCEEDED / artifacts に PNG + JSON（JSON が `CalibrationResult.load` で読める）/ apply payload に `camera.calibration_file` と files 1 件 / Klipper 不通による z 警告が log にあり `z_position=None`
- camera_calibration 異常系: 検出不能画像（fake_camera.png）→ 「検出できませんでした」log 後に再 prompt / confirm=False 応答 → ABORTED
- board_tour / orthogonality_test / reference_point_setup（異常系）: test-fixture（Klipper 不通）で start → FAILED + エラー log + 排他ロック解放 + relax 失敗警告（成功系のステージ移動・照合は pcbasm テストと実機区分でカバー済み、という分担）

`tests/webui/routers/test_jobs.py`（追記）: TestClient + checkerboard FakeCamera で camera_calibration を WS 完走 → `POST /api/jobs/last/apply` → tmp configs の `machine.toml` の calibration_file 更新 + JSON ファイル生成を実ファイルで確認

`tests/webui/routers/test_pages.py`（追記）: posctrl 4 feature ページが 200 + マーカー（param フォーム / job-console / preview img / reference_point_setup の Record・Quit ボタン）

### integration-hardware（ユーザー実行）

- 実カメラ + 実 Klipper での reference_point_setup（ジョグ→Record→Apply）/ camera_calibration（実チェッカーボード + z 記録）/ board_tour / orthogonality_test の通し（§5 末尾の引き継ぎ一覧）

## 5. Claude 自身による E2E 手順（FakeCamera + 実 uvicorn/WS）

ビジョン処理の成立範囲（調査結果）:

| 処理 | FakeCamera での成立性 |
| --- | --- |
| チェッカーボード検出（camera_calibration） | **成立**: `PCBASM_WEBUI_FAKE_CAMERA_IMAGE=data/testing/checkerboard.png` |
| 円検出 overlay（preview / reference_point の注釈） | **成立**: 既定の fake_camera.png（円入り合成画像） |
| 円検出での位置収束（XYPositionAdjustor） | **不成立**: 固定画像では観測オフセットが移動に追従せず収束しない。かつ Klipper 不通で setup 自体が先に失敗する |
| 銅箔照合（PadAlignmentSession） | webui E2E では不成立（同上）。**pcbasm 単体テストの合成画像列（FakeCamera(list)）でのみ成立**（既存 test_alignment.py 方式）→ frame_sink 経路はそちらで検証済み |

→ E2E は「camera_calibration のフル通し（Apply 含む）」+「装置ジョブの起動→FAILED 終端のグレースフル性」+「reference_point の command 経路はジョブ起動前段まで」を確認する。

```bash
mkdir -p /tmp/webui-e2e-p4
cd /home/gop/pcb-assembly
PCBASM_WEBUI_FAKE_CAMERA=1 \
PCBASM_WEBUI_FAKE_CAMERA_IMAGE=data/testing/checkerboard.png \
PCBASM_WEBUI_DATA_DIR=/tmp/webui-e2e-p4 \
  uv run uvicorn webui.app:create_app --factory --port 8080 \
  > /tmp/webui-e2e-p4/server.log 2>&1 &
sleep 3

# 0. test-fixture + PCB 選択
curl -s -X PUT localhost:8080/api/machine -H 'Content-Type: application/json' -d '{"name":"test-fixture"}'
curl -s -X PUT localhost:8080/api/pcb-file -H 'Content-Type: application/json' \
  -d '{"path":"data/testing/fill_coverage/fill_coverage.kicad_pcb"}'

# 1. ページ巡回: posctrl 4 feature ページのマーカー確認
for f in camera_calibration board_tour orthogonality_test; do
  curl -s localhost:8080/posctrl/$f | grep -c "job-console"; done
curl -s localhost:8080/posctrl/reference_point_setup | grep -E "record|quit" -ci

# 2. camera_calibration フル通し（WS: prompt confirm → succeeded → Apply）
#    websockets + httpx のワンショットスクリプトで:
#    - POST /api/jobs/camera_calibration {"params":{"square_size":1.5}}
#    - prompt(confirm) に true 応答 → job_status(succeeded)
#    - result.artifacts に PNG(image) + JSON(file) → /artifacts から取得し
#      PNG は cv2 で復号、JSON は CalibrationResult.load で読める
#    - log に「Z 取得失敗（Klipper 不通）」警告があること
#    - POST /api/jobs/last/apply → 200
git diff configs/test-fixture/machine.toml   # calibration_file の 1 行のみ変化・コメント保持
ls configs/test-fixture/*.json               # 新 JSON が保存されている
git checkout configs/test-fixture/ && git clean -f configs/test-fixture/

# 3. 装置ジョブのグレースフル失敗（Klipper 不通 = 接続拒否で即 FAILED）
#    POST board_tour → job_status(failed) + error に接続エラー
#    + log 末尾に「relax (M84) 送信失敗」警告 + ロック解放
#      （直後の POST /api/machine-control が 409 でなく 502 になることで確認）
#    POST reference_point_setup → 同様に failed（ホーミングで失敗）
#    POST orthogonality_test → 同様

# 4. 中止経路: camera_calibration を再 POST → prompt 待ちで WS abort → aborted

# 5. プレビュー経路（既定画像に戻して再起動）: overlay=circle の MJPEG を
#    数フレーム取得し JPEG 復号（fake_camera.png の円が検出される Phase 2 経路の無風確認）
#    + Copper Detection ページの「設定に保存」が PUT /api/settings/machine で
#      pad_align.canny_* を書くこと（Phase 2 実装の確認のみ）

# 後始末
kill %1 2>/dev/null; wait; rm -rf /tmp/webui-e2e-p4
```

### ユーザーへ引き継ぐ実機確認項目

1. reference_point_setup: 実機でジョグ（マシン操作パネルのジョブモード）→ preview の円検出注釈 → Record → Apply で `[reference_point]` 反映 → Quit で ABORTED
2. camera_calibration: 実チェッカーボードで z_position が記録されること（フォーカス Z ボタンの有効化まで）
3. board_tour: 四隅 → 銅箔照合 → 補正巡回の全行程と preview への照合 overlay 配信、途中 abort、終了時 M84
4. orthogonality_test: 数値 result の妥当性（ベルトテンション調整の実用性）
5. 既存 scripts 6 本（board_tour / orthogonality_test / paste_solder / toolhead_offset / height_plane / reference_point_setup）が表示込みで従来どおり動くこと（IF 追従の実機回帰）
6. ジョブ実行中の preview 常時更新（30 フレームキャプチャ中も MJPEG が止まらないこと = FrameHub の本来の狙い）

## 6. 想定リスク・判断保留点（ユーザー確認事項）

1. **camera_calibration の Z 取得を best-effort 化**: script は Klipper 接続が前提（必須）だが、本計画では失敗時に警告 log + `z_position=None` で続行する（`CalibrationResult.z_position` が元々 Optional であることと整合し、FakeCamera E2E がフルで通る）。実機で Moonraker が落ちていると Z なし calibration ができてしまう点が懸念なら「必須（失敗で FAILED）」に倒す。**要ユーザー判断**（計画は best-effort で進める）
2. **orthogonality_test の result 数値定義は新規**: 軸間角ずれ（deg）+ 軸スケールを board_transform（3 点法）から導出する。「グリッド交点ごとの目視」は自動巡回 + preview 配信で代替。数値の物理的解釈（ベルトテンション調整にどう使うか）は実機で要検証
3. **abort の粒度**: `setup_board_calibration` / `session.align()` の内部ループ（最大 10 反復 × 30 フレーム）には checkpoint が無く、abort はフェーズ境界まで効かない（最悪十数秒）。E-STOP は従来どおりジョブ非経由で即時。pcbasm 側に checkpoint コールバックを足すのは今回見送り（変更範囲の抑制）
4. **破壊的変更の波及**: `window_name` 全廃・`machine_session` / `PasteSession.__exit__` の cv2 削除により、追従漏れがあると scripts が起動時 TypeError で落ちる。§1 の表が全量（grep `window_name|machine_session|setup_board_calibration|PadAlignmentSession` で検証済み）だが、plan-implementer は実装後に同 grep で再確認すること
5. **ジョブとプレビューのフレーム競合**: 照合計算中（observe 1 回 ≈ 30 フレーム + match）は frame_sink の発火が 1〜2 秒に 1 回になり、override TTL（1 秒）切れで生フレームに戻る「ちらつき」が出得る。実害なし（むしろライブ感）と判断。気になる場合は TTL 調整で対応可
6. **reference_point_setup の command スキーマ依存**: ジョブの jog 距離キーは machine_control.js の `toCommand` が作る `dist`。JS とジョブ実装の両方を触るため、キー名の不一致に注意（テストでピンする）
7. **spec への追記事項（docs-keeper へ引き継ぎ）**: §4 の全面改稿（frame_sink=None=非表示・window_name 廃止・render.py / window_sink・machine_session の cv2 削除）、`camera.calibration_file` のホワイトリスト追加、JobContext.open_camera / PreviewService.hold_camera、M84 finally の実装、orthogonality_test の数値定義、camera_calibration の Z best-effort、§10 posctrl 表の details
8. **code-simplifier との競合**: Phase 3 分の整理が進行中。plan-implementer は simplifier の変更取り込み後に着手（または直後に rebase）すること

## 参照

- 仕様: `docs/webui/specification.md`（§4, §6, §8, §10 posctrl 表, §11, §12 Phase 4）
- 前 Phase: `memory/agents/implementation-planner/webui-phase{1,2,3}.md`, `memory/agents/plan-implementer/webui-phase{1,2,3}.md`
- pcbasm 改修対象: `src/pcbasm/posctrl/{setup,pad,alignment,tour}.py`, `src/pcbasm/session.py`, `src/pcbasm/vision/{image,overlay}.py`
- 参照元スクリプト: `src/scripts/posctrl/`（6 本）, `src/scripts/pasting/{paste_solder,toolhead_offset,height_plane}.py`
- webui 基盤: `src/webui/jobs/{context,catalog,manager,dev}.py`, `src/webui/preview.py`, `src/webui/state.py`, `src/webui/config_store.py`, `src/webui/routers/{jobs,pages,machine_control}.py`, `src/webui/static/js/{machine_control,job_console,preview}.js`
- フィクスチャ: `configs/test-fixture/`, `data/testing/checkerboard.png`, `data/testing/webui/fake_camera.png`, `tests/helpers.py`（FakeCamera）
- 規約: skill `testing-strategy`, `refactor-conventions`, `hardware-test`, `agent-team-startup`
