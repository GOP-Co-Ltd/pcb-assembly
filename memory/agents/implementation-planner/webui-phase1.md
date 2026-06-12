# WebUI Phase 1: 骨格 + 状態管理 + 設定画面 + マシン操作パネル

## 概要

`docs/webui/specification.md` §12 Phase 1 の実装計画。FastAPI ベースの `src/webui/` を新設し、ページ骨格・マシン/PCB 選択・設定編集（machine.toml / printer.cfg ホワイトリスト）・マシン操作パネル・E-STOP・Klipper ステータスを実装する。カメラ / FrameHub / ジョブ基盤は Phase 2/3 であり**一切実装しない**（pcbasm 側の変更もゼロ）。

前提（調査済み事実）:

- `src/` は `.pth` 経由で sys.path に載っており（`/home/gop/pcb-assembly/src`）、`src/webui/` を作れば `python -m webui` / `from webui import ...` が即座に通る。pyproject の build 設定変更は不要
- Klipper クライアントは `pcbasm.hal.Klipper`（Moonraker REST, `httpx.Client(timeout=None)`）、ステージは `pcbasm.hal.XYZStage`（`move()` が limits 検証込みで `GCode` を返す）。G-code 生成は `pcbasm.gcode` の `homing(x,y,z) / wait_for_done() / relax()`
- tomlkit 書き戻しの既存パターン: `src/scripts/posctrl/reference_point_setup.py` の `update_reference_point()`（`tomlkit.parse` → Table 更新 → `tomlkit.dumps`。コメント保持）
- フォーカス Z は `pcbasm.vision.CalibrationResult.load(machine.camera.calibration_file).z_position`（`float | None`）
- `~/printer_data/config/printer.cfg` は `configs/<machine>/printer.cfg` への symlink（`install-printer-cfg.sh`）
- pytest は `--strict-markers`・`--doctest-modules`（testpaths=tests のみ）・`hardware` マーカー登録済み。`tests/helpers.py` に `mark_hardware` / `FakeCamera` あり

## 設計判断（spec で「判断して明記」とされた点）

| 論点 | 判断 | 理由 |
| --- | --- | --- |
| カメラ/FrameHub の構築フック | **Phase 1 では用意しない**。`Settings` / `AppState` にカメラ関連フィールドを置かず、Phase 2 で追加 | CLAUDE.md「投機的な実装はしない」。AppState への後付けは小差分で済む |
| Moonraker 接続失敗時の `/api/machine-control` | `httpx.TransportError`（接続不能）と Klipper 側拒否（`RuntimeError`、未ホーミング等）は **502**、limits 超過の `ValueError` は **400** | spec「Moonraker 接続失敗で 502 等」「エラーをそのままトーストで表示」。detail にメッセージを載せる |
| `/api/klipper/status` の接続失敗 | **常に 200** で `{connected: false, error: "..."}` を返す | UI はエラー表示するだけ（spec 注意書き）。ポーリングで 5xx を量産しない |
| Klipper RESTART の実装 | webui 側から Moonraker の `POST /printer/restart` を httpx で直接叩く（`pcbasm.hal.Klipper` には restart メソッドが無く、pcbasm は無改造とする） | `send_gcode("RESTART")` は klippy 再起動中にレスポンスが壊れる。Moonraker の専用エンドポイントが正攻法 |
| RESTART 失敗時の motion 設定 PUT | ファイル保存が成功していれば **200**。レスポンスボディに `restart_ok: bool, restart_error: str | null` を含める | 保存と再起動は別の関心事。開発機（Klipper 未接続）でも保存フローの E2E が通る |
| Klipper / XYZStage の生成 | リクエストごとに生成・使い捨て（AppState に保持しない） | `Klipper.get_config` は `functools.cache`、`XYZStage.limits` は `cached_property`。RESTART 後の設定変更で stale になる事故を構造的に防ぐ。localhost への接続コストは無視できる |
| 配列値の設定項目 | `reference_point.offsets.*` / `probe.shift` は Phase 1 ホワイトリストから**除外**（スカラーのみ） | フォーム表現とバリデーションが複雑化する。必要になったら追加 |
| `camera.calibration_file` | ホワイトリストから除外 | Apply フロー（Phase 3+ の camera_calibration ジョブ）が管理する項目 |
| printer.cfg の編集 | 対象オプションが**既存行として存在する場合のみ**値を書き換える（行追加はしない、無ければ 400） | セクション内挿入位置の判断を避ける。実ファイルには全項目が存在する |
| machine.toml の欠落キー | GET は `value: null` で返す（フォームは placeholder 表示）。PUT で与えられたキーは tomlkit で追加可 | kurousagi に `bead_width_factor` 等が無いため |
| デフォルトマシン | `webui_state.json` が無い/不正なら `"kurousagi"`、それも無ければマシン一覧のソート先頭 | sorted 先頭だと `test-fixture` が選ばれてしまう |
| state 永続化先 | `data/webui_state.json`。`data/.gitignore` に `webui_state.json` を追記。E2E では `PCBASM_WEBUI_DATA_DIR` で /tmp に逃がす | リポジトリを汚さない |
| test-fixture の Klipper ポート | `port = 7126`（Moonraker 実機は 7125） | test-fixture 選択中の machine-control / E-STOP が必ず接続拒否 → 502 パスを実機なしで検証でき、誤って実機を動かさない |

