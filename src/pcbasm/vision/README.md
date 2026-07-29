# vision

画像処理・解析を担当するモジュール。

- カメラキャリブレーション（`calibration.py` — 多視点コーナーから内部パラメータと pixel/mm を推定）
- レンズ歪み補正の実行時経路（`intrinsics.py` — `CameraIntrinsics` / `Undistorter`）
- 画像からの特徴検出
- 座標変換

`hal`から取得した画像を受け取り、処理結果を返す。ハードウェア制御は行わない。
ステージを動かす多視点スキャンは `posctrl/checkerboard_scan.py` にある。

## レンズ歪み補正

`Undistorter` は `initUndistortRectifyMap` + `remap` で事前計算したマップを適用する
（`cv2.undistort` は毎回マップを作り直すので使わない）。新カメラ行列には**元のカメラ行列**を
そのまま使う（`getOptimalNewCameraMatrix` は使わない）ため、画像サイズ・主点・中心付近の
スケールが保存され、既存の px↔mm 変換点は無改造で正しいままになる。

補正は `FrameHub` の撮像ループで 1 回だけ適用する。校正ジョブ自身は `subscribe(raw=True)` で
歪み補正前のフレームを取る（補正済みフレームで再校正すると残差歪みモデルになり、元の補正が
静かに失われる）。校正 JSON が読めないときは `load_undistorter` が warning 1 行 + `None` を
返し、フレーム経路は素通しになる。

## 2 つの「定規」を混同しない

- **`CalibrationResult.pixel_per_mm` はステージ変位を定規にした値**。コマンドしたステージ移動量と
    歪み補正後コーナーの変位の対応（`p_v - p_0 = A (s_0 - s_v)` の最小二乗フィット）から出るので、
    盤の印刷寸法にも `square_size` の入力値にも依存しない。消費側（`Offset.mm` のステージ移動量、
    `CopperProjector.pixel_of`、`safe_move_distance`）が欲しいのは「ステージ 1mm あたりの画素数」
    なのでこの定規が整合する。
- **盤（`square_size_mm`）を定規にした値は採用値にしない**。盤が 1% 大きく印刷されていれば
    倍率が 1% ずれるが残差は 0 のままで、品質ゲートに掛からない。盤定規は視点別スケールの
    ばらつき `CalibrationQuality.pixel_per_mm_std`（FOV 一様性の指標）としてのみ残す。
    `measure_pixel_per_mm` も盤定規で、`ScanGrid.plan` へ渡す概算にだけ使う。

## 残差レポートの読み方

- 半径帯（`RESIDUAL_BUCKET_EDGES_PX`）は**フル解像度 1280x720 前提の固定 px 値**。最外帯は
    上限を超えたサンプルもすべて回収し、上限として実際に観測された最大半径を報告するので、
    帯のサンプル数の合計は常に全残差数と一致する（表から黙って消えるサンプルはない）。
- `ScanGrid.positions[0]` は中心視点で、これが残差の基準視点になる。基準視点のコーナーが
    画像中央付近に集まっていないと最内の半径帯にサンプルが入らず、
    `CalibrationResult.usable_crop_side_px` が crop の根拠を測れない。
- 残差が画像半径ではなく移動量に比例して増える場合は、レンズ歪みではなくボードの傾きを疑う。
