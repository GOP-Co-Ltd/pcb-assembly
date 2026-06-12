# WebUI Phase 2: FrameHub + MJPEG preview

## 概要

`docs/webui/specification.md` §12 Phase 2 の実装計画。pcbasm 側に `hal/framehub.py`（カメラ専有スレッド + 最新フレーム共有）を新設し、webui 側に `PreviewService`（参照カウント・オーバーレイ・オーバーライドスロット）+ MJPEG / snapshot エンドポイント + posctrl の Camera Preview / Copper Detection ページを実装する。ジョブ基盤（Phase 3）は実装しない（オーバーライドスロットの**受け口**のみ用意し、書く側 `ctx.frame` は Phase 3）。posctrl への frame_sink 注入（spec §4）も Phase 4 であり pcbasm の改修は framehub 新設 + `hal/__init__.py` の export 追加のみ。

前提（調査済み事実）:

- `Camera` ABC（`src/pcbasm/hal/camera.py`）: 抽象は `resolution: Resolution` / `info: CameraInfo` / `capture() -> Image` の 3 つ。`close()` は**存在しない**（CSI は `__del__` で stop/close、USB は GC 任せ）
- `Image`（`src/pcbasm/vision/image.py`）はイミュータブル（`numpy()` は内部配列を返すが、描画側は必ず `.copy()` してから加工する既存規約）。参照共有で安全、FrameHub でのコピー不要
- `pcbasm.vision` の再利用部品: `draw_crosshair`（in-place 十字線）、`draw_overlay(image, crop_size, offset=None)`（十字線 + crop 枠 + オフセットテキスト）、`Image.crop_center`、`CircleDetector(pixel_per_mm, target_diameter_mm, crop_size).detect_nearest_center / detect_with_statistics`、`CopperEdgeDetector(canny_low, canny_high, blur_ksize).detect_edges -> 2値マスク`。**JPEG エンコードの既存ユーティリティは無い**（src に `imencode` 使用箇所なし）→ webui 側で `cv2.imencode(".jpg", ...)` を直接使う
- 円オーバーレイの描画参照元: `src/scripts/posctrl/reference_point_setup.py:44 draw_detected_circle`（クロップ座標 → フル画像座標変換 + 検出円・中心点・中心線描画）。銅箔オーバーレイの参照元: `src/scripts/posctrl/copper_detection.py:_run_interactive`（`display[edges > 0] = (0, 255, 0)` + パラメータテキスト）。scripts は再利用せず webui 内に私的ヘルパとして再実装する（spec §1「WebUI は別実装」）
- `tests/helpers.FakeCamera`: 固定 `Image` 列を順に返す `Camera` 実装。**テストからは流用可**（FrameHub ユニットテストで使用）。ただし tests パッケージはランタイムの webui から import できないため、`PCBASM_WEBUI_FAKE_CAMERA=1` 用のカメラは `src/webui/` に別途置く（後述 `FixedImageCamera`）
- Phase 1 の webui 公開 IF: `create_app(settings)`、`AppState`（`machine()`, `machine_lock`, `select_machine` 等）、`Settings`（attrs frozen + `from_env`）、`app.state.{appstate,store,settings,templates}`、Depends ヘルパ（`StateDep` 等）、例外ハンドラ（BusyError→409, UnknownFieldError→400）。**AppState / ConfigStore は `create_app()` 内で即時構築**（lifespan ではない。implementer メモ「計画外の判断」1）
- `tests/webui/conftest.py` は `with TestClient(app)` で lifespan が走る構成。`webui_settings` fixture が tmp_path コピーの test-fixture を注入
- test-fixture: `[camera]` は device_id=0, 1280x720@30, YUYV, crop 600x600, `calibration_file = "ov9281_test_fixture.json"`（pixel_per_mm=40.0, z_position=-25.0）。`[paste_dispenser.pad_align]` に canny_low=81.0 / canny_high=192.0 / blur_ksize=5 あり（= Copper Detection ページの初期値・保存先キーは Phase 1 ホワイトリスト済み）
- `pcbasm.config.Camera` に `backend: str = "csi"` あり（`create_camera(backend=...)` に渡す）
- **注意: code-simplifier が `src/webui/` 内部を整理中**（公開 IF は不変）。本計画は公開 IF にのみ依存して書いてあるが、plan-implementer は着手時に最新の `src/webui/` を読み、内部構造（私的ヘルパ名等）は現物に合わせること

## 設計判断（spec で未規定 → 本計画で確定）

