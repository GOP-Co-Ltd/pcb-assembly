# 点塗布・多視点データ収集への `src/ml/` 適応

## 概要

MR !207（`refactor/2026-09-08/paste-dataset-dot-core`）で収集方式が「点塗布・銅板・多視点・
mask なし・metadata v2」へ変わった。これに合わせて `src/ml/` のサイズ契約・多視点統合・
head 活性化・blank 評価を適応させる。`!207` の merge は待たない（依存の向きが
`pcbasm` → `ml` の一方向なので、`ml` 側は metadata schema を知らないまま先に進められる）。

**ベースライン実測（main、`make ml-docker-check`）: 1160 passed / 1 skipped / exit 0。**

## 実測で確かめたこと

計画の根拠はすべてコンテナ内（`pcb-assembly-ml-ml-1`）の実測。推論ではない。

### E1. 現行 encoder は 53 px に対して downsample が過剰

`ImageEncoder` に候補 config を入れ、最終 feature map・parameter 数・GMAC を
`ml.model.inspection.ModelSize` で実測した（入力 6 channel、head は
`conditioning_features=1` / `hidden_features=128`）。

| 候補 | total_stride | out_feat | params | GMAC@53 | GMAC@159 | featmap 27 / 53 / 159 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 計画書 v1（stem 3 段） | 32 | 160 | 1,268,840 | 0.0152 | 0.1087 | 1×1 / 2×2 / 5×5 |
| **stride8_A** | **8** | **96** | **395,048** | **0.0320** | **0.2614** | **4×4 / 7×7 / 20×20** |
| stride8_B | 8 | 128 | 1,028,168 | 0.1853 | 1.5725 | 4×4 / 7×7 / 20×20 |
| stride16_A | 16 | 160 | 1,249,640 | 0.0455 | 0.3459 | 2×2 / 4×4 / 10×10 |
| stride4_A | 4 | 96 | 457,544 | 0.1576 | 1.3464 | 7×7 / 14×14 / 40×40 |

計画書 v1 は **53 px を 2×2 へ、27 px を 1×1 へ潰す**。Global Average Pooling の
平均対象が 1〜4 位置しかなく、空間情報がほぼ残らない。

### E2. 多視点 5D 入力の ONNX export は通る

`[B, V, C, H, W]` を `flatten(0,1)` → 共有 encoder → `unflatten(0,(B,V))` →
`mean(dim=1)` → head、という forward を `ml.export.OnnxExportResult.export` に
かけた（batch / view / height / width の 4 軸を dynamic 宣言）。

- `export error: None`。`verify_dynamic_dimensions` と opset 照合も通過
- graph の入力軸: `images = ('batch','view','6','height','width')`、
    `valid_pixel_mask = ('batch','view','1','height','width')`
- ONNX Runtime で `(B,V,S) = (1,5,53) / (2,3,27) / (1,1,159)` を実行し、いずれも
    `(B,1)` を返した。**V=5 の example から export した graph が V=1 で動く**

torch は `view`/`height`/`width` について「同じ shape 制約を共有する軸なので symbol 名を
使わない」という `UserWarning` を出すが、graph 側の `dim_param` は保存されている。

### E3. `valid_pixel_mask` は「常に None」にならない

`ImageConstraints(minimum_size=16, maximum_size=512, maximum_pixels=262_144, stride=8)`
で 53×53 を前処理した実測。

- 回転 augmentation で有効画素率は 0°:1.000 / 15°:0.900 / 45°:0.829 / 90°:1.000
- **均一サイズ 53 px でも `PaddedBatch.pad` が stride 切り上げで padding を作る**。
    stride 32 → 64×64（無駄 31.4%）、stride 8 → 56×56（10.4%）、stride 1 → 53×53（0%）。
    stride 8 のとき `valid_pixel_masks.all()` は False

### E4. 均一サイズは bucket を 1 個へ潰す

`BatchShape` 10 件すべて 53×53 で `plan_pixel_budget_batches` を呼ぶと batch は 1 個。
`_bucket_key` の aspect 項と area 項が観測できなくなる。

### E5. ReLU の「出力が厳密に 0」と「勾配 0」は同値

`Linear(bias=-2.0)` で全 sample を負の前活性に落として実測。

- ReLU: 出力 == 0 が 64/64、そのうち前活性への勾配が 0 なのが 64/64。
    出力 > 0 側で勾配 0 は 0 件
- Softplus: 出力が厳密に 0 の件数は 0/64、最小値 2.438e-02

**`mean == 0.0` の判定だけで「その sample の平均側勾配が 0」を過不足なく取れる。**
model へ前活性を返す出口を足す必要がない。

### E6. サイズ契約と augmentation の「合成」に穴がある

`minimum_size=16` で 27 px の画像に scale 0.5 を掛けると
`前処理後の画像が minimum_size を下回ります: 13x13 < 16` で **sample が丸ごと落ちる**。
`_applied_scale` は上限側だけ `limit` で clip し、下限側は clip せずエラーにする非対称な
作りになっている。

### E7. 既存 API の粗

