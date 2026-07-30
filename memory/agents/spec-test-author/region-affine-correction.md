# region-affine-correction のテスト（spec-test-author）

ブランチ: `feature/20260729/region-alignment-average`。計画書
`memory/agents/implementation-planner/region-affine-correction.md` +
orchestrator 裁定（`region_size_px=300` / `min_regions=4` / 収束しない区は採用して警告）を仕様として
`tests/` のみ書き換えた。`src/` は触っていない。

## 書いたテスト

| ファイル | 内容 |
|---|---|
| `tests/pcbasm/posctrl/test_alignment.py` | 全面書き換え。`fit_displacement`（アフィン復元・純並進への退化・共線/準共線/2 区以下の並進フォールバック・引数検証）、`BoardAlignment`（fit 委譲・残差 0 / 非線形で有意・RMS/最大の定義・空で ValueError）、`RegionAlignmentSession`（`plan_regions(pad_centers)`・pad なしで空・`board_edge_margin` のピン・2 パス計測・失敗の握りつぶし・frame_sink はパスごと 1 枚） |
| `tests/pcbasm/posctrl/test_region.py` | 全面書き換え。タイル張り（重なりなし・上限なし・位相 = pad 重心・anchor 写像・ROI 共通・回転で区数が崩れない・巡回順）、pad なし区スキップ、sharpness 閾値、外周マージン、予測 vs 実測 sharpness、引数検証 |
| `tests/pcbasm/posctrl/test_aligner.py` | 全面書き換え。1 パス収束の早期打ち切り、**2 パスの二重計上防止**、`max_passes=1`、未収束でも採用、`converge_tolerance` の早期打ち切り、`increment`、照合失敗（1/2 パス目）、`max_correction` は累積判定、anchor 非依存、frame_sink、`to_machine_transform` の純並進と合成 = 和 |
| `tests/pcbasm/posctrl/test_copper.py` | 追加のみ（`TestCopperProjectorWithCorrection`）。合成順序・`Shift` 補正の px 効果・polygons/ppm/image_size の継承・元投影器の不変性。既存の照合テストは無変更（45° 交差項のピンは既にあった） |
| `tests/pcbasm/test_config.py` | 既定値を 300 / 4 / 2 / 0.01 へ。`region_count` 撤去、`converge_tolerance <= 0` の拒否を追加 |
| `tests/webui/test_config_store.py` | `region_count` → `max_passes`（int・1 未満拒否）と `converge_tolerance`（float・mm・0 以下拒否） |
| `tests/webui/routers/test_settings_api.py` | `TestPadAlignRegionCountApi` → `TestPadAlignMaxPassesApi`（+ converge_tolerance の 400） |
| `tests/e2e/test_webui_e2e.py` | 実 HTTP 往復を `max_passes` へ。キー一覧に `converge_tolerance` を追加し `region_count` の不在をアサート |
| `tests/webui/jobs/test_board_ops.py` | `RegionAlignment` の新 IF へ追随。log の `passes` / 補正モデル / スケール ppm / スキュー / 残差 RMS・最大 / 区ごとの残差、`translation` 縮退の警告、未収束の警告、中止メッセージに `region_count` を含まないこと |

`tests/pcbasm/posctrl/test_correction.py` は変更していない（実機検証済みの符号規約）。
`mocker.Mock` は使わず、klipper / stage / pcb はすべて手書き stub（`_StubKlipper` / `_StubStage` /
`_StubPcb`）。カメラは `FakeCamera`、投影・照合・Canny は実物。

## 検証結果

- `make format` / `make type`（0 errors）/ `make test-no-hardware`（**1716 passed**）/ `make test-e2e`（51 passed）すべて緑
- `grep -rn '</content>' src tests` なし。`region_count` の残りは負のアサートとコメントのみ
- 実機テスト（`make test` / `@mark_hardware`）は実行していない

## mutation testing（テストがバグを捕まえることの確認）

