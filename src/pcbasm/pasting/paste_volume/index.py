"""学習に使う sample の index.

:mod:`pcbasm.pasting.paste_volume.session` が構造を保証した後に、画像を 1 度だけ
decode して寸法を突き合わせ、前処理を通せない cell を隔離する。

失敗の切り分けは 2 段階。**session が壊れている**（view ごとに寸法が違う、``pixel_rect``
と実画像が食い違う）場合は build 全体を失敗させ、**その cell だけが使えない**（前処理が
分散 0 を理由に拒否する）場合は :class:`PasteVolumeRejection` へ隔離する。

拒否を index を作る時点で決めるのは、``materialize`` の途中で sample を落とすと
``plan_epoch`` の計画と食い違い、checkpoint の batch plan 一致検査が壊れるため。

**この選別は「全 view・変換なし」の 1 通りしか試さない。** view を間引いた部分集合や
回転後の有効領域では分散がさらに下がるので、ここを通った sample が学習中に前処理を
通せない可能性は残る。サイズ由来の拒否は ``validate_augmentation`` で構造的に潰して
あるので、残るのは定数に近い画像だけ。落ちるときは ``ValueError`` なので黙って
母集団が減ることはない。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Self

import attrs
from torch import Tensor

from ml.artifact.fingerprint import fingerprint_json
from ml.data.image import (
    NO_AUGMENTATION,
    ImageConstraints,
    PreprocessedMultiViewSample,
    decode_rgb_image,
)
from pcbasm.pasting.dataset.metadata import DatasetCapturedView
from pcbasm.pasting.paste_volume.session import (
    METADATA_FILE_NAME,
    PasteVolumeCell,
    PasteVolumeSession,
)

INDEX_SCHEMA_VERSION = 1

# sample_id へ入れる session fingerprint の桁数。"sha256:" を除いた先頭から取る。
_FINGERPRINT_DIGITS = 12
_FINGERPRINT_PREFIX_LENGTH = len("sha256:")


@attrs.frozen
class PasteVolumeViewPaths:
    """1 view の塗布前後画像の絶対 path."""

    number: int
    pre: Path
    post: Path


@attrs.frozen
class PasteVolumeSampleEntry:
    """学習 sample 1 件の index 項目.

    ``sample_id`` は session fingerprint と cell index から作る。view 番号は入れない。
    多視点は 1 sample の中で畳むので、cell が sample の単位になる。
    """

    sample_id: str
    session_fingerprint: str
    session_label: str
    machine_id: str
    cell_key: str
    index: int
    order: int | None
    is_blank: bool
    commanded_volume_ul: float | None
    measured_volume_ul: float
    pixel_per_mm: float
    source_height: int
    source_width: int
    session_sample_count: int
    views: tuple[PasteVolumeViewPaths, ...]

    @property
    def view_count(self) -> int:
        """この sample が持つ view の数."""

        return len(self.views)


@attrs.frozen
class PasteVolumeRejection:
    """学習に使えないと判定した cell と、その理由."""

    sample_id: str
    session_label: str
    reason: str


@attrs.frozen
class PasteVolumeSampleIndex:
    """複数 session をまたいだ sample index.

    ``dataset_fingerprint`` は session の内容と ``constraints`` から決まる。root の並び順にも
    mount 位置にも依存しない。

    ``constraints`` を含めるのは、これが変わると使える sample の集合が変わるため。
    含めないと、別の母集団で作った checkpoint と split manifest を同一と見なしてしまう。
    """

    entries: tuple[PasteVolumeSampleEntry, ...]
    rejections: tuple[PasteVolumeRejection, ...]
    constraints: ImageConstraints
    dataset_fingerprint: str

    @classmethod
    def from_roots(
        cls, roots: Sequence[Path], *, constraints: ImageConstraints
    ) -> tuple[Self | None, str | None]:
        """収集 session の directory かその親を走査して index を作る."""

        sessions: list[PasteVolumeSession] = []
        for directory in _session_directories(roots):
            session, error = PasteVolumeSession.load(directory)
            if session is None:
                return None, f"{directory.name}: {error}"
            sessions.append(session)
        if not sessions:
            return None, f"session が見つかりません: {[str(root) for root in roots]}"
        return cls.build(sessions, constraints=constraints)

    @classmethod
    def build(
        cls, sessions: Sequence[PasteVolumeSession], *, constraints: ImageConstraints
    ) -> tuple[Self | None, str | None]:
        """読み込み済み session から index を作る.

        同じ内容の session が複数あっても 1 回ぶんとして扱う。
        """

        unique = _deduplicated(sessions)
        entries: list[PasteVolumeSampleEntry] = []
        rejections: list[PasteVolumeRejection] = []
        for session in unique:
            for cell in sorted(session.cells, key=lambda cell: cell.index):
                entry, reason = _entry_for_cell(
                    session,
                    cell,
                    constraints=constraints,
                    sample_count=len(session.cells),
                )
                if entry is None:
                    return None, f"{session.label} cell {cell.index}: {reason}"
                if isinstance(entry, PasteVolumeRejection):
                    rejections.append(entry)
                    continue
                entries.append(entry)
        if not entries:
            return None, "使える sample がありません"
        return (
            cls(
                entries=tuple(entries),
                rejections=tuple(rejections),
                constraints=constraints,
                dataset_fingerprint=_dataset_fingerprint(
                    unique, constraints=constraints
                ),
            ),
            None,
        )

    @property
    def smallest_source_size(self) -> int:
        """全 sample の元画像の最小辺 [px].

        ``ImageConstraints.validate_augmentation`` へ渡し、どの epoch でも前処理後が
        下限を割らないことを構造的に保証するのに使う。
        """

        return min(
            min(entry.source_height, entry.source_width) for entry in self.entries
        )

    def sample_groups(self) -> dict[str, str]:
        """``{sample_id: cell_key}``.

        split の不可分単位は物理 cell.
        """

        return {entry.sample_id: entry.cell_key for entry in self.entries}

    def entry_for(self, sample_id: str) -> PasteVolumeSampleEntry:
        """sample_id から entry を引く.

        未知の ID は呼び出し側の不変条件違反なので例外にする。
        """

        for entry in self.entries:
            if entry.sample_id == sample_id:
                return entry
        raise ValueError(f"index に無い sample_id です: {sample_id}")


def _session_directories(roots: Sequence[Path]) -> list[Path]:
    """渡された root 自身か、その直下から session directory を集める."""

    directories: list[Path] = []
    for root in roots:
        if (root / METADATA_FILE_NAME).is_file():
            directories.append(root)
            continue
        directories.extend(
            sorted(
                child
                for child in root.iterdir()
                if (child / METADATA_FILE_NAME).is_file()
            )
            if root.is_dir()
            else []
        )
    return directories


def _deduplicated(sessions: Sequence[PasteVolumeSession]) -> list[PasteVolumeSession]:
    """内容が同じ session を 1 つに畳み、fingerprint 順に並べる."""

    unique: dict[str, PasteVolumeSession] = {}
    for session in sessions:
        unique.setdefault(session.session_fingerprint, session)
    return [unique[key] for key in sorted(unique)]


def _entry_for_cell(
    session: PasteVolumeSession,
    cell: PasteVolumeCell,
    *,
    constraints: ImageConstraints,
    sample_count: int,
) -> tuple[PasteVolumeSampleEntry | PasteVolumeRejection | None, str | None]:
    """1 cell を entry か rejection へ落とす。session が壊れていれば理由を返す."""

    views = sorted(cell.views, key=lambda view: view.number)
    stacks = [
        [
            decode_rgb_image(session.image_path(view.pre)),
            decode_rgb_image(session.image_path(view.post)),
        ]
        for view in views
    ]
    if error := _validate_image_sizes(
        stacks, views=views, crop_size_px=session.metadata.config.crop_size_px
    ):
        return None, error
    sample_id = _sample_id(session.session_fingerprint, cell.index)
    _, reason = PreprocessedMultiViewSample.preprocess(
        stacks, constraints=constraints, parameters=NO_AUGMENTATION
    )
    if reason is not None:
        return (
            PasteVolumeRejection(
                sample_id=sample_id, session_label=session.label, reason=reason
            ),
            None,
        )
    height, width = int(stacks[0][0].shape[1]), int(stacks[0][0].shape[2])
    return (
        PasteVolumeSampleEntry(
            sample_id=sample_id,
            session_fingerprint=session.session_fingerprint,
            session_label=session.label,
            machine_id=session.metadata.machine.machine_id,
            cell_key=_cell_key(cell),
            index=cell.index,
            order=cell.order,
            is_blank=cell.is_blank,
            commanded_volume_ul=cell.commanded_volume_ul,
            measured_volume_ul=cell.measured_volume_ul,
            pixel_per_mm=session.pixel_per_mm,
            source_height=height,
            source_width=width,
            session_sample_count=sample_count,
            views=tuple(
                PasteVolumeViewPaths(
                    number=view.number,
                    pre=session.image_path(view.pre),
                    post=session.image_path(view.post),
                )
                for view in views
            ),
        ),
        None,
    )


def _validate_image_sizes(
    stacks: Sequence[Sequence[Tensor]],
    *,
    views: Sequence[DatasetCapturedView],
    crop_size_px: int,
) -> str | None:
    """実画像の寸法が cell 内で揃い、metadata と一致することを確かめる.

    ``plan_epoch`` は index の寸法から前処理後 shape を求め、collate は実 decode から
    求める。ここがずれると bucket と pixel budget が黙って壊れる。
    """

    sizes = {
        (int(image.shape[1]), int(image.shape[2]))
        for stack in stacks
        for image in stack
    }
    if len(sizes) != 1:
        return f"cell 内の画像は同じ高さ・幅が必要です: {sorted(sizes)}"
    height, width = next(iter(sizes))
    for view in views:
        left, top, right, bottom = view.pixel_rect
        if (bottom - top, right - left) != (height, width):
            return (
                f"view {view.number} の pixel_rect と画像の寸法が違います: "
                f"{(bottom - top, right - left)} と {(height, width)}"
            )
    if (height, width) != (crop_size_px, crop_size_px):
        return (
            f"画像の寸法が crop_size_px と違います: {(height, width)} と {crop_size_px}"
        )
    return None


def _sample_id(session_fingerprint: str, index: int) -> str:
    digits = session_fingerprint[
        _FINGERPRINT_PREFIX_LENGTH : _FINGERPRINT_PREFIX_LENGTH + _FINGERPRINT_DIGITS
    ]
    return f"{digits}:{index:06d}"


def _cell_key(cell: PasteVolumeCell) -> str:
    """物理 cell を指す split group の鍵.

    座標で作るので、同じ銅板の同じ位置なら session をまたいで同じ値になる。
    """

    rect = cell.cell
    return f"{rect.x:.3f},{rect.y:.3f},{rect.width:.3f},{rect.height:.3f}"


def _dataset_fingerprint(
    sessions: Sequence[PasteVolumeSession], *, constraints: ImageConstraints
) -> str:
    """収集内容と前処理の制約から fingerprint を作る.

    並べ直さない。``_deduplicated`` が既に fingerprint 順へ揃えているので、渡された
    順序は root の並び順に依存しない。
    """

    return fingerprint_json(
        {
            "schema_version": INDEX_SCHEMA_VERSION,
            "sessions": [session.session_fingerprint for session in sessions],
            "constraints": attrs.asdict(constraints),
        }
    )


__all__ = [
    "INDEX_SCHEMA_VERSION",
    "PasteVolumeRejection",
    "PasteVolumeSampleEntry",
    "PasteVolumeSampleIndex",
    "PasteVolumeViewPaths",
]
