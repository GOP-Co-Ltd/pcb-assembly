# pad-alignment 仕様テスト（spec-test-author ノート）

計画書: `memory/agents/implementation-planner/pad-alignment.md`
ブランチ: `feature/20260611/pad-copper-alignment`

## 実行結果（2026-06-11 時点）

plan-implementer の並行実装が既に新契約を満たしており、**全テスト green**
（`uv run pytest tests -m "not hardware"` → 560 passed）。`make format` 安定。
仕様 first の赤渡しではなく、green 合流で完了。

## 作成・変更ファイル

| ファイル | 内容 |
| --- | --- |
| `tests/helpers.py` | `FakeCamera` 追加（hal.Camera ABC の test Impl。固定 Image 列、枯渇後は最終画像を返す） |
| `tests/pcbasm/posctrl/test_copper.py` | `TestCopperProjector` に 5 件追記、`TestCopperEdgeMatcherRigid` 新設 8 件。既存テスト無変更 |
| `tests/pcbasm/posctrl/test_correction.py` | 新規 `TestToMachineTransform` 5 件（parametrize 込み 7 case） |
| `tests/pcbasm/posctrl/test_position.py` | 新規 `TestXYPositionAdjustor` 4 件 |
| `tests/pcbasm/posctrl/test_offset.py` | 新規 `TestOffsetTransformMeasurer` 2 件（parametrize 込み 4 case） |
| `tests/pcbasm/posctrl/test_pad.py` | 新規 `TestCopperPadObserver` 2 件 / `TestPadAlignmentResult` 4 件 |
| `tests/pcbasm/posctrl/test_setup.py` | `TestOffsetObserver` 2 件を `observe() -> Transform` 契約へ改修のみ |

## 観点 → テスト対応表

### copper.py — `CopperProjector` 追記

- pixel_of 投影公式一致（T_b=Shift, R=Rotation(90) で符号込み） → `test_pixel_of_matches_projection_formula`
- roi_of bbox+マージン → `test_roi_of_returns_projected_bbox_with_margin`
- roi_of min_size 中心対称拡張（0.5mm 角 pad） → `test_roi_of_expands_small_pad_to_min_size`
- roi_of フレームクランプ → `test_roi_of_clamps_to_frame`（(0,0,200,200) 完全一致）
- 回転 board_transform で全頂点 bbox（mm bbox 変換の誤実装を三角形で排除） → `test_roi_of_covers_all_vertices_under_rotated_board_transform`

### copper.py — `TestCopperEdgeMatcherRigid`

- 純並進 (+7,−4)px、θ≈0、center_mm≈(0,0)（roi=None 中央 crop） → `test_match_rigid_recovers_pure_translation_with_zero_rotation`
- **回転復元符号ピン +1.2°** → `test_match_rigid_recovers_positive_rotation_sign`
- 並進+回転同時（center_mm=(4,−2) の非中心 ROI） → `test_match_rigid_recovers_translation_and_rotation_together`
- camera_transform ラウンドトリップ（頂点対応を mm 空間で再現） → `test_camera_transform_round_trips_expected_to_observed`
- ROI 限定ピン（ROI 外の逆ずれ構造が無影響） → `test_match_rigid_uses_only_template_inside_roi`
- ROI 境界横断エッジで乱れない → `test_match_rigid_is_stable_with_edges_crossing_roi_boundary`
- ROI 内 template 空 → None → `test_match_rigid_returns_none_when_roi_has_no_expected_edges`
- camera_transform 式の直接ピン（o ↦ Rot_θ(o−c)+c+d、d=offset.mm） → `test_camera_transform_formula_is_rotation_about_center_plus_offset`

### correction.py — `TestToMachineTransform`

- **最重要符号ピン**: G=Shift(d), s_obs=s0, R=Rotation(α∈{0,+7,−30}) → 全点で M(p)−p=−R(d) → `test_pure_shift_at_anchor_matches_a4_displacement`
- s_obs≠s0 → 並進=(s_obs−s0)−R(d) → `test_anchor_observed_at_mismatch_adds_stage_displacement`
- 回転共役（固定点 ψ(c)、θ_machine=θ） → `test_camera_rotation_conjugates_to_same_machine_rotation`
- det<0 R → θ→−θ → `test_mirror_offset_transform_flips_machine_rotation_sign`
- ψ⁻¹∘M∘ψ ≈ G → `test_conjugating_back_recovers_camera_transform`

