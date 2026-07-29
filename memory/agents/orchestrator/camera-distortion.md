# camera-distortion（レンズ歪み補正）orchestrator ノート

ブランチ `feature/20260728/camera-distortion`。計画は
`~/.claude/plans/claude-pixels-mm-0-1mm-300-x-swirling-robin.md`（承認済み）。

## ユーザー確定事項

1. `cv2.calibrateCamera` + ステージ自動スキャンで多視点（手動の傾け撮影は却下）
2. 補正はフレーム源（`FrameHub` の capture ループ）で適用
3. `camera.crop` 600 化のみがスコープ。銅箔照合 ROI は触らない
4. `CalibrationResult` は破壊的変更（intrinsics 必須・旧 JSON は読めない）
5. ボードはマス目 12x9・1.5mm（内部コーナー 11x8）。格子は 5x3 = 15 点
6. 中央合わせを手動で行うため、ページ表示時から十字線 + 関心領域オーバーレイが必須

## 裁定ログ

### Phase 1 で実装側から上がった逸脱の採否

- **採用: `A` を一般 2x2 行列でフィット**（計画 §2 の「等方スケール × 回転、4 パラメータ」は
  記述矛盾。等方スケール+回転は 2 パラメータ）。下向きカメラは画像 y が機械 Y と逆向きで
  `det A < 0` になるため、回転のみのモデルでは実機を表現できない。一般 2x2 は tilt の
  線形成分も吸収するが、レンズ歪みは radial で線形写像に吸収されないため残差は歪みを
  捉え続ける。むしろ tilt のアフィン成分を吸収してくれる分だけ良い。
- **採用: `ResidualReport.measure` は 3 視点以上・非同一直線を要求（`ValueError`）**。
  計画の「views < 2 で ValueError」からの強化。無防備だと rank-1 フィットで `ppm≈0`
  → 残差 NaN → `NaN > residual_limit` が False になり、Phase 3 の品質ゲートが
  **ゴミの (K,D) を黙って通す**。`ScanGrid` の 5x3 格子は条件を満たす。
- **採用: `CheckerboardView.image_size` と `ScanGrid.plan(pattern_size=...)` の追加**。
  前者は半径バケットの中心が決まらないため、後者は静穏枠の導出に内部コーナー数が
  必要なため。どちらも計画のシグネチャでは実装不可能だった。
- **採用: 半径バケットの帰属は「コーナー対の 2 半径の平均」**。4 方式を実測し、
  これだけが計画のテストが期待する単調増加を示す（82/105/140/196µm 対
  素朴な方式の 118/139/137/133µm）。
- **修正指示: `usable_crop_side_px` を `CalibrationResult` へ移してクランプ**。
  720px 高のフレームに対し 905 を返していた。実装側は「Phase 3 で `min()` を取れ」と
  提案したが、それは WebUI 薄ラッパー原則違反（ドメインロジックの漏れ）。解像度を持つ
  `CalibrationResult` 側で `min(side, width, height)` する。
- **裁定不要（両エージェントが独立に同じ結論）**: `TestDistortionRecovery` の閾値は
  全画面 < 0.5px / 中央600 < 0.2px の 2 段、`after.rms_um < 8`。計画の実測表の
  0.2px は中央600 の数字であり、全画面 max は 0.714px だった。

### 合流検証で確認したこと（orchestrator 自身の grep）

- 削除した API（`CheckerboardCalibrator` / `mm_per_pixel` / `mean_distance_px` /
  `std_distance_px` / `crop_size`）の残存参照は `src/webui/jobs/posctrl.py`（Phase 3 担当）
  と `data/testing/config/ov9281_test_fixture.json`（Phase 3 担当）のみ。
  他のヒットはすべて**別物**なので触らない:
  - `crop_size` の大半は `camera.crop.size` / `CircleDetector(crop_size=)` /
    `CopperEdgeMatcher(crop_size=)` の引数（`CalibrationResult.crop_size` とは無関係）
  - `posctrl/copper.py` と `webui/jobs/board_ops.py:145` の `mean_distance_px` は
    `RigidEdgeMatch` の chamfer 品質指標という**別クラスの同名フィールド**
- `grep -rn '</content>' src tests` は空（サブエージェント Write の既知事故なし）
- `data/camera_calibration/*.json`（3 世代）は **git 管理外**かつコードからの参照なしの
  ローカル残骸（WebUI 導入前のスクリプト時代の出力先）。新スキーマでは読めなくなるが
  誰も読まないので触らない。CLAUDE.md 原則 3。

### 指摘に留めたもの（削除しない）