## 公開インターフェース案（シグネチャ確定）

以下が spec-test-author / plan-implementer 間の契約。pydantic は API 境界のみ、内部は attrs。

### `src/webui/settings.py`

```python
@attrs.frozen
class Settings:
    configs_root: Path          # 既定 PROJECT_ROOT / "configs"
    data_dir: Path              # 既定 PROJECT_ROOT / "data"
    pcb_browse_root: Path       # 既定 PROJECT_ROOT
    printer_cfg_link: Path      # 既定 Path.home() / "printer_data/config/printer.cfg"
    mainsail_url: str = "http://localhost"
    default_machine: str = "kurousagi"
    host: str = "0.0.0.0"
    port: int = 8080

    @classmethod
    def from_env(cls) -> Settings: ...
```

env 対応（`from_env` が読む）: `PCBASM_WEBUI_CONFIGS_ROOT`, `PCBASM_WEBUI_DATA_DIR`, `PCBASM_WEBUI_PCB_ROOT`, `PCBASM_WEBUI_PRINTER_CFG_LINK`, `PCBASM_MAINSAIL_URL`, `PCBASM_WEBUI_PORT`。`PCBASM_WEBUI_FAKE_CAMERA` は Phase 2 で追加。

### `src/webui/config_store.py`

```python
@attrs.frozen
class FieldSpec:
    key: str                                    # ドット区切り toml パス（例 "paste_dispenser.rotations_per_ul"）
    label: str                                  # UI 表示名（日本語）
    value_type: Literal["float", "int", "str"]
    unit: str | None = None                     # 例 "mm", "rev/uL"

MACHINE_FIELDS: tuple[FieldSpec, ...]   # 下記ホワイトリスト
MOTION_FIELDS: tuple[FieldSpec, ...]    # key は "セクション名.オプション名"（例 "printer.max_velocity",
                                        # "manual_stepper paste_dispenser.velocity"）

class UnknownFieldError(ValueError): ...   # 未知キー / 型不一致（→ 400）

class ConfigStore:
    def __init__(self, configs_root: Path) -> None: ...
    def list_machines(self) -> list[str]                          # machine.toml を持つ dir 名、sorted
    def machine_toml_path(self, machine: str) -> Path             # configs/<machine>/machine.toml
    def printer_cfg_path(self, machine: str) -> Path
    def read_machine_settings(self, machine: str) -> dict[str, float | int | str | None]
        # MACHINE_FIELDS の key → 現在値（toml に無いキーは None）
    def write_machine_settings(self, machine: str, values: Mapping[str, float | int | str]) -> None
        # tomlkit でコメント・構造保持。未知キー/型不一致は UnknownFieldError
    def read_motion_settings(self, machine: str) -> dict[str, float | None]
    def write_motion_settings(self, machine: str, values: Mapping[str, float]) -> None
        # 行ベース書き換え（既存行のみ）。対象行が無い場合も UnknownFieldError
    def symlink_points_to(self, machine: str, link: Path) -> bool
        # link が configs/<machine>/printer.cfg を指す symlink か（resolve 比較）
```

