# WebUI Phase 3: ジョブ実行基盤 + dev タブ

## 概要

`docs/webui/specification.md` §12 Phase 3 の実装計画。`src/webui/jobs/`（JobManager / JobContext / JobCatalog / dev ジョブ）、`routers/jobs.py`（REST + WS `/api/ws`）、Apply/Discard 機構、dev タブ UI（パラメータフォーム + ジョブコンソール + 成果物表示 + Klipper/Stage カード + G-code 送信）を実装する。装置を動かすジョブは無い（posctrl/pasting は Phase 4/5）が、排他・prompt・abort・command・frame の全機構を装置非依存ジョブで固める。

**方針変更の反映（ユーザー宣言 2026-06-12）**: pcbasm への破壊的変更を許可。dev ジョブのコア再利用のため、scripts のレンダリング/生成ロジックを pcbasm へ昇格する（下記「pcbasm 昇格」）。既存 scripts は削除せず import 先を追従修正する。

前提（調査済み事実）:

- `AppState.machine_lock(owner)` は `threading.Lock` の非ブロッキング contextmanager（`src/webui/state.py`）。**ジョブは request スレッドで取得し worker スレッドで解放する必要がある**ため、acquire/release を分離した公開メソッドを AppState に追加する（`threading.Lock` はスレッド跨ぎ解放可。RLock ではないので問題ない）
- `PreviewService.submit_override(image)`（`src/webui/preview.py`）が `ctx.frame()` の受け口として実装済み（override_ttl=1.0s、最新 1 枚保持）
- `app.state` には settings / store / appstate / preview / templates が載る。Depends ヘルパ（`StateDep` 等）と例外ハンドラ（`BusyError`→409, `UnknownFieldError`→400）は `app.py` に集約
- `pcbnew` は uv 環境で import 可（KiCAD 9.0.2+dfsg-1、システムパッケージ）。pyproject に宣言は無い
- starlette 1.3 の sync `TestClient.websocket_connect()` で WS テスト可（pytest-asyncio 不要）。**無限 StreamingResponse は TestClient で読めない**（Phase 2 実装メモ）が WS は portal 経由で双方向に動く
- `Klipper.send_gcode(GCodeLike)` は str を受ける。`GCode(str)` で wrap でき `+ gcode.wait_for_done()` で連結可。`XYZStage(klipper.readonly).limits` で可動域取得可
- dev TABS は Phase 1 で定義済み: `extract_pcb / fill_path_simulate / generate_grid_pcb / make_fill_coverage_pcb / klipper_status`（`routers/pages.py`）。ジョブ名 = feature slug で一致させる
- `tests/scripts/test_generate_grid_pcb.py` は pcbnew をモックして `generate_grid_pcb()` をテスト中 → 昇格に伴い移設（既存方式は温存、import 先のみ変更）
- `pcbasm/visualization.py` は `polygon_with_holes_patch` のみの単一モジュール。`render_pcb`（extract_pcb）と `render_fill_paths`（fill_path_simulate）の昇格先としてパッケージ化する
- **注意: code-simplifier が `src/webui/` 内部を整理中**（公開 IF 不変）。plan-implementer は着手時に最新の `src/webui/` を読み、内部構造は現物に合わせること

## 設計判断（spec 未規定 → 本計画で確定）

| 論点 | 判断 | 理由 |
| --- | --- | --- |
| 排他ロックの取得/解放分離 | `AppState.acquire_machine(owner)` / `release_machine()` を追加し、既存 `machine_lock` はその上に再実装（公開 IF は追加のみ） | POST で 409 を同期返却するには request スレッドで取得が必要。worker 終了時に別スレッドから解放する |
| dev ジョブ（装置非使用）もロックを取るか | **全ジョブが machine ロックを取る** | 「ジョブ実行中はマシン切替・PCB 切替・設定保存・machine-control が 409」が単一機構で自然に成立する。単一オペレータ UI で同時実行の利点なし |
| `uses_machine` フラグの意味 | JobDefinition に持たせるが Phase 3 では情報のみ（dev ジョブは False）。**finally の M84（relax）送信は Phase 4 へ繰り越し** | Phase 3 に装置ジョブが無く、Klipper 送信コードを検証なしで入れない |
| イベント配信の橋渡し | JobManager 内に subscriber 機構（`asyncio.Queue` の集合 + lifespan で bind した loop へ `loop.call_soon_threadsafe(q.put_nowait, event)`） | spec §6 の `asyncio.run_coroutine_threadsafe` 同等をより単純に。put_nowait は非ブロッキングで worker を止めない |
| WS の異常応答 | サーバー → クライアントに `error` イベントを追加（spec §6 の 6 種 + 1） | respond_prompt の id 不一致・型不一致を黙殺しない。docs-keeper が spec §6 に追記する |
| Discard | `POST /api/jobs/last/discard` を追加（spec §9 表に無い → docs-keeper が追記） | spec §8「破棄で無効化」をサーバー側で成立させる。冪等（無くても 200） |
| Apply の書き込み主体 | ルーターが `ConfigStore.write_machine_settings()` を実行（`machine_lock("apply-settings")` 下）。manager は payload の検証・消費管理のみ | 設定書き込みの経路を Phase 1 と一本化。書込成功後に `mark_applied()`（失敗時は再試行可） |
| 成果物の置き場所 | `settings.data_dir / "webui" / <job_id>/`。`/artifacts` に StaticFiles をマウント。**新ジョブ開始時に前ジョブの成果物ディレクトリを削除** | 「直近 1 件のみ保持」をファイルにも適用しディスクを有界に。traversal 防止は StaticFiles 任せ |
| 任意 G-code 送信 | `POST /api/machine-control` に `action: "gcode"` を追加（`gcode: str` + `wait_for_done`） | ジョブ排他ロックを既存経路で自然に共有。新エンドポイント不要 |
| Stage ステータスカード | `GET /api/stage/limits` を新設（位置/homed は既存 `/api/klipper/status` のポーリング） | stage_demo 代替。limits は静的なのでページ表示時 1 回の取得 |
| prompt/abort/Apply の E2E 用ジョブ | `job_demo`（hidden、dev 名前空間）を catalog に常設登録 | 4 つの dev ジョブはどれも prompt を使わないため、E2E（prompt 往復・abort・Apply スタブ）が成立しない。テストと E2E の両方が使う |
| WS クライアント JS の責務 | `job_console.js` 1 本に WS クライアント + コンソール UI を同居（`tab.html` から全タブで読み込み）。`window.webui.jobs` を export | spec §5 のファイル構成どおり。machine_control.js のジョブモード切替と将来のサイドバーバッジが同じチャネルを使える |
| pcbnew の import 位置 | `pcbasm/pcb/generate.py` はモジュールレベル import（scripts と同じ）。**webui の `jobs/dev.py` はジョブ関数内で遅延 import** | KiCAD 未導入環境でも webui 自体は起動できるようにする。`pcbasm/pcb/__init__.py` からは re-export しない |
| matplotlib の扱い | 昇格先モジュールで `matplotlib.use("Agg")`（scripts の現行動作を踏襲）。pyplot を使うが**ジョブは同時 1 件なので非スレッドセーフ問題は顕在化しない** | OO API への書き換えは差分を広げる。リスク欄 4 に明記 |
| make_fill_coverage_pcb ジョブの出力先 | 成果物ディレクトリのみ（リポジトリの `data/testing/fill_coverage/` には**書かない**） | webui からリポジトリ管理ファイルを変異させない。fixture 更新は従来どおり scripts で行う |
| ジョブ終了とロック解放の順序 | 終端ステータス確定 → `job_status` 発行 → ロック解放 | 解放後に旧ステータスが見える競合を防ぐ |
| ログ保持量 | `deque(maxlen=500)`。`GET /api/jobs/current` は全保持分を返す | spec「リングバッファ（直近 N 行）」の N を確定 |
| シャットダウン | lifespan shutdown で `jobs.shutdown(timeout=10)`（request_abort + worker join）→ `appstate.close()` | ワーカー残存で uvicorn が固まらない。worker は daemon=True |

