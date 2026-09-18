# はんだ塗布のタクトタイム表示

## 要件（ユーザー）

- タスク実行時にタイマーをスタートし、終了時に止める（事後の実測タクトタイム）
- 実行前に予測されたタスク時間を「h時間 m分 s秒」で表示（事前の見積り）

## 段階 1: 計画

### ユーザー確認（AskUserQuestion）で確定した分岐

| 論点       | 採用                     | 却下                                                   |
| ---------- | ------------------------ | ------------------------------------------------------ |
| 予測の根拠 | 理論計算                 | 実績履歴ベース / 理論 + 実測学習（実績ストアが要る）   |
| 対象ジョブ | はんだ塗布のみ           | 塗布系ジョブ全て（ジョブごとに見積り関数が要る）       |
| 表示位置   | 実行フォーム上に常時表示 | 開始時ログのみ / フォーム + コンソールの残り時間       |

### 公開インターフェース

pcbasm（装置非依存・実機不要でテスト可能）:

```python
# pcbasm/pasting/fill_sequence.py
def trapezoidal_time(distance: float, rate: float, accel: float) -> float  # private からの公開化
class FillSequence:
    @property
    def dispense_duration(self) -> float  # prime + 吐出（= 経路走行）の所要時間 [sec]

# pcbasm/pasting/tact.py（新規）
@attrs.frozen
class TactEstimate:
    setup_sec: float
    dispense_sec: float
    pad_count: int
    @property
    def total_sec(self) -> float

def estimate_paste_tact(
    pads: Sequence[Pad],
    params_for: Callable[[Pad], PasteParams],
    *,
    config: PasteDispenser,
    tact: Tact,
    start: Point2d = Point2d(0.0, 0.0),
) -> TactEstimate

# pcbasm/config.py
@attrs.frozen
class Tact:  # [tact] セクション（欠落時は既定値）
    travel_speed: float = 10.0    # 接近・退避の移動速度 [mm/sec]（printer.cfg の max_velocity）
    travel_accel: float = 50.0    # 移動加速度 [mm/sec^2]（printer.cfg の max_accel）
    setup_sec: float = 300.0      # 位置合わせ・高さ計測・パージ等の固定オーバーヘッド [sec]
class Machine:
    @property
    def tact(self) -> Tact
```

web:

```python
# web/api/jobs/manager.py
class JobRecord:
    @property
    def elapsed_seconds(self) -> float  # 実行中は現在まで、終端は確定値

# web/api/models.py
class JobSummary:
    elapsed_seconds: float | None = None

# web/api/routers/pasting_view.py
class TactEstimateResponse(BaseModel):
    total_seconds: float
    setup_seconds: float
    dispense_seconds: float
    pad_count: int
def build_tact_estimate(loaded: Loaded, tact: Tact) -> TactEstimateResponse

# web/api/routers/pasting.py
GET /api/pasting/tact-estimate -> TactEstimateResponse
```

frontend:

- `app.js`: `formatDuration(seconds) -> "h時間m分s秒"`（`window.webui` へ公開）
- `job_console.js`: `#jc-elapsed` を job_status の `elapsed_seconds` 基準に毎秒更新、終端で停止
- `tact_estimate.js`（新規、paste_solder ページのみ）: `/api/pasting/tact-estimate` を取得して表示。
  `state_changed` と pad 設定保存後に再取得

### 見積りモデル（塗布ループ）

pad ごとに `FillPlan.for_pad` で成分別ポリラインを生成し、成分ごとに

1. travel: 直前の終点 → 始点上空（XY）+ 下降（lift_height）を台形プロファイルで
2. `FillSequence.dispense_duration`（prime + 吐出）
3. 終端: リトラクションと Z 上昇が同時開始なので `max(retract_time, lift_time)`

board 座標で計算する（board → machine は回転 + 平行移動で等長なので経路長は不変）。

### 計画外の判断と理由

- **h時間m分s秒 の整形は JS 側 1 箇所に置く**。webui-thin-wrapper は「表示文字列はサーバーで組む」
    が原則だが、経過時間はクライアントが毎秒更新するので JS の整形が必須。同じ表記規則を
    Python と JS に二重化するより、サーバーは秒数のみ返し整形を JS に集約する方が矛盾が少ない。
    サーバーが返す値（秒数）を JS で再導出はしていない
- **`[tact]` を新設して printer.cfg の速度を写す**。Klipper から max_velocity を取ると見積りが
    装置接続に依存し、フォーム表示が接続状態に左右される。見積り専用パラメータとして
    machine.toml に持たせ、実測に合わせて調整できる形にする（ずれても見積りだけに効く）
