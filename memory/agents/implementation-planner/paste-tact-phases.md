# ジョブ時間のフェーズ別計測・待ち時間の設定化・所要時間の理論分解

## 概要

ジョブの所要時間を「どの段階に何秒、うち人待ち何秒」まで分解して実測・記録し、要所の待ち時間を
`machine.toml` から制御できるようにする。そのうえで予測を「フェーズまるごとの定数」ではなく
**点数 × (移動 + 静定 + 撮影/検出/プローブ) の理論式 + 少数の実測単価**へ組み替える。
計測と設定はジョブ共通の仕組みとして `JobContext` / `JobRecord` に入れ、塗布固有の見積りだけを
`pcbasm/pasting/tact.py` に置く。

## スコープ（2 群に分ける）

ブランチは `feature/2026-09-18/paste-tact-time` のまま。commit 3723201 の上に積み、最終的に 1 本の
PR にまとめる。**main へは merge しない。**

| 群 | 内容 | 今回 |
| -- | ---- | ---- |
| **I. 観測と設定** | MR-A 待ち時間の設定化 / MR-B フェーズ別計測 / MR-C 待ちの 3 分類 / MR-D 計数と履歴の永続化・内訳表示 | **実装する** |
| **II. 予測** | MR-E 照合領域数の理論計算 / MR-F フェーズ別理論見積り / MR-G 単価の実測フィットと予測 vs 実測 | 計画のみ。実測を見てから着手 |

群 I が終われば「観測できない・設定できない」は解消する。群 II の理論式は本書で確定させるが、
式の中の単価の既定値は群 I の実測が出てから詰める。

## 設計判断

### 1. フェーズ境界は `ctx.progress(stage)` に相乗りする。明示 API は `ctx.sleep` だけ足す

- `progress` の stage 名が変わった時点で前フェーズを閉じ、次を開く。同名連続（`ctx.progress("塗布", pct)`
    のループ）は同一フェーズを延長する
- ジョブ開始〜最初の `progress` は既定ラベル `"開始"`、最後の `progress`〜`finish` は最後のフェーズ。
    **タイムラインに隙間を作らない**（`sum(span.elapsed_sec) == elapsed_seconds` が不変条件）。
    「全体の時間を完全に把握」はこの不変条件で表す
- `with ctx.phase(...)` は足さない。`progress` で表現できるものを二重化するだけで call site も増える
    （AGENTS.md 原則 2）
- 例外は装置の待ち（静定待ち）。progress では区別できないので `ctx.sleep` を足す

### 2. 待ちの 3 分類は runtime 側で自動計上する

| 種別 | 捕捉点 | ジョブ側の変更 |
| ---- | ------ | -------------- |
| 人待ち | `_JobRuntime.prompt` の待機、`next_command(timeout=None)` のブロック | 不要 |
| 装置待ち | `JobContext.sleep(seconds, reason=...)` | `_wait_to_settle` の置換のみ |
| 装置稼働 | 残り（`elapsed - operator_wait - device_wait`） | 不要 |

- 待ちは**必ず現在のフェーズへ加算する**。フェーズ遷移は worker スレッドでしか起きず、待機中の
    worker はブロックしているので、1 つの待ちが 2 つのフェーズに跨ることはない
- `next_command(timeout=0)`（`drain_commands`）は人待ちに数えない。ブロックしないため
- `run_loading_loop` は「待つ → 押出を実行する → また待つ」を繰り返すが、ブロック区間だけを計上
    するので押出の実行時間は装置稼働に残る。ジョブ側から測るのは不可能な区別で、runtime に置く理由
- 残る取りこぼし: G4 dwell（下記 6）。ホスト側からは移動時間と分離できず、装置稼働に入る

### 3. 実測は機体ごとに履歴を貯める（ユーザー確定）

`data/webui/job_timings/<job_name>/<started_at>-<job_id>.json`、job_name ごとに直近 50 件。
`JobRecord` は非永続のまま（公開 IF を壊さない）で、`JobManager` が終端時に 1 件書き出す。
record は `source_pcb` / `board_id` / 計数を持ち、基板ごとの参照はキーでの絞り込みで行う
（基板ごとに別ツリーを作らない）。

### 4. 計数をジョブが記録する（`ctx.count`）

**フェーズの合計時間だけでは単価が同定できない。** 銅箔照合が 200 秒かかったとき、それが
「20 領域 × 2 パス」なのか「10 領域 × 4 パス」なのかで 1 パスの単価は倍違う。パス数は収束次第で
データ依存なので理論では出ない。したがって**ジョブが実際にやった回数を record へ残す**必要がある。

`align_regions` は `passes={alignment.passes}` を既にログへ出しており、数える材料は揃っている。
群 II の単価フィットはこの計数が前提なので、**計数 API は群 I に入れる**（これが無いと群 II で
フィットする材料が残らない）。

### 5. 予測は「構造は理論式、式の中の少数の単価だけ実測」（ユーザー指示で方針変更）

前案の「フェーズまるごとを履歴の中央値で置く」を撤回する。理由はユーザーの指摘どおりで、
基板や設定が変わると中央値が外れる（pad が倍になれば probe 点数も領域数も変わる）。

- セットアップ・高さ計測・銅箔照合・pad照合・初回パージ・流量キャリブは、すべて
    **点数 × (移動 + 静定 + 撮影/検出/プローブ)** に分解する
