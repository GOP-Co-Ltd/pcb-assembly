"""学習に使う sample index の公開契約.

index は「その cell が学習に使えるか」を決める層。session が構造を保証した後に、 画像を 1 度だけ decode
して寸法を突き合わせ、前処理を通せない cell を隔離する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import attrs
import pytest

from ml.data.image import ImageConstraints
from pcbasm.pasting.paste_volume.index import PasteVolumeSampleIndex
from pcbasm.pasting.paste_volume.session import PasteVolumeSession
from tests.pcbasm.pasting.paste_volume.helpers import (
    CROP_SIZE_PX,
    MEASURED_RATIO,
    VIEW_COUNT,
    SyntheticCell,
    corrupt_metadata,
    write_session,
)

CONSTRAINTS = ImageConstraints()

CELLS = (
    SyntheticCell(index=1, commanded_volume_ul=0.10, x_mm=0.5),
    SyntheticCell(index=2, commanded_volume_ul=0.30, x_mm=2.7),
    SyntheticCell(index=3, commanded_volume_ul=None, x_mm=4.9),
)


def _index(*roots: Path) -> PasteVolumeSampleIndex:
    index, reason = PasteVolumeSampleIndex.from_roots(roots, constraints=CONSTRAINTS)
    assert index is not None, reason
    return index


def _built(root: Path, **overrides: object) -> PasteVolumeSampleIndex:
    return _index(write_session(root, cells=CELLS, **overrides))  # type: ignore[arg-type]


def _rejected(*roots: Path) -> str:
    index, reason = PasteVolumeSampleIndex.from_roots(roots, constraints=CONSTRAINTS)
    assert index is None
    assert reason is not None
    return reason


class TestSampleEntries:
    """1 cell が 1 entry になること."""

    def test_keeps_one_entry_per_cell_including_blanks(self, tmp_path: Path):
        """学習 sample の単位は view ではなく cell.

        多視点は model 内部の平均 pooling で畳むので、view を別 sample にしない。
        """

        index = _built(tmp_path / "session")

        assert len(index.entries) == len(CELLS)
        assert [entry.view_count for entry in index.entries] == [VIEW_COUNT] * 3
        assert [entry.is_blank for entry in index.entries] == [False, False, True]

    def test_sample_id_pairs_the_session_with_the_cell_index(self, tmp_path: Path):
        """sample_id は session fingerprint の先頭 12 桁と cell index だけで決まる.

        view 番号は入らない。cell が sample の単位なので、view を増減しても 1 cell から できる ID は
        1 つ。
        """

        index = _built(tmp_path / "session")

        for entry in index.entries:
            digits = entry.session_fingerprint.removeprefix("sha256:")[:12]
            assert entry.sample_id == f"{digits}:{entry.index:06d}"
        assert len({entry.sample_id for entry in index.entries}) == len(CELLS)

    def test_sample_id_survives_a_rename_and_a_different_location(self, tmp_path: Path):
        """展開先と directory 名を変えても sample_id が変わらない.

        split manifest と checkpoint がこの ID に載るので、mount を変えただけで
        学習をやり直す羽目にならないことを固定する。
        """

        first = _built(tmp_path / "one" / "plate-a")
        second = _built(tmp_path / "another" / "plate-b")

        assert [entry.sample_id for entry in first.entries] == [
            entry.sample_id for entry in second.entries
        ]

    def test_sample_ids_do_not_collide_across_sessions(self, tmp_path: Path):
        """別 session の同じ cell index が同じ ID にならない.

        index は session 内で一意なだけなので、session fingerprint を混ぜないと衝突する。
        """

        index = _index(
            write_session(tmp_path / "a", cells=CELLS, machine_id="m1"),
            write_session(tmp_path / "b", cells=CELLS, machine_id="m2"),
        )

        sample_ids = [entry.sample_id for entry in index.entries]

        assert len(index.entries) == 2 * len(CELLS)
        assert len(set(sample_ids)) == len(sample_ids)

    def test_carries_the_label_and_the_collection_resolution(self, tmp_path: Path):
        """教師値は指令量ではなく計量由来の measured_volume_ul.

        実データでは ``measured = commanded x k``（k は session ごとの 1 定数）なので、
        両者は一致しない。指令量を教師値に使う取り違えを見分けられるようにする。
        """

        entries = _built(tmp_path / "session").entries

        assert entries[0].commanded_volume_ul == 0.10
        assert entries[0].measured_volume_ul == pytest.approx(0.10 * MEASURED_RATIO)
        assert entries[2].measured_volume_ul == 0.0
        assert entries[2].commanded_volume_ul is None
        assert all(entry.pixel_per_mm > 0 for entry in entries)

    def test_records_the_source_size_from_the_decoded_image(self, tmp_path: Path):
        """実 PNG の寸法を持つ.

        plan_epoch はこの値から前処理後の shape を求め、collate 側は実 decode から
        求める。両者が一致することが resume 契約の前提なので、metadata ではなく 画像から取る。
        """

        entries = _built(tmp_path / "session").entries

        assert {(entry.source_height, entry.source_width) for entry in entries} == {
            (CROP_SIZE_PX, CROP_SIZE_PX)
        }

    def test_counts_the_whole_session_for_the_loss_weight(self, tmp_path: Path):
        """収集 session 全体の cell 数を持つ.

        loss weight が 1/N_session なので、split で切った後の件数ではなく session 全体。
        """

        index = _index(
            write_session(tmp_path / "a", cells=CELLS),
            write_session(
                tmp_path / "b",
                cells=(
                    *CELLS,
                    SyntheticCell(index=4, commanded_volume_ul=0.2, x_mm=7.1),
                ),
            ),
        )

        counts = {
            entry.session_label: entry.session_sample_count for entry in index.entries
        }

        assert sorted(counts.values()) == [3, 4]

    def test_orders_views_by_number_and_resolves_absolute_paths(self, tmp_path: Path):
        entry = _built(tmp_path / "session").entries[0]

        assert [view.number for view in entry.views] == list(range(VIEW_COUNT))
        assert all(view.pre.is_file() and view.post.is_file() for view in entry.views)

    def test_exposes_the_smallest_source_size(self, tmp_path: Path):
        """どの sample も下限を割らないかの検証に使う最小辺.

        これが ImageConstraints.validate_augmentation へ渡り、どの epoch でも
        前処理後が下限を割らないことを構造的に保証する。
        """

        assert _built(tmp_path / "session").smallest_source_size == CROP_SIZE_PX

    def test_looks_up_an_entry_by_sample_id(self, tmp_path: Path):
        index = _built(tmp_path / "session")
        expected = index.entries[1]

        assert index.entry_for(expected.sample_id) is expected


class TestDatasetFingerprint:
    """内容だけで dataset fingerprint が決まること."""

    def test_does_not_depend_on_the_order_of_the_roots(self, tmp_path: Path):
        first = write_session(tmp_path / "a", cells=CELLS, machine_id="m1")
        second = write_session(tmp_path / "b", cells=CELLS, machine_id="m2")

        forward = _index(first, second)
        backward = _index(second, first)

        assert forward.dataset_fingerprint == backward.dataset_fingerprint

    def test_treats_the_same_session_given_twice_as_one(self, tmp_path: Path):
        """同一内容の session を重複して渡しても 1 回ぶんになる.

        zip と展開 directory が並んでいる実データ構成でも安全にする。
        """

        root = write_session(tmp_path / "a", cells=CELLS)
        copy = write_session(tmp_path / "b", cells=CELLS)

        once = _index(root)
        twice = _index(root, copy)

        assert twice.dataset_fingerprint == once.dataset_fingerprint
        assert len(twice.entries) == len(once.entries)

    def test_does_not_change_with_the_directory_name(self, tmp_path: Path):
        """展開先と directory 名を変えても同じ値になる.

        checkpoint と split manifest がこの値との一致を要求するので、mount を変えた だけで
        resume が落ちないことを固定する。
        """

        first = _built(tmp_path / "one" / "plate-a")
        second = _built(tmp_path / "another" / "plate-b")

        assert first.dataset_fingerprint == second.dataset_fingerprint

    def test_orders_entries_independently_of_the_root_order(self, tmp_path: Path):
        """並び順が root の渡し順に依存しない.

        session を fingerprint 順へ揃えていることの観測点。dataset_fingerprint が
        並べ直さずに済むのはこの性質があるから。
        """

        first = write_session(tmp_path / "a", cells=CELLS, machine_id="m1")
        second = write_session(tmp_path / "b", cells=CELLS, machine_id="m2")

        forward = _index(first, second)
        backward = _index(second, first)

        assert [entry.sample_id for entry in forward.entries] == [
            entry.sample_id for entry in backward.entries
        ]

    def test_changes_when_a_session_differs(self, tmp_path: Path):
        one = _built(tmp_path / "a")
        other = _index(write_session(tmp_path / "b", cells=CELLS, machine_id="other"))

        assert one.dataset_fingerprint != other.dataset_fingerprint

    def test_changes_when_the_constraints_change(self, tmp_path: Path):
        """前処理の制約が変わると fingerprint も変わる.

        制約は使える sample の集合を決める。含めないと、別の母集団で作った checkpoint と split
        manifest を同一と見なしてしまう。
        """

        root = write_session(tmp_path / "session", cells=CELLS)
        loose, _ = PasteVolumeSampleIndex.from_roots([root], constraints=CONSTRAINTS)
        strict, _ = PasteVolumeSampleIndex.from_roots(
            [root], constraints=attrs.evolve(CONSTRAINTS, maximum_size=256)
        )
        assert loose is not None and strict is not None

        assert loose.dataset_fingerprint != strict.dataset_fingerprint

    def test_keeps_the_constraints_it_screened_with(self, tmp_path: Path):
        """使った制約を持ち歩く.

        collator が違う制約を使うと「拒否は index を作る時点で済ませる」前提が崩れるので、 突き合わせられるようにする。
        """

        assert _built(tmp_path / "session").constraints == CONSTRAINTS


class TestSplitGroups:
    """分割の不可分単位が物理 cell であること."""

    def test_groups_the_same_physical_cell_across_sessions(self, tmp_path: Path):
        """同じ座標の cell は session をまたいで同じ group になる.

        銅板の背景テクスチャが train と test へ分かれて漏れるのを防ぐ。
        """

        index = _index(
            write_session(tmp_path / "a", cells=CELLS, machine_id="m1"),
            write_session(tmp_path / "b", cells=CELLS, machine_id="m2"),
        )
        groups = index.sample_groups()

        assert set(groups) == {entry.sample_id for entry in index.entries}
        assert len(set(groups.values())) == len(CELLS)

    def test_groups_by_position_even_when_the_cell_index_differs(self, tmp_path: Path):
        """同じ座標なら cell index が違っても同じ group になる.

        index は session 内の連番でしかない。銅板の同じ場所を指しているかは座標だけが 決めるので、index を
        group にすると背景テクスチャが split をまたいで漏れる。
        """

        index = _index(
            write_session(
                tmp_path / "a",
                cells=(SyntheticCell(index=1, commanded_volume_ul=0.1, x_mm=0.5),),
                machine_id="m1",
            ),
            write_session(
                tmp_path / "b",
                cells=(SyntheticCell(index=7, commanded_volume_ul=0.2, x_mm=0.5),),
                machine_id="m2",
            ),
        )

        assert [entry.index for entry in index.entries] in ([1, 7], [7, 1])
        assert len(set(index.sample_groups().values())) == 1

    def test_separates_cells_at_different_positions(self, tmp_path: Path):
        index = _built(tmp_path / "session")

        assert len({entry.cell_key for entry in index.entries}) == len(CELLS)


class TestOrderingAndSizes:
    """並び順と寸法の集計."""

    def test_orders_cells_by_index_even_when_a_blank_comes_first(self, tmp_path: Path):
        """塗布しない cell が小さい index を持っていても index 昇順になる.

        収集 schema は samples と blanks を別配列で持つので、畳んだだけでは 「塗布した cell
        が先」の順になる。
        """

        root = write_session(
            tmp_path / "session",
            cells=(
                SyntheticCell(index=5, commanded_volume_ul=0.2, x_mm=0.5),
                SyntheticCell(index=2, commanded_volume_ul=None, x_mm=2.7),
                SyntheticCell(index=9, commanded_volume_ul=0.3, x_mm=4.9),
            ),
        )

        index = _index(root)

        assert [entry.index for entry in index.entries] == [2, 5, 9]
        assert [entry.is_blank for entry in index.entries] == [True, False, False]

    def test_reports_the_smallest_side_across_sessions(self, tmp_path: Path):
        """最小辺は session をまたいだ最小値.

        augmentation の下限検証に使うので、いちばん小さい画像が下限を割らないことを 見なければ意味がない。
        """

        index = _index(
            write_session(tmp_path / "big", cells=CELLS, crop_size_px=CROP_SIZE_PX + 8),
            write_session(tmp_path / "small", cells=CELLS, machine_id="m2"),
        )

        assert index.smallest_source_size == CROP_SIZE_PX

    def test_orders_views_by_number_even_when_the_metadata_is_shuffled(
        self, tmp_path: Path
    ):
        """収集 metadata の view 配列が昇順でなくても番号順に並べ直す.

        pre と post の対応は view 番号で決まる。並びが崩れたまま collate すると、 別の view どうしを 1
        組として扱う。
        """

        def reverse_views(document: dict[str, Any]) -> None:
            for sample in document["samples"]:
                sample["views"] = list(reversed(sample["views"]))

        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS), reverse_views
        )

        entry = _index(root).entries[0]

        assert [view.number for view in entry.views] == list(range(VIEW_COUNT))


class TestRejection:
    """前処理を通せない cell を隔離すること."""

    def test_moves_a_constant_image_out_of_the_entries(self, tmp_path: Path):
        """全画素が同値の cell は学習に使えないので entries から外す.

        SampleLayerNorm が分散 0 を拒否する。materialize の途中で落とすと plan_epoch の
        計画と食い違って resume が壊れるので、index を作る時点で決めておく。
        """

        root = write_session(
            tmp_path / "session",
            cells=(
                *CELLS,
                SyntheticCell(index=9, commanded_volume_ul=0.2, x_mm=7.1, uniform=True),
            ),
        )
        index = _index(root)

        assert [entry.index for entry in index.entries] == [1, 2, 3]
        assert [
            rejection.sample_id.endswith(":000009") for rejection in index.rejections
        ] == [True]
        assert index.rejections[0].reason

    def test_a_rejected_cell_never_appears_in_a_split_group(self, tmp_path: Path):
        root = write_session(
            tmp_path / "session",
            cells=(
                *CELLS,
                SyntheticCell(index=9, commanded_volume_ul=0.2, x_mm=7.1, uniform=True),
            ),
        )
        index = _index(root)

        assert index.rejections[0].sample_id not in index.sample_groups()

    def test_does_not_count_a_rejected_cell_in_the_loss_weight(self, tmp_path: Path):
        """隔離した cell を weight の分母に入れない.

        weight は session 間の寄与を揃えるためのもの。

        学習へ寄与しない cell を数に入れると拒否の多い session が過小評価される。
        """

        root = write_session(
            tmp_path / "session",
            cells=(
                *CELLS,
                SyntheticCell(index=8, commanded_volume_ul=0.2, x_mm=7.1, uniform=True),
                SyntheticCell(index=9, commanded_volume_ul=0.2, x_mm=9.3, uniform=True),
            ),
        )
        index = _index(root)

        assert len(index.rejections) == 2
        assert {entry.session_sample_count for entry in index.entries} == {len(CELLS)}

    def test_reports_a_dataset_where_every_cell_is_rejected(self, tmp_path: Path):
        root = write_session(
            tmp_path / "session",
            cells=(SyntheticCell(index=1, commanded_volume_ul=0.2, uniform=True),),
        )

        assert "使える sample がありません" in _rejected(root)


class TestStructuralRejection:
    """収集 session が壊れている場合に build 全体を失敗させること."""

    def test_reports_an_image_whose_size_differs_from_the_metadata(
        self, tmp_path: Path
    ):
        """PNG の実寸が pixel_rect と食い違う session を拒否する.

        plan_epoch は entry の寸法から前処理後 shape を求め、collate は実 decode から
        求める。ここがずれると bucket と pixel budget が黙って壊れるので、衛生検査では なく resume
        契約を支える検証。
        """

        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document["samples"][0]["views"][0].update(
                pixel_rect=[0, 0, CROP_SIZE_PX + 4, CROP_SIZE_PX + 4]
            ),
        )

        assert "pixel_rect" in _rejected(root)

    def test_reports_an_image_that_disagrees_with_the_crop_size(self, tmp_path: Path):
        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document["config"].update(crop_size_px=CROP_SIZE_PX + 8),
        )

        assert "crop_size_px" in _rejected(root)

    def test_reports_views_of_a_cell_that_differ_in_size(self, tmp_path: Path):
        """1 cell の中で view ごとに寸法が違う session を拒否する.

        preprocess は全 view が同じ高さ幅であることを要求する。
        """

        root = write_session(tmp_path / "session", cells=CELLS)
        session, _ = PasteVolumeSession.load(root)
        assert session is not None
        odd = write_session(
            tmp_path / "odd", cells=CELLS, crop_size_px=CROP_SIZE_PX + 8
        )
        target = session.image_path(session.cells[0].views[1].pre)
        target.write_bytes((odd / f"pre/{CELLS[0].index:06d}.01.png").read_bytes())

        assert "同じ高さ" in _rejected(root)

    def test_propagates_the_reason_from_a_broken_session(self, tmp_path: Path):
        root = corrupt_metadata(
            write_session(tmp_path / "session", cells=CELLS),
            lambda document: document.update(schema_version=1),
        )

        assert "schema_version" in _rejected(root)

    def test_reports_an_empty_root(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()

        assert "session が見つかりません" in _rejected(empty)
