# WebUI Phase 5: pasting ジョブ + 仕上げ

## 概要

`docs/webui/specification.md` §12 Phase 5 の実装計画。webui に pasting 系 6 ジョブ
（paste_solder / height_plane / loading / flow_calibration / toolhead_offset /
probe_gnd_down_adjust）と pasting タブの feature ページ、loading 用コマンドボタン UI を実装する。
pcbasm への破壊的変更は許可（webui 主力化のユーザー宣言）、scripts は追従修正する。
ドキュメント整備は docs-keeper が別途行う（§7 に引き継ぎ事項を集約）。

前提（調査済み事実）:

- `PasteSession` は Phase 4 で frame_sink/camera 注入対応済み。webui は
  `setup_board_calibration(camera=…, frame_sink=ctx.frame)` → `PasteSession.from_calibration(result)` を使う（`PasteSession.setup` は不使用。呼び出し元は現在もゼロ）
- Apply 反映先 3 キー（`paste_dispenser.rotations_per_ul` / `paste_dispenser.toolhead.x`,`y` /
  `probe.down_distance`）は **config_store の MACHINE_FIELDS に登録済み** → ホワイトリスト追加不要
- Apply の `payload.values` / `payload.files` 書き込みは Phase 3/4 実装済み（`routers/jobs.py`）→ 変更不要
- 対話点の現状: `interactive_loading`（`pcbasm/pasting/loading.py`、input REPL）、
  flow_calibration の質量/比重 input、height_plane の Y/n input、probe_gnd の距離 REPL。
  **pcbasm 内の input() は interactive_loading の 1 箇所のみ**（他は scripts 内）
- height_plane のヒートマップ/計画点 PNG 生成（`_visualize` / `_visualize_planned` /
  `_draw_pcb_background` 等）は scripts ローカル関数 → webui 再利用には pcbasm 昇格が必要
- `sample_points_in_polygons`（`pcbasm/geometry/sampling.py`）は**乱数非依存**
  → 計画点プレビューと `HeightPlaneMeasurer.measure()` 内の再サンプル結果は一致する
- PasteApplicator の 12 kwargs 構築 boilerplate が scripts 3 本（loading / flow_calibration /
  toolhead_offset）+ `PasteSession.make_applicator` に重複
- `/artifacts` は StaticFiles マウント → **ジョブ実行中でも artifacts_dir に書いた時点で配信可能**
  （height_plane の「計測前に計画点 PNG を見せて confirm」に利用できる）
- ジョブ終了時の M84（relax）は manager の finally で実装済み（uses_machine のみ、best-effort）
- マシン操作パネルのジョブモード（accepts_commands ジョブへの WS command 切替）は実装済み。
  command スキーマ: `{type:"jog",axis,dist}` / `{type:"home",axes}` / `{type:"move",x,y,z}` /
  `{type:"relax"}` / `{type:"focus_z"}`。posctrl.py の `_dispatch_reference_command` が処理の先例
- `JobSummary` に `progress_stage` あり。ただし `job_console.js` は WS "progress" イベントで
  `currentJob` を更新せず listeners にも通知しない → loading ボタンの stage ゲーティングには JS 拡張が必要
- pnp プレースホルダは動作済み（`TABS["pnp"] = ()` + tab.html の「機能を選択」表示）→ 確認のみ
- Makefile の `webui` / `webui-dev` ターゲットは実装済み → 変更不要
- pcbasm の logging（HeightPlaneMeasurer の probe 点進捗、setup_board_calibration のフェーズログ等）は
  現状ジョブコンソールに**届かない**（scripts は setup_logging のコンソール出力で見えていた）
- **注意: code-simplifier が Phase 4 分の `src/webui/` 内部を整理中**（公開 IF 不変）。
  plan-implementer は simplifier 完了後に最新の `src/webui/` を読み直してから着手（または直後に rebase）

## 1. pcbasm 側の変更（公開 IF。破壊的変更可だが今回は追加のみ）

webui からの再利用に必要な昇格 + 重複解消。**既存シグネチャの変更・削除はなし**
（Phase 4 と異なり posctrl/session は触らない）。

```python
# src/pcbasm/geometry/sampling.py（追加）
@attrs.frozen
class SamplingDiagnostics:
    """probe 計画点の安全余裕と基板カバレッジ.

    Attributes:
        point_count: 計画点数
        min_clearance: 点が乗る銅箔境界までの最小距離 [mm]
        hull_area_ratio: 計画点凸包の面積 / outline 面積
    """
    point_count: int
    min_clearance: float
    hull_area_ratio: float

def sampling_diagnostics(
    points: Sequence[Point2d],
    polygons: Sequence[Polygon],     # 銅箔ポリゴン（Copper ではなく shapely を受け geometry 層に閉じる）
    outline: Polygon,
) -> SamplingDiagnostics | None      # points が空なら None
# scripts/pasting/height_plane.py の _print_sampling_diagnostics / _clearance_to_copper の計算部を昇格
# geometry/__init__.py へ export 追加

# src/pcbasm/visualization/height_render.py（新設。scripts/pasting/height_plane.py から移動）
def render_planned_points(
    planned_points: Sequence[Point2d], pcb: PcbFile, title: str, output_path: Path
) -> None
    # 計測予定点 + 凸包 + 基板背景（銅箔 TOP + アウトライン）を PNG 保存
def render_height_plane(
    height_plane: HeightPlane, pcb: PcbFile, title: str, output_path: Path
) -> None
    # 2D ヒートマップ + probe 点 + 基板背景を PNG 保存
# _draw_pcb_background / _draw_planned_hull / _MESH_RESOLUTION は本モジュールの私的ヘルパとして移動
# visualization/__init__.py へ export 追加（matplotlib.use("Agg") は既存 visualization と同様）

# src/pcbasm/pasting/applicator.py（classmethod 追加）
@classmethod
def from_config(
    cls,
    klipper: Klipper,
    paste_dispenser: PasteDispenser,           # hal 側
    stage: XYZStage,
    config: PasteDispenserConfig,              # pcbasm.config.PasteDispenser（名前衝突は import 別名で回避）
    *,
    transform: Transform = Identity(),
    lift_height: float = 2.0,
) -> Self
    # nozzle_diameter / fill_speed / max_dispense_rate / dispense_accel / ul_per_mm2 /
    # retract_amount / retract_rate / retract_accel_factor / paste_height /
    # prime_extra_delay / bead_width_factor / overlap / boundary_margin を config から配線

# src/pcbasm/session.py（内部整理のみ、公開 IF 不変）
# PasteSession.make_applicator は PasteApplicator.from_config へ委譲
```