- 点数は装置を動かさずに決まる（下記「予測の理論式」）
- 移動時間は PR #34 の `_move_duration`（XY / Z で速度を分ける）をそのまま使う
- 静定待ちは MR-A で設定値になるので**予測式に直接入る**。設定を下げれば予測も下がる
- 理論で出ないのは 6 個の単価だけ（下記）。これらはフェーズ別計測 + 計数から同定できる

**前案で却下した「銅箔照合の領域数の理論計算」は撤回して計算する。** 幾何ロジックを二重化せずに
済む道があるため（下記 MR-E: `plan_alignment_regions` から数える部分を純関数として切り出し、
実行時は実測アフィン、見積りは名目アフィンで**同じ関数**を呼ぶ）。

### 6. 待ち時間は `[settle]` 1 セクションへ集約し、コンストラクタ引数を必須化する

現状 `settle_time` は 8 クラスのコンストラクタ引数だが**渡している構築側が 1 つも無い**。
既定値が事実上の唯一の値で、機体ごとに変えられない。詳細は次章。

### 7. `config.Tact.setup_sec` は削除する

群 II で「セットアップ」は理論式 + `setup_fixed_sec`（接続・カメラ初期化・G28）に置き換わるので、
`setup_sec`（= 前段処理すべての固定分）の役割は無くなる。`[tact]` は cattrs の
`structure` を通しており余剰キーを無視する（確認済み）ため、既存 machine.toml に
`setup_sec = 300` が残っていても読み込みは落ちない。README から記述を消す。

______________________________________________________________________

## 待ち時間の棚卸しと設定化

### 棚卸し（grep で裏取り済み。渡された一覧からの訂正を含む）

**A. ステージ移動後の静定（撮影・計測の前）。すべて `GCode.wait()` = G4 dwell**

| 箇所 | 現在の既定 | 用途 |
| ---- | ---------- | ---- |
| `posctrl/position.py:39` `XYPositionAdjustor` | 0.5 | 補正移動後、撮影前 |
| `posctrl/aligner.py:43` `RegionAligner` | 0.5 | 領域へ移動後、撮影前 |
| `posctrl/board.py:49` `BoardTransformMeasurer` | 0.5 | 基板四隅へ移動後 |
| `posctrl/offset.py:39` `OffsetTransformMeasurer` | 0.5 | 2 点法の移動後 |
| `pasting/capture.py:33` `PointCapturer` | 0.5 | 撮影点へ移動後 |
| `pasting/height.py:66,130` `move_settle_time` | 0.5 | プローブ点へ移動後 |
| `pasting/toolhead_offset.py:363,480` | 1.0 | **ホスト側 `time.sleep`** |
| `posctrl/setup.py:252` | 1.0 | **名前の無いハードコード。ホスト側 `time.sleep`** |

**B. 工程固有の待ち**

| 箇所 | 既定 | 扱い |
| ---- | ---- | ---- |
| `pasting/probe.py:20` `ProbeExecutor.settle_time`（G4） | 0.0 | 設定に出す（A とは別現象） |
| `config.py:224` `FlowCalibration.settle_seconds` | 10.0 | **既に設定可能。据え置く** |
| `config.py:380` `PasteDispenser.prime_extra_delay` | 0.0 | 待ちではなく吐出量に効く。**合成しない** |

**C. 再試行の間隔（設定に出さない）**

渡された一覧の `posctrl/setup.py:55 CircleDetector.retry_delay` は誤りで、正しくは
**`OffsetObserver.retry_delay`**（`CircleDetector` は `vision/detection.py:177` で `retry_delay` を
持たない）。既定 0.0 に対し `toolhead_offset.py:42 _DETECTION_RETRY_DELAY = 0.5` だけが実値を
渡しており、既定と実値が食い違っている。検出実装の都合であって機体差ではないので設定には出さず、
**既定を 0.5 に揃える**（この不整合の解消だけ行う）。

**D. 表示・ポーリング粒度（設定に出さない）**

`posctrl.py RESULT_DISPLAY_SEC = 1.0` は実時間を食うが posctrl 系ジョブ専用で塗布ジョブには
現れない。`POSITION_CACHE_SEC` / `_PROMPT_POLL_SEC` / `_SETTLE_TICK_SEC` は応答性の粒度。
いずれもフェーズ計測では「装置稼働」に入る。

**E. 通信タイムアウト（設定に出さない）**

`PRESENT_TIMEOUT` / `MOVE_TIMEOUT` / `CLEAN_TIMEOUT` / `STATUS_TIMEOUT` / `Z_QUERY_TIMEOUT` /
`COMMAND_TIMEOUT` / `_APLAY_TIMEOUT`。**上限であって待ちではない**（正常時は 0 秒消費）。

### 合成

**A の 8 箇所は 1 個（`settle.move_sec`）に束ねる。** いずれも「ステージが止まってから像・機構が
落ち着くまで」という同一の物理現象で、8 箇所とも 0.5 か 1.0 の未調整な既定値。軸差（XY / Z）で
分ける根拠は現状の値から読み取れない。分ける必要が出るのは「Z 下降後だけ長く要る」等の実測が
出てからで、その時点で `settle.z_move_sec` を足せばよい（先回りしない）。

- 1.0 の 2 箇所（`toolhead_offset` / 基準点移動）が 0.5 に短くなる。**実機確認が要る**（確認事項 1）
- ホスト側 `time.sleep` の 2 箇所は `GCode.wait()` へ寄せる。移動 gcode に連結すれば
    `wait_for_done()` が飲み込むので、待ちの実装が A 全体で 1 通りになる
