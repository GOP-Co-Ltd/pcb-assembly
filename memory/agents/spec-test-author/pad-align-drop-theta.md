# 銅箔照合の位置合わせから回転 θ を撤去する（テスト側）

計画書: `/home/gop/.claude/plans/claude-maximize-parallels-majestic-pike.md`

## 書いたテスト一覧

新規:

- `tests/pcbasm/posctrl/test_pad.py::TestPadAlignmentIsPureTranslation::test_displacement_is_independent_of_lever_arm`
  — 正常系。`EdgeMatch.camera_transform` → `to_machine_transform` の実経路で作った M を
  アンカーと 10mm 離れた 2 点に適用し、変位ベクトルが一致することを確認。
  `offset_transform` を Identity / Rotation(30) / Compose([Rotation(90), Shift]) で parametrize。
- `tests/pcbasm/posctrl/test_pad.py::TestPadAlignmentIsPureTranslation::test_result_translation_applies_to_every_pad_of_the_component`
  — 正常系。`PadAlignmentResult.translation`（anchor での変位）が 10mm 離れた pad の補正量と一致。
- `tests/pcbasm/posctrl/test_copper.py::TestEdgeMatch::test_camera_transform_is_pure_shift_by_offset`
  — 公開 IF ピン。`camera_transform` が任意の点で `o ↦ o + offset.mm`。
- `tests/pcbasm/posctrl/test_copper.py::TestCopperEdgeMatcherRoi::test_match_with_roi_recovers_known_translation`
  — 正常系。ROI 中心を画像中心からずらしても既知並進 (+5,+3)px を復元（回転中心という
  概念が消えたので結果が ROI 中心に依存しない）。
- `tests/pcbasm/posctrl/test_alignment.py::TestPadAlignmentSession::test_align_result_transform_has_no_lever_arm`
  — 結合。実 matcher / 実 Canny / FakeCamera の通しで `align()` の M が純並進。
- `tests/pcbasm/posctrl/test_alignment.py::TestPadAlignmentSession::test_warns_when_tolerance_below_diagonal_pixel`
  — 警告系。**閾値は 1px ではなく斜め 1px = √2/ppm**（レビュー S1）。ppm=10 固定で
  tolerance を 0.05（<1px）/ 0.12（1px 超・√2px 未満）/ 0.2（>√2px）に parametrize。
  中央のケースが S1 の見落としそのもののピン。tolerance は
  `_machine_config_with_tolerance()` が常に明示的に差し替えるので、
  `data/testing/config/machine.toml` の値が動いても影響を受けない。

移行（`match_rigid` → `match`、`RigidEdgeMatch` → `EdgeMatch`）:

- `test_copper.py::TestCopperEdgeMatcherRoi` の
  `test_camera_transform_maps_expected_vertices_onto_observed` /
  `test_match_uses_only_template_inside_roi` /
  `test_match_is_stable_with_edges_crossing_roi_boundary` /
  `test_match_returns_none_when_roi_has_no_expected_edges`
- `test_pad.py` の `_dummy_match` と `isinstance(match, EdgeMatch)`
- `test_alignment.py` の `_dummy_match`

削除:

- `test_copper.py` の θ 系 3 本（`test_match_rigid_recovers_pure_translation_with_zero_rotation`
  は `test_match_recovers_known_pixel_shift` と重複、`..._recovers_positive_rotation_sign`、
  `..._translation_and_rotation_together` は並進版へ移行、
  `test_camera_transform_formula_is_rotation_about_center_plus_offset` は
  `TestEdgeMatch` の Shift ピンへ置換）とヘルパー `_rotated_vertices`
- `test_pad.py` の `test_rotation_recovers_machine_angle` /
  `test_rotation_flips_sign_under_mirror_transform`（`PadAlignmentResult.rotation` 廃止）

**削除していない**: `test_correction.py` の回転共役ピン（計画書「変更しない前提」。
`to_machine_transform` は任意 Transform を受ける契約のままで、符号規約 A2/A4 の安全網）。

## 仕様根拠の対応表