`interactive_loading`（input REPL）は **scripts 専用ヘルパとして温存**する。webui は command 駆動の
別実装（§2 の loading ループ）を持つ — input() の「除去」ではなく「迂回」。CLI の REPL と WS の
ボタン UI は対話モデルが違いすぎ、共通抽象を作るほうが過剰と判断。

### scripts 追従箇所（全列挙。振る舞いは従来同等）

| ファイル | 変更内容 |
| --- | --- |
| `scripts/pasting/height_plane.py` | `_visualize` / `_visualize_planned` / `_draw_pcb_background` / `_draw_planned_hull` / `_MESH_RESOLUTION` を削除し `pcbasm.visualization` から import。`_print_sampling_diagnostics` は `sampling_diagnostics()` を呼んで print する薄い整形に縮小 |
| `scripts/pasting/loading.py` | PasteApplicator 構築を `from_config` 化（bead_width_factor 等が config 値になるが、loading は apply() を呼ばないため挙動不変） |
| `scripts/pasting/flow_calibration.py` | 同上 |
| `scripts/pasting/toolhead_offset.py` | `from_config(..., transform=Identity(), lift_height=args.lift_height)` 化 |
| `scripts/pasting/paste_solder.py` / `probe_gnd_down_adjust.py` | 変更なし |

## 2. webui 側の公開インターフェース

### `src/webui/jobs/machine_commands.py`（新設。posctrl.py からの抽出）

loading ループ中もマシン操作パネル（ジョブモード）の jog / home / move / relax / focus_z を
受けられるよう、`_dispatch_reference_command` の機械操作部分を共有化する。

```python
# wait_for_done (M400) を含む移動完了待ちのため長め（machine_control / posctrl と同値）
COMMAND_TIMEOUT = 60.0

def create_command_klipper(machine: Machine) -> Klipper
    # Klipper(host, port, timeout=COMMAND_TIMEOUT)。posctrl._create_klipper を移動・公開化

def handle_machine_command(
    ctx: JobContext,
    klipper: Klipper,
    stage: XYZStage,
    command: Mapping[str, Any],
    *,
    focus_z: float | None,        # None なら focus_z コマンドは log のみ
) -> bool
    # jog / home / move / relax / focus_z を処理したら True（ValueError は ctx.log して True）。
    # 未知 type は False（log は呼び出し側の責務 — ジョブ固有コマンドの後段判定があるため）
```

`jobs/posctrl.py` 追従: `_dispatch_reference_command` は record / quit を自前 match し、
残りを `handle_machine_command(..., focus_z=calibration.z_position)` へ委譲（True なら
`position.invalidate()`）。`_create_klipper` / `COMMAND_TIMEOUT` は machine_commands から import。
**posctrl の公開挙動は不変**。

### `src/webui/jobs/pasting.py`（新設・本 Phase の主戦場）

```python
def register_pasting_jobs(catalog: JobCatalog) -> None

# loading フェーズの progress stage 名（loading_controls.html の data 属性・テストでピンする契約値）
LOADING_STAGE = "ローディング"

@attrs.frozen
class Extrude:
    amount: float        # 符号付き押出量 [uL]（吸引は負）

@attrs.frozen
class Finish:
    """ローディング終了."""

type LoadingAction = Extrude | Finish

def parse_loading_command(command: Mapping[str, Any]) -> LoadingAction | None
    # {type:"extrude", amount: 正数} → Extrude(+amount)
    # {type:"suck", amount: 正数}    → Extrude(-amount)
    # {type:"finish"}                → Finish()
    # amount 欠落・非正・未知 type は None（純粋関数。unit テスト対象）
```

`default_catalog()`（catalog.py）に `register_pasting_jobs` を追加（docstring の件数も更新）。
ジョブ名は feature slug と一致させる。

#### ジョブ定義表