| 論点 | 判断 | 理由 |
| --- | --- | --- |
| `FrameSource.capture()` のタイムアウト指定 | `Camera.capture()` は引数なしのため、`FrameHub.subscribe(timeout: float = 5.0)` で**購読時に固定**。超過は `TimeoutError` | ABC 互換（既存検出コードへ無改造注入）が最優先 |
| hub 停止中・停止された待機者 | 新フレームが来ない状態（`running=False` かつ待ち条件未充足）では `RuntimeError` を即時送出。ただし `latest()` は停止後も**最後のフレームがあればそれを返す** | 無限待ち・タイムアウト待ちより診断しやすい。stop 時に notify_all して待機者を起こす |
| stop → start の再開 | 可能（シーケンス番号は**リセットしない**単調増加） | 既存 FrameSource のカーソルが再開後も壊れない |
| キャプチャスレッド例外の再送出 | 保持した例外オブジェクトをそのまま `raise`（latest / capture の双方、繰り返し可）。`start()` し直すと例外はクリア | spec「以降の latest() / capture() で再送出」の素直な実装 |
| ランタイム FakeCamera の置き場所 | `src/webui/fake_camera.py` の `FixedImageCamera`（固定画像 1 枚 + fps ペーシング）。tests/helpers.FakeCamera とは別物 | webui ランタイムから tests/ は import 不可。tests 側 FakeCamera は fps 待機が無くテスト高速性のためにそのままが良い |
| 固定画像アセットの置き場所 | `data/testing/webui/fake_camera.png`（git 管理、**spec-test-author が生成**）。`tests/helpers.TESTING_DATA_DIR` 配下なのでテストとランタイムの両方から同一パスで参照できる | 「tests/ とアセット = spec-test-author、src/ = plan-implementer」の分担が成立する。src/ 配下に画像バイナリを置かない |
| Camera の「破棄」 | `hub.stop()` + 参照破棄（GC）。`Camera.close()` は**追加しない** | ABC 改修は最小差分原則に反する。CSI は `__del__` で解放される。リスク欄 1 参照 |
| 参照カウントと hub 再構築の整合 | `acquire()` は「カウント 0→1 のとき start」ではなく**毎回 `hub.start()`（冪等）を呼ぶ**。`release()` はカウント 1→0 で現行 hub を stop | マシン切替（rebuild）で hub が入れ替わった直後の新規接続でも確実に起動する。冪等なのでコスト無し |
| 検出間引きキャッシュの共有範囲 | **ストリームジェネレータごと**（共有キャッシュにしない） | スレッド間共有 mutable state を増やさない。単一オペレータ前提でクライアント数は実質 1〜2 |
| Canny の動的反映 | クエリパラメータ方式（spec §7 どおり）。スライダー変更はデバウンス（300ms）して `<img src>` を張り替え再接続 | サーバー側に可変状態を持たない。MJPEG 再接続は FakeCamera/実機とも軽量 |
| AppState 構築タイミング | Phase 1 のまま `create_app()` 内で即時構築。lifespan には **shutdown の後始末（`appstate.close()`）のみ**追加 | カメラ/hub は遅延構築（下記）なので startup 処理が不要。`with` なし TestClient も壊れない |
| Camera/FrameHub の構築タイミング | `AppState.frame_hub()` への**初回アクセスで遅延構築**（内部 `_camera_lock` で直列化）。構築失敗（デバイス無し等）は例外を伝播し、preview ルーターが 503 に変換 | カメラ未接続の開発機でも webui 全体は起動できる必要がある |
| マシン切替/カメラ設定変更時 | `select_machine` 内および `PUT /api/settings/machine` で `camera.` プレフィックスのキーを書いた後に `rebuild_camera()`（hub.stop + 参照破棄）。配信中の旧ストリームはエラーで終了し、`preview.js` がリトライ再接続して新 hub で復帰 | spec §8「カメラ設定が変わった場合は FrameHub/Camera を再生成」 |
| circle オーバーレイで calibration が読めない場合 | 検出をスキップし「no calibration」の注釈テキストを描画して配信は継続 | ストリームを 4xx で殺すより診断しやすい。crosshair/copper は calibration 不要 |
| snapshot の挙動 | `acquire()` → 1 フレーム取得 → `release()`（ストリーム未接続時は hub の start/stop が 1 回走る） | 参照カウントの仕組みに乗せるのが最小。チャーンはスポット用途なので許容 |
| Camera Preview ページの overlay 選択肢 | spec §10 どおり none / crosshair の 2 択（API 自体は 4 種受け付ける） | circle は Phase 4 の reference_point ジョブ用、copper は専用ページがある |

## 公開インターフェース案（シグネチャ確定 = spec-test-author / plan-implementer 間の契約）

### `src/pcbasm/hal/framehub.py`（新設。pcbasm 改修はこのファイルと `hal/__init__.py` の export 追加のみ）

