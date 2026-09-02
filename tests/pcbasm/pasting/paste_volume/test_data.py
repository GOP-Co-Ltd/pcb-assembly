from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

import pytest
import torch
from torchvision.io import read_image, write_png

from pcbasm.pasting.paste_volume.data import (
    AugmentationConfig,
    DatasetInput,
    ImageConstraints,
    PasteVolumeDataset,
    PixelBudgetBatchSampler,
    build_sample_index,
    collate_paste_volume,
    create_session_split,
    index_samples_by_id,
    load_composite_manifest,
    load_preprocessed_sample,
    load_split_manifest,
    merge_datasets,
    preprocess_rgb_pair,
    resolve_dataset_inputs,
    save_split_manifest,
    select_session_balanced_samples,
    summarize_dataset,
    validate_datasets,
    validate_session,
    validate_split_manifest,
)
from tests.pcbasm.pasting.paste_volume.support_data import (
    read_metadata,
    write_metadata,
    write_synthetic_session,
)


def _nested(metadata: dict[str, object], *keys: str) -> Any:
    current: Any = metadata
    for key in keys:
        current = current[key]
    return current


class TestSessionValidation:
    def test_validates_real_png_schema(self, tmp_path: Path):
        session = write_synthetic_session(tmp_path, "session-a", pad_count=2)

        validated = validate_session(session)

        assert validated.sample_count == 2

    def test_rejects_unknown_metadata_key(self, tmp_path: Path):
        session = write_synthetic_session(tmp_path, "session-a")
        metadata = read_metadata(session)
        metadata["unknown"] = 1
        write_metadata(session, metadata)

        with pytest.raises(ValueError, match="未知key"):
            validate_session(session)

    @pytest.mark.parametrize(
        ("keys", "value", "message"),
        [
            (("camera", "pixel_per_mm"), 0.0, "pixel_per_mm"),
            (("pads", "0", "measured_volume_ul"), float("nan"), "finite"),
            (("nozzle", "diameter_mm"), -0.1, "diameter_mm"),
        ],
    )
    def test_rejects_invalid_numbers(
        self, tmp_path: Path, keys: tuple[str, ...], value: object, message: str
    ):
        session = write_synthetic_session(tmp_path, "session-a")
        metadata = read_metadata(session)
        current: Any = metadata
        for key in keys[:-1]:
            current = current[int(key)] if isinstance(current, list) else current[key]
        current[keys[-1]] = value
        write_metadata(session, metadata)

        with pytest.raises(ValueError, match=message):
            validate_session(session)

    def test_rejects_duplicate_pad_and_view_numbers(self, tmp_path: Path):
        session = write_synthetic_session(tmp_path, "session-a", views_per_pad=2)
        metadata = read_metadata(session)
        pads = _nested(metadata, "pads")
        pads[1]["index"] = pads[0]["index"]
        write_metadata(session, metadata)

        with pytest.raises(ValueError, match="pad index"):
            validate_session(session)

        session = write_synthetic_session(tmp_path, "session-b", views_per_pad=2)
        metadata = read_metadata(session)
        views = _nested(metadata, "pads")[0]["views"]
        views[1]["number"] = views[0]["number"]
        write_metadata(session, metadata)
        with pytest.raises(ValueError, match="view number"):
            validate_session(session)

    def test_rejects_path_traversal_and_external_symlink(self, tmp_path: Path):
        outside = tmp_path / "outside.png"
        write_png(torch.zeros((3, 48, 64), dtype=torch.uint8), str(outside))
        session = write_synthetic_session(tmp_path, "session-a")
        metadata = read_metadata(session)
        _nested(metadata, "pads")[0]["views"][0]["pre"] = "../outside.png"
        write_metadata(session, metadata)
        with pytest.raises(ValueError, match="session外"):
            validate_session(session)

        session = write_synthetic_session(tmp_path, "session-b")
        pre = session / "pre" / "000001.00.png"
        pre.unlink()
        pre.symlink_to(outside)
        with pytest.raises(ValueError, match="session外"):
            validate_session(session)

    def test_rejects_wrong_channels_mask_values_and_dimensions(self, tmp_path: Path):
        session = write_synthetic_session(tmp_path, "gray-pre")
        write_png(
            torch.zeros((1, 48, 64), dtype=torch.uint8),
            str(session / "pre" / "000001.00.png"),
        )
        with pytest.raises(ValueError, match="3 channel"):
            validate_session(session)

        session = write_synthetic_session(tmp_path, "bad-mask")
        mask = torch.zeros((1, 48, 64), dtype=torch.uint8)
        mask[:, 10, 10] = 1
        write_png(mask, str(session / "mask" / "000001.00.png"))
        with pytest.raises(ValueError, match="0/255"):
            validate_session(session)

        session = write_synthetic_session(tmp_path, "bad-size")
        write_png(
            torch.zeros((3, 40, 64), dtype=torch.uint8),
            str(session / "post" / "000001.00.png"),
        )
        with pytest.raises(ValueError, match="寸法"):
            validate_session(session)

    def test_rejects_pixel_rect_and_total_mismatches(self, tmp_path: Path):
        session = write_synthetic_session(tmp_path, "bad-rect")
        metadata = read_metadata(session)
        _nested(metadata, "pads")[0]["views"][0]["pixel_rect"][2] += 1
        write_metadata(session, metadata)
        with pytest.raises(ValueError, match="pixel_rect"):
            validate_session(session)

        session = write_synthetic_session(tmp_path, "bad-volume")
        metadata = read_metadata(session)
        _nested(metadata, "total")["measured_volume_ul"] += 1.0
        write_metadata(session, metadata)
        with pytest.raises(ValueError, match="配分体積"):
            validate_session(session)

        session = write_synthetic_session(tmp_path, "bad-rotations")
        metadata = read_metadata(session)
        _nested(metadata, "total")["rotations"] += 1.0
        write_metadata(session, metadata)
        with pytest.raises(ValueError, match="回転数"):
            validate_session(session)

    def test_rejects_duplicate_image_triples_within_and_across_sessions(
        self, tmp_path: Path
    ):
        session = write_synthetic_session(tmp_path, "within", views_per_pad=2)
        for role in ("pre", "post", "mask"):
            shutil.copyfile(
                session / role / "000001.00.png", session / role / "000001.01.png"
            )
        with pytest.raises(ValueError, match="同一画像内容"):
            validate_session(session)

        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        first = write_synthetic_session(
            root_a, "session-a", machine_id="a", content_seed=10
        )
        second = write_synthetic_session(
            root_b, "session-b", machine_id="b", content_seed=11
        )
        for role in ("pre", "post", "mask"):
            shutil.copyfile(
                first / role / "000001.00.png", second / role / "000001.00.png"
            )
        with pytest.raises(ValueError, match="同一pre/post/mask"):
            resolve_dataset_inputs(roots=(root_a, root_b))

    def test_ignores_temporary_incomplete_and_zip_artifacts(self, tmp_path: Path):
        write_synthetic_session(tmp_path, "complete")
        write_synthetic_session(tmp_path, "ignored.tmp")
        write_synthetic_session(tmp_path, "ignored.incomplete")
        write_synthetic_session(tmp_path, ".hidden")
        (tmp_path / "archive.zip").write_bytes(b"not a dataset")

        composite = resolve_dataset_inputs(roots=(tmp_path,))

        assert [session.session_id for session in composite.sessions] == ["complete"]


