# paste-dataset-dot-core（MR1 コア層の仕様テスト）

計画書: `/home/gop/.claude/plans/docs-image-based-dispense-calibration-m-generic-frog.md`
（「MR1」節 +「設計レビュー反映（確定・上記より優先）」節。後者が正）

ブランチ: `refactor/2026-09-08/paste-dataset-dot-core`

## 書いたテスト一覧

| ファイル | クラス | 観点 |
| --- | --- | --- |
| `tests/pcbasm/pasting/dataset/test_plan.py`（新規） | `TestDotGridSpec` | validate の None 返却／派生量（`sample_count` / `target_count` / `volumes_ul`）／`crop_size_mm <= pitch` 境界 |
| | `TestPlanDotGridLayout` | 有効領域・パージ位置・容量・パージ除外・行優先採番・ピッチ |
| | `TestPlanDotGridVolumeAssignment` | multiset 一致・決定性・`volume_index`・`order` |
| | `TestPlanDotGridBlankCells` | blank 数・量を持たないこと・採番の共有・シャッフル混在・seed 追従 |
| | `TestPlanDotGridRejections` | 不正 spec・容量不足（blank を含む） |
| | `TestPlanViews` | 角度・n=0・半径不正・負の count |
| | `TestValidateCropInFrame` | ちょうど収まる／1 px 外れる／短辺が効く／`pixel_per_mm` 不正 |
| | `TestValidateCaptureReach` | 可動域内／view offset で外れる／blank も検査対象 |
| | `TestValidateMinRotations` | 下限そのもの／わずかに下回る／`rotations_per_ul` 不正 |
| `tests/pcbasm/pasting/dataset/test_metadata.py`（書き換え） | `TestDatasetView` | view 番号・offset の検証 |
| | `TestAllocateVolumeByRotations` | purge を含む比例配分（既存を維持） |
| | `TestMetadataOnDiskShape` | v2 の全キー集合／`plate` / `label` / `blanks` / `capture_order` の on-disk ピン |
| | `TestParseMetadataV2` | strict 型・未知キー拒否・`label.kind` / `capture_order` の literal・v1 doc 拒否 |
| `tests/pcbasm/pasting/dataset/test_writer.py`（書き換え） | `TestPasteDatasetWriterOpen` | `plate_name` / `crop_size_px` / 採番衝突 |
| | `TestPasteDatasetWriterCaptures` | `pre/` `post/` のみ・mask 無し・lossless・`(n, n, 3)` uint8 強制・session 寸法との一致強制 |
| | `TestPasteDatasetWriterFinalize` | atomic 確定・blank capture も必須・index 衝突・命名規則 |
| | `TestPasteDatasetWriterContextManager` | incomplete 保持 |
| `tests/pcbasm/pasting/dataset/test_recorder.py`（書き換え） | `TestValidateDatasetRun` | purge 量・paste_id・塗布高さ |
| | `TestPasteDatasetRecorder` | interleave 記録 → finalize、v2 キー集合、配分合計、plan からの写し、`pixel_rect` 不一致検出 |
| | `TestBlankCells` | `blanks[]` への振り分け・厳密 0.0・配分の分母に入らない・pre/post が残る |
| | `TestDispenseOrder` | `order` が 1..N・blank は order を消費しない |
| | `TestDotCellIsTheRecordingKey` | PNG 名が target index |
| `tests/pcbasm/pasting/dataset/test_capture.py`（書き換え） | `TestDatasetCapturer` | 送信 G-code が view offset を反映・全 view / 全セルで同寸法 crop・blank も同じ経路・frame 外は理由文 |
| `tests/pcbasm/vision/test_crop.py`（書き換え） | `TestCropPixelSize` | 整数化・奇数寄せ・不正入力 |
| | `TestCropCentered` | 寸法厳守・中心 rounding・frame 外・affine 不正 |

削除:

- `tests/pcbasm/pasting/test_workflow.py::TestPlanDatasetTargets`（`plan_dataset_targets` 廃止）
- `tests/pcbasm/pasting/test_initial_purge.py::TestResolveDatasetInitialPurge`
    （`resolve_dataset_initial_purge` / `DATASET_PURGE_PAD_ID` 廃止）
- `tests/pcbasm/vision/test_crop.py` の `crop_polygon` / `PolygonCrop` / `validate_crop_margins` 分

## fixture

- `data/testing/schemas/paste_dataset_metadata_v2.json`（新規）— v2 の on-disk 形状ピン。
    sample index 1（量点、view 0/1）と blank index 2 を 1 件ずつ持ち、writer / recorder の
    テストがそのまま参照できる形にしてある
- `data/testing/schemas/paste_dataset_metadata_v1.json` は**削除しない**
    （レビュー反映節 must-fix 9。v1 doc 拒否テストの入力として使う）

## 仕様根拠の対応表（主要契約）

| テスト | 計画書の根拠 |
| --- | --- |
| `test_assigned_volumes_are_the_sweep_repeated_per_sample_count` | MR1「量の割り当て: `volumes_ul` を `samples_per_volume` 回繰り返した列を shuffle」 |
| `test_same_seed_reproduces_the_same_placement` / `test_different_seed_changes_the_placement` | MR1「Python の `random` は決定論的なので seed が同じなら同じ配置」 |
| `test_no_remaining_target_touches_the_expanded_purge_cell`（パージ 1.0〜5.5 mm） | MR1「パージセルを `cell_gap_mm` 分広げた矩形と交差する格子セルは除外（パージ寸法がセル寸法と違っても一般に成り立つ規則）」 |
| `test_capacity_shortfall_reports_cell_and_target_counts` | MR1「`capacity < sample_count` は『セル N 個に対しサンプル M 個が必要』の理由文」 |
| `test_crop_size_may_equal_the_cell_pitch` / `test_crop_size_above_the_cell_pitch_is_rejected` | レビュー反映「`crop_size_mm <= cell_size_mm + cell_gap_mm` を validate で強制」 |
| `TestPlanDotGridBlankCells` 全件 | レビュー反映「blank_count 既定 4 …`blanks[]` へ `measured_volume_ul = 0.0`」 |
| `TestDispenseOrder` | レビュー反映「`samples[].order`（塗布実行順）を記録」 |
| `test_config_records_the_interleaved_capture_order` / `capture_order` literal | レビュー反映「撮影順序は点ごとの interleave 固定。`config.capture_order = "interleaved"`」 |
| `test_size_is_odd_so_a_single_center_pixel_exists` | レビュー反映 must-fix 4「奇数へ寄せる（中心 pixel が 1 つ存在する）」 |
| `test_rejects_non_square_crop` / `test_rejects_a_crop_whose_size_differs_from_the_session_size` | レビュー反映 must-fix 4「`writer` は `(n, n, 3)` の uint8 を強制（全画像同一寸法の永続化境界）」 |
| `test_finalize_records_plate_and_dot_grid_config` の `prime_extra_delay_s == 0.0` | レビュー反映 must-fix 5「`prime_extra_delay` は 0.0 に固定し `config.prime_extra_delay_s` に記録」 |
| 同 `plate.height_plane_z_mm` | must-fix 6 |
| `TestValidateCropInFrame` / `TestValidateCaptureReach` / `TestValidateMinRotations` | must-fix 7「装置を動かす前の検証を増やす」（3 項目すべて） |
| `test_rejects_the_real_v1_document_without_migrating_it` | must-fix 9 + MR1「`parse_metadata` は `schema_version != 2` を拒否（v1 の移行関数は用意しない）」 |
| `test_exactly_fitting_offset_is_accepted` / `test_one_pixel_beyond_the_frame_is_rejected` | must-fix 7 の式 `max(|dx|,|dy|) * pixel_per_mm + crop_size_px/2 + 1 <= min(resolution)/2` |