machine.toml ホワイトリスト（`MACHINE_FIELDS`）:

- `paste_dispenser`: rotations_per_ul, nozzle_diameter, fill_speed, max_dispense_rate, dispense_accel, retract_amount, retract_rate, retract_accel_factor, paste_height, ul_per_mm2, prime_extra_delay, bead_width_factor, overlap, boundary_margin（全 float）
- `paste_dispenser.toolhead`: x, y（float）
- `paste_dispenser.pad_align`: tolerance, max_correction, search_window, roi_margin, min_roi, theta_range, canny_low, canny_high（float）, blur_ksize（int）
- `probe`: servo_name（str）, revolution_distance, down_distance, min_radius（float）, min_samples, max_samples（int）
- `reference_point`: x, y, target_diameter（float）
- `camera`: device_id（int）, width, height（int）, fps（float）, format（str）
- `camera.crop`: width, height（int）

printer.cfg ホワイトリスト（`MOTION_FIELDS`）: `printer.max_velocity`, `printer.max_accel`, `manual_stepper paste_dispenser.velocity`, `manual_stepper paste_dispenser.accel`（全 float）

### `src/webui/state.py`

```python
class BusyError(RuntimeError):
    """マシン排他ロックが取得できない（→ HTTP 409）."""
    def __init__(self, owner: str) -> None: ...
    @property
    def owner(self) -> str: ...

class AppState:
    def __init__(self, settings: Settings, store: ConfigStore) -> None
        # data_dir/webui_state.json を読んで selected_machine / selected_pcb を復元。
        # 不正値は default_machine（無ければ list_machines()[0]）へフォールバック

    @property
    def selected_machine(self) -> str: ...
    @property
    def selected_pcb(self) -> Path | None: ...      # pcb_browse_root からの相対パス
    @property
    def busy_owner(self) -> str | None: ...

    def select_machine(self, name: str) -> None     # BusyError / ValueError(未知マシン)。成功で永続化
    def select_pcb(self, path: Path) -> None        # BusyError / ValueError(範囲外・拡張子・不存在)。成功で永続化
    def machine(self) -> Machine                    # 選択マシンの pcbasm.config.Machine（毎回ロード）
    def focus_z(self) -> float | None               # calibration JSON の z_position（ファイル欠落等は None）

    @contextmanager
    def machine_lock(self, owner: str) -> Iterator[None]
        # threading.Lock の非ブロッキング取得。失敗時 BusyError(現 owner)。
        # Phase 3 の JobManager はこの同一ロック（この contextmanager）を共有する
```

設定保存（PUT settings）も `machine_lock("settings")` を経由する。E-STOP はロック非経由。

### `src/webui/models.py`（API 共有 pydantic モデル）

```python
class Position(BaseModel):
    x: float; y: float; z: float

class KlipperStatus(BaseModel):
    connected: bool
    position: Position | None = None
    homed_axes: str | None = None       # 例 "xyz", ""
    error: str | None = None
```

### routers（プレフィックスはすべて `/api`、ページのみ素のパス）

`src/webui/routers/pages.py` — Jinja2 ページ。`TABS: dict[str, tuple[str, ...]]`（tab → feature slug 列）をモジュール定数で持つ。

| route | 動作 |
| --- | --- |
| `GET /` | `/posctrl` へ 307 リダイレクト |
| `GET /{tab}` | tab ∈ {dev, pasting, pnp, posctrl} → `tab.html`。他は 404 |
| `GET /{tab}/{feature}` | 既知 feature → `feature.html`（Phase 1 はプレースホルダ）。他は 404 |
| `GET /settings` | `settings.html`（config_store の現在値で描画） |

`src/webui/routers/machine.py`

```python
class MachineSelect(BaseModel):
    name: str

GET /api/state    -> StateResponse   # 200
GET /api/machines -> MachinesResponse  # 200
GET /api/machine  -> MachineSelect     # 200
PUT /api/machine  (body: MachineSelect) -> MachineSelect  # 200 / 404(未知) / 409(busy)

class StateResponse(BaseModel):
    machine: str
    pcb_file: str | None        # 相対パス
    busy: bool
    busy_owner: str | None
    focus_z: float | None
    mainsail_url: str

class MachinesResponse(BaseModel):
    machines: list[str]
    selected: str
```

