# docs/operations.md・github-runner/README.md 理解度テスト

## 対象
- ファイル: `docs/operations.md`、`github-runner/README.md`
- 読者と用途: 装置を運用・保守する担当者が、WebUI サービスの常駐起動・WebUI からの更新・障害時の復旧と、CI 用 self-hosted runner の設置・保守を手順どおりに行う。

## 評価用問題
### Q1 新しく frontend 専用機を用意した。WebUI の常駐サービスを設置するために実行するコマンドを答えよ。sudo を付けるかどうかも答えること。
- 要点: `./scripts/web-service.sh install ui`
- 要点: `sudo` を付けない

### Q2 同居機で `./scripts/web-service.sh install ui` を実行したら、旧 `pcbasm-webui.service` が残っているという警告が出た。次に何をするか。
- 要点: `./scripts/web-service.sh install api`（同居機なので `install all` も可）を実行する（旧 unit は対象が api / all のときだけ削除される）

### Q3 機体の作業ツリーに未コミットの変更がある状態で、WebUI から更新を実行した。何が起きるか。
- 要点: 何もせず中断する
- 要点: 作業ツリーは変わらない

### Q4 WebUI から更新した後、新しいリビジョンでサービスが起動しなくなった。WebUI は自動で元に戻すか。戻さないなら、ssh で入ってから再起動するまでの手順を、復旧ブランチを作る前にすることも含めて順に答えよ。
- 要点: 自動ロールバックはしない
- 要点: ssh して、未保存の変更を確認・退避したうえで、更新前の commit から復旧ブランチを作る（`git switch -c ... <更新前の-sha>`）
- 要点: `uv sync --locked --inexact` の後にサービスを再起動する（`journalctl` で理由を見る）

### Q5 機体で dependency group `foo` も同期させたい。`PCBASM_API_UPDATE_UV_SYNC_ARGS="--group foo"` と設定してよいか。理由と正しい設定も答えよ。
- 要点: よくない。既定の引数を丸ごと置き換えるため `--locked --inexact` が消える
- 要点: `--locked --inexact --group foo` のように既定の 2 つを含めて設定する

### Q6 Moonraker が応答しない状態で、トップバーの「ファームウェア再起動」を押した。pcbasm のサービスは再起動されるか。
- 要点: 再起動されない
- 要点: 502 を返す

### Q7 WebUI からの更新を使うため、機体（backend だけのホスト）で sudoers を設置した。続けて unit 定義を最新にするコマンドを答えよ。
- 要点: `./scripts/web-service.sh install api`（`install` だけでも可）。`install all` ではない

### Q8 CI runner 用の Raspberry Pi を新しく用意した。runner を設置するために最初に実行するコマンドと、実行する場所・権限の条件を答えよ。
- 要点: `./github-runner/setup.sh setup`
- 要点: repository を checkout した Pi 上で、`sudo` を付けずに実行する

### Q9 CI でカメラを使うテストを走らせたい。`github-runner` user を `video` group に追加してよいか。理由も答えよ。
- 要点: よくない。`video`・`gpio`・`dialout` には追加しない
- 要点: GitHub Actions では hardware test を実行しない

### Q10 Actions Runner を version 2.330.0 に明示的に入れ替えたい。手順を順に答えよ。
- 要点: 「登録解除と削除」の手順で 3 instance すべてを削除する
- 要点: その後 `RUNNER_VERSION=2.330.0 ./github-runner/setup.sh setup` を実行する
- 要点: （削除せずに install しても既存 instance は上書きされない、に触れていればなお良い。必須ではない）

### Q11 workflow でこの runner を選ぶとき、`runs-on` に書く値を答えよ。
- 要点: `[self-hosted, linux, ARM64, rpi-ci]`

### Q12 runner の disk が逼迫してきた。uv cache を安全に整理するコマンドを答えよ。pre-commit cache も消してよいか。
- 要点: `sudo -u github-runner -H /usr/local/bin/uv cache prune`
- 要点: pre-commit cache は通常運用では削除しない（消すと次回 hook 環境を再構築する）