- `ModelSize.measure` は `example_inputs` に `None` を渡すと理由文字列ではなく
    `AttributeError` になる（`inspection.py:99`）。多視点 model は mask が
    `Tensor | None` なので、計測側は必ず実 mask を渡す必要がある
- `OnnxInferenceModel.predict` は入力名・dtype・ndim を一切検査せず ORT の例外を
    文字列化するだけ。`manifest.inputs` は predict 経路で参照されない。
    4D と 5D を取り違えても `ml` 側は気付かない

## 論点 1〜7 の判断

### 論点 1. サイズ契約の新しい既定値

`ImageConstraints` の既定値を次へ変える。

| 項目 | 現行 | 新 | 理由 |
| --- | ---: | ---: | --- |
| `minimum_size` | 32 | **16** | 53 × 0.5 = 26 を通す。16 は推奨 encoder の `total_stride` の 2 倍で、最小入力でも feature map 2×2 以上を保つ |
| `maximum_size` | 1024 | **512** | 上限 159 px の 3 倍強。誤設定した巨大 crop を早く縮める guard として意味を持つ大きさ |
| `maximum_pixels` | 262,144 | **262,144（据置）** | `maximum_size` 512 の正方形がちょうど予算に一致し、2 つの制約が矛盾しない |
| `stride` | 32 | **8** | 推奨 encoder の `total_stride` と一致。53 → 56 で padding 10.4%（stride 32 なら 31.4%、E3） |

`AugmentationRange` の既定値は `minimum_scale=0.5` / `maximum_scale=3.0` とする。
53 px 源に対する後処理サイズが 26〜159 px となり、要求範囲に一致する。

**stride を 8 にしても `PaddedBatch` の 56 px は feature map を変えない**（53 の
ceil 連鎖 27→14→7 と 56/8 = 7 が一致、E1）。stride 8 の利得は最終 feature 位置が
部分的な receptive field にならないことと、AMP の alignment であって、精度ではない。
10.4% の計算増はこの規模では無視できるので採る。

**`stride` の配線に注意。** `ImageConstraints.stride` は `image.py` の `validate()`
以外どこからも読まれていない。実際に padding を決めるのは `PaddedBatch.pad(stride=)` と
`plan_pixel_budget_batches(stride=)` の引数で、`PaddedBatch.pad` の既定値は別途 32。
既定値を 8 へそろえるだけでなく、2 つが食い違いうる構造であることをテストで固定する。

**E6 の穴を塞ぐ。** 「源のサイズ × 最小 augmentation scale」が `minimum_size` を
下回る設定は、学習中に sample を黙って捨てる。設定段階で理由を返す検証を足す
（下記 `ImageConstraints.validate_augmentation`）。

### 論点 2. 軽量 encoder の設計

**`stride8_A` を baseline とする**（E1）。

```
stem_channels=(24, 32)   stem_strides=(2, 2)
stage_channels=(48, 96)  stage_strides=(1, 2)  blocks_per_stage=(2, 2)
group_norm_groups=8      → total_stride 8 / output_features 96 / 395,048 params
```

- 27 / 53 / 159 px で feature map 4×4 / 7×7 / 20×20。全域で Global Average Pooling が
    意味を持つ
- 計画書の上限（150 万 parameter 以下、1.5 GMAC 以下）に対し params 39.5 万。
    **GMAC は 1 view あたり 0.032（53 px）/ 0.261（159 px）で、V=5 なら
    0.16 / 1.31 GMAC。159 px × 5 view が上限に最も近い**
- GroupNorm は維持（計画書の要件）。24 / 32 / 48 / 96 はすべて 8 で割り切れる

`ImageEncoderConfig` は既定値を持たない設計（`group_norm_groups` を除く）なので、
**この値は `ml` にハードコードせずドメイン側の設定として渡す**。`ml` 側の追加は
論点 1 との整合を機械検証する `validate_for_encoder` だけとする。

容量を上げるなら stage を (48, 96, 128) / strides (1, 2, 1) にする方向だが、
精度比較なしに増やさない（計画書「モデル規模」節）。Optuna の探索対象とする。

### 論点 3. 多視点の統合をどこに置くか

**新規 module `src/ml/model/multiview.py`（RUNTIME 層）に置く。**

境界の引き方:

- **`ml` が持つ**: 「view 軸を持つ 5D tensor を、共有 encoder で符号化して順序不変に
    集約し、1 個の feature に落とす」。view が何であるかは知らない
- **ドメイン（`pcbasm` / 設定）が持つ**: view 数 5、offset 1 mm、中心 + 360/n 度、
    どの画像をどの view 番号に割り当てるか、metadata v2 の読み取り

`blocks.py` へ入れないのは、あの module が「畳み込み部品」で閉じているため。集合の
集約は畳み込みではない。

**pooling は平均（`mean(dim=1)`）を採る。**

- 順序不変かつ view 数可変。V=5 で export した graph が V=1 で動くことを実測（E2）
- parameter を持たないので、V=1 のとき退化しない（softmax attention は V=1 で
    恒等になるだけなので実害はないが、利得も無い）
- 5 view は中心 + 対称 4 方向で、view 間に先験的な優劣が無い。重み付けを学ばせる
    根拠が現時点で無い