class TestCompositeDataset:
    def test_merges_aliases_without_copying_and_round_trips(self, tmp_path: Path):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        session = write_synthetic_session(root_a, "same", content_seed=7)
        shutil.copytree(session, root_b / "same")
        output = tmp_path / "composite.json"

        composite = merge_datasets(
            (DatasetInput(root_a, "source-a"), DatasetInput(root_b, "source-b")),
            output,
            name="fixture",
        )
        loaded = load_composite_manifest(output)

        assert len(composite.sessions) == 1
        assert composite.sessions[0].source_ids == ("source-a", "source-b")
        assert len(composite.sessions[0].locations) == 2
        assert loaded.composite_fingerprint == composite.composite_fingerprint
        assert not (tmp_path / "pre").exists()

    def test_fingerprints_ignore_mount_path_and_input_order(self, tmp_path: Path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        write_synthetic_session(left, "s1", content_seed=1)
        write_synthetic_session(left, "s2", content_seed=2)
        shutil.copytree(left, right)

        first = merge_datasets(
            (DatasetInput(left, "stable"),), tmp_path / "first.json", name="one"
        )
        second = merge_datasets(
            (DatasetInput(right, "stable"),), tmp_path / "second.json", name="two"
        )

        assert first.content_fingerprint == second.content_fingerprint
        assert first.composite_fingerprint == second.composite_fingerprint

    def test_source_alias_changes_only_composite_fingerprint(self, tmp_path: Path):
        root = tmp_path / "root"
        write_synthetic_session(root, "session")

        first = merge_datasets(
            (DatasetInput(root, "a"),), tmp_path / "a.json", name="a"
        )
        second = merge_datasets(
            (DatasetInput(root, "b"),), tmp_path / "b.json", name="b"
        )

        assert first.content_fingerprint == second.content_fingerprint
        assert first.composite_fingerprint != second.composite_fingerprint

    def test_rejects_session_id_source_id_and_label_collisions(self, tmp_path: Path):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        write_synthetic_session(root_a, "same", machine_id="a", content_seed=1)
        write_synthetic_session(root_b, "same", machine_id="b", content_seed=2)
        with pytest.raises(ValueError, match="session_id"):
            resolve_dataset_inputs(roots=(root_a, root_b))

        with pytest.raises(ValueError, match="source_id"):
            merge_datasets(
                (DatasetInput(root_a, "duplicate"), DatasetInput(root_b, "duplicate")),
                tmp_path / "collision.json",
                name="collision",
            )

        root_c = tmp_path / "c"
        root_d = tmp_path / "d"
        write_synthetic_session(root_c, "c", machine_id="a", content_seed=5)
        write_synthetic_session(root_d, "d", machine_id="b", content_seed=5)
        with pytest.raises(ValueError, match="画像集合|同一pre/post/mask"):
            resolve_dataset_inputs(roots=(root_c, root_d))

    def test_rejects_overwrite_and_manifest_tampering(self, tmp_path: Path):
        root = tmp_path / "root"
        write_synthetic_session(root, "session")
        output = tmp_path / "composite.json"
        merge_datasets((DatasetInput(root, "a"),), output, name="a")
        with pytest.raises(FileExistsError, match="in-place"):
            merge_datasets((DatasetInput(root, "a"),), output, name="a")

        payload = json.loads(output.read_text())
        payload["content_fingerprint"] = "sha256:bad"
        output.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match="content_fingerprint"):
            load_composite_manifest(output)

    def test_resolve_rejects_manifest_and_roots_together(self, tmp_path: Path):
        with pytest.raises(ValueError, match="同時指定"):
            resolve_dataset_inputs(manifest=tmp_path / "x", roots=(tmp_path,))


