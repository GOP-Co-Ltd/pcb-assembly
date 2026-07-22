# spec-test-author ノート: region-pad-align

計画書: memory/agents/orchestrator/region-pad-align-plan.md（「凍結する公開 IF」「設計（確定）」「テスト」節）

plan-implementer が同じ計画書から src/ を並列実装中のため、公開 IF は計画書のシグネチャから一切変更していない。

## 追加・変更したテスト

### tests/pcbasm/posctrl/test_pad.py — `TestGroupPadsByComponent` → `TestPlanPadRegions`

- `test_single_pad_creates_one_region` — 単一pad→1領域、key=(0,0)
- `test_nearby_pads_in_same_cell_are_merged_into_one_region` — region_size内の近接padが同一領域へ併合（冗長照合解消）
- `test_pads_in_different_cells_are_kept_separate_and_sorted_by_key` — 離れたpadは別key、(col,row)昇順で返る
- `test_grid_is_anchored_at_board_origin_not_at_pad_bounding_box` — 他pad追加/非追加でも対象padのkeyが不変（原点固定グリッドのピン。min-bboxベースの相対グリッドだと壊れる）
- `test_giant_pad_is_reassigned_to_cell_with_max_boundary_intersection` — サーマルパッド相当（copper半辺7mm、region_size 10mm）の再割当て。中心セル(0,0)は交差長0、隣接4セルが同値10mmでタイブレーク(col,row)昇順により(-1,0)へ再割当て。**shapelyで実際にintersection lengthを計算して検証済み**（下記「幾何検証」参照）
- `test_label_and_designators_are_derived_from_key_and_pads` — label=="C3R5"（計画書の例と同一の文字列を直接ピン）、designatorsは重複除去・ソート済み
- `test_empty_pads_returns_empty_list` — 空入力 → []

`TestCopperPadObserver` / `TestPadAlignmentResult` は無変更。`_pad`ヘルパーに`copper_half`引数を追加（`copper_polygon`を`polygon`と別サイズで設定可能に）。

### tests/pcbasm/posctrl/test_alignment.py — `TestComponentAlignments` → `TestRegionAlignments`、他

- **`TestRegionAlignments`**
  - `test_result_for_returns_result_of_the_pad` — 登録済みpadのlookup
  - `test_unregistered_pad_returns_none` — 未登録pad → `result_for`/`board_correction`ともNone
  - `test_result_for_matches_equal_but_distinct_pad_object` — **同値だが別オブジェクトのPadでも当たる契約のピン**（`pad_copy is not pad`かつ`pad_copy == pad`を明示してから確認）
  - `test_board_correction_is_conjugation_of_machine_transform` — 共役ピン（旧`TestComponentAlignments`から移植、数学は無変更）
  - 旧`corrected_board_transform`のテストは削除（計画書: src内未使用と確認済みのため`RegionAlignments`に存在しない）
- **`TestPadAlignmentSession`**
  - 既存4テスト（`test_align_returns_result_with_translation_matching_known_shift`ほか）は`target`を`ComponentPads(component=..., pads=...)`から`PadRegion(key=(0,0), bounds=(-6,-6,6,6), pads=(pcb.pads[0],))`に差し替えただけで、アサーションは無変更
  - **新規** `test_init_raises_value_error_when_region_size_does_not_fit_field_of_view` — crop由来収容制約違反のValueError。test-fixtureのcrop 600px÷ppm10=60mm領域に対し、calibration.resolutionを(400,400)へ縮小してFOVを40mm四方にし、`60 + 2*(0.5+1.4) = 63.8mm > 40mm`で制約違反を作る（`_calibration()`に`resolution`引数を追加）
- **`TestSortedTopComponentPads`** → **`TestSortedTopPadRegions`**
  - `test_filters_to_top_layer_pads_when_pads_omitted` — pads省略時、pcb.padsのBOTTOM除外
  - `test_uses_given_pads_argument_instead_of_pcb_pads` — pads指定時はpcb.padsでなく引数を使う（TOPフィルタは維持）
  - `test_region_size_is_derived_from_crop_size_and_pixel_per_mm` — crop 600px÷ppm10=60mmの結線。x=1/55mmは同一領域、x=65mmは別領域
  - `test_cyclic_order_is_based_on_machine_coordinates_not_board_coordinates` — **旧実装の board/machine 座標混在バグの回帰テスト**。board_transform=Shift(100,0)、stage=(105,0,z)のとき、board座標だけで比較すると[FAR,NEAR]、機械座標で正しく比較すると[NEAR,FAR]になるよう数値を設計。計画書「現行の board/machine 座標混在も修正」の直接検証