`src/webui/routers/files.py`

```python
class FileEntry(BaseModel):
    name: str
    type: Literal["dir", "file"]

class FilesResponse(BaseModel):
    path: str                   # 正規化済み相対パス（root は ""）
    entries: list[FileEntry]    # dir 全部 + *.kicad_pcb のみ。名前順

class PcbFileSelect(BaseModel):
    path: str

GET /api/files?path=<rel>  -> FilesResponse   # 200 / 400(traversal・root 外) / 404(不存在)
PUT /api/pcb-file (body: PcbFileSelect) -> StateResponse  # 200 / 400(拡張子・root 外) / 404 / 409
```

`src/webui/routers/settings_api.py`

```python
class SettingsField(BaseModel):
    key: str
    label: str
    value_type: Literal["float", "int", "str"]
    unit: str | None
    value: float | int | str | None

class MachineSettingsResponse(BaseModel):
    machine: str
    fields: list[SettingsField]

class SettingsUpdate(BaseModel):
    values: dict[str, float | int | str]

class MotionSettingsResponse(MachineSettingsResponse):
    symlink_ok: bool            # printer_cfg_link が選択マシンを指しているか

class MotionUpdate(SettingsUpdate):
    restart: bool = False       # 保存後に Klipper RESTART を実行するか

class MotionUpdateResult(BaseModel):
    restart_requested: bool
    restart_ok: bool
    restart_error: str | None

GET /api/settings/machine -> MachineSettingsResponse           # 200
PUT /api/settings/machine (body: SettingsUpdate) -> MachineSettingsResponse  # 200 / 400(UnknownFieldError) / 409
GET /api/settings/motion  -> MotionSettingsResponse            # 200
PUT /api/settings/motion  (body: MotionUpdate) -> MotionUpdateResult  # 200 / 400 / 409
```

RESTART は `httpx.post(f"http://{host}:{port}/printer/restart", timeout=10.0)`（host/port は選択マシンの `machine.klipper`）。失敗は `restart_ok=False` + エラー文字列。

`src/webui/routers/machine_control.py`

```python
class MachineControlRequest(BaseModel):
    action: Literal["home", "jog", "move", "relax", "focus_z"]
    axes: list[Literal["x", "y", "z"]] | None = None   # home: 空/None は全軸
    axis: Literal["x", "y", "z"] | None = None         # jog で必須
    distance: float | None = None                      # jog で必須 [mm]（UI は ±0.1/1/10）
    x: float | None = None                             # move（None の軸は現在位置維持）
    y: float | None = None
    z: float | None = None

POST /api/machine-control (body: MachineControlRequest) -> KlipperStatus
# 200: 操作完了（G-code + M400 まで送信完了）。KlipperStatus は操作後の位置
# 400: パラメータ不足（jog に axis/distance 無し等）/ limits 超過 ValueError / focus_z 不可（z_position なし）
# 409: machine_lock 取得失敗（detail に owner）
# 502: Moonraker 接続不能（httpx.TransportError）/ Klipper 拒否（send_gcode の RuntimeError、未ホーミング等）
```

実装: `with state.machine_lock("machine-control"):` 内でリクエストごとに `Klipper` / `XYZStage` を生成し、

- home → `gcode.homing(x=..., y=..., z=...) + gcode.wait_for_done()`
- jog → `stage.move(**{axis: distance}, relative=True) + gcode.wait_for_done()`
- move → `stage.move(x=x, y=y, z=z) + gcode.wait_for_done()`
- relax → `gcode.relax()`
- focus_z → `state.focus_z()` が None なら 400、else `stage.move(z=focus_z) + gcode.wait_for_done()`

`src/webui/routers/system.py`

```python
POST /api/emergency-stop -> {"ok": true}   # 200 / 502(接続不能)。ロック非経由で Klipper.emergency_stop() を直接実行
GET  /api/klipper/status -> KlipperStatus  # 常に 200。接続失敗は connected=false + error
```

### `src/webui/app.py` / `__main__.py`

```python
def create_app(settings: Settings | None = None) -> FastAPI
    # settings=None → Settings.from_env()（uvicorn --factory 用）
    # lifespan で ConfigStore / AppState を構築し app.state.appstate 等に保持
    # BusyError → 409 JSON の exception_handler を登録
    # StaticFiles("/static") と Jinja2Templates を構成
```