attention pooling は「crop から外れた view を下げる」用途で後から比較する候補として
残す（計画書が Normalization で採った「同じ validation split で比較する」型に従う）。
v1 で ABC を切って実装 1 個だけ置くことはしない（AGENTS.md 開発原則 2）。

**`log(pixel_per_mm)` は sample あたり 1 値のまま**。全 view が同じ pixel_per_mm と
同じ augmentation scale を共有するため。`view_count` を conditioning に入れない
（入れると平均 pooling の view 数不変性が壊れる）。

**augmentation parameter は 1 sample の全 view で共有する。** 物理的には撮像リグごと
回すことに相当し、かつ全 view の `valid_mask` が同一になるので、正規化を
`[V*C, H, W]` へ積んで既存 `sample_layer_norm` へ 1 回通せる。pre/post を別々に
標準化しないのと同じ理由で、**view 間の明るさ差を保つため view をまたいで 1 組の
mean/variance で標準化する**。

### 論点 4. `valid_pixel_mask` と `PaddedBatch`

**「常に None」にはならない**（E3）。空洞化するのは別の場所なので、そこを名指しで守る。

| 機構 | 新方式での状態 | 対応 |
| --- | --- | --- |
| 回転由来の `sample_valid_mask` | 生きている（45° で有効画素率 0.829） | 現状維持 |
| stride 切り上げ padding | 生きている（53 → 56、mask は all-true でない） | 現状維持 |
| learnable padding pixel | 生きている（上 2 つが供給する） | 現状維持 |
| **`_bucket_key` の aspect / area** | **空洞化**（均一サイズは 1 bucket、E4） | 下記 |
| `_placement` の training 分岐 | stride 8 なら spare = 3 で生きる。stride 1 にすると死ぬ | stride 8 を採る根拠の 1 つ |
| 推論経路の mask | 実際に all-true になる | mask 省略経路を足さない |

**`_bucket_key` の守り方。** 均一サイズは 1 session 内の話であって、composite manifest は
`crop_size_mm` の違う session を混ぜる（計画書「複合データセット」）。したがって
**混合サイズは production で実際に起きる**。テストは「複数 `crop_size_px` を混ぜた
composite 相当の入力で bucket が 2 個以上に割れること」を観測点にし、
`len(plan) >= 1` のような真になりやすい assert にしない。

機構そのものは削らない。`ml` はドメイン非依存で、crop 寸法が将来変わりうる。

### 論点 5. ONNX export への影響

E2 で経路は確認済み。契約として固定する点。

- 入力は `images [B, V, C, H, W]` / `valid_pixel_mask [B, V, 1, H, W]` /
    `conditioning [B, 1]`。**batch / view / height / width の 4 軸を dynamic に宣言する**
- **export の example は 4 軸すべて 2 以上にする。** `OnnxExportOptions.validate_for` は
    大きさ 1 以下の軸を dynamic 宣言できない。V=1 の example では view 軸を宣言できず、
    黙って固定される
- `crop_size_px` の 27〜159 は height / width が dynamic であれば吸収できる。
    ORT で 27 / 53 / 159 を実行済み（E2）
- `InferenceManifest.inputs` の `TensorContract.dimensions` は文字列 tuple なので
    `("batch", "view", "6", "height", "width")` と書ける。表現力は足りる
- **`OnnxInferenceModel.predict` は ndim を検査しない**（E7）。4D を渡しても
    ORT のエラー文字列になるだけ。多視点化で 4D / 5D の取り違えが現実的な事故に
    なるので、predict 側の contract 照合を足すか、少なくとも「ORT が弾いた」ことを
    テストで固定する。**予算次第。本 MR の必須には含めない**（未決事項に記載）

### 論点 6. `order` による drift と blank の評価

**`order` は既存機構で足りる。** `NumericDimension(name="capture_order", values=(...))` を
`DiagnosticReport.build` へ渡せば、percentile で 4 分割された bucket ごとに
`relative_error_mean` が出る。3 パス撮影の先頭・末尾の系統差はここに現れる。
`ml` 側の追加は不要で、値を渡すのはドメインの仕事。

**blank は既存機構で足りない。** `GaussianPredictions.valid_sample_mask()` は
`target > 0` と `mean > 0` を要求する（`regression.py:67`）。したがって、

- 真値 0 の blank は**全 metric から invalid として除外される**
- ReLU で正しく 0 を当てた sample も `mean > 0` に反して除外される
- `CategoricalDimension("is_blank", ...)` で slice を切っても、blank 側の
    `GaussianRegressionMetrics.measure` は「有効 sample が無い」理由を返すだけになる

relative error は真値 0 で定義できないので、blank を既存 metric へ混ぜるのは誤り。
**`valid_sample_mask` の `target > 0` 契約は変えない**（変えると promotion gate の
`relative_error_score` の意味が変わり、MR5 / MR6 のテストが広範に落ちる）。

代わりに `ml.evaluation.regression` へ 2 つの frozen class を足し、
`DiagnosticReport` から参照する。