## pcbasm 昇格（破壊的変更の範囲）

scripts のコアロジックを pcbasm へ移し、scripts は薄い CLI ラッパとして温存する。

1. **`src/pcbasm/visualization.py` → `src/pcbasm/visualization/` パッケージ化**
   - `patches.py` — 既存 `polygon_with_holes_patch`（移動のみ）
   - `pcb_render.py` — `render_pcb(...)`（extract_pcb から移動。シグネチャは現行どおり）
   - `fill_render.py` — `render_fill_paths(...)` + 私的ヘルパ `_draw_fill_path` / `_annotate_coverage` / `_plot_start_marker`（fill_path_simulate から移動）
   - `__init__.py` — `polygon_with_holes_patch`, `render_pcb`, `render_fill_paths` を export。既存 import（`scripts/pasting/height_plane.py` 等 3 箇所）は無風
   - `matplotlib.use("Agg")` は pcb_render / fill_render のモジュール先頭（pyplot import 前）で実行
2. **`src/pcbasm/pcb/generate.py` 新設**（モジュールレベル `import pcbnew`。`pcb/__init__.py` には載せない）
   - `generate_grid_pcb(size: float, divisions: int, pad_size: float, output: Path) -> None`（scripts/dev/generate_grid_pcb.py から移動）
   - `build_fill_coverage_board() -> pcbnew.BOARD` + `save_board(board, output: Path) -> None`（make_fill_coverage_pcb の `build_board` + 保存処理を一般化。print は持ち込まない）
3. **scripts の追従修正**（振る舞い不変）
   - `scripts/dev/extract_pcb.py` — `render_pcb` を pcbasm.visualization から import（ローカル定義削除）
   - `scripts/dev/fill_path_simulate.py` — `render_fill_paths` を import（`_build_paths` / `_pads_on_layer` / argparse は script に残す）
   - `scripts/dev/generate_grid_pcb.py` — `pcbasm.pcb.generate` へ委譲
   - `scripts/dev/make_fill_coverage_pcb.py` — `build_fill_coverage_board` + `save_board` へ委譲（出力先固定値は script 側に残す）
4. **テスト移設**: `tests/scripts/test_generate_grid_pcb.py` の `TestGenerateGridPcb` 部分 → `tests/pcbasm/pcb/test_generate.py`（pcbnew モック fixture ごと移設、monkeypatch 先を `pcbasm.pcb.generate.pcbnew` に変更）。`TestMainArgparse` は scripts 側に残す。`tests/pcbasm/test_visualization.py` は import 不変（`__init__` re-export）なので無風確認のみ

## 公開インターフェース案（シグネチャ確定 = spec-test-author / plan-implementer 間の契約）

### `src/webui/state.py`（AppState へ追加。既存 IF 不変）

```python
class AppState:
    ...
    def acquire_machine(self, owner: str) -> None
        # 非ブロッキングで装置排他ロックを取得。Raises: BusyError
    def release_machine(self) -> None
        # 取得済みロックを解放（取得スレッドと別スレッドからの呼び出し可）。
        # 未取得での呼び出しは RuntimeError（threading.Lock の挙動どおり）
    # machine_lock(owner) は acquire_machine/release_machine の contextmanager として再実装（外部挙動不変）
```

### `src/webui/jobs/context.py`

```python
class JobAborted(Exception):
    """abort 要求により協調的に中断されたことを示す（worker 内部制御用）."""

@attrs.frozen
class PromptSpec:
    kind: Literal["confirm", "number", "text", "choice"]
    message: str
    default: bool | float | str | None = None
    choices: tuple[str, ...] = ()        # kind="choice" のみ必須

type Answer = bool | float | str

class JobContext:
    """ワーカースレッド ↔ Web 層の橋。JobManager だけが生成する（直接コンストラクトしない）."""

    @property
    def params(self) -> Mapping[str, bool | float | int | str]   # catalog 検証済みパラメータ
    @property
    def pcb_path(self) -> Path | None      # 選択 PCB の絶対パス（requires_pcb=False なら None あり得る）
    @property
    def machine(self) -> Machine           # 選択マシンの設定（ジョブ開始時に 1 回ロード）
    @property
    def artifacts_dir(self) -> Path        # data/webui/<job_id>/（作成済み）

    def log(self, message: str) -> None
        # リングバッファへ追記 + WS "log" イベント発行
    def progress(self, stage: str, percent: float | None = None) -> None
        # 直近 stage/percent を record に保持 + WS "progress" イベント発行
    def frame(self, image: Image) -> None
        # PreviewService.submit_override() へ委譲
    def prompt(self, spec: PromptSpec) -> Answer
        # WAITING_INPUT へ遷移し WS "prompt" を発行、respond_prompt までブロック。
        # 応答後 RUNNING に復帰し "prompt_resolved" + "job_status" を発行。
        # Raises: JobAborted（待機中に abort された場合）
    def next_command(self, timeout: float | None = None) -> dict[str, Any] | None
        # WS "command" のキューから 1 件取得（timeout 超過は None）。
        # Raises: JobAborted（待機中に abort された場合）
    def checkpoint(self) -> None
        # Raises: JobAborted（abort 要求済みの場合）。それ以外は no-op
```