- `PasteSession.setup`（`src/pcbasm/session.py:50`）は dead code。CLAUDE.md 原則 3 に従い
  型追随のみで削除しない。
- リポジトリ直下の untracked なバイナリ名ファイル `"\0014\253\006@W@8"` は本件と無関係
  （Phase 1 開始前から存在）。触らない。

### レビュー（code-reviewer, xhigh）指摘の裁定

verdict: request-changes（must-fix 1 件）。全件受理し `review-fix` に修正させた。

- **ユーザー裁定（計画からの意図的な逸脱）**: `CalibrationResult.pixel_per_mm` を
  **ステージ変位を定規にした値**（`quality.after.pixel_per_mm`）に変更。計画 §2 手順 5 は
  補正後コーナー × `square_size_mm` の相似変換フィット＝**印刷ボード定規**を指定していたが、
  消費側（`Offset.mm` → ステージ移動量・`CopperProjector.pixel_of`・`safe_move_distance`）が
  欲しいのはすべて「ステージ 1mm あたりの画素数」。盤が 1% 大きく印刷されていると盤定規は
  +1.0% ずれ、画像中心から 10mm で 100µm ＝ 0.1mm 予算を単独で使い切るが、**残差は 0.0µm
  なので品質ゲートで検出できない**。ステージ定規なら誤差 0.0% で、`square_size` の入力値を
  取り違えても壊れない。計画自身が残差の節で「印刷ボードの寸法精度に依存せず」を利点として
  挙げており自己矛盾していた。盤定規の視点別ばらつきは `pixel_per_mm_std` として残す。
- **must-fix**: `usable_crop_side_px` が「サンプル 0 の帯」を「上限超過」と同一視して 0 を
  返す。ppm 20.0 の合成ケースで再現。**校正が健全なのに crop 600 化が阻止される**。
  先頭側の空帯は読み飛ばす（内側の帯は外側に包含され歪みは半径単調なので安全）。
- **orchestrator の指示ミスの訂正**: Phase 1/2 のブリーフで「計画の `positions[0] == (0,0)`
  は誤りなので採用しない」と両エージェントに伝えたが、**計画の記述が正しく私の判断が誤り**。
  基準視点が格子の隅になり最内帯が埋まらないことが上記 must-fix の遠因（中心先頭なら
  最内帯 n=168 で `usable_crop_side_px(30)=636`）。「中心 → 隅 → 蛇行」に戻させた。
- **should-fix**: 非有限値の `isfinite` ガード（`json.loads` は `NaN`/`Infinity` リテラルを
  既定で受理するので、非有限の (K,D) が警告なしに全フレームを壊す経路が残っていた）／
  最外帯を超えたサンプルの黙殺／`residuals.png` の URL をログに出す／検出候補を大きい
  パターン優先に（誤った `pattern_size` の確定・キャッシュを構造的に防ぐ）／`det A < 0` を
  通すテストの追加（一般 2x2 を採った根拠そのものが未ピンだった＝被覆漏れ）。
- **レビュアーが問題なしと確認**: `_fit_residuals` の最小二乗と `sqrt(|det|)` によるスケール
  抽出（アスペクト非等方は混入しない）／`calibrateCamera` のフラグと主点非固定／
  `Undistorter` が `CV_16SC2` + `remap` で `newCameraMatrix` は元の K（`cv2.undistort` /
  `getOptimalNewCameraMatrix` 不使用）／`measure_pixel_per_mm` と `_object_points_mm` の
  一本化が `solve` の値を変えていないこと／生フレーム経路（計画用ショットとスキャン本体の
  両方が `raw=True`、`_frame` と `_frame_raw` は同一 seq で原子的に公開）／degrade 4 経路／
  薄ラッパー境界／`ApplyPayload.files` の代替カバレッジ／モック方針（3rd-party なし、
  `SyntheticCheckerboardCamera` は実 `cv2.projectPoints` で実装のミラーではない）／
  `TestDistortionRecovery` の閾値は決定論的で flaky でなくマージン 2〜4 倍。

## 進捗

- Phase 1（コア層 + そのテスト）完了。`tests/pcbasm -m "not hardware"` → 1034 passed。
  `make type` の残り 2 件は Phase 3 スコープ（`webui/jobs/posctrl.py` の
  `CheckerboardCalibrator` import と `tests/webui/jobs/test_posctrl.py` の `crop_size`）。
- Phase 2 着手: `posctrl/checkerboard_scan.py`、`visualization/residual_render.py`、
  `overlay.draw_scan_coverage`、および `undistort_views` / `residual_field` /
  `usable_crop_side_px` の移設。
