# phase5: fill_path テストを公開 API 経由に再設計

`tests/pcbasm/pasting/test_fill_path.py` が private 関数
`_generate_linear_path` / `_generate_spiral_path` を直接 import してテスト
していた（規約違反・pyright `reportPrivateUsage` warning）。これを公開 API
`build_paste_fill_path(polygon, nozzle_diameter)` 経由の振る舞い検証に再設計した。

`src/` は一切編集していない。private を public に昇格させる解法は採らず、
テスト側を公開 API 経由に直した。

## 分岐の誘発方法（公開 API のみ）

`build_paste_fill_path` のロジック（fill_path.py L39-56）に基づく:

- `nozzle_diameter <= 0` → `ValueError`
- `polygon.is_empty or not polygon.is_valid` → `[]`
- `line_spacing = nozzle_diameter`, `initial_inset = nozzle_diameter / 2`
- `polygon.buffer(-nozzle_diameter).is_empty` が真 → **線形分岐**（end_inset = nozzle/2）
- それ以外 → **螺旋分岐**（数値誤差で空なら線形フォールバック）

実測で全分岐が公開 API 経由で到達可能なことを確認済み:

- 螺旋: `Polygon([(0,0),(10,0),(10,6),(0,6)])` + nozzle=1.0 → 16点, 先頭=(5,3)
- 線形: `Polygon` 0.3x2.0 + nozzle=0.34 → 2点 / 0.8x5.0 + nozzle=1.0 → 2点
- 空: 0.1x0.1 + nozzle=1.0（buffer空 かつ 線形も短すぎ）→ []
- 空: 1.0x0.5 + nozzle=1.0（線形分岐だが長軸<=2*inset）→ []
- L字 + nozzle=1.0 → 47点（両腕被覆）, ダンベル + nozzle=0.8 → 78点（両ローブ）

## 旧テストのカバレッジ意図 → 新テストの対応表

| 旧テスト | カバレッジ意図 | 新テスト |
| --- | --- | --- |
| `TestBuildPasteFillPath::test_normal_polygon_picks_spiral` | 大ポリゴン→螺旋(>2点) | `TestSpiralBranch::test_large_polygon_yields_multi_point_path` |
| `TestBuildPasteFillPath::test_narrow_polygon_picks_linear` | 細ポリゴン→線形2点・最長軸・端点インセット | `TestLinearBranch::test_narrow_polygon_yields_two_point_path` + `test_linear_path_runs_along_longest_axis_with_end_inset` + `test_linear_path_centered_on_short_axis`（観点ごとに分割） |
| `TestBuildPasteFillPath::test_too_small_polygon_returns_empty` | 小さすぎ→[] | `TestInvalidInput::test_polygon_too_small_for_nozzle_returns_empty` |
| `TestBuildPasteFillPath::test_invalid_nozzle_diameter_raises_value_error` | nozzle<=0→ValueError | `TestInvalidInput::test_non_positive_nozzle_diameter_raises_value_error` |
| `TestBuildPasteFillPath::test_empty_polygon_returns_empty` | 空ポリゴン→[] | `TestInvalidInput::test_empty_polygon_returns_empty` |
| `TestBuildPasteFillPath::test_all_points_are_point2d` | 戻り値型 | `TestReturnType::test_all_points_are_point2d` |
| `TestGenerateSpiralPath::test_path_starts_at_center` | 先頭=representative_point | `TestSpiralBranch::test_spiral_starts_at_representative_point` |
| `TestGenerateSpiralPath::test_simple_rectangle_innermost_first` | 全点内包・先頭中心近傍・終端外周近傍 | `TestSpiralBranch::test_spiral_all_points_inside_polygon` + `test_spiral_first_point_near_center_last_near_exterior`（観点分割） |
| `TestGenerateSpiralPath::test_outward_step_matches_line_spacing` | 異常ジャンプなし（最大セグメント長<=最長辺） | `TestSpiralBranch::test_spiral_has_no_abnormal_jumps` |
| `TestGenerateSpiralPath::test_returns_empty_when_initial_inset_buffer_empty` | buffer空→[] | `TestInvalidInput::test_polygon_too_small_for_nozzle_returns_empty`（公開 API では buffer空は線形分岐に入り、線形が短ければ[]になる経路で被覆） |
| `TestGenerateSpiralPath::test_l_shape_covers_both_arms` | L字両腕被覆 | `TestSpiralBranch::test_spiral_covers_both_arms_of_l_shape` |
| `TestGenerateSpiralPath::test_dumbbell_split_handled` | ダンベル分裂両ローブ被覆 | `TestSpiralBranch::test_spiral_covers_both_lobes_of_dumbbell` |
| `TestGenerateSpiralPath::test_all_points_are_point2d` | 戻り値型 | `TestReturnType::test_all_points_are_point2d`（螺旋分岐ポリゴンで集約） |
| `TestGenerateLinearPath::test_long_horizontal_rectangle` | 線形2点・最長軸・端点インセット・短軸中央 | `TestLinearBranch` の3テスト（上記） |
| `TestGenerateLinearPath::test_returns_empty_when_too_short` | 長軸<=2*end_inset→[] | `TestInvalidInput::test_polygon_too_small_for_nozzle_returns_empty`（1.0x0.5+nozzle=1.0 が該当） |