- `probe.settle_time`（PROBE 実行後）は別現象なので分ける。現状 0.0 = 無効で、設定に出すことで
    初めて有効化できる
- `FlowCalibration.settle_seconds` は**移さない**。ステージの静定ではなくペーストが広がるのを待つ
    工程そのもので、既に `machine.toml` にあり設定 UI と塗布ページのフォームにも載っている
    （`web/ui/layout.py:186` / `web/api/config_store.py:116`）。移すと既存 machine.toml と UI の
    キー許可リストが壊れる。ドキュメント上は同じ「待ち一覧」に並べて所在を示す

### 設定ファイルの形

`[tact]` には入れない。`[tact]` は「見積り専用で装置の動作には効かない」ことが契約
（`config.py` の `Tact` docstring と `data/config-templates/README.md:47-53`）で、実動作に効く値を
混ぜると契約が壊れる。`[audio]` / `[tact]` と同じ**省略可能セクション**を新設する。

```toml
[settle]
move_sec = 0.5    # ステージ移動後、撮影・計測に入るまでの静定待ち [sec]
probe_sec = 0.0   # PROBE 実行後の待ち [sec]
```

`[probe]` 配下に `probe_sec` を置く案も取れるが、**待ちを 1 箇所で見渡せること**を優先する
（今回の要求そのもの）。`[probe]` は必須セクション（欠落で `KeyError`）なのに対し `[settle]` は
省略可能にしたい、という実装上の差もある。

### 計測との対応

- A と B の待ちは **G4 dwell** なので `wait_for_done()` の往復の中で消費され、ホスト側の
    ストップウォッチでは移動時間と分離できない。**フェーズ計測では「装置稼働」に入り、
    `ctx.sleep` の「装置待ち」には現れない**
- 例外は流量キャリブの静定。元々ホスト側 `time.sleep` なので MR-C で `ctx.sleep` へ移り、
    「装置待ち」に立つ
- したがって「静定待ち合計」は実測ではなく**理論値**として出す:
    `move_sec × 移動回数 + probe_sec × probe 点数`。移動回数は `ctx.count` の計数から確定する
- 値を詰める判断は「設定を変えて再実行し、同じ基板のフェーズ `active_sec` を前後比較する」で行う。
    履歴ストア（MR-D）がその比較を成立させる。**これが「設定できる」と「観測できる」を繋ぐ経路**
- 群 II の予測では settle を**設定値から直接引く理論項**として積む。単価に織り込むと、設定を
    変えたときに予測が追従しない

______________________________________________________________________

## 公開インターフェース（群 I）

### `src/pcbasm/config.py`（追加）

```python
@attrs.frozen
class Settle:
    """装置の待ち時間（[settle]。セクションごと省略可）.

    ``move_sec`` はステージが止まってから撮影・プローブに入るまでの静定待ち、
    ``probe_sec`` は PROBE 実行後の待ち。どちらも G4 dwell として送る。
    """
    move_sec: float = 0.5
    probe_sec: float = 0.0
    def __attrs_post_init__(self) -> None: ...   # 非負の有限値（0 を許す）

class Machine:
    @property
    def settle(self) -> Settle      # [settle] 未設定・キー欠落は既定値
```

配り方: **各クラスのコンストラクタ引数を残し、構築側が `machine.settle` から解決して渡す。**
`Settle` オブジェクト自体を末端クラスへ配らない（1 つの float のために設定型へ依存させない。
テストも float を渡すだけで済む）。`pcbasm` が `web` を import しない制約は、**構築側が
すでに全て `Machine` を持っている**ので自然に守られる（下表、grep で確認済み）。

```python
# 引数名を統一し、既定値を消して必須化する（既定値を残すと「また誰も渡さない」が再発し、
# 渡し忘れを型チェックで検出できない）
XYPositionAdjustor(..., settle_sec: float)          # 旧 settle_time: float = 0.5
RegionAligner(..., settle_sec: float)               # 同上
BoardTransformMeasurer(..., settle_sec: float)      # 同上
OffsetTransformMeasurer(..., settle_sec: float)     # 同上
HeightPlaneMeasurer(..., settle_sec: float)         # 旧 move_settle_time: float = 0.5
ProbeExecutor(..., settle_sec: float)               # 旧 settle_time: float = 0.0
PointCapturer(session, ...)                         # session.machine.settle から内部解決
ToolheadOffsetProcedure(result, ...)                # result.machine.settle から内部解決
```

| 構築側 | 手元にある設定 | 渡す値 |
| ------ | -------------- | ------ |
| `posctrl/setup.py:230-290` `setup_board_calibration` | `machine` | `settle.move_sec`（+ 252 行の `time.sleep(1.0)` を `GCode.wait` へ） |
| `posctrl/alignment.py:134` `RegionAlignmentSession` | `result.machine` | `settle.move_sec` |
| `pasting/session.py:82-90` `PasteSession.from_calibration` | `machine` | Probe は `probe_sec`、Height は `move_sec` |
| `pasting/toolhead_offset.py:388-411` `ToolheadOffsetProcedure` | `result.machine` | `move_sec` / `probe_sec`（+ 480 行の `time.sleep` を `GCode.wait` へ） |
| `flow_calibration.py:86` / `purge_check.py:118` / `paste_volume_calibration.py:622` の `PointCapturer` | `session.machine` | 内部解決（呼び出し側は変更なし） |

