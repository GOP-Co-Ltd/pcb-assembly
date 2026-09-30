# Geometry

装置やファイル形式に依存しない、純粋な座標・幾何計算を置く。
外部依存は numpy・shapely・attrs だけで、`pcbasm` の他パッケージを import しない。

## 構成

| モジュール     | 役割                                                                                                                                    |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `transform.py` | 点 `Point2d` / `Point3d` と変換 `Transform`（`Shift` / `Rotation` / `Scale` / `Matrix2d` / `HeightPlane` / `Identity`）、合成 `Compose` |
| `path.py`      | 速度を持たない順序付き 3D 点列 `Path`                                                                                                   |
| `routing.py`   | 巡回順の最適化 `sort_by_nearest`（nearest neighbor + 2-opt）                                                                            |
| `sampling.py`  | ポリゴン内部からのプローブ点サンプリング `sample_points_in_polygons` と診断 `sampling_diagnostics`                                      |
| `polygon.py`   | shapely ポリゴンの結合・変換・最小回転外接矩形 `oriented_bbox`・線分クリップ `clip_segment`                                             |
| `polyline.py`  | 点列の総延長 `polyline_length`・環の部分列 `ring_segment`                                                                               |
| `packing.py`   | 軸平行矩形のビンパッキング `pack_rects`（MaxRects）。左上原点・下向き正                                                                 |

`packing.py` 以外の公開名は `pcbasm.geometry` から re-export している。
`packing.py` は `from pcbasm.geometry.packing import pack_rects` のように直接 import する。

## 約束事

- 変換は `apply(point)` で点に適用し、`inverse()` で逆変換を得る
- `Compose([a, b])` は `a` → `b` の順に適用する
- `Path` は点の並びだけを持つ。送り速度は G-code 生成側（`pcbasm.hal.XYZStage.to_gcode`）で与える

## 置き場所の判断

- 塗布・位置合わせなどのドメイン知識を含まない計算幾何だけをここに置く
- ドメイン知識を含む計算は各ドメインパッケージ（`pasting` / `posctrl`）に置く
- KiCad に固有の処理は `pcbasm.pcb` に置く
