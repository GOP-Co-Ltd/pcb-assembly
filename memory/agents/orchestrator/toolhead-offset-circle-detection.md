# ツールヘッドオフセットの円検出を Otsu 方式へ

## 段階 1: 計画

### 要求

ツールヘッドオフセット計測の円検出を、流量キャリブレーション（`paste_volume.detect`）で
使っている Otsu 方式へ寄せて検出しやすくする。流用できるコードは積極的に流用する。

### 現状

- ツールヘッドオフセット: `ToolheadOffsetProcedure` → `CircleDetector`（`cv2.HoughCircles`）
    → `OffsetObserver` → `XYPositionAdjustor`。Hough は勾配が弱い塗布痕に弱い
- 流量キャリブレーション: `paste_volume/detect.py` が `pre - post` 差分 → 分位点
    コントラストガード → Otsu（下限付き）→ open → 最大連結成分の面積等価直径
- 既存の wart: `CircleDetector` に `target_diameter=(min+max)/2` /
    `diameter_tolerance=(max-min)/2` を渡す一方、`_filter_by_size` は
    `abs(radius - target_radius) <= tolerance/2*ppm` で比較するため、
    実際に通る直径帯が指定の半分になっていた

### 採用案: 単一画像の背景差分 + Otsu

`pre - post` 差分は使えない。`XYPositionAdjustor` が観測ごとにステージを動かすので、
塗布前に撮った pre 画像は 2 回目以降の観測と位置が合わない。代わりに同一フレーム内から
背景を推定して差分を作る:

`crop → gray → MORPH_CLOSE（カーネル = 1.5 × 最大直径 px）で背景推定 → clip(bg - gray)`

以降は `paste_volume` と同一の段（コントラストガード → Otsu 下限 → open →
連結成分）。実素材 `data/testing/paste-volume/` での検証結果:

| 素材   | pre-post 直径 | 単一画像 直径 | 重心差 | circularity |
| ------ | ------------: | ------------: | -----: | ----------: |
| small  |    0.590/0.584 |   0.585/0.586 | <0.3px |   1.00/1.01 |
| medium |    0.762/0.765 |   0.761/0.758 | <0.3px |   0.98/0.98 |
| large  |    0.864/0.866 |   0.856/0.861 | <0.3px |   0.96/0.96 |
| blank  |            0.0 |  ノイズ 12 成分 |      — |   0.13-0.60 |

blank のコントラストは 38-48 で `min_contrast=20` のガードは抜けてしまうので、
**circularity（4πA/P²）下限 0.7** を検出側に足してノイズ成分を落とす
（実点は 0.96 以上、ハッチング/ヘアラインのノイズは 0.60 以下）。
`paste_volume` が円形度を使わないのは「潰れた点の面積も測りたい」ためで、
中心座標を出す用途では非円形を落とすほうが正しい。

### 却下した案

- pre/post 差分を流用: 上記のとおり adjustor の移動と両立しない
- 背景をスカラー高分位点で推定: 実点は同等だが blank のノイズ面積が 1.4 mm 相当まで
    膨らむ（照明ムラを吸収できない）
- `contourArea` 基準へ統一: `paste_volume` の直径定義（画素数の等価直径）が変わり、
    既存校正の数値が黙って動くため不可
- 円形度を `DotDetectionSpec` に足す: `paste_volume` の校正 JSON は全フィールド必須
    なので、既存校正ファイルが読めなくなる。検出器側のパラメータに置く

### 公開インターフェース

`src/pcbasm/vision/dot.py`（新規。`paste_volume/detect.py` の内部から移設）

```python
MAX_OPEN_KERNEL_PX: int
@attrs.frozen class DotDetectionSpec:  # 移設（フィールド・validate は不変）
@attrs.frozen class DarkSpot:
    area_px: int
    center: Point2d
    perimeter_px: float
    @property diameter_px: float      # 面積等価直径
    @property circularity: float      # 4πA/P²（周長 0 なら 0.0）
def darkening(pre_bgr, post_bgr) -> ImageArray
def background_darkening(bgr, *, kernel_px: int) -> ImageArray
def segment_dark_region(difference, spec) -> tuple[ImageArray | None, float, float]
def dark_spots(mask) -> tuple[DarkSpot, ...]
```

`src/pcbasm/vision/detection.py`

```python
class CenterOffsetDetector(ABC):          # detect_with_statistics を共有
    @abstractmethod
    def detect_nearest_center(self, image: Image) -> DetectedCircle | None
    def detect_with_statistics(self, images, *, minimum_sample_count=1) -> OffsetStatistics | None
class CircleDetector(CenterOffsetDetector)    # 既存 Hough。基準点マーカー用で変更なし
class PasteDotDetector(CenterOffsetDetector):
    def __init__(self, *, pixel_per_mm, diameter_min_mm, diameter_max_mm,
                 crop_size=None, spec=DotDetectionSpec(), min_circularity=0.7)
```

