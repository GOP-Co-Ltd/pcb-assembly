# GitHub Actions Runner

GitHub Actions専用のRaspberry Pi 5（arm64、8GB RAM）を、self-hosted runnerとして
構築する。self-hosted runnerは1 instanceあたり1 jobしか実行しないため、3 job並列に
するためにrunner instanceを3つ登録する。

## 前提

- DebianまたはRaspberry Pi OS Trixie（arm64）
- GitHub repositoryのAdmin権限
- outbound HTTPS接続
- `sudo`を利用できる一般ユーザー
- 認証済みの`gh` CLI（registration tokenの自動取得に使う。無い場合は手入力）

self-hosted runnerではCI jobがhost上で直接コマンドを実行する。このrunnerは
pcb-assembly repository専用とし、信頼できるbranchやpull requestだけを実行すること。
**public repositoryでは絶対に使わない**（fork PRが任意のコードをhost上で実行できる）。

runner userを`video`、`gpio`、`dialout` groupへ追加しない。GitHub Actionsではcameraや
装置を使うhardware testを実行しない。

## セットアップ

repositoryをcheckoutしたRaspberry Pi上で、`sudo`を付けずに実行する。

```bash
./github-runner/setup.sh setup
```

次を一度に実行する。

- CIに必要なOS package、Git LFS、KiCad（`pcbnew`）、Chromium、`uv 0.10.9`のインストール
- `github-runner` userの作成とhost cache directoryの準備
- `actions/runner`の最新releaseを`/opt/actions-runner/pcb-assembly-rpi-{1,2,3}`へ展開
- registration tokenの取得（`gh`）または非表示入力による3 instanceの登録
- systemd serviceの作成・起動と、service・登録状態・GitHub接続の検証

処理を分ける場合は、次の順に実行する。

```bash
./github-runner/setup.sh install
./github-runner/setup.sh register
./github-runner/setup.sh verify
```

runner versionを固定する場合は`RUNNER_VERSION`を指定する。

```bash
RUNNER_VERSION=2.330.0 ./github-runner/setup.sh install
```

registration tokenは1時間で失効する短命tokenで、コマンド引数のみで渡すため
repositoryやshell履歴には残らない。登録後の認証情報はrunner instance directoryの
`.runner` / `.credentials`に保存されるため、これらをrepositoryへコピーしないこと。

## label

各instanceは`rpi-ci` labelを付けて登録する。`self-hosted`、`linux`、`ARM64`は
GitHubが自動で付与する。workflow側は次で選択する。

```yaml
runs-on: [self-hosted, linux, ARM64, rpi-ci]
```

## 状態確認

```bash
./github-runner/setup.sh status
./github-runner/setup.sh verify
```

`verify`は次を確認する。

- 3 instanceが登録済みで、systemd serviceが有効かつ起動中
- Git LFSのsystem filter設定と`pcbnew`のimport
- `uv` version
- GitHub側のrunner一覧（name / status / busy / label）
- CPU、memory、disk使用量

GitHub側の一覧は**Settings > Actions > Runners**でも確認できる。

## 依存関係とcache

OS packageはjobごとにインストールしない。各jobは独立した`.venv`を作るが、package
本体とpre-commit hook環境は次のhost cacheを再利用する。

```text
/home/github-runner/.cache/uv
/home/github-runner/.cache/pre-commit
```

3 instanceは同じhost cacheを共有する。`uv`とpre-commitはcacheへの並行アクセスを
lockで保護するため、並列jobでも安全に共有できる。

`pcbnew` Python moduleはTrixieの`kicad` packageから導入する。CIではsymbol、footprint、
demoを使わないため、`kicad`の推奨packageはインストールしない。

テスト画像・音声はGit LFSで管理する。`install`はGit LFS filterをsystem設定し、pytest
jobはcheckout済みworkspaceに対して`git lfs pull`を実行する。

`pytest` jobは非実機テストに続けて、fakeカメラと隔離したbackend・frontendを使う
WebUI E2Eを実行する。Chromiumはheadlessで起動し、カメラやGPIOの権限は不要。
両スイートのJUnitレポートは`pytest-report` artifactに7日間保存する。

新しいrunnerには`install`でsystem Chromiumを導入する。既存runnerに
`/usr/bin/chromium`が無い場合、workflowはPlaywrightのheadless shellをユーザーcacheへ
取得する。OS依存ライブラリが不足するホストは、管理者がChromiumを導入する。
jobからOS packageをインストールするためのsudo権限は与えない。

容量確認とuv cacheの安全な整理は次のとおり。

```bash
sudo du -sh /home/github-runner/.cache/uv
sudo du -sh /home/github-runner/.cache/pre-commit
sudo -u github-runner -H /usr/local/bin/uv cache prune
```

pre-commit cacheを全削除すると次回workflowでhook環境を再構築する。通常運用では削除
しない。

## Upgrade

Actions Runnerはserviceが自動でself-updateするため、通常は作業不要。

`install`は、`config.sh`がすでにあるinstance directoryを上書きしない。`register`も登録済み
instanceを登録し直さない。runner versionを明示的に入れ替える場合は、次の順に行う。

1. 「登録解除と削除」の手順で3 instanceすべてを削除する
2. `RUNNER_VERSION=<version> ./github-runner/setup.sh setup`を実行する

`uv`は、`setup.sh`の`UV_VERSION`を変えた変更がreviewされた後に`install`を再実行して
入れ替える（`install`は導入済み`uv`のversionが`UV_VERSION`と違うときだけ入れ直す）。

## 登録解除と削除

誤操作防止のため自動化していない。対象instanceを確認してから、1 instanceずつ手動で実行する。
`svc.sh`と`config.sh`はrunner rootをcwdにして実行する。

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

3 instanceすべてを削除したら、GitHubの**Settings > Actions > Runners**に残骸が無いか
確認する。

## 参考

- [About self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners)
- [Adding self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/adding-self-hosted-runners)
- [Self-hosted runner security](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#hardening-for-self-hosted-runners)