```python
import logging
logger = logging.getLogger(__name__)   # start/stop を INFO ログ（E2E の停止確認に使う）

class FrameHub:
    """カメラ専有スレッドで capture し、最新フレームを複数消費者へ共有する."""

    def __init__(self, camera: Camera) -> None: ...
    def start(self) -> None
        # キャプチャスレッド起動。冪等（running 中の再呼び出しは no-op）。
        # stop 後の再 start 可。保持中のエラーはクリアする。logger.info("FrameHub started")
    def stop(self) -> None
        # 停止要求 + notify_all + join。冪等。camera は閉じない。logger.info("FrameHub stopped")
    @property
    def running(self) -> bool: ...
    def latest(self, timeout: float = 5.0) -> Image
        # 最新フレーム。初回到着まで待つ。
        # Raises: TimeoutError（timeout 超過）/ RuntimeError（未 start・停止済みでフレーム皆無）
        #         / キャプチャスレッドの保持例外（再送出、繰り返し可）
        # 停止後でも最後のフレームが存在すれば待たずに返す
    def subscribe(self, timeout: float = 5.0) -> FrameSource
        # 消費者カーソル付きの FrameSource を返す。timeout は capture() 1 回あたりの待ち上限


class FrameSource(Camera):
    """FrameHub.subscribe() が生成する Camera 実装。直接コンストラクトしない."""

    @property
    def resolution(self) -> Resolution   # 元カメラへ委譲
    @property
    def info(self) -> CameraInfo         # 元カメラへ委譲
    def capture(self) -> Image
        # 自分のカーソルより新しいフレームが来るまで待ち、カーソルを進めて返す。
        # Raises: TimeoutError / RuntimeError（待機中に hub 停止）/ 保持例外の再送出
```

内部設計（実装者向け指針、テスト対象ではない）: `threading.Condition` 1 本 + 単調増加 `_seq: int`（stop/start でリセットしない）+ `_frame: Image | None` + `_error: BaseException | None` + `_stop_event`。専有スレッドのループは `camera.capture()` → `with cond: _seq += 1; _frame = ...; notify_all()`。例外時は `_error` 保持 + notify_all + スレッド終了。`Image` はイミュータブルなので参照共有（コピー禁止 — 性能要件）。

`src/pcbasm/hal/__init__.py` に `FrameHub`, `FrameSource` を追加 export。

### `src/webui/fake_camera.py`（新設）

```python
class FixedImageCamera(Camera):
    """固定画像を返す開発・E2E 用カメラ。capture() は fps に合わせて待機する."""

    def __init__(self, image_path: Path, fps: float = 15.0) -> None
        # 画像を 1 度だけ Image.load し、以後同一インスタンスを返す。
        # Raises: FileNotFoundError（Image.load 由来）
    @property
    def resolution(self) -> Resolution    # 画像サイズ + fps
    @property
    def info(self) -> CameraInfo          # name="FixedImageCamera"
    def capture(self) -> Image            # monotonic デッドラインで 1/fps ペーシング（time.sleep）
```

fps ペーシングが無いと FrameHub の専有スレッドが空回りして CPU を食うため必須。`time.sleep` のモックは禁止（skill testing-strategy）— fps 15 の実待機でテストする（タイミングのアサートはしない）。

### `src/webui/settings.py`（フィールド追加）

```python
@attrs.frozen
class Settings:
    ...  # Phase 1 フィールドは不変
    fake_camera: bool = False
    fake_camera_image: Path = PROJECT_ROOT / "data" / "testing" / "webui" / "fake_camera.png"
```

`from_env()` 追加対応: `PCBASM_WEBUI_FAKE_CAMERA`（"1" で True）、`PCBASM_WEBUI_FAKE_CAMERA_IMAGE`（パス上書き）。

### `src/webui/state.py`（AppState へのメソッド追加。既存 IF は不変）

```python
class AppState:
    ...
    def frame_hub(self) -> FrameHub
        # 選択マシン用の FrameHub（遅延構築・内部 _camera_lock で直列化）。
        # settings.fake_camera なら FixedImageCamera(settings.fake_camera_image, fps=15.0)、
        # でなければ machine().camera から create_camera(device_id, width, height, fps, format, backend)。
        # 構築失敗（OSError / RuntimeError 等）はそのまま伝播（ルーターが 503 化）。
        # start はしない（PreviewService の責務）
    def rebuild_camera(self) -> None
        # 現行 hub があれば stop し、Camera/FrameHub への参照を破棄（次回 frame_hub() で再構築）。
        # 未構築なら no-op（冪等）
    def close(self) -> None
        # シャットダウン後始末。rebuild_camera() と同じ（hub stop + 参照破棄）
```

- `select_machine()` は成功時（lock 保持中）に `rebuild_camera()` を呼ぶ
- `_camera_lock` は `machine_lock`（装置排他）とは**別の**内部ロック。preview はジョブ排他の対象外（spec §6）

