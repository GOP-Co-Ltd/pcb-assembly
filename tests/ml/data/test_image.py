"""画像前処理（サイズ制約・augmentation・SampleLayerNorm）の公開契約."""

from pathlib import Path

import pytest
import torch
from torch.nn import functional
from torchvision.io import write_png

from ml.data.image import (
    NO_AUGMENTATION,
    AugmentationParameters,
    AugmentationRange,
    ImageConstraints,
    ImageShape,
    PreprocessedMultiViewSample,
    PreprocessedSample,
    decode_rgb_image,
    sample_layer_norm,
)

CONSTRAINTS = ImageConstraints()

# 点塗布 crop の想定入力。1 mm 角 crop を 27 / 53 / 159 px で撮る
SMALLEST_CROP_SIZE = 27
NOMINAL_CROP_SIZE = 53
LARGEST_CROP_SIZE = 159


def _image(height: int, width: int, *, seed: int = 0) -> torch.Tensor:
    generator = torch.Generator().manual_seed(seed)
    return torch.randint(
        0, 256, (3, height, width), generator=generator, dtype=torch.uint8
    )


class TestImageConstraints:
    """V1 encoder が受け取れる画像サイズの宣言."""

    def test_defaults_match_the_documented_contract(self):
        """既定値は点塗布 crop の 27〜159 px を素通しできる範囲へそろえる.

        ``minimum_size`` 16 は推奨 encoder の ``total_stride`` 8 の 2 倍で、
        53 px を scale 0.5 へ振った 26 px を通す。``stride`` 8 は
        ``PaddedBatch.pad`` の既定と一致させる。
        """

        assert CONSTRAINTS.validate() is None
        assert (CONSTRAINTS.minimum_size, CONSTRAINTS.maximum_size) == (16, 512)
        assert CONSTRAINTS.maximum_pixels == 262_144
        assert CONSTRAINTS.stride == 8

    @pytest.mark.parametrize(
        ("constraints", "expected"),
        [
            (ImageConstraints(minimum_size=0), "minimum_size は正の整数が必要です: 0"),
            (
                ImageConstraints(maximum_size=8),
                "maximum_size は minimum_size 以上が必要です: 8",
            ),
            (
                ImageConstraints(maximum_pixels=100),
                "maximum_pixels が小さすぎます: 100",
            ),
            (ImageConstraints(stride=0), "stride は正の整数が必要です: 0"),
        ],
    )
    def test_rejects_inconsistent_constraints(
        self, constraints: ImageConstraints, expected: str
    ):
        """1 ケースが 1 分岐だけを発火させ、理由文は実値まで一致させる.

        既定値が新しくなったので、どの入力がどの分岐へ落ちるかも変わる。
        ``maximum_size=8`` は ``maximum_pixels`` 分岐へ落ちない値を選んである。
        """

        assert constraints.validate() == expected