prompt 応答の型検証: confirm→bool, number→float（int は float 化）, text→str, choice→choices 内の str。不一致は respond 側で `ValueError`（prompt は未解決のまま待ち続ける）。

### `src/webui/jobs/catalog.py`

```python
@attrs.frozen
class ParamSpec:
    name: str
    label: str
    value_type: Literal["float", "int", "str", "bool", "choice"]
    default: bool | float | int | str | None = None   # None = 必須
    choices: tuple[str, ...] = ()
    unit: str | None = None
    help: str | None = None

@attrs.frozen
class JobDefinition:
    name: str                                   # URL の {name}。feature slug と一致させる
    label: str
    tab: Literal["dev", "pasting", "posctrl"]
    run: Callable[[JobContext], JobResult | None]
    params: tuple[ParamSpec, ...] = ()
    requires_pcb: bool = False                  # True で PCB 未選択なら開始 400
    uses_machine: bool = True                   # Phase 3 では情報のみ（dev ジョブは False）
    accepts_commands: bool = False              # ジョブモード対話（next_command）を受けるか
    hidden: bool = False                        # UI のフォーム導出から除外（POST は可）

class JobCatalog:
    def register(self, definition: JobDefinition) -> None
        # Raises: ValueError（名前重複）
    def get(self, name: str) -> JobDefinition
        # Raises: KeyError（未知ジョブ → ルーターが 404 化）
    def list(self, tab: str | None = None) -> tuple[JobDefinition, ...]
        # hidden を含む全件（tab で絞り込み）。UI 用の除外は呼び出し側
    def validate_params(
        self, definition: JobDefinition, values: Mapping[str, object]
    ) -> dict[str, bool | float | int | str]
        # 未知キー・型不一致・必須欠落・choice 範囲外は ValueError（→ 400）。
        # default 充填済みの dict を返す。型変換規則は config_store._coerce と同様
        #（bool は value_type="bool" のみ受理、int→float 許容、float→int は整数値のみ）

def default_catalog() -> JobCatalog
    # dev の 5 ジョブ（下記 + job_demo）を登録済みの catalog を返す
```

### `src/webui/jobs/manager.py`

```python
class JobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"

    @property
    def terminal(self) -> bool   # SUCCEEDED / FAILED / ABORTED

@attrs.frozen
class Artifact:
    label: str
    path: str                                   # data/webui/ からの相対パス（URL = /artifacts/<path>）
    kind: Literal["image", "file"]              # image はインライン表示、file はダウンロードリンク

@attrs.frozen
class ApplyFile:
    filename: str                               # configs/<machine>/ 直下に書くファイル名（Phase 4 用）
    content: bytes

@attrs.frozen
class ApplyPayload:
    label: str                                  # コンソール表示用（例「canny_low = 60.0 を設定に反映」）
    values: Mapping[str, bool | float | int | str]  # machine.toml ホワイトリストキー → 値
    files: tuple[ApplyFile, ...] = ()

@attrs.frozen
class JobResult:
    summary: str | None = None
    artifacts: tuple[Artifact, ...] = ()
    apply: ApplyPayload | None = None

class JobRecord:
    """直近 1 件のジョブ状態（mutable、JobManager がロック保護で更新する）.

    公開 read プロパティ: id / name / params / status / error / result /
    log_lines (tuple[str, ...]) / progress_stage / progress_percent /
    pending_prompt ((prompt_id, PromptSpec) | None) / apply_available (bool)
    """

class JobManager:
    def __init__(
        self,
        state: AppState,
        preview: PreviewService,
        catalog: JobCatalog,
        settings: Settings,
        *,
        log_capacity: int = 500,
    ) -> None: ...

    # --- ライフサイクル ---
    def start(self, name: str, values: Mapping[str, object]) -> JobRecord
        # 1. catalog.get（KeyError → 404）2. validate_params / requires_pcb 検査（ValueError → 400）
        # 3. state.acquire_machine(f"job:{name}")（BusyError → 409）
        # 4. 前 record の成果物 dir 削除 → 新 record（PENDING）作成・artifacts_dir 作成
        # 5. worker Thread（daemon）起動 → record を返す（"job_status" 発行）
        # worker: RUNNING → run(ctx) → SUCCEEDED（result 保持）
        #         / JobAborted → ABORTED / Exception → FAILED（error=str(exc)、トレースバックは log へ）
        #         finally: 終端 job_status 発行 → release_machine
    def current(self) -> JobRecord | None      # 直近 1 件（実行中含む）。非永続
    def request_abort(self) -> bool
        # abort フラグ + prompt/command 待機者を JobAborted で起こす。アクティブジョブ無しは False
    def shutdown(self, timeout: float = 10.0) -> None
        # request_abort + worker join（lifespan shutdown 用、冪等）

    # --- 対話 ---
    def respond_prompt(self, prompt_id: str, answer: object) -> None
        # Raises: ValueError（pending prompt 無し / id 不一致 / 型不一致）
    def submit_command(self, command: Mapping[str, Any]) -> None
        # Raises: ValueError（accepts_commands なジョブが実行中でない / "type" キー欠落）

    # --- Apply / Discard ---
    def apply_payload(self) -> ApplyPayload
        # Raises: LookupError（直近ジョブ無し / SUCCEEDED でない / payload 無し / 適用・破棄済み）→ 409
    def mark_applied(self) -> None             # 書込成功後にルーターが呼ぶ（以後 apply_payload は LookupError）
    def discard(self) -> None                  # payload を無効化（冪等）

    # --- イベント購読（async 側から呼ぶ）---
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None   # lifespan startup で 1 回
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]
    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None
    def publish_state_changed(self) -> None    # マシン/PCB/設定変更時にルーターが呼ぶ
```