### `src/webui/preview.py`（新設）

```python
type OverlayKind = Literal["none", "crosshair", "circle", "copper"]

MJPEG_BOUNDARY = "frame"
MJPEG_MEDIA_TYPE = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"

class PreviewService:
    """MJPEG 配信・オーバーレイ・参照カウント・ジョブ用オーバーライドスロット."""

    def __init__(
        self,
        state: AppState,
        *,
        max_fps: float = 15.0,        # 配信上限。実効は min(カメラ fps, max_fps)
        detect_fps: float = 5.0,      # circle / copper 検出の実行上限
        override_ttl: float = 1.0,    # ジョブ提供フレームの優先時間 [sec]
        jpeg_quality: int = 80,
    ) -> None: ...

    @property
    def client_count(self) -> int          # 現在のストリーム接続数（/api/state 用）

    def submit_override(self, image: Image) -> None
        # ジョブ用オーバーライドスロットへ書き込む（最新 1 枚 + monotonic タイムスタンプ、ロック保護）。
        # Phase 3 の JobContext.frame() がこれを呼ぶ。Phase 2 ではテストと将来の呼び出しのみ

    def mjpeg_stream(
        self,
        overlay: OverlayKind,
        canny_low: float | None = None,    # overlay=copper の上書き（None は machine.toml 値）
        canny_high: float | None = None,
    ) -> Iterator[bytes]
        # 同期ジェネレータ。開始時 acquire（カウント +1、hub.start()）、
        # finally（GeneratorExit / 例外含む）で release（カウント -1、0 で hub.stop()）。
        # ループ: オーバーライドスロットに override_ttl 以内のフレームがあればそれを優先、
        # なければ FrameSource.capture() + オーバーレイ描画。JPEG エンコードして
        # 完全な multipart パート（boundary 行 + Content-Type/Length ヘッダ + JPEG + CRLF）を yield。
        # 配信間引きは monotonic デッドライン方式（間引き中も capture は回す = USB バッファ滞留対策）

    def snapshot(
        self,
        overlay: OverlayKind,
        canny_low: float | None = None,
        canny_high: float | None = None,
    ) -> bytes
        # acquire → 1 フレーム取得 + オーバーレイ → release。JPEG bytes を返す
```

オーバーレイ描画（モジュール内私的ヘルパ。ストリーム開始時に machine 設定を 1 回読んで構築）:

- `crosshair`: `draw_overlay(frame, machine.camera.crop.size)`（十字線 + crop 枠。offset なし）
- `circle`: `CircleDetector(pixel_per_mm=calibration.pixel_per_mm, target_diameter_mm=machine.reference_point.target_diameter, crop_size=machine.camera.crop.size)` の `detect_nearest_center` を detect_fps に間引いて実行、直近結果を後続フレームに再利用。描画は `reference_point_setup.draw_detected_circle` 相当（クロップ座標→フル座標変換、検出円 + 中心点 + 中心線）+ crosshair。calibration 不読は検出スキップ + 「no calibration」テキスト
- `copper`: `CopperEdgeDetector(canny_low, canny_high, blur_ksize=machine.paste_dispenser.pad_align.blur_ksize)` の `detect_edges` を detect_fps に間引き、`display[edges > 0] = (0, 255, 0)` + `canny: low / high` テキスト（copper_detection スクリプトと同等表示）。low/high の既定は `machine.paste_dispenser.pad_align.canny_low / canny_high`
- 検出キャッシュ（直近マスク / 直近円 + 実行時刻）はジェネレータローカル変数（スレッド間共有しない）

### `src/webui/routers/preview.py`（新設）

```python
GET /api/preview/stream?overlay=none|crosshair|circle|copper&canny_low=&canny_high=
    -> StreamingResponse(media_type=MJPEG_MEDIA_TYPE)
    # overlay 既定 "none"。不正値は FastAPI Literal バリデーションで 422
    # 503: カメラ構築失敗（frame_hub() の OSError / RuntimeError を detail 付きで変換）

GET /api/preview/snapshot?overlay=&canny_low=&canny_high=
    -> Response(content=jpeg, media_type="image/jpeg")
    # 503: 同上。TimeoutError も 503
```

注: 503 変換のため、ジェネレータ方式では最初のフレーム取得前にエラーを検出できない。`stream` エンドポイントは **`hub = state.frame_hub()` をジェネレータ生成前に呼んで**構築エラーを 503 化し、その後 `PreviewService.mjpeg_stream(...)` を StreamingResponse に渡す（配信開始後のエラーはストリーム切断として扱う = クライアントの `onerror` リトライに任せる）。

### 既存ルーターへの変更

