"""Paste-volume dataset roots, metadata, targets, and persisted manifests."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import (
    Literal,
    TypedDict,
    override,
)

import attrs
import torch
from torch import Tensor
from torch.utils.data import Dataset
from torchvision.io import ImageReadMode, decode_image

from ml.data.batch import pad_image_samples
from ml.data.image import (
    AugmentationConfig,
    ImageConstraints,
    PreprocessedImagePair as PreprocessedSample,
    augmentation_parameters,
    preprocess_rgb_pair,
)
from ml.data.split import select_group_balanced, split_groups
from pcbasm.pasting.paste_dataset import PasteDatasetMetadata

COMPOSITE_KIND = "pcbasm-paste-volume-composite-dataset"
COMPOSITE_SCHEMA_VERSION = 1
SPLIT_KIND = "pcbasm-paste-volume-split"
SPLIT_SCHEMA_VERSION = 1
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

type SplitName = Literal["train", "validation", "test"]
type TrainingMode = Literal["base", "finetune"]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _atomic_json_save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@attrs.frozen
class DatasetInput:
    """A dataset root/session/manifest with an optional stable source alias."""

    path: Path
    source_id: str | None = None

    def __attrs_post_init__(self) -> None:
        if self.source_id is not None and not self.source_id.strip():
            raise ValueError("source_idは空にできません")


@attrs.frozen
class DatasetSource:
    source_id: str
    path: Path

    def to_dict(self) -> dict[str, str]:
        return {"source_id": self.source_id, "path": str(self.path)}


@attrs.frozen
class SessionLocation:
    source_id: str
    relative_path: str

    def to_dict(self) -> dict[str, str]:
        return {"source_id": self.source_id, "relative_path": self.relative_path}


@attrs.frozen
class CompositeSession:
    session_id: str
    session_fingerprint: str
    image_set_fingerprint: str
    image_triple_fingerprints: tuple[str, ...]
    source_ids: tuple[str, ...]
    locations: tuple[SessionLocation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "session_fingerprint": self.session_fingerprint,
            "image_set_fingerprint": self.image_set_fingerprint,
            "image_triple_fingerprints": list(self.image_triple_fingerprints),
            "source_ids": list(self.source_ids),
            "locations": [location.to_dict() for location in self.locations],
        }


@attrs.frozen
class CompositeDataset:
    """Flattened, content-addressed collection of immutable sessions."""

    name: str
    sources: tuple[DatasetSource, ...]
    sessions: tuple[CompositeSession, ...]
    content_fingerprint: str
    composite_fingerprint: str
    kind: str = COMPOSITE_KIND
    schema_version: int = COMPOSITE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "name": self.name,
            "sources": [source.to_dict() for source in self.sources],
            "sessions": [session.to_dict() for session in self.sessions],
            "content_fingerprint": self.content_fingerprint,
            "composite_fingerprint": self.composite_fingerprint,
        }


@attrs.frozen
class DatasetValidationReport:
    composite_fingerprint: str
    content_fingerprint: str
    source_count: int
    session_count: int
    sample_count: int

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class DatasetSummary:
    composite_fingerprint: str
    source_count: int
    session_count: int
    sample_count: int
    machine_counts: Mapping[str, int]
    paste_lot_counts: Mapping[str, int]
    nozzle_counts: Mapping[str, int]
    dispense_mode_counts: Mapping[str, int]
    min_width: int
    max_width: int
    min_height: int
    max_height: int
    min_measured_volume_ul: float
    max_measured_volume_ul: float

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class _ValidatedSession:
    session_id: str
    path: Path
    metadata: PasteDatasetMetadata
    session_fingerprint: str
    image_set_fingerprint: str
    image_triple_fingerprints: tuple[str, ...]
    sample_count: int


class _CaptureRecord(TypedDict):
    pad_index: int
    view_number: int
    pre: str
    post: str
    mask: str


def _load_metadata(path: Path) -> PasteDatasetMetadata:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"metadata.jsonを読み込めません: {path}: {error}") from error
    if not isinstance(raw, Mapping):
        raise ValueError(f"metadata schema v1が不正です: {path}: objectが必要です")
    try:
        metadata = PasteDatasetMetadata.from_dict(raw)
    except ValueError as error:
        raise ValueError(f"metadata schema v1が不正です: {path}: {error}") from error
    return metadata


def _require_positive(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}は正の有限値が必要です: {value!r}")


def _resolve_capture_path(session_path: Path, relative: str, label: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts:
        raise ValueError(f"{label}はsession内の相対pathが必要です: {relative!r}")
    try:
        resolved_root = session_path.resolve(strict=True)
        resolved = (session_path / candidate).resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        raise ValueError(
            f"{label}がsession外または存在しません: {relative!r}"
        ) from error
    return resolved


def _resolve_session_location(source_path: Path, relative: str) -> Path:
    if relative != ".":
        return _resolve_capture_path(source_path, relative, "session location")
    try:
        return source_path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        raise ValueError("session locationが存在しません: '.'") from error


def _decode_png(path: Path, *, channels: int, label: str) -> Tensor:
    if path.suffix.lower() != ".png" or path.read_bytes()[:8] != PNG_SIGNATURE:
        raise ValueError(f"{label}はlossless PNGが必要です: {path}")
    try:
        decoded = decode_image(str(path), mode=ImageReadMode.UNCHANGED)
    except RuntimeError as error:
        raise ValueError(f"{label} PNGをdecodeできません: {path}: {error}") from error
    if (
        decoded.dtype != torch.uint8
        or decoded.ndim != 3
        or decoded.shape[0] != channels
    ):
        raise ValueError(
            f"{label}はuint8 {channels} channel PNGが必要です: "
            f"shape={tuple(decoded.shape)}, dtype={decoded.dtype}"
        )
    return decoded


def validate_session(session_path: Path) -> _ValidatedSession:
    """Validate every metadata field and capture in one completed session."""

    session_path = session_path.resolve(strict=True)
    if (
        not session_path.is_dir()
        or session_path.name.startswith(".")
        or session_path.name.endswith(".incomplete")
    ):
        raise ValueError(f"完成session directoryではありません: {session_path}")
    metadata_path = session_path / "metadata.json"
    if not metadata_path.is_file():
        raise ValueError(f"metadata.jsonがありません: {session_path}")
    metadata = _load_metadata(metadata_path)
    if metadata.kind != "pcbasm-paste-volume-dataset" or metadata.schema_version != 1:
        raise ValueError(f"dataset kind/schema versionが不正です: {session_path}")
    _require_positive("camera.pixel_per_mm", metadata.camera.pixel_per_mm)
    _require_positive("paste.density_mg_per_ul", metadata.paste.density_mg_per_ul)
    _require_positive("nozzle.diameter_mm", metadata.nozzle.diameter_mm)
    _require_positive("total.measured_volume_ul", metadata.total.measured_volume_ul)
    _require_positive("total.rotations", metadata.total.rotations)
    _require_positive("purge.measured_volume_ul", metadata.purge.measured_volume_ul)
    if any(size <= 0 for size in metadata.camera.resolution):
        raise ValueError("camera.resolutionは正の寸法が必要です")
    if not metadata.pads:
        raise ValueError("padsが空です")

    pad_indices: set[int] = set()
    capture_paths: set[Path] = set()
    capture_records: list[_CaptureRecord] = []
    image_triples: set[str] = set()
    pad_volume = 0.0
    pad_rotations = 0.0
    for pad in metadata.pads:
        if pad.index < 1 or pad.index in pad_indices:
            raise ValueError(f"pad indexが不正または重複しています: {pad.index}")
        pad_indices.add(pad.index)
        _require_positive(
            f"pad[{pad.index}].measured_volume_ul", pad.measured_volume_ul
        )
        _require_positive(
            f"pad[{pad.index}].execution.rotations", pad.execution.rotations
        )
        if not pad.views:
            raise ValueError(f"pad[{pad.index}].viewsが空です")
        view_numbers: set[int] = set()
        for view in pad.views:
            if view.number < 0 or view.number in view_numbers:
                raise ValueError(
                    f"view numberが不正または重複しています: pad={pad.index}, view={view.number}"
                )
            view_numbers.add(view.number)
            paths = {
                role: _resolve_capture_path(session_path, relative, role)
                for role, relative in (
                    ("pre", view.pre),
                    ("post", view.post),
                    ("mask", view.mask),
                )
            }
            if any(path in capture_paths for path in paths.values()):
                raise ValueError(
                    f"capture pathが重複しています: pad={pad.index}, view={view.number}"
                )
            capture_paths.update(paths.values())
            pre = _decode_png(paths["pre"], channels=3, label="pre")
            post = _decode_png(paths["post"], channels=3, label="post")
            mask = _decode_png(paths["mask"], channels=1, label="mask")
            if pre.shape != post.shape or tuple(mask.shape[1:]) != tuple(pre.shape[1:]):
                raise ValueError(
                    f"pre/post/maskの寸法が一致しません: pad={pad.index}, view={view.number}"
                )
            mask_values = torch.unique(mask)
            if not all(int(value) in (0, 255) for value in mask_values):
                raise ValueError(
                    f"maskは0/255だけで構成する必要があります: {paths['mask']}"
                )
            height, width = int(pre.shape[1]), int(pre.shape[2])
            mask_region = mask.to(torch.bool)
            if not torch.any(mask_region):
                raise ValueError(f"maskの有効領域が空です: {paths['mask']}")
            mask_coordinates = torch.nonzero(mask_region[0], as_tuple=False)
            mask_height = int(
                mask_coordinates[:, 0].max() - mask_coordinates[:, 0].min() + 1
            )
            mask_width = int(
                mask_coordinates[:, 1].max() - mask_coordinates[:, 1].min() + 1
            )
            if int(mask_region.sum()) < 4 or mask_height < 2 or mask_width < 2:
                raise ValueError(f"maskの有効領域が小さすぎます: {paths['mask']}")
            if (
                torch.any(mask_region[:, 0, :])
                or torch.any(mask_region[:, -1, :])
                or torch.any(mask_region[:, :, 0])
                or torch.any(mask_region[:, :, -1])
            ):
                raise ValueError(
                    f"maskがcrop境界へ接しており欠損を検証できません: {paths['mask']}"
                )
            minimum_size = ImageConstraints().min_size
            if height < minimum_size or width < minimum_size:
                raise ValueError(
                    f"source imageは各辺{minimum_size}px以上が必要です: "
                    f"{width}x{height}"
                )
            sample_variance = (
                torch.cat((pre, post), dim=0).to(torch.float32).var(correction=0)
            )
            if not torch.isfinite(sample_variance) or float(sample_variance) < 1e-12:
                raise ValueError(
                    f"pre/post sampleの分散が小さすぎます: pad={pad.index}, "
                    f"view={view.number}"
                )
            x0, y0, x1, y1 = view.pixel_rect
            if x1 <= x0 or y1 <= y0 or x1 - x0 != width or y1 - y0 != height:
                raise ValueError(
                    f"pixel_rectと保存画像の寸法が一致しません: pad={pad.index}, view={view.number}"
                )
            hashes = {role: _file_hash(path) for role, path in paths.items()}
            triple = _fingerprint([hashes["pre"], hashes["post"], hashes["mask"]])
            if triple in image_triples:
                raise ValueError(
                    f"同一画像内容のsampleが重複しています: pad={pad.index}, view={view.number}"
                )
            image_triples.add(triple)
            capture_records.append(
                {
                    "pad_index": pad.index,
                    "view_number": view.number,
                    "pre": hashes["pre"],
                    "post": hashes["post"],
                    "mask": hashes["mask"],
                }
            )
        pad_volume += pad.measured_volume_ul
        pad_rotations += pad.execution.rotations

    expected_volume = metadata.purge.measured_volume_ul + pad_volume
    expected_rotations = metadata.purge.execution.rotations + pad_rotations
    if not math.isclose(
        expected_volume, metadata.total.measured_volume_ul, rel_tol=1e-9, abs_tol=1e-9
    ):
        raise ValueError(
            "purgeとpadの配分体積合計がtotal.measured_volume_ulと一致しません"
        )
    if not math.isclose(
        expected_rotations, metadata.total.rotations, rel_tol=1e-9, abs_tol=1e-9
    ):
        raise ValueError("purgeとpadの正方向回転数合計がtotal.rotationsと一致しません")

    capture_records.sort(key=lambda item: (item["pad_index"], item["view_number"]))
    image_set_fingerprint = _fingerprint(
        sorted(
            (
                image_hash
                for record in capture_records
                for image_hash in (record["pre"], record["post"], record["mask"])
            ),
        )
    )
    session_fingerprint = _fingerprint(
        {
            "metadata": metadata.to_dict(),
            "captures": capture_records,
        }
    )
    return _ValidatedSession(
        session_id=session_path.name,
        path=session_path,
        metadata=metadata,
        session_fingerprint=session_fingerprint,
        image_set_fingerprint=image_set_fingerprint,
        image_triple_fingerprints=tuple(sorted(image_triples)),
        sample_count=sum(len(pad.views) for pad in metadata.pads),
    )


def _discover_sessions(root: Path) -> tuple[tuple[Path, str], ...]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"dataset root/session directoryではありません: {root}")
    if (root / "metadata.json").is_file():
        return ((root, "."),)
    found: list[tuple[Path, str]] = []
    for metadata_path in root.rglob("metadata.json"):
        relative = metadata_path.parent.relative_to(root)
        if any(
            part.startswith(".")
            or part.endswith(".tmp")
            or part.endswith(".incomplete")
            for part in relative.parts
        ):
            continue
        found.append((metadata_path.parent, relative.as_posix()))
    if not found:
        raise ValueError(f"完成sessionが見つかりません: {root}")
    return tuple(sorted(found, key=lambda item: item[1]))


def _dataset_fingerprints(sessions: Sequence[CompositeSession]) -> tuple[str, str]:
    content = _fingerprint(
        {
            "kind": COMPOSITE_KIND,
            "schema_version": COMPOSITE_SCHEMA_VERSION,
            "sessions": sorted(session.session_fingerprint for session in sessions),
        }
    )
    source_mapping = [
        {
            "session_fingerprint": session.session_fingerprint,
            "source_ids": sorted(session.source_ids),
        }
        for session in sorted(sessions, key=lambda item: item.session_fingerprint)
    ]
    composite = _fingerprint(
        {
            "content_fingerprint": content,
            "session_sources": source_mapping,
        }
    )
    return content, composite


def _compose(
    name: str,
    sources: Mapping[str, Path],
    session_candidates: Iterable[CompositeSession],
) -> CompositeDataset:
    by_fingerprint: dict[str, CompositeSession] = {}
    by_session_id: dict[str, str] = {}
    by_image_set: dict[str, str] = {}
    by_image_triple: dict[str, str] = {}
    for candidate in session_candidates:
        existing_session_fingerprint = by_session_id.get(candidate.session_id)
        if (
            existing_session_fingerprint is not None
            and existing_session_fingerprint != candidate.session_fingerprint
        ):
            raise ValueError(
                f"同じsession_idに異なる内容があります: {candidate.session_id}"
            )
        by_session_id[candidate.session_id] = candidate.session_fingerprint
        existing_image_session = by_image_set.get(candidate.image_set_fingerprint)
        if (
            existing_image_session is not None
            and existing_image_session != candidate.session_fingerprint
        ):
            raise ValueError("同じ画像集合に異なるmetadataまたは教師値があります")
        by_image_set[candidate.image_set_fingerprint] = candidate.session_fingerprint
        for image_triple in candidate.image_triple_fingerprints:
            existing_triple_session = by_image_triple.get(image_triple)
            if (
                existing_triple_session is not None
                and existing_triple_session != candidate.session_fingerprint
            ):
                raise ValueError("同一pre/post/mask画像内容が複数sessionに存在します")
            by_image_triple[image_triple] = candidate.session_fingerprint
        existing = by_fingerprint.get(candidate.session_fingerprint)
        if existing is None:
            by_fingerprint[candidate.session_fingerprint] = candidate
            continue
        locations = {
            (location.source_id, location.relative_path): location
            for location in (*existing.locations, *candidate.locations)
        }
        by_fingerprint[candidate.session_fingerprint] = attrs.evolve(
            existing,
            session_id=min(existing.session_id, candidate.session_id),
            source_ids=tuple(
                sorted(set(existing.source_ids) | set(candidate.source_ids))
            ),
            locations=tuple(locations[key] for key in sorted(locations)),
        )
    sessions = tuple(
        sorted(
            by_fingerprint.values(),
            key=lambda item: (item.session_id, item.session_fingerprint),
        )
    )
    if not sessions:
        raise ValueError("datasetにsessionがありません")
    used_source_ids = {
        source_id for session in sessions for source_id in session.source_ids
    }
    missing_sources = used_source_ids - set(sources)
    if missing_sources:
        raise ValueError(
            f"session locationのsourceが定義されていません: {sorted(missing_sources)}"
        )
    content, composite = _dataset_fingerprints(sessions)
    return CompositeDataset(
        name=name,
        sources=tuple(
            DatasetSource(key, sources[key]) for key in sorted(used_source_ids)
        ),
        sessions=sessions,
        content_fingerprint=content,
        composite_fingerprint=composite,
    )


def _from_root(dataset_input: DatasetInput) -> CompositeDataset:
    path = dataset_input.path.resolve(strict=True)
    if path.is_file():
        if dataset_input.source_id is not None:
            raise ValueError("composite manifestへsource_idを上書き指定できません")
        return load_composite_manifest(path)
    discovered = _discover_sessions(path)
    validated = [
        (validate_session(session_path), relative)
        for session_path, relative in discovered
    ]
    source_content = _fingerprint(
        sorted(session.session_fingerprint for session, _ in validated)
    )
    source_id = (
        dataset_input.source_id
        or f"source-{source_content.removeprefix('sha256:')[:16]}"
    )
    candidates = [
        CompositeSession(
            session_id=session.session_id,
            session_fingerprint=session.session_fingerprint,
            image_set_fingerprint=session.image_set_fingerprint,
            image_triple_fingerprints=session.image_triple_fingerprints,
            source_ids=(source_id,),
            locations=(SessionLocation(source_id, relative),),
        )
        for session, relative in validated
    ]
    return _compose(path.name, {source_id: path}, candidates)


def merge_datasets(
    inputs: Sequence[DatasetInput], output: Path, *, name: str
) -> CompositeDataset:
    """Recursively flatten inputs without copying any source data."""

    if not inputs:
        raise ValueError("dataset inputを1個以上指定してください")
    if not name.strip():
        raise ValueError("composite nameは空にできません")
    if output.exists():
        raise FileExistsError(f"既存manifestはin-place更新しません: {output}")
    sources: dict[str, Path] = {}
    sessions: list[CompositeSession] = []
    for dataset_input in inputs:
        nested = _from_root(dataset_input)
        for source in nested.sources:
            existing = sources.get(source.source_id)
            if existing is not None and existing.resolve() != source.path.resolve():
                raise ValueError(
                    f"同じsource_idが異なるrootを指しています: {source.source_id}"
                )
            sources[source.source_id] = source.path.resolve()
        sessions.extend(nested.sessions)
    composite = _compose(name, sources, sessions)
    save_composite_manifest(composite, output)
    return composite


def save_composite_manifest(composite: CompositeDataset, path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"既存manifestはin-place更新しません: {path}")
    _atomic_json_save(path, composite.to_dict())


def _expect_keys(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}はobjectが必要です")
    actual = set(value)
    if actual != keys:
        raise ValueError(
            f"{label}のkeyが不正です: missing={sorted(keys - actual)}, unknown={sorted(actual - keys)}"
        )
    return value


def load_composite_manifest(path: Path) -> CompositeDataset:
    """Load a flattened manifest and revalidate all referenced immutable
    data."""

    path = path.resolve(strict=True)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"composite manifestを読み込めません: {path}: {error}"
        ) from error
    root = _expect_keys(
        raw,
        {
            "kind",
            "schema_version",
            "name",
            "sources",
            "sessions",
            "content_fingerprint",
            "composite_fingerprint",
        },
        "composite",
    )
    if (
        root["kind"] != COMPOSITE_KIND
        or root["schema_version"] != COMPOSITE_SCHEMA_VERSION
    ):
        raise ValueError("composite kind/schema versionが不正です")
    if type(root["name"]) is not str or not root["name"]:
        raise ValueError("composite nameが不正です")
    if not isinstance(root["sources"], list) or not isinstance(root["sessions"], list):
        raise ValueError("composite sources/sessionsはarrayが必要です")
    sources: dict[str, Path] = {}
    for index, item in enumerate(root["sources"]):
        source = _expect_keys(item, {"source_id", "path"}, f"sources[{index}]")
        if type(source["source_id"]) is not str or not source["source_id"]:
            raise ValueError("source_idが不正です")
        if type(source["path"]) is not str:
            raise ValueError("source pathが不正です")
        source_id = source["source_id"]
        if source_id in sources:
            raise ValueError(f"source_idが重複しています: {source_id}")
        source_path = Path(source["path"]).expanduser().resolve(strict=True)
        sources[source_id] = source_path
    candidates: list[CompositeSession] = []
    for index, item in enumerate(root["sessions"]):
        session = _expect_keys(
            item,
            {
                "session_id",
                "session_fingerprint",
                "image_set_fingerprint",
                "image_triple_fingerprints",
                "source_ids",
                "locations",
            },
            f"sessions[{index}]",
        )
        if any(
            type(session[key]) is not str or not session[key]
            for key in ("session_id", "session_fingerprint", "image_set_fingerprint")
        ):
            raise ValueError(f"sessions[{index}]のidentifier/fingerprintが不正です")
        if not isinstance(session["image_triple_fingerprints"], list) or any(
            type(value) is not str for value in session["image_triple_fingerprints"]
        ):
            raise ValueError(f"sessions[{index}].image_triple_fingerprintsが不正です")
        if not isinstance(session["source_ids"], list) or not session["source_ids"]:
            raise ValueError(f"sessions[{index}].source_idsが不正です")
        if not isinstance(session["locations"], list) or not session["locations"]:
            raise ValueError(f"sessions[{index}].locationsが不正です")
        source_ids = tuple(session["source_ids"])
        if any(
            type(source_id) is not str or not source_id or source_id not in sources
            for source_id in source_ids
        ):
            raise ValueError(f"sessions[{index}].source_idsに未知sourceがあります")
        if source_ids != tuple(sorted(set(source_ids))):
            raise ValueError(
                f"sessions[{index}].source_idsはsort済みかつ一意である必要があります"
            )
        locations: list[SessionLocation] = []
        location_pairs: set[tuple[str, str]] = set()
        location_source_ids: set[str] = set()
        validated_reference: _ValidatedSession | None = None
        for location_index, item_location in enumerate(session["locations"]):
            location = _expect_keys(
                item_location,
                {"source_id", "relative_path"},
                f"sessions[{index}].locations[{location_index}]",
            )
            if (
                type(location["source_id"]) is not str
                or location["source_id"] not in sources
            ):
                raise ValueError("session location source_idが不正です")
            if type(location["relative_path"]) is not str:
                raise ValueError("session relative_pathが不正です")
            location_pair = (location["source_id"], location["relative_path"])
            if location_pair in location_pairs:
                raise ValueError("session location pairが重複しています")
            location_pairs.add(location_pair)
            location_source_ids.add(location["source_id"])
            session_path = _resolve_session_location(
                sources[location["source_id"]], location["relative_path"]
            )
            validated = validate_session(session_path)
            if (
                validated.session_id != session["session_id"]
                and location["relative_path"] != "."
            ):
                raise ValueError("manifest session_idがdirectory名と一致しません")
            if validated.session_fingerprint != session["session_fingerprint"]:
                raise ValueError("manifest session fingerprintが実データと一致しません")
            if validated.image_set_fingerprint != session["image_set_fingerprint"]:
                raise ValueError(
                    "manifest image-set fingerprintが実データと一致しません"
                )
            if tuple(sorted(validated.image_triple_fingerprints)) != tuple(
                sorted(session["image_triple_fingerprints"])
            ):
                raise ValueError(
                    "manifest image-triple fingerprintsが実データと一致しません"
                )
            validated_reference = validated
            locations.append(
                SessionLocation(location["source_id"], location["relative_path"])
            )
        if set(source_ids) != location_source_ids:
            raise ValueError("session source_idsとlocation source集合が一致しません")
        assert validated_reference is not None
        candidates.append(
            CompositeSession(
                session_id=str(session["session_id"]),
                session_fingerprint=str(session["session_fingerprint"]),
                image_set_fingerprint=str(session["image_set_fingerprint"]),
                image_triple_fingerprints=tuple(
                    sorted(str(value) for value in session["image_triple_fingerprints"])
                ),
                source_ids=source_ids,
                locations=tuple(
                    sorted(
                        locations, key=lambda item: (item.source_id, item.relative_path)
                    )
                ),
            )
        )
    composite = _compose(str(root["name"]), sources, candidates)
    if composite.content_fingerprint != root["content_fingerprint"]:
        raise ValueError("content_fingerprintが一致しません")
    if composite.composite_fingerprint != root["composite_fingerprint"]:
        raise ValueError("composite_fingerprintが一致しません")
    return composite


def resolve_dataset_inputs(
    *, manifest: Path | None = None, roots: Sequence[Path] = ()
) -> CompositeDataset:
    if manifest is not None and roots:
        raise ValueError("data.manifestとdata.rootsは同時指定できません")
    if manifest is not None:
        return load_composite_manifest(manifest)
    if not roots:
        raise ValueError("data.manifestまたはdata.rootsを指定してください")
    inputs = tuple(DatasetInput(path=root) for root in roots)
    composites = [_from_root(item) for item in inputs]
    sources: dict[str, Path] = {}
    sessions: list[CompositeSession] = []
    for composite in composites:
        for source in composite.sources:
            existing = sources.get(source.source_id)
            if existing is not None and existing != source.path:
                raise ValueError(
                    f"同じsource_idが異なるrootを指しています: {source.source_id}"
                )
            sources[source.source_id] = source.path
        sessions.extend(composite.sessions)
    return _compose("resolved", sources, sessions)


@attrs.frozen
class PasteVolumeSample:
    """Stable metadata and immutable source paths for one pad view."""

    sample_id: str
    source_ids: tuple[str, ...]
    session_id: str
    session_fingerprint: str
    machine_id: str
    paste_id: str
    paste_lot: str | None
    nozzle_diameter_mm: float
    board_signature: str
    pad_id: str
    pad_index: int
    view_number: int
    pre_path: Path
    post_path: Path
    mask_path: Path
    width: int
    height: int
    pixel_per_mm: float
    measured_volume_ul: float
    session_pad_count: int
    pad_view_count: int
    dispense_mode: str

    @property
    def loss_weight(self) -> float:
        return 1.0 / (self.session_pad_count * self.pad_view_count)

    @property
    def image_area_pixels(self) -> int:
        return self.width * self.height

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height

    def to_dict(self) -> dict[str, object]:
        value = attrs.asdict(self)
        for key in ("pre_path", "post_path", "mask_path"):
            value[key] = str(value[key])
        value["loss_weight"] = self.loss_weight
        return value


def _session_path(composite: CompositeDataset, session: CompositeSession) -> Path:
    sources = {source.source_id: source.path for source in composite.sources}
    location = min(
        session.locations, key=lambda item: (item.source_id, item.relative_path)
    )
    return _resolve_session_location(
        sources[location.source_id], location.relative_path
    )


def build_sample_index(composite: CompositeDataset) -> tuple[PasteVolumeSample, ...]:
    """Create one stable sample per view without modifying source data."""

    samples: list[PasteVolumeSample] = []
    for session in composite.sessions:
        session_path = _session_path(composite, session)
        metadata = _load_metadata(session_path / "metadata.json")
        for pad in sorted(metadata.pads, key=lambda item: item.index):
            for view in sorted(pad.views, key=lambda item: item.number):
                sample_id = _fingerprint(
                    {
                        "session_fingerprint": session.session_fingerprint,
                        "pad_index": pad.index,
                        "view_number": view.number,
                    }
                )
                x0, y0, x1, y1 = view.pixel_rect
                samples.append(
                    PasteVolumeSample(
                        sample_id=sample_id,
                        source_ids=session.source_ids,
                        session_id=session.session_id,
                        session_fingerprint=session.session_fingerprint,
                        machine_id=metadata.machine.machine_id,
                        paste_id=metadata.paste.paste_id,
                        paste_lot=metadata.paste.lot,
                        nozzle_diameter_mm=metadata.nozzle.diameter_mm,
                        board_signature=metadata.board.signature,
                        pad_id=pad.pad_id,
                        pad_index=pad.index,
                        view_number=view.number,
                        pre_path=_resolve_capture_path(session_path, view.pre, "pre"),
                        post_path=_resolve_capture_path(
                            session_path, view.post, "post"
                        ),
                        mask_path=_resolve_capture_path(
                            session_path, view.mask, "mask"
                        ),
                        width=x1 - x0,
                        height=y1 - y0,
                        pixel_per_mm=metadata.camera.pixel_per_mm,
                        measured_volume_ul=pad.measured_volume_ul,
                        session_pad_count=len(metadata.pads),
                        pad_view_count=len(pad.views),
                        dispense_mode=pad.execution.applied_mode
                        or pad.resolved.dispense_mode,
                    )
                )
    samples.sort(key=lambda item: item.sample_id)
    if len({sample.sample_id for sample in samples}) != len(samples):
        raise ValueError("sample_idが衝突しています")
    return tuple(samples)


def sample_index_fingerprint(samples: Sequence[PasteVolumeSample]) -> str:
    return _fingerprint(
        [
            {
                "sample_id": sample.sample_id,
                "session_id": sample.session_id,
                "source_ids": sorted(sample.source_ids),
                "target": sample.measured_volume_ul,
                "width": sample.width,
                "height": sample.height,
                "pixel_per_mm": sample.pixel_per_mm,
                "dispense_mode": sample.dispense_mode,
            }
            for sample in sorted(samples, key=lambda item: item.sample_id)
        ]
    )


def save_sample_index(samples: Sequence[PasteVolumeSample], path: Path) -> None:
    _atomic_json_save(
        path,
        {
            "kind": "pcbasm-paste-volume-sample-index",
            "schema_version": 1,
            "sample_index_fingerprint": sample_index_fingerprint(samples),
            "samples": [sample.to_dict() for sample in samples],
        },
    )


def index_samples_by_id(
    samples: Sequence[PasteVolumeSample],
) -> dict[str, PasteVolumeSample]:
    indexed = {sample.sample_id: sample for sample in samples}
    if len(indexed) != len(samples):
        raise ValueError("sample_idが重複しています")
    return indexed


@attrs.frozen
class SplitManifest:
    """Immutable session-level primary or held-out split assignment."""

    composite_fingerprint: str
    sample_index_fingerprint: str
    seed: int
    mode: TrainingMode
    train_sample_ids: tuple[str, ...]
    validation_sample_ids: tuple[str, ...]
    test_sample_ids: tuple[str, ...]
    split_fingerprint: str
    kind: str = SPLIT_KIND
    schema_version: int = SPLIT_SCHEMA_VERSION

    def sample_ids(self, split: SplitName) -> tuple[str, ...]:
        if split == "train":
            return self.train_sample_ids
        if split == "validation":
            return self.validation_sample_ids
        return self.test_sample_ids

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "composite_fingerprint": self.composite_fingerprint,
            "sample_index_fingerprint": self.sample_index_fingerprint,
            "seed": self.seed,
            "mode": self.mode,
            "train_sample_ids": list(self.train_sample_ids),
            "validation_sample_ids": list(self.validation_sample_ids),
            "test_sample_ids": list(self.test_sample_ids),
            "split_fingerprint": self.split_fingerprint,
        }


def make_split_manifest(
    *,
    samples: Sequence[PasteVolumeSample],
    composite_fingerprint: str,
    seed: int,
    mode: TrainingMode,
    train_sample_ids: Iterable[str],
    validation_sample_ids: Iterable[str],
    test_sample_ids: Iterable[str],
) -> SplitManifest:
    groups = {
        "train": tuple(sorted(train_sample_ids)),
        "validation": tuple(sorted(validation_sample_ids)),
        "test": tuple(sorted(test_sample_ids)),
    }
    fingerprint_payload = {
        "kind": SPLIT_KIND,
        "schema_version": SPLIT_SCHEMA_VERSION,
        "composite_fingerprint": composite_fingerprint,
        "sample_index_fingerprint": sample_index_fingerprint(samples),
        "seed": seed,
        "mode": mode,
        **{f"{name}_sample_ids": list(ids) for name, ids in groups.items()},
    }
    return SplitManifest(
        composite_fingerprint=composite_fingerprint,
        sample_index_fingerprint=fingerprint_payload["sample_index_fingerprint"],
        seed=seed,
        mode=mode,
        train_sample_ids=groups["train"],
        validation_sample_ids=groups["validation"],
        test_sample_ids=groups["test"],
        split_fingerprint=_fingerprint(fingerprint_payload),
    )


def create_session_split(
    samples: Sequence[PasteVolumeSample],
    composite_fingerprint: str,
    *,
    seed: int = 42,
    train_ratio: float = 0.70,
    validation_ratio: float = 0.15,
    test_ratio: float = 0.15,
    mode: TrainingMode = "base",
) -> SplitManifest:
    """Make a deterministic split whose indivisible unit is a complete
    session."""

    if not samples:
        raise ValueError("sample indexが空です")
    session_ids = sorted({sample.session_id for sample in samples})
    minimum_sessions = 3 if mode == "base" else 2
    if len(session_ids) < minimum_sessions:
        raise ValueError(
            f"{mode} trainingには最低{minimum_sessions} sessionが必要です: "
            f"actual={len(session_ids)}"
        )
    group_split = split_groups(
        session_ids,
        seed=seed,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
        test_ratio=test_ratio,
        require_test=mode == "base",
    )
    train_sessions = set(group_split.train_group_ids)
    validation_sessions = set(group_split.validation_group_ids)
    test_sessions = set(group_split.test_group_ids)
    return make_split_manifest(
        samples=samples,
        composite_fingerprint=composite_fingerprint,
        seed=seed,
        mode=mode,
        train_sample_ids=(
            sample.sample_id
            for sample in samples
            if sample.session_id in train_sessions
        ),
        validation_sample_ids=(
            sample.sample_id
            for sample in samples
            if sample.session_id in validation_sessions
        ),
        test_sample_ids=(
            sample.sample_id for sample in samples if sample.session_id in test_sessions
        ),
    )


def validate_split_manifest(
    split: SplitManifest,
    samples: Sequence[PasteVolumeSample],
    *,
    expected_composite_fingerprint: str,
) -> None:
    if split.composite_fingerprint != expected_composite_fingerprint:
        raise ValueError("splitのcomposite fingerprintがdatasetと一致しません")
    if split.sample_index_fingerprint != sample_index_fingerprint(samples):
        raise ValueError("splitのsample index fingerprintがdatasetと一致しません")
    groups = (
        split.train_sample_ids,
        split.validation_sample_ids,
        split.test_sample_ids,
    )
    flat = [sample_id for group in groups for sample_id in group]
    if len(flat) != len(set(flat)):
        raise ValueError("split間でsample_idが重複しています")
    expected = {sample.sample_id for sample in samples}
    if set(flat) != expected:
        raise ValueError("splitのsample集合がdatasetと一致しません")
    by_id = index_samples_by_id(samples)
    session_assignment: dict[str, str] = {}
    for name, ids in zip(("train", "validation", "test"), groups, strict=True):
        for sample_id in ids:
            session_id = by_id[sample_id].session_id
            previous = session_assignment.setdefault(session_id, name)
            if previous != name:
                raise ValueError("同一sessionが複数splitへ跨がっています")
    if not split.train_sample_ids or not split.validation_sample_ids:
        raise ValueError("train/validation splitにはsampleが必要です")
    if split.mode == "base" and not split.test_sample_ids:
        raise ValueError("base splitのtestが空です")


def save_split_manifest(split: SplitManifest, path: Path) -> None:
    _atomic_json_save(path, split.to_dict())


def load_split_manifest(
    path: Path,
    *,
    expected_composite_fingerprint: str | None = None,
) -> SplitManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"split manifestを読み込めません: {path}: {error}") from error
    data = _expect_keys(
        raw,
        {
            "kind",
            "schema_version",
            "composite_fingerprint",
            "sample_index_fingerprint",
            "seed",
            "mode",
            "train_sample_ids",
            "validation_sample_ids",
            "test_sample_ids",
            "split_fingerprint",
        },
        "split",
    )
    if data["kind"] != SPLIT_KIND or data["schema_version"] != SPLIT_SCHEMA_VERSION:
        raise ValueError("split kind/schema versionが不正です")
    if type(data["seed"]) is not int or data["mode"] not in ("base", "finetune"):
        raise ValueError("split seed/modeが不正です")
    string_keys = (
        "composite_fingerprint",
        "sample_index_fingerprint",
        "split_fingerprint",
    )
    if any(type(data[key]) is not str or not data[key] for key in string_keys):
        raise ValueError("split fingerprintが不正です")
    id_groups: list[tuple[str, ...]] = []
    for key in ("train_sample_ids", "validation_sample_ids", "test_sample_ids"):
        value = data[key]
        if not isinstance(value, list) or any(type(item) is not str for item in value):
            raise ValueError(f"split.{key}はstring arrayが必要です")
        id_groups.append(tuple(value))
    payload = {key: data[key] for key in data if key != "split_fingerprint"}
    if _fingerprint(payload) != data["split_fingerprint"]:
        raise ValueError("split_fingerprintが内容と一致しません")
    if (
        expected_composite_fingerprint is not None
        and data["composite_fingerprint"] != expected_composite_fingerprint
    ):
        raise ValueError("splitのcomposite fingerprintがdatasetと一致しません")
    return SplitManifest(
        composite_fingerprint=str(data["composite_fingerprint"]),
        sample_index_fingerprint=str(data["sample_index_fingerprint"]),
        seed=int(data["seed"]),
        mode=data["mode"],
        train_sample_ids=id_groups[0],
        validation_sample_ids=id_groups[1],
        test_sample_ids=id_groups[2],
        split_fingerprint=str(data["split_fingerprint"]),
    )


@attrs.frozen
class PasteVolumeItem:
    sample: PasteVolumeSample
    preprocessed: PreprocessedSample
    target_volume_ul: float
    loss_weight: float
    placement_seed: int


@attrs.frozen
class PasteVolumeBatch:
    image_6ch: Tensor
    valid_pixel_mask: Tensor
    pixel_per_mm: Tensor
    target_volume_ul: Tensor
    loss_weight: Tensor
    sample_ids: tuple[str, ...]
    session_ids: tuple[str, ...]
    samples: tuple[PasteVolumeSample, ...]

    def pin_memory(self) -> PasteVolumeBatch:
        return attrs.evolve(
            self,
            image_6ch=self.image_6ch.pin_memory(),
            valid_pixel_mask=self.valid_pixel_mask.pin_memory(),
            pixel_per_mm=self.pixel_per_mm.pin_memory(),
            target_volume_ul=self.target_volume_ul.pin_memory(),
            loss_weight=self.loss_weight.pin_memory(),
        )


def load_preprocessed_sample(
    sample: PasteVolumeSample,
    *,
    constraints: ImageConstraints = ImageConstraints(),
    training: bool = False,
    global_seed: int = 0,
    epoch: int = 0,
    augmentation: AugmentationConfig = AugmentationConfig(),
) -> PreprocessedSample:
    angle, scale = augmentation_parameters(
        sample.sample_id,
        global_seed=global_seed,
        epoch=epoch,
        augmentation=augmentation
        if training
        else attrs.evolve(augmentation, enabled=False),
    )
    pre = decode_image(str(sample.pre_path), mode=ImageReadMode.RGB)
    post = decode_image(str(sample.post_path), mode=ImageReadMode.RGB)
    mask = decode_image(str(sample.mask_path), mode=ImageReadMode.GRAY)
    return preprocess_rgb_pair(
        pre,
        post,
        sample.pixel_per_mm,
        constraints=constraints,
        angle_degrees=angle,
        scale=scale,
        geometry_mask=mask,
    )


class PasteVolumeDataset(Dataset[PasteVolumeItem]):
    def __init__(
        self,
        samples: Sequence[PasteVolumeSample],
        *,
        constraints: ImageConstraints = ImageConstraints(),
        training: bool = False,
        global_seed: int = 0,
        augmentation: AugmentationConfig = AugmentationConfig(),
    ) -> None:
        incompatible = next(
            (
                sample
                for sample in samples
                if sample.height < constraints.min_size
                or sample.width < constraints.min_size
            ),
            None,
        )
        if incompatible is not None:
            raise ValueError(
                f"sample {incompatible.sample_id}は設定された最小画像サイズ"
                f"{constraints.min_size}pxを満たしません: "
                f"{incompatible.width}x{incompatible.height}"
            )
        self._samples = tuple(samples)
        self._constraints = constraints
        self._training = training
        self._global_seed = global_seed
        self._augmentation = augmentation
        self._epoch = 0

    @property
    def samples(self) -> tuple[PasteVolumeSample, ...]:
        return self._samples

    def set_epoch(self, epoch: int) -> None:
        if epoch < 0:
            raise ValueError("epochは0以上が必要です")
        self._epoch = epoch

    def __len__(self) -> int:
        return len(self._samples)

    @override
    def __getitem__(self, index: int) -> PasteVolumeItem:
        sample = self._samples[index]
        preprocessed = load_preprocessed_sample(
            sample,
            constraints=self._constraints,
            training=self._training,
            global_seed=self._global_seed,
            epoch=self._epoch,
            augmentation=self._augmentation,
        )
        digest = hashlib.sha256(
            f"{self._global_seed}:{self._epoch}:{sample.sample_id}:placement".encode()
        ).digest()
        return PasteVolumeItem(
            sample=sample,
            preprocessed=preprocessed,
            target_volume_ul=sample.measured_volume_ul,
            loss_weight=sample.loss_weight,
            placement_seed=int.from_bytes(digest[:8], "big"),
        )


def collate_paste_volume(
    items: Sequence[PasteVolumeItem],
    *,
    training: bool = False,
    stride: int = 32,
) -> PasteVolumeBatch:
    padded = pad_image_samples(
        tuple(item.preprocessed.image_6ch for item in items),
        tuple(item.preprocessed.valid_pixel_mask for item in items),
        tuple(item.placement_seed for item in items),
        training=training,
        stride=stride,
    )
    return PasteVolumeBatch(
        image_6ch=padded.images,
        valid_pixel_mask=padded.valid_pixel_masks,
        pixel_per_mm=torch.tensor(
            [[item.preprocessed.pixel_per_mm] for item in items], dtype=torch.float32
        ),
        target_volume_ul=torch.tensor(
            [item.target_volume_ul for item in items], dtype=torch.float32
        ),
        loss_weight=torch.tensor(
            [item.loss_weight for item in items], dtype=torch.float32
        ),
        sample_ids=tuple(item.sample.sample_id for item in items),
        session_ids=tuple(item.sample.session_id for item in items),
        samples=tuple(item.sample for item in items),
    )


def select_session_balanced_samples(
    samples: Sequence[PasteVolumeSample],
    sample_ids: Iterable[str],
    *,
    limit: int,
    seed: int,
) -> tuple[PasteVolumeSample, ...]:
    """Select deterministically in round-robin session order."""

    return select_group_balanced(
        samples,
        sample_ids,
        item_id_of=lambda sample: sample.sample_id,
        group_id_of=lambda sample: sample.session_id,
        limit=limit,
        seed=seed,
    )


def summarize_dataset(composite: CompositeDataset) -> DatasetSummary:
    samples = build_sample_index(composite)
    if not samples:
        raise ValueError("datasetにsampleがありません")

    def counts(values: Iterable[str]) -> dict[str, int]:
        result: dict[str, int] = defaultdict(int)
        for value in values:
            result[value] += 1
        return dict(sorted(result.items()))

    return DatasetSummary(
        composite_fingerprint=composite.composite_fingerprint,
        source_count=len(composite.sources),
        session_count=len(composite.sessions),
        sample_count=len(samples),
        machine_counts=counts(sample.machine_id for sample in samples),
        paste_lot_counts=counts(
            f"{sample.paste_id}:{sample.paste_lot or '<none>'}" for sample in samples
        ),
        nozzle_counts=counts(f"{sample.nozzle_diameter_mm:g}" for sample in samples),
        dispense_mode_counts=counts(sample.dispense_mode for sample in samples),
        min_width=min(sample.width for sample in samples),
        max_width=max(sample.width for sample in samples),
        min_height=min(sample.height for sample in samples),
        max_height=max(sample.height for sample in samples),
        min_measured_volume_ul=min(sample.measured_volume_ul for sample in samples),
        max_measured_volume_ul=max(sample.measured_volume_ul for sample in samples),
    )


def validate_datasets(inputs: Sequence[DatasetInput]) -> DatasetValidationReport:
    if not inputs:
        raise ValueError("dataset inputを1個以上指定してください")
    composites = [_from_root(dataset_input) for dataset_input in inputs]
    sources: dict[str, Path] = {}
    sessions: list[CompositeSession] = []
    for composite in composites:
        for source in composite.sources:
            existing = sources.get(source.source_id)
            if existing is not None and existing != source.path:
                raise ValueError(
                    f"同じsource_idが異なるrootを指しています: {source.source_id}"
                )
            sources[source.source_id] = source.path
        sessions.extend(composite.sessions)
    composite = _compose("validation", sources, sessions)
    samples = build_sample_index(composite)
    return DatasetValidationReport(
        composite_fingerprint=composite.composite_fingerprint,
        content_fingerprint=composite.content_fingerprint,
        source_count=len(composite.sources),
        session_count=len(composite.sessions),
        sample_count=len(samples),
    )