ワーカー → イベントの橋渡し: worker スレッドは `_publish(event: dict)` を呼び、bind 済み loop へ `loop.call_soon_threadsafe(queue.put_nowait, event)` で全 subscriber に配る（loop 未 bind なら record 更新のみ）。prompt 応答は `threading.Condition`（または `queue.Queue` 1 要素）、command は `queue.Queue`、abort は `threading.Event` で worker へ渡す。

### `src/webui/jobs/dev.py`

```python
def register_dev_jobs(catalog: JobCatalog) -> None
```

| name | requires_pcb | params（label/unit は実装時に整備、default は argparse 由来） | run の内容（成果物はすべて ctx.artifacts_dir） |
| --- | --- | --- | --- |
| `extract_pcb` | True | なし | `PcbFile(ctx.pcb_path)` → outline.json / pnp.csv / pads.json / copper.json 保存 + `pcbasm.visualization.render_pcb` で `<stem>_pcb.png`。Artifact: png=image、他 4 件=file。progress: 読込→抽出→描画 |
| `fill_path_simulate` | True | `nozzle_diameter: float=0.4 [mm]`, `layer: choice(top,bottom)=top`, `bead_width_factor: float=1.0`, `overlap: float=0.0`, `boundary_margin: float=0.0 [mm]` | レイヤ抽出 → pad ごと `build_paste_fill_path`（pad 単位で progress + checkpoint）→ `render_fill_paths` で PNG。summary に「生成 n/m 成功」 |
| `generate_grid_pcb` | False | `size: float=40 [mm]`, `divisions: int=3`, `pad_size: float=0.5 [mm]` | 関数内で `from pcbasm.pcb.generate import generate_grid_pcb`（遅延 import）→ `grid_{n}x{n}.kicad_pcb` 生成。Artifact: file |
| `make_fill_coverage_pcb` | False | なし | 遅延 import で `build_fill_coverage_board` + `save_board` → `fill_coverage.kicad_pcb`。Artifact: file |
| `job_demo`（hidden） | False | `steps: int=5`, `interval: float=0.2 [s]`, `fail: bool=False`, `command_phase: bool=False` | 機構検証用デモ: steps 回の log+progress+checkpoint+sleep → `fail=True` なら RuntimeError → `prompt(confirm)` → `prompt(number "canny_low")` → `command_phase=True` なら `next_command()` ループ（`{"type":"jog"}` を log エコー、`{"type":"quit"}` で離脱）→ JobResult(apply=ApplyPayload(values={"paste_dispenser.pad_align.canny_low": answer}))。`ctx.frame()` も 1 回呼ぶ（override 経路の通電確認）。accepts_commands=True |

すべて `uses_machine=False`。`job_demo` は UI に出ないが POST 可能（E2E・テスト・Apply スタブの実体）。

### `src/webui/models.py`（追加。REST と WS で共有する pydantic モデル）

```python
class PromptInfo(BaseModel):
    id: str
    kind: Literal["confirm", "number", "text", "choice"]
    message: str
    default: bool | float | str | None = None
    choices: list[str] = []

class ArtifactInfo(BaseModel):
    label: str
    url: str          # "/artifacts/<path>"
    kind: Literal["image", "file"]

class ApplyInfo(BaseModel):
    label: str
    values: dict[str, bool | float | int | str]

class JobResultInfo(BaseModel):
    summary: str | None = None
    artifacts: list[ArtifactInfo] = []
    apply: ApplyInfo | None = None

class JobSummary(BaseModel):
    id: str
    name: str
    label: str
    status: Literal["pending", "running", "waiting_input", "succeeded", "failed", "aborted"]
    params: dict[str, bool | float | int | str]
    error: str | None = None
    progress_stage: str | None = None
    progress_percent: float | None = None
    log_tail: list[str] = []                  # リングバッファ全量（最大 500 行）
    pending_prompt: PromptInfo | None = None
    result: JobResultInfo | None = None
    accepts_commands: bool = False
    apply_available: bool = False

class JobBrief(BaseModel):                    # /api/state 用
    id: str
    name: str
    status: str
```

変換ヘルパ `job_summary(record: JobRecord, definition: JobDefinition) -> JobSummary` は `routers/jobs.py` に置く（record→summary の唯一の変換点。WS と REST が共用）。

### `src/webui/routers/jobs.py`（REST + WS）

```
POST /api/jobs/{name}            body: {"params": {...}}（省略可、既定 {}）
    → 201 {"job": JobSummary}
    → 404（未知ジョブ）/ 400（パラメータ不正・PCB 未選択）/ 409（BusyError）
GET  /api/jobs/current
    → 200 {"job": JobSummary | null}      # WS 再接続時の同期用
POST /api/jobs/current/abort
    → 200 {"aborted": true} / 409（アクティブジョブ無し）
POST /api/jobs/last/apply
    → 200 {"applied": {<key>: <value>, ...}}
    # manager.apply_payload() → machine_lock("apply-settings") 下で
    #   store.write_machine_settings(state.selected_machine, payload.values)
    #   + payload.files を configs/<machine>/ へ書込 → manager.mark_applied()
    # → 409（LookupError: 反映可能なジョブ無し / BusyError: ジョブ実行中）/ 400（UnknownFieldError）
POST /api/jobs/last/discard
    → 200 {"ok": true}（冪等）
WS   /api/ws
```

WS ハンドラ実装方針: accept → `queue = manager.subscribe()` → 送信タスク（`queue.get()` → `send_json`）と受信ループ（`receive_json` → dispatch）を並走（`asyncio.gather` / starlette の WebSocketDisconnect で終了）→ finally で `unsubscribe`。manager 呼び出し（respond_prompt / submit_command / request_abort）は非ブロッキングなので直接呼んでよい。ValueError は `{"type": "error", "detail": ...}` を返信して継続。

#### WS メッセージスキーマ（JSON。"type" で判別）

サーバー → クライアント:

```jsonc
{"type": "job_status", "job": JobSummary}                    // 全ステータス遷移時（PENDING/RUNNING/WAITING_INPUT/終端）
{"type": "log", "job_id": "...", "line": "..."}
{"type": "progress", "job_id": "...", "stage": "...", "percent": 42.0 | null}
{"type": "prompt", "job_id": "...", "prompt": PromptInfo}
{"type": "prompt_resolved", "job_id": "...", "prompt_id": "..."}
{"type": "state_changed"}                                     // マシン/PCB/設定変更。クライアントは GET /api/state を取り直す
{"type": "error", "detail": "..."}                            // 不正な respond_prompt / command への応答
```

