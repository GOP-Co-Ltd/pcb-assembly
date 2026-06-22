# WebUI 仕様書

PCB アセンブリ装置の操作を Web ブラウザから行うための WebUI（`src/webui/`）の全体仕様と段階実装ロードマップ。

## 1. 背景と目的

開発者向けの単発実行ツールとして成長してきた装置操作機能を、ブラウザから操作できるようにする。

- WebUI のロジックは `src/webui/` に実装する（コアロジックは pcbasm を再利用）
- 現在は posctrl の補正処理中（30 フレーム連続キャプチャ等）にカメラ preview が止まる。WebUI では**位置合わせ中も常時 preview が走る**ことを必須要件とする
- WebUI ユーザーには `machine.toml` / `printer.cfg` といった裏側のファイルの存在を意識させない

### スコープ

- `src/webui/` 新設（FastAPI + Jinja2 + buildless ES modules + CSS、Node ビルド不要）
- pcbasm 側の改修: FrameHub の新設（共通部品）、posctrl の表示責務分離（frame_sink 注入・cv2 GUI 依存の全廃、破壊的変更）、webui 再利用のための昇格 API（§4）
- マシン設定の閲覧・編集（machine.toml + printer.cfg の限定項目）、計測結果の設定反映フロー
- PCB ファイル選択、Klipper console（Mainsail）へのリンク、Emergency Stop / Firmware Restart

### 非スコープ

- 既存 scripts の置き換え・削除（従来どおり動き続ける）
- pnp の機能実装（タブはプレースホルダのみ）
- 認証・マルチユーザー（単一オペレータの装置 UI 前提）
- ジョブ履歴の永続化（直近 1 件のみ保持）

## 2. 全体アーキテクチャ

```
ブラウザ
  │  HTML (Jinja2) / fetch / WebSocket / MJPEG <img>
  ▼
src/webui/  (FastAPI, uvicorn, port 8080)
  ├─ routers/   REST + WS + MJPEG
  ├─ jobs/      単一ジョブ排他のワーカースレッド実行基盤
  ├─ preview.py MJPEG 配信・オーバーレイ・参照カウント
  └─ state.py   AppState（選択マシン / PCB / FrameHub / JobManager）
  │
  ▼ 再利用
src/pcbasm/
  ├─ hal/framehub.py   ★新設: カメラ専有スレッド + 最新フレーム共有
  ├─ hal/klipper.py    Moonraker REST (localhost:7125)
  ├─ posctrl/ pasting/ vision/ visualization/ pcb/ geometry/  コアロジック（表示は frame_sink 注入）
  └─ config.py         get_machine_config()
```

- ポートは 8080（Moonraker 7125 / Mainsail 80 と非衝突）
- フロントは Jinja2 + buildless ES modules のみ。動的部分（WS ジョブコンソール・MJPEG・ジョグ）は命令的 JS が本体であり、SPA フレームワークや htmx は採用しない
- 起動: `make webui-dev`（`uvicorn webui.app:create_app --factory --reload`）/ `make webui`（reload なし）。`[project.scripts]` は使わず `python -m webui`

## 3. FrameHub — カメラパイプライン（pcbasm 側の新共通部品）

### 課題

- `Camera.capture()` は同期呼び出し。posctrl の `OffsetObserver` は 30 フレーム連続キャプチャ（`XYPositionAdjustor` では最大 10 反復 × 30 フレーム）するため、その間 preview が更新できない
- picamera2 は複数スレッドからの `capture_array()` を非推奨としており、preview スレッドとジョブスレッドが素朴に Camera を共有できない

### 設計

配置: `src/pcbasm/hal/framehub.py`（デバイスアクセスの直列化は HAL の関心事。vision に依存しない）。

```python
class FrameHub:
    def __init__(self, camera: Camera) -> None: ...
    def start(self) -> None     # キャプチャスレッド起動（冪等）
    def stop(self) -> None      # 停止・join（冪等）。camera 自体は閉じない
    @property
    def running(self) -> bool: ...
    def latest(self, timeout: float = 5.0) -> Image   # 最新フレーム（初回到着まで待つ）
    def subscribe(self) -> FrameSource

class FrameSource(Camera):      # Camera ABC を実装
    def capture(self) -> Image  # 自分のカーソルより新しいフレームが来るまで待って返す
```

- **専有スレッド 1 本だけ**が `camera.capture()` を呼ぶ。`threading.Condition` + 単調増加シーケンス番号 + 最新フレーム 1 枚を保持（`Image` はイミュータブルなので参照共有で安全、コピー不要）
- 消費者は Condition を wait するだけでデバイスに触れない。preview とジョブの検出処理が同じフレームをそれぞれ受け取れる
- `FrameSource` は消費者ごとのカーソルを持ち「呼ぶたびに新しいフレーム」を保証する。`Camera` ABC を実装するため、`CircleDetector.detect_with_statistics()`（30 フレームのジェネレータを受ける）など既存検出コードへ**無改造で注入できる**
- キャプチャスレッドで例外が出たら保持して notify し、以降の `latest()` / `capture()` で再送出する
- 副次効果: USB カメラ（`cv2.VideoCapture`）の内部バッファによる「数フレーム古い画が返る」問題も連続読みで解消される
- カメラオブジェクトの生成・破棄は FrameHub の責務外（webui の AppState が所有）。既存 scripts は素の `create_camera()` を使い続けるため一切影響しない

## 4. pcbasm 側の改修（表示責務分離と昇格 API）

### 表示責務分離（Phase 4。破壊的変更を採択）

posctrl / session から cv2 GUI 依存を全廃した。表示は (a) 純粋な画像合成関数（pcbasm、headless テスト可能）と (b) cv2 ウィンドウへ出力する sink（scripts 用）に分解し、webui は (a) + `ctx.frame()` を使う。

