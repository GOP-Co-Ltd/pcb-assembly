# HeightPlane 平面フィット → 一般2次曲面フィット

承認済み計画書: `/home/gop/.claude/plans/height-plane-0-1mm-wild-pascal.md`
仕様: `z_offset = c + a*x + b*y + d*x² + e*y² + f*x*y`、最小点数 3→6、共線テスト→退化配置テスト。

## 編集したテストファイル

- `tests/pcbasm/geometry/test_transform.py`（`class TestHeightPlane` を2次曲面仕様へ全面更新）
- `tests/pcbasm/test_config.py`（`class TestProbe`：min_samples 3→6 へ追従。計画書 section 2 由来）

`src/` は一切編集していない。

## 書いたテスト一覧（TestHeightPlane）

- `test_apply_point3d_evaluates_plane` — 正常系：平面 z=0.1x+0.2y を6点で与え (5,5)→z_offset 1.5（後方互換）
- `test_corner_and_edge_values`（parametrize 5 ケース）— 正常系：角・辺上で平面値復元（abs=1e-9）
- `test_outside_sample_extent_extrapolates` — エッジ：サンプル外 (100,100)→30.0、2次項≈0で平面外挿
- `test_recovers_convex_paraboloid` — 新規・正常系：凸パラボロイド z=-0.001(x²+y²)+0.1x+0.2y+0.3 を9点フィット、サンプル外 (7,3) で復元
- `test_recovers_twisted_surface` — 新規・正常系：ねじれ面 z=0.002xy+0.05x-0.03y+1.0 を9点フィット、(7,3) で復元
- `test_apply_point2d_returns_unchanged` — 正常系：Point2d 素通し
- `test_inverse_roundtrip_on_plane` — 正常系：平面での inverse ラウンドトリップ
- `test_inverse_roundtrip_on_quadratic_surface` — 新規・正常系：反り＋ねじれ面での inverse ラウンドトリップ
- `test_fewer_than_six_points_raises_value_error`（parametrize 0/1/2/5点）— 異常系：6点未満で ValueError、match `"6点以上"`
- `test_collinear_points_raises_value_error` — 異常系：共線6点 → design行列 rank<6 → ValueError、match `"退化"`
- `test_least_squares_fits_noisy_quadratic_points` — 正常系：ノイズ付き2次曲面9点から最小二乗復元（abs=0.05、実測 diff≈0.004）

旧テストからの差し替え:
- `triangle_points`(3点) → `plane_points`(非退化6点)。平面 z=0.1x+0.2y を維持し既存値アサーションをそのまま通す。
- `test_apply_with_four_points` 削除（6点未満は ValueError になるため）。
- `test_fewer_than_three_points_raises_value_error` → `test_fewer_than_six_points_raises_value_error`。
- `test_collinear_points_raises_value_error` / `test_collinear_four_points_raises_value_error` → 退化6点1本に統合。

補助: モジュールレベル `_quadratic_z(x,y,c,a,b,d,e,f)` を追加（期待値生成のみ。テスト対象ではない）。

## TestProbe（config）の追従

- `test_sample_defaults`: min_samples 期待 3→6
- `test_valid_custom_values`: min_samples 4→7（4 は新ガードで無効になるため）
- `test_min_samples_less_than_3_raises` → `test_min_samples_less_than_6_raises`（min_samples=5, match `"min_samplesは6以上"`）
- `test_min_samples_greater_than_max_samples_raises`: min/max を 8/6 に（両方6以上にして順序ガードのみを隔離）

## 退化メッセージの想定文言

実装側（既に更新済み）は `"pointsが退化しています。2次曲面フィットには非退化な6点以上が必要です。"`。
テストは substring `"退化"` で検証（実装文言に依存しすぎない）。`"6点以上"`・`"min_samplesは6以上"`・`"min_samplesはmax_samples以下"` も substring で一致確認済み。

## 想定 pass/fail と実測

- 計画上は「実装が平面のままなら新テストは赤」の想定だったが、**plan-implementer が並行で `src/` を既に2次曲面へ更新済み**だった。
- 実測（合流時点）:
  - `TestHeightPlane` 18 件 全 pass
  - `TestProbe` config 追従後 全 pass
  - `make format` pass、`make test-no-hardware` = **504 passed, 15 deselected**（実機テストは未実行）

## 実装側に求める修正

なし。`src/pcbasm/geometry/transform.py`・`src/pcbasm/config.py` は仕様どおり実装済みでテストと整合。
（モック不使用・実 numpy のみ・内部 private 直接テストなし。testing-strategy 準拠。）