## 保留問題
### H1 runner instance `pcb-assembly-rpi-2` だけを登録解除して削除したい。どの directory で、何を順に実行するか。
- 要点: `/opt/actions-runner/pcb-assembly-rpi-2` を cwd にする
- 要点: `svc.sh stop` → `svc.sh uninstall` → `config.sh remove --token <remove token>` → directory を削除
- 要点: 削除後に GitHub の Settings > Actions > Runners に残骸が無いか確認する（全削除時。触れていれば可）

### H2 `API_VERSION` を上げる PR が main に入った。mDNS で見つけている機体はどう更新するか。
- 要点: WebUI からは更新しない（上げると frontend の一覧から消える）
- 要点: ssh で更新する（静的登録の機体だけは WebUI から更新できる）

### H3 WebUI から更新した後、「unit 定義が古い」という警告が画面に出た。どうするか。
- 要点: 自動では install されないので、ホストの構成に合わせて `./scripts/web-service.sh install <all|api|ui>` を手で実行する

## ラウンド記録
### R0 改稿（執筆時）
- operations.md 142 → 137 行、README 161 → 157 行
- 矛盾: operations.md の更新セットアップが全ホストで `install all` を指示していた（機体に ui unit を入れてしまう）→ ホスト構成の表を参照させた
- 矛盾: README の Upgrade は stop → install だったが、`install` は `config.sh` がある instance を skip するため入れ替わらない → 削除 → setup に変更
- 矛盾（要実機確認）: 登録解除の token を registration-token から remove-token に変更（GitHub の `config.sh remove` は removal token を取る）
- 削る: 旧 unit 掃除・更新方針の開発者向け理由（-15、600 秒タイムアウト等）を削り、運用者の行動だけ残した

### R1（operations.md 140 行、README 157 行）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1 | 正解 | - | - |
| Q2 | 正解 | - | - |
| Q3 | 正解 | - | - |
| Q4 | 不正解 | 埋没 | 「未保存の変更を確認・退避」の要点が欠落。コードブロックのコメントにしか無かったので、ブロック直前の本文に出した |
| Q5 | 正解 | - | - |
| Q6 | 正解 | - | - |
| Q7 | 正解 | - | - |
| Q8 | 正解 | - | - |
| Q9 | 不正解 | 問題不良 | 要点に理由（hardware test を Actions で実行しない）を含めたのに問題文が理由を求めていなかった。問題文に「理由も答えよ」を足した |
| Q10 | 正解 | - | - |
| Q11 | 正解 | - | - |
| Q12 | 正解 | - | - |

### R2（operations.md 134 行、README 157 行）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q3, Q5〜Q12 | 正解 | - | - |
| Q4 | 不正解 | 埋没（2 ラウンド連続） | 本文に 1 文足しても「未保存の変更を確認・退避」が拾われなかった。復旧手順がコードブロックと前置きの本文に分かれていたため、手順を番号付きリスト 1 本にまとめ、退避を独立した手順（2）にした |

### R3（operations.md 134 行、README 157 行。文書は変更なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q3, Q5〜Q12 | 正解 | - | Q10 の「Settings で確認」は余分だが誤りではない |
| Q4 | 不正解 | 問題不良 | 3 ラウンド連続で「未保存の変更の確認・退避」が欠落。R3 の文書では手順 2 として独立しており、生徒は「詳細は『復旧』の手順を参照」と要約している。問題文「どう復旧するか」が要約を許すのに、要点が全手順の列挙を求めていた。問題文を「ssh 後から再起動までの手順を、復旧ブランチを作る前にすることも含めて順に」に変えた。文書を直したくないためではなく、R2 の改稿で文書側の埋没は解消済みと判断した |

### R4（operations.md 134 行、README 157 行。文書は変更なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q12 | 正解 | - | - |

全問正解。次は保留問題 H1〜H3 で汎化確認。

### 保留問題（汎化確認、operations.md 136 行、README 157 行）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1 | 正解 | - | - |
| H2 | 正解 | - | オーケストレーターの指摘で「それ以外の機体」の指す先（静的登録以外 = mDNS）が割れうると確認。曖昧として、機体の見つけ方ごとの 2 項目に分けた（静的登録は discovery の API_VERSION フィルタを通らないことを `src/web/ui/discovery.py` で確認） |
| H3 | 正解 | - | 「<対象>」をホスト構成で選ぶ点は明示していないが、api/ui/all を挙げており許容 |

保留 3/3 正解。汎化確認を通過。
