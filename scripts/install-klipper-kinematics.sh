#!/bin/bash
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KLIPPER_KINEMATICS_DIR="$HOME/klipper/klippy/kinematics"

ln -sf "$PROJECT_ROOT/klipper/inversed_corexy.py" "$KLIPPER_KINEMATICS_DIR/"

echo "Linked kinematics to $KLIPPER_KINEMATICS_DIR"
