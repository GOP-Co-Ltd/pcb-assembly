# カメラ歪み補正 — code-reviewer 指摘の修正

ブランチ `feature/20260728/camera-distortion`。計画は
`~/.claude/plans/claude-pixels-mm-0-1mm-300-x-swirling-robin.md`。
前提ノート: `camera-distortion-core.md` / `-scan.md` / `-webui.md`。

`src/` と `tests/` の両方を 1 体で担当（A / B / C1 が数値期待を連動させるため）。

## 計画外の判断ログ

### A. 採用 `pixel_per_mm` を「ステージ定規」に変更（ユーザー裁定・計画 §2 手順 5 からの逸脱）

`CalibrationResult.pixel_per_mm` を `quality.after.pixel_per_mm`（コマンドした
ステージ変位と補正後コーナー変位の対応）にした。従来は補正後コーナーと
`square_size_mm` の相似変換フィット平均＝盤定規。

- 盤定規の視点別スケールは `pixel_per_mm_std`（FOV 一様性）として残す。
  `IntrinsicsCalibrator.solve` は `_fit_view_scale` を σ の算出にだけ使う。
- `square_size_mm` は `calibrateCamera` の objectPoints 用として引き続き必要。
- `CalibrationQuality.summary_lines()` の先頭行に
  `採用 pixel/mm 30.31（ステージ定規） / 視点別 pixel/mm の σ=…（盤定規・FOV 一様性）`
  を追加。ジョブ summary も `pixel/mm 30.31（ステージ定規）` と明示。

### B. `usable_crop_side_px` が空帯を「上限超過」と同一視していた

先頭側の空帯は「証拠なし」として読み飛ばし、サンプルが出たあとの空帯で打ち切る
（`sampled` フラグ）。docstring を実挙動に更新。

### C1. `ScanGrid.positions[0]` を中心視点に戻した