class TestSampleIndexAndSplit:
    def test_builds_stable_ids_weights_and_provenance(self, tmp_path: Path):
        root = tmp_path / "root"
        write_synthetic_session(root, "a", pad_count=2, views_per_pad=2)
        first = resolve_dataset_inputs(roots=(root,))
        first_samples = build_sample_index(first)
        write_synthetic_session(root, "b", pad_count=1, content_seed=99)
        second_samples = build_sample_index(resolve_dataset_inputs(roots=(root,)))

        second_by_id = index_samples_by_id(second_samples)
        assert all(sample.sample_id in second_by_id for sample in first_samples)
        assert sum(sample.loss_weight for sample in first_samples) == pytest.approx(1.0)
        assert all(sample.source_ids for sample in first_samples)
        assert {sample.dispense_mode for sample in first_samples} == {"dot", "line"}

    def test_primary_split_is_session_level_reproducible_and_round_trips(
        self, tmp_path: Path
    ):
        for index in range(6):
            write_synthetic_session(tmp_path, f"s{index}", content_seed=index)
        composite = resolve_dataset_inputs(roots=(tmp_path,))
        samples = build_sample_index(composite)

        first = create_session_split(samples, composite.composite_fingerprint, seed=19)
        second = create_session_split(samples, composite.composite_fingerprint, seed=19)
        path = tmp_path / "split.json"
        save_split_manifest(first, path)
        loaded = load_split_manifest(
            path, expected_composite_fingerprint=composite.composite_fingerprint
        )
        validate_split_manifest(
            loaded,
            samples,
            expected_composite_fingerprint=composite.composite_fingerprint,
        )

        assert first == second == loaded
        by_id = index_samples_by_id(samples)
        session_sets = [
            {by_id[sample_id].session_id for sample_id in ids}
            for ids in (
                first.train_sample_ids,
                first.validation_sample_ids,
                first.test_sample_ids,
            )
        ]
        assert session_sets[0].isdisjoint(session_sets[1])
        assert session_sets[0].isdisjoint(session_sets[2])
        assert session_sets[1].isdisjoint(session_sets[2])

    def test_base_and_finetune_reject_insufficient_sessions(self, tmp_path: Path):
        write_synthetic_session(tmp_path, "only")
        composite = resolve_dataset_inputs(roots=(tmp_path,))
        samples = build_sample_index(composite)
        with pytest.raises(ValueError, match="最低3 session"):
            create_session_split(samples, composite.composite_fingerprint, mode="base")
        with pytest.raises(ValueError, match="最低2 session"):
            create_session_split(
                samples, composite.composite_fingerprint, mode="finetune"
            )

    def test_session_balanced_selection(self, tmp_path: Path):
        write_synthetic_session(tmp_path, "large", pad_count=5, content_seed=1)
        write_synthetic_session(tmp_path, "small", pad_count=1, content_seed=2)
        samples = build_sample_index(resolve_dataset_inputs(roots=(tmp_path,)))

        selected = select_session_balanced_samples(
            samples, (sample.sample_id for sample in samples), limit=2, seed=4
        )

        assert {sample.session_id for sample in selected} == {"large", "small"}