`__main__.py`: `uvicorn.run("webui.app:create_app", factory=True, host=settings.host, port=settings.port)`。

ルーター内からの依存取得は `request.app.state` 経由の `Depends` ヘルパ（`get_state(request) -> AppState`, `get_store`, `get_settings`）を `app.py` に置く。

### templates / static

```
templates/
├── base.html        # ヘッダ: 4 タブリンク / マシン選択 <select> / PCB チップ（ファイルブラウザモーダル起動）
│                    # / 歯車(/settings) / console リンク(mainsail_url, target=_blank) / E-STOP（赤・常時）
├── tab.html         # サイドバー（feature リスト + 下部にマシン操作パネル partial）+ 空メインペイン
├── feature.html     # 「未実装（Phase N で実装予定）」プレースホルダ
├── settings.html    # マシン設定フォーム + モーション設定フォーム（symlink 警告表示・RESTART 確認ダイアログ）
└── partials/
    ├── sidebar.html
    ├── machine_control.html   # Homing(X/Y/Z/全軸) / ジョグ(3軸×±0.1/1/10) / 絶対移動 / Relax / フォーカスZ
    │                          # / 現在位置・homed_axes 表示（展開中のみポーリング）
    └── file_browser.html      # モーダル
static/
├── app.css
└── js/
    ├── app.js              # マシン選択 PUT・PCB チップ/ファイルブラウザ・E-STOP・トースト表示
    ├── machine_control.js  # パネル開閉・/api/machine-control 送信・/api/klipper/status ポーリング（展開中のみ）
    │                       # ※ Phase 3 でジョブモード（WS 送信）に切替可能な構造にするが、Phase 1 は REST のみ
    └── settings.js         # 設定フォーム収集 → PUT、結果トースト、RESTART 確認
```

### `configs/test-fixture/`（git 管理）

- `machine.toml`: kurousagi のコピーをベースに `[klipper] port = 7126`、`calibration_file` は同梱のミニマル JSON（`z_position` 付き）を指す。コメントを意図的に多めに残す（コメント保持テストの素材）
- `printer.cfg`: kurousagi から `[printer]` / `[stepper_x/y/z]` / `[manual_stepper paste_dispenser]` を中心に抜粋（コメント付き）
- `ov9281_test_fixture.json`: `CalibrationResult` 形式の小さな JSON（focus_z 検証用）

### Makefile / pyproject / .gitignore

```make
webui-dev: ## Run WebUI dev server (auto-reload)
	uv run uvicorn webui.app:create_app --factory --reload --host 0.0.0.0 --port 8080

webui: ## Run WebUI server
	uv run python -m webui
```

pyproject dependencies 追加: `fastapi>=0.115`, `uvicorn[standard]>=0.34`, `jinja2>=3.1`, `tomlkit>=0.13`。`data/.gitignore` に `webui_state.json` を追記。

## 実装ステップ（ファイル単位・依存順）

並列レーン: **A（基盤+設定）** と **B（ルーター+UI）** は step 3 完了後に並列可。spec-test-author は本計画確定後すぐ `tests/webui/` に並列着手可（上記シグネチャが契約）。

1. **依存とフィクスチャ**（他の全てが依存）
   - `pyproject.toml` へ 4 依存追加、`uv sync`
   - `configs/test-fixture/`（machine.toml / printer.cfg / calibration JSON）
   - `data/.gitignore` 追記
2. **`src/webui/settings.py`** — Settings + from_env（依存なし）
3. **`src/webui/config_store.py`** — FieldSpec / ホワイトリスト定数 / ConfigStore（依存: test-fixture）
4. **`src/webui/state.py`** — BusyError / AppState（依存: settings, config_store）
5. **`src/webui/models.py`** — Position / KlipperStatus
6. **routers**（依存: 4, 5。相互独立なので並列実装可）
   - `routers/machine.py`, `routers/files.py`
   - `routers/settings_api.py`
   - `routers/machine_control.py`, `routers/system.py`
