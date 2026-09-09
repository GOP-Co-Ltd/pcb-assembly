# ML 学習・開発コンテナ

GPU workstation上で`src/ml/`を開発・学習するためのコンテナ。装置ドメインの依存
（KiCADの`pcbnew`、`picamera2`）を持たない環境で、ML側のコードとテストを完結させる。

方針は
[画像ベース吐出量推定 ML 実装計画](../../docs/image-based-dispense-calibration-ml-plan.md)
に従う。

## なぜコンテナに分けるか

`pyproject.toml`の`python-preference = "only-system"`は変えられない。Raspberry Pi 5
では`picamera2`と`pcbnew`をOSの`dist-packages`から取るため、uvのmanaged Pythonに
切り替えるとこれらが見えなくなる。

一方でhostのUbuntu 24.04はsystem Pythonの開発ヘッダ（`Python.h`）を別packageへ
分けている。これが無いと`torch.compile`のinductor backendがtritonのC拡張を
buildできず、計画で既定ONの`torch.compile`が落ちる。

base imageをDebian Trixieにすると、Python 3.13と`python3.13-dev`を標準packageで
持つため、`only-system`のままinductorが通る。Raspberry Pi 5も同じDebian Trixieなので、
3環境でPythonの出どころがそろう。

## 前提

