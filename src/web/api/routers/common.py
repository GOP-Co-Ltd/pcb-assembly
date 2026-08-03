"""複数ルーターが共有するヘルパ（Klipper 接続・状態レスポンス・設定項目）.

2 つ以上の router から使われるものだけを置く（1 router 専用のヘルパは 各 router に残す）。

pydantic モデルの定義は `web.api.models` に集約してある。``StateResponse`` は既存
import を壊さないためここから再 export する。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import attrs
import httpx
from fastapi import HTTPException

from pcbasm.config import Machine
from pcbasm.hal import Klipper
from web.api.config_store import (
    MACHINE_FIELDS,
    ConfigStore,
    FieldSpec,
    MachineSettingValue,
)
from web.api.control import ClientIdentity, LeaseInfo
from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.manager import JobManager
from web.api.models import (
    API_VERSION,
    ClientInfo,
    ControlInfo,
    JobBrief,
    KlipperStatus,
    MachineInfo,
    Position,
    SettingsField,
    StateResponse,
)
from web.api.preview import PreviewService
from web.api.settings import Settings, resolve_machine_id
from web.api.state import AppState

# --------------------------------------------------------------------------- #
# Klipper 接続
# --------------------------------------------------------------------------- #


def create_klipper(state: AppState, timeout: float) -> Klipper:
    """マシン設定で Klipper クライアントを生成する."""
    klipper_config = state.machine().klipper
    return Klipper(host=klipper_config.host, port=klipper_config.port, timeout=timeout)


def fetch_status(klipper: Klipper) -> KlipperStatus:
    """Klipper から位置と homed_axes を取得する。失敗時は connected=False."""
    try:
        position = klipper.get_status("gcode_move", "gcode_position")
        homed_axes = klipper.get_status("toolhead", "homed_axes")
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        return KlipperStatus(connected=False, error=str(exc) or type(exc).__name__)
    return KlipperStatus(
        connected=True,
        position=Position(x=position[0], y=position[1], z=position[2]),
        homed_axes=homed_axes,
    )


@contextmanager
def klipper_errors_to_502() -> Iterator[None]:
    """Klipper 通信エラーを HTTP 502 へ変換する.

    Raises:
        HTTPException: httpx.HTTPError / RuntimeError / KeyError の場合（502）
    """
    try:
        yield
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        raise HTTPException(
            status_code=502, detail=str(exc) or type(exc).__name__
        ) from exc


# --------------------------------------------------------------------------- #
# アプリ状態レスポンス
# --------------------------------------------------------------------------- #


def control_payload(info: LeaseInfo) -> ControlInfo:
    """リーススナップショットを API 表現へ変換する（唯一の変換点）."""
    return ControlInfo(
        key=info.key,
        display_name=info.display_name,
        held=info.held,
        connections=info.connections,
    )


def build_state_response(
    state: AppState,
    settings: Settings,
    preview: PreviewService,
    jobs: JobManager,
    control: LeaseInfo,
    identity: ClientIdentity,
) -> StateResponse:
    """現在のアプリ状態から StateResponse を構築する."""
    owner = state.busy_owner
    pcb = state.selected_pcb
    record = jobs.current()
    cap = state.nozzle_cap()
    return StateResponse(
        control=control_payload(control),
        you=ClientInfo(key=identity.key),
        pcb_file=pcb.as_posix() if pcb else None,
        busy=owner is not None,
        busy_owner=owner,
        focus_z=state.focus_z(),
        mainsail_url=settings.mainsail_url,
        preview_clients=preview.client_count,
        job=(
            JobBrief(id=record.id, name=record.name, status=record.status.value)
            if record is not None
            else None
        ),
        nozzle_cap=(None if cap is None else Position(x=cap.x, y=cap.y, z=cap.z)),
    )


# --------------------------------------------------------------------------- #
# backend の自己申告（/api/machine-info と SSR が共有する解決結果）
# --------------------------------------------------------------------------- #


def _fb_start(settings: Settings) -> str:
    """ファイルブラウザの初期表示パス（pcb_browse_root からの相対）."""
    try:
        start = (
            settings.pcb_browse_start.resolve()
            .relative_to(settings.pcb_browse_root.resolve())
            .as_posix()
        )
    except ValueError:
        return ""
    return "" if start == "." else start


def _default_mainsail_url(machine_id: str) -> str:
    """``mainsail_url`` 未設定時のフォールバック URL.

    ``machine_id`` は短いホスト名（``socket.gethostname()``）なので、LAN の他端末
    からは mDNS 経由の ``*.local`` しか引けない。裸のホスト名を返すとリモートから
    Mainsail を開けなくなるため ``.local`` を付ける。既にドットを含む
    （FQDN や ``.local`` 付きが注入された）場合は重ねない。
    """
    host = machine_id if "." in machine_id else f"{machine_id}.local"
    return f"http://{host}"


def build_machine_info(state: AppState, settings: Settings) -> MachineInfo:
    """Backend の自己申告情報を組み立てる.

    ホスト名・表示名の未設定フォールバックといった環境依存の解決はここに集約する
    （pcbasm 層は machine.toml に書かれた値だけを返す）。``mainsail_url`` は
    リクエストのホスト名に依存させない（プロキシ配下で必ず誤るため）。
    """
    machine_id = resolve_machine_id(settings)
    return MachineInfo(
        machine_id=machine_id,
        machine_name=state.machine_name() or machine_id,
        machine_type=state.machine_type(),
        mainsail_url=settings.mainsail_url or _default_mainsail_url(machine_id),
        fb_start=_fb_start(settings),
        api_version=API_VERSION,
    )


# --------------------------------------------------------------------------- #
# ジョブ定義のパラメータ（ジョブページ SSR / /api/jobs 共用）
# --------------------------------------------------------------------------- #


def param_specs_with_saved_defaults(
    definition: JobDefinition, state: AppState, catalog: JobCatalog
) -> tuple[ParamSpec, ...]:
    """保存済み既定値を ParamSpec の default に反映する.

    型判定・coerce は :meth:`JobCatalog.filter_persisted_defaults` に一本化する
    （persisted_params 外・型不一致は黙って除外 = spec 既定値のまま）。
    """
    saved = state.job_param_defaults(definition.name)
    if not saved or not definition.persisted_params:
        return definition.params
    valid = catalog.filter_persisted_defaults(definition, saved)
    return tuple(
        attrs.evolve(spec, default=valid[spec.name]) if spec.name in valid else spec
        for spec in definition.params
    )


# --------------------------------------------------------------------------- #
# マシン設定項目（settings ページ / API 共用）
# --------------------------------------------------------------------------- #


def _fields(
    specs: tuple[FieldSpec, ...],
    values: Mapping[str, MachineSettingValue | None],
    resolved: Mapping[str, MachineSettingValue | None],
) -> list[SettingsField]:
    return [
        SettingsField(
            key=spec.key,
            label=spec.label,
            value_type=spec.value_type,
            unit=spec.unit,
            value=values[spec.key],
            resolved=resolved.get(spec.key),
        )
        for spec in specs
    ]


def machine_settings_fields(store: ConfigStore, state: AppState) -> list[SettingsField]:
    """machine.toml のホワイトリスト項目を現在値と実効値付きで返す.

    実効値（``resolved``）は cattrs が既定値を埋めたあとの値で、SSR ページが現在値を
    表示するために使う。未記載キーの代替値を frontend 側に置くとプロセス境界の両側で
    二重管理になり、片方がずれると誤った値が保存フォームに乗る。
    """
    return _fields(
        MACHINE_FIELDS, store.read_machine_settings(), _resolved_values(state)
    )


def _resolved_values(state: AppState) -> dict[str, MachineSettingValue | None]:
    """ホワイトリスト項目の実効値（machine.toml が読めなければ全て None）.

    machine.toml の不在・破損で例外にしないのは `AppState` の他の getter と同じ理由
    （設定を直す画面まで開けなくなる）。
    """
    try:
        machine = state.machine()
    except Exception:
        return {}
    return {spec.key: _resolved_value(machine, spec.key) for spec in MACHINE_FIELDS}


def _resolved_value(machine: Machine, key: str) -> MachineSettingValue | None:
    """ドット区切りキーを `Machine` から辿って実効値を読む（読めなければ None）.

    セクションが machine.toml に無い / 必須キーが欠けている場合は `Machine` の
    プロパティか cattrs が例外を投げる。1 セクションの不備で他セクションの実効値まで
    失わないよう、キー単位で None に潰す。
    """
    try:
        node: object = machine
        for part in key.split("."):
            node = getattr(node, part)
            if node is None:
                return None
        return _as_setting_value(node)
    except Exception:
        return None


def _as_setting_value(value: object) -> MachineSettingValue | None:
    """`Machine` の値を API の設定値表現へ落とす（表現できないものは None）."""
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, tuple):
        # reference_point.offsets.* の [x, y]
        return [float(item) for item in value]
    if isinstance(value, bool | int | float | str):
        return value
    return None
