"""ジョブの golden を採るシナリオの表（ジョブ × 分岐 × カメラの回転）.

送信列の golden は ``jobs/<シナリオ名>.gcode`` に置く。
"""

import attrs

# カメラの回転 [度]。0、実機相当の小角、R と R⁻¹ の差が見える 5°
# 0.3° は 001 の実測値ではない（回転は位置合わせのたびに測って保存しないので、実測値が残っていない）
CAMERA_ROTATIONS = (0.0, 0.3, 5.0)
# 塗布系ジョブは両端の 2 通りで採る
PASTE_ROTATIONS = (0.0, 5.0)

# paste_solder の分岐。初回パージの写り方・運転時補正・クリーニング位置の教示で分かれる
PASTE_SOLDER_BRANCHES = (
    "purge_shows",
    "cleaned",
    "still_clogged",
    "flow_corrected",
    "no_nozzle_clean",
)


@attrs.frozen
class Scenario:
    """1 本の golden を採る実行.

    Attributes:
        job: ジョブ名（catalog の name）
        branch: ジョブの中で通る分岐
        rotation: 機械の軸に対するカメラの回転 [度]
    """

    job: str
    branch: str
    rotation: float = 0.0

    @property
    def name(self) -> str:
        """拡張子を除いた golden のファイル名（jobs/ に置く）."""
        return f"{self.job}_{self.branch}_rot{self.rotation:g}"


SCENARIOS = (
    Scenario("reference_point_setup", "jog_then_record"),
    Scenario("camera_calibration", "checkerboard"),
    *(Scenario("board_tour", "success", rotation) for rotation in CAMERA_ROTATIONS),
    *(
        Scenario("orthogonality_test", "one_cycle", rotation)
        for rotation in CAMERA_ROTATIONS
    ),
    *(
        Scenario("paste_solder", branch, rotation)
        for branch in PASTE_SOLDER_BRANCHES
        for rotation in PASTE_ROTATIONS
    ),
    Scenario("height_plane", "success"),
    Scenario("toolhead_offset", "success"),
    Scenario("loading", "commands"),
)


def scenarios_of(job: str, branch: str | None = None) -> list[Scenario]:
    """ジョブ（branch を与えればその分岐）のシナリオを表の順に返す."""
    return [
        scenario
        for scenario in SCENARIOS
        if scenario.job == job and branch in (None, scenario.branch)
    ]