| name | params（ParamSpec） | requires_pcb | uses_machine | accepts_commands |
| --- | --- | --- | --- | --- |
| `paste_solder` | `tolerance: float=0.1 [mm]`, `amount: float=0.1 [uL]`, `interactive_loading: bool=False` | True | True | **True** |
| `height_plane` | `tolerance: float=0.1 [mm]` | True | True | False |
| `loading` | `amount: float=0.1 [uL]` | False | True | **True** |
| `flow_calibration` | `rotations: float=30 [rev]`, `rate: float=5.0 [rev/s]`, `accel: float=10.0 [rev/s^2]`, `load_amount: float=0.1 [uL]` | False | True | **True** |
| `toolhead_offset` | `tolerance: float=0.1 [mm]`, `dispense_amount: float=0.1 [uL]`, `loading_amount: float=0.1 [uL]`, `lift_height: float=5.0 [mm]`, `paste_diameter_min: float=0.0 [mm]`, `paste_diameter_max: float=2.0 [mm]` | True | True | **True** |
| `probe_gnd_down_adjust` | なし | False | True | False |

スクリプトの `--output` 引数（height_plane / toolhead_offset）は**廃止**し、成果物は
`ctx.artifacts_dir` 固定（Phase 3 の artifacts 機構に統一。spec §10 表からの差分 → docs-keeper）。

#### 共有ヘルパ（pasting.py 私的）

```python
def _dispenser_rig(machine: Machine) -> tuple[Klipper, XYZStage, PasteDispenser]
    # create_command_klipper + XYZStage(readonly) + hal.PasteDispenser(rotations_per_ul) の定型 3 点

def _run_loading_loop(
    ctx, klipper, stage, applicator, *, focus_z: float | None = None
) -> float   # 押出合計 [uL] を返す（吸引は負を合算）
    # 1. ctx.progress(LOADING_STAGE)
    # 2. 滞留コマンドを drain（next_command(timeout=0) を None まで。破棄数を log）
    #    — ローディング段階以前に押されたボタン/ジョグの遅延実行を防ぐ
    # 3. while True: command = ctx.next_command(timeout=None)  ← abort はセンチネルで即時覚醒
    #    - parse_loading_command が Extrude → applicator.load(amount) + log、Finish → 合計を返す
    #    - None → handle_machine_command(...) で機械操作、それも False なら「未知のコマンド」log

def _prompt_positive_number(ctx, message: str, default: float | None = None) -> float
    # prompt(number) を正数になるまでループ（非正は log して再 prompt）。
    # scripts/flow_calibration の _prompt_positive_float の WS 版
```

#### `_run_paste_solder(ctx) -> JobResult`

1. `with ctx.open_camera() as camera:` → `progress("セットアップ")` →
   `result = setup_board_calibration(machine=ctx.machine, pcb_file_path=ctx.pcb_path, tolerance=…, camera=camera, frame_sink=ctx.frame)` → `session = PasteSession.from_calibration(result)`
2. `progress("銅箔照合", 100*i/n)`: `sorted_top_component_pads(result)` →
   `PadAlignmentSession.from_calibration(result, frame_sink=ctx.frame)` → 各部品 `session.align(group)`
   + `ctx.checkpoint()`。dx/dy/theta を log、失敗部品は log 警告のみ（script と同じ。board_tour の
   PadResultRenderer 表示は照合「確認」が目的のジョブ向けでここでは過剰）
3. `progress("高さ計測")` → `session.height_measurer.measure(coppers=top_coppers, board_to_machine=session.board_to_machine, outline=…)`
4. 補正適用（script の `_corrected_polygons` と同等。未照合 pad は log 警告して無補正）→
   現在位置から `sort_by_nearest`
5. `transform = Compose([session.board_transform, session.toolhead_offset, height_plane])` →
   `with session.make_applicator(transform=transform) as applicator:`
   - `interactive_loading` パラメータが True なら: 現在位置を退避 → `stage.move(x=0,y=0,z=0)` →
     `_run_loading_loop(ctx, …, applicator)` → 退避位置へ復帰 + wait_for_done（script と同じ）
   - `progress("リトラクション")` → `applicator.retract()`
   - `progress("塗布", 100*i/n)`: ポリゴンを 1 件ずつ `ctx.checkpoint()` → `applicator.apply([polygon])`
     （per-pad の進捗と abort 境界を webui 側ループで確保。挙動は一括 apply と同一）
6. `JobResult(summary=f"照合成功 {…}/{…} 部品 / 塗布 {n} pads（押出合計 {…} uL)")`。Apply なし

#### `_run_height_plane(ctx) -> JobResult`

1. `pcb = PcbFile(ctx.pcb_path)` → TOP 銅箔 → `sample_points_in_polygons(machine.probe の min_radius/min_samples/max_samples, outline)`
2. `render_planned_points(...)` で `ctx.artifacts_dir / "planned_points.png"` を生成 →
   cv2 で読み戻して `ctx.frame()`（1 回）+ `ctx.log(f"計測予定点: /artifacts/{id}/planned_points.png")`
   （/artifacts は即時配信可能。job_console.js のログリンク化 §2 末尾で開ける）
3. `sampling_diagnostics(...)` を log（点数 / min_clearance / hull 比）
4. `prompt(confirm f"{n} 点を計測します。続行しますか?", default=True)` → False なら `raise JobAborted`
5. `with ctx.open_camera() as camera:` → `progress("セットアップ")` → `setup_board_calibration(camera=…, frame_sink=ctx.frame)` → `PasteSession.from_calibration`
6. `progress("高さ計測")` → `measure(...)`（点ごとの進捗は pcbasm ログブリッジ §2 manager で届く）
7. `render_height_plane(...)` → `ctx.artifacts_dir / "height_plane.png"`
8. ```python
   zs = [p.z for p in height_plane.points]
   JobResult(
       summary=f"{len(zs)} 点計測 / Z {min(zs):.3f}〜{max(zs):.3f} mm",
       artifacts=(
           Artifact("計測予定点", f"{ctx.artifacts_dir.name}/planned_points.png", "image"),
           Artifact("ヒートマップ", f"{ctx.artifacts_dir.name}/height_plane.png", "image"),
       ),
   )
   ```
   Apply なし（HeightPlane は塗布ジョブが都度計測する。設定ファイルへの反映先もない）

