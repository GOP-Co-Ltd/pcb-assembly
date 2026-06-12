"""ジョブ定義（JobDefinition / ParamSpec）とカタログ."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Literal

import attrs

from webui.jobs.context import ParamValue

if TYPE_CHECKING:
    from webui.jobs.context import JobContext
    from webui.jobs.manager import JobResult


@attrs.frozen
class ParamSpec:
    """ジョブパラメータ 1 件の定義.

    Attributes:
        name: パラメータ名（POST body のキー）
        label: UI 表示名
        value_type: 値の型
        default: 既定値（None は必須パラメータ）
        choices: value_type="choice" の選択肢
        unit: 表示用の単位（任意）
        help: 補足説明（任意）
    """

    name: str
    label: str
    value_type: Literal["float", "int", "str", "bool", "choice"]
    default: bool | float | int | str | None = None
    choices: tuple[str, ...] = ()
    unit: str | None = None
    help: str | None = None


@attrs.frozen
class JobDefinition:
    """ジョブ 1 種の定義.

    Attributes:
        name: URL の {name}。feature slug と一致させる
        label: UI 表示名
        tab: 所属タブ
        run: ワーカースレッドで実行するジョブ関数
        params: パラメータ定義
        requires_pcb: True で PCB 未選択なら開始 400
        uses_machine: 装置を動かすか（Phase 3 では情報のみ）
        accepts_commands: ジョブモード対話（next_command）を受けるか
        hidden: UI のフォーム導出から除外（POST は可）
    """

    name: str
    label: str
    tab: Literal["dev", "pasting", "posctrl"]
    run: Callable[[JobContext], JobResult | None]
    params: tuple[ParamSpec, ...] = ()
    requires_pcb: bool = False
    uses_machine: bool = True
    accepts_commands: bool = False
    hidden: bool = False


class JobCatalog:
    """ジョブ定義の登録・参照・パラメータ検証を担うクラス."""

    def __init__(self) -> None:
        self._definitions: dict[str, JobDefinition] = {}

    def register(self, definition: JobDefinition) -> None:
        """ジョブ定義を登録する.

        Raises:
            ValueError: 名前が重複している場合
        """
        if definition.name in self._definitions:
            raise ValueError(f"ジョブ名が重複しています: {definition.name}")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> JobDefinition:
        """ジョブ定義を返す.

        Raises:
            KeyError: 未知のジョブ名の場合（ルーターが 404 化する）
        """
        if name not in self._definitions:
            raise KeyError(f"未知のジョブです: {name}")
        return self._definitions[name]

    def list(self, tab: str | None = None) -> tuple[JobDefinition, ...]:
        """登録済みジョブ定義を登録順で返す（hidden を含む）.

        Args:
            tab: 指定時はそのタブのジョブのみに絞り込む
        """
        return tuple(
            definition
            for definition in self._definitions.values()
            if tab is None or definition.tab == tab
        )

    def validate_params(
        self, definition: JobDefinition, values: Mapping[str, object]
    ) -> dict[str, ParamValue]:
        """パラメータを検証し、default 充填済みの dict を返す.

        型変換規則は config_store._coerce と同様（bool は value_type="bool"
        のみ受理、int→float 許容、float→int は整数値のみ）。

        Raises:
            ValueError: 未知キー・型不一致・必須欠落・choice 範囲外の場合
        """
        known = {spec.name for spec in definition.params}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"未知のパラメータです: {', '.join(unknown)}")

        validated: dict[str, ParamValue] = {}
        for spec in definition.params:
            if spec.name in values:
                validated[spec.name] = _coerce_param(spec, values[spec.name])
            elif spec.default is not None:
                validated[spec.name] = spec.default
            else:
                raise ValueError(f"必須パラメータがありません: {spec.name}")
        return validated


def default_catalog() -> JobCatalog:
    """Dev 5 + posctrl 4 + pasting 6 ジョブ登録済みのカタログを返す."""
    # 循環 import（dev/posctrl/pasting → manager → catalog）を避けるため遅延 import する
    from webui.jobs.dev import register_dev_jobs
    from webui.jobs.pasting import register_pasting_jobs
    from webui.jobs.posctrl import register_posctrl_jobs

    catalog = JobCatalog()
    register_dev_jobs(catalog)
    register_posctrl_jobs(catalog)
    register_pasting_jobs(catalog)
    return catalog


def _coerce_param(spec: ParamSpec, value: object) -> ParamValue:
    """値を ParamSpec の型に合わせて検証・変換する.

    Raises:
        ValueError: 型が一致しない・choice 範囲外の場合
    """
    match spec.value_type:
        case "bool":
            if isinstance(value, bool):
                return value
        case "float":
            if not isinstance(value, bool) and isinstance(value, (int, float)):
                return float(value)
        case "int":
            if not isinstance(value, bool):
                if isinstance(value, int):
                    return value
                if isinstance(value, float) and value.is_integer():
                    return int(value)
        case "str":
            if isinstance(value, str):
                return value
        case "choice":
            if isinstance(value, str) and value in spec.choices:
                return value
            raise ValueError(
                f"{spec.name}: {', '.join(spec.choices)} のいずれかを"
                f"指定してください（与えられた値: {value!r}）"
            )
    raise ValueError(
        f"{spec.name}: {spec.value_type} 型の値が必要です（与えられた値: {value!r}）"
    )
