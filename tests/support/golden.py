"""送信 G-code と成果物 JSON の golden（記録と比較）と、golden を採る fake の機体・カメラ・基板の置き方.

golden はジャーナルと同じ書式で、送信 1 回を ``# send timeout=<実効値>`` の見出しと G-code の行で書く。
比べるときは G-code を字句に分け、命令と語の並びは完全一致、数値は |Δ| ≤ 1e-9 で比べる。
float の末尾桁の揺れは吸収し、送る列の意味は変えさせない。見出しは完全一致で比べる。
成果物の JSON も、数値は |Δ| ≤ 1e-9、それ以外は完全一致で比べる。
``PCBASM_UPDATE_GOLDEN=1`` のときだけ、比べずに golden を書き換える。
"""

import json
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TypeGuard

import pytest
from shapely import Polygon

from pcbasm.align.scene import board_scene
from pcbasm.board import Board
from pcbasm.config import MachineConfig, load_machine
from pcbasm.geometry import Compose, Point2d, Rotation, Shift
from pcbasm.hal.fake import (
    Checkerboard,
    FakeAudioPlayer,
    FakeKlipper,
    SceneCamera,
    SentGCode,
)
from pcbasm.kicad import load_board
from pcbasm.machine.journal import format_block
from pcbasm.pasting.hierarchy import PadHierarchy
from pcbasm.pasting.overrides import BoardOverrides
from pcbasm.pasting.paste_volume.calibration import (
    CALIBRATION_SUFFIX,
    parse_calibration,
    write_calibration,
)
from pcbasm.paths import PROJECT_ROOT
from pcbasm.vision import CameraModel
from tests.support import polling
from tests.support.paths import PASTE_VOLUME_CALIBRATION
from web.api.backend import Backend
from web.api.env import Env
from web.api.jobs.runtime.record import JobRecord, JobStatus
from web.api.machine_config import MachineConfigFile

UPDATE_ENV = "PCBASM_UPDATE_GOLDEN"

TOLERANCE = 1e-9

# 語の末尾の数値（"X12.5" → "X" と "12.5"、"MOVE=-0.1" → "MOVE=" と "-0.1"）
_NUMBER = re.compile(r"(.*?)([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")


def transcript(sent: Iterable[SentGCode]) -> str:
    """送信の列をジャーナルと同じ書式の文字列にする."""
    return "".join(format_block(block.gcode, block.timeout) for block in sent)


def assert_transcript(sent: Iterable[SentGCode], path: Path) -> None:
    """送信の列が golden ``path`` と同じであることを確かめる."""
    actual = transcript(sent)
    expected = _golden(path, actual)
    if expected is None:
        return
    difference = _difference(expected, actual)
    if difference is not None:
        pytest.fail(f"golden {path.name} と違います: {difference}")


def assert_document(document: object, path: Path) -> None:
    """JSON の値 ``document`` が golden ``path`` と同じであることを確かめる."""
    actual = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    expected = _golden(path, actual)
    if expected is None:
        return
    difference = _document_difference(json.loads(expected), json.loads(actual), "$")
    if difference is not None:
        pytest.fail(f"golden {path.name} と違います: {difference}")


