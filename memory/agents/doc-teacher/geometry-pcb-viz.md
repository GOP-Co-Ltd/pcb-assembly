# geometry / pcb / visualization / パッケージ直下 docstring 理解度テスト

## 対象
- ファイル: `src/pcbasm/geometry/*.py`、`src/pcbasm/pcb/*.py`、`src/pcbasm/visualization/*.py`、`src/pcbasm/*.py`（docstring・コメントのみ）
- 読者と用途: 開発者が、3D 座標・幾何計算、KiCAD 読込と PCB 設計情報、可視化の公開 API の契約（座標系・単位・前提・戻り値）を理解し、正しく使い・修正できるようにする
- 生徒に読ませるファイル（計 1339 行）: `geometry/transform.py`、`geometry/routing.py`、`geometry/packing.py`、`pcb/__init__.py`、`pcb/kicad.py`、`visualization/height_render.py`

## 評価用問題
### Q1 塗布経路の点を基板表面から 0.3 mm 上に置きたい。HeightPlane を含む変換を適用する前の Point3d の z に何を入れるか。理由も答えよ。
- 要点: 0.3（表面からの相対高さ）
- 要点: HeightPlane.apply は点の XY で評価した曲面の高さを z に足すため

### Q2 Point2d の長さと Point3d の長さを取るとき、書き方はどう違うか。
- 要点: Point2d は `p.norm`（プロパティ、括弧なし）
- 要点: Point3d は `p.norm()`（メソッド呼び出し）

### Q3 `Compose([Matrix2d(m), Shift(x=10.0)])` を点に適用する。行列と平行移動のどちらが先にかかるか。
- 要点: Matrix2d が先、次に Shift（self[0] から順に適用）

### Q4 KiCad から読んだ基板座標の点 (1, 0) に `Rotation(90.0)` を適用した。結果の座標と、KiCad の画面上で見たときの回転の向きを答えよ。
- 要点: 結果は (0, 1)
- 要点: 基板座標は Y 下向きなので、画面上では時計回り

### Q5 `sort_by_nearest(points, start)` の戻り値に start は含まれるか。また、最後の点から start へ戻る距離まで短くなるよう並ぶか。
- 要点: start は含まれない
- 要点: 片道で、start へ戻る区間は考慮しない

### Q6 `pack_rects` に `gap=0.5` と keepout を渡した。配置された矩形と keepout の間に 0.5 の隙間は保証されるか。隙間が要るならどうするか。
- 要点: 保証されない
- 要点: 呼び出し側で keepout を gap だけ広げて渡す

### Q7 `PcbFile.pads` の座標の原点・Y 軸の向き・単位を答えよ。
- 要点: 原点は基板外形 bbox の左上（KiCad 座標の最小 X・最小 Y）
- 要点: Y は下向き
- 要点: 単位は mm

### Q8 `PcbFile.pads` を `(designator, pad_number)` をキーにした dict に詰めたら、要素数が `len(pads)` より少なくなった。考えられる理由を答えよ。
- 要点: ペースト形状が複数の輪郭に分かれる pad は輪郭ごとに別の Pad になり、同じ (designator, pad_number) が複数できる

### Q9 KiCad（pcbnew）が入っていない環境のスクリプトで `from pcbasm.pcb import PadList` を使えるか。理由も答えよ。
- 要点: 使えない
- 要点: pcbasm.pcb は PcbFile を re-export しており、PcbFile が pcbnew を import するため

### Q10 `render_height_plane` の図と `render_pcb` の図を並べたら、基板の上下が逆に見えた。バグとして直すべきか。
- 要点: バグではない（仕様）
- 要点: render_height_plane は Y 上向き、render_pcb は Y 下向き（KiCad と同じ見た目）

### Q11 高さ計測の点が 6 点あるが、全点が一直線上に並んでいる。これで HeightPlane を作るとどうなるか。
- 要点: ValueError になる
- 要点: 6 点以上かつ退化していない点が必要

## 保留問題
### H1 `Rotation.from_points(Point2d(1, 0), Point2d(0, -2))` の角度（度）はいくつか。
- 要点: -90 度
- 要点: ベクトルの長さは無視する（範囲は -180 超 180 以下）

### H2 KiCad で DNP に設定した部品のパッドは、`PcbFile.pads` と `PcbFile.copper` に含まれるか。
- 要点: どちらにも含まれない

### H3 HeightPlane が機械座標で計測されている。`render_height_plane` の `pcb_to_plane` に何を渡すか。省略するとどうなるか。
- 要点: 基板座標から機械座標への変換（board_to_machine）を渡す
- 要点: 省略すると Identity になり、基板背景が基板座標のまま描かれてヒートマップとずれる

## ラウンド記録
### R0 執筆（対象全体 5397 → 5506 行、+2%）
直した主な点:
- transform: モジュール docstring、Point2d/3d の norm の違い、Rotation の正の向き（Y 下向きで時計回り）、from_points の範囲、Matrix2d の z、HeightPlane の入力 z の意味・ValueError 条件（既存の docformatter 崩れ「。 」も解消）、Compose の適用順
- routing: 「巡回経路」は誤り（片道、start は含まない）
- packing: keepout との gap は保証しない、矩形は回転しない（既存の「。 」崩れも解消）
- sampling: min_radius の意味、戻り点数、Raises を実装どおりに
- pcb: 基板座標の定義（原点・Y 下向き・mm）、pcbnew 必須、DNP 除外、分割 pad が重複すること、Outline.save が穴を保存しないこと、re-export しない理由の誤り（「pcbnew 依存だから」は PcbFile も同じなので誤り）を削除、format_footprint_id の書式誤り
- visualization: 図の Y 向き（pcb/fill は下向き、height は上向き）、ヒートマップ値の意味、calibration scatter の崩れた docstring
- config: get_machine_config の読み先、paste_height のコメント（表面からの高さ）
- gcode: モジュール docstring、G1 は G90/G91 状態に従う、F は mm/min

### R1（生徒用 1339 行 → 1341 行。対象全体 5506 → 5508 行）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1 | 正解 | - | - |
| Q2 | 正解 | - | - |
| Q3 | 正解 | - | - |
| Q4 | 正解 | - | - |
| Q5 | 正解 | - | - |
| Q6 | 正解 | - | - |
| Q7 | 正解 | - | - |
| Q8 | 正解 | - | - |
| Q9 | 不正解（判断不能） | 曖昧 | pcb/__init__.py の「このパッケージの import にも pcbnew が要る」は、PcbFile 以外の名前にも効くのかが読み取れなかった。どの名前を import しても pcbnew が要ること、その理由（`__init__` が PcbFile を読み込む）を 2 文に分けて書いた |
| Q10 | 正解 | - | - |
| Q11 | 正解 | - | - |

### R2（生徒用 1341 行、変更なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q11 | 全問正解 | - | - |

Q9 は R1 の改稿で直った（根拠に pcb/__init__.py 9-12 行を挙げた）。Q2 は根拠の箇所を書いていないが、R1 で transform.py を挙げ、答えも同じなので正解とした。次は保留問題 H1〜H3 を出す。

### 汎化確認（保留問題、生徒用 1341 行、変更なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1 | 正解 | - | - |
| H2 | 正解 | - | - |
| H3 | 正解 | - | - |

H1 は docstring ではなく実装の atan2 から計算した答えだが、値は正しい。長さを無視することも計算に含まれているので正解とした。ループはここで完了。