- `ZeroTargetMetrics`: 真値 0 の部分集団を絶対誤差で測る（相対誤差を使わない）
- `MeanSaturationDiagnostic`: 論点で要求された「死んだ領域に落ちた sample の割合」。
    E5 より `mean == 0.0` の厳密比較で過不足なく取れるので、model 側に前活性の
    出口を足さない。真値が正の側と blank 側を**分けて数える**（blank の飽和は正常、
    正の真値の飽和が問題）

### 論点 7. 既存テストへの影響

契約として pin されている箇所と、既定値を使っていただけの箇所を分ける。

| 箇所 | 種別 | 影響 |
| --- | --- | --- |
| `tests/ml/data/test_image.py:36-37` | **契約 pin** | `(32, 1024)` と `262_144` を直接 assert。新値へ更新が必要 |
| `tests/ml/data/test_image.py:43` | 既定利用 | `minimum_size=2048` は新 `maximum_size=512` でも同じ理由を返す。落ちない |
| `tests/ml/data/test_image.py:376` | **暗黙依存** | `_image(16, 64)` が `minimum_size` 未満である前提。16 → 16 で成立しなくなる。入力を小さくする |
| `tests/ml/data/test_image.py:80,98,108` | 半 pin | `CONSTRAINTS.maximum_*` を参照するが、元画像が新上限を超えるかどうかに依存。要確認 |
| `tests/ml/data/test_batch.py:11,59` | **契約 pin** | `stride: 32` と「64x64 は stride 32 で 64x64」のコメント。既定変更に追従 |
| `tests/ml/model/test_heads.py:110-113` | **契約 pin** | `test_mean_is_positive` が `(mean > 0).all()`。**ReLU 化で落ちる** |
| `tests/ml/model/test_heads.py:190` | **契約 pin** | 同上（regressor 側） |
| `tests/ml/support.py:54` | 既定利用 | 独自 `ImageEncoderConfig`（total_stride 2）。影響なし |
| `tests/ml/export/support.py:80` | 既定利用 | 32 px 固定の tiny model。影響なし |
| `tests/ml/test_architecture.py` | 列挙 | `RUNTIME_MODULES` へ `ml.model.multiview` を追加 |

**見積り: 直接落ちるのは 5〜8 件、周辺確認が 5 件程度。** 大半は `ml.data.image` と
`ml.model.heads` に集中し、`ml.export` / `ml.training` / `ml.tuning` / `ml.artifact` は
無傷のはず（多視点 export の統合ケースを 1 本足すだけ）。

## 公開インターフェース案

### `src/ml/data/image.py`（変更）

```python
@attrs.frozen
class ImageConstraints:
    minimum_size: int = 16
    maximum_size: int = 512
    maximum_pixels: int = 262_144
    stride: int = 8

    def validate(self) -> str | None: ...                       # 既存、変更なし

    def validate_augmentation(
        self, augmentation: AugmentationRange, *, smallest_source_size: int
    ) -> str | None: ...

    def validate_for_encoder(self, encoder: ImageEncoderConfig) -> str | None: ...


@attrs.frozen
class AugmentationRange:
    rotation_enabled: bool = True
    minimum_scale: float = 0.5
    maximum_scale: float = 3.0
    # validate / parameters_for は変更なし


@attrs.frozen(eq=False)
class PreprocessedMultiViewSample:
    images: Tensor        # [V, C, H, W] float32、全 view をまたいで 1 組の統計で標準化
    valid_mask: Tensor    # [1, H, W] bool、全 view 共通
    scale: float

    @property
    def view_count(self) -> int: ...

    @classmethod
    def preprocess(
        cls,
        views: Sequence[Sequence[Tensor]],
        *,
        constraints: ImageConstraints,
        parameters: AugmentationParameters,
        eps: float = 1e-5,
    ) -> tuple[PreprocessedMultiViewSample | None, str | None]: ...
```

`views[v]` は view `v` の画像列（pre, post）で `uint8 [3, H, W]`。全 view・全画像が
同じ高さ・幅であることを要求する。`parameters` は全 view に同じものを適用する。

`validate_augmentation` は
`floor(smallest_source_size * augmentation.minimum_scale) < minimum_size` のとき理由を返す。

`validate_for_encoder` は `minimum_size < encoder.total_stride` のとき理由を返す
（feature map が 1×1 未満になる設定を弾く）。`ImageEncoderConfig` を `ml.data.image` へ
import する向きになるが、両者とも RUNTIME 層で循環しない。

### `src/ml/data/batch.py`（変更）

```python
@attrs.frozen
class BatchShape:
    sample_id: str
    height: int
    width: int
    view_count: int = 1


@attrs.frozen(eq=False)
class MultiViewPaddedBatch:
    images: Tensor              # [B, V, C, H, W]
    valid_pixel_masks: Tensor   # [B, V, 1, H, W]

    @classmethod
    def pad(
        cls,
        images: Sequence[Tensor],        # 各 [V, C, H, W]
        valid_masks: Sequence[Tensor],   # 各 [1, H, W]（view 共通）
        *,
        placement_seeds: Sequence[int],
        training: bool = False,
        stride: int = 8,
    ) -> MultiViewPaddedBatch: ...


def plan_pixel_budget_batches(
    shapes: Sequence[BatchShape],
    *,
    max_batch_pixels: int,
    max_batch_size: int,
    stride: int,
    seed: int,
    epoch: int,
) -> tuple[tuple[str, ...], ...]: ...        # シグネチャ不変、コストと bucket key が変わる
```

