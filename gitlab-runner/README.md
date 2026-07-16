# GitLab Runner

GitLab CI専用のRaspberry Pi 5（arm64、8GB RAM）を、3 job並列のShell
executorとして構築する。

## 前提

- DebianまたはRaspberry Pi OS Trixie（arm64）
- GitLab projectのMaintainer権限
- outbound HTTPS接続
- `sudo`を利用できる一般ユーザー

Shell executorではCI jobがhost上で直接コマンドを実行する。このrunnerは
pcb-assembly project専用とし、信頼できるbranchやMRだけを実行すること。

runner userを`video`、`gpio`、`dialout` groupへ追加しない。GitLab CIではcameraや
装置を使うhardware testを実行しない。

## GitLab側でproject runnerを作成

1. GitLabのprojectを開く。
2. **Settings > CI/CD > Runners > Create project runner**を開く。
3. 次の値を指定する。
    - Tags: `rpi-ci`
    - Run untagged jobs: 無効
    - Protected: 無効
    - Runner description: `pcb-assembly-rpi`
4. runnerを作成し、一度だけ表示される`glrt-`で始まる認証tokenを控える。

tagやprotected設定はrunner登録コマンドではなく、GitLab側で管理する。

## セットアップ

repositoryをcheckoutしたRaspberry Pi上で、`sudo`を付けずに実行する。

```bash
./gitlab-runner/setup.sh setup
```

次を一度に実行する。

- GitLab公式APT repositoryとGitLab Runnerのインストール
- CIに必要なOS packageと`uv 0.10.9`のインストール
- `concurrent = 3`、runner `limit = 3`の設定
- GitLab UIで発行した認証tokenの非表示入力
- service、設定、GitLab接続の検証

処理を分ける場合は、次の順に実行する。

```bash
./gitlab-runner/setup.sh install
./gitlab-runner/setup.sh register
./gitlab-runner/setup.sh verify
```

`install`は`/etc/gitlab-runner/config.toml`を作成または更新し、globalの
`concurrent = 3`を設定する。再実行しても既存runnerの登録情報は保持される。

認証tokenはコマンド引数、repository、shell履歴には保存されない。GitLab Runnerが
`/etc/gitlab-runner/config.toml`へ保存するため、このファイルをrepositoryへコピー
しないこと。

## 状態確認

```bash
./gitlab-runner/setup.sh status
./gitlab-runner/setup.sh verify
```

`verify`は次を確認する。

- systemd serviceが有効かつ起動中
- `concurrent = 3`、Shell executor、runner `limit = 3`
- GitLabへのrunner認証
- `uv` version
- CPU、memory、disk使用量

## 依存関係とcache

OS packageはjobごとにインストールしない。各jobは独立した`.venv`を作るが、package
本体とpre-commit hook環境は次のhost cacheを再利用する。

```text
/home/gitlab-runner/.cache/uv
/home/gitlab-runner/.cache/pre-commit
```

容量確認とuv cacheの安全な整理は次のとおり。

```bash
sudo du -sh /home/gitlab-runner/.cache/uv
sudo du -sh /home/gitlab-runner/.cache/pre-commit
sudo -u gitlab-runner -H /usr/local/bin/uv cache prune
```

pre-commit cacheを全削除すると次回pipelineでhook環境を再構築する。通常運用では削除
しない。

## Upgrade

GitLab Runnerを明示的にupgradeする。

```bash
sudo apt-get update
sudo apt-get install --only-upgrade gitlab-runner
./gitlab-runner/setup.sh verify
```

`uv`は`setup.sh`の`UV_VERSION`を更新した変更をreviewした後、`install`を再実行する。

## 登録解除と削除

誤操作防止のため自動化していない。対象runner名とGitLab UIのrunner IDを確認してから
手動で実行する。

```bash
sudo gitlab-runner unregister --name pcb-assembly-rpi
sudo apt-get remove gitlab-runner
```

最後にGitLabの**Settings > CI/CD > Runners**からproject runnerを削除する。

## 参考

- [Registering runners](https://docs.gitlab.com/runner/register/)
- [Install GitLab Runner using the official repositories](https://docs.gitlab.com/runner/install/linux-repository/)
- [Shell executor security](https://docs.gitlab.com/runner/executors/shell/#security)
