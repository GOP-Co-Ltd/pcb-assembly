#!/bin/bash
set -e

# システム依存関係をインストール
sudo apt-get update
sudo apt-get install -y \
    v4l-utils \
    curl \
    kicad \
    make

# Astral uvをインストール
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# Pythonの環境をセットアップ
make setup
