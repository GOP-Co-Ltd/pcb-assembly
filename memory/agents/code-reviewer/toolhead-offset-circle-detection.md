# ツールヘッドオフセットの円検出を Otsu 方式へ レビュー（再レビュー: 2 巡目）

## verdict: approve

初回レビューの must-fix 4 件・should-fix 4 件はすべて解消を実測で確認した。
残りは非ブロッキングの should-fix 2 件と nit のみ。

## must-fix の解消確認

### M1 出荷既定での未塗布誤検出 → 解消（実測確認）

出荷既定 `paste_diameter_min = 0.4` / `paste_diameter_max = 2.0` で実素材を再測定:

```
blank/0: None   blank/1: None                      <- 初回は 0.167mm / 0.252mm を誤検出
small/0: 0.5889mm  small/1: 0.5783mm
medium/0: 0.7589mm medium/1: 0.7579mm
large/0: 0.8557mm  large/1: 0.8584mm                <- 実点の検出は初回と同値（退行なし）
validate_paste_diameters: (0.0,2.0)/( -0.1,2.0)/(2.0,2.0) -> エラー文, (0.1,2.0)/(0.4,2.0) -> None
```

### M2 円形度の根拠 → 解消（README・コメントの全数値が再現）

`data/testing/paste-volume/README.md` の blank 内訳表を実測と突き合わせ、全行一致:

```
blank/0: 1.277mm/c0.17, 0.310mm/c0.62, 0.208/c0.99, 0.185/c1.16, 0.167/c1.37,
         0.152/c1.45, 0.152/c1.31, 0.096/c2.09, 0.096/c2.09
blank/1: 1.180mm/c0.18, 0.255mm/c0.45, 0.252/c0.97, 0.233/c0.89, 0.176/c1.06,
         0.176/c0.89, 0.118/c1.77, 0.096/c2.09
```

「円形度 = 大きい非円形成分、直径下限 = 小片」の役割分担も実測どおり。
単一画像検出の参考値表（0.589/0.578、円形度 1.02/1.03 等）も再現した。

### M3 `circularity` の値域 → 解消

docstring の「r=1 で約 2.0、r=2 で約 1.3」は実測 1.963 / 1.276 と一致。

### M4 テストが出荷挙動を固定 → 解消

`JOB_DEFAULT_DIAMETER_MIN_MM = 0.4` / `..._MAX_MM = 2.0` が ParamSpec の既定と一致。
`test_blank_material_needs_the_diameter_floor`（下限 0.01 で blank を拾う）が機構を固定。
`tests/web/api/jobs/test_pasting.py` が既定 0.4・`minimum=0.1`・0.0 の拒否をピン。

### S1 穴の中の成分が周長 0 → 解消（実測確認）

```
ring(r=30,t=4) + inner disk(r=6):
  before: area=940 perim=213.82 circ=0.258 / area=113 perim=  0.00 circ=0.000
  after : area=940 perim=213.82 circ=0.258 / area=113 perim= 38.63 circ=0.952
```

通常成分への退行なし（disk r=12 の周長は前後とも 77.25、実素材の円形度も同値）。
`test_measures_a_spot_nested_in_another_spots_hole` が `circularity > 0.7` まで固定していて良い。

### S3 / S5 / S6 → 解消

`TestDotDetectionSpec` が `tests/pcbasm/vision/test_dot.py` へ逐語移設。
ROI は最大直径の 3 倍という条件が検出器 docstring に明記。
`dot.py` から `pasting` への参照を除去。

## 見送り判断の当否

### S2（光沢点の環状マスク）: 判断に同意。ただし README に測定経路を書き足したい

「外 12 / 穴 6 で円形度 0.74」は**検出器パイプライン経由（`open_kernel_px=3`）でのみ**再現する。
生マスクを直接測ると 0.691 で既定 0.7 を**割る**:

```
direct mask       : area=328 perim=77.25 circ=0.691  -> REJECTED
pipeline (open=3) : area=324 perim=73.94 circ=0.745  -> kept   <- README の値
pipeline (open=0) : area=328 perim=77.25 circ=0.691  -> REJECTED
```

open が階段を丸めて周長を縮めるぶんが差。出荷構成は open=3 なので README の結論
（通る）は正しいが、数値だけ見ると再現できないので「合成形状での円形度」表に
測定経路（検出器経由・open=3）を注記してほしい。

出荷パイプラインでの許容範囲も実測した。MR に限界として書くとよい:

```
外径 r=12 / 光沢コア r=0..6 -> kept（r=6 で 0.708mm）
外径 r=12 / 光沢コア r=7..  -> None（検出不能）
→ 光沢コアが塗布痕の半径の約半分を超えると検出できない
```

### S4（公開面の粒度）: 判断に同意

撮影前にジョブが弾く必要があり、規則が検出器と同一。`pcbasm.vision` 据え置きで妥当。

## 新規 should-fix（非ブロッキング）

### N1 `minimum=0.1` は M1 の失敗モードをまだ許す（確信度 高 / 深刻度 低）

