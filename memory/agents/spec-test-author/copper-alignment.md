# copper-alignment（CopperProjector / CopperEdgeMatcher 仕様テスト）

計画書: `memory/agents/implementation-planner/copper-alignment.md` §1/§2/§4

## 書いたテスト一覧

すべて `tests/pcbasm/posctrl/test_copper.py`。合成データ unit（numpy + cv2 + shapely 実物、モックなし、`@mark_hardware` 不要）。

`TestCopperProjector`（計画書 §4 Projector 1〜7 に対応）:

- `test_identity_transform_projects_square_at_expected_pixels` — 正常系。恒等変換 + ppm=10 + 非正方 image_size=(320,200) で、mask shape=(height,width)=(200,320)・center=(width/2,height/2) の規約と fill/edge の pixel 位置をピン
- `test_stage_plus_x_shifts_projection_plus_x_pixels` — **符号ピン（公開 API 契約）**。stage +1mm(x) → 投影 +10px(x)
- `test_rotation_180_offset_transform_projects_point_symmetric` — Rotation(180) で画像中心の点対称（絶対位置も検証）
- `test_mirror_matrix_and_shift_compose_is_reflected_in_projection` — Compose([Matrix2d(diag(-1,1)), Shift(2,1)]) を board_transform に与え、期待 pixel (90,85) を検証
- `test_polygon_hole_is_unfilled_and_inner_ring_edged` — 穴付き polygon: fill の穴=0、edge に内外 2 リング
- `test_out_of_view_polygon_yields_empty_masks` — 視野外 polygon → 全ゼロ（bbox フィルタ経路）
- `test_clipped_polygon_has_no_false_edge_on_frame_border` — エッジケース。はみ出し polygon でフレーム最終列の edge 非ゼロ数 ≤ 4（polylines 方式のピン。fill 輪郭由来なら縦の偽エッジ線 ~60px で fail する）

`TestCopperEdgeMatcher`（計画書 §4 Matcher 1〜7 に対応）:

- `test_match_recovers_known_pixel_shift` — **符号ピン（公開 API 契約）**。観測=(+7,−4)px ずれ → offset.px ≈ (7,−4) ±1（offset = 観測 − 想定）
- `test_identical_masks_match_with_zero_offset` — 完全一致 → (0,0)、mean_distance_px ≈ 0
- `test_match_recovers_shift_with_partially_missing_observed_edges` — 観測下半分欠損でも (5,3) を復元
- `test_match_recovers_shift_despite_noise_edges` — ノイズ 40px 追加でも (6,−2) を復元（rng seed=42）
- `test_empty_observed_or_expected_mask_returns_none` — 異常系。observed 空 / expected 空の両方向で None
- `test_shift_beyond_window_stays_in_window_with_large_distance` — 窓 20px に対し 30px ずれ → |offset| ≤ 21 かつ mean_distance_px > 2.0
- `test_expected_edges_outside_crop_do_not_affect_match` — crop_size=(100,100) 外に逆向き (−8,−6) を示唆する構造を置いても crop 内の (4,2) を返す

## 仕様根拠の対応表

- 投影公式・座標規約（image_center=全画面中心、x右・y下） → 計画書「座標変換チェーン」と §1。Projector テスト 1〜4 の期待値はすべて `pixel = center + ppm·R(s − T_b(b))` の手計算
- fill/edge の uint8 0/255・(height,width) shape → §1 CopperProjection docstring
- 「fill の輪郭から edge を作らない」 → §1 実装注記（フレーム端クリップ線が偽エッジになる）→ Projector テスト 7
- offset 符号 = 観測 − 想定 → §1 EdgeMatch / §2 符号契約 → Matcher テスト 1
- 空マスク None → §1 `match()` 注記 → Matcher テスト 5
- 窓クランプ・mean_distance 品質指標 → §2 手順 1〜3 → Matcher テスト 6

## 期待される失敗（仕様 first の場合）

なし。plan-implementer の `src/pcbasm/posctrl/copper.py` が既に同一ツリーに存在し、**14/14 pass** を確認済み（2026-06-11、`uv run pytest tests/pcbasm/posctrl/test_copper.py` → 14 passed）。

## 実装側に求める修正

現時点でなし。テストはすべて計画書の公式から独立に導出した期待値であり、実装と一致した。

## tests/helpers.py への追加

なし（HAL 不要の純粋計算のため fake 不要。cv2/shapely/numpy は実物使用、モックゼロ）。

## IF 解釈で迷った点（親への申し送り）

1. **crop_size=None 時の探索可否**: テンプレート=「crop 中心領域」だが crop=None で全画面だと探索余地がなくなる。仕様（match が offset を返す）からデフォルト引数のまま既知シフトを復元できることを要求するテストにした（Matcher 1〜6 は crop なし）。実装はこれを満たしている
2. **§4 Projector 4「反転 Matrix2d + Shift の Compose」の適用先**が board/offset どちらか不明瞭 → board_transform 側に適用（A3 の board キャリブレーション結果が Compose になる実態に合わせた）。offset 側の回転は テスト 3 で別途カバー
3. **`@pytest.mark.api_contract`** は `--strict-markers` 下で未登録のため付与せず（`tests/pcbasm/pasting/test_fill_path.py` の先例に従い、docstring で契約ピンを明記）

## 検証結果

- make format: pass（全 hook Passed、ファイル無修正）
- uv run pytest tests/pcbasm/posctrl/test_copper.py: **14 passed**（合流前の参考値。最終確認は親の `make test-no-hardware`）