#### `_run_loading(ctx) -> JobResult`

1. `_dispenser_rig(ctx.machine)` → `PasteApplicator.from_config(klipper, dispenser, stage, ctx.machine.paste_dispenser)`
2. `with applicator:` → `total = _run_loading_loop(ctx, klipper, stage, applicator)`
   （カメラ・PCB 不要。preview ペインなし）
3. `JobResult(summary=f"押出合計 {total:+.3f} uL")`

#### `_run_flow_calibration(ctx) -> JobResult`

1. `_dispenser_rig` + `from_config`（script と同じ構成）
2. `with applicator:`
   - `_run_loading_loop(...)`（ノズル先端まで充填。Finish で次へ）
   - `prompt(confirm "はかりにキャッチ皿を置き、タール (0g) にしましたか?", default=True)` →
     False なら `raise JobAborted`（script の Enter 待ちの代替。中止口を兼ねる）
   - `progress("キャリブレーション回転")` → `applicator.calibrate(rotations, rate, accel)`
   - `mass = _prompt_positive_number(ctx, "ペーストが安定したら計測した質量 (mg) を入力")`
   - `applicator.retract()`
   - `sg = _prompt_positive_number(ctx, "ペーストの比重（水比重, データシート値）")`
3. `result = FlowCalibration(rotations=…, mass_mg=mass, specific_gravity=sg)`:
   ```python
   JobResult(
       summary=f"rotations_per_ul = {result.rotations_per_ul:.6f}",
       apply=ApplyPayload(
           label=f"[paste_dispenser] rotations_per_ul = {result.rotations_per_ul:.6f} を設定に反映",
           values={"paste_dispenser.rotations_per_ul": round(result.rotations_per_ul, 6)},
       ),
   )
   ```

#### `_run_toolhead_offset(ctx) -> JobResult`

script の Phase 1〜6 を踏襲（`machine_session` は manager の M84 finally が代替するため不使用）:

1. `with ctx.open_camera() as camera:` → `progress("セットアップ")` →
   `setup_board_calibration(camera=…, frame_sink=ctx.frame)`。ServoGroundProbe / ProbeExecutor /
   hal.PasteDispenser を script と同じ配線で構築
2. `progress("プローブ")`: board 中央 → toolhead 座標へ移動 → `probe_executor.probe()` → board_surface_z を log
3. `PasteApplicator.from_config(..., transform=Identity(), lift_height=lift_height)` → `with applicator:`
   - `stage.move(z=0)` → `_run_loading_loop(ctx, ..., focus_z=result.calibration.z_position)` → `applicator.retract()`
   - `progress("吐出")`: dispense_z へ移動 → pushpull(dispense_amount) → リトラクション → lift（script と同一の gcode 列）
   - `progress("ペースト検出")`: center_camera + calibration.z_position へ移動 → `time.sleep(1.0)` →
     paste 用 `CircleDetector`（diameter min/max から導出）→
     `OffsetObserver(detector, camera=result.camera, crop_size, frame_sink=ctx.frame)` →
     `XYPositionAdjustor(observe, klipper=result.klipper, stage, offset_transform, tolerance).adjust()`
4. `measured_offset = center_toolhead - camera_final_pos` →
   `ToolheadOffsetResult(...)` を `ctx.artifacts_dir / "toolhead_offset.json"` に保存:
   ```python
   JobResult(
       summary=(f"オフセット X={measured_offset.x:+.4f} Y={measured_offset.y:+.4f} mm"
                f"（現在設定との差 dX={diff_x:+.4f} dY={diff_y:+.4f}）"),
       artifacts=(Artifact("計測結果 JSON", f"{ctx.artifacts_dir.name}/toolhead_offset.json", "file"),),
       apply=ApplyPayload(
           label=f"[paste_dispenser.toolhead] x={measured_offset.x:.4f}, y={measured_offset.y:.4f} を設定に反映",
           values={"paste_dispenser.toolhead.x": round(measured_offset.x, 4),
                   "paste_dispenser.toolhead.y": round(measured_offset.y, 4)},
       ),
   )
   ```

#### `_run_probe_gnd_down_adjust(ctx) -> JobResult`

1. `klipper = create_command_klipper(machine)` → `ProbeGround(klipper.readonly, servo_name, revolution_distance)`
2. `try:` ループ（`progress("距離調整")`）:
   - `d = ctx.prompt(number "ダウン距離 [mm] を入力", default=直前値 or machine.probe.down_distance)`
   - `d < 0` なら log して再 prompt
   - `klipper.send_gcode(ground.down(d) + gcode.wait_for_done())` → log
   - `prompt(confirm f"down_distance = {d:.3f} mm で確定しますか?（いいえで再調整）", default=False)` → True で離脱
3. `finally:` `ground.down(0.0)` を best-effort（送信失敗は log のみ。script の「終了時は必ず 0 へ」を踏襲。
   abort / FAILED でも実行される）
4. ```python
   JobResult(
       summary=f"down_distance: {d:.3f} mm",
       apply=ApplyPayload(
           label=f"[probe] down_distance = {d:.3f} を設定に反映",
           values={"probe.down_distance": round(d, 3)},
       ),
   )
   ```