- `routers/machine.py` — `StateResponse` に `preview_clients: int` を追加（spec §9）。`GET /api/state` が `PreviewService.client_count` を返す
- `routers/settings_api.py` — `PUT /api/settings/machine` の書き込み成功後、values のキーに `camera.` で始まるものが含まれていたら `state.rebuild_camera()` を呼ぶ
- `routers/pages.py` — feature ごとの専用テンプレート解決を追加:

```python
FEATURE_TEMPLATES: dict[tuple[str, str], str] = {
    ("posctrl", "camera_preview"): "posctrl/camera_preview.html",
    ("posctrl", "copper_detection"): "posctrl/copper_detection.html",
}
# feature_page() は FEATURE_TEMPLATES にあればそれを、なければ従来の feature.html（プレースホルダ）を描画
```

copper_detection ページのコンテキストには `machine().paste_dispenser.pad_align` の canny_low / canny_high / blur_ksize（スライダー初期値）を渡す。

### `src/webui/app.py`（変更）

- `app.state.preview = PreviewService(state)` を即時構築
- `preview` ルーターを include（pages の前）
- lifespan の shutdown で `appstate.close()`（AppState 構築は即時のまま変えない）
- Depends ヘルパ `get_preview(request) -> PreviewService` + `PreviewDep` を追加

### templates / static（新設・変更）

```
templates/posctrl/camera_preview.html    # preview ペイン + overlay 切替（none / crosshair ラジオ）
templates/posctrl/copper_detection.html  # preview ペイン（overlay=copper 固定）+ canny low/high スライダー（0–500）
                                         # + 現在値表示 + 「設定に保存」ボタン
templates/partials/preview_pane.html     # <img> コンテナ + ステータス表示（接続中/エラー/再接続）。両ページで共用
static/js/preview.js
    # - ページ表示時に <img src="/api/preview/stream?..."> を装着、離脱（pagehide）で src を外して切断
    # - overlay / canny 変更は 300ms デバウンスで src を張り替え（再接続）
    # - img onerror は指数バックオフ（1s→…→5s 上限）でリトライ（マシン切替・カメラ失敗からの復帰）
    # - 「設定に保存」= PUT /api/settings/machine
    #   {"values": {"paste_dispenser.pad_align.canny_low": <low>, "paste_dispenser.pad_align.canny_high": <high>}}
    #   （float で送る。成功/失敗はトースト表示 — Phase 1 の settings.js と同じパターン）
static/app.css                           # preview ペイン分の追記
```

「設定に保存」は既存 Phase 1 API で完結する（pad_align.canny_low / canny_high はホワイトリスト済み）。新規エンドポイント不要。保存はジョブ排他（busy 時 409 → トースト）に従う。

### 固定画像アセット（spec-test-author 担当）

`data/testing/webui/fake_camera.png` — 1280x720 PNG。cv2 で合成生成してコミットする:

- 明るい基板風の背景
- **中央付近に直径 120px（= 3.0mm × pixel_per_mm 40.0）の暗色塗りつぶし円**（中心から数十 px ずらす。crop 600x600 内に収める）→ circle オーバーレイの検出が成立する
- 数本の矩形/直線パターン → copper オーバーレイ（Canny）でエッジが出る

生成スクリプトはコミット不要（ワンショット）。test-fixture の calibration（pixel_per_mm=40, crop 600x600）・`reference_point.target_diameter=3.0` と整合させること。

## 実装ステップ（ファイル単位・依存順）

並列レーン: **A（pcbasm: framehub）** と **B（webui）** は独立に着手可。spec-test-author は本計画確定後すぐ `tests/` + アセットに並列着手可（上記シグネチャが契約）。

**レーン A（pcbasm）**

1. `src/pcbasm/hal/framehub.py` — FrameHub / FrameSource（依存なし）
2. `src/pcbasm/hal/__init__.py` — export 追加

**レーン B（webui）** — 着手前に最新の `src/webui/` を読み直すこと（code-simplifier 整理中）

3. `src/webui/fake_camera.py` — FixedImageCamera（依存なし。アセットはパス指定のみなので spec-test-author と独立）
4. `src/webui/settings.py` — fake_camera / fake_camera_image + from_env（依存なし）
5. `src/webui/state.py` — `frame_hub()` / `rebuild_camera()` / `close()` + select_machine への組み込み（依存: 1, 3, 4）
6. `src/webui/preview.py` — PreviewService + オーバーレイヘルパ（依存: 1, 5）
7. `src/webui/routers/preview.py`（依存: 6）
8. 既存ルーター変更 — `machine.py`（preview_clients）/ `settings_api.py`（camera.* → rebuild）（依存: 5, 6）
9. `src/webui/app.py` — PreviewService 構築・ルーター登録・lifespan shutdown・PreviewDep（依存: 7, 8）
10. `routers/pages.py` の FEATURE_TEMPLATES + `templates/posctrl/*.html` + `partials/preview_pane.html` + `static/js/preview.js` + css（依存: 9）