WebUI への露出: `web/api/config_store.py` の許可キーと `web/ui/layout.py` の
`EDITABLE_MACHINE_KEYS` + `FieldSpec` に `settle.move_sec` / `settle.probe_sec` を追加（単位 `s`）。
**`POSITIVE_ONLY_MACHINE_KEYS` には入れない**（0 = 待たない、が有効な設定）。

### `src/pcbasm/timing.py`（新規・装置非依存・web 非依存）

```python
@attrs.frozen
class PhaseSpan:
    label: str
    started_at: float          # ジョブ開始からのオフセット [sec]
    elapsed_sec: float
    operator_wait_sec: float
    device_wait_sec: float
    @property
    def active_sec(self) -> float

@attrs.frozen
class JobTimeline:
    spans: tuple[PhaseSpan, ...]
    @property
    def total_sec(self) -> float
    @property
    def active_sec(self) -> float
    @property
    def operator_wait_sec(self) -> float
    @property
    def device_wait_sec(self) -> float
    def merged_by_label(self) -> tuple[PhaseSpan, ...]   # 同名を合算し最初の出現順に並べる

class PhaseTimer:
    """フェーズ境界と待ちを積むビルダ（ロックは持たない。呼び手が直列化する）."""
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 first_label: str = "開始", max_phases: int = 200) -> None
    def enter(self, label: str) -> None            # 同名は no-op（フェーズ継続）
    def add_operator_wait(self, seconds: float) -> None
    def add_device_wait(self, seconds: float) -> None
    def stop(self) -> None                          # 冪等
    def snapshot(self) -> JobTimeline               # 実行中でも現在までで作れる

@attrs.frozen
class JobTimingRecord:
    version: int
    job_id: str
    job_name: str
    started_at: datetime            # 壁時計（並べ替えと表示用）
    status: str
    source_pcb: str | None
    board_id: str | None
    counts: Mapping[str, int]       # ctx.count の集計（単価フィットの分母）
    timeline: JobTimeline

JOB_TIMING_SCHEMA_VERSION: int
def encode_job_timing(record: JobTimingRecord) -> dict[str, Any]
def decode_job_timing(doc: Mapping[str, Any]) -> JobTimingRecord   # 未知 version は ValueError
```

`PhaseTimer` にロックを持たせない。`JobRecord` が既存の `self._lock` の中で呼ぶ
（pcbasm に threading を持ち込まない）。`max_phases` は動的ラベルでフェーズが際限なく増えるのを
防ぐ上限（超過分は最後のフェーズへ合算）。

### `src/web/api/job_timings.py`（新規）

`board_settings.py` と同じ分担（schema は pcbasm、ファイル所在と atomic write は web）。

```python
class JobTimingStore:
    def __init__(self, data_dir: Path, *, keep: int = 50) -> None
    def save(self, record: JobTimingRecord) -> Path       # 保存後に prune
    def load(self, job_name: str, *, limit: int = 20) -> list[JobTimingRecord]  # 新しい順
    def load_for_board(self, job_name: str, board_id: str, *, limit: int = 20) -> list[JobTimingRecord]
```

### `src/web/api/jobs/context.py`（変更）

```python
class JobBridge(Protocol):      # 実装は _JobRuntime だけ（grep 済み。外部実装なし）
    def sleep(self, seconds: float, reason: str) -> None: ...
    def count(self, key: str, value: int) -> None: ...

class JobContext:
    def sleep(self, seconds: float, *, reason: str) -> None:
        """装置の待ちを中断可能に待ち、装置待ちとして計上する（Raises: JobAborted）."""
    def count(self, key: str, value: int = 1) -> None:
        """実施回数を記録へ積む（probe 点数・照合パス数など。単価の同定に使う）."""
```

### `src/web/api/jobs/manager.py`（変更）

```python
class JobRecord:
    @property
    def timeline(self) -> JobTimeline           # 追加。実行中は現在まで、終端後は確定
    @property
    def counts(self) -> Mapping[str, int]       # 追加
    @property
    def elapsed_seconds(self) -> float          # 維持（= timeline.total_sec）
    # 内部更新（JobManager 専用）
    def set_progress(self, stage: str, percent: float | None) -> None   # 内部で timer.enter
    def add_operator_wait(self, seconds: float) -> None   # 追加
    def add_device_wait(self, seconds: float) -> None     # 追加
    def add_count(self, key: str, value: int) -> None     # 追加
    def finish(...) -> None                     # timer.stop() を含む（_finished_at を置換）

class JobManager:
    def __init__(..., timing_store: JobTimingStore | None = None, ...)  # 追加（None で保存しない）
```

### `src/pcbasm/posctrl/setup.py`（変更）

```python
def setup_board_calibration(
    machine, pcb_file_path, tolerance=0.1, *, camera=None, frame_sink=None,
    on_stage: Callable[[str], None] | None = None,   # 追加
) -> BoardCalibrationResult
```

既存の `logger.info("=== ... ===")` の区切り（ホーミング / カメラ回転角の計測 / Board変換の計測）で
呼ぶ。「セットアップ」が数分の単一ブロックのままだと、どこで時間を食っているか結局見えない。
`frame_sink` と同じ形の任意コールバックなので追加コストは小さい。

### `src/pcbasm/pasting/tact.py`（群 I では追加のみ）

