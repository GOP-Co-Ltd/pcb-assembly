# PCB Assembly

KiCad の基板データを読み込み、はんだペーストをパッドへ塗る装置の制御ソフトウェア。
Raspberry Pi 5 の上で Klipper にステージとディスペンサーを動かさせ、カメラで基板の位置を合わせ、ロードセルで高さを測ってから塗る。
操作はすべてブラウザから行う。

- 塗布: パッドごとの塗布設定、経路のプレビュー、位置合わせと高さ計測、初回パージの撮影確認、塗布
- 校正: カメラ、基準点、ノズル位置、ツールヘッドオフセット、吐出量、画像による塗布量の校正
- 基板: テスト塗布基板・格子基板・矩形基板の生成、PCB の情報の抽出
- 運用: LAN 上の複数機体の切替と操作権、通知音、設定履歴とバックアップ、WebUI からのソフトウェア更新

## 前提

- Raspberry Pi 5 と Raspberry Pi OS 64bit（Debian 13 trixie）
- Klipper の MCU、カメラ、ロードセルプローブ、オーガースクリュー式ディスペンサーを備えた機体。通知音を使うならスピーカー
- 機体とブラウザが同じ信頼できる LAN にあること（WebUI に認証は無い）

## クイックスタート

ホスト名を機体ごとに一意な名前へ変えた Raspberry Pi で、次の 3 行を実行する。

```sh
git clone https://github.com/GOP-Co-Ltd/pcb-assembly.git ~/pcb-assembly
cd ~/pcb-assembly && ./install.sh
sudo reboot
```

再起動したら、ブラウザで `http://<ホスト名>.local:8080` を開く。
途中で聞かれること（カメラやスピーカーの種類、機種）は [導入](docs/install.md) にある。

## 文書の地図

上から順に読めば、空の SD カードから日常の塗布と保守まで進める。

| 段階        | 文書                                                                                      | 内容                                                                 |
| ----------- | ----------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 1. 導入     | [導入](docs/install.md)                                                                   | 空の SD カードから WebUI が開くまで                                  |
| 2. 立ち上げ | [立ち上げ](docs/commissioning.md)                                                         | 新しい機体を塗布できる状態まで校正する順序と、各段の合格条件         |
| 3. 日常     | [日常の運用](docs/daily.md)                                                               | 塗布、ローディング、ノズル清掃、複数機体、通知音、データセットの収集 |
| 4. 保守     | [保守ガイド](docs/maintenance.md)                                                         | 更新、バックアップと復元、ロールバック、復旧、ホストの追加、ログ     |
| 確認        | [実機受け入れ試験](docs/acceptance.md)                                                    | 変更を merge する前に実機で確かめる項目と手順                        |
| 参照        | [設定キー一覧](docs/reference/config.md)                                                  | `machine.toml` に書けるキー                                          |
| 参照        | [ファイルと環境変数](docs/reference/files.md)                                             | 設定・データ・ログの置き場所と、起動時の環境変数                     |
| 開発        | [開発の手引き](docs/develop/index.md)                                                     | 不具合の報告、開発環境、検証、テストの書き方、実機なしでの画面確認   |
| 開発        | [アーキテクチャ](docs/develop/architecture.md)                                            | 層と依存方向、機能からコードへの地図、機能を足すときの変更先         |
| 設計        | [画像ベース吐出量キャリブレーション要件](docs/design/image-based-dispense-calibration.md) | 運転時の流量補正と、塗布量校正のデータ収集の要件                     |
| 設計        | [直径ベースの塗布量校正](docs/design/paste-volume-diameter-calibration.md)                | 塗布量の推定モデルと実測記録                                         |
| 資産        | [マシン設定テンプレート](deploy/templates/README.md)                                      | 機種ごとの `machine.toml` と `printer.cfg` の初期値                  |
| 資産        | [GitHub Actions Runner](github-runner/README.md)                                          | CI 用の専用 Raspberry Pi                                             |
