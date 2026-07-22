# region-pad-align レビュー

対象: `git diff main`（feature/20260722/region-pad-align、コミット済み crop 300px 変更 + 未コミット全変更）
突き合わせ: memory/agents/orchestrator/region-pad-align-plan.md（設計（確定）/ 凍結 IF / ファイル別計画）

## verdict: approve

must-fix なし。仕様準拠・幾何・テスト・規約とも計画書と一致していることを確認した。
should-fix 1 件（非正方形 crop の収容制約式）は現行 configs（全て正方形）では動作に影響せず、
実行時バックストップもあるためマージを阻害しない。

## must-fix

なし。

## should-fix

1. **`src/pcbasm/posctrl/alignment.py:129-163` `_validate_region_fits_frame` — 非正方形 crop への「一般化」が数学的に不正**
   - 問題: 回転矩形 (w×h, θ) の bbox は 幅 `w|cosθ|+h|sinθ|` / 高さ `w|sinθ|+h|cosθ|`。実装の per-axis
     `region_size[i]×ρ`（ρ=|cosθ|+|sinθ|）は w=h のときのみ一致する。
   - 根拠（数値検証済み）: (w,h,θ)=(60,30,30°) で実要求幅 66.96mm に対し実装は 81.96mm を要求
     （**過剰拒否** = 収まる設定を構築時 ValueError で拒否。align 側バックストップでは救えない）。
     高さは実要求 55.98mm に対し 40.98mm しか検証しない（**過小検証** = 構築を通過し
     align() 実行時 ValueError までジョブ中盤に検出が遅延）。
   - 影響範囲: `[camera.crop]` は width/height 独立キーのため非正方形はユーザー到達可能な設定。
     ただし現行 configs（kurousagi 300×300 / test-fixture 600×600）は正方形で数値的に計画書の
     単一スカラー式と同一、かつ過小側は `PadAligner._validate_roi_fits_frame` が実行時に捕捉する
     ため「静かな精度劣化」には至らない。実装ノートの「正方形前提を崩さない拡張」という主張は
     拡張としては誤りで、正方形限定なら正しい。
   - 修正案: `required_width = w*|cosθ| + h*|sinθ|`, `required_height = w*|sinθ| + h*|cosθ|`。
     code-simplifier で対応可能（式 2 行 + docstring）。

## nit

1. **`PadAligner.align` の凍結 IF「ROI がフレーム(search_window inset)に収まらなければ ValueError」の直接テストがない。**
   Session `__init__` の解析的検証（`test_init_raises_value_error_when_region_size_does_not_fit_field_of_view`）
   のみテストされている。計画書テスト節のリストには align 側 ValueError は含まれていないため計画準拠ではある（任意）。
2. **`src/pcbasm/posctrl/render.py` の最小 ROI が実機で 1.0mm → 3.0mm に変わる（表示のみ）。**
   旧実装は `pad_align.min_roi`（configs 実値 1.0）を `roi_of(min_size_mm=)` と `_centered_roi` に渡していた。
   新実装は `roi_of` のデフォルト 3.0 / `_DEFAULT_MIN_ROI_MM = 3.0` に依存する。実装ノートの
   「旧 `PadAlign.min_roi` の既定値と同値で挙動を保った」はクラス既定値 3.0 に対しては正しいが、
   configs 実値 1.0 に対しては不正確（board_tour の per-pad overlay ROI が最小 3mm になる）。
   overlay 表示のみの変化で照合には無関係。同様に `PadAligner.align` の `roi_of` 呼び出しも
   `min_size_mm` 未指定でデフォルト 3.0 が暗黙に残るが、領域は crop÷ppm ≈ 9.9mm/60mm ≥ 3mm
   のため dormant（crop < 3mm×ppm の設定でのみ「ROI = 領域矩形 + roi_margin」契約から静かに逸脱）。
   気にするなら `min_size_mm=0.0` の明示渡しで copper.py 無変更のまま契約どおりにできる。
3. **`src/pcbasm/posctrl/README.md`** に `ComponentAlignments` / `sorted_top_component_pads` の記載が残る。
   計画書どおり docs-keeper 担当（実装ノートにも申し送り済み）。
4. **リポジトリ直下の未追跡ファイル `"\0014\253\006@W@8"`**（diff 外・レビュー対象外）。
   コミット前に削除を推奨。

## 仕様準拠の確認結果（計画書「設計（確定）」との突き合わせ）

