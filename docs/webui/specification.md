# WebUI 仕様書

PCB アセンブリ装置の操作を Web ブラウザから行うための WebUI（`src/webui/`）の全体仕様と段階実装ロードマップ。

## 1. 背景と目的

`src/scripts/` の 18 スクリプト（dev 6 / pasting 6 / posctrl 6、pnp は将来用）は開発者向けの単発実行ツールとして成長してきた。これらの機能をブラウザから操作できるようにする。

- scripts は単発実行ライブラリとして温存する。WebUI のロジックは `src/webui/` に**完全に別実装**する（コアロジックは pcbasm を再利用）
- 現在は posctrl の補正処理中（30 フレーム連続キャプチャ等）にカメラ preview が止まる。WebUI では**位置合わせ中も常時 preview が走る**ことを必須要件とする
- WebUI ユーザーには `machine.toml` / `printer.cfg` といった裏側のファイルの存在を意識させない

### スコープ

- `src/webui/` 新設（FastAPI + Jinja2 + vanilla JS、Node ビルド不要）
- pcbasm 側の最小改修 2 点: FrameHub の新設（共通部品）、posctrl の表示責務分離（frame_sink 注入）
- マシン設定の閲覧・編集（machine.toml + printer.cfg の限定項目）、計測結果の設定反映フロー
- PCB ファイル選択、Klipper console（Mainsail）へのリンク、Emergency Stop

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
  ├─ posctrl/ pasting/ vision/ pcb/ geometry/  コアロジック（ほぼ無改造）
  └─ config.py         get_machine_config()
```

- ポートは 8080（Moonraker 7125 / Mainsail 80 と非衝突）
- フロントは Jinja2 + vanilla JS のみ。動的部分（WS ジョブコンソール・MJPEG・ジョグ）は命令的 JS が本体であり、SPA フレームワークや htmx は採用しない
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

## 4. posctrl の表示責務分離（pcbasm 側の最小改修）

現状、`OffsetObserver`（`src/pcbasm/posctrl/setup.py`）と `PadAligner`（`src/pcbasm/posctrl/pad.py`、`window_name` 指定時）が `cv2.imshow` を直接呼び、`setup_board_calibration()` が内部で `create_camera()` を直接生成している。後方互換のまま注入点を開ける。

1. `OffsetObserver.__init__` に `frame_sink: Callable[[Image], None]` を追加。cv2 表示はデフォルトシンク（setup.py 内のプライベートヘルパ）として切り出す
2. `setup_board_calibration(..., *, camera: Camera | None = None, frame_sink: Callable[[Image], None] | None = None)` を追加
    - `camera=None` なら従来どおり `create_camera()`（既存 scripts 無変更）。webui は `hub.subscribe()` を渡す
    - `frame_sink=None` なら従来どおり cv2 ウィンドウ。webui はジョブコンテキストの `ctx.frame` を渡す
3. `PadAligner` / `PadAlignmentSession`（`src/pcbasm/posctrl/alignment.py`）に `frame_sink` を追加。照合状況の注釈付き画像（ROI 枠 + エッジ重畳）を `window_name` の cv2 表示と同列のシンクとして流す（`window_name=None, frame_sink=ctx.frame` が webui の使い方。両方 None なら表示なしで従来どおり）
4. `PasteSession.setup()`（`src/pcbasm/session.py`）に同じパススルーを追加
5. `machine_session` / `PasteSession.__exit__` の `cv2.destroyAllWindows()` はウィンドウが無ければ no-op なので触らない
6. `tour.py` / `board_tour.py` の表示ループ（`display_at_point`、`_show_pad_result` 等）は表示そのものが責務なので pcbasm は触らず、webui 側で「移動 → FrameSource から取得 → オーバーレイ描画 → preview へ push」を再実装する

検出・補正のコア（`CircleDetector`, `CopperEdgeDetector`, `CopperEdgeMatcher`, `CopperProjector`, `XYPositionAdjustor`, `OffsetTransformMeasurer`, `BoardTransformMeasurer`, `HeightPlaneMeasurer`）は元から表示非依存であり無改造で再利用する。

## 5. webui/ モジュール構成

```
src/webui/
├── __init__.py
├── __main__.py            # python -m webui → uvicorn.run
├── app.py                 # create_app() ファクトリ、lifespan で AppState 構築/破棄
├── settings.py            # ポート、configs ルート、PCB ブラウズ root、Mainsail URL（env 上書き可）
├── state.py               # AppState: 選択マシン/PCB、Camera+FrameHub、JobManager、JSON 永続化
├── config_store.py        # machine.toml / printer.cfg のホワイトリスト読み書き
├── preview.py             # PreviewService: 参照カウント、MJPEG、オーバーレイ
├── jobs/
│   ├── manager.py         # JobManager / JobRecord / JobStatus
│   ├── context.py         # JobContext（log/progress/frame/prompt/command/checkpoint）
│   ├── catalog.py         # ジョブ名 → JobDefinition（実体 + パラメータスキーマ）レジストリ
│   ├── posctrl.py         # posctrl 系ジョブ実装
│   ├── pasting.py         # pasting 系ジョブ実装
│   └── dev.py             # dev 系ジョブ実装
├── routers/
│   ├── pages.py           # GET /{tab}, /{tab}/{feature}, /settings（Jinja2）
│   ├── machine.py         # マシン選択
│   ├── files.py           # PCB ファイルブラウザ
│   ├── jobs.py            # ジョブ REST + WS /api/ws
│   ├── preview.py         # MJPEG / snapshot
│   ├── settings_api.py    # マシン設定・モーション設定の取得/保存
│   ├── machine_control.py # マシン操作パネル（homing/ジョグ/移動/relax/フォーカスZ）
│   └── system.py          # emergency stop、Klipper ステータス
├── templates/
│   ├── base.html          # ヘッダ（タブ・マシン選択・PCB チップ・設定・console リンク・E-STOP）
│   ├── {dev,pasting,pnp,posctrl}/…
│   ├── settings.html
│   └── partials/          # サイドバー、job console、preview ペイン、prompt モーダル、マシン操作パネル
└── static/
    ├── app.css
    └── js/
        ├── job_console.js # WS クライアント（ログ/進捗/プロンプト/中止/Apply）
        ├── preview.js     # <img> の付け外し・overlay 切替
        └── machine_control.js  # マシン操作パネル（REST / ジョブ command の送信切替）