### `src/webui/jobs/manager.py`（pcbasm ログブリッジ。公開 IF 不変・挙動追加）

pasting ジョブの主要な進捗情報（probe 点ごとの log、setup のフェーズ log、applicator のローディング
log 等）は pcbasm の logging にあり、現状ジョブコンソールから見えない。scripts では setup_logging が
担っていた可視性を manager で回復する:

- `_run_worker` 冒頭で `logging.getLogger("pcbasm")` に私的 Handler を attach、finally で detach
- Handler は **worker スレッドのレコードのみ**（`record.thread == worker ident`）を
  `runtime.log(record.getMessage())` へ転送（preview スレッド等の pcbasm ログは混ぜない）
- レベル INFO、フォーマットはメッセージのみ。リングバッファ（500 行）であふれは許容

### `src/webui/routers/pages.py`

- `FEATURE_TEMPLATES`: pasting 6 feature → すべて `"pasting/job.html"`
- `_JOB_TEMPLATES` に `"pasting/job.html"` を追加
- ページコンテキスト用の定数を追加:
  - `_PASTING_PREVIEW = frozenset({"paste_solder", "height_plane", "toolhead_offset"})` →
    `show_preview: bool`（カメラを使うジョブのみ preview ペイン）
  - `_PASTING_LOADING_PARAM = {"paste_solder": "amount", "loading": "amount", "flow_calibration": "load_amount", "toolhead_offset": "loading_amount"}` →
    キーがあれば `show_loading_controls=True` + `loading_default=<該当 ParamSpec の default>`

### templates / static

```
templates/pasting/job.html
    # {% if show_preview %} preview_pane（overlay 切替なし・固定 none。spec §10「ジョブ提供フレームのみ」）
    # + job_form + {% if show_loading_controls %} loading_controls + job_console
templates/partials/loading_controls.html
    # <div id="loading-controls" data-loading-stage="ローディング">（LOADING_STAGE と一致。テストでピン）
    # 量 [uL] 数値入力（value={{ loading_default }}）+ 押出 / 吸引 / ローディング終了 ボタン
    # ※ spec §10 の「量変更」ボタンは数値フィールド常設に置換（extrude/suck が都度 amount を運ぶ）
static/js/loading_controls.js
    # 押出 → sendCommand({type:"extrude", amount}) / 吸引 → {type:"suck", amount} / 終了 → {type:"finish"}
    # 有効化条件: currentJob が非終端 && accepts_commands && progress_stage === data-loading-stage
static/js/job_console.js（拡張 2 点）
    # 1. WS "progress" イベントで currentJob.progress_stage / percent を更新し listeners へ通知
    #    （loading_controls / machine_control の状態追従用。onUpdate の契約は不変）
    # 2. log 行レンダリング時に "/artifacts/..." パスを <a target="_blank"> にリンク化
    #    （height_plane の計測前 計画点 PNG 確認用。テキストはエスケープ維持）
static/app.css   # loading-controls 分の追記のみ
```

## 3. 実装ステップ（ファイル単位・依存順）

並列レーン: **A（pcbasm + scripts）** と **B（webui 基盤）** は独立着手可。**C（ジョブ実装）** は
A・B の後、**D（UI）** は C と並列可（テンプレートは定義表が契約）。spec-test-author は本計画確定後
すぐ並列着手可（§1〜2 のシグネチャ・ジョブ定義表・LOADING_STAGE・コマンド schema が契約）。

**レーン A（pcbasm 昇格 + scripts 追従）**

1. `src/pcbasm/geometry/sampling.py`（SamplingDiagnostics / sampling_diagnostics）+ `geometry/__init__.py`
2. `src/pcbasm/visualization/height_render.py` 新設 + `visualization/__init__.py`
3. `src/pcbasm/pasting/applicator.py`（`from_config`）
4. `src/pcbasm/session.py`（make_applicator の from_config 委譲。IF 不変）
5. scripts 追従 4 本（§1 の表どおり）

**レーン B（webui 基盤）** — 着手前に最新の `src/webui/` を読み直すこと（code-simplifier 整理中）

6. `src/webui/jobs/machine_commands.py` 新設 + `jobs/posctrl.py` の委譲リファクタ
7. `src/webui/jobs/manager.py`（pcbasm ログブリッジ）
8. `static/js/job_console.js`（progress 通知 + artifacts リンク化）

**レーン C（pasting ジョブ）** — 依存: A, B

9. `src/webui/jobs/pasting.py`（6 ジョブ + parse_loading_command + LOADING_STAGE + 共有ヘルパ）
10. `src/webui/jobs/catalog.py`（default_catalog へ登録追加）

**レーン D（UI）** — 依存: C（テンプレート自体は定義表ベースで先行可）

11. `routers/pages.py`（FEATURE_TEMPLATES / show_preview / loading_controls コンテキスト）
12. `templates/pasting/job.html` + `templates/partials/loading_controls.html`
13. `static/js/loading_controls.js` + `app.css`

**統合**

14. pnp プレースホルダ・Makefile ターゲットの確認（変更なしの想定。E2E で 200 を確認するのみ）
15. `make format && make type && make test-no-hardware` グリーン化 → E2E（§5）

## 4. テスト観点（spec-test-author 担当。tests/ は src を 1 対 1 ミラー）