```python
# 進捗ラベルの正典。web 側の ctx.progress と群 II の見積りが同じ文字列を使う
class PasteStage(enum.StrEnum):
    SETUP = "セットアップ"
    HOMING = "ホーミング"
    CAMERA_ROTATION = "カメラ回転角の計測"
    BOARD_TRANSFORM = "board変換の計測"
    HEIGHT = "高さ計測"
    COPPER_ALIGN = "銅箔照合"
    PAD_ALIGN = "pad照合"
    RETRACT = "リトラクション"
    INITIAL_PURGE = "初回パージ"
    FLOW_CALIBRATION = "流量キャリブレーション"
    DISPENSE = "塗布"

# ctx.count のキー（フェーズ計測と対にして単価を同定するための計数）
class PasteCount(enum.StrEnum):
    PROBE_POINT = "probe_point"
    ALIGN_REGION = "align_region"
    ALIGN_PASS = "align_pass"       # 全領域のパス数合計
    REFINE_PAD = "refine_pad"
    REFINE_PASS = "refine_pass"     # 全 pad のパス数合計
    FLOW_POINT = "flow_point"
    FRAME = "frame"                 # 撮影フレーム総数
    PAD = "pad"
```

### `src/web/api/models.py`（変更）

```python
class PhaseRow(BaseModel):
    label: str
    elapsed_seconds: float
    active_seconds: float
    operator_wait_seconds: float
    device_wait_seconds: float

class JobTimingInfo(BaseModel):
    total_seconds: float
    active_seconds: float
    operator_wait_seconds: float
    device_wait_seconds: float
    phases: list[PhaseRow]

class JobSummary(BaseModel):
    elapsed_seconds: float = 0.0          # 維持（JS の毎秒更新の基準）
    timing: JobTimingInfo | None = None   # 追加
```

### ルーター

```python
GET /api/jobs/timings?name=<job_name>&limit=<n> -> JobTimingHistoryResponse
```

行の組み立て（フェーズ名・秒数・計数）は**サーバーが済ませて返す**。JS は
`formatDuration` で秒を整形するだけ（`webui-thin-wrapper`）。

______________________________________________________________________

## 予測の理論式（群 II の骨子）

### 記法

- `M(d)` = `_move_duration(d, tact.travel_speed, tact.travel_accel)`（XY、停止 → 停止）
- `Mz(d)` = `_move_duration(d, tact.z_speed, tact.z_accel)`（純 Z）
- `S` = `settle.move_sec`、`P` = `settle.probe_sec`
- `Obs` = 1 観測 = `OFFSET_OBSERVE_SAMPLES × frame_sec`
    （`OFFSET_OBSERVE_SAMPLES` は `OffsetObserver.sample_count` の既定 30。
    `observe()` は 30 フレームを撮って統計を取る。pcbasm 内のコード定数を見積りが参照するだけで、
    設定項目は増やさない）

### 理論化できない 6 個の単価（`[tact]` に置く）

| 名前 | 意味 | 理論化できない理由 | 同定の仕方 |
| ---- | ---- | ------------------ | ---------- |
| `setup_fixed_sec` | Klipper 接続・カメラ初期化・キャリブ読込・G28 | ホーミング速度と軸長が printer.cfg 側、カメラ初期化は driver 依存 | セットアップ phase の残差 |
| `frame_sec` | 1 フレームの撮影 + 円検出 | Hough の CPU 時間。理論下限は `1/camera.fps` | 流量キャリブ phase ÷ `frame` 計数 |
| `align_pass_sec` | 銅箔照合 1 パスの撮影 + Canny + 照合 | 画像処理の CPU 時間 | 銅箔照合 phase ÷ `align_pass` 計数 |
| `probe_touch_sec` | PROBE 1 回（下降 → 接触 → 読み出し） | probe 速度・接近距離が printer.cfg の `[load_cell_probe]` | 高さ計測 phase ÷ `probe_point` 計数 |
| `adjust_passes` | `XYPositionAdjustor` の平均反復回数 | 収束がデータ依存（照明・基板の状態） | **同定しない**（既定固定。理由は下記） |
| `align_passes` | 銅箔照合 / pad照合の平均パス数 | 同上 | `align_pass / align_region` 計数の比 |

`adjust_passes` だけは同定しない。セットアップ phase の式には `setup_fixed_sec` と
`adjust_passes` の 2 未知数が入り、1 本の phase 合計からは分離できない。`XYPositionAdjustor` は
pcbasm 内部で反復しており `adjust()` は回数を返さないので、計数するには公開 IF の変更が要る。
**既定 2.0 に固定し、残差を `setup_fixed_sec` へ寄せる**。式の構造（4 コーナー × 移動・静定）は
理論のままなので、基板サイズが変わっても追従する。

### フェーズ別の式

**セットアップ**（`setup_board_calibration`）

```
setup_fixed_sec
+ M(|reference_point - home|) + S                      # 基準点へ移動
+ 2 × Obs + 2 × M(move_distance) + 2 × S               # カメラ回転角（2 点法・戻りを含む）
+ Σ_4corners [ M(d_corner) + S + adjust_passes × (Obs + S) ]
```

- `move_distance = safe_move_distance(camera.crop.size, margin=0.3) / pixel_per_mm`（理論）
- 四隅の巡回距離は PCB outline の width / height から出る（TL → TR → BR → BL）
- `adjust` 内の補正移動は sub-mm なので無視する（0.1 秒未満／回）

**高さ計測**（`HeightPlaneMeasurer.measure`）

```
Σ_i [ M(d_i) + S + probe_touch_sec + P + Mz(probe.lift_height) ]
```