class TestImageConstraintsValidateAugmentation:
    """源のサイズと augmentation の縮小率を合成して検証する.

    ``minimum_size`` を満たす設定でも、``minimum_scale`` を掛けた後の大きさが
    下限を割ると、学習中に sample が丸ごと落ちる。設定の段階で弾く。
    """

    def test_rejects_a_range_that_shrinks_the_smallest_source_below_the_minimum(self):
        """27 px 源に scale 0.5 を掛けると 13 px となり下限 16 を割る."""

        error = CONSTRAINTS.validate_augmentation(
            AugmentationRange(minimum_scale=0.5, maximum_scale=2.0),
            smallest_source_size=SMALLEST_CROP_SIZE,
        )

        assert error is not None
        assert "13" in error
        assert "16" in error

    def test_accepts_a_range_that_keeps_the_smallest_source_above_the_minimum(self):
        """53 px 源に scale 0.5 を掛けた 26 px は下限を満たす."""

        assert (
            CONSTRAINTS.validate_augmentation(
                AugmentationRange(minimum_scale=0.5, maximum_scale=2.0),
                smallest_source_size=NOMINAL_CROP_SIZE,
            )
            is None
        )

    @pytest.mark.parametrize(
        ("smallest_source_size", "accepted"), [(32, True), (31, False)]
    )
    def test_the_boundary_is_the_floor_of_the_scaled_size(
        self, smallest_source_size: int, accepted: bool
    ):
        """境界は ``floor(源 * minimum_scale) < minimum_size``.

        32 px は 16 px ちょうどで通り、31 px は 15 px になって落ちる。
        """

        error = CONSTRAINTS.validate_augmentation(
            AugmentationRange(minimum_scale=0.5, maximum_scale=2.0),
            smallest_source_size=smallest_source_size,
        )

        assert (error is None) is accepted

    def test_reports_an_unusable_augmentation_range_before_scaling(self):
        """壊れた ``AugmentationRange`` は範囲側の理由をそのまま返す.

        非有限な ``minimum_scale`` を ``math.floor`` へ渡すと例外になる。

        設定 file 由来の未検証な範囲が届く経路があるので、先に委譲する。
        """

        error = CONSTRAINTS.validate_augmentation(
            AugmentationRange(minimum_scale=float("nan")),
            smallest_source_size=NOMINAL_CROP_SIZE,
        )

        assert error == "minimum_scale は正の有限値が必要です: nan"

    def test_rejects_a_range_that_clips_the_smallest_source_at_the_maximum(self):
        """最小の源すら最大 scale で maximum_size に当たる設定は弾く.

        上限側は sample を落とさず黙って clip される。

        設定した振り幅がそのまま出ないことに気付けない。
        """

        error = CONSTRAINTS.validate_augmentation(
            AugmentationRange(minimum_scale=1.0, maximum_scale=20.0),
            smallest_source_size=NOMINAL_CROP_SIZE,
        )

        assert error is not None
        assert "1060" in error
        assert "512" in error

    @pytest.mark.parametrize(
        ("scaled_size", "accepted"), [(512.0, True), (512.5, True), (513.0, False)]
    )
    def test_the_upper_boundary_is_the_floor_of_the_scaled_size(
        self, scaled_size: float, accepted: bool
    ):
        """境界は ``floor(源 * maximum_scale) > maximum_size``.

        53 px 源では 512 px ちょうどまで通り、513 px から落ちる。

        512.5 px は floor で 512 px になるので通る。``ceil`` へ変える変異は
        この 1 ケースだけが捕まえる。
        """

        error = CONSTRAINTS.validate_augmentation(
            AugmentationRange(
                minimum_scale=1.0, maximum_scale=scaled_size / NOMINAL_CROP_SIZE
            ),
            smallest_source_size=NOMINAL_CROP_SIZE,
        )

        assert (error is None) is accepted


