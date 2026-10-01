# GitHub Actions Runner

GitHub Actions 専用の Raspberry Pi 5（arm64、8GB RAM）を self-hosted runner として
構築する。self-hosted runner は 1 instance あたり 1 job しか実行しない。3 job を並列に
実行するため、runner instance を 3 つ登録する。

## 前提

- Debian または Raspberry Pi OS Trixie（arm64）
- GitHub repository の Admin 権限
- outbound HTTPS 接続
- `sudo` を利用できる一般ユーザー
- 認証済みの `gh` CLI（registration token の自動取得に使う。無い場合は手入力）

self-hosted runner では CI job が host 上で直接コマンドを実行する。この runner は
pcb-assembly repository 専用とし、信頼できる branch や pull request だけを実行すること。
**public repository では絶対に使わない**（fork PR が任意のコードを host 上で実行できる）。

runner user を `video`、`gpio`、`dialout` group へ追加しない。GitHub Actions では camera や
装置を使う hardware test を実行しない。

## セットアップ

repository を checkout した Raspberry Pi 上で、`sudo` を付けずに実行する。

```bash
./github-runner/setup.sh setup
```

次を一度に実行する。

- CI に必要な OS package、Git LFS、KiCad（`pcbnew`）、Chromium、`uv 0.10.9` のインストール
- `github-runner` user の作成と host cache directory の準備
- `actions/runner` の最新 release を `/opt/actions-runner/pcb-assembly-rpi-{1,2,3}` へ展開
- registration token の取得（`gh`）または非表示入力による 3 instance の登録
- systemd service の作成・起動と、service・登録状態・GitHub 接続の検証

処理を分ける場合は、次の順に実行する。

```bash
./github-runner/setup.sh install
./github-runner/setup.sh register
./github-runner/setup.sh verify
```

runner version を固定する場合は `RUNNER_VERSION` を指定する。

```bash
RUNNER_VERSION=2.330.0 ./github-runner/setup.sh install
```

registration token は 1 時間で失効する短命 token で、コマンド引数のみで渡すため
repository や shell 履歴には残らない。登録後の認証情報は runner instance directory の
`.runner` / `.credentials` に保存される。これらを repository へコピーしないこと。

## label

各 instance は `rpi-ci` label を付けて登録する。`self-hosted`、`linux`、`ARM64` は
GitHub が自動で付与する。workflow 側は次の指定で runner を選択する。

```yaml
runs-on: [self-hosted, linux, ARM64, rpi-ci]
```

## 状態確認

```bash
./github-runner/setup.sh status
./github-runner/setup.sh verify
```

`verify` は次を確認する。

- 3 instance が登録済みで、systemd service が有効かつ起動中
- Git LFS の system filter 設定と `pcbnew` の import
- `uv` version
- GitHub 側の runner 一覧（name / status / busy / label）
- CPU、memory、disk 使用量

GitHub 側の一覧は Settings > Actions > Runners でも確認できる。

## 依存関係と cache

OS package は job ごとにインストールしない。各 job は独立した `.venv` を作るが、package
本体と pre-commit hook 環境は次の host cache を再利用する。

```text
/home/github-runner/.cache/uv
/home/github-runner/.cache/pre-commit
```

3 instance は同じ host cache を共有する。`uv` と pre-commit は cache への並行アクセスを
lock で保護するため、並列 job でも安全に共有できる。

`pcbnew` Python module は Trixie の `kicad` package から導入する。CI では symbol、footprint、
demo を使わないため、`kicad` の推奨 package はインストールしない。

テスト画像・音声は Git LFS で管理する。`install` は Git LFS filter を system 設定し、pytest
job は checkout 済み workspace に対して `git lfs pull` を実行する。

`pytest` job は非実機テストに続けて、fake カメラと隔離した backend・frontend を使う
WebUI E2E を実行する。Chromium は headless で起動し、カメラや GPIO の権限は不要。
両スイートの JUnit レポートは `pytest-report` artifact に 7 日間保存する。

新しい runner には `install` で system Chromium を導入する。既存 runner に
`/usr/bin/chromium` が無い場合、workflow は Playwright の headless shell をユーザー cache へ
取得する。OS 依存ライブラリが不足するホストには、管理者が Chromium を導入する。
job から OS package をインストールするための sudo 権限は与えない。

容量確認と uv cache の安全な整理は次のとおり。

```bash
sudo du -sh /home/github-runner/.cache/uv
sudo du -sh /home/github-runner/.cache/pre-commit
sudo -u github-runner -H /usr/local/bin/uv cache prune
```

pre-commit cache を全削除すると、次回の workflow で hook 環境を再構築する。通常運用では
削除しない。

## Upgrade

Actions Runner は service が自動で self-update するため、通常は作業不要。

`install` は、`config.sh` がすでにある instance directory を上書きしない。`register` も登録済み
instance を登録し直さない。runner version を明示的に入れ替える場合は、次の順に行う。

1. 「登録解除と削除」の手順で 3 instance すべてを削除する
2. `RUNNER_VERSION=<version> ./github-runner/setup.sh setup` を実行する

`uv` を入れ替えるには、`setup.sh` の `UV_VERSION` を変えた変更が review された後に `install` を
再実行する（`install` は導入済み `uv` の version が `UV_VERSION` と違うときだけ入れ直す）。

## 登録解除と削除

誤操作防止のため自動化していない。対象 instance を確認してから、1 instance ずつ手動で実行する。
`svc.sh` と `config.sh` は runner root を cwd にして実行する。

```bash
dir=/opt/actions-runner/pcb-assembly-rpi-1
cd "$dir"
sudo ./svc.sh stop
sudo ./svc.sh uninstall
sudo -u github-runner ./config.sh remove \
    --token "$(gh api --method POST \
        repos/GOP-Co-Ltd/pcb-assembly/actions/runners/remove-token --jq .token)"
cd - && sudo rm -rf "$dir"
```

3 instance すべてを削除したら、GitHub の Settings > Actions > Runners に削除済み runner が
残っていないか確認する。

## 参考

- [About self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners)
- [Adding self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/adding-self-hosted-runners)
- [Self-hosted runner security](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#hardening-for-self-hosted-runners)