`_component`ヘルパー・`Component`importは削除（region設計はComponentに依存しない）。`pcb`フィクスチャから未使用の`.components`代入を削除。

### tests/webui/jobs/test_board_ops.py

- **`TestPadAlignAbortMessage`** — 既存の境界パラメトリズ（`[]/0`, `[R1]/1`, `[R1,R2]/2` 等）を維持しつつ、ラベルを領域ラベル風文字列（`"C0R0"`等）に変更。`test_exceeding_limit_returns_message_with_counts_and_region_label`で新たに`"領域"`という語がメッセージに含まれることを追加検証（部品→領域の文言変化の最小限のピン、完全一致はしていない）
- **`TestAlignPadRegions`（新設）** — `align_pad_regions`のループ骨格。`JobContext`は`JobManager`だけが構築する契約のため、`register_synthetic`で合成ジョブとして`manager`経由実行し、`PadAlignmentSession`は`mocker.Mock()`（自前クラスのため許容、照合自体の振る舞いはtest_alignment.pyが担保）
  - `test_collects_successful_alignments_in_order` — 2領域とも成功 → `[(region_a, result_a), (region_b, result_b)]`を順序どおり返す
  - `test_calls_on_failure_with_region_and_index_and_excludes_it_from_result` — 2件目失敗 → `on_failure(region_b, 1)`が呼ばれ、戻り値には含まれない
  - `test_raises_value_error_and_stops_further_aligns_when_failures_exceed_max` — 3領域全滅・max_failures=0 → `record.status==FAILED`かつ`session.align.call_count==1`（1件目の失敗で即中止、以降未呼出）
  - `test_completes_with_empty_result_when_every_region_fails_and_max_failures_is_none` — 全滅でもmax_failures=None（board_tourが使用）なら例外なく完走、戻り値`[]`、`call_count==2`（全領域を試行）

### tests/webui/jobs/test_pasting.py / test_posctrl.py

grep確認の結果、`ComponentPads`/`ComponentAlignments`/`sorted_top_component_pads`/`align_component_groups`等のシンボル参照、および「部品」「designator」を含む summary/log 文言のピンは**いずれも存在しなかった**ため、無変更。

## 幾何検証（巨大pad再割当てテストの根拠）

`test_giant_pad_is_reassigned_to_cell_with_max_boundary_intersection`の期待値はshapelyで実際に計算して確認済み（手計算のみに依存していない）:

```
poly = box(-2,-2,12,12)  # 半辺7・中心(5,5)
ring.intersection(cell) の length:
  (-1,0)=10.0  (0,-1)=10.0  (0,1)=10.0  (1,0)=10.0  ← 4way tie（最大）
  (-1,-1)=4.0  (-1,1)=4.0  (1,-1)=4.0  (1,1)=4.0
  (0,0)=0.0
```
タイブレーク(col,row)昇順 → `(-1,0)`が最小。候補セルのドメイン解釈（bbox内/隣接のみ等）に依らず、bbox外は交差長0であるため、この結論は実装の探索範囲の取り方に対して頑健。

## 実装側に求める修正（現時点）

現時点でなし。plan-implementer は本タスクと並列で src/ を実装中（確認時点で `src/pcbasm/config.py` / `posctrl/{pad,alignment,__init__,render}.py` が変更途上）。テストは計画書の凍結IFに基づいて書いたので、実装完了後に `make test-no-hardware` の該当ファイルで答え合わせを行う想定。

## 曖昧だった点（計画書に対して）

