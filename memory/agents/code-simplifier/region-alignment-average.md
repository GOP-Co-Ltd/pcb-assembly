# 銅箔照合を「数領域の平均補正」へ作り直す — 簡素化とドキュメント同期

対象: `feature/20260729/region-alignment-average`（未コミット）。
入力: `memory/agents/code-reviewer/region-alignment-average.md`（verdict approve、S7 + nit 12 件）。

検証: `make format` pass / `make type` 0 errors / `make test-no-hardware` **1660 passed** /
`make test-e2e` **51 passed** / `grep -rn '</content>' src tests` ヒットなし。
実機テスト（`make test` / `@mark_hardware`）は実行していない。

## 1. ドキュメント同期（S7）

`src/pcbasm/posctrl/README.md` を全面更新。

- 旧 API 2 行（`PadAligner` / `PadAlignmentSession` / `ComponentAlignments` /
  `sorted_top_component_pads`）を撤去し、`AlignmentRegion` / `plan_alignment_regions` /
  `RegionAligner` / `RegionAlignment` / `BoardAlignment` / `RegionAlignmentSession` に差し替え
- 「銅箔照合の設計」節を新設し、レビュー指示の 3 点を明記:
  - 並進のみ（回転・スケールは扱わない）・3 点放物線でサブピクセル
  - `sharpness` による開口問題（縮退）の検出と棄却
  - 補正は board ポリゴンを書き換えず、**機械座標へ出る瞬間**（`board → correction →
    toolhead → height`）に 1 回だけ適用する
- 領域を撮像前に幾何（`λ_min(Σ L·n nᵀ)`）で選ぶことを追記

## 2. 対応した nit

| nit | 対応 |
|---|---|
| N1 `sharpness` の単位表記 | `sharpness` 自体に付いていた `[px]` を外し、正しい定義側（「弱軸方向へ 1px ずらしたときの RMS chamfer 距離の増分 [px]」）に単位を移した。`min_sharpness` は「`EdgeMatch.sharpness` と同じ尺度」と書き換え、設定 UI に「px」と出ていた `FieldSpec` の `unit` を削除（`config_store.py`）。`machine.toml` 2 本と `config.py` のコメントからも `[px]` を除去。`region.py` の `constraint: λ_min(A) [px]` は本当に px 次元なので**残した** |
| N2 `cstar` が負に振れ得る | ガードは既存の `max(cstar, 0.0)` で足りている。コメントを「FFT 丸め（実測 -8.9e-08）や放物線当てはめ誤差で負値になり得る」に拡張し、丸めだけが理由でないことを残した（挙動は変えない） |
| N3 窓端ガードの診断劣化 | `copper.py` の窓端ガードに「この判定は sharpness より前なので、『探索窓を超えるずれ』も『拘束不足』も呼び出し側には None としてしか見えない」とコメント。エラーメッセージは `aligner.py` の文言をテストが押さえているため変更していない |
| N4 `region.index` 依存 | `measure_regions` を `enumerate(regions)` に変更し、progress / log を渡されたリストの位置から作るようにした。併せて `f"領域 {i+1}/{n}"` を `label` に括り出して 3 箇所の重複を排除 |
| N5 `_candidate_grid` の `count == 1` 分岐 | 削除。`hi == lo` のとき `ceil(0) + 1 == 1` で `np.linspace(lo, hi, 1) == [lo] == [(lo+hi)/2]` なので同値。`max(1, ...)` も同様に不要なので落とし、docstring に「bbox が 1 軸で潰れている場合はその 1 点だけ」と明記 |
| N6 stale docstring | `tests/webui/test_config_store.py` の `TestCameraCropFields` から削除済み `max_failures` への参照を除去 |
| N10 サブピクセルテストの `abs=0.15` | `test_match_recovers_subpixel_shift` の docstring に「シフト値は余裕のある 3 点を選んである。±1px を 0.1px 刻みで振ると最悪誤差 0.127px まで伸びるので、網羅的に増やすなら許容も緩める」と追記（テストの意味は変えていない） |

## 3. 却下した nit と理由

