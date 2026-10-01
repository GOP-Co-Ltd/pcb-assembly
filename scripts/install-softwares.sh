#!/bin/bash
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

# git-lfs を有効化し、git-lfs 未導入のまま clone して
# ポインタのまま残った (破損した) LFS ファイルを実体に置き換える
git lfs install
(cd "$PROJECT_ROOT" && git lfs pull)

# KIAUH で Klipper をインストール
cd ~ && git clone https://github.com/dw-0/kiauh.git
./kiauh/kiauh.sh

# 独自 kinematics (inversed_corexy) を Klipper に symlink する
"$PROJECT_ROOT/scripts/install-klipper-kinematics.sh"

# ロードセルプローブ ([load_cell_probe]) の依存を klippy-env に追加
# numpy は必須、scipy は drift/notch フィルタ (drift_filter_cutoff_frequency 等) 用
~/klippy-env/bin/pip install numpy scipy

# Astral uv をインストール
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# Python の環境をセットアップ
cd "$PROJECT_ROOT"
make setup
