"""同梱 config group と argv 合成の公開契約.

検査は 3 層ある。

1. 同梱 TOML の**形**（group ごとの top-level key、既定値の二重定義、空の option）
2. 全 group x 全 option が strict に**構造化**でき、最小の上書きで**validate** も通ること
3. group 解決と上書き解決の**振り分け**（experiment preset、held-out、resume と初期 weight）

1 と 2 は走査で回すので、走査対象が空だと素通りする。範囲そのものを別に固定し、
検査器が働くことを合成入力で確かめる（``memory/negative-assertion-needs-self-check.md``）。
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import get_args, get_type_hints

import attrs
import pytest

from ml.config.packaged import PackagedConfiguration
from ml.data.image import ImageConstraints
from ml.paste_volume.batch import PasteVolumeCollator
from ml.paste_volume.experiment import (
    BASE_LAYER_NAMES,
    PasteVolumeDataConfig,
    PasteVolumeExperimentConfig,
    compose_experiment,
    packaged_configuration,
)
from ml.paste_volume.task import PasteVolumeTrainingConfig
from ml.tuning.search_space import ParameterDistribution

# 同梱 conf が持つ group と option。増減させたらここも直す。
#
# 走査で回す検査は対象が空でも緑になるので、木そのものを 1 箇所へ固定する。
PACKAGED_TREE: dict[str, tuple[str, ...]] = {
    "experiment": ("base", "cell_split", "fine_tune", "search"),
    "hyperparameter_search": ("base_optuna",),
    "logger": ("mlflow",),
    "trainer": ("gpu", "pi"),
}

# root config の field 名。group 名がこれに入っていれば「その group の table だけを
# 書く」option、入っていなければ複数 group をまたぐ preset とみなす。
ROOT_FIELDS = frozenset(
    field.name for field in attrs.fields(PasteVolumeExperimentConfig)
)

# 複数 group をまたいでよい preset の group。
#
# ここに登録の無い group は ROOT_FIELDS のいずれかでなければならない。新しい group を
# 足したときに、どちらの規則を当てるかを必ず決めさせる。
COMPOSITE_GROUPS = frozenset({"experiment"})

# 機械固有なので同梱 conf へ書かず、argv から渡す値。
DATASET_ROOT = "/abs/paste-volume-datasets"
HELD_OUT_SESSION = "plate-47.5x20-20260908T144137.001+0900"
TRACKING_URI = "https://mlflow.example/"
STORAGE_URI = "sqlite:////abs/optuna.db"

# どの合成でも要る最小の argv。experiment preset は split_dimension を宣言する
# 唯一の場所なので省けない。
MINIMUM_ARGUMENTS = ("experiment=base", f'data.roots=["{DATASET_ROOT}"]')

# group を選んだときだけ要る、機械固有の値の上書き。
GROUP_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "logger": (f"logger.tracking_uri={TRACKING_URI}",),
    "hyperparameter_search": (f"hyperparameter_search.storage_uri={STORAGE_URI}",),
}


def _located() -> PackagedConfiguration:
    return packaged_configuration()


def _option_path(group: str, option: str) -> Path:
    return _located().root / group / f"{option}.toml"


def _layer_paths() -> list[Path]:
    """Base 層と全 option file。合成に載りうる TOML をすべて挙げる."""

    root = _located().root
    return [
        *(root / f"{name}.toml" for name in BASE_LAYER_NAMES),
        *(
            _option_path(group, option)
            for group in _located().group_names()
            for option in _located().option_names(group)
        ),
    ]


def _layer_data(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _nested_attrs_class(annotation: object) -> type | None:
    """Annotation が attrs クラス（``| None`` 込み）ならその class を返す.

    ``field.type`` は ``from __future__ import annotations`` の下では文字列なので、
    解決済みの annotation から辿る。既定値の実体から辿ると、既定値を持たない
    必須 field（``data`` / ``trainer``）の中身が丸ごと走査から漏れる。
    """

    candidates = get_args(annotation) or (annotation,)
    classes = [
        item for item in candidates if isinstance(item, type) and attrs.has(item)
    ]
    if len(classes) != 1:
        return None
    return classes[0]


def _duplicated_defaults(
    target: type, data: Mapping[str, object], *, prefix: str = ""
) -> list[str]:
    """``data`` のうち、到達先 attrs の既定値と等しい値を書いているキーを返す.

    既定値が factory の field は比較対象にしない（呼び出しに副作用がありうる）。

    手口は ``tests/ml/tuning/test_integration.py`` と同じで、入れ子の辿り方だけを
    既定値の実体から annotation へ変えてある。
    """

    fields = {field.name: field for field in attrs.fields(target)}
    annotations = get_type_hints(target)
    duplicated: list[str] = []
    for name, value in data.items():
        field = fields.get(name)
        if field is None:
            # 未知キーは strict converter 側の検査が拒否を守る
            continue
        nested = _nested_attrs_class(annotations.get(name))
        if isinstance(value, Mapping) and nested is not None:
            duplicated.extend(
                _duplicated_defaults(nested, value, prefix=f"{prefix}{name}.")
            )
            continue
        default = field.default
        # ``attrs.Factory`` は stub 上 overload された関数なので ``isinstance`` の
        # 第 2 引数にできない。factory 属性の有無で判定する
        if default is attrs.NOTHING or hasattr(default, "factory"):
            continue
        # ``bool`` は ``int`` の部分型なので、値の一致だけでは区別できない
        if value == default and type(value) is type(default):
            duplicated.append(f"{prefix}{name}")
    return duplicated


def _shape_offenders(group: str, data: Mapping[str, object]) -> list[str]:
    """Option file の top-level key が group の規則から外れていれば理由を返す.

    ``ml/config/conf/trainer/edge.toml`` は key を file root へ直書きしている。単体で
    ``TrainerConfig`` へ構造化する用途だから成り立つ形で、複数 group を merge する
    こちらでは別 group の table と衝突する。
    """

    keys = sorted(data)
    if not keys:
        return [f"{group}: top-level key がありません"]
    if group in ROOT_FIELDS:
        return [f"{group}: top-level key が {key!r}" for key in keys if key != group]
    if group not in COMPOSITE_GROUPS:
        return [f"{group}: root field でも preset group でもありません"]
    return [
        f"{group}: root field でない top-level key {key!r}"
        for key in keys
        if key not in ROOT_FIELDS
    ]


def _probe_value(distribution: ParameterDistribution) -> object:
    """その分布が実際に引く値と同じ型の 1 点を返す.

    ``suggest_int`` は int を返すので、integer 分布の境界（float で書く）をそのまま
    上書きへ流すと strict converter に拒否される。探索経路と同じ型で試す。
    """

    if distribution.choices:
        return distribution.choices[0]
    if distribution.kind == "integer" and distribution.low is not None:
        return int(distribution.low)
    return distribution.low


def _arguments_for(group: str, option: str) -> tuple[str, ...]:
    """その option を選ぶ最小の argv."""

    return (*MINIMUM_ARGUMENTS, f"{group}={option}", *GROUP_ARGUMENTS.get(group, ()))


def _composed(arguments: tuple[str, ...]) -> PasteVolumeExperimentConfig:
    value, error = compose_experiment(arguments)

    assert error is None, f"{arguments}: {error}"
    assert value is not None
    return value


def _rejected(arguments: tuple[str, ...]) -> str:
    value, error = compose_experiment(arguments)

    assert value is None
    assert error is not None
    return error


def _valid_arguments(group: str, option: str) -> tuple[str, ...]:
    """構造化した結果を見て、その次元で意味を持つ上書きまで足した argv.

    ``held_out_session`` は session 次元でだけ必須で、cell 次元では拒否される。
    どちらになるかは選んだ experiment preset が決めるので、一度組んでから足す。
    """

    arguments = _arguments_for(group, option)
    if _composed(arguments).data.split_dimension == "session":
        return (*arguments, f"data.held_out_session={HELD_OUT_SESSION}")
    return arguments


class TestPackagedTree:
    """同梱 conf の木そのもの."""

    def test_the_configuration_root_is_a_directory(self):
        located = _located()

        assert located.root.is_dir()
        assert located.validate() is None

    def test_it_is_not_the_core_configuration_root(self):
        """``ml.config`` の conf を指していないこと.

        :meth:`PackagedConfiguration.locate` を使うと ``trainer/edge.toml`` だけの
        別 root を見て、以降の走査がドメイン側の TOML を 1 枚も検査しなくなる。
        """

        assert _located().root != PackagedConfiguration.locate().root

    def test_the_base_layers_exist(self):
        root = _located().root

        assert BASE_LAYER_NAMES != ()
        for name in BASE_LAYER_NAMES:
            assert (root / f"{name}.toml").is_file()

    def test_it_lists_the_expected_groups_and_options(self):
        located = _located()

        assert located.group_names() == tuple(sorted(PACKAGED_TREE))
        for group, options in PACKAGED_TREE.items():
            assert located.option_names(group) == options

    def test_every_layer_carries_at_least_one_key(self):
        """空の option file を置かないこと.

        既定値と同じ値を書けないので中身が無くなる option が出る。空の file は
        「選んでも何も起きない」ので、選択肢として残すと読み手を誤らせる。
        """

        paths = _layer_paths()

        assert paths != []
        for path in paths:
            assert _layer_data(path) != {}, path


class TestOptionFileShape:
    """Option file の top-level key が group の規則に収まっている."""

    def test_every_group_is_a_root_field_or_a_registered_preset(self):
        groups = set(_located().group_names())

        assert groups != set()
        assert groups <= ROOT_FIELDS | COMPOSITE_GROUPS

    def test_every_option_file_keeps_its_shape(self):
        checked: list[str] = []
        offenders: list[str] = []
        for group in _located().group_names():
            for option in _located().option_names(group):
                data = _layer_data(_option_path(group, option))
                offenders.extend(_shape_offenders(group, data))
                checked.append(f"{group}={option}")

        assert checked != []
        assert offenders == []

    @pytest.mark.parametrize(
        "data",
        (
            pytest.param({"max_epochs": 60, "monitor": "loss"}, id="root-level-keys"),
            pytest.param({"trainer": {}, "data": {}}, id="another-group-table"),
            pytest.param({}, id="empty"),
        ),
    )
    def test_the_shape_check_rejects_a_field_group_option(self, data: dict):
        """検査器が働くこと.

        ``root-level-keys`` は ``ml/config/conf/trainer/edge.toml`` の形そのもので、
        ドメイン側へそのまま持ち込むと別 group と衝突する。
        """

        assert _shape_offenders("trainer", data) != []

    def test_the_shape_check_accepts_a_field_group_option(self):
        assert _shape_offenders("trainer", {"trainer": {"max_epochs": 60}}) == []

    @pytest.mark.parametrize(
        "data",
        (
            pytest.param({"trainerr": {}}, id="typo-key"),
            pytest.param({"experiment": {}}, id="its-own-name"),
            pytest.param({}, id="empty"),
        ),
    )
    def test_the_shape_check_rejects_a_preset_option(self, data: dict):
        assert _shape_offenders("experiment", data) != []

    def test_the_shape_check_accepts_a_preset_option(self):
        data = {"run_kind": "fine-tune", "data": {}, "model": {}, "trainer": {}}

        assert _shape_offenders("experiment", data) == []

    def test_the_shape_check_rejects_an_unregistered_group(self):
        assert _shape_offenders("runner", {"trainer": {}}) != []


class TestDefaultsAreNotDuplicated:
    """同梱 TOML は差分だけを書く（既定値は attrs にのみ置く）."""

    def test_no_layer_repeats_an_attrs_default(self):
        checked: list[str] = []
        offenders: list[str] = []
        for path in _layer_paths():
            data = _layer_data(path)
            offenders.extend(
                f"{path.name}: {name}"
                for name in _duplicated_defaults(PasteVolumeExperimentConfig, data)
            )
            checked.append(path.name)

        assert checked != []
        assert offenders == []

    @pytest.mark.parametrize(
        ("data", "expected"),
        (
            pytest.param(
                {"trainer": {"weight_decay": 1e-4}},
                ["trainer.weight_decay"],
                id="required-nested-field",
            ),
            pytest.param(
                {"data": {"validation_ratio": 0.15}},
                ["data.validation_ratio"],
                id="required-nested-field-of-the-domain",
            ),
            pytest.param(
                {"run_kind": "base-train"}, ["run_kind"], id="root-level-field"
            ),
            pytest.param(
                {"trainer": {"compile_options": {"backend": "inductor"}}},
                ["trainer.compile_options.backend"],
                id="two-levels-of-nesting",
            ),
        ),
    )
    def test_it_detects_a_default_written_into_a_layer(
        self, data: dict, expected: list[str]
    ):
        """検査器が働くこと.

        ``data`` と ``trainer`` は既定値を持たない必須 field なので、入れ子を既定値の
        実体から辿る版では中身が丸ごと素通りする。annotation から辿ることでしか
        見えない。
        """

        assert _duplicated_defaults(PasteVolumeExperimentConfig, data) == expected

    @pytest.mark.parametrize(
        "data",
        (
            pytest.param({"trainer": {"weight_decay": 2.0e-4}}, id="different-value"),
            pytest.param({"trainer": {"deterministic": 1}}, id="int-against-bool"),
            pytest.param({"trainer": {"max_epochs": 200}}, id="no-default"),
            pytest.param({"data": {"split_dimension": "session"}}, id="no-default-too"),
        ),
    )
    def test_it_leaves_a_value_that_is_not_a_duplicated_default(self, data: dict):
        """検査器が広すぎないこと.

        ``int-against-bool`` は ``1 == True`` が成り立つ組で、型を見ない版だけが
        誤検出する。``no-default`` は既定値を持たない field で、TOML が唯一の
        出典になる。
        """

        assert _duplicated_defaults(PasteVolumeExperimentConfig, data) == []


class TestStructuring:
    """全 group x 全 option が strict に構造化でき、最小の上書きで成立する."""

    def test_every_option_structures_strictly(self):
        checked: list[str] = []
        for group in _located().group_names():
            for option in _located().option_names(group):
                _composed(_arguments_for(group, option))
                checked.append(f"{group}={option}")

        assert checked != []

    def test_every_option_validates_with_the_minimum_overrides(self):
        checked: list[str] = []
        for group in _located().group_names():
            for option in _located().option_names(group):
                config = _composed(_valid_arguments(group, option))
                assert config.validate() is None, f"{group}={option}"
                checked.append(f"{group}={option}")

        assert checked != []

    def test_the_group_arguments_name_existing_groups(self):
        """機械固有の値を足す表が、実在の group を指していること.

        group を rename すると表が死に、その group の option は必須 field を欠いた
        まま検査され続ける。
        """

        assert set(GROUP_ARGUMENTS) <= set(_located().group_names())

    def test_it_requires_an_experiment_preset(self):
        error = _rejected((f'data.roots=["{DATASET_ROOT}"]',))

        assert "split_dimension" in error

    def test_it_accepts_the_same_arguments_with_an_experiment_preset(self):
        assert _composed(MINIMUM_ARGUMENTS).data.split_dimension == "session"

    @pytest.mark.parametrize(
        "override",
        (
            pytest.param("data.nonexistent=1", id="unknown-key-in-a-group"),
            pytest.param("nonexistent=1", id="unknown-key-at-the-root"),
            pytest.param("data.held_out_sessions=x", id="typo-of-a-real-key"),
        ),
    )
    def test_it_rejects_an_unknown_key(self, override: str):
        error = _rejected((*MINIMUM_ARGUMENTS, override))

        assert "nonexistent" in error or "held_out_sessions" in error

    def test_it_accepts_the_key_that_the_typo_meant(self):
        """未知キー拒否の検査が働くこと.

        同じ形の上書きが通ることを示さないと、``key=value`` 経路そのものが
        壊れていても拒否として観測されてしまう。
        """

        config = _composed((*MINIMUM_ARGUMENTS, "data.held_out_session=x"))

        assert config.data.held_out_session == "x"

    def test_it_rejects_a_split_dimension_outside_the_contract(self):
        error = _rejected((*MINIMUM_ARGUMENTS, "data.split_dimension=machine"))

        assert "machine" in error

    def test_it_accepts_the_other_split_dimension(self):
        config = _composed((*MINIMUM_ARGUMENTS, "data.split_dimension=cell"))

        assert config.data.split_dimension == "cell"

    def test_it_rejects_an_integer_where_a_float_is_required(self):
        """暗黙の型変換を拒むこと.

        ``0`` と ``0.0`` を混ぜると config fingerprint が変わり、同じ設定の run が
        別物として記録される。
        """

        error = _rejected((*MINIMUM_ARGUMENTS, "data.validation_ratio=1"))

        assert "float" in error

    def test_it_reports_an_unknown_option_of_a_known_group(self):
        error = _rejected((*MINIMUM_ARGUMENTS, "trainer=absent"))

        assert "trainer" in error
        assert "absent" in error


class TestGroupResolution:
    """Group 選択が到達先の値へ効く."""

    def test_the_base_preset_selects_the_session_dimension(self):
        config = _composed(_valid_arguments("experiment", "base"))

        assert config.data.split_dimension == "session"
        assert config.data.held_out_session == HELD_OUT_SESSION
        assert config.run_kind == "base-train"

    def test_the_cell_split_preset_selects_the_cell_dimension(self):
        config = _composed(_valid_arguments("experiment", "cell_split"))

        assert config.data.split_dimension == "cell"
        assert config.data.held_out_session is None
        assert config.run_kind == "cell-split-train"

    def test_the_search_preset_shortens_the_trial(self):
        """HPO trial の 60 epoch / patience 10（仕様書 §3）が効くこと.

        base.toml の 200 epoch / patience 15 のままだと 1 trial が本番 run と
        同じ長さになり、20 trial を積めない。
        """

        config = _composed(_valid_arguments("experiment", "search"))

        assert config.trainer.max_epochs == 60
        assert config.trainer.early_stopping_patience == 10
        assert config.data.split_dimension == "session"
        assert config.run_kind == "hpo-trial"

    def test_the_fine_tune_preset_spans_three_groups(self):
        config = _composed(_valid_arguments("experiment", "fine_tune"))

        assert config.model.fine_tune is True
        assert config.trainer.learning_rate == pytest.approx(1e-4)
        assert config.trainer.early_stopping_patience == 10
        assert config.data.split_dimension == "session"

    def test_the_gpu_trainer_turns_on_mixed_precision_only(self):
        config = _composed(_valid_arguments("trainer", "gpu"))

        assert config.trainer.automatic_mixed_precision_enabled is True
        assert config.trainer.compile_enabled is True
        assert config.trainer.max_epochs == 200
        assert config.trainer.gradient_accumulation == 1

    def test_the_pi_trainer_keeps_full_precision_and_takes_a_deadline(self):
        config = _composed(_valid_arguments("trainer", "pi"))

        assert config.trainer.automatic_mixed_precision_enabled is False
        assert config.trainer.gradient_accumulation == 4
        assert config.trainer.deadline_seconds == pytest.approx(3300.0)

    def test_the_logger_group_is_the_only_way_to_get_a_logger(self):
        assert _composed(MINIMUM_ARGUMENTS).logger is None

        config = _composed(_valid_arguments("logger", "mlflow"))

        assert config.logger is not None
        assert config.logger.experiment_name == "paste-volume"
        assert config.logger.tracking_uri == TRACKING_URI

    def test_the_logger_group_needs_a_tracking_uri_from_argv(self):
        error = _rejected((*MINIMUM_ARGUMENTS, "logger=mlflow"))

        assert "tracking_uri" in error

    def test_a_local_database_tracking_uri_needs_an_artifact_location(self):
        """Local sqlite の記録先では成果物の置き場所を要求すること.

        指定しないと MLflow が現在 directory の相対 path を experiment へ焼き付け、起こした
        directory ごとに成果物が散る。
        """

        config = _composed(
            (
                *_valid_arguments("logger", "mlflow"),
                "logger.tracking_uri=sqlite:////abs/mlflow.db",
            )
        )

        error = config.validate()
        assert error is not None
        assert "artifact_location" in error

    def test_the_same_tracking_uri_passes_with_an_artifact_location(self):
        """置き場所を足すだけで通ること.

        上の拒否が「sqlite の記録先は常に駄目」へ退化していないことを見る。
        """

        config = _composed(
            (
                *_valid_arguments("logger", "mlflow"),
                "logger.tracking_uri=sqlite:////abs/mlflow.db",
                "logger.artifact_location=/abs/mlartifacts",
            )
        )

        assert config.logger is not None
        assert config.validate() is None

    def test_a_server_tracking_uri_does_not_need_one(self):
        """Server の記録先では置き場所を要求しないこと.

        artifact root は server 側の設定で、client が渡す値ではない。
        """

        config = _composed(_valid_arguments("logger", "mlflow"))

        assert config.logger is not None
        assert config.logger.artifact_location is None
        assert config.validate() is None

    def test_the_search_group_declares_the_initial_search_space(self):
        assert _composed(MINIMUM_ARGUMENTS).hyperparameter_search is None

        config = _composed(_valid_arguments("hyperparameter_search", "base_optuna"))
        search = config.hyperparameter_search

        assert search is not None
        assert set(search.search_space.parameters) == {
            "model.group_norm_groups",
            "trainer.gradient_accumulation",
            "trainer.learning_rate",
            "trainer.weight_decay",
        }
        assert search.storage_uri == STORAGE_URI

    def test_the_search_group_needs_a_storage_uri_from_argv(self):
        error = _rejected((*MINIMUM_ARGUMENTS, "hyperparameter_search=base_optuna"))

        assert "storage_uri" in error

    def test_the_search_parameters_name_real_configuration_paths(self):
        """探索する dotted path が実在の設定へ届くこと.

        ``TrialAssignment.as_override_arguments`` はこの名前を ``key=value`` として
        そのまま積むので、綴りを間違えると全 trial が構造化で落ちる。
        """

        config = _composed(_valid_arguments("hyperparameter_search", "base_optuna"))
        search = config.hyperparameter_search

        assert search is not None
        for name, distribution in search.search_space.parameters.items():
            _composed((*MINIMUM_ARGUMENTS, f"{name}={_probe_value(distribution)}"))


class TestOverrideResolution:
    """``key=value`` の上書きが到達先の意味を保つ."""

    def test_the_held_out_session_reaches_the_training_config(self):
        config = _composed(
            (*MINIMUM_ARGUMENTS, f"data.held_out_session={HELD_OUT_SESSION}")
        )

        assert config.data.training_config().held_out_session == HELD_OUT_SESSION
        assert config.data.training_config().validate() is None

    def test_the_resume_checkpoint_and_the_initial_weights_stay_separate(self):
        """再開点と初期 weight が混ざらないこと.

        ``resume.checkpoint`` は同じ run の続きで、fingerprint の一致を要求する。
        ``model.initial_weights`` は別 run の weight を起点にする。取り違えると、
        別条件で学んだ weight を同じ run の続きとして記録してしまう。
        """

        config = _composed(
            (
                *MINIMUM_ARGUMENTS,
                "resume.checkpoint=/abs/runs/session-0/latest.pt",
                "model.initial_weights=/abs/runs/base/weights.pt",
            )
        )

        assert config.resume.checkpoint == Path("/abs/runs/session-0/latest.pt")
        assert config.model.initial_weights == Path("/abs/runs/base/weights.pt")

    @pytest.mark.parametrize(
        ("override", "expected_resume", "expected_weights"),
        (
            pytest.param(
                "resume.checkpoint=/abs/latest.pt",
                Path("/abs/latest.pt"),
                None,
                id="resume-only",
            ),
            pytest.param(
                "model.initial_weights=/abs/weights.pt",
                None,
                Path("/abs/weights.pt"),
                id="initial-weights-only",
            ),
        ),
    )
    def test_setting_one_of_them_leaves_the_other_unset(
        self, override: str, expected_resume: Path | None, expected_weights: Path | None
    ):
        config = _composed((*MINIMUM_ARGUMENTS, override))

        assert config.resume.checkpoint == expected_resume
        assert config.model.initial_weights == expected_weights

    def test_the_preset_and_the_trainer_profile_do_not_depend_on_the_argv_order(self):
        """重ならない group の層は積む順に依存しないこと.

        ``ConfigComposition`` は argv の順に層を積むので、同じキーを持つ層を混ぜると
        順序が結果を変える。fine_tune preset と trainer profile は重ならない。
        """

        forward = _composed((*MINIMUM_ARGUMENTS, "experiment=fine_tune", "trainer=pi"))
        backward = _composed((*MINIMUM_ARGUMENTS, "trainer=pi", "experiment=fine_tune"))

        assert forward == backward

    def test_the_last_override_of_the_same_key_wins(self):
        """順序が効く場合があること.

        上の検査は合成が順序を捨てていても緑になる。

        同じキーを 2 回渡した形で、順序が実際に効くことを示す。
        """

        config = _composed(
            (*MINIMUM_ARGUMENTS, "data.split_seed=1", "data.split_seed=2")
        )

        assert config.data.split_seed == 2


class TestDataConfigBridging:
    """``PasteVolumeDataConfig`` が既存の設定オブジェクトへ写る."""

    @staticmethod
    def _data_config(**changes: object) -> PasteVolumeDataConfig:
        return attrs.evolve(
            PasteVolumeDataConfig(
                roots=(Path(DATASET_ROOT),), split_dimension="session"
            ),
            **changes,
        )

    def test_it_carries_the_training_defaults_without_copying_them(self):
        """既定値の出典が 1 つであること.

        同じ既定値を 2 つの attrs へ書くと、片方だけ変えたときに食い違う。
        """

        assert self._data_config().training_config() == PasteVolumeTrainingConfig()

    def test_it_writes_every_override_into_the_training_config(self):
        config = self._data_config(
            held_out_session="session-0",
            validation_ratio=0.25,
            split_seed=7,
            max_batch_pixels=1024,
            max_batch_size=4,
        )

        training = config.training_config()

        assert training.held_out_session == "session-0"
        assert training.validation_ratio == pytest.approx(0.25)
        assert training.split_seed == 7
        assert training.max_batch_pixels == 1024
        assert training.max_batch_size == 4

    def test_it_builds_the_collator_from_the_image_settings(self):
        config = self._data_config(global_seed=3)

        assert config.collator() == PasteVolumeCollator(global_seed=3)

    def test_it_reports_empty_roots(self):
        error = attrs.evolve(self._data_config(), roots=()).validate()

        assert error is not None
        assert "roots" in error

    def test_it_reports_a_session_split_without_a_held_out_session(self):
        assert self._data_config().validate() is not None
        assert self._data_config(held_out_session="session-0").validate() is None


class TestExperimentValidation:
    """Group をまたぐ整合は root config が見る."""

    def test_the_model_is_checked_against_the_image_constraints(self):
        """Encoder の総 stride と前処理の最小サイズの組み合わせ.

        どちらの group も単体では成立するので、この組み合わせは root でしか見られない。
        """

        config = _composed(
            (*MINIMUM_ARGUMENTS, f"data.held_out_session={HELD_OUT_SESSION}")
        )
        broken = attrs.evolve(
            config,
            data=attrs.evolve(
                config.data, constraints=ImageConstraints(minimum_size=4)
            ),
        )

        assert config.validate() is None
        error = broken.validate()
        assert error is not None
        assert error.startswith("model:")

    def test_an_empty_run_kind_is_rejected(self):
        config = _composed(
            (
                *MINIMUM_ARGUMENTS,
                f"data.held_out_session={HELD_OUT_SESSION}",
                "run_kind=",
            )
        )

        assert config.validate() is not None
