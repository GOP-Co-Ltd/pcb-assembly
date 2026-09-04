# コア ML 基盤 MR2: データパイプライン基盤

計画全体は `/home/gop/.claude/plans/mr185-codex-docs-image-based-dispense-ca-modular-sonnet.md`。
MR1 は `feature/2026-09-04/ml-core-1-package-artifact`（commit 2a9dac0）。

## 段階 1: 計画

### 公開インターフェース

```python
# ml/data/image.py  （torch / torchvision を module-level import してよい層）
type SplitName = Literal["train", "validation", "test"]   # ml/data/split.py

@attrs.frozen
class ImageShape:
    height: int
    width: int
    @property
    def pixels(self) -> int: ...

@attrs.frozen
class ImageConstraints:
    minimum_size: int = 32
    maximum_size: int = 1024
    maximum_pixels: int = 262_144
    stride: int = 32

@attrs.frozen
class AugmentationRange:
    rotation_enabled: bool = True
    minimum_scale: float = 0.8
    maximum_scale: float = 1.2

@attrs.frozen
class AugmentationParameters:
    rotation_degrees: float
    scale: float

NO_AUGMENTATION: AugmentationParameters   # rotation 0 / scale 1

def validate_image_constraints(constraints) -> str | None
def validate_augmentation_range(augmentation) -> str | None
def augmentation_parameters(*, sample_id: str, global_seed: int, epoch: int,
                            augmentation: AugmentationRange) -> AugmentationParameters
def constraint_scale(shape: ImageShape, *, constraints: ImageConstraints) -> float
def preprocessed_shape(shape: ImageShape, *, constraints: ImageConstraints,
                       parameters: AugmentationParameters) -> ImageShape
def decode_rgb_image(path: Path) -> Tensor                       # uint8 [3, H, W]
def sample_layer_norm(image: Tensor, *, valid_mask: Tensor | None = None,
                      eps: float = 1e-5) -> tuple[Tensor | None, str | None]

@attrs.frozen
class PreprocessedSample:
    image: Tensor        # float32 [C, H, W]、SampleLayerNorm 済み
    valid_mask: Tensor   # bool [1, H, W]
    scale: float         # 元画像 → 前処理後の等方 scale

def preprocess_image_stack(images: Sequence[Tensor], *, constraints, parameters,
                           eps: float = 1e-5
                           ) -> tuple[PreprocessedSample | None, str | None]

# ml/data/batch.py
@attrs.frozen
class BatchShape:
    sample_id: str
    height: int
    width: int

@attrs.frozen
class PaddedBatch:
    images: Tensor             # float32 [B, C, H, W]
    valid_pixel_masks: Tensor  # bool    [B, 1, H, W]

def plan_pixel_budget_batches(shapes: Sequence[BatchShape], *, max_batch_pixels: int,
                              max_batch_size: int, stride: int, seed: int, epoch: int
                              ) -> tuple[tuple[str, ...], ...]
def pad_image_samples(images: Sequence[Tensor], valid_masks: Sequence[Tensor], *,
                      placement_seeds: Sequence[int], training: bool = False,
                      stride: int = 32) -> PaddedBatch

# ml/data/split.py
SPLIT_MANIFEST_DOCUMENT = DocumentKind("ml-split-manifest", 1)

@attrs.frozen
class SplitRatios:
    train: float
    validation: float
    test: float

@attrs.frozen
class SplitManifest:
    dataset_fingerprint: str
    seed: int
    train_sample_ids: tuple[str, ...]
    validation_sample_ids: tuple[str, ...]
    test_sample_ids: tuple[str, ...]
    def sample_ids_for(self, split: SplitName) -> tuple[str, ...]: ...

def validate_split_ratios(ratios: SplitRatios) -> str | None
def build_split_manifest(sample_groups: Mapping[str, str], *, dataset_fingerprint: str,
                         seed: int, ratios: SplitRatios, require_test: bool
                         ) -> tuple[SplitManifest | None, str | None]
def validate_split_manifest(manifest: SplitManifest, sample_groups: Mapping[str, str],
                            *, dataset_fingerprint: str) -> str | None
def save_split_manifest(path: Path, manifest: SplitManifest) -> None
def load_split_manifest(path: Path, *, dataset_fingerprint: str
                        ) -> tuple[SplitManifest | None, str | None]

@attrs.frozen
class LeaveOneGroupOutFold:
    held_out_value: str
    held_out_group_ids: tuple[str, ...]
    train_group_ids: tuple[str, ...]
    validation_group_ids: tuple[str, ...]
    seed: int

@attrs.frozen
class LeaveOneGroupOutPlan:
    available: bool
    reason: str | None
    folds: tuple[LeaveOneGroupOutFold, ...]

def build_leave_one_group_out_plan(group_values: Mapping[str, str], *, dimension: str,
                                   seed: int, validation_ratio: float = 0.15
                                   ) -> LeaveOneGroupOutPlan
```

### テスト観点

- `sample_layer_norm`: 全画素有効なら `F.layer_norm(x, x.shape, None, None, eps)` と一致 /
  masked なら有効要素だけの参照計算と一致 / invalid 領域は 0 / 有効要素 0・分散ほぼ 0 を拒否 /
  pre と post を別々に正規化していない（片方だけ明るくすると両方の値が動く）
- サイズ制約: 最大辺と最大画素数の両方を満たす最大 scale / upscale しない / 極端な縦長を
  正方形へ歪めない / 最小辺未満は拒否 / 32 px 未満へ縮む場合も拒否
