# 開発への参加

[ドキュメント一覧](README.md) · [アーキテクチャ](docs/architecture.md)

不具合報告、使い勝手の改善、ドキュメントの修正も歓迎する。
変更は解決したい問題と確認方法を明確にし、レビューできる大きさにまとめる。

## 不具合を報告する

GitHub の Issue に、次の情報を添える。

- 期待する動作と実際の動作、再現する操作順
- 対象の画面・ジョブ、機体の種類、OS と Python のバージョン
- `git rev-parse --short HEAD` の結果と、backend / frontend が同じ版か
- エラーメッセージと関係するログ。UI の問題は画面幅やスクリーンショットも有用

ログや設定を共有するときは、認証情報、社内ホスト名、共有できない基板データを取り除く。
装置の移動が関係する問題は、実機で再現させずに分かっている範囲を記載してよい。

## 開発環境

このリポジトリの全体検証は Raspberry Pi OS の環境を前提とする。
`pcbnew` は KiCad、`picamera2` は OS パッケージから提供される。
Python は `pyproject.toml` で 3.12 以上を要求し、型チェック・整形・CI は
システム Python 3.13 を対象とする。

1. [セットアップ](docs/setup.md) に従って必要な OS パッケージと uv を用意する。
2. リポジトリを取得し、Git LFS の実体を取得する。
3. Python 環境とフックを作る。

```sh
git clone https://github.com/GOP-Co-Ltd/pcb-assembly.git
cd pcb-assembly
git lfs install
git lfs pull
make setup
```

`make setup` は既存の `.venv` を作り直し、生成ファイルを掃除する。
既存環境で依存関係だけを同期するときは `uv sync --locked --all-groups` を使う。
venv は `--system-site-packages` が必要で、uv の `python-preference = "only-system"`
を維持する。Pi 向け OS パッケージがない PC では全テストを collect できない。

## 変更と PR

```sh
git fetch origin
git switch -c fix/YYYY-MM-DD/short-description origin/main
```

ブランチの種別は `feature` / `fix` / `refactor` / `docs` / `chore`。
コミットは英語で `<type>(<scope>): <description>` とし、1 コミットを 1 つの関心事にする。
例: `fix(web): keep machine controls visible on small screens`。

1. 再現条件または成功条件を先に決める。
2. 必要な範囲を実装し、公開インターフェースを使って動作を確かめる。
3. 下記の検証を通してからコミットする。
4. PR に問題、変更後の動作、検証コマンドと結果、未確認の範囲を書く。

main に直接コミットしない。main が進んでいる場合は作業ブランチへ
`git merge origin/main` で取り込み、検証する。push 済みの履歴は書き換えない。
積み重ねる PR は直前のブランチを base に指定し、依存する PR を説明にリンクする。
main へのマージはメンテナーが判断する。

## 検証の選び方

コミット前の基本検証:

```sh
make format
make type
make test-no-hardware
```

WebUI の画面・HTTP・WebSocket・動画配信を変更したら `make test-e2e` も実行する。
Chromium がシステムにない場合は `make playwright-install` でブラウザを用意する。

| 検証                     | 用途                                              | 実行例                                                              |
| ------------------------ | ------------------------------------------------- | ------------------------------------------------------------------- |
| 非実機テスト             | 計算、ファイル、HAL fake を通す結合、SSR / JSON   | `make test-no-hardware`                                             |
| 対象を絞った非実機テスト | 修正中の短い検証                                  | `uv run pytest tests/pcbasm/geometry -m "not hardware and not e2e"` |
| E2E                      | 隔離した実サーバーを通すブラウザ・HTTP・WS・MJPEG | `make test-e2e`                                                     |
| 実機テスト               | 接続された装置での動作。担当者が現場で実行        | `make test`                                                         |

**`make test` と `make run` はカメラ・ステージ・サーボ等を動かし得る。**
無人の開発作業や自動エージェントは `make test-no-hardware` と隔離 E2E までを実行する。
pytest を直接起動する場合も `-m "not hardware"`（必要なら `and not e2e`）を付ける。