- Docker Engineとdocker compose plugin v2.30以降（`gpus: all`がこのversionから）
- NVIDIA driverと[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- 実行ユーザーが`docker` groupに属していること
- outbound HTTPS接続

CUDA toolkitのhostへのinstallは不要。`torch`のcu130 wheelがCUDA runtime libraryを
同梱し、driver側の`libcuda`はContainer Toolkitが注入する。

## セットアップ

リポジトリをcheckoutしたhost上で、`sudo`を付けずに実行する。

```bash
make ml-docker-build   # image を build する
make ml-docker-up      # コンテナを常駐起動する
make ml-docker-sync    # ML 依存 (ml-hpo + ml-export) を install する
make ml-docker-smoke   # 環境を確認する
```

各targetは先に`docker/ml/write-env.sh`（`make ml-docker-env`）を実行し、hostの実状から
2つのfileを生成する。どちらもGit管理外。

- `docker/ml/.env`: `id -un` / `id -u` / `id -g`。コンテナ内のユーザーをhostと同じ
    uid/gidで作るため。bind mountしたリポジトリへ書いたfileの所有者がhost側で
    rootにならない
- `docker/ml/compose.credentials.yaml`: 後述の資格情報mount

`compose.yaml`は既定のuid/gidを持たず`.env`を必須にする。uidがずれたままbuildして
mount先の所有者が食い違う事故を防ぐ。

資格情報mountを別fileへ切り出すのは、**存在しないpathをbind mount sourceに書くと
Dockerがそこへroot所有の空directoryを作る**ため。host側の`~/.gitconfig`の位置に
root所有directoryができるとgitが壊れ、一般ユーザーでは消せない。生成scriptは
実在するsourceだけを書き出す。

## 日常の操作

コンテナは常駐させ、`docker compose exec`で使う。`run --rm`を毎回叩くと
container作成と破棄を繰り返して無駄になる。

```bash
make ml-docker-shell   # 対話 shell に入る
make ml-docker-test    # tests/ml を実行する
make ml-docker-check   # format → 型検査 → tests/ml
make ml-docker-down    # 停止する（named volume は残る）
```

`ml-docker-shell`は`ml-docker-up`に、`-test` / `-smoke` / `-check`は`ml-docker-sync`に
依存する。停止していれば起動し、依存が入っていなければinstallしてから走る。
named volumeを作り直した直後でもそのまま動く。

`ml-docker-up`は`up -d --build`を実行する。素の`up -d`はimageの有無だけを見るため、
Dockerfileを直しても古いimageで起動し、検証が古い環境で通ってしまう。層cacheが
効くので変更が無ければほぼ待たない。

`make ml-docker-check`が学習機での標準検証となる。hostの`make test-no-hardware`は
`pcbnew` / `picamera2`が無いためcollectできない。

## 型検査の範囲

`make ml-docker-check`の`pyright`と`pytest`は`src/ml`、`tests/ml`、`scripts/ml_smoke.py`を
対象にする（`Makefile`の`ML_TREES`と`ML_TEST_PATHS`）。

装置ドメインの大半は`pcbnew`と`picamera2`を要求し、それが無いコンテナでは未解決import
として必ず赤くなるので外す。ドメイン層`ml.paste_volume`は収集schemaを読むが装置HALへは
届かないので通る。その到達範囲は`tests/ml/test_architecture.py`が推移的に検証する。
装置側の型検査は実機環境の`make type`が担当する。

## コンテナに入っているもの

| 用途        | 内容                                                |
| ----------- | --------------------------------------------------- |
| Python      | Debian Trixieのsystem Python 3.13と`python3.13-dev` |
| build       | `build-essential`（inductorとtritonのC拡張）        |
| package管理 | version固定した`uv`                                 |
| Git         | `git`、`openssh-client`                             |
| GitLab      | version固定した`glab`                               |
| 権限        | hostと同じuid/gidの一般ユーザー、`sudo`はNOPASSWD   |

`git-lfs`は入れない。Debianのpackageが`/etc/gitconfig`へLFS filterを登録するのに
host側は未設定なので、同じworking treeをbind mountで共有するとLFS pointerの展開状態が
食い違う。ML学習データはLFS管理外。

## 資格情報とその露出範囲

`docker/ml/write-env.sh`が、hostに実在するものだけをmountする。

| source               | mount  | 用途                       |
| -------------------- | ------ | -------------------------- |
| `~/.gitconfig`       | ro     | commit時のuser情報とhelper |
| `~/.config/glab-cli` | ro     | `glab`の認証token          |
| `$SSH_AUTH_SOCK`     | socket | SSH remoteへのpush（優先） |
| `~/.ssh`             | ro     | agentが無い場合のfallback  |

SSH agentが動いていればsocketだけを渡す。秘密鍵をコンテナへ見せずに署名だけhost側へ
委譲できるため、鍵の露出範囲が狭い。agentを使うには`ssh-agent`と`ssh-add`を先に
実行する。

agentが無い場合は`~/.ssh`をread-onlyでmountする。
**read-onlyは改変を防ぐが読み出しは防がない。**
コンテナ内はPyPIのwheel、pre-commitがネットから取るhook環境、inductorが生成するC++を
それぞれ実行するので、そのいずれかがこのrepository用に限らない全鍵を読める。気にする場合はSSH agentを使うか、remoteをHTTPSにする（hostの`~/.gitconfig`は
`glab auth git-credential`をhelperに設定済みなので、HTTPSなら`~/.ssh`は不要）。

またread-onlyでは`known_hosts`へ追記できないため、未知hostへの初回SSH接続は
非対話で失敗する。host側で一度接続して`known_hosts`に入れておく。

## 保存領域

| path                     | 実体                   | 意図                        |
| ------------------------ | ---------------------- | --------------------------- |
| `/workspace`             | リポジトリのbind mount | 編集結果をhostと共有する    |
| `/opt/venv`              | named volume `ml-venv` | 仮想環境。再build間で保つ   |
| `~/.cache/uv`            | named volume           | package cache               |
| `~/.cache/pre-commit`    | named volume           | hook環境cache               |
| `~/.cache/torchinductor` | named volume           | `torch.compile`の生成コード |
| `~/.triton`              | named volume           | tritonのkernel cache        |

`torch.compile`のcacheはnamed volumeへ置く。既定は`/tmp`配下なのでcontainerを
作り直すたびに全再compileになる。

仮想環境をリポジトリの外（`/opt/venv`）へ置くのは、hostの`.venv`がhostの
interpreterを指しているため。`UV_PROJECT_ENVIRONMENT`で切り替えており、hostと
コンテナが同じ`.venv`を壊し合わない。

compose projectは`pcb-assembly-ml`に固定してある。`-f docker/ml/compose.yaml`指定では
project directoryが`docker/ml/`になり、既定のproject名が`ml`になってしまう。他
プロジェクトの同名directoryとnamed volume（学習環境そのもの）を共有して
しまうのを防ぐ。

named volumeは`make ml-docker-down`では消えない。作り直すときは明示する。消したあとは
`make ml-docker-sync`が依存の再installを行う。

```bash
docker compose -p pcb-assembly-ml down -v
```

## version の更新

`docker/ml/Dockerfile`の`ARG`を更新してreviewしたうえで、`make ml-docker-build`を
再実行する。

- `UV_VERSION`
- `GLAB_VERSION`

## 参考

- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [Compose GPU support](https://docs.docker.com/compose/how-tos/gpu-support/)
- [uv: Project environment](https://docs.astral.sh/uv/concepts/projects/config/#project-environment-path)