1. `frame_sink: FrameSink | None`（`pcbasm.vision.FrameSink = Callable[[Image], None]`）を `OffsetObserver` / `CopperPadObserver` / `PadAligner` / `PadAlignmentSession` / `setup_board_calibration` / `PasteSession.setup` に注入。**`frame_sink=None` は表示なし**。旧 `window_name` 引数は全廃
2. cv2 表示は scripts が `posctrl.window_sink(name)`（`cv2.imshow` する sink を返す）を明示注入する
3. `setup_board_calibration(..., *, camera: Camera | None = None)` — `camera=None` なら従来どおり `create_camera()`。webui は `hub.subscribe()` を渡す
4. `machine_session` / `PasteSession.__exit__` は M84 のみ（`cv2.destroyAllWindows()` 削除。ウィンドウ破棄は開いた側 = scripts の責務）
5. 画像合成は `posctrl/render.py`（`render_label` / `render_edge_match` / `PadResultRenderer`）へ昇格し scripts / webui で共用
6. `draw_detected_circle` を `pcbasm.vision.overlay` へ昇格（scripts / webui の重複解消）

検出・補正のコア（`CircleDetector`, `CopperEdgeDetector`, `CopperEdgeMatcher`, `CopperProjector`, `XYPositionAdjustor`, `OffsetTransformMeasurer`, `BoardTransformMeasurer`, `HeightPlaneMeasurer`）は元から表示非依存であり無改造で再利用する。

### scripts からの昇格 API（webui 再利用のため。scripts は薄いラッパに縮小）

- `pcbasm.visualization` — `patches` / `pcb_render` / `fill_render`（Phase 3）、`height_render`（`render_height_plane` / `render_planned_points`、Phase 5）。matplotlib は Agg
- `pcbasm.pcb.generate` — `generate_grid_pcb` / `build_fill_coverage_board` / `save_board`（Phase 3）
- `pcbasm.geometry.sampling` — `SamplingDiagnostics` / `sampling_diagnostics`（probe 計画点の安全余裕・カバレッジ診断。Phase 5）
- `PasteApplicator.from_config` — config からの構築 boilerplate を集約（`PasteSession.make_applicator` も委譲。Phase 5）
- `Klipper.__init__` に `timeout` 引数を追加。webui が自前構築する Klipper はコマンド 60 秒 / relax 5 秒を指定する（pcbasm 内部の既定は従来どおり無制限）

## 5. webui/ モジュール構成

```
src/webui/
├── __init__.py
├── __main__.py            # python -m webui → uvicorn.run
├── app.py                 # create_app() ファクトリ、lifespan で AppState 構築/破棄
├── settings.py            # ポート、configs ルート、PCB ブラウズ root、Mainsail URL（env 上書き可）
├── state.py               # AppState: 選択マシン/PCB、Camera+FrameHub、JobManager、JSON 永続化
├── models.py              # API 境界の pydantic モデル
├── config_store.py        # machine.toml / printer.cfg のホワイトリスト読み書き
├── preview.py             # PreviewService: 参照カウント、MJPEG、オーバーレイ
├── fake_camera.py         # 固定画像カメラ（PCBASM_WEBUI_FAKE_CAMERA 用）
├── jobs/
│   ├── manager.py         # JobManager / JobRecord / JobStatus
│   ├── context.py         # JobContext（log/progress/frame/prompt/command/checkpoint/open_camera）
│   ├── catalog.py         # ジョブ名 → JobDefinition（実体 + パラメータスキーマ）レジストリ
│   ├── machine_commands.py # ジョブ中のマシン操作コマンド処理（jog/home/move/relax/focus_z）
│   ├── posctrl.py         # posctrl 系ジョブ実装
│   ├── pasting.py         # pasting 系ジョブ実装
│   └── dev.py             # dev 系ジョブ実装
├── routers/
│   ├── pages.py           # GET /{tab}, /{tab}/{feature}, /settings（Jinja2）
│   ├── machine.py         # マシン選択
│   ├── files.py           # PCB ファイルブラウザ
│   ├── jobs.py            # ジョブ REST + WS /api/ws
│   ├── preview.py         # MJPEG / snapshot
│   ├── settings_api.py    # マシン設定の取得/保存
│   ├── machine_control.py # マシン操作パネル（homing/ジョグ/移動/relax/フォーカスZ）
│   └── system.py          # emergency stop、firmware restart、Klipper ステータス
├── templates/
│   ├── base.html          # ヘッダ（タブ・マシン選択・PCB チップ・設定・console リンク・Firmware Restart・E-STOP）
│   ├── {dev,pasting,pnp,posctrl}/…
│   ├── settings.html
│   └── partials/          # サイドバー、job console、preview ペイン、prompt モーダル、マシン操作パネル
└── static/
    ├── app.css
    └── js/
        ├── job_console.js # WS クライアント（ログ/進捗/プロンプト/中止/Apply、/artifacts/ リンク化）
        ├── preview.js     # <img> の付け外し・overlay 切替
        ├── machine_control.js  # マシン操作パネル（REST / ジョブ command の送信切替）
        ├── pad_editor.js       # buildless ES module entry（pasting pad 編集）
        ├── pad_editor/         # model / viewer helper
        └── …                   # ページ別 JS（loading_controls / reference_point_setup / klipper_status / settings 等）
```

### テスト容易性のフック

- `create_app(settings: Settings)` で configs ルート・data ディレクトリ・カメラファクトリを注入可能にする（既定は本番値）
- env による起動時切替:
    - `PCBASM_WEBUI_CONFIGS_ROOT` — configs ルートの差し替え
    - `PCBASM_WEBUI_DATA_DIR` — data ディレクトリ（成果物・`webui_state.json`）の差し替え
    - `PCBASM_WEBUI_FAKE_CAMERA=1` — 固定画像を返す FakeCamera で FrameHub を構成。カメラ実機なしで preview / MJPEG / オーバーレイを通しで検証できる（画像は `PCBASM_WEBUI_FAKE_CAMERA_IMAGE` で差し替え可）
    - 全量（`PCBASM_WEBUI_PORT` / `PCBASM_MAINSAIL_URL` 等）は `src/webui/settings.py` を参照
- `configs/test-fixture/` を新設（machine.toml + printer.cfg を持つフィクスチャマシン、git 管理）。設定編集・Apply フローの検証はこのマシンに対して行い、実マシン設定（kurousagi / pd_china_frame）を汚さない。pytest では tmp_path にコピーして使用、手動 E2E では編集後 `git checkout` で復元する