- `PaddedBatch.pad` の既定 `stride` を 32 → 8 へ変更（単視点経路は残す）
- `_bucket_key` に `view_count` を加える。**batch 内の view 数を均一にするため**
    （view 軸の padding と view mask を持ち込まない）
- コストを `len(candidate) * view_count * ceil(H) * ceil(W)` にする。
    view 数を無視すると pixel budget が V 倍過小評価になる
- `MultiViewPaddedBatch.pad` は view 数が不揃いなら `ValueError`
    （呼び出し側の invariant 違反。既存 `PaddedBatch.pad` の作法に合わせる）
- mask は `[1, H, W]` を受け、内部で `[B, V, 1, H, W]` へ expand する

### `src/ml/model/multiview.py`（新規、RUNTIME 層）

```python
class MultiViewImageEncoder(nn.Module):
    def __init__(self, encoder: ImageEncoder) -> None: ...

    @property
    def encoder(self) -> ImageEncoder: ...

    @property
    def output_features(self) -> int: ...

    @override
    def forward(
        self, images: Tensor, valid_pixel_mask: Tensor | None = None
    ) -> Tensor: ...
        # images [B, V, C, H, W] / mask [B, V, 1, H, W] -> [B, output_features]


class MultiViewGaussianRegressor(nn.Module):
    def __init__(
        self, encoder: MultiViewImageEncoder, head: GaussianRegressionHead
    ) -> None: ...

    @override
    def forward(
        self,
        images: Tensor,
        valid_pixel_mask: Tensor | None = None,
        conditioning: Tensor | None = None,
    ) -> tuple[Tensor, Tensor]: ...
```

`forward` は `flatten(0, 1)` → `ImageEncoder` → `unflatten(0, (B, V))` →
`mean(dim=1)` の順。**各軸へ `int()` を掛けない**（非 strict export が SymInt を
example の値へ落とし、`dynamic_shapes` の宣言が黙って無視される。`blocks.py:333` と
同じ罠）。`B` と `V` は `images.shape[0]` / `images.shape[1]` のまま使う。

入力検査は `ndim != 5`、channel 数不一致、mask の dtype と shape。既存
`ImageEncoder._reject_invalid_inputs` と重複する検査は足さない（flatten 後に
そちらが働く）。

### `src/ml/model/heads.py`（変更）

```python
class GaussianRegressionHead(nn.Module):
    # __init__ 内: self._mean_activation = nn.ReLU()   （Softplus から変更）
```

シグネチャは不変。docstring の「平均は Softplus で常に非負にする」を
「平均は ReLU で非負にする。真値 0 の sample を厳密に表現するため」へ書き換える。

### `src/ml/evaluation/regression.py`（追加）

```python
@attrs.frozen
class ZeroTargetMetrics:
    """真値 0 の部分集団を絶対誤差で測った結果."""

    sample_count: int
    weight_sum: float
    mean_absolute_error: float
    p95_absolute_error: float
    exact_zero_fraction: float
    one_standard_deviation_coverage: float

    @classmethod
    def measure(
        cls, predictions: GaussianPredictions
    ) -> tuple[ZeroTargetMetrics | None, str | None]: ...


@attrs.frozen
class MeanSaturationDiagnostic:
    """平均 head の ReLU が 0 に張り付いた sample の割合."""

    positive_target_count: int
    saturated_positive_count: int
    saturated_positive_fraction: float
    zero_target_count: int
    saturated_zero_count: int
    saturated_zero_fraction: float

    @classmethod
    def measure(cls, predictions: GaussianPredictions) -> MeanSaturationDiagnostic: ...
```

- 対象は `target == 0.0`（`ZeroTargetMetrics`）と `mean == 0.0`
    （`MeanSaturationDiagnostic`）の厳密比較。E5 より ReLU の出力 0 と勾配 0 は同値
- `MeanSaturationDiagnostic.measure` は数え上げだけで失敗しないので `str | None` を
    返さない。件数 0 のときの割合は 0.0 とする
- `saturated_zero_fraction` が高いのは**正常**（blank を正しく 0 と予測している）。
    `saturated_positive_fraction` が高いのが問題。2 つを混ぜて 1 つの割合にしない

### `src/ml/evaluation/slices.py`（変更）

```python
@attrs.frozen
class DiagnosticReport:
    overall: GaussianRegressionMetrics
    slices: tuple[DiagnosticSlice, ...]
    reliability_bins: tuple[ReliabilityBin, ...]
    zero_target: ZeroTargetMetrics | None            # 追加
    mean_saturation: MeanSaturationDiagnostic        # 追加

    @classmethod
    def build(
        cls,
        predictions: GaussianPredictions,
        *,
        dimensions: Sequence[SliceDimension],
        reliability_bin_count: int = 5,
    ) -> tuple[DiagnosticReport | None, str | None]: ...   # シグネチャ不変
```