```

### テスト容易性のフック

- `create_app(settings: Settings)` で configs ルート・data ディレクトリ・カメラファクトリを注入可能にする（既定は本番値）
- env による起動時切替:
    - `PCBASM_WEBUI_CONFIGS_ROOT` — configs ルートの差し替え
    - `PCBASM_WEBUI_FAKE_CAMERA=1` — 固定画像を返す FakeCamera で FrameHub を構成。カメラ実機なしで preview / MJPEG / オーバーレイを通しで検証できる
- `configs/test-fixture/` を新設（machine.toml + printer.cfg を持つフィクスチャマシン、git 管理）。設定編集・Apply フローの検証はこのマシンに対して行い、実マシン設定（kurousagi / pd_china_frame）を汚さない。pytest では tmp_path にコピーして使用、手動 E2E では編集後 `git checkout` で復元する

## 6. ジョブ実行基盤

### 排他とライフサイクル

- 装置を動かすジョブは**同時 1 件**。`JobManager` が `threading.Lock` を非ブロッキング取得し、競合は HTTP 409
- preview はロック対象外（FrameHub 読みのみで装置を動かさない）。マシン切替・PCB 切替・設定保存もジョブ実行中は 409
- ライフサイクル: `PENDING → RUNNING ⇄ WAITING_INPUT → SUCCEEDED | FAILED | ABORTED`
- ワーカーは `threading.Thread` 1 本（pcbasm は全て同期コードのため。asyncio 化はしない）
- `JobRecord`: id, name, params, status, ログのリングバッファ（直近 N 行）, result, error。**直近 1 件のみ保持・非永続**

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
```

- スレッド間は `queue.Queue`（イベント出力）+ `threading.Event` / `Queue`（prompt 応答・command 入力）。Web 側（asyncio）へは `asyncio.run_coroutine_threadsafe` で WS にブロードキャスト
- `input()` の代替: flow_calibration の重量入力 → `prompt(number)`、height_plane の Y/n → `prompt(confirm)`、probe_gnd_down_adjust の距離調整 → `prompt(number)` の繰り返し
- 連続対話（ジョグ）: reference_point_setup はジョブとして起動後 `next_command()` ループで `{type:"jog", axis, dist}` / `{type:"record"}` / `{type:"quit"}` を消費。preview には `ctx.frame()` で円検出オーバーレイを流し続ける
- 中止（abort）は協調的: フラグを立て、`prompt` / `next_command` 待ちは即 JobAborted 化、長い処理はループ内 `checkpoint()`。終了処理（finally）で M84（relax）を送る