- 点数 = `len(plan_probe_points(coppers, outline, config=machine.probe))`（装置不要・純関数）
- 巡回順 = `sort_by_nearest`（`route_points` と同じ純関数）。開始位置は名目値（パーク位置）

**銅箔照合**

```
Σ_regions [ M(d_i) + align_passes × (S + align_pass_sec) ]
```

- 領域数 = MR-E で切り出す純関数（下記）
- 巡回順 = `sort_by_nearest`（実行時と同じ）

**pad照合**

```
Σ_refine_pads [ M(d_i) + align_passes × (S + align_pass_sec) ]
```

- 対象数 = `len(refinement_targets(pads, max_short_side_mm=pad_align.refine_max_short_side))`（純関数）

**リトラクション**: `_move_duration(retract_amount, retract_rate, retract_accel)`（完全に理論）

**初回パージ**

```
M(d_purge) + S + frame_sec                 # 塗布前の撮影
+ Mz(lift) + dispense(initial_purge_ul) + Mz(lift)
+ S + frame_sec                            # 塗布後の撮影
```

- クリーニングのやり直しは詰まったときだけなので見積りに入れない（明記する）

**流量キャリブレーション**

```
2 × Σ_points [ M(d_i) + S + frame_sec ]    # 塗布前・塗布後の 2 パス
+ Σ_points [ M(d_i) + Mz(lift) + dispense(amount_ul) + Mz(lift) ]
+ flow_calibration.settle_seconds          # 設定値そのもの（厳密）
```

**塗布**: PR #34 の現行式のまま（pad ごとの移動 + 吐出）

**ローディング（`interactive_loading = true`）**: 人待ちなので見積らない（0 を入れ、
「人待ちは見積り対象外」と表示する）

### MR-E: 照合領域数の理論計算（幾何を二重化しない方法）

`plan_alignment_regions`（`posctrl/region.py:31`）から、**数える部分だけを純関数へ切り出す**。

```python
def plan_region_centers(
    pad_centers: Sequence[Point2d], *, matrix: np.ndarray, shift: np.ndarray,
    safe_area: BaseGeometry, region_size_px: int, overlap: float,
) -> list[tuple[Point2d, Polygon]]
```

- `plan_alignment_regions` はこれを呼び、巡回順と ROI を足すだけにする（実行時の挙動は不変）
- 見積りは同じ関数を**名目アフィン**（`matrix = pixel_per_mm × I`, `shift = 0`）で呼ぶ

```python
def estimate_region_count(
    pad_centers: Sequence[Point2d], *, pixel_per_mm: float, safe_area: BaseGeometry,
    region_size_px: int, overlap: float,
) -> int
```

実行時との差は board_transform の回転（基板の置き方）とカメラ回転角ぶんだけ。どちらも 1° 未満の
オーダーで、タイル分割の境界に当たった領域が ±1 ずれうる。**実装は 1 本**なので二重化にならない。
`pixel_per_mm` は `CalibrationResult.load(machine.camera.calibration_file)` から読む。
校正ファイルが無い機体では領域数を出せないので、そのフェーズだけ `basis="unavailable"` として
予測から外し、UI に理由を出す。

### 予測の出力形（群 II）

```python
@attrs.frozen
class PhaseEstimate:
    label: str                                    # PasteStage の値
    seconds: float
    basis: Literal["theory", "default", "unavailable"]
    counts: Mapping[str, int] = {}                # このフェーズの理論点数

@attrs.frozen
class JobEstimate:
    phases: tuple[PhaseEstimate, ...]
    @property
    def total_sec(self) -> float

def estimate_paste_job(
    targets: PasteTargets,          # plan_paste_targets の戻り（実行時と同じ関数を通す）
    *, machine: Machine, pixel_per_mm: float | None,
) -> JobEstimate

def fit_tact_units(records: Sequence[JobTimingRecord], *, base: Tact) -> Tact
    """履歴のフェーズ別 active_sec と計数から 6 単価を更新した Tact を返す（純関数）."""
```

`estimate_paste_job` が `plan_paste_targets` を起点にすることで、見積りの pad 選別が実行時と
必ず一致する（1 巡目レビュー S3 / T3 の指摘も同時に解消する）。
`fit_tact_units` は**提案を返すだけ**で machine.toml へは自動で書かない。反映は既存の
`ApplyPayload` / 設定ページの経路に載せる（確認事項 3）。

______________________________________________________________________

## 実装ステップ

各 MR の検証は共通で `make format && make type && make test-no-hardware`。実機テストは書いても
実行しない（`pytest` は常に `-m "not hardware"`）。

### 群 I（今回実装する）

#### MR-A: 待ち時間の設定化（MR-B と並行可能）

1. `config.Settle` + `Machine.settle`（省略可能セクション）
2. 8 クラスの `settle_time` / `move_settle_time` を `settle_sec` へ統一し**既定値を外す**
3. 構築側 5 箇所で `machine.settle` から解決して渡す
4. ホスト側 `time.sleep` 2 箇所（`posctrl/setup.py:252`、`toolhead_offset.py:480`）を
    `GCode.wait` へ寄せる
5. `OffsetObserver.retry_delay` の既定を 0.5 に揃える（設定には出さない）
6. `config_store.py` / `layout.py` のキー許可リストと設定ページ、
    `data/config-templates/README.md` に `[settle]` の節