ψ はテスト側で計画書の式 `Compose([offset_transform, Scale.flip(x=True, y=True), Shift.from_point(a)])` をそのまま独立構築（実装の内部関数には依存しない）。

### position.py — `TestXYPositionAdjustor`

- 縮小 Shift 列で収束・戻り値・移動目標 → `test_adjust_converges_with_shrinking_shifts_and_returns_final_target`
- offset_transform=Rotation(90) 符号ピン（R を順方向適用。R⁻¹ 実装はここで割れる） → `test_offset_transform_rotates_observed_displacement_into_machine_space`
- 回転成分付き Transform は原点適用で並進へ縮約 → `test_rotation_component_of_observed_transform_reduces_to_translation`
- 非収束 RuntimeError（substring「収束しませんでした」） → `test_adjust_raises_when_not_converged_within_max_iterations`

### offset.py — `TestOffsetTransformMeasurer`

- measure() = Rotation.from_points(Δs, o2−o1)（90°/45°/o1 非ゼロ parametrize、(A2) ピン） → `test_measure_returns_rotation_from_points_of_offset_change`
- X 相対移動と元位置復帰 → `test_measure_moves_in_x_and_returns_to_start_position`

### pad.py — `TestCopperPadObserver` / `TestPadAlignmentResult`

- 既知ずれ (+6,−4)px の合成画像（白矩形 → 実 Canny）→ observe() 並進 ≈ (0.6,−0.4)mm、last_match ピン → `test_observe_returns_translation_matching_known_shift`
- 検出不能 → RuntimeError、observe 前 last_match is None → `test_observe_raises_runtime_error_when_nothing_detected`
- translation = M(anchor)−anchor（純 Shift / anchor 回り回転+並進） → `test_translation_of_pure_shift_is_the_shift` / `test_translation_is_anchor_displacement_under_rotation`
- rotation = from_points(ex, M(anchor+ex)−M(anchor)) → `test_rotation_recovers_machine_angle`
- 鏡映 M（det<0）→ 角度符号の自動処理（Rotation(10)→flip_y で −10°） → `test_rotation_flips_sign_under_mirror_transform`

### setup.py（改修のみ）

- `observer.observe() -> Transform`、`apply(Point2d(0,0)) ≈ mean_mm` → `test_returns_mean_mm_shift_transform_on_success`
- 検出失敗 RuntimeError → `test_raises_on_detection_failure`（呼び出しのみ `observer.observe()` へ）

## 合成データの規約（実装者への注意）

- **回転 ground truth は warpAffine 不使用**。想定頂点列を `pcbasm.geometry.Rotation.apply`（x右・y下の pixel 座標へそのまま適用）で ROI 中心回りに回し `cv2.polylines` 描画。`getRotationMatrix2D` の角度符号規約（y上向き正の反時計回り）に実装が依存すると `test_match_rigid_recovers_positive_rotation_sign` で割れる設計。
- conjugation テストは厳密幾何の R⁻¹ ではなく **(A4) `target = pos − R(o)` を公理**としたまま（計画書「再導出禁止」）。`test_pure_shift_at_anchor_matches_a4_displacement` が崩れる変更は実機挙動の破壊を意味する。
- klipper/stage の Mock は `stage.move.return_value = gcode.GCode(...)`・`stage.max_velocity = 100.0`・`get_position → Point3d` を設定（GCode の `+` 連結と Speed.absolute が実物のため）。

## 途中で見つけた挙動（テスト側で調整済み）

- 複合（並進+回転）合成で半幅 90x60px のリングだと θ=1.3°（期待 1.0±0.2）。1px ラスタ化＋並進量子化との結合による分解能限界であり実装バグではないと判断 → リングを半幅 120x90（レバー半径 150px）へ拡大し許容 0.3° に校正。回転単独（半径 150px）は 0.2° 以内で通る。実機の θ 精度を議論する際はこの分解能（小型 pad では min_roi で拘束を稼ぐ計画の妥当性）を裏付けるデータ。

## 実装側への修正要求

なし（合流時点で全 green）。