- board 原点固定グリッド + pad.center floor 割当: `_assign_cell`（math.floor は負座標も正しい）。原点固定はテストでピン済み ✓
- 巨大 pad 再割当て: exterior∩セル矩形の交差長最大 + (col,row) 昇順タイブレーク。実装は col-major 走査 +
  strict `>` 保持で昇順タイブレークと等価。テストは shapely 実計算で裏取りされた 4 方向タイ（10.0mm）で
  (-1,0) 選択を検証しており妥当 ✓
- 領域サイズ = `machine.camera.crop.size ÷ calibration.pixel_per_mm`: `_region_size` ✓（テストで 600px÷10=60mm 結線をピン）
- アンカー = `board_transform.apply(region.center)`、ROI = `roi_of([target.box], margin_mm=roi_margin)`、
  投影アンカー固定は現行踏襲 ✓
- 収容制約 ValueError（構築時）+ align 内 ROI 非クランプ検証（実行時、二重チェック）✓。
  従来の「静かなクランプ」は排除された（roi_of がクランプしても直後の inset 検証で必ず ValueError になる）✓
- `RegionAlignments.result_for/board_correction` は Pad の attrs 同値比較（`pad in region.pads`）。
  同値別オブジェクトの契約テストあり。共役 C = T_b⁻¹∘M∘T_b は移植（Compose 順序は corrected_projector と整合）✓
- max_failures = 失敗領域数（既定 0 維持、境界 <= 許容、None 無制限）✓、abort 文言は領域ラベル列挙 ✓
- board_tour 統一（全 TOP pad、on_failure overlay = label + designator 先頭 3 件 + FAILED）✓
- paste_solder 対象 = 有効 pad ∪ 初回パージ pad（`initial_purge.pad not in align_pads` の同値判定で重複回避、
  失敗領域の pad は None → 無補正フォールバック + 警告ログ、`_initial_purge_point` も pad lookup）✓
- rename 最小限・旧シンボル削除: src/tests に残骸なし（README のみ、nit 3）✓
- 巡回順の機械座標化: `sort_by_nearest(key=board_transform.apply(region.center).to3d())`。
  旧 board/machine 混在バグの回帰テストあり（Shift(100,0) で逆順になる設計）✓
- configs: min_roi 行削除（test-fixture/kurousagi。data/testing/machine.toml は pad_align 節自体なし）、
  `[camera.crop]` は kurousagi のみ 300px（コミット済み・計画の実機前提 9.9mm と一致）✓

## 計画外判断 3 件の裁定

1. `PadAligner.__init__` への `image_size` / `search_window_px` 追加: **妥当**。
   凍結は align() シグネチャのみで __init__ は対象外。CopperProjector/Matcher「無変更」を守りつつ
   ROI 検証に必要な値を Session から明示注入する形は正しい。`round(search_window * ppm)` は
   `CopperEdgeMatcher._window_px = round(search_window_mm * pixel_per_mm)`（copper.py:316）と同一式で一貫 ✓
2. render.py の `_DEFAULT_MIN_ROI_MM` 定数化: **妥当**（min_roi 削除に伴うクラッシュ回避、表示専用）。
   ただし configs 実値 1.0 からの表示変化あり（nit 2 に記録）。
3. config_store.py の max_failures ラベル「許容部品数」→「許容領域数」: **妥当**。UI 文言の意味追従で要求からトレース可能。

## テスト品質

- 計画書テスト観点リスト（test_pad.py 7 項目 / test_alignment.py の RegionAlignments・Session・SortedTopPadRegions /
  test_board_ops.py の abort 境界 + AlignPadRegions 4 件）は全て網羅 ✓
- 公開契約のみ検証（private 直接テストなし）。モックは自前クラス（PadAlignmentSession / HAL / pcb）に限定、
  3rd-party 表面のモックなし。クラス集約・parametrize・substring 検証（abort message 完全一致禁止）準拠 ✓
- test_config.py の min_roi 参照は削除済み、test_render.py は影響なし（全テストグリーンで確認）✓

## WebUI thin-wrapper

領域分割・lookup・収容制約は pcbasm（pad.py/alignment.py）に集約。jobs 層はループ骨格
（progress/checkpoint/log）と表示整形（`_truncated_designators`）のみで、JS 変更なし。漏れなし ✓

## 検証結果

- make format: pass（自動整形による差分なし）
- make type: pass（pyright 0 errors）
- make test-no-hardware: pass（1572 passed / 85 deselected, 47s）
- `grep -rn '</content>' src tests configs`: 検出なし（サブエージェント Write 事故なし）
- make test / @mark_hardware: 実行していない（実機はユーザー）
- make test-e2e: 未実行（本レビューの権限外。計画書の検証項目に含まれるため合流後に orchestrator 判断。
  なお tests/e2e は paste_solder の pad editor UI のみで PadAlignmentSession は構築しないことを確認済み）
