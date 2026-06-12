# Pcb Assembly

## セットアップ

### ハードウェア

- Raspberry Pi 5
- Pick and PlaceまたはPaste Dispenser Machine

### ソフトウェア

- RPi OS 64bit
- [Klipper](https://www.klipper3d.org/)
    - [KIAUH](https://github.com/dw-0/kiauh) 経由でインストール
        - Klipper + Moonraker + Mailsail
- [KiCAD](https://www.kicad.org/download/linux/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

OS以外のソフトウェア類は[`install-softwares.sh`](install-softwares.sh)を実行

### 開発

上記のソフトウェアをインストールしたうえで、次を実行

```sh
make setup
```

- VSCodeでリモートアクセスし、開発することを推奨する。

## WebUI

装置をブラウザから操作するUI（port 8080）。仕様は [`docs/webui/specification.md`](docs/webui/specification.md)。

```sh
make webui      # 起動
make webui-dev  # 開発用（auto-reload）
```

環境変数で動作を切り替えられる（全量は `src/webui/settings.py`）:

- `PCBASM_WEBUI_FAKE_CAMERA=1` — カメラ実機なしで固定画像を配信
- `PCBASM_WEBUI_CONFIGS_ROOT` — configsルートの差し替え
- `PCBASM_WEBUI_DATA_DIR` — 成果物・状態ファイルの保存先
