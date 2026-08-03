# Pcb Assembly

## セットアップ

### ハードウェア

- Raspberry Pi 5
- Pick and PlaceまたはPaste Dispenser Machine
- スピーカー（通知音を使う場合。I2S DAC / アンプまたは HDMI 音声出力）

### ソフトウェア

- RPi OS 64bit
- [Klipper](https://www.klipper3d.org/)
    - [KIAUH](https://github.com/dw-0/kiauh) 経由でインストール
        - Klipper + Moonraker + Mailsail
- [KiCAD](https://www.kicad.org/download/linux/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- `alsa-utils`（通知音を再生する `aplay` を含む）

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

### マシン設定の配置

装置を動かす前に、テンプレートから `config/` を作る。

```sh
./setup-machine-config.sh       # data/config-templates/ から選んで config/ を作る
sudo systemctl restart klipper  # printer.cfg の反映
```

`config/` は git 管理外（実測値の書き換えでリポジトリが dirty にならないようにするため）。
テンプレートの規約と printer.cfg の運用は
[`data/config-templates/README.md`](data/config-templates/README.md) を参照。

## WebUI

装置をブラウザから操作するUI（port 8080）。

```sh
make webui      # 起動
make webui-dev  # 開発用（auto-reload）
make webui-fake # fake camera + 隔離 data_dir で起動
```

### 通知音

通知対象ジョブ（はんだ塗布など）の成功・失敗時に、Raspberry Pi 本体に接続したスピーカーから
通知音を再生する。ブラウザからは音を鳴らさず、完了通知は画面表示のみ。

開発タブの `/dev/audio` で出力デバイス・音量を選び、テスト再生で確認できる。設定は
`config/machine.toml` の `[audio]` に保存される（未設定時は ALSA のシステム既定デバイス・音量 75%）。

音声ファイルを差し替える場合は `src/pcbasm/hal/sounds/success.wav` と `failure.wav` を
**非圧縮 16-bit PCM WAV** で同名のまま上書きする（git-lfs 追跡下）。差し替え後は `/dev/audio` の
テスト再生で確認する。

systemd サービスとして登録し、システム起動時に自動起動する場合:

```bash
./webui-service.sh install  # サービス登録・自動起動を有効化
./webui-service.sh start    # 起動
./webui-service.sh stop     # 一時停止（次回のシステム起動時には自動起動）
./webui-service.sh restart  # 最新のソースで再起動
./webui-service.sh status   # 状態確認
./webui-service.sh remove   # サービス登録を削除
```

通知音を使う場合は、サービスの実行ユーザーから対象の ALSA PCM を再生できることも確認する。

環境変数で動作を切り替えられる（全量は `src/webui/settings.py`）:

- `PCBASM_WEBUI_FAKE_CAMERA=1` — カメラ実機なしで固定画像を配信
- `PCBASM_CONFIG_DIR` — マシン設定ディレクトリ（既定 `config/`）の差し替え。WebUI と pcbasm コア層で共通
- `PCBASM_WEBUI_DATA_DIR` — 成果物・状態ファイルの保存先

WebUI のブラウザ E2E は実 uvicorn と Chromium で検証する:

```sh
make test-e2e
make playwright-install  # /usr/bin/chromium が無い環境向け
```

### XYステージのキャリブレーション

XY軸の移動誤差は、WebUIの「位置合わせ」→「XYキャリブレーション」から補正できる。
既知間隔の穴を格子状に配置した実ボードが必要で、既定値は穴径3 mm、間隔10 mm、
縦5点×横5点。使用するボードに合わせて各値を変更する。

ジョブを開始したら左上の穴をカメラ中央へ合わせて `Record` を押す。以降は四隅、
全点、逆順の精度検証まで自動で進み、RMS誤差0.03 mm以下かつ最大誤差0.05 mm以下の
場合だけ結果をApplyできる。Apply時は基準点、ノズルキャップ、ツールヘッドoffsetの
既存座標も新しい座標系へ自動移行するが、精密運用の前に各位置を再確認する。

通常のWebUI操作や塗布経路には補正が適用される。開発用コンソール等から直接送る
raw G-codeは補正対象外となる。