## 削った検証とその理由

testing-strategy「実装詳細・private のプリコンディションはテストしない」に従い、
以下は**公開 API の契約ではない private ヘルパー固有のプリコンディション**のため削除:

1. `TestGenerateSpiralPath::test_invalid_line_spacing_raises_value_error`
   （line_spacing<=0 で ValueError）
   - 理由: `line_spacing` は公開 API が `nozzle_diameter`（既に正と検証済み）から
     導出する内部パラメータ。公開 API 経由で line_spacing<=0 を渡す経路は存在しない。
     private ヘルパーの防御的ガードであり公開契約ではない。nozzle 検証は
     `TestInvalidInput::test_non_positive_nozzle_diameter_raises_value_error` で被覆。

2. `TestGenerateSpiralPath::test_invalid_initial_inset_raises_value_error`
   （initial_inset<0 で ValueError）
   - 理由: 同上。`initial_inset = nozzle_diameter / 2` は常に非負。公開 API では負値
     経路が成立しない private プリコンディション。

3. `TestGenerateLinearPath::test_invalid_end_inset_raises_value_error`
   （end_inset<0 で ValueError）
   - 理由: 同上。`end_inset = initial_inset = nozzle_diameter / 2` は常に非負。

補足: truncate 長・rotate 順序・`_estimate_ring_spacing` などの内部手順は元々
直接アサートしておらず、「異常ジャンプなし」「全点内包」「両腕/両ローブ被覆」
という振る舞い契約として残している（実装手順が変わっても壊れない形）。

## 書いたテスト一覧（21 ケース、4 クラスに集約）

- `tests/pcbasm/pasting/test_fill_path.py`
  - `TestInvalidInput::test_non_positive_nozzle_diameter_raises_value_error`[3] — 異常系
  - `TestInvalidInput::test_empty_polygon_returns_empty` — 異常系
  - `TestInvalidInput::test_polygon_too_small_for_nozzle_returns_empty`[2] — エッジ
  - `TestLinearBranch::test_narrow_polygon_yields_two_point_path`[2] — 正常系（線形）
  - `TestLinearBranch::test_linear_path_runs_along_longest_axis_with_end_inset`[2] — 正常系
  - `TestLinearBranch::test_linear_path_centered_on_short_axis`[2] — 正常系
  - `TestSpiralBranch::test_large_polygon_yields_multi_point_path` — 正常系（螺旋）
  - `TestSpiralBranch::test_spiral_starts_at_representative_point` — 正常系
  - `TestSpiralBranch::test_spiral_all_points_inside_polygon` — 正常系
  - `TestSpiralBranch::test_spiral_first_point_near_center_last_near_exterior` — 正常系
  - `TestSpiralBranch::test_spiral_has_no_abnormal_jumps`[2] — エッジ（異常ジャンプ防止）
  - `TestSpiralBranch::test_spiral_covers_both_arms_of_l_shape` — エッジ（凹形状）
  - `TestSpiralBranch::test_spiral_covers_both_lobes_of_dumbbell` — エッジ（分裂形状）
  - `TestReturnType::test_all_points_are_point2d` — 型契約

## tests/helpers.py への追加

なし。純計算（shapely + Point2d）のため fake 不要。3rd-party モックも内部関数
モックも使用していない。`@mark_hardware` 不要。

## 検証結果

- private 参照: ゼロ。`fill_path` からの import は `build_paste_fill_path` のみ。
  （task 検証 grep は `test_spiral_*` 等のテストメソッド名に substring 一致するが、
  private 関数 import/呼び出しは存在しない。`fill_path\._` 検索でゼロ確認済み）
- `make format`: pass（ruff-format が初回整形後 2 回目で pass）
- `uv run pytest tests/pcbasm/pasting/test_fill_path.py -v`: 21 passed
- `make test-no-hardware`: 465 passed, 15 deselected
- `make type`: 0 errors, 0 warnings（reportPrivateUsage warning 消失）

仕様 first ではなく既存実装の振る舞いを公開 API 経由に翻訳した再設計のため、
全テスト green が正常な完了状態。実装側に求める修正はなし。