- `OffsetObserver` の `detector` 型を `CenterOffsetDetector` へ（受け口を広げるだけ）
- `ToolheadOffsetProcedure` は `PasteDotDetector` を配線し、直径帯を min/max のまま渡す
- `paste_volume/detect.py` は公開 API（`measure_dot` / `detection_mask` /
    `DotMeasurement` / `DotDetectionSpec` 再 export）を保ったまま内部を委譲

### テスト観点

- 実素材の post 画像単体で small/medium/large を検出し、直径が README の参考値
    ±0.05 mm・順序関係が成立（下位桁はピンしない）
- blank は `None`（検出なし）
- 既知量だけ平行移動した実素材で `offset.px` が移動量と一致
- 直径帯の下限/上限外は `None`、circularity 下限でノイズ成分を落とす
- crop_size で ROI 外の点を無視
- 不正引数（pixel_per_mm<=0 / 直径帯逆順 / spec 不正 / circularity 範囲外）は ValueError
- `dark_spots` の面積・重心・circularity、`background_darkening` が照明勾配を消すこと
- `paste_volume` 側の既存テストが無変更で green（移設の回帰）
- `ToolheadOffsetProcedure.measure` が ROI 中心の点で sample を返す（配線）

### リスク

- 実機の板（銅箔 or レジスト）で contrast が実素材と違う → `min_contrast=20` は
    実素材の blank（38-48）より低く、感度優先。誤検出は circularity と直径帯で抑える
- ROI が大きい場合（点間隔 5 mm → 143 px）の closing カーネル 86 px は ROI に近いが、
    実素材の 53 px ROI / 41 px カーネルで検証済み

## 段階 2-3: テスト → 実装

- 共通段を `src/pcbasm/vision/dot.py` へ移設（`DotDetectionSpec` もここへ。
    `paste_volume/detect.py` が再 export するので校正 JSON の schema は不変）
- `CenterOffsetDetector` (ABC) に `detect_with_statistics` を集約し、
    `CircleDetector`（Hough・基準点マーカー用）と `PasteDotDetector`（塗布痕用）が継承
- `OffsetObserver` の `detector` 型を ABC へ広げただけで、posctrl 側の挙動は不変

### 計画外の判断とその理由

- `validate_paste_diameters` を `pasting/toolhead_offset.py` から
    `vision/detection.py` へ移した。検出器自身が同じ規則で引数を検証するので、
    規則を 2 箇所に書かないため。web ジョブとテストの import 元を `pcbasm.vision` に変更
    （module-level 関数のままにしたのは、独立スカラー 2 個の突き合わせなので
    memory/feedback_validation_method.md の例外に当たる）
- `min_circularity` は `DotDetectionSpec` ではなく検出器のパラメータにした
    （spec にフィールドを足すと既存の校正 JSON が必須キー不足で読めなくなる）
- ジョブ params は増やさない。既存の `paste_diameter_min` / `paste_diameter_max` を
    そのまま直径帯として使い、2 値化ハイパラは検出器の既定値に任せる

### 自己レビューの指摘と対応

- 再 export のために `toolhead_offset.py` へ足した `__all__` は余分 → 撤去し、
    ジョブが `pcbasm.vision` から直接読むようにした。`TestValidatePasteDiameters` も
    tests/pcbasm/vision/test_detection.py へ移動（tests のミラー配置を保つ）
- `CircleDetector` に渡していた直径帯が `_filter_by_size` の計算で実質半分になる
    既存の不具合は、min/max をそのまま使う新検出器で解消

### 既知の限界（ドキュメント済み）

`diameter_max` を実際の塗布痕より小さく設定すると背景推定カーネルが塗布痕を
覆えず、差分が輪郭だけになって小片が候補に残る（実測: 0.76 mm の点に対し
`diameter_max=0.4` で 0.15 mm の小片を拾う）。検出器の docstring に明記した。

## 段階 5: ドキュメント

- `data/testing/paste-volume/README.md` に単一画像検出の参考値表（直径・重心差・
    円形度）と円形度 0.7 の根拠を追記。`DEFAULT_MIN_CIRCULARITY` のコメントがここを指す
- docformatter が長い日本語 docstring summary を壊す件を Claude memory へ記録

## レビュー（code-reviewer）指摘と対応

verdict は当初 request-changes。指摘は実データで再現を確認してから対応した。