skill `testing-strategy` 準拠。Moonraker / cv2 / matplotlib / time.sleep のモック禁止。
test-fixture の Klipper（port 7126 非リッスン）は接続拒否が即時に返るため、
**全 pasting ジョブの異常系（graceful FAILED）はモックなしで結合テストできる**。
PasteApplicator / HeightPlaneMeasurer の実動作・SUCCEEDED 到達は実機区分（ユーザー実行）。

### pcbasm（新規 + 追従）

`tests/pcbasm/geometry/test_sampling.py`（追記）:
- 正常系: 既知の正方形銅箔 + 内部点で `sampling_diagnostics` の point_count / min_clearance /
  hull_area_ratio を数値ピン（凸包が成立する 3 点以上）
- エッジ: 点が空 → None / 点 2 個（凸包が線分）→ hull_area_ratio = 0.0 / outline 面積 0 の防御

`tests/pcbasm/visualization/test_height_render.py`（新設）:
- 正常系: 合成 HeightPlane（既知 3〜5 点）+ 既存 PCB fixture で `render_height_plane` /
  `render_planned_points` が PNG を生成し cv2 で復号可能・非自明サイズ（既存 render_pcb テストの方式踏襲）

`tests/pcbasm/pasting/test_applicator.py`（追記）:
- `from_config` で構築した applicator が手動 kwargs 構築と同じ gcode を生成する（既存テストの
  gcode 検証パターンに相乗り。retract / load の 1 操作で十分）
- 異常系: config の retract_accel_factor <= 1.0 で ValueError（__init__ の検証が通ること）

`tests/pcbasm/session.py 系` / `tests/scripts/`: make_applicator 委譲・scripts 追従の無風確認

### webui（unit / integration-with-fakes）

`tests/webui/jobs/test_machine_commands.py`（新設）:
- `handle_machine_command`: 未知 type → False / relax は Klipper 不通で ValueError にならず
  例外系の扱いが posctrl 既存テストと整合（Klipper 不通の send は例外 → ジョブ FAILED 系で担保）
  ※ jog/move の実送信は Moonraker 必須のため実機区分。ここでは戻り値契約（認識した type で True を
  返そうとして送信例外、未知 type で False）を test-fixture の接続拒否で検証

`tests/webui/jobs/test_pasting.py`（新設・主戦場）:
- catalog: `default_catalog()` に pasting 6 ジョブ（name / params の default・unit / requires_pcb /
  uses_machine / accepts_commands をピン。posctrl/dev と合わせた全件数も）
- `parse_loading_command`: extrude/suck の符号と amount / finish / amount 欠落・0・負・非数 → None /
  未知 type → None / `LOADING_STAGE == "ローディング"` のピン
- **probe_gnd_down_adjust（プロンプトフロー・装置なし）**: manager 経由で start →
  pending_prompt(number) → 負数応答 → log + 再 prompt → 正数応答 → Klipper 不通で FAILED +
  finally の down(0) 失敗 log + 排他ロック解放 / prompt 待ちで abort → ABORTED（down(0) 失敗 log あり）
- **height_plane（前半フロー・装置なし）**: PCB fixture 選択で start →
  artifacts_dir に planned_points.png 生成（cv2 復号可）+ diagnostics log + "/artifacts/" を含む log 行 →
  confirm True → setup で Klipper 不通 FAILED / confirm False → ABORTED
- paste_solder / loading / flow_calibration / toolhead_offset（異常系）: start → 即 FAILED
  （Klipper 接続拒否）+ relax 失敗警告 + ロック解放
- requires_pcb: paste_solder / height_plane / toolhead_offset は PCB 未選択で start 400

`tests/webui/jobs/test_manager.py`（追記）:
- ログブリッジ: ジョブ関数内で `logging.getLogger("pcbasm.x").info(...)` した行が record.log_lines に
  現れる / 別スレッドから出した pcbasm ログは現れない / ジョブ終了後は転送されない（detach 済み）

`tests/webui/routers/test_pages.py`（追記）:
- pasting 6 feature ページ 200 + マーカー: 全ページ job-console / job-form、
  preview img は paste_solder・height_plane・toolhead_offset のみ、
  loading-controls（`data-loading-stage="ローディング"` と loading_default 値込み）は
  paste_solder・loading・flow_calibration・toolhead_offset のみ、probe_gnd_down_adjust には両方なし
- `/pnp` 200 + サイドバー空 + プレースホルダ文言（確認のみ）

`tests/webui/routers/test_jobs.py`（追記）:
- WS で probe_gnd_down_adjust の prompt(number) 往復 → FAILED（イベント列の決定性確認）
- height_plane 実行中（confirm 待ち）に GET /artifacts/<id>/planned_points.png が 200
  （実行中配信のピン）

### integration-hardware（ユーザー実行）

§5 末尾の引き継ぎ一覧。loading ループの実押出・SUCCEEDED 到達・Apply 反映はすべて実機区分。

## 5. Claude 自身による E2E 手順（FakeCamera + 実 uvicorn/WS）

Klipper 不通（test-fixture）のため、装置ジョブは「フォーム → 起動 → prompt シーケンス → graceful
FAILED」までが検証範囲。SUCCEEDED・Apply・実押出はユーザー引き継ぎ。