クライアント → サーバー:

```jsonc
{"type": "respond_prompt", "prompt_id": "...", "answer": true | 1.5 | "text"}
{"type": "command", "command": {"type": "jog", "axis": "x", "dist": 0.1}}   // command は "type" キー必須の自由形式
{"type": "abort"}
```

### 既存ルーター・app への変更

- `app.py` — `app.state.catalog = default_catalog()`、`app.state.jobs = JobManager(state, preview, catalog, settings)`、`JobsDep` / `CatalogDep` 追加。lifespan startup で `jobs.bind_loop(asyncio.get_running_loop())`、shutdown で `jobs.shutdown()` → `appstate.close()`。`(settings.data_dir / "webui").mkdir(parents=True, exist_ok=True)` して `/artifacts` に StaticFiles マウント。jobs ルーター登録（pages より前）
- `routers/machine.py` — `StateResponse` に `job: JobBrief | None` 追加。`PUT /api/machine` 成功後に `jobs.publish_state_changed()`
- `routers/files.py` — `PUT /api/pcb-file` 成功後に `publish_state_changed()`
- `routers/settings_api.py` — 設定 PUT 成功後に `publish_state_changed()`
- `routers/system.py` — `post_emergency_stop` の**先頭**で `jobs.request_abort()`（Klipper 送信失敗でも abort は立つ）
- `routers/machine_control.py` — `MachineControlRequest.action` に `"gcode"` 追加 + `gcode: str | None = None` フィールド。`_build_gcode` に `case "gcode"`: 空文字は ValueError、`gcode.GCode(body.gcode) + gcode.wait_for_done()`
- `routers/system.py`（または machine_control.py） — `GET /api/stage/limits` 新設: `XYZStage(create_klipper(state, STATUS_TIMEOUT).readonly).limits` → `{"x": {"min": ..., "max": ...}, "y": ..., "z": ...}`。Moonraker 不通は 502
- `routers/pages.py` — `FEATURE_TEMPLATES` に dev 5 件を追加。ジョブ系 4 件は共通 `dev/job.html`、`klipper_status` は `dev/klipper_status.html`。feature_page で catalog からジョブ定義を引き、`param_specs`（ParamSpec のリスト）と `job_name` をコンテキストに渡す

### templates / static（新設・変更）

```
templates/partials/job_console.html   # ログペイン・進捗バー・中止ボタン・結果（summary/成果物/Apply/Discard）
                                      # ・プロンプト <dialog>。data-job-name 属性でページのジョブ名を宣言
templates/dev/job.html                # tab.html 継承。ParamSpec からフォーム生成
                                      # （float/int=number, str=text, bool=checkbox, choice=select、unit/help 表示）
                                      # + 実行ボタン + job_console partial
templates/dev/klipper_status.html     # ステータスカード（位置/homed = /api/klipper/status 2s ポーリング、
                                      # limits = /api/stage/limits 1 回取得）+ G-code 送信ボックス
templates/tab.html                    # <script src="/static/js/job_console.js"> を追加（全タブで WS 接続）
static/js/job_console.js
    # - WS /api/ws へ接続。切断時は指数バックオフ再接続 + GET /api/jobs/current で再同期
    # - window.webui.jobs = { sendCommand(cmd): bool, abort(), currentJob(), onUpdate(cb) } を公開
    # - ページに #job-console（data-job-name 一致）があればコンソール描画:
    #   log 追記 / progress バー / prompt モーダル（kind 別入力 → respond_prompt）/ 中止 / 実行中はフォーム disabled
    # - 終端時: summary・成果物（kind=image は <img>、file は <a download>）・Apply/Discard ボタン
    #   （Apply = POST /api/jobs/last/apply、Discard = POST /api/jobs/last/discard、結果はトースト）
    # - フォーム submit: 入力を型変換して POST /api/jobs/{name}。409 はトースト
static/js/machine_control.js
    # - sendControl を拡張: window.webui.jobs.currentJob() が実行中（非終端）かつ accepts_commands
    #   なら WS command（{"type":"jog","axis","dist"} 等）で送信、それ以外は従来 REST
    # - 実行中ジョブが accepts_commands でないときはパネルを disabled 表示
    #   （jobs.onUpdate で job_status を購読）
static/app.css                        # コンソール・フォーム・モーダル分の追記
```

## 実装ステップ（ファイル単位・依存順）

並列レーン: **A（pcbasm 昇格 + scripts 追従）** と **B（webui ジョブ基盤）** は独立着手可。**C（UI）** は B の後。spec-test-author は本計画確定後すぐ `tests/` に並列着手可（上記シグネチャが契約）。

**レーン A（pcbasm + scripts。既存テストの無風確認込み）**

1. `src/pcbasm/visualization/` パッケージ化（patches.py / pcb_render.py / fill_render.py / `__init__.py`）
2. `src/pcbasm/pcb/generate.py`（generate_grid_pcb / build_fill_coverage_board / save_board）
3. `scripts/dev/` 4 本の追従修正（extract_pcb / fill_path_simulate / generate_grid_pcb / make_fill_coverage_pcb）
4. `tests/scripts/test_generate_grid_pcb.py` の移設対応（spec-test-author と調整: ロジック部 → `tests/pcbasm/pcb/test_generate.py`）

**レーン B（webui ジョブ基盤）** — 着手前に最新の `src/webui/` を読み直すこと（code-simplifier 整理中）

5. `src/webui/state.py` — `acquire_machine` / `release_machine` + `machine_lock` 再実装（依存なし）
6. `src/webui/jobs/context.py` — JobAborted / PromptSpec / JobContext（依存なし）
7. `src/webui/jobs/catalog.py` — ParamSpec / JobDefinition / JobCatalog / default_catalog 骨格（依存: 6）
8. `src/webui/jobs/manager.py` — JobStatus / Artifact / ApplyPayload / JobResult / JobRecord / JobManager（依存: 5–7）
9. `src/webui/jobs/dev.py` — dev 4 ジョブ + job_demo + register_dev_jobs（依存: 1, 2, 6–8）
10. `src/webui/models.py` — JobSummary ほか（依存: 8）
11. `src/webui/routers/jobs.py` — REST + WS + job_summary 変換（依存: 8, 10）
12. 既存ルーター変更 — machine / files / settings_api（state_changed）、system（E-STOP abort 連動 + stage/limits）、machine_control（gcode アクション）（依存: 8）
13. `src/webui/app.py` — catalog / jobs 構築、lifespan、/artifacts マウント、ルーター登録、Deps（依存: 11, 12）