**統合**

11. `make format && make type && make test-no-hardware` グリーン化 → E2E（下記）。`@mark_hardware`（実カメラスモーク）はユーザー実行

## テスト観点（spec-test-author 担当。tests/ は src を 1 対 1 ミラー）

skill `testing-strategy` 準拠。picamera2 / cv2 / time.sleep のモック禁止。FakeCamera（自前 `Camera` ABC の実装）と `FixedImageCamera` は fake 可。webui 結合は conftest の `webui_settings` を拡張（`fake_camera=True`, `fake_camera_image=data/testing/webui/fake_camera.png` を追加）して TestClient で検証。

### `tests/pcbasm/hal/test_framehub.py`（unit + integration-hardware）

テスト補助: `tests/helpers.FakeCamera`（即時返却・列順）に加え、**capture が `threading.Event` を待つゲート付きカメラ**（フレーム供給タイミングを制御）と**capture が例外を投げるカメラ**をテストモジュール内に定義する（新フレーム待ち・例外伝播の決定的テスト用）。

- 正常系:
  - start → `latest()` が Image を返す / `running` が True
  - **複数 subscriber**: 2 つの FrameSource がそれぞれ `capture()` で同一シーケンスのフレームを取得できる（奪い合いがない）
  - **新フレーム待ち**: ゲート付きカメラで、`capture()` が次フレーム供給まで返らず、供給後にカーソルが進んだフレームを返す（同じフレームを 2 度返さない）
  - `latest()` は停止後も最後のフレームを返す
  - FrameSource の `resolution` / `info` が元カメラへ委譲される
  - `detect_with_statistics(FrameSource を 30 回 capture するジェネレータ)` 互換の消費パターンが成立する（Camera ABC 注入の契約確認）
- 異常系:
  - **エラー伝播**: capture が例外を投げると `latest()` / `capture()` が同例外を再送出（2 回呼んでも再送出）/ `running` が False になる
  - 未 start の `latest()` → RuntimeError / 待機中に `stop()` → RuntimeError / フレーム未到着のタイムアウト → TimeoutError（ゲート付きカメラ + 短 timeout）
- エッジケース:
  - **start/stop 冪等**: start 2 連打・stop 2 連打が no-op / stop → start 再開後に capture 続行可（シーケンス非リセット）
  - エラー後の `start()` で例外がクリアされ復帰する
  - stop が camera を閉じない（stop 後に camera.capture() を直接呼べる）
- integration-hardware（`@mark_hardware` + `skip_if_no_csi_camera`、**ユーザー実行**）: 実カメラで start → `latest()` 取得 → 2 subscriber がフレームを取得 → stop → join 完了のスモーク

### `tests/webui/test_fake_camera.py`（unit）

- 正常系: アセット画像を返す（サイズ一致）/ resolution・info / 連続 capture が同一 Image インスタンス
- 異常系: 不存在パス → FileNotFoundError
- ペーシングのタイミングアサートは**書かない**（実時間依存のフレーキー回避）

### `tests/webui/test_state.py`（追記）

- 正常系: `fake_camera=True` 設定で `frame_hub()` が FrameHub を返し 2 回目は同一インスタンス / `rebuild_camera()` 後は別インスタンス + 旧 hub は停止済み / `select_machine` が hub を再構築する / `close()` 後に hub 停止
- 異常系: `fake_camera_image` 不存在で `frame_hub()` が例外伝播
- エッジ: 未構築での `rebuild_camera()` / `close()` が no-op（冪等）

### `tests/webui/test_preview.py`（integration-with-fakes）

- 正常系:
  - `mjpeg_stream("none")` から 2〜3 パート取得 → boundary 形式 + `cv2.imdecode` で JPEG 復号可・サイズ一致（取得後 `close()` でジェネレータ終了）
  - 参照カウント: ストリーム消費中 `client_count == 1`・hub running / ジェネレータ close 後 `client_count == 0`・hub 停止
  - 2 ストリーム並行 → count 2、片方 close で 1（hub は running のまま）、両方 close で停止
  - overlay 描画: crosshair → 出力に緑画素 / copper → 固定画像のエッジ位置に緑画素 / circle → 固定画像の円が検出され赤画素（描画有無のスモークで良い。座標の厳密検証はしない）
  - **オーバーライドスロット**: `submit_override(注釈画像)` 直後のフレームが注釈画像になる / override_ttl 経過後は生フレームに戻る（ttl を短く注入）
  - `snapshot("none")` が JPEG bytes を返し、前後で hub が停止している（参照カウント 0 復帰）