蛇行列を作ってから中心 `(0,0)` を先頭へ移す（`列数・行数がともに奇数のときだけ
中心が格子点なので `if center in positions` で条件付き）。中央行に 1 か所だけ
2 ステップの飛びが残る。

### C3. 最外の半径帯が上限超過をすべて回収

`_radial_buckets` の最外帯は `radii >= lower` を全部取り、`radius_px[1]` に
**観測された最大半径**を報告する。`RESIDUAL_BUCKET_EDGES_PX` の px 値が 1280x720
前提であることを定数コメントに注記。

副作用として、centre-first では最外帯（450px 超）にサンプルが入らないことが
普通になった。`residual_render._draw_radial_profile` が空帯を 0.0 として折れ線に
打つと「そこで急に良くなった」ように見えるため、**サンプルのある帯だけ**を
プロットするようにした（C1/C3 が生んだ表示の嘘なので合わせて直した）。

### D1. `intrinsics.py` のモジュール docstring

`framehub.py` を `from pcbasm.vision.intrinsics import Undistorter` /
`from pcbasm.vision.image import Image` に変更した**うえで**、docstring は
「パッケージの `__init__` が両方を re-export するのでモジュールのロード自体は
避けられない。分離の目的は依存の向きを明示することであって遅延ロードではない」と
実態に合わせた（submodule import でも親パッケージの `__init__` は必ず走るため、
「calibration.py をロードしない」とは書けない）。

### 指摘したが直さなかったもの

- **C5 の候補順に対する専用テストは書いていない**。降順（大きいパターン優先）に
  したが、これを公開 API から観測するには「本物の部分パターンを含む画像」が必要で、
  合成盤・実 `checkerboard.png` のどちらでも昇順・降順で同じ結果になる。全レンジ
  総当たりは 1 視点 5.45 秒かかりテストに入れられない。キャッシュ契約は既存の
  `test_pattern_size_is_locked_after_the_first_view` が引き続きピンしている。
- `_converter` の `Path` フック、`PasteSession.setup`、リポジトリ直下の untracked
  バイナリ名ファイルは指示どおり触っていない。`vision/README.md` /
  `posctrl/README.md` は `code-simplifier` 担当なので触っていない。

## 他 implementer への IF 変更通知

- `CalibrationResult.pixel_per_mm` の**意味**が変わった（値の出所がステージ定規）。
  型・シグネチャは不変。
- `RadialResidualBucket.radius_px[1]` は最外帯だけ可変（観測最大半径）。帯の px 値を
  定数と決め打ちにしているコードがあれば要修正（現状 `residual_render` のみで対応済み）。
- `tests/helpers.py::SyntheticCheckerboardCamera(mirror_y: bool = False)` を追加。

## 既知の制約・残課題

- 中心視点が基準になったことで最外帯（>450px）にサンプルが入りにくく、
  `usable_crop_side_px(30)` は実機想定で 636〜649px を返す（crop 600 の根拠には十分。
  それ以上を推奨させたい場合は帯の刻みを見直す必要がある）。
- 実機確認（`@mark_hardware` / ブラウザ）はユーザー担当。

## 実測検証

### A: 盤を意図的に 1% 大きく印刷（公称 `square_size=1.5` のまま校正）

| 盤の実寸 | 採用 ppm（ステージ定規） | 盤定規 ppm | after.rms |
|---|---|---|---|
| x1.00 | 30.3100（誤差 -0.000%） | 30.3100（+0.000%） | 0.00um |
| **x1.01** | **30.3100（誤差 +0.000%）** | **30.6131（+1.000%）** | **0.00um** |

盤定規は残差 0 のまま +1.0% ずれる＝品質ゲートで検出できない。ステージ定規は
誤差 0.000%。`TestIntrinsicsCalibrator::test_adopted_pixel_per_mm_uses_the_stage_as_the_ruler`
としてテスト化した。

### B: `SyntheticCheckerboardCamera(pixel_per_mm=20.0)` の再現

| 訪問順 | 帯のサンプル数 | `usable_crop_side_px(30)` | 旧ロジックなら |
|---|---|---|---|
| 中心が先頭（C1 修正後） | 168 / 852 / 212 / 0 | **636** | 636 |
| 隅が基準（C1 修正前の順） | **0** / 135 / 674 / 423 | **720** | **0** |

どちらの順でも 0 を返さなくなった（B と C1 は独立に効く）。

## 更新した既存テスト

| テスト | 変更 | 根拠 |
|---|---|---|
| `_serpentine_offsets` → `_scan_offsets` | 中心を先頭に | `ScanGrid.plan` の訪問順に合わせる（C1） |
| `test_rejects_collinear_stage_positions` | `offsets[0:3]` → `offsets[1:4]` | 新順序では先頭 3 点が同一直線でなくなった。`[1:4]` は蛇行 1 行目＝Y が同じ 3 点 |
| `test_visits_…_including_the_start` + 新規 `test_starts_at_the_centre_view` | `positions[0] == (0,0)` を追加 | 計画 §2 の契約（C1） |
| `test_consecutive_moves_stay_within_one_lattice_step` → `test_moves_after_the_centre_are_single_axis_lattice_steps` | 中心→隅の跳びを除外、X は 2 ステップまで許容、代わりに**単軸移動**をピン | 中心を先頭へ出すと中央行に 1 か所だけ 2 ステップの飛びが残る |
| `test_all_radial_buckets_receive_samples_…` → `test_radial_buckets_tile_the_radius_and_keep_every_sample` | 「全帯にサンプル」→「タイル + 合計 = corner_count + 最内帯にサンプル」 | 中心基準では最外帯が空になる（帯上限 450px 超のサンプルが出ない）。落ちてはいけないのは**合計**の方（C3） |
| `test_raw_residual_grows_monotonically_with_image_radius` | 空帯（rms 0.0）を単調性の対象外に | 同上 |
| `test_side_never_exceeds_the_shorter_frame_edge` / `test_side_is_not_clamped_when_the_frame_is_large_enough` | 視点集合を `_corner_first_quality(small_board)` に | 中心基準 + 観測最大半径では 649px でクランプが効かず、クランプの回帰テストにならないため、半径が 628px まで伸びるケースへ差し替え |
| `test_quality_does_not_own_the_crop_side` | **削除** | `assert not hasattr(...)` が壊れやすい。所有者の契約は「解像度を変えると値が変わる」2 テストが実質的にピンしており、その旨をクラス docstring に明記した |
| `test_recovers_pixel_per_mm_from_stage_displacement` | `mirror_y` で 2 パラメトライズ + `rms_um < 1.0` | `det A < 0`（実機の下向きカメラ）の被覆漏れ（C6） |
| `test_recovers_camera_mount_rotation` | `abs()` を外して +3.0 を符号込みでピン、コメント修正 | 合成モデルは `A = ppm R` なので符号は決定的 |
| `test_load_legacy_json_without_intrinsics_raises` | `pytest.raises(Exception)` の理由をコメント化 | cattrs の例外型は ExceptionGroup 派生でバージョン依存 |
| `tests/webui/jobs/test_posctrl.py` の `TestPosctrlHardware` クラス docstring | 「キャリブレーション済み」の共通前提を訂正 | `camera_calibration` は未校正から始めるジョブ |

新規テスト: `test_outermost_bucket_collects_everything_beyond_the_nominal_edge`（C3）、
`test_empty_inner_buckets_are_read_through_instead_of_failing`（B）、
`test_adopted_pixel_per_mm_uses_the_stage_as_the_ruler`（A）、
`test_rejects_non_finite_camera_matrix` / `test_rejects_non_finite_distortion_from_json`（C2）。

## 検証結果

- `make format`: pass（修正なし）
- `make type`: pass（0 errors）
- `make test-no-hardware`: pass（**1710 passed** / 87 deselected。修正前 1702）
- `make test-e2e`: pass（51 passed）
- `grep -rn '</content>' src tests data`: 空
- `make test` / `pytest -m hardware`: **実行していない**（実機保護）
