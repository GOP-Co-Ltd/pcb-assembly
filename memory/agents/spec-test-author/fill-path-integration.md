# fill-path-integration

## 担当範囲

tests/ 配下のみ。src/ は plan-implementer が並列で対応済み（refactor/20260527/fill-path-integration ブランチ、Phase 2 src 側完了）。

## 書いたテスト一覧

すべて [tests/pcb_assembly/control/pasting/test_fill_path.py](../../../tests/pcb_assembly/control/pasting/test_fill_path.py) に集約。

### TestBuildPasteFillPath（既存維持）

- `test_normal_polygon_picks_spiral` — 正常系：通常矩形で spiral 経路（>2 点）
- `test_narrow_polygon_picks_linear[0.3-2.0-0.34]` / `[0.8-5.0-1.0]` — 正常系：細長矩形で linear 経路、長軸-nozzle 径の長さ
- `test_too_small_polygon_returns_empty[0.1-0.1-1.0]` / `[1.0-0.5-1.0]` — エッジ：長軸が nozzle より短いと []
- `test_invalid_nozzle_diameter_raises_value_error[0.0]` / `[-1.0]` / `[-0.001]` — 異常系：nozzle_diameter \<= 0
- `test_empty_polygon_returns_empty` — エッジ：empty Polygon
- `test_all_points_are_point2d` — 型契約：Point2d 型

### TestGenerateSpiralPath（test_fill.py から移送、`_generate_spiral_path` を private 参照）

- `test_path_starts_at_center` — 正常系：先頭点が `polygon.representative_point()` と一致
- `test_simple_rectangle_innermost_first` — 正常系：innermost first 順序、全点ポリゴン内包、終端が外周近傍
- `test_outward_step_matches_line_spacing[10.0-6.0-1.0-0.5]` / `[12.0-8.0-0.8-0.4]` — 正常系：セグメント長がポリゴン辺長を超えない（異常ジャンプ防止）
- `test_returns_empty_when_initial_inset_buffer_empty` — エッジ：initial_inset で buffer 空 → []
- `test_l_shape_covers_both_arms` — エッジ：L 字形状で両腕に点
- `test_dumbbell_split_handled` — エッジ：ダンベル形状で両ローブに点
- `test_invalid_line_spacing_raises_value_error[0.0]` / `[-1.0]` — 異常系：line_spacing \<= 0
- `test_invalid_initial_inset_raises_value_error[-0.1]` / `[-1.0]` — 異常系：initial_inset < 0
- `test_all_points_are_point2d` — 型契約：Point2d 型

### TestGenerateLinearPath（test_fill.py から移送、`_generate_linear_path` を private 参照）

- `test_long_horizontal_rectangle[0.3-2.0-0.17]` / `[0.8-5.0-0.5]` — 正常系：長軸方向 2 点パス、長さは長軸-2\*end_inset
- `test_returns_empty_when_too_short[0.1-0.1-0.5]` / `[1.0-0.5-0.5]` / `[1.0-1.0-0.5]` — エッジ：長軸 \<= 2\*end_inset で []
- `test_invalid_end_inset_raises_value_error` — 異常系：end_inset < 0

### 削除したテスト

- 旧 `TestGenerateConcentricRings` クラスは丸ごと削除（`generate_concentric_rings` / `_concentric_rings_one_component` が src 側で削除済みのため。計画書「設計上の決定 1」に準拠）

### 削除したファイル

- `tests/pcb_assembly/geometry/test_fill.py`

## 仕様根拠の対応表

- TestBuildPasteFillPath — 計画書「公開 IF：`build_paste_fill_path(polygon, nozzle_diameter)` の挙動」（既存維持の振る舞い契約）
- TestGenerateSpiralPath — 計画書「公開 IF：`_generate_spiral_path(polygon, line_spacing, initial_inset)` 引数不変・private 化」
- TestGenerateLinearPath — 計画書「公開 IF：`_generate_linear_path(polygon, end_inset)` 引数不変・private 化」
- concentric テスト削除 — 計画書「設計上の決定 1：`generate_concentric_rings` と `_concentric_rings_one_component` を削除」

## 期待される失敗

なし。src 側は plan-implementer が Phase 2 で完了済みのため、移送と import 付け替え完了の時点で全テスト pass する状態に到達。

## 実装側に求める修正

なし。src/ 側は既に統合・private 化済み。テスト側からの追加修正要求はない。

## tests/helpers.py への追加

なし。本タスクは純粋幾何関数のテスト移送のみで、HAL ABC の fake は使用しない。3rd-party モック（shapely、pytest、numpy 等）も導入せず、`shapely.Polygon` と `shapely.geometry.Point` を実オブジェクトで直接構築している。

## 検証結果

- `make format`: pass（ruff-format で 1 ファイル自動整形あり、再実行で pass）
- `uv run pytest tests/pcb_assembly/control/pasting/test_fill_path.py -q`: **28 passed in 0.08s**（spec-test-author スコープでグリーン）
- 合流時の `make test-no-hardware` は親側で確認（test_fill.py の旧 import 経路が破綻していた状態が解消されたため、collect は通る想定）

## 計画外の判断ログ

なし。計画書「Phase 2 spec-test-author 作業範囲」通り。

- 移送方針：assert / parametrize 値は完全に維持、import 付け替えと関数名 `_` prefix 化、クラス docstring の関数名参照を `_generate_spiral_path` / `_generate_linear_path` に揃える
- import グルーピング：3 関数を 1 ブロックの `from pcb_assembly.control.pasting.fill_path import (...)` に統合（ruff-format 自動整形）
- `shapely.geometry.Point as ShapelyPoint` は spiral テストのみで使う追加 import