| テスト | 計画書の根拠 |
|---|---|
| lever-arm 系 3 本 | §5「レバー腕ゼロ」／Context「誤差 = \|pad − anchor\| × θ」 |
| `TestEdgeMatch` | §1 `camera_transform` = `Shift.from_point(self.offset.mm)` |
| ROI 並進復元 | §5「既知の並進を仕込んだ合成エッジで match(roi=...)」 |
| tolerance 警告 | §4「tolerance < 閾値のとき警告ログを 1 行」＋ レビュー S1（閾値 = √2/ppm） |

## 期待される失敗

なし。テスト作成時点で `plan-implementer` の §1〜4 実装が既に着地しており、
`make test-no-hardware` は 1608 passed で全緑。仕様と実装の食い違いは検出されなかった。

## 実装側に求める修正

なし。ただし観測した事実を 2 点:

- `data/testing/config/machine.toml` の `pad_align.tolerance` は 0.03mm のまま。
  テストの PPM=10（1px = 0.1mm）では 1px 未満なので、§4 の警告が全 session テストで出る。
  警告テストはこれを前提にしている（実機 `config/machine.toml` は 0.04 に更新済み）。
- `CopperEdgeMatcher.crop_size` / `_template_rect` / `roi=None` 経路は本番未使用のまま。
  計画書どおり今回は触っていない（`test_expected_edges_outside_crop_do_not_affect_match`
  が唯一の利用者）。

## tests/helpers.py への追加

なし。カメラは既存の `FakeCamera`（自前 HAL `Camera` の test Impl）、klipper/stage は
`mocker.Mock`（既存イディオム）、エッジ検出は実 `CopperEdgeDetector`。3rd-party
（cv2 / picamera2 / RPC / time.sleep）のモックはゼロ。

## 検証結果

- `pre-commit run --files <3 ファイル>`: 再実行で内容が変化しない（安定）。
  なお `make format`（-a）は実装側の並行編集と競合して毎回別のフックが
  「files were modified」を報告する状態だった。合流時に再実行が必要。
- `make test-no-hardware`: 1608 passed / 87 deselected。
- `grep -rn '</content>' tests/`: 0 件。


## レビュー差し戻し対応（2 巡目）

- **S2**（採用）: `test_displacement_is_independent_of_lever_arm` の parametrize に
  `Compose([Rotation(30), Scale.flip(y=True)])`（det<0）を追加。削除した
  `test_rotation_flips_sign_under_mirror_transform` が唯一の鏡映ケースだったため。
- **S3**（採用）: `_dummy_match_with_offset` を廃し、`_dummy_match(offset_px=Point2d(0,0))`
  の既定引数 1 本に統合。
- **nit**（採用）: リポジトリに存在しない「計画書 pad-align-drop-theta.md」への docstring
  参照を全 6 箇所から除去（test_pad.py のモジュール docstring のみ、このノートへの
  相対パス参照に置換）。
- **S1 波及**: 警告テストを √2px 基準へ書き直し（上記）。警告文リテラル "1px" への依存を
  やめ、substring は "tolerance" のみに絞った。実装側は既に
  `math.sqrt(2) / pixel_per_mm` へ変更済みで、3 ケースとも green。

`tests/pcbasm/posctrl/test_correction.py` は 2 巡目も無変更。

## 残る不確定要素（実装側の tolerance 決定に依存）

- 私のテストは **`data/testing/config/machine.toml` の `tolerance` 値に依存しない**
  （警告テストは常に上書き、他の session テストは警告有無を見ない）。
  実装側が 0.03 → 0.04 のどちらに決めても、テストの追従は不要。
- 実機 `config/machine.toml` と `kurousagi.paste` テンプレートの tolerance 値
  （0.04 → 0.05 の判断）はテスト対象外。値そのものは実機確認でユーザーが決める。
- 依存しているのは「閾値が `√2 / pixel_per_mm`」という**式**のみ。ここが再度変わる場合は
  parametrize の 3 つの境界値を見直すこと。

## 検証結果（2 巡目）

- `pre-commit run --files <3 ファイル>`: 再実行で内容が変化しない（安定）。
- `make test-no-hardware`: 1612 passed / 87 deselected。
- `grep -rn '</content>' tests/`: 0 件。
