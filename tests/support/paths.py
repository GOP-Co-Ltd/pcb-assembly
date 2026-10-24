"""テストが参照するリポジトリ内のパス."""

from pcbasm.paths import PROJECT_ROOT

TESTING_DATA_DIR = PROJECT_ROOT / "data" / "testing"

# WebUI / E2E 用の config ディレクトリ fixture（Klipper port 7126 = 非リッスン）。
# コア層用の data/testing/machine.toml とは別物（用途差は data/config-templates/README.md 参照）
TESTING_CONFIG_DIR = TESTING_DATA_DIR / "config"