`zero_target` は真値 0 の sample が 1 件も無ければ `None`。

## 実装ステップと commit 境界

`spec-test-author` と `plan-implementer` は上のシグネチャで並列起動できる。
ステップ 1 / 5 / 6 と 2 / 3 / 4 は書き込み範囲が重ならないので並列実装できる
（1 と 2 だけ `ml/data/image.py` を共有するので逐次）。

| # | commit | 変更範囲 | 依存 |
| --- | --- | --- | --- |
| 1 | `refactor(ml): 画像サイズ契約を点塗布 crop の寸法へそろえる` | `ml/data/image.py`（既定値 + 2 検証）、`tests/ml/data/test_image.py` | なし |
| 2 | `feat(ml): 多視点 sample の前処理を追加する` | `ml/data/image.py`（`PreprocessedMultiViewSample`）、同テスト | 1 |
| 3 | `feat(ml): batch 計画と padding を view 軸へ広げる` | `ml/data/batch.py`、`tests/ml/data/test_batch.py` | 1 |
| 4 | `feat(ml): 共有 encoder と view pooling の多視点 model を追加する` | `ml/model/multiview.py`（新規）、`tests/ml/model/test_multiview.py`（新規）、`tests/ml/test_architecture.py` | なし |
| 5 | `refactor(ml): 平均 head の活性化を ReLU へ変える` | `ml/model/heads.py`、`tests/ml/model/test_heads.py` | なし |
| 6 | `feat(ml): 真値 0 と平均飽和の診断を追加する` | `ml/evaluation/regression.py`、`ml/evaluation/slices.py`、両テスト | 5 |
| 7 | `test(ml): 多視点 model の ONNX export と ORT 実行を固定する` | `tests/ml/export/test_integration.py`、`tests/ml/export/support.py` | 3, 4 |

各 commit の前に `make ml-docker-check`。ステップ 1 と 5 は既存テストを落とすので、
同じ commit の中でテスト側を直す。

## テスト観点と「テスト ↔ 潰す機構」の対応表

MR6 の知見（`error is not None` だけを見るテストは前段を消しても緑、3rd-party が
最後の砦の経路は入口検査を消しても緑、object 単位の parametrize は分岐数だけ穴を残す、
値の範囲だけを見るテストは確率的に嘘をつく）を反映する。

### 共通の書き方

- `validate()` 系の異常系は **1 分岐 1 ケース**の parametrize。1 ケースで 1 分岐だけが
    発火する入力を選ぶ
- `error is not None` で止めず、**理由文の識別できる部分**（数値・軸名・
    どちらの制約か）まで assert する
- **「`ml` が弾いたのか torch / ORT が弾いたのか」を区別する。** 多視点の shape 検査は
    torch も同じ入力を拒否しうるので、理由文が `ml` のものであることを確かめる
- 観測点は決定的なものを選ぶ。`mean == 0.0` / feature map の shape / bucket の個数は
    決定的。「誤差が小さい」「値が範囲内」は選ばない

### 対応表