## 6. ジョブ実行基盤

### 排他とライフサイクル

- 装置を動かすジョブは**同時 1 件**。`JobManager` が `threading.Lock` を非ブロッキング取得し、競合は HTTP 409
- preview はロック対象外（FrameHub 読みのみで装置を動かさない）。マシン切替・PCB 切替・設定保存もジョブ実行中は 409
- ライフサイクル: `PENDING → RUNNING ⇄ WAITING_INPUT → SUCCEEDED | FAILED | ABORTED`（API 上の status 値は小文字: `pending` 等）
- ワーカーは `threading.Thread` 1 本（pcbasm は全て同期コードのため。asyncio 化はしない）
- `JobRecord`: id, name, params, status, ログのリングバッファ（直近 N 行）, result, error。**直近 1 件のみ保持・非永続**
- 成果物は `data/webui/<job_id>/` に保存し `/artifacts` で配信（StaticFiles。ジョブ**実行中**でも書いた時点で配信可能）。**新ジョブ開始で前回分は削除**する
- `uses_machine` のジョブ終了時は manager が best-effort で M84（relax、timeout 5 秒）を送る。失敗はログ警告のみで終端ステータスは変えない
- ワーカースレッドが出す `pcbasm` logger の INFO 以上はジョブコンソールへ転送する（ログブリッジ。setup のフェーズログや probe 点進捗が見える）。ログ中の `/artifacts/...` パスはコンソール上でリンク化される
- カタログには `hidden` ジョブを登録できる（UI フォーム非表示・POST は可。基盤検証用の `job_demo` が該当）

### JobContext（ワーカー ↔ Web 層の橋）

```python
class JobContext:
    def log(self, message: str) -> None
    def progress(self, stage: str, percent: float | None = None) -> None
    def frame(self, image: Image) -> None        # preview のオーバーライドスロットへ push
    def prompt(self, spec: PromptSpec) -> Answer # WAITING_INPUT に遷移してブロック
                                                 # kind: confirm | number | text | choice
    def next_command(self, timeout: float | None) -> Command | None  # ジョグ等の連続対話キュー
    def checkpoint(self) -> None                 # abort 要求済みなら JobAborted を送出
    @contextmanager
    def open_camera(self) -> Iterator[Camera]    # FrameHub の参照カウントを保持して FrameSource を貸し出す
```

- `open_camera()` は `PreviewService.hold_camera()` と参照カウントを共有する。preview クライアントの切断でジョブ使用中の hub が止まることはない（逆も同様）

- スレッド間は `queue.Queue`（イベント出力）+ `threading.Event` / `Queue`（prompt 応答・command 入力）。Web 側（asyncio）へは `asyncio.run_coroutine_threadsafe` で WS にブロードキャスト

- `input()` の代替: flow_calibration の重量入力 → `prompt(number)`、height_plane の Y/n → `prompt(confirm)`、probe_gnd_down_adjust の距離調整 → `prompt(number)` の繰り返し

- `prompt(confirm)` は `true_label` / `false_label` で肯定・否定ボタンの表示名を指定できる。未指定時は従来どおり「はい」/「いいえ」

- 連続対話（ジョグ）: reference_point_setup はジョブとして起動後 `next_command()` ループで `{type:"jog", axis, dist}` / `{type:"record"}` / `{type:"quit"}` を消費。preview には `ctx.frame()` で円検出オーバーレイを流し続ける

- 中止（abort）は協調的: フラグを立て、`prompt` / `next_command` 待ちは即 JobAborted 化、長い処理はループ内 `checkpoint()`。終了時の M84 は manager が送る（上記）

### マシン操作パネル（ジョブ外の単発操作）

全タブから使える共通コンポーネント（右サイドバーに常設。左端の全高縦バー（開閉トグル）で畳める。展開中は ">"（右へ畳む）、折りたたみ中は "\<"（左へ開く）の三角表示。左端ハンドルのドラッグで幅を可変＝localStorage 永続）。Klipper console（Mainsail）リンクは残しつつ、基本操作は console に飛ばずに完結できるようにする。

- **Homing**: X / Y / Z 軸個別 + 全軸（`gcode.homing()`）
- **ジョグ**: XY は円形ジョグパッド（SVG）、Z は縦バーで ±0.1 / ±1 / ±10 mm。`XYZStage.move(relative=True)` を使う（limits 検証込み）。Z はマシン設計上「上昇＝Z マイナス」のため、縦バーは上端を負（上昇）・下端を正（下降）に並べる
- **座標直接入力**: x / y / z の数値フィールド + 移動ボタン（絶対座標。空欄の軸は現在位置を維持 = `move()` の None 渡し）
- **Relax**: M84
- **フォーカス位置へ**: 選択マシンの calibration（`CalibrationResult.z_position` — camera_calibration 撮影時の Z 値）へ Z を移動。calibration 未設定や `z_position` が無い場合はボタンを無効化
- パネルには現在位置と homed_axes を表示（`/api/klipper/status` をパネル展開中のみポーリング）

実装と排他:

- `POST /api/machine-control` ボディ `{action: "home" | "jog" | "move" | "relax" | "focus_z" | "gcode", …}`。1 リクエスト = 1 同期操作（G-code 送信 + `wait_for_done`、完了でレスポンス）。`gcode` は任意 G-code 送信（dev タブの Klipper Status から使用）
- ジョグ UI の移動範囲表示には `GET /api/stage/limits`（`XYZStage` の limits）を使う
- **ジョブと同じ排他ロックを共有する**: ジョブ実行中は 409 を返し、UI はパネルを disabled 表示にする。逆にマシン操作の実行中（移動完了待ち）もジョブ開始は 409
- 未ホーミング軸への移動や limits 超過は `XYZStage` / Klipper のエラーをそのままエラートーストで表示する
- reference_point_setup 等の**対話ジョブ中**のジョグは従来案どおり WS の `command`（`next_command`）経由。UI は同じパネル部品を「ジョブモード」へ切り替えて使う（送信先が REST か WS かの違いのみで、ボタン構成は共通）