**レーン C（UI。依存: 13）**

14. `routers/pages.py` の FEATURE_TEMPLATES + コンテキスト、`templates/dev/job.html` / `dev/klipper_status.html` / `partials/job_console.html`、`tab.html` への script 追加
15. `static/js/job_console.js` / `machine_control.js` 拡張 / `app.css`

**統合**

16. `make format && make type && make test-no-hardware` グリーン化 → E2E（下記）

## テスト観点（spec-test-author 担当。tests/ は src を 1 対 1 ミラー）

skill `testing-strategy` 準拠。Moonraker / cv2 / time.sleep のモック禁止（pcbnew モックは既存テストの先例どおり許容）。同期待ちは `threading.Event` ゲート付きの**合成ジョブ**（テスト内で catalog に register する JobDefinition）で決定的に行う。完了待ちは `record.status` のポーリング（短い実 sleep + 上限）か `manager.shutdown()`/専用ヘルパで行い、タイミングのアサートはしない。

### `tests/pcbasm/pcb/test_generate.py`（移設 + 追加）

- 既存 `TestGenerateGridPcb` を import 先変更で移設（pcbnew モック方式は温存）
- `build_fill_coverage_board` + `save_board`: 実 pcbnew で tmp_path に保存 → `PcbFile` で読み戻し、paste pad 数・凹形/線/点パッドの存在をピン（実 pcbnew が使える環境なので実体テスト可）

### `tests/pcbasm/test_visualization.py`（無風確認 + 追加）

- 既存 `polygon_with_holes_patch` テストが import 変更なしで通る
- `render_pcb` / `render_fill_paths`: 実 PCB fixture（`data/testing/` の既存 kicad_pcb）から tmp_path へ PNG 出力 → ファイル生成 + cv2 で読めるサイズ > 0（描画内容の厳密検証はしない）

### `tests/webui/test_state.py`（追記・unit）

- 正常系: `acquire_machine` 後に `busy_owner` が立つ / **別スレッドから `release_machine` で解放できる** / 解放後に再取得可 / `machine_lock` の従来挙動が不変
- 異常系: 取得済みでの `acquire_machine` → BusyError（owner 引き継ぎ）

### `tests/webui/jobs/test_catalog.py`（unit）

- 正常系: register/get/list（tab 絞り込み・hidden 含む）/ validate_params の default 充填・int→float 許容・choice 受理
- 異常系: 名前重複 register / 未知ジョブ get / 未知キー・型不一致・必須欠落・choice 範囲外・bool の数値カラム混入
- `default_catalog()` に dev 5 ジョブが登録済み（名前・requires_pcb・hidden をピン）

### `tests/webui/jobs/test_manager.py`（unit。合成ジョブ + 実 AppState/PreviewService）

- 状態遷移: start → PENDING/RUNNING → 正常終了 SUCCEEDED（result 保持）/ 例外 FAILED（error + ログにトレースバック）/ abort → ABORTED
- prompt: ゲート付きジョブが prompt → WAITING_INPUT + pending_prompt 公開 → respond_prompt で Answer が worker に渡り RUNNING 復帰 / 型不一致・id 不一致の respond → ValueError + prompt 維持 / confirm・number・text・choice の各型検証
- abort: 実行中 checkpoint で JobAborted / prompt 待機中の abort が即時 / next_command 待機中の abort が即時 / アクティブジョブ無しの request_abort → False
- command: submit_command → next_command が受け取る / timeout 切れ None / accepts_commands=False のジョブ実行中の submit → ValueError
- リングバッファ: log_capacity を小さく注入し、溢れた古い行が落ちる
- 排他: 実行中の二重 start → BusyError / 実行中は state.machine_lock 系（select_machine 等）が BusyError / 終了後にロック解放されている
- 直近 1 件: 新 start で旧 record が置き換わり旧 artifacts_dir が消える
- Apply: SUCCEEDED + payload → apply_payload 取得可 / mark_applied 後・discard 後・FAILED 後・payload 無し → LookupError / 新ジョブ開始で無効化
- frame: 合成ジョブの ctx.frame → PreviewService のオーバーライドスロットに載る（`_current_override` 相当は公開挙動の `mjpeg_stream` 経由か `submit_override` 検証ヘルパで）
- shutdown: 実行中ジョブが ABORTED になり join 完了（冪等）

### `tests/webui/jobs/test_dev.py`（integration-with-fakes。実 PcbFile / 実 pcbnew / 実 matplotlib）

- extract_pcb: 実 kicad_pcb fixture を選択した manager 経由実行 → SUCCEEDED + artifacts 5 件（png が cv2 で読める）
- fill_path_simulate: SUCCEEDED + PNG 生成 + summary に成功数
- generate_grid_pcb: 出力 .kicad_pcb が PcbFile で読めて pad 数 = divisions^2
- make_fill_coverage_pcb: 出力が PcbFile で読める
- job_demo: prompt 2 回の往復で SUCCEEDED + apply payload（canny_low=応答値）/ fail=True で FAILED / command_phase で jog エコー → quit
- requires_pcb ジョブを PCB 未選択で start → ValueError

### `tests/webui/routers/test_jobs.py`（integration-with-fakes。TestClient + 装置非依存ジョブ）

- REST: POST 201（JobSummary 形）/ 未知ジョブ 404 / パラメータ不正 400 / PCB 未選択 400 / 実行中の二重 POST 409 / GET current（実行中・終了後・null）/ abort 200・アクティブ無し 409
- Apply: job_demo 完走 → POST last/apply 200 → tmp configs の machine.toml に canny_low が書き込まれている（tomlkit コメント保持）→ 再 apply 409 / discard 後 apply 409 / ジョブ実行中の apply 409（BusyError）
- WS（`client.websocket_connect("/api/ws")`）:
  - job_demo を POST → job_status(running) / log / progress / prompt を受信 → `respond_prompt` 送信 → prompt_resolved → job_status(succeeded) まで通し
  - `abort` メッセージで実行中ジョブが aborted の job_status
  - 不正 respond_prompt（id 不一致）→ error イベント受信、接続は維持
  - `command` 送信 → ジョブの log にエコーが現れる
  - マシン切替 PUT → state_changed 受信