7. **`src/webui/app.py` + `__main__.py`** — create_app / lifespan / exception handler / Depends ヘルパ（依存: 6）
8. **`routers/pages.py` + templates + static**（依存: 7。JS/CSS は API 確定後いつでも）
9. **`Makefile`** ターゲット追加
10. **`make format && make type && make test-no-hardware`** グリーン化 → E2E（下記）

## テスト観点（`tests/webui/` は `src/webui/` を 1 対 1 ミラー）

skill `testing-strategy` 準拠。3rd-party（httpx / Moonraker / tomlkit）の表面はモックしない。**Moonraker 成功系は実機区分（`@mark_hardware`）とし、Phase 1 では Claude は実行しない（ユーザー実施）**。接続拒否系は test-fixture（port 7126 = 非リッスン）への実接続で検証するためモック不要・常時実行可。pytest は tmp_path に `configs/test-fixture` をコピーした `Settings` を `create_app(settings)` へ注入し、`fastapi.testclient.TestClient` で叩く（共有 fixture は `tests/webui/conftest.py`）。

### `tests/webui/test_settings.py`（unit）

- 正常系: 既定値（configs_root, port=8080 等）/ env 設定時の上書き（monkeypatch.setenv）
- 異常系: `PCBASM_WEBUI_PORT` に非数値 → ValueError

### `tests/webui/test_config_store.py`（unit）

- 正常系: `list_machines` がフィクスチャを列挙 / `read_machine_settings` が toml 値を型どおり返す / 欠落キーは None / `write_machine_settings` 後の再読込で値反映 / **書き込み後もコメント・既存構造が保持される**（変更対象外の行が byte 同一）/ motion: printer.cfg の値読み出し・書き換え（`max_velocity: 50` → 書換後も他行・コメント不変）/ `symlink_points_to` 真偽（tmp_path に symlink を作って検証）
- 異常系: 未知キー → UnknownFieldError / 型不一致（float 項目に str）→ UnknownFieldError / printer.cfg に対象行が無い → UnknownFieldError / 未知マシン → 適切な例外
- エッジ: blur_ksize 等 int 項目に float を渡した場合の扱い / 値にコメント付き行（`down_distance = 2.0  # ...`）の書換でコメント保持

### `tests/webui/test_state.py`（unit / integration-with-fakes）

- 正常系: 初期化で default_machine 選択 / select_machine → webui_state.json 永続化 → 新 AppState で復元 / select_pcb の相対パス保存 / machine_lock の取得・解放 / focus_z がフィクスチャ calibration の z_position を返す
- 異常系: ロック保持中の select_machine / select_pcb / machine_lock → BusyError（owner 文字列含む）/ 未知マシン名 → ValueError / root 外・非 .kicad_pcb の select_pcb → ValueError
- エッジ: webui_state.json が壊れた JSON / 存在しないマシン名を含む → フォールバック / calibration_file 不存在 → focus_z None

### `tests/webui/routers/test_pages.py`（integration-with-fakes）

- 正常系: `/` → `/posctrl` リダイレクト / 4 タブ + feature + `/settings` が 200、E-STOP・マシン操作パネル・mainsail リンクのマーカー文字列を含む
- 異常系: 未知タブ・未知 feature → 404

### `tests/webui/routers/test_machine.py`（integration-with-fakes）

- 正常系: GET /api/state の全フィールド / GET /api/machines / PUT /api/machine で選択が切り替わり state に反映
- 異常系: 未知マシン PUT → 404 / ロック保持中（AppState のロックをテストから直接取得して再現）→ 409

### `tests/webui/routers/test_files.py`（integration-with-fakes）

- 正常系: ルート列挙（dir + .kicad_pcb のみ、他拡張子が出ない）/ サブディレクトリ指定 / PUT /api/pcb-file → state 反映
- 異常系: `path=../..` トラバーサル → 400 / 不存在パス → 404 / 非 .kicad_pcb の PUT → 400 / busy → 409

### `tests/webui/routers/test_settings_api.py`（integration-with-fakes）

- 正常系: GET machine/motion がホワイトリスト全項目（label / unit / value）を返す / PUT machine → 実ファイル反映 + コメント保持 / PUT motion（restart=False）→ 反映 / motion GET の symlink_ok
- 異常系: 未知キー PUT → 400 / busy → 409 / restart=True で Moonraker 不達（port 7126）→ 200 + `restart_ok=false` + restart_error 非空

