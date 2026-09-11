"""直径ベース塗布量校正のファイル形式（schema v1）.

1 校正 = 1 ファイル。使うものは WebUI で明示的に選ぶ（条件からの自動選択は作らない）。
`data/paste-volume-calibrations/` へ置き、Git 管理する。

検出ハイパラと係数は同じ document に持つ。違うハイパラで測った直径へ係数を当てても
意味がないので、両者は不可分。

``source.label_kind`` を残すのは、``rotation_allocated``（総質量を指令回転数比で
配分したラベル）に点ごとの真値が無いことをファイルだけで判別できるようにするため。
達成条件を session 総体積の誤差で見る根拠がここに残る。

永続化の判断:
    strict converter は :mod:`pcbasm.pasting.dataset.metadata` のものを共有する
    （未知 key と暗黙の型変換を拒否する挙動が dataset schema と同じでよい）。
    ``schema_version`` が現版と異なる document は移行せず ``(None, 理由)`` で拒否する。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import attrs

from pcbasm.atomic import write_text_atomic
from pcbasm.pasting.dataset.metadata import structure_document
from pcbasm.pasting.paste_volume.detect import DotDetectionSpec
from pcbasm.pasting.paste_volume.model import CubicVolumeModel

CALIBRATION_KIND = "pcbasm-paste-volume-diameter-calibration"
CALIBRATION_SCHEMA_VERSION = 1

# 校正ファイルの拡張子（dataset の JSON と取り違えないための識別子を兼ねる）
CALIBRATION_SUFFIX = ".paste-volume.json"

# 保存名に使わない文字（path として扱いにくいものと空白）
_UNSAFE_NAME = re.compile(r"[^0-9a-z._-]+")


# 撮影スケールが校正時から何割ずれたら条件不一致として報告するか。
# 直径は mm なのでスケール差は原理的に吸収され、これは推定を断る閾値ではなく
# 「camera calibration をやり直したのでは」と運転者へ知らせるための警告閾値。
# 同じ機体で撮り直したときの pixel_per_mm の再現性（実測で 1% 未満）に対して
# 十分広く、crop 条件が別物になる水準よりは狭い値として 5% を置く。
SCALE_TOLERANCE = 0.05


@attrs.frozen
class CalibrationConditions:
    """校正が成り立つ条件（ペースト・ノズル・塗布高さ・撮影スケール）."""

    paste_id: str
    paste_lot: str | None
    density_mg_per_ul: float
    nozzle_diameter_mm: float
    paste_height_mm: float
    machine_id: str
    pixel_per_mm: float
    crop_size_px: int
    crop_size_mm: float

    def mismatches(
        self,
        *,
        nozzle_diameter_mm: float,
        paste_height_mm: float,
        pixel_per_mm: float,
        paste_id: str | None = None,
        crop_size_mm: float | None = None,
    ) -> tuple[str, ...]:
        """いま置かれている条件との食い違いを表示用の文で並べる.

        推定を断るためではなく運転者へ知らせるための情報なので、呼び出し側は
        これが空でなくても処理を止めなくてよい。
        ``paste_id`` / ``crop_size_mm`` は分かるときだけ渡す。
        """
        mismatches: list[str] = []
        if paste_id is not None and self.paste_id != paste_id:
            mismatches.append(f"ペースト: 校正 {self.paste_id} / 現在 {paste_id}")
        if self.nozzle_diameter_mm != nozzle_diameter_mm:
            mismatches.append(
                f"ノズル径: 校正 {self.nozzle_diameter_mm} mm / "
                f"現在 {nozzle_diameter_mm} mm"
            )
        if self.paste_height_mm != paste_height_mm:
            mismatches.append(
                f"塗布高さ: 校正 {self.paste_height_mm} mm / "
                f"現在 {paste_height_mm} mm"
            )
        if _scale_differs(self.pixel_per_mm, pixel_per_mm):
            mismatches.append(
                f"撮影スケール: 校正 {self.pixel_per_mm:.3f} px/mm / "
                f"現在 {pixel_per_mm:.3f} px/mm"
            )
        # 2 値化は crop 内の Otsu なので、crop に対する背景の割合が閾値を動かす
        if crop_size_mm is not None and self.crop_size_mm != crop_size_mm:
            mismatches.append(
                f"撮影crop寸法: 校正 {self.crop_size_mm} mm / 現在 {crop_size_mm} mm"
            )
        return tuple(mismatches)


def _scale_differs(calibrated: float, actual: float) -> bool:
    """撮影スケールが許容を超えて違うか."""
    if calibrated <= 0 or actual <= 0:
        return True
    return abs(actual - calibrated) / calibrated > SCALE_TOLERANCE


@attrs.frozen
class CalibrationSource:
    """校正の材料にした収集 session（1 校正 = 1 session）."""

    session: str
    label_kind: Literal["rotation_allocated"]
    sample_count: int
    blank_count: int
    measured_volume_ul: float
    metadata_sha256: str


@attrs.frozen
class CalibrationDiagnostics:
    """フィットの当てはまりと、検出の健全性.

    Attributes:
        blank_false_positive_count: blank セルで直径が 0 にならなかった数（0 が要件）
        detection_failure_count: 全 view で検出できなかった塗布セル数
        monotonic_in_range: 被覆域内でモデルが単調非減少か（中央値集約の前提）
        residual_relative_std: 点ごと相対残差の標準偏差（推定の std の根拠）
        point_relative_mae: 点ごと相対誤差の平均絶対値（参考値）
        point_relative_max: 同じく最大（参考値）
        total_relative_error: session 総体積の相対誤差（主基準）
    """

    blank_false_positive_count: int
    detection_failure_count: int
    monotonic_in_range: bool
    residual_relative_std: float
    point_relative_mae: float
    point_relative_max: float
    total_relative_error: float


@attrs.frozen
class PasteVolumeCalibration:
    """校正ファイル schema v1."""

    kind: Literal["pcbasm-paste-volume-diameter-calibration"]
    schema_version: Literal[1]
    created_at: str
    label: str
    conditions: CalibrationConditions
    detection: DotDetectionSpec
    model: CubicVolumeModel
    source: CalibrationSource
    diagnostics: CalibrationDiagnostics

    def to_dict(self) -> dict[str, Any]:
        """JSON 互換 dict へ変換する（``detection`` / ``model`` は kind を補う）."""
        document = attrs.asdict(self)
        document["detection"] = {
            "kind": _DETECTION_KIND,
            **attrs.asdict(self.detection),
        }
        document["model"] = {"kind": _MODEL_KIND, **attrs.asdict(self.model)}
        return document

    def validate(self) -> str | None:
        """検出ハイパラとモデルの整合を確かめる（不正なら理由文）."""
        return self.detection.validate() or self.model.validate()


def parse_calibration(
    data: Mapping[str, object],
) -> tuple[PasteVolumeCalibration | None, str | None]:
    """校正 document を復元する（暗黙の型変換と未知 key は受理しない）.

    ``detection`` / ``model`` の ``kind`` は方式の識別子で、DTO には持たせない。
    現版が扱える方式かをここで確かめてから取り除く。

    ``detection`` だけは key の欠落も拒否する。

    :class:`DotDetectionSpec` は既定値を持つので欠落が黙って埋まってしまい、既定値を
    将来変えるとキーを欠いた既存ファイルの意味が静かに変わる。

    検出ハイパラは校正と不可分なので、書かれていないことを既定値の指定として扱わない。
    """
    version = data.get("schema_version")
    if version != CALIBRATION_SCHEMA_VERSION:
        return None, f"未対応の校正schema_versionです: {version!r}"
    if data.get("kind") != CALIBRATION_KIND:
        return None, f"校正ファイルではありません: kind={data.get('kind')!r}"
    document, error = _without_method_kinds(data)
    if document is None:
        return None, error
    error = _missing_detection_keys(document["detection"])
    if error is not None:
        return None, error
    try:
        calibration = structure_document(document, PasteVolumeCalibration)
    except Exception as error_detail:
        return None, f"校正schema v1が不正です: {error_detail}"
    invalid = calibration.validate()
    if invalid is not None:
        return None, invalid
    return calibration, None


def load_calibration(
    path: Path,
) -> tuple[PasteVolumeCalibration | None, str | None]:
    """校正ファイルを読む（読めない・不正は理由を返す）."""
    if not path.is_file():
        return None, f"校正ファイルがありません: {path}"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, f"校正ファイルを読めません: {error}"
    if not isinstance(document, dict):
        return None, f"校正ファイルがobjectではありません: {path}"
    calibration, error_text = parse_calibration(document)
    if calibration is None:
        return None, f"{path.name}: {error_text}"
    return calibration, None


def write_calibration(path: Path, calibration: PasteVolumeCalibration) -> None:
    """校正ファイルを atomic に書く（親ディレクトリは作る）."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(calibration.to_dict(), ensure_ascii=False, indent=2) + "\n"
    write_text_atomic(path, text)