class TestPreprocessing:
    def test_uses_one_scalar_layer_norm_across_all_six_channels(self):
        pre = torch.zeros((3, 40, 40), dtype=torch.uint8)
        post = torch.zeros((3, 40, 40), dtype=torch.uint8)
        for channel, value in enumerate((10, 40, 90)):
            pre[channel].fill_(value)
        for channel, value in enumerate((100, 160, 240)):
            post[channel].fill_(value)

        result = preprocess_rgb_pair(pre, post, 12.0)

        assert float(result.image_6ch.mean()) == pytest.approx(0.0, abs=1e-6)
        assert float(result.image_6ch.var(correction=0)) == pytest.approx(1.0, rel=2e-3)
        channel_means = result.image_6ch.mean(dim=(1, 2))
        assert not torch.allclose(channel_means, torch.zeros_like(channel_means))
        assert torch.all(channel_means[1:] > channel_means[:-1])

    def test_accepts_rgb_hwc_numpy_without_bgr_conversion(self):
        rgb = torch.zeros((40, 40, 3), dtype=torch.uint8).numpy()
        rgb[:, :, 0] = 10
        rgb[:, :, 1] = 100
        rgb[:, :, 2] = 220
        post = rgb.copy()
        post[10:20, 10:20, :] += 10

        result = preprocess_rgb_pair(rgb, post, 10.0)

        assert result.image_6ch[0].mean() < result.image_6ch[1].mean()
        assert result.image_6ch[1].mean() < result.image_6ch[2].mean()

    def test_downscales_isotropically_without_upscaling(self):
        generator = torch.Generator().manual_seed(1)
        image = torch.randint(
            0, 256, (3, 256, 1024), dtype=torch.uint8, generator=generator
        )
        result = preprocess_rgb_pair(image, image.roll(1, 2), 20.0)
        assert result.image_6ch.shape == (6, 256, 1024)
        assert result.pixel_per_mm == pytest.approx(20.0)

        large = torch.randint(
            0, 256, (3, 700, 700), dtype=torch.uint8, generator=generator
        )
        resized = preprocess_rgb_pair(large, large.roll(1, 1), 20.0)
        assert resized.image_6ch.shape[1] * resized.image_6ch.shape[2] <= 262_144
        assert resized.image_6ch.shape[1] == resized.image_6ch.shape[2]
        assert resized.pixel_per_mm < 20.0

    def test_rejects_small_constant_and_invalid_inputs(self):
        with pytest.raises(ValueError, match="32px"):
            preprocess_rgb_pair(
                torch.zeros((3, 31, 40), dtype=torch.uint8),
                torch.ones((3, 31, 40), dtype=torch.uint8),
                10.0,
            )
        with pytest.raises(ValueError, match="分散"):
            preprocess_rgb_pair(
                torch.zeros((3, 40, 40), dtype=torch.uint8),
                torch.zeros((3, 40, 40), dtype=torch.uint8),
                10.0,
            )
        with pytest.raises(ValueError, match="shape"):
            preprocess_rgb_pair(
                torch.zeros((3, 40, 40), dtype=torch.uint8),
                torch.zeros((3, 41, 40), dtype=torch.uint8),
                10.0,
            )

    def test_augmentation_is_sample_epoch_deterministic(self, tmp_path: Path):
        write_synthetic_session(tmp_path, "session")
        sample = build_sample_index(resolve_dataset_inputs(roots=(tmp_path,)))[0]

        first = load_preprocessed_sample(sample, training=True, global_seed=8, epoch=1)
        same = load_preprocessed_sample(sample, training=True, global_seed=8, epoch=1)
        next_epoch = load_preprocessed_sample(
            sample, training=True, global_seed=8, epoch=2
        )

        assert torch.equal(first.image_6ch, same.image_6ch)
        assert torch.equal(first.valid_pixel_mask, same.valid_pixel_mask)
        assert not torch.equal(first.image_6ch, next_epoch.image_6ch)
        assert (~first.valid_pixel_mask).any()

    def test_constraints_validate_configuration(self):
        with pytest.raises(ValueError):
            ImageConstraints(min_size=64, max_size=32)
        with pytest.raises(ValueError):
            AugmentationConfig(min_scale=1.2, max_scale=0.8)