### マシン操作パネル（ジョブ外の単発操作）

全タブから使える共通コンポーネント（サイドバー下部に折りたたみで常設）。Klipper console（Mainsail）リンクは残しつつ、基本操作は console に飛ばずに完結できるようにする。

- **Homing**: X / Y / Z 軸個別 + 全軸（`gcode.homing()`）
- **ジョグ**: X / Y / Z 共通で ±0.1 / ±1 / ±10 mm の 6 ボタン × 3 軸。`XYZStage.move(relative=True)` を使う（limits 検証込み。マシンサイズが小さいためこの 3 段で足りる）
- **座標直接入力**: x / y / z の数値フィールド + 移動ボタン（絶対座標。空欄の軸は現在位置を維持 = `move()` の None 渡し）
- **Relax**: M84
- **フォーカス位置へ**: 選択マシンの calibration（`CalibrationResult.z_position` — camera_calibration 撮影時の Z 値）へ Z を移動。calibration 未設定や `z_position` が無い場合はボタンを無効化
- パネルには現在位置と homed_axes を表示（`/api/klipper/status` をパネル展開中のみポーリング）

実装と排他:

- `POST /api/machine-control` ボディ `{action: "home" | "jog" | "move" | "relax" | "focus_z", …}`。1 リクエスト = 1 同期操作（G-code 送信 + `wait_for_done`、完了でレスポンス）
- **ジョブと同じ排他ロックを共有する**: ジョブ実行中は 409 を返し、UI はパネルを disabled 表示にする。逆にマシン操作の実行中（移動完了待ち）もジョブ開始は 409
- 未ホーミング軸への移動や limits 超過は `XYZStage` / Klipper のエラーをそのままエラートーストで表示する
- reference_point_setup 等の**対話ジョブ中**のジョグは従来案どおり WS の `command`（`next_command`）経由。UI は同じパネル部品を「ジョブモード」へ切り替えて使う（送信先が REST か WS かの違いのみで、ボタン構成は共通）

### Emergency Stop

ジョブ機構を**経由しない**。`POST /api/emergency-stop` がその場で `Klipper.emergency_stop()` を直接叩く（Moonraker は REST なのでジョブと独立に届く）。同時に abort フラグも立てる。UI のボタンは赤・常時表示・確認なし即時。

### WebSocket

グローバル 1 本 `WS /api/ws` をマルチプレクスする（同時ジョブ 1 件なのでチャネル分離の利点がなく、E-STOP・状態変更通知も同送できる）。再接続時は `GET /api/jobs/current` で状態を取り直す。

- サーバー → クライアント: `job_status`, `log`, `progress`, `prompt`, `prompt_resolved`, `state_changed`
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

ユーザーには「マシン設定」という一つの画面として見せ、裏が machine.toml / printer.cfg であることは意識させない。

### 設定画面（ヘッダの歯車 → `/settings`）

- machine.toml の編集可能項目を**ホワイトリスト化したフォーム**で表示: ディスペンサー諸元 `[paste_dispenser]`、pad 照合パラメータ `[paste_dispenser.pad_align]`（canny 閾値等）、プローブ `[probe]`、基準点 `[reference_point]`、カメラ `[camera]` 等。保存は tomlkit でコメント・構造を保持して書き戻す（既存パターン: `update_reference_point`（`src/scripts/posctrl/reference_point_setup.py`）の一般化）
- printer.cfg は「モーション設定」セクションとして**限定編集**。初期ホワイトリスト: `[printer] max_velocity / max_accel`、`[manual_stepper paste_dispenser] velocity / accel`（ホワイトリストは拡張可能な定義方式）
    - 編集対象は `configs/<選択マシン>/printer.cfg`。`~/printer_data/config/printer.cfg` はここへの symlink（`install-printer-cfg.sh` 方式）なので、保存 → Klipper RESTART で反映される
    - 保存時は確認ダイアログ付きで Moonraker 経由の Klipper RESTART を実行
    - symlink が選択マシンを指していない場合は警告を表示する
- 読み書きは `webui/config_store.py` に集約（ホワイトリスト定義 + tomlkit 書き込み + printer.cfg の限定パーサ/ライタ）。ジョブ実行中の設定保存は 409
- 設定変更後はマシン設定を再ロード（AppState の Machine 再構築。カメラ設定が変わった場合は FrameHub/Camera を再生成）