- 「巨大padの再割当て候補セルのドメイン」（全グリッド／bbox内／中心セルの隣接のみ、等）が計画書に明記されていない。上記の幾何検証により、どの妥当な解釈でも結果が一致するテストケースを設計することで実質的に解消した
- `pad_align_abort_message`の新文言の正確な語（「失敗領域」「対象領域」等）は計画書に確定した文字列が無いため、"領域"という語の存在のみを緩く検証している（完全一致は避けた）

## 検証結果

- `make format`（`pre-commit run --files <3ファイル>`）: 全hook pass（ruff/ruff-format/docformatterが軽微な整形を自動適用）
- `python -m py_compile` 3ファイルとも成功
- `grep -rn '</content>'`: 3ファイルとも検出なし
- plan-implementer が同一リポジトリで src/ を並列編集中のため、**このセッションでは対象ファイルに対する pytest 実行を意図的に見送った**（WIP中のsrcに対して実行すると不安定な中間状態を拾う恐れがあるため）。合流後に `make test-no-hardware -- tests/pcbasm/posctrl/test_pad.py tests/pcbasm/posctrl/test_alignment.py tests/webui/jobs/test_board_ops.py` 等での答え合わせを推奨
- （解消済み。下記「追加対応」参照）`tests/pcbasm/test_config.py::test_pad_align_section_overrides_defaults`が`PadAlign.min_roi`を直接参照していた件は、orchestratorからの追加依頼で修正済み

## 設計変更対応: region_size を camera.crop 分離 → pad_align.region_size（2026-07-22 再指示後）

ユーザー再指示（orchestrator ノート「設計変更・MR !138 提出後」節）を受け、領域サイズの由来を
`camera.crop.size ÷ calibration.pixel_per_mm` から新設定 `pad_align.region_size`
（mm、既定 10.0、正方形）へ切り替える変更に追従した。凍結 IF は本メッセージ冒頭のもの
（`plan_pad_regions` シグネチャ・収容制約検証は維持、`sorted_top_pad_regions` シグネチャ不変）。
plan-implementer（impl-region-align / impl-region-size）が並列で src/config を実装中、確認時点で
`src/pcbasm/config.py`（`PadAlign.region_size` + 正値検証）・`src/pcbasm/posctrl/alignment.py`
（`_region_size` が config 由来に）・`src/webui/config_store.py`（`region_size` FieldSpec +
正値検証）・`configs/{test-fixture,kurousagi}/machine.toml`（`region_size = 10.0` 明示追加）は
既に反映済みだった。テストは実装完了後の状態と付き合わせて green を確認した（詳細下記）。

### 変更したファイル

1. **`tests/pcbasm/posctrl/test_alignment.py`**
   - モジュール docstring: 「crop÷pixel_per_mm 由来」の記述を「pad_align.region_size 由来」へ更新
   - `TestPadAlignmentSession::test_init_raises_value_error_when_region_size_does_not_fit_field_of_view`
     — 収容制約違反の作り方を再設計。test-fixture の `region_size=10mm` に対し
     `calibration.resolution=(100,100)` へ縮小して視野 10mm 四方にし、
     `10 + 2*(0.5+1.4) = 13.8mm > 10mm` で制約違反を作る（旧: crop 600px÷ppm10=60mm →
     resolution (400,400) で 40mm 四方にして破る、という設計だったものを region_size ベースへ）。
     `pytest.raises(ValueError)` に `match="region_size"` を追加（旧は match 無し）。
     「region_size」は既存の PadAlign/config_store の他フィールド検証テスト
     （`max_failures`, `auto_area_short_side_factor` 等）が例外なく属性名そのものを
     substring 検証に使っている慣例に倣った選択で、実装側メッセージも実際に
     `pad_align.region_size を縮小するか、...` を含んでいることを確認済み
   - `TestSortedTopPadRegions::test_region_size_is_derived_from_crop_size_and_pixel_per_mm`
     → `test_region_size_is_derived_from_pad_align_region_size_config` に改名・再設計。
     pad 間隔を crop 由来 60mm 境界（x=1/55mm 同一・x=65mm 別）から
     region_size=10mm 境界（x=1/5mm 同一・x=15mm 別）へ変更