**検証で確かめること**: `[settle]` 省略で既定値（0.5 / 0.0）になる。設定値が G4 の文字列
（`G4 P500`）として移動 gcode に現れる。`probe_sec = 0` で G4 が出ない。渡し忘れが
`make type` で落ちる（既定値を外したことの担保）。

#### MR-B: フェーズ別計測の基盤

1. `src/pcbasm/timing.py` に `PhaseSpan` / `JobTimeline` / `PhaseTimer`（clock 注入）
2. `JobRecord` が `PhaseTimer` を持ち、`set_progress` / `finish` から駆動。`timeline` を公開。
    `elapsed_seconds` は `timeline.total_sec` へ委譲（挙動は不変）
3. `models.JobSummary.timing` を追加し `routers/jobs.py` で詰める
4. `job_console.js` に終端後の内訳表示（サーバーが返す行をそのまま描画）

**検証で確かめること**: 隙間なし（`sum(span.elapsed_sec) == elapsed_seconds`）。同名連続の
`progress` が 1 フェーズに畳まれる。終端後に `snapshot` が伸びない。`elapsed_seconds` の
既存テストが通る（後方互換）。

#### MR-C: 待ちの 3 分類

1. `JobContext.sleep` + `JobBridge.sleep` + `_JobRuntime.sleep`（刻んで `checkpoint`、
    経過を `record.add_device_wait`）
2. `_JobRuntime.prompt` / `next_command` のブロック区間を `record.add_operator_wait` へ
3. `flow_calibration._wait_to_settle` を `ctx.sleep` へ置換。stage 名から秒数を外して
    `PasteStage.FLOW_CALIBRATION` に固定（動的ラベルでフェーズが分裂するのを防ぐ）
4. `setup_board_calibration(on_stage=...)` を追加し `board_ops.setup_board` から `ctx.progress`
    を渡す。セットアップを 3 フェーズに割る
5. 進捗ラベルを `PasteStage` の定数へ寄せる（現状 `"高さ計測"` が 4 モジュールに直書き）

**検証で確かめること**: `prompt` の待機が人待ちに、`ctx.sleep` が装置待ちに、それぞれ現在の
フェーズへ載る。`next_command(timeout=0)` は人待ちに載らない。3 分類の和が各フェーズの
`elapsed_sec` に一致する。`ctx.sleep` 中の abort が `JobAborted` になる。

#### MR-D: 計数と履歴の永続化

1. `JobContext.count` + `JobRecord.counts`
2. `paste_solder` / `common.prepare_paste_workflow` / `align_regions` / `flow_calibration` /
    `purge_check` に `ctx.count(PasteCount...)` を置く（`alignment.passes` /
    `refinement.result.passes` は既にログに出ている値をそのまま積む）
3. `pcbasm/timing.py` に `JobTimingRecord` / `encode` / `decode` / スキーマ版
4. `web/api/job_timings.py` の `JobTimingStore`（atomic write は `pcbasm.atomic`）
5. `JobManager.__init__(timing_store=...)`、`_run_worker` の終端で保存
    （保存失敗はログに落としジョブ結果に影響させない）
6. `GET /api/jobs/timings`、はんだ塗布ページに直近 N 件の内訳

**検証で確かめること**: store の round-trip / prune（51 件目で最古が消える）/ 未知 version は
`ValueError`。manager が終端で 1 件書く（FAILED / ABORTED も）。ASGITransport で履歴エンドポイント。
`ctx.count` が record に積まれ JSON に載る。

### 群 II（計画のみ。実測を見てから着手）

- **MR-E**: `plan_region_centers` の切り出しと `estimate_region_count`
    （検証: 既存の `plan_alignment_regions` のテストが通ること + 名目アフィンでの領域数が
    実アフィンでの領域数と一致すること）
- **MR-F**: `[tact]` を 6 単価へ、`setup_sec` 削除、`estimate_paste_job`、見積り API のフェーズ化
- **MR-G**: `fit_tact_units`、予測 vs 実測の突き合わせ表示、実行中の残り時間

## テスト観点

### 正常系

- `PhaseTimer`: 隙間なし／同名連続で 1 フェーズ／`first_label` から始まる／`stop` 後は
    `snapshot` が伸びない／`max_phases` 超過は最後のフェーズへ合算
- 待ちの分類: 3 分類の和が各フェーズの `elapsed_sec` に一致する
- `prompt` の待機が人待ちとして現在のフェーズに載る（`answer_next_prompt` 併用）
- `next_command(timeout=None)` のブロックが人待ち、`timeout=0` は載らない
- `ctx.sleep(0.05, reason=...)` が装置待ちに載る
- `ctx.count` が加算され、同じキーの複数回呼び出しが合算される
- `Settle`: `[settle]` 省略で既定値、記載値の読み込み
- 移動 gcode に `settle.move_sec` が G4 として乗る（`GCode.wait(0)` が空になることも固定）
- `JobTimingStore`: save → load の round-trip、`keep` 超過で古い順に消える、
    `load_for_board` が board_id で絞る
- `GET /api/jobs/timings` の応答形

### 異常系

- `Settle` の負値 / 非有限で `ValueError`
- `decode_job_timing` の未知 version / 壊れた JSON → `ValueError`
- `ctx.sleep` 中の abort → `JobAborted`、そこまでの待ちは計上済み
- FAILED / ABORTED ジョブも record は保存される（群 II の単価フィットからは除外する）
- timing_store の書き込み失敗でジョブ結果が壊れない
- `[settle]` に余剰キーがあっても読み込みが落ちない（cattrs の既定。確認済み）