### Emergency Stop / Firmware Restart

ジョブ機構を**経由しない**。`POST /api/emergency-stop` がその場で `Klipper.emergency_stop()` を直接叩く（Moonraker は REST なのでジョブと独立に届く）。同時に abort フラグも立てる。UI のボタンは赤・常時表示・確認なし即時。

Firmware Restart もジョブ機構を**経由しない**。`POST /api/firmware-restart` が `FIRMWARE_RESTART` を送信し、送信前に abort フラグを立てる。UI のボタンはヘッダに常時表示し、確認なしで送信する。

### WebSocket

グローバル 1 本 `WS /api/ws` をマルチプレクスする（同時ジョブ 1 件なのでチャネル分離の利点がなく、E-STOP・状態変更通知も同送できる）。再接続時は `GET /api/jobs/current` で状態を取り直す。

- サーバー → クライアント: `job_status`, `log`, `progress`, `prompt`, `prompt_resolved`, `state_changed`, `error`（不正コマンド・prompt_id 不一致等の通知）
- クライアント → サーバー: `respond_prompt {prompt_id, answer}`, `command {…}`, `abort`

## 7. カメラ preview 配信

- `GET /api/preview/stream?overlay=none|crosshair|circle|copper` — MJPEG（`multipart/x-mixed-replace`）。同期ジェネレータ（FastAPI が threadpool で実行）で `hub.subscribe()` → capture → オーバーレイ → JPEG エンコード → yield。配信は min(カメラ fps, 15) に間引き
- **起動/停止は参照カウント**: `PreviewService` がストリーム接続で `acquire()`（0→1 で `hub.start()`）、切断（GeneratorExit）で `release()`（1→0 で `hub.stop()`）。クライアントは posctrl 系ページにだけ `<img src="/api/preview/stream">` を置き、タブ遷移で img が DOM から消えれば自動停止する。明示的な start/stop API は不要
- オーバーレイ:
    - `crosshair`: 既存 `draw_crosshair` + crop 枠
    - `circle`: `CircleDetector` の検出結果 + 円描画（既存 `draw_overlay` 相当）
    - `copper`: `CopperEdgeDetector.detect_edges` のエッジ重畳（copper_detection スクリプトと同等の表示。Canny low / high はクエリパラメータで上書き可）
    - 検出は重いので **~5fps に間引き、直近の検出結果を後続フレームに再利用**して描画する
- **ジョブ実行中のオーバーライドスロット**: `ctx.frame()` が最新 1 枚 + タイムスタンプを書き、ストリームは「直近 1 秒以内にジョブ提供フレームがあればそれを優先、なければ生フレーム + overlay」を配信。`OffsetObserver` の注釈付き画像（検出円・オフセット表示）がそのまま画面に出る

## 8. 設定管理

ユーザーには「マシン設定」という一つの画面として見せ、裏が machine.toml であることは意識させない。

### 設定画面（ヘッダの歯車 → `/settings`）

- machine.toml の編集可能項目を**ホワイトリスト化したフォーム**で表示: ディスペンサー諸元 `[paste_dispenser]`、pad 照合パラメータ `[paste_dispenser.pad_align]`（canny 閾値等）、プローブ `[probe]`、基準点 `[reference_point]`、カメラ `[camera]` 等。TOML セクション単位の折りたたみ表示（`SECTION_LABELS`）。保存は tomlkit でコメント・構造を保持して書き戻す
- printer.cfg（モーション設定）は WebUI では編集しない。ヘッダの「Klipper」リンクから Mainsail に飛び直接編集する（2026-06-12 のユーザー判断で「モーション設定」セクションを削除）
- 読み書きは `webui/config_store.py` に集約（ホワイトリスト定義 + tomlkit 書き込み）。ジョブ実行中の設定保存は 409
- 設定変更後はマシン設定を再ロード（AppState の Machine 再構築。カメラ設定が変わった場合は FrameHub/Camera を再生成）

### 計測結果の Apply / Discard フロー

キャリブレーション系ジョブは SUCCEEDED 時に result へ「設定反映ペイロード」（toml キーパスと値、表示用ラベル）を含め、ジョブコンソールに計測値と「設定に反映」/「破棄」ボタンを表示する。`POST /api/jobs/last/apply` で machine.toml へ書き込み（成功時は `state_changed` も発行）、`POST /api/jobs/last/discard` で破棄する。反映可能なのは直近完了ジョブのみ（次のジョブ開始または破棄で無効化）。

| ジョブ                | 反映先                                                                        |
| --------------------- | ----------------------------------------------------------------------------- |
| camera_calibration    | calibration JSON を `configs/<machine>/` へ保存 + `[camera].calibration_file` |
| toolhead_offset       | `[paste_dispenser.toolhead]` x, y                                             |
| flow_calibration      | `[paste_dispenser].rotations_per_ul`                                          |
| reference_point_setup | `[reference_point]` x, y                                                      |
| probe_gnd_down_adjust | `[probe].down_distance`                                                       |

## 9. REST / WS API