- 異常系: カメラ構築失敗（不存在アセット）で mjpeg_stream の初回 next() が例外

### `tests/webui/routers/test_preview.py`（integration-with-fakes）

- 正常系: `client.stream("GET", "/api/preview/stream?overlay=crosshair")` で Content-Type が `multipart/x-mixed-replace; boundary=frame`、boundary を 2 回読んだら break（**無限ストリームなので必ず上限付きで読む**）/ snapshot 200 + image/jpeg + imdecode 成功 / copper の canny_low/high クエリ受理
- 異常系: `overlay=bogus` → 422 / fake_camera_image 不存在の Settings → stream・snapshot 503
- `/api/state`: ストリーム接続中に `preview_clients == 1`、切断後 0（test_machine.py 側でも `preview_clients` フィールド存在をピン）

### `tests/webui/routers/test_pages.py`（追記）

- camera_preview / copper_detection ページが 200 + 専用マーカー（`/api/preview/stream`、canny スライダー、「設定に保存」）を含む / 他 feature は従来プレースホルダのまま

### `tests/webui/routers/test_settings_api.py`（追記）

- `camera.fps` を含む PUT 後に AppState の hub が再構築される（PUT 前に frame_hub() を触って構築 → PUT → インスタンスが変わる）/ camera 以外のみの PUT では再構築されない

## Claude 自身による E2E 手順（spec §11 / §12 Phase 2）

前提: 実装完了・`make test-no-hardware` グリーン。FakeCamera で実サーバーを叩く。

```bash
mkdir -p /tmp/webui-e2e-p2
cd /home/gop/pcb-assembly
PCBASM_WEBUI_FAKE_CAMERA=1 PCBASM_WEBUI_DATA_DIR=/tmp/webui-e2e-p2 \
  uv run uvicorn webui.app:create_app --factory --port 8080 \
  > /tmp/webui-e2e-p2/server.log 2>&1 &
sleep 3

# 1. ページ: preview ペイン・スライダー・保存ボタンのマーカー確認
curl -s localhost:8080/posctrl/camera_preview | grep -E "api/preview/stream|overlay"
curl -s localhost:8080/posctrl/copper_detection | grep -E "canny|設定に保存"

# 2. snapshot: 全 overlay で JPEG 復号可
for ov in none crosshair circle copper; do
  curl -s "localhost:8080/api/preview/snapshot?overlay=$ov" -o /tmp/webui-e2e-p2/snap_$ov.jpg; done
python3 -c "
import cv2
for ov in ['none','crosshair','circle','copper']:
    img = cv2.imread(f'/tmp/webui-e2e-p2/snap_{ov}.jpg')
    assert img is not None and img.shape == (720, 1280, 3), ov
    print(ov, img.shape, 'OK')"

# 3. MJPEG: 数フレーム取得 → multipart 境界 + JPEG デコード確認（overlay 切替も）
for ov in none copper; do
  timeout 3 curl -s "localhost:8080/api/preview/stream?overlay=$ov&canny_low=50&canny_high=150" \
    -o /tmp/webui-e2e-p2/stream_$ov.bin; done
python3 -c "
import cv2, numpy as np
for ov in ['none','copper']:
    data = open(f'/tmp/webui-e2e-p2/stream_{ov}.bin','rb').read()
    parts = data.split(b'--frame')
    jpegs = [p.split(b'\r\n\r\n',1)[1] for p in parts if b'image/jpeg' in p]
    assert len(jpegs) >= 3, (ov, len(jpegs))
    for j in jpegs[:3]:
        img = cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR)
        assert img is not None
    print(ov, 'frames:', len(jpegs), 'OK')"

# 4. 参照カウント: 接続中 preview_clients=1 → 切断後 0 + hub 停止ログ
timeout 6 curl -s "localhost:8080/api/preview/stream" -o /dev/null &
sleep 2
curl -s localhost:8080/api/state | python3 -m json.tool | grep preview_clients   # 1
sleep 6
curl -s localhost:8080/api/state | python3 -m json.tool | grep preview_clients   # 0
grep -i "framehub" /tmp/webui-e2e-p2/server.log    # started / stopped が対で出ている

# 5. 422 / Canny 保存フロー（test-fixture へ書いて git checkout で復元）
curl -s -o /dev/null -w "%{http_code}\n" "localhost:8080/api/preview/stream?overlay=bogus"  # 422
curl -s -X PUT localhost:8080/api/machine -H 'Content-Type: application/json' -d '{"name":"test-fixture"}'
curl -s -X PUT localhost:8080/api/settings/machine -H 'Content-Type: application/json' \
  -d '{"values":{"paste_dispenser.pad_align.canny_low":60.0,"paste_dispenser.pad_align.canny_high":180.0}}'
git diff configs/test-fixture/machine.toml      # canny 2 行のみ変化・コメント保持
git checkout configs/test-fixture/

# 後始末
kill %1 2>/dev/null; wait
```