### 計測結果の Apply / Discard フロー

キャリブレーション系ジョブは SUCCEEDED 時に result へ「設定反映ペイロード」（toml キーパスと値、表示用ラベル）を含め、ジョブコンソールに計測値と「設定に反映」/「破棄」ボタンを表示する。`POST /api/jobs/last/apply` で machine.toml へ書き込む。反映可能なのは直近完了ジョブのみ（次のジョブ開始または破棄で無効化）。

| ジョブ                | 反映先                                                                        |
| --------------------- | ----------------------------------------------------------------------------- |
| camera_calibration    | calibration JSON を `configs/<machine>/` へ保存 + `[camera].calibration_file` |
| toolhead_offset       | `[paste_dispenser.toolhead]` x, y                                             |
| flow_calibration      | `[paste_dispenser].rotations_per_ul`                                          |
| reference_point_setup | `[reference_point]` x, y                                                      |
| probe_gnd_down_adjust | `[probe].down_distance`                                                       |

## 9. REST / WS API

| Method  | Path                                      | 内容                                                                    |
| ------- | ----------------------------------------- | ----------------------------------------------------------------------- |
| GET     | `/`                                       | `/posctrl` へリダイレクト                                               |
| GET     | `/{tab}`, `/{tab}/{feature}`, `/settings` | Jinja2 ページ                                                           |
| GET     | `/api/state`                              | 選択マシン・PCB・現行ジョブ要約・preview クライアント数                 |
| GET     | `/api/machines`                           | `configs/*/machine.toml` を列挙                                         |
| GET/PUT | `/api/machine`                            | マシン選択（ジョブ中 409。FrameHub/Camera 再構築）                      |
| GET     | `/api/files?path=`                        | dir + `*.kicad_pcb` のみ列挙。PROJECT_ROOT 配下に制限（traversal 防止） |
| PUT     | `/api/pcb-file`                           | PCB 選択（ジョブ中 409）                                                |
| POST    | `/api/jobs/{name}`                        | ジョブ開始 → 201 `{job_id}` / 409                                       |
| GET     | `/api/jobs/current`                       | 状態・ログ末尾・pending prompt（WS 再接続時の同期用）                   |
| POST    | `/api/jobs/current/abort`                 | 協調的中止                                                              |
| POST    | `/api/jobs/last/apply`                    | 直近完了ジョブの計測結果を設定へ反映                                    |
| GET/PUT | `/api/settings/machine`                   | マシン設定（machine.toml ホワイトリスト項目）                           |
| GET/PUT | `/api/settings/motion`                    | モーション設定（printer.cfg 限定項目）+ RESTART                         |
| POST    | `/api/machine-control`                    | homing / ジョグ / 絶対移動 / relax / フォーカス Z（§6。ジョブ中 409）   |
| POST    | `/api/emergency-stop`                     | 即時 M112 相当（ジョブ非経由）                                          |
| GET     | `/api/preview/stream?overlay=`            | MJPEG                                                                   |
| GET     | `/api/preview/snapshot?overlay=`          | JPEG 1 枚（スポット確認用）                                             |
| GET     | `/api/klipper/status`                     | position / homed_axes 等（ステータスカード用）                          |
| WS      | `/api/ws`                                 | §6 のイベント / コマンド                                                |

- API ボディは JSON 統一。レスポンスモデルは FastAPI 標準の pydantic を **webui の API 境界のみ**で使用する（pcbasm の attrs/cattrs とは層が違うため混在を許容）
- Klipper console リンク: `MAINSAIL_URL`（既定 `http://localhost`、env `PCBASM_MAINSAIL_URL` で上書き）への `<a target="_blank">`

## 10. UI 構成

### レイアウト

- **ヘッダ**: タブ（dev / pasting / pnp / posctrl）+ マシン選択ドロップダウン + PCB ファイルチップ（クリックでファイルブラウザモーダル）+ 設定（歯車 → `/settings`）+ Klipper console リンク + **E-STOP（赤・常時表示・確認なし即時）**
- **サイドバー**: タブ内の feature リスト。実行中ジョブがあればバッジ表示。下部に**マシン操作パネル**（§6。折りたたみ、全タブ共通）
- **メインペイン**: feature ごとに「パラメータフォーム（argparse 引数から導出、デフォルト値も引き継ぐ）+ 実行ボタン + ジョブコンソール（ログ / 進捗バー / プロンプトモーダル / 中止 / Apply）+ 必要なら preview ペイン」
- machine / pcb-file はグローバル選択値のため各フォームから除外。`data/webui_state.json` にこの 2 値のみ永続化（起動時に復元）