| Method  | Path                                      | 内容                                                                                |
| ------- | ----------------------------------------- | ----------------------------------------------------------------------------------- |
| GET     | `/`                                       | `/posctrl` へリダイレクト                                                           |
| GET     | `/{tab}`, `/{tab}/{feature}`, `/settings` | Jinja2 ページ                                                                       |
| GET     | `/api/state`                              | 選択マシン・PCB・現行ジョブ要約・preview クライアント数                             |
| GET     | `/api/machines`                           | `configs/*/machine.toml` を列挙                                                     |
| GET/PUT | `/api/machine`                            | マシン選択（ジョブ中 409。FrameHub/Camera 再構築）                                  |
| GET     | `/api/files?path=`                        | dir + `*.kicad_pcb` のみ列挙。PROJECT_ROOT 配下に制限（traversal 防止）             |
| PUT     | `/api/pcb-file`                           | PCB 選択（ジョブ中 409）                                                            |
| POST    | `/api/jobs/{name}`                        | ジョブ開始 → 201 `{job_id}` / 409                                                   |
| GET     | `/api/jobs/current`                       | 状態・ログ末尾・pending prompt（WS 再接続時の同期用）                               |
| POST    | `/api/jobs/current/abort`                 | 協調的中止                                                                          |
| POST    | `/api/jobs/last/apply`                    | 直近完了ジョブの計測結果を設定へ反映                                                |
| POST    | `/api/jobs/last/discard`                  | 直近完了ジョブの計測結果を破棄                                                      |
| GET     | `/api/pasting/pad-config`                 | 選択基板の outline / pads / 階層ツリー / 解決済み設定 / 疎 override（§15）          |
| PATCH   | `/api/pasting/pad-config/node`            | ノードの enabled / override を upsert・clear（即保存、affected_pads を返す）        |
| PATCH   | `/api/pasting/pad-config/pads`            | pad id 配列の enabled を L4 ノードとして一括設定                                    |
| POST    | `/api/pasting/pad-config/reset`           | 全 override を破棄し machine.toml 由来の初期値へ戻す                                |
| GET/PUT | `/api/settings/machine`                   | マシン設定（machine.toml ホワイトリスト項目）                                       |
| POST    | `/api/machine-control`                    | homing / ジョグ / 絶対移動 / relax / フォーカス Z / 任意 G-code（§6。ジョブ中 409） |
| GET     | `/api/stage/limits`                       | XYZStage の移動範囲（ジョグ UI 用）                                                 |
| POST    | `/api/emergency-stop`                     | 即時 M112 相当（ジョブ非経由）                                                      |
| POST    | `/api/firmware-restart`                   | Firmware Restart（ジョブ非経由、abort フラグ先行）                                  |
| GET     | `/api/preview/stream?overlay=`            | MJPEG                                                                               |
| GET     | `/api/preview/snapshot?overlay=`          | JPEG 1 枚（スポット確認用）                                                         |
| GET     | `/api/klipper/status`                     | position / homed_axes 等（ステータスカード用）                                      |
| WS      | `/api/ws`                                 | §6 のイベント / コマンド                                                            |

- API ボディは JSON 統一。レスポンスモデルは FastAPI 標準の pydantic を **webui の API 境界のみ**で使用する（pcbasm の attrs/cattrs とは層が違うため混在を許容）
- Klipper console リンク: `MAINSAIL_URL`（既定 `http://localhost`、env `PCBASM_MAINSAIL_URL` で上書き）への `<a target="_blank">`

## 10. UI 構成

### レイアウト

装置操作用の静かな作業 UI として、過度な装飾より情報密度、視認性、状態の予測しやすさを優先する。フォーム、preview、pad viewer、job console、machine control は狭い viewport でも重ならず、操作中・disabled・error・selected・focus の状態を同じ表現体系で示す。

- **ヘッダ**: タブ（dev / pasting / pnp / posctrl）+ マシン選択ドロップダウン + PCB ファイルチップ（クリックでファイルブラウザモーダル）+ 設定（歯車 → `/settings`）+ Klipper console リンク + **Firmware Restart（警告色・常時表示・確認なし）** + **E-STOP（赤・常時表示・確認なし即時）**
- **サイドバー**: タブ内の feature リスト。実行中ジョブがあればバッジ表示。下部に**マシン操作パネル**（§6。折りたたみ、全タブ共通）
- **メインペイン**: feature ごとに「パラメータフォーム（argparse 引数から導出、デフォルト値も引き継ぐ）+ 実行ボタン + ジョブコンソール（ログ / 進捗バー / プロンプトモーダル / 中止 / Apply）+ 必要なら preview ペイン」
- machine / pcb-file はグローバル選択値のため各フォームから除外。`data/webui_state.json` にこの 2 値のみ永続化（起動時に復元）

### posctrl タブ（常時 preview ペインあり）

| 項目                  | 形態             | フォーム / 操作                                                                                                                                                                                               |
| --------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Camera Preview        | preview のみ     | overlay 切替（none / crosshair）                                                                                                                                                                              |
| Copper Detection      | preview のみ     | overlay=copper + Canny low / high スライダー（動的反映）。「設定に保存」で `[paste_dispenser.pad_align]` へ書き込み（現行スクリプトの調整専用フローを Apply 化）                                              |
| Camera Calibration    | ジョブ           | square_size（必須）+ crop サイズ。prompt(confirm) で撮影を進行。Z 位置は best-effort（Klipper 不通なら警告ログ + `z_position` なしで続行）。Apply で calibration JSON 保存 + `[camera].calibration_file` 反映 |
| Reference Point Setup | 対話ジョブ       | マシン操作パネル（ジョブモード、±0.1/1/10mm。focus_z 含む）+ Record / Quit。preview に円検出 + 現在位置                                                                                                       |
| Board Tour            | ジョブ           | tolerance。四隅巡回 → 部品単位の銅箔照合（`PadAlignmentSession`）→ 補正適用済み全 pad 巡回。各点 1 秒の自動進行、照合 overlay を `ctx.frame()` で配信。中止は abort                                           |
| Orthogonality Test    | ジョブ           | tolerance。board_transform（3 点法）から導出した軸間角の 90° からのずれ [deg] と軸スケール X / Y を result 表示                                                                                               |
| Generate Grid PCB     | ジョブ（非装置） | size / divisions / pad_size。直行性テスト用グリッド PCB を `data/webui/` に生成しダウンロードリンク（カメラ preview なし。dev タブから移設）                                                                  |

### pasting タブ（preview はジョブ提供フレームのみ）

