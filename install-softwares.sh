#!/bin/bash
set -e

# システム依存関係をインストール
sudo apt-get update
sudo apt-get install -y \
    alsa-utils \
    v4l-utils \
    curl \
    kicad \
    make \
    git \
    git-lfs \
    python3-picamera2

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