| # | テスト観点 | 潰す機構 | 観測点（決定的なもの） |
| --- | --- | --- | --- |
| T1 | 正常系: 26 px（53 × 0.5）が前処理を通る | `minimum_size` を 32 へ戻す変異 | 出力 shape `(6, 26, 26)`、error が None |
| T2 | 正常系: 159 px（53 × 3.0）が縮小されずに通る | `maximum_scale` を 1.2 へ戻す変異、`maximum_size` を 128 未満にする変異 | 出力 shape `(6, 159, 159)`、`scale == 3.0` |
| T3 | 異常系: `minimum_size < 1` | `validate` の該当 1 分岐 | 理由文に `minimum_size` と実値 |
| T4 | 異常系: `maximum_size < minimum_size` | 同上の別分岐 | 理由文に `maximum_size` |
| T5 | 異常系: `maximum_pixels < minimum_size**2` | 同上 | 理由文に `maximum_pixels` |
| T6 | 異常系: `stride < 1` | 同上 | 理由文に `stride` |
| T7 | `validate_augmentation`: 27 px 源 × scale 0.5 が理由を返す | 合成ガードの欠落（E6 の穴） | 理由文に `13` と `16` の両方 |
| T8 | `validate_augmentation`: 53 px 源 × scale 0.5 は None | ガードが常に理由を返す変異 | 戻り値が None |
| T9 | `validate_for_encoder`: `total_stride` 32 の encoder を弾く | 検査の欠落 | 理由文に `32` と `16` |
| T10 | 多視点前処理: V 個の view が同じ `valid_mask` を共有する | view ごとに別 parameter を引く変異 | `torch.equal` で mask 同士が一致 |
| T11 | 多視点前処理: view をまたいで 1 組の統計で標準化する | view 別標準化への変異 | 「view 0 だけ 2 倍明るい入力」で正規化後の view 間平均差が 0 でない（view 別なら 0 になる） |
| T12 | 多視点前処理: view のサイズ不一致が理由を返す | 検査の欠落 | 理由文に両方の shape |
| T13 | 多視点前処理: 空の view 列が理由を返す | 同上 | 理由文に「1 個以上」 |
| T14 | `_bucket_key`: **crop_size_px 混在で bucket が 2 個以上に割れる** | aspect / area 項の削除（E4 で空洞化する箇所） | `len(plan) >= 2` かつ 各 batch 内のサイズが均一 |
| T15 | `_bucket_key`: view 数が違う sample は同じ batch に入らない | view_count を key から外す変異 | 各 batch 内の `view_count` が 1 種類 |
| T16 | pixel budget が view 数を掛けて評価される | コストから `view_count` を落とす変異 | V=5 の 10 件が V=1 のときより多い batch 数に割れる |
| T17 | `MultiViewPaddedBatch.pad`: 出力が `[B, V, C, H, W]` / `[B, V, 1, H, W]` | rank の取り違え | `tuple(shape)` の完全一致 |
| T18 | `MultiViewPaddedBatch.pad`: view 数不揃いで `ValueError` | 検査の欠落 | 例外型と文言 |
| T19 | `MultiViewPaddedBatch.pad`: 均一 53 px + stride 8 で 56 px へ padding され mask が all-true でない | stride 既定の巻き戻し（E3） | padded shape `(B, V, C, 56, 56)`、`valid_pixel_masks.all()` が False |
| T20 | 多視点 encoder: view 順を入れ替えても出力が一致する | mean を「先頭 view を取る」等へ変える変異 | `torch.allclose` ではなく **permutation 前後の差の最大値が 0 に十分近い**ことを固定 tolerance で |
| T21 | 多視点 encoder: V=1 と V=5（同一画像を 5 枚）で出力が一致する | 平均でなく総和にする変異 | 同上 |
| T22 | 多視点 encoder: 4D 入力を `ml` 側の理由で拒否する | 入口検査の欠落（torch も別の理由で落ちるので**理由文まで見る**） | 例外文言が `ml` のもの（`[B, V, C, H, W]` を含む） |
| T23 | 多視点 encoder: `padding_pixel` に勾配が流れる | learnable padding の配線切れ | `padding_pixel.grad` が None でなく非零 |
| T24 | head: 前活性が負の入力で `mean` が**厳密に 0.0** | Softplus への巻き戻し（E5 で Softplus は最小 2.4e-2） | `mean == 0.0` の厳密比較 |
| T25 | head: 前活性が正の入力で `mean` が前活性と一致する | ReLU を別活性へ変える変異 | `torch.equal(mean, pre_activation)` |
| T26 | head: `log_variance` の clamp は ReLU 化で変わらない | 巻き添え変更 | 上下限での厳密一致 |
| T27 | `MeanSaturationDiagnostic`: 真値正で mean 0 の件数を数える | 真値による切り分けの欠落 | 既知の構成で `saturated_positive_count` が厳密一致 |
| T28 | `MeanSaturationDiagnostic`: blank の飽和を positive 側へ混ぜない | 2 つの母集団を合算する変異 | `saturated_zero_count` と `saturated_positive_count` が別々に厳密一致 |
| T29 | `MeanSaturationDiagnostic`: 該当 0 件で割合が 0.0（0 除算しない） | 分母 guard の欠落 | 例外にならず 0.0 |
| T30 | `ZeroTargetMetrics`: 真値 0 の sample だけを拾う | 母集団の取り違え | `sample_count` が厳密一致 |
| T31 | `ZeroTargetMetrics`: 相対誤差を計算しない（0 除算・inf を出さない） | 既存 metric の流用 | 全 field が有限 |
| T32 | `DiagnosticReport.build`: 真値 0 が 0 件なら `zero_target` が None | 空集団で誤った metric を作る変異 | None |
| T33 | `DiagnosticReport.build`: blank 混在でも `overall` は従来どおり真値正だけを使う | `valid_sample_mask` の契約変更 | `overall.invalid_sample_count` が blank 件数と一致 |
| T34 | `NumericDimension` で `order` を slice すると bucket が分かれる（`ml` 変更なしの確認） | 既存機構の回帰 | slice の `dimension` 名と bucket 数 |
| T35 | 多視点 model の ONNX export で 4 軸が `dim_param` として残る | `dynamic_shapes` が黙って無視される回帰（E2） | `summary.inputs` の `dimensions` が `('batch','view','6','height','width')` に厳密一致 |
| T36 | export した graph が ORT で V=1 / V=5、S=27 / 159 で動く | view 軸・空間軸の固定化 | 出力 shape が `(B, 1)` |
| T37 | export の example に V=1 を渡すと `ml` が理由を返す | `validate_for` の該当分岐 | 理由文に `大きさ 1 の軸` と軸番号 |
| T38 | `ml.model.multiview` が RUNTIME 層に属する | 層の逸脱 | `test_architecture.py` の既存機構 |

### 意図的に置かないテスト

- 到達不能に見える伝播 guard を「到達しない」と決め打ちしない。**MR6 では 3 件が実測で
    覆った。** 実測してから判断する
- `mean` が「小さい」「非負」といった範囲だけの assert は置かない。ReLU の観測点は
    厳密 0 一択