| 項目                  | フォーム / 操作                                                                                                                                                                                                                                                                                |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Paste Solder          | tolerance, amount, interactive-loading。進捗 = セットアップ → 銅箔照合（`PadAlignmentSession`）→ 高さ計測 →（任意ローディング）→ 塗布の stage 表示                                                                                                                                             |
| Height Plane          | tolerance。計測前に計画点 PNG + サンプリング診断を artifacts 配信して prompt(confirm)。完了後ヒートマップ PNG をインライン表示（成果物は artifacts のみ・次ジョブ開始で削除。`--output` 相当の恒久保存はなし）                                                                                 |
| Loading               | amount。コマンドボタン（押出 / 吸引 / 終了。量は数値フィールド常設）= `{type:"extrude"\|"suck", amount}` / `{type:"finish"}` の `next_command` 駆動。進捗 stage「ローディング」中のみ有効                                                                                                      |
| Flow Calibration      | rotations, rate, accel, count（計測回数, 既定 3）, load-amount。ローディング →（タール confirm（いいえで中止）→ 回転 → 質量 prompt(number)）× count → リトラクト → 比重 prompt(number)。質量の平均から rotations_per_ul を算出し、各回値・平均・標準偏差を summary に表示。結果は Apply で反映 |
| Toolhead Offset       | tolerance, dispense-amount, loading-amount, lift-height, paste-diameter-min/max。結果 JSON は artifacts。結果は Apply で反映                                                                                                                                                                   |
| Probe GND Down Adjust | prompt(number) + 確定 confirm のループで down distance 調整。終了時（abort 含む）はダウン距離 0 へ復帰。結果は Apply で反映                                                                                                                                                                    |

#### Paste Solder の pad 編集（基板ビューア + 階層 override）

`paste_solder` ジョブのフォーム上部に pad 編集 UI（`partials/pad_editor.html` + `static/js/pad_editor.js`、専用テンプレ `pasting/paste_solder.html`）を載せ、塗布対象 pad の有効/無効と、pad 種類/部品ごとの塗布設定上書きを基板単位で行う。設定は基板ごとに永続化し、`paste_solder` 実行はその設定で塗布する（後述 §15）。

**基板ビューア**: SVG（`viewBox` を mm 系に一致させ pad の `polygon.exterior` を無変換描画、`vector-effect=non-scaling-stroke`）。Top / Bottom 切替。

- pad クリック = 単 pad の有効/無効を即トグル
- 左ドラッグ = 矩形選択（交差判定。修飾なし=置換 / Shift=追加 / Alt=除外）→「選択を有効化 / 無効化」「全有効 / 全無効」ボタンで一括
- 「順路計算」は有効 pad の塗布順を SVG overlay 表示する。「塗布パス計算」は有効 pad のみを対象に、pad ごとの解決済み `bead_width_factor` / `overlap` / `boundary_margin` と machine の `nozzle_diameter` から fill path を SVG overlay 表示する。layer・enabled・設定値変更時は overlay を消し、再計算を要求する
- ビューア ⇄ 階層表の選択ハイライト連動。所属判定は API が返す `pad.node_ids` の membership を使う
- 主要 DOM には `data-testid` を持たせ、SVG 表示・階層 highlight・ジョブ中 lock・responsive layout を実ブラウザ E2E で固定する

**階層 override 表**: 5 階層を下位ほど優先（override）で解決する。

- **L0** 全部品デフォルト → **L1** 同規格 package（例 `0402`）→ **L2** 各コンポーネント（designator）→ **L3** コンポーネント内の同形状 pad（`PadShapeKey` で分類。熱パッドと信号ピンを区別）→ **L4** 個別 pad
- 各項目は非 None 値で上書き。**`enabled` は最具体レベルの明示値が勝つ**（L2 で false でも L4 で true なら有効）
- 表示: 継承 = 薄字 placeholder（祖先チェーンを client 合成）/ このノードの override = `*` バッジ、値セルは ●（濃字）+ × でクリア（継承に戻す）/ 子孫ノードに override がある祖先行・項目 = `v` バッジ（件数・対象項目を tooltip 表示）/ 無効行 = 灰色 + 取消線 / 無効ノードの編集時、および子孫 override がある項目を祖先側で編集した時は警告 toast
- 編集は **PATCH で即時保存**。`window.webui.jobs.onUpdate` 購読で**ジョブ実行中は編集ロック**

**override 対象 7 項目**: `fill_speed` / `paste_height` / `ul_per_mm2` / `prime_extra_delay` / `bead_width_factor` / `overlap` / `boundary_margin`（`PASTE_OVERRIDE_FIELDS`）。マシン固定（設定ページ §8 の `[paste_dispenser]` のまま、pad ごとに変えない）: `rotations_per_ul` / `nozzle_diameter` / `toolhead` / `pad_align`、および吐出・リトラクト動特性（`max_dispense_rate` / `dispense_accel` / `retract_*`）。

**責務境界**: 階層 group-by（`pcbasm.pcb.grouping`）と override 解決（`pcbasm.pasting.settings`）は装置非依存の純ロジックとして pcbasm に置き、塗布実行（§15）と UI が**同一の解決規則**を共有する。永続化・API・UI は webui。

### dev タブ

| 項目                   | 形態                                                                                                  |
| ---------------------- | ----------------------------------------------------------------------------------------------------- |
| Extract PCB            | ジョブ（装置非使用）。結果 PNG をインライン表示                                                       |
| Make Fill Coverage PCB | ジョブ（非装置）。`data/webui/` に生成しダウンロードリンク                                            |
| Klipper / Stage Status | ステータスカード（`/api/klipper/status` をポーリング）+ 任意 G-code 送信ボックス（klipper_demo 代替） |

> Generate Grid PCB は位置合わせタブへ移設（feature のタブ所属は `pages.py` の `TABS` が真。サイドバー / 見出しの日本語表示名は `FEATURE_LABELS`）。

### pnp タブ

プレースホルダ（「未実装」表示）のみ。

### セッションの扱い

キャリブレーション結果（Board 変換等）はジョブ間で保持しない。Board 計測はステージ状態に依存し、ジョブをまたいだ流用はズレ事故のもとになるため、各装置ジョブが `setup_board_calibration(camera=hub.subscribe(), frame_sink=ctx.frame)` から実行する。現行スクリプトと同じ安全側のフロー。

## 11. 検証戦略

skill `testing-strategy` のテスト 4 区分に従う。

### pytest 層