ブラウザでの体感確認（preview の見え方・スライダーの動的反映・タブ遷移での自動停止）と**実カメラ**（kurousagi / CSI）でのスモーク（`make test-hardware` の test_framehub + Camera Preview ページの実映像）は**ユーザーが実施**する。

## 想定リスク・トレードオフ（ユーザー確認事項）

1. **`Camera.close()` が無い**: rebuild / shutdown は「hub.stop + 参照破棄（GC）」で行う。CSI は `__del__` で解放されるが GC タイミング依存であり、実機でマシン切替を高頻度に繰り返すと picamera2 の二重 open（`OSError`）が起き得る。Phase 2 では pcbasm 最小差分を優先して ABC に `close()` を**追加しない**。実機で問題が出たら `Camera.close()`（既定 no-op）追加を別途判断いただきたい
2. **MJPEG 接続中の graceful shutdown**: StreamingResponse が生きている間 uvicorn の shutdown が待たされ得る。E2E ではクライアント（curl）を先に止める運用とし、本番でも Ctrl-C 後にブラウザタブが残っていると終了が遅れる可能性をリスクとして明記（uvicorn の `--timeout-graceful-shutdown` で逃げられる。Phase 2 では設定しない）
3. **停止時の待機者の挙動は本計画で確定**（RuntimeError 即時送出）。spec §3 未規定のため、Phase 4 でジョブへ FrameSource を渡す際に「マシン切替でジョブ側の capture が RuntimeError で死ぬ」挙動になる。マシン切替はジョブ排他（409）で防がれるので実害は無い想定だが、契約として明記しておく
4. **検出間引きキャッシュをストリームごとに持つ**ため、同一 overlay の複数クライアントで検出が多重実行される（CPU 増）。単一オペレータ前提で許容。問題化したら共有キャッシュ化（preview.py 内に閉じた変更）
5. **snapshot の hub start/stop チャーン**: ストリーム未接続時の snapshot はカメラの起動を伴う。実 USB カメラでは初回フレームが暗い・古い可能性があるが、スポット確認用途なので許容
6. **TestClient での無限ストリーム読み**: `client.stream()` で上限なしに読むとテストがハングする。spec-test-author は必ず「N パート読んだら break + with ブロックで close」のパターンを使うこと（テスト観点に明記済み）
7. **code-simplifier との競合**: 本計画は公開 IF にのみ依存するが、`app.py` / `state.py` / `settings.py` / `tests/webui/conftest.py` は両者が触るため、plan-implementer は simplifier のブランチ取り込み後に着手（または取り込み直後に rebase）すること
8. **検出結果の再利用によるオーバーレイ遅延**: 間引き中はフレームが動いても直近の検出マスク/円をそのまま重畳するため最大 200ms の表示ずれが出る（spec §7 が明示的に許容する設計）

## 参照

- 仕様: `docs/webui/specification.md`（§3, §5, §7, §9, §10 posctrl 表, §11, §12 Phase 2, §14）
- Phase 1 計画 / 実装ログ: `memory/agents/implementation-planner/webui-phase1.md`, `memory/agents/plan-implementer/webui-phase1.md`（計画外の判断 1 = lifespan、2 = 例外ハンドラ）
- Camera ABC / 実装: `src/pcbasm/hal/camera.py`（`Camera`, `Resolution`, `CameraInfo`, `create_camera`）
- Image / 検出 / 描画: `src/pcbasm/vision/image.py`, `detection.py`（`CircleDetector`）, `copper.py`（`CopperEdgeDetector`）, `overlay.py`（`draw_crosshair`, `draw_overlay`）, `calibration.py`（`CalibrationResult.pixel_per_mm / z_position`）
- 表示の参照元スクリプト: `src/scripts/posctrl/camera_preview.py`, `src/scripts/posctrl/copper_detection.py`（Canny 調整 UI）, `src/scripts/posctrl/reference_point_setup.py:44`（`draw_detected_circle`）
- テスト基盤: `tests/helpers.py`（`FakeCamera`, `mark_hardware`, `skip_if_no_csi_camera`, `TESTING_DATA_DIR`）, `tests/webui/conftest.py`
- 設定: `configs/test-fixture/machine.toml`（[camera] / [paste_dispenser.pad_align]）, `configs/test-fixture/ov9281_test_fixture.json`, `src/pcbasm/config.py`（`Camera` 設定クラス, backend 既定 "csi"）
- テスト規約: skill `testing-strategy`, `refactor-conventions`, `hardware-test`