class TestBatching:
    def test_pixel_budget_plan_is_deterministic_and_keeps_last_batch(
        self, tmp_path: Path
    ):
        for index, dimensions in enumerate(
            ((64, 48), (80, 48), (64, 80), (96, 64), (72, 52))
        ):
            write_synthetic_session(
                tmp_path,
                f"s{index}",
                width=dimensions[0],
                height=dimensions[1],
                content_seed=index,
            )
        samples = build_sample_index(resolve_dataset_inputs(roots=(tmp_path,)))
        sampler = PixelBudgetBatchSampler(
            samples,
            max_batch_pixels=32 * 96 * 2,
            max_batch_size=2,
            training=False,
            global_seed=3,
        )

        first = tuple(tuple(batch) for batch in sampler)
        second = tuple(tuple(batch) for batch in sampler)

        assert first == second
        assert sorted(index for batch in first for index in batch) == list(
            range(len(samples))
        )
        assert all(1 <= len(batch) <= 2 for batch in first)

    def test_collate_centers_evaluation_and_combines_valid_masks(self, tmp_path: Path):
        write_synthetic_session(tmp_path, "a", width=64, height=40, content_seed=1)
        write_synthetic_session(tmp_path, "b", width=40, height=64, content_seed=2)
        samples = build_sample_index(resolve_dataset_inputs(roots=(tmp_path,)))
        dataset = PasteVolumeDataset(samples, training=False)

        batch = collate_paste_volume([dataset[0], dataset[2]], training=False)

        assert batch.image_6ch.shape == (2, 6, 64, 64)
        assert batch.valid_pixel_mask.shape == (2, 1, 64, 64)
        assert batch.valid_pixel_mask[0].sum() == samples[0].width * samples[0].height
        assert batch.target_volume_ul.shape == (2,)
        assert batch.pixel_per_mm.shape == (2, 1)

    def test_training_collate_placement_is_deterministic(self, tmp_path: Path):
        write_synthetic_session(tmp_path, "a", width=40, height=40)
        sample = build_sample_index(resolve_dataset_inputs(roots=(tmp_path,)))[0]
        dataset = PasteVolumeDataset(
            (sample,),
            training=True,
            global_seed=10,
            augmentation=AugmentationConfig(enabled=False),
        )
        item = dataset[0]

        first = collate_paste_volume((item,), training=True)
        second = collate_paste_volume((item,), training=True)

        assert torch.equal(first.image_6ch, second.image_6ch)
        assert torch.equal(first.valid_pixel_mask, second.valid_pixel_mask)


class TestDatasetReports:
    def test_validate_and_summarize_multiple_datasets(self, tmp_path: Path):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        write_synthetic_session(root_a, "a", machine_id="a", content_seed=1)
        write_synthetic_session(root_b, "b", machine_id="b", content_seed=2)

        report = validate_datasets((DatasetInput(root_a), DatasetInput(root_b)))
        summary = summarize_dataset(resolve_dataset_inputs(roots=(root_a, root_b)))

        assert report.session_count == 2
        assert report.sample_count == 4
        assert summary.machine_counts == {"a": 2, "b": 2}
        assert summary.min_width == summary.max_width == 64
        assert math.isfinite(summary.min_measured_volume_ul)