- **unit**: `config_store` のホワイトリスト解釈・tomlkit 書き戻し、`JobManager` の状態遷移、`FrameHub` のカーソル管理。FakeCamera（自前 `Camera` ABC の test Impl）は `tests/helpers.py` に配置
- **integration-with-fakes**: `fastapi.testclient.TestClient` + 注入 Settings（tmp_path にコピーした test-fixture configs、FakeCamera）で API〜設定ファイル書き込み〜WS イベントまでの結合を検証。`make test-no-hardware` の主体
- **e2e**: `pytest-playwright` + 実 uvicorn + fake camera + 実 Chromium で HTTP / WebSocket / MJPEG / DOM / SVG / responsive layout を検証。`make test-e2e` で実行し、Chromium は `/usr/bin/chromium` を優先する。無い環境では `make playwright-install` で Playwright 管理 Chromium を入れる
- **integration-hardware**: FrameHub × 実カメラのスモーク、Klipper status / FIRMWARE_RESTART 疎通など。`@mark_hardware` + `skip_if_no_*` で gating。**実機テストの実行はユーザーが行う**
- 3rd-party 表面（picamera2, cv2, Moonraker REST）のモックは作らない。Moonraker が絡む結合は実機区分へ寄せる
- `tests/webui/` は `src/webui/` を 1 対 1 でミラーする

### ブラウザ E2E と手動確認

`make test-e2e` は pytest fixture 内で uvicorn を 127.0.0.1 のエフェメラルポートに起動し、tmp_path にコピーした test-fixture と fake camera を使う。実機設定や常駐サーバーを汚さず、有限コマンドとして起動から停止まで完結する。

- `tests/e2e/test_webui_e2e.py`: ページ配信、実 HTTP の preview snapshot/MJPEG、WebSocket ジョブ lifecycle、pad-config の HTTP roundtrip
- `tests/e2e/test_paste_solder_browser.py`: PCB 選択後の `paste_solder` DOM/SVG 表示、Top/Bottom 切替、pad click、bulk enable/disable、L2/L3/L4 の `node_ids` membership highlight、fake preview image、ジョブ中 lock、desktop/tablet/mobile の横溢れ検出
- 手動確認は `make webui-fake` を使う。fake camera と隔離 data_dir で起動するため、ブラウザから UI polish や操作感を確認しやすい

装置を動かすフロー（posctrl 補正・塗布・Klipper FIRMWARE_RESTART）の実機検証はユーザーが実施する。

## 12. 段階実装ロードマップ

各 Phase の完了条件 = `make format && make type && make test-no-hardware` 通過 + §11 の Claude 自身による E2E 手順の通過。

**進捗: Phase 1〜5 すべて実装完了（2026-06-12）。** pnp タブはプレースホルダのまま将来実装。実機での通し確認（塗布・キャリブレーション・scripts 回帰）はユーザー実施分が残る（各 Phase の `memory/agents/plan-implementer/webui-phase*.md` 参照）。

**Phase 5 完了後の追加機能（2026-06-15）**: `paste_solder` の pad 有効/無効 + 階層 override 塗布設定（§10「Paste Solder の pad 編集」・§15）。pcbasm 純ロジック（grouping / settings）+ webui 永続化・API・UI・ジョブ統合。

### Phase 1: 骨格 + 状態管理 + 設定画面

- 依存追加、`webui/` 骨格、`base.html` + 4 タブ + サイドバー、AppState、マシン / PCB 選択 + ファイルブラウザ、E-STOP、Klipper ステータスカード、設定画面（`config_store.py`、machine.toml / printer.cfg のホワイトリスト編集）、`configs/test-fixture/` 新設、Settings 注入フック、Makefile ターゲット
- **マシン操作パネル**（homing / ジョグ / 絶対移動 / relax / フォーカス Z、`POST /api/machine-control`）。排他ロックは AppState に置き、Phase 3 の JobManager が同じロックを共有する
- E2E: ページ巡回 + マシン選択 + 設定 PUT → test-fixture の実ファイル diff 確認（FIRMWARE_RESTART・実移動の実機確認のみユーザー）

### Phase 2: FrameHub + MJPEG preview

- `pcbasm/hal/framehub.py` + FakeCamera ユニットテスト（複数 subscriber、新フレーム待ち、エラー伝播、start/stop 冪等）。実カメラスモークは `@mark_hardware`
- `PreviewService`（参照カウント、オーバーレイ、オーバーライドスロット）、stream / snapshot エンドポイント、posctrl の Camera Preview / Copper Detection ページ
- E2E: FAKE_CAMERA 起動で MJPEG 取得・overlay 切替・参照カウントによる hub 停止をログで確認

### Phase 3: ジョブ基盤 + dev タブ

- JobManager / JobContext / WS / prompt / abort + Apply/Discard 機構（`/api/jobs/last/apply`）。装置を使わない dev 系ジョブを最初の実装対象にして基盤を固める
- E2E: dev ジョブを WS クライアントで通し実行（prompt 往復・abort・409・成果物生成）。Apply はスタブジョブで test-fixture への書き込みまで確認

### Phase 4: posctrl

- `setup.py` への camera / frame_sink 注入、`PadAligner` / `PadAlignmentSession` への frame_sink 注入（既存テスト・scripts の無風確認込み）
- reference_point_setup（ジョグ + Record。machine.toml 書き込みは Apply フロー）、camera_calibration、board_tour（四隅 + 銅箔照合 + 補正巡回）、orthogonality_test、Copper Detection の調整値保存
- E2E: FakeCamera + 録画画像で frame_sink 経路と WS 配信を確認。実機フローはユーザー

### Phase 5: pasting + 仕上げ

- `PasteSession` 経由の 6 ジョブ（flow_calibration / toolhead_offset / probe_gnd_down_adjust の Apply 反映含む）、成果物配信（`data/webui/` + artifacts ルート）、pnp プレースホルダ、README / CLAUDE.md への起動手順追記
- E2E: 装置非依存部分（フォーム → ジョブ起動 → prompt シーケンス）まで。実機塗布はユーザー

## 13. 依存追加

`pyproject.toml` の dependencies へ:

```
fastapi>=0.115
uvicorn[standard]>=0.34   # WS 用（websockets 同梱）
jinja2>=3.1
tomlkit>=0.13             # scripts が既に import しているが宣言漏れのため明示追加
```