| nit | 却下理由 |
|---|---|
| N7 設定セクション名「パッド位置合わせ」 | 設定キーが `pad_align` のままである以上、ラベルだけ変えるとキーとラベルが乖離する。計画書が明示的に据え置きと決めた論点で、キー改名は別タスク |
| N8 `projector` と `board_transform` の二重受け取り | `plan_alignment_regions` の公開シグネチャそのもの。計画書のトレードオフ節で検討済みの設計で、変更は公開 IF 変更にあたる |
| N9 `centered_roi` の 0.5px 中心ずれ | 「一辺 size_px の整数 ROI」を取る以上 `(width - size)` が奇数なら必ず生じる原理的な性質。既定値では偶奇が揃って発生せず、隠す対策（中心を丸める等）は挙動を複雑にするだけ |
| N11 `pasting.py` の未使用 import `Machine` | 元から dead（`HEAD` でも同じ 1 件が出る）。CLAUDE.md「元から dead だったコードは頼まれない限り消さない」に従い指摘のみ |
| N12 高さ計測が補正前 XY で probe | 変更前と同一挙動で回帰ではない。`height_plane` の定義域が機械 XY なので補正後 XY で評価する側は正しく、非対称そのものは設計どおり。実機の probe 点を動かす判断はユーザー領域 |
| S3（平均の外れ値除去） | orchestrator が却下済み。推定器（平均並進のみ）と `spread` のログ出力は現状維持し、一切触っていない |

## 4. 簡素化（公開 IF は不変。1 点だけ追加あり）

### `src/pcbasm/posctrl/region.py`

- `_Candidate` NamedTuple（`constraint` / `edge_length_px` / `board_xy`）を導入。
  `candidate[2]` や `for _, _, (sx, sy) in selected` という位置参照が消え、貪欲選択の
  最小分離判定が `(candidate.board_xy - other.board_xy).norm >= region_mm` になった
- 候補格子を `tuple[float, float]` から `Point2d` に変更（board 点は既に `Point2d` で
  扱っている型なので、`Point2d(x=x, y=y)` への詰め直しが 1 箇所減る）
- `_projected_segments` の戻り値を `(P0, P1, L, N)` → `(P0, D, L, N)` に変更。
  呼び出し側の `delta = p1 - p0` と `p1` の keep フィルタが不要になった
- 選択後の `anchored` 中間リストを削除し、`sort_by_nearest` に直接
  `(anchor, candidate)` を渡す形へ

### `src/pcbasm/posctrl/copper.py`

数式は 1 文字も変えていない（`hxy = a5`、`a3 = -s0/3 + sx2/2`、`cstar` の係数はそのまま）。
`_fit_sharpness` の docstring に「返すのは弱軸方向へ 1px ずらしたときの RMS chamfer 距離の
増分 [px]」を明示し、コメントを上記 N2 / N3 のとおり補強しただけ。

### `src/webui/jobs/{board_ops,posctrl}.py`

- `t, m = alignment.translation, alignment.match` の 1 文字変数を
  `translation, match` に改名（board_ops の log と posctrl の `render_measured`）
- `measure_regions` の `f"領域 {index+1}/{len(regions)}"` を `label` に括り出し
- **`AlignmentRegion.predicted_sharpness` プロパティを追加**し、`posctrl.py` にあった
  `math.sqrt(region.constraint / region.edge_length_px)` を置換。理由は CLAUDE.md
  「WebUI 設計（ロジックは pcbasm、JS/router は薄いラッパー）」— 予測 sharpness の式は
  ドメイン計算であって表示変換ではない。副産物として `posctrl.py` の `import math`（この
  1 箇所のために追加されていた）が不要になり、`test_region.py` のローカルヘルパー
  `_predicted_sharpness`（同じ式の重複実装）も削除して公開プロパティのピンに置き換えた。
  **これが今回唯一の公開 IF 追加**（既存クラスへの派生プロパティ 1 個、破壊的変更なし）

## 5. 触っていないもの（指示どおり）

`src/pcbasm/posctrl/correction.py` / `position.py` / `tests/pcbasm/posctrl/test_correction.py` /
`config/machine.toml`、`_fit_sharpness` と `_parabolic_subpixel` の数式、`BoardAlignment` の
推定器と `spread` のログ。テストの削除・許容の緩めもしていない。