def list_calibrations(root: Path) -> tuple[Path, ...]:
    """保存済み校正ファイルを名前順に返す."""
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.name.endswith(CALIBRATION_SUFFIX)
        )
    )


def calibration_path(root: Path, name: str, created_at: datetime | None = None) -> Path:
    """運転者が入力した保存名から、``root`` 直下に閉じた保存先 path を作る.

    名前は WebUI のテキスト欄から来るので、``/`` や ``..`` を含んでいても保存先の
    外へ出さない。

    :func:`calibration_filename` が path に使えない文字を畳むので、脱出は
    そこで潰れる。

    既に ``CALIBRATION_SUFFIX`` で終わる名前は「その名前で保存し直す」意図とみなし、
    時刻を足さずに stem だけを畳む。

    Args:
        root: 保存先 directory
        name: 運転者が入力した保存名
        created_at: 時刻を足すときに使う（省略時は現在時刻）

    Returns:
        ``root`` 直下の保存先 path
    """
    if name.endswith(CALIBRATION_SUFFIX):
        stem = _safe_stem(name[: -len(CALIBRATION_SUFFIX)])
        return root / f"{stem}{CALIBRATION_SUFFIX}"
    return root / calibration_filename(name, created_at)


def default_calibration_label(
    conditions: CalibrationConditions, created_at: datetime
) -> str:
    """校正の条件と生成時刻から既定の表示名を組み立てる.

    ペースト・ノズル径・塗布高さだけだと、同条件で採り直した校正が同じ名前になり WebUI の選択肢で見分けられない。

    時刻は運転者の地方時で入れる。
    """
    return (
        f"{conditions.paste_id} / n{conditions.nozzle_diameter_mm:.2f} / "
        f"h{conditions.paste_height_mm:.2f} / "
        f"{created_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def auto_calibration_path(root: Path, label: str) -> Path:
    """自動命名した校正の保存先 path を作る.

    自動生成のラベルは既に生成時刻を含むので、:func:`calibration_filename` のように
    時刻を足さない。
    足すと同じ時刻が名前に 2 度出る。
    """
    return root / f"{_safe_stem(label)}{CALIBRATION_SUFFIX}"


def calibration_filename(label: str, created_at: datetime | None = None) -> str:
    """表示ラベルから保存名を作る（path に使えない文字は ``-`` へ畳む）."""
    stem = _UNSAFE_NAME.sub("-", label.lower()).strip("-")
    if not stem:
        stem = "calibration"
    moment = (created_at or datetime.now().astimezone()).strftime("%Y%m%dT%H%M%S")
    return f"{stem}-{moment}{CALIBRATION_SUFFIX}"


def _safe_stem(stem: str) -> str:
    """保存名の stem を 1 つの path 片へ畳む（空になれば既定名）."""
    folded = _UNSAFE_NAME.sub("-", stem.lower()).strip("-")
    return folded or "calibration"


# 方式の識別子は DTO の外側（document のキー）に置く。DTO へ持たせると
# strict converter が Literal の一致まで見るので、方式追加のたびに DTO が増える。
_DETECTION_KIND = "diameter_otsu_v1"
_MODEL_KIND = "cubic_through_origin"


def _missing_detection_keys(detection: object) -> str | None:
    """``detection`` が検出ハイパラを 1 つ残らず書いているかを確かめる."""
    if not isinstance(detection, dict):
        return "detectionセクションがobjectではありません"
    required = {field.name for field in attrs.fields(DotDetectionSpec)}
    missing = sorted(required - set(detection))
    if missing:
        return f"detectionに検出ハイパラがありません: {', '.join(missing)}"
    return None


def _without_method_kinds(
    data: Mapping[str, object],
) -> tuple[dict[str, object] | None, str | None]:
    """``detection`` / ``model`` の ``kind`` を検証して取り除いた document を返す."""
    document = dict(data)
    for section, expected in (
        ("detection", _DETECTION_KIND),
        ("model", _MODEL_KIND),
    ):
        nested = document.get(section)
        if not isinstance(nested, dict):
            return None, f"{section}セクションがありません"
        kind = nested.get("kind")
        if kind != expected:
            return None, f"未対応の{section} kindです: {kind!r}"
        document[section] = {
            key: value for key, value in nested.items() if key != "kind"
        }
    return document, None
