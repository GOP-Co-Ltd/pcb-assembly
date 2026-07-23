# Pcb Assembly

## セットアップ

### ハードウェア

- Raspberry Pi 5
- MAX98357A I2S 音声アンプ（通知音を使う場合。I2C デバイスではない）
- Pick and PlaceまたはPaste Dispenser Machine

### ソフトウェア

- RPi OS 64bit
- [Klipper](https://www.klipper3d.org/)
    - [KIAUH](https://github.com/dw-0/kiauh) 経由でインストール
        - Klipper + Moonraker + Mailsail
- [KiCAD](https://www.kicad.org/download/linux/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- `alsa-utils`（Raspberry Pi 本体から通知音を再生する `aplay` を含む）

OS以外のソフトウェア類は[`install-softwares.sh`](install-softwares.sh)を実行

### GitLab CI Runner

専用のRaspberry Pi 5をGitLab Runnerとして構築する場合は、
[`gitlab-runner/README.md`](gitlab-runner/README.md)を参照する。

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

選択中マシンに[`[audio]`](configs/README.md#audio)を設定すると、通知対象の
ジョブ完了時に成功音または失敗音を Raspberry Pi 本体から再生する。開発タブ
`/dev/audio_test` では両方の音を個別に確認できる。ブラウザからは音声を再生せず、
完了時の視覚通知だけを表示する。

systemd サービスとして登録し、システム起動時に自動起動する場合:

```bash
./webui-service.sh install  # サービス登録・自動起動を有効化
./webui-service.sh start    # 起動
./webui-service.sh stop     # 一時停止（次回のシステム起動時には自動起動）
./webui-service.sh restart  # 最新のソースで再起動
./webui-service.sh status   # 状態確認
./webui-service.sh remove   # サービス登録を削除
```

音声を有効にする場合は、systemd サービスの実行ユーザーから設定した ALSA PCM を
利用できることも確認する。

環境変数で動作を切り替えられる（全量は `src/webui/settings.py`）:

- `PCBASM_WEBUI_FAKE_CAMERA=1` — カメラ実機なしで固定画像を配信
- `PCBASM_WEBUI_CONFIGS_ROOT` — configsルートの差し替え
- `PCBASM_WEBUI_DATA_DIR` — 成果物・状態ファイルの保存先

WebUI のブラウザ E2E は実 uvicorn と Chromium で検証する:

```sh
make test-e2e
make playwright-install  # /usr/bin/chromium が無い環境向け
```
