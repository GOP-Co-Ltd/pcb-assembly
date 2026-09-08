"""Wheel 同梱の設定 group の公開契約.

計画 §13.2 / §15.2 に対応する。
"""

from __future__ import annotations

from pathlib import Path

from ml.config.composition import ConfigComposition
from ml.config.packaged import PackagedConfiguration
from ml.serialization import make_strict_converter
from ml.training.loop import TrainerConfig

# 同梱 group と、その option が構造化される到達先。
#
# group を増やしたら必ずここへ登録する。登録の無い group はテストが落ちる。
#
# 「同梱した TOML が実在の attrs クラスへ strict に落ちる」ことを機械で守るための表で、
# ``tests/ml/tuning/test_integration.py`` の既定値二重管理ガードも同じ対応を使う。
PACKAGED_GROUP_TARGETS: dict[str, type] = {"trainer": TrainerConfig}


class TestPackagedConfiguration:
    """同梱 conf の所在と列挙."""

    def test_locates_an_existing_directory(self):
        located = PackagedConfiguration.locate()

        assert located.root.is_dir()
        assert located.validate() is None

    def test_lists_the_trainer_group(self):
        assert "trainer" in PackagedConfiguration.locate().group_names()

    def test_lists_the_edge_option_of_the_trainer_group(self):
        assert "edge" in PackagedConfiguration.locate().option_names("trainer")

    def test_sorts_group_and_option_names(self):
        located = PackagedConfiguration.locate()

        groups = located.group_names()
        assert list(groups) == sorted(groups)
        for group in groups:
            options = located.option_names(group)
            assert list(options) == sorted(options)

    def test_returns_no_options_for_an_unknown_group(self):
        assert PackagedConfiguration.locate().option_names("absent") == ()

    def test_reports_a_root_that_does_not_exist(self, tmp_path: Path):
        error = PackagedConfiguration(root=tmp_path / "absent").validate()

        assert error is not None
        assert "absent" in error


class TestPackagedOptionsStructure:
    """同梱 TOML のキーが到達先 attrs のフィールドに収まっている."""

    def test_every_group_has_a_registered_target(self):
        groups = PackagedConfiguration.locate().group_names()

        assert groups != ()
        assert set(groups) <= set(PACKAGED_GROUP_TARGETS)

    def test_every_packaged_option_structures_strictly(self):
        located = PackagedConfiguration.locate()
        converter = make_strict_converter()

        checked: list[str] = []
        for group, target in PACKAGED_GROUP_TARGETS.items():
            for option in located.option_names(group):
                composition, error = ConfigComposition.from_arguments(
                    (f"{group}={option}",), configuration_root=located.root
                )
                assert error is None, f"{group}={option}: {error}"
                assert composition is not None
                value, structure_error = composition.structure(
                    target, converter=converter
                )
                assert structure_error is None, f"{group}={option}: {structure_error}"
                assert value is not None
                checked.append(f"{group}={option}")

        # 走査対象が空でも上の assert は通ってしまうため、検査した件数を固定する
        assert checked != []