- 排他の波及: ゲート付きジョブ実行中に POST /api/machine-control → 409 / PUT /api/machine → 409 / PUT /api/settings/machine → 409 / PUT /api/pcb-file → 409
- /artifacts: 完了ジョブの成果物 URL が 200、`/artifacts/../` 形の traversal が 404
- `/api/state` に job ブリーフ（実行中 id/name/status、無ければ null）

### `tests/webui/routers/test_system.py` / `test_machine_control.py`（追記）

- E-STOP: ゲート付きジョブ実行中に POST /api/emergency-stop →（Moonraker 不通で 502 でも）ジョブが ABORTED になる
- machine-control: `action="gcode"` の空文字 → 400 / gcode 欠落 → 400（Moonraker 送信成功系は実機区分）
- GET /api/stage/limits: Moonraker 不通（port 7126）→ 502

### `tests/webui/routers/test_pages.py`（追記）

- dev ジョブ 4 ページが 200 + フォームマーカー（param name 属性・実行ボタン・job-console）/ klipper_status ページが 200 + G-code ボックス・limits マーカー / job_demo はサイドバーに出ない

## Claude 自身による E2E 手順（spec §11 / §12 Phase 3）

前提: 実装完了・`make test-no-hardware` グリーン。FakeCamera 不要のジョブ経路が主体だが、起動は他 Phase と同条件で行う。

```bash
mkdir -p /tmp/webui-e2e-p3
cd /home/gop/pcb-assembly
PCBASM_WEBUI_FAKE_CAMERA=1 PCBASM_WEBUI_DATA_DIR=/tmp/webui-e2e-p3 \
  uv run uvicorn webui.app:create_app --factory --port 8080 \
  > /tmp/webui-e2e-p3/server.log 2>&1 &
sleep 3

# 0. test-fixture 選択 + PCB 選択（リポジトリ内の実 kicad_pcb を選ぶ）
curl -s -X PUT localhost:8080/api/machine -H 'Content-Type: application/json' -d '{"name":"test-fixture"}'
curl -s -X PUT localhost:8080/api/pcb-file -H 'Content-Type: application/json' \
  -d '{"path":"data/testing/fill_coverage/fill_coverage.kicad_pcb"}'

# 1. ページ巡回: dev 4 ジョブページ + klipper_status のフォーム/コンソールマーカー
for f in extract_pcb fill_path_simulate generate_grid_pcb make_fill_coverage_pcb; do
  curl -s localhost:8080/dev/$f | grep -c "job-console"; done
curl -s localhost:8080/dev/klipper_status | grep -E "gcode|limits"

# 2. extract_pcb 実行 → 成果物 PNG の生成と /artifacts 配信を確認
JOB=$(curl -s -X POST localhost:8080/api/jobs/extract_pcb -H 'Content-Type: application/json' -d '{}')
echo "$JOB"; sleep 5
curl -s localhost:8080/api/jobs/current | python3 -m json.tool | grep -E '"status"|"url"'
PNG_URL=$(curl -s localhost:8080/api/jobs/current | python3 -c \
  "import sys,json; a=[x for x in json.load(sys.stdin)['job']['result']['artifacts'] if x['kind']=='image']; print(a[0]['url'])")
curl -s "localhost:8080$PNG_URL" -o /tmp/webui-e2e-p3/result.png
python3 -c "import cv2; img=cv2.imread('/tmp/webui-e2e-p3/result.png'); assert img is not None; print('PNG OK', img.shape)"

# 3. WS 通し（job_demo）: log / progress / prompt 往復 / 実行中 409 / abort
uv run python - <<'EOF'
import asyncio, json, httpx, websockets

async def main():
    async with websockets.connect("ws://localhost:8080/api/ws") as ws:
        async with httpx.AsyncClient(base_url="http://localhost:8080") as http:
            r = await http.post("/api/jobs/job_demo",
                                json={"params": {"steps": 5, "interval": 0.3}})
            assert r.status_code == 201, r.text
            # 実行中の二重起動 → 409 / machine-control → 409
            assert (await http.post("/api/jobs/extract_pcb", json={})).status_code == 409
            assert (await http.post("/api/machine-control",
                                    json={"action": "relax"})).status_code == 409
            seen = set()
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
                seen.add(msg["type"])
                if msg["type"] == "prompt":
                    p = msg["prompt"]
                    answer = True if p["kind"] == "confirm" else 60.0
                    await ws.send(json.dumps({"type": "respond_prompt",
                                              "prompt_id": p["id"], "answer": answer}))
                if msg["type"] == "job_status" and msg["job"]["status"] == "succeeded":
                    assert msg["job"]["apply_available"] is True
                    break
            assert {"log", "progress", "prompt", "prompt_resolved"} <= seen, seen
            # abort: 2 本目を流して即中止
            r = await http.post("/api/jobs/job_demo", json={"params": {"steps": 50}})
            assert r.status_code == 201
            await ws.send(json.dumps({"type": "abort"}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if msg["type"] == "job_status" and msg["job"]["status"] == "aborted":
                    break
    print("WS E2E OK")

asyncio.run(main())
EOF

# 4. Apply スタブ: job_demo を完走（手順 3 の 1 本目）後に apply → test-fixture 実ファイル確認
#   （注: 手順 3 の abort で直近ジョブが置き換わるため、apply 検証は再度 job_demo を完走してから行う）
uv run python - <<'EOF'
import asyncio, json, httpx, websockets
async def main():
    async with websockets.connect("ws://localhost:8080/api/ws") as ws, \
               httpx.AsyncClient(base_url="http://localhost:8080") as http:
        await http.post("/api/jobs/job_demo", json={"params": {"steps": 1, "interval": 0.1}})
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if msg["type"] == "prompt":
                p = msg["prompt"]
                await ws.send(json.dumps({"type": "respond_prompt", "prompt_id": p["id"],
                                          "answer": True if p["kind"] == "confirm" else 61.5}))
            if msg["type"] == "job_status" and msg["job"]["status"] == "succeeded":
                break
        r = await http.post("/api/jobs/last/apply")
        assert r.status_code == 200, r.text
        assert (await http.post("/api/jobs/last/apply")).status_code == 409  # 二重 apply 不可
    print("Apply OK")
asyncio.run(main())
EOF
git diff configs/test-fixture/machine.toml      # canny_low の 1 行のみ変化・コメント保持
git checkout configs/test-fixture/

# 5. fill_path_simulate / generate_grid_pcb / make_fill_coverage_pcb の成果物
curl -s -X POST localhost:8080/api/jobs/fill_path_simulate -H 'Content-Type: application/json' \
  -d '{"params":{"nozzle_diameter":0.4,"overlap":0.3}}'; sleep 8
curl -s localhost:8080/api/jobs/current | python3 -c \
  "import sys,json; j=json.load(sys.stdin)['job']; assert j['status']=='succeeded', j; print(j['result']['summary'])"
curl -s -X POST localhost:8080/api/jobs/generate_grid_pcb -H 'Content-Type: application/json' \
  -d '{"params":{"divisions":2}}'; sleep 5
curl -s localhost:8080/api/jobs/current | python3 -m json.tool | grep kicad_pcb

# 6. E-STOP 連動（Moonraker 不在でも abort フラグが立つこと）
curl -s -X POST localhost:8080/api/jobs/job_demo -H 'Content-Type: application/json' \
  -d '{"params":{"steps":50,"interval":0.3}}'
curl -s -X POST localhost:8080/api/emergency-stop -o /dev/null -w "%{http_code}\n"   # 502 でよい
sleep 2; curl -s localhost:8080/api/jobs/current | grep -o '"status": *"[a-z_]*"'    # aborted

# 後始末
kill %1 2>/dev/null; wait
rm -rf /tmp/webui-e2e-p3
```

