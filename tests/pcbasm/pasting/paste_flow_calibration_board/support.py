"""流量キャリブレーション基板テストの共有実データ識別子."""

from pcbasm.pasting.paste_flow_calibration_board import (
    PasteFlowCalibrationCustomPadShapeId,
    PasteFlowCalibrationCustomPadSpec,
)

R0402 = "Resistor_SMD.pretty/R_0402_1005Metric#pad-0"
R0603 = "Resistor_SMD.pretty/R_0603_1608Metric#pad-0"
R1206 = "Resistor_SMD.pretty/R_1206_3216Metric#pad-0"
QFN = "Package_DFN_QFN.pretty/QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
SOT223 = "Package_TO_SOT_SMD.pretty/SOT-223-3_TabPin2"
CUSTOM_A = "custom:00000000000000000000000000000001"
CUSTOM_B = "custom:00000000000000000000000000000002"
CUSTOM_C = "custom:00000000000000000000000000000003"


def custom_pad(
    catalog_id: str,
    name: str,
    shape: PasteFlowCalibrationCustomPadShapeId = "rectangle",
    width_mm: float = 1.0,
    height_mm: float = 1.0,
    corner_radius_mm: float = 0.0,
) -> PasteFlowCalibrationCustomPadSpec:
    return PasteFlowCalibrationCustomPadSpec(
        catalog_id,
        name,
        shape,
        width_mm,
        height_mm,
        corner_radius_mm,
    )