対象: `src/web/api/jobs/pasting/toolhead_offset.py:60-70`

既定 0.4 は安全だが、ユーザーが下限まで下げると誤検出が戻る:

```
diameter_min=0.1 -> blank を 0.167mm / 0.252mm で検出
diameter_min=0.2 -> blank を 0.208mm / 0.252mm で検出
diameter_min=0.3 -> None / None
```

実素材の最小の点は 0.578 mm なので `minimum=0.3` にしても実用上の余裕は
約 93% 残る。help 文で警告済み・既定が安全なのでブロックはしないが、機構で
塞げる余地がある（`memory/MEMORY.md`「指示文では足りない。機構で塞ぐ」）。

### N2 下限違反のエラー文が「マイナスにできません」（確信度 高 / 深刻度 低）

対象: `src/web/api/jobs/catalog.py:230-232`

```
validate_params({"paste_diameter_min": 0.0})
  -> ValueError: paste_diameter_min: マイナスにできません（与えられた値: 0.0）
```

0.0 はマイナスではない。`minimum` が 0 前提の汎用文で、既存の `point_count`
(`minimum=5`) も同じ問題を持つ既存不具合だが、今回 `minimum=0.1` を足したことで
ユーザーに見える経路が増えた。新テストは param 名だけを assert しているので
文言の誤りを捕まえない。

## nit（初回から未変化・ブロックしない）

- `_perimeter` が成分ごとに bbox コピー + `findContours` を回すため、周長を使わない
    `paste_volume._largest_component_area` では成分数ぶんの無駄が増えた（従来は 1 回）。
    53x53 マスクでは無視できる
- `background_darkening` は `MORPH_ELLIPSE`、open は矩形 `np.ones` で構造要素の選択が不統一
- `TestBackgroundKernelPx` は 1 行の算術に境界値 parametrize を当てていて価値が薄い
- `test_crop_size_limits_detection_area` は crop 後が一様（blank ガード発火）なので
    crop が壊れていても通る
- `tests/pcbasm/vision/test_detection.py` の `_material` が `tests/helpers.py` と
    素材 path 構築を重複
- `Image.crop_center` は crop > 画像で負の slice 開始によりサイズ違いを黙って返す（既存）

## 確認依頼への回答

### 1. 「未塗布なら検出なし」は出荷構成で成立している

上記 M1 のとおり `blank/0` `blank/1` ともに `None`。実点の検出直径は初回と同値。

### 2. 0.4 mm の余裕は十分

- 実素材の最小の点（`small` = 0.050 uL）: 0.578-0.589 mm → 余裕 +0.18 mm / +45%
- **ジョブ既定の吐出量は 0.1 uL**（`LOADING_DEFAULT_AMOUNT`）で、素材の
    `small` 0.050 uL の 2 倍・`medium` 0.125 uL の 0.8 倍。README の 3 次関係
    （d ∝ V^(1/3)）で外挿すると約 0.74 mm → 余裕 +85%
- 直径 0.4 mm に落ちるのは吐出量が既定の約 15%（0.016 uL）まで減ったとき

実機のペースト粘度・板の濡れ性で変わるので最終確認はユーザー側だが、
素材と既定吐出量から見て不足はない。

### 3. 移行コストの前提が誤り（MR 記述を直してください）

永続化済みの `paste_diameter_min = 0.0` でジョブ起動エラーにはならない。
`JobCatalog.filter_persisted_defaults`（`src/web/api/jobs/catalog.py:193-196`）が
`ValueError` を catch してそのキーを黙って落とすため、フォームは新しい spec 既定 0.4 に
フォールバックする。実測:

```
filter_persisted_defaults(toolhead_offset, {"paste_diameter_min": 0.0}) -> {}
filter_persisted_defaults(toolhead_offset, {"paste_diameter_min": 0.2}) -> {"paste_diameter_min": 0.2}
validate_params(toolhead_offset, {"paste_diameter_min": 0.0})           -> ValueError
```

つまり移行コストはゼロ。エラーになるのは 0.0 を明示的に POST するクライアント
（古いブックマーク・API スクリプト）だけで、それは意図どおりの 400。
MR には「永続化値は自動で新既定へフォールバックする」と書くのが正確。

## ユーザーへの質問（残り 1 件）

実機で中心にハイライトが入る光沢のある塗布痕は出るか。出る場合、光沢コアが
半径の約半分を超えると円形度 0.7 の下限で検出できない（上記 S2 の実測範囲）。

## 検証結果

- make format: pass
- make type: pass（pyright 0 errors）
- targeted pytest: pass（487 passed, 8 deselected /
    `tests/pcbasm/vision tests/pcbasm/pasting/test_toolhead_offset.py
    tests/pcbasm/pasting/paste_volume tests/web/api/jobs/test_pasting.py
    tests/test_package.py -m "not hardware"`）
    ※ coordinator が全体を並行実行中のため範囲を絞った
- 成果物汚染: なし
- 実機テストは未実行