### posctrl タブ（常時 preview ペインあり）

| 項目                  | 形態         | フォーム / 操作                                                                                                                                                     |
| --------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Camera Preview        | preview のみ | overlay 切替（none / crosshair）                                                                                                                                    |
| Copper Detection      | preview のみ | overlay=copper + Canny low / high スライダー（動的反映）。「設定に保存」で `[paste_dispenser.pad_align]` へ書き込み（現行スクリプトの調整専用フローを Apply 化）    |
| Camera Calibration    | ジョブ       | crop サイズ等。prompt(confirm) で撮影を進行。結果は Apply で反映                                                                                                    |
| Reference Point Setup | 対話ジョブ   | マシン操作パネル（ジョブモード、±0.1/1/10mm）+ Record / Quit。preview に円検出 + 現在位置                                                                           |
| Board Tour            | ジョブ       | tolerance。四隅巡回 → 部品単位の銅箔照合（`PadAlignmentSession`）→ 補正適用済み全 pad 巡回。各点 1 秒の自動進行、照合 overlay を `ctx.frame()` で配信。中止は abort |
| Orthogonality Test    | ジョブ       | tolerance。結果数値を result 表示                                                                                                                                   |

### pasting タブ（preview はジョブ提供フレームのみ）

| 項目                  | フォーム / 操作                                                                                                                 |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Paste Solder          | tolerance, amount, interactive-loading。進捗 = 計測 → pad 位置合わせ（`PadAlignmentSession`）→ ローディング → 塗布の stage 表示 |
| Height Plane          | tolerance, output。完了後ヒートマップ PNG をインライン表示（`data/webui/` へ保存し artifacts ルートで配信）                     |
| Loading               | amount。コマンドボタン（押出 / 吸引 / 量変更 / 終了）= `next_command` 駆動                                                      |
| Flow Calibration      | rotations, rate, accel, load-amount。重量入力 prompt(number)。結果は Apply で反映                                               |
| Toolhead Offset       | tolerance, dispense-amount, loading-amount, lift-height, paste-diameter-min/max。結果は Apply で反映                            |
| Probe GND Down Adjust | prompt(number) ループで down distance 調整。結果は Apply で反映                                                                 |

### dev タブ

| 項目                                       | 形態                                                                                                  |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| Extract PCB                                | ジョブ（装置非使用）。結果 PNG をインライン表示                                                       |
| Fill Path Simulate                         | ジョブ（非装置）。パラメータ多数、結果 PNG 表示                                                       |
| Generate Grid PCB / Make Fill Coverage PCB | ジョブ（非装置）。`data/webui/` に生成しダウンロードリンク                                            |
| Klipper / Stage Status                     | ステータスカード（`/api/klipper/status` をポーリング）+ 任意 G-code 送信ボックス（klipper_demo 代替） |

### pnp タブ

プレースホルダ（「未実装」表示）のみ。

### セッションの扱い

キャリブレーション結果（Board 変換等）はジョブ間で保持しない。Board 計測はステージ状態に依存し、ジョブをまたいだ流用はズレ事故のもとになるため、各装置ジョブが `setup_board_calibration(camera=hub.subscribe(), frame_sink=ctx.frame)` から実行する。現行スクリプトと同じ安全側のフロー。

## 11. 検証戦略

skill `testing-strategy` のテスト 4 区分に従う。

### pytest 層

- **unit**: `config_store` のホワイトリスト解釈・tomlkit 書き戻し、`JobManager` の状態遷移、`FrameHub` のカーソル管理。FakeCamera（自前 `Camera` ABC の test Impl）は `tests/helpers.py` に配置
- **integration-with-fakes**: `fastapi.testclient.TestClient` + 注入 Settings（tmp_path にコピーした test-fixture configs、FakeCamera）で API〜設定ファイル書き込み〜WS イベントまでの結合を検証。`make test-no-hardware` の主体
- **integration-hardware**: FrameHub × 実カメラのスモーク、Klipper status / RESTART 疎通など。`@mark_hardware` + `skip_if_no_*` で gating。**実機テストの実行はユーザーが行う**
- 3rd-party 表面（picamera2, cv2, Moonraker REST）のモックは作らない。Moonraker が絡む結合は実機区分へ寄せる
- `tests/webui/` は `src/webui/` を 1 対 1 でミラーする