2. **`tests/pcbasm/test_config.py`**
   - `TestMachine::test_pad_align_section_overrides_defaults` に `region_size = 12.5` の
     override 検証を追加（既存の `tolerance` override 検証と同居させる最小差分）
   - 新設 `TestPadAlignRegionSize`（`TestPadAlignMaxFailures` の後、`TestMachineType` の前）
     — `test_defaults_to_ten_mm`（`PadAlign().region_size == 10.0`）、
     `test_rejects_non_positive_value`（`[0.0, -1.0]` パラメトリズ、
     `match="region_size"`、`auto_area_short_side_factor_rejects_non_positive` と同型）
3. **`tests/webui/test_config_store.py`**
   - 新設 `TestPadAlignRegionSize`（`TestPadAlignMaxFailures` の直後）
     — `test_read_returns_configured_value`（test-fixture の実値 10.0 を読む。
     `region_size` は fixture に明示されているため `max_failures`/`nozzle_cap` と異なり
     「欠落時 None」ではなく実値読み取りのテストにした）、
     `test_write_then_reread_reflects_value`、
     `test_non_positive_value_raises`（`[0.0, -1.0]` パラメトリズ、`UnknownFieldError`）

`tests/pcbasm/posctrl/test_pad.py` は変更なし（`plan_pad_regions(pads, region_size: tuple[float,float])`
はシグネチャ・契約とも無変更で、同ファイルは既に region_size を直接引数で渡す設計だったため
crop/config 由来の結線に依存していなかった。grep で確認済み）。

### 検証結果

- `python -m py_compile` 3ファイルとも成功、`grep '</content>'` 検出なし
- `pre-commit run --files <3ファイル>`: 全hook pass（初回 `docformatter` が軽微な整形を自動適用、
  再実行で pass 確認済み）
- **plan-implementer 側の実装が確認時点で既に完了していたため、今回は pytest 実行まで行った**
  （通常は仕様 first で赤のまま引き継ぐ運用だが、src が先に green になっていたため答え合わせを実施）:
  `tests/pcbasm/test_config.py` + `tests/pcbasm/posctrl/test_alignment.py` +
  `tests/webui/test_config_store.py` → **126 passed**（新規/変更テスト全て pass）。
  加えて `tests/webui/jobs/{test_board_ops,test_pasting,test_posctrl}.py` も回帰なし
  （101 passed, 12 deselected=hardware）
- 実装側への修正要求: **なし**（実装は凍結 IF・メッセージ慣例とも整合していた）

## 追加対応: tests/pcbasm/test_config.py の min_roi 参照修正

orchestratorから、上記の申し送り事項が実は担当範囲内（`tests/`配下）であるとの指摘を受け、追従修正した。

- **変更ファイル**: `/home/gop/pcb-assembly/tests/pcbasm/test_config.py`
- **対象**: `TestMachine.test_pad_align_section_overrides_defaults`（114行目付近）
- **変更内容**:
  - 書き込むTOML断片から`min_roi = 5.0\n`を削除（`tolerance = 0.08\n`のみ残す）
  - `assert pad_align.min_roi == pytest.approx(5.0)`の行を削除
  - テストの意図（pad_alignセクションのoverride検証）は、残る2つのassert（`tolerance`のoverride確認 / `canny_low`のデフォルトフォールバック確認）で維持されている。置き換えとなる新フィールドの追加はしていない（最小限の修正）
- **grep確認**: `grep -rn min_roi tests/pcbasm/test_config.py`は編集後ヒットなし。リポジトリ全体では`tests/pcbasm/posctrl/test_render.py`にのみ`min_roi`の語が残るが、これは`PadResultRenderer`固有の`min_roi_mm`パラメータ（空`roi_polygons`時のフォールバックROIサイズ）であり、`PadAlign.min_roi`とは無関係の別概念（render.py自体にも同名の別パラメータとして存在）。担当範囲外のtest_render.py自体は変更していない
- **検証**: `python -m py_compile`成功、`pre-commit run --files tests/pcbasm/test_config.py`は全hook pass（初回実行でruff-format/codespellが軽微な整形を自動適用、`git diff`で意図した2箇所の変更のみであることを確認済み）、`grep '</content>'`検出なし
- **src/への変更**: なし（引き続き未編集）