```bash
mkdir -p /tmp/webui-e2e-p5
cd /home/gop/pcb-assembly
PCBASM_WEBUI_FAKE_CAMERA=1 \
PCBASM_WEBUI_DATA_DIR=/tmp/webui-e2e-p5 \
  uv run uvicorn webui.app:create_app --factory --port 8080 \
  > /tmp/webui-e2e-p5/server.log 2>&1 &
sleep 3

# 0. test-fixture + PCB 選択
curl -s -X PUT localhost:8080/api/machine -H 'Content-Type: application/json' -d '{"name":"test-fixture"}'
curl -s -X PUT localhost:8080/api/pcb-file -H 'Content-Type: application/json' \
  -d '{"path":"data/testing/fill_coverage/fill_coverage.kicad_pcb"}'

# 1. ページ巡回: pasting 6 ページのマーカー（job-console 全件 / preview img 3 件 /
#    loading-controls 4 件）+ /pnp の 200 とプレースホルダ + /dev /posctrl の無風 200
for f in paste_solder height_plane loading flow_calibration toolhead_offset probe_gnd_down_adjust; do
  curl -s localhost:8080/pasting/$f | grep -c "job-console"; done
curl -s localhost:8080/pasting/loading | grep -c "loading-controls"
curl -s localhost:8080/pasting/probe_gnd_down_adjust | grep -c "loading-controls"   # 0 のはず
curl -s localhost:8080/pnp | grep -c "placeholder"

# 2. probe_gnd_down_adjust のプロンプトシーケンス（websockets + httpx のワンショットスクリプト）:
#    POST /api/jobs/probe_gnd_down_adjust → prompt(number) 受信 → -1 応答 → 再 prompt（log に警告）
#    → 1.5 応答 → job_status(failed)（Klipper 接続拒否）+ log に down(0) 復帰失敗警告
#    → 直後の POST /api/machine-control が 409 でなく 502（ロック解放の確認）

# 3. height_plane の前半フロー:
#    POST /api/jobs/height_plane → log に計測予定点・diagnostics・/artifacts/ リンク →
#    GET /artifacts/<id>/planned_points.png が 200（実行中配信、cv2 復号可）→
#    prompt(confirm) に false → aborted / 再実行して true → failed（Klipper 不通）

# 4. 残り 4 ジョブの graceful FAILED:
#    paste_solder / loading / flow_calibration / toolhead_offset を順に POST →
#    いずれも failed + relax 失敗警告 + ロック解放。
#    loading 実行中（即 FAILED 前）の二重 start 409 は dev ジョブで既検証のため省略可

# 5. requires_pcb: PCB 選択解除状態で paste_solder POST → 400

# 6. ブラウザ相当の確認: pasting/loading ページの loading-controls ボタンが
#    ジョブ非実行時 disabled であること（HTML 属性で確認）

# 後始末
kill %1 2>/dev/null; wait; rm -rf /tmp/webui-e2e-p5
```

### ユーザーへ引き継ぐ実機確認項目

1. loading: ローディングボタン（押出 / 吸引 / 量変更 / 終了）での実押出と Finish → SUCCEEDED、
   ジョブモードパネルからのジョグ併用
2. flow_calibration: ローディング → タール confirm → N 回転 → 質量/比重 prompt → Apply で
   `rotations_per_ul` が machine.toml に反映されること
3. toolhead_offset: 全フェーズ通し（プローブ → ローディング → 吐出 → 円検出収束 → Apply で
   `[paste_dispenser.toolhead]` 反映）、preview への検出注釈配信
4. probe_gnd_down_adjust: 距離調整ループ → 確定 → Apply で `[probe].down_distance` 反映、
   終了時（abort 含む）のダウン距離 0 復帰
5. height_plane: 計画点 PNG リンク確認 → confirm → 実計測 → ヒートマップ artifacts、
   pcbasm ログブリッジで probe 点ごとの進捗がコンソールに出ること
6. paste_solder: 照合 → 高さ計測 → （任意ローディング）→ 実塗布の通し、塗布中の進捗表示、
   ポリゴン境界での abort、終了時 M84
7. 既存 scripts 4 本（height_plane / loading / flow_calibration / toolhead_offset）の実機回帰
   （from_config / 可視化昇格の追従確認）

## 6. docs-keeper への引き継ぎ事項リスト（Phase 3/4 分も集約）

README / CLAUDE.md / `docs/webui/specification.md` の整備は docs-keeper が別途実施。

### Phase 3 実装メモより（spec §6/§9 への追記）

- WS `error` イベント / `POST /api/jobs/last/discard` / `GET /api/stage/limits` /
  machine-control の `gcode` アクション / hidden ジョブ `job_demo`
- 成果物の自動削除ポリシー（新ジョブ開始で前回分削除）/ apply 成功時の `state_changed` 発行

### Phase 4 計画書 §6-7・実装メモより

- spec §4 の全面改稿（`frame_sink=None`=非表示・`window_name` 全廃・`posctrl/render.py` /
  `window_sink`・`machine_session` / `PasteSession.__exit__` の cv2 削除・`draw_detected_circle` 昇格）
- `camera.calibration_file` のホワイトリスト追加 / `JobContext.open_camera` /
  `PreviewService.hold_camera` / manager の M84 finally
- orthogonality_test の数値定義（軸間角ずれ + スケール）/ camera_calibration の Z best-effort /
  reference_point_setup の focus_z コマンド対応 / §10 posctrl 表の details 反映

### Phase 5（本計画）

- spec §10 pasting 表の更新: `output` パラメータ廃止（artifacts 固定）、loading コマンド schema
  （`extrude` / `suck` / `finish`、「量変更」は数値フィールド常設に置換）、progress stage 名、
  probe_gnd_down_adjust の number+confirm ループ・終了時 down(0)、flow_calibration の confirm 中止口
