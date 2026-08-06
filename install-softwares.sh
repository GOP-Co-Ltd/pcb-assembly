#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# システム依存関係をインストール
"${SCRIPT_DIR}/install-os-packages.sh"

# git-lfsを有効化し、git-lfs未導入のままcloneして
# ポインタのまま残った (破損した) LFSファイルを実体に置き換える
git lfs install
(cd "$(dirname "$0")" && git lfs pull)

# KIAUHでKlipperをインストール
cd ~ && git clone https://github.com/dw-0/kiauh.git
./kiauh/kiauh.sh

# ロードセルプローブ ([load_cell_probe]) の依存を klippy-env に追加
# numpy は必須、scipy は drift/notch フィルタ (drift_filter_cutoff_frequency 等) 用
~/klippy-env/bin/pip install numpy scipy

# Astral uvをインストール
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# Pythonの環境をセットアップ
cd "$(dirname "$0")"
make setup
