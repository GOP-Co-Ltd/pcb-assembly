# region-pad-align

## 計画外の判断ログ

- **`PadAligner.__init__` に `image_size` / `search_window_px` を追加した**（計画書は
  `align()` メソッドのシグネチャのみ凍結、`__init__` は対象外）。align() 契約
  「ROI がフレーム(search_window inset)に収まらなければ ValueError」を実装するには
  フレームサイズと探索窓幅が必要で、`CopperProjector` / `CopperEdgeMatcher` は
  「無変更」指定のためそれらの private 属性を読まず、Session 側で計算して
  明示的に渡す形にした。`PadAlignmentSession.__init__` の収容制約（analytic な
  region_size×ρ+2×(margin+window)≤FOV）とは別に、align() 自身も実際の ROI が
  フレームに収まるかを（search_window 分の余白込みで）検証する二重チェックに
  なっている。PadAligner は本セッション以外から構築されないため実害はないが、
  belt-and-suspenders 的な追加である。
- **`posctrl/render.py`（`PadResultRenderer`）を計画外で修正した**。
  `config.py` から `PadAlign.min_roi` を削除すると `render.py` が
  `pad_align.min_roi` を参照していてクラッシュするため、モジュール定数
  `_DEFAULT_MIN_ROI_MM = 3.0`（旧 `PadAlign.min_roi` の既定値と同値）に置き換えて
  挙動を保った。`tests/pcbasm/posctrl/test_render.py` は `PadAlign()` の既定値に
  依存するのみで `min_roi` 属性自体は参照していないため、この修正で
  引き続きグリーンのはず（実行未確認、下記参照）。
- **`config_store.py` の `max_failures` FieldSpec ラベルも「許容部品数」→
  「許容領域数」に更新した**（計画は config.py の docstring のみ言及）。
  UI 表示文言が意味変更後も古いままになるのを避けるための直接的な追従。

## 他implementerへのIF変更通知（並列時）

なし（spec-test-author とは計画書の凍結 IF どおりに実装。関数/クラスの
公開シグネチャは計画書のコードブロックから逸脱していない）。

## 既知の制約・残課題

- **`tests/pcbasm/test_config.py` の `test_pad_align_section_overrides_defaults`
  （L114-125）が `pad_align.min_roi == 5.0` を assert しており、`min_roi` 削除で
  確実に壊れる。** このファイルは spec-test-author の担当リスト
  （test_pad.py / test_alignment.py / test_board_ops.py / test_pasting.py /
  test_posctrl.py）に含まれておらず、私の編集範囲（src/ + configs/）にも
  含まれないため、どちらの agent も編集していない。`min_roi = 5.0` を含む
  TOML 追記と対応する assert 行の削除が必要（1テストメソッド内の1行修正）。
  同様に `tests/pcbasm/posctrl/test_render.py` の
  `test_empty_roi_polygons_falls_back_to_min_roi` は `pad_align.min_roi` 属性
  自体は参照していないため壊れないはず（実行して確認要）。
- 収容制約の formula は「各軸を独立に region_size[i]×ρ+2×(margin+window) ≤
  FOV[i]」で実装（ρ=|cosθ|+|sinθ|、θ は board_transform の x 単位ベクトル
  からの実回転）。crop が正方形（現行 configs は全て正方形）なら計画書の
  単一スカラー式と数値的に同一。crop が非正方形になった場合の一般化としての
  拡張であり、正方形前提を崩す変更ではない。
- `posctrl/README.md` に `ComponentAlignments` / `sorted_top_component_pads`
  の記載が残っている（docs-keeper 担当領域として計画書どおり未着手）。

## 検証結果

- make format（変更ファイルへの `pre-commit run --files ...`。tests/ は
  spec-test-author が並列編集中のため `-a`（全ファイル）は意図的に避けた）: pass
  （ruff / docformatter が対象ファイルを自動整形、再実行で green）
- make type: `uv run pyright src` は 0 errors。参考までに `uv run pyright`
  （リポジトリ全体、tests/ 含む）も現時点のスナップショットで 0 errors
  だった（spec-test-author の作業が実装時点で概ね完了していたため）。ただし
  tests/ は並列編集中のため今後変動しうる
- make test: 未実行（指示により実機/pytest 実行は行っていない。代わりに
  scratchpad の ad-hoc スクリプトで `plan_pad_regions` の基本/巨大 pad
  再割当て、`PadAlignmentSession` の構築・収容制約 ValueError・align 成功、
  `RegionAlignments.result_for` の同値比較、`sorted_top_pad_regions` を
  FakeCamera + 実 test-fixture 設定で一通り smoke 実行し、全て期待通りの
  挙動を確認済み）
