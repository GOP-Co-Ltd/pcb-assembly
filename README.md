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

装置をブラウザから操作するUI（port 8080）。

```sh
make webui      # 起動
make webui-dev  # 開発用（auto-reload）
make webui-fake # fake camera + 隔離 data_dir で起動
```

systemd サービスとして登録し、システム起動時に自動起動する場合:

```bash
./webui-service.sh install  # サービス登録・自動起動を有効化
./webui-service.sh start    # 起動
./webui-service.sh stop     # 一時停止（次回のシステム起動時には自動起動）
./webui-service.sh restart  # 最新のソースで再起動
./webui-service.sh status   # 状態確認
./webui-service.sh remove   # サービス登録を削除
```

`stop`、`restart`、`remove`、再度の `install`、および直接の
`systemctl stop` は、実行中ジョブを中断せず、自然終了後の退避処理と装置ロック解放まで
無期限に待ってからサービスを停止する。待機中は新規ジョブを拒否するが、HTTP / WebSocket /
MJPEG は維持されるため、入力待ちジョブの prompt には引き続き応答できる。

停止待機を緊急に打ち切る場合は、別の端末から次を実行する。

```bash
sudo systemctl kill --kill-whom=all --signal=SIGINT pcbasm-webui.service
```

この仕組みを初めて導入する際は、稼働中の旧プロセスには反映されない。ジョブが実行されて
いないことを確認し、`./webui-service.sh install` を再実行する。SIGKILL、プロセスの crash、
電源断ではジョブ完了待機は保証されない。

環境変数で動作を切り替えられる（全量は `src/webui/settings.py`）:

- `PCBASM_WEBUI_FAKE_CAMERA=1` — カメラ実機なしで固定画像を配信
- `PCBASM_WEBUI_CONFIGS_ROOT` — configsルートの差し替え
- `PCBASM_WEBUI_DATA_DIR` — 成果物・状態ファイルの保存先

WebUI のブラウザ E2E は実 uvicorn と Chromium で検証する:

```sh
make test-e2e
make playwright-install  # /usr/bin/chromium が無い環境向け
```
