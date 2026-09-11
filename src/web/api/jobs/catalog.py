"""ジョブ定義（JobDefinition / ParamSpec）とカタログ."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

import attrs

from web.api.jobs.context import JobContext, JobResult, ParamValue


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
        runtime_editable: 実行中に値を変更できるか（True で patch 受理）
        minimum: 数値型の下限（None は制約なし。下回る値は検証エラー）
        optional: True なら値の省略を許し、省略時は検証結果にキーを含めない
    """

    name: str
    label: str
    value_type: Literal["float", "int", "str", "bool", "choice"]
    default: bool | float | int | str | None = None
    choices: tuple[str, ...] = ()
    unit: str | None = None
    help: str | None = None
    runtime_editable: bool = False
    minimum: float | None = None
    optional: bool = False


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
        uses_machine: 装置を動かすか。終了時のノズルキャップ駐機と、機体スピーカー
            の完了通知音（成功 / 失敗）の対象をこれで決める
        notify_on_completion: 成功・失敗時にブラウザで終了通知するか
            （画面表示のみ。機体スピーカーは ``uses_machine`` で決まる）
        accepts_commands: ジョブモード対話（next_command）を受けるか
        persisted_params: 起動時の値を次回フォーム既定値として保存するパラメータ名
        hidden: UI のフォーム導出から除外（POST は可）
        provides_preview: ジョブがカメラフレームを提供するか（preview ペインの有無）
        loading_param: ローディング UI の既定量に使う ParamSpec 名
            （None ならローディング UI を出さない）
        loading_stages: ローディング UI を有効化する progress_stage 名。
            カンマ区切りで複数指定できる（loading_controls.js が Set として解釈する）
    """

    name: str
    label: str
    tab: Literal["dev", "pasting", "posctrl"]
    run: Callable[[JobContext], JobResult | None]
    params: tuple[ParamSpec, ...] = ()
    requires_pcb: bool = False
    uses_machine: bool = True
    notify_on_completion: bool = False
    accepts_commands: bool = False
    persisted_params: tuple[str, ...] = ()
    hidden: bool = False
    provides_preview: bool = False
    loading_param: str | None = None
    loading_stages: str = "ローディング"

    @property
    def runtime_params(self) -> tuple[str, ...]:
        """実行中に変更可能なパラメータ名（フォーム / router 補助用）."""
        return tuple(spec.name for spec in self.params if spec.runtime_editable)


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
            elif spec.optional:
                continue
            else:
                raise ValueError(f"必須パラメータがありません: {spec.name}")
        return validated

    def validate_runtime_params(
        self, definition: JobDefinition, values: Mapping[str, Any]
    ) -> dict[str, ParamValue]:
        """実行中編集用に runtime_editable な subset のみ検証して返す.

        与えられたキーだけを coerce して返す（default 充填はしない＝patch）。
        型変換規則・int 規則・minimum 下限は ``validate_params`` と同じ
        ``_coerce_param`` を共有する。

        Raises:
            ValueError: runtime_editable でないキー（固定／未知）・型不一致・
                minimum 未満の場合
        """
        editable = {
            spec.name: spec for spec in definition.params if spec.runtime_editable
        }
        fixed = {spec.name for spec in definition.params}
        validated: dict[str, ParamValue] = {}
        for key, value in values.items():
            spec = editable.get(key)
            if spec is None:
                if key in fixed:
                    raise ValueError(f"実行中に変更できないパラメータです: {key}")
                raise ValueError(f"未知のパラメータです: {key}")
            validated[key] = _coerce_param(spec, value)
        return validated

    def filter_persisted_defaults(
        self, definition: JobDefinition, values: Mapping[str, object]
    ) -> dict[str, ParamValue]:
        """persisted_params のうち型整合する値だけを coerce して返す.

        フォーム入力途中の「即保存」用。``persisted_params`` 以外のキー・未提供・
        型不一致は黙って除外する（実行前なので必須欠落でエラーにしない）。
        """
        specs = {spec.name: spec for spec in definition.params}
        result: dict[str, ParamValue] = {}
        for key in definition.persisted_params:
            if key not in values:
                continue
            try:
                result[key] = _coerce_param(specs[key], values[key])
            except ValueError:
                continue
        return result


def default_catalog() -> JobCatalog:
    """Dev 3 + posctrl 5 + pasting 6 ジョブ登録済みのカタログを返す."""
    # 循環 import（dev/posctrl/pasting → manager → catalog）を避けるため遅延 import する
    from web.api.jobs.dev import register_dev_jobs
    from web.api.jobs.pasting import register_pasting_jobs
    from web.api.jobs.posctrl import register_posctrl_jobs

    catalog = JobCatalog()
    register_dev_jobs(catalog)
    register_posctrl_jobs(catalog)
    register_pasting_jobs(catalog)
    return catalog


def _coerce_param(spec: ParamSpec, value: object) -> ParamValue:
    """値を ParamSpec の型に合わせて検証・変換する.

    Raises:
        ValueError: 型が一致しない・choice 範囲外・minimum 未満の場合
    """
    coerced = _coerce_type(spec, value)
    # 下限制約（minimum）は起動時 validate_params と実行中
    # validate_runtime_params の両経路で効くよう coerce 共通層に置く。
    if (
        spec.minimum is not None
        and not isinstance(coerced, bool)
        and isinstance(coerced, (int, float))
        and coerced < spec.minimum
    ):
        raise ValueError(
            f"{spec.name}: マイナスにできません（与えられた値: {coerced!r}）"
        )
    return coerced


def _coerce_type(spec: ParamSpec, value: object) -> ParamValue:
    """値を ParamSpec の value_type に合わせて検証・変換する.

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