### エッジケース

- `progress` を一度も呼ばないジョブ → `"開始"` 1 フェーズだけ
- ジョブ開始直後（elapsed ≈ 0）の `snapshot`
- 待ちがフェーズの全長を占める（`active_sec == 0`）
- 実行中の `snapshot` を別スレッド（WS 配信）から取る
- `ProbeExecutor` が `probe_sec = 0.0` のとき G4 を出さない
- 計数 0 件（pad 0 枚の基板）

## リスク・トレードオフ

- **`settle.move_sec` の統合で 2 箇所が 1.0 → 0.5 に短くなる**（`toolhead_offset` と基準点移動）。
    振動が残っていれば最初の円検出がぶれる。実機確認が要る（確認事項 1）。ぶれるなら
    `move_sec` を 1.0 にして全体を揃える
- **G4 dwell は実測で分離できない**。`settle` を下げた効果は「静定待ち」という行としてではなく、
    フェーズの `active_sec` が縮むことで確認する。UI で誤解を招かないよう、内訳には
    「うち静定待ち（設定値 × 計数の理論値）」と明示して並べる
- **ラベル結合が壊れやすい**。群 II の見積りラベルと `ctx.progress` のラベルが文字列一致で繋がる。
    `PasteStage` に寄せて緩和するが、塗布ジョブは実機なしで通しテストできないので
    「見積りのラベルが実測に現れる」ことは自動検証できない。`PasteStage` の値以外を
    `ctx.progress` へ渡さない規律に頼る（レビュー観点）
- **`adjust_passes` を同定しない**ことで、セットアップの予測は `setup_fixed_sec` に残差を
    押し込む形になる。基板サイズが変われば式は追従するが、照明が変わって収束回数が増えると外れる
- **領域数の名目アフィン近似**は基板の置き方の回転ぶんだけ実行時とずれうる（±1 領域）。
    record に実際の `align_region` 計数が残るので、事後に誤差を確認できる
- **`JobSummary` の肥大**。`timing` は WS の `job_status` ごとに飛ぶ。フェーズ数は塗布で 10 前後
    なので許容（`max_phases` で上限も付ける）
- **`JobBridge` の拡張**は Protocol だが実装は `_JobRuntime` のみ（grep 済み）。テストにも fake
    実装は無いので公開影響は無い
- **作り直す既存コード**: `JobRecord` の時刻管理（`_started_at` / `_finished_at` → `PhaseTimer`）、
    8 クラスの `settle_time` 引数（`settle_sec` へ改名・必須化）、
    群 II で `pcbasm/pasting/tact.py`（`TactEstimate` 廃止）と `config.Tact`（`setup_sec` 削除・
    6 単価の追加）と `routers/pasting_view.build_tact_estimate` / `tact_estimate.js`。
    残すもの: `_move_duration`、`FillSequence.dispense_duration`、`FillPlan.component_amount_ul`、
    `JobRecord.elapsed_seconds` の公開形、`formatDuration`

## 確認事項

1. **`settle.move_sec` を 0.5 に統一してよいか**（暫定案: 8 箇所を 1 個に束ね、既定 0.5。
    `toolhead_offset` と基準点移動の 1.0 が短くなる）。実機で最初の円検出がぶれないかの確認が要る。
    ぶれるなら既定を 1.0 にして全体を揃える（その場合は全体のタクトが伸びる）
2. **`FlowCalibration.settle_seconds` を `[settle]` へ移すか**（暫定案: **移さない**。
    ステージの静定ではなく工程そのもので、既に設定 UI と塗布ページのフォームに載っている。
    移すと既存 machine.toml とキー許可リストが壊れる）
3. **群 II の単価を machine.toml へ自動反映するか**（暫定案: `fit_tact_units` は提案を返すだけで
    自動では書かない。反映は既存の Apply / 設定ページの経路で人が押す）。群 II 着手時に決めればよい

## 参照

- 実測・予測の現状: `src/pcbasm/pasting/tact.py`、`src/web/api/jobs/manager.py:91-230`（`JobRecord`）、
    `src/web/api/jobs/manager.py:242-414`（`_JobRuntime`）
- 進捗ラベルの一覧: `grep -rn "ctx.progress(" src/web/api/jobs/`
- 待ちの実装: `src/pcbasm/posctrl/{position,aligner,board,offset,setup}.py`、
    `src/pcbasm/pasting/{capture,height,probe,toolhead_offset}.py`、
    `src/web/api/jobs/pasting/flow_calibration.py:143-158`（`_wait_to_settle`）
- 理論計算に使う純関数: `pasting/height.py:24`（`plan_probe_points`）、
    `pasting/alignment.py:47`（`refinement_targets`）、`posctrl/region.py:31`
    （`plan_alignment_regions`）、`pasting/workflow.py`（`plan_paste_targets`）
- 観測 1 回のフレーム数: `posctrl/setup.py:39-160`（`OffsetObserver.sample_count = 30`）
- 永続化の先行例: `src/web/api/board_settings.py` + `src/pcbasm/pasting/persist.py`
- 設定の露出: `src/web/api/config_store.py:100-135`、`src/web/ui/layout.py:165-195`
- 前タスクの判断: `memory/agents/orchestrator/paste-tact-time.md`、
    `memory/agents/code-reviewer/paste-tact-time.md`
- 規約: skill `testing-strategy` / `refactor-conventions` / `webui-thin-wrapper`