- spec §6 への追記: pcbasm ログブリッジ（worker スレッドの pcbasm ログをジョブコンソールへ転送）、
  job_console の log 内 `/artifacts/` リンク化、WS "progress" の listener 通知拡張
- pcbasm 新 API: `pcbasm.visualization.render_height_plane` / `render_planned_points`、
  `pcbasm.geometry.SamplingDiagnostics` / `sampling_diagnostics`、`PasteApplicator.from_config`
- webui 新モジュール: `webui/jobs/machine_commands.py`（COMMAND_TIMEOUT / create_command_klipper /
  handle_machine_command）、`webui/jobs/pasting.py`（LOADING_STAGE / parse_loading_command）
- README / CLAUDE.md: WebUI 起動手順（`make webui` / `make webui-dev`、`python -m webui`、
  env: `PCBASM_WEBUI_FAKE_CAMERA` / `PCBASM_WEBUI_CONFIGS_ROOT` / `PCBASM_WEBUI_DATA_DIR` /
  `PCBASM_MAINSAIL_URL`）、scripts と WebUI の役割分担（webui 主力化）
- spec §12 のロードマップ完了状況反映（Phase 1〜5 済・pnp は将来）
- `PasteSession.setup` の呼び出し元ゼロ継続（温存 or 削除の判断は docs ではなくユーザーへ提起）

## 7. 想定リスク・判断保留点（ユーザー確認事項）

1. **height_plane の成果物は artifacts のみ（非永続）**: scripts は `data/height_plane/<machine>/` に
   恒久保存するが、webui の artifacts は次ジョブ開始で削除される。計測記録を残したい場合は
   ブラウザからダウンロードする運用。恒久保存が必要なら `data/height_plane/` への複製を足す
   （計画は artifacts のみで進める。**要ユーザー判断**）
2. **abort の粒度**: 高さ計測（`measure()` 全点ループ）と塗布 1 ポリゴンの最中は checkpoint が
   入らず abort は境界まで持ち越し。E-STOP は従来どおりジョブ非経由で即時。pcbasm 側への
   checkpoint コールバック追加は Phase 4 と同じく見送り（変更範囲の抑制）
3. **paste_solder の accepts_commands は静的に True**: `interactive_loading=False` でもマシン操作
   パネルはジョブモードに切り替わる（ジョブ中の単発 REST 操作は元々 409 なので実害なし）。
   ローディング段階以外で送られたコマンドはループ入口の drain で破棄（log あり）
4. **pcbasm ログブリッジは新規挙動**: ジョブコンソールの情報量が大きく増える（setup の全フェーズ
   log 等）。多すぎる場合はブリッジのレベルや logger 名の絞り込みで調整可能
5. **COMMAND_TIMEOUT (60s) の射程**: `setup_board_calibration` 内部や `result.klipper` は pcbasm 既定
   timeout のまま（scripts と同条件）。webui が自前構築する Klipper のみ 60s。大量押出
   （>600 uL 相当）や巨大ポリゴンの fill で 60s を超える場合は要調整（実機検証で確認）
6. **loading ボタンの stage ゲーティングは job_console.js の progress 通知拡張に依存**:
   `LOADING_STAGE` 文字列がジョブ実装・テンプレート data 属性・テストの 3 箇所の契約になる
   （テストでピンして不一致を防ぐ）
7. **flow_calibration の confirm False → ABORTED**: script は Enter 待ちのみで中止口がなかった。
   回転前の中止口として意図的に追加（ペースト充填済みでも回転前なら安全に中止できる）
8. **from_config による scripts の挙動差**: loading / flow_calibration で bead_width_factor /
   overlap / boundary_margin が config 値になるが、両 script は `apply()` を呼ばないため実挙動は不変
9. **code-simplifier との競合**: Phase 4 分の `src/webui/` 整理が進行中。レーン B（posctrl.py の
   委譲リファクタ・manager・job_console.js）は simplifier の変更取り込み後に着手すること

## 参照

- 仕様: `docs/webui/specification.md`（§6, §8, §10 pasting 表, §11, §12 Phase 5）
- 前 Phase: `memory/agents/implementation-planner/webui-phase{1,2,3,4}.md`,
  `memory/agents/plan-implementer/webui-phase{1,2,3,4}.md`
- 参照元スクリプト: `src/scripts/pasting/`（6 本）
- pcbasm 改修対象: `src/pcbasm/pasting/applicator.py`, `src/pcbasm/geometry/sampling.py`,
  `src/pcbasm/visualization/`, `src/pcbasm/session.py`
- pcbasm 再利用（無改造）: `src/pcbasm/pasting/{calibration,height,probe,loading,toolhead_offset}.py`,
  `src/pcbasm/posctrl/{setup,alignment}.py`, `src/pcbasm/hal/`
- webui 基盤: `src/webui/jobs/{context,catalog,manager,posctrl,dev}.py`, `src/webui/preview.py`,
  `src/webui/routers/{jobs,pages}.py`, `src/webui/static/js/{job_console,machine_control}.js`,
  `src/webui/templates/{posctrl,partials}/`
- フィクスチャ: `configs/test-fixture/`, `data/testing/fill_coverage/fill_coverage.kicad_pcb`,
  `data/testing/webui/fake_camera.png`, `tests/helpers.py`（FakeCamera）
- 規約: skill `testing-strategy`, `refactor-conventions`, `hardware-test`, `agent-team-startup`
