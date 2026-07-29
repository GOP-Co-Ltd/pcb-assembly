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
- 領域は撮像前に幾何だけで選ぶ（`plan_alignment_regions`）。想定エッジを約 1px 間隔の点に落とし、両方向に拘束がある位置を `λ_min(Σ n nᵀ)` で採点して、互いに領域サイズ以上離して数個採る。点の数で正規化するのは、実測 `sharpness` が template 画素数で正規化されているため
- 候補は **ROI 全体が `safe_area`（基板外形を `board_edge_margin` [mm] 縮めた領域）に収まる**位置だけ。外周はやすり掛けで銅箔が削れやすく、さらに基板外形線は投影する想定エッジに含まれないため、視野に入ると片方向 chamfer ではペナルティを受けない偽エッジとして働く。領域選定の定義域と外周除外を同じ図形で決めている
- 数領域の並進の**平均 1 つ**を基板共通の補正とする。設計 board 座標のポリゴンを書き換えることはせず、補正は**機械座標へ出る瞬間**（塗布の変換合成 `board → correction → toolhead → height`）に 1 回だけ適用する

ペースト塗布と Pick and Place の双方から利用する。