### Claude 自身による E2E 検証（各 Phase の受け入れ手順）

`PCBASM_WEBUI_FAKE_CAMERA=1 PCBASM_WEBUI_CONFIGS_ROOT=...` で uvicorn をバックグラウンド起動し、実 HTTP/WS に対して検証する。

1. curl で全ページ（タブ・feature・settings）の 200 とレンダリング内容を確認
2. curl で API シーケンス: マシン `test-fixture` 選択 → 設定 GET/PUT → `configs/test-fixture/machine.toml` の diff を実ファイルで確認（コメント保持も確認）→ `git checkout` で復元
3. dev ジョブ（extract_pcb 等、装置非依存）を POST で起動し、python WS クライアント（httpx / websockets のワンショットスクリプト）で log / progress / prompt 往復・abort・排他 409 を通しで確認。成果物 PNG の生成を確認
4. MJPEG ストリームを数フレーム取得し、multipart 境界と JPEG デコードを確認（FakeCamera）

装置を動かすフロー（posctrl 補正・塗布・Klipper RESTART）の実機検証はユーザーが実施する。

## 12. 段階実装ロードマップ

各 Phase の完了条件 = `make format && make type && make test-no-hardware` 通過 + §11 の Claude 自身による E2E 手順の通過。

### Phase 1: 骨格 + 状態管理 + 設定画面

- 依存追加、`webui/` 骨格、`base.html` + 4 タブ + サイドバー、AppState、マシン / PCB 選択 + ファイルブラウザ、E-STOP、Klipper ステータスカード、設定画面（`config_store.py`、machine.toml / printer.cfg のホワイトリスト編集）、`configs/test-fixture/` 新設、Settings 注入フック、Makefile ターゲット
- **マシン操作パネル**（homing / ジョグ / 絶対移動 / relax / フォーカス Z、`POST /api/machine-control`）。排他ロックは AppState に置き、Phase 3 の JobManager が同じロックを共有する
- E2E: ページ巡回 + マシン選択 + 設定 PUT → test-fixture の実ファイル diff 確認（RESTART・実移動の実機確認のみユーザー）

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

- `python-multipart` は不要（JSON 統一）。pydantic は fastapi 同梱。pytest-asyncio も不要（sync TestClient で WS テスト可）
- `[tool.uv.build-backend]` の module-name 追加は wheel 配布が必要になった時点で行う（editable 運用の現状では不要）

## 14. 主要トレードオフと採択理由

| 論点                    | 採択                                 | 理由                                                                           |
| ----------------------- | ------------------------------------ | ------------------------------------------------------------------------------ |
| FrameHub の Camera 互換 | `subscribe() -> FrameSource(Camera)` | 消費者ごとカーソルで重複フレーム・奪い合いを防ぎつつ既存検出コードへ無改造注入 |
| FrameHub の配置         | `pcbasm/hal/`                        | デバイスアクセス直列化は HAL の関心事。vision 非依存                           |
| posctrl 改修範囲        | frame_sink / camera の引数注入のみ   | 既存 scripts・テスト無風で WebUI 要件を満たす最小差分                          |
| フロント                | Jinja2 + vanilla JS                  | 動的部分は WS / MJPEG / ジョグで命令的 JS が本体。ビルド工程ゼロ               |
| ジョブ実行              | threading.Thread + queue 橋渡し      | pcbasm が同期コードのため。asyncio 化は改修範囲が爆発する                      |
| WS                      | グローバル 1 本                      | 同時ジョブ 1 件なので分離の利点なし。E-STOP / 状態通知も同送                   |
| セッション保持          | ジョブごとに再計測                   | キャリブレーション流用はズレ事故リスク。現行スクリプトと同じ安全側             |
| ジョブ履歴              | 直近 1 件のみ・非永続                | 単一オペレータの装置 UI に履歴 DB は過剰                                       |
| マシン操作の排他        | ジョブと同一ロックの単発 REST        | 移動とジョブの同時実行を構造的に排除。対話ジョブ中のみ WS command に切替       |
| 設定編集                | ホワイトリスト方式                   | machine.toml / printer.cfg の存在を隠しつつ、壊れる編集を構造的に防ぐ          |