def _golden(path: Path, actual: str) -> str | None:
    """Golden の中身を返す（更新するときは ``actual`` を書いて None を返す）."""
    if os.environ.get(UPDATE_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return None
    if not path.exists():
        pytest.fail(f"golden {path} がありません（{UPDATE_ENV}=1 で作る）")
    return path.read_text(encoding="utf-8")


def _difference(expected: str, actual: str) -> str | None:
    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    for number, (want, got) in enumerate(zip(expected_lines, actual_lines), start=1):
        if not _same_line(want, got):
            return f"{number} 行目 golden={want!r} 実際={got!r}"
    if len(expected_lines) != len(actual_lines):
        return f"行数 golden={len(expected_lines)} 実際={len(actual_lines)}"
    return None


def _same_line(expected: str, actual: str) -> bool:
    if expected.startswith("#") or actual.startswith("#"):
        return expected == actual
    want = expected.split()
    got = actual.split()
    return len(want) == len(got) and all(map(_same_word, want, got))


def _same_word(expected: str, actual: str) -> bool:
    want = _NUMBER.fullmatch(expected)
    got = _NUMBER.fullmatch(actual)
    if want is None or got is None:
        return expected == actual
    return want[1] == got[1] and abs(float(want[2]) - float(got[2])) <= TOLERANCE


def _document_difference(expected: object, actual: object, where: str) -> str | None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        if expected.keys() != actual.keys():
            return f"{where} のキー golden={sorted(expected)} 実際={sorted(actual)}"
        for key, value in expected.items():
            difference = _document_difference(value, actual[key], f"{where}.{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return f"{where} の長さ golden={len(expected)} 実際={len(actual)}"
        for index, (want, got) in enumerate(zip(expected, actual)):
            difference = _document_difference(want, got, f"{where}[{index}]")
            if difference is not None:
                return difference
        return None
    if _is_number(expected) and _is_number(actual):
        same = abs(expected - actual) <= TOLERANCE
    else:
        same = type(expected) is type(actual) and expected == actual
    return None if same else f"{where} golden={expected!r} 実際={actual!r}"


def _is_number(value: object) -> TypeGuard[float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


GOLDEN_DIR = PROJECT_ROOT / "tests" / "golden"
JOBS = GOLDEN_DIR / "jobs"
CONFIG_FILES = (GOLDEN_DIR / "machine.toml", GOLDEN_DIR / "camera_calibration.json")
CAMERA_MODEL = CameraModel.load(GOLDEN_DIR / "camera_calibration.json")
SOLDER = load_machine(GOLDEN_DIR / "machine.toml").require_solder()
# 運転時補正で塗る点（board 座標）。pad の無い下側のベタに、crop が重ならない間隔で置く
FLOW_POINTS = (Point2d(5.0, 20.0), Point2d(10.0, 20.0), Point2d(15.0, 20.0))

# 基板の実際の置き方（board → 機械座標）。左上の基準円は reference_point から 0.3 mm ほどずれる
BOARD_PLACEMENT = Compose([Rotation(0.3), Shift(6.2, 10.5)])
# 基準円に対する銅箔のずれ（銅箔照合が測る変位）
COPPER_OFFSET = Shift(0.04, -0.03)


def board_shapes(pcb: Board, machine: MachineConfig) -> list[Polygon]:
    """基板を BOARD_PLACEMENT に置いたときの、4 隅の基準円と TOP 銅箔を機械座標で返す."""
    return board_scene(
        pcb,
        machine.reference_point,
        BOARD_PLACEMENT,
        Compose([BOARD_PLACEMENT, COPPER_OFFSET]),
    )


def scene_camera(
    klipper: FakeKlipper,
    rotation: float,
    *,
    shapes: Sequence[Polygon] = (),
    checkerboard: Checkerboard | None = None,
    pixel_per_mm: float = CAMERA_MODEL.pixel_per_mm,
) -> SceneCamera:
    """校正ファイルと machine.toml のとおりに、klipper の位置から見える画像を描くカメラ.

    ``pixel_per_mm`` を校正と変えると、校正の後でカメラの倍率が変わった機体を模せる。
    """
    return SceneCamera(
        klipper,
        pixel_per_mm=pixel_per_mm,
        toolhead=Point2d(SOLDER.nozzle.offset.x, SOLDER.nozzle.offset.y),
        rotations_per_ul=SOLDER.dispenser.rotations_per_ul,
        rotation=rotation,
        size=CAMERA_MODEL.image_size,
        shapes=shapes,
        checkerboard=checkerboard,
    )


def finish(
    backend: Backend, record: JobRecord, status: JobStatus = JobStatus.SUCCEEDED
) -> None:
    """終了時の駐機が済む（装置のロックが外れる）まで待ち、ジョブの終わり方を確かめる."""
    polling.wait_until(
        lambda: record.status.terminal and backend.lock.owner is None, timeout=120.0
    )
    assert record.status == status, record.error


def wait_operator(
    record: JobRecord, audio_player: FakeAudioPlayer, stage: str, calls: int = 1
) -> None:
    """``stage`` にいて、ジョブ開始からの入力待ち音が通算 ``calls`` 回鳴る（入口で滞留コマンドを捨て終える）まで待つ."""
    polling.wait_until(
        lambda: record.progress_stage == stage
        and sum(sound == "prompt" for sound, *_ in audio_player.played) >= calls,
        timeout=60.0,
    )


def enable_flow_calibration(settings: Env, backend: Backend, pcb: Path) -> None:
    """校正ファイルと基板ごとの測定点を置き、運転時補正を有効にする."""
    calibration, error = parse_calibration(
        json.loads(PASTE_VOLUME_CALIBRATION.read_text(encoding="utf-8"))
    )
    assert calibration is not None, error
    name = f"golden{CALIBRATION_SUFFIX}"
    write_calibration(settings.paste_volume_calibration_dir / name, calibration)
    MachineConfigFile(settings.config_dir).write(
        {
            "solder.volume_correction.calibration_file": name,
            "solder.volume_correction.settle_seconds": 0.0,
        }
    )
    board = load_board(pcb)
    store = BoardOverrides(settings.webui_data_dir)
    model = store.load_or_init(
        pcb.name, backend.machine_config.machine().require_solder()
    )
    store.save(
        pcb.name,
        model.with_flow_calibration_points(FLOW_POINTS),
        board_signature=PadHierarchy.build(board.components, board.pads).signature(),
    )
