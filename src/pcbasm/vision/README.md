# vision

画像を受け取り、検出・計測の結果を返す画像処理を置く。
ハードウェアに触らず、`pcbasm.hal` を import しない。画像は呼び出し側が `hal` のカメラから取得して渡す。

## 構成

| モジュール       | 役割                                                                                                                                                                                      |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `image.py`       | BGR 3 チャネル画像 `Image`（イミュータブル）、表示先コールバック型 `FrameSink`、`safe_move_distance`                                                                                      |
| `calibration.py` | チェッカーボードから pixel/mm 比を求める `CheckerboardCalibrator` と結果 `CalibrationResult`                                                                                              |
| `detection.py`   | 画像中心からのずれを返す検出器。Hough 円検出 `CircleDetector`、塗布痕検出 `PasteDotDetector`                                                                                              |
| `dot.py`         | 暗い斑点（塗布痕）の差分画像（塗布前後 2 枚は `darkening`、1 枚は `background_darkening`）・Otsu 2 値化・連結成分の実装。`PasteDotDetector` と `pasting.paste_volume.detect` はこれを呼ぶ |
| `copper.py`      | 銅箔の境界エッジ抽出 `CopperEdgeDetector`（照合は `posctrl.copper` が行う）                                                                                                               |
| `crop.py`        | Board 座標の点の周りを固定ピクセル寸法で切り出す `crop_centered`                                                                                                                          |
| `overlay.py`     | 十字線・検出円・オフセット表示の描画                                                                                                                                                      |

主要な公開名は `pcbasm.vision` から re-export している。
`dot.py` と `crop.py` の関数は `from pcbasm.vision.dot import ...` のように直接 import する。

## 置き場所の判断

- 1 枚（または塗布前後の 2 枚）の画像だけで完結する処理はここに置く
- 機械を動かしながら画像を撮り直す手順（反復計測・位置合わせ）は `posctrl` か `pasting` に置く
- 座標変換そのもの（`Transform` など）は `pcbasm.geometry` に置く
