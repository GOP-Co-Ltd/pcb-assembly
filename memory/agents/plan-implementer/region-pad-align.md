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

## 設計変更対応（region_size 分離、MR !138 提出後のユーザー再指示）

orchestrator ノート（`memory/agents/orchestrator/region-pad-align.md` の
「設計変更・MR !138 提出後」項）と計画書 (`region-pad-align-plan.md` 差分は
なし。凍結 IF は orchestrator からの追加指示テキストで指定) に基づき、
領域サイズの導出元を `camera.crop÷pixel_per_mm` から新設定
`pad_align.region_size`（正方形、既定 10.0mm）へ切り替えた。

- **`src/pcbasm/config.py`**: `PadAlign.region_size: float = 10.0` を
  フィールド先頭（`tolerance` の前）に追加。`__attrs_post_init__` に
  `region_size <= 0` の ValueError を追加（既存 `max_failures` 検証と同じ
  colon スタイルのメッセージ）。フィールド順は「領域サイズ→収束→検出→
  失敗許容」の概念順で `tolerance` の直前に置いた（計画書に位置指定なし、
  既存フィールドの並びは崩さず追加のみ）
- **`src/pcbasm/posctrl/alignment.py`**: `_region_size` を
  `result.machine.paste_dispenser.pad_align.region_size` を読んで
  `(size, size)` を返すだけに変更（crop/pixel_per_mm 参照を削除）。
  `sorted_top_pad_regions` / `_validate_region_fits_frame` /
  `PadAlignmentSession.__init__` の docstring・エラーメッセージの
  「crop由来」表現を「pad_align.region_size由来」に置換。収容制約の
  数式（ρ=|cosθ|+|sinθ|、per-axis 検証）自体は変更していない（code-reviewer
  が承認した現行実装のまま、入力元だけを差し替え）
- **`src/pcbasm/posctrl/pad.py`（計画外・追従）**: `PadAligner.align()` の
  belt-and-suspenders 検証 `_validate_roi_fits_frame` にも同一文言の
  ValueError メッセージ「camera.crop を縮小するか...」が複製されていたのを
  grep で発見（この二重チェック自体は前回セッションの計画外判断として
  ログ済み・reviewer 承認済み）。凍結 IF・変更ファイル一覧には pad.py は
  含まれていないが、タスク指示の「crop 由来を前提にした他の記述が src 内に
  残っていないか grep（crop × posctrl）で確認し、位置合わせ経路のみ追従」に
  従い、同じ文言修正（`pad_align.region_size を縮小するか...`）を適用した。
  テストに文字列ピンがないことを事前に grep で確認済み
  （`tests/pcbasm/posctrl/test_pad.py` に `camera.crop`/`を縮小` の一致なし）
- **`src/webui/config_store.py`**: `paste_dispenser.pad_align.region_size`
  の FieldSpec（ラベル「関心領域サイズ」、unit "mm"）を pad_align 系の先頭に
  追加。`_coerce` の float 分岐に `solder_paste_density` と同型の正値検証
  （`<= 0.0` で `UnknownFieldError`）を追加
- **`configs/test-fixture/machine.toml` / `configs/kurousagi/machine.toml`**:
  `[paste_dispenser.pad_align]` の先頭行に `region_size = 10.0` を明示追加
  （既定値と同値だが、他の pad_align フィールドと同様に明示する既存スタイルに
  合わせた）。`data/testing/machine.toml` は `[paste_dispenser.pad_align]`
  テーブル自体を持たない（全デフォルト依存）ため変更不要と確認
- **`src/pcbasm/posctrl/README.md`**: 「crop 由来サイズで分割」の記述を
  「`pad_align.region_size` で分割」に修正（1 行）

### 検証（本設計変更分）

- `uv run pre-commit run --files <変更7ファイル>`: 初回 ruff-format が
  config_store.py の新規 FieldSpec 行を1行化して green（再実行で全 hook
  pass）
- `uv run pyright src`: 0 errors, 0 warnings
- `make test` / `make test-no-hardware` は指示により未実行（tests/ は
  spec-test-author 側で並列対応中のため、テスト側の答え合わせは合流後の
  orchestrator/code-reviewer 検証に委ねる）
- `grep -rn '</content>' src configs`: 検出なし
- `grep -rn "crop"` を `src/pcbasm/posctrl/` 全体に対して再実行し、位置合わせ
  経路（alignment.py / pad.py）に crop 由来の記述が残っていないことを確認。
  他ファイル（setup.py / tour.py / copper.py / render.py）の `crop_size` は
  カメラの実クロップ機能（本タスクと無関係の別用途）であり意図的に不変