## リスクと未決事項

### R1. augmentation scale 0.5〜3.0 は攻撃的

見かけの大きさが体積と直結する回帰課題で 6 倍の scale 範囲を振る。`log(pixel_per_mm)` を
conditioning に入れているので原理的には補償できるが、学習が難しくなる可能性がある。
`AugmentationRange` の既定値は Hydra config で上書きできるので、Optuna の探索対象に
含める。既定値そのものを変えるのは学習結果を見てから。

### R2. 159 px × 5 view が GMAC 予算の端にある

`stride8_A` で 1.31 GMAC（E1）。計画書の上限 1.5 GMAC に対して余裕が薄い。
運用上の入力が 53 px であれば 0.16 GMAC で問題にならないが、
**Raspberry Pi 5 の推論時間は実機で測るまで確定しない**。

### R3. 学習と推論の view 数が食い違う可能性

収集は既定 5 view だが、塗布フロー中の推論で 5 view 撮るとは限らない（要件書
「パージ対象を除く最初の n 個を撮影・推論」）。平均 pooling は V 可変に耐え、V=1 でも
動くことを実測した（E2）が、**V=5 で学習した model を V=1 で使うと feature の分布が
ずれる**。view dropout を augmentation に入れれば緩和できるが、要求外なので v1 では
入れない。→ 確認事項 2。

### R4. `stride` の二重管理

`ImageConstraints.stride` と `PaddedBatch.pad(stride=)` / `plan_pixel_budget_batches` が
別々の値を取りうる。既定値をそろえても、ドメイン側が片方だけ変えれば黙ってずれる。
本 MR では既定値をそろえてテストで固定するにとどめ、配線の一本化は別 MR とする。

### R5. `OnnxInferenceModel.predict` の無検査

E7 のとおり ndim も dtype も見ない。多視点化で 4D / 5D の取り違えが起きやすくなる。
本 MR の必須には含めないが、`manifest.inputs` との照合を足す価値は上がった。

### R6. 理由文の部分一致 assert がさらに増える

MR6 の申し送りどおり、`str | None` 規約では理由の種類を型で表せないため、
T3〜T9 / T12 / T22 / T37 はいずれも文面に結び付く。理由コード（`Literal`）と
人間向け文面の分離は引き続き別 MR の候補。

### docformatter の落とし穴（実装者へ）

**書き換えが起きると、無関係な位置の日本語文字が化ける**（`実` → `殟`、`内` → `憅`）。
防御は「書き換えさせない」ことだけ。

- summary は折り返しが起きない長さに保つ（`--wrap-summaries=79`、判定は `len()`）
- 説明部は 1 文 1 段落
- summary を小文字の識別子・英単語で始めない

新規 `ml/model/multiview.py` と追加 class の docstring は書き下ろしになるので、
書いた直後に `make format` を通し、diff に化けが無いことを確認してから次へ進む。

## 確認事項

### 1. `AugmentationRange` の既定を 0.5〜3.0 にしてよいか

**暫定案: する。** ユーザーの「1/2 〜 2 倍、3 倍にも対応したい（27 〜 159 px）」は
53 px 源に対する post-augmentation 範囲と一致するので、そのまま既定にした。
ただし R1 のとおり回帰課題としては攻撃的で、`ImageConstraints` の下限緩和だけ
先に入れて augmentation は 0.5〜2.0 に留める選択もある。
**「27〜159 px を入力として受け付けたい」のか「augmentation でその範囲まで振りたい」の
どちらだったか**で判断が変わる。

### 2. 塗布フロー中の推論は何 view 撮るか

**暫定案: 学習・推論とも 5 view を前提に組み、V 可変性は model 側の性質として
確保するだけにとどめる。** R3 のとおり、V=5 で学習して V=1 で推論すると分布が
ずれる。推論を 1 view で回す運用なら、view dropout augmentation を v1 に入れる
判断が要る（本計画には含めていない）。

### 3. `ml.export` / `ml.model.inspection` の粗（E7）を本 MR で直すか

**暫定案: 直さない。** `OnnxInferenceModel.predict` の contract 照合追加と
`ModelSize.measure` の `None` 耐性は、いずれも多視点化で踏みやすくなるが
本 MR の要件ではない。別 MR に切る前提で記録だけ残す。

## 参照

- MR !207: `refactor/2026-09-08/paste-dataset-dot-core`。
    schema v2 の DTO は `src/pcbasm/pasting/dataset/metadata.py`
- 要件: `docs/image-based-dispense-calibration.md`（view は 185-197 行、240-251 行）
- 計画書: `docs/image-based-dispense-calibration-ml-plan.md`
    （画像サイズ 231 行、v1 encoder 349 行、Normalization 384 行、規模 414 行）
- 変異実験の知見: `memory/agents/plan-implementer/ml-core-6-export.md` の結論部
- 規約: `AGENTS.md` / `CLAUDE.md`、skill `refactor-conventions`、`testing-strategy`
- 層の定義: `tests/ml/test_architecture.py` の
    `DEPENDENCY_FREE_MODULES` / `RUNTIME_MODULES` / `INFERENCE_ONLY_MODULES`