`make format` がファイルを書き換えた場合は diff を確認して再実行する。
GitHub Actions は専用 Raspberry Pi runner で pre-commit、pyright、非実機テストと
隔離 E2E を実行する。`pytest` チェックは非実機テストに続けてブラウザ・HTTP・WS・MJPEG を
検証し、`pytest-report` artifact に両スイートの JUnit レポートを残す。
WebUI の変更は、PR を出す前にも `make test-e2e` で確認する。

## テストの書き方

- `tests/pcbasm/` と `tests/web/` は `src/` のモジュール配置に合わせる。
    実サーバーが必要な検証は `tests/e2e/` に置く。
- `class TestXxx` に集約し、同じ振る舞いの入力違いは `pytest.mark.parametrize` にする。
- 公開 API の入出力と観測可能な振る舞いを検証する。private 属性や呼出順を固定しない。
- 実データと一時ファイルを優先する。fake は自前 HAL の抽象だけに使い、
    OpenCV、picamera2、Klipper RPC、`time.sleep`、内部関数をモックしない。
- 実機テストには `tests.helpers.mark_hardware` と必要な接続確認を付ける。
- getter の追試や定数の複写だけのテストを増やさない。失敗がどの回帰を示すかを説明できる内容にする。

JSON / SSR は in-process の実 backend で検証できる。
WebSocket・MJPEG・ジョブ実行は `tests/e2e/conftest.py` の `live_ui` / `live_server`
を使う。fixture が起動・停止を管理し、config とデータを一時ディレクトリへ隔離する。
mDNS の検証はランダムなサービス型とループバックだけを使う。

詳しい規約は [テスト戦略](.agents/skills/testing-strategy/SKILL.md) と
[WebUI E2E](.agents/skills/webui-e2e/SKILL.md) を参照する。

## 実機設定を使わずに画面を確認する

自動確認はまず `make test-e2e` を使う。手動でブラウザを開く場合は、
`make api-fake` と `make ui-fake` を使う。初回起動時にテスト用設定が複製される。

```sh
make api-fake
```

別ターミナルで frontend を起動し、`http://127.0.0.1:8098` を開く。

```sh
make ui-fake
```

両プロセスを `Ctrl-C` で終了する。設定は `/tmp/pcbasm-webui-fake/config`、
ジョブの状態と成果物は `/tmp/pcbasm-webui-fake` 配下に残り、再起動しても維持される。
両方の起動で mDNS とソフトウェア更新は無効になる。

別の作業用データで始める場合は、`PCBASM_API_DATA_DIR` を新しいディレクトリへ向ける。
設定はその配下の `config/` に複製される。`PCBASM_CONFIG_DIR` を明示した場合は
その設定を使うため、実機の設定ディレクトリを指定しない。

```sh
PCBASM_API_DATA_DIR="$(mktemp -d /tmp/pcbasm-dev.XXXXXX)" make api-fake
```

`api-fake` が置き換えるのはカメラだけであり、ステージのシミュレーターではない。
テスト設定は Klipper の非運用ポート 7126 を使うため、装置を使うジョブは成功しない。

## 実装の責務

計算・装置の手順は `src/pcbasm/` に置く。WebAPI router は入出力変換を行い、
frontend は backend が返した値を表示する。JS に設定の継承解決や幾何計算を複製しない。
内部属性は原則 `_` prefix とし、既存の公開インターフェースを維持する。
入力の検証は値オブジェクトの `validate() -> str | None` を基本にする。

責務ごとの配置は [アーキテクチャ](docs/architecture.md)、詳細は [AGENTS.md](AGENTS.md)
と [リファクタリング規約](.agents/skills/refactor-conventions/SKILL.md) を参照する。

## Codex の完了通知

このリポジトリで Codex CLI を起動すると、処理完了時に Windows 側へ通知する。
`.codex/config.toml` は、terminal が非フォーカスのときだけ
`agent-turn-complete` 通知を送る設定である。通知方法は自動選択され、VSCode の
integrated terminal では OSC 9 のポップアップ通知を優先し、未対応の場合は
terminal bell にフォールバックする。

設定は Codex の起動時に読み込まれるため、追加・変更後は Codex を再起動する。
通知が表示されない場合は、Windows の「設定 > システム > 通知」で VSCode の通知を
許可し、集中モードが通知を抑止していないことを確認する。
