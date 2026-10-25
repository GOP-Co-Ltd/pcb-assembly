"""テストが読む入力ファイルの場所（tests/data）."""

from pcbasm.paths import PROJECT_ROOT

TEST_DATA_DIR = PROJECT_ROOT / "tests" / "data"

# 機体の設定ディレクトリの完全版。WebUI と E2E が複製して読み書きする（Klipper は非リッスンの 127.0.0.1:7126）
TEST_CONFIG_DIR = TEST_DATA_DIR / "config"
# machine.toml の最小版。pcbasm の単体テストがパスを渡して読む
MINIMAL_MACHINE_TOML = TEST_DATA_DIR / "machine_minimal.toml"

# TOP 銅箔のベタを持つ実基板（高さ計測と銅箔照合ができる）
LED_BLINKER_PCB = TEST_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
# pad だけを持つ実基板（銅箔のベタが無い）
FILL_COVERAGE_PCB = TEST_DATA_DIR / "fill_coverage" / "fill_coverage.kicad_pcb"

# FakeCamera が返し続ける固定画像
FAKE_CAMERA_IMAGE = TEST_DATA_DIR / "webui" / "fake_camera.png"
# 5x5 内部コーナー・1 マス約 66.7 px のチェッカーボード（400x400、square_size=10 mm で pixel_per_mm ≈ 6.67）
CHECKERBOARD_IMAGE = TEST_DATA_DIR / "checkerboard.png"

# 塗布量の校正ファイル（v1 形式の実物）
PASTE_VOLUME_CALIBRATION = (
    TEST_DATA_DIR / "schemas" / "paste_volume_calibration_v1.json"
)

# 塗布量 dataset の metadata（schema v3 の実物）
PASTE_DATASET_METADATA = TEST_DATA_DIR / "schemas" / "paste_dataset_metadata_v3.json"
# 同じ収集の、計量質量が未確定の metadata（pending schema v1 の実物）
PASTE_DATASET_PENDING = TEST_DATA_DIR / "schemas" / "paste_dataset_pending_v1.json"