ブラウザでの体感確認（フォーム→実行→コンソール・プロンプトモーダル・PNG インライン表示・Apply ボタン）、実 Moonraker での G-code 送信・stage limits・対話ジョブ中のジョグ（Phase 4 の reference_point_setup で本格化）は**ユーザーが実施**する。

## 想定リスク・トレードオフ（ユーザー確認事項）

1. **pcbnew 依存の波及**: `pcbasm/pcb/generate.py` はモジュールレベルで pcbnew を import する（`pcb/__init__.py` には載せないため通常の pcbasm 利用は無風、webui は遅延 import）。ただし `make test` を KiCAD 未導入環境で走らせると `tests/pcbasm/pcb/test_generate.py` の実 pcbnew テストが落ちる。現開発機（Pi, KiCAD 9.0.2）では問題なし。未導入環境対応が要るなら `pytest.importorskip("pcbnew")` を入れる（spec-test-author 判断に委譲）
2. **成果物の自動削除**: 新ジョブ開始で前ジョブの成果物（生成 .kicad_pcb 含む）が消える。「ダウンロードしてから次を実行」という運用前提。残したい場合は手動ダウンロードのみ（履歴非永続の spec と整合）
3. **WS イベントの取りこぼし**: `put_nowait` で配るため、クライアント側が極端に遅いと asyncio.Queue が伸びる（上限なし）。単一オペレータ前提で許容。再接続時は GET /api/jobs/current で全量同期できるため整合は崩れない
4. **matplotlib pyplot のスレッド安全性**: render はジョブ worker スレッドで走る。ジョブは同時 1 件・preview は matplotlib 非使用のため実害はないが、将来ジョブ外で pyplot を使う実装を足すと競合し得る（pcbasm.visualization のレンダラ内に閉じている限り安全）
5. **TestClient + worker スレッドのタイミング**: WS テストは「イベントが来るまで receive」で書けるが、終了直後の record 参照はポーリング待ちが要る。spec-test-author はゲート付き合成ジョブ + 受信駆動で決定的に書くこと（sleep ベースのアサート禁止）
6. **spec への追記事項（docs-keeper へ引き継ぎ）**: WS `error` イベント、`POST /api/jobs/last/discard`、`GET /api/stage/limits`、machine-control の `gcode` アクション、`job_demo`（hidden ジョブ）の存在、成果物の自動削除ポリシー
7. **finally の M84（relax）は未実装**（Phase 4 で uses_machine ジョブと同時に実装・実機検証）。Phase 3 の dev ジョブはモーターを動かさないため安全性への影響なし
8. **respond_prompt は WS 経由のみ**（REST に respond エンドポイントは作らない。spec §9 どおり）。WS が繋がらない環境ではプロンプト応答不能だが、その場合 abort（REST）で脱出できる
9. **code-simplifier との競合**: 本計画は公開 IF にのみ依存するが、`app.py` / `state.py` / `models.py` / 各ルーター / `tests/webui/conftest.py` は両者が触る。plan-implementer は simplifier の変更取り込み後に着手（または直後に rebase）すること

## 参照

- 仕様: `docs/webui/specification.md`（§5, §6, §8 Apply, §9, §10 dev 表, §11, §12 Phase 3, §14）
- Phase 1/2 計画・実装ログ: `memory/agents/implementation-planner/webui-phase{1,2}.md`, `memory/agents/plan-implementer/webui-phase{1,2}.md`（特に Phase 2 メモ「TestClient は無限 StreamingResponse を読めない」「__main__ の setup_logging」）
- 排他ロック・オーバーライドスロット: `src/webui/state.py`（`machine_lock`, `BusyError`）, `src/webui/preview.py`（`submit_override`）
- 参照元スクリプト: `src/scripts/dev/extract_pcb.py`, `fill_path_simulate.py`, `generate_grid_pcb.py`, `make_fill_coverage_pcb.py`, `klipper_demo.py`, `stage_demo.py`
- pcbasm コア: `src/pcbasm/pcb/__init__.py`（PcbFile, PadList, Layer, Outline）, `src/pcbasm/pasting/fill_path.py`（`build_paste_fill_path`）, `src/pcbasm/visualization.py`, `src/pcbasm/gcode.py`（`GCode`, homing/relax/wait_for_done）, `src/pcbasm/hal/klipper.py` / `stage.py`（XYZStage.limits）
- フロント既存構造: `src/webui/static/js/app.js`（`window.webui` = toast/api）, `machine_control.js`（sendControl 集約済み・ジョブモード切替を見越したコメントあり）, `templates/tab.html`
- テスト基盤: `tests/webui/conftest.py`（webui_settings / client / appstate fixtures）, `tests/scripts/test_generate_grid_pcb.py`（pcbnew モック先例）
- 規約: skill `testing-strategy`, `refactor-conventions`, `agent-team-startup`