- **M1 未塗布板の誤検出（対応）**: ジョブ既定 `paste_diameter_min = 0.0` では
    未塗布板の小片（0.10-0.25 mm、円形度 0.89-2.09）が候補に残り、旧 Hough では
    検出されなかったものを拾う退行だった。`validate_paste_diameters` を
    `0 < min < max` に締め、ジョブ既定を 0.4 mm（`minimum=0.1`）に変更
- **M2 円形度の根拠が誤り（対応）**: 「ノイズは 0.60 以下」は大きい成分だけの話で、
    小片は円形度が 1 を超える。README に blank の全成分内訳と「円形度は大きい成分、
    直径下限は小片」という役割分担を明記し、コメント/docstring も直した
- **M3 円形度が 0-1 に収まらない（対応）**: `DarkSpot.circularity` の docstring に
    直径 10 px 未満で 1 を超えることと、小片は直径下限で弾く設計を明記
- **M4 テストが出荷挙動を固定していない（対応）**: 検出器テストの直径帯をジョブ既定
    （0.4-2.0）に合わせ、「下限を外すと blank を拾う」テストで下限の効果も固定
- **S1 入れ子成分の周長 0（対応）**: マスク全体への `RETR_EXTERNAL` をやめ、成分ごとに
    bbox を切り出して周長を測る（環の内側にある点が円形度 0 で棄却される経路を除去）
- **S3 `TestDotDetectionSpec` の配置（対応）**: `tests/pcbasm/vision/test_dot.py` へ移設
- **S5 背景推定カーネルと ROI の関係（docstring で対応）**: ROI は最大直径の 3 倍程度を
    確保する旨を検出器の docstring に明記（点間隔 5 mm / 最大直径 2 mm の既定は満たす）
- **S6 vision → pasting の参照（対応）**: `dot.py` のコメントから上位層の型参照を除去
- **S2 光沢点が環状マスクになる懸念（対応せず）**: 実素材には現れない。合成の環
    （外 12 / 穴 6）でも円形度 0.74 で既定 0.7 を通る。実機で光沢点が出るかは
    ユーザーへの質問として残す
- **S4 公開面の粒度（対応せず）**: `validate_paste_diameters` はジョブが撮影前に弾く
    ために必要で、検出器と同じ規則なので `vision` 側に置くのが自然と判断

## 再レビュー（approve）後の追加対応

- `paste_diameter_min` の `minimum` を 0.1 → **0.3** に締めた。実測で下限 0.1/0.2 では
    未塗布板の小片（0.167/0.252 mm）をまだ拾う。実素材最小の点 0.578 mm に対して
    余裕 93%（memory/MEMORY.md の「指示文では足りない。機構で塞ぐ」に沿う）
- `catalog.py` の下限違反エラー文が `minimum=0` 前提（「マイナスにできません」）
    だったので、実際の下限を出す文言に変更（`point_count` の既存の誤りも直る）
- README に円形度の測定経路（`open_kernel_px=3` を通した値）と、光沢コアが半径の
    半分を超えると検出できない限界を追記
- **移行コストは無い**（レビューで判明）。`JobCatalog.filter_persisted_defaults` が
    下限違反の永続化値を黙って落とすので、既存の 0.0 は新既定 0.4 にフォールバックする

## 残るユーザーへの質問

実機で中心にハイライトが入る光沢のある塗布痕は出るか。出る場合、光沢コアが半径の
半分を超えると円形度下限 0.7 で検出できない（下限を下げるか、マスクの穴を埋める
処理が必要になる）。

## 光沢点への対応（ユーザー判断: 保険を入れる）

残していた質問「実機で光沢のある塗布痕は出るか」にユーザーが「出るかもしれない
（保険を入れる）」と回答したため、マスクの穴埋め
（`fill_dark_spot_holes`）を検出器に挟んだ。

- 穴は「画像の縁から届かない背景」として求める（1 px padding して縁から floodFill）
- 実測: 光沢コア r<=8（外 r=12）まで直径 0.823 mm・円形度 1.00 に戻る。以前は r<=6 が限界。
    r=9 以上は環が細って open で分断されるので穴埋めでは救えない
- **未塗布の判定に影響しない**（blank の大きい成分の円形度 0.17 → 0.17 / 0.18 → 0.19）
- 穴埋めは `paste_volume` 側には入れない（面積等価直径の定義が変わり既存校正が動く）。
    検出器だけが `dark_spots` の前に呼ぶ
- 代償: 別の成分の穴の中にある成分は間の背景ごと埋まって外側と繋がる。その塊は
    直径の範囲で弾かれ、失敗として ROI 画像付きで報告される（黙って誤った中心を
    返すことはない）