class TestImageShapePreprocessed:
    """縮小後の高さ・幅."""

    def test_does_not_upscale_a_small_image(self):
        shape = ImageShape(64, 64).preprocessed(
            constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert shape == ImageShape(64, 64)

    def test_is_limited_by_the_longest_side(self):
        shape = ImageShape(64, 4096).preprocessed(
            constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert shape == ImageShape(8, 512)

    def test_is_limited_by_the_pixel_budget(self):
        """面積の予算が最大辺より先に効く設定で、予算側の縮小を観測する.

        既定値では ``maximum_size`` の正方形が ``maximum_pixels`` にちょうど
        一致するので、既定のままでは面積側が単独で効く入力が存在しない。
        機構を観測できる制約を明示して渡す。
        """

        constraints = ImageConstraints(maximum_size=512, maximum_pixels=65_536)

        shape = ImageShape(512, 512).preprocessed(
            constraints=constraints, parameters=NO_AUGMENTATION
        )

        assert shape == ImageShape(256, 256)
        assert shape.pixels == constraints.maximum_pixels

    def test_the_default_side_and_pixel_limits_agree_on_a_square(self):
        """既定値では 512 px 正方形が両方の上限へ同時に接する."""

        shape = ImageShape(1024, 1024).preprocessed(
            constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert shape == ImageShape(512, 512)
        assert shape.pixels == CONSTRAINTS.maximum_pixels

    def test_keeps_an_extreme_aspect_ratio(self):
        shape = ImageShape(64, 2048).preprocessed(
            constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert shape.width == 512
        assert shape.height == 16

    @pytest.mark.parametrize(
        ("source_size", "scale", "expected"),
        [
            (NOMINAL_CROP_SIZE, 0.5, 26),
            (NOMINAL_CROP_SIZE, 2.0, 106),
            (SMALLEST_CROP_SIZE, 1.0, SMALLEST_CROP_SIZE),
            (LARGEST_CROP_SIZE, 1.0, LARGEST_CROP_SIZE),
        ],
    )
    def test_covers_the_point_dispense_crop_range_without_clipping(
        self, source_size: int, scale: float, expected: int
    ):
        """27〜159 px の crop と 0.5〜2.0 の scale が上限に触れない.

        ``maximum_size`` を 159 未満へ戻す変異と、``maximum_pixels`` を
        106 px 平方より小さくする変異を、厳密なサイズ一致で捕まえる。
        """

        shape = ImageShape(source_size, source_size).preprocessed(
            constraints=CONSTRAINTS,
            parameters=AugmentationParameters(rotation_degrees=0.0, scale=scale),
        )

        assert shape == ImageShape(expected, expected)

    @pytest.mark.parametrize(
        "original", [ImageShape(512, 512), ImageShape(1024, 256), ImageShape(64, 2048)]
    )
    def test_result_satisfies_every_size_constraint(self, original: ImageShape):
        shape = original.preprocessed(
            constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert max(shape.height, shape.width) <= CONSTRAINTS.maximum_size
        assert shape.pixels <= CONSTRAINTS.maximum_pixels

    def test_augmentation_scale_is_clipped_by_the_constraints(self):
        enlarged = AugmentationParameters(rotation_degrees=0.0, scale=1.2)

        shape = ImageShape(512, 512).preprocessed(
            constraints=CONSTRAINTS, parameters=enlarged
        )

        assert shape.pixels <= CONSTRAINTS.maximum_pixels

    def test_augmentation_scale_shrinks_the_result(self):
        shrunk = AugmentationParameters(rotation_degrees=0.0, scale=0.5)

        shape = ImageShape(256, 256).preprocessed(
            constraints=CONSTRAINTS, parameters=shrunk
        )

        assert shape == ImageShape(128, 128)


class TestAugmentationRangeParametersFor:
    """Worker 数や中断再開に依存しない決定論的な変換."""

    def test_defaults_match_the_documented_contract(self):
        """既定の振り幅は 0.5〜2.0.

        入力として受け付ける 27〜159 px（3 倍幅）とは別物。

        学習時に振る幅は回帰課題として無理のない範囲へ留める。
        """

        augmentation = AugmentationRange()

        assert augmentation.validate() is None
        assert (augmentation.minimum_scale, augmentation.maximum_scale) == (0.5, 2.0)
        assert augmentation.rotation_enabled is True

    def test_is_reproducible_for_the_same_sample_and_epoch(self):
        first = AugmentationRange().parameters_for(
            sample_id="sample-a",
            global_seed=7,
            epoch=3,
        )
        second = AugmentationRange().parameters_for(
            sample_id="sample-a",
            global_seed=7,
            epoch=3,
        )

        assert first == second

    @pytest.mark.parametrize(
        ("sample_id", "epoch", "global_seed"),
        [("sample-b", 3, 7), ("sample-a", 4, 7), ("sample-a", 3, 8)],
    )
    def test_differs_when_the_derivation_inputs_differ(
        self, sample_id: str, epoch: int, global_seed: int
    ):
        baseline = AugmentationRange().parameters_for(
            sample_id="sample-a",
            global_seed=7,
            epoch=3,
        )

        other = AugmentationRange().parameters_for(
            sample_id=sample_id,
            global_seed=global_seed,
            epoch=epoch,
        )

        assert other != baseline

    def test_stays_inside_the_declared_range(self):
        augmentation = AugmentationRange(minimum_scale=0.8, maximum_scale=1.2)

        for index in range(64):
            parameters = augmentation.parameters_for(
                sample_id=f"sample-{index}",
                global_seed=1,
                epoch=0,
            )

            assert 0.0 <= parameters.rotation_degrees < 360.0
            assert 0.8 <= parameters.scale <= 1.2

    def test_disabled_rotation_and_unit_scale_produce_no_augmentation(self):
        disabled = AugmentationRange(
            rotation_enabled=False, minimum_scale=1.0, maximum_scale=1.0
        )

        parameters = disabled.parameters_for(
            sample_id="sample-a", global_seed=7, epoch=3
        )

        assert parameters == NO_AUGMENTATION

    @pytest.mark.parametrize(
        ("augmentation", "expected"),
        [
            (AugmentationRange(minimum_scale=0.0), "minimum_scale"),
            (AugmentationRange(minimum_scale=1.5, maximum_scale=1.2), "maximum_scale"),
        ],
    )
    def test_rejects_an_inconsistent_range(
        self, augmentation: AugmentationRange, expected: str
    ):
        error = augmentation.validate()

        assert error is not None
        assert expected in error


class TestDecodeRgbImage:
    """Torchvision で RGB の CHW tensor として読む契約."""

    def test_reads_channels_in_rgb_order(self, tmp_path: Path):
        source = torch.zeros((3, 4, 5), dtype=torch.uint8)
        source[0] = 10
        source[1] = 120
        source[2] = 240
        path = tmp_path / "controlled.png"
        write_png(source, str(path), compression_level=0)

        decoded = decode_rgb_image(path)

        assert decoded.shape == (3, 4, 5)
        assert decoded.dtype == torch.uint8
        assert torch.equal(decoded, source)

    def test_expands_a_grayscale_png_to_three_channels(self, tmp_path: Path):
        path = tmp_path / "gray.png"
        write_png(torch.full((1, 4, 5), 77, dtype=torch.uint8), str(path))

        decoded = decode_rgb_image(path)

        assert decoded.shape == (3, 4, 5)
        assert torch.equal(decoded, torch.full((3, 4, 5), 77, dtype=torch.uint8))


class TestSampleLayerNorm:
    """Sample 1 個の全軸で標準化する入力正規化."""

    def test_matches_torch_layer_norm_when_every_pixel_is_valid(self):
        image = torch.rand((6, 8, 9))

        normalized, error = sample_layer_norm(image)

        assert error is None
        assert normalized is not None
        expected = functional.layer_norm(image, image.shape, None, None, 1e-5)
        assert torch.allclose(normalized, expected, atol=1e-6)

    def test_uses_only_valid_pixels_when_masked(self):
        image = torch.rand((6, 8, 9))
        valid_mask = torch.zeros((1, 8, 9), dtype=torch.bool)
        valid_mask[:, :4, :] = True

        normalized, error = sample_layer_norm(image, valid_mask=valid_mask)

        assert error is None
        assert normalized is not None
        visible = image[:, :4, :]
        mean = visible.mean()
        variance = visible.var(correction=0)
        expected = (visible - mean) / torch.sqrt(variance + 1e-5)
        assert torch.allclose(normalized[:, :4, :], expected, atol=1e-6)

    def test_ignores_non_finite_values_outside_the_mask(self):
        image = torch.rand((6, 8, 9))
        image[:, 4:, :] = float("nan")
        valid_mask = torch.zeros((1, 8, 9), dtype=torch.bool)
        valid_mask[:, :4, :] = True

        normalized, error = sample_layer_norm(image, valid_mask=valid_mask)

        assert error is None
        assert normalized is not None
        assert bool(torch.isfinite(normalized).all())
        visible = image[:, :4, :]
        expected = (visible - visible.mean()) / torch.sqrt(
            visible.var(correction=0) + 1e-5
        )
        assert torch.allclose(normalized[:, :4, :], expected, atol=1e-6)

    def test_zeroes_the_invalid_region(self):
        image = torch.rand((6, 8, 9)) + 1.0
        valid_mask = torch.zeros((1, 8, 9), dtype=torch.bool)
        valid_mask[:, :4, :] = True

        normalized, _ = sample_layer_norm(image, valid_mask=valid_mask)

        assert normalized is not None
        assert torch.all(normalized[:, 4:, :] == 0.0)

    def test_keeps_the_relation_between_stacked_images(self):
        image = torch.rand((6, 8, 9))
        brightened = image.clone()
        brightened[3:] += 0.5

        first, _ = sample_layer_norm(image)
        second, _ = sample_layer_norm(brightened)

        assert first is not None
        assert second is not None
        # 前半 channel だけを触っていないのに値が動く = 全 channel 共通の統計を使っている
        assert not torch.allclose(first[:3], second[:3], atol=1e-3)

    @pytest.mark.parametrize(
        ("image", "valid_mask", "expected"),
        [
            (torch.zeros((6, 8, 9)), None, "分散が小さすぎます"),
            (
                torch.rand((6, 8, 9)),
                torch.zeros((1, 8, 9), dtype=torch.bool),
                "有効画素が 1 つもありません",
            ),
            (
                torch.full((6, 8, 9), float("nan")),
                None,
                "平均または分散が有限値になりません",
            ),
        ],
    )
    def test_rejects_samples_without_usable_statistics(
        self,
        image: torch.Tensor,
        valid_mask: torch.Tensor | None,
        expected: str,
    ):
        """使えない統計は理由文まで見て区別する.

        3 つの入力はいずれも後段の検査にも掛かる。

        ``error is not None`` だけでは前段を消した変異が素通りする。
        """

        normalized, error = sample_layer_norm(image, valid_mask=valid_mask)

        assert normalized is None
        assert error is not None
        assert expected in error

    def test_rejects_an_image_without_a_channel_axis(self):
        with pytest.raises(ValueError, match="CHW tensor"):
            sample_layer_norm(torch.rand((8, 9)))

    def test_rejects_a_valid_mask_of_the_wrong_dtype(self):
        with pytest.raises(ValueError, match=r"bool \[1, H, W\]"):
            sample_layer_norm(
                torch.rand((6, 8, 9)),
                valid_mask=torch.ones((1, 8, 9), dtype=torch.uint8),
            )


class TestPreprocessedSamplePreprocess:
    """Decode 済み画像列を model 入力へそろえる一連の処理."""

    def test_concatenates_images_along_the_channel_axis(self):
        sample, error = PreprocessedSample.preprocess(
            [_image(64, 48, seed=1), _image(64, 48, seed=2)],
            constraints=CONSTRAINTS,
            parameters=NO_AUGMENTATION,
        )

        assert error is None
        assert sample is not None
        assert sample.image.shape == (6, 64, 48)
        assert sample.image.dtype == torch.float32
        assert sample.valid_mask.shape == (1, 64, 48)
        assert bool(sample.valid_mask.all())
        assert sample.scale == pytest.approx(1.0)

    def test_accepts_a_single_image(self):
        sample, error = PreprocessedSample.preprocess(
            [_image(64, 48)], constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert error is None
        assert sample is not None
        assert sample.image.shape == (3, 64, 48)

    def test_downscales_and_reports_the_applied_scale(self):
        sample, error = PreprocessedSample.preprocess(
            [_image(128, 4096)], constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert error is None
        assert sample is not None
        assert sample.image.shape[1:] == (16, 512)
        assert sample.scale == pytest.approx(0.125)

    def test_rotation_marks_the_corners_invalid(self):
        rotated = AugmentationParameters(rotation_degrees=45.0, scale=1.0)

        sample, error = PreprocessedSample.preprocess(
            [_image(64, 64)], constraints=CONSTRAINTS, parameters=rotated
        )

        assert error is None
        assert sample is not None
        assert not bool(sample.valid_mask.all())
        assert not bool(sample.valid_mask[0, 0, 0])
        assert bool(sample.valid_mask[0, 32, 32])
        assert torch.all(sample.image[:, ~sample.valid_mask[0]] == 0.0)

    def test_is_normalized_over_the_valid_region(self):
        sample, _ = PreprocessedSample.preprocess(
            [_image(64, 48, seed=1), _image(64, 48, seed=2)],
            constraints=CONSTRAINTS,
            parameters=NO_AUGMENTATION,
        )

        assert sample is not None
        assert float(sample.image.mean()) == pytest.approx(0.0, abs=1e-5)
        assert float(sample.image.std(correction=0)) == pytest.approx(1.0, abs=1e-3)

    @pytest.mark.parametrize(
        ("images", "expected"),
        [
            ([], "画像"),
            # minimum_size が 16 になったので、下限未満の入力は 16 px より小さくする
            ([_image(8, 64)], "minimum_size"),
            ([_image(64, 64), _image(64, 48)], "同じ"),
            ([torch.rand((3, 64, 64))], "uint8"),
            ([_image(64, 64).unsqueeze(0)], "CHW"),
        ],
    )
    def test_rejects_unusable_input(self, images: list[torch.Tensor], expected: str):
        sample, error = PreprocessedSample.preprocess(
            images, constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert sample is None
        assert error is not None
        assert expected in error

    def test_rejects_the_composition_of_a_small_source_and_a_shrinking_scale(self):
        """27 px 源 × scale 0.5 は 13 px となり、sample ごと落ちる.

        ``ImageConstraints.validate_augmentation`` が設定段階で弾くべき組み合わせ
        が、実行時に何を返すかを固定する。黙って通してしまう変異を捕まえる。
        """

        sample, error = PreprocessedSample.preprocess(
            [_image(SMALLEST_CROP_SIZE, SMALLEST_CROP_SIZE)],
            constraints=CONSTRAINTS,
            parameters=AugmentationParameters(rotation_degrees=0.0, scale=0.5),
        )

        assert sample is None
        assert error is not None
        assert "13x13" in error
        assert "16" in error

    def test_rejects_an_image_that_downscales_below_the_minimum(self):
        sample, error = PreprocessedSample.preprocess(
            [_image(40, 4096)], constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert sample is None
        assert error is not None
        assert "minimum_size" in error

    def test_compares_by_identity(self):
        sample, _ = PreprocessedSample.preprocess(
            [_image(64, 48, seed=1)],
            constraints=CONSTRAINTS,
            parameters=NO_AUGMENTATION,
        )
        different_image, _ = PreprocessedSample.preprocess(
            [_image(64, 48, seed=2)],
            constraints=CONSTRAINTS,
            parameters=NO_AUGMENTATION,
        )

        assert sample is not None
        assert different_image is not None
        assert sample.scale == different_image.scale
        assert sample == sample
        assert sample != different_image
        assert len({sample, different_image}) == 2

    def test_rotates_the_image_content_not_only_the_valid_mask(self):
        """回転は画像そのものへ掛かる.

        有効画素 mask は別経路で作るので、画像側の回転を落としても mask を見る
        テストは全て通る。

        90 度回転は補間誤差が出ないので ``rot90`` と一致する。

        残る 1e-7 台の差は標準化の float32 丸めで、回転を落とす変異の差（3.4）とは
        7 桁離れている。
        """

        image = _image(40, 40, seed=3)
        plain, _ = PreprocessedSample.preprocess(
            [image],
            constraints=CONSTRAINTS,
            parameters=AugmentationParameters(rotation_degrees=0.0, scale=1.0),
        )
        turned, error = PreprocessedSample.preprocess(
            [image],
            constraints=CONSTRAINTS,
            parameters=AugmentationParameters(rotation_degrees=90.0, scale=1.0),
        )

        assert error is None
        assert plain is not None
        assert turned is not None
        assert torch.allclose(
            torch.rot90(plain.image, 1, dims=(-2, -1)), turned.image, atol=1e-6
        )


class TestPreprocessedMultiViewSamplePreprocess:
    """同一対象を複数視点から撮った 1 sample の前処理.

    幾何 augmentation を view 間で共有し、標準化も view をまたいで 1 回で行う。
    """

    def test_stacks_the_views_along_a_leading_axis(self):
        views = [
            [_image(53, 53, seed=10 * index), _image(53, 53, seed=10 * index + 1)]
            for index in range(5)
        ]

        sample, error = PreprocessedMultiViewSample.preprocess(
            views, constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert error is None
        assert sample is not None
        assert tuple(sample.images.shape) == (5, 6, 53, 53)
        assert sample.images.dtype == torch.float32
        assert tuple(sample.valid_mask.shape) == (1, 53, 53)
        assert sample.view_count == 5
        assert sample.scale == pytest.approx(1.0)

    def test_accepts_a_single_view(self):
        sample, error = PreprocessedMultiViewSample.preprocess(
            [[_image(53, 53)]], constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert error is None
        assert sample is not None
        assert tuple(sample.images.shape) == (1, 3, 53, 53)
        assert sample.view_count == 1

    def test_rotates_the_image_content_of_every_view(self):
        """回転は全 view の画像そのものへ掛かる.

        mask は 1 枚を共有するので、画像側の回転を落としても mask を見るテストは
        通る。

        単視点側と同じく ``rot90`` との一致で固定する。
        """

        views = [[_image(40, 40, seed=3)], [_image(40, 40, seed=4)]]
        plain, _ = PreprocessedMultiViewSample.preprocess(
            views,
            constraints=CONSTRAINTS,
            parameters=AugmentationParameters(rotation_degrees=0.0, scale=1.0),
        )
        turned, error = PreprocessedMultiViewSample.preprocess(
            views,
            constraints=CONSTRAINTS,
            parameters=AugmentationParameters(rotation_degrees=90.0, scale=1.0),
        )

        assert error is None
        assert plain is not None
        assert turned is not None
        assert torch.allclose(
            torch.rot90(plain.images, 1, dims=(-2, -1)), turned.images, atol=1e-6
        )

    def test_applies_the_same_geometric_transform_to_every_view(self):
        """同じ源画像を 2 view へ入れると、回転後も 2 view が完全一致する.

        view ごとに別の augmentation parameter を引く変異は、45 度回転の下で
        必ず違う画素値を生むので ``torch.equal`` が落ちる。
        """

        rotated = AugmentationParameters(rotation_degrees=45.0, scale=1.0)
        source = _image(53, 53, seed=3)

        sample, error = PreprocessedMultiViewSample.preprocess(
            [[source], [source]], constraints=CONSTRAINTS, parameters=rotated
        )

        assert error is None
        assert sample is not None
        assert torch.equal(sample.images[0], sample.images[1])

    def test_the_shared_valid_mask_matches_the_single_view_preprocessing(self):
        """View 共通 mask は、同じ parameter の単視点前処理の mask と一致する."""

        rotated = AugmentationParameters(rotation_degrees=45.0, scale=1.0)
        source = _image(53, 53, seed=4)

        sample, _ = PreprocessedMultiViewSample.preprocess(
            [[source], [_image(53, 53, seed=5)]],
            constraints=CONSTRAINTS,
            parameters=rotated,
        )
        single, _ = PreprocessedSample.preprocess(
            [source], constraints=CONSTRAINTS, parameters=rotated
        )

        assert sample is not None
        assert single is not None
        assert torch.equal(sample.valid_mask, single.valid_mask)
        assert not bool(sample.valid_mask.all())

    def test_zeroes_the_invalid_region_of_every_view(self):
        rotated = AugmentationParameters(rotation_degrees=45.0, scale=1.0)

        sample, _ = PreprocessedMultiViewSample.preprocess(
            [[_image(53, 53, seed=6)], [_image(53, 53, seed=7)]],
            constraints=CONSTRAINTS,
            parameters=rotated,
        )

        assert sample is not None
        outside = ~sample.valid_mask[0]
        assert torch.all(sample.images[:, :, outside] == 0.0)

    def test_standardizes_across_the_views_with_one_set_of_statistics(self):
        """View 0 だけ明るい入力で、正規化後も view 間の明るさ差が残る.

        View ごとに標準化すると各 view の平均は厳密に 0 へ寄るので、差は消える。

        ここでは全体平均が 0 で、view 別平均が離れていることを同時に見る。

        乱数は種で固定しているので、この 2 つの観測は実行ごとに揺れない。
        """

        base = _image(53, 53, seed=8)
        bright = torch.clamp(base.to(torch.int16) + 60, 0, 255).to(torch.uint8)

        sample, error = PreprocessedMultiViewSample.preprocess(
            [[bright], [base]], constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert error is None
        assert sample is not None
        assert float(sample.images.mean()) == pytest.approx(0.0, abs=1e-5)
        assert float(sample.images[0].mean()) - float(sample.images[1].mean()) > 0.5

    def test_reports_the_applied_scale_shared_by_every_view(self):
        views = [[_image(53, 53, seed=index)] for index in range(3)]

        sample, error = PreprocessedMultiViewSample.preprocess(
            views,
            constraints=CONSTRAINTS,
            parameters=AugmentationParameters(rotation_degrees=0.0, scale=0.5),
        )

        assert error is None
        assert sample is not None
        assert tuple(sample.images.shape) == (3, 3, 26, 26)
        assert sample.scale == pytest.approx(0.5)

    def test_rejects_an_empty_view_sequence(self):
        sample, error = PreprocessedMultiViewSample.preprocess(
            [], constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert sample is None
        assert error is not None
        assert "1 個以上" in error

    def test_rejects_a_view_without_any_image(self):
        sample, error = PreprocessedMultiViewSample.preprocess(
            [[]], constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert sample is None
        assert error is not None
        assert "画像" in error

    def test_rejects_views_whose_sizes_disagree(self):
        """View をまたいだ高さ・幅の食い違いは、両方の shape を理由文へ出す."""

        sample, error = PreprocessedMultiViewSample.preprocess(
            [[_image(53, 53)], [_image(53, 48)]],
            constraints=CONSTRAINTS,
            parameters=NO_AUGMENTATION,
        )

        assert sample is None
        assert error is not None
        assert "(3, 53, 53)" in error
        assert "(3, 53, 48)" in error

    def test_rejects_views_with_different_image_counts(self):
        """View ごとの画像枚数がそろわない入力を弾く.

        pre / post の片方だけ欠けた view を混ぜると ``[V, C, H, W]`` の C が
        view ごとに変わり、stack できない。
        """

        sample, error = PreprocessedMultiViewSample.preprocess(
            [
                [_image(53, 53, seed=1), _image(53, 53, seed=2)],
                [_image(53, 53, seed=3)],
            ],
            constraints=CONSTRAINTS,
            parameters=NO_AUGMENTATION,
        )

        assert sample is None
        assert error is not None
        assert "枚数" in error

    def test_rejects_a_composition_that_falls_below_the_minimum_size(self):
        sample, error = PreprocessedMultiViewSample.preprocess(
            [[_image(SMALLEST_CROP_SIZE, SMALLEST_CROP_SIZE)]],
            constraints=CONSTRAINTS,
            parameters=AugmentationParameters(rotation_degrees=0.0, scale=0.5),
        )

        assert sample is None
        assert error is not None
        assert "13x13" in error

    def test_compares_by_identity(self):
        views = [[_image(53, 53, seed=1)]]

        sample, _ = PreprocessedMultiViewSample.preprocess(
            views, constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )
        same_input, _ = PreprocessedMultiViewSample.preprocess(
            views, constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert sample is not None
        assert same_input is not None
        assert sample == sample
        assert sample != same_input
        assert len({sample, same_input}) == 2
