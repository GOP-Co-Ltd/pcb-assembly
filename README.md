# PCB Assembly

Raspberry Pi 5 と Klipper で PCB アセンブリ装置を制御するソフトウェア。
KiCad の基板データを読み込み、カメラによる位置合わせ、高さ計測、ペースト塗布を
ブラウザから操作する。部品実装（Pick and Place）は名前空間のみで、未実装。

## できること

- カメラのキャリブレーション、銅箔検出、基板の基準点合わせ
- パッドごとの塗布設定、経路プレビュー、ロードセルによる高さ計測、はんだ塗布
- 吐出量のキャリブレーション、塗布画像の収集と直径ベースの塗布量校正
- LAN 上の複数機体の切替、操作権の管理、ジョブの進捗・ログ・成果物の確認

## はじめる

装置を初めて構築する場合は [セットアップ](docs/setup.md) を参照する。
Raspberry Pi OS 64bit、システム Python、KiCad、picamera2、Klipper、uv を利用する。
Python パッケージのインストールだけでは OS 提供の `pcbnew` / `picamera2` は入らない。

セットアップ済みの機体では、別々のターミナルで起動する。

```sh
make api  # 機体の backend（8081）
make ui   # ブラウザ向け frontend（8080）
```

ブラウザで `http://<frontendのホスト名>:8080` を開き、機体を選択する。
同じ LAN の機体は mDNS で探索される。見つからない場合は
[マシンの登録](docs/webui.md#%E3%83%9E%E3%82%B7%E3%83%B3%E3%81%AE%E7%99%BB%E9%8C%B2) を行う。

WebUI は装置を実際に動かす。変更操作の前に画面で操作権を取得する。
緊急停止とジョブ中止は、操作権を持たない閲覧者も実行できる。
認証は備えていないため、[信頼するネットワーク内で運用する](docs/webui.md#%E5%85%AC%E9%96%8B%E7%AF%84%E5%9B%B2%E7%84%A1%E8%AA%8D%E8%A8%BC%E3%81%A7%E3%81%82%E3%82%8B%E3%81%93%E3%81%A8%E3%81%AE%E6%B3%A8%E6%84%8F)。

## ドキュメント

| 目的                                   | ガイド                                                                             |
| -------------------------------------- | ---------------------------------------------------------------------------------- |
| 装置・OS・マシン設定を準備する         | [セットアップ](docs/setup.md)                                                      |
| 機体を選び、PCB を開いて作業する       | [WebUI の使い方](docs/webui.md)                                                    |
| サービスを常駐させ、更新・復旧する     | [運用ガイド](docs/operations.md)                                                   |
| 不具合を報告し、コードを変更・検証する | [CONTRIBUTING](CONTRIBUTING.md)                                                    |
| コードの責務と依存方向を知る           | [アーキテクチャ](docs/architecture.md)                                             |
| マシン設定テンプレートを扱う           | [設定テンプレート](data/config-templates/README.md)                                |
| 塗布量校正の原理・実測結果を確認する   | [直径ベースの塗布量校正](docs/paste-volume-diameter-calibration.md)                |
| 画像収集・校正の設計背景を調べる       | [画像ベース吐出量キャリブレーション要件](docs/image-based-dispense-calibration.md) |
| CI 用の専用 Raspberry Pi を構築する    | [GitHub Actions Runner](github-runner/README.md)                                   |

## 開発コマンド

```sh
make help              # コマンド一覧
make format            # フォーマットと lint
make type              # 型チェック
make test-no-hardware  # ハードウェア・E2E を除くテスト
make test-e2e          # 隔離した backend + frontend + Chromium の E2E
```

`make test` と `make run` は実機テストを含む。装置の動作を確認できる担当者が実行する。
開発環境の準備、実機設定を使わない画面確認、テストの選び方は
[CONTRIBUTING](CONTRIBUTING.md) にまとめている。
