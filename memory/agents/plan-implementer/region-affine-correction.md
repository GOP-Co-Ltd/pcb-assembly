# region-affine-correction — plan-implementer の判断ログ

計画書: `memory/agents/implementation-planner/region-affine-correction.md`
ブランチ: `feature/20260729/region-alignment-average`
担当: `src/` と設定テンプレート 2 本のみ（`tests/` は `spec-test-author`）

## 計画からの逸脱・追加

### 1. `RegionAlignment.increment` を追加（IF 変更通知）

orchestrator の裁定 3（「収束しなかった区は採用して警告。ただし最終パスの増分を
`RegionAlignment` に持たせ、ログと overlay に出す」）に従い、計画書の IF に無い
`increment: Point2d` を `displacement` の直後に足した。`converged` は
`increment.norm <= converge_tolerance_mm` そのものなので、フラグと数値の両方が
実機ログで読める。`spec-test-author` へ `SendMessage` で通知済み。

- `board_ops`: `passes=N (増分 X.X um)` + `※収束せず`
- `board_tour` overlay: `passes=N (+X.X um)` + `NOT CONVERGED`

### 2. `constraint` を 0 でクランプ

計画書の擬似コードは `constraint = eigvalsh(...)[0]` を素のまま `_Candidate` へ入れ、
閾値判定だけ `max(constraint, 0)` していた。それだと `min_sharpness=0.0` のとき
丸めで微小な負値になった区が採用され、`AlignmentRegion.predicted_sharpness`
（`sqrt(constraint / n)`、変更対象外）が定義域外で落ちる。λ_min は Gram 行列の
固有値なので数学的に非負であり、格納時に `max(..., 0.0)` するのが正しい。
旧実装の `if constraint <= 0.0: continue` が担っていたガードの置き換え。

### 3. `measure` は `while True` + 明示カウンタ

計画書が許した選択肢のうち、`match` / `increment` が未束縛になり得ないほうを採った
（`for` の後で return する形だと pyright が unbound を疑う）。ループ内で必ず
`RegionAlignment` を返すので `max_passes >= 1` の前提だけで型が閉じる。

## 実装で確認した事実

- **二重計上していないことの直接確認**: 同じ設定で 2 パス走らせると、投影エッジ
  マスクの重心が 1 パス目と 2 パス目で完全に一致する（(640.000, 360.000)）。
  `with_correction(Shift(cumulative))` で `T_b' = T_b + cum`、移動先が
  `anchor + cum` なので `s − T_b'(b) = anchor − T_b(b)` が厳密に保たれる。
- **`tests/pcbasm/posctrl/test_alignment.py` の `_board_image()` には 0.47px の
  系統バイアスがある（合成フィクスチャ固有。実機では起きない）**。
  `cv2.rectangle` の終点が inclusive で塗り domain が 81px になるため、Canny した
  観測エッジが設計 polyline に対して常に (−0.468, −0.468) px ずれる。

  **これがテストで累積したのは、合成観測が 1px 格子に丸められて
  サブピクセルの補正移動を画に描けないため**で、アルゴリズムの性質ではない。
  推定量のバイアスを `b` とすると 1 パス目の測定は `offset₁ = 真値 + b`、
  補正後に幾何的に残る量はちょうど `−b` になる。実機はステージが連続に動くので
  2 パス目の観測はその `−b` を実際に含み、そこへ推定量が再び `+b` を足して
  **`offset₂ = −b + b = 0` で相殺する**。合成画像では `−b` を整数格子に描けず
  幾何が 0 のままなので、同じ `b` を 2 回測ってしまい、累積が `max_correction`
  を超えて `RuntimeError` になっていた。フィクスチャ側で終点を 1px 広げて
  （`(681 + dx, 401 + dy)`）`b` 自体を +0.023px に落とすと、2 パス目の増分は
  0.0032mm（< `converge_tolerance` 0.01mm）で期待どおり収束する。

  → **実機で「毎区 ※収束せず」が出たときにこの節を根拠にしないこと。**
  実機の未収束は照明・Canny・銅箔形状の実誤差か下記の指令位置の量子化が原因で、
  合成テストの離散化アーティファクトとは別物。
- **`fit_displacement` の `anchor_spread_mm`** は `σ_min(P_centered)/sqrt(n)` なので、
  アンカーの x と y に相関があると「y の RMS」より小さく出る。y が `±1` 交互の
  6 点（x は等間隔）では 1.0 ではなく 0.95604。`model` の判定は変わらない。
- **タイル位相は pad 重心固定**なので、`board_edge_margin = 0` でも ROI が外形に
  接するとは限らない（格子位相の偶然）。マージンの効き目は「margin を上げると
  ROI が外形から margin 以上離れる」「区数が減る」で見るのが正しい。

## 実機で反復（`max_passes`）を読むときの注意（`code-reviewer` の検算より）

- **`observed_at` は Klipper の指令位置**（`gcode_move.gcode_position`）なので、
  2 パス目の微小移動が物理的に出ない分がそのまま増分に乗る。半 step ≈ 2.5um
  程度の量子化が下限になり、planner が見積もった 2 パス目の理想利得
  1.8um と同オーダー。**増分ログが常に数 um で張り付くなら `max_passes = 1` に
  落として計測時間を半分にするのが妥当**（設計はそれを許してある）。
- **反復は固定点反復で、収束は `R ∘ Q ≈ I`（`R` = `offset_transform`、
  `Q` = その共役）に依存する**。実機の `offset_transform` は約 180° 回転
  （involution なので `R² ≈ I`）で条件を満たすため反復は安定し、むしろ
  1 ショット版が持っていた共役誤差 `2 sin δ · |D|`（`δ` = 180° からのずれ）を
  消してくれる。`offset_transform` が 180° から大きく外れた機体では
  この前提が崩れるので、その場合は増分ログが振動するかどうかで見る。

## 既定値（orchestrator 裁定を反映）

`region_size_px = 300` / `min_regions = 4` / `max_passes = 2` /
`converge_tolerance = 0.01`。`region_count` は削除。
`data/config-templates/kurousagi.paste/machine.toml` と
`data/testing/config/machine.toml` を同期（canny / blur は機体ごとの値を維持）。

## 検証

`spec-test-author` が上記 4 点（`increment` / 既定値 / フィクスチャのバイアス /
期待値の算術）を反映したあとの最終状態:

- `make format` パス / `make type` 0 errors
- `make test-no-hardware` 1716 passed
- `make test-e2e` 51 passed
- `grep -rn '</content>' src tests data` ヒットなし
- `region_count` / `corrected_projector` / `.spread` は `src/` から消滅
  （残るのは「撤去済み」を確認するテストの否定アサートとコメントだけ）