| 壊した箇所 | 結果 |
|---|---|
| `aligner.measure` の投影補正を撤去（`projector = self._projector` 固定 = 二重計上） | **10 件 fail**（`test_second_pass_does_not_double_count_the_displacement` ほか、session の 2 パステストも） |
| `aligner.measure` の投影補正を `Shift(cumulative)` → `Shift(increment)`（3 パス目の二重計上） | `test_third_pass_corrects_the_projection_by_the_cumulative_not_the_increment` が fail（累積 0.7mm のはずが 1.3mm） |
| `fit_displacement` の `spread < _MIN_ANCHOR_SPREAD_MM` を撤去 | 共線・準共線の 2 件が `'affine' == 'translation'` で fail |
| `region.py` の pad 包含条件を撤去 | `test_tiles_without_a_pad_center_are_skipped` が `3 == 2` で fail |
| `region.py` のタイル位相 `pad_px.mean(axis=0)` → `min(axis=0)` | `test_tile_phase_is_aligned_to_the_pad_centroid` が `0 == 1` で fail |
| 同 → `max(axis=0)` | 同テストが `0 == 1` で fail |

（実行はいずれも一時的な編集で、確認後に `src/` を byte-identical に復元済み）

補足: `test_single_pad_yields_only_the_tile_containing_it` は pad 包含条件を壊しても通る
（pad 1 個だと走査範囲 `k_lo..k_hi` がそのタイル 1 枚に潰れるため）。pad 条件のピンは
複数 pad 版の `test_tiles_without_a_pad_center_are_skipped` が担っている。orchestrator 裁定で
現状維持（レビュアーも同種の穴はこれ 1 件だけと確認済み）。

## code-reviewer should-fix への対応（2 件、orchestrator 採用分）

1. **`test_tile_phase_is_aligned_to_the_pad_centroid` が空振り** → pad を x = 0 / 9 / 11 の
   非対称配置にし、`safe_area` を重心 (20/3, 2.0) 中心の半径 9mm の円に置いた。位相が
   min（格子 0 / 10mm）や max（1 / 11mm）へずれるとどのタイルも ROI の隅が円外に出て
   領域 0 個になるので、1 件で弁別できる。上表のとおり min / max 両方のミューテーションで落ちる
2. **`max_passes >= 3` の経路が未テスト** → `test_third_pass_corrects_the_projection_by_the_cumulative_not_the_increment`
   を追加（観測列 −6px → −1px → 一致、`max_passes=3`）。2 パスまでは累積 = 増分なので
   この穴は 3 パス目でしか露出しない。`max_correction_mm=None` にして「累積 0.7mm か
   二重計上の 1.3mm か」で弁別する（上限判定の副作用で落ちるのではなく、値そのもので落とす）。
   `max_passes` の上限検証が無く WebUI から 3 以上が到達し得ることを docstring に記載

## 実装側への申し送り（いずれも既に整合済み）

1. `RegionAlignment.increment: Point2d` が計画書の IF に無いまま追加されている。orchestrator 裁定 3
   「最終パスの増分が `RegionAlignment` に載る」の実現なのでテスト側で受け入れ、
   「最終パスの値であること」「1 パスなら累積と一致すること」をピンした。計画書の IF 表は未更新
2. 合成画像のフィクスチャに系統バイアスがあった（`cv2.rectangle` の終点 inclusive で
   塗り domain が 1px 広く、Canny 後の観測が設計 polyline に対し −0.47px ずれる）。
   終点を +1px 伸ばして 0.02px（2um）に落とした。実装の欠陥ではない
3. 期待値の算術ミス 2 件をテスト側で修正した:
   `anchor_spread_mm` は `σ_min(P_centered)/sqrt(n)` なので x と相関する ±1mm ジッタでは
   1.0 ではなく 0.956（`< 2.0` の範囲アサートへ）。タイル位相が pad 重心固定なので
   「margin=0 なら外形に接する区が出る」は格子位相の偶然 → pad 重心が (25.5, 15.5) になる
   地形を選んで ROI 端が外形から 0.5mm に来るようにし、margin 3 で落ちることをピンした

## 期待される失敗（現時点では無し）

`src` 実装が計画書どおりに揃ったため赤は残っていない。今後 IF を変える場合は、
上記 mutation の 3 点（二重計上・共線縮退・pad なし区）を落とさないこと。