- **初回パージ・流量キャリブは setup_sec に含める**。数秒〜十数秒で、位置合わせ（数分）に対して
    誤差。専用の見積りを足すとモデルが複雑になる（AGENTS.md 開発原則 2）

### テスト観点

- `trapezoidal_time`: 加速のみで届く距離 / 定速区間ありの 2 ケース
- `FillSequence.dispense_duration`: レート cap あり / なし、経路長 0（点塗布）
- `estimate_paste_tact`: pad 0 枚は setup のみ / pad 数に応じて dispense_sec が増える /
    塗布量が倍なら吐出時間も概ね倍
- `Machine.tact`: `[tact]` 欠落で既定値、記載値の読み込み
- `JobRecord.elapsed_seconds`: 実行中は増える / 終端で固定される
- `GET /api/pasting/tact-estimate`: PCB 未選択で 409 / 有効 pad の数が反映される
- E2E（ASGITransport）: `JobSummary.elapsed_seconds` が返る

## 段階 2 以降

（進行に応じて追記）

## 段階 2-3: テスト → 実装

計画どおり。公開 IF の変更点だけ計画から外れたものを記す。

- `trapezoidal_time` の公開化は**取り下げた**。見積りが要るのは「停止 → 加速 → 巡航 → 減速 →
    停止」の移動時間で、`FillSequence` 内の ramp モデル（吐出は次動作へ連続するので減速しない）
    とは別物だったため。`tact.py` に `_move_duration` を置き、`fill_sequence` 側は
    `_trapezoidal_time` のまま戻した
- `FillPlan.component_amount_ul` を追加し、成分 1 本あたりの塗布量の式を
    `PasteApplicator.apply` と見積りで共有した（式の二重化を避ける）

## 段階 4: 自己レビュー + code-reviewer

`code-reviewer` の verdict は request-changes。must-fix 2 件は**いずれも妥当**で、自分で
`klipper/inversed_corexy.py` と各 `printer.cfg` を確認して裏を取った。

| 指摘 | 内容 | 対応 |
| ---- | ---- | ---- |
| M1 | 純 Z 移動（下降・上昇）を XY の速度・加速度で見積もっていた。kinematics は Z 成分を含む移動を `max_z_velocity` / `max_z_accel` へ落とす（kurousagi では XY 50/250 に対し Z 5/15）。pad 1 枚で約 -38% | `[tact]` に `z_speed` / `z_accel` を追加し、下降・上昇に使う |
| M2 | 移動時間に減速区間が無かった。ステージ移動は `wait_for_done` / `G4 P0` / `sync` で毎回停止するので加速 + 巡航 + 減速が正しい | `_move_duration` を新設（三角プロファイル込み）。リトラクションも停止 → 停止なのでこちらへ寄せた |
| S1 | `per_component_ul` の式が二重化 | `FillPlan.component_amount_ul` へ集約 |
| S2 | pad 編集のたびに全 pad の経路を再生成（1000 枚で 1 秒） | JS 側で 400 ms debounce |
| S3 | `build_tact_estimate` の docstring が不正確（pad の出どころが実行時と違う） | 記述を訂正 |
| S4 | `[tact]` が `data/config-templates/README.md` 未記載で printer.cfg と同期が切れる | 節を追加 |
| S5 | `TactEstimateResponse` がバナー直下に入りセクションが崩れていた | モデル定義の位置を戻した |
| T1/T2/T5 | 移動時間の式が値として固定されていない / 空 plan / 量配分の一致が未固定 | 3 テストを追加（閉形式・Z 速度の効き・空ポリゴン・成分配分） |

### 却下した指摘

- **S6「実測タクトが画面にしか残らない（`setup_sec` 調整の材料が消える）」** — 今回は
    「開始で計り、終了で止める」までが要求。ジョブ結果サマリーへ実測を焼き込むと、
    表記の組み立てが Python と JS の 2 箇所になる（整形を JS 1 箇所に寄せた判断と衝突する）。
    記録が要るという判断はユーザーに委ねる

## 段階 5: ドキュメント

- `src/pcbasm/pasting/README.md`: `tact.py` の行と依存方向
- `data/config-templates/README.md`: `[tact]` と printer.cfg の対応（同期が切れると見積りだけ外れる）
- `docs/webui.md`: 経過時間と予測所要時間の見方、ずれたときに直す設定