## 期待される失敗

**なし。全 274 件 green。**

`plan-implementer` が並列で同じ作業ツリーの `src/` を実装し、テスト記述中に
`plan.py` / `crop.py` / `metadata.py` / `writer.py` / `recorder.py` / `capture.py` が
そろったため、仕様 first で書いたテストがそのまま通った。実装側が計画書に違反している
箇所は見つかっていない。

```
uv run pytest -m "not hardware" tests/pcbasm/pasting/dataset \
  tests/pcbasm/vision/test_crop.py tests/pcbasm/pasting/test_workflow.py \
  tests/pcbasm/pasting/test_initial_purge.py
→ 274 passed
make format → pass
make type   → 0 errors
```

## 実装側に求める修正

なし。ただし下記「裁定が必要な差分」を orchestrator に確認してほしい。

## 裁定が必要な差分（計画書の文言 vs 実装）

1. **blank の識別方法。** coordinator の指示は「blank は `volume_index = -1` /
    `amount_ul = 0.0` で識別できる」だったが、実装は `DotCell` / `DotBlank` の
    **別型** + `plan.cells` / `plan.blanks` / `plan.targets` で表現している。
    sentinel 値を `volume_index`（docstring 上は `0..divisions-1`）へ押し込まないぶん
    実装のほうが素直で、coordinator が挙げた振る舞い契約（総点数・識別可能性・
    シャッフル混在・finalize の振り分け・厳密 0.0）はすべて満たす。
    **テストは実装の別型構造に対して書いた。** 文言どおりの sentinel を契約にしたい場合は
    テストと実装の両方を戻す必要がある
2. **総点数の property 名。** 指示は「`plan` の総点数 = divisions × samples + blank_count」。
    実装は `sample_count`（塗布点のみ）と `target_count`（塗布 + blank）に分けている。
    テストは `target_count` を使っている
3. **`samples[].order` の決まり方。** 実装は `DotCell.order` として **plan 時に**
    採番（cell index 昇順）し、recorder はそれを写すだけ。interleave 固定なので
    計画順 = 実行順で一致するが、「record_execution の呼び出し順」を order の定義に
    したい場合は別実装になる。テストは「plan の order と一致し、blank は order を
    消費しない」という形で固めた
4. **エラー文の語彙。** `DotGridSpec.validate` / `validate_dataset_run` は
    フィールド名ではなく UI ラベル（「銅板の幅」「塗布高さ」等）を返す。計画書は文言を
    決めていないので、テストは基本「不正値の `repr` が理由文に載る」ことで検証し、
    ラベルへの依存を最小化した（`validate_dataset_run` の塗布高さだけは
    ジョブパラメータ表の「塗布高さ」に合わせて substring 検証している）
5. **`metadata.blanks[]` のキー集合。** 計画書に列挙が無いので fixture で
    `{index, cell, center, measured_volume_ul, views}` と定義した（`execution` と
    `commanded_volume_ul` を持たないことを `not in` でピン）。実装と一致している

## tests/helpers.py への追加

なし。`FakeCamera` / `FakeKlipper`（既存の自前 HAL ABC の fake）だけを使い、
3rd-party 表面（cv2 / picamera2 / Moonraker / `time.sleep`）はモックしていない。
`test_capture.py` は実 `XYZStage` / `PasteSession` / `CopperProjector` /
実 PCB（`led_blinker.kicad_pcb`）を通している。

## 実機確認（ユーザー担当）が残る点

- 全 PNG が同一ピクセル寸法であること（writer レベルでは強制済み、実撮影は未確認）
- `samples[*].measured_volume_ul` の合計 + `purge.measured_volume_ul` が
    `total.measured_volume_ul` と一致すること（配分ロジックは単体で検証済み）
- blank セルにドロールが落ちていないか（blank の意義そのもの）
