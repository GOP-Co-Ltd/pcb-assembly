# README.md / CONTRIBUTING.md / docs/setup.md 理解度テスト

## 対象
- ファイル: `README.md`、`CONTRIBUTING.md`、`docs/setup.md`
- 読者と用途: リポジトリに初めて触れる開発者が、環境を構築し、開発コマンドとブランチ / commit 規約に従って最初の変更を出す

## 評価用問題
### Q1 新品の Raspberry Pi（OS 導入済み）で環境を作る。scripts/ のスクリプトを実行する前に、最初に何をするか。
- 要点: `git clone`（https://github.com/GOP-Co-Ltd/pcb-assembly.git）でリポジトリを取得する（スクリプトはリポジトリ内にあるため）

### Q2 `./scripts/install-softwares.sh` が正常に終わった。続けて `make setup` を実行する必要はあるか。理由も。
- 要点: 不要／スクリプトの最後で `make setup` まで実行されるため

### Q3 `install-softwares.sh`、`setup-hardware.sh`、`setup-machine-config.sh` を実行する順番を答えよ。
- 要点: install-softwares → setup-hardware → setup-machine-config

### Q4 `setup-machine-config.sh` を実行した直後に、printer.cfg を反映するために実行するコマンドは何か。
- 要点: `sudo systemctl restart klipper`

### Q5 pyproject.toml に依存を 1 つ追加した（uv.lock も更新済み）。既存の `.venv` を残したまま依存だけ反映したい。`make setup` と別のコマンドのどちらを使うか。コマンドと理由を答えよ。
- 要点: `uv sync --locked --all-groups`／`make setup` は既存の `.venv` を消して作り直すため

### Q6 2026-10-01 にドキュメントの誤字を直す作業を始める。ブランチの作り方（名前の形式と分岐元）と、コミットメッセージの形式を答えよ。
- 要点: `docs/2026-10-01/<短い説明>` の形式／`origin/main` から作る（`git fetch origin` 後に `git switch -c ... origin/main`）／コミットは英語で `docs(<scope>): <description>`

### Q7 新しいファイル `src/pcbasm/foo.py` を作ったが、まだ git add していない。この状態で `make format` を実行すると、このファイルは検査されるか。どうすべきか。
- 要点: 検査されない（git が追跡するファイルだけが対象）／`git add` してから `make format` を実行する

### Q8 自動エージェントとして無人で作業している。コミット前に `make test` を実行してよいか。代わりに何を実行するか。
- 要点: 実行しない（カメラ・ステージ・サーボ等を動かし得る）／代わりに `make test-no-hardware` を実行する（隔離 E2E は WebUI 変更時のみ必須なので要点に含めない）

### Q9 WebUI の画面テンプレートを変更した。基本検証に加えて実行するコマンドは何か。Chromium がシステムに無い場合はどうするか。
- 要点: `make test-e2e`／`make playwright-install` でブラウザを用意する

### Q10 修正中に `tests/pcbasm/geometry` だけを pytest で直接走らせたい。付けるべきマーカー指定は何か。
- 要点: `-m "not hardware and not e2e"`（少なくとも `not hardware` を含む）

### Q11 `make api-fake` で backend を起動し、ステージを動かす塗布ジョブを実行したら失敗した。故障か。理由も。
- 要点: 故障ではない／api-fake が置き換えるのはカメラだけでステージのシミュレーターではない／テスト設定は非リッスンのポート 7126 を使うため装置を使うジョブは成功しない

### Q12 日本語 docstring を 2 行にまたがる 1 文で書いた。何が起きるか、どう書くべきか。
- 要点: `make format` の docformatter が文を崩す／1 文を 1 行に書く

## 保留問題
### H1 `make api-fake` と `make ui-fake` で手動確認するとき、ブラウザで開く URL は何か。
- 要点: `http://127.0.0.1:8098`

### H2 main が進んだので作業ブランチを最新化したい。push 済みのブランチを rebase してよいか。代わりにどうするか。
- 要点: rebase しない（push 済みの履歴は書き換えない）／`git merge origin/main` で取り込み、検証する

### H3 `setup-hardware.sh` を 2 回目に実行すると、`/boot/firmware/config.txt` に手で書いた設定は消えるか。
- 要点: 消えない／スクリプトが管理する marker 内だけを置換し、それ以外は保持する

## ラウンド記録
### R0（執筆）
- 行数: README 61 → 61、CONTRIBUTING 168 → 163、setup 70 → 79（合計 299 → 303）
- setup.md: 手順を番号付きに再構成。clone を先頭に追加（欠落）。install-softwares.sh が LFS 取得と `make setup` まで行うことを明記し、重複していた「Python 環境」節を削除。GitHub Actions Runner 節を末尾へ。`Mailsail` → `Mainsail`（誤記）
- CONTRIBUTING: 開発環境の clone 手順を setup.md へ一本化し、make setup / uv sync / make clean を表に。make clean の詳細説明を削除。ブランチ名形式と分岐元、commit type とブランチ種別の対応を明記。新規ファイルは git add してから make format（欠落）。日本語 docstring 1 文 1 行（欠落）。E2E 推奨の重複行を削除。7126 は Klipper ではなく Moonraker のポート（矛盾）

### R1（README 61 / CONTRIBUTING 163 / setup 79 行。文書は無変更）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1-Q7 | 正解 | - | - |
| Q8 | 正解（要点修正後） | 問題不良 | 旧要点は `make format` / `make type` も求めていたが、問いは「`make test` の代わり」であり、基本検証の全列挙は問いの範囲外。要点を `make test-no-hardware` に絞った |
| Q9-Q12 | 正解 | - | - |

- 生徒の「読みにくかった箇所」: なし
- 所見: Q10 の対象パスが CONTRIBUTING の実行例と同じで、表のコピーで解ける。次に再利用するときは別パス（例: tests/web/api）に変え、一般規則（`-m "not hardware"` を必ず付ける）を読ませる問いにする
- 評価用が全問正解したので、次は保留問題 H1-H3 を出す

### 保留問題（汎化確認）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1 | 正解 | - | - |
| H2 | 正解 | - | - |
| H3 | 正解 | - | - |

- 生徒の確信は H3 だけ「中」。答えと根拠は要点を満たす。「読みにくかった箇所」はなし
- 結果: 評価用 12/12、保留 3/3 で完了。最終行数は README 61 / CONTRIBUTING 163 / setup 79（合計 303）
