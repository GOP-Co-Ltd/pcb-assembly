# posctrl

Board/オフセットの位置合わせを担う共通制御モジュール。ペースト塗布（`pcbasm.pasting`）と Pick and Place の双方から利用する。

Board 巡回の実行・ユーザー入力・フレーム配信は `web/api/jobs/posctrl.py` が担う。
このパッケージでは OpenCV のウィンドウ表示やキーボード入力待ちは行わない。

## 構成

| モジュール         | 役割                                                                                                    |
| ------------------ | ------------------------------------------------------------------------------------------------------- |
| `setup.py`         | マシン初期化から Board 変換計測までの入口 `setup_board_calibration`、終了時に駐機する `machine_session` |
| `board.py`         | 4 隅の基準点から Board 座標 → 機械座標の 6 パラメータ affine を最小二乗推定 `BoardTransformMeasurer`    |
| `offset.py`        | 観測座標（カメラ）→ 機械座標の回転を 2 点法で計測 `OffsetTransformMeasurer`                             |
| `position.py`      | 観測結果が許容誤差に収まるまで XY 位置を反復補正 `XYPositionAdjustor`                                   |
| `orthogonality.py` | Board 変換から直交性指標を出す純粋計算 `OrthogonalityMetrics`                                           |
| `copper.py`        | 設計銅箔のカメラ投影 `CopperProjector` と観測エッジとの照合 `CopperEdgeMatcher`（chamfer、並進のみ）    |
| `correction.py`    | カメラ空間の照合結果を機械座標の補正 `Transform` へ変換 `to_machine_transform`                          |
| `region.py`        | 銅箔照合領域の計画 `plan_alignment_regions`                                                             |
| `aligner.py`       | 1 領域の反復計測 `RegionAligner` → `RegionAlignment`                                                    |
| `alignment.py`     | 全領域の計測 `RegionAlignmentSession` と pad ごとの補正選択 `BoardAlignment`                            |
| `render.py`        | 位置合わせ・巡回のプレビュー画像合成（cv2 GUI 非依存）                                                  |

公開名は `pcbasm.posctrl` から re-export している。

Board / オフセット調整用の観測は `observe() -> Transform`
（カメラ mm 空間、原点=画像中心、想定→観測）契約の observer で統一している（`setup.OffsetObserver`）。

## 銅箔位置合わせの仕様

以下の既定値は machine 設定 `[paste_dispenser.pad_align]`（`pcbasm.config.PadAlign`）で変えられる。

銅箔位置合わせでは、基板 outline を既定 0.5 mm inset した safe area 内に、
既定 100 px・overlap 0.5 の grid を左上から計画する。対象 pad の所属は中心点で
判定し、中心点を含まない候補領域は計画から除外する。候補領域が safe area を越える
場合は、領域全体が収まる最寄りの位置へ移動する。

`RegionAligner` は各領域を反復計測し、既定 0.03 mm 以下の増分で収束した
`RegionAlignment` だけを採用する。照合失敗・補正上限超過・非収束の領域は
`RegionAlignmentSession` が棄却する。`BoardAlignment` は pad 中心を覆う成功領域のうち、
重なり合う成功領域群の補正量と最も整合するものを選ぶ。同点なら RMS が小さいもの、
さらに同点なら領域中心が pad 中心に近いものを優先する。pad 中心を覆う成功領域が
なければ、中心が最も近い成功領域の補正を使う。成功領域が1件もなければジョブ全体を
中止する。

Board 巡回とペースト塗布では、`Pad.polygon` の最小回転外接矩形の短辺が machine
設定 `paste_dispenser.pad_align.refine_max_short_side` 以下の pad だけ、この領域補正を
初期値として pad 中心で反復照合する。既定値は 0.4 mm で、0 にすると pad ごとの
逐次位置合わせを無効化する。照合対象は従来どおり ROI 内の全銅箔輪郭であり、pad
輪郭だけには限定しない。対象外または照合失敗・非収束の pad は元の領域補正へ
フォールバックし、全 pad の巡回・塗布は維持する。