- dev dependency に `pytest-playwright` を追加し、実 Chromium で browser E2E を実行する
- Node / npm / Vite / React は導入しない。装置制御 UI と Raspberry Pi 運用では、ビルド工程より実ブラウザ E2E と整理された buildless module 分割を優先する
- pydantic は fastapi 同梱。pytest-asyncio は不要（sync TestClient / Playwright sync API / websockets sync API で検証可）
- `[tool.uv.build-backend]` の module-name 追加は wheel 配布が必要になった時点で行う（editable 運用の現状では不要）

## 14. 主要トレードオフと採択理由

| 論点                    | 採択                                 | 理由                                                                           |
| ----------------------- | ------------------------------------ | ------------------------------------------------------------------------------ |
| FrameHub の Camera 互換 | `subscribe() -> FrameSource(Camera)` | 消費者ごとカーソルで重複フレーム・奪い合いを防ぎつつ既存検出コードへ無改造注入 |
| FrameHub の配置         | `pcbasm/hal/`                        | デバイスアクセス直列化は HAL の関心事。vision 非依存                           |
| posctrl 改修範囲        | frame_sink / camera の引数注入のみ   | 既存 scripts・テスト無風で WebUI 要件を満たす最小差分                          |
| フロント                | Jinja2 + buildless ES modules        | 動的部分は WS / MJPEG / ジョグで命令的 JS が本体。ビルド工程ゼロ               |
| ジョブ実行              | threading.Thread + queue 橋渡し      | pcbasm が同期コードのため。asyncio 化は改修範囲が爆発する                      |
| WS                      | グローバル 1 本                      | 同時ジョブ 1 件なので分離の利点なし。E-STOP / 状態通知も同送                   |
| セッション保持          | ジョブごとに再計測                   | キャリブレーション流用はズレ事故リスク。現行スクリプトと同じ安全側             |
| ジョブ履歴              | 直近 1 件のみ・非永続                | 単一オペレータの装置 UI に履歴 DB は過剰                                       |
| マシン操作の排他        | ジョブと同一ロックの単発 REST        | 移動とジョブの同時実行を構造的に排除。対話ジョブ中のみ WS command に切替       |
| 設定編集                | ホワイトリスト方式                   | machine.toml / printer.cfg の存在を隠しつつ、壊れる編集を構造的に防ぐ          |

## 15. はんだ塗布の pad 設定（Phase 5 完了後の追加機能）

`paste_solder` を「全 Top pad を単一 `[paste_dispenser]` で一律塗布」から「**基板ごとの pad 有効/無効 + 階層 override 設定**で塗布」へ拡張する。UI は §10、API は §9 を参照。本節は永続化・API 契約・塗布実行統合をまとめる。

### 永続化（`board_settings.py`）

`BoardSettingsStore(data_dir)` が基板ごとの設定を JSON で保存する。

- 保存先 `data_dir/board_settings/<machine>/<board_id>.json`（`board_id` = `source_pcb`（PCB browse root からの相対 posix パス）の SHA-256 先頭 16hex）。`version` / `source_pcb` / `machine` を付与
- `load_or_init`: ファイルが在れば復元、無ければ `machine.toml` の `[paste_dispenser]` 値を L0 デフォルトに据えた新規モデルを返す（**この時点では保存しない** = 編集が入るまでファイルを作らない）
- 真実の源は基板ごとの JSON。初回 bootstrap 以降は基板固有値を尊重し machine.toml から独立する（再現性）
- 解決モデルは pcbasm の純ロジック: `PasteSettingsModel`（`base` = 全項目確定の L0 / `base_enabled` / `levels` = L1–L4 の疎マップ）と `resolve_pad_settings(hierarchy, model)`。階層は `build_pad_hierarchy(components, pads)`

### API 契約（node_id 規約）

`HierKey` tuple ⇔ node_id 文字列は `":".join(key)` / `tuple(node.split(":"))`。

- node_id: `L0` / `L1:{package}` / `L2:{designator}` / `L3:{designator}:{shape_label}` / `L4:{designator}:{pad_number}`
- pad id: `{designator}.{pad_number}`
- `GET /api/pasting/pad-config` は outline + pads（ジオメトリ + 解決済み enabled/resolved + `node_ids`）+ 階層 tree（構造のみ）+ 疎 overrides（L0 は常に存在、L1–L4 は明示設定があるノードのみ）を 1 発で返す。`node_ids` は各 pad が属する L0〜L4 node_id の配列で、L3 shape preview は designator ではなくこの membership で判定する
- `PATCH .../node` は ノードの enabled / values(upsert) / clear(継承へ戻す) を適用し即保存、応答 `affected_pads` で配下 pad を部分更新（再 GET 不要）。`PATCH .../pads` は pad id 配列を L4 ノードの enabled として一括適用
- 検証: PCB 未選択 409 / 未知 override 項目・未知 node 400 / L0 の `enabled=null` 400
- `pcbnew`（PCB 読込）はリクエスト時に遅延 import するため、ルーター自体は KiCAD 未導入環境でも import 可能

### 塗布実行統合（`jobs/pasting.py` の `_run_paste_solder`）

`PasteApplicator.apply(polygons, *, fill_speed=None, ..., boundary_margin=None)` に per-pad override 引数（7 項目、`None` = `__init__` 値）を追加済み。`_run_paste_solder` は:

1. `build_pad_hierarchy` + `BoardSettingsStore.load_or_init` + `resolve_pad_settings`（装置不要・前段で解決。store/`source_pcb` は `JobContext` 経由で配線）
2. **有効 pad のみ**抽出（無効除外はここ一点。階層から除外された pad は後方互換で有効扱い）。有効 pad を 1 つ以上持つ部品のみ銅箔照合
3. 補正 transform 適用後、各 pad を解決済み設定で `applicator.apply([polygon], ...)`
4. summary に「有効 N / 全 M pads」を表示

- **フォールバック**: 設定ファイル不在（または store/`source_pcb` 未配線）時は machine.toml デフォルトで全 pad 有効 = **現行と等価**
