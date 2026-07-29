# posctrl

Board/オフセットの位置合わせを担う共通制御モジュール。

- マシン初期化と Board 変換計測のセットアップ
- Board / 位置 / オフセットの調整
- 設計銅箔のカメラ投影と観測エッジ照合（chamfer、並進のみ・サブピクセル）による位置ずれ算出
- 照合する関心領域の幾何計画（`AlignmentRegion` / `plan_alignment_regions`）
- 領域単位の 1 ショット計測（`RegionAligner` / `RegionAlignment`）と、基板全体の平均並進補正（`BoardAlignment` / `RegionAlignmentSession`）
- Board 巡回用のカメラ表示

観測は `observe() -> Transform`（カメラ mm 空間、原点=画像中心、想定→観測）契約の observer で統一している。

## 銅箔照合の設計

- 推定するのは**並進のみ**。回転・スケールは扱わない（実運用の ROI では微小回転が測定可能な信号にならない）。並進は 3 点放物線でサブピクセルまで補間する
- 一方向のエッジしか無い領域ではコスト曲面が縮退して並進が決まらない（開口問題）。`EdgeMatch.sharpness`（コスト曲面の弱軸方向の曲率）で検出し、`min_sharpness` を下回る照合は棄却する
- 領域は撮像前に幾何だけで選ぶ（`plan_alignment_regions`）。両方向に拘束がある位置を `λ_min(Σ L·n nᵀ)` で採点し、互いに領域サイズ以上離して数個採る
- 数領域の並進の**平均 1 つ**を基板共通の補正とする。設計 board 座標のポリゴンを書き換えることはせず、補正は**機械座標へ出る瞬間**（塗布の変換合成 `board → correction → toolhead → height`）に 1 回だけ適用する

ペースト塗布と Pick and Place の双方から利用する。