### `tests/webui/routers/test_machine_control.py`

- integration-with-fakes（常時実行）:
  - 異常系: jog で axis 欠落 → 400 / focus_z で calibration の z_position 無し（フィクスチャ差替）→ 400 / busy → 409（detail に owner）/ Moonraker 不達（test-fixture port 7126）で home/relax → 502
- integration-hardware（`@mark_hardware`、ユーザー実行）:
  - 実 Moonraker に対する relax(M84) 200 / jog ±0.1 往復 / limits 超過 move → 400 / 未ホーミング jog → 502
  - ※ limits 検証（`XYZStage.limits`）は `get_config` が必要なため実機区分にのみ置く

### `tests/webui/routers/test_system.py`

- integration-with-fakes: GET /api/klipper/status 不達 → 200 + connected=false + error / POST /api/emergency-stop 不達 → 502
- integration-hardware: 実 Moonraker への status（position / homed_axes 取得）。emergency_stop の実機テストは書かない（装置への副作用が大きい。E-STOP 配線確認はユーザーの手動操作）

### `tests/webui/test_app.py`（integration-with-fakes）

- create_app(settings) が起動し /api/state が 200 / BusyError ハンドラが 409 JSON を返す / /static/app.css 配信

## Claude 自身による E2E 手順（spec §11 / §12 Phase 1）

実行前提: 実装完了・`make test-no-hardware` グリーン。リポジトリの実 `configs/` を使い `test-fixture` を選択する（実マシン設定は読み取りのみ）。state は /tmp に逃がす。

```bash
# 1. 起動（バックグラウンド）
mkdir -p /tmp/webui-e2e
cd /home/gop/pcb-assembly
PCBASM_WEBUI_DATA_DIR=/tmp/webui-e2e uv run uvicorn webui.app:create_app --factory --port 8080 &
sleep 2

# 2. ページ巡回（200 + 内容マーカー）
for p in /posctrl /dev /pasting /pnp /settings /posctrl/reference_point_setup; do
  curl -s -o /dev/null -w "$p %{http_code}\n" "localhost:8080$p"; done
curl -s localhost:8080/posctrl | grep -E "E-STOP|machine-control"   # ヘッダ/パネルの存在確認
curl -s -o /dev/null -w "%{http_code}\n" localhost:8080/   # 307

# 3. マシン選択 → 設定 GET/PUT → 実ファイル diff（コメント保持確認）→ 復元
curl -s -X PUT localhost:8080/api/machine -H 'Content-Type: application/json' -d '{"name":"test-fixture"}'
curl -s localhost:8080/api/state          # machine=test-fixture, focus_z 数値
curl -s localhost:8080/api/settings/machine | python3 -m json.tool
curl -s -X PUT localhost:8080/api/settings/machine -H 'Content-Type: application/json' \
  -d '{"values":{"paste_dispenser.fill_speed":0.9,"probe.down_distance":2.5}}'
git diff configs/test-fixture/machine.toml   # 値 2 行のみ変化・コメント無傷を目視確認
curl -s localhost:8080/api/settings/motion   # symlink_ok=false（リンクは実マシンを指す）
curl -s -X PUT localhost:8080/api/settings/motion -H 'Content-Type: application/json' \
  -d '{"values":{"printer.max_velocity":45},"restart":true}'
# → 200, restart_ok=false（port 7126 不達）を確認
git diff configs/test-fixture/printer.cfg && git checkout configs/test-fixture/

# 4. ファイルブラウザ / PCB 選択
curl -s "localhost:8080/api/files?path=data"           # *.kicad_pcb のみ列挙
curl -s "localhost:8080/api/files?path=../etc" -o /dev/null -w "%{http_code}\n"   # 400
curl -s -X PUT localhost:8080/api/pcb-file -H 'Content-Type: application/json' \
  -d '{"path":"data/TJ-56-67/TJ-56-67.kicad_pcb"}'
cat /tmp/webui-e2e/webui_state.json                    # 永続化確認

# 5. Klipper 系エラーパス（test-fixture = port 7126 不達）
curl -s localhost:8080/api/klipper/status              # 200, connected=false
curl -s -X POST localhost:8080/api/machine-control -H 'Content-Type: application/json' \
  -d '{"action":"relax"}' -o /dev/null -w "%{http_code}\n'                          # 502
curl -s -X POST localhost:8080/api/emergency-stop -o /dev/null -w "%{http_code}\n"  # 502
curl -s -X POST localhost:8080/api/machine-control -H 'Content-Type: application/json' \
  -d '{"action":"jog","axis":"x"}' -o /dev/null -w "%{http_code}\n"                 # 400 (distance 欠落)
```