- augmentation: `(global_seed, epoch, sample_id)` から決定論的 / epoch が違えば変わる /
  sample が違えば変わる / 無効化時は回転 0・scale 1 / scale は上限制約でクリップされる
- `decode_rgb_image`: 実 PNG を round-trip して RGB 順と CHW shape が保たれる（BGR ではない）
- batch plan: 同じ seed と epoch で同一 / epoch が変われば並びが変わる / pixel budget と
  batch size を超えない / 最後の小 batch を捨てない / 全 sample がちょうど 1 回現れる /
  1 sample が budget を超えるなら失敗
- padding: stride の倍数へ切り上げ / 評価時は中央・学習時は seed 決定論の位置 /
  valid mask が実画像位置だけ真 / device と dtype の不一致を拒否
- split: group が split をまたがない / seed 固定で再現 / 全 sample を過不足なく覆う /
  fingerprint 不一致の manifest を拒否 / group が 2 種類未満なら leave-one-group-out は
  「評価不能」を理由付きで返す

### 計画からの判断

1. **`PixelBudgetBatchSampler`（`torch.utils.data.Sampler` 継承）を作らない。**
   MR4 の `TrainingData.plan_epoch()` が batch 計画を返す設計なので、必要なのは純関数
   `plan_pixel_budget_batches()` だけ。DataLoader へ渡す Sampler 実装は不要。
2. **`select_group_balanced`（group 均等サブセット選択）は MR2 に入れない。**
   利用者は MR4 の deadline サブセットと MR6 の INT8 calibration。現れてから足す。
3. **ドメイン mask（pad geometry など）の同時変換は入れない。**
   MR2 は画像列と `sample_valid_mask` だけを扱う。追加 mask が要るのは paste_volume 側なので
   そのとき拡張する。
4. **画像列は任意本数にする。** MR185 は pre/post 2 枚 → 6 channel 固定だった。
   同サイズ RGB 画像の列を channel 方向に連結する形にすれば 1 枚でも 2 枚でも扱える。
5. **テスト用 PNG は LFS 資産にせず、torchvision で書いて読み戻す。**
   `.gitattributes` が `*.png` を git-lfs 管理にしているため。実ライブラリを通す点は変わらない。

## 段階 2: テスト実装

`tests/ml/data/` に 92 テスト。red 確認は `ModuleNotFoundError: No module named 'ml.data'`。

書いたあとで矛盾に気付き 1 件直した。`test_downscales_and_reports_the_applied_scale` を
64x4096 で書いていたが、縮小後 16px となり `minimum_size` 32 を下回るので成功例にならない
（同じ矛盾を突く `test_rejects_an_image_that_downscales_below_the_minimum` を別に書いていた）。
128x4096 → 32x1024 に変えた。サイズ下限は「前処理後の画像」に掛かる、というのが doc の契約。

## 段階 3: 機能実装

- **`rotate` は tensor 入力で `nearest-exact` を受け付けない。** doc は mask に nearest-exact を
  指定しているが、これは resize の話。本実装は mask を resize 後の全 true から作るので、
  補間が効くのは回転だけであり `NEAREST` で足りる。
- 画像列は任意本数。1 枚なら 3 channel、2 枚なら 6 channel になる。

## 段階 4: リファクタリングと自己レビュー

1. **mask 外の非有限値が伝播するバグを直した。** `values * expanded` は
   `NaN * False` が `NaN` のままなので、invalid 領域に NaN があると平均も分散も NaN になり、
   「有効画素だけで統計を取る」契約が破れる。`torch.where` に置き換え、
   `test_ignores_non_finite_values_outside_the_mask` を追加した。
   現状 invalid 領域は回転の 0 埋めしか作らないので実害は出ていないが、契約としては誤り。
2. **`constraint_scale` / `applied_scale` を private 化した。** 呼び出し側が要るのは
   `preprocessed_shape()` と `PreprocessedSample.scale` だけ。private を直接テストしない規約
   （`memory/feedback_no_private_test.md`）に合わせ、テストは `preprocessed_shape` 経由へ移した。
3. `validate_split_manifest` と `load_split_manifest` に同じ fingerprint 不一致メッセージが
   2 箇所あったので `_fingerprint_mismatch()` へまとめた。

`tests/ml/test_architecture.py` に `TestRuntimeLayer` を追加した。`ml.data.*` が
MLflow / Hydra / Optuna / ONNX を読まないことを素の interpreter で検証する
（Raspberry Pi 5 へ `ml-runtime` だけ入れる運用の契約）。torch と torchvision は隠さない。

意図的に採らなかった案:

- **`_derived_seed()` の共通化。** `ml/data/image.py` と `ml/data/split.py` に同じ 2 行が
  ある。「3 度現れたら抽出」の目安に従い今回は放置する。MR4 の epoch seed 派生で 3 度目に
  なる見込みなので、そのとき `ml` 共通の再現性 module へ出す。
- **`select_group_balanced`（group 均等サブセット選択）。** 利用者が現れる MR4 / MR6 で足す。

## 段階 5: ドキュメント

`AGENTS.md` と `docs/image-based-dispense-calibration-ml-plan.md` は MR1 で `ml.data` を
含む形へ更新済みなので追加の同期は不要。module docstring は実装時に記載した。

commit 時に pre-commit が 4 file を再整形して commit が中断した。原因は
`docformatter --wrap-descriptions=72` が日本語の複数文段落を句読点直後で折り返し、
「保存し、 中断後は」のような壊れた文にすること（列数を文字数で数えるため）。
説明部を 1 文 1 段落に書き直して収束させた。この知見は auto-memory の
`docformatter-japanese-docstrings.md` へ保存した。
