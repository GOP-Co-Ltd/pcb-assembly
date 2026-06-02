#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KLIPPER_KINEMATICS_DIR="$HOME/klipper/klippy/kinematics"

ln -sf "$SCRIPT_DIR/klipper/inversed_corexy.py" "$KLIPPER_KINEMATICS_DIR/"

echo "Linked kinematics to $KLIPPER_KINEMATICS_DIR"