- 409 の E2E はジョブ基盤が無い Phase 1 では自然に再現できないため pytest 側（ロック直接取得）で担保する
- **実機確認（ユーザー実施）**: kurousagi 選択での homing / jog / move / relax / focus_z の実動作、motion 設定保存 → RESTART の反映、E-STOP の実停止

## 想定リスク・トレードオフ（ユーザー確認事項）

1. **`pcbasm.hal.Klipper` の `timeout=None`**: Moonraker が「接続は受けるが応答しない」状態だと status ポーリングや machine-control が無期限ブロックする。localhost 運用では接続拒否で即失敗するため Phase 1 はそのまま使う。気になる場合は `Klipper.__init__(timeout: float | None = None)` の後方互換追加（pcbasm 1 行改修）を別途判断いただきたい
2. **machine-control 中のサーバー応答**: 同期エンドポイントは FastAPI の threadpool で動くため、移動完了待ち中も他リクエスト（status / ページ）は応答する。ただしホーミング等の長い操作中にクライアントが切断してもサーバー側操作は完走する（中断手段は E-STOP のみ）— Phase 1 仕様として許容
3. **printer.cfg の symlink 警告のみで保存は許可**する（spec どおり）。選択マシン以外を編集していても保存自体は configs/ 配下に閉じるため実害は限定的
4. **ホワイトリストの過不足**: `reference_point.offsets` / `probe.shift`（配列）と `camera.calibration_file` を除外した。必要なら Phase 1 内で追加可能（FieldSpec 追加 + 型対応）
5. **test-fixture のポート 7126**: 万一同ポートで何かが listen している環境では「接続拒否」前提のテストが崩れる。問題があれば pytest 側でポートを動的に確保して machine.toml を書き換える方式に変更する
6. **`GET /api/state` の項目**: spec §9 の「現行ジョブ要約・preview クライアント数」は Phase 3 / Phase 2 で追加するフィールドとし、Phase 1 では busy / busy_owner のみ（先回り実装しない）

## 参照

- 仕様: `docs/webui/specification.md`（§2, §5, §6, §8, §9, §10, §11, §12 Phase 1, §13, §14）
- tomlkit パターン: `src/scripts/posctrl/reference_point_setup.py:64` `update_reference_point`
- Klipper/Stage: `src/pcbasm/hal/klipper.py`, `src/pcbasm/hal/stage.py`, `src/pcbasm/gcode.py`
- 設定: `src/pcbasm/config.py`（`Machine`, `get_machine_config`）, `configs/kurousagi/{machine.toml,printer.cfg}`
- calibration: `src/pcbasm/vision/calibration.py`（`CalibrationResult.z_position`）
- symlink 方式: `install-printer-cfg.sh`
- テスト規約: skill `testing-strategy`, `refactor-conventions`, `tests/helpers.py`, `tests/conftest.py`

## ユーザー決定の反映（main 追記 2026-06-12）

- 「基本的にすべて実現する」方針。確認事項 1 は **採用**: `pcbasm.hal.Klipper.__init__` に後方互換の `timeout: float | None = None` 引数を追加し、webui からは有限 timeout（10.0 秒、machine-control の移動系は wait_for_done を含むため 60.0 秒）を渡す
- 確認事項 4 のホワイトリスト除外（配列値・`camera.calibration_file`）は**維持**（spec 上 calibration_file は Apply フローの管轄。配列はフォーム要件外）
- MR は各フェーズで個別に出さず、全フェーズスタック後に最終ブランチ → main の MR を 1 本作成する
- pyproject への依存追加（fastapi / uvicorn / jinja2 / tomlkit）+ `uv sync` は main が実施済み（実装ステップ 1 の依存追加部分はスキップ）
