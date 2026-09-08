"""共有 encoder と view pooling を持つ多視点 model の公開契約.

集約は平均なので順序不変かつ view 数可変。V=5 で書き出した ONNX graph が V=1 でも動くことまでを、この module
の契約として固定する。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast, override

import attrs
import numpy as np
import onnxruntime
import pytest
import torch
from numpy.typing import NDArray
from torch import Tensor, nn

from ml.export.onnx_export import (
    DynamicDimension,
    OnnxExportOptions,
    OnnxExportResult,
)
from ml.model.blocks import ImageEncoder, ImageEncoderConfig
from ml.model.heads import GaussianHeadConfig, GaussianRegressionHead
from ml.model.multiview import MultiViewGaussianRegressor, MultiViewImageEncoder

IMAGE_CHANNELS = 6

ENCODER_CONFIG = ImageEncoderConfig(
    input_channels=IMAGE_CHANNELS,
    stem_channels=(8,),
    stem_strides=(2,),
    stage_channels=(8, 16),
    stage_strides=(1, 2),
    blocks_per_stage=(1, 1),
    group_norm_groups=8,
)

HEAD_CONFIG = GaussianHeadConfig(
    input_features=ENCODER_CONFIG.output_features,
    conditioning_features=1,
    hidden_features=8,
)

IMAGES_INPUT = "images"
CONDITIONING_INPUT = "conditioning"
MEAN_OUTPUT = "predicted_mean"
LOG_VARIANCE_OUTPUT = "predicted_log_variance"

BATCH_SYMBOL = "batch"
VIEW_SYMBOL = "view"
HEIGHT_SYMBOL = "height"
WIDTH_SYMBOL = "width"

# batch 軸は images と conditioning の両方へ宣言する。head が両者の batch size を
# 突き合わせるので、片方だけ宣言すると torch の tracing が batch を静的に確定させ、
# export そのものが「user 指定と推論結果が衝突する」として失敗する。
EXPORT_OPTIONS = OnnxExportOptions(
    input_names=(IMAGES_INPUT, CONDITIONING_INPUT),
    output_names=(MEAN_OUTPUT, LOG_VARIANCE_OUTPUT),
    dynamic_dimensions=(
        DynamicDimension(input_name=IMAGES_INPUT, axis=0, symbol=BATCH_SYMBOL),
        DynamicDimension(input_name=IMAGES_INPUT, axis=1, symbol=VIEW_SYMBOL),
        DynamicDimension(input_name=IMAGES_INPUT, axis=3, symbol=HEIGHT_SYMBOL),
        DynamicDimension(input_name=IMAGES_INPUT, axis=4, symbol=WIDTH_SYMBOL),
        DynamicDimension(input_name=CONDITIONING_INPUT, axis=0, symbol=BATCH_SYMBOL),
    ),
)


def _encoder(*, seed: int = 11) -> MultiViewImageEncoder:
    torch.manual_seed(seed)
    return MultiViewImageEncoder(ImageEncoder(ENCODER_CONFIG)).eval()


def _regressor(*, seed: int = 13, **overrides) -> MultiViewGaussianRegressor:
    torch.manual_seed(seed)
    encoder = MultiViewImageEncoder(ImageEncoder(ENCODER_CONFIG))
    head = GaussianRegressionHead(attrs.evolve(HEAD_CONFIG, **overrides))
    return MultiViewGaussianRegressor(encoder, head).eval()


def _images(*, batch: int = 2, views: int = 5, size: int = 32, seed: int = 0) -> Tensor:
    generator = torch.Generator().manual_seed(seed)
    return torch.randn(
        (batch, views, IMAGE_CHANNELS, size, size),
        generator=generator,
        dtype=torch.float32,
    )


def _view_mask(images: Tensor) -> Tensor:
    """``[B, V, C, H, W]`` に対応する全 true の有効画素 mask を返す."""

    batch, views, _, height, width = images.shape
    return torch.ones((batch, views, 1, height, width), dtype=torch.bool)


class _ExportableRegressor(nn.Module):
    """Mask を取らない 2 入力の出荷形 wrapper.

    export の入口は位置引数をそのまま ONNX の入力にする。

    そこで mask を持たない推論経路と同じ形へ絞る。
    """

    def __init__(self) -> None:
        super().__init__()
        self._regressor = _regressor()

    @override
    def forward(self, images: Tensor, conditioning: Tensor) -> tuple[Tensor, Tensor]:
        """``[B, V, 6, H, W]`` と ``[B, 1]`` から平均と log 分散を返す."""

        return self._regressor(images, None, conditioning)


class TestMultiViewImageEncoder:
    """View 軸を平坦化して共有 encoder へ通し、平均で集約する."""

    def test_exposes_the_shared_encoder_and_its_feature_size(self):
        encoder = _encoder()

        assert encoder.output_features == ENCODER_CONFIG.output_features
        assert encoder.encoder.output_features == ENCODER_CONFIG.output_features

    def test_reduces_the_view_axis_to_one_feature_vector_per_sample(self):
        features = _encoder()(_images(batch=3, views=5))

        assert tuple(features.shape) == (3, ENCODER_CONFIG.output_features)

    @pytest.mark.parametrize("size", [27, 53, 159])
    def test_accepts_the_whole_point_dispense_crop_range(self, size: int):
        features = _encoder()(_images(batch=2, views=2, size=size))

        assert tuple(features.shape) == (2, ENCODER_CONFIG.output_features)

    def test_is_invariant_to_the_order_of_the_views(self):
        """View を並べ替えても出力が変わらない.

        平均を「先頭 view を取る」「重み付き和にする」等へ変える変異は、並べ替えで必ず値が動く。
        """

        encoder = _encoder()
        images = _images(batch=2, views=5, seed=1)
        permuted = images[:, torch.tensor([3, 0, 4, 1, 2])]

        with torch.no_grad():
            baseline = encoder(images)
            shuffled = encoder(permuted)

        assert float((baseline - shuffled).abs().max().item()) < 1e-5

    def test_repeating_one_view_matches_the_single_view_result(self):
        """同じ画像を 5 枚並べた V=5 と V=1 の出力が一致する.

        平均を総和へ変える変異はここで 5 倍ずれる。V=5 で学習した model を V=1 で使えるという主張の、model
        側の根拠でもある。
        """

        encoder = _encoder()
        single = _images(batch=2, views=1, seed=2)
        repeated = single.expand(-1, 5, -1, -1, -1)

        with torch.no_grad():
            one_view = encoder(single)
            five_views = encoder(repeated)

        assert float((one_view - five_views).abs().max().item()) < 1e-5

    def test_aggregates_the_views_by_their_arithmetic_mean(self):
        """集約は算術平均そのもの.

        最大値や中央値も順序不変で、同じ view を並べても変わらない。

        その 2 つでは平均から区別できない。

        違う 2 view を 1 枚ずつ通した結果の平均と厳密に突き合わせる。
        """

        encoder = _encoder()
        images = _images(batch=2, views=2, seed=5)

        with torch.no_grad():
            pooled = encoder(images)
            first = encoder(images[:, :1])
            second = encoder(images[:, 1:])

        assert torch.allclose(pooled, (first + second) / 2, atol=1e-6)

    def test_applies_the_valid_pixel_mask_across_the_view_axis(self):
        """Mask で無効化した領域の中身は出力へ効かない."""

        encoder = _encoder()
        images = _images(batch=2, views=3, seed=3)
        mask = torch.ones((2, 3, 1, 32, 32), dtype=torch.bool)
        mask[:, :, :, 24:, :] = False
        disturbed = images.clone()
        disturbed[:, :, :, 24:, :] += 100.0

        with torch.no_grad():
            baseline = encoder(images, mask)
            perturbed = encoder(disturbed, mask)

        assert torch.equal(baseline, perturbed)

    def test_the_learnable_padding_pixel_receives_a_gradient(self):
        """Padding 画素の parameter へ勾配が流れる.

        多視点の flatten / unflatten を挟んでも learnable padding の配線が
        切れていないことを見る。
        """

        encoder = _encoder()
        encoder.train()
        images = _images(batch=2, views=3, seed=4)
        mask = torch.ones((2, 3, 1, 32, 32), dtype=torch.bool)
        mask[:, :, :, 24:, :] = False

        encoder(images, mask).sum().backward()

        gradient = encoder.encoder.padding_pixel.grad
        assert gradient is not None
        assert bool((gradient != 0).any())

    def test_rejects_images_without_a_view_axis(self):
        """4D 入力は multiview 自身の理由文で拒否する.

        ``ImageEncoder`` は 4D を正当な入力として受け取ってしまうので、ここで
        止めないと view 軸の取り違えが黙って通る。``ml`` のどちらの層が弾いたか
        を区別するため、理由文に ``[B, V, C, H, W]`` が入ることまで見る。
        """

        with pytest.raises(ValueError, match=r"images は \[B, V, C, H, W\] が必要です"):
            _encoder()(torch.randn(2, IMAGE_CHANNELS, 32, 32))

    def test_rejects_a_mask_without_a_view_axis(self):
        with pytest.raises(
            ValueError, match=r"valid pixel mask は \[B, V, 1, H, W\] が必要です"
        ):
            _encoder()(
                _images(batch=2, views=3),
                torch.ones((2, 1, 32, 32), dtype=torch.bool),
            )

    def test_rejects_a_mask_whose_batch_and_view_are_swapped(self):
        """B と V を取り違えた mask は multiview 自身が弾く.

        ``flatten(0, 1)`` のあとはどちらも ``B * V`` になるので、``ImageEncoder``
        からは観測できない。

        通してしまうと違う view の mask を当てたまま結果が返る。
        """

        encoder = _encoder()
        images = _images(batch=2, views=3, seed=8)
        swapped = torch.ones((3, 2, 1, 32, 32), dtype=torch.bool)

        with pytest.raises(ValueError, match="batch と view"):
            encoder(images, swapped)

    def test_the_shared_encoder_reports_a_channel_count_mismatch(self):
        """Channel 数の食い違いは平坦化後の ``ImageEncoder`` が弾く.

        multiview 側に同じ検査を重ねると、前段が後段を隠して回帰に気付けなく
        なる。したがって期待するのは ``ImageEncoder`` の理由文であって、
        multiview 固有の文面ではない。
        """

        images = torch.randn(2, 3, IMAGE_CHANNELS + 1, 32, 32)

        with pytest.raises(ValueError, match="images の channel 数が config"):
            _encoder()(images)

    def test_the_shared_encoder_reports_a_mask_of_the_wrong_dtype(self):
        mask = torch.ones((2, 3, 1, 32, 32), dtype=torch.float32)

        with pytest.raises(ValueError, match="valid pixel mask は bool が必要です"):
            _encoder()(_images(batch=2, views=3), mask)

    def test_the_shared_encoder_reports_a_mask_of_the_wrong_shape(self):
        mask = torch.ones((2, 3, 1, 16, 16), dtype=torch.bool)

        with pytest.raises(ValueError, match="valid pixel mask は"):
            _encoder()(_images(batch=2, views=3), mask)


class TestMultiViewGaussianRegressor:
    """多視点 encoder と Gaussian head をつないだ単一 model."""

    def test_rejects_a_head_that_does_not_match_the_encoder(self):
        torch.manual_seed(13)
        encoder = MultiViewImageEncoder(ImageEncoder(ENCODER_CONFIG))
        head = GaussianRegressionHead(
            attrs.evolve(HEAD_CONFIG, input_features=ENCODER_CONFIG.output_features + 8)
        )

        with pytest.raises(ValueError, match="input_features"):
            MultiViewGaussianRegressor(encoder, head)

    def test_predicts_a_mean_and_a_log_variance_per_sample(self):
        regressor = _regressor()

        mean, log_variance = regressor(
            _images(batch=3, views=5), None, torch.zeros(3, 1)
        )

        assert tuple(mean.shape) == (3, 1)
        assert tuple(log_variance.shape) == (3, 1)

    def test_accepts_a_mask_and_conditioning_together(self):
        regressor = _regressor()
        images = _images(batch=2, views=4)
        mask = torch.ones((2, 4, 1, 32, 32), dtype=torch.bool)

        mean, _ = regressor(images, mask, torch.zeros(2, 1))

        assert tuple(mean.shape) == (2, 1)

    def test_matches_the_encoder_and_head_applied_in_order(self):
        """出力は encoder → head をこの順に通した結果そのもの.

        平均と log 分散の取り違えは shape だけを見るテストで区別できない。

        mask を encoder へ渡さずに捨てる変異も同じ。

        seed 3 を使うのは、seed 13 では head の trunk ReLU が全 sample で死ぬため。

        出力が bias 固定になり、encoder の違いを映さない。
        """

        torch.manual_seed(3)
        encoder = MultiViewImageEncoder(ImageEncoder(ENCODER_CONFIG))
        head = GaussianRegressionHead(HEAD_CONFIG)
        regressor = MultiViewGaussianRegressor(encoder, head).eval()
        images = _images(batch=2, views=3, seed=6)
        mask = torch.ones((2, 3, 1, 32, 32), dtype=torch.bool)
        mask[:, :, :, 24:, :] = False
        conditioning = torch.zeros(2, 1)

        with torch.no_grad():
            mean, log_variance = regressor(images, mask, conditioning)
            expected_mean, expected_log_variance = head(
                encoder(images, mask), conditioning
            )
            unmasked_mean, _ = head(encoder(images, None), conditioning)

        # mask を捨てる変異が観測できる配置であることをテスト自身で確かめる
        assert not torch.equal(expected_mean, unmasked_mean)
        assert torch.equal(mean, expected_mean)
        assert torch.equal(log_variance, expected_log_variance)

    def test_the_conditioning_stays_one_value_per_sample(self):
        """条件変数は view 数に依らず sample あたり 1 本.

        view 数を conditioning へ混ぜると平均 pooling の view 数不変性が壊れる。
        """

        regressor = _regressor()
        images = _images(batch=2, views=3)

        baseline, _ = regressor(images, None, torch.zeros(2, 1))
        shifted, _ = regressor(images, None, torch.ones(2, 1))

        assert not bool(torch.allclose(baseline, shifted))


class TestExportedDynamicShapes:
    """``torch.export`` 後も batch・view・空間軸が固定されない.

    forward へ ``int()`` が入ると、非 strict export（``torch.export`` の既定）が
    SymInt を example の値へ落とし、``dynamic_shapes`` の宣言が黙って無視される。

    ``torch.onnx.export(dynamo=True)`` は非 strict の失敗を黙って strict へ
    落とすので、ONNX の ``dim_param`` だけを見てもこの回帰は捕まえられない。

    ``strict=False`` を明示するのは、torch 側の既定が変わっても検出力を保つため。
    """

    def test_accepts_another_batch_view_count_and_resolution(self):
        encoder = _encoder()
        dynamic = {
            0: torch.export.Dim.AUTO,
            1: torch.export.Dim.AUTO,
            3: torch.export.Dim.AUTO,
            4: torch.export.Dim.AUTO,
        }

        exported = torch.export.export(
            encoder,
            (_images(batch=2, views=5, size=32),),
            dynamic_shapes={"images": dynamic},
            strict=False,
        )
        features = exported.module()(_images(batch=3, views=2, size=48, seed=9))

        assert tuple(features.shape) == (3, ENCODER_CONFIG.output_features)

    def test_accepts_another_batch_view_count_and_resolution_on_the_mask_path(self):
        encoder = _encoder()
        dynamic = {
            0: torch.export.Dim.AUTO,
            1: torch.export.Dim.AUTO,
            3: torch.export.Dim.AUTO,
            4: torch.export.Dim.AUTO,
        }
        images = _images(batch=2, views=5, size=32)

        exported = torch.export.export(
            encoder,
            (images, _view_mask(images)),
            dynamic_shapes={"images": dynamic, "valid_pixel_mask": dynamic},
            strict=False,
        )
        other = _images(batch=3, views=2, size=48, seed=9)
        features = exported.module()(other, _view_mask(other))

        assert tuple(features.shape) == (3, ENCODER_CONFIG.output_features)


@pytest.fixture(scope="module")
def exported_model(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """多視点 model を 4 軸 dynamic で 1 度だけ ONNX へ書き出す."""

    directory = tmp_path_factory.mktemp("multiview-export")
    result, error = OnnxExportResult.export(
        _ExportableRegressor(),
        (_images(batch=2, views=5, size=32), torch.zeros(2, 1)),
        directory / "multiview.onnx",
        options=EXPORT_OPTIONS,
    )

    assert result is not None, error
    return result.model_path


class TestMultiViewOnnxExport:
    """多視点 model の ONNX export と ONNX Runtime 実行."""

    def test_keeps_the_batch_view_and_spatial_axes_symbolic(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """4 軸が ``dim_param`` として graph に残る.

        forward のどこかへ ``int()`` が入ると、非 strict export が SymInt を
        example の値へ落とし、``dynamic_shapes`` の宣言が黙って無視される。
        軸の並びを厳密一致で固定して、その回帰を捕まえる。
        """

        directory = tmp_path_factory.mktemp("multiview-export-symbols")
        result, error = OnnxExportResult.export(
            _ExportableRegressor(),
            (_images(batch=2, views=5, size=32), torch.zeros(2, 1)),
            directory / "multiview.onnx",
            options=EXPORT_OPTIONS,
        )

        assert result is not None, error
        images = next(
            tensor for tensor in result.summary.inputs if tensor.name == IMAGES_INPUT
        )
        assert images.dimensions == (
            BATCH_SYMBOL,
            VIEW_SYMBOL,
            str(IMAGE_CHANNELS),
            HEIGHT_SYMBOL,
            WIDTH_SYMBOL,
        )

    @pytest.mark.parametrize(
        ("batch", "views", "size"), [(1, 1, 53), (1, 5, 27), (2, 3, 159)]
    )
    def test_the_exported_graph_runs_for_other_view_counts_and_resolutions(
        self, exported_model: Path, batch: int, views: int, size: int
    ):
        """V=5 の example から出した graph が V=1 でも 27/159 px でも動く."""

        session = onnxruntime.InferenceSession(
            str(exported_model), providers=["CPUExecutionProvider"]
        )

        outputs = cast(
            "list[NDArray[np.float32]]",
            session.run(
                [MEAN_OUTPUT, LOG_VARIANCE_OUTPUT],
                {
                    IMAGES_INPUT: _images(
                        batch=batch, views=views, size=size, seed=7
                    ).numpy(),
                    CONDITIONING_INPUT: np.zeros((batch, 1), dtype=np.float32),
                },
            ),
        )

        assert outputs[0].shape == (batch, 1)
        assert outputs[1].shape == (batch, 1)

    def test_rejects_an_example_whose_view_axis_has_a_single_view(self):
        """V=1 の example では view 軸を dynamic に宣言できない.

        大きさ 1 の軸は「実際に変わる」ことを一度も確かめられない。

        そこで export の入口で理由を返す。軸番号まで理由文へ出す。
        """

        error = EXPORT_OPTIONS.validate_for(
            (_images(batch=2, views=1, size=32), torch.zeros(2, 1))
        )

        assert error is not None
        assert "大きさ 1 の軸" in error
        assert "軸 1" in error
